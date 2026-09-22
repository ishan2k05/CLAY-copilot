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
exports.BackendClient = void 0;
const vscode = __importStar(require("vscode"));
class BackendClient {
    backendUrl;
    constructor() {
        const config = vscode.workspace.getConfiguration('codebaseCopilot');
        this.backendUrl = config.get('backendUrl', 'http://127.0.0.1:8765');
    }
    async checkHealth() {
        try {
            const res = await fetch(`${this.backendUrl}/health`, { method: 'GET' });
            return res.ok;
        }
        catch {
            return false;
        }
    }
    async checkOllamaStatus() {
        try {
            const res = await fetch(`${this.backendUrl}/api/ollama/status`);
            if (res.ok) {
                return await res.json();
            }
        }
        catch { }
        return { connected: false, models: [], has_gemma: false };
    }
    async updateSettings(settings) {
        try {
            const res = await fetch(`${this.backendUrl}/api/settings`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(settings)
            });
            if (res.ok) {
                return await res.json();
            }
        }
        catch { }
        return { status: 'error' };
    }
    async streamChat(query, activeFileContext, onToken, onComplete, onError, workspacePathOverride, securityMode) {
        try {
            const workspaceFolder = workspacePathOverride || vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
            const payload = {
                query,
                context_file: activeFileContext,
                workspace_path: workspaceFolder,
                stream: true
            };
            if (securityMode) {
                payload.security_mode = securityMode;
            }
            const response = await fetch(`${this.backendUrl}/api/chat`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            if (!response.ok) {
                const errorText = await response.text();
                onError(`Backend error (${response.status}): ${errorText}`);
                return;
            }
            if (!response.body) {
                onError('No response stream received from backend.');
                return;
            }
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let accumulatedDiff = '';
            let citations = [];
            while (true) {
                const { done, value } = await reader.read();
                if (done)
                    break;
                const text = decoder.decode(value, { stream: true });
                const lines = text.split('\n');
                for (const line of lines) {
                    if (line.startsWith('data: ')) {
                        const dataStr = line.replace('data: ', '').trim();
                        if (dataStr === '[DONE]')
                            continue;
                        try {
                            const parsed = JSON.parse(dataStr);
                            if (parsed.token) {
                                onToken(parsed.token);
                            }
                            if (parsed.diff) {
                                accumulatedDiff = parsed.diff;
                            }
                            if (parsed.citations) {
                                citations = parsed.citations;
                            }
                        }
                        catch {
                            onToken(dataStr);
                        }
                    }
                }
            }
            onComplete({ citations, diff: accumulatedDiff });
        }
        catch (err) {
            onError(err.message || 'Failed to connect to Codebase Copilot backend.');
        }
    }
    async triggerIndexing(workspacePath) {
        const response = await fetch(`${this.backendUrl}/api/index/scan`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ workspace_path: workspacePath })
        });
        if (!response.ok) {
            throw new Error(`Indexing failed: ${response.statusText}`);
        }
        return await response.json();
    }
    async getMermaidGraph() {
        try {
            const res = await fetch(`${this.backendUrl}/api/graph/mermaid`);
            if (res.ok) {
                const data = await res.json();
                return data.mermaid || '';
            }
        }
        catch { }
        return '';
    }
    async createPlan(goal, workspacePath) {
        const res = await fetch(`${this.backendUrl}/api/agent/plan`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ goal, workspace_path: workspacePath })
        });
        if (!res.ok) {
            throw new Error(`Planning failed: ${res.statusText}`);
        }
        return await res.json();
    }
    async applyPatch(filePath, diff) {
        const res = await fetch(`${this.backendUrl}/api/agent/apply-patch`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ file_path: filePath, diff })
        });
        if (!res.ok) {
            const err = await res.text();
            throw new Error(err);
        }
        return await res.json();
    }
    async getAgentTask() {
        try {
            const res = await fetch(`${this.backendUrl}/api/agent/task`);
            if (res.ok) {
                return await res.json();
            }
        }
        catch { }
        return null;
    }
    async reviewChanges(taskId, decision) {
        const res = await fetch(`${this.backendUrl}/api/agent/review`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ task_id: taskId, decision })
        });
        return res.ok;
    }
}
exports.BackendClient = BackendClient;
//# sourceMappingURL=backendClient.js.map