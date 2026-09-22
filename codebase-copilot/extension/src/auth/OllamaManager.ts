import * as vscode from 'vscode';

/**
 * Manages connectivity and model verification with the local Ollama daemon
 * running gemma4:31b-cloud authenticated via the user's Ollama account.
 */
export class OllamaManager {
    public static readonly DEFAULT_OLLAMA_URL = 'http://127.0.0.1:11434';
    public static readonly TARGET_MODEL = 'gemma4:31b-cloud';

    constructor(private readonly context: vscode.ExtensionContext) {}

    /**
     * Verifies whether the local Ollama daemon is reachable.
     */
    async isDaemonReachable(): Promise<boolean> {
        try {
            const res = await fetch(`${OllamaManager.DEFAULT_OLLAMA_URL}/api/tags`, { method: 'GET' });
            return res.ok;
        } catch {
            return false;
        }
    }

    /**
     * Lists registered models in the local Ollama instance.
     */
    async getAvailableModels(): Promise<string[]> {
        try {
            const res = await fetch(`${OllamaManager.DEFAULT_OLLAMA_URL}/api/tags`);
            if (res.ok) {
                const data = await res.json() as any;
                return (data.models || []).map((m: any) => m.name);
            }
        } catch {}
        return [];
    }
}
