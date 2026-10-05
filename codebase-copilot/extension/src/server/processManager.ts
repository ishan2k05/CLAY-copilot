import * as cp from 'child_process';
import * as net from 'net';
import * as path from 'path';
import * as vscode from 'vscode';

export class ServerProcessManager {
    private process: cp.ChildProcess | null = null;
    private outputChannel: vscode.OutputChannel;
    private activePort: number = 8765;
    private activeUrl: string = 'http://127.0.0.1:8765';

    constructor() {
        this.outputChannel = vscode.window.createOutputChannel('CLAY Copilot Server');
    }

    public getActivePort(): number {
        return this.activePort;
    }

    public getActiveUrl(): string {
        return this.activeUrl;
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
     * Attempts to start the FastAPI backend sidecar process automatically,
     * dynamically binding to an available port if the default is in use.
     */
    async startServer(extensionPath: string, configuredUrl?: string): Promise<boolean> {
        let targetPort = 8765;
        if (configuredUrl) {
            try {
                const parsed = new URL(configuredUrl);
                if (parsed.port) {
                    targetPort = parseInt(parsed.port, 10);
                }
            } catch {}
        }

        // 1. Check if server is already running on the target port
        try {
            const res = await fetch(`http://127.0.0.1:${targetPort}/health`);
            if (res.ok) {
                this.activePort = targetPort;
                this.activeUrl = `http://127.0.0.1:${targetPort}`;
                this.outputChannel.appendLine(`[Sidecar] Backend is already running on port ${targetPort}.`);
                return true;
            }
        } catch {}

        // 2. Resolve available port if target port is occupied
        let chosenPort = targetPort;
        const available = await this.isPortAvailable(targetPort);
        if (!available) {
            this.outputChannel.appendLine(`[Sidecar] Port ${targetPort} is occupied. Searching for open port...`);
            chosenPort = await this.findAvailablePort(targetPort + 1, targetPort + 20);
        }

        this.activePort = chosenPort;
        this.activeUrl = `http://127.0.0.1:${chosenPort}`;

        const backendDir = path.resolve(extensionPath, '..', 'backend');
        this.outputChannel.appendLine(`[Sidecar] Launching Python backend from ${backendDir} on port ${chosenPort}...`);

        try {
            const pythonCmd = process.platform === 'win32' ? 'python' : 'python3';
            this.process = cp.spawn(
                pythonCmd,
                ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', chosenPort.toString()],
                { cwd: backendDir }
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
     * Cleanly terminates the sidecar on VS Code shutdown.
     */
    stopServer() {
        if (this.process) {
            this.outputChannel.appendLine('[Sidecar] Terminating Python backend process...');
            this.process.kill('SIGTERM');
            this.process = null;
        }
    }
}
