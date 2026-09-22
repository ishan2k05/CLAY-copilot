"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.OllamaManager = void 0;
/**
 * Manages connectivity and model verification with the local Ollama daemon
 * running gemma4:31b-cloud authenticated via the user's Ollama account.
 */
class OllamaManager {
    context;
    static DEFAULT_OLLAMA_URL = 'http://127.0.0.1:11434';
    static TARGET_MODEL = 'gemma4:31b-cloud';
    constructor(context) {
        this.context = context;
    }
    /**
     * Verifies whether the local Ollama daemon is reachable.
     */
    async isDaemonReachable() {
        try {
            const res = await fetch(`${OllamaManager.DEFAULT_OLLAMA_URL}/api/tags`, { method: 'GET' });
            return res.ok;
        }
        catch {
            return false;
        }
    }
    /**
     * Lists registered models in the local Ollama instance.
     */
    async getAvailableModels() {
        try {
            const res = await fetch(`${OllamaManager.DEFAULT_OLLAMA_URL}/api/tags`);
            if (res.ok) {
                const data = await res.json();
                return (data.models || []).map((m) => m.name);
            }
        }
        catch { }
        return [];
    }
}
exports.OllamaManager = OllamaManager;
//# sourceMappingURL=OllamaManager.js.map