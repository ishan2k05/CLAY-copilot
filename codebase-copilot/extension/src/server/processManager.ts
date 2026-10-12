import * as cp from 'child_process';
import * as net from 'net';
import * as path from 'path';
import * as os from 'os';
import * as fs from 'fs';
import * as crypto from 'crypto';
import * as vscode from 'vscode';

export class ServerProcessManager {
    private process: cp.ChildProcess | null = null;
    private outputChannel: vscode.OutputChannel;
    private activePort: number = 8765;
    private activeUrl: string = 'http://127.0.0.1:8765';
    private heartbeatInterval: NodeJS.Timeout | null = null;
    private isRestarting: boolean = false;
    private sessionToken: string = '';
    private context: vscode.ExtensionContext | null = null;

    constructor() {
        this.outputChannel = vscode.window.createOutputChannel('CLAY Copilot Server');
    }

    public setContext(context: vscode.ExtensionContext): void {
        this.context = context;
    }

    public getActivePort(): number {
        return this.activePort;
    }

    public getActiveUrl(): string {
        return this.activeUrl;
    }

    public getSessionToken(): string {
        if (!this.sessionToken) {
            this.sessionToken = this.resolveOrCreateSessionToken();
        }
        return this.sessionToken;
    }

    private resolveOrCreateSessionToken(): string {
        // 1. Check environment variable
        if (process.env.CLAY_SESSION_TOKEN && process.env.CLAY_SESSION_TOKEN.trim()) {
            return process.env.CLAY_SESSION_TOKEN.trim();
        }

        // 2. Check ~/.clay/auth_token.json
        const tokenDir = path.join(os.homedir(), '.clay');
        const tokenFile = path.join(tokenDir, 'auth_token.json');
        try {
            if (fs.existsSync(tokenFile)) {
                const data = JSON.parse(fs.readFileSync(tokenFile, 'utf-8'));
                if (data && data.token && typeof data.token === 'string') {
                    return data.token.trim();
                }
            }
        } catch {}

        // 3. Generate a 256-bit random hex token and persist it
        const token = crypto.randomBytes(32).toString('hex');
        try {
            if (!fs.existsSync(tokenDir)) {
                fs.mkdirSync(tokenDir, { recursive: true });
            }
            fs.writeFileSync(tokenFile, JSON.stringify({ token }), { encoding: 'utf-8', mode: 0o600 });
        } catch {}

        return token;
    }

    private async isPortAvailable(port: number): Promise<boolean> {
        return new Promise((resolve) => {
            const server = net.createServer();
            server.once('error', () => resolve(false));
            server.once('listening', () => {
                server.close(() => resolve(true));
            });
            server.listen(port, '127.0.0.1');
        });
    }

    private async findAvailablePort(startPort: number = 8765, maxPort: number = 8785): Promise<number> {
        for (let p = startPort; p <= maxPort; p++) {
            if (await this.isPortAvailable(p)) {
                return p;
            }
        }
        return startPort;
    }

    /**
     * Kills a process and all child processes recursively.
     * On Windows, uses taskkill /T /F to ensure no orphaned Python workers remain.
     */
    public killProcessTree(pid: number): boolean {
        if (!pid) return false;
        try {
            if (process.platform === 'win32') {
                cp.execSync(`taskkill /pid ${pid} /T /F`, { stdio: 'ignore', timeout: 5000 });
                return true;
            } else {
                try {
                    process.kill(-pid, 'SIGKILL');
                } catch {
                    process.kill(pid, 'SIGKILL');
                }
                return true;
            }
        } catch {
            return false;
        }
    }

    /**
     * Reclaims a port by terminating any lingering stale process holding it.
     */
    public async reclaimPort(port: number): Promise<boolean> {
        if (await this.isPortAvailable(port)) {
            return true;
        }

        this.outputChannel.appendLine(`[Sidecar] Attempting to reclaim occupied port ${port}...`);
        try {
            if (process.platform === 'win32') {
                const stdout = cp.execSync(`netstat -ano -p tcp | findstr :${port}`, { encoding: 'utf-8', timeout: 3000 });
                const lines = stdout.split('\n');
                for (const line of lines) {
                    const match = line.trim().match(/LISTENING\s+(\d+)$/i);
                    if (match && match[1]) {
                        const holdingPid = parseInt(match[1], 10);
                        if (holdingPid > 4) { // Don't touch System (PID 4)
                            this.outputChannel.appendLine(`[Sidecar] Terminating stale process on port ${port} (PID ${holdingPid})...`);
                            this.killProcessTree(holdingPid);
                        }
                    }
                }
            } else {
                cp.execSync(`fuser -k ${port}/tcp 2>/dev/null || true`, { stdio: 'ignore', timeout: 3000 });
            }
        } catch {}

        // Wait briefly for socket to release
        await new Promise((r) => setTimeout(r, 600));
        return await this.isPortAvailable(port);
    }

    private findPythonExecutable(backendDir: string): string {
        const isWin = process.platform === 'win32';
        const candidates = [
            path.join(backendDir, '.venv', isWin ? 'Scripts/python.exe' : 'bin/python'),
            path.join(backendDir, 'venv', isWin ? 'Scripts/python.exe' : 'bin/python'),
            path.join(backendDir, '..', '.venv', isWin ? 'Scripts/python.exe' : 'bin/python'),
            path.join(backendDir, '..', 'venv', isWin ? 'Scripts/python.exe' : 'bin/python')
        ];
        for (const cand of candidates) {
            try {
                if (fs.existsSync(cand)) {
                    return cand;
                }
            } catch {}
        }
        return isWin ? 'python' : 'python3';
    }

    private getServerExecutable(extensionPath: string): { cmd: string; args: string[]; cwd: string; isBinary: boolean } {
        const isWin = process.platform === 'win32';
        const binaryName = isWin ? 'clay-server.exe' : 'clay-server';
        const bundledBinary = path.join(extensionPath, 'bin', binaryName);

        if (fs.existsSync(bundledBinary)) {
            if (!isWin) {
                try { fs.chmodSync(bundledBinary, 0o755); } catch {}
            }
            this.outputChannel.appendLine(`[Sidecar] Found bundled production binary: ${bundledBinary}`);
            return {
                cmd: bundledBinary,
                args: [],
                cwd: path.dirname(bundledBinary),
                isBinary: true
            };
        }

        // Developer mode: fallback to local Python in backend directory
        const backendDir = path.resolve(extensionPath, '..', 'backend');
        const pythonCmd = this.findPythonExecutable(backendDir);
        this.outputChannel.appendLine(`[Sidecar] Dev Mode: using local Python: ${pythonCmd}`);
        return {
            cmd: pythonCmd,
            args: ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1'],
            cwd: backendDir,
            isBinary: false
        };
    }

    /**
     * Attempts to start the FastAPI backend sidecar process automatically,
     * dynamically binding to an available port if the default is in use.
     */
    async startServer(extensionPath: string, configuredUrl?: string, context?: vscode.ExtensionContext): Promise<boolean> {
        if (context) {
            this.context = context;
        }

        let targetPort = 8765;
        if (configuredUrl) {
            try {
                const parsed = new URL(configuredUrl);
                if (parsed.port) {
                    targetPort = parseInt(parsed.port, 10);
                }
            } catch {}
        }

        // 1. Check if an active responsive server is already running on the target port
        try {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 1200);
            const res = await fetch(`http://127.0.0.1:${targetPort}/health`, { signal: controller.signal });
            clearTimeout(timeoutId);
            if (res.ok) {
                this.activePort = targetPort;
                this.activeUrl = `http://127.0.0.1:${targetPort}`;
                this.outputChannel.appendLine(`[Sidecar] Backend is already running and responsive on port ${targetPort}.`);
                return true;
            }
        } catch {}

        // 2. If port is occupied by an unresponsive/zombie process, attempt to reclaim it
        let chosenPort = targetPort;
        const reclaimed = await this.reclaimPort(targetPort);
        if (!reclaimed) {
            this.outputChannel.appendLine(`[Sidecar] Port ${targetPort} could not be reclaimed. Searching for open port...`);
            chosenPort = await this.findAvailablePort(targetPort + 1, targetPort + 20);
        }

        this.activePort = chosenPort;
        this.activeUrl = `http://127.0.0.1:${chosenPort}`;

        const globalStorage = this.context?.globalStorageUri.fsPath || path.join(os.homedir(), '.clay');
        const activeWorkspace = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || '';
        const workspaceStorage = this.context?.storageUri?.fsPath || (activeWorkspace ? path.join(activeWorkspace, '.clay') : globalStorage);

        try { fs.mkdirSync(globalStorage, { recursive: true }); } catch {}
        try { fs.mkdirSync(workspaceStorage, { recursive: true }); } catch {}

        this.outputChannel.appendLine(`[Sidecar] Storage configured: Global=${globalStorage}, Workspace=${workspaceStorage}`);

        try {
            const env: Record<string, string | undefined> = {
                ...process.env,
                CLAY_SESSION_TOKEN: this.getSessionToken(),
                CLAY_GLOBAL_DATA_DIR: globalStorage,
                CLAY_WORKSPACE_STORAGE_DIR: workspaceStorage,
                CLAY_PORT: chosenPort.toString(),
                CLAY_HOST: '127.0.0.1'
            };
            if (activeWorkspace) {
                env.WORKSPACE_ROOT = activeWorkspace;
            }

            const serverExec = this.getServerExecutable(extensionPath);
            const spawnArgs = [...serverExec.args, '--port', chosenPort.toString()];
            this.outputChannel.appendLine(`[Sidecar] Spawning backend from ${serverExec.cwd}: ${serverExec.cmd} ${spawnArgs.join(' ')}`);

            this.process = cp.spawn(
                serverExec.cmd,
                spawnArgs,
                { cwd: serverExec.cwd, detached: process.platform !== 'win32', env }
            );

            this.process.stdout?.on('data', (data) => {
                this.outputChannel.append(`[Server] ${data.toString()}`);
            });

            this.process.stderr?.on('data', (data) => {
                this.outputChannel.append(`[Server Error] ${data.toString()}`);
            });

            this.process.on('close', (code) => {
                this.outputChannel.appendLine(`[Sidecar] Server process exited with code ${code}`);
                this.process = null;
            });

            return true;
        } catch (err: any) {
            this.outputChannel.appendLine(`[Sidecar] Failed to start backend: ${err.message}`);
            return false;
        }
    }

    /**
     * Cleanly terminates the sidecar on VS Code shutdown or restart.
     * Issues graceful shutdown HTTP request first, then terminates process tree.
     */
    async stopServer(): Promise<void> {
        this.stopHeartbeatWatchdog();

        // 1. Send graceful shutdown request to flush SQLite WAL buffers
        try {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 600);
            await fetch(`http://127.0.0.1:${this.activePort}/api/system/shutdown`, {
                method: 'POST',
                headers: {
                    'X-Clay-Auth-Token': this.getSessionToken()
                },
                signal: controller.signal
            }).catch(() => {});
            clearTimeout(timeoutId);
        } catch {}

        // 2. Kill the process tree explicitly
        if (this.process && this.process.pid) {
            this.outputChannel.appendLine(`[Sidecar] Terminating backend process tree (PID ${this.process.pid})...`);
            this.killProcessTree(this.process.pid);
            this.process = null;
        }

        // 3. Ensure port is liberated
        await this.reclaimPort(this.activePort);
    }

    /**
     * Restarts the backend server cleanly with in-process restart first, then process respawn fallback.
     */
    async restartServer(extensionPath: string, configuredUrl?: string): Promise<boolean> {
        if (this.isRestarting) return false;
        this.isRestarting = true;
        this.outputChannel.appendLine('[Sidecar] Initiating backend restart sequence...');

        try {
            // 1. Attempt graceful in-process backend restart if server is running
            let inProcessRestartTriggered = false;
            try {
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), 1500);
                const res = await fetch(`http://127.0.0.1:${this.activePort}/api/system/restart`, {
                    method: 'POST',
                    headers: {
                        'X-Clay-Auth-Token': this.getSessionToken()
                    },
                    signal: controller.signal
                });
                clearTimeout(timeoutId);
                if (res.ok) {
                    inProcessRestartTriggered = true;
                    this.outputChannel.appendLine(`[Sidecar] Triggered graceful in-process restart on port ${this.activePort}. Waiting for server to cycle...`);
                }
            } catch {}

            if (inProcessRestartTriggered) {
                // Poll health check for up to 10 seconds for the backend to cycle back online
                for (let i = 0; i < 20; i++) {
                    await new Promise((r) => setTimeout(r, 500));
                    try {
                        const res = await fetch(`http://127.0.0.1:${this.activePort}/health`);
                        if (res.ok) {
                            this.outputChannel.appendLine(`[Sidecar] Backend restarted successfully and verified healthy on port ${this.activePort}.`);
                            return true;
                        }
                    } catch {}
                }
                this.outputChannel.appendLine(`[Sidecar] In-process restart timed out. Falling back to process respawn...`);
            }

            // 2. Fallback: Stop existing process and spawn fresh instance
            await this.stopServer();
            await new Promise((r) => setTimeout(r, 800));
            const started = await this.startServer(extensionPath, configuredUrl, this.context || undefined);

            // Wait up to 10 seconds for health check
            if (started) {
                for (let i = 0; i < 20; i++) {
                    await new Promise((r) => setTimeout(r, 500));
                    try {
                        const res = await fetch(`http://127.0.0.1:${this.activePort}/health`);
                        if (res.ok) {
                            this.outputChannel.appendLine(`[Sidecar] Backend restarted successfully and verified healthy on port ${this.activePort}.`);
                            return true;
                        }
                    } catch {}
                }
            }
            return started;
        } finally {
            this.isRestarting = false;
        }
    }

    /**
     * Starts background health check watchdog that detects unresponsiveness.
     */
    startHeartbeatWatchdog(onUnresponsive?: () => void) {
        this.stopHeartbeatWatchdog();
        let consecutiveFailures = 0;

        this.heartbeatInterval = setInterval(async () => {
            if (this.isRestarting) return;
            try {
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), 2000);
                const res = await fetch(`http://127.0.0.1:${this.activePort}/health`, { signal: controller.signal });
                clearTimeout(timeoutId);

                if (res.ok) {
                    consecutiveFailures = 0;
                } else {
                    consecutiveFailures++;
                }
            } catch {
                consecutiveFailures++;
            }

            if (consecutiveFailures >= 3) {
                this.outputChannel.appendLine(`[Sidecar] Warning: Backend server on port ${this.activePort} is unresponsive.`);
                if (onUnresponsive) {
                    onUnresponsive();
                }
            }
        }, 15000);
    }

    /**
     * Stops the background health check watchdog.
     */
    stopHeartbeatWatchdog() {
        if (this.heartbeatInterval) {
            clearInterval(this.heartbeatInterval);
            this.heartbeatInterval = null;
        }
    }
}
