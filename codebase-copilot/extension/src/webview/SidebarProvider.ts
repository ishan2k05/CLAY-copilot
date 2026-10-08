import * as vscode from 'vscode';
import * as cp from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import { BackendClient } from '../api/backendClient';
import { ServerProcessManager } from '../server/processManager';

export class SidebarProvider implements vscode.WebviewViewProvider {
    public static readonly viewType = 'codebaseCopilot.sidebarView';
    private _view?: vscode.WebviewView;
    private _lastActiveDocument?: vscode.TextDocument;

    constructor(
        private readonly _extensionUri: vscode.Uri,
        private readonly _backendClient: BackendClient,
        private readonly _context?: vscode.ExtensionContext,
        private readonly _processManager?: ServerProcessManager
    ) { }

    public notifyBackendRestarted(url: string) {
        const token = this._processManager?.getSessionToken() || this._backendClient.getSessionToken();
        this._view?.webview.postMessage({
            command: 'backendRestarted',
            url: url,
            sessionToken: token
        });
    }

    public resolveWebviewView(
        webviewView: vscode.WebviewView,
        _context: vscode.WebviewViewResolveContext,
        _token: vscode.CancellationToken
    ) {
        this._view = webviewView;

        if (vscode.window.activeTextEditor?.document?.uri.scheme === 'file') {
            this._lastActiveDocument = vscode.window.activeTextEditor.document;
        }

        vscode.window.onDidChangeActiveTextEditor((editor) => {
            if (editor?.document && editor.document.uri.scheme === 'file') {
                this._lastActiveDocument = editor.document;
                const cursorLine = editor.selection ? editor.selection.active.line + 1 : undefined;
                this._view?.webview.postMessage({
                    command: 'activeFileChanged',
                    file: editor.document.uri.fsPath,
                    line: cursorLine,
                    content: editor.document.getText()
                });

                // Auto-sync workspace if active editor belongs to an open workspace folder
                const wsFolder = vscode.workspace.getWorkspaceFolder(editor.document.uri);
                if (wsFolder && wsFolder.uri.fsPath) {
                    const currentFolder = wsFolder.uri.fsPath;
                    const lastKnown = this._context?.globalState.get<string>('lastRepoPath');
                    if (currentFolder !== lastKnown) {
                        this._context?.globalState.update('lastRepoPath', currentFolder);
                        this._view?.webview.postMessage({
                            command: 'repoPathSelected',
                            path: currentFolder
                        });
                    }
                }
            }
        });

        vscode.workspace.onDidChangeWorkspaceFolders(() => {
            const currentFolder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
            if (currentFolder) {
                this._context?.globalState.update('lastRepoPath', currentFolder);
                this._view?.webview.postMessage({
                    command: 'repoPathSelected',
                    path: currentFolder
                });
            }
        });

        vscode.window.onDidChangeTextEditorSelection((event) => {
            if (event.textEditor?.document && event.textEditor.document.uri.scheme === 'file') {
                const cursorLine = event.selections[0] ? event.selections[0].active.line + 1 : undefined;
                this._view?.webview.postMessage({
                    command: 'cursorPositionChanged',
                    file: event.textEditor.document.uri.fsPath,
                    line: cursorLine,
                    content: event.textEditor.document.getText()
                });
            }
        });

        vscode.workspace.onDidChangeTextDocument((event) => {
            if (event.document.uri.scheme === 'file') {
                this._view?.webview.postMessage({
                    command: 'activeFileContentChanged',
                    file: event.document.uri.fsPath,
                    content: event.document.getText(),
                    isDirty: event.document.isDirty
                });
            }
        });

        vscode.workspace.onDidChangeConfiguration((event) => {
            if (event.affectsConfiguration('codebaseCopilot.backendUrl')) {
                const newUrl = this._backendClient.getBackendUrl();
                const token = this._processManager?.getSessionToken() || this._backendClient.getSessionToken();
                this._view?.webview.postMessage({
                    command: 'setBackendUrl',
                    url: newUrl,
                    sessionToken: token
                });
            }
        });

        // Domain 2: Incremental single-file AST reindex on file save
        vscode.workspace.onDidSaveTextDocument(async (document) => {
            if (document.uri.scheme === 'file') {
                const filePath = document.uri.fsPath;
                const activeWorkspace = this._context?.globalState.get<string>('lastRepoPath') || vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
                if (activeWorkspace && !this._isFileInWorkspace(filePath, activeWorkspace)) {
                    return;
                }
                const content = document.getText();
                const syncRes = await this._backendClient.syncFile(filePath, content, 'changed');
                if (syncRes && syncRes.status === 'synced') {
                    this._view?.webview.postMessage({
                        command: 'fileSavedAndSynced',
                        file: filePath,
                        filename: path.basename(filePath),
                        lines: syncRes.lines !== undefined ? syncRes.lines : document.lineCount
                    });
                }
            }
        });

        // Domain 2: Purge deleted file symbols from SQLite AST graph & vector index
        vscode.workspace.onDidDeleteFiles(async (event) => {
            const activeWorkspace = this._context?.globalState.get<string>('lastRepoPath') || vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
            for (const fileUri of event.files) {
                if (fileUri.scheme === 'file') {
                    if (activeWorkspace && !this._isFileInWorkspace(fileUri.fsPath, activeWorkspace)) {
                        continue;
                    }
                    await this._backendClient.syncFile(fileUri.fsPath, '', 'deleted');
                    this._view?.webview.postMessage({
                        command: 'fileDeletedAndPurged',
                        file: fileUri.fsPath,
                        filename: path.basename(fileUri.fsPath)
                    });
                }
            }
        });

        webviewView.onDidChangeVisibility(() => {
            if (webviewView.visible) {
                // When un-minimized / restored to visibility, update active file context if needed
                if (this._lastActiveDocument?.uri.fsPath) {
                    const cursorLine = vscode.window.activeTextEditor?.selection ? vscode.window.activeTextEditor.selection.active.line + 1 : undefined;
                    this._view?.webview.postMessage({
                        command: 'activeFileChanged',
                        file: this._lastActiveDocument.uri.fsPath,
                        line: cursorLine,
                        content: this._lastActiveDocument.getText()
                    });
                }
            }
        });

        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [
                vscode.Uri.joinPath(this._extensionUri, 'resources'),
                vscode.Uri.joinPath(this._extensionUri, 'src', 'webview', 'media'),
                vscode.Uri.joinPath(this._extensionUri, 'out')
            ]
        };

        webviewView.webview.html = this._getHtmlForWebview(webviewView.webview);

        // Notify Webview of system info and current workspace path on startup
        const activeFolder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
        const savedRepo = this._context?.globalState.get<string>('lastRepoPath');
        // Always prioritize the active workspace folder currently open in VS Code!
        const initialRepo = activeFolder || savedRepo || '';

        const sessionToken = this._processManager?.getSessionToken() || this._backendClient.getSessionToken();
        setTimeout(() => {
            if (sessionToken) {
                this._view?.webview.postMessage({
                    command: 'setSessionToken',
                    token: sessionToken
                });
            }
            if (initialRepo) {
                this._view?.webview.postMessage({
                    command: 'repoPathSelected',
                    path: initialRepo
                });
            } else if (savedRepo === '') {
                this._view?.webview.postMessage({
                    command: 'repoPathCleared'
                });
            }
            if (this._lastActiveDocument?.uri.fsPath) {
                const cursorLine = vscode.window.activeTextEditor?.selection ? vscode.window.activeTextEditor.selection.active.line + 1 : undefined;
                this._view?.webview.postMessage({
                    command: 'activeFileChanged',
                    file: this._lastActiveDocument.uri.fsPath,
                    line: cursorLine
                });
            }

            // Domain 5: Load encrypted secret from OS Keychain (SecretStorage)
            const SECRET_KEY_NAME = 'clay_cloud_api_key';
            if (this._context) {
                Promise.resolve(this._context.secrets.get(SECRET_KEY_NAME)).then(async (storedKey) => {
                    const hasKey = !!(storedKey && storedKey.trim());
                    const cleanKey = hasKey ? storedKey!.trim() : '';
                    const maskedKey = cleanKey.length > 8
                        ? `${cleanKey.slice(0, 4)}••••••••${cleanKey.slice(-4)}`
                        : (cleanKey ? '••••••••' : '');

                    if (hasKey) {
                        await this._backendClient.updateSettings({ api_key: cleanKey } as any);
                    }

                    this._view?.webview.postMessage({
                        command: 'cloudApiKeyLoaded',
                        hasKey: hasKey,
                        maskedKey: maskedKey
                    });
                }).catch(() => { });
            }
        }, 300);

        // Handle messages from Webview
        webviewView.webview.onDidReceiveMessage(async (data) => {
            switch (data.command) {
                case 'userQuery': {
                    await this._handleUserQuery(
                        data.text,
                        data.workspace_path,
                        data.security_mode,
                        data.temperature !== undefined ? {
                            temperature: data.temperature,
                            num_ctx: data.num_ctx,
                            num_predict: data.num_predict
                        } : undefined,
                        data.history,
                        data.pinned_mentions
                    );
                    break;
                }
                case 'stopChat': {
                    this._backendClient.stopStreamChat();
                    this._view?.webview.postMessage({ command: 'streamStopped' });
                    break;
                }
                case 'restartBackendServer': {
                    vscode.commands.executeCommand('codebaseCopilot.restartBackend');
                    break;
                }
                case 'abortChatGeneration': {
                    this._backendClient.stopStreamChat();
                    try {
                        await this._backendClient.fetchWithAuth(`${this._backendClient.getBackendUrl()}/api/chat/abort`, { method: 'POST' }).catch(() => {});
                    } catch {}
                    break;
                }
                case 'abortAgentExecution': {
                    try {
                        await this._backendClient.abortAgentExecution();
                    } catch {}
                    break;
                }
                case 'abortTerminalCommand': {
                    try {
                        await this._backendClient.abortTerminalCommand(data?.execution_id);
                    } catch {}
                    break;
                }
                case 'clearChatContext': {
                    vscode.window.setStatusBarMessage('CLAY: Chat context cleared', 2500);
                    break;
                }
                case 'createAgentPlan':
                case 'planAgentExecution': {
                    const workspaceFolder = data.workspace_path || vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
                    try {
                        const plan = await this._backendClient.createPlan(data.goal, workspaceFolder, data.target_file, data.model_name);
                        this._view?.webview.postMessage({ command: 'agentPlanResult', plan });
                    } catch (err: any) {
                        vscode.window.showErrorMessage(`Planning failed: ${err.message}`);
                    }
                    break;
                }
                case 'scanWorkspace': {
                    const workspaceFolder = (data.path || '').trim();
                    if (!workspaceFolder || workspaceFolder === '.') {
                        vscode.window.showWarningMessage('CLAY Copilot: Please select a repository.');
                        this._view?.webview.postMessage({
                            command: 'scanError',
                            error: 'Please select a repository'
                        });
                        break;
                    }
                    const selectedFile = data.file || data.filePath;
                    try {
                        await vscode.workspace.saveAll(false);
                        const status = await this._backendClient.triggerIndexing(workspaceFolder);
                        const graphData = await this._backendClient.getMermaidGraph(workspaceFolder, selectedFile);
                        const security = await this._backendClient.getSecurityAudit(workspaceFolder);
                        this._view?.webview.postMessage({
                            command: 'scanResult',
                            status,
                            mermaid: graphData.mermaid,
                            nodes_meta: graphData.nodes_meta,
                            security,
                            file: selectedFile
                        });
                        vscode.window.showInformationMessage(`Indexed ${status.filesCount} files and ${status.symbolsCount} symbols.`);
                    } catch (err: any) {
                        vscode.window.showErrorMessage(`Scanning failed: ${err.message}`);
                        this._view?.webview.postMessage({ command: 'scanError', error: err.message });
                    }
                    break;
                }
                case 'getMermaidGraph': {
                    const workspaceFolder = (data.path || '').trim();
                    if (!workspaceFolder || workspaceFolder === '.') {
                        this._view?.webview.postMessage({
                            command: 'mermaidResult',
                            mermaid: '',
                            nodes_meta: {},
                            file: data.file,
                            error: 'No repository selected.'
                        });
                        break;
                    }
                    try {
                        const graphData = await this._backendClient.getMermaidGraph(workspaceFolder, data.file);
                        this._view?.webview.postMessage({
                            command: 'mermaidResult',
                            mermaid: graphData.mermaid,
                            nodes_meta: graphData.nodes_meta,
                            file: data.file
                        });
                    } catch (err: any) {
                        this._view?.webview.postMessage({ command: 'mermaidResult', mermaid: '', nodes_meta: {}, file: data.file, error: err.message });
                    }
                    break;
                }
                case 'getSecurityAudit': {
                    const workspaceFolder = (data.path || '').trim();
                    if (!workspaceFolder || workspaceFolder === '.') {
                        this._view?.webview.postMessage({ command: 'securityAuditResult', error: 'No repository selected.' });
                        break;
                    }
                    try {
                        const security = await this._backendClient.getSecurityAudit(workspaceFolder);
                        this._view?.webview.postMessage({ command: 'securityAuditResult', security });
                    } catch (err: any) {
                        this._view?.webview.postMessage({ command: 'securityAuditResult', error: err.message });
                    }
                    break;
                }
                case 'testOllamaConnection': {
                    const status = await this._backendClient.checkOllamaStatus();
                    this._view?.webview.postMessage({
                        command: 'ollamaStatusResult',
                        data: status
                    });
                    break;
                }
                case 'saveSettings': {
                    const s = data.settings || {};
                    const SECRET_KEY_NAME = 'clay_cloud_api_key';
                    if (s.api_key !== undefined) {
                        const rawKey = typeof s.api_key === 'string' ? s.api_key.trim() : '';
                        if (rawKey && !rawKey.includes('••••')) {
                            await this._context?.secrets.store(SECRET_KEY_NAME, rawKey);
                        } else if (rawKey === '') {
                            await this._context?.secrets.delete(SECRET_KEY_NAME);
                        }
                    }
                    const activeKey = (await this._context?.secrets.get(SECRET_KEY_NAME)) || '';
                    const backendPayload = { ...s, api_key: activeKey };
                    const res = await this._backendClient.updateSettings(backendPayload);
                    this._view?.webview.postMessage({
                        command: 'settingsSaved',
                        result: res
                    });
                    const maskedKey = activeKey.length > 8
                        ? `${activeKey.slice(0, 4)}••••••••${activeKey.slice(-4)}`
                        : (activeKey ? '••••••••' : '');
                    this._view?.webview.postMessage({
                        command: 'cloudApiKeyLoaded',
                        hasKey: !!activeKey,
                        maskedKey: maskedKey
                    });
                    vscode.window.showInformationMessage(`CLAY Copilot: Settings saved. Model: ${data.settings.model || 'default'}`);
                    break;
                }
                case 'applyPatch': {
                    try {
                        let targetFile = data.file;
                        const targetWorkspace = data.workspacePath || vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || (this._lastActiveDocument ? path.dirname(this._lastActiveDocument.uri.fsPath) : undefined);
                        const genericPlaceholders = new Set([
                            'file', 'unknown', 'filename', 'patch', 'diff',
                            'solution.py', 'patch.diff', 'code', 'script',
                            'untitled', 'path/to/file', 'new_file', ''
                        ]);
                        const isGeneric = !targetFile || genericPlaceholders.has(path.basename(targetFile).toLowerCase()) || (!path.extname(targetFile) && !fs.existsSync(targetFile));
                        if (isGeneric && this._lastActiveDocument) {
                            targetFile = this._lastActiveDocument.uri.fsPath;
                        } else if (targetWorkspace && !path.isAbsolute(targetFile)) {
                            targetFile = path.resolve(targetWorkspace, targetFile);
                        }
                        const res = await this._backendClient.applyPatch(targetFile, data.diff, targetWorkspace);
                        const finalName = res?.filename || targetFile;
                        vscode.window.showInformationMessage(`CLAY Copilot: Patch applied cleanly to ${finalName}`);
                        this._view?.webview.postMessage({ command: 'patchApplied', file: finalName });
                    } catch (err: any) {
                        vscode.window.showErrorMessage(`Failed to apply patch: ${err.message}`);
                    }
                    break;
                }
                case 'rebaseDiff': {
                    try {
                        let targetFile = data.file;
                        const targetWorkspace = data.workspacePath || vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || (this._lastActiveDocument ? path.dirname(this._lastActiveDocument.uri.fsPath) : undefined);
                        const genericPlaceholders = new Set([
                            'file', 'unknown', 'filename', 'patch', 'diff',
                            'solution.py', 'patch.diff', 'code', 'script',
                            'untitled', 'path/to/file', 'new_file', ''
                        ]);
                        const isGeneric = !targetFile || genericPlaceholders.has(path.basename(targetFile).toLowerCase()) || (!path.extname(targetFile) && !fs.existsSync(targetFile));
                        if (isGeneric && this._lastActiveDocument) {
                            targetFile = this._lastActiveDocument.uri.fsPath;
                        } else if (targetWorkspace && !path.isAbsolute(targetFile)) {
                            targetFile = path.resolve(targetWorkspace, targetFile);
                        }
                        const res = await this._backendClient.rebaseDiff(targetFile, data.baseContent, data.diff, data.currentContent, targetWorkspace, data.forceResolution);
                        this._view?.webview.postMessage({
                            command: 'diffRebased',
                            file: targetFile,
                            result: res
                        });
                    } catch (err: any) {
                        this._view?.webview.postMessage({
                            command: 'diffRebaseFailed',
                            file: data.file,
                            error: err.message
                        });
                        vscode.window.showErrorMessage(`Failed to rebase diff: ${err.message}`);
                    }
                    break;
                }

                case 'loadMermaid': {
                    const mermaidSyntax = await this._backendClient.getMermaidGraph(data.path);
                    this._view?.webview.postMessage({ command: 'renderMermaid', mermaid: mermaidSyntax });
                    break;
                }
                case 'acceptChanges': {
                    await this._backendClient.reviewChanges('TASK #0428', 'accept');
                    vscode.window.showInformationMessage('CLAY Copilot: Staged changes accepted.');
                    break;
                }
                case 'rejectChanges': {
                    await this._backendClient.reviewChanges('TASK #0428', 'reject');
                    vscode.window.showWarningMessage('CLAY Copilot: Staged changes rejected.');
                    break;
                }
                case 'browseRepoFolder': {
                    const uris = await vscode.window.showOpenDialog({
                        canSelectFiles: false,
                        canSelectFolders: true,
                        canSelectMany: false,
                        openLabel: 'Select Repository Folder'
                    });
                    if (uris && uris.length > 0) {
                        const selectedPath = uris[0].fsPath;
                        if (this._context) {
                            await this._context.globalState.update('lastRepoPath', selectedPath);
                        }
                        await this._backendClient.updateSettings({ model: undefined });
                        const treeData = await this._backendClient.getRepoTree(selectedPath);
                        this._view?.webview.postMessage({
                            command: 'repoPathSelected',
                            path: selectedPath,
                            tree: treeData.tree,
                            stats: treeData.stats
                        });
                        vscode.window.showInformationMessage(`CLAY Copilot: Repository set to ${selectedPath}`);
                    }
                    break;
                }
                case 'setRepoPath': {
                    const newPath = (data.path || '').trim();
                    if (this._context) {
                        await this._context.globalState.update('lastRepoPath', newPath);
                    }
                    break;
                }
                case 'getInitialState': {
                    const savedRepo = this._context?.globalState.get<string>('lastRepoPath');
                    const activeFolder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
                    const initialRepo = savedRepo !== undefined ? savedRepo : (data.currentPath || activeFolder || '');
                    const isWin = process.platform === 'win32';
                    if (initialRepo) {
                        this._view?.webview.postMessage({
                            command: 'repoPathSelected',
                            path: initialRepo
                        });
                    } else if (savedRepo === '') {
                        this._view?.webview.postMessage({
                            command: 'repoPathCleared'
                        });
                    }
                    this._view?.webview.postMessage({
                        command: 'systemInfoResult',
                        data: {
                            os: isWin ? 'windows' : 'linux',
                            platform: process.platform,
                            is_windows: isWin,
                            shell: isWin ? 'PowerShell / CMD' : (process.env.SHELL || 'bash'),
                            workspace: initialRepo || activeFolder,
                            recommended_commands: isWin
                                ? ['dir', 'python --version', 'git status', 'pytest']
                                : ['ls -la', 'python3 --version', 'git status', 'pytest']
                        }
                    });
                    const currentToken = this._processManager?.getSessionToken() || this._backendClient.getSessionToken();
                    this._view?.webview.postMessage({
                        command: 'setBackendUrl',
                        url: this._backendClient.getBackendUrl(),
                        sessionToken: currentToken
                    });
                    break;
                }
                case 'resetRepoPath': {
                    if (this._context) {
                        await this._context.globalState.update('lastRepoPath', '');
                    }
                    this._view?.webview.postMessage({
                        command: 'repoPathCleared'
                    });
                    vscode.window.showInformationMessage('CLAY Copilot: Repository path cleared');
                    break;
                }
                case 'getRepoTree': {
                    let targetPath = (data.path || '').trim();
                    if (!targetPath) {
                        this._view?.webview.postMessage({
                            command: 'repoTreeResult',
                            tree: [],
                            stats: { filesCount: 0, dirsCount: 0 },
                            root: ''
                        });
                        break;
                    }
                    if (!fs.existsSync(targetPath)) {
                        this._view?.webview.postMessage({
                            command: 'repoTreeResult',
                            error: `Directory not found: ${targetPath}`
                        });
                        break;
                    }
                    try {
                        const treeData = await this._backendClient.getRepoTree(targetPath);
                        this._view?.webview.postMessage({
                            command: 'repoTreeResult',
                            tree: treeData.tree,
                            stats: treeData.stats,
                            root: treeData.root
                        });
                    } catch (err: any) {
                        this._view?.webview.postMessage({
                            command: 'repoTreeResult',
                            error: err.message
                        });
                    }
                    break;
                }
                case 'openFile': {
                    let filePath = (data.path || data.file_path || '').trim();
                    const symbolName = (data.symbol || '').trim();
                    const requestedLine = typeof data.line === 'number' && data.line > 0 ? data.line : 0;
                    if (!filePath && !symbolName) {
                        break;
                    }
                    try {
                        const savedRepo = this._context?.globalState.get<string>('lastRepoPath') || '';
                        const repoHint = (data.repo_path || savedRepo || '').trim();
                        const wsFolder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || '';

                        let candidate = filePath;
                        if (!candidate && repoHint) {
                            if (this._lastActiveDocument) {
                                candidate = this._lastActiveDocument.uri.fsPath;
                            }
                        }

                        if (candidate && (!path.isAbsolute(candidate) || !fs.existsSync(candidate))) {
                            if (repoHint && fs.existsSync(path.resolve(repoHint, candidate))) {
                                candidate = path.resolve(repoHint, candidate);
                            } else if (wsFolder && fs.existsSync(path.resolve(wsFolder, candidate))) {
                                candidate = path.resolve(wsFolder, candidate);
                            }
                        }

                        if (!candidate || !fs.existsSync(candidate)) {
                            if (symbolName) {
                                const searchDir = repoHint || wsFolder;
                                if (searchDir) {
                                    const files = await vscode.workspace.findFiles('**/*.{py,ts,js,jsx,tsx,go,rs,java,c,cpp}', '**/node_modules/**', 25);
                                    for (const f of files) {
                                        try {
                                            const content = (await vscode.workspace.fs.readFile(f)).toString();
                                            if (content.includes(symbolName)) {
                                                candidate = f.fsPath;
                                                break;
                                            }
                                        } catch { }
                                    }
                                }
                            }
                        }

                        if (!candidate || !fs.existsSync(candidate)) {
                            vscode.window.showWarningMessage(`CLAY Copilot: Could not locate source file for ${symbolName || filePath}`);
                            break;
                        }

                        const targetUri = vscode.Uri.file(candidate);
                        const doc = await vscode.workspace.openTextDocument(targetUri);
                        const text = doc.getText();
                        const lines = text.split(/\r?\n/);
                        let targetLine = requestedLine > 0 ? requestedLine - 1 : 0;
                        let foundBySymbol = false;
                        let matchedSymWord = symbolName;

                        if (symbolName) {
                            const symCandidates = [symbolName];
                            if (symbolName.includes('_')) {
                                symCandidates.push(symbolName.replace(/_/g, '.'));
                                const subParts = symbolName.split('_');
                                if (subParts.length > 1 && subParts[subParts.length - 1].length > 1) {
                                    symCandidates.push(subParts[subParts.length - 1]);
                                }
                            }

                            // 1. Check if requested line from SQLite already matches the symbol
                            if (requestedLine > 0 && targetLine >= 0 && targetLine < lines.length) {
                                const candLine = lines[targetLine];
                                for (const sc of symCandidates) {
                                    const esc = sc.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                                    if (new RegExp(`\\b${esc}\\b`).test(candLine)) {
                                        foundBySymbol = true;
                                        matchedSymWord = sc;
                                        break;
                                    }
                                }
                            }

                            // 2. Strict function/class/method definition patterns (e.g. def main, class Calculator)
                            if (!foundBySymbol) {
                                for (const sc of symCandidates) {
                                    const esc = sc.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                                    const defPattern = new RegExp(`^\\s*(?:def|async\\s+def|class|function|fn|pub\\s+fn)\\s+${esc}\\b`);
                                    for (let i = 0; i < lines.length; i++) {
                                        if (defPattern.test(lines[i])) {
                                            targetLine = i;
                                            foundBySymbol = true;
                                            matchedSymWord = sc;
                                            break;
                                        }
                                    }
                                    if (foundBySymbol) break;
                                }
                            }

                            // 3. Variable/constant assignment or export pattern
                            if (!foundBySymbol) {
                                for (const sc of symCandidates) {
                                    const esc = sc.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                                    const assignPattern = new RegExp(`^\\s*(?:export\\s+)?(?:const|let|var)\\s+${esc}\\b`);
                                    for (let i = 0; i < lines.length; i++) {
                                        if (assignPattern.test(lines[i])) {
                                            targetLine = i;
                                            foundBySymbol = true;
                                            matchedSymWord = sc;
                                            break;
                                        }
                                    }
                                    if (foundBySymbol) break;
                                }
                            }

                            // 4. Function call or invocation site: symbol(...)
                            if (!foundBySymbol) {
                                for (const sc of symCandidates) {
                                    const esc = sc.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                                    const callPattern = new RegExp(`\\b${esc}\\s*\\(`);
                                    for (let i = 0; i < lines.length; i++) {
                                        if (callPattern.test(lines[i])) {
                                            targetLine = i;
                                            foundBySymbol = true;
                                            matchedSymWord = sc;
                                            break;
                                        }
                                    }
                                    if (foundBySymbol) break;
                                }
                            }

                            // 5. Any standalone word match
                            if (!foundBySymbol) {
                                for (const sc of symCandidates) {
                                    const esc = sc.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                                    const wordPattern = new RegExp(`\\b${esc}\\b`);
                                    for (let i = 0; i < lines.length; i++) {
                                        if (wordPattern.test(lines[i])) {
                                            targetLine = i;
                                            foundBySymbol = true;
                                            matchedSymWord = sc;
                                            break;
                                        }
                                    }
                                    if (foundBySymbol) break;
                                }
                            }
                        }

                        // Bound check
                        if (targetLine < 0) targetLine = 0;
                        if (targetLine >= lines.length) targetLine = Math.max(0, lines.length - 1);

                        // Calculate exact character column offset for the symbol token
                        const targetLineText = lines[targetLine] || '';
                        let charStart = 0;
                        let charEnd = 0;
                        let tokenToFind = matchedSymWord || symbolName || '';
                        let symIdx = tokenToFind ? targetLineText.indexOf(tokenToFind) : -1;
                        if (symIdx < 0 && symbolName) {
                            symIdx = targetLineText.indexOf(symbolName);
                            tokenToFind = symbolName;
                        }
                        if (symIdx >= 0) {
                            charStart = symIdx;
                            charEnd = symIdx + tokenToFind.length;
                        } else {
                            const nonWs = targetLineText.search(/\S/);
                            charStart = nonWs >= 0 ? nonWs : 0;
                            charEnd = charStart;
                        }

                        const startPos = new vscode.Position(targetLine, charStart);
                        const endPos = new vscode.Position(targetLine, charEnd);
                        const range = new vscode.Range(startPos, endPos);

                        const targetColumn = vscode.window.visibleTextEditors[0]?.viewColumn || vscode.ViewColumn.One;

                        const editor = await vscode.window.showTextDocument(doc, {
                            viewColumn: targetColumn,
                            preserveFocus: false,
                            preview: false,
                            selection: range
                        });

                        // Explicitly set selection, reveal in center, and command editor group focus to guarantee cursor jump and keyboard focus
                        editor.selection = new vscode.Selection(startPos, endPos);
                        editor.revealRange(range, vscode.TextEditorRevealType.InCenter);
                        await vscode.commands.executeCommand('workbench.action.focusActiveEditorGroup');

                        this._view?.webview.postMessage({
                            command: 'fileOpened',
                            file: candidate,
                            line: targetLine + 1,
                            symbol: symbolName
                        });
                    } catch (err: any) {
                        vscode.window.showErrorMessage(`CLAY: Could not open file ${filePath}: ${err.message}`);
                    }
                    break;
                }
                case 'executeTerminalCommand': {
                    let workspace = data.workspace_path;
                    const activeFolder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
                    if (!workspace || (typeof workspace === 'string' && workspace.trim() === '.') || !fs.existsSync(workspace)) {
                        workspace = activeFolder || process.cwd();
                    }
                    const shellCmd = (data.commandText || data.cmd || data.command || '').trim();
                    const secMode = data.security_mode || 'sandboxed';
                    const allowNet = data.allow_network !== false;

                    const res = await this._executeTerminalSafely(shellCmd, workspace, secMode, allowNet);
                    this._view?.webview.postMessage({
                        command: 'terminalCommandResult',
                        data: res
                    });
                    break;
                }
                case 'getSystemInfo': {
                    let workspace = data.workspace_path;
                    const activeFolder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
                    if (!workspace || (typeof workspace === 'string' && workspace.trim() === '.') || !fs.existsSync(workspace)) {
                        workspace = activeFolder || process.cwd();
                    }
                    const isWin = process.platform === 'win32';
                    const info = {
                        os: isWin ? 'windows' : 'linux',
                        platform: process.platform,
                        is_windows: isWin,
                        shell: isWin ? 'PowerShell / CMD' : (process.env.SHELL || 'bash'),
                        workspace: workspace,
                        recommended_commands: isWin
                            ? ['dir', 'python --version', 'git status', 'pytest']
                            : ['ls -la', 'python3 --version', 'git status', 'pytest']
                    };
                    this._view?.webview.postMessage({
                        command: 'systemInfoResult',
                        data: info
                    });
                    break;
                }
                case 'executeAgentStep': {
                    const workspace = data.workspace_path || vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
                    const activeEditor = vscode.window.activeTextEditor;
                    const candidateFile = data.active_file || (activeEditor?.document?.uri?.scheme === 'file' ? activeEditor.document.uri.fsPath : undefined);
                    const activeFile = this._isFileInWorkspace(candidateFile, workspace) ? candidateFile : undefined;
                    const cursorLine = activeFile && (data.cursor_line !== undefined ? data.cursor_line : (activeEditor?.selection?.active?.line !== undefined ? activeEditor.selection.active.line + 1 : undefined));
                    const activeFileContent = activeFile ? (data.active_file_content || (activeEditor?.document?.uri?.scheme === 'file' ? activeEditor.document.getText() : (this._lastActiveDocument ? this._lastActiveDocument.getText() : undefined))) : undefined;
                    const res = await this._backendClient.executeAgentStep(
                        data.step_id,
                        data.goal,
                        workspace,
                        activeFile,
                        cursorLine,
                        data.target_file,
                        data.step_action,
                        data.total_steps,
                        activeFileContent
                    );
                    this._view?.webview.postMessage({
                        command: 'agentStepResult',
                        step_id: data.step_id,
                        data: res
                    });
                    break;
                }
                case 'abortChatGeneration': {
                    await this._backendClient.stopStreamChat();
                    break;
                }
                case 'abortAgentExecution': {
                    await this._backendClient.abortAgentExecution();
                    break;
                }
                case 'abortTerminalCommand': {
                    await this._backendClient.abortTerminalCommand(data.execution_id);
                    break;
                }
                case 'restartBackendServer': {
                    vscode.commands.executeCommand('codebaseCopilot.restartBackend');
                    break;
                }
            }
        });
    }

    private _isFileInWorkspace(filePath: string | undefined, workspacePath: string | undefined): boolean {
        if (!filePath || !workspacePath) return false;
        try {
            const normFile = path.resolve(filePath).toLowerCase();
            const normWorkspace = path.resolve(workspacePath).toLowerCase();
            return normFile.startsWith(normWorkspace + path.sep) || normFile === normWorkspace;
        } catch {
            return false;
        }
    }

    private async _executeTerminalSafely(
        command: string,
        cwd: string,
        securityMode: string = 'sandboxed',
        allowNetwork: boolean = true
    ): Promise<any> {
        const isWindows = process.platform === 'win32';
        const osType = isWindows ? 'windows' : 'linux';

        if (!command || !command.trim()) {
            return { exit_code: 0, stdout: '', stderr: '', passed: true, os: osType, workspace: cwd };
        }

        const cmdClean = command.trim();

        // 1. Strict mode: only permit safe inspection / testing operations
        if (securityMode === 'strict') {
            const strictSafePrefixes = [
                'ls', 'dir', 'git status', 'git diff', 'git log', 'git show',
                'pytest', 'python -m pytest', 'python -m unittest', 'python3 -m unittest',
                'python -m py_compile', 'python3 -m py_compile', 'npm test', 'npm.cmd test',
                'python --version', 'python3 --version', 'node -v', 'npm -v',
                'cat', 'type', 'echo', 'pwd', 'whoami', 'get-childitem', 'get-content'
            ];
            const isSafe = strictSafePrefixes.some(prefix => cmdClean.toLowerCase().startsWith(prefix));
            if (!isSafe) {
                return {
                    exit_code: -1,
                    stdout: '',
                    stderr: `Execution blocked: Strict mode requires explicit review for modifying command '${command}'.`,
                    passed: false,
                    os: osType,
                    workspace: cwd
                };
            }
        }

        // 2. Sandboxed mode: block destructive operations and network if disallowed
        if (securityMode === 'sandboxed' || securityMode === 'strict') {
            const globalDangerous = [
                /:\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:/i,
                /\b(drop\s+database|truncate\s+table)\b/i,
                /\bbash\s+-i\s+>&/i,
                /\b(nc|ncat|netcat)\b.*-e\s+/i
            ];
            for (const pat of globalDangerous) {
                if (pat.test(cmdClean)) {
                    return { exit_code: -1, stdout: '', stderr: 'Execution blocked: High-risk operation detected.', passed: false, os: osType, workspace: cwd };
                }
            }

            if (isWindows) {
                const winDangerous = [
                    /\bformat\s+[a-z]:/i,
                    /\bdel\s+(\/[a-z]\s+)*[a-z]:\\/i,
                    /\b(rd|rmdir)\s+(\/s\s+\/q|\/q\s+\/s)\s+[a-z]:\\/i,
                    /\bRemove-Item\b.*-Recurse\b.*(-Force\b.*)?([a-z]:\\|\$env:)/i,
                    /\breg\s+(delete|add)\s+hklm/i,
                    /\b(shutdown\s+\/s|Stop-Computer)\b/i
                ];
                for (const pat of winDangerous) {
                    if (pat.test(cmdClean)) {
                        return { exit_code: -1, stdout: '', stderr: 'Execution blocked: Dangerous Windows system operation detected.', passed: false, os: osType, workspace: cwd };
                    }
                }
            } else {
                const linuxDangerous = [
                    /\brm\s+-[a-z]*r[a-z]*f\s+(\/|~|\.\.|\*)/i,
                    /\bmkfs(\.[a-z0-9]+)?\s+/i,
                    /\bdd\s+if=\S+\s+of=\/dev\/[a-z0-9]+/i,
                    /\b(shutdown|reboot|poweroff)\b/i
                ];
                for (const pat of linuxDangerous) {
                    if (pat.test(cmdClean)) {
                        return { exit_code: -1, stdout: '', stderr: 'Execution blocked: Dangerous Linux system operation detected.', passed: false, os: osType, workspace: cwd };
                    }
                }
            }

            if (!allowNetwork) {
                const netCommands = ['curl', 'wget', 'git clone', 'git pull', 'git push', 'npm install', 'npm i', 'pip install', 'pip3 install'];
                if (netCommands.some(nc => cmdClean.toLowerCase().includes(nc))) {
                    return { exit_code: -1, stdout: '', stderr: 'Execution blocked: Sandboxed network requests are disabled in settings.', passed: false, os: osType, workspace: cwd };
                }
            }
        }

        // 3. ChildProcess Execution directly in selected workspace directory
        return new Promise((resolve) => {
            const finalCmd = isWindows
                ? `powershell.exe -NoProfile -ExecutionPolicy Bypass -NonInteractive -Command "${cmdClean.replace(/"/g, '`"')}"`
                : cmdClean;

            cp.exec(finalCmd, {
                cwd: cwd,
                timeout: 35000,
                windowsHide: true,
                maxBuffer: 10 * 1024 * 1024
            }, (err, stdout, stderr) => {
                const exitCode = err ? (typeof err.code === 'number' ? err.code : 1) : 0;
                const errDetail = stderr || (err ? (err.message || String(err)) : '');
                resolve({
                    exit_code: exitCode,
                    stdout: stdout || '',
                    stderr: errDetail,
                    passed: exitCode === 0,
                    os: osType,
                    workspace: cwd
                });
            });
        });
    }

    private async _handleUserQuery(
        query: string,
        workspacePath?: string,
        securityMode?: string,
        inferenceOptions?: { temperature?: number; num_ctx?: number; num_predict?: number },
        history?: Array<{ role: string; content: string }>,
        pinnedMentions?: Array<Record<string, any>>
    ) {
        let activeDoc = vscode.window.activeTextEditor?.document;
        if ((!activeDoc || activeDoc.uri.scheme !== 'file') && this._lastActiveDocument && !this._lastActiveDocument.isClosed) {
            activeDoc = this._lastActiveDocument;
        }
        if ((!activeDoc || activeDoc.uri.scheme !== 'file') && vscode.window.visibleTextEditors.length > 0) {
            const visible = vscode.window.visibleTextEditors.find(e => e.document.uri.scheme === 'file');
            if (visible) activeDoc = visible.document;
        }

        const effectiveWorkspace = workspacePath && workspacePath.trim() !== '.' ? workspacePath.trim() : undefined;
        const isDocInWorkspace = (doc?: vscode.TextDocument) => {
            if (!effectiveWorkspace) return true;
            return doc && this._isFileInWorkspace(doc.fileName, effectiveWorkspace);
        };
        const isDocReal = (doc?: vscode.TextDocument) => {
            if (!doc || !isDocInWorkspace(doc)) return false;
            if (fs.existsSync(doc.fileName)) return true;
            return doc.isDirty && doc.getText().trim().length > 0;
        };

        const activeFileContext = (activeDoc && isDocReal(activeDoc)) ? activeDoc.fileName : undefined;
        const activeFileContent = (activeDoc && isDocReal(activeDoc)) ? activeDoc.getText() : undefined;

        // Collect all open text documents to sync in-memory state with backend (strictly within workspace if one is active)
        const openFiles: Array<{ path: string; content: string; is_dirty?: boolean }> = [];
        for (const doc of vscode.workspace.textDocuments) {
            if (doc.uri.scheme === 'file' && isDocReal(doc) && !doc.fileName.includes('node_modules') && !doc.fileName.includes('.git')) {
                openFiles.push({
                    path: doc.fileName,
                    content: doc.getText(),
                    is_dirty: doc.isDirty
                });
            }
        }

        const cursorLine = vscode.window.activeTextEditor?.selection ? vscode.window.activeTextEditor.selection.active.line + 1 : undefined;

        await this._backendClient.streamChat(
            query,
            activeFileContext,
            (token) => {
                this._view?.webview.postMessage({ command: 'streamToken', token });
            },
            (meta) => {
                this._view?.webview.postMessage({ command: 'streamComplete', diff: meta.diff, citations: meta.citations });
            },
            (err) => {
                this._view?.webview.postMessage({
                    command: 'streamToken',
                    token: `\n\n[Error: ${err}]`
                });
            },
            effectiveWorkspace,
            securityMode,
            activeFileContent,
            openFiles,
            inferenceOptions,
            cursorLine,
            history,
            pinnedMentions
        );
    }

    private _getHtmlForWebview(webview: vscode.Webview) {
        const styleUri = webview.asWebviewUri(vscode.Uri.joinPath(this._extensionUri, 'src', 'webview', 'media', 'style.css'));
        const scriptUri = webview.asWebviewUri(vscode.Uri.joinPath(this._extensionUri, 'src', 'webview', 'media', 'main.js'));

        const nonce = getNonce();
        const activeBackendUrl = this._backendClient.getBackendUrl() || 'http://127.0.0.1:8765';
        const sessionToken = this._processManager?.getSessionToken() || this._backendClient.getSessionToken();

        return `<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="clay-session-token" content="${sessionToken}">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; connect-src ${activeBackendUrl} http://127.0.0.1:* http://localhost:* ws://127.0.0.1:* ws://localhost:* https://ollama.com; style-src ${webview.cspSource} 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; script-src 'nonce-${nonce}';">
    <link href="${styleUri}" rel="stylesheet">
    <title>CLAY Copilot</title>
</head>
<body>
    <div class="app-frame">
        <!-- Header Context Bar -->
        <div class="context-bar">
        <div class="brand-crumb">
            <div class="clay-brand-badge">
                <svg viewBox="1 1 13 9" class="clay-cube-icon" shape-rendering="crispEdges">
                    <rect x="1" y="1" width="2" height="1" fill="#C15C3D"/>
                    <rect x="2" y="2" width="2" height="1" fill="#C15C3D"/>
                    <rect x="3" y="3" width="2" height="1" fill="#C15C3D"/>
                    <rect x="4" y="4" width="2" height="1" fill="#C15C3D"/>
                    <rect x="5" y="5" width="2" height="1" fill="#C15C3D"/>
                    <rect x="4" y="6" width="2" height="1" fill="#C15C3D"/>
                    <rect x="3" y="7" width="2" height="1" fill="#C15C3D"/>
                    <rect x="2" y="8" width="2" height="1" fill="#C15C3D"/>
                    <rect x="1" y="9" width="2" height="1" fill="#C15C3D"/>
                    <rect x="8" y="8" width="6" height="2" fill="#C15C3D"/>
                </svg>
                <span class="brand-title">CLAY</span>
            </div>
        </div>
        <div class="context-pills">
            <button class="pill-clear-chat" id="btn-clear-chat" title="Clear chat history and reset context" aria-label="Clear chat">
                <span class="pill-clear-text">Clear</span>
            </button>
            <button class="pill-theme-switch" id="btn-theme-toggle" title="Toggle Light / CLAY Dark Mode" aria-label="Toggle theme">
                <span class="pill-theme-knob">
                    <svg class="theme-icon-moon" id="icon-theme-moon" viewBox="0 0 16 16" width="9" height="9" fill="currentColor">
                        <path d="M6 1a5 5 0 0 0 5 5 5 5 0 0 0 4.9 4A6 6 0 1 1 6 1z"/>
                    </svg>
                    <svg class="theme-icon-sun" id="icon-theme-sun" style="display: none;" viewBox="0 0 16 16" width="9" height="9" fill="currentColor">
                        <path d="M8 11a3 3 0 1 1 0-6 3 3 0 0 1 0 6zm0 1a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM8 0a.5.5 0 0 1 .5.5v2a.5.5 0 0 1-1 0v-2A.5.5 0 0 1 8 0zm0 13a.5.5 0 0 1 .5.5v2a.5.5 0 0 1-1 0v-2A.5.5 0 0 1 8 13zm8-5a.5.5 0 0 1-.5.5h-2a.5.5 0 0 1 0-1h2a.5.5 0 0 1 .5.5zM3 8a.5.5 0 0 1-.5.5h-2a.5.5 0 0 1 0-1h2A.5.5 0 0 1 3 8zm10.657-5.657a.5.5 0 0 1 0 .707l-1.414 1.415a.5.5 0 1 1-.707-.708l1.414-1.414a.5.5 0 0 1 .707 0zm-9.193 9.193a.5.5 0 0 1 0 .707L3.05 13.657a.5.5 0 0 1-.707-.707l1.414-1.414a.5.5 0 0 1 .707 0zm9.193 0a.5.5 0 0 1-.707 0l-1.414-1.414a.5.5 0 0 1 .707-.707l1.414 1.414a.5.5 0 0 1 0 .707zM4.464 4.465a.5.5 0 0 1-.707 0L2.343 3.05a.5.5 0 1 1 .707-.707l1.414 1.414a.5.5 0 0 1 0 .708z"/>
                    </svg>
                </span>
                <span class="theme-switch-text" id="theme-switch-text">Dark</span>
            </button>
            <span class="pill pill-teal" id="crumb-status"><span class="pill-dot"></span>Live</span>
        </div>
    </div>

    <!-- Global 3-Way Merge Conflict Header Notification -->
    <div class="header-conflict-banner" id="header-conflict-banner" style="display: none;">
        <div class="header-conflict-badge">
            <span class="header-conflict-title" id="header-conflict-title">WARNING: 3-WAY MERGE CONFLICT DETECTED</span>
        </div>
        <div class="header-conflict-actions">
            <button type="button" class="btn-header-dismiss-conflict" id="btn-header-dismiss-conflict" title="Dismiss warning">
                Dismiss
            </button>
        </div>
    </div>

    <!-- 1. Chat View (Active by default) -->
    <div id="view-chat" class="view-container active-view">
        <div class="message-list" id="message-list">
            <div class="empty-state-card" id="chat-empty-state">
                <div class="clay-hero-logo">
                    <svg viewBox="0 0 512 512" shape-rendering="crispEdges">
                        <rect width="512" height="512" fill="#FAF6F0" rx="24"/>
                        <g transform="translate(64, 148) scale(24)">
                            <rect x="1" y="1" width="2" height="1" fill="#C15C3D"/>
                            <rect x="2" y="2" width="2" height="1" fill="#C15C3D"/>
                            <rect x="3" y="3" width="2" height="1" fill="#C15C3D"/>
                            <rect x="4" y="4" width="2" height="1" fill="#C15C3D"/>
                            <rect x="5" y="5" width="2" height="1" fill="#C15C3D"/>
                            <rect x="4" y="6" width="2" height="1" fill="#C15C3D"/>
                            <rect x="3" y="7" width="2" height="1" fill="#C15C3D"/>
                            <rect x="2" y="8" width="2" height="1" fill="#C15C3D"/>
                            <rect x="1" y="9" width="2" height="1" fill="#C15C3D"/>
                            <rect x="8" y="8" width="6" height="2" fill="#C15C3D"/>
                        </g>
                    </svg>
                </div>
                <div class="empty-state-title">CLAY Copilot</div>
                <div class="empty-state-desc">Autonomous repository agent & surgical engineering copilot. Running on <strong id="welcome-model-text">gemma4:31b-cloud</strong>.</div>
            </div>
        </div>
    </div>

    <!-- 2. Agent Task View -->
    <div id="view-agent" class="view-container">
        <!-- Top: Agent Console -->
        <div class="terminal-widget agent-console-widget">
            <div class="terminal-header">
                <span class="console-title">agent-console</span>
                <div class="terminal-actions">
                    <span class="terminal-status-badge" id="agent-terminal-status">READY</span>
                    <button class="terminal-action-btn" id="btn-clear-terminal" title="Clear Console">clear</button>
                    <button class="terminal-action-btn" id="btn-copy-terminal" title="Copy Log">copy</button>
                </div>
            </div>
            <div class="terminal-stream" id="terminal-stream"><span class="cmd-line">$ clay-agent ready</span>
[clay] Waiting for task execution goal...</div>

            <!-- Interactive CLI Command Bar with Security Mode Indicator -->
            <div class="terminal-cli-bar">
                <span class="terminal-prompt-symbol">$</span>
                <input type="text" class="terminal-cli-input" id="input-terminal-cmd" placeholder="Run shell command in workspace (e.g. pytest, git status)..." spellcheck="false" autocomplete="off">
                <span class="terminal-mode-indicator" id="terminal-preview-security">[SANDBOXED]</span>
                <button class="terminal-cli-exec-btn" id="btn-exec-terminal-cmd">Run</button>
            </div>

            <!-- Quick Action Command Chips -->
            <div class="terminal-quick-chips" id="terminal-quick-chips">
                <span class="terminal-quick-chips-label">Quick:</span>
                <button type="button" class="cli-chip" data-cmd="python -m pytest">pytest</button>
                <button type="button" class="cli-chip" data-cmd="git status">git status</button>
                <button type="button" class="cli-chip" data-cmd="python -m unittest">python test</button>
                <button type="button" class="cli-chip" data-cmd="dir">dir</button>
            </div>
        </div>

        <!-- Bottom: User Console (Task Planner + Embedded Execution Task List) -->
        <div class="terminal-widget user-console-widget">
            <div class="terminal-header">
                <span class="console-title user-console-title">user-console</span>
            </div>

            <!-- Ambiguity Clarification Bar -->
            <div class="agent-ambiguity-bar" id="agent-ambiguity-bar" style="display: none;">
                <div class="ambiguity-header">
                    <span class="ambiguity-title">Target Ambiguity:</span>
                    <span class="ambiguity-question" id="ambiguity-question">Which file would you like to update?</span>
                </div>
                <div class="ambiguity-chips-list" id="ambiguity-chips-list"></div>
            </div>

            <!-- CLI Style Task Planner Input -->
            <div class="user-console-cli-bar">
                <span class="user-console-prompt">&gt;</span>
                <textarea class="user-task-input" id="agent-goal-input" rows="1" placeholder="Enter task goal (e.g. Refactor error handling or add unit tests)..." spellcheck="false" autocomplete="off"></textarea>
                <button class="btn-run-task" id="btn-run-agent" title="Execute Task">Run</button>
                <button class="btn-stop-task" id="btn-stop-agent" style="display: none;" title="Stop Task">Stop</button>
            </div>

            <!-- Execution Progress Track (Inside User Console) -->
            <div class="terminal-progress-section" id="agent-progress-track" style="display: none;">
                <div class="terminal-progress-bar-track">
                    <div class="terminal-progress-bar" id="agent-progress-bar"></div>
                </div>
                <div class="terminal-progress-text" id="agent-progress-text"></div>
            </div>

            <!-- Execution Task List UI (Inside User Console Only) -->
            <div class="terminal-plan-header">
                <span>// EXECUTION PLAN</span>
            </div>
            <div class="terminal-step-list" id="agent-step-list">
                <div class="terminal-step-empty">No active task. Enter a goal above and click Run to execute.</div>
            </div>
        </div>
    </div>

    <!-- 3. Diff Reviewer View -->
    <div id="view-diff" class="view-container">
        <div class="diff-summary-bar">
            <div class="diff-summary-left">
                <span id="diff-summary-label">Staged Changes // 0 files</span>
                <div class="diff-file-selector" id="diff-file-selector" style="display: none;"></div>
            </div>
            <div class="diff-summary-controls">
                <button type="button" class="btn-diff-view-toggle" id="btn-diff-view-mode" title="Toggle Split / Unified Diff View">
                    <span id="diff-mode-label">Unified</span>
                </button>
                <div class="diff-badges-wrap">
                    <span class="diff-badge-add" id="diff-badge-add">+0</span>
                    <span class="diff-badge-del" id="diff-badge-del">-0</span>
                </div>
            </div>
        </div>

        <!-- 3-Way Merge Conflict Guard Status Pill -->
        <div class="diff-conflict-guard-pill" id="diff-conflict-pill" style="display: none;">
            <div class="guard-pill-badge">
                <span class="guard-pill-title" id="guard-pill-title">WARNING: 3-WAY MERGE CONFLICT DETECTED</span>
            </div>
            <div class="guard-pill-actions">
                <button type="button" class="btn-guard-dismiss" id="btn-guard-dismiss" title="Dismiss warning">
                    Dismiss
                </button>
            </div>
        </div>

        <div id="diff-staged-container">
            <div class="diff-empty-state" id="diff-empty-state">
                <div class="diff-pixel-art-card">
                    <svg viewBox="0 0 48 48" width="52" height="52" shape-rendering="crispEdges">
                        <!-- Pixel Card Outer Border & Shadow -->
                        <rect x="6" y="6" width="36" height="36" fill="#CC6B49" opacity="0.08"/>
                        <!-- Pixel Document Border (terracotta) -->
                        <rect x="10" y="6" width="22" height="2" fill="#D97757"/>
                        <rect x="8" y="8" width="2" height="32" fill="#D97757"/>
                        <rect x="38" y="14" width="2" height="26" fill="#D97757"/>
                        <rect x="10" y="40" width="28" height="2" fill="#D97757"/>
                        <!-- Pixel Folded Corner (terracotta & amber) -->
                        <rect x="32" y="6" width="2" height="2" fill="#D97757"/>
                        <rect x="34" y="8" width="2" height="2" fill="#D97757"/>
                        <rect x="36" y="10" width="2" height="2" fill="#D97757"/>
                        <rect x="38" y="12" width="2" height="2" fill="#D97757"/>
                        <rect x="32" y="8" width="6" height="6" fill="#E5A84B" opacity="0.35"/>
                        <!-- Pixel + Addition Line (Green) -->
                        <rect x="15" y="14" width="2" height="6" fill="#22C55E"/>
                        <rect x="13" y="16" width="6" height="2" fill="#22C55E"/>
                        <rect x="22" y="16" width="13" height="2" fill="#22C55E"/>
                        <!-- Pixel - Deletion Line (Crimson) -->
                        <rect x="13" y="24" width="6" height="2" fill="#EF4444"/>
                        <rect x="22" y="24" width="11" height="2" fill="#EF4444"/>
                        <!-- Pixel Context Code Lines (terracotta muted) -->
                        <rect x="14" y="32" width="18" height="2" fill="#8C847A"/>
                        <rect x="14" y="36" width="10" height="2" fill="#8C847A" opacity="0.7"/>
                        <!-- Pixel Prompt Cursor (terracotta) -->
                        <rect x="26" y="36" width="4" height="2" fill="#D97757"/>
                    </svg>
                </div>
                <div class="diff-empty-title">NO STAGED CHANGES</div>
                <div class="diff-pixel-tip">
                    <span class="diff-pixel-tag">HOT-RELOAD</span> Live sync with active editor &amp; agent tasks
                </div>
            </div>
        </div>

        <div class="sticky-review-bar" id="diff-action-bar" style="display: none;">
            <button class="btn-reject" id="btn-reject-diff">Reject Changes</button>
            <button class="btn-accept" id="btn-accept-diff">Accept & Apply Patch</button>
        </div>
    </div>

    <!-- 4. Repo Intelligence View -->
    <div id="view-repo" class="view-container">
        <div class="task-card">
            <div class="task-title">Repository Intelligence Graph</div>
            <div style="font-size: 11px; color: var(--text-secondary); margin-bottom: 10px;" id="repo-stats-text">
                Indexed: -- Files // -- Functions & Classes // -- Chunks
            </div>

            <div class="settings-group" style="margin-bottom: 10px;">
                <label class="settings-label">Local Repository Workspace Path</label>
                <div style="display: flex; gap: 6px;">
                    <input type="text" class="settings-input" id="input-repo-path" value="" placeholder="Repository workspace folder" style="margin-bottom: 0;">
                    <button class="btn-secondary-action pixel-btn" id="btn-browse-repo-path" style="width: auto; padding: 0 10px;" title="Browse local repository folder">Browse</button>
                    <button class="btn-secondary-action pixel-btn" id="btn-reset-repo-path" style="width: auto; padding: 0 10px;" title="Reset to default workspace path">Reset</button>
                    <input type="file" id="repo-folder-picker" webkitdirectory directory style="display: none;">
                </div>
                <div class="settings-subtext">Local directory to scan, parse AST symbols, and map into SQLite.</div>
            </div>

            <button class="btn-primary-action" id="btn-scan-repo">
                <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
                Scan & Index Workspace
            </button>
            <div style="margin-top: 10px; padding: 8px; background: var(--surface-subtle); border-radius: 10px; font-size: 11px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                    <div>Status: <strong id="repo-status-label">Ready to scan</strong></div>
                </div>
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <div>Security: <strong id="repo-security-label" style="color: #4ade80;">Zero Leaked Secrets Detected</strong></div>
                    <button class="pixel-mini-btn" id="btn-security-scan" style="padding: 2px 7px; font-size: 10px;" title="Scan repository for leaked credentials and sensitive files">Audit</button>
                </div>
                <div id="repo-security-details" style="display: none; margin-top: 6px; padding: 6px 8px; background: rgba(0,0,0,0.2); border-radius: 6px; font-size: 10px; font-family: var(--font-mono); max-height: 120px; overflow-y: auto;">
                </div>
            </div>
        </div>
        <div class="task-card" style="margin-top: 10px;">
            <div class="task-header-row" style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                <div class="task-title" style="margin-bottom: 0;" id="hierarchy-card-title">Call Hierarchy & Relationships</div>
            </div>
            <pre style="font-family: var(--font-mono); font-size: 10.5px; line-height: 1.4; color: var(--text-secondary); background: var(--surface-subtle); padding: 8px; border-radius: 10px; overflow-x: auto; white-space: normal;" id="mermaid-code">No SQLite code graph available for this repository.
Click 'Scan & Index Workspace' to analyze codebase structure.</pre>
        </div>

        <!-- Repository Files Tree Card -->
        <div class="task-card repo-tree-card" style="margin-top: 10px;">
            <div class="task-header-row" style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                <div class="task-title" style="margin-bottom: 0;">Repository Files</div>
                <div class="pixel-badge" id="repo-tree-count">0 Files</div>
            </div>
            <div class="repo-tree-actions" style="display: flex; gap: 6px; margin-bottom: 8px;">
                <button class="pixel-mini-btn" id="btn-refresh-tree" title="Refresh file tree">↻ Refresh</button>
                <button class="pixel-mini-btn" id="btn-expand-tree" title="Toggle expand/collapse all">± Toggle All</button>
                <button class="pixel-mini-btn" id="btn-clear-file-selection" style="display: none;" title="Deselect active file">✕ Clear Selection</button>
            </div>
            <div class="repo-file-tree" id="repo-file-tree">
                <div class="tree-empty-state">
                    <span class="pixel-glyph">»</span> Loading repository files...
                </div>
            </div>
        </div>
    </div>

    <!-- 5. Settings View (Reference Styled: Execution, Security Mode, Terminal) -->
    <div id="view-settings" class="view-container">
        <!-- Section 0: Theme & IDE Bindings -->
        <div class="settings-section">
            <div class="settings-section-title">Theme &amp; IDE Bindings</div>
            <div class="settings-section-desc">Choose between the signature CLAY warm palette or adopt native VS Code editor tokens (var(--vscode-editor-background)).</div>
            <div class="theme-binding-grid" id="theme-mode-selector">
                <div class="theme-binding-card active" data-theme="claude" id="card-theme-claude">
                    <div class="theme-binding-badge">DEFAULT</div>
                    <div class="theme-binding-title">
                        <svg viewBox="0 0 16 16" width="12" height="12" fill="currentColor">
                            <path d="M8 1a7 7 0 1 0 0 14A7 7 0 0 0 8 1zm0 1.5v11A5.5 5.5 0 0 1 8 2.5z"/>
                        </svg>
                        CLAY Palette
                    </div>
                    <div class="theme-binding-desc">Signature warm minimalist canvas with custom CLAY Light &amp; Dark tones.</div>
                </div>
                <div class="theme-binding-card" data-theme="vscode" id="card-theme-vscode">
                    <div class="theme-binding-badge">IDE BINDING</div>
                    <div class="theme-binding-title">
                        <svg viewBox="0 0 16 16" width="12" height="12" fill="currentColor">
                            <path d="M14.5 1.5l-5 4.5v4l5 4.5v-13zM1.5 5.2L7 1.5v13L1.5 10.8V5.2z"/>
                        </svg>
                        IDE PALETTE
                    </div>
                    <div class="theme-binding-desc">Binds live to IDE editor tokens (var(--vscode-editor-background), foreground, diffs &amp; borders).</div>
                </div>
            </div>
        </div>

        <div class="settings-divider"></div>

        <!-- Section 1: Agent Security Mode -->
        <div class="settings-section">
            <div class="settings-section-title">Agent security mode</div>
            <div class="settings-section-desc">Select one of the three options. Agent settings and permissions can be further customized below.</div>
            <div class="security-mode-grid" id="security-mode-selector">
                <div class="security-mode-card" data-mode="full">
                    <div class="security-mode-title">Full access</div>
                    <div class="security-mode-desc">Executes shell commands across your system and modifies workspace files directly without containment or command safety filters.</div>
                </div>
                <div class="security-mode-card active" data-mode="sandboxed">
                    <div class="security-mode-title">Sandboxed</div>
                    <div class="security-mode-desc">Confines execution to active workspace, blocks destructive OS commands via safety guardrails, and gates network access.</div>
                </div>
                <div class="security-mode-card" data-mode="strict">
                    <div class="security-mode-title">Strict</div>
                    <div class="security-mode-desc">Zero-trust read-only containment: permits only non-mutating test &amp; diagnostic commands with strict file and secret isolation.</div>
                </div>
            </div>
        </div>

        <div class="settings-divider"></div>

        <!-- Section 2: Terminal -->
        <div class="settings-section">
            <div class="settings-section-title">Terminal</div>
            <div class="settings-card">
                <div class="settings-row">
                    <div class="settings-row-info">
                        <div class="settings-row-title">Sandbox Allow Network</div>
                        <div class="settings-row-desc">When enabled, sandboxed commands are allowed to make network requests.</div>
                    </div>
                    <label class="toggle-switch">
                        <input type="checkbox" id="chk-sandbox-network" checked>
                        <span class="toggle-slider"></span>
                    </label>
                </div>
            </div>
        </div>

        <div class="settings-divider"></div>

        <!-- Section 3: Ollama Daemon & Cloud Models -->
        <div class="settings-section">
            <div class="settings-section-title">Ollama Daemon & Cloud Models</div>
            <div class="settings-card">
                <div class="status-badge-container">
                    <span class="status-indicator" id="settings-status-indicator">
                        <span class="pill-dot"></span>
                        <span id="settings-status-text">Checking daemon...</span>
                    </span>
                    <span style="font-size: 10px; font-family: var(--font-mono); color: var(--text-muted);" id="settings-latency-text">-- ms</span>
                </div>

                <div class="settings-group">
                    <label class="settings-label">Ollama Provider / Connection Type</label>
                    <select class="settings-select" id="select-connection-type">
                        <option value="local" selected>Ollama Local (Background Daemon)</option>
                        <option value="cloud">Ollama Cloud (API Key)</option>
                    </select>
                    <div class="settings-subtext" id="connection-type-desc">Runs against local background Ollama (http://127.0.0.1:11434). Zero API keys required.</div>
                </div>

                <div class="settings-group" id="group-api-key" style="display: none;">
                    <label class="settings-label">Ollama Cloud API Key</label>
                    <div class="password-input-wrapper">
                        <input type="password" class="settings-input password-input" id="input-api-key" placeholder="Enter ollama.com API key (e.g. sk_...)" autocomplete="off" spellcheck="false">
                        <button type="button" class="btn-toggle-eye" id="btn-toggle-api-key" title="Toggle API key preview">
                            <svg id="eye-icon-show" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
                                <circle cx="12" cy="12" r="3"/>
                            </svg>
                            <svg id="eye-icon-hide" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" style="display: none;">
                                <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/>
                                <line x1="1" y1="1" x2="23" y2="23"/>
                            </svg>
                        </button>
                    </div>
                    <div class="settings-subtext">Direct cloud model access without running Ollama in the background. Authenticated with Bearer token.</div>
                </div>

                <div class="settings-group">
                    <label class="settings-label">Ollama Host URL</label>
                    <input type="text" class="settings-input" id="input-ollama-host" value="http://127.0.0.1:11434">
                </div>

                <div class="settings-group">
                    <label class="settings-label">Active Model</label>
                    <select class="settings-select" id="select-ollama-model">
                        <option value="gemma4:31b-cloud">gemma4:31b-cloud</option>
                        <option value="gemma4:cloud">gemma4:cloud</option>
                    </select>
                    <div class="settings-subtext">Zero API keys required. Authenticated via local Ollama.</div>
                </div>

                <button class="btn-secondary-action" id="btn-test-ollama" style="margin-bottom: 12px;">
                    <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/></svg>
                    Test Ollama Connection
                </button>

                <div class="settings-group" style="margin-top: 6px;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                        <label class="settings-label" style="margin-bottom: 0;">Inference Temperature</label>
                        <span style="font-family: var(--font-mono); font-size: 11px; font-weight: 700; color: var(--primary-terracotta);" id="val-temperature">0.20</span>
                    </div>
                    <input type="range" class="settings-slider" id="input-temperature" min="0" max="1" step="0.05" value="0.20">
                    <div class="settings-subtext">Lower values (0.1 - 0.3) provide precise, deterministic code generation.</div>
                </div>

                <div class="settings-group" style="margin-top: 10px;">
                    <label class="settings-label">Context Window Length</label>
                    <select class="settings-select" id="select-context-window">
                        <option value="8192">8,192 tokens (8k)</option>
                        <option value="16384">16,384 tokens (16k)</option>
                        <option value="32768" selected>32,768 tokens (32k - Recommended)</option>
                        <option value="65536">65,536 tokens (64k)</option>
                        <option value="131072">131,072 tokens (128k)</option>
                    </select>
                </div>

                <div class="settings-group" style="margin-top: 10px;">
                    <label class="settings-label">Max Output Tokens</label>
                    <select class="settings-select" id="select-max-tokens">
                        <option value="1024">1,024 tokens</option>
                        <option value="2048" selected>2,048 tokens</option>
                        <option value="4096">4,096 tokens</option>
                        <option value="8192">8,192 tokens</option>
                    </select>
                </div>

                <div class="settings-group" style="margin-top: 10px;">
                    <label class="settings-label">Privacy Redaction Patterns</label>
                    <input type="text" class="settings-input" id="input-privacy-patterns" value=".env, *.pem, credentials.json, id_rsa, *.key">
                    <div class="settings-subtext">Files and patterns automatically scrubbed before sending prompts.</div>
                </div>

                <div class="settings-row" style="margin-top: 6px; margin-bottom: 12px;">
                    <div class="settings-row-info">
                        <div class="settings-row-title">Zero-Leak Privacy Guard</div>
                        <div class="settings-row-desc">Redacts secrets and tokens prior to transmission</div>
                    </div>
                    <label class="toggle-switch">
                        <input type="checkbox" id="chk-privacy-guard" checked>
                        <span class="toggle-slider"></span>
                    </label>
                </div>

                <button class="btn-primary-action" id="btn-save-all-settings" style="margin-top: 8px;">
                    Save All Settings
                </button>
            </div>
        </div>
    </div>

    <!-- Chat Input Dock (Rounder, Cleared) -->
    <div class="input-dock">
        <!-- Floating Mention Autocomplete Popup -->
        <div class="mention-autocomplete-popup" id="mention-autocomplete-popup" style="display: none;">
            <div class="mention-autocomplete-header">
                <span class="mention-autocomplete-title">MENTION CONTEXT</span>
                <span class="mention-autocomplete-hint">ARROWS NAVIGATE // ENTER TO PIN</span>
            </div>
            <div class="mention-autocomplete-list" id="mention-autocomplete-list"></div>
        </div>

        <!-- Pixel Style Transparent Jump Down Button (Centered) -->
        <button class="chat-jump-down-btn" id="btn-chat-jump-down" title="Jump to bottom" aria-label="Jump to latest message">
            <svg viewBox="0 0 10 10" width="10" height="10" fill="currentColor" shape-rendering="crispEdges">
                <path d="M1,2 h2 v2 h-2 z M7,2 h2 v2 h-2 z M2,4 h2 v2 h-2 z M6,4 h2 v2 h-2 z M3,6 h2 v2 h-2 z M5,6 h2 v2 h-2 z M4,8 h2 v2 h-2 z"/>
            </svg>
        </button>
        <div class="input-container">
            <!-- Pinned Mentions Chips Tray (Option B) -->
            <div class="mention-chips-tray" id="mention-chips-tray" style="display: none;"></div>
            <textarea class="chat-textarea" id="chat-input" placeholder="Start typing to mold CLAY (@ to mention file or symbol)"></textarea>
            <div class="input-toolbar">
                <div class="input-meta-hint">
                    <span class="pill-dot"></span> <span id="input-model-hint">CLAY // gemma4:31b-cloud</span>
                </div>
                <button class="send-btn" id="btn-send" title="Send (Enter)">
                    <svg viewBox="0 0 16 16" width="13" height="13" fill="currentColor" shape-rendering="crispEdges">
                        <path d="M2,7 h6 v2 h-6 z M8,5 h2 v6 h-2 z M10,6 h2 v4 h-2 z M12,7 h2 v2 h-2 z"/>
                    </svg>
                </button>
            </div>
        </div>
    </div>

    <!-- Bottom 5-Tab Navigation Bar -->
    <nav class="nav-tab-bar">
        <button class="nav-tab active-tab" data-target="chat">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
            Chat
        </button>
        <button class="nav-tab" data-target="agent">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4"/></svg>
            Agent
        </button>
        <button class="nav-tab" data-target="diff">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="9" y1="15" x2="15" y2="15"/></svg>
            Diff
        </button>
        <button class="nav-tab" data-target="repo">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
            Repo
        </button>
        <button class="nav-tab" data-target="settings">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
            Settings
        </button>
    </nav>
    </div> <!-- /.app-frame -->

    <script nonce="${nonce}">
        window.__CLAY_BACKEND_URL__ = "${activeBackendUrl}";
    </script>
    <script nonce="${nonce}" src="${scriptUri}"></script>
</body>
</html>`;
    }
}

function getNonce() {
    let text = '';
    const possible = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
    for (let i = 0; i < 32; i++) {
        text += possible.charAt(Math.floor(Math.random() * possible.length));
    }
    return text;
}
