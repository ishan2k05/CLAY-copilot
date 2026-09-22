import * as vscode from 'vscode';

export interface ChatMessage {
    role: 'user' | 'assistant' | 'system';
    content: string;
    timestamp?: string;
    citations?: string[];
    diff?: string;
}

export interface IndexStatus {
    status: 'idle' | 'indexing' | 'synced' | 'error';
    filesCount: number;
    symbolsCount: number;
    relationshipsCount?: number;
    chunksCount?: number;
    activeBranch?: string;
}

export interface AgentTask {
    id: string;
    title: string;
    priority: string;
    mode: 'autonomous' | 'supervised';
    model: string;
    totalSteps: number;
    completedSteps: number;
    steps: Array<{
        id: number;
        title: string;
        status: 'done' | 'active' | 'queued';
        detail?: string;
    }>;
    liveLog?: string;
    stagedChanges: Array<{
        file: string;
        added: number;
        deleted: number;
        status: 'MODIFIED' | 'GENERATING' | 'ADDED';
    }>;
}

export class BackendClient {
    private backendUrl: string;

    constructor() {
        const config = vscode.workspace.getConfiguration('codebaseCopilot');
        this.backendUrl = config.get<string>('backendUrl', 'http://127.0.0.1:8765');
    }

    async checkHealth(): Promise<boolean> {
        try {
            const res = await fetch(`${this.backendUrl}/health`, { method: 'GET' });
            return res.ok;
        } catch {
            return false;
        }
    }

    async checkOllamaStatus(): Promise<{
        connected: boolean;
        models: string[];
        has_gemma: boolean;
        active_model?: string;
        host?: string;
        latency_ms?: number;
    }> {
        try {
            const res = await fetch(`${this.backendUrl}/api/ollama/status`);
            if (res.ok) {
                return await res.json() as any;
            }
        } catch {}
        return { connected: false, models: [], has_gemma: false };
    }

    async updateSettings(settings: { model?: string; host?: string; privacy_filter?: boolean }): Promise<any> {
        try {
            const res = await fetch(`${this.backendUrl}/api/settings`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(settings)
            });
            if (res.ok) {
                return await res.json();
            }
        } catch {}
        return { status: 'error' };
    }

    async streamChat(
        query: string,
        activeFileContext: string | undefined,
        onToken: (token: string) => void,
        onComplete: (metadata: { citations?: string[]; diff?: string }) => void,
        onError: (err: string) => void,
        workspacePathOverride?: string,
        securityMode?: string
    ): Promise<void> {
        try {
            const workspaceFolder = workspacePathOverride || vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
            const payload: any = {
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

            const reader = (response.body as any).getReader();
            const decoder = new TextDecoder();
            let accumulatedDiff = '';
            let citations: string[] = [];

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                const text = decoder.decode(value, { stream: true });
                const lines = text.split('\n');

                for (const line of lines) {
                    if (line.startsWith('data: ')) {
                        const dataStr = line.replace('data: ', '').trim();
                        if (dataStr === '[DONE]') continue;

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
                        } catch {
                            onToken(dataStr);
                        }
                    }
                }
            }

            onComplete({ citations, diff: accumulatedDiff });
        } catch (err: any) {
            onError(err.message || 'Failed to connect to Codebase Copilot backend.');
        }
    }

    async triggerIndexing(workspacePath: string): Promise<IndexStatus> {
        const response = await fetch(`${this.backendUrl}/api/index/scan`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ workspace_path: workspacePath })
        });
        if (!response.ok) {
            throw new Error(`Indexing failed: ${response.statusText}`);
        }
        return await response.json() as IndexStatus;
    }

    async getMermaidGraph(): Promise<string> {
        try {
            const res = await fetch(`${this.backendUrl}/api/graph/mermaid`);
            if (res.ok) {
                const data = await res.json() as any;
                return data.mermaid || '';
            }
        } catch {}
        return '';
    }

    async createPlan(goal: string, workspacePath?: string): Promise<AgentTask> {
        const res = await fetch(`${this.backendUrl}/api/agent/plan`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ goal, workspace_path: workspacePath })
        });
        if (!res.ok) {
            throw new Error(`Planning failed: ${res.statusText}`);
        }
        return await res.json() as AgentTask;
    }

    async applyPatch(filePath: string, diff: string): Promise<any> {
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

    async getAgentTask(): Promise<AgentTask | null> {
        try {
            const res = await fetch(`${this.backendUrl}/api/agent/task`);
            if (res.ok) {
                return await res.json() as AgentTask;
            }
        } catch {}
        return null;
    }

    async reviewChanges(taskId: string, decision: 'accept' | 'reject'): Promise<boolean> {
        const res = await fetch(`${this.backendUrl}/api/agent/review`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ task_id: taskId, decision })
        });
        return res.ok;
    }
}
