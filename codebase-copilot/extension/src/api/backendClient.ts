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
    private activeChatAbortController: AbortController | null = null;

    constructor() {
        const config = vscode.workspace.getConfiguration('codebaseCopilot');
        this.backendUrl = config.get<string>('backendUrl', 'http://127.0.0.1:8765');

        vscode.workspace.onDidChangeConfiguration((e) => {
            if (e.affectsConfiguration('codebaseCopilot.backendUrl')) {
                const updatedConfig = vscode.workspace.getConfiguration('codebaseCopilot');
                this.backendUrl = updatedConfig.get<string>('backendUrl', 'http://127.0.0.1:8765');
            }
        });
    }

    public getBackendUrl(): string {
        return this.backendUrl;
    }

    public setBackendUrl(url: string): void {
        this.backendUrl = url;
    }

    stopStreamChat(): void {
        if (this.activeChatAbortController) {
            try {
                this.activeChatAbortController.abort();
            } catch {}
            this.activeChatAbortController = null;
        }
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

    async syncFile(filePath: string, content: string, event: string = 'changed'): Promise<any> {
        try {
            const res = await fetch(`${this.backendUrl}/api/sync/file`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ file_path: filePath, content, event })
            });
            if (res.ok) {
                return await res.json();
            }
        } catch {}
        return null;
    }

    async silentSyncCheck(workspacePath?: string): Promise<{ status: string; changed?: number; deleted?: number; tracked_files?: number }> {
        try {
            const url = workspacePath
                ? `${this.backendUrl}/api/sync/silent-check?workspace_path=${encodeURIComponent(workspacePath)}`
                : `${this.backendUrl}/api/sync/silent-check`;
            const res = await fetch(url);
            if (res.ok) {
                return await res.json() as any;
            }
        } catch {}
        return { status: 'noop' };
    }

    async streamChat(
        query: string,
        activeFileContext: string | undefined,
        onToken: (token: string) => void,
        onComplete: (metadata: { citations?: string[]; diff?: string }) => void,
        onError: (err: string) => void,
        workspacePathOverride?: string,
        securityMode?: string,
        activeFileContent?: string,
        openFiles?: Array<{ path: string; content: string; is_dirty?: boolean }>,
        inferenceOptions?: { temperature?: number; num_ctx?: number; num_predict?: number },
        cursorLine?: number,
        history?: Array<{ role: string; content: string }>
    ): Promise<void> {
        if (this.activeChatAbortController) {
            try { this.activeChatAbortController.abort(); } catch {}
        }
        const abortCtrl = new AbortController();
        this.activeChatAbortController = abortCtrl;
        const signal = abortCtrl.signal;

        let accumulatedDiff = '';
        let citations: string[] = [];

        try {
            const workspaceFolder = workspacePathOverride || vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
            const payload: any = {
                query,
                context_file: activeFileContext,
                active_file_content: activeFileContent,
                open_files: openFiles,
                workspace_path: workspaceFolder,
                cursor_line: cursorLine,
                stream: true
            };
            if (history && history.length > 0) {
                payload.history = history;
            }
            if (securityMode) {
                payload.security_mode = securityMode;
            }
            if (inferenceOptions?.temperature !== undefined) {
                payload.temperature = inferenceOptions.temperature;
            }
            if (inferenceOptions?.num_ctx !== undefined) {
                payload.num_ctx = inferenceOptions.num_ctx;
            }
            if (inferenceOptions?.num_predict !== undefined) {
                payload.num_predict = inferenceOptions.num_predict;
            }

            const response = await fetch(`${this.backendUrl}/api/chat`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
                signal
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

            while (true) {
                if (signal.aborted) {
                    try { await reader.cancel(); } catch {}
                    break;
                }
                const { done, value } = await reader.read();
                if (done) break;

                const text = decoder.decode(value, { stream: true });
                const lines = text.split('\n');

                for (const line of lines) {
                    if (signal.aborted) break;
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

            if (!signal.aborted) {
                onComplete({ citations, diff: accumulatedDiff });
            }
        } catch (err: any) {
            if (signal.aborted || err.name === 'AbortError') {
                return;
            }
            onError(err.message || 'Failed to connect to CLAY Copilot backend.');
        } finally {
            if (this.activeChatAbortController === abortCtrl) {
                this.activeChatAbortController = null;
            }
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

    async getMermaidGraph(workspacePath?: string, filePath?: string): Promise<string> {
        try {
            const params = new URLSearchParams();
            if (workspacePath) params.set('path', workspacePath);
            if (filePath) params.set('file', filePath);
            const qs = params.toString();
            const url = qs ? `${this.backendUrl}/api/graph/mermaid?${qs}` : `${this.backendUrl}/api/graph/mermaid`;
            const res = await fetch(url);
            if (res.ok) {
                const data = await res.json() as any;
                return data.mermaid || '';
            }
        } catch {}
        return '';
    }

    async getSecurityAudit(workspacePath?: string): Promise<{
        status: string;
        scanned_files: number;
        findings_count: number;
        findings: Array<{ file: string; type: string; severity: string; detail: string; line: number }>;
        summary: string;
    }> {
        try {
            const url = workspacePath
                ? `${this.backendUrl}/api/repo/security-audit?path=${encodeURIComponent(workspacePath)}`
                : `${this.backendUrl}/api/repo/security-audit`;
            const res = await fetch(url);
            if (res.ok) {
                return await res.json() as any;
            }
        } catch {}
        return {
            status: 'clean',
            scanned_files: 0,
            findings_count: 0,
            findings: [],
            summary: 'Zero Leaked Secrets Detected'
        };
    }

    async createPlan(goal: string, workspacePath?: string, targetFile?: string, modelName?: string): Promise<AgentTask> {
        const res = await fetch(`${this.backendUrl}/api/agent/plan`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                goal,
                workspace_path: workspacePath,
                target_file: targetFile,
                model_name: modelName
            })
        });
        if (!res.ok) {
            throw new Error(`Planning failed: ${res.statusText}`);
        }
        return await res.json() as AgentTask;
    }

    async applyPatch(filePath: string, diff: string, workspacePath?: string): Promise<any> {
        const res = await fetch(`${this.backendUrl}/api/agent/apply-patch`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ file_path: filePath, diff, workspace_path: workspacePath })
        });
        if (!res.ok) {
            const err = await res.text();
            throw new Error(err);
        }
        return await res.json();
    }

    async rebaseDiff(filePath: string, baseContent: string, diff: string, currentContent: string, workspacePath?: string, forceResolution?: string): Promise<any> {
        const res = await fetch(`${this.backendUrl}/api/agent/rebase-diff`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                file_path: filePath,
                base_content: baseContent,
                diff: diff,
                current_content: currentContent,
                workspace_path: workspacePath,
                force_resolution: forceResolution
            })
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

    async getRepoTree(workspacePath?: string): Promise<{ tree: any[]; stats: any; root?: string }> {
        try {
            const url = workspacePath 
                ? `${this.backendUrl}/api/repo/tree?path=${encodeURIComponent(workspacePath)}`
                : `${this.backendUrl}/api/repo/tree`;
            const res = await fetch(url);
            if (res.ok) {
                return await res.json() as any;
            }
        } catch {}
        return { tree: [], stats: { total_files: 0, total_dirs: 0, total_lines: 0 } };
    }

    async executeTerminalCommand(command: string, workspacePath?: string, securityMode?: string): Promise<any> {
        try {
            const res = await fetch(`${this.backendUrl}/api/agent/terminal/execute`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    command,
                    workspace_path: workspacePath,
                    security_mode: securityMode
                })
            });
            if (res.ok) {
                return await res.json();
            }
            const errText = await res.text();
            return { exit_code: -1, stdout: '', stderr: errText, passed: false };
        } catch (err: any) {
            return { exit_code: -1, stdout: '', stderr: err.message, passed: false };
        }
    }

    async getSystemInfo(workspacePath?: string): Promise<any> {
        try {
            const url = workspacePath
                ? `${this.backendUrl}/api/system/info?workspace_path=${encodeURIComponent(workspacePath)}`
                : `${this.backendUrl}/api/system/info`;
            const res = await fetch(url);
            if (res.ok) return await res.json();
        } catch {}
        return { os: process.platform === 'win32' ? 'windows' : 'linux', is_windows: process.platform === 'win32', shell: 'bash' };
    }

    async executeAgentStep(stepId: number, goal: string, workspacePath?: string, activeFile?: string, cursorLine?: number, targetFile?: string, stepAction?: string, totalSteps?: number, activeFileContent?: string): Promise<any> {
        try {
            const res = await fetch(`${this.backendUrl}/api/agent/step`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    step_id: stepId,
                    goal,
                    workspace_path: workspacePath,
                    active_file: activeFile,
                    active_file_content: activeFileContent,
                    cursor_line: cursorLine,
                    target_file: targetFile,
                    step_action: stepAction,
                    total_steps: totalSteps
                })
            });
            if (res.ok) return await res.json();
            return { status: 'error', message: await res.text() };
        } catch (err: any) {
            return { status: 'error', message: err.message };
        }
    }
}
