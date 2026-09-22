import * as cp from 'child_process';
import * as path from 'path';
import * as vscode from 'vscode';

export class ServerProcessManager {
    private process: cp.ChildProcess | null = null;
    private outputChannel: vscode.OutputChannel;

    constructor() {
        this.outputChannel = vscode.window.createOutputChannel('Codebase Copilot Server');
    }

    /**
     * Attempts to start the FastAPI backend sidecar process automatically.
     */
    async startServer(extensionPath: string): Promise<boolean> {
        // First check if server is already running on port 8765
        try {
            const res = await fetch('http://127.0.0.1:8765/health');
            if (res.ok) {
                this.outputChannel.appendLine('[Sidecar] Backend is already running on port 8765.');
                return true;
            }
        } catch {}

        const backendDir = path.resolve(extensionPath, '..', 'backend');
        this.outputChannel.appendLine(`[Sidecar] Launching Python backend from ${backendDir}...`);

        try {
            this.process = cp.spawn(
                'python3',
                ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8765'],
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
