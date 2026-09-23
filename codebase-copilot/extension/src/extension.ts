import * as vscode from 'vscode';
import { BackendClient } from './api/backendClient';
import { SidebarProvider } from './webview/SidebarProvider';
import { ServerProcessManager } from './server/processManager';

let serverManager: ServerProcessManager | null = null;

export async function activate(context: vscode.ExtensionContext) {
    // 1. Auto-launch or connect to local Python FastAPI backend sidecar
    serverManager = new ServerProcessManager();
    await serverManager.startServer(context.extensionPath);

    const backendClient = new BackendClient();

    // 2. Register Webview View Provider (Primary Sidebar)
    const sidebarProvider = new SidebarProvider(context.extensionUri, backendClient, context);
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider(SidebarProvider.viewType, sidebarProvider)
    );

    // 3. Command: Check Ollama Status
    context.subscriptions.push(
        vscode.commands.registerCommand('codebaseCopilot.checkOllama', async () => {
            const status = await backendClient.checkOllamaStatus();
            if (status.connected) {
                vscode.window.showInformationMessage(
                    `Codebase Copilot: Ollama connected! Active model: gemma4:31b-cloud.`
                );
            } else {
                vscode.window.showWarningMessage(
                    'Codebase Copilot: Ollama daemon not detected. Run "ollama serve" in your terminal.'
                );
            }
        })
    );

    // 4. Command: Analyze Repository (Trigger structural indexing)
    context.subscriptions.push(
        vscode.commands.registerCommand('codebaseCopilot.analyzeRepo', async () => {
            const workspaceFolders = vscode.workspace.workspaceFolders;
            if (!workspaceFolders || workspaceFolders.length === 0) {
                vscode.window.showWarningMessage('Codebase Copilot: Open a workspace folder to index.');
                return;
            }

            const rootPath = workspaceFolders[0].uri.fsPath;
            vscode.window.withProgress(
                {
                    location: vscode.ProgressLocation.Notification,
                    title: 'Codebase Copilot: Scanning & Indexing AST...',
                    cancellable: false
                },
                async () => {
                    try {
                        const status = await backendClient.triggerIndexing(rootPath);
                        vscode.window.showInformationMessage(
                            `Codebase Copilot: Successfully indexed ${status.filesCount} files and ${status.symbolsCount} symbols into code graph.`
                        );
                    } catch (err: any) {
                        vscode.window.showErrorMessage(`Codebase Copilot Indexing failed: ${err.message}`);
                    }
                }
            );
        })
    );

    // 5. Command: Explain Selected Code
    context.subscriptions.push(
        vscode.commands.registerCommand('codebaseCopilot.explainSelection', async () => {
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
        })
    );

    // 6. Command: Fix Current Error (Extracts Language Diagnostics)
    context.subscriptions.push(
        vscode.commands.registerCommand('codebaseCopilot.fixCurrentError', async () => {
            const editor = vscode.window.activeTextEditor;
            if (!editor) return;

            const diagnostics = vscode.languages.getDiagnostics(editor.document.uri);
            const errors = diagnostics.filter(d => d.severity === vscode.DiagnosticSeverity.Error);

            if (errors.length === 0) {
                vscode.window.showInformationMessage('Codebase Copilot: No active errors detected in current file.');
                return;
            }

            const firstError = errors[0];
            vscode.window.showInformationMessage(
                `Codebase Copilot: Diagnosing "${firstError.message}" with gemma4:31b-cloud...`
            );
            await vscode.commands.executeCommand('codebaseCopilot.sidebarView.focus');
        })
    );

    // 7. Real-Time Document Synchronization & Hot Reload
    let docChangeTimeout: NodeJS.Timeout | null = null;
    context.subscriptions.push(
        vscode.workspace.onDidChangeTextDocument((e) => {
            if (e.document.uri.scheme !== 'file') return;
            const fileName = e.document.fileName;
            if (fileName.includes('node_modules') || fileName.includes('.git')) return;

            if (docChangeTimeout) clearTimeout(docChangeTimeout);
            docChangeTimeout = setTimeout(() => {
                backendClient.syncFile(fileName, e.document.getText(), 'changed');
            }, 350);
        })
    );

    context.subscriptions.push(
        vscode.workspace.onDidSaveTextDocument((doc) => {
            if (doc.uri.scheme !== 'file') return;
            if (doc.fileName.includes('node_modules') || doc.fileName.includes('.git') || doc.fileName.includes('__pycache__')) return;
            backendClient.syncFile(doc.fileName, doc.getText(), 'saved');
        })
    );

    // 8. Handle file creations and deletions
    context.subscriptions.push(
        vscode.workspace.onDidCreateFiles((e) => {
            for (const fileUri of e.files) {
                if (fileUri.scheme === 'file') {
                    vscode.workspace.openTextDocument(fileUri).then((doc) => {
                        backendClient.syncFile(doc.fileName, doc.getText(), 'created');
                    });
                }
            }
        })
    );

    context.subscriptions.push(
        vscode.workspace.onDidDeleteFiles((e) => {
            for (const fileUri of e.files) {
                if (fileUri.scheme === 'file') {
                    backendClient.syncFile(fileUri.fsPath, '', 'deleted');
                }
            }
        })
    );

    // 9. OS-level File System Watcher for external editors (VS Code, external editors, git checkout)
    const fileWatcher = vscode.workspace.createFileSystemWatcher('**/*.{py,js,ts,tsx,jsx,json,md,html,css}');
    fileWatcher.onDidChange(async (uri) => {
        if (uri.fsPath.includes('node_modules') || uri.fsPath.includes('.git') || uri.fsPath.includes('__pycache__')) return;
        try {
            const data = await vscode.workspace.fs.readFile(uri);
            const content = new TextDecoder().decode(data);
            backendClient.syncFile(uri.fsPath, content, 'changed');
        } catch {}
    });
    fileWatcher.onDidCreate(async (uri) => {
        if (uri.fsPath.includes('node_modules') || uri.fsPath.includes('.git') || uri.fsPath.includes('__pycache__')) return;
        try {
            const data = await vscode.workspace.fs.readFile(uri);
            const content = new TextDecoder().decode(data);
            backendClient.syncFile(uri.fsPath, content, 'created');
        } catch {}
    });
    fileWatcher.onDidDelete((uri) => {
        if (uri.fsPath.includes('node_modules') || uri.fsPath.includes('.git') || uri.fsPath.includes('__pycache__')) return;
        backendClient.syncFile(uri.fsPath, '', 'deleted');
    });
    context.subscriptions.push(fileWatcher);

    // 10. Background Silent Sync Heartbeat (Lightweight microsecond stat check, 0 LLM tokens)
    const silentSyncInterval = setInterval(async () => {
        const rootPath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
        if (rootPath) {
            await backendClient.silentSyncCheck(rootPath);
        }
    }, 4000);
    context.subscriptions.push({ dispose: () => clearInterval(silentSyncInterval) });

    // Also trigger silent sync when user focuses back into VS Code
    context.subscriptions.push(
        vscode.window.onDidChangeWindowState((state) => {
            if (state.focused) {
                const rootPath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
                if (rootPath) {
                    backendClient.silentSyncCheck(rootPath);
                }
            }
        })
    );
}

export function deactivate() {
    if (serverManager) {
        serverManager.stopServer();
        serverManager = null;
    }
}
