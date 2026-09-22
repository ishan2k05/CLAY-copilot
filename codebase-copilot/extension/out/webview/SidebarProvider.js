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
exports.SidebarProvider = void 0;
const vscode = __importStar(require("vscode"));
class SidebarProvider {
    _extensionUri;
    _backendClient;
    static viewType = 'codebaseCopilot.sidebarView';
    _view;
    constructor(_extensionUri, _backendClient) {
        this._extensionUri = _extensionUri;
        this._backendClient = _backendClient;
    }
    resolveWebviewView(webviewView, _context, _token) {
        this._view = webviewView;
        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [
                vscode.Uri.joinPath(this._extensionUri, 'resources'),
                vscode.Uri.joinPath(this._extensionUri, 'src', 'webview', 'media'),
                vscode.Uri.joinPath(this._extensionUri, 'out')
            ]
        };
        webviewView.webview.html = this._getHtmlForWebview(webviewView.webview);
        // Handle messages from Webview
        webviewView.webview.onDidReceiveMessage(async (data) => {
            switch (data.command) {
                case 'userQuery': {
                    await this._handleUserQuery(data.text, data.workspace_path, data.security_mode);
                    break;
                }
                case 'createAgentPlan': {
                    const workspaceFolder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
                    try {
                        const plan = await this._backendClient.createPlan(data.goal, workspaceFolder);
                        this._view?.webview.postMessage({ command: 'agentPlanResult', plan });
                    }
                    catch (err) {
                        vscode.window.showErrorMessage(`Planning failed: ${err.message}`);
                    }
                    break;
                }
                case 'scanWorkspace': {
                    const workspaceFolder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || '/home/ishan/.gemini/antigravity-ide/scratch/codebase-copilot';
                    try {
                        const status = await this._backendClient.triggerIndexing(workspaceFolder);
                        const mermaid = await this._backendClient.getMermaidGraph();
                        this._view?.webview.postMessage({ command: 'scanResult', status, mermaid });
                        vscode.window.showInformationMessage(`Indexed ${status.filesCount} files and ${status.symbolsCount} symbols.`);
                    }
                    catch (err) {
                        vscode.window.showErrorMessage(`Scanning failed: ${err.message}`);
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
                    const res = await this._backendClient.updateSettings(data.settings);
                    this._view?.webview.postMessage({
                        command: 'settingsSaved',
                        result: res
                    });
                    vscode.window.showInformationMessage(`Codebase Copilot: Active model set to ${data.settings.model || 'default'}`);
                    break;
                }
                case 'applyPatch': {
                    try {
                        await this._backendClient.applyPatch(data.file, data.diff);
                        vscode.window.showInformationMessage(`Codebase Copilot: Patch applied cleanly to ${data.file}`);
                    }
                    catch (err) {
                        vscode.window.showErrorMessage(`Failed to apply patch: ${err.message}`);
                    }
                    break;
                }
                case 'loadMermaid': {
                    const mermaidSyntax = await this._backendClient.getMermaidGraph();
                    this._view?.webview.postMessage({ command: 'renderMermaid', mermaid: mermaidSyntax });
                    break;
                }
                case 'acceptChanges': {
                    await this._backendClient.reviewChanges('TASK #0428', 'accept');
                    vscode.window.showInformationMessage('Codebase Copilot: Staged changes accepted.');
                    break;
                }
                case 'rejectChanges': {
                    await this._backendClient.reviewChanges('TASK #0428', 'reject');
                    vscode.window.showWarningMessage('Codebase Copilot: Staged changes rejected.');
                    break;
                }
            }
        });
    }
    async _handleUserQuery(query, workspacePath, securityMode) {
        const editor = vscode.window.activeTextEditor;
        const activeFileContext = editor ? editor.document.fileName : undefined;
        await this._backendClient.streamChat(query, activeFileContext, (token) => {
            this._view?.webview.postMessage({ command: 'streamToken', token });
        }, (meta) => {
            this._view?.webview.postMessage({ command: 'streamComplete', diff: meta.diff, citations: meta.citations });
        }, (err) => {
            this._view?.webview.postMessage({
                command: 'streamToken',
                token: `\n\n[Error: ${err}]`
            });
        }, workspacePath, securityMode);
    }
    _getHtmlForWebview(webview) {
        const styleUri = webview.asWebviewUri(vscode.Uri.joinPath(this._extensionUri, 'src', 'webview', 'media', 'style.css'));
        const scriptUri = webview.asWebviewUri(vscode.Uri.joinPath(this._extensionUri, 'src', 'webview', 'media', 'main.js'));
        const nonce = getNonce();
        return `<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}';">
    <link href="${styleUri}" rel="stylesheet">
    <title>Codebase Copilot</title>
</head>
<body>
    <div class="app-frame">
        <!-- Header Context Bar -->
        <div class="context-bar">
        <div class="brand-crumb">
            <div class="clay-brand-badge">
                <svg viewBox="0 0 163 178" class="clay-cube-icon" shape-rendering="crispEdges">
                    <path fill="#C15C3D" d="M82,6h1v6h-1z M75,7h7v5h-7z M83,7h5v5h-5z M69,10h6v3h-6z M88,10h6v3h-6z M68,11h1v5h-1z M94,12h1v4h-1z M62,13h6v4h-6z M69,13h5v3h-5z M89,13h5v3h-5z M95,13h6v4h-6z M61,16h1v3h-1z M101,16h1v3h-1z M55,17h6v5h-6z M62,17h5v2h-5z M96,17h5v2h-5z M102,17h6v5h-6z M54,19h1v4h-1z M50,20h4v6h-4z M108,20h5v3h-5z M55,22h5v1h-5z M103,22h5v1h-5z M49,23h1v3h-1z M109,23h10v3h-10z M43,24h6v2h-6z M119,24h1v5h-1z M42,26h6v3h-6z M115,26h4v3h-4z M120,26h1v4h-1z M38,27h4v6h-4z M121,27h4v6h-4z M31,30h7v3h-7z M125,30h7v3h-7z M30,33h5v3h-5z M127,33h6v1h-6z M25,34h5v2h-5z M128,34h11v2h-11z M24,35h1v4h-1z M25,36h4v3h-4z M134,36h5v1h-5z M18,37h6v2h-6z M135,37h10v2h-10z M17,39h6v1h-6z M140,39h6v1h-6z M12,40h2v10h-2z M15,40h7v2h-7z M141,40h11v2h-11z M11,41h1v9h-1z M14,41h1v11h-1z M15,42h6v1h-6z M142,42h10v1h-10z M10,43h1v7h-1z M148,43h5v95h-5z M6,44h4v90h-4z M153,44h4v90h-4z M15,47h5v6h-5z M143,47h5v48h-5z M142,48h1v47h-1z M20,50h1v4h-1z M141,50h1v45h-1z M21,51h6v5h-6z M136,51h5v26h-5z M27,54h6v3h-6z M130,54h6v20h-6z M33,56h1v4h-1z M129,56h1v19h-1z M28,57h5v3h-5z M34,57h6v5h-6z M128,57h1v18h-1z M123,58h5v17h-5z M40,60h1v3h-1z M122,60h1v91h-1z M41,61h6v5h-6z M116,61h6v93h-6z M35,62h5v1h-5z M47,64h7v3h-7z M109,64h7v18h-7z M48,67h7v3h-7z M108,67h1v17h-1z M55,68h6v5h-6z M102,68h6v17h-6z M21,71h2v6h-2z M61,71h6v4h-6z M96,71h6v20h-6z M23,72h2v8h-2z M25,73h2v7h-2z M67,73h1v4h-1z M95,73h1v41h-1z M27,74h2v10h-2z M68,74h6v6h-6z M89,74h6v41h-6z M130,74h1v1h-1z M134,74h2v1h-2z M29,75h1v14h-1z M62,75h5v2h-5z M123,75h4v1h-4z M123,76h2v1h-2z M30,77h1v13h-1z M74,77h1v3h-1z M87,77h2v95h-2z M123,77h1v74h-1z M138,77h3v17h-3z M31,78h2v15h-2z M75,78h12v5h-12z M33,79h1v18h-1z M26,80h1v3h-1z M34,81h1v17h-1z M109,82h3v1h-3z M114,82h2v1h-2z M76,83h11v1h-11z M109,83h2v1h-2z M115,83h1v72h-1z M28,84h1v3h-1z M35,84h1v16h-1z M79,84h8v88h-8z M126,84h3v40h-3z M36,85h1v15h-1z M102,85h3v2h-3z M124,85h2v41h-2z M102,87h1v3h-1z M37,88h3v13h-3z M114,89h1v44h-1z M40,90h1v28h-1z M41,91h1v26h-1z M96,91h5v3h-5z M111,91h3v41h-3z M110,92h1v41h-1z M32,93h1v1h-1z M109,93h1v40h-1z M42,94h1v23h-1z M96,94h4v16h-4z M139,94h2v1h-2z M43,95h1v22h-1z M144,95h4v43h-4z M129,97h1v14h-1z M44,98h1v16h-1z M130,99h1v8h-1z M45,100h1v14h-1z M131,100h1v5h-1z M38,101h2v18h-2z M46,101h1v13h-1z M132,101h1v3h-1z M133,102h1v1h-1z M143,102h1v39h-1z M141,103h2v38h-2z M47,105h2v5h-2z M140,105h1v39h-1z M37,106h1v13h-1z M49,106h1v4h-1z M139,106h1v38h-1z M36,107h1v14h-1z M108,107h1v13h-1z M138,107h1v37h-1z M35,108h1v13h-1z M34,109h1v12h-1z M107,109h1v10h-1z M31,110h3v12h-3z M47,110h1v1h-1z M96,110h3v1h-3z M30,111h1v13h-1z M96,111h2v3h-2z M29,112h1v14h-1z M106,112h1v6h-1z M28,113h1v13h-1z M27,114h1v12h-1z M105,114h1v3h-1z M24,115h3v10h-3z M89,115h5v53h-5z M23,116h1v8h-1z M21,117h2v7h-2z M94,118h1v48h-1z M31,122h2v2h-2z M96,122h2v43h-2z M95,123h1v42h-1z M98,123h1v42h-1z M99,124h1v41h-1z M126,124h2v1h-2z M25,125h2v1h-2z M136,125h2v19h-2z M124,126h1v1h-1z M135,128h1v17h-1z M45,129h2v7h-2z M134,129h1v19h-1z M47,130h3v8h-3z M133,130h1v18h-1z M50,131h1v8h-1z M10,132h5v6h-5z M51,132h3v7h-3z M111,132h1v1h-1z M132,132h1v16h-1z M54,133h1v8h-1z M129,133h3v15h-3z M7,134h3v3h-3z M55,134h2v7h-2z M126,134h3v16h-3z M153,134h3v2h-3z M15,135h3v3h-3z M57,135h3v8h-3z M124,135h2v16h-2z M18,136h2v5h-2z M46,136h1v1h-1z M60,136h1v8h-1z M153,136h2v2h-2z M8,137h2v1h-2z M61,137h3v8h-3z M16,138h2v3h-2z M20,138h1v4h-1z M64,138h1v8h-1z M144,138h3v3h-3z M21,139h6v6h-6z M52,139h2v1h-2z M65,139h2v7h-2z M53,140h1v1h-1z M67,140h1v2h-1z M100,140h1v25h-1z M113,140h2v18h-2z M56,141h1v1h-1z M101,141h1v22h-1z M112,141h1v17h-1z M141,141h1v3h-1z M27,142h6v3h-6z M102,142h1v20h-1z M108,142h4v16h-4z M103,143h1v18h-1z M107,143h1v18h-1z M33,144h1v4h-1z M104,144h3v17h-3z M28,145h5v3h-5z M34,145h1v5h-1z M36,145h4v6h-4z M63,145h1v1h-1z M35,146h1v5h-1z M66,146h1v1h-1z M40,147h1v4h-1z M41,149h6v4h-6z M126,150h2v1h-2z M47,152h7v3h-7z M42,153h5v2h-5z M116,154h5v1h-5z M48,155h7v3h-7z M55,156h6v5h-6z M54,158h1v1h-1z M108,158h1v1h-1z M61,159h6v4h-6z M67,160h1v5h-1z M60,161h1v1h-1z M68,162h1v4h-1z M62,163h5v2h-5z M69,163h3v5h-3z M72,164h1v4h-1z M78,165h1v7h-1z M73,166h5v5h-5z M89,168h1v3h-1z M74,171h4v1h-4z"/>
                </svg>
                <span class="brand-title">CLAY</span>
            </div>
        </div>
        <div class="context-pills">
            <button class="pill-theme-switch" id="btn-theme-toggle" title="Toggle Light / Claude Dark Mode" aria-label="Toggle theme">
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

    <!-- 1. Chat View (Active by default) -->
    <div id="view-chat" class="view-container active-view">
        <div class="message-list" id="message-list">
            <div class="empty-state-card" id="chat-empty-state">
                <div class="clay-hero-logo">
                    <svg viewBox="0 0 512 512" shape-rendering="crispEdges">
                        <rect width="512" height="512" fill="#FAF6F0" rx="24"/>
                        <path fill="#C15C3D" d="M257,35h2v12h-2z M243,37h14v10h-14z M259,37h10v10h-10z M231,43h12v6h-12z M269,43h12v6h-12z M229,45h2v10h-2z M281,47h2v8h-2z M217,49h12v8h-12z M231,49h10v6h-10z M271,49h10v6h-10z M283,49h12v8h-12z M215,55h2v6h-2z M295,55h2v6h-2z M203,57h12v10h-12z M217,57h10v4h-10z M285,57h10v4h-10z M297,57h12v10h-12z M201,61h2v8h-2z M193,63h8v12h-8z M309,63h10v6h-10z M203,67h10v2h-10z M299,67h10v2h-10z M191,69h2v6h-2z M311,69h20v6h-20z M179,71h12v4h-12z M331,71h2v10h-2z M177,75h12v6h-12z M323,75h8v6h-8z M333,75h2v8h-2z M169,77h8v12h-8z M335,77h8v12h-8z M155,83h14v6h-14z M343,83h14v6h-14z M153,89h10v6h-10z M347,89h12v2h-12z M143,91h10v4h-10z M349,91h22v4h-22z M141,93h2v8h-2z M143,95h8v6h-8z M361,95h10v2h-10z M129,97h12v4h-12z M363,97h20v4h-20z M127,101h12v2h-12z M373,101h12v2h-12z M117,103h4v20h-4z M123,103h14v4h-14z M375,103h22v4h-22z M115,105h2v18h-2z M121,105h2v22h-2z M123,107h12v2h-12z M377,107h20v2h-20z M113,109h2v14h-2z M389,109h10v190h-10z M105,111h8v180h-8z M399,111h8v180h-8z M123,117h10v12h-10z M379,117h10v96h-10z M377,119h2v94h-2z M133,123h2v8h-2z M375,123h2v90h-2z M135,125h12v10h-12z M365,125h10v52h-10z M147,131h12v6h-12z M353,131h12v40h-12z M159,135h2v8h-2z M351,135h2v38h-2z M149,137h10v6h-10z M161,137h12v10h-12z M349,137h2v36h-2z M339,139h10v34h-10z M173,143h2v6h-2z M337,143h2v182h-2z M175,145h12v10h-12z M325,145h12v186h-12z M163,147h10v2h-10z M187,151h14v6h-14z M311,151h14v36h-14z M189,157h14v6h-14z M309,157h2v34h-2z M203,159h12v10h-12z M297,159h12v34h-12z M135,165h4v12h-4z M215,165h12v8h-12z M285,165h12v40h-12z M139,167h4v16h-4z M143,169h4v14h-4z M227,169h2v8h-2z M283,169h2v82h-2z M147,171h4v20h-4z M229,171h12v12h-12z M271,171h12v82h-12z M353,171h2v2h-2z M361,171h4v2h-4z M151,173h2v28h-2z M217,173h10v4h-10z M339,173h8v2h-8z M339,175h4v2h-4z M153,177h2v26h-2z M241,177h2v6h-2z M267,177h4v190h-4z M339,177h2v148h-2z M369,177h6v34h-6z M155,179h4v30h-4z M243,179h24v10h-24z M159,181h2v36h-2z M145,183h2v6h-2z M161,185h2v34h-2z M311,187h6v2h-6z M321,187h4v2h-4z M245,189h22v2h-22z M311,189h4v2h-4z M323,189h2v144h-2z M149,191h2v6h-2z M163,191h2v32h-2z M251,191h16v176h-16z M345,191h6v80h-6z M165,193h2v30h-2z M297,193h6v4h-6z M341,193h4v82h-4z M297,197h2v6h-2z M167,199h6v26h-6z M321,201h2v88h-2z M173,203h2v56h-2z M175,205h2v52h-2z M285,205h10v6h-10z M315,205h6v82h-6z M313,207h2v82h-2z M157,209h2v2h-2z M311,209h2v80h-2z M177,211h2v46h-2z M285,211h8v32h-8z M371,211h4v2h-4z M179,213h2v44h-2z M381,213h8v86h-8z M351,217h2v28h-2z M181,219h2v32h-2z M353,221h2v16h-2z M183,223h2v28h-2z M355,223h2v10h-2z M169,225h4v36h-4z M185,225h2v26h-2z M357,225h2v6h-2z M359,227h2v2h-2z M379,227h2v78h-2z M375,229h4v76h-4z M187,233h4v10h-4z M373,233h2v78h-2z M167,235h2v26h-2z M191,235h2v8h-2z M371,235h2v76h-2z M165,237h2v28h-2z M309,237h2v26h-2z M369,237h2v74h-2z M163,239h2v26h-2z M161,241h2v24h-2z M307,241h2v20h-2z M155,243h6v24h-6z M187,243h2v2h-2z M285,243h6v2h-6z M153,245h2v26h-2z M285,245h4v6h-4z M151,247h2v28h-2z M305,247h2v12h-2z M149,249h2v26h-2z M147,251h2v24h-2z M303,251h2v6h-2z M141,253h6v20h-6z M271,253h10v106h-10z M139,255h2v16h-2z M135,257h4v14h-4z M281,259h2v96h-2z M155,267h4v4h-4z M285,267h4v86h-4z M283,269h2v84h-2z M289,269h2v84h-2z M291,271h2v82h-2z M345,271h4v2h-4z M143,273h4v2h-4z M365,273h4v38h-4z M341,275h2v2h-2z M363,279h2v34h-2z M183,281h4v14h-4z M361,281h2v38h-2z M187,283h6v16h-6z M359,283h2v36h-2z M193,285h2v16h-2z M113,287h10v12h-10z M195,287h6v14h-6z M315,287h2v2h-2z M357,287h2v32h-2z M201,289h2v16h-2z M351,289h6v30h-6z M107,291h6v6h-6z M203,291h4v14h-4z M345,291h6v32h-6z M399,291h6v4h-6z M123,293h6v6h-6z M207,293h6v16h-6z M341,293h4v32h-4z M129,295h4v10h-4z M185,295h2v2h-2z M213,295h2v16h-2z M399,295h4v4h-4z M109,297h4v2h-4z M215,297h6v16h-6z M125,299h4v6h-4z M133,299h2v8h-2z M221,299h2v16h-2z M381,299h6v6h-6z M135,301h12v12h-12z M197,301h4v2h-4z M223,301h4v14h-4z M199,303h2v2h-2z M227,303h2v4h-2z M293,303h2v50h-2z M319,303h4v36h-4z M205,305h2v2h-2z M295,305h2v44h-2z M317,305h2v34h-2z M375,305h2v6h-2z M147,307h12v6h-12z M297,307h2v40h-2z M309,307h8v32h-8z M299,309h2v36h-2z M307,309h2v36h-2z M159,311h2v8h-2z M301,311h6v34h-6z M149,313h10v6h-10z M161,313h2v10h-2z M165,313h8v12h-8z M219,313h2v2h-2z M163,315h2v10h-2z M225,315h2v2h-2z M173,317h2v8h-2z M175,321h12v8h-12z M345,323h4v2h-4z M187,327h14v6h-14z M177,329h10v4h-10z M325,331h10v2h-10z M189,333h14v6h-14z M203,335h12v10h-12z M201,339h2v2h-2z M309,339h2v2h-2z M215,341h12v8h-12z M227,343h2v10h-2z M213,345h2v2h-2z M229,347h2v8h-2z M217,349h10v4h-10z M231,349h6v10h-6z M237,351h2v8h-2z M249,353h2v14h-2z M239,355h10v10h-10z M271,359h2v6h-2z M241,365h8v2h-8z M127,401h30v10h-30z M193,401h10v76h-10z M289,401h14v14h-14z M337,401h6v6h-6z M345,401h4v18h-4z M395,401h10v6h-10z M123,403h4v12h-4z M157,403h2v14h-2z M287,403h2v28h-2z M303,403h2v28h-2z M343,403h2v12h-2z M393,403h2v20h-2z M405,403h2v2h-2z M119,405h4v16h-4z M159,405h4v14h-4z M349,405h2v20h-2z M391,405h2v22h-2z M117,407h2v20h-2z M163,407h2v16h-2z M285,407h2v30h-2z M305,407h2v28h-2z M339,407h4v2h-4z M351,407h2v22h-2z M395,407h8v4h-8z M115,409h2v60h-2z M165,409h2v14h-2z M341,409h2v6h-2z M389,409h2v22h-2z M113,411h2v56h-2z M127,411h4v2h-4z M151,411h6v2h-6z M167,411h2v12h-2z M307,411h2v28h-2z M353,411h2v22h-2z M387,411h2v22h-2z M395,411h6v4h-6z M111,413h2v52h-2z M155,413h2v2h-2z M169,413h2v10h-2z M283,413h2v28h-2z M309,413h2v30h-2z M355,413h2v22h-2z M123,415h2v4h-2z M171,415h2v8h-2z M289,415h6v2h-6z M299,415h4v4h-4z M385,415h2v22h-2z M395,415h2v6h-2z M109,417h2v44h-2z M281,417h2v42h-2z M289,417h4v6h-4z M357,417h2v20h-2z M161,419h2v4h-2z M173,419h2v4h-2z M301,419h2v6h-2z M311,419h2v40h-2z M347,419h2v2h-2z M359,419h2v22h-2z M383,419h2v20h-2z M107,421h2v36h-2z M119,421h2v2h-2z M279,421h2v38h-2z M381,421h2v20h-2z M289,423h2v4h-2z M361,423h2v22h-2z M105,425h2v28h-2z M313,425h2v34h-2z M379,425h2v20h-2z M277,427h2v32h-2z M363,427h2v20h-2z M377,427h2v20h-2z M315,429h2v30h-2z M365,429h2v46h-2z M375,429h2v48h-2z M275,431h2v30h-2z M317,433h2v30h-2z M367,433h2v44h-2z M373,433h2v44h-2z M273,435h2v30h-2z M369,435h4v42h-4z M271,439h2v32h-2z M319,439h2v30h-2z M269,445h2v30h-2z M321,445h2v28h-2z M309,447h2v12h-2z M323,447h2v28h-2z M117,449h2v22h-2z M283,449h26v10h-26z M267,451h2v26h-2z M163,453h10v10h-10z M325,453h2v24h-2z M119,455h2v18h-2z M161,455h2v18h-2z M173,455h2v4h-2z M121,457h2v16h-2z M265,457h2v20h-2z M327,457h2v20h-2z M123,459h2v14h-2z M159,459h2v14h-2z M263,459h2v18h-2z M125,461h2v14h-2z M157,461h2v14h-2z M329,461h2v16h-2z M127,463h2v12h-2z M153,463h4v12h-4z M163,463h8v2h-8z M261,463h2v14h-2z M129,465h2v10h-2z M151,465h2v12h-2z M163,465h6v2h-6z M203,465h4v12h-4z M131,467h20v10h-20z M163,467h4v2h-4z M207,467h38v10h-38z M331,467h2v10h-2z M163,469h2v2h-2z M259,469h2v8h-2z M333,471h2v6h-2z M257,473h2v2h-2z M153,475h2v2h-2z"/>
                    </svg>
                </div>
                <div class="empty-state-title">CLAY Intelligence Engine</div>
                <div class="empty-state-desc">Autonomous repository agent & surgical engineering copilot. Running on <strong id="welcome-model-text">gemma4:31b-cloud</strong>.</div>
            </div>
        </div>
    </div>

    <!-- 2. Agent Task View -->
    <div id="view-agent" class="view-container">
        <!-- Top: Agent Console (Longer/Taller length) -->
        <div class="terminal-widget agent-console-widget">
            <div class="terminal-header">
                <div style="display: flex; align-items: center; gap: 6px;">
                    <span>agent-console</span>
                    <span style="font-size: 9px; color: #555;">// workspace</span>
                </div>
                <div class="terminal-actions">
                    <span class="terminal-os-badge" id="terminal-os-badge" style="font-family: var(--font-pixel); font-size: 7.5px; color: var(--primary-terracotta); background: rgba(204,107,73,0.12); padding: 2px 6px; border-radius: 4px; letter-spacing: 0.2px;">DETECTING OS...</span>
                    <span style="color: #4CAF50; font-weight: 600;" id="agent-terminal-status">READY</span>
                    <button class="terminal-action-btn" id="btn-clear-terminal" title="Clear Console">clear</button>
                    <button class="terminal-action-btn" id="btn-copy-terminal" title="Copy Log">copy</button>
                </div>
            </div>
            <div class="terminal-stream" id="terminal-stream"><span class="cmd-line">$ clay-agent ready</span>
[clay] Waiting for task execution goal...</div>

            <!-- Interactive CLI Command Bar -->
            <div class="terminal-cli-bar">
                <span class="terminal-prompt-symbol">$</span>
                <input type="text" class="terminal-cli-input" id="input-terminal-cmd" placeholder="Run safe shell command in workspace (e.g. pytest, python3 solution.py)..." spellcheck="false" autocomplete="off">
                <button class="terminal-cli-exec-btn" id="btn-exec-terminal-cmd">Run</button>
            </div>

            <!-- Quick Command Chips -->
            <div class="terminal-quick-chips" id="terminal-quick-chips">
                <span class="terminal-quick-chips-label">Quick:</span>
                <span class="cli-chip" data-cmd="python3 -m py_compile backend/app/main.py">syntax check</span>
                <span class="cli-chip" data-cmd="pytest">pytest</span>
                <span class="cli-chip" data-cmd="git status">git status</span>
                <span class="cli-chip" data-cmd="ls -la">ls -la</span>
            </div>
        </div>

        <!-- Bottom: User Console (CLI Task Planner + Embedded Execution Task List) -->
        <div class="terminal-widget user-console-widget">
            <div class="terminal-header">
                <div style="display: flex; align-items: center; gap: 6px;">
                    <span style="color: var(--primary-terracotta); font-weight: 700;">user-console</span>
                    <span style="font-size: 9px; color: #666;">// task planner</span>
                </div>
                <div class="terminal-actions">
                    <span style="color: #888; font-size: 9.5px; font-family: var(--font-mono);">AUTONOMOUS</span>
                </div>
            </div>

            <!-- CLI Style Task Planner Input -->
            <div class="user-console-cli-bar">
                <span class="user-console-prompt">&gt;</span>
                <input type="text" class="user-task-input" id="agent-goal-input" placeholder="Enter task goal (e.g. Refactor error handling or add unit tests)..." spellcheck="false" autocomplete="off">
                <button class="btn-run-task" id="btn-run-agent" title="Execute Task">
                    <svg viewBox="0 0 24 24" width="10" height="10" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                    Run Task
                </button>
                <button class="btn-stop-task" id="btn-stop-agent" style="display: none;" title="Stop Task">
                    <svg viewBox="0 0 24 24" width="10" height="10" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>
                    Stop
                </button>
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
                <div class="terminal-step-empty">No active task. Enter a goal above and click Run Task to execute.</div>
            </div>
        </div>
    </div>

    <!-- 3. Diff Reviewer View -->
    <div id="view-diff" class="view-container">
        <div class="diff-summary-bar">
            <span id="diff-summary-label">Staged Changes // 0 files</span>
            <div>
                <span class="diff-badge-add" id="diff-badge-add">+0</span>
                <span class="diff-badge-del" id="diff-badge-del">-0</span>
            </div>
        </div>

        <div id="diff-staged-container">
            <div style="text-align: center; padding: 36px 12px; color: var(--text-muted);">
                <svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" stroke-width="1.5" style="margin-bottom: 8px; opacity: 0.5;"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                <div style="font-weight: 600; font-size: 12px; color: var(--text-secondary);">No Staged Changes</div>
                <div style="font-size: 11px; margin-top: 4px;">Changes proposed by Chat or Agent will appear here for review.</div>
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
                    <input type="text" class="settings-input" id="input-repo-path" value="/home/ishan/.gemini/antigravity-ide/scratch/codebase-copilot/backend" placeholder="/absolute/path/to/repo" style="margin-bottom: 0;">
                    <button class="btn-secondary-action" id="btn-reset-repo-path" style="width: auto; padding: 0 10px;" title="Reset to backend">Reset</button>
                </div>
                <div class="settings-subtext">Local directory to scan, parse AST symbols, and map into SQLite.</div>
            </div>

            <button class="btn-primary-action" id="btn-scan-repo">
                <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
                Scan & Index Workspace
            </button>
            <div style="margin-top: 10px; padding: 8px; background: var(--surface-subtle); border-radius: 10px; font-size: 11px;">
                Status: <strong id="repo-status-label">Ready to scan</strong><br/>
                Security: <strong>Zero Leaked Secrets Detected</strong>
            </div>
        </div>
        <div class="task-card" style="margin-top: 10px;">
            <div class="task-title">Call Hierarchy & Relationships</div>
            <pre style="font-family: var(--font-mono); font-size: 10.5px; line-height: 1.4; color: var(--text-secondary); background: var(--surface-subtle); padding: 8px; border-radius: 10px; overflow-x: auto; white-space: pre;" id="mermaid-code">Click 'Scan & Index Workspace' to analyze codebase structure.</pre>
        </div>
    </div>

    <!-- 5. Settings View (Reference Styled: Execution, Security Mode, Terminal) -->
    <div id="view-settings" class="view-container">
        <!-- Section 1: Execution -->
        <div class="settings-section">
            <div class="settings-section-title">Execution</div>
            <div class="settings-card">
                <div class="settings-row">
                    <div class="settings-row-info">
                        <div class="settings-row-title">Queued Messages</div>
                        <div class="settings-row-desc">Configure when follow-up messages are sent.</div>
                        <div class="settings-row-meta" id="btn-kb-shortcuts" title="Keyboard Shortcuts">
                            Keyboard shortcuts
                            <svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.5">
                                <circle cx="8" cy="8" r="6"/>
                                <path d="M8 7v4M8 5h.01"/>
                            </svg>
                        </div>
                    </div>
                    <div class="segmented-toggle" id="toggle-queued-messages">
                        <button class="segmented-toggle-btn active" data-value="queue">Queue</button>
                        <button class="segmented-toggle-btn" data-value="immediate">Send Immediately</button>
                    </div>
                </div>
            </div>
        </div>

        <!-- Section 2: Agent Security Mode -->
        <div class="settings-section">
            <div class="settings-section-title">Agent security mode</div>
            <div class="settings-section-desc">Select one of the three options. Agent settings and permissions can be further customized below.</div>
            <div class="security-mode-grid" id="security-mode-selector">
                <div class="security-mode-card" data-mode="full">
                    <div class="security-mode-title">Full access</div>
                    <div class="security-mode-desc">Agents have full access to your machine and external resources.</div>
                </div>
                <div class="security-mode-card active" data-mode="sandboxed">
                    <div class="security-mode-title">Sandboxed</div>
                    <div class="security-mode-desc">Agents run in a secure sandbox that restricts access to external resources outside of your trusted folders.</div>
                </div>
                <div class="security-mode-card" data-mode="strict">
                    <div class="security-mode-title">Strict</div>
                    <div class="security-mode-desc">Terminal commands always require review and the agent cannot access files outside of its given workspaces.</div>
                </div>
            </div>
        </div>

        <div class="settings-divider"></div>

        <!-- Section 3: Terminal Controls -->
        <div class="settings-section">
            <div class="settings-section-title">Terminal</div>
            <div class="settings-card">
                <div class="settings-row">
                    <div class="settings-row-info">
                        <div class="settings-row-title">Terminal Command Auto Execution</div>
                        <div class="settings-row-desc">Controls whether terminal commands require your approval before running.</div>
                        <div class="settings-sub-note">Note: A change to this setting will only apply to new messages sent to Agent. In-progress responses will use the previous setting value.</div>
                    </div>
                    <div style="flex-shrink: 0;">
                        <select class="settings-select-pill" id="select-auto-execution">
                            <option value="always" selected>Always Proceed ⌵</option>
                            <option value="safe">Safe Commands Only ⌵</option>
                            <option value="prompt">Ask Every Time ⌵</option>
                        </select>
                    </div>
                </div>

                <div class="settings-card-divider"></div>

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

        <!-- Section 4: Ollama Daemon & Cloud Models (Full Functionality Restored) -->
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
        <div class="input-container">
            <textarea class="chat-textarea" id="chat-input" placeholder="Ask CLAY (Enter to send, Shift+Enter for newline)..."></textarea>
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

    <script nonce="${nonce}" src="${scriptUri}"></script>
</body>
</html>`;
    }
}
exports.SidebarProvider = SidebarProvider;
function getNonce() {
    let text = '';
    const possible = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
    for (let i = 0; i < 32; i++) {
        text += possible.charAt(Math.floor(Math.random() * possible.length));
    }
    return text;
}
//# sourceMappingURL=SidebarProvider.js.map