import * as vscode from 'vscode';
import * as path from 'path';
import { BackendClient } from './api/backendClient';
import { SidebarProvider } from './webview/SidebarProvider';
import { ServerProcessManager } from './server/processManager';

let serverManager: ServerProcessManager | null = null;

export async function activate(context: vscode.ExtensionContext) {
    // 1. Auto-launch or connect to local Python FastAPI backend sidecar
    serverManager = new ServerProcessManager();
    const config = vscode.workspace.getConfiguration('codebaseCopilot');
    const configuredUrl = config.get<string>('backendUrl');
    await serverManager.startServer(context.extensionPath, configuredUrl, context);

    const backendClient = new BackendClient();
    backendClient.setSessionToken(serverManager.getSessionToken());
    if (serverManager.getActiveUrl()) {
        backendClient.setBackendUrl(serverManager.getActiveUrl());
    }

    // 2. Register Webview View Provider (Primary Sidebar) with retainContextWhenHidden
    // Keeps webview execution context alive when minimized, collapsed, or when switching sidebar views
    const sidebarProvider = new SidebarProvider(context.extensionUri, backendClient, context, serverManager);
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider(
            SidebarProvider.viewType,
            sidebarProvider,
            {
                webviewOptions: {
                    retainContextWhenHidden: true
                }
            }
        )
    );

    // 2b. Register Command: Restart Backend Server
    context.subscriptions.push(
        vscode.commands.registerCommand('codebaseCopilot.restartBackend', async () => {
            if (!serverManager) return;
            await vscode.window.withProgress({
                location: vscode.ProgressLocation.Notification,
                title: "CLAY Copilot: Restarting backend server...",
                cancellable: false
            }, async () => {
                const restarted = await serverManager!.restartServer(context.extensionPath, configuredUrl);
                if (restarted) {
                    backendClient.setBackendUrl(serverManager!.getActiveUrl());
                    sidebarProvider.notifyBackendRestarted(serverManager!.getActiveUrl());
                    vscode.window.showInformationMessage('CLAY Copilot: Backend restarted successfully.');
                } else {
                    vscode.window.showErrorMessage('CLAY Copilot: Failed to restart backend server. Check output channel.');
                }
            });
        })
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('clay.restartBackend', async () => {
            return vscode.commands.executeCommand('codebaseCopilot.restartBackend');
        })
    );

    // 2c. Start background heartbeat watchdog with auto-recovery prompt
    serverManager.startHeartbeatWatchdog(async () => {
        const choice = await vscode.window.showWarningMessage(
            'CLAY Copilot: Backend server is unresponsive.',
            'Restart Backend'
        );
        if (choice === 'Restart Backend') {
            vscode.commands.executeCommand('codebaseCopilot.restartBackend');
        }
    });

    // 3. Command: Check Ollama Status
    context.subscriptions.push(
        vscode.commands.registerCommand('codebaseCopilot.checkOllama', async () => {
            const status = await backendClient.checkOllamaStatus();
            if (status.connected) {
                vscode.window.showInformationMessage(
                    `CLAY Copilot: Ollama connected! Active model: gemma4:31b-cloud.`
                );
            } else {
                vscode.window.showWarningMessage(
                    'CLAY Copilot: Ollama daemon not detected. Run "ollama serve" in your terminal.'
                );
            }
        })
    );

    // 4. Command: Analyze Repository (Trigger structural indexing)
    context.subscriptions.push(
        vscode.commands.registerCommand('codebaseCopilot.analyzeRepo', async () => {
            const workspaceFolders = vscode.workspace.workspaceFolders;
            if (!workspaceFolders || workspaceFolders.length === 0) {
                vscode.window.showWarningMessage('CLAY Copilot: Open a workspace folder to index.');
                return;
            }

            const rootPath = workspaceFolders[0].uri.fsPath;
            vscode.window.withProgress(
                {
                    location: vscode.ProgressLocation.Notification,
                    title: 'CLAY Copilot: Scanning & Indexing AST...',
                    cancellable: false
                },
                async () => {
                    try {
                        const status = await backendClient.triggerIndexing(rootPath);
                        vscode.window.showInformationMessage(
                            `CLAY Copilot: Successfully indexed ${status.filesCount} files and ${status.symbolsCount} symbols into code graph.`
                        );
                    } catch (err: any) {
                        vscode.window.showErrorMessage(`CLAY Copilot Indexing failed: ${err.message}`);
                    }
                }
            );
        })
    );

    // 2d. Command: Ask Question (Focus Sidebar Chat)
    context.subscriptions.push(
        vscode.commands.registerCommand('codebaseCopilot.ask', async () => {
            await vscode.commands.executeCommand('codebaseCopilot.sidebarView.focus');
            const editor = vscode.window.activeTextEditor;
            const selection = editor ? editor.document.getText(editor.selection) : '';

            if (selection && selection.trim()) {
                const fileName = path.basename(editor!.document.fileName);
                const startLine = editor!.selection.start.line + 1;
                const endLine = editor!.selection.end.line + 1;
                const lang = editor!.document.languageId || '';
                const prompt = `Regarding this code in \`${fileName}\` (L${startLine}-L${endLine}):\n\`\`\`${lang}\n${selection.trim()}\n\`\`\`\n`;
                sidebarProvider.focusChatWithPrompt(prompt, false);
            } else {
                sidebarProvider.focusChatWithPrompt('', false);
            }
        })
    );

    // 5. Command: Explain Selected Code
    context.subscriptions.push(
        vscode.commands.registerCommand('codebaseCopilot.explainSelection', async () => {
            const editor = vscode.window.activeTextEditor;
            if (!editor) {
                vscode.window.showInformationMessage('CLAY Copilot: Select code in an open editor first.');
                return;
            }
            const selection = editor.document.getText(editor.selection);
            if (!selection || !selection.trim()) {
                vscode.window.showInformationMessage('CLAY Copilot: No code selected.');
                return;
            }

            const fileName = path.basename(editor.document.fileName);
            const startLine = editor.selection.start.line + 1;
            const endLine = editor.selection.end.line + 1;
            const lang = editor.document.languageId || '';

            await vscode.commands.executeCommand('codebaseCopilot.sidebarView.focus');
            sidebarProvider.focusChatWithPrompt(
                `Explain this code from \`${fileName}\` (L${startLine}-L${endLine}):\n\`\`\`${lang}\n${selection.trim()}\n\`\`\`\nBreak down its architecture, behavior, edge cases, and any potential optimizations.`,
                true
            );
        })
    );

    // 6. Command: Fix Current Error (Extracts Language Diagnostics)
    context.subscriptions.push(
        vscode.commands.registerCommand('codebaseCopilot.fixCurrentError', async () => {
            const editor = vscode.window.activeTextEditor;
            if (!editor) return;

            const diagnostics = vscode.languages.getDiagnostics(editor.document.uri);
            const errors = diagnostics.filter(d => d.severity === vscode.DiagnosticSeverity.Error || d.severity === vscode.DiagnosticSeverity.Warning);

            if (errors.length === 0) {
                vscode.window.showInformationMessage('CLAY Copilot: No active errors or warnings detected in current file.');
                return;
            }

            // Target the error closest to the user's cursor, or the first error in the file
            const targetError = errors.find(e => e.range.contains(editor.selection.active)) || errors[0];
            const errorLine = targetError.range.start.line;
            const lineContent = editor.document.lineAt(errorLine).text;
            const fileName = path.basename(editor.document.fileName);
            const lang = editor.document.languageId || '';

            await vscode.commands.executeCommand('codebaseCopilot.sidebarView.focus');
            sidebarProvider.focusChatWithPrompt(
                `Diagnose and fix this error in \`${fileName}\` at line ${errorLine + 1}:\n` +
                `Error Message: "${targetError.message}"\n` +
                `Offending Code:\n\`\`\`${lang}\n${lineContent.trim()}\n\`\`\`\n` +
                `Please analyze the root cause and provide the minimal surgical patch to resolve it.`,
                true
            );
        })
    );

    // 6b. Quick Fix CodeActionProvider for inline lightbulb & Quick Fix (Ctrl+.)
    context.subscriptions.push(
        vscode.languages.registerCodeActionsProvider(
            { scheme: 'file' },
            {
                provideCodeActions(document, range, context) {
                    if (!context.diagnostics || context.diagnostics.length === 0) {
                        return [];
                    }
                    const action = new vscode.CodeAction('Fix with CLAY Copilot', vscode.CodeActionKind.QuickFix);
                    action.command = {
                        command: 'codebaseCopilot.fixCurrentError',
                        title: 'Fix with CLAY Copilot'
                    };
                    action.isPreferred = true;
                    return [action];
                }
            },
            {
                providedCodeActionKinds: [vscode.CodeActionKind.QuickFix]
            }
        )
    );

    // 7. Real-Time Document Synchronization & Hot Reload
    const isFileInActiveWorkspace = (filePath: string): boolean => {
        const targetWs = context.globalState.get<string>('lastRepoPath') || vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
        if (!targetWs || !filePath) return true;
        try {
            const normFile = path.resolve(filePath).toLowerCase();
            const normWs = path.resolve(targetWs).toLowerCase();
            return normFile.startsWith(normWs + path.sep) || normFile === normWs;
        } catch {
            return true;
        }
    };

    let docChangeTimeout: NodeJS.Timeout | null = null;
    context.subscriptions.push(
        vscode.workspace.onDidChangeTextDocument((e) => {
            if (e.document.uri.scheme !== 'file') return;
            const fileName = e.document.fileName;
            if (fileName.includes('node_modules') || fileName.includes('.git') || !isFileInActiveWorkspace(fileName)) return;

            if (docChangeTimeout) clearTimeout(docChangeTimeout);
            docChangeTimeout = setTimeout(() => {
                backendClient.syncFile(fileName, e.document.getText(), 'changed');
            }, 350);
        })
    );

    context.subscriptions.push(
        vscode.workspace.onDidSaveTextDocument((doc) => {
            if (doc.uri.scheme !== 'file') return;
            if (doc.fileName.includes('node_modules') || doc.fileName.includes('.git') || doc.fileName.includes('__pycache__') || !isFileInActiveWorkspace(doc.fileName)) return;
            backendClient.syncFile(doc.fileName, doc.getText(), 'saved');
        })
    );

    // 8. Handle file creations and deletions
    context.subscriptions.push(
        vscode.workspace.onDidCreateFiles((e) => {
            for (const fileUri of e.files) {
                if (fileUri.scheme === 'file' && isFileInActiveWorkspace(fileUri.fsPath)) {
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
                if (fileUri.scheme === 'file' && isFileInActiveWorkspace(fileUri.fsPath)) {
                    backendClient.syncFile(fileUri.fsPath, '', 'deleted');
                }
            }
        })
    );

    // 9. OS-level File System Watcher for external editors (VS Code, external editors, git checkout)
    const fileWatcher = vscode.workspace.createFileSystemWatcher('**/*.{py,js,ts,tsx,jsx,json,md,html,css}');
    fileWatcher.onDidChange(async (uri) => {
        if (uri.fsPath.includes('node_modules') || uri.fsPath.includes('.git') || uri.fsPath.includes('__pycache__') || !isFileInActiveWorkspace(uri.fsPath)) return;
        try {
            const data = await vscode.workspace.fs.readFile(uri);
            const content = new TextDecoder().decode(data);
            backendClient.syncFile(uri.fsPath, content, 'changed');
        } catch {}
    });
    fileWatcher.onDidCreate(async (uri) => {
        if (uri.fsPath.includes('node_modules') || uri.fsPath.includes('.git') || uri.fsPath.includes('__pycache__') || !isFileInActiveWorkspace(uri.fsPath)) return;
        try {
            const data = await vscode.workspace.fs.readFile(uri);
            const content = new TextDecoder().decode(data);
            backendClient.syncFile(uri.fsPath, content, 'created');
        } catch {}
    });
    fileWatcher.onDidDelete((uri) => {
        if (uri.fsPath.includes('node_modules') || uri.fsPath.includes('.git') || uri.fsPath.includes('__pycache__') || !isFileInActiveWorkspace(uri.fsPath)) return;
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
