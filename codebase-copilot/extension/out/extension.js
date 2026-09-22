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
exports.activate = activate;
exports.deactivate = deactivate;
const vscode = __importStar(require("vscode"));
const backendClient_1 = require("./api/backendClient");
const SidebarProvider_1 = require("./webview/SidebarProvider");
const processManager_1 = require("./server/processManager");
let serverManager = null;
async function activate(context) {
    // 1. Auto-launch or connect to local Python FastAPI backend sidecar
    serverManager = new processManager_1.ServerProcessManager();
    await serverManager.startServer(context.extensionPath);
    const backendClient = new backendClient_1.BackendClient();
    // 2. Register Webview View Provider (Primary Sidebar)
    const sidebarProvider = new SidebarProvider_1.SidebarProvider(context.extensionUri, backendClient);
    context.subscriptions.push(vscode.window.registerWebviewViewProvider(SidebarProvider_1.SidebarProvider.viewType, sidebarProvider));
    // 3. Command: Check Ollama Status
    context.subscriptions.push(vscode.commands.registerCommand('codebaseCopilot.checkOllama', async () => {
        const status = await backendClient.checkOllamaStatus();
        if (status.connected) {
            vscode.window.showInformationMessage(`Codebase Copilot: Ollama connected! Active model: gemma4:31b-cloud.`);
        }
        else {
            vscode.window.showWarningMessage('Codebase Copilot: Ollama daemon not detected. Run "ollama serve" in your terminal.');
        }
    }));
    // 4. Command: Analyze Repository (Trigger structural indexing)
    context.subscriptions.push(vscode.commands.registerCommand('codebaseCopilot.analyzeRepo', async () => {
        const workspaceFolders = vscode.workspace.workspaceFolders;
        if (!workspaceFolders || workspaceFolders.length === 0) {
            vscode.window.showWarningMessage('Codebase Copilot: Open a workspace folder to index.');
            return;
        }
        const rootPath = workspaceFolders[0].uri.fsPath;
        vscode.window.withProgress({
            location: vscode.ProgressLocation.Notification,
            title: 'Codebase Copilot: Scanning & Indexing AST...',
            cancellable: false
        }, async () => {
            try {
                const status = await backendClient.triggerIndexing(rootPath);
                vscode.window.showInformationMessage(`Codebase Copilot: Successfully indexed ${status.filesCount} files and ${status.symbolsCount} symbols into code graph.`);
            }
            catch (err) {
                vscode.window.showErrorMessage(`Codebase Copilot Indexing failed: ${err.message}`);
            }
        });
    }));
    // 5. Command: Explain Selected Code
    context.subscriptions.push(vscode.commands.registerCommand('codebaseCopilot.explainSelection', async () => {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            vscode.window.showInformationMessage('Codebase Copilot: Select code in an open editor first.');
            return;
        }
        const selection = editor.document.getText(editor.selection);
        if (!selection) {
            vscode.window.showInformationMessage('Codebase Copilot: No code selected.');
            return;
        }
        // Focus sidebar
        await vscode.commands.executeCommand('codebaseCopilot.sidebarView.focus');
    }));
    // 6. Command: Fix Current Error (Extracts Language Diagnostics)
    context.subscriptions.push(vscode.commands.registerCommand('codebaseCopilot.fixCurrentError', async () => {
        const editor = vscode.window.activeTextEditor;
        if (!editor)
            return;
        const diagnostics = vscode.languages.getDiagnostics(editor.document.uri);
        const errors = diagnostics.filter(d => d.severity === vscode.DiagnosticSeverity.Error);
        if (errors.length === 0) {
            vscode.window.showInformationMessage('Codebase Copilot: No active errors detected in current file.');
            return;
        }
        const firstError = errors[0];
        vscode.window.showInformationMessage(`Codebase Copilot: Diagnosing "${firstError.message}" with gemma4:31b-cloud...`);
        await vscode.commands.executeCommand('codebaseCopilot.sidebarView.focus');
    }));
}
function deactivate() {
    if (serverManager) {
        serverManager.stopServer();
        serverManager = null;
    }
}
//# sourceMappingURL=extension.js.map