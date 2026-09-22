"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.ServerProcessManager = void 0;
const cp = __importStar(require("child_process"));
const path = __importStar(require("path"));
const vscode = __importStar(require("vscode"));
class ServerProcessManager {
    process = null;
    outputChannel;
    constructor() {
        this.outputChannel = vscode.window.createOutputChannel('Codebase Copilot Server');
    }
    /**
     * Attempts to start the FastAPI backend sidecar process automatically.
     */
    async startServer(extensionPath) {
        // First check if server is already running on port 8765
        try {
            const res = await fetch('http://127.0.0.1:8765/health');
            if (res.ok) {
                this.outputChannel.appendLine('[Sidecar] Backend is already running on port 8765.');
                return true;
            }
        }
        catch { }
        const backendDir = path.resolve(extensionPath, '..', 'backend');
        this.outputChannel.appendLine(`[Sidecar] Launching Python backend from ${backendDir}...`);
        try {
            this.process = cp.spawn('python3', ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8765'], { cwd: backendDir });
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
        }
        catch (err) {
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
exports.ServerProcessManager = ServerProcessManager;
//# sourceMappingURL=processManager.js.map