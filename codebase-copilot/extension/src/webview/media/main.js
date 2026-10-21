/**
 * CLAY Copilot: Unified Webview Controller
 * Powers both the VS Code Webview Provider and Standalone Browser Preview.
 */
(function () {
  const isVsCode = typeof acquireVsCodeApi === 'function';
  const vscode = isVsCode ? acquireVsCodeApi() : null;
  let BACKEND_URL = (typeof window !== 'undefined' && window.__CLAY_BACKEND_URL__) ? window.__CLAY_BACKEND_URL__ : 'http://127.0.0.1:8765';
  const metaSessionToken = (typeof document !== 'undefined') ? (document.querySelector('meta[name="clay-session-token"]')?.getAttribute('content') || '') : '';
  let SESSION_TOKEN = metaSessionToken || (typeof window !== 'undefined' && window.__CLAY_SESSION_TOKEN__) || '';

  // Intercept window.fetch to automatically attach X-Clay-Auth-Token for local backend requests
  const originalFetch = window.fetch;
  window.fetch = function secureFetch(input, init) {
    const urlStr = typeof input === 'string' ? input : (input && input.url ? input.url : '');
    const isBackendCall = urlStr && (
      urlStr.startsWith(BACKEND_URL) ||
      urlStr.startsWith('http://127.0.0.1:') ||
      urlStr.startsWith('http://localhost:')
    );
    if (isBackendCall && SESSION_TOKEN) {
      init = init ? Object.assign({}, init) : {};
      if (init.headers instanceof Headers) {
        if (!init.headers.has('X-Clay-Auth-Token')) {
          init.headers.set('X-Clay-Auth-Token', SESSION_TOKEN);
        }
      } else if (Array.isArray(init.headers)) {
        init.headers.push(['X-Clay-Auth-Token', SESSION_TOKEN]);
      } else {
        init.headers = Object.assign({}, init.headers, {
          'X-Clay-Auth-Token': SESSION_TOKEN
        });
      }
    }
    return originalFetch.call(this, input, init);
  };

  // Host OS Autodetection from browser runtime / navigator
  const isHostWin = typeof navigator !== 'undefined' && (/Win/i.test(navigator.platform) || /Windows/i.test(navigator.userAgent));
  let detectedSystem = {
    os: isHostWin ? 'windows' : 'linux',
    is_windows: isHostWin,
    shell: isHostWin ? 'PowerShell / CMD' : 'bash'
  };

  let savedRepoPath = localStorage.getItem('codebase_copilot_repo_path');
  // Discard invalid Linux path if on Windows
  if (isHostWin && savedRepoPath && (savedRepoPath.startsWith('/home/') || savedRepoPath.startsWith('/usr/') || savedRepoPath.startsWith('/tmp/'))) {
    savedRepoPath = null;
    localStorage.removeItem('codebase_copilot_repo_path');
  }

  // State
  let currentModel = 'gemma4:31b-cloud';
  let currentHost = 'http://127.0.0.1:11434';
  let currentTemperature = parseFloat(localStorage.getItem('clay_temperature') || '0.20');
  let currentNumCtx = parseInt(localStorage.getItem('clay_num_ctx') || '32768', 10);
  let currentNumPredict = parseInt(localStorage.getItem('clay_num_predict') || '2048', 10);
  let currentPrivacyFilter = localStorage.getItem('clay_privacy_filter') !== 'false';
  let currentPrivacyPatterns = localStorage.getItem('clay_privacy_patterns') || '.env, *.pem, credentials.json, id_rsa, *.key';
  let currentRepoPath = savedRepoPath || '';
  let stagedDiffData = null;
  let stagedDiffIsEdited = false;
  let isAgentRunning = false;
  let lastUserQuery = '';
  const CLAY_ICON_SVG = '<svg viewBox="1 1 13 9" class="clay-cube-icon" shape-rendering="crispEdges"><rect x="1" y="1" width="2" height="1" fill="#C15C3D"/><rect x="2" y="2" width="2" height="1" fill="#C15C3D"/><rect x="3" y="3" width="2" height="1" fill="#C15C3D"/><rect x="4" y="4" width="2" height="1" fill="#C15C3D"/><rect x="5" y="5" width="2" height="1" fill="#C15C3D"/><rect x="4" y="6" width="2" height="1" fill="#C15C3D"/><rect x="3" y="7" width="2" height="1" fill="#C15C3D"/><rect x="2" y="8" width="2" height="1" fill="#C15C3D"/><rect x="1" y="9" width="2" height="1" fill="#C15C3D"/><rect x="8" y="8" width="6" height="2" fill="#C15C3D"/></svg>';

  // Active repository files registry for real-time diff mapping
  let currentRepoFilesList = [];
  let currentRepoDirsList = [];
  let activeEditorFile = '';
  let activeEditorCursorLine = null;
  let activeEditorContent = '';

  function isFileInCurrentRepo(filePath) {
    if (!filePath || typeof filePath !== 'string') return false;
    const inputEl = document.getElementById('input-repo-path');
    const inputVal = inputEl ? inputEl.value.trim() : '';
    const repoRoot = (currentRepoPath && currentRepoPath !== '.' ? currentRepoPath : (inputVal && inputVal !== '.' ? inputVal : '')).replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
    if (!repoRoot) return false;
    const normalized = filePath.replace(/\\/g, '/').toLowerCase();
    return normalized.startsWith(repoRoot + '/') || normalized === repoRoot;
  }

  function getActiveEditorFileInRepo() {
    if (isFileInCurrentRepo(activeEditorFile)) {
      return activeEditorFile;
    }
    if (activeEditorFile && (activeEditorFile.includes('/') || activeEditorFile.includes('\\') || activeEditorFile.includes('.'))) {
      return activeEditorFile;
    }
    return null;
  }

  function getActiveEditorContentInRepo() {
    if (isFileInCurrentRepo(activeEditorFile)) {
      return activeEditorContent;
    }
    if (activeEditorContent !== undefined && activeEditorContent !== null) {
      return activeEditorContent;
    }
    return null;
  }

  function getExplicitFileFromGoal(goalText) {
    if (!goalText || typeof goalText !== 'string') return null;
    const trimmed = goalText.trim();
    // Check filename patterns: e.g. app.py, models/user.py, test.ts
    const fileMatches = trimmed.match(/(?:[a-zA-Z0-9_\-\.\/\\\:]+\.[a-zA-Z0-9_\-]+)/g);
    if (fileMatches && fileMatches.length > 0) {
      for (let i = fileMatches.length - 1; i >= 0; i--) {
        let token = fileMatches[i].trim().replace(/^['"`<(\[]+|['"`>)\]]+$/g, '');
        if (token && token.includes('.') && !token.startsWith('.') && !/^(https?:\/\/|www\.)/i.test(token)) {
          const ext = token.split('.').pop().toLowerCase();
          if (ext && /^[a-zA-Z0-9]{1,10}$/.test(ext) && !['com', 'org', 'net', 'io', 'ai'].includes(ext)) {
            return token.replace(/\\/g, '/');
          }
        }
      }
    }
    if (currentRepoFilesList && currentRepoFilesList.length > 0) {
      const lower = trimmed.toLowerCase();
      for (const f of currentRepoFilesList) {
        if (f.name && lower.includes(f.name.toLowerCase())) {
          return f.name;
        }
      }
    }
    return null;
  }

  function getAllFilesFromGoal(goalText) {
    if (!goalText || typeof goalText !== 'string') return [];
    const trimmed = goalText.trim();
    const cleanFiles = [];
    const fileMatches = trimmed.match(/(?:[a-zA-Z0-9_\-\.\/\\\:]+\.[a-zA-Z0-9_\-]+)/g) || [];
    for (const match of fileMatches) {
      let token = match.trim().replace(/^['"`<(\[]+|['"`>)\]]+$/g, '').replace(/\\/g, '/');
      if (token && token.includes('.') && !token.startsWith('.') && !/^(https?:\/\/|www\.)/i.test(token)) {
        const ext = token.split('.').pop().toLowerCase();
        if (ext && /^[a-zA-Z0-9]{1,10}$/.test(ext) && !['com', 'org', 'net', 'io', 'ai', 'edu', 'gov'].includes(ext)) {
          const fn = token.split('/').pop();
          if (fn && !cleanFiles.includes(fn)) cleanFiles.push(fn);
        }
      }
    }
    if (Array.isArray(currentRepoFilesList) && currentRepoFilesList.length > 0) {
      const lower = trimmed.toLowerCase();
      for (const f of currentRepoFilesList) {
        if (f.name && lower.includes(f.name.toLowerCase())) {
          if (!cleanFiles.includes(f.name)) cleanFiles.push(f.name);
        }
      }
    }
    return cleanFiles;
  }

  const GENERIC_DIFF_PLACEHOLDERS = new Set([
    'file', 'unknown', 'filename', 'patch', 'diff',
    'solution.py', 'patch.diff', 'code', 'script',
    'untitled', 'path/to/file', 'new_file', 'null', 'example'
  ]);

  function isPlaceholderFilename(name) {
    if (!name || typeof name !== 'string') return true;
    const clean = name.trim().toLowerCase();
    if (GENERIC_DIFF_PLACEHOLDERS.has(clean)) return true;
    if (!clean.includes('.') && clean.length <= 15) return true;
    return false;
  }

  function extractDiffFilename(diffText) {
    if (!diffText) return null;
    const plusMatch = diffText.match(/\+\+\+\s+(?:b\/)?([^\s\n\r]+)/);
    if (plusMatch && plusMatch[1] && plusMatch[1] !== '/dev/null') {
      const parts = plusMatch[1].split(/[\\/]/);
      const cand = parts[parts.length - 1];
      if (!isPlaceholderFilename(cand)) return cand;
    }
    const minusMatch = diffText.match(/---\s+(?:a\/)?([^\s\n\r]+)/);
    if (minusMatch && minusMatch[1] && minusMatch[1] !== '/dev/null') {
      const parts = minusMatch[1].split(/[\\/]/);
      const cand = parts[parts.length - 1];
      if (!isPlaceholderFilename(cand)) return cand;
    }
    const srMatch = diffText.match(/<{4,}\s*SEARCH(?::|\s+in|\s+for)?\s+([^\s\n\r]+)/i);
    if (srMatch && srMatch[1]) {
      const parts = srMatch[1].split(/[\\/]/);
      const cand = parts[parts.length - 1];
      if (!isPlaceholderFilename(cand)) return cand;
    }
    const fileCommentMatch = diffText.match(/^(?:#|\/\/|\/\*)\s*([a-zA-Z0-9_\-./\\]+\.[a-zA-Z0-9]+)/m);
    if (fileCommentMatch && fileCommentMatch[1]) {
      const parts = fileCommentMatch[1].split(/[\\/]/);
      const cand = parts[parts.length - 1];
      if (!isPlaceholderFilename(cand)) return cand;
    }
    return null;
  }

  function getPrimaryRepoFile(hintText = '') {
    if (hintText && typeof hintText === 'string') {
      const trimmed = hintText.trim();
      // Check for folder mention like "folder named documentation" or "create folder dsa"
      let folderPrefix = '';
      const stopWords = new Set([
        'named', 'called', 'name', 'titled', 'as', 'of', 'for', 'with', 'a', 'an', 'the',
        'new', 'me', 'folder', 'directory', 'dir', 'and', 'in', 'into', 'inside', 'it',
        'to', 'put', 'file', 'files', 'my', 'this', 'that', 'placeholder', 'statement', 'is'
      ]);

      const folderRegex = /\b(?:folder|directory|dir)\s*(?:named|called|titled|name(?:\s+is)?|as|of|for|with|:\s*|\s+)?\s*['"`]?([a-zA-Z0-9_\-\.\/]+)['"`]?/gi;
      let fMatch;
      let detectedFolder = null;
      while ((fMatch = folderRegex.exec(trimmed)) !== null) {
        const cand = fMatch[1].trim().replace(/^['"`<(\[]+|['"`>)\]]+$/g, '').replace(/[\\/]+$/, '');
        if (cand && !stopWords.has(cand.toLowerCase()) && !cand.startsWith('.') && cand.length > 0) {
          detectedFolder = cand;
          break;
        }
      }

      if (!detectedFolder) {
        // Check secondary pattern: e.g. "create a documentation folder"
        const secMatch = trimmed.match(/\b([a-zA-Z0-9_\-\.\/]+)\s+(?:folder|directory|dir)\b/i);
        if (secMatch) {
          const candSec = secMatch[1].trim().replace(/^['"`<(\[]+|['"`>)\]]+$/g, '');
          if (candSec && !stopWords.has(candSec.toLowerCase()) && !candSec.startsWith('.')) {
            detectedFolder = candSec;
          }
        }
      }

      if (detectedFolder && !detectedFolder.includes('.')) {
        folderPrefix = detectedFolder.replace(/\\/g, '/').replace(/^\/+|\/+$/g, '') + '/';
      }

      // Check for target filename patterns with code or data extensions
      const fileMatches = trimmed.match(/(?:[a-zA-Z0-9_\-\.\/\\\:]+\.[a-zA-Z0-9_\-]+)/g);
      if (fileMatches && fileMatches.length > 0) {
        for (let i = fileMatches.length - 1; i >= 0; i--) {
          let token = fileMatches[i].trim().replace(/^['"`<(\[]+|['"`>)\]]+$/g, '');
          if (token && token.includes('.') && !token.startsWith('.') && !/^(https?:\/\/|www\.)/i.test(token)) {
            // Strip repeated extensions like .py.py -> .py
            token = token.replace(/(\.[a-zA-Z0-9]+)\1+$/gi, '$1');
            const ext = token.split('.').pop().toLowerCase();
            // Valid code/data extension
            if (ext && /^[a-zA-Z0-9]{1,10}$/.test(ext) && !['com', 'org', 'net', 'io', 'ai'].includes(ext)) {
              token = token.replace(/\\/g, '/');
              if (folderPrefix && !token.includes('/')) {
                token = folderPrefix + token;
              }
              return token;
            }
          }
        }
      }

      // Check for filename without extension if language/format is indicated in text
      const nameMatch = trimmed.match(/\b(?:file|script)\s+(?:named|called|titled|as)?\s*['"`]?([a-zA-Z0-9_-]+)['"`]?/i);
      if (nameMatch) {
        const base = nameMatch[1].trim();
        if (base && !stopWords.has(base.toLowerCase()) && base.length > 1) {
          if (/\b(?:python|py)\b/i.test(trimmed)) {
            return (folderPrefix && !base.includes('/') ? folderPrefix : '') + base + '.py';
          }
        }
      }

      // If user asks for pure folder creation (e.g. "create folder dsa")
      if (detectedFolder && (trimmed.toLowerCase().includes('create') || trimmed.toLowerCase().includes('make'))) {
        return detectedFolder.replace(/\\/g, '/').replace(/^\/+|\/+$/g, '');
      }

      // Check if any existing file is mentioned in hintText
      if (currentRepoFilesList && currentRepoFilesList.length > 0) {
        const lower = trimmed.toLowerCase();
        for (const f of currentRepoFilesList) {
          if (f.name && lower.includes(f.name.toLowerCase())) {
            return f.name;
          }
        }
      }
    }

    if (currentRepoFilesList && currentRepoFilesList.length > 0) {
      const pyFile = currentRepoFilesList.find(f => f.name && f.name.endsWith('.py'));
      if (pyFile) return pyFile.name;
      const srcFile = currentRepoFilesList.find(f => f.name && /\.(js|ts|jsx|tsx|json|html|css|md|rs|go|cpp|c|java|sql|sh)$/i.test(f.name));
      if (srcFile) return srcFile.name;
      return currentRepoFilesList[0].name;
    }
    return '';
  }


  // Single-Instance Retro Pixel Toast System
  let activeToastEl = null;
  let activeToastTimer = null;

  function dismissActiveToast() {
    if (!activeToastEl) return;
    const el = activeToastEl;
    el.style.opacity = '0';
    el.style.transform = 'translateY(6px)';
    setTimeout(() => {
      if (el && el.parentNode) {
        el.remove();
      }
      if (activeToastEl === el) {
        activeToastEl = null;
        activeToastTimer = null;
      }
    }, 200);
  }

  function showToast(message, type = 'success') {
    let container = document.getElementById('copilot-toast-container');
    if (!container) {
      container = document.createElement('div');
      container.id = 'copilot-toast-container';
      container.className = 'toast-container';
      document.body.appendChild(container);
    }

    const glyph = type === 'success' ? '[✓]' : type === 'error' ? '[✖]' : '[»]';
    const contentHtml = `<span class="pixel-toast-glyph">${glyph}</span><span class="pixel-toast-text">${escapeHtml(message)}</span>`;

    if (activeToastEl && activeToastEl.parentNode) {
      // Re-use active single instance: update contents, pulse/blink animation, and refresh dismissal timer
      activeToastEl.className = `pixel-toast toast toast-${type}`;
      activeToastEl.innerHTML = contentHtml;
      activeToastEl.style.opacity = '1';
      activeToastEl.style.transform = 'translateY(0)';

      // Trigger high-contrast retro pixel blink
      activeToastEl.classList.remove('toast-blink');
      void activeToastEl.offsetWidth; // Force layout reflow
      activeToastEl.classList.add('toast-blink');

      if (activeToastTimer) {
        clearTimeout(activeToastTimer);
      }
      activeToastTimer = setTimeout(() => dismissActiveToast(), 2600);
      return;
    }

    // Otherwise create brand new single instance (ensure container is clean)
    container.innerHTML = '';
    const toast = document.createElement('div');
    toast.className = `pixel-toast toast toast-${type}`;
    toast.innerHTML = contentHtml;
    container.appendChild(toast);
    activeToastEl = toast;

    if (activeToastTimer) {
      clearTimeout(activeToastTimer);
    }
    activeToastTimer = setTimeout(() => dismissActiveToast(), 2600);
  }

  // Elements
  const navTabs = document.querySelectorAll('.nav-tab');
  const views = {
    chat: document.getElementById('view-chat'),
    agent: document.getElementById('view-agent'),
    diff: document.getElementById('view-diff'),
    repo: document.getElementById('view-repo'),
    settings: document.getElementById('view-settings')
  };
  const inputDock = document.querySelector('.input-dock');

  // ==========================================
  // 1. Tab Switching
  // ==========================================
  function switchTab(target) {
    if (!views[target]) return;
    try {
      localStorage.setItem('clay_active_tab', target);
    } catch (e) { }

    navTabs.forEach((t) => t.classList.remove('active-tab'));
    const activeTabBtn = document.querySelector(`.nav-tab[data-target="${target}"]`);
    if (activeTabBtn) activeTabBtn.classList.add('active-tab');

    Object.keys(views).forEach((k) => {
      if (views[k]) views[k].classList.remove('active-view');
    });
    if (views[target]) views[target].classList.add('active-view');

    if (inputDock) {
      inputDock.style.display = target === 'chat' ? 'block' : 'none';
    }
    if (target === 'chat') {
      setTimeout(checkChatScrollPosition, 60);
    } else if (btnChatJumpDown) {
      btnChatJumpDown.classList.remove('visible');
    }
    if (target === 'repo') {
      loadRepoTree();
    }
    if (target === 'diff') {
      const diffTabBtn = document.querySelector('.nav-tab[data-target="diff"]');
      const dot = diffTabBtn?.querySelector('.diff-staged-dot');
      if (dot) dot.remove();
      // Hide global header conflict banner when in Diff view to prevent duplicate banner
      const headerBanner = document.getElementById('header-conflict-banner');
      if (headerBanner) {
        headerBanner.style.display = 'none';
      }
    } else {
      // When outside the Diff view, show global header banner if an active undismissed conflict exists
      const headerBanner = document.getElementById('header-conflict-banner');
      const hasConflict = Boolean(
        stagedDiffData &&
        (stagedDiffData.isDiverged || (stagedDiffData.diff && hasConflictMarkers(stagedDiffData.diff))) &&
        !stagedDiffData.isDismissed
      );
      if (headerBanner) {
        if (hasConflict) {
          headerBanner.style.display = 'flex';
          const hTitle = document.getElementById('header-conflict-title');
          if (hTitle && stagedDiffData) {
            hTitle.textContent = stagedDiffData.isDiverged
              ? 'WARNING: 3-WAY MERGE CONFLICT (BUFFER DIVERGED)'
              : 'WARNING: 3-WAY MERGE CONFLICT DETECTED';
          }
        } else {
          headerBanner.style.display = 'none';
        }
      }
    }
  }

  navTabs.forEach((tab) => {
    tab.addEventListener('click', () => {
      const target = tab.getAttribute('data-target');
      switchTab(target);
    });
  });

  // Restore last active tab on load
  try {
    const savedActiveTab = localStorage.getItem('clay_active_tab');
    if (savedActiveTab && views[savedActiveTab] && savedActiveTab !== 'chat') {
      switchTab(savedActiveTab);
    }
  } catch (e) { }

  // ==========================================
  // 2. Settings: Reference-Styled Architecture & OS Detection
  // ==========================================
  const btnTestOllama = document.getElementById('btn-test-ollama');
  const statusIndicator = document.getElementById('settings-status-indicator');
  const statusText = document.getElementById('settings-status-text');
  const latencyText = document.getElementById('settings-latency-text');
  const hostInput = document.getElementById('input-ollama-host');
  const modelSelect = document.getElementById('select-ollama-model');
  const tempSlider = document.getElementById('input-temperature');
  const tempValDisplay = document.getElementById('val-temperature');
  const ctxSelect = document.getElementById('select-context-window');
  const maxTokensSelect = document.getElementById('select-max-tokens');
  const privacyCheck = document.getElementById('chk-privacy-guard');
  const privacyPatterns = document.getElementById('input-privacy-patterns');
  const btnSaveSettings = document.getElementById('btn-save-all-settings');
  const crumbStatus = document.getElementById('crumb-status');
  const welcomeModelText = document.getElementById('welcome-model-text');
  const inputModelHint = document.getElementById('input-model-hint');

  // Cloud API & Connection Type Elements
  const selectConnectionType = document.getElementById('select-connection-type');
  const groupApiKey = document.getElementById('group-api-key');
  const inputApiKey = document.getElementById('input-api-key');
  const btnToggleApiKey = document.getElementById('btn-toggle-api-key');
  const eyeIconShow = document.getElementById('eye-icon-show');
  const eyeIconHide = document.getElementById('eye-icon-hide');
  const connectionTypeDesc = document.getElementById('connection-type-desc');

  // State: Reference Settings
  let currentConnectionType = localStorage.getItem('clay_connection_type') || 'local';
  let currentApiKey = '';
  // Domain 5: Purge any legacy unencrypted API keys left in localStorage
  try {
    localStorage.removeItem('clay_api_key');
  } catch (e) { }
  let currentSecurityMode = localStorage.getItem('clay_security_mode') || 'sandboxed';
  let currentAllowNetwork = localStorage.getItem('clay_allow_net') !== 'false';

  // Immediately reflect saved local storage values in the UI controls
  if (tempSlider && tempValDisplay) {
    tempSlider.value = currentTemperature.toString();
    tempValDisplay.textContent = currentTemperature.toFixed(2);
  }
  if (ctxSelect) {
    ctxSelect.value = currentNumCtx.toString();
  }
  if (maxTokensSelect) {
    maxTokensSelect.value = currentNumPredict.toString();
  }
  if (privacyCheck) {
    privacyCheck.checked = currentPrivacyFilter;
  }
  if (privacyPatterns) {
    privacyPatterns.value = currentPrivacyPatterns;
  }

  // 0. Theme & IDE Bindings (CLAY Palette vs. IDE Palette Tokens)
  let currentThemeMode = localStorage.getItem('clay_theme_mode') || 'claude';
  const themeCards = document.querySelectorAll('.theme-binding-card');

  function applyThemeMode(mode) {
    currentThemeMode = mode;
    try {
      localStorage.setItem('clay_theme_mode', mode);
    } catch (e) { }

    const isNative = mode === 'vscode';
    if (isNative) {
      document.body.classList.add('vscode-native');
    } else {
      document.body.classList.remove('vscode-native');
    }

    themeCards.forEach((c) => {
      if (c.getAttribute('data-theme') === mode) {
        c.classList.add('active');
      } else {
        c.classList.remove('active');
      }
    });

    // Re-render any visible mermaid diagram cards to match palette tokens immediately
    refreshAllMermaidDiagrams();
  }

  themeCards.forEach((card) => {
    card.addEventListener('click', () => {
      const mode = card.getAttribute('data-theme') || 'claude';
      applyThemeMode(mode);
    });
  });

  // Apply saved theme mode immediately on initialization
  applyThemeMode(currentThemeMode);

  // 1. 3-Card Security Mode Grid
  const securityCards = document.querySelectorAll('.security-mode-card');
  securityCards.forEach((card) => {
    if (card.getAttribute('data-mode') === currentSecurityMode) {
      card.classList.add('active');
    } else {
      card.classList.remove('active');
    }
    card.addEventListener('click', () => {
      securityCards.forEach((c) => c.classList.remove('active'));
      card.classList.add('active');
      currentSecurityMode = card.getAttribute('data-mode');
      localStorage.setItem('clay_security_mode', currentSecurityMode);
      const title = card.querySelector('.security-mode-title')?.textContent || currentSecurityMode;
      showToast(`Agent security mode: ${title}`, 'info');

      // Immediately synchronize security mode with backend and extension host
      updateCommandPreview();
      if (isVsCode) {
        vscode.postMessage({
          command: 'saveSettings',
          settings: { security_mode: currentSecurityMode }
        });
      } else {
        fetch(`${BACKEND_URL}/api/settings`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ security_mode: currentSecurityMode })
        }).catch(() => { });
      }
    });
  });

  // 2. Terminal Controls: Sandbox Allow Network
  const chkSandboxNetwork = document.getElementById('chk-sandbox-network');
  if (chkSandboxNetwork) {
    chkSandboxNetwork.checked = currentAllowNetwork;
    chkSandboxNetwork.addEventListener('change', () => {
      currentAllowNetwork = chkSandboxNetwork.checked;
      localStorage.setItem('clay_allow_net', currentAllowNetwork ? 'true' : 'false');
      if (isVsCode) {
        vscode.postMessage({
          command: 'saveSettings',
          settings: { allow_network: currentAllowNetwork }
        });
      } else {
        fetch(`${BACKEND_URL}/api/settings`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ allow_network: currentAllowNetwork })
        }).catch(() => { });
      }
    });
  }

  // 5. Host OS Autodetection & Dynamic CLI Chips
  function getSyntaxCheckCommand() {
    try {
      const isWin = detectedSystem && detectedSystem.is_windows;
      const py = isWin ? 'python' : 'python3';
      const inputEl = document.getElementById('input-repo-path');
      const inputVal = inputEl ? inputEl.value.trim() : '';
      const repoRoot = (currentRepoPath && currentRepoPath !== '.' ? currentRepoPath : (inputVal && inputVal !== '.' ? inputVal : '')).replace(/\\/g, '/').replace(/\/+$/, '');

      // 1. Check if user has an active file open in the editor within the repo
      const repoActive = getActiveEditorFileInRepo();
      if (repoActive) {
        const activeNorm = repoActive.replace(/\\/g, '/');
        if (repoRoot && activeNorm.startsWith(repoRoot)) {
          let rel = activeNorm.slice(repoRoot.length).replace(/^\/+/, '');
          if (rel.endsWith('.py')) {
            return `${py} -m py_compile ${rel}`;
          }
        }
      }

      // 2. Check currentRepoFilesList for actual files in the uploaded repository
      if (Array.isArray(currentRepoFilesList) && currentRepoFilesList.length > 0) {
        const pyFiles = currentRepoFilesList.filter(f => (f.path || f.name || '').endsWith('.py'));
        if (pyFiles.length > 0) {
          const preferred = pyFiles.find(f => {
            const n = (f.name || f.path || '').toLowerCase();
            return !n.includes('test_') && !n.includes('__') && !n.includes('test.') && !n.includes('tests/');
          }) || pyFiles[0];

          let target = (preferred.path || preferred.name).replace(/\\/g, '/');
          if (repoRoot && target.startsWith(repoRoot)) {
            target = target.slice(repoRoot.length).replace(/^\/+/, '');
          }
          return `${py} -m py_compile ${target}`;
        }
      }

      // 3. Fallback to primary repo file if ends with .py
      if (typeof getPrimaryRepoFile === 'function') {
        const primary = getPrimaryRepoFile();
        if (primary && primary.endsWith('.py')) {
          return `${py} -m py_compile ${primary}`;
        }
      }

      // 4. Default: compile all python files in the current repository folder
      return `${py} -m compileall -q .`;
    } catch (err) {
      console.warn('Error determining syntax check command:', err);
      return 'python3 -m compileall -q .';
    }
  }

  function applySystemInfo(info) {
    if (!info) return;
    detectedSystem = info;
    try {
      const osBadge = document.getElementById('terminal-os-badge');
      if (osBadge) {
        const osName = (info.os || (info.is_windows ? 'WINDOWS' : 'LINUX')).toUpperCase();
        const shellName = info.shell || (info.is_windows ? 'powershell' : 'bash');
        osBadge.textContent = `${osName} : ${shellName}`;
      }

      // Dynamically populate CLI chips for host OS
      const quickChipsContainer = document.getElementById('terminal-quick-chips');
      if (quickChipsContainer) {
        quickChipsContainer.innerHTML = '<span class="terminal-quick-chips-label">Quick:</span>';
        const isWin = !!info.is_windows;
        const chips = isWin ? [
          { label: 'syntax check', cmd: () => getSyntaxCheckCommand() },
          { label: 'pytest', cmd: () => 'python -m pytest' },
          { label: 'git status', cmd: () => 'git status' },
          { label: 'dir', cmd: () => 'dir' }
        ] : [
          { label: 'syntax check', cmd: () => getSyntaxCheckCommand() },
          { label: 'pytest', cmd: () => 'pytest' },
          { label: 'git status', cmd: () => 'git status' },
          { label: 'ls -la', cmd: () => 'ls -la' }
        ];

        chips.forEach((c) => {
          const chipEl = document.createElement('span');
          chipEl.className = 'cli-chip';
          const initialCmd = typeof c.cmd === 'function' ? c.cmd() : c.cmd;
          chipEl.setAttribute('data-cmd', initialCmd);
          chipEl.textContent = c.label;
          chipEl.addEventListener('click', () => {
            const cmdToRun = typeof c.cmd === 'function' ? c.cmd() : c.cmd;
            const inputTerminalCmd = document.getElementById('input-terminal-cmd');
            if (inputTerminalCmd) {
              inputTerminalCmd.value = cmdToRun;
              if (typeof updateCommandPreview === 'function') {
                updateCommandPreview(cmdToRun);
              }
            }
            if (typeof executeCliCommand === 'function') {
              executeCliCommand(cmdToRun);
            }
          });
          quickChipsContainer.appendChild(chipEl);
        });
      }
    } catch (e) {
      console.warn('Error applying system info to chips:', e);
    }
  }

  async function detectSystemAndInitChips() {
    // Immediate setup with local detectedSystem
    applySystemInfo(detectedSystem);

    if (isVsCode) {
      vscode.postMessage({ command: 'getSystemInfo', workspace_path: currentRepoPath });
      return;
    }

    try {
      const res = await fetch(`${BACKEND_URL}/api/system/info`);
      if (res.ok) {
        const info = await res.json();
        applySystemInfo(info);
      }
    } catch (e) {
      console.warn('System info detection deferred:', e);
    }
  }

  if (tempSlider && tempValDisplay) {
    tempSlider.addEventListener('input', () => {
      currentTemperature = parseFloat(tempSlider.value);
      tempValDisplay.textContent = currentTemperature.toFixed(2);
    });
  }

  if (ctxSelect) {
    ctxSelect.addEventListener('change', () => {
      currentNumCtx = parseInt(ctxSelect.value, 10);
    });
  }

  if (maxTokensSelect) {
    maxTokensSelect.addEventListener('change', () => {
      currentNumPredict = parseInt(maxTokensSelect.value, 10);
    });
  }

  // Connection Type Switcher (Local Daemon vs. Cloud API Key)
  if (selectConnectionType) {
    selectConnectionType.value = currentConnectionType;
    if (groupApiKey) {
      groupApiKey.style.display = currentConnectionType === 'cloud' ? 'block' : 'none';
    }
    selectConnectionType.addEventListener('change', () => {
      currentConnectionType = selectConnectionType.value;
      localStorage.setItem('clay_connection_type', currentConnectionType);
      if (groupApiKey) {
        groupApiKey.style.display = currentConnectionType === 'cloud' ? 'block' : 'none';
      }
      if (currentConnectionType === 'cloud') {
        if (hostInput && (hostInput.value === 'http://127.0.0.1:11434' || hostInput.value === 'http://localhost:11434')) {
          hostInput.value = 'https://ollama.com/api';
        }
        if (connectionTypeDesc) {
          connectionTypeDesc.textContent = 'Direct cloud access to Ollama Cloud models. Authenticated via Bearer token.';
        }
        showToast('Switched to Ollama Cloud (API Key)', 'info');
      } else {
        if (hostInput && hostInput.value === 'https://ollama.com/api') {
          hostInput.value = 'http://127.0.0.1:11434';
        }
        if (connectionTypeDesc) {
          connectionTypeDesc.textContent = 'Runs against local background Ollama (http://127.0.0.1:11434). Zero API keys required.';
        }
        showToast('Switched to Ollama Local (Daemon)', 'info');
      }
    });
  }

  // API Key Visibility Eye Toggle
  if (btnToggleApiKey && inputApiKey) {
    btnToggleApiKey.addEventListener('click', () => {
      if (inputApiKey.type === 'password') {
        inputApiKey.type = 'text';
        if (eyeIconShow) eyeIconShow.style.display = 'none';
        if (eyeIconHide) eyeIconHide.style.display = 'inline-block';
      } else {
        inputApiKey.type = 'password';
        if (eyeIconShow) eyeIconShow.style.display = 'inline-block';
        if (eyeIconHide) eyeIconHide.style.display = 'none';
      }
    });
  }

  async function testOllamaConnection() {
    if (statusText) statusText.textContent = 'Connecting to daemon...';
    if (latencyText) latencyText.textContent = '...';

    if (isVsCode) {
      vscode.postMessage({ command: 'testOllamaConnection' });
    } else {
      const start = Date.now();
      try {
        const res = await fetch(`${BACKEND_URL}/api/ollama/status`);
        const latency = Date.now() - start;
        if (res.ok) {
          const data = await res.json();
          data.latency_ms = data.latency_ms || latency;
          applyOllamaStatus(data);
        } else {
          applyOllamaStatus({ connected: false });
        }
      } catch (err) {
        applyOllamaStatus({ connected: false });
      }
    }
  }

  function applyOllamaStatus(data) {
    if (data.connected) {
      if (statusIndicator) statusIndicator.className = 'status-indicator connected';
      if (statusText) statusText.textContent = 'Connected (Ollama Live)';
      if (latencyText) latencyText.textContent = `${data.latency_ms || 3} ms`;
      if (crumbStatus) {
        crumbStatus.className = 'pill pill-teal';
        crumbStatus.innerHTML = '<span class="pill-dot"></span>Live';
      }

      // Populate models dropdown dynamically
      if (modelSelect && data.models && data.models.length > 0) {
        const savedModel = localStorage.getItem('clay_model');
        const currentVal = savedModel || modelSelect.value || currentModel;
        modelSelect.innerHTML = '';
        let matched = false;
        data.models.forEach((m) => {
          const opt = document.createElement('option');
          opt.value = m;
          opt.textContent = m;
          if (m === currentVal) {
            opt.selected = true;
            matched = true;
          }
          modelSelect.appendChild(opt);
        });
        if (!matched && modelSelect.options.length > 0) {
          const partial = Array.from(modelSelect.options).find(o => o.value.includes(currentVal) || currentVal.includes(o.value));
          if (partial) {
            partial.selected = true;
          } else {
            modelSelect.selectedIndex = 0;
          }
        }
        currentModel = modelSelect.value;
        localStorage.setItem('clay_model', currentModel);
        updateActiveModelDisplays(currentModel);
      }
    } else {
      if (statusIndicator) statusIndicator.className = 'status-indicator disconnected';
      if (statusText) statusText.textContent = 'Disconnected / Offline';
      if (latencyText) latencyText.textContent = 'err';
      if (crumbStatus) {
        crumbStatus.className = 'pill pill-red';
        crumbStatus.innerHTML = '<span class="pill-dot"></span>Offline';
      }
    }
  }

  function updateActiveModelDisplays(model) {
    currentModel = model;
    if (welcomeModelText) welcomeModelText.textContent = model;
    if (inputModelHint) inputModelHint.textContent = 'CLAY // ' + model;
    const chatModelBadge = document.getElementById('chat-active-model');
    if (chatModelBadge) chatModelBadge.textContent = model;
  }

  if (modelSelect) {
    modelSelect.addEventListener('change', () => {
      currentModel = modelSelect.value;
      localStorage.setItem('clay_model', currentModel);
      updateActiveModelDisplays(currentModel);
      showToast(`Active model switched to ${currentModel}`, 'info');
      const payload = { model: currentModel };
      if (isVsCode) {
        vscode.postMessage({ command: 'saveSettings', settings: payload });
      } else {
        fetch(`${BACKEND_URL}/api/settings`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        }).catch(() => { });
      }
    });
  }

  if (btnTestOllama) {
    btnTestOllama.addEventListener('click', testOllamaConnection);
  }

  if (btnSaveSettings) {
    btnSaveSettings.addEventListener('click', async () => {
      let apiKeyToSend = undefined;
      if (inputApiKey) {
        const trimmed = inputApiKey.value.trim();
        if (trimmed && !trimmed.includes('••••')) {
          apiKeyToSend = trimmed;
        } else if (inputApiKey.dataset.cleared === 'true') {
          apiKeyToSend = '';
        }
      }

      const payload = {
        model: modelSelect ? modelSelect.value : currentModel,
        host: hostInput ? hostInput.value.trim() : currentHost,
        connection_type: selectConnectionType ? selectConnectionType.value : currentConnectionType,
        api_key: apiKeyToSend,
        temperature: tempSlider ? parseFloat(tempSlider.value) : currentTemperature,
        num_ctx: ctxSelect ? parseInt(ctxSelect.value, 10) : currentNumCtx,
        num_predict: maxTokensSelect ? parseInt(maxTokensSelect.value, 10) : currentNumPredict,
        privacy_filter: privacyCheck ? privacyCheck.checked : true,
        exclusion_patterns: privacyPatterns ? privacyPatterns.value.trim() : '.env, *.pem, credentials.json, id_rsa, *.key',
        security_mode: currentSecurityMode,
        allow_network: currentAllowNetwork,
        theme_mode: currentThemeMode
      };

      // Persist non-sensitive preferences to local storage immediately
      currentTemperature = payload.temperature;
      currentNumCtx = payload.num_ctx;
      currentNumPredict = payload.num_predict;
      currentPrivacyFilter = payload.privacy_filter;
      currentPrivacyPatterns = payload.exclusion_patterns;
      localStorage.setItem('clay_temperature', payload.temperature.toString());
      localStorage.setItem('clay_num_ctx', payload.num_ctx.toString());
      localStorage.setItem('clay_num_predict', payload.num_predict.toString());
      localStorage.setItem('clay_privacy_filter', payload.privacy_filter ? 'true' : 'false');
      localStorage.setItem('clay_privacy_patterns', payload.exclusion_patterns);
      localStorage.setItem('clay_model', payload.model);
      if (payload.host) localStorage.setItem('clay_host', payload.host);
      if (payload.connection_type) localStorage.setItem('clay_connection_type', payload.connection_type);
      localStorage.setItem('clay_theme_mode', currentThemeMode);

      btnSaveSettings.textContent = 'Settings Saved!';
      setTimeout(() => (btnSaveSettings.textContent = 'Save All Settings'), 2000);
      updateActiveModelDisplays(payload.model);

      if (isVsCode) {
        vscode.postMessage({ command: 'saveSettings', settings: payload });
        showToast('Settings saved successfully', 'success');
      } else {
        try {
          const res = await fetch(`${BACKEND_URL}/api/settings`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
          });
          if (res.ok) {
            showToast('Settings saved successfully', 'success');
          } else {
            showToast('Failed to save settings', 'error');
          }
        } catch (e) {
          showToast('Failed to save settings', 'error');
        }
      }
    });
  }

  // Backend Lifecycle State
  let restartPollInterval = null;

  // Load initial settings from backend
  async function loadInitialSettings() {
    try {
      const res = await fetch(`${BACKEND_URL}/api/settings`);
      if (res.ok) {
        const s = await res.json();
        if (s.temperature !== undefined && tempSlider && tempValDisplay) {
          tempSlider.value = s.temperature;
          tempValDisplay.textContent = parseFloat(s.temperature).toFixed(2);
          currentTemperature = parseFloat(s.temperature);
          localStorage.setItem('clay_temperature', s.temperature.toString());
        }
        if (s.num_ctx && ctxSelect) {
          ctxSelect.value = s.num_ctx.toString();
          currentNumCtx = parseInt(s.num_ctx, 10);
          localStorage.setItem('clay_num_ctx', s.num_ctx.toString());
        }
        if (s.num_predict && maxTokensSelect) {
          maxTokensSelect.value = s.num_predict.toString();
          currentNumPredict = parseInt(s.num_predict, 10);
          localStorage.setItem('clay_num_predict', s.num_predict.toString());
        }
        if (s.privacy_filter !== undefined && privacyCheck) {
          privacyCheck.checked = !!s.privacy_filter;
          currentPrivacyFilter = !!s.privacy_filter;
          localStorage.setItem('clay_privacy_filter', s.privacy_filter ? 'true' : 'false');
        }
        if (s.exclusion_patterns && privacyPatterns) {
          privacyPatterns.value = s.exclusion_patterns;
          currentPrivacyPatterns = s.exclusion_patterns;
          localStorage.setItem('clay_privacy_patterns', s.exclusion_patterns);
        }
        if (s.connection_type) {
          currentConnectionType = s.connection_type;
          localStorage.setItem('clay_connection_type', currentConnectionType);
          if (selectConnectionType) selectConnectionType.value = currentConnectionType;
          if (groupApiKey) groupApiKey.style.display = currentConnectionType === 'cloud' ? 'block' : 'none';
        }
        if (inputApiKey) {
          if (s.has_api_key || (s.api_key && s.api_key.trim())) {
            const masked = s.api_key_masked || (s.api_key && s.api_key.includes('••••') ? s.api_key : '••••••••');
            inputApiKey.placeholder = `Saved in OS Keychain (${masked})`;
            inputApiKey.dataset.hasKey = 'true';
            inputApiKey.value = '';
          } else {
            inputApiKey.placeholder = 'Enter ollama.com API key (e.g. sk_...)';
            inputApiKey.dataset.hasKey = 'false';
          }
        }
        if (s.host && hostInput) {
          hostInput.value = s.host;
          currentHost = s.host;
        }
        if (s.security_mode) {
          currentSecurityMode = s.security_mode;
          localStorage.setItem('clay_security_mode', currentSecurityMode);
          securityCards.forEach((c) => {
            if (c.getAttribute('data-mode') === currentSecurityMode) {
              c.classList.add('active');
            } else {
              c.classList.remove('active');
            }
          });
        }
        if (s.allow_network !== undefined && chkSandboxNetwork) {
          chkSandboxNetwork.checked = s.allow_network;
          currentAllowNetwork = s.allow_network;
          localStorage.setItem('clay_allow_net', currentAllowNetwork ? 'true' : 'false');
        }
      }
    } catch (e) {
      console.warn('Deferred settings load:', e);
    }
  }

  // Auto-test and detect system on startup
  testOllamaConnection();
  detectSystemAndInitChips();
  loadInitialSettings();

  // ==========================================
  // 3. Chat: Clean Citations, Zero-Emoji & Streaming
  // ==========================================
  const chatInput = document.getElementById('chat-input');
  const sendBtn = document.getElementById('btn-send');
  const messageList = document.getElementById('message-list');
  const chatEmptyState = document.getElementById('chat-empty-state');
  const btnChatJumpDown = document.getElementById('btn-chat-jump-down');
  const btnClearChat = document.getElementById('btn-clear-chat');
  let currentAssistantTextEl = null;
  let chatSessionHistory = [];
  try {
    const savedChat = sessionStorage.getItem('clay_chat_session_history');
    if (savedChat) {
      chatSessionHistory = JSON.parse(savedChat);
    }
  } catch { }

  function saveChatSessionHistory() {
    try {
      sessionStorage.setItem('clay_chat_session_history', JSON.stringify(chatSessionHistory));
    } catch { }
  }

  function updateAssistantCursorStates(activeCard = null, isProcessing = false) {
    if (!messageList) return;
    const cards = messageList.querySelectorAll('.assistant-card');
    cards.forEach((card, idx) => {
      const isLatest = idx === cards.length - 1;
      card.classList.remove('is-latest', 'is-processing', 'is-idle');
      if (isLatest) {
        card.classList.add('is-latest');
        if (isProcessing || (activeCard && card === activeCard)) {
          card.classList.add('is-processing');
        } else {
          card.classList.add('is-idle');
        }
      }
    });
  }

  function hideChatEmptyState() {
    if (chatEmptyState) {
      chatEmptyState.classList.add('hidden');
      chatEmptyState.style.setProperty('display', 'none', 'important');
    }
  }

  function showChatEmptyState() {
    if (chatEmptyState) {
      chatEmptyState.classList.remove('hidden');
      chatEmptyState.style.setProperty('display', 'flex', 'important');
    }
  }

  // Initial pass on existing chat messages & holder screen restoration
  if (messageList && chatEmptyState) {
    const bubbles = messageList.querySelectorAll('.user-bubble, .assistant-card');
    if (bubbles.length === 0) {
      showChatEmptyState();
    } else {
      hideChatEmptyState();
    }
  }
  updateAssistantCursorStates();

  function checkChatScrollPosition() {
    const container = document.getElementById('view-chat');
    if (!container || !btnChatJumpDown) return;
    const isChatActive = views.chat && views.chat.classList.contains('active-view');
    if (!isChatActive) {
      btnChatJumpDown.classList.remove('visible');
      return;
    }
    const distFromBottom = container.scrollHeight - container.scrollTop - container.clientHeight;
    if (distFromBottom > 60) {
      btnChatJumpDown.classList.add('visible');
    } else {
      btnChatJumpDown.classList.remove('visible');
    }
  }

  const SEND_ICON_SVG = `
    <svg viewBox="0 0 16 16" width="13" height="13" fill="currentColor" shape-rendering="crispEdges">
      <path d="M2,7 h6 v2 h-6 z M8,5 h2 v6 h-2 z M10,6 h2 v4 h-2 z M12,7 h2 v2 h-2 z"/>
    </svg>
  `;

  const STOP_ICON_SVG = `
    <svg viewBox="0 0 16 16" width="12" height="12" fill="currentColor" shape-rendering="crispEdges">
      <rect x="3" y="3" width="10" height="10" fill="currentColor"/>
    </svg>
  `;

  let isChatStreaming = false;
  let chatAbortController = null;

  function setChatGeneratingState(generating) {
    isChatStreaming = generating;
    if (!sendBtn) return;
    if (generating) {
      sendBtn.classList.add('is-stop', 'is-generating');
      sendBtn.title = 'Stop Generating (Click to cancel)';
      sendBtn.setAttribute('aria-label', 'Stop Generating');
      sendBtn.innerHTML = STOP_ICON_SVG;
    } else {
      sendBtn.classList.remove('is-stop', 'is-generating');
      sendBtn.title = 'Send (Enter)';
      sendBtn.setAttribute('aria-label', 'Send');
      sendBtn.innerHTML = SEND_ICON_SVG;
    }
  }

  // ==========================================
  // Claude Code Dynamic Thinking Drawer (<think>)
  // ==========================================
  let activeThinkingState = {
    isInsideThinking: false,
    hasFinishedThinking: false,
    isPreStreaming: false,
    preStreamTimer: null,
    startTime: 0,
    timerInterval: null,
    elapsedSec: 0,
    drawerEl: null,
    bodyEl: null,
    cardsContainerEl: null,
    streamContainerEl: null,
    thoughtPreEl: null,
    titleEl: null,
    stopwatchEl: null,
    stepEl: null,
    toggleBtn: null,
    cardEl: null
  };

  function resetActiveThinkingState() {
    if (activeThinkingState) {
      if (activeThinkingState.timerInterval) {
        clearInterval(activeThinkingState.timerInterval);
      }
      if (activeThinkingState.preStreamTimer) {
        clearTimeout(activeThinkingState.preStreamTimer);
      }
    }
    activeThinkingState = {
      isInsideThinking: false,
      hasFinishedThinking: false,
      isPreStreaming: false,
      preStreamTimer: null,
      startTime: 0,
      timerInterval: null,
      elapsedSec: 0,
      drawerEl: null,
      bodyEl: null,
      cardsContainerEl: null,
      streamContainerEl: null,
      thoughtPreEl: null,
      titleEl: null,
      stopwatchEl: null,
      stepEl: null,
      toggleBtn: null,
      cardEl: null
    };
  }

  function analyzePromptIntentAndContext(promptText, pinnedList = []) {
    const raw = (promptText || '').trim();
    const clean = normalizeGoalText(raw);
    const lower = clean.toLowerCase();

    // 1. Identify target file or symbol
    let targetEntity = null;
    let targetType = 'file';

    if (pinnedList && pinnedList.length > 0) {
      const firstPin = pinnedList[0];
      targetEntity = firstPin.name;
      targetType = firstPin.type || 'file';
    } else {
      const primaryFile = getPrimaryRepoFile(raw);
      if (primaryFile) {
        targetEntity = primaryFile;
        targetType = 'file';
      } else {
        const activeFile = getActiveEditorFileInRepo();
        if (activeFile) {
          targetEntity = activeFile.replace(/\\/g, '/').split('/').pop();
          targetType = 'active-file';
        }
      }
    }

    // 2. Identify Action / Intent
    let actionTitle = 'Codebase Analysis';
    let actionDetail = 'Processing prompt specifications';

    if (/\b(fix|bug|error|repair|syntax|fail|failing|broken|issue|patch|indentation|conflict)\b/i.test(lower)) {
      actionTitle = targetEntity ? `Repair: ${targetEntity}` : 'Bug Diagnosis & Repair';
      actionDetail = 'Analyzing error patterns, syntax invariants, and patch stability';
    } else if (/\b(implement|create|build|write|add|generate|make|new|scaffold)\b/i.test(lower)) {
      actionTitle = targetEntity ? `Implement: ${targetEntity}` : 'Feature Synthesis';
      actionDetail = 'Constructing domain structures, typing models, and logic components';
    } else if (/\b(refactor|optimize|clean|speed up|simplify|restructure|improve)\b/i.test(lower)) {
      actionTitle = targetEntity ? `Refactor: ${targetEntity}` : 'Architecture Optimization';
      actionDetail = 'Evaluating code complexity, algorithmic bounds, and regression safety';
    } else if (/\b(test|spec|assert|verify|unit test|integration test)\b/i.test(lower)) {
      actionTitle = targetEntity ? `Test: ${targetEntity}` : 'Test Harness & Verification Plan';
      actionDetail = 'Formulating test assertions, boundary edge cases, and mocks';
    } else if (/\b(explain|what does|how does|why|walkthrough|overview|understand)\b/i.test(lower)) {
      actionTitle = targetEntity ? `Explain: ${targetEntity}` : 'Codebase Inspection';
      actionDetail = 'Tracing execution lifecycle, data flows, and dependencies';
    }

    // Check if domain topic detected (e.g. A*, JWT, LRU, Trie, etc.)
    const topicInfo = resolveTopicDomain(raw);
    if (topicInfo && topicInfo.topic && topicInfo.topic !== 'Production Software Module') {
      actionDetail = `${topicInfo.topic} // ${topicInfo.dataStructure}`;
    }

    // 3. Context Grounding Details
    let groundingTitle = 'Repository Context';
    let groundingDetail = 'Querying workspace index and symbol definitions';

    if (pinnedList && pinnedList.length > 0) {
      groundingTitle = 'Context Grounding';
      groundingDetail = pinnedList.map(p => `${p.type}: ${p.name}`).join(', ');
    } else if (targetEntity) {
      groundingTitle = 'Workspace Grounding';
      groundingDetail = `${targetEntity} (active reference)`;
    } else {
      groundingTitle = 'Hybrid Codebase Retrieval';
      groundingDetail = 'Searching repository map, graph store, and vector index';
    }

    return {
      actionTitle,
      actionDetail,
      groundingTitle,
      groundingDetail,
      modelName: currentModel || 'CLAY Core'
    };
  }

  function startPreStreamingReasoning(cardEl, pinnedList = [], promptText = '') {
    if (!cardEl) return;
    activeThinkingState.isInsideThinking = true;
    activeThinkingState.isPreStreaming = true;
    activeThinkingState.startTime = Date.now();

    const drawer = createThinkingDrawer(cardEl);
    if (!drawer) return;

    const analysis = analyzePromptIntentAndContext(promptText, pinnedList);

    if (activeThinkingState.titleEl) {
      activeThinkingState.titleEl.textContent = 'Thinking...';
    }
    if (activeThinkingState.stopwatchEl) {
      activeThinkingState.stopwatchEl.textContent = '(0s)';
    }
    if (activeThinkingState.stepEl) {
      activeThinkingState.stepEl.textContent = analysis.actionTitle;
    }

    if (activeThinkingState.cardsContainerEl) {
      activeThinkingState.cardsContainerEl.innerHTML = `
        <div class="thought-step is-done">
          <span class="thought-step-icon">
            <svg viewBox="0 0 12 12" width="9" height="9" fill="currentColor">
              <path d="M10.28 2.28L4.5 8.06 1.72 5.28a.75.75 0 00-1.06 1.06l3.5 3.5a.75.75 0 001.06 0l6.5-6.5a.75.75 0 00-1.06-1.06z"/>
            </svg>
          </span>
          <div class="thought-step-content">
            <span class="thought-step-title">${escapeHtml(analysis.actionTitle)}</span>
            <span class="thought-step-detail">${escapeHtml(analysis.actionDetail)}</span>
          </div>
        </div>

        <div class="thought-step is-done">
          <span class="thought-step-icon">
            <svg viewBox="0 0 12 12" width="9" height="9" fill="currentColor">
              <path d="M10.28 2.28L4.5 8.06 1.72 5.28a.75.75 0 00-1.06 1.06l3.5 3.5a.75.75 0 001.06 0l6.5-6.5a.75.75 0 00-1.06-1.06z"/>
            </svg>
          </span>
          <div class="thought-step-content">
            <span class="thought-step-title">${escapeHtml(analysis.groundingTitle)}</span>
            <span class="thought-step-detail">${escapeHtml(analysis.groundingDetail)}</span>
          </div>
        </div>

        <div class="thought-step is-active" id="thought-step-model">
          <span class="thought-step-icon">
            <span class="thought-step-dot"></span>
          </span>
          <div class="thought-step-content">
            <span class="thought-step-title">Reasoning with ${escapeHtml(analysis.modelName)}</span>
            <span class="thought-step-detail" id="thought-step-model-detail">Evaluating context &amp; formulating solution...</span>
          </div>
        </div>
      `;
    }

    // Live timer starting at 0s
    activeThinkingState.timerInterval = setInterval(() => {
      if (!activeThinkingState.isInsideThinking) return;
      const s = Math.max(1, Math.round((Date.now() - activeThinkingState.startTime) / 1000));
      activeThinkingState.elapsedSec = s;
      if (activeThinkingState.stopwatchEl) {
        activeThinkingState.stopwatchEl.textContent = `(${s}s)`;
      }
    }, 1000);

    // Dynamic progress transitions during pre-stream
    activeThinkingState.preStreamTimer = setTimeout(() => {
      if (activeThinkingState.isInsideThinking && activeThinkingState.isPreStreaming) {
        if (activeThinkingState.stepEl) {
          activeThinkingState.stepEl.textContent = 'Synthesizing response...';
        }
        const modelDetail = drawer.querySelector('#thought-step-model-detail');
        if (modelDetail) {
          modelDetail.textContent = 'Synthesizing response with active model...';
        }
      }
    }, 600);
  }

  function createThinkingDrawer(cardEl) {
    if (!cardEl) return null;
    const existing = cardEl.querySelector('.thinking-drawer');
    if (existing) return existing;

    const drawer = document.createElement('div');
    drawer.className = 'thinking-drawer is-thinking is-expanded';
    drawer.innerHTML = `
      <div class="thinking-drawer-header">
        <div class="thinking-header-left">
          <span class="thinking-pulse-indicator">
            <span class="thinking-dot"></span>
            <span class="thinking-ring"></span>
          </span>
          <span class="thinking-glyph-icon">
            <svg viewBox="0 0 10 10" width="8" height="8" fill="currentColor">
              <polygon points="2,1 8,5 2,9"/>
            </svg>
          </span>
          <span class="thinking-header-title">Thinking...</span>
          <span class="thinking-stopwatch">(0s)</span>
        </div>
        <div class="thinking-header-right">
          <span class="thinking-progress-step">Analyzing query</span>
          <button type="button" class="btn-thinking-toggle" title="Toggle thoughts" aria-label="Toggle thinking">
            <span class="thinking-toggle-label">Hide</span>
            <svg class="thinking-toggle-icon" viewBox="0 0 10 10" width="7" height="7" fill="currentColor">
              <polygon points="1,7 9,7 5,2"/>
            </svg>
          </button>
        </div>
      </div>
      <div class="thinking-drawer-body">
        <div class="thought-cards-container"></div>
        <div class="thought-stream-container" style="display: none;">
          <div class="thought-stream-header">
            <span class="thought-stream-title">REASONING TRACE</span>
          </div>
          <pre class="thinking-thought-content"></pre>
        </div>
      </div>
    `;

    const contentEl = cardEl.querySelector('.assistant-content');
    if (contentEl) {
      cardEl.insertBefore(drawer, contentEl);
    } else {
      cardEl.appendChild(drawer);
    }

    const titleEl = drawer.querySelector('.thinking-header-title');
    const stopwatchEl = drawer.querySelector('.thinking-stopwatch');
    const stepEl = drawer.querySelector('.thinking-progress-step');
    const bodyEl = drawer.querySelector('.thinking-drawer-body');
    const cardsContainerEl = drawer.querySelector('.thought-cards-container');
    const streamContainerEl = drawer.querySelector('.thought-stream-container');
    const thoughtPreEl = drawer.querySelector('.thinking-thought-content');
    const toggleBtn = drawer.querySelector('.btn-thinking-toggle');
    const headerEl = drawer.querySelector('.thinking-drawer-header');

    const toggleDrawer = (e) => {
      if (e) e.stopPropagation();
      const isCurrentlyCollapsed = drawer.classList.contains('is-collapsed');
      if (isCurrentlyCollapsed) {
        drawer.classList.remove('is-collapsed');
        drawer.classList.add('is-expanded');
        if (toggleBtn) {
          const lbl = toggleBtn.querySelector('.thinking-toggle-label');
          if (lbl) lbl.textContent = 'Hide';
        }
      } else {
        drawer.classList.add('is-collapsed');
        drawer.classList.remove('is-expanded');
        if (toggleBtn) {
          const lbl = toggleBtn.querySelector('.thinking-toggle-label');
          if (lbl) lbl.textContent = 'Expand';
        }
      }
    };

    if (headerEl) {
      headerEl.addEventListener('click', toggleDrawer);
    }
    if (toggleBtn) {
      toggleBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        toggleDrawer();
      });
    }

    activeThinkingState.drawerEl = drawer;
    activeThinkingState.bodyEl = bodyEl;
    activeThinkingState.cardsContainerEl = cardsContainerEl;
    activeThinkingState.streamContainerEl = streamContainerEl;
    activeThinkingState.thoughtPreEl = thoughtPreEl;
    activeThinkingState.titleEl = titleEl;
    activeThinkingState.stopwatchEl = stopwatchEl;
    activeThinkingState.stepEl = stepEl;
    activeThinkingState.toggleBtn = toggleBtn;
    activeThinkingState.cardEl = cardEl;

    return drawer;
  }

  function finishThinkingDrawer(interrupted = false) {
    if (activeThinkingState.timerInterval) {
      clearInterval(activeThinkingState.timerInterval);
      activeThinkingState.timerInterval = null;
    }
    if (activeThinkingState.preStreamTimer) {
      clearTimeout(activeThinkingState.preStreamTimer);
      activeThinkingState.preStreamTimer = null;
    }
    activeThinkingState.isInsideThinking = false;
    activeThinkingState.hasFinishedThinking = true;
    activeThinkingState.isPreStreaming = false;

    const drawer = activeThinkingState.drawerEl;
    if (!drawer) return;

    // Transition model step to completed
    const modelStep = drawer.querySelector('#thought-step-model');
    if (modelStep) {
      modelStep.classList.remove('is-active');
      modelStep.classList.add('is-done');
      const iconEl = modelStep.querySelector('.thought-step-icon');
      if (iconEl) {
        iconEl.innerHTML = `
          <svg viewBox="0 0 12 12" width="9" height="9" fill="currentColor">
            <path d="M10.28 2.28L4.5 8.06 1.72 5.28a.75.75 0 00-1.06 1.06l3.5 3.5a.75.75 0 001.06 0l6.5-6.5a.75.75 0 00-1.06-1.06z"/>
          </svg>
        `;
      }
      const detail = modelStep.querySelector('#thought-step-model-detail');
      if (detail && (!activeThinkingState.thoughtPreEl || !activeThinkingState.thoughtPreEl.textContent.trim())) {
        detail.textContent = interrupted ? 'Generation cancelled' : 'Complete';
      }
    }

    drawer.classList.remove('is-thinking');
    drawer.classList.add('is-finished');

    // Auto-collapse into sleek summary pill
    drawer.classList.add('is-collapsed');
    drawer.classList.remove('is-expanded');

    const sec = activeThinkingState.elapsedSec || 1;
    if (activeThinkingState.titleEl) {
      activeThinkingState.titleEl.textContent = interrupted
        ? `Thought for ${sec}s (Interrupted)`
        : `Thought for ${sec}s`;
    }
    if (activeThinkingState.stopwatchEl) {
      activeThinkingState.stopwatchEl.textContent = '';
    }
    if (activeThinkingState.toggleBtn) {
      const lbl = activeThinkingState.toggleBtn.querySelector('.thinking-toggle-label');
      if (lbl) lbl.textContent = 'Expand';
    }
  }

  function stopChatGeneration(notifyHost = true) {
    flushStreamRender();
    if (!isChatStreaming) return;

    if (currentAssistantTextEl && accumulatedRawText) {
      const thinkOpenMatch = accumulatedRawText.match(/<(?:think|thinking)>/i);
      const thinkCloseMatch = accumulatedRawText.match(/<\/(?:think|thinking)>/i);
      if (thinkOpenMatch && thinkCloseMatch) {
        const answerText = accumulatedRawText.slice(thinkCloseMatch.index + thinkCloseMatch[0].length).trimStart();
        currentAssistantTextEl.innerHTML = formatMarkdown(answerText);
      } else if (!thinkOpenMatch) {
        currentAssistantTextEl.innerHTML = formatMarkdown(accumulatedRawText);
      }
    }

    if (notifyHost && isVsCode) {
      vscode.postMessage({ command: 'stopChat' });
      vscode.postMessage({ command: 'abortChatGeneration' });
    }

    try {
      fetch(`${BACKEND_URL}/api/chat/abort`, { method: 'POST' }).catch(() => {});
    } catch {}

    if (chatAbortController) {
      try {
        chatAbortController.abort();
      } catch { }
      chatAbortController = null;
    }

    setChatGeneratingState(false);
    updateAssistantCursorStates(null, false);

    if (activeThinkingState && activeThinkingState.isInsideThinking) {
      finishThinkingDrawer(true);
    }

    if (currentAssistantTextEl) {
      const stopNotice = document.createElement('div');
      stopNotice.className = 'chat-stopped-notice';
      stopNotice.style.cssText = 'color: #F97316; font-size: 10.5px; font-family: var(--font-mono); margin-top: 8px; display: inline-flex; align-items: center; gap: 4px;';
      stopNotice.innerHTML = '<span style="font-size: 11px;">■</span> [Generation stopped by user]';
      currentAssistantTextEl.appendChild(stopNotice);
    }
    showToast('Generation stopped', 'info');
  }

  // ==========================================
  // Pinned Mentions Context System (Option B)
  // ==========================================
  let pinnedMentions = [];
  let mentionAutocompleteItems = [];
  let mentionSelectedIndex = -1;
  let isMentionPopupVisible = false;
  let mentionDebounceTimer = null;
  let mentionAtTriggerIndex = -1;

  const mentionChipsTray = document.getElementById('mention-chips-tray');
  const mentionAutocompletePopup = document.getElementById('mention-autocomplete-popup');
  const mentionAutocompleteList = document.getElementById('mention-autocomplete-list');

  function renderMentionChipsTray() {
    if (!mentionChipsTray) return;
    if (!pinnedMentions || pinnedMentions.length === 0) {
      mentionChipsTray.style.display = 'none';
      mentionChipsTray.innerHTML = '';
      return;
    }

    mentionChipsTray.style.display = 'flex';
    mentionChipsTray.innerHTML = pinnedMentions.map((m, idx) => {
      const typeClass = `mention-chip-${m.type || 'file'}`;
      const badgeText = m.type === 'symbol'
        ? `[${(m.kind || 'SYM').toUpperCase()}]`
        : (m.type === 'folder' ? '[DIR]' : '[FILE]');
      return `
        <div class="mention-chip ${typeClass}" data-idx="${idx}" title="${escapeHtml(m.path || m.name)}">
          <span class="mention-chip-badge">${badgeText}</span>
          <span class="mention-chip-name">${escapeHtml(m.name)}</span>
          <button type="button" class="btn-remove-chip" data-idx="${idx}" title="Remove pinned mention" aria-label="Remove">
            <svg viewBox="0 0 10 10" width="7" height="7" fill="none" stroke="currentColor" stroke-width="1.8">
              <line x1="1" y1="1" x2="9" y2="9"/>
              <line x1="9" y1="1" x2="1" y2="9"/>
            </svg>
          </button>
        </div>
      `;
    }).join('');

    mentionChipsTray.querySelectorAll('.btn-remove-chip').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const idx = parseInt(btn.getAttribute('data-idx'), 10);
        if (!isNaN(idx) && idx >= 0 && idx < pinnedMentions.length) {
          pinnedMentions.splice(idx, 1);
          renderMentionChipsTray();
          if (chatInput) chatInput.focus();
        }
      });
    });
  }

  function hideMentionPopup() {
    isMentionPopupVisible = false;
    mentionSelectedIndex = -1;
    mentionAtTriggerIndex = -1;
    if (mentionAutocompletePopup) {
      mentionAutocompletePopup.style.display = 'none';
    }
  }

  function showMentionPopup() {
    isMentionPopupVisible = true;
    if (mentionAutocompletePopup) {
      mentionAutocompletePopup.style.display = 'flex';
    }
  }

  function getInstantLocalMentionMatches(query) {
    if (!query) return [];
    const q = query.toLowerCase();
    const results = [];

    // 1. Folders from in-memory tree
    if (currentRepoDirsList && currentRepoDirsList.length > 0) {
      for (const d of currentRepoDirsList) {
        const dName = (d.name || '').toLowerCase();
        const dPath = (d.path || '').toLowerCase();
        if (dName.includes(q) || dPath.includes(q)) {
          results.push({
            type: 'folder',
            name: d.name.endsWith('/') ? d.name : d.name + '/',
            path: d.path || d.name,
            icon: 'folder'
          });
          if (results.length >= 3) break;
        }
      }
    }

    // 2. Files from in-memory tree (instant < 1ms match)
    if (currentRepoFilesList && currentRepoFilesList.length > 0) {
      const fileMatches = [];
      for (const f of currentRepoFilesList) {
        const fname = (f.name || '').toLowerCase();
        const fpath = (f.path || '').toLowerCase();
        if (fname.includes(q) || fpath.includes(q)) {
          fileMatches.push({
            type: 'file',
            name: f.name,
            path: f.path || f.name,
            icon: 'file'
          });
        }
      }
      fileMatches.sort((a, b) => {
        const aN = a.name.toLowerCase();
        const bN = b.name.toLowerCase();
        const aExact = aN === q || aN.startsWith(q + '.');
        const bExact = bN === q || bN.startsWith(q + '.');
        if (aExact && !bExact) return -1;
        if (!aExact && bExact) return 1;
        const aPrefix = aN.startsWith(q);
        const bPrefix = bN.startsWith(q);
        if (aPrefix && !bPrefix) return -1;
        if (!aPrefix && bPrefix) return 1;
        return aN.localeCompare(bN);
      });
      results.push(...fileMatches.slice(0, 10));
    }
    return results;
  }

  async function fetchMentionCandidates(query) {
    try {
      const repoPath = (inputRepoPath ? inputRepoPath.value.trim() : '') || currentRepoPath || '';
      const url = `${BACKEND_URL}/api/context/mentions?query=${encodeURIComponent(query)}&workspace_path=${encodeURIComponent(repoPath)}&limit=12`;
      const res = await fetch(url);
      if (!res.ok) return;
      const data = await res.json();
      const serverResults = data.results || [];

      // Combine authoritative server results and current local matches
      const combined = [];
      const seen = new Set();
      for (const item of serverResults) {
        const key = `${item.type}:${item.name}:${item.path}`;
        if (!seen.has(key)) {
          seen.add(key);
          combined.push(item);
        }
      }

      // Only merge instant matches for the currently active workspace
      const localMatches = getInstantLocalMentionMatches(query);
      for (const item of localMatches) {
        const key = `${item.type}:${item.name}:${item.path}`;
        if (!seen.has(key)) {
          seen.add(key);
          combined.push(item);
        }
      }

      mentionAutocompleteItems = combined;
      renderMentionList(mentionAutocompleteItems);
    } catch (e) {
      console.warn('Mention candidate search failed:', e);
    }
  }

  function renderMentionList(items) {
    if (!mentionAutocompleteList) return;
    if (!items || items.length === 0) {
      mentionAutocompleteList.innerHTML = '<div class="mention-autocomplete-empty">No matching files or symbols found</div>';
      showMentionPopup();
      mentionSelectedIndex = -1;
      return;
    }

    mentionSelectedIndex = 0;
    mentionAutocompleteList.innerHTML = items.map((it, idx) => {
      const badgeCls = it.type === 'symbol' ? 'badge-symbol' : (it.type === 'folder' ? 'badge-folder' : 'badge-file');
      const badgeTxt = it.type === 'symbol' ? (it.kind || 'SYM').toUpperCase() : (it.type === 'folder' ? 'DIR' : 'FILE');
      const detailTxt = it.type === 'symbol'
        ? `${it.path}:${it.line || 1}`
        : (it.path || '');
      return `
        <div class="mention-autocomplete-item ${idx === 0 ? 'selected' : ''}" data-idx="${idx}">
          <span class="mention-item-badge ${badgeCls}">[${badgeTxt}]</span>
          <span class="mention-item-name">${escapeHtml(it.name)}</span>
          <span class="mention-item-detail">${escapeHtml(detailTxt)}</span>
        </div>
      `;
    }).join('');

    mentionAutocompleteList.querySelectorAll('.mention-autocomplete-item').forEach(el => {
      el.addEventListener('click', () => {
        const idx = parseInt(el.getAttribute('data-idx'), 10);
        if (!isNaN(idx) && mentionAutocompleteItems[idx]) {
          pinMentionItem(mentionAutocompleteItems[idx]);
        }
      });
    });

    showMentionPopup();
  }

  function updateMentionSelection() {
    if (!mentionAutocompleteList) return;
    const items = mentionAutocompleteList.querySelectorAll('.mention-autocomplete-item');
    items.forEach((it, idx) => {
      if (idx === mentionSelectedIndex) {
        it.classList.add('selected');
        it.scrollIntoView({ block: 'nearest' });
      } else {
        it.classList.remove('selected');
      }
    });
  }

  function pinMentionItem(item) {
    if (!item) return;

    // Option B: Remove the @<query> from chatInput leaving prompt clean
    if (chatInput && mentionAtTriggerIndex >= 0) {
      const val = chatInput.value;
      const before = val.slice(0, mentionAtTriggerIndex);
      const after = val.slice(chatInput.selectionStart);
      chatInput.value = before + after.trimStart();
      chatInput.selectionStart = before.length;
      chatInput.selectionEnd = before.length;
      chatInput.style.height = 'auto';
      chatInput.style.height = Math.min(chatInput.scrollHeight, 120) + 'px';
      chatInput.focus();
    }

    // Check for duplicate pinned mentions
    const exists = pinnedMentions.some(m => m.name === item.name && m.path === item.path && m.type === item.type);
    if (!exists) {
      pinnedMentions.push({
        type: item.type,
        name: item.name,
        path: item.path,
        kind: item.kind,
        line: item.line
      });
      renderMentionChipsTray();
      showToast(`Pinned context: ${item.name}`, 'info', 1400);
    }

    hideMentionPopup();
  }

  let lastChatAllowMulti = false;
  let lastChatTargetFiles = [];

  async function sendChatMessage() {
    if (!chatInput) return;
    const text = chatInput.value.trim();
    if (!text) return;

    await dispatchChatMessage(text);
  }

  async function dispatchChatMessage(text) {
    if (!text) return;
    setChatGeneratingState(true);

    // Hide empty state immediately
    hideChatEmptyState();

    // Copy currently pinned mentions and reset tray
    const mentionsPayload = [...pinnedMentions];
    pinnedMentions = [];
    renderMentionChipsTray();
    hideMentionPopup();

    // Append user message with pinned context pills
    lastUserQuery = text;
    appendUserMessage(text, mentionsPayload);
    if (chatInput) {
      chatInput.value = '';
      chatInput.style.height = 'auto';
    }

    // Create assistant streaming placeholder with prompt-aware dynamic reasoning
    createAssistantCard(mentionsPayload, text);

    const pathFromInput = inputRepoPath ? inputRepoPath.value.trim() : '';
    if (pathFromInput) {
      currentRepoPath = pathFromInput;
      localStorage.setItem('codebase_copilot_repo_path', currentRepoPath);
    }

    const historyPayload = chatSessionHistory.slice(-10);

    // Scope parameterization: evaluate target files from mentions and prompt text
    // The LLM decides scope naturally based on context and prompt instructions
    const fileMentions = mentionsPayload.filter(m => m.type === 'file');
    const filesFromText = getAllFilesFromGoal(text);
    const targetFilesSet = new Set(fileMentions.map(m => m.path || m.name));
    for (const f of filesFromText) {
      targetFilesSet.add(f);
    }
    const targetFiles = Array.from(targetFilesSet);

    // Multi-file is allowed if multiple files are referenced or if open-ended scope
    const allowMultiFile = targetFiles.length > 1 || (targetFiles.length === 0);
    if (targetFiles.length === 0 && activeEditorFile) {
      targetFiles.push(activeEditorFile);
    }

    lastChatAllowMulti = allowMultiFile;
    lastChatTargetFiles = targetFiles;

    if (isVsCode) {
      vscode.postMessage({
        command: 'userQuery',
        text,
        history: historyPayload,
        workspace_path: currentRepoPath,
        security_mode: currentSecurityMode,
        temperature: currentTemperature,
        num_ctx: currentNumCtx,
        num_predict: currentNumPredict,
        pinned_mentions: mentionsPayload,
        allow_multi_file: allowMultiFile,
        target_files: targetFiles
      });
      // Remains in generating state until streamComplete or streamStopped
    } else {
      try {
        chatAbortController = new AbortController();
        const response = await fetch(`${BACKEND_URL}/api/chat`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            query: text,
            history: historyPayload,
            workspace_path: currentRepoPath,
            security_mode: currentSecurityMode,
            temperature: currentTemperature,
            num_ctx: currentNumCtx,
            num_predict: currentNumPredict,
            pinned_mentions: mentionsPayload,
            allow_multi_file: allowMultiFile,
            target_files: targetFiles,
            stream: true
          }),
          signal: chatAbortController.signal
        });

        if (!response.ok) {
          appendStreamToken(`\n[Backend Error ${response.status}: Failed to stream]`);
          setChatGeneratingState(false);
          return;
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let rawBuffer = '';
        let fullResponse = '';

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          rawBuffer += decoder.decode(value, { stream: true });
          const lines = rawBuffer.split('\n');
          rawBuffer = lines.pop() || '';

          for (const line of lines) {
            if (line.startsWith('data: ')) {
              const dataStr = line.replace('data: ', '').trim();
              if (dataStr === '[DONE]') continue;
              try {
                const parsed = JSON.parse(dataStr);
                if (parsed.token) {
                  fullResponse += parsed.token;
                  appendStreamToken(parsed.token);
                }
                if (parsed.done) {
                  onStreamFinished(fullResponse, parsed.citations);
                }
              } catch {
                appendStreamToken(dataStr);
              }
            }
          }
        }
      } catch (err) {
        if (err.name !== 'AbortError') {
          appendStreamToken(`\n[Connection Error: ${err.message}]`);
        }
      } finally {
        setChatGeneratingState(false);
        chatAbortController = null;
      }
    }
  }

  if (sendBtn) {
    sendBtn.addEventListener('click', () => {
      if (isChatStreaming) {
        stopChatGeneration(true);
      } else {
        sendChatMessage();
      }
    });
  }

  if (chatInput) {
    chatInput.addEventListener('keydown', (e) => {
      if (isMentionPopupVisible && mentionAutocompleteItems.length > 0) {
        if (e.key === 'ArrowDown') {
          e.preventDefault();
          mentionSelectedIndex = (mentionSelectedIndex + 1) % mentionAutocompleteItems.length;
          updateMentionSelection();
          return;
        } else if (e.key === 'ArrowUp') {
          e.preventDefault();
          mentionSelectedIndex = (mentionSelectedIndex - 1 + mentionAutocompleteItems.length) % mentionAutocompleteItems.length;
          updateMentionSelection();
          return;
        } else if (e.key === 'Enter' || e.key === 'Tab') {
          e.preventDefault();
          if (mentionSelectedIndex >= 0 && mentionAutocompleteItems[mentionSelectedIndex]) {
            pinMentionItem(mentionAutocompleteItems[mentionSelectedIndex]);
          }
          return;
        } else if (e.key === 'Escape') {
          e.preventDefault();
          hideMentionPopup();
          return;
        }
      }

      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        if (isChatStreaming) {
          showToast('Model is currently generating. Click Stop to cancel.', 'info');
          return;
        }
        sendChatMessage();
      }
    });

    chatInput.addEventListener('input', () => {
      chatInput.style.height = 'auto';
      chatInput.style.height = Math.min(chatInput.scrollHeight, 120) + 'px';

      // Check for @ trigger
      const cursorPos = chatInput.selectionStart;
      const textBefore = chatInput.value.slice(0, cursorPos);
      const atIdx = textBefore.lastIndexOf('@');

      if (atIdx >= 0 && (atIdx === 0 || /\s/.test(textBefore[atIdx - 1]))) {
        const query = textBefore.slice(atIdx + 1);
        if (!/\s/.test(query)) {
          mentionAtTriggerIndex = atIdx;

          // Instant 0ms synchronous local match from in-memory workspace tree
          const localMatches = getInstantLocalMentionMatches(query);
          if (localMatches.length > 0) {
            mentionAutocompleteItems = localMatches;
            renderMentionList(localMatches);
          }

          if (mentionDebounceTimer) clearTimeout(mentionDebounceTimer);
          mentionDebounceTimer = setTimeout(() => {
            fetchMentionCandidates(query);
          }, 120);
          return;
        }
      }

      hideMentionPopup();
    });

    // Close mention popup if clicking outside
    document.addEventListener('click', (e) => {
      if (isMentionPopupVisible && mentionAutocompletePopup && !mentionAutocompletePopup.contains(e.target) && e.target !== chatInput) {
        hideMentionPopup();
      }
    });
  }

  function clearChatSession() {
    // Clear pinned mentions
    pinnedMentions = [];
    renderMentionChipsTray();
    hideMentionPopup();

    if (messageList) {
      // Remove all user and assistant chat bubbles and action blocks
      const dynamicItems = messageList.querySelectorAll('.user-bubble, .assistant-card, .chat-agent-action');
      dynamicItems.forEach((el) => el.remove());

      // Restore holder screen ("CLAY Copilot" empty state card)
      if (chatEmptyState) {
        showChatEmptyState();
        if (!messageList.contains(chatEmptyState)) {
          messageList.prepend(chatEmptyState);
        }
      }
    }

    if (chatInput) {
      chatInput.value = '';
      chatInput.style.height = 'auto';
    }

    lastUserQuery = '';
    accumulatedRawText = '';
    currentAssistantTextEl = null;
    isChatStreaming = false;
    chatSessionHistory = [];
    try {
      sessionStorage.removeItem('clay_chat_session_history');
    } catch { }

    // Clear staged diff / review bar if present
    clearStagedDiff();

    // Hide jump down button
    if (btnChatJumpDown) {
      btnChatJumpDown.classList.remove('visible');
    }

    // Notify VS Code extension host to clear context
    if (isVsCode) {
      vscode.postMessage({
        command: 'clearChatContext'
      });
    }

    // Notify backend endpoint to reset chat context
    fetch(`${BACKEND_URL}/api/chat/clear`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    }).catch(() => { });

    showToast('Chat & context cleared', 'info');
  }

  if (btnClearChat) {
    btnClearChat.addEventListener('click', (e) => {
      e.preventDefault();
      clearChatSession();
    });
  }

  function getFormattedTime() {
    const d = new Date();
    let hours = d.getHours();
    const minutes = String(d.getMinutes()).padStart(2, '0');
    const ampm = hours >= 12 ? 'PM' : 'AM';
    hours = hours % 12;
    hours = hours ? hours : 12;
    return `${hours}:${minutes} ${ampm}`;
  }

  function copyTextToClipboard(text, btn) {
    if (!text) return;
    const onCopied = () => {
      btn.classList.add('copied');
      btn.setAttribute('title', 'Copied!');
      setTimeout(() => {
        btn.classList.remove('copied');
        btn.setAttribute('title', 'Copy text');
      }, 2000);
    };

    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(onCopied).catch(() => {
        fallbackCopyText(text, onCopied);
      });
    } else {
      fallbackCopyText(text, onCopied);
    }
  }

  function fallbackCopyText(text, callback) {
    try {
      const textArea = document.createElement('textarea');
      textArea.value = text;
      textArea.style.position = 'fixed';
      textArea.style.left = '-999999px';
      textArea.style.top = '-999999px';
      document.body.appendChild(textArea);
      textArea.focus();
      textArea.select();
      document.execCommand('copy');
      textArea.remove();
      if (callback) callback();
    } catch (err) {
      console.error('Copy fallback failed', err);
    }
  }

  function createBubbleFooter(getTextToCopy, role = 'user') {
    const footer = document.createElement('div');
    footer.className = `bubble-footer ${role}-bubble-footer`;

    const timeSpan = document.createElement('span');
    timeSpan.className = 'bubble-timestamp';
    timeSpan.textContent = getFormattedTime();

    const copyBtn = document.createElement('button');
    copyBtn.type = 'button';
    copyBtn.className = 'bubble-copy-btn';
    copyBtn.setAttribute('title', 'Copy text');
    copyBtn.setAttribute('aria-label', 'Copy message');
    copyBtn.innerHTML = `
      <svg class="icon-copy" viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">
        <rect x="5.5" y="5.5" width="7.5" height="7.5" rx="1.5"/>
        <path d="M3.5 10.5V3.5A1.5 1.5 0 0 1 5 2h6"/>
      </svg>
      <svg class="icon-copied" viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="3.5 8.5 6.5 11.5 12.5 4.5"/>
      </svg>
      <span class="bubble-copy-tooltip">Copied!</span>
    `;

    copyBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const text = typeof getTextToCopy === 'function' ? getTextToCopy() : getTextToCopy;
      copyTextToClipboard(text, copyBtn);
    });

    footer.appendChild(timeSpan);
    footer.appendChild(copyBtn);
    return footer;
  }

  function appendUserMessage(text, mentions = []) {
    hideChatEmptyState();
    const bubble = document.createElement('div');
    bubble.className = 'user-bubble';

    if (mentions && mentions.length > 0) {
      const badgesContainer = document.createElement('div');
      badgesContainer.className = 'user-pinned-badges';
      badgesContainer.innerHTML = mentions.map((m) => {
        const bText = m.type === 'symbol'
          ? `[${(m.kind || 'SYM').toUpperCase()}]`
          : (m.type === 'folder' ? '[DIR]' : '[FILE]');
        return `<span class="user-pinned-pill"><span style="font-family: var(--font-pixel); font-size: 7px; font-weight: 700;">${bText}</span> ${escapeHtml(m.name)}</span>`;
      }).join('');
      bubble.appendChild(badgesContainer);
    }

    const textDiv = document.createElement('div');
    textDiv.className = 'bubble-content';
    textDiv.innerHTML = escapeHtml(text);
    bubble.appendChild(textDiv);

    const footer = createBubbleFooter(() => text, 'user');
    bubble.appendChild(footer);

    messageList.appendChild(bubble);
    scrollChatToBottom();
  }

  function createAssistantCard(pinnedList = [], promptText = '') {
    flushStreamRender();
    accumulatedRawText = '';
    hideChatEmptyState();
    resetActiveThinkingState();

    const card = document.createElement('div');
    card.className = 'assistant-card is-latest is-processing';

    card.innerHTML = `
      <div class="assistant-header">
        <div class="pixel-prompt-badge" title="CLAY">
          <svg class="pixel-prompt-glyph" viewBox="0 0 20 12" width="30" height="18" fill="currentColor" shape-rendering="crispEdges">
            <path class="pixel-prompt-chevron" d="M1,1 h2 v2 h-2 z M3,3 h2 v2 h-2 z M5,5 h2 v2 h-2 z M3,7 h2 v2 h-2 z M1,9 h2 v2 h-2 z"/>
            <path class="pixel-prompt-cursor" d="M9,9 h10 v2 h-10 z"/>
          </svg>
        </div>
      </div>
      <div class="assistant-content"><span class="streaming-cursor"></span></div>
    `;

    const contentEl = card.querySelector('.assistant-content');
    const footer = createBubbleFooter(() => {
      if (contentEl) {
        const clone = contentEl.cloneNode(true);
        const cursor = clone.querySelector('.streaming-cursor');
        if (cursor) cursor.remove();
        return clone.innerText.trim();
      }
      return '';
    }, 'assistant');
    card.appendChild(footer);

    messageList.appendChild(card);
    currentAssistantTextEl = contentEl;
    updateAssistantCursorStates(card, true);

    // Launch prompt-aware dynamic reasoning phase immediately
    startPreStreamingReasoning(card, pinnedList, promptText);

    scrollChatToBottom();
  }

  let accumulatedRawText = '';
  let streamRenderRafId = null;
  let isStreamRenderPending = false;

  function renderStreamFrame() {
    isStreamRenderPending = false;
    streamRenderRafId = null;
    if (!currentAssistantTextEl) return;

    const thinkOpenMatch = accumulatedRawText.match(/<(?:think|thinking)>/i);
    const thinkCloseMatch = accumulatedRawText.match(/<\/(?:think|thinking)>/i);

    if (thinkOpenMatch) {
      if (!thinkCloseMatch) {
        currentAssistantTextEl.innerHTML = '<span class="streaming-cursor"></span>';
      } else {
        const answerText = accumulatedRawText.slice(thinkCloseMatch.index + thinkCloseMatch[0].length).trimStart();
        currentAssistantTextEl.innerHTML = formatMarkdown(answerText) + '<span class="streaming-cursor"></span>';
      }
    } else {
      currentAssistantTextEl.innerHTML = formatMarkdown(accumulatedRawText) + '<span class="streaming-cursor"></span>';
    }
    scrollChatToBottom();
  }

  function scheduleStreamRender() {
    if (!isStreamRenderPending) {
      isStreamRenderPending = true;
      streamRenderRafId = requestAnimationFrame(renderStreamFrame);
    }
  }

  function flushStreamRender() {
    if (streamRenderRafId) {
      cancelAnimationFrame(streamRenderRafId);
      streamRenderRafId = null;
    }
    isStreamRenderPending = false;
  }

  function appendStreamToken(token) {
    if (!currentAssistantTextEl) return;
    accumulatedRawText += token;

    const card = currentAssistantTextEl.closest('.assistant-card');
    if (card && !card.classList.contains('is-processing')) {
      updateAssistantCursorStates(card, true);
    }

    const thinkOpenMatch = accumulatedRawText.match(/<(?:think|thinking)>/i);
    const thinkCloseMatch = accumulatedRawText.match(/<\/(?:think|thinking)>/i);

    if (thinkOpenMatch) {
      activeThinkingState.isPreStreaming = false;
      const thinkStartIdx = thinkOpenMatch.index + thinkOpenMatch[0].length;

      if (!thinkCloseMatch) {
        // Actively inside thinking!
        if (!activeThinkingState.isInsideThinking && !activeThinkingState.hasFinishedThinking) {
          activeThinkingState.isInsideThinking = true;
          activeThinkingState.startTime = Date.now();
          createThinkingDrawer(card);
          activeThinkingState.timerInterval = setInterval(() => {
            if (!activeThinkingState.isInsideThinking) return;
            const s = Math.max(1, Math.round((Date.now() - activeThinkingState.startTime) / 1000));
            activeThinkingState.elapsedSec = s;
            if (activeThinkingState.stopwatchEl) {
              activeThinkingState.stopwatchEl.textContent = `(${s}s)`;
            }
          }, 1000);
        }

        const thoughtsSoFar = accumulatedRawText.slice(thinkStartIdx);
        if (activeThinkingState.streamContainerEl) {
          activeThinkingState.streamContainerEl.style.display = 'block';
        }
        if (activeThinkingState.thoughtPreEl) {
          activeThinkingState.thoughtPreEl.textContent = thoughtsSoFar;
        }
        if (activeThinkingState.bodyEl) {
          activeThinkingState.bodyEl.scrollTop = activeThinkingState.bodyEl.scrollHeight;
        }

        const modelStep = card ? card.querySelector('#thought-step-model') : null;
        if (modelStep) {
          const detail = modelStep.querySelector('#thought-step-model-detail');
          if (detail) detail.textContent = 'Streaming reasoning trace...';
        }

        // Dynamic progress update: extract latest meaningful line of thought
        if (activeThinkingState.stepEl) {
          const lines = thoughtsSoFar.split('\n').map((l) => l.trim()).filter(Boolean);
          if (lines.length > 0) {
            const lastLine = lines[lines.length - 1].replace(/^[#\-*`> ]+/, '').trim();
            if (lastLine && lastLine.length <= 55) {
              activeThinkingState.stepEl.textContent = lastLine;
            }
          }
        }

        scheduleStreamRender();
        return;
      } else {
        // Thinking tag closed!
        if (activeThinkingState.isInsideThinking) {
          finishThinkingDrawer(false);
        } else if (!activeThinkingState.hasFinishedThinking) {
          createThinkingDrawer(card);
          activeThinkingState.elapsedSec = Math.max(1, Math.round((Date.now() - (activeThinkingState.startTime || Date.now())) / 1000));
          finishThinkingDrawer(false);
        }

        const thoughts = accumulatedRawText.slice(thinkStartIdx, thinkCloseMatch.index).trim();
        if (activeThinkingState.streamContainerEl) {
          activeThinkingState.streamContainerEl.style.display = 'block';
        }
        if (activeThinkingState.thoughtPreEl) {
          activeThinkingState.thoughtPreEl.textContent = thoughts;
        }

        scheduleStreamRender();
        return;
      }
    }

    // Standard model without <think> tags (e.g. GPT-OSS 12B, Gemma, Llama 3):
    // First token arrived! Cleanly finish pre-streaming thinking drawer and collapse.
    if (activeThinkingState.isPreStreaming) {
      activeThinkingState.isPreStreaming = false;
      activeThinkingState.elapsedSec = Math.max(1, Math.round((Date.now() - activeThinkingState.startTime) / 1000));
      finishThinkingDrawer(false);
    }

    // Default: normal markdown stream without think block
    scheduleStreamRender();
  }

  function onStreamFinished(fullText, citations) {
    flushStreamRender();
    setChatGeneratingState(false);
    if (activeThinkingState.isInsideThinking) {
      finishThinkingDrawer(false);
    }

    let answerText = fullText;
    const thinkOpenMatch = fullText.match(/<(?:think|thinking)>/i);
    const thinkCloseMatch = fullText.match(/<\/(?:think|thinking)>/i);

    if (thinkOpenMatch && thinkCloseMatch) {
      const card = currentAssistantTextEl ? currentAssistantTextEl.closest('.assistant-card') : null;
      if (card && !card.querySelector('.thinking-drawer')) {
        createThinkingDrawer(card);
        finishThinkingDrawer(false);
      }
      const thoughts = fullText.slice(thinkOpenMatch.index + thinkOpenMatch[0].length, thinkCloseMatch.index).trim();
      if (activeThinkingState.thoughtPreEl) {
        activeThinkingState.thoughtPreEl.textContent = thoughts;
      }
      answerText = fullText.slice(thinkCloseMatch.index + thinkCloseMatch[0].length).trimStart();
    }

    if (currentAssistantTextEl) {
      currentAssistantTextEl.innerHTML = formatMarkdown(answerText);
      const card = currentAssistantTextEl.closest('.assistant-card');

      // Detect unified diff or code block
      const diffMatch = answerText.match(/```(?:diff)?\n([\s\S]*?@@[\s\S]*?)```/);
      let detectedDiff = null;
      let targetFilename = null;

      const isCreationQuery = /\b(create|new\s+file|put\s+.*in|write\s+.*in|save\s+as|in\s+a\s+new\s+file|make\s+(?:a\s+)?(?:new\s+)?file)\b/i.test(lastUserQuery || '');
      const queryFile = getPrimaryRepoFile(lastUserQuery || '');
      const streamActiveFile = getActiveEditorFileInRepo();
      const repoActiveFilename = streamActiveFile ? streamActiveFile.replace(/\\/g, '/').split('/').pop() : null;

      if (diffMatch) {
        detectedDiff = diffMatch[1].trim();
        targetFilename = extractDiffFilename(detectedDiff) || (isCreationQuery && queryFile ? queryFile : null) || repoActiveFilename || queryFile || getPrimaryRepoFile(answerText);
      } else {
        const rawDiffMatch = answerText.match(/(--- (?:a\/|\/dev\/null)[^\n]+\n\+\+\+ (?:b\/|[^\n]+)[^\n]*\n@@[\s\S]*?)(?:```|$)/);
        if (rawDiffMatch) {
          detectedDiff = rawDiffMatch[1].trim();
          targetFilename = extractDiffFilename(detectedDiff) || (isCreationQuery && queryFile ? queryFile : null) || repoActiveFilename || queryFile || getPrimaryRepoFile(answerText);
        }
      }

      if (targetFilename) {
        targetFilename = targetFilename.replace(/(\.[a-zA-Z0-9]+)\1+$/gi, '$1');
      }

      // Multi-code-block and multi-file extraction from cleaned answerText (excluding thinking block)
      const codeBlockRegex = /```(?!mermaid\b)([a-zA-Z0-9_\-\.:/]+)?\s*\n([\s\S]*?)```/gi;
      const allExtractedBlocks = [];
      let blockMatch;
      while ((blockMatch = codeBlockRegex.exec(answerText)) !== null) {
        const header = (blockMatch[1] || '').trim();
        const code = (blockMatch[2] || '').trim();
        if (!code || ['mermaid', 'json', 'bash', 'sh', 'terminal', 'output', 'text'].includes(header.toLowerCase())) {
          continue;
        }

        let fn = null;
        if (header.includes(':')) {
          fn = header.split(':')[1].trim();
        } else if (header.includes('.') && !header.startsWith('.')) {
          fn = header;
        }
        if (!fn && code) {
          const lines = code.split('\n').map(l => l.trim()).filter(Boolean);
          for (const cl of lines.slice(0, 5)) {
            const flm = cl.match(/^(?:#|\/\/|\/\*|<!--|--|;)\s*(?:File:?|filepath:?|path:?|filename:?)?\s*([A-Za-z0-9_\-\.\/]+\.[A-Za-z0-9]+)/i);
            if (flm) { fn = flm[1].trim(); break; }
          }
        }
        if (!fn) {
          const pre = answerText.substring(Math.max(0, blockMatch.index - 300), blockMatch.index).trim();
          const lines = pre.split('\n').filter(l => l.trim());
          if (lines.length > 0) {
            const lastLine = lines[lines.length - 1];
            const preM = lastLine.match(/(?:###|\*\*|File:?|Target:?|Path:?|In|Update|Create)?\s*[`'\"]?([A-Za-z0-9_\-\.\/]+\.[A-Za-z0-9]+)[`'\"]?/i);
            if (preM && preM[1].includes('.')) fn = preM[1].trim();
          }
        }
        allExtractedBlocks.push({ file_path: fn || targetFilename || 'file', code: code });
      }

      const detectedCode = allExtractedBlocks.length > 0 ? allExtractedBlocks.map(b => b.code).join('\n\n') : null;
      const activeFile = getActiveEditorFileInRepo();
      const activeContent = getActiveEditorContentInRepo();
      const activeBaseFilename = activeFile ? activeFile.replace(/\\/g, '/').split('/').pop() : null;
      if (!targetFilename) {
        targetFilename = (allExtractedBlocks[0] && allExtractedBlocks[0].file_path) || getPrimaryRepoFile(lastUserQuery || answerText) || activeBaseFilename || repoActiveFilename || 'solution.py';
      }

      // 1. Immediate staging for fast UI feedback
      if (allExtractedBlocks.length > 1) {
        const initialStaged = allExtractedBlocks.map(b => {
          const rawLines = b.code.split('\n');
          const d = `--- /dev/null\n+++ b/${b.file_path}\n@@ -0,0 +1,${rawLines.length} @@\n` + rawLines.map(l => `+${l}`).join('\n');
          return {
            file_path: b.file_path,
            diff: d,
            adds: rawLines.length,
            dels: 0,
            applied: false
          };
        });
        stageDiff(initialStaged[0].file_path, initialStaged[0].diff, initialStaged);
      } else if (detectedDiff && targetFilename) {
        stageDiff(targetFilename, detectedDiff);
      } else if (allExtractedBlocks.length === 1 && targetFilename) {
        const b = allExtractedBlocks[0];
        const rawLines = b.code.split('\n');
        const initialDiff = `--- /dev/null\n+++ b/${targetFilename}\n@@ -0,0 +1,${rawLines.length} @@\n` + rawLines.map(l => `+${l}`).join('\n');
        stageDiff(targetFilename, initialDiff);
      }

      // 2. Asynchronously compute exact surgical unified diffs across all target files
      if (allExtractedBlocks.length > 0 || (detectedDiff && targetFilename)) {
        (async () => {
          try {
            const isTargetActive = streamActiveFile && isSameFileReference(streamActiveFile, targetFilename);
            const compTargetFiles = allExtractedBlocks.length > 1 ? allExtractedBlocks.map(b => b.file_path) : (lastChatTargetFiles || undefined);
            const compAllowMulti = allExtractedBlocks.length > 1 || Boolean(lastChatAllowMulti);
            const compRes = await fetch(`${BACKEND_URL}/api/agent/compute-diff`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                target_file: targetFilename,
                target_files: compTargetFiles,
                allow_multi_file: compAllowMulti,
                code_or_snippet: answerText,
                workspace_path: currentRepoPath,
                active_file_content: isTargetActive ? (activeEditorContent || undefined) : undefined,
                preferred_line: isTargetActive ? (activeEditorCursorLine || undefined) : undefined
              })
            });
            if (compRes.ok) {
              const compData = await compRes.json();
              if (compData && compData.staged_files && compData.staged_files.length > 0) {
                const firstF = compData.staged_files[0];
                stageDiff(firstF.file_path, firstF.diff, compData.staged_files);
              } else if (compData && compData.diff && compData.diff.trim().length > 0) {
                stageDiff(targetFilename, compData.diff);
              }
            }
          } catch (e) {
            console.warn('[compute-diff in onStreamFinished failed]', e);
          }
        })();
      }

      // Detect code change intent in user query or response
      const codeIntentRegex = /\b(create|write|add|implement|fix|refactor|change|update|make|delete|build|patch|edit|modify)\b/i;
      const textWithoutMermaid = fullText.replace(/```mermaid[\s\S]*?```/gi, '');
      const hasExecutableCode = /```(?!mermaid\b)[a-zA-Z0-9_-]*\n[\s\S]*?```/i.test(textWithoutMermaid);

      // Only show "Run in Agent" for actual actionable code diffs or proposed script implementations
      // Never show "Run in Agent" for purely visual architecture diagrams or explanations
      const isActionableCode = Boolean(detectedDiff || (detectedCode && hasExecutableCode && codeIntentRegex.test(lastUserQuery)));

      if (card && isActionableCode) {
        if (!card.querySelector('.btn-run-in-agent')) {
          const actionDiv = document.createElement('div');
          actionDiv.className = 'chat-agent-action';
          actionDiv.innerHTML = `
            <button class="btn-run-in-agent" title="Apply these exact generated changes in the Agent tab">
              <svg viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></svg>
              <span>Run in Agent</span>
            </button>
          `;

          const footer = card.querySelector('.assistant-bubble-footer');
          if (footer) {
            card.insertBefore(actionDiv, footer);
          } else {
            card.appendChild(actionDiv);
          }

          const capturedCode = detectedCode;
          const capturedTarget = targetFilename || getPrimaryRepoFile(lastUserQuery || fullText);
          const capturedGoal = lastUserQuery || 'Apply generated code changes';
          const capturedTargets = allExtractedBlocks.length > 0 ? allExtractedBlocks.map(b => b.file_path) : (capturedTarget ? [capturedTarget] : []);

          actionDiv.querySelector('.btn-run-in-agent').addEventListener('click', async () => {
            switchTab('agent');
            const agentTaskLabel = capturedTargets.length > 1
              ? `Apply changes across ${capturedTargets.length} files (${capturedTargets.join(', ')})`
              : (capturedTarget ? `Apply changes for ${capturedTarget}` : capturedGoal);
            if (agentGoalInput) {
              agentGoalInput.value = agentTaskLabel;
              resizeAgentGoalInput();
            }
            await runAgentTask({
              pregeneratedDiff: detectedDiff,
              pregeneratedCode: capturedCode,
              targetFile: capturedTarget,
              targetFiles: capturedTargets,
              stagedFiles: (stagedMultiFiles && stagedMultiFiles.length > 0) ? stagedMultiFiles : null,
              sourceGoal: capturedGoal
            });
          });
        }
      }
    }

    // Record this completed conversational turn into chatSessionHistory
    if (lastUserQuery && fullText) {
      chatSessionHistory.push({ role: 'user', content: lastUserQuery });
      chatSessionHistory.push({ role: 'assistant', content: fullText });
      if (chatSessionHistory.length > 20) {
        chatSessionHistory = chatSessionHistory.slice(-20);
      }
      saveChatSessionHistory();
    }

    accumulatedRawText = '';
    updateAssistantCursorStates(null, false);
    scrollChatToBottom();
  }

  function scrollChatToBottom(smooth = false) {
    const container = document.getElementById('view-chat');
    if (container) {
      if (smooth) {
        container.scrollTo({ top: container.scrollHeight, behavior: 'smooth' });
      } else {
        container.scrollTop = container.scrollHeight;
      }
    }
    if (btnChatJumpDown) btnChatJumpDown.classList.remove('visible');
  }

  if (btnChatJumpDown) {
    btnChatJumpDown.addEventListener('click', (e) => {
      e.preventDefault();
      scrollChatToBottom(true);
    });
  }

  const chatScrollContainer = document.getElementById('view-chat');
  if (chatScrollContainer) {
    chatScrollContainer.addEventListener('scroll', checkChatScrollPosition, { passive: true });
  }

  // ==========================================
  // 4. Agent: Multi-Step Execution with Run/Stop
  // ==========================================
  const agentGoalInput = document.getElementById('agent-goal-input');
  const btnRunAgent = document.getElementById('btn-run-agent');
  const btnStopAgent = document.getElementById('btn-stop-agent');
  const agentStepList = document.getElementById('agent-step-list');
  const agentProgressTrack = document.getElementById('agent-progress-track');
  const agentProgressBar = document.getElementById('agent-progress-bar');
  const agentProgressText = document.getElementById('agent-progress-text');
  const agentTerminalStatus = document.getElementById('agent-terminal-status');
  const terminalStream = document.getElementById('terminal-stream');
  const agentAmbiguityBar = document.getElementById('agent-ambiguity-bar');
  const ambiguityQuestion = document.getElementById('ambiguity-question');
  const ambiguityChipsList = document.getElementById('ambiguity-chips-list');
  let pendingAmbiguityResolver = null;

  function renderAmbiguityBar(ambiguity) {
    if (!agentAmbiguityBar || !ambiguityChipsList) return;
    if (ambiguityQuestion) ambiguityQuestion.textContent = ambiguity.question || 'Which file would you like to update?';
    ambiguityChipsList.innerHTML = (ambiguity.candidates || []).map(c => `
      <button type="button" class="ambiguity-chip" data-file="${escapeHtml(c)}" title="Select ${escapeHtml(c)} as target">
        <svg class="ambiguity-chip-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
          <polyline points="14 2 14 8 20 8"></polyline>
        </svg>
        <span class="ambiguity-chip-text">${escapeHtml(c)}</span>
        <span class="ambiguity-chip-badge">SELECT</span>
      </button>
    `).join('');

    ambiguityChipsList.querySelectorAll('.ambiguity-chip').forEach(chip => {
      chip.addEventListener('click', () => {
        const chosenFile = chip.getAttribute('data-file');
        if (agentGoalInput) {
          const curVal = agentGoalInput.value.trim();
          if (!curVal.toLowerCase().includes(chosenFile.toLowerCase())) {
            agentGoalInput.value = `${curVal} in ${chosenFile}`;
            resizeAgentGoalInput();
          }
        }
        hideAmbiguityBar();
        showToast(`Target confirmed: ${chosenFile}`, 'info');
        if (pendingAmbiguityResolver) {
          const resolver = pendingAmbiguityResolver;
          pendingAmbiguityResolver = null;
          resolver(chosenFile);
        }
      });
    });

    agentAmbiguityBar.style.display = 'block';
  }

  function hideAmbiguityBar() {
    if (agentAmbiguityBar) agentAmbiguityBar.style.display = 'none';
  }

  function normalizeGoalText(text) {
    if (!text) return '';
    let t = text;
    const typoMap = [
      [/\bsophesticated\b/gi, 'sophisticated'],
      [/\bsophistcated\b/gi, 'sophisticated'],
      [/\badn\b/gi, 'and'],
      [/\balgo\b/gi, 'algorithm'],
      [/\balgos\b/gi, 'algorithms'],
      [/\bastar\b/gi, 'a-star algorithm'],
      [/\ba\*\b/gi, 'a-star algorithm'],
      [/\ba-star\b/gi, 'a-star algorithm'],
      [/\bdsa\b/gi, 'data structures and algorithms'],
      [/\bfucntion\b/gi, 'function'],
      [/\bfuncton\b/gi, 'function'],
      [/\bimplemet\b/gi, 'implement'],
      [/\bimpliment\b/gi, 'implement'],
      [/\bcompelx\b/gi, 'complex'],
      [/\bdelte\b/gi, 'delete'],
      [/\bfoler\b/gi, 'folder'],
      [/\bfloder\b/gi, 'folder']
    ];
    for (const [pattern, repl] of typoMap) {
      t = t.replace(pattern, repl);
    }
    return t;
  }

  function resolveTopicDomain(goal) {
    const normalized = normalizeGoalText(goal).toLowerCase();

    if (normalized.includes('a*') || normalized.includes('astar') || normalized.includes('a-star') || normalized.includes('pathfinding')) {
      return {
        topic: 'A* Pathfinding Algorithm',
        domain: 'pathfinding',
        heuristic: 'Manhattan & Euclidean heuristics',
        dataStructure: 'Node representation & min-heap priority queue',
        edgeCases: 'obstacle traps, unreachable targets & boundary conditions'
      };
    }
    if (normalized.includes('dijkstra')) {
      return {
        topic: "Dijkstra's Shortest Path Algorithm",
        domain: 'graph search',
        heuristic: 'accumulated edge weights',
        dataStructure: 'min-heap priority queue and adjacency graph',
        edgeCases: 'disconnected nodes and weighted cycles'
      };
    }
    if (normalized.includes('bfs') || normalized.includes('dfs') || normalized.includes('breadth first') || normalized.includes('depth first')) {
      return {
        topic: 'Graph Traversal Engine (BFS/DFS)',
        domain: 'graph search',
        heuristic: 'visited sets and frontier queue',
        dataStructure: 'deque frontier and visited tracking',
        edgeCases: 'cyclic graphs and isolated vertices'
      };
    }
    if (normalized.includes('binary search') || normalized.includes('bsearch') || normalized.includes('bisect')) {
      return {
        topic: 'Binary Search & Bisect Engine',
        domain: 'search algorithm',
        heuristic: 'monotonic midpoint bisection',
        dataStructure: 'ordered arrays and search pointers',
        edgeCases: 'empty lists, duplicates, and out-of-bound targets'
      };
    }
    if (normalized.includes('sort') || normalized.includes('quicksort') || normalized.includes('mergesort') || normalized.includes('heapsort')) {
      return {
        topic: 'Optimized Divide-and-Conquer Sorting Algorithm',
        domain: 'sorting',
        heuristic: 'partition pivot and merge balance',
        dataStructure: 'contiguous arrays and auxiliary partitions',
        edgeCases: 'already-sorted arrays, reverse arrays, and duplicates'
      };
    }
    if (normalized.includes('dynamic programming') || normalized.includes('dp') || normalized.includes('memoization') || normalized.includes('knapsack')) {
      return {
        topic: 'Dynamic Programming & Memoization Engine',
        domain: 'dynamic programming',
        heuristic: 'optimal substructure and overlapping subproblems',
        dataStructure: 'memoization table and state transitions',
        edgeCases: 'base cases, 0-weight boundaries, and large state matrices'
      };
    }
    if (normalized.includes('tree') || normalized.includes('bst') || normalized.includes('avl') || normalized.includes('red-black') || normalized.includes('red black')) {
      return {
        topic: 'Self-Balancing Binary Search Tree',
        domain: 'data structures',
        heuristic: 'balance factors and rotational invariants',
        dataStructure: 'TreeNode references with left/right child pointers',
        edgeCases: 'duplicate keys, root deletions, and zigzag rotations'
      };
    }
    if (normalized.includes('trie') || normalized.includes('prefix tree')) {
      return {
        topic: 'Prefix Tree (Trie) Search Engine',
        domain: 'string search',
        heuristic: 'common prefix compression',
        dataStructure: 'TrieNode with character edge mapping and terminal flags',
        edgeCases: 'empty strings, unicode prefixes, and nonexistent keys'
      };
    }
    if (normalized.includes('lru') || normalized.includes('cache')) {
      return {
        topic: 'Least-Recently-Used (LRU) Cache',
        domain: 'data caching',
        heuristic: 'temporal locality and eviction policy',
        dataStructure: 'doubly-linked list with hash map lookup',
        edgeCases: 'capacity zero, repeated lookups, and concurrent eviction'
      };
    }
    if (normalized.includes('rate limit') || normalized.includes('token bucket') || normalized.includes('leaky bucket')) {
      return {
        topic: 'Token Bucket Rate Limiting Engine',
        domain: 'network protocols',
        heuristic: 'token refill rate and burst capacity',
        dataStructure: 'thread-safe state buckets with timestamp tracking',
        edgeCases: 'clock drift, high concurrency, and depleted burst quotas'
      };
    }
    if (normalized.includes('snake') || normalized.includes('game') || normalized.includes('curses') || normalized.includes('paddle') || normalized.includes('pong')) {
      return {
        topic: 'Real-Time Game Loop & Movement Engine',
        domain: 'game development',
        heuristic: 'frame delta timing and collision bounds',
        dataStructure: 'coordinate vectors, event queues, and screen buffers',
        edgeCases: 'border collisions, self-intersections, and input buffering'
      };
    }
    if (normalized.includes('auth') || normalized.includes('jwt') || normalized.includes('oauth') || normalized.includes('token')) {
      return {
        topic: 'Authentication & JWT Token Verification Pipeline',
        domain: 'security architecture',
        heuristic: 'cryptographic signature and expiration claims',
        dataStructure: 'header payload models and crypto verification context',
        edgeCases: 'expired tokens, malformed headers, and secret mismatch'
      };
    }

    const match = normalized.match(/\b(?:code\s+for|implement|write(?:\s+me)?(?:\s+code\s+for)?|build|create)\s+([a-zA-Z0-9_\-\s]{3,30}?)(?:\s+in|\s+into|\s+for|\s+file|$)/);
    if (match) {
      const noise = new Set(['a', 'an', 'the', 'new', 'me', 'some', 'large', 'sophisticated', 'code', 'file']);
      const tokens = match[1].trim().split(/\s+/).filter(t => !noise.has(t));
      if (tokens.length > 0) {
        const phrase = tokens.map(t => t.charAt(0).toUpperCase() + t.slice(1)).join(' ');
        return {
          topic: `${phrase} Engine`,
          domain: 'software architecture',
          heuristic: 'state specifications and invariant models',
          dataStructure: 'core data models and helper functions',
          edgeCases: 'input validation, boundary cases, and error handling'
        };
      }
    }

    return {
      topic: 'Production Software Module',
      domain: 'software engineering',
      heuristic: 'domain constraints and invariants',
      dataStructure: 'component architecture and data models',
      edgeCases: 'boundary conditions and error handling'
    };
  }

  function planExecutionTasks(goal, isFromChat = false, chatPayload = null) {
    const cleanGoal = (goal || '').trim();
    const normalizedGoal = normalizeGoalText(cleanGoal);
    const lowerGoal = cleanGoal.toLowerCase();
    const lowerNorm = normalizedGoal.toLowerCase();

    // Extract filename from goal or chat
    let resolvedTarget = (chatPayload && chatPayload.targetFile) ? chatPayload.targetFile : null;
    const fileMatches = cleanGoal.match(/([a-zA-Z0-9_\-\.\/]+\.[a-zA-Z0-9]+)/g);
    if (!resolvedTarget) {
      if (fileMatches && fileMatches.length > 0) {
        resolvedTarget = fileMatches[0];
      } else {
        const words = cleanGoal.split(/\s+/);
        for (const w of words) {
          const cw = w.replace(/[.,;:?!'"`()\[\]{}<>]/g, '');
          if (cw.includes('.') && !cw.startsWith('.')) {
            resolvedTarget = cw;
            break;
          }
        }
      }
    }
    if (!resolvedTarget) {
      resolvedTarget = getPrimaryRepoFile(cleanGoal) || 'test1.py';
    }

    // Extract folder if any
    let folderName = null;
    const folderMatch = cleanGoal.match(/\b(?:folder|directory|dir)\s+(?:named|called|titled|name(?:\s+is)?|as|of|:\s*)?['"`]?([a-zA-Z0-9_\-\.\/]+)['"`]?/i);
    if (folderMatch) {
      folderName = folderMatch[1].replace(/[.,;:?!'"`]/g, '');
    }

    // Check deletion
    const isDelete = (
      lowerGoal.includes('delete all') ||
      lowerGoal.includes('wipe all') ||
      lowerGoal.includes('remove all code') ||
      lowerGoal.includes('clear code') ||
      lowerGoal.includes('delete code') ||
      lowerGoal.includes('wipe code') ||
      lowerGoal.includes('truncate file') ||
      lowerGoal.includes('empty file')
    );

    // Check pure folder creation
    const isPureFolder = Boolean(folderName && !cleanGoal.match(/\b(?:file|script|module)\b/i) && (!fileMatches || fileMatches.length === 0));

    // Resolve topic information for domain-specific engineering steps
    const topicInfo = resolveTopicDomain(cleanGoal);

    // Check algorithm / sophisticated code synthesis (Max 5 steps)
    const isAlgorithmOrComplex = (
      lowerNorm.includes('astar') || lowerNorm.includes('a*') || lowerNorm.includes('a-star') ||
      lowerNorm.includes('dijkstra') || lowerNorm.includes('pathfinding') || lowerNorm.includes('algorithm') ||
      lowerNorm.includes('dsa') || lowerNorm.includes('heuristic') || lowerNorm.includes('binary search') ||
      lowerNorm.includes('sorting') || lowerNorm.includes('quicksort') || lowerNorm.includes('mergesort') ||
      lowerNorm.includes('dynamic programming') || lowerNorm.includes('graph') || lowerNorm.includes('tree') ||
      lowerNorm.includes('bst') || lowerNorm.includes('heap') || lowerNorm.includes('priority queue') ||
      lowerNorm.includes('trie') || lowerNorm.includes('backtracking') || lowerNorm.includes('solver') ||
      lowerNorm.includes('engine') || lowerNorm.includes('simulator') || lowerNorm.includes('compiler') ||
      lowerNorm.includes('parser') || lowerNorm.includes('lexer') || lowerNorm.includes('interpreter') ||
      lowerNorm.includes('pipeline') ||
      lowerNorm.includes('sophisticated') || lowerNorm.includes('large code') || lowerNorm.includes('complex') ||
      lowerNorm.includes('comprehensive') || lowerNorm.includes('full implementation') || lowerNorm.includes('from scratch') ||
      lowerNorm.includes('production-ready') || lowerNorm.includes('advanced')
    );

    const isCreationIntent = (
      lowerGoal.includes('create') ||
      lowerGoal.includes('new file') ||
      lowerGoal.includes('scaffold') ||
      lowerGoal.includes('write me') ||
      lowerGoal.includes('make file') ||
      lowerGoal.includes('author') ||
      lowerGoal.includes('generate file') ||
      lowerGoal.includes('add file')
    );

    // Check multi-file refactor strictly (Max 5 steps) - never hijack single-file creations
    const isHuge = !isCreationIntent && (
      (fileMatches && fileMatches.length >= 2) ||
      lowerGoal.includes('across files') ||
      lowerGoal.includes('multiple files') ||
      lowerGoal.includes('all files') ||
      lowerGoal.includes('all modules') ||
      lowerGoal.includes('architecture overhaul') ||
      lowerGoal.includes('overhaul') ||
      lowerGoal.includes('migrate')
    );

    let steps = [];

    // 1. Pure folder creation (2 Steps)
    if (isPureFolder) {
      const fDisplay = folderName || resolvedTarget;
      steps = [
        {
          id: 1,
          action: 'scan',
          title: `1. Verify workspace directory tree for '${fDisplay}'`,
          progress: `Verifying path for '${fDisplay}'...`,
          detail: `Target folder verified: '${fDisplay}' in workspace.`
        },
        {
          id: 2,
          action: 'prep',
          title: `2. Initialize directory '${fDisplay}' and register in workspace`,
          progress: `Creating directory '${fDisplay}'...`,
          detail: `Created folder '${fDisplay}' successfully.`
        }
      ];
    }
    // 2. Pre-generated Code or Diff from Chat (3 Steps - Instant Fast Path with AST Verification)
    else if (isFromChat && chatPayload && (chatPayload.pregeneratedDiff || chatPayload.pregeneratedCode)) {
      steps = [
        {
          id: 1,
          action: 'scan',
          title: `1. Correlate symbols and anchor lines in '${resolvedTarget}'`,
          progress: `Correlating symbols in '${resolvedTarget}'...`,
          detail: `Correlated symbols in '${resolvedTarget}'.`
        },
        {
          id: 2,
          action: 'synthesize',
          title: `2. Apply surgical changes and compute unified diff for '${resolvedTarget}'`,
          progress: `Applying verified changes from Chat for '${resolvedTarget}'...`,
          detail: `Synthesized surgical solution for '${resolvedTarget}'.`
        },
        {
          id: 3,
          action: 'verify',
          title: `3. Run AST syntax validation & verify zero regressions in '${resolvedTarget}'`,
          progress: `Verifying syntax and AST integrity...`,
          detail: `Syntax and AST verification passed. Changes ready for inspection in Diff tab.`
        }
      ];
    }
    // 3. Brand new file creation (3 Steps - Scan -> Synthesize -> Verify)
    else if (isCreationIntent && (!fileMatches || fileMatches.length <= 1)) {
      steps = [
        {
          id: 1,
          action: 'scan',
          title: `1. Analyze requirements & scaffold environment for '${resolvedTarget}'`,
          progress: `Analyzing requirements for '${resolvedTarget}'...`,
          detail: `Scaffold environment prepared for '${resolvedTarget}'.`
        },
        {
          id: 2,
          action: 'synthesize',
          title: isFromChat ? `2. Apply generated code changes for '${resolvedTarget}'` : `2. Synthesize complete, production-ready implementation for '${resolvedTarget}'`,
          progress: `Synthesizing '${resolvedTarget}' via ${currentModel}...`,
          detail: `Generated complete implementation for '${resolvedTarget}'.`
        },
        {
          id: 3,
          action: 'verify',
          title: `3. Run syntax validation, verify zero regressions & stage '${resolvedTarget}' diff`,
          progress: `Validating syntax and staging diff...`,
          detail: `Syntax and AST verified. Staged diff for '${resolvedTarget}' in Diff tab.`
        }
      ];
    }
    // 4. Code deletion / wipe / truncation (3 Steps)
    else if (isDelete) {
      steps = [
        {
          id: 1,
          action: 'scan',
          title: `1. Inspect '${resolvedTarget}' to isolate code boundaries for removal`,
          progress: `Inspecting '${resolvedTarget}' code boundaries...`,
          detail: `Identified deletion scope in '${resolvedTarget}'.`
        },
        {
          id: 2,
          action: 'delete',
          title: `2. Generate clean truncation diff to remove code from '${resolvedTarget}'`,
          progress: `Synthesizing truncation diff for '${resolvedTarget}'...`,
          detail: `Generated clean truncation diff for '${resolvedTarget}'.`
        },
        {
          id: 3,
          action: 'verify',
          title: `3. Validate clean file state, verify integrity & stage removal diff`,
          progress: `Validating clean state for '${resolvedTarget}'...`,
          detail: `Integrity verified. Staged deletion diff for '${resolvedTarget}'.`
        }
      ];
    }
    // 5. Algorithm / Complex Engine / Sophisticated Large Code (5 Steps - Max)
    else if (isAlgorithmOrComplex && !isDelete) {
      const topicTitle = topicInfo.topic;
      steps = [
        {
          id: 1,
          action: 'scan',
          title: `1. Analyze problem domain & formulate mathematical / heuristic models for '${topicTitle}' in '${resolvedTarget}'`,
          progress: `Formulating ${topicInfo.heuristic} and domain constraints for '${topicTitle}'...`,
          detail: `Domain constraints and heuristic models formulated for '${topicTitle}'.`
        },
        {
          id: 2,
          action: 'prep',
          title: `2. Structure ${topicInfo.dataStructure} for '${topicTitle}'`,
          progress: `Structuring ${topicInfo.dataStructure} for '${topicTitle}'...`,
          detail: `Structured ${topicInfo.dataStructure} and state invariants.`
        },
        {
          id: 3,
          action: 'synthesize',
          title: isFromChat ? `3. Apply generated ${topicTitle} implementation in '${resolvedTarget}'` : `3. Synthesize complete, production-ready implementation of '${topicTitle}' in '${resolvedTarget}'`,
          progress: `Synthesizing complete '${topicTitle}' implementation via ${currentModel}...`,
          detail: `Synthesized full production-ready implementation for '${topicTitle}' in '${resolvedTarget}'.`
        },
        {
          id: 4,
          action: 'validate',
          title: `4. Execute AST validation, benchmark boundary conditions & verify ${topicInfo.edgeCases}`,
          progress: `Validating AST integrity, checking ${topicInfo.edgeCases}...`,
          detail: `Syntax and AST verified. Handled ${topicInfo.edgeCases}.`
        },
        {
          id: 5,
          action: 'verify',
          title: `5. Verify computational complexity, check zero regressions & stage '${resolvedTarget}' diff`,
          progress: `Verifying complexity bounds and staging diff for '${resolvedTarget}'...`,
          detail: `Zero regressions verified. Staged complete '${topicTitle}' implementation in Diff tab.`
        }
      ];
    }
    // 6. Huge multi-file refactor (5 Steps - Max)
    else if (isHuge) {
      const targetsDesc = (fileMatches && fileMatches.length >= 2) ? fileMatches.slice(0, 3).join(', ') : resolvedTarget;
      steps = [
        {
          id: 1,
          action: 'scan',
          title: `1. Cross-index repository AST symbols & map call hierarchy dependencies`,
          progress: `Mapping cross-module call graph and dependencies across ${targetsDesc}...`,
          detail: `Indexed repository AST symbols across ${targetsDesc}.`
        },
        {
          id: 2,
          action: 'prep',
          title: `2. Compute change specifications & evaluate impact boundaries across modules`,
          progress: `Evaluating module impact boundaries...`,
          detail: `Impact boundaries and change specifications established.`
        },
        {
          id: 3,
          action: 'synthesize',
          title: `3. Synthesize coordinated surgical diffs across target modules`,
          progress: `Synthesizing coordinated diffs via ${currentModel}...`,
          detail: `Synthesized coordinated surgical diffs cleanly.`
        },
        {
          id: 4,
          action: 'validate',
          title: `4. Run multi-file AST validation & automated self-healing repair loop`,
          progress: `Executing AST validation and integrity checks...`,
          detail: `Multi-file AST validation and syntax verification passed.`
        },
        {
          id: 5,
          action: 'verify',
          title: `5. Verify cross-module consistency, check regression boundaries & stage diffs`,
          progress: `Verifying regression boundaries and staging diffs...`,
          detail: `Cross-module consistency verified. Staged changes ready in Diff tab.`
        }
      ];
    }
    // 7. Nested folder + file creation (4 Steps)
    else if (folderName && resolvedTarget) {
      steps = [
        {
          id: 1,
          action: 'scan',
          title: `1. Resolve destination directory and prepare folder structure for '${folderName}'`,
          progress: `Resolving destination directory '${folderName}'...`,
          detail: `Directory structure verified for '${folderName}'.`
        },
        {
          id: 2,
          action: 'prep',
          title: `2. Structure boilerplate schema & specifications for '${resolvedTarget}'`,
          progress: `Structuring schema for '${resolvedTarget}'...`,
          detail: `Path specifications established for '${resolvedTarget}'.`
        },
        {
          id: 3,
          action: 'synthesize',
          title: `3. Synthesize complete implementation for '${resolvedTarget}'`,
          progress: `Synthesizing '${resolvedTarget}' via ${currentModel}...`,
          detail: `Generated complete implementation for '${resolvedTarget}'.`
        },
        {
          id: 4,
          action: 'verify',
          title: `4. Verify path hierarchy, validate syntax & stage unified creation diff`,
          progress: `Verifying path hierarchy and staging diff...`,
          detail: `Path hierarchy verified. Staged creation diff in Diff tab.`
        }
      ];
    }
    // 8. Bug fix / Test repair / Debugging (4 Steps)
    else if (lowerGoal.includes('fix') || lowerGoal.includes('repair') || lowerGoal.includes('debug') || lowerGoal.includes('failing') || lowerGoal.includes('traceback') || lowerGoal.includes('assertion')) {
      steps = [
        {
          id: 1,
          action: 'scan',
          title: `1. Scan error trace & isolate failing assertions in '${resolvedTarget}'`,
          progress: `Scanning assertions in '${resolvedTarget}'...`,
          detail: `Isolated failure locus in '${resolvedTarget}'.`
        },
        {
          id: 2,
          action: 'prep',
          title: `2. Analyze code invariants and correlate call stack in '${resolvedTarget}'`,
          progress: `Analyzing code invariants in '${resolvedTarget}'...`,
          detail: `Invariants and call stack correlated.`
        },
        {
          id: 3,
          action: 'synthesize',
          title: `3. Synthesize targeted bugfix patch for '${resolvedTarget}'`,
          progress: `Synthesizing targeted fix via ${currentModel}...`,
          detail: `Synthesized targeted fix for '${resolvedTarget}'.`
        },
        {
          id: 4,
          action: 'verify',
          title: `4. Execute AST validation, verify zero regressions & stage fix`,
          progress: `Verifying syntax and regressions...`,
          detail: `Syntax and AST verification passed. Fix staged in Diff tab.`
        }
      ];
    }
    // 7. Standard modification (multi-file or single-file) (3 Steps)
    else {
      const allDetectedFiles = getAllFilesFromGoal(cleanGoal);
      const isMultiTarget = allDetectedFiles.length > 1;
      const targetLabel = isMultiTarget ? allDetectedFiles.join(', ') : `'${resolvedTarget}'`;

      steps = [
        {
          id: 1,
          action: 'scan',
          title: `1. Correlate symbols and anchor lines in ${targetLabel}`,
          progress: `Correlating symbols in ${targetLabel}...`,
          detail: `Correlated symbols in ${targetLabel}.`
        },
        {
          id: 2,
          action: 'synthesize',
          title: isFromChat
            ? `2. Apply surgical changes and compute unified diff for ${targetLabel}`
            : (isMultiTarget
                ? `2. Synthesize surgical code solution across ${targetLabel}`
                : `2. Synthesize surgical code solution and compute unified diff for ${targetLabel}`),
          progress: `Querying ${currentModel} for surgical code changes...`,
          detail: isMultiTarget
            ? `Synthesized surgical solution across ${targetLabel}.`
            : `Synthesized surgical solution for ${targetLabel}.`
        },
        {
          id: 3,
          action: 'verify',
          title: `3. Run AST syntax validation & verify zero regressions in ${targetLabel}`,
          progress: `Verifying syntax and AST integrity...`,
          detail: `Syntax and AST verification passed. Changes ready for inspection in Diff tab.`
        }
      ];
    }

    const allGoalFiles = getAllFilesFromGoal(cleanGoal);
    const resolvedTargetFiles = allGoalFiles.length > 0 ? allGoalFiles : (resolvedTarget ? [resolvedTarget] : []);

    return {
      goal: cleanGoal,
      target_file: resolvedTarget,
      target_files: resolvedTargetFiles,
      folder_name: folderName,
      total_steps: steps.length,
      steps: steps
    };
  }

  function renderDynamicSteps(steps) {
    if (!agentStepList) return;
    agentStepList.innerHTML = steps.map(step => `
      <div class="step-item step-queued" id="agent-step-${step.id}">
        <div class="step-bullet">[-]</div>
        <div><strong>${escapeHtml(step.title)}</strong></div>
      </div>
    `).join('');
  }

  async function runAgentTask(chatPayload = null) {
    const rawGoal = agentGoalInput ? agentGoalInput.value.trim() : '';
    const goal = rawGoal || (chatPayload && chatPayload.targetFile ? `Apply changes for ${chatPayload.targetFile}` : '');
    if (!goal) return;

    hideAmbiguityBar();
    isAgentRunning = true;
    if (btnRunAgent) btnRunAgent.style.display = 'none';
    if (btnStopAgent) btnStopAgent.style.display = 'flex';
    if (agentTerminalStatus) {
      agentTerminalStatus.textContent = 'RUNNING';
      agentTerminalStatus.style.color = '#4CAF50';
    }
    if (agentProgressTrack) agentProgressTrack.style.display = 'block';

    const isFromChat = Boolean(chatPayload && (chatPayload.pregeneratedDiff || chatPayload.pregeneratedCode));
    appendTerminalLog(`\n$ clay-copilot --execute "${goal}"`);
    appendTerminalLog(`[agent] Target Model: ${currentModel}`);

    // Generate dynamic execution plan
    const repoActiveFile = getActiveEditorFileInRepo();
    const repoActiveContent = getActiveEditorContentInRepo();
    const allGoalFiles = getAllFilesFromGoal(goal);
    const candidateTarget = (chatPayload && chatPayload.targetFile)
      ? chatPayload.targetFile
      : (allGoalFiles.length > 0 ? allGoalFiles[0] : (repoActiveFile ? repoActiveFile.replace(/\\/g, '/').split('/').pop() : undefined));

    // Fast-path resolution for Chat code: ensure pregeneratedDiff is computed and ready BEFORE running steps
    if (isFromChat && chatPayload && chatPayload.pregeneratedCode && !chatPayload.pregeneratedDiff) {
      const targetToUse = candidateTarget || 'app.py';
      appendTerminalLog(`[agent] Resolving diff for chat-generated code for '${targetToUse}'...`);
      try {
        const compRes = await fetch(`${BACKEND_URL}/api/agent/compute-diff`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            target_file: targetToUse,
            code_or_snippet: chatPayload.pregeneratedCode,
            workspace_path: currentRepoPath,
            active_file_content: repoActiveContent || undefined
          })
        });
        if (compRes.ok) {
          const compData = await compRes.json();
          if (compData && compData.diff) {
            chatPayload.pregeneratedDiff = compData.diff;
          }
        }
      } catch (err) {
        console.warn('[compute-diff failed for chat pregeneratedCode]', err);
      }

      // Fallback: If backend compute-diff did not produce a diff, build unified diff from code
      if (!chatPayload.pregeneratedDiff && chatPayload.pregeneratedCode) {
        const codeLines = chatPayload.pregeneratedCode.split('\n');
        if (repoActiveContent && repoActiveContent.trim()) {
          const oldLines = repoActiveContent.split('\n');
          chatPayload.pregeneratedDiff = `--- a/${targetToUse}\n+++ b/${targetToUse}\n@@ -1,${oldLines.length} +1,${codeLines.length} @@\n`
            + oldLines.map(l => `-${l}`).join('\n') + '\n'
            + codeLines.map(l => `+${l}`).join('\n');
        } else {
          chatPayload.pregeneratedDiff = `--- /dev/null\n+++ b/${targetToUse}\n@@ -0,0 +1,${codeLines.length} @@\n`
            + codeLines.map(l => `+${l}`).join('\n');
        }
      }
    }

    let plan = null;
    if (isFromChat && chatPayload && (chatPayload.pregeneratedDiff || chatPayload.pregeneratedCode)) {
      plan = planExecutionTasks(goal, isFromChat, chatPayload);
    } else {
      try {
        const planRes = await fetch(`${BACKEND_URL}/api/agent/plan`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            goal: goal,
            workspace_path: currentRepoPath,
            target_file: candidateTarget,
            target_files: allGoalFiles.length > 0 ? allGoalFiles : undefined,
            active_file: repoActiveFile || undefined,
            active_file_content: repoActiveContent || undefined,
            model_name: currentModel
          })
        });
        if (planRes.ok) {
          plan = await planRes.json();
        }
      } catch (e) {
        console.warn('[fetch dynamic plan error]', e);
      }
    }

    if (!plan || !plan.steps || plan.steps.length === 0) {
      plan = planExecutionTasks(goal, isFromChat, chatPayload);
    }

    const steps = plan.steps;
    const totalSteps = steps.length;
    renderDynamicSteps(steps);

    appendTerminalLog(`[plan] Dynamic plan: ${totalSteps} steps created for '${goal.slice(0, 45)}'`);

    let effectiveGoal = goal;
    let planTargetFiles = (plan && Array.isArray(plan.target_files) && plan.target_files.length > 0)
      ? [...plan.target_files]
      : (allGoalFiles.length > 0 ? [...allGoalFiles] : []);
    if (chatPayload && Array.isArray(chatPayload.targetFiles) && chatPayload.targetFiles.length > 0) {
      planTargetFiles = Array.from(new Set([...planTargetFiles, ...chatPayload.targetFiles]));
    }
    let confirmedTargetFile = (chatPayload && chatPayload.targetFile)
      ? chatPayload.targetFile
      : ((plan && plan.target_file) || (planTargetFiles.length > 0 ? planTargetFiles[0] : null) || (repoActiveFile ? repoActiveFile.replace(/\\/g, '/').split('/').pop() : null) || getPrimaryRepoFile(goal));

    try {
      for (let i = 0; i < steps.length; i++) {
        if (!isAgentRunning) return;
        const currentStep = steps[i];
        const stepNum = i + 1;
        const stepAction = currentStep.action;

        updateStepUI(currentStep.id, 'active');
        const progressPct = Math.round((stepNum / (totalSteps + 1)) * 90);
        setAgentProgress(progressPct, `Step ${stepNum} of ${totalSteps}: ${currentStep.progress || currentStep.title}...`);

        let stepRes = null;
        if (stepAction === 'synthesize' && isFromChat && chatPayload && (chatPayload.pregeneratedDiff || chatPayload.pregeneratedCode || (chatPayload.stagedFiles && chatPayload.stagedFiles.length > 0))) {
          // Fast-path: Directly use pre-generated code/diff from Chat without redundant LLM re-generation
          let finalChatDiff = chatPayload.pregeneratedDiff;
          if (!finalChatDiff && chatPayload.pregeneratedCode) {
            const rawLines = chatPayload.pregeneratedCode.split('\n');
            finalChatDiff = `--- /dev/null\n+++ b/${confirmedTargetFile}\n@@ -0,0 +1,${rawLines.length} @@\n` + rawLines.map(l => `+${l}`).join('\n');
            chatPayload.pregeneratedDiff = finalChatDiff;
          }
          const stagedFromChat = (chatPayload.stagedFiles && chatPayload.stagedFiles.length > 0) ? chatPayload.stagedFiles : null;
          stepRes = {
            step_id: currentStep.id,
            status: 'done',
            target_file: confirmedTargetFile,
            diff: finalChatDiff || (stagedFromChat ? stagedFromChat[0].diff : ''),
            staged_files: stagedFromChat,
            valid: true,
            log: (stagedFromChat && stagedFromChat.length > 1)
              ? `Staged exact changes from Chat across ${stagedFromChat.length} files (${stagedFromChat.map(s => s.file_path).join(', ')}).`
              : `Staged exact code from Chat directly for '${confirmedTargetFile}'.`
          };
          appendTerminalLog(`[fast-path] Staged exact changes from Chat ${stagedFromChat && stagedFromChat.length > 1 ? `across ${stagedFromChat.length} files` : `for '${confirmedTargetFile}'`} (LLM re-query bypassed).`);
        } else {
          // Execute step with effectiveGoal (the user prompt), confirmedTargetFile, stepAction, and all candidate target files
          stepRes = await executeStep(currentStep.id, effectiveGoal, confirmedTargetFile, stepAction, totalSteps, planTargetFiles);
        }
        if (!isAgentRunning) return;

        // Check ambiguity if step 1 returned multiple candidates
        if (stepAction === 'scan' && stepRes && stepRes.ambiguity && stepRes.ambiguity.candidates && stepRes.ambiguity.candidates.length > 1) {
          updateStepUI(currentStep.id, 'active', `Target Ambiguity: Multiple candidates found (${stepRes.ambiguity.candidates.join(', ')}). Awaiting selection...`);
          if (agentTerminalStatus) {
            agentTerminalStatus.textContent = 'PAUSED';
            agentTerminalStatus.style.color = '#E5A84B';
          }
          setAgentProgress(25, 'Paused: Select which target file to update above...');
          appendTerminalLog(`[ambiguity] Multiple candidates detected: ${stepRes.ambiguity.candidates.join(', ')}`);
          appendTerminalLog(`[agent] Execution paused. Waiting for user confirmation...`);
          renderAmbiguityBar(stepRes.ambiguity);

          const chosenFile = await new Promise(resolve => {
            pendingAmbiguityResolver = resolve;
          });

          if (!isAgentRunning || !chosenFile) {
            appendTerminalLog(`[agent] Execution stopped while awaiting target confirmation.`);
            return;
          }

          confirmedTargetFile = chosenFile;
          effectiveGoal = `${goal} in ${chosenFile}`;
          appendTerminalLog(`[ambiguity] Target confirmed by user: ${chosenFile}. Resuming execution...`);
          if (agentTerminalStatus) {
            agentTerminalStatus.textContent = 'RUNNING';
            agentTerminalStatus.style.color = '#4CAF50';
          }
          updateStepUI(currentStep.id, 'done', `Target confirmed: ${chosenFile}. ${stepRes.log || currentStep.detail}`);
          appendTerminalLog(`[step ${stepNum}/${totalSteps}] Target confirmed: ${chosenFile}.`);
        } else {
          hideAmbiguityBar();
          const detailMsg = stepRes.log || currentStep.detail;
          updateStepUI(currentStep.id, 'done', detailMsg);
          appendTerminalLog(`[step ${stepNum}/${totalSteps}] ${detailMsg}`);
        }

        // Propagate target files discovered in Step 1 (scan) forward to synthesis and validation steps
        if (stepAction === 'scan' && stepRes && Array.isArray(stepRes.target_files) && stepRes.target_files.length > 0) {
          planTargetFiles = Array.from(new Set([...planTargetFiles, ...stepRes.target_files]));
          if (!confirmedTargetFile && planTargetFiles.length > 0) {
            confirmedTargetFile = planTargetFiles[0];
          }
        }

        // Handle diff staging
        if (stepAction === 'synthesize' || stepAction === 'delete') {
          let diffToStage = stepRes.diff;
          const targetFilename = stepRes.target_file || confirmedTargetFile || (diffToStage ? extractDiffFilename(diffToStage) : '') || getPrimaryRepoFile(effectiveGoal);

          if (isFromChat && chatPayload && chatPayload.pregeneratedDiff) {
            diffToStage = chatPayload.pregeneratedDiff;
          }

          if (diffToStage || (stepRes.staged_files && stepRes.staged_files.length > 0)) {
            stageDiff(targetFilename, diffToStage, stepRes.staged_files);
            const diffType = stepAction === 'delete' ? 'deletion' : 'surgical';
            const targetDesc = (stepRes.staged_files && stepRes.staged_files.length > 1)
              ? `across ${stepRes.staged_files.length} files (${stepRes.staged_files.map(s => s.file_path).join(', ')})`
              : `for ${targetFilename}`;
            appendTerminalLog(`[diff] Staged ${diffType} diff ${targetDesc} in Diff Reviewer tab.`);
          }
        }
      }

      setAgentProgress(100, `All ${totalSteps} steps completed (100% complete)`);
      if (agentTerminalStatus) {
        agentTerminalStatus.textContent = 'COMPLETE';
        agentTerminalStatus.style.color = '#10B981';
      }
      appendTerminalLog(`[complete] All ${totalSteps} steps completed successfully! Inspect diff in Diff tab.`);
      if (stagedDiffData && stagedDiffData.diff) {
        showToast(`Diff staged for ${stagedDiffData.file} in Diff tab`, 'success');
      }
    } catch (err) {
      appendTerminalLog(`[error] Execution failed: ${err.message}`);
    } finally {
      stopAgentTask(false);
    }
  }

  function stopAgentTask(userTriggered = true) {
    isAgentRunning = false;
    if (pendingAmbiguityResolver) {
      const resolver = pendingAmbiguityResolver;
      pendingAmbiguityResolver = null;
      resolver(null);
    }
    hideAmbiguityBar();
    if (btnRunAgent) btnRunAgent.style.display = 'flex';
    if (btnStopAgent) btnStopAgent.style.display = 'none';

    // Instantly abort backend execution and terminal processes
    try {
      if (isVsCode) {
        vscode.postMessage({ command: 'abortAgentExecution' });
        vscode.postMessage({ command: 'abortTerminalCommand' });
      } else {
        const headers = SESSION_TOKEN ? { 'X-Clay-Auth-Token': SESSION_TOKEN } : {};
        fetch(`${BACKEND_URL}/api/agent/abort`, { method: 'POST', headers }).catch(() => {});
        fetch(`${BACKEND_URL}/api/agent/terminal/abort`, { method: 'POST', headers }).catch(() => {});
      }
    } catch (e) {}

    if (userTriggered) {
      if (agentTerminalStatus) {
        agentTerminalStatus.textContent = 'STOPPED';
        agentTerminalStatus.style.color = '#EF4444';
      }
      appendTerminalLog(`[clay] Execution stopped by user.`);
    }
  }

  async function executeStep(stepId, goal, targetFile = null, stepAction = null, totalSteps = 3, planTargetFiles = null) {
    const repoActiveFile = getActiveEditorFileInRepo();
    const repoActiveContent = getActiveEditorContentInRepo();
    const allGoalFiles = getAllFilesFromGoal(goal);

    // Combine candidate target files from planTargetFiles, allGoalFiles, targetFile
    const candidateFilesSet = new Set();
    if (Array.isArray(planTargetFiles) && planTargetFiles.length > 0) {
      planTargetFiles.forEach(f => candidateFilesSet.add(f));
    }
    allGoalFiles.forEach(f => candidateFilesSet.add(f));
    if (targetFile) candidateFilesSet.add(targetFile);

    let stepTargetFiles = Array.from(candidateFilesSet);
    if (stepTargetFiles.length === 0 && repoActiveFile) {
      const activeName = repoActiveFile.replace(/\\/g, '/').split('/').pop();
      if (activeName) stepTargetFiles.push(activeName);
    }

    const finalTarget = (stepTargetFiles.length > 0) ? stepTargetFiles[0] : (targetFile || getPrimaryRepoFile(goal));

    // Scope parameterization: The LLM decides scope naturally based on context and prompt instructions.
    // If multiple candidate files are identified or scope is unconstrained, enable multi-file modifications.
    const stepAllowMulti = stepTargetFiles.length > 1 || (stepTargetFiles.length === 0);

    // Step Action: Streaming synthesis route for real-time live terminal tokens
    if (stepAction === 'synthesize') {
      try {
        const streamRes = await fetch(`${BACKEND_URL}/api/agent/step/stream`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            goal,
            step_id: stepId,
            step_action: stepAction,
            total_steps: totalSteps,
            workspace_path: currentRepoPath,
            active_file: repoActiveFile || undefined,
            active_file_content: repoActiveContent || undefined,
            cursor_line: repoActiveFile ? activeEditorCursorLine : undefined,
            target_file: finalTarget,
            allow_multi_file: stepAllowMulti,
            target_files: stepTargetFiles
          })
        });

        if (streamRes.ok && streamRes.body) {
          const reader = streamRes.body.getReader();
          const decoder = new TextDecoder();
          let rawBuffer = '';
          let finalStepData = null;
          appendTerminalLog(`[agent] Generating code via ${currentModel} (streaming live)...`);

          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            rawBuffer += decoder.decode(value, { stream: true });
            const lines = rawBuffer.split('\n');
            rawBuffer = lines.pop() || '';

            for (const line of lines) {
              if (line.startsWith('data: ')) {
                const dataStr = line.replace('data: ', '').trim();
                if (dataStr === '[DONE]') continue;
                try {
                  const parsed = JSON.parse(dataStr);
                  if (parsed.token) {
                    appendTerminalLiveToken(parsed.token);
                  }
                  if (parsed.done) {
                    finalStepData = parsed;
                  }
                } catch { }
              }
            }
          }
          if (finalStepData) {
            appendTerminalLog('\n[agent] Code synthesis completed successfully.');
            return finalStepData;
          }
        }
      } catch (streamErr) {
        console.warn('[agent stream synthesis error, falling back to standard step]', streamErr);
      }
    }

    try {
      const res = await fetch(`${BACKEND_URL}/api/agent/step`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          goal,
          step_id: stepId,
          step_action: stepAction || undefined,
          total_steps: totalSteps,
          workspace_path: currentRepoPath,
          active_file: repoActiveFile || undefined,
          active_file_content: repoActiveContent || undefined,
          cursor_line: repoActiveFile ? activeEditorCursorLine : undefined,
          target_file: finalTarget,
          allow_multi_file: stepAllowMulti,
          target_files: stepTargetFiles
        })
      });
      if (res.ok) return await res.json();
    } catch (e) {
      if (isVsCode) {
        return new Promise((resolve) => {
          const handler = (event) => {
            if (event.data?.command === 'agentStepResult' && event.data?.step_id === stepId) {
              window.removeEventListener('message', handler);
              resolve(event.data.data || { log: `Step ${stepId} completed via extension host.` });
            }
          };
          window.addEventListener('message', handler);
          vscode.postMessage({
            command: 'executeAgentStep',
            step_id: stepId,
            step_action: stepAction || undefined,
            total_steps: totalSteps,
            goal,
            workspace_path: currentRepoPath,
            active_file: repoActiveFile || undefined,
            active_file_content: repoActiveContent || undefined,
            cursor_line: repoActiveFile ? activeEditorCursorLine : undefined,
            target_file: finalTarget,
            allow_multi_file: stepAllowMulti,
            target_files: stepTargetFiles
          });
          setTimeout(() => {
            window.removeEventListener('message', handler);
            resolve({ log: `Step ${stepId} processed (extension host fallback timeout reached after 90s).` });
          }, 90000);
        });
      }
    }
    return { log: `Step ${stepId} processed locally.` };
  }

  function renderInitialSteps(goal, isFromChat = false) {
    const plan = planExecutionTasks(goal, isFromChat);
    renderDynamicSteps(plan.steps);
  }

  function updateStepUI(stepId, status, detail) {
    const el = document.getElementById(`agent-step-${stepId}`);
    if (!el) return;
    el.className = `step-item step-${status}`;
    const bullet = el.querySelector('.step-bullet');
    if (bullet) {
      bullet.textContent = status === 'done' ? '[+]' : status === 'active' ? '[>]' : '[-]';
    }
    if (detail) {
      const container = el.querySelector('div:last-child');
      if (container) {
        let span = container.querySelector('span');
        if (!span) {
          container.appendChild(document.createElement('br'));
          span = document.createElement('span');
          span.style.color = '#8C847A';
          span.style.fontSize = '9.5px';
          span.style.fontFamily = 'var(--font-mono)';
          container.appendChild(span);
        }
        span.textContent = detail;
      }
    }
  }

  function setAgentProgress(pct, text) {
    if (agentProgressBar) agentProgressBar.style.width = `${pct}%`;
    if (agentProgressText) agentProgressText.textContent = text;
  }

  if (btnRunAgent) btnRunAgent.addEventListener('click', () => runAgentTask());
  if (btnStopAgent) btnStopAgent.addEventListener('click', () => stopAgentTask(true));

  function resizeAgentGoalInput() {
    if (!agentGoalInput) return;
    agentGoalInput.style.height = 'auto';
    const newHeight = Math.min(agentGoalInput.scrollHeight, 140);
    agentGoalInput.style.height = (newHeight > 20 ? newHeight : 20) + 'px';
    agentGoalInput.style.overflowY = agentGoalInput.scrollHeight > 140 ? 'auto' : 'hidden';
  }

  if (agentGoalInput) {
    try {
      const savedGoal = localStorage.getItem('clay_agent_goal');
      if (savedGoal) {
        agentGoalInput.value = savedGoal;
        setTimeout(resizeAgentGoalInput, 20);
      }
    } catch (e) { }

    agentGoalInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        runAgentTask();
      }
    });

    agentGoalInput.addEventListener('input', () => {
      resizeAgentGoalInput();
      try {
        localStorage.setItem('clay_agent_goal', agentGoalInput.value);
      } catch (e) { }
    });
  }

  function appendTerminalLog(text) {
    if (terminalStream) {
      terminalStream.textContent += text + '\n';
      terminalStream.scrollTop = terminalStream.scrollHeight;
      try {
        const lines = terminalStream.textContent.split('\n');
        if (lines.length > 250) {
          terminalStream.textContent = lines.slice(-250).join('\n');
        }
        localStorage.setItem('clay_terminal_logs', terminalStream.textContent);
      } catch (e) { }
    }
  }

  function appendTerminalLiveToken(token) {
    if (terminalStream && token) {
      terminalStream.textContent += token;
      terminalStream.scrollTop = terminalStream.scrollHeight;
      try {
        const lines = terminalStream.textContent.split('\n');
        if (lines.length > 250) {
          terminalStream.textContent = lines.slice(-250).join('\n');
        }
        localStorage.setItem('clay_terminal_logs', terminalStream.textContent);
      } catch (e) { }
    }
  }

  // Restore terminal logs if present
  if (terminalStream) {
    try {
      const savedLogs = localStorage.getItem('clay_terminal_logs');
      if (savedLogs && !terminalStream.textContent.trim()) {
        terminalStream.textContent = savedLogs;
        terminalStream.scrollTop = terminalStream.scrollHeight;
      }
    } catch (e) { }
  }

  // Interactive CLI Terminal in Agent Tab
  const btnClearTerminal = document.getElementById('btn-clear-terminal');
  const btnCopyTerminal = document.getElementById('btn-copy-terminal');
  const inputTerminalCmd = document.getElementById('input-terminal-cmd');
  const btnExecTerminalCmd = document.getElementById('btn-exec-terminal-cmd');
  const terminalPreviewSecurity = document.getElementById('terminal-preview-security');

  function updateCommandPreview(cmd = '') {
    const text = (cmd || (inputTerminalCmd ? inputTerminalCmd.value : '')).trim();
    const dangerousPatterns = [
      /\brm\s+-[rf]{1,2}\b/i,
      /\bmkfs\b/i,
      /\bdd\s+if=/i,
      /:(){ :|:& };:/,
      /\bformat\s+[a-z]:/i,
      /\bshutdown\b/i,
      /\breboot\b/i,
      /\bdrop\s+database\b/i
    ];
    const isDangerous = dangerousPatterns.some((pattern) => pattern.test(text));

    if (terminalPreviewSecurity) {
      if (currentSecurityMode === 'sandboxed' && isDangerous) {
        terminalPreviewSecurity.textContent = '[BLOCKED]';
        terminalPreviewSecurity.classList.add('blocked');
      } else {
        terminalPreviewSecurity.textContent = `[${currentSecurityMode.toUpperCase()}]`;
        terminalPreviewSecurity.classList.remove('blocked');
      }
    }
  }

  if (inputTerminalCmd) {
    inputTerminalCmd.addEventListener('input', () => {
      updateCommandPreview(inputTerminalCmd.value);
    });
  }

  // Initial render of preview indicator
  updateCommandPreview();

  if (btnClearTerminal) {
    btnClearTerminal.addEventListener('click', () => {
      if (terminalStream) {
        terminalStream.textContent = '$ console cleared\n';
      }
      try { localStorage.removeItem('clay_terminal_logs'); } catch (e) { }
    });
  }

  if (btnCopyTerminal) {
    btnCopyTerminal.addEventListener('click', () => {
      if (terminalStream) {
        navigator.clipboard.writeText(terminalStream.textContent);
        showToast('Terminal output copied', 'success');
      }
    });
  }

  if (terminalStream) {
    // Allow user to select specific lines with cursor and copy via Ctrl+C / Cmd+C
    terminalStream.addEventListener('copy', () => {
      const selected = window.getSelection()?.toString();
      if (selected && selected.trim()) {
        showToast('Selection copied to clipboard', 'success');
      }
    });

    // Right-click copy: in VS Code webviews where browser context menu is suppressed,
    // right-clicking any highlighted terminal selection instantly copies it to clipboard
    terminalStream.addEventListener('contextmenu', (e) => {
      const selected = window.getSelection()?.toString();
      if (selected && selected.trim()) {
        e.preventDefault();
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(selected).then(() => {
            showToast('Selection copied to clipboard', 'success');
          }).catch(() => {
            fallbackCopyText(selected, () => showToast('Selection copied to clipboard', 'success'));
          });
        } else {
          fallbackCopyText(selected, () => showToast('Selection copied to clipboard', 'success'));
        }
      }
    });
  }

  async function executeCliCommand(cmd) {
    if (!cmd || !cmd.trim()) return;
    const command = cmd.trim();

    if (agentTerminalStatus) {
      agentTerminalStatus.textContent = 'RUNNING';
      agentTerminalStatus.style.color = '#F59E0B';
    }
    appendTerminalLog(`\n[${currentSecurityMode.toUpperCase()}] $ ${command}`);

    const selectedPath = (inputRepoPath && inputRepoPath.value.trim() && inputRepoPath.value.trim() !== '.')
      ? inputRepoPath.value.trim()
      : (currentRepoPath && currentRepoPath !== '.' ? currentRepoPath : '');

    // If inside VS Code extension webview, route through extension host
    if (isVsCode) {
      vscode.postMessage({
        command: 'executeTerminalCommand',
        commandText: command,
        workspace_path: selectedPath,
        security_mode: currentSecurityMode
      });
      return;
    }

    try {
      const res = await fetch(`${BACKEND_URL}/api/agent/terminal/execute`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          command,
          workspace_path: selectedPath,
          security_mode: currentSecurityMode
        })
      });
      const data = await res.json();
      if (data.stdout && data.stdout.trim()) {
        appendTerminalLog(data.stdout.trimEnd());
      }
      if (data.stderr && data.stderr.trim()) {
        appendTerminalLog(`[stderr] ${data.stderr.trimEnd()}`);
      }
      if (data.exit_code !== 0 && (!data.stdout || !data.stdout.trim()) && (!data.stderr || !data.stderr.trim())) {
        appendTerminalLog(`[error] Command exited with code ${data.exit_code} (no output)`);
      }
      appendTerminalLog(`[${data.os || detectedSystem.os}] process exited with code ${data.exit_code}`);
      if (agentTerminalStatus) {
        agentTerminalStatus.textContent = data.exit_code === 0 ? 'READY' : 'ERROR';
        agentTerminalStatus.style.color = data.exit_code === 0 ? '#10B981' : '#EF4444';
      }
    } catch (err) {
      appendTerminalLog(`[execution error] ${err.message}`);
      if (agentTerminalStatus) {
        agentTerminalStatus.textContent = 'ERROR';
        agentTerminalStatus.style.color = '#EF4444';
      }
    }
  }

  if (btnExecTerminalCmd && inputTerminalCmd) {
    btnExecTerminalCmd.addEventListener('click', () => {
      const val = inputTerminalCmd.value;
      if (val && val.trim()) {
        executeCliCommand(val);
        inputTerminalCmd.value = '';
        updateCommandPreview('');
      }
    });
    inputTerminalCmd.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        const val = inputTerminalCmd.value;
        if (val && val.trim()) {
          executeCliCommand(val);
          inputTerminalCmd.value = '';
          updateCommandPreview('');
        }
      }
    });
  }

  document.querySelectorAll('.cli-chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      const cmd = chip.getAttribute('data-cmd');
      if (cmd) {
        if (inputTerminalCmd) {
          inputTerminalCmd.value = cmd;
          updateCommandPreview(cmd);
        }
        executeCliCommand(cmd);
      }
    });
  });

  // ==========================================
  // 5. Diff Reviewer: Surgical Inspection & Patching
  // ==========================================
  const diffStagedContainer = document.getElementById('diff-staged-container');
  const diffActionBar = document.getElementById('diff-action-bar');
  const diffSummaryLabel = document.getElementById('diff-summary-label');
  const diffBadgeAdd = document.getElementById('diff-badge-add');
  const diffBadgeDel = document.getElementById('diff-badge-del');
  const btnAcceptDiff = document.getElementById('btn-accept-diff');
  const btnRejectDiff = document.getElementById('btn-reject-diff');
  const btnDiffViewMode = document.getElementById('btn-diff-view-mode');
  const diffModeLabel = document.getElementById('diff-mode-label');
  const diffFileSelector = document.getElementById('diff-file-selector');
  const btnAcceptAllDiff = document.getElementById('btn-accept-all-diff');

  // Domain 4: 3-Way Merge Conflict Guard Elements
  const diffConflictPill = document.getElementById('diff-conflict-pill');
  const guardPillTitle = document.getElementById('guard-pill-title');
  const btnGuardDismiss = document.getElementById('btn-guard-dismiss');
  const headerConflictBanner = document.getElementById('header-conflict-banner');
  const headerConflictTitle = document.getElementById('header-conflict-title');
  const btnHeaderDismissConflict = document.getElementById('btn-header-dismiss-conflict');

  let diffViewMode = 'unified';
  let stagedMultiFiles = [];

  function computeFastHash(str) {
    if (!str || typeof str !== 'string') return '0';
    const normalized = str.replace(/\r\n/g, '\n').replace(/\r/g, '\n').trimEnd();
    let hash = 2166136261;
    for (let i = 0; i < normalized.length; i++) {
      hash ^= normalized.charCodeAt(i);
      hash = Math.imul(hash, 16777619);
    }
    return (hash >>> 0).toString(16);
  }

  function ensureConflictGuardPill() {
    let pill = document.getElementById('diff-conflict-pill');
    if (!pill) {
      const viewDiff = document.getElementById('view-diff');
      const stagedContainer = document.getElementById('diff-staged-container');
      if (viewDiff) {
        pill = document.createElement('div');
        pill.className = 'diff-conflict-guard-pill';
        pill.id = 'diff-conflict-pill';
        pill.style.display = 'none';
        pill.innerHTML = `
          <div class="guard-pill-badge">
            <span class="guard-pill-title" id="guard-pill-title">WARNING: 3-WAY MERGE CONFLICT DETECTED</span>
          </div>
          <div class="guard-pill-actions">
            <button type="button" class="btn-guard-dismiss" id="btn-guard-dismiss" title="Dismiss warning">
              Dismiss
            </button>
          </div>
        `;
        if (stagedContainer) {
          viewDiff.insertBefore(pill, stagedContainer);
        } else {
          viewDiff.appendChild(pill);
        }
        pill.querySelector('#btn-guard-dismiss')?.addEventListener('click', () => {
          if (stagedDiffData) stagedDiffData.isDismissed = true;
          hideConflictGuardPill();
          showToast('Conflict warning dismissed. Review diff before applying.', 'info', 2000);
        });
      }
    }
    return pill;
  }

  function showConflictGuardPill(state, titleText) {
    const isDiffActive = Boolean(views.diff && views.diff.classList.contains('active-view'));
    const pill = ensureConflictGuardPill();
    if (pill) {
      pill.style.display = 'flex';
      pill.className = 'diff-conflict-guard-pill';
      if (state === 'rebased') {
        pill.classList.add('status-rebased');
      } else if (state === 'conflict') {
        pill.classList.add('status-conflict');
      }
      const titleEl = pill.querySelector('#guard-pill-title') || document.getElementById('guard-pill-title');
      if (titleEl && titleText) {
        titleEl.textContent = titleText;
      }
    }

    const headerBanner = document.getElementById('header-conflict-banner');
    if (headerBanner) {
      // Only show global header banner when the user is NOT on the Diff tab.
      // In the Diff tab, the in-view diff pill is already displayed, preventing duplicate banners.
      if (state === 'conflict' && !isDiffActive) {
        headerBanner.style.display = 'flex';
        const hTitle = document.getElementById('header-conflict-title');
        if (hTitle && titleText) {
          hTitle.textContent = titleText;
        }
      } else {
        headerBanner.style.display = 'none';
      }
    }
  }

  function hideConflictGuardPill() {
    const pill = document.getElementById('diff-conflict-pill');
    if (pill) {
      pill.style.display = 'none';
      pill.className = 'diff-conflict-guard-pill';
    }
    const headerBanner = document.getElementById('header-conflict-banner');
    if (headerBanner) {
      headerBanner.style.display = 'none';
    }
  }

  function hasConflictMarkers(text) {
    if (!text || typeof text !== 'string') return false;
    return text.includes('<<<<<<<') || text.includes('=======') || text.includes('>>>>>>>') || text.includes('(CONFLICT)');
  }

  function isSameFileReference(f1, f2) {
    if (!f1 || !f2) return false;
    const clean1 = f1.replace(/\\/g, '/').toLowerCase();
    const clean2 = f2.replace(/\\/g, '/').toLowerCase();
    if (clean1 === clean2) return true;
    const b1 = clean1.split('/').pop();
    const b2 = clean2.split('/').pop();
    return b1 === b2;
  }

  function checkDiffDivergence(currentBufferContent) {
    if (!stagedDiffData || !stagedDiffData.diff) return;
    if (stagedDiffData.isDismissed) return;

    const currentHash = computeFastHash(currentBufferContent);
    stagedDiffData.currentBufferContent = currentBufferContent;

    if (hasConflictMarkers(stagedDiffData.diff)) {
      const matchCount = (stagedDiffData.diff.match(/<<<<<<</g) || []).length || (stagedDiffData.conflicts ? stagedDiffData.conflicts.length : 1);
      showConflictGuardPill('conflict', `WARNING: 3-WAY MERGE CONFLICT DETECTED (${matchCount})`);
      const countEl = document.getElementById('conflict-count');
      if (countEl) countEl.textContent = matchCount;
      const hCount = document.getElementById('header-conflict-count');
      if (hCount) hCount.textContent = matchCount;
      return;
    }

    if (!stagedDiffData.baseContent && currentBufferContent) {
      stagedDiffData.baseContent = currentBufferContent;
      stagedDiffData.baseHash = currentHash;
      stagedDiffData.isDiverged = false;
      hideConflictGuardPill();
      return;
    }

    if (stagedDiffData.baseHash && currentHash !== stagedDiffData.baseHash) {
      stagedDiffData.isDiverged = true;
      showConflictGuardPill('conflict', 'WARNING: 3-WAY MERGE CONFLICT (BUFFER DIVERGED)');
      const countEl = document.getElementById('conflict-count');
      if (countEl) countEl.textContent = '1';
      const hCount = document.getElementById('header-conflict-count');
      if (hCount) hCount.textContent = '1';
    } else {
      stagedDiffData.isDiverged = false;
      hideConflictGuardPill();
    }
  }

  async function triggerAutoRebase(resolution = null) {
    if (!stagedDiffData) return;
    const currentContent = stagedDiffData.currentBufferContent || activeEditorContent || '';
    const baseContent = stagedDiffData.baseContent || '';
    const targetFilePath = stagedDiffData.fullPath || stagedDiffData.file;
    const diffToSend = stagedDiffData.cleanDiff || stagedDiffData.diff;

    showToast('Rebasing diff against current editor buffer...', 'info', 2000);

    if (isVsCode) {
      vscode.postMessage({
        command: 'rebaseDiff',
        file: targetFilePath,
        baseContent: baseContent,
        diff: diffToSend,
        currentContent: currentContent,
        workspacePath: currentRepoPath,
        forceResolution: resolution
      });
    } else {
      try {
        const res = await fetch(`${BACKEND_URL}/api/agent/rebase-diff`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            file_path: targetFilePath,
            base_content: baseContent,
            diff: diffToSend,
            current_content: currentContent,
            workspace_path: currentRepoPath,
            force_resolution: resolution
          })
        });
        const data = await res.json();
        handleRebaseResult(data);
      } catch (err) {
        showToast(`Rebase failed: ${err.message}`, 'error');
      }
    }
  }

  function handleRebaseResult(res) {
    if (!res || !stagedDiffData) return;
    if (res.status === 'clean') {
      showToast('Diff is already aligned with current buffer', 'info');
      stagedDiffData.isDiverged = false;
      hideConflictGuardPill();
    } else if (res.status === 'already_applied') {
      showToast('All diff changes are already present in editor', 'success');
      clearStagedDiff();
    } else if (res.status === 'rebased') {
      const updatedBase = stagedDiffData.currentBufferContent || activeEditorContent || '';
      stagedDiffData.diff = res.diff;
      stagedDiffData.cleanDiff = res.diff;
      stagedDiffData.baseContent = updatedBase;
      stagedDiffData.baseHash = computeFastHash(updatedBase);
      stagedDiffData.isDiverged = false;
      stagedDiffData.conflicts = [];
      stageDiff(stagedDiffData.file, res.diff, stagedMultiFiles, false, updatedBase);
      showConflictGuardPill('rebased', 'Rebased cleanly to buffer');
      showToast('Diff rebased cleanly to match editor changes', 'success');
      setTimeout(() => {
        if (stagedDiffData && !stagedDiffData.isDiverged) {
          hideConflictGuardPill();
        }
      }, 3500);
    } else if (res.status === 'conflict') {
      const conflictCount = res.conflicts ? res.conflicts.length : 1;
      stagedDiffData.conflicts = res.conflicts || [];
      showConflictGuardPill('conflict', `${conflictCount} collision(s) detected`);
      const conflictCountSpan = document.getElementById('conflict-count');
      if (conflictCountSpan) conflictCountSpan.textContent = conflictCount;
      const hCount = document.getElementById('header-conflict-count');
      if (hCount) hCount.textContent = conflictCount;
      if (res.diff) {
        stageDiff(stagedDiffData.file, res.diff, stagedMultiFiles, false, stagedDiffData.baseContent);
      }
      showToast(`Warning: ${conflictCount} overlapping edit(s) collide with diff`, 'warning', 4000);
    } else {
      showToast(res.message || 'Rebase failed', 'error');
    }
  }



  if (btnGuardDismiss) {
    btnGuardDismiss.addEventListener('click', () => {
      if (stagedDiffData) stagedDiffData.isDismissed = true;
      hideConflictGuardPill();
      showToast('Conflict warning dismissed. Diff can be applied as-is.', 'info', 2000);
    });
  }

  if (btnHeaderDismissConflict) {
    btnHeaderDismissConflict.addEventListener('click', (e) => {
      e.stopPropagation();
      if (stagedDiffData) stagedDiffData.isDismissed = true;
      hideConflictGuardPill();
      showToast('Conflict warning dismissed. Review diff before applying.', 'info', 2000);
    });
  }

  const headerConflictBadge = headerConflictBanner?.querySelector('.header-conflict-badge');
  if (headerConflictBadge) {
    headerConflictBadge.style.cursor = 'pointer';
    headerConflictBadge.title = 'Click to review diff';
    headerConflictBadge.addEventListener('click', () => {
      switchTab('diff');
    });
  }

  if (btnDiffViewMode) {
    btnDiffViewMode.addEventListener('click', () => {
      diffViewMode = diffViewMode === 'unified' ? 'split' : 'unified';
      if (diffModeLabel) diffModeLabel.textContent = diffViewMode === 'unified' ? 'Unified' : 'Split';
      if (stagedDiffData) {
        stageDiff(stagedDiffData.file, stagedDiffData.diff, stagedMultiFiles);
      }
    });
  }

  async function discardHunk(hunkIdx) {
    if (!stagedDiffData) return;
    try {
      const res = await fetch(`${BACKEND_URL}/api/agent/discard-hunk`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          file_path: stagedDiffData.file,
          diff: stagedDiffData.diff,
          hunk_index: hunkIdx,
          workspace_path: currentRepoPath
        })
      });
      const data = await res.json();
      if (res.ok && data.status === 'success') {
        if (!data.diff || !data.diff.trim()) {
          showToast('All changes in hunk discarded. No remaining diff.', 'info');
          clearStagedDiff();
        } else {
          showToast(`Discarded hunk #${hunkIdx + 1}`, 'info');
          stageDiff(stagedDiffData.file, data.diff, stagedMultiFiles);
        }
      } else {
        showToast(data.detail || 'Failed to discard hunk', 'error');
      }
    } catch (err) {
      showToast(`Discard failed: ${err.message}`, 'error');
    }
  }

  function stageDiff(filename, diffText, stagedFiles = null, forceEditedState = null, baseContent = null) {
    let activeFile = filename;
    let activeDiff = diffText;

    if (stagedFiles && stagedFiles.length > 0) {
      let matched = stagedFiles.find(sf => isSameFileReference(sf.file_path || sf.file, filename));
      if (!matched) {
        matched = stagedFiles[0];
        activeFile = matched.file_path || matched.file || filename;
      }
      if (matched && matched.diff) {
        activeDiff = matched.diff;
      }
    } else if (stagedMultiFiles && stagedMultiFiles.length > 0) {
      let matched = stagedMultiFiles.find(sf => isSameFileReference(sf.file_path, filename));
      if (matched && matched.diff) {
        activeDiff = matched.diff;
      }
    }

    filename = activeFile;
    diffText = activeDiff;

    if (forceEditedState !== null) {
      stagedDiffIsEdited = forceEditedState;
    } else if (!stagedDiffData || stagedDiffData.file !== filename) {
      stagedDiffIsEdited = false;
    }

    let resolvedBaseContent = '';
    let resolvedFullPath = filename;
    if (typeof baseContent === 'string' && baseContent.length > 0) {
      resolvedBaseContent = baseContent;
    } else if (activeEditorContent !== undefined && activeEditorFile && isSameFileReference(activeEditorFile, filename)) {
      resolvedBaseContent = activeEditorContent;
      resolvedFullPath = activeEditorFile;
    } else if (stagedDiffData && isSameFileReference(stagedDiffData.file, filename) && stagedDiffData.baseContent) {
      resolvedBaseContent = stagedDiffData.baseContent;
      resolvedFullPath = stagedDiffData.fullPath || filename;
    }

    let cleanDiff = diffText;
    if (diffText && diffText.includes('<<<<<<< YOUR CHANGES')) {
      cleanDiff = (stagedDiffData && stagedDiffData.cleanDiff) ? stagedDiffData.cleanDiff : diffText;
    } else if (stagedDiffData && isSameFileReference(stagedDiffData.file, filename) && stagedDiffData.cleanDiff && diffText === stagedDiffData.diff) {
      cleanDiff = stagedDiffData.cleanDiff;
    }

    stagedDiffData = {
      file: filename,
      fullPath: resolvedFullPath,
      diff: diffText,
      cleanDiff: cleanDiff,
      baseContent: resolvedBaseContent,
      baseHash: computeFastHash(resolvedBaseContent),
      isDiverged: false,
      isDismissed: false,
      currentBufferContent: resolvedBaseContent,
      conflicts: (stagedDiffData && isSameFileReference(stagedDiffData.file, filename)) ? stagedDiffData.conflicts || [] : []
    };

    const hasConflicts = Boolean(
      hasConflictMarkers(diffText) ||
      (stagedDiffData && stagedDiffData.conflicts && stagedDiffData.conflicts.length > 0)
    );

    if (hasConflicts) {
      const matchCount = (diffText.match(/<<<<<<</g) || []).length || (stagedDiffData && stagedDiffData.conflicts ? stagedDiffData.conflicts.length : 1);
      showConflictGuardPill('conflict', `WARNING: 3-WAY MERGE CONFLICT DETECTED (${matchCount})`);
      const countEl = document.getElementById('conflict-count');
      if (countEl) countEl.textContent = matchCount;
    } else if (resolvedBaseContent && activeEditorContent && isSameFileReference(filename, activeEditorFile) && computeFastHash(resolvedBaseContent) !== computeFastHash(activeEditorContent)) {
      stagedDiffData.isDiverged = true;
      showConflictGuardPill('conflict', 'WARNING: 3-WAY MERGE CONFLICT (BUFFER DIVERGED)');
      const countEl = document.getElementById('conflict-count');
      if (countEl) countEl.textContent = '1';
    } else {
      hideConflictGuardPill();
    }

    const lines = diffText.split('\n');
    let adds = 0;
    let dels = 0;
    lines.forEach((l) => {
      if (l.startsWith('+') && !l.startsWith('+++')) adds++;
      if (l.startsWith('-') && !l.startsWith('---')) dels++;
    });

    if (stagedFiles && stagedFiles.length > 0) {
      stagedMultiFiles = stagedFiles.map(sf => {
        const fp = sf.file_path || sf.file || filename;
        const d = sf.diff !== undefined ? sf.diff : (isSameFileReference(fp, filename) ? diffText : '');
        const lAdds = sf.adds !== undefined ? sf.adds : (d ? d.split('\n').filter(l => l.startsWith('+') && !l.startsWith('+++')).length : 0);
        const lDels = sf.dels !== undefined ? sf.dels : (d ? d.split('\n').filter(l => l.startsWith('-') && !l.startsWith('---')).length : 0);
        const existing = stagedMultiFiles ? stagedMultiFiles.find(item => isSameFileReference(item.file_path, fp)) : null;
        return {
          file_path: fp,
          diff: d,
          applied: existing ? existing.applied : Boolean(sf.applied),
          adds: lAdds,
          dels: lDels,
          error: existing ? existing.error : (sf.error || null)
        };
      });
    } else if (stagedMultiFiles && stagedMultiFiles.length > 0) {
      const existing = stagedMultiFiles.find(item => isSameFileReference(item.file_path, filename));
      if (existing) {
        existing.diff = diffText;
        existing.adds = adds;
        existing.dels = dels;
      }
    } else {
      stagedMultiFiles = [{ file_path: filename, diff: diffText, applied: false, adds: adds, dels: dels, error: null }];
    }

    try {
      localStorage.setItem('clay_staged_diff', JSON.stringify({
        file: filename,
        diff: diffText,
        stagedFiles: stagedMultiFiles
      }));
    } catch (e) { }

    const unappliedList = stagedMultiFiles.filter(f => !f.applied && f.diff);
    if (diffSummaryLabel) {
      diffSummaryLabel.textContent = stagedMultiFiles.length > 1
        ? `Reviewing (${unappliedList.length}/${stagedMultiFiles.length} pending): ${filename}`
        : `Reviewing: ${filename}`;
    }
    if (diffBadgeAdd) diffBadgeAdd.textContent = `+${adds}`;
    if (diffBadgeDel) diffBadgeDel.textContent = `-${dels}`;
    if (diffActionBar) diffActionBar.style.display = 'flex';

    // Update Apply All and Accept Patch buttons
    if (btnAcceptDiff) {
      const activeBase = filename ? filename.replace(/\\/g, '/').split('/').pop() : 'Patch';
      if (stagedMultiFiles.length > 1 && unappliedList.length > 1) {
        btnAcceptDiff.textContent = `Apply ${activeBase}`;
      } else {
        btnAcceptDiff.textContent = 'Accept & Apply Patch';
      }
    }

    if (btnAcceptAllDiff) {
      if (stagedMultiFiles.length > 1 && unappliedList.length > 0) {
        btnAcceptAllDiff.style.display = 'inline-flex';
        btnAcceptAllDiff.textContent = `Apply All (${unappliedList.length})`;
      } else {
        btnAcceptAllDiff.style.display = 'none';
      }
    }

    // Multi-file selector chips with status badges and single discard
    if (diffFileSelector) {
      if (stagedMultiFiles.length > 1) {
        diffFileSelector.style.display = 'flex';
        diffFileSelector.innerHTML = stagedMultiFiles.map((sf, idx) => {
          const isActive = isSameFileReference(sf.file_path, filename);
          const isApplied = Boolean(sf.applied);
          const hasError = Boolean(sf.error);
          const statusIcon = isApplied ? '✓' : (hasError ? '✖' : '●');
          const statusClass = isApplied ? 'is-done' : (hasError ? 'is-err' : 'is-pending');
          const fName = sf.file_path ? sf.file_path.replace(/\\/g, '/').split('/').pop() : 'file';
          return `
            <div class="diff-file-chip ${isActive ? 'active' : ''} ${isApplied ? 'is-applied' : ''}" data-file-idx="${idx}" title="${escapeHtml(sf.file_path)}">
              <span class="diff-chip-status ${statusClass}">${statusIcon}</span>
              <span class="diff-chip-name">${escapeHtml(fName)}</span>
              <span class="diff-chip-metrics"><span class="chip-add">+${sf.adds || 0}</span> <span class="chip-del">-${sf.dels || 0}</span></span>
              <button type="button" class="btn-chip-discard" data-discard-idx="${idx}" title="Discard changes for this file">✕</button>
            </div>
          `;
        }).join('');

        diffFileSelector.querySelectorAll('.diff-file-chip').forEach(chip => {
          chip.addEventListener('click', (e) => {
            if (e.target.closest('.btn-chip-discard')) return;
            const idx = parseInt(chip.getAttribute('data-file-idx'), 10);
            const chosen = stagedMultiFiles[idx];
            if (chosen) {
              stageDiff(chosen.file_path, chosen.diff, stagedMultiFiles);
            }
          });
        });

        diffFileSelector.querySelectorAll('.btn-chip-discard').forEach(btn => {
          btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const dIdx = parseInt(btn.getAttribute('data-discard-idx'), 10);
            if (dIdx >= 0 && dIdx < stagedMultiFiles.length) {
              const discarded = stagedMultiFiles.splice(dIdx, 1)[0];
              showToast(`Discarded staged changes for ${discarded.file_path}`, 'info', 2000);
              if (stagedMultiFiles.length === 0) {
                clearStagedDiff();
              } else {
                const nextFile = stagedMultiFiles[Math.min(dIdx, stagedMultiFiles.length - 1)];
                stageDiff(nextFile.file_path, nextFile.diff, stagedMultiFiles);
              }
            }
          });
        });
      } else {
        diffFileSelector.style.display = 'none';
      }
    }

    const diffTabBtn = document.querySelector('.nav-tab[data-target="diff"]');
    if (diffTabBtn && !diffTabBtn.querySelector('.diff-staged-dot')) {
      const dot = document.createElement('span');
      dot.className = 'diff-staged-dot';
      dot.style.cssText = 'display:inline-block;width:6px;height:6px;border-radius:50%;background:#F97316;margin-left:4px;vertical-align:middle;';
      diffTabBtn.appendChild(dot);
    }

    if (diffStagedContainer) {
      diffStagedContainer.innerHTML = `
        <div class="diff-box" style="margin-top: 0;">
          <div class="diff-box-header">
            <div style="display:flex;align-items:center;gap:6px;">
              <span>${escapeHtml(filename)}</span>
              <span class="diff-edited-badge" id="diff-edited-badge" style="display:${stagedDiffIsEdited ? 'inline-block' : 'none'};">EDITED</span>
            </div>
            <div class="diff-actions">
              <button class="btn-diff-action" id="btn-copy-staged-diff">Copy</button>
            </div>
          </div>
          <div class="diff-content"><div class="diff-lines-wrapper">${renderDiffLines(diffText)}</div></div>
        </div>
      `;

      const copyBtn = document.getElementById('btn-copy-staged-diff');
      if (copyBtn) {
        copyBtn.addEventListener('click', () => {
          navigator.clipboard.writeText(stagedDiffData ? stagedDiffData.diff : diffText);
          copyBtn.textContent = 'Copied!';
          setTimeout(() => (copyBtn.textContent = 'Copy'), 1500);
        });
      }

      // Wire per-hunk discard buttons
      diffStagedContainer.querySelectorAll('.btn-hunk-discard').forEach(btn => {
        btn.addEventListener('click', async (e) => {
          e.stopPropagation();
          const hIdx = parseInt(btn.getAttribute('data-hunk-idx'), 10);
          await discardHunk(hIdx);
        });
      });

      // Wire inline editable diff lines (+ additions)
      diffStagedContainer.querySelectorAll('.editable-diff-line').forEach(el => {
        el.addEventListener('keydown', (e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            el.blur();
          } else if (e.key === 'Escape') {
            e.preventDefault();
            el.blur();
          }
        });

        el.addEventListener('paste', (e) => {
          e.preventDefault();
          const text = (e.clipboardData || window.clipboardData).getData('text/plain');
          const cleanText = text.replace(/[\r\n]+/g, ' ');
          document.execCommand('insertText', false, cleanText);
        });

        el.addEventListener('input', () => {
          const rawIdx = parseInt(el.getAttribute('data-raw-idx'), 10);
          if (isNaN(rawIdx) || !stagedDiffData) return;

          const updatedLines = stagedDiffData.diff.split('\n');
          if (rawIdx >= 0 && rawIdx < updatedLines.length) {
            const newCode = el.textContent || '';
            updatedLines[rawIdx] = '+' + newCode;
            stagedDiffData.diff = updatedLines.join('\n');
            stagedDiffIsEdited = true;

            if (stagedMultiFiles && stagedMultiFiles.length > 0) {
              const fileObj = stagedMultiFiles.find(f => f.file_path === stagedDiffData.file);
              if (fileObj) {
                fileObj.diff = stagedDiffData.diff;
              }
            }

            try {
              localStorage.setItem('clay_staged_diff', JSON.stringify({
                file: stagedDiffData.file,
                diff: stagedDiffData.diff,
                stagedFiles: stagedMultiFiles
              }));
            } catch (e) { }

            const badge = document.getElementById('diff-edited-badge');
            if (badge) badge.style.display = 'inline-block';
          }
        });
      });
    }
  }

  if (btnAcceptDiff) {
    btnAcceptDiff.addEventListener('click', async () => {
      if (!stagedDiffData) return;
      if (stagedDiffData.diff && stagedDiffData.diff.includes('<<<<<<< YOUR CHANGES')) {
        showToast('Cannot apply diff with merge conflicts. Choose [Accept Agent\'s] or [Keep Mine] first.', 'warning', 4000);
        return;
      }
      const targetFilePath = stagedDiffData.fullPath || stagedDiffData.file;
      if (isVsCode) {
        vscode.postMessage({
          command: 'applyPatch',
          file: targetFilePath,
          diff: stagedDiffData.diff,
          workspacePath: currentRepoPath
        });
      } else {
        try {
          const res = await fetch(`${BACKEND_URL}/api/agent/apply-patch`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              file_path: targetFilePath,
              diff: stagedDiffData.diff,
              workspace_path: currentRepoPath
            })
          });
          const data = await res.json();
          if (res.ok && data.status === 'success') {
            const appliedFile = data.filename || stagedDiffData.file;
            showToast(`Patch applied cleanly to ${appliedFile}`, 'success');
            loadRepoTree(currentRepoPath);

            if (stagedMultiFiles && stagedMultiFiles.length > 0) {
              const matched = stagedMultiFiles.find(f => isSameFileReference(f.file_path, appliedFile));
              if (matched) {
                matched.applied = true;
              }
              const remaining = stagedMultiFiles.filter(f => !f.applied && f.diff);
              if (remaining.length > 0) {
                const nextFile = remaining[0];
                stageDiff(nextFile.file_path, nextFile.diff, stagedMultiFiles);
                showToast(`Applied ${appliedFile}. ${remaining.length} file(s) remaining for review.`, 'info', 3000);
              } else {
                showToast('All staged file patches applied cleanly!', 'success', 3500);
                clearStagedDiff();
              }
            } else {
              clearStagedDiff();
            }
          } else {
            showToast(data.detail || data.message || 'Error applying patch', 'error');
          }
        } catch (e) {
          showToast(`Failed: ${e.message}`, 'error');
        }
      }
    });
  }

  if (btnRejectDiff) {
    btnRejectDiff.addEventListener('click', () => {
      clearStagedDiff();
    });
  }

  if (btnAcceptAllDiff) {
    btnAcceptAllDiff.addEventListener('click', async () => {
      const unapplied = stagedMultiFiles.filter(f => !f.applied && f.diff);
      if (unapplied.length === 0) {
        showToast('All staged files are already applied', 'info', 2000);
        return;
      }
      btnAcceptAllDiff.disabled = true;
      btnAcceptAllDiff.textContent = 'Applying...';

      if (isVsCode) {
        vscode.postMessage({
          command: 'applyAllPatches',
          patches: unapplied.map(f => ({ file_path: f.file_path, diff: f.diff })),
          workspacePath: currentRepoPath
        });
        setTimeout(() => {
          if (btnAcceptAllDiff) btnAcceptAllDiff.disabled = false;
        }, 3000);
      } else {
        try {
          const res = await fetch(`${BACKEND_URL}/api/agent/apply-all-patches`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              patches: unapplied.map(f => ({ file_path: f.file_path, diff: f.diff })),
              workspace_path: currentRepoPath
            })
          });
          const data = await res.json();
          if (res.ok && (data.status === 'success' || data.status === 'partial')) {
            for (const r of (data.results || [])) {
              const matched = stagedMultiFiles.find(f => isSameFileReference(f.file_path, r.file_path));
              if (matched) {
                if (r.applied) matched.applied = true;
                else matched.error = r.message;
              }
            }
            if (data.status === 'success' || stagedMultiFiles.every(f => f.applied)) {
              showToast(`Applied all ${data.applied_count} patches successfully!`, 'success');
              clearStagedDiff();
              loadRepoTree(currentRepoPath);
            } else {
              showToast(`Applied ${data.applied_count} of ${data.total_count} patches. Some files encountered errors.`, 'warning', 4000);
              const remaining = stagedMultiFiles.filter(f => !f.applied);
              if (remaining.length > 0) {
                stageDiff(remaining[0].file_path, remaining[0].diff, stagedMultiFiles);
              }
            }
          } else {
            showToast(data.detail || data.message || 'Error applying patches', 'error');
          }
        } catch (e) {
          showToast(`Failed: ${e.message}`, 'error');
        } finally {
          if (btnAcceptAllDiff) btnAcceptAllDiff.disabled = false;
        }
      }
    });
  }

  function clearStagedDiff() {
    stagedDiffData = null;
    stagedDiffIsEdited = false;
    stagedMultiFiles = [];
    hideConflictGuardPill();
    try {
      localStorage.removeItem('clay_staged_diff');
    } catch (e) { }
    const diffTabBtn = document.querySelector('.nav-tab[data-target="diff"]');
    const dot = diffTabBtn?.querySelector('.diff-staged-dot');
    if (dot) dot.remove();
    if (diffActionBar) diffActionBar.style.display = 'none';
    if (btnAcceptAllDiff) btnAcceptAllDiff.style.display = 'none';
    if (diffFileSelector) diffFileSelector.style.display = 'none';
    if (diffSummaryLabel) diffSummaryLabel.textContent = 'Staged Changes // 0 files';
    if (diffBadgeAdd) diffBadgeAdd.textContent = '+0';
    if (diffBadgeDel) diffBadgeDel.textContent = '-0';
    if (diffStagedContainer) {
      diffStagedContainer.innerHTML = `
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
      `;
    }
  }

  // Restore staged diff on startup if present
  try {
    const savedDiffStr = localStorage.getItem('clay_staged_diff');
    if (savedDiffStr) {
      const parsed = JSON.parse(savedDiffStr);
      if (parsed && parsed.file && parsed.diff) {
        stageDiff(parsed.file, parsed.diff, parsed.stagedFiles);
      }
    }
  } catch (e) { }

  // ==========================================
  // 6. Repo Intelligence: Configurable Local Path
  // ==========================================
  const inputRepoPath = document.getElementById('input-repo-path');
  const btnResetRepoPath = document.getElementById('btn-reset-repo-path');
  const btnScanRepo = document.getElementById('btn-scan-repo');
  const repoStatsText = document.getElementById('repo-stats-text');
  const repoStatusLabel = document.getElementById('repo-status-label');
  const repoSecurityLabel = document.getElementById('repo-security-label');
  const btnSecurityScan = document.getElementById('btn-security-scan');
  const repoSecurityDetails = document.getElementById('repo-security-details');
  const mermaidCode = document.getElementById('mermaid-code');
  const hierarchyCardTitle = document.getElementById('hierarchy-card-title');
  const btnClearFileSelection = document.getElementById('btn-clear-file-selection');
  let selectedRepoFile = null;

  const DEFAULT_REPO_PATH = '.';
  const btnBrowseRepoPath = document.getElementById('btn-browse-repo-path');
  const repoFolderPicker = document.getElementById('repo-folder-picker');
  const repoFileTree = document.getElementById('repo-file-tree');
  const repoTreeCount = document.getElementById('repo-tree-count');
  const btnRefreshTree = document.getElementById('btn-refresh-tree');
  const btnExpandTree = document.getElementById('btn-expand-tree');



  function isDiagramLightMode() {
    // 1. In CLAY mode (default): if claude-dark is present on body, it is ALWAYS Dark Mode!
    if (document.body.classList.contains('claude-dark')) {
      return false;
    }

    // 2. If user explicitly chose "IDE Tokens" (vscode-native mode), follow VS Code's editor theme
    if (document.body.classList.contains('vscode-native')) {
      if (document.body.classList.contains('vscode-light') || document.body.getAttribute('data-vscode-theme-kind') === 'vscode-light') {
        return true;
      }
      if (document.body.classList.contains('vscode-dark') || document.body.getAttribute('data-vscode-theme-kind') === 'vscode-dark') {
        return false;
      }
      try {
        const bg = window.getComputedStyle(document.body).backgroundColor;
        if (bg && bg.startsWith('rgb')) {
          const rgb = bg.match(/\d+/g);
          if (rgb && rgb.length >= 3) {
            const brightness = (parseInt(rgb[0], 10) * 299 + parseInt(rgb[1], 10) * 587 + parseInt(rgb[2], 10) * 114) / 1000;
            return brightness > 128;
          }
        }
      } catch (e) { }
      return false;
    }

    // 3. In CLAY mode without claude-dark, default is warm cream Light Mode!
    return true;
  }

  function decodeHtmlEntities(str) {
    if (!str) return '';
    if (!str.includes('&')) return str;
    const txt = document.createElement('textarea');
    txt.innerHTML = str;
    return txt.value;
  }

  function refreshAllMermaidDiagrams() {
    try {
      document.querySelectorAll('.mermaid-diagram-card[data-raw-mermaid]').forEach(card => {
        const rawAttr = card.getAttribute('data-raw-mermaid');
        if (rawAttr) {
          const raw = decodeHtmlEntities(rawAttr);
          const viewportInner = card.querySelector('.mermaid-viewport-inner');
          if (viewportInner) {
            const newSvg = renderMermaidSvg(raw);
            if (newSvg) {
              const wMatch = newSvg.match(/data-base-width="(\d+)"/);
              const hMatch = newSvg.match(/data-base-height="(\d+)"/);
              const baseW = wMatch ? parseInt(wMatch[1], 10) : 600;
              const baseH = hMatch ? parseInt(hMatch[1], 10) : 380;
              const oldCanvas = viewportInner.querySelector('.mermaid-diagram-canvas');
              const zoom = oldCanvas ? (oldCanvas.getAttribute('data-zoom') || '1.0') : '1.0';
              const zVal = parseFloat(zoom) || 1.0;
              const currW = Math.round(baseW * zVal);
              const currH = Math.round(baseH * zVal);
              viewportInner.innerHTML = `<div class="mermaid-diagram-canvas" data-zoom="${zVal}" data-base-width="${baseW}" data-base-height="${baseH}" style="width: ${currW}px; height: ${currH}px; min-width: ${currW}px;">${newSvg}</div>`;
            }
          }
        }
      });
    } catch (err) {
      console.warn('Failed to refresh mermaid diagrams:', err);
    }
  }

  function renderMermaidSvg(code) {
    if (!code || !code.trim()) return '';
    const trimmed = code.trim();
    try {
      // 1. Sequence Diagram check
      if (/^sequenceDiagram\b/i.test(trimmed)) {
        return renderSequenceDiagram(trimmed);
      }

      // 2. Class Diagram check
      if (/^classDiagram\b/i.test(trimmed)) {
        return renderClassDiagram(trimmed);
      }

      // 3. Gantt Chart check
      if (/^gantt\b/i.test(trimmed)) {
        return renderGanttChart(trimmed);
      }

      // 4. Flowchart / Architecture Graph (Default)
      return renderFlowchart(trimmed);
    } catch (err) {
      console.warn('Clay Mermaid render error:', err);
      return '';
    }
  }

  function renderSequenceDiagram(code) {
    const lines = code.trim().split(/\r?\n/);
    const participants = [];
    const participantSet = new Set();
    const messages = [];

    function addParticipant(id, label) {
      if (!participantSet.has(id)) {
        participantSet.add(id);
        participants.push({ id, label: (label || id).replace(/^"|"$/g, '') });
      }
    }

    for (let rawLine of lines) {
      let line = rawLine.trim();
      if (!line || line.startsWith('%%') || line === 'sequenceDiagram') continue;

      let pMatch = line.match(/^participant\s+([A-Za-z0-9_-]+)(?:\s+as\s+(.*))?$/i);
      if (pMatch) {
        addParticipant(pMatch[1], pMatch[2]);
        continue;
      }

      let msgMatch = line.match(/^([A-Za-z0-9_-]+)\s*(-->>|->>|->|-->)\s*([A-Za-z0-9_-]+)\s*:\s*(.*)$/);
      if (msgMatch) {
        const from = msgMatch[1];
        const arrow = msgMatch[2];
        const to = msgMatch[3];
        const text = msgMatch[4];
        addParticipant(from);
        addParticipant(to);
        messages.push({
          from,
          to,
          text,
          dashed: arrow.includes('--')
        });
      }
    }

    if (participants.length === 0) return '';

    const colWidth = 140;
    const startX = 60;
    const topY = 36;
    const msgGapY = 40;
    const totalW = Math.max(340, startX * 2 + (participants.length - 1) * colWidth);
    const totalH = topY * 2 + messages.length * msgGapY + 40;

    const isVscodeNative = document.body.classList.contains('vscode-native');
    const isLight = isDiagramLightMode();

    let nodeBg, nodeBorder, nodeText, edgeColor, lineColor, textMuted, shadowColor;

    if (isVscodeNative) {
      nodeBg = 'var(--vscode-editorWidget-background, #252526)';
      nodeBorder = 'var(--vscode-focusBorder, var(--vscode-button-background, #007acc))';
      nodeText = 'var(--vscode-editor-foreground, #EDEDEC)';
      edgeColor = 'var(--vscode-editor-foreground, #888888)';
      lineColor = 'var(--vscode-widget-border, rgba(128, 128, 128, 0.3))';
      textMuted = 'var(--vscode-descriptionForeground, #999999)';
      shadowColor = 'transparent';
    } else if (isLight) {
      nodeBg = '#FFFFFF';
      nodeBorder = '#CC6B49';
      nodeText = '#2D2926';
      edgeColor = '#B85837';
      lineColor = 'rgba(204, 107, 73, 0.3)';
      textMuted = '#746C63';
      shadowColor = 'rgba(0, 0, 0, 0.08)';
    } else {
      nodeBg = '#1E1B18';
      nodeBorder = '#CC6B49';
      nodeText = '#EDEDEC';
      edgeColor = '#E07A5F';
      lineColor = 'rgba(204, 107, 73, 0.25)';
      textMuted = '#E8B4A2';
      shadowColor = 'rgba(204, 107, 73, 0.25)';
    }

    const partCoords = new Map();
    participants.forEach((p, idx) => {
      partCoords.set(p.id, startX + idx * colWidth);
    });

    let svg = `
      <defs>
        <marker id="seq-arrow" viewBox="0 0 10 10" refX="7" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M 0 1.5 L 8 5 L 0 8.5 z" fill="${edgeColor}" />
        </marker>
        <filter id="clay-glow-seq" x="-10%" y="-10%" width="120%" height="120%">
          <feDropShadow dx="0" dy="2" stdDeviation="3" flood-color="${shadowColor}" />
        </filter>
      </defs>
    `;

    participants.forEach(p => {
      const x = partCoords.get(p.id);
      svg += `<line x1="${x}" y1="${topY + 28}" x2="${x}" y2="${totalH - topY}" stroke="${lineColor}" stroke-width="1.2" stroke-dasharray="4 4" />`;
    });

    messages.forEach((m, idx) => {
      const y = topY + 40 + idx * msgGapY;
      const x1 = partCoords.get(m.from);
      const x2 = partCoords.get(m.to);
      const dash = m.dashed ? 'stroke-dasharray="4 3"' : '';
      svg += `<line x1="${x1}" y1="${y}" x2="${x2}" y2="${y}" stroke="${edgeColor}" stroke-width="1.5" ${dash} marker-end="url(#seq-arrow)" />`;
      const midX = (x1 + x2) / 2;
      svg += `<text x="${midX}" y="${y - 6}" text-anchor="middle" fill="${textMuted}" font-family="sans-serif" font-size="9.5" font-weight="500">${escapeHtml(m.text)}</text>`;
    });

    participants.forEach(p => {
      const x = partCoords.get(p.id);
      const w = 90;
      const h = 26;
      svg += `
        <g class="mermaid-node-item">
          <rect x="${x - w / 2}" y="${topY}" width="${w}" height="${h}" rx="5" fill="${nodeBg}" stroke="${nodeBorder}" stroke-width="1.2" filter="url(#clay-glow-seq)" />
          <text x="${x}" y="${topY + 16}" text-anchor="middle" fill="${nodeText}" font-family="sans-serif" font-size="10.5" font-weight="600">${escapeHtml(p.label)}</text>
        </g>
      `;
    });

    return `
      <svg class="mermaid-svg" viewBox="0 0 ${totalW} ${totalH}" data-base-width="${totalW}" data-base-height="${totalH}" width="100%" height="100%" xmlns="http://www.w3.org/2000/svg" style="display: block; width: 100%; height: 100%; overflow: visible;">
        ${svg}
      </svg>
    `.trim();
  }

  function renderGanttChart(code) {
    const lines = code.trim().split(/\r?\n/);
    let title = 'Workflow Timeline';
    const sections = [];
    let currentSection = { name: 'Tasks', tasks: [] };
    sections.push(currentSection);

    for (let rawLine of lines) {
      const line = rawLine.trim();
      if (!line || line.startsWith('%%') || line === 'gantt' || line.startsWith('dateFormat') || line.startsWith('axisFormat')) {
        continue;
      }
      const titleMatch = line.match(/^title\s+(.*)$/i);
      if (titleMatch) {
        title = titleMatch[1].trim();
        continue;
      }
      const secMatch = line.match(/^section\s+(.*)$/i);
      if (secMatch) {
        currentSection = { name: secMatch[1].trim(), tasks: [] };
        sections.push(currentSection);
        continue;
      }
      const taskMatch = line.match(/^([^:]+):\s*(.*)$/);
      if (taskMatch) {
        const taskName = taskMatch[1].trim();
        const meta = taskMatch[2].trim();
        const parts = meta.split(',').map(s => s.trim());
        let status = 'active';
        let duration = '1 step';
        for (let p of parts) {
          if (['done', 'active', 'crit', 'milestone'].includes(p.toLowerCase())) {
            status = p.toLowerCase();
          } else if (/^\d+[dhwms]$/i.test(p) || /^\d+$/.test(p)) {
            duration = p;
          }
        }
        currentSection.tasks.push({
          name: taskName,
          status,
          duration: duration.endsWith('d') || duration.endsWith('step') ? duration : `${duration}d`
        });
      }
    }

    const allTasks = sections.flatMap(s => s.tasks.map(t => ({ ...t, section: s.name })));
    if (allTasks.length === 0) {
      // Fallback single milestone if empty
      allTasks.push({ name: 'Execution Plan', status: 'active', duration: '1d', section: 'Tasks' });
    }

    const labelColW = 160;
    const timelineW = 380;
    const padX = 24;
    const headerH = 46;
    const rowH = 34;
    const totalW = padX * 2 + labelColW + timelineW;
    const totalH = headerH + allTasks.length * rowH + 20;

    const isVscodeNative = document.body.classList.contains('vscode-native');
    const isLight = isDiagramLightMode();

    let terracotta, activeColor, doneColor, critColor, gridLineColor, phaseTextColor, taskTextColor, rowBgEven;

    if (isVscodeNative) {
      terracotta = 'var(--vscode-panelTitle-activeForeground, var(--vscode-button-background, #3794ff))';
      activeColor = 'var(--vscode-button-background, #0e639c)';
      doneColor = '#10B981';
      critColor = '#EF4444';
      gridLineColor = 'var(--vscode-widget-border, rgba(128, 128, 128, 0.2))';
      phaseTextColor = 'var(--vscode-descriptionForeground, #888888)';
      taskTextColor = 'var(--vscode-editor-foreground, #EDEDEC)';
      rowBgEven = 'rgba(255, 255, 255, 0.02)';
    } else if (isLight) {
      terracotta = '#CC6B49';
      activeColor = '#CC6B49';
      doneColor = '#059669';
      critColor = '#DC2626';
      gridLineColor = 'rgba(204, 107, 73, 0.18)';
      phaseTextColor = '#746C63';
      taskTextColor = '#2D2926';
      rowBgEven = 'rgba(204, 107, 73, 0.04)';
    } else {
      terracotta = '#CC6B49';
      activeColor = '#D97757';
      doneColor = '#10B981';
      critColor = '#EF4444';
      gridLineColor = 'rgba(204, 107, 73, 0.14)';
      phaseTextColor = '#8C847A';
      taskTextColor = '#EDEDEC';
      rowBgEven = 'rgba(255, 255, 255, 0.02)';
    }

    let svgRows = '';
    const numCols = Math.min(6, Math.max(3, allTasks.length));
    const colStepW = timelineW / numCols;

    let gridSvg = '';
    for (let c = 0; c <= numCols; c++) {
      const gx = padX + labelColW + c * colStepW;
      gridSvg += `<line x1="${gx}" y1="${headerH - 6}" x2="${gx}" y2="${totalH - 12}" stroke="${gridLineColor}" stroke-width="1" stroke-dasharray="3 3" />`;
      if (c < numCols) {
        gridSvg += `<text x="${gx + colStepW / 2}" y="${headerH - 10}" text-anchor="middle" fill="${phaseTextColor}" font-family="monospace, sans-serif" font-size="9" font-weight="600">PHASE ${c + 1}</text>`;
      }
    }

    allTasks.forEach((task, idx) => {
      const y = headerH + idx * rowH;
      const rowBg = idx % 2 === 0 ? rowBgEven : 'transparent';

      const startCol = idx % numCols;
      const barSpan = Math.min(numCols - startCol, Math.max(1, parseInt(task.duration, 10) || 1));
      const barX = padX + labelColW + startCol * colStepW + 4;
      const barW = Math.max(48, barSpan * colStepW - 8);
      const barH = 20;

      let barFill = activeColor;
      if (task.status === 'done') barFill = doneColor;
      if (task.status === 'crit') barFill = critColor;

      svgRows += `
        <rect x="${padX}" y="${y}" width="${totalW - padX * 2}" height="${rowH}" fill="${rowBg}" />
        <text x="${padX + 8}" y="${y + 19}" fill="${taskTextColor}" font-family="Outfit, -apple-system, sans-serif" font-size="10" font-weight="500">${escapeHtml(task.name)}</text>
        <g class="mermaid-node-item" style="cursor: default;">
          <rect x="${barX}" y="${y + 7}" width="${barW}" height="${barH}" rx="5" fill="${barFill}" opacity="0.9" />
          <text x="${barX + barW / 2}" y="${y + 20.5}" text-anchor="middle" fill="#FFFFFF" font-family="sans-serif" font-size="9" font-weight="600">${escapeHtml(task.duration)}</text>
        </g>
      `;
    });

    return `
      <svg class="mermaid-svg" viewBox="0 0 ${totalW} ${totalH}" data-base-width="${totalW}" data-base-height="${totalH}" width="100%" height="100%" xmlns="http://www.w3.org/2000/svg" style="display: block; width: 100%; height: 100%; overflow: visible;">
        <text x="${padX + 8}" y="22" fill="${terracotta}" font-family="monospace, sans-serif" font-size="10.5" font-weight="700" letter-spacing="0.5">${escapeHtml(title.toUpperCase())}</text>
        <line x1="${padX}" y1="${headerH - 4}" x2="${totalW - padX}" y2="${headerH - 4}" stroke="rgba(204, 107, 73, 0.3)" stroke-width="1.2" />
        ${gridSvg}
        ${svgRows}
      </svg>
    `.trim();
  }

  function renderClassDiagram(code) {
    const lines = code.trim().split(/\r?\n/);
    const classes = new Map();
    const relations = [];
    let currentClass = null;

    for (let rawLine of lines) {
      const line = rawLine.trim();
      if (!line || line.startsWith('%%') || line === 'classDiagram') continue;

      const classDefMatch = line.match(/^class\s+([A-Za-z0-9_]+)(?:\s*\{)?$/i);
      if (classDefMatch) {
        const cName = classDefMatch[1];
        currentClass = { name: cName, fields: [], methods: [] };
        classes.set(cName, currentClass);
        continue;
      }
      if (line === '}' && currentClass) {
        currentClass = null;
        continue;
      }

      if (currentClass) {
        if (line.includes('(') || /^[+\-#~]/.test(line)) {
          currentClass.methods.push(line);
        } else {
          currentClass.fields.push(line);
        }
        continue;
      }

      const relMatch = line.match(/^([A-Za-z0-9_]+)\s*(<\|--|\*--|o--|-->|--)\s*([A-Za-z0-9_]+)(?:\s*:\s*(.*))?$/);
      if (relMatch) {
        relations.push({
          from: relMatch[1],
          rel: relMatch[2],
          to: relMatch[3],
          label: relMatch[4] || ''
        });
        if (!classes.has(relMatch[1])) classes.set(relMatch[1], { name: relMatch[1], fields: [], methods: [] });
        if (!classes.has(relMatch[3])) classes.set(relMatch[3], { name: relMatch[3], fields: [], methods: [] });
      }
    }

    const classList = Array.from(classes.values());
    if (classList.length === 0) return '';

    const cols = classList.length > 1 ? 2 : 1;
    const cardW = 180;
    const gapX = 36;
    const gapY = 28;
    const padX = 28;
    const padY = 28;

    let maxX = 0;
    let maxY = 0;

    classList.forEach((cls, idx) => {
      const col = idx % cols;
      const row = Math.floor(idx / cols);
      const x = padX + col * (cardW + gapX);
      const headerH = 28;
      const fH = cls.fields.length > 0 ? cls.fields.length * 16 + 8 : 0;
      const mH = cls.methods.length > 0 ? cls.methods.length * 16 + 8 : 24;
      const cardH = headerH + fH + mH;
      cls.x = x;
      cls.y = padY + row * (140 + gapY);
      cls.width = cardW;
      cls.height = cardH;
      if (cls.x + cls.width > maxX) maxX = cls.x + cls.width;
      if (cls.y + cls.height > maxY) maxY = cls.y + cls.height;
    });

    const totalW = maxX + padX;
    const totalH = maxY + padY;

    const isVscodeNative = document.body.classList.contains('vscode-native');
    const isLight = isDiagramLightMode();

    let terracotta, edgeColor, nodeBg, nodeText, headerFill, dividerColor, fieldColor, methodColor, noMethodColor;

    if (isVscodeNative) {
      terracotta = 'var(--vscode-focusBorder, var(--vscode-button-background, #007acc))';
      edgeColor = 'var(--vscode-editor-foreground, #888888)';
      nodeBg = 'var(--vscode-editorWidget-background, #252526)';
      nodeText = 'var(--vscode-editor-foreground, #EDEDEC)';
      headerFill = 'var(--vscode-sideBarSectionHeader-background, rgba(128, 128, 128, 0.15))';
      dividerColor = 'var(--vscode-widget-border, rgba(128, 128, 128, 0.25))';
      fieldColor = 'var(--vscode-descriptionForeground, #999999)';
      methodColor = 'var(--vscode-editor-foreground, #EDEDEC)';
      noMethodColor = 'var(--vscode-descriptionForeground, #888888)';
    } else if (isLight) {
      terracotta = '#CC6B49';
      edgeColor = '#B85837';
      nodeBg = '#FFFFFF';
      nodeText = '#2D2926';
      headerFill = '#EDE6D8';
      dividerColor = 'rgba(204, 107, 73, 0.25)';
      fieldColor = '#746C63';
      methodColor = '#2D2926';
      noMethodColor = '#9E958A';
    } else {
      terracotta = '#CC6B49';
      edgeColor = '#E07A5F';
      nodeBg = '#1E1B18';
      nodeText = '#EDEDEC';
      headerFill = 'rgba(204, 107, 73, 0.18)';
      dividerColor = 'rgba(204, 107, 73, 0.25)';
      fieldColor = '#A8A29E';
      methodColor = '#EDEDEC';
      noMethodColor = '#8C847A';
    }

    let svg = '';
    classList.forEach(cls => {
      const headerH = 26;
      svg += `
        <g class="mermaid-node-item" style="cursor: default;">
          <rect x="${cls.x}" y="${cls.y}" width="${cls.width}" height="${cls.height}" rx="6" fill="${nodeBg}" stroke="${terracotta}" stroke-width="1.3" />
          <path d="M ${cls.x} ${cls.y + 6} A 6 6 0 0 1 ${cls.x + 6} ${cls.y} L ${cls.x + cls.width - 6} ${cls.y} A 6 6 0 0 1 ${cls.x + cls.width} ${cls.y + 6} L ${cls.x + cls.width} ${cls.y + headerH} L ${cls.x} ${cls.y + headerH} Z" fill="${headerFill}" />
          <line x1="${cls.x}" y1="${cls.y + headerH}" x2="${cls.x + cls.width}" y2="${cls.y + headerH}" stroke="${terracotta}" stroke-width="1" />
          <text x="${cls.x + cls.width / 2}" y="${cls.y + 17}" text-anchor="middle" fill="${nodeText}" font-family="monospace, sans-serif" font-size="11" font-weight="700">${escapeHtml(cls.name)}</text>
      `;

      let curY = cls.y + headerH + 14;
      if (cls.fields.length > 0) {
        cls.fields.forEach(f => {
          svg += `<text x="${cls.x + 10}" y="${curY}" fill="${fieldColor}" font-family="monospace, sans-serif" font-size="9">${escapeHtml(f)}</text>`;
          curY += 16;
        });
        svg += `<line x1="${cls.x}" y1="${curY - 6}" x2="${cls.x + cls.width}" y2="${curY - 6}" stroke="${dividerColor}" stroke-width="1" />`;
        curY += 6;
      }

      if (cls.methods.length > 0) {
        cls.methods.forEach(m => {
          svg += `<text x="${cls.x + 10}" y="${curY}" fill="${methodColor}" font-family="monospace, sans-serif" font-size="9.5">${escapeHtml(m)}</text>`;
          curY += 16;
        });
      } else {
        svg += `<text x="${cls.x + 10}" y="${curY}" fill="${noMethodColor}" font-family="monospace, sans-serif" font-size="9" font-style="italic">(no methods)</text>`;
      }
      svg += `</g>`;
    });

    relations.forEach(r => {
      const src = classes.get(r.from);
      const tgt = classes.get(r.to);
      if (!src || !tgt) return;
      const x1 = src.x + src.width / 2;
      const y1 = src.y + src.height;
      const x2 = tgt.x + tgt.width / 2;
      const y2 = tgt.y;
      svg += `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${edgeColor}" stroke-width="1.5" stroke-dasharray="3 3" />`;
    });

    return `
      <svg class="mermaid-svg" viewBox="0 0 ${totalW} ${totalH}" data-base-width="${totalW}" data-base-height="${totalH}" width="100%" height="100%" xmlns="http://www.w3.org/2000/svg" style="display: block; width: 100%; height: 100%; overflow: visible;">
        ${svg}
      </svg>
    `.trim();
  }

  function renderFlowchart(code) {
    const lines = code.trim().split(/\r?\n/);
    let direction = 'TD';
    const nodes = new Map();
    const edges = [];
    const subgraphs = [];
    let currentSubgraph = null;

    for (let rawLine of lines) {
      let line = rawLine.trim();
      if (!line || line.startsWith('%%')) continue;

      const dirMatch = line.match(/^(?:graph|flowchart)\s+(TD|TB|LR|BT|RL)/i);
      if (dirMatch) {
        direction = dirMatch[1].toUpperCase();
        if (direction === 'TB') direction = 'TD';
        continue;
      }

      const subMatch = line.match(/^subgraph\s+([A-Za-z0-9_-]+)(?:\s*\[(.*?)\])?/i);
      if (subMatch) {
        currentSubgraph = {
          id: subMatch[1],
          title: subMatch[2] || subMatch[1],
          nodeIds: new Set()
        };
        subgraphs.push(currentSubgraph);
        continue;
      }
      if (line === 'end' && currentSubgraph) {
        currentSubgraph = null;
        continue;
      }

      if (/^(?:classDef|class|style|click|linkStyle)\b/i.test(line)) continue;

      function extractNode(token) {
        token = token.trim();
        if (!token) return null;

        let m = token.match(/^([A-Za-z0-9_-]+)\[\((.*?)\)\]$/);
        if (m) {
          registerNode(m[1], m[2], 'cylinder');
          return m[1];
        }
        m = token.match(/^([A-Za-z0-9_-]+)\(\[(.*?)\]\)$/);
        if (m) {
          registerNode(m[1], m[2], 'stadium');
          return m[1];
        }
        m = token.match(/^([A-Za-z0-9_-]+)\{(.*?)\}$/);
        if (m) {
          registerNode(m[1], m[2], 'diamond');
          return m[1];
        }
        m = token.match(/^([A-Za-z0-9_-]+)\((.*?)\)$/);
        if (m) {
          registerNode(m[1], m[2], 'rounded');
          return m[1];
        }
        m = token.match(/^([A-Za-z0-9_-]+)\[\[(.*?)\]\]$/);
        if (m) {
          registerNode(m[1], m[2], 'module');
          return m[1];
        }
        m = token.match(/^([A-Za-z0-9_-]+)\[(?:"(.*)"|(.*?))\]$/);
        if (m) {
          const label = m[2] !== undefined ? m[2] : m[3];
          registerNode(m[1], label, 'rect');
          return m[1];
        }
        m = token.match(/^([A-Za-z0-9_-]+)$/);
        if (m) {
          registerNode(m[1], m[1], 'rect');
          return m[1];
        }
        return null;
      }

      function registerNode(id, label, shape) {
        if (!nodes.has(id)) {
          nodes.set(id, {
            id,
            label: (label || id).replace(/^"|"$/g, ''),
            shape: shape || 'rect',
            subgraph: currentSubgraph ? currentSubgraph.id : null
          });
        } else {
          const n = nodes.get(id);
          if (label && label !== id) n.label = label.replace(/^"|"$/g, '');
          if (shape) n.shape = shape;
          if (currentSubgraph) n.subgraph = currentSubgraph.id;
        }
        if (currentSubgraph) {
          currentSubgraph.nodeIds.add(id);
        }
      }

      const edgePattern = /(-->\|.*?\||--\s*.*?\s*-->|-->|-\.->|==>|---|---\|.*?\|)/g;
      const parts = line.split(edgePattern);

      if (parts.length >= 3) {
        for (let p = 0; p < parts.length - 2; p += 2) {
          const fromToken = parts[p].trim();
          const arrowToken = parts[p + 1].trim();
          const toToken = parts[p + 2].trim();

          const fromId = extractNode(fromToken);
          const toId = extractNode(toToken);

          if (fromId && toId) {
            let edgeLabel = '';
            let edgeStyle = 'solid';

            if (arrowToken.startsWith('-.->')) {
              edgeStyle = 'dashed';
            } else if (arrowToken.startsWith('==>')) {
              edgeStyle = 'thick';
            }

            const pipeMatch = arrowToken.match(/\|(.*?)\|/);
            if (pipeMatch) {
              edgeLabel = pipeMatch[1].trim();
            } else {
              const inlineMatch = arrowToken.match(/--\s*(.*?)\s*-->/);
              if (inlineMatch) edgeLabel = inlineMatch[1].trim();
            }

            edges.push({
              from: fromId,
              to: toId,
              label: edgeLabel,
              style: edgeStyle
            });
          }
        }
      } else {
        extractNode(line);
      }
    }

    const nodeList = Array.from(nodes.values());
    if (nodeList.length === 0) {
      return '';
    }

    // Helper: Wrap long labels intelligently into multiple lines to prevent text overflow
    function splitLabelLines(rawText, maxCharsPerLine = 24) {
      if (!rawText) return [''];
      const clean = rawText.replace(/<br\s*\/?>/gi, '\n').replace(/\\n/g, '\n').trim();
      const paragraphs = clean.split('\n');
      const lines = [];

      for (let para of paragraphs) {
        para = para.trim();
        if (!para) continue;
        if (para.length <= maxCharsPerLine) {
          lines.push(para);
          continue;
        }

        // Split on whitespace or common word boundaries (parentheses, dots, underscores, hyphens)
        const tokens = para.split(/(\s+|(?<=[_.()/-]))/);
        let curr = '';
        for (let tok of tokens) {
          if (!tok) continue;
          if ((curr + tok).length <= maxCharsPerLine) {
            curr += tok;
          } else {
            if (curr.trim()) lines.push(curr.trim());
            curr = tok.trim();
          }
        }
        if (curr.trim()) lines.push(curr.trim());
      }
      return lines.length > 0 ? lines : [rawText];
    }

    // Measure and size each node dynamically based on its text
    for (let n of nodeList) {
      n.lines = splitLabelLines(n.label || n.id, 24);
      const maxLineLen = Math.max(...n.lines.map(l => l.length), 1);
      // Generous width so text never clips or bleeds outside box (min 120, comfortable padding)
      n.width = Math.max(120, Math.min(290, maxLineLen * 8.2 + 36));
      const lineH = 15;
      const basePad = n.shape === 'cylinder' ? 32 : (n.shape === 'diamond' ? 34 : 24);
      n.height = Math.max(n.shape === 'cylinder' ? 46 : 38, n.lines.length * lineH + basePad);
      if (n.shape === 'diamond') {
        n.width = Math.max(n.width, n.height * 1.45);
      }
    }

    const inDegree = new Map();
    const outDegree = new Map();
    const adj = new Map();
    const revAdj = new Map();

    for (let n of nodeList) {
      inDegree.set(n.id, 0);
      outDegree.set(n.id, 0);
      adj.set(n.id, []);
      revAdj.set(n.id, []);
    }

    for (let e of edges) {
      if (adj.has(e.from)) {
        adj.get(e.from).push(e.to);
        outDegree.set(e.from, (outDegree.get(e.from) || 0) + 1);
      }
      if (inDegree.has(e.to)) {
        inDegree.set(e.to, (inDegree.get(e.to) || 0) + 1);
      }
      if (revAdj.has(e.to)) {
        revAdj.get(e.to).push(e.from);
      }
    }

    // 1. Detect back-edges (cycles/loops) with 3-color DFS to PREVENT rank explosion & gigantic vertical lines
    const color = new Map(); // 0 = unvisited, 1 = in-stack (ancestor), 2 = visited
    for (let n of nodeList) color.set(n.id, 0);
    const backEdges = new Set();

    function dfsCycle(u) {
      color.set(u, 1);
      const neighbors = adj.get(u) || [];
      for (let v of neighbors) {
        const c = color.get(v);
        if (c === 1) {
          // u -> v is a back-edge (loop back to an ancestor)
          backEdges.add(`${u}->${v}`);
        } else if (c === 0) {
          dfsCycle(v);
        }
      }
      color.set(u, 2);
    }

    // Run DFS starting from root nodes (inDegree === 0)
    for (let n of nodeList) {
      if ((inDegree.get(n.id) || 0) === 0 && color.get(n.id) === 0) {
        dfsCycle(n.id);
      }
    }
    // Then any remaining unvisited nodes
    for (let n of nodeList) {
      if (color.get(n.id) === 0) {
        dfsCycle(n.id);
      }
    }

    // Tag edges with isBackEdge
    for (let e of edges) {
      e.isBackEdge = backEdges.has(`${e.from}->${e.to}`);
    }

    // 2. Assign ranks on forward-edge DAG only (immune to cycle explosions)
    const ranks = new Map();
    for (let n of nodeList) ranks.set(n.id, 0);

    for (let iter = 0; iter < nodeList.length; iter++) {
      let changed = false;
      for (let e of edges) {
        if (e.isBackEdge) continue; // Skip back-edges in rank assignment!
        const rFrom = ranks.get(e.from) || 0;
        const rTo = ranks.get(e.to) || 0;
        if (rTo <= rFrom) {
          ranks.set(e.to, rFrom + 1);
          changed = true;
        }
      }
      if (!changed) break;
    }

    // 3. Rank Compaction: eliminate any empty rank gaps so nodes stay tightly and proportionally packed
    const uniqueRanks = Array.from(new Set(ranks.values())).sort((a, b) => a - b);
    const rankMap = new Map();
    uniqueRanks.forEach((r, idx) => rankMap.set(r, idx));
    for (let [id, r] of ranks.entries()) {
      ranks.set(id, rankMap.get(r));
    }

    const maxRank = Math.max(...Array.from(ranks.values()), 0);
    const layers = [];
    for (let r = 0; r <= maxRank; r++) layers.push([]);

    for (let n of nodeList) {
      const r = Math.min(ranks.get(n.id) || 0, maxRank);
      layers[r].push(n);
    }

    // 4. Barycenter ordering within each layer to minimize edge crossings
    for (let r = 1; r < layers.length; r++) {
      const layer = layers[r];
      if (layer.length <= 1) continue;
      const prevLayer = layers[r - 1];
      const prevPos = new Map();
      prevLayer.forEach((n, idx) => prevPos.set(n.id, idx));

      layer.sort((a, b) => {
        const parentsA = revAdj.get(a.id) || [];
        const parentsB = revAdj.get(b.id) || [];
        const avgA = parentsA.length > 0
          ? parentsA.reduce((sum, p) => sum + (prevPos.has(p) ? prevPos.get(p) : prevLayer.length / 2), 0) / parentsA.length
          : prevLayer.length / 2;
        const avgB = parentsB.length > 0
          ? parentsB.reduce((sum, p) => sum + (prevPos.has(p) ? prevPos.get(p) : prevLayer.length / 2), 0) / parentsB.length
          : prevLayer.length / 2;
        return avgA - avgB;
      });
    }

    const isTD = direction === 'TD';
    const hasBackEdges = backEdges.size > 0;
    const padX = hasBackEdges ? 54 : 36;
    const padY = 32;
    const gapX = isTD ? 28 : 44;
    const gapY = isTD ? 52 : 24;

    let totalWidth = 0;
    let totalHeight = 0;

    if (isTD) {
      let maxLayerWidth = 0;
      const wrappedLayers = [];
      const MAX_NODES_PER_ROW = 5;

      for (let layer of layers) {
        if (layer.length === 0) continue; // Skip empty ranks!
        if (layer.length <= MAX_NODES_PER_ROW) {
          wrappedLayers.push(layer);
        } else {
          // Balance sub-rows evenly: e.g. 6 nodes -> 3 and 3, 7 nodes -> 4 and 3
          const numRows = Math.ceil(layer.length / MAX_NODES_PER_ROW);
          const perRow = Math.ceil(layer.length / numRows);
          for (let i = 0; i < layer.length; i += perRow) {
            wrappedLayers.push(layer.slice(i, i + perRow));
          }
        }
      }

      const layerWidths = wrappedLayers.map(layer => {
        const w = layer.reduce((acc, n) => acc + n.width, 0) + Math.max(0, layer.length - 1) * gapX;
        if (w > maxLayerWidth) maxLayerWidth = w;
        return w;
      });

      totalWidth = Math.max(360, maxLayerWidth + padX * 2);
      let currY = padY;

      for (let r = 0; r < wrappedLayers.length; r++) {
        const layer = wrappedLayers[r];
        const layerW = layerWidths[r];
        let currX = (totalWidth - layerW) / 2;

        let layerMaxH = 0;
        for (let n of layer) {
          n.x = currX;
          n.y = currY;
          currX += n.width + gapX;
          if (n.height > layerMaxH) layerMaxH = n.height;
        }
        currY += layerMaxH + gapY;
      }
      totalHeight = currY - gapY + padY;
    } else {
      let maxLayerHeight = 0;
      const layerHeights = layers.filter(l => l.length > 0).map(layer => {
        const h = layer.reduce((acc, n) => acc + n.height, 0) + Math.max(0, layer.length - 1) * gapY;
        if (h > maxLayerHeight) maxLayerHeight = h;
        return h;
      });

      totalHeight = maxLayerHeight + padY * 2;
      let currX = padX;

      for (let r = 0; r < layers.length; r++) {
        const layer = layers[r];
        if (layer.length === 0) continue;
        const layerH = layer.reduce((acc, n) => acc + n.height, 0) + Math.max(0, layer.length - 1) * gapY;
        let currY = (totalHeight - layerH) / 2;

        let layerMaxW = 0;
        for (let n of layer) {
          n.x = currX;
          n.y = currY;
          currY += n.height + gapY;
          if (n.width > layerMaxW) layerMaxW = n.width;
        }
        currX += layerMaxW + gapX;
      }
      totalWidth = currX - gapX + padX;
    }

    const nodeMap = new Map(nodeList.map(n => [n.id, n]));

    // 5. Calculate distributed connection ports for cleaner non-overlapping edges
    const outEdgesBySrc = new Map();
    const inEdgesByTgt = new Map();

    for (let e of edges) {
      if (e.isBackEdge) continue;
      if (!outEdgesBySrc.has(e.from)) outEdgesBySrc.set(e.from, []);
      outEdgesBySrc.get(e.from).push(e);

      if (!inEdgesByTgt.has(e.to)) inEdgesByTgt.set(e.to, []);
      inEdgesByTgt.get(e.to).push(e);
    }

    for (let [srcId, edgeList] of outEdgesBySrc.entries()) {
      edgeList.sort((a, b) => {
        const tgtA = nodeMap.get(a.to);
        const tgtB = nodeMap.get(b.to);
        return (tgtA ? tgtA.x : 0) - (tgtB ? tgtB.x : 0);
      });
      edgeList.forEach((e, idx) => {
        e.srcPortIndex = idx;
        e.srcPortCount = edgeList.length;
      });
    }

    for (let [tgtId, edgeList] of inEdgesByTgt.entries()) {
      edgeList.sort((a, b) => {
        const srcA = nodeMap.get(a.from);
        const srcB = nodeMap.get(b.from);
        return (srcA ? srcA.x : 0) - (srcB ? srcB.x : 0);
      });
      edgeList.forEach((e, idx) => {
        e.tgtPortIndex = idx;
        e.tgtPortCount = edgeList.length;
      });
    }

    const subgraphBoxes = [];
    for (let sub of subgraphs) {
      const subNodes = nodeList.filter(n => n.subgraph === sub.id);
      if (subNodes.length > 0) {
        const minX = Math.min(...subNodes.map(n => n.x)) - 14;
        const minY = Math.min(...subNodes.map(n => n.y)) - 24;
        const maxX = Math.max(...subNodes.map(n => n.x + n.width)) + 14;
        const maxY = Math.max(...subNodes.map(n => n.y + n.height)) + 14;
        subgraphBoxes.push({
          id: sub.id,
          title: sub.title,
          x: minX,
          y: minY,
          width: maxX - minX,
          height: maxY - minY
        });
      }
    }

    const isVscodeNative = document.body.classList.contains('vscode-native');
    const isLight = isDiagramLightMode();

    let terracotta, nodeBg, nodeBorder, nodeText, subBg, subBorder, subHeaderBg, edgeColor, lblFill, lblStroke, lblText, shadowColor;

    if (isVscodeNative) {
      terracotta = 'var(--vscode-panelTitle-activeForeground, var(--vscode-button-background, #3794ff))';
      nodeBg = 'var(--vscode-editorWidget-background, #252526)';
      nodeBorder = 'var(--vscode-focusBorder, var(--vscode-button-background, #007acc))';
      nodeText = 'var(--vscode-editor-foreground, #EDEDEC)';
      subBg = 'var(--vscode-sideBar-background, rgba(128, 128, 128, 0.05))';
      subBorder = 'var(--vscode-widget-border, rgba(128, 128, 128, 0.3))';
      subHeaderBg = 'var(--vscode-editorWidget-background, #252526)';
      edgeColor = 'var(--vscode-editor-foreground, #888888)';
      lblFill = 'var(--vscode-editor-background, #1e1e1e)';
      lblStroke = 'var(--vscode-widget-border, #333333)';
      lblText = 'var(--vscode-descriptionForeground, #999999)';
      shadowColor = 'transparent';
    } else if (isLight) {
      terracotta = '#CC6B49';
      nodeBg = '#FFFFFF';
      nodeBorder = '#CC6B49';
      nodeText = '#2D2926';
      subBg = 'rgba(204, 107, 73, 0.04)';
      subBorder = 'rgba(204, 107, 73, 0.28)';
      subHeaderBg = '#EDE6D8';
      edgeColor = '#B85837';
      lblFill = '#EDE6D8';
      lblStroke = 'rgba(204, 107, 73, 0.35)';
      lblText = '#594F45';
      shadowColor = 'rgba(0, 0, 0, 0.08)';
    } else {
      terracotta = '#CC6B49';
      nodeBg = '#1E1B18';
      nodeBorder = '#CC6B49';
      nodeText = '#EDEDEC';
      subBg = 'rgba(204, 107, 73, 0.05)';
      subBorder = 'rgba(204, 107, 73, 0.3)';
      subHeaderBg = '#24201D';
      edgeColor = '#E07A5F';
      lblFill = '#1C1917';
      lblStroke = 'rgba(204, 107, 73, 0.4)';
      lblText = '#E8B4A2';
      shadowColor = 'rgba(204, 107, 73, 0.25)';
    }

    let svgContent = '';

    svgContent += `
      <defs>
        <marker id="clay-arrow" viewBox="0 0 10 10" refX="7" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M 0 1.5 L 8 5 L 0 8.5 z" fill="${edgeColor}" />
        </marker>
        <filter id="clay-glow" x="-10%" y="-10%" width="120%" height="120%">
          <feDropShadow dx="0" dy="2" stdDeviation="3" flood-color="${shadowColor}" />
        </filter>
      </defs>
    `;

    for (let s of subgraphBoxes) {
      svgContent += `
        <g class="mermaid-subgraph">
          <rect x="${s.x}" y="${s.y}" width="${s.width}" height="${s.height}" rx="10" fill="${subBg}" stroke="${subBorder}" stroke-width="1.2" stroke-dasharray="4 3" />
          <rect x="${s.x + 10}" y="${s.y - 10}" width="${Math.min(s.width - 20, s.title.length * 7 + 16)}" height="18" rx="4" fill="${subHeaderBg}" stroke="${subBorder}" stroke-width="1" />
          <text x="${s.x + 18}" y="${s.y + 3}" fill="${terracotta}" font-family="monospace, sans-serif" font-size="9.5" font-weight="600" letter-spacing="0.5">${escapeHtml(s.title.toUpperCase())}</text>
        </g>
      `;
    }

    // 6. Edge drawing with distributed ports and side routing for loop-backs
    for (let e of edges) {
      const src = nodeMap.get(e.from);
      const tgt = nodeMap.get(e.to);
      if (!src || !tgt) continue;

      let x1, y1, x2, y2, cx1, cy1, cx2, cy2;

      if (isTD) {
        if (e.isBackEdge || tgt.y < src.y) {
          // Route loop-back edge gracefully around outer margin rather than slicing through center nodes
          const goRight = (src.x + src.width / 2) >= totalWidth / 2;
          const xSide = goRight
            ? Math.max(totalWidth - padX + 18, Math.max(src.x + src.width, tgt.x + tgt.width) + 24)
            : Math.min(padX - 18, Math.min(src.x, tgt.x) - 24);

          x1 = goRight ? src.x + src.width : src.x;
          y1 = src.y + src.height / 2;
          x2 = goRight ? tgt.x + tgt.width : tgt.x;
          y2 = tgt.y + tgt.height / 2;

          cx1 = xSide;
          cy1 = y1;
          cx2 = xSide;
          cy2 = y2;
        } else {
          // Forward edge: use distributed ports across node borders
          const srcFrac = (e.srcPortCount && e.srcPortCount > 1)
            ? (e.srcPortIndex + 1) / (e.srcPortCount + 1)
            : 0.5;
          const tgtFrac = (e.tgtPortCount && e.tgtPortCount > 1)
            ? (e.tgtPortIndex + 1) / (e.tgtPortCount + 1)
            : 0.5;

          x1 = src.x + Math.max(12, Math.min(src.width - 12, src.width * srcFrac));
          y1 = src.y + src.height;
          x2 = tgt.x + Math.max(12, Math.min(tgt.width - 12, tgt.width * tgtFrac));
          y2 = tgt.y;

          const dy = Math.max(18, Math.min(100, (y2 - y1) * 0.5));
          cx1 = x1;
          cy1 = y1 + dy;
          cx2 = x2;
          cy2 = y2 - dy;
        }
      } else {
        // LR layout
        if (e.isBackEdge || tgt.x < src.x) {
          const goBottom = (src.y + src.height / 2) >= totalHeight / 2;
          const ySide = goBottom
            ? Math.max(totalHeight - padY + 16, Math.max(src.y + src.height, tgt.y + tgt.height) + 20)
            : Math.min(padY - 16, Math.min(src.y, tgt.y) - 20);

          x1 = src.x + src.width / 2;
          y1 = goBottom ? src.y + src.height : src.y;
          x2 = tgt.x + tgt.width / 2;
          y2 = goBottom ? tgt.y + tgt.height : tgt.y;

          cx1 = x1;
          cy1 = ySide;
          cx2 = x2;
          cy2 = ySide;
        } else {
          const srcFrac = (e.srcPortCount && e.srcPortCount > 1)
            ? (e.srcPortIndex + 1) / (e.srcPortCount + 1)
            : 0.5;
          const tgtFrac = (e.tgtPortCount && e.tgtPortCount > 1)
            ? (e.tgtPortIndex + 1) / (e.tgtPortCount + 1)
            : 0.5;

          x1 = src.x + src.width;
          y1 = src.y + Math.max(10, Math.min(src.height - 10, src.height * srcFrac));
          x2 = tgt.x;
          y2 = tgt.y + Math.max(10, Math.min(tgt.height - 10, tgt.height * tgtFrac));

          const dx = Math.max(18, (x2 - x1) * 0.5);
          cx1 = x1 + dx;
          cy1 = y1;
          cx2 = x2 - dx;
          cy2 = y2;
        }
      }

      const strokeDash = e.style === 'dashed' ? 'stroke-dasharray="4 3"' : '';
      const strokeWidth = e.style === 'thick' ? '2.4' : '1.5';
      const pathD = `M ${x1} ${y1} C ${cx1} ${cy1}, ${cx2} ${cy2}, ${x2} ${y2}`;

      svgContent += `
        <g class="mermaid-edge-group">
          <path d="${pathD}" fill="none" stroke="${edgeColor}" stroke-width="${strokeWidth}" ${strokeDash} marker-end="url(#clay-arrow)" opacity="0.85" />
      `;

      if (e.label) {
        const midX = (x1 + x2) / 2;
        const midY = (y1 + y2) / 2;
        const lblW = Math.max(34, e.label.length * 6.6 + 14);
        const lblH = 17;
        svgContent += `
          <rect x="${midX - lblW / 2}" y="${midY - lblH / 2}" width="${lblW}" height="${lblH}" rx="4" fill="${lblFill}" stroke="${lblStroke}" stroke-width="1" />
          <text x="${midX}" y="${midY + 3.5}" text-anchor="middle" fill="${lblText}" font-family="sans-serif" font-size="8.5" font-weight="500">${escapeHtml(e.label)}</text>
        `;
      }
      svgContent += `</g>`;
    }

    // 7. Node rendering with multi-line centered text
    for (let n of nodeList) {
      let shapeSvg = '';
      const rx = n.shape === 'rounded' ? 14 : n.shape === 'stadium' ? 20 : 6;
      const glowFilter = isVscodeNative ? 'none' : 'url(#clay-glow)';

      if (n.shape === 'cylinder') {
        shapeSvg = `
          <path d="M ${n.x} ${n.y + 10} C ${n.x} ${n.y}, ${n.x + n.width} ${n.y}, ${n.x + n.width} ${n.y + 10} L ${n.x + n.width} ${n.y + n.height - 8} C ${n.x + n.width} ${n.y + n.height + 2}, ${n.x} ${n.y + n.height + 2}, ${n.x} ${n.y + n.height - 8} Z" fill="${nodeBg}" stroke="${nodeBorder}" stroke-width="1.3" filter="${glowFilter}" />
          <path d="M ${n.x} ${n.y + 10} C ${n.x} ${n.y + 18}, ${n.x + n.width} ${n.y + 18}, ${n.x + n.width} ${n.y + 10}" fill="none" stroke="${nodeBorder}" stroke-width="1.1" />
        `;
      } else if (n.shape === 'diamond') {
        const cx = n.x + n.width / 2;
        const cy = n.y + n.height / 2;
        shapeSvg = `<polygon points="${cx},${n.y} ${n.x + n.width},${cy} ${cx},${n.y + n.height} ${n.x},${cy}" fill="${nodeBg}" stroke="${nodeBorder}" stroke-width="1.3" filter="${glowFilter}" />`;
      } else {
        shapeSvg = `<rect x="${n.x}" y="${n.y}" width="${n.width}" height="${n.height}" rx="${rx}" fill="${nodeBg}" stroke="${nodeBorder}" stroke-width="1.3" filter="${glowFilter}" />`;
      }

      const numLines = (n.lines && n.lines.length) || 1;
      const lineHeight = 13.5;
      const startTextY = (n.y + n.height / 2) - ((numLines - 1) * lineHeight) / 2 + 3.5;
      let textTspans = '';
      if (n.lines && n.lines.length > 0) {
        n.lines.forEach((l, idx) => {
          const dy = idx === 0 ? 0 : lineHeight;
          textTspans += `<tspan x="${n.x + n.width / 2}" dy="${dy}">${escapeHtml(l)}</tspan>`;
        });
      } else {
        textTspans = `<tspan x="${n.x + n.width / 2}">${escapeHtml(n.label)}</tspan>`;
      }

      svgContent += `
        <g class="mermaid-node-item" data-node-id="${escapeHtml(n.id)}" style="cursor: default;">
          ${shapeSvg}
          <text x="${n.x + n.width / 2}" y="${startTextY}" text-anchor="middle" fill="${nodeText}" font-family="Outfit, -apple-system, sans-serif" font-size="10" font-weight="500">
            ${textTspans}
          </text>
        </g>
      `;
    }

    const finalWidth = Math.max(340, totalWidth);
    const finalHeight = Math.max(140, totalHeight);

    return `
      <svg class="mermaid-svg" viewBox="0 0 ${finalWidth} ${finalHeight}" data-base-width="${finalWidth}" data-base-height="${finalHeight}" width="100%" height="100%" xmlns="http://www.w3.org/2000/svg" style="display: block; width: 100%; height: 100%; overflow: visible;">
        ${svgContent}
      </svg>
    `.trim();
  }

  function createMermaidDiagramHtml(code) {
    const rawCode = code.trim();
    const svg = renderMermaidSvg(rawCode);
    if (!svg) {
      return `
        <div class="code-block-wrapper">
          <div class="code-block-header">
            <span class="code-block-lang">mermaid</span>
            <button class="btn-copy-code" type="button" title="Copy code">
              <svg class="icon-code-copy" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
              <span class="btn-copy-code-text">Copy</span>
            </button>
          </div>
          <pre class="code-block-pre"><code>${escapeHtml(rawCode)}</code></pre>
        </div>
      `;
    }

    const wMatch = svg.match(/data-base-width="(\d+)"/);
    const hMatch = svg.match(/data-base-height="(\d+)"/);
    const baseW = wMatch ? parseInt(wMatch[1], 10) : 600;
    const baseH = hMatch ? parseInt(hMatch[1], 10) : 380;

    let titleText = 'Architecture Diagram';
    if (/^sequenceDiagram\b/i.test(rawCode)) titleText = 'Sequence Diagram';
    else if (/^classDiagram\b/i.test(rawCode)) titleText = 'Class Architecture';
    else if (/^gantt\b/i.test(rawCode)) titleText = 'Workflow Timeline';

    return `
      <div class="mermaid-diagram-card" data-raw-mermaid="${escapeHtml(rawCode)}">
        <div class="mermaid-diagram-header">
          <div class="mermaid-diagram-title">
            <svg class="icon-mermaid-diagram" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect x="3" y="3" width="7" height="7"></rect>
              <rect x="14" y="3" width="7" height="7"></rect>
              <rect x="14" y="14" width="7" height="7"></rect>
              <rect x="3" y="14" width="7" height="7"></rect>
              <line x1="10" y1="6.5" x2="14" y2="6.5"></line>
              <line x1="6.5" y1="10" x2="6.5" y2="14"></line>
              <line x1="14" y1="17.5" x2="10" y2="17.5"></line>
            </svg>
            <span>${escapeHtml(titleText)}</span>
          </div>
          <div class="mermaid-diagram-actions">
            <div class="mermaid-zoom-pill" title="Zoom diagram (30% - 500%)">
              <button type="button" class="btn-mermaid-zoom btn-mermaid-zoom-out" title="Zoom out">−</button>
              <button type="button" class="btn-mermaid-zoom btn-mermaid-zoom-fit" title="Fit to window">Fit</button>
              <button type="button" class="btn-mermaid-zoom btn-mermaid-zoom-reset" title="Reset zoom (100%)">100%</button>
              <button type="button" class="btn-mermaid-zoom btn-mermaid-zoom-in" title="Zoom in">+</button>
            </div>
            <div class="mermaid-view-pill">
              <button type="button" class="btn-mermaid-view-toggle btn-mermaid-active" data-view="diagram" title="Visual architecture graph">Diagram</button>
              <button type="button" class="btn-mermaid-view-toggle" data-view="code" title="Mermaid syntax source">Code</button>
            </div>
            <button type="button" class="btn-mermaid-action btn-mermaid-copy-svg" title="Copy rendered SVG markup">Copy SVG</button>
            <button type="button" class="btn-mermaid-action btn-mermaid-copy-code" title="Copy Mermaid text">Copy</button>
          </div>
        </div>
        <div class="mermaid-diagram-viewport" tabindex="0" title="Click and drag to pan" style="scrollbar-width: none; -ms-overflow-style: none;">
          <div class="mermaid-viewport-inner">
            <div class="mermaid-diagram-canvas" data-zoom="1.0" data-base-width="${baseW}" data-base-height="${baseH}" style="width: ${baseW}px; height: ${baseH}px; min-width: ${baseW}px;">
              ${svg}
            </div>
          </div>
        </div>
        <div class="mermaid-code-fallback" style="display: none;">
          <pre class="code-block-pre"><code>${escapeHtml(rawCode)}</code></pre>
        </div>
      </div>
    `;
  }

  let currentGraphNodesMeta = {};

  function renderInteractiveHierarchyLine(line, nodesMeta) {
    const trimmed = line.trim();
    if (!trimmed || trimmed === 'graph TD' || trimmed === 'graph LR') {
      return `<div class="hierarchy-header-line">${escapeHtml(trimmed || '')}</div>`;
    }

    // 1. Edge with relation label: A -->|calls| B
    const labeledEdgeMatch = trimmed.match(/^([^\s\-\>\|\[\]]+)\s*(?:-->|---\>)\s*\|([^|]+)\|\s*([^\s\-\>\|\[\]]+)$/);
    if (labeledEdgeMatch) {
      const [_, src, rel, tgt] = labeledEdgeMatch;
      const srcMeta = (nodesMeta && nodesMeta[src]) || null;
      const tgtMeta = (nodesMeta && nodesMeta[tgt]) || null;
      const srcTitle = srcMeta && srcMeta.file ? `Jump to definition: ${srcMeta.file.split(/[\\/]/).pop() || srcMeta.file}:${srcMeta.line}` : `Click to jump to definition of ${src}`;
      const tgtTitle = tgtMeta && tgtMeta.file ? `Jump to definition: ${tgtMeta.file.split(/[\\/]/).pop() || tgtMeta.file}:${tgtMeta.line}` : `Click to jump to definition of ${tgt}`;
      return `<div class="hierarchy-row"><span class="hierarchy-sym-btn" data-symbol="${escapeHtml(src)}" title="${escapeHtml(srcTitle)}">${escapeHtml(src)}</span><span class="hierarchy-arrow">&rarr; ${escapeHtml(rel)} &rarr;</span><span class="hierarchy-sym-btn" data-symbol="${escapeHtml(tgt)}" title="${escapeHtml(tgtTitle)}">${escapeHtml(tgt)}</span></div>`;
    }

    // 2. Unlabeled edge: A --> B
    const unlabeledEdgeMatch = trimmed.match(/^([^\s\-\>\|\[\]]+)\s*(?:-->|---\>)\s*([^\s\-\>\|\[\]]+)$/);
    if (unlabeledEdgeMatch) {
      const [_, src, tgt] = unlabeledEdgeMatch;
      const srcMeta = (nodesMeta && nodesMeta[src]) || null;
      const tgtMeta = (nodesMeta && nodesMeta[tgt]) || null;
      const srcTitle = srcMeta && srcMeta.file ? `Jump to definition: ${srcMeta.file.split(/[\\/]/).pop() || srcMeta.file}:${srcMeta.line}` : `Click to jump to definition of ${src}`;
      const tgtTitle = tgtMeta && tgtMeta.file ? `Jump to definition: ${tgtMeta.file.split(/[\\/]/).pop() || tgtMeta.file}:${tgtMeta.line}` : `Click to jump to definition of ${tgt}`;
      return `<div class="hierarchy-row"><span class="hierarchy-sym-btn" data-symbol="${escapeHtml(src)}" title="${escapeHtml(srcTitle)}">${escapeHtml(src)}</span><span class="hierarchy-arrow">&rarr;</span><span class="hierarchy-sym-btn" data-symbol="${escapeHtml(tgt)}" title="${escapeHtml(tgtTitle)}">${escapeHtml(tgt)}</span></div>`;
    }

    // 3. Node definition: A["function: A"] or A[A]
    const nodeMatch = trimmed.match(/^([^\s\[\]]+)\s*\[(?:"|')?([^"'\]]+)(?:"|')?\]$/);
    if (nodeMatch) {
      const [_, sym, label] = nodeMatch;
      const meta = (nodesMeta && nodesMeta[sym]) || null;
      const title = meta && meta.file ? `Jump to definition: ${meta.file.split(/[\\/]/).pop() || meta.file}:${meta.line}` : `Click to jump to definition of ${sym}`;
      return `<div class="hierarchy-row"><span class="hierarchy-sym-btn" data-symbol="${escapeHtml(sym)}" title="${escapeHtml(title)}">${escapeHtml(label || sym)}</span></div>`;
    }

    return `<div class="hierarchy-raw-line">${escapeHtml(line)}</div>`;
  }

  function renderMermaidGraph(mermaid, targetFile = null, nodesMeta = null) {
    if (!mermaidCode) return;
    if (nodesMeta) {
      currentGraphNodesMeta = { ...currentGraphNodesMeta, ...nodesMeta };
    }
    const activeTarget = targetFile !== undefined ? targetFile : selectedRepoFile;
    if (mermaid && mermaid.trim() && mermaid.trim() !== 'graph TD') {
      const lines = mermaid.split(/\r?\n/);
      const rowsHtml = lines.map(l => renderInteractiveHierarchyLine(l, currentGraphNodesMeta)).join('');
      mermaidCode.innerHTML = rowsHtml;
      mermaidCode.style.display = 'block';
    } else {
      mermaidCode.style.display = 'block';
      if (activeTarget) {
        const shortName = activeTarget.split(/[\\/]/).pop() || activeTarget;
        mermaidCode.innerHTML = `<div class="tree-empty-state">No call relationships or symbols found for '${escapeHtml(shortName)}'.<br>Click 'Scan & Index Workspace' to analyze this file or select another file.</div>`;
      } else {
        mermaidCode.innerHTML = `<div class="tree-empty-state">No SQLite code graph available for this repository.<br>Click 'Scan & Index Workspace' to analyze codebase structure.</div>`;
      }
    }
  }

  async function fetchMermaidGraph(targetFile = null) {
    const path = inputRepoPath ? inputRepoPath.value.trim() : currentRepoPath;
    if (!path) return;
    const activeTarget = targetFile !== undefined ? targetFile : selectedRepoFile;
    if (isVsCode) {
      vscode.postMessage({ command: 'getMermaidGraph', path, file: activeTarget });
    } else {
      try {
        const fileQuery = activeTarget ? `&file=${encodeURIComponent(activeTarget)}` : '';
        const graphRes = await fetch(`${BACKEND_URL}/api/graph/mermaid?path=${encodeURIComponent(path)}${fileQuery}`);
        if (graphRes.ok) {
          const graphData = await graphRes.json();
          renderMermaidGraph(graphData.mermaid, activeTarget, graphData.nodes_meta);
        }
      } catch (err) { }
    }
  }

  function setSelectedRepoFile(filePath, fileName = null) {
    if (!filePath || selectedRepoFile === filePath) {
      selectedRepoFile = null;
      if (repoFileTree) {
        repoFileTree.querySelectorAll('.tree-file.selected-file').forEach(el => el.classList.remove('selected-file'));
      }
      if (btnClearFileSelection) btnClearFileSelection.style.display = 'none';
      if (hierarchyCardTitle) hierarchyCardTitle.textContent = 'Call Hierarchy & Relationships';
      fetchMermaidGraph(null);
      showToast('Cleared file filter // Showing entire repository hierarchy', 'info');
      return;
    }

    selectedRepoFile = filePath;
    const displayName = fileName || filePath.split(/[\\/]/).pop() || filePath;

    if (repoFileTree) {
      repoFileTree.querySelectorAll('.tree-file').forEach(el => {
        const itemRel = el.getAttribute('data-file-path');
        const itemFull = el.getAttribute('data-full-path');
        if (itemRel === filePath || itemFull === filePath || (itemRel && itemRel.endsWith(displayName))) {
          el.classList.add('selected-file');
        } else {
          el.classList.remove('selected-file');
        }
      });
    }

    if (btnClearFileSelection) {
      btnClearFileSelection.style.display = 'inline-block';
    }
    if (hierarchyCardTitle) {
      hierarchyCardTitle.textContent = `Call Hierarchy & Relationships // ${displayName}`;
    }

    showToast(`Selected: ${displayName} // Hierarchy filtered`, 'success');
    fetchMermaidGraph(selectedRepoFile);
  }

  if (btnClearFileSelection) {
    btnClearFileSelection.addEventListener('click', () => {
      setSelectedRepoFile(null);
    });
  }

  function resetRepoGraphState() {
    renderMermaidGraph(null, selectedRepoFile);
    if (repoStatusLabel) {
      repoStatusLabel.textContent = currentRepoPath ? "Ready to scan (Path updated)" : "No repository selected";
    }
    if (repoSecurityLabel) {
      repoSecurityLabel.textContent = "Ready to audit";
      repoSecurityLabel.style.color = "var(--text-muted)";
    }
    if (repoSecurityDetails) {
      repoSecurityDetails.style.display = "none";
      repoSecurityDetails.innerHTML = "";
    }
  }

  if (inputRepoPath) {
    inputRepoPath.value = currentRepoPath;
    const syncRepoPath = async () => {
      const val = inputRepoPath.value.trim();
      currentRepoPath = val;
      resetRepoGraphState();
      if (isVsCode) {
        vscode.postMessage({ command: 'setRepoPath', path: currentRepoPath });
      } else {
        try {
          const res = await fetch(`${BACKEND_URL}/api/settings`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ repo_path: currentRepoPath })
          });
          if (res.ok) {
            const sData = await res.json();
            if (sData.repo_path && sData.repo_path !== currentRepoPath) {
              currentRepoPath = sData.repo_path;
              inputRepoPath.value = currentRepoPath;
            }
          }
        } catch (e) { }
      }
      if (currentRepoPath) {
        localStorage.setItem('codebase_copilot_repo_path', currentRepoPath);
      } else {
        localStorage.removeItem('codebase_copilot_repo_path');
      }
    };
    inputRepoPath.addEventListener('input', syncRepoPath);
    inputRepoPath.addEventListener('change', async () => {
      await syncRepoPath();
      loadRepoTree(currentRepoPath);
    });
  }

  // Reset button: completely clears the path bar, storage, backend, and resets tree view
  if (btnResetRepoPath) {
    btnResetRepoPath.addEventListener('click', () => {
      currentRepoPath = '';
      selectedRepoFile = null;
      if (btnClearFileSelection) btnClearFileSelection.style.display = 'none';
      if (hierarchyCardTitle) hierarchyCardTitle.textContent = 'Call Hierarchy & Relationships';
      if (inputRepoPath) inputRepoPath.value = '';
      localStorage.removeItem('codebase_copilot_repo_path');
      resetRepoGraphState();
      if (repoFileTree) {
        repoFileTree.innerHTML = '<div class="tree-empty-state"><span class="pixel-glyph">»</span> No repository selected. Enter a path above or click BROWSE.</div>';
      }
      if (repoTreeCount) repoTreeCount.textContent = '0 Files';
      if (repoStatsText) repoStatsText.textContent = 'Indexed: 0 Files // 0 Functions & Classes // 0 Vector Chunks';
      if (isVsCode) {
        vscode.postMessage({ command: 'resetRepoPath' });
      } else {
        fetch(`${BACKEND_URL}/api/settings`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ repo_path: '' })
        }).catch(() => { });
      }
      showToast('Repository path cleared', 'info');
    });
  }

  // Browse button: explorer folder selection in VS Code or browser directory picker
  if (btnBrowseRepoPath) {
    btnBrowseRepoPath.addEventListener('click', async () => {
      if (isVsCode) {
        vscode.postMessage({ command: 'browseRepoFolder' });
      } else {
        if (window.showDirectoryPicker) {
          try {
            const dirHandle = await window.showDirectoryPicker({ mode: 'read' });
            const folderName = dirHandle.name;
            const tree = await readDirectoryHandle(dirHandle);
            inputRepoPath.value = folderName;
            currentRepoPath = folderName;
            resetRepoGraphState();
            showToast(`Selected repository: ${folderName}`, 'success');
            renderRepoTree(tree);

            try {
              const res = await fetch(`${BACKEND_URL}/api/settings`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ repo_path: currentRepoPath })
              });
              if (res.ok) {
                const sData = await res.json();
                if (sData.repo_path) {
                  currentRepoPath = sData.repo_path;
                  inputRepoPath.value = currentRepoPath;
                  localStorage.setItem('codebase_copilot_repo_path', currentRepoPath);
                  loadRepoTree(currentRepoPath);
                }
              }
            } catch (err) { }
            return;
          } catch (err) {
            if (err && err.name === 'AbortError') return;
          }
        }
        if (repoFolderPicker) {
          repoFolderPicker.click();
        }
      }
    });
  }

  if (repoFolderPicker) {
    repoFolderPicker.addEventListener('change', async (e) => {
      const files = Array.from(e.target.files || []);
      if (files.length === 0) return;
      const firstRel = files[0].webkitRelativePath || '';
      const rootName = firstRel.split('/')[0] || 'Selected Folder';
      inputRepoPath.value = rootName;
      currentRepoPath = rootName;
      resetRepoGraphState();
      showToast(`Selected repository: ${rootName} (${files.length} files)`, 'success');
      const tree = buildTreeFromFiles(files);
      renderRepoTree(tree);

      try {
        const res = await fetch(`${BACKEND_URL}/api/settings`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ repo_path: currentRepoPath })
        });
        if (res.ok) {
          const sData = await res.json();
          if (sData.repo_path) {
            currentRepoPath = sData.repo_path;
            inputRepoPath.value = currentRepoPath;
            localStorage.setItem('codebase_copilot_repo_path', currentRepoPath);
            loadRepoTree(currentRepoPath);
          }
        }
      } catch (err) { }
    });
  }

  function formatBytes(bytes) {
    if (!bytes) return '0 B';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1048576).toFixed(1) + ' MB';
  }

  async function readDirectoryHandle(dirHandle, relativePath = '') {
    const EXCLUDED_DIRS = new Set([
      '__pycache__', '.git', '.github', '.vscode', '.idea', '.gemini',
      'venv', '.venv', 'env', 'node_modules', 'dist', 'build',
      '.pytest_cache', '.mypy_cache', '.ruff_cache', '.coverage', 'htmlcov'
    ]);
    const EXCLUDED_FILES = new Set(['.DS_Store', 'Thumbs.db', 'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock']);
    const EXCLUDED_EXTS = new Set(['pyc', 'pyo', 'pyd', 'log', 'tmp', 'swp', 'bak']);

    const entries = [];
    for await (const [name, handle] of dirHandle.entries()) {
      if (name.startsWith('.') && name !== '.gitignore') continue;
      const itemPath = relativePath ? `${relativePath}/${name}` : name;
      if (handle.kind === 'directory') {
        if (EXCLUDED_DIRS.has(name)) continue;
        const children = await readDirectoryHandle(handle, itemPath);
        if (children.length > 0) {
          entries.push({
            name,
            type: 'directory',
            path: itemPath,
            children
          });
        }
      } else if (handle.kind === 'file') {
        const ext = name.includes('.') ? name.split('.').pop().toLowerCase() : '';
        if (EXCLUDED_FILES.has(name) || EXCLUDED_EXTS.has(ext)) continue;
        try {
          const fileObj = await handle.getFile();
          entries.push({
            name,
            type: 'file',
            path: itemPath,
            ext,
            size: formatBytes(fileObj.size),
            bytes: fileObj.size,
            lines: 0
          });
        } catch {
          entries.push({ name, type: 'file', path: itemPath, ext, size: '0 B', bytes: 0, lines: 0 });
        }
      }
    }
    entries.sort((a, b) => {
      if (a.type !== b.type) return a.type === 'directory' ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
    return entries;
  }

  function buildTreeFromFiles(files) {
    const EXCLUDED_DIRS = new Set([
      '__pycache__', '.git', '.github', '.vscode', '.idea', '.gemini',
      'venv', '.venv', 'env', 'node_modules', 'dist', 'build',
      '.pytest_cache', '.mypy_cache', '.ruff_cache', '.coverage', 'htmlcov'
    ]);
    const EXCLUDED_FILES = new Set(['.DS_Store', 'Thumbs.db', 'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock']);
    const EXCLUDED_EXTS = new Set(['pyc', 'pyo', 'pyd', 'log', 'tmp', 'swp', 'bak']);

    const rootMap = {};
    for (const file of files) {
      const parts = (file.webkitRelativePath || file.name).split('/');
      const subParts = parts.length > 1 ? parts.slice(1) : parts;
      if (subParts.some(p => EXCLUDED_DIRS.has(p) || (p.startsWith('.') && p !== '.gitignore'))) continue;
      const fileName = subParts[subParts.length - 1];
      const ext = fileName.includes('.') ? fileName.split('.').pop().toLowerCase() : '';
      if (EXCLUDED_FILES.has(fileName) || EXCLUDED_EXTS.has(ext)) continue;

      let current = rootMap;
      for (let i = 0; i < subParts.length - 1; i++) {
        const dir = subParts[i];
        if (!current[dir]) {
          current[dir] = { __type: 'directory', __children: {} };
        }
        current = current[dir].__children;
      }
      current[fileName] = {
        __type: 'file',
        name: fileName,
        path: subParts.join('/'),
        ext,
        size: formatBytes(file.size),
        bytes: file.size,
        lines: 0
      };
    }

    function mapToTree(mapObj, parentPath = '') {
      const result = [];
      for (const key of Object.keys(mapObj).sort()) {
        const item = mapObj[key];
        const itemPath = parentPath ? `${parentPath}/${key}` : key;
        if (item.__type === 'directory') {
          const children = mapToTree(item.__children, itemPath);
          if (children.length > 0) {
            result.push({
              name: key,
              type: 'directory',
              path: itemPath,
              children
            });
          }
        } else {
          result.push(item);
        }
      }
      result.sort((a, b) => {
        if (a.type !== b.type) return a.type === 'directory' ? -1 : 1;
        return a.name.localeCompare(b.name);
      });
      return result;
    }

    return mapToTree(rootMap);
  }

  // File tree loader & hierarchical pixelated renderer
  async function loadRepoTree(targetPath) {
    const path = targetPath !== undefined ? targetPath : (inputRepoPath ? inputRepoPath.value.trim() : currentRepoPath);
    if (!path) {
      if (repoFileTree) {
        repoFileTree.innerHTML = '<div class="tree-empty-state"><span class="pixel-glyph">»</span> No repository selected. Enter a path above or click BROWSE.</div>';
      }
      if (repoTreeCount) repoTreeCount.textContent = '0 Files';
      return;
    }
    if (repoTreeCount) repoTreeCount.textContent = 'Loading...';
    if (isVsCode) {
      vscode.postMessage({ command: 'getRepoTree', path });
    } else {
      try {
        const res = await fetch(`${BACKEND_URL}/api/repo/tree?path=${encodeURIComponent(path || '')}`);
        if (res.ok) {
          const data = await res.json();
          renderRepoTree(data.tree, data.stats, data.root || path);
        } else {
          renderTreeError(`Server response (${res.status})`);
        }
      } catch (err) {
        renderTreeError(`Backend unavailable: ${err.message}`);
      }
    }
  }

  let pendingFileOpenTimer = null;

  function openFileInIdeOrSystem(fullPath, line = 0, symbol = '') {
    const repoHint = (inputRepoPath ? inputRepoPath.value.trim() : '') || currentRepoPath || '';
    let targetPath = fullPath || '';
    if (targetPath && !/^[a-zA-Z]:[\\\/]/.test(targetPath) && !targetPath.startsWith('/') && repoHint) {
      const sep = repoHint.includes('/') && !repoHint.includes('\\') ? '/' : '\\';
      targetPath = `${repoHint.replace(/[\\\/]+$/, '')}${sep}${targetPath.replace(/^[\\\/]+/, '')}`;
    }
    const fileName = targetPath ? (targetPath.split(/[\\/]/).pop() || targetPath) : (symbol || 'file');
    const toastMsg = line > 0 ? `Opening ${fileName}:${line}...` : `Opening ${fileName}...`;

    showToast(toastMsg, 'info');

    if (isVsCode) {
      vscode.postMessage({ command: 'openFile', path: targetPath, line: line || 0, symbol: symbol || '', repo_path: repoHint });
    } else {
      fetch(`${BACKEND_URL}/api/file/open`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file_path: targetPath, repo_path: repoHint })
      })
        .then(res => res.json())
        .then(data => {
          if (data.status === 'success') {
            showToast(`Opened: ${fileName}`, 'success');
          } else {
            showToast(`Could not open file: ${data.detail || data.error || 'Unknown error'}`, 'error');
          }
        })
        .catch(err => {
          showToast(`Error opening file: ${err.message}`, 'error');
        });
    }
  }

  function renderRepoTree(treeNodes, stats, rootPath) {
    if (!repoFileTree) return;
    if (!treeNodes || treeNodes.length === 0) {
      repoFileTree.innerHTML = '<div class="tree-empty-state"><span class="pixel-glyph">»</span> No files found in directory.</div>';
      if (repoTreeCount) repoTreeCount.textContent = '0 Files';
      return;
    }

    currentRepoFilesList = [];
    currentRepoDirsList = [];
    let fileCount = 0;
    function countFiles(nodes) {
      if (!nodes) return;
      nodes.forEach(n => {
        if (n.type === 'directory') {
          currentRepoDirsList.push(n);
          countFiles(n.children || []);
        } else {
          fileCount++;
          currentRepoFilesList.push(n);
        }
      });
    }
    countFiles(treeNodes);
    if (detectedSystem) {
      applySystemInfo(detectedSystem);
    }

    if (repoTreeCount) {
      repoTreeCount.textContent = stats ? `${stats.total_files} Files` : `${fileCount} Files`;
    }

    function getTagBadge(ext) {
      const cleanExt = (ext || '').toLowerCase();
      switch (cleanExt) {
        case 'py': return '<span class="pixel-tag tag-py">[PY]</span>';
        case 'ts': return '<span class="pixel-tag tag-ts">[TS]</span>';
        case 'js': return '<span class="pixel-tag tag-js">[JS]</span>';
        case 'json': return '<span class="pixel-tag tag-json">[JSON]</span>';
        case 'html': return '<span class="pixel-tag tag-html">[HTML]</span>';
        case 'css': return '<span class="pixel-tag tag-css">[CSS]</span>';
        case 'md': return '<span class="pixel-tag tag-md">[DOC]</span>';
        case 'sh': return '<span class="pixel-tag tag-sh">[SH]</span>';
        case 'sql': return '<span class="pixel-tag tag-sql">[SQL]</span>';
        case 'yaml':
        case 'yml':
        case 'toml': return '<span class="pixel-tag tag-cfg">[CFG]</span>';
        default: return `<span class="pixel-tag tag-file">[${cleanExt.slice(0, 4).toUpperCase() || 'FILE'}]</span>`;
      }
    }

    function buildNodeHtml(node, depth = 0) {
      const indent = depth * 14;
      if (node.type === 'directory') {
        const childrenHtml = (node.children || []).map(c => buildNodeHtml(c, depth + 1)).join('');
        const childCount = (node.children || []).length;
        return `
          <div class="tree-dir-group" data-expanded="true">
            <div class="tree-node tree-folder" style="padding-left: ${indent + 6}px;" title="Click to collapse/expand">
              <span class="tree-toggle-icon">[-]</span>
              <span class="tree-folder-name">${escapeHtml(node.name)}</span>
              <span class="tree-item-count">(${childCount})</span>
            </div>
            <div class="tree-children">
              ${childrenHtml}
            </div>
          </div>
        `;
      } else {
        const badge = getTagBadge(node.ext);
        const linesStr = node.lines ? `${node.lines}L` : '';
        const sizeStr = node.size || '';
        const metaStr = [linesStr, sizeStr].filter(Boolean).join(' • ');

        // Resolve absolute full path
        const effectiveRoot = (rootPath || (inputRepoPath ? inputRepoPath.value.trim() : '') || currentRepoPath || '').trim();
        let fullPath = node.full_path;
        if (!fullPath) {
          if (effectiveRoot) {
            const sep = effectiveRoot.includes('/') && !effectiveRoot.includes('\\') ? '/' : '\\';
            const cleanRoot = effectiveRoot.replace(/[\\\/]+$/, '');
            const cleanRel = (node.path || '').replace(/^[\\\/]+/, '');
            fullPath = `${cleanRoot}${sep}${cleanRel}`;
          } else {
            fullPath = node.path;
          }
        }

        const isSelected = selectedRepoFile && (
          node.path === selectedRepoFile ||
          fullPath === selectedRepoFile ||
          node.name === selectedRepoFile ||
          selectedRepoFile.endsWith(node.name)
        );

        return `
          <div class="tree-node tree-file ${isSelected ? 'selected-file' : ''}" style="padding-left: ${indent + 22}px;" data-file-path="${escapeHtml(node.path)}" data-full-path="${escapeHtml(fullPath)}" title="${escapeHtml(fullPath)}&#10;Single-click: Select file to view call hierarchy&#10;Double-click: Open file in IDE">
            ${badge}
            <span class="tree-file-name">${escapeHtml(node.name)}</span>
            ${metaStr ? `<span class="tree-file-meta">${metaStr}</span>` : ''}
          </div>
        `;
      }
    }

    repoFileTree.innerHTML = treeNodes.map(n => buildNodeHtml(n, 0)).join('');

    // Attach folder collapse/expand listeners
    repoFileTree.querySelectorAll('.tree-folder').forEach(folder => {
      folder.addEventListener('click', (e) => {
        e.stopPropagation();
        const group = folder.closest('.tree-dir-group');
        if (!group) return;
        const isExpanded = group.getAttribute('data-expanded') === 'true';
        group.setAttribute('data-expanded', isExpanded ? 'false' : 'true');
        const icon = folder.querySelector('.tree-toggle-icon');
        if (icon) icon.textContent = isExpanded ? '[+]' : '[-]';
        const children = group.querySelector('.tree-children');
        if (children) children.style.display = isExpanded ? 'none' : 'block';
      });
    });

    // Single-click selects file to focus Call Hierarchy, Double-click opens file in IDE / OS
    let fileClickTimer = null;
    repoFileTree.querySelectorAll('.tree-file').forEach(fileItem => {
      fileItem.addEventListener('click', (e) => {
        e.stopPropagation();
        const filePath = fileItem.getAttribute('data-file-path') || '';
        const fullPath = fileItem.getAttribute('data-full-path') || filePath;
        const fileName = fileItem.querySelector('.tree-file-name')?.textContent || filePath;

        if (fileClickTimer) {
          clearTimeout(fileClickTimer);
          fileClickTimer = null;
        }
        fileClickTimer = setTimeout(() => {
          fileClickTimer = null;
          if (selectedRepoFile === filePath || selectedRepoFile === fullPath) {
            setSelectedRepoFile(null); // Toggle deselect
          } else {
            setSelectedRepoFile(filePath, fileName);
          }
        }, 180);
      });

      fileItem.addEventListener('dblclick', (e) => {
        e.stopPropagation();
        if (fileClickTimer) {
          clearTimeout(fileClickTimer);
          fileClickTimer = null;
        }
        let fullPath = fileItem.getAttribute('data-full-path') || fileItem.getAttribute('data-file-path');
        const currentRoot = (inputRepoPath ? inputRepoPath.value.trim() : '') || currentRepoPath || '';
        if (fullPath && !/^[a-zA-Z]:[\\\/]/.test(fullPath) && !fullPath.startsWith('/') && currentRoot) {
          const sep = currentRoot.includes('/') && !currentRoot.includes('\\') ? '/' : '\\';
          fullPath = `${currentRoot.replace(/[\\\/]+$/, '')}${sep}${fullPath.replace(/^[\\\/]+/, '')}`;
        }
        if (fullPath) {
          openFileInIdeOrSystem(fullPath);
        }
      });
    });
  }

  function renderTreeError(errText) {
    if (!repoFileTree) return;
    repoFileTree.innerHTML = `
      <div class="tree-empty-state" style="color: var(--primary-terracotta);">
        <span class="pixel-glyph">!</span> ${escapeHtml(errText)}
        <div style="margin-top: 8px;">
          <button class="pixel-mini-btn" id="btn-tree-retry">Try Again</button>
        </div>
      </div>
    `;
    const retryBtn = document.getElementById('btn-tree-retry');
    if (retryBtn) {
      retryBtn.addEventListener('click', () => loadRepoTree());
    }
  }

  if (btnRefreshTree) {
    btnRefreshTree.addEventListener('click', () => {
      const rawInput = inputRepoPath ? inputRepoPath.value.trim() : '';
      const path = rawInput || (inputRepoPath ? '' : currentRepoPath);
      if (!path || path === '.') {
        showToast('Please select a repository', 'warning');
        return;
      }
      loadRepoTree();
      showToast('Refreshing repository files tree...', 'info');
    });
  }

  let allExpanded = true;
  if (btnExpandTree) {
    btnExpandTree.addEventListener('click', () => {
      allExpanded = !allExpanded;
      repoFileTree.querySelectorAll('.tree-dir-group').forEach(group => {
        group.setAttribute('data-expanded', allExpanded ? 'true' : 'false');
        const icon = group.querySelector('.tree-toggle-icon');
        if (icon) icon.textContent = allExpanded ? '[-]' : '[+]';
        const children = group.querySelector('.tree-children');
        if (children) children.style.display = allExpanded ? 'block' : 'none';
      });
    });
  }

  function applySecurityResults(data) {
    if (!repoSecurityLabel) return;
    if (!data) {
      repoSecurityLabel.textContent = '✓ Zero Leaked Secrets Detected';
      repoSecurityLabel.style.color = '#4ade80';
      if (repoSecurityDetails) repoSecurityDetails.style.display = 'none';
      return;
    }

    const count = data.findings_count !== undefined ? data.findings_count : (data.findings ? data.findings.length : 0);
    if (count === 0) {
      repoSecurityLabel.textContent = '✓ Zero Leaked Secrets Detected';
      repoSecurityLabel.style.color = '#4ade80';
      if (repoSecurityDetails) {
        repoSecurityDetails.style.display = 'none';
        repoSecurityDetails.innerHTML = '';
      }
    } else {
      repoSecurityLabel.textContent = `⚠ ${count} Leaked Secret${count === 1 ? '' : 's'} Flagged`;
      repoSecurityLabel.style.color = '#f87171';
      if (repoSecurityDetails) {
        repoSecurityDetails.style.display = 'block';
        const findings = data.findings || [];
        repoSecurityDetails.innerHTML = findings.map(f => `
          <div class="sec-finding-item">
            <div class="sec-finding-header">
              <span class="sec-finding-file">${escapeHtml(f.file)}${f.line ? `:${f.line}` : ''}</span>
              <span class="${f.severity === 'CRITICAL' ? 'sec-badge-critical' : 'sec-badge-high'}">${escapeHtml(f.severity || 'HIGH')}</span>
            </div>
            <div class="sec-finding-detail">${escapeHtml(f.detail || f.type || 'Secret pattern detected')}</div>
          </div>
        `).join('');
      }
    }
  }

  async function loadSecurityAudit(targetPath) {
    const rawInput = inputRepoPath ? inputRepoPath.value.trim() : '';
    const path = targetPath || rawInput || (inputRepoPath ? '' : currentRepoPath);
    if (!path || path === '.') {
      if (repoSecurityLabel) {
        repoSecurityLabel.textContent = 'No repository selected';
        repoSecurityLabel.style.color = 'var(--text-muted)';
      }
      return;
    }
    if (repoSecurityLabel) {
      repoSecurityLabel.textContent = 'Auditing secrets...';
      repoSecurityLabel.style.color = 'var(--text-muted)';
    }

    if (isVsCode) {
      vscode.postMessage({ command: 'getSecurityAudit', path });
    } else {
      try {
        const res = await fetch(`${BACKEND_URL}/api/repo/security-audit?path=${encodeURIComponent(path || '')}`);
        if (res.ok) {
          const secData = await res.json();
          applySecurityResults(secData);
        } else {
          applySecurityResults({ findings_count: 0 });
        }
      } catch (err) {
        if (repoSecurityLabel) {
          repoSecurityLabel.textContent = 'Audit service offline';
          repoSecurityLabel.style.color = 'var(--text-muted)';
        }
      }
    }
  }

  if (btnSecurityScan) {
    btnSecurityScan.addEventListener('click', () => {
      const rawInput = inputRepoPath ? inputRepoPath.value.trim() : '';
      const path = rawInput || (inputRepoPath ? '' : currentRepoPath);
      if (!path || path === '.') {
        showToast('Please select a repository', 'warning');
        if (repoSecurityLabel) {
          repoSecurityLabel.textContent = 'No repository selected';
          repoSecurityLabel.style.color = 'var(--text-muted)';
        }
        return;
      }
      showToast('Running security secret audit...', 'info');
      loadSecurityAudit(path);
    });
  }

  async function scanRepository() {
    const rawInput = inputRepoPath ? inputRepoPath.value.trim() : '';
    const path = rawInput || (inputRepoPath ? '' : currentRepoPath);
    if (!path || path === '.') {
      showToast('Please select a repository', 'warning');
      if (repoStatusLabel) {
        repoStatusLabel.textContent = 'No repository selected';
      }
      return;
    }
    currentRepoPath = path;
    localStorage.setItem('codebase_copilot_repo_path', currentRepoPath);
    if (repoStatusLabel) {
      const activeFileShort = selectedRepoFile ? (selectedRepoFile.split(/[\\/]/).pop() || selectedRepoFile) : null;
      repoStatusLabel.textContent = activeFileShort
        ? `Scanning AST & Call Graph (${activeFileShort})...`
        : 'Scanning AST & Call Graph...';
    }
    if (btnScanRepo) btnScanRepo.disabled = true;

    if (isVsCode) {
      vscode.postMessage({ command: 'scanWorkspace', path, file: selectedRepoFile });
      setTimeout(() => {
        if (btnScanRepo) btnScanRepo.disabled = false;
      }, 15000);
    } else {
      try {
        const scanRes = await fetch(`${BACKEND_URL}/api/index/scan`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ workspace_path: path })
        });
        const fileQuery = selectedRepoFile ? `&file=${encodeURIComponent(selectedRepoFile)}` : '';
        const graphRes = await fetch(`${BACKEND_URL}/api/graph/mermaid?path=${encodeURIComponent(path)}${fileQuery}`);
        const secRes = await fetch(`${BACKEND_URL}/api/repo/security-audit?path=${encodeURIComponent(path)}`);

        if (scanRes.ok) {
          const scanData = await scanRes.json();
          const graphData = graphRes.ok ? await graphRes.json() : { mermaid: '', available: false };
          const secData = secRes.ok ? await secRes.json() : null;
          applyScanResults(scanData, graphData.mermaid, secData, selectedRepoFile);
        }
      } catch (err) {
        if (repoStatusLabel) repoStatusLabel.textContent = `Scan failed: ${err.message}`;
      } finally {
        if (btnScanRepo) btnScanRepo.disabled = false;
      }
    }
  }

  function applyScanResults(scanData, mermaid, security, focusedFile = null, nodesMeta = null) {
    if (btnScanRepo) btnScanRepo.disabled = false;
    if (repoStatsText && scanData) {
      repoStatsText.textContent = `Indexed: ${scanData.filesCount ?? 0} Files // ${scanData.symbolsCount ?? 0} Functions & Classes // ${scanData.chunksCount ?? 0} Vector Chunks`;
    }
    if (repoStatusLabel) {
      repoStatusLabel.textContent = 'Indexed & Synced (SQLite Graph Active)';
    }
    renderMermaidGraph(mermaid, focusedFile !== undefined ? focusedFile : selectedRepoFile, nodesMeta);
    if (security) {
      applySecurityResults(security);
    }
  }

  if (btnScanRepo) btnScanRepo.addEventListener('click', scanRepository);

  // ==========================================
  // 7. Extension Host Message Listener (VS Code runtime)
  // ==========================================
  window.addEventListener('message', (event) => {
    const msg = event.data;
    if (!msg) return;

    switch (msg.command) {
      case 'injectPrompt': {
        const targetTab = msg.targetTab || 'chat';
        switchTab(targetTab);
        if (targetTab === 'chat') {
          if (chatInput) {
            chatInput.value = msg.text || '';
            chatInput.style.height = 'auto';
            chatInput.style.height = Math.min(chatInput.scrollHeight, 120) + 'px';
            chatInput.focus();
            if (msg.autoSend && msg.text && msg.text.trim()) {
              setTimeout(() => {
                dispatchChatMessage(msg.text.trim());
              }, 50);
            }
          }
        } else if (targetTab === 'agent') {
          const agentGoalInput = document.getElementById('agent-goal-input');
          if (agentGoalInput) {
            agentGoalInput.value = msg.text || '';
            agentGoalInput.focus();
          }
        }
        break;
      }

      case 'setBackendUrl':
        if (msg.url && typeof msg.url === 'string') {
          BACKEND_URL = msg.url.replace(/\/+$/, '');
        }
        if (msg.sessionToken) {
          SESSION_TOKEN = msg.sessionToken;
        }
        break;

      case 'setSessionToken':
        if (msg.token) {
          SESSION_TOKEN = msg.token;
        }
        break;

      case 'backendRestarted':
        if (restartPollInterval) {
          clearInterval(restartPollInterval);
          restartPollInterval = null;
        }
        if (msg.url && typeof msg.url === 'string') {
          BACKEND_URL = msg.url.replace(/\/+$/, '');
        }
        if (msg.sessionToken) {
          SESSION_TOKEN = msg.sessionToken;
        }
        showToast('Backend restarted successfully', 'success');
        testOllamaConnection();
        break;

      case 'backendRestartFailed':
        if (restartPollInterval) {
          clearInterval(restartPollInterval);
          restartPollInterval = null;
        }
        showToast(msg.error || 'Restart failed', 'error');
        break;

      case 'terminalCommandResult': {
        const data = msg.data;
        if (data) {
          if (data.stdout && data.stdout.trim()) {
            appendTerminalLog(data.stdout.trimEnd());
          }
          if (data.stderr && data.stderr.trim()) {
            appendTerminalLog(`[stderr] ${data.stderr.trimEnd()}`);
          }
          if (data.exit_code !== 0 && (!data.stdout || !data.stdout.trim()) && (!data.stderr || !data.stderr.trim())) {
            appendTerminalLog(`[error] Command exited with code ${data.exit_code} (no output)`);
          }
          appendTerminalLog(`[${data.os || detectedSystem.os}] process exited with code ${data.exit_code}`);
          if (agentTerminalStatus) {
            agentTerminalStatus.textContent = data.exit_code === 0 ? 'READY' : 'ERROR';
            agentTerminalStatus.style.color = data.exit_code === 0 ? '#10B981' : '#EF4444';
          }
        }
        break;
      }

      case 'ollamaStatusResult':
        applyOllamaStatus(msg.data);
        break;

      case 'settingsSaved':
        testOllamaConnection();
        showToast('Settings saved successfully', 'success');
        break;

      case 'streamToken':
        if (!isChatStreaming) {
          setChatGeneratingState(true);
        }
        if (msg.token && msg.token.startsWith('\n\n[Error:')) {
          setChatGeneratingState(false);
        }
        appendStreamToken(msg.token);
        break;

      case 'streamComplete':
        setChatGeneratingState(false);
        onStreamFinished(accumulatedRawText, msg.citations);
        if (msg.diff && (!stagedMultiFiles || stagedMultiFiles.length <= 1)) {
          const targetFilename = extractDiffFilename(msg.diff) || getPrimaryRepoFile();
          stageDiff(targetFilename, msg.diff);
        }
        break;

      case 'streamStopped':
        stopChatGeneration(false);
        break;

      case 'patchApplied': {
        const appliedFile = msg.file;
        showToast(`Patch applied cleanly to ${appliedFile}`, 'success');
        loadRepoTree(currentRepoPath);

        if (stagedMultiFiles && stagedMultiFiles.length > 0) {
          const matched = stagedMultiFiles.find(f => isSameFileReference(f.file_path, appliedFile));
          if (matched) {
            matched.applied = true;
          }
          const remaining = stagedMultiFiles.filter(f => !f.applied && f.diff);
          if (remaining.length > 0) {
            const nextFile = remaining[0];
            stageDiff(nextFile.file_path, nextFile.diff, stagedMultiFiles);
            showToast(`Applied ${appliedFile}. ${remaining.length} file(s) remaining for review.`, 'info', 3000);
          } else {
            showToast('All staged file patches applied cleanly!', 'success', 3500);
            clearStagedDiff();
          }
        } else {
          clearStagedDiff();
        }
        break;
      }

      case 'allPatchesApplied': {
        const results = msg.results || [];
        let allOk = true;
        for (const r of results) {
          const matched = stagedMultiFiles.find(f => isSameFileReference(f.file_path, r.file_path));
          if (matched) {
            if (r.applied) {
              matched.applied = true;
            } else {
              matched.error = r.message || 'Patch failed';
              allOk = false;
            }
          }
        }
        loadRepoTree(currentRepoPath);
        if (allOk || stagedMultiFiles.every(f => f.applied)) {
          showToast(`All ${msg.applied_count || results.length} patches applied successfully!`, 'success', 4000);
          clearStagedDiff();
        } else {
          const remaining = stagedMultiFiles.filter(f => !f.applied && f.diff);
          showToast(`Applied ${msg.applied_count || 0} of ${msg.total_count || results.length} patches. Check remaining files.`, 'warning', 4500);
          if (remaining.length > 0) {
            stageDiff(remaining[0].file_path, remaining[0].diff, stagedMultiFiles);
          }
        }
        break;
      }

      case 'fileOpened':
        if (msg.symbol) {
          showToast(`Jumped to ${msg.symbol} (line ${msg.line || 1})`, 'info', 1600);
        }
        break;

      case 'scanResult':
        if (btnScanRepo) btnScanRepo.disabled = false;
        applyScanResults(msg.status, msg.mermaid, msg.security, msg.file, msg.nodes_meta);
        loadRepoTree(currentRepoPath);
        break;

      case 'mermaidResult':
        renderMermaidGraph(msg.mermaid, msg.file, msg.nodes_meta);
        break;

      case 'scanError':
        if (btnScanRepo) btnScanRepo.disabled = false;
        if (repoStatusLabel) {
          repoStatusLabel.textContent = msg.error && msg.error.toLowerCase().includes('please select')
            ? 'No repository selected'
            : `Scan failed: ${msg.error || 'Unknown error'}`;
        }
        if (msg.error && msg.error.toLowerCase().includes('please select')) {
          showToast(msg.error, 'warning');
        }
        break;

      case 'securityAuditResult':
        applySecurityResults(msg.security);
        break;

      case 'activeFileChanged':
        if (msg.file) {
          activeEditorFile = msg.file;
          if (msg.line !== undefined) {
            activeEditorCursorLine = msg.line;
          }
          if (msg.content !== undefined) {
            activeEditorContent = msg.content;
          }
          if (detectedSystem) applySystemInfo(detectedSystem);
        }
        break;

      case 'cursorPositionChanged':
        if (msg.line !== undefined) {
          activeEditorCursorLine = msg.line;
        }
        if (msg.file) {
          activeEditorFile = msg.file;
        }
        if (msg.content !== undefined) {
          activeEditorContent = msg.content;
        }
        break;

      case 'activeFileContentChanged':
        if (msg.file && msg.file === activeEditorFile && msg.content !== undefined) {
          activeEditorContent = msg.content;
        }
        if (stagedDiffData && stagedDiffData.diff && msg.content !== undefined) {
          if (isSameFileReference(stagedDiffData.file, msg.file)) {
            checkDiffDivergence(msg.content);
          }
        }
        break;

      case 'diffRebased':
        handleRebaseResult(msg.result);
        break;

      case 'diffRebaseFailed':
        showToast(`Rebase failed: ${msg.error || 'Unknown error'}`, 'error');
        break;

      case 'fileSavedAndSynced': {
        const fname = msg.filename || (msg.file || '').split(/[\\/]/).pop() || 'File';
        const linesInfo = msg.lines ? ` (${msg.lines} lines)` : '';
        showToast(`AST Synced: ${fname}${linesInfo}`, 'info', 1800);
        const crumb = document.getElementById('crumb-status');
        if (crumb) {
          const originalHtml = crumb.innerHTML;
          crumb.innerHTML = '<span class="pill-dot" style="background:#4E9F3D;"></span>Synced';
          setTimeout(() => { crumb.innerHTML = originalHtml; }, 1800);
        }
        break;
      }

      case 'fileDeletedAndPurged': {
        const fname = msg.filename || (msg.file || '').split(/[\\/]/).pop() || 'File';
        showToast(`AST Purged: ${fname}`, 'info', 1800);
        break;
      }

      case 'cloudApiKeyLoaded': {
        if (inputApiKey) {
          if (msg.hasKey) {
            inputApiKey.placeholder = `Saved in OS Keychain (${msg.maskedKey || '••••••••'})`;
            inputApiKey.dataset.hasKey = 'true';
            inputApiKey.value = '';
          } else {
            inputApiKey.placeholder = 'Enter ollama.com API key (e.g. sk_...)';
            inputApiKey.dataset.hasKey = 'false';
          }
        }
        break;
      }

      case 'renderMermaid':
        if (mermaidCode) {
          if (msg.mermaid && msg.mermaid.trim() && msg.mermaid.trim() !== 'graph TD') {
            renderMermaidGraph(msg.mermaid, null, msg.nodes_meta);
          } else {
            mermaidCode.innerHTML = `<div class="tree-empty-state">No SQLite code graph available for this repository.<br>Click 'Scan & Index Workspace' to analyze codebase structure.</div>`;
          }
        }
        break;

      case 'repoPathCleared':
        currentRepoPath = '';
        if (inputRepoPath) inputRepoPath.value = '';
        localStorage.removeItem('codebase_copilot_repo_path');
        mentionAutocompleteItems = [];
        currentRepoFilesList = [];
        currentRepoDirsList = [];
        pinnedMentions = [];
        renderMentionChipsTray();
        hideMentionPopup();
        resetRepoGraphState();
        if (repoFileTree) {
          repoFileTree.innerHTML = '<div class="tree-empty-state"><span class="pixel-glyph">»</span> No repository selected. Enter a path above or click BROWSE.</div>';
        }
        if (repoTreeCount) repoTreeCount.textContent = '0 Files';
        if (repoStatsText) repoStatsText.textContent = 'Indexed: 0 Files // 0 Functions & Classes // 0 Vector Chunks';
        break;

      case 'repoPathSelected':
        if (msg.path) {
          const isSameRepo = currentRepoPath === msg.path;
          currentRepoPath = msg.path;
          if (inputRepoPath) inputRepoPath.value = currentRepoPath;
          localStorage.setItem('codebase_copilot_repo_path', currentRepoPath);
          if (!isSameRepo) {
            mentionAutocompleteItems = [];
            currentRepoFilesList = [];
            currentRepoDirsList = [];
            pinnedMentions = [];
            renderMentionChipsTray();
            hideMentionPopup();
            resetRepoGraphState();
            loadSecurityAudit(currentRepoPath);
            if (msg.tree) {
              renderRepoTree(msg.tree, msg.stats, msg.path);
            } else {
              loadRepoTree(currentRepoPath);
            }
          }
        }
        break;

      case 'repoTreeResult':
        if (msg.tree) {
          renderRepoTree(msg.tree, msg.stats, msg.root);
        } else if (msg.error) {
          renderTreeError(msg.error);
        }
        break;
      case 'systemInfoResult':
        if (msg.data) {
          applySystemInfo(msg.data);
        }
        break;
      case 'fileOpened':
        if (pendingFileOpenTimer) {
          clearTimeout(pendingFileOpenTimer);
          pendingFileOpenTimer = null;
        }
        const openName = (msg.file || '').split(/[\\/]/).pop() || 'File';
        showToast(`Opened ${openName} in IDE`, 'success');
        break;
    }
  });

  // ==========================================
  // Helper Utilities
  // ==========================================
  function computeTokenLCS(tokensA, tokensB) {
    const m = tokensA.length;
    const n = tokensB.length;
    const dp = Array.from({ length: m + 1 }, () => new Uint16Array(n + 1));
    for (let i = 0; i < m; i++) {
      for (let j = 0; j < n; j++) {
        if (tokensA[i] === tokensB[j]) {
          dp[i + 1][j + 1] = dp[i][j] + 1;
        } else {
          dp[i + 1][j + 1] = Math.max(dp[i + 1][j], dp[i][j + 1]);
        }
      }
    }

    let i = m, j = n;
    const spansA = [];
    const spansB = [];
    while (i > 0 || j > 0) {
      if (i > 0 && j > 0 && tokensA[i - 1] === tokensB[j - 1]) {
        spansA.unshift({ text: tokensA[i - 1], type: 'same' });
        spansB.unshift({ text: tokensB[j - 1], type: 'same' });
        i--; j--;
      } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
        spansB.unshift({ text: tokensB[j - 1], type: 'add' });
        j--;
      } else if (i > 0 && (j === 0 || dp[i][j - 1] < dp[i - 1][j])) {
        spansA.unshift({ text: tokensA[i - 1], type: 'del' });
        i--;
      }
    }
    return { spansA, spansB };
  }

  function renderWordDiffSpans(spans) {
    return spans.map(s => {
      if (s.type === 'del') {
        return `<span class="diff-word-del">${escapeHtml(s.text)}</span>`;
      } else if (s.type === 'add') {
        return `<span class="diff-word-add">${escapeHtml(s.text)}</span>`;
      }
      return escapeHtml(s.text);
    }).join('');
  }

  function renderDiffLines(diffText) {
    if (!diffText) return '';
    const rawLines = diffText.split('\n');
    const hunkHeaderRe = /^@@\s*(?:-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?)?.*?@@?/;

    let oldLine = 1;
    let newLine = 1;
    let insideHunk = false;
    let hunkIdx = 0;

    const parsedItems = [];
    let idx = 0;

    while (idx < rawLines.length) {
      const line = rawLines[idx];
      const hunkMatch = line.match(hunkHeaderRe);
      if (hunkMatch && line.startsWith('@@')) {
        if (hunkMatch[1]) oldLine = parseInt(hunkMatch[1], 10);
        if (hunkMatch[2]) newLine = parseInt(hunkMatch[2], 10);
        insideHunk = true;
        parsedItems.push({
          type: 'hunk_header',
          text: line,
          hunkIdx: hunkIdx++
        });
        idx++;
        continue;
      }

      if (line.startsWith('---') || line.startsWith('+++')) {
        parsedItems.push({ type: 'meta', text: line, rawIdx: idx });
        idx++;
        continue;
      }

      if (line.startsWith('<<<<<<<') || line.startsWith('=======') || line.startsWith('>>>>>>>')) {
        parsedItems.push({ type: 'conflict_marker', text: line, rawIdx: idx });
        idx++;
        continue;
      }

      if (line.startsWith('-')) {
        // Collect all consecutive deletion lines in this block
        const delLines = [];
        let dIdx = idx;
        while (dIdx < rawLines.length && rawLines[dIdx].startsWith('-') && !rawLines[dIdx].startsWith('---')) {
          delLines.push({
            type: 'del',
            text: rawLines[dIdx],
            rawIdx: dIdx
          });
          dIdx++;
        }

        // Collect all consecutive addition lines immediately following
        const addLines = [];
        let aIdx = dIdx;
        while (aIdx < rawLines.length && rawLines[aIdx].startsWith('+') && !rawLines[aIdx].startsWith('+++')) {
          addLines.push({
            type: 'add',
            text: rawLines[aIdx],
            rawIdx: aIdx
          });
          aIdx++;
        }

        const pairCount = Math.min(delLines.length, addLines.length);
        const tokenRe = /\w+|[^\w\s]|\s+/g;

        // Pair up index-by-index for intra-line word diffs
        for (let k = 0; k < pairCount; k++) {
          const oldContent = delLines[k].text.slice(1);
          const newContent = addLines[k].text.slice(1);
          const tokensOld = oldContent.match(tokenRe) || [oldContent];
          const tokensNew = newContent.match(tokenRe) || [newContent];
          const { spansA, spansB } = computeTokenLCS(tokensOld, tokensNew);
          delLines[k].spans = spansA;
          addLines[k].spans = spansB;
          delLines[k].pairedAdd = addLines[k];
          addLines[k].pairedDel = delLines[k];
        }

        // Assign line numbers and append deletions
        for (let k = 0; k < delLines.length; k++) {
          delLines[k].oldNum = insideHunk ? oldLine++ : '';
          delLines[k].newNum = '';
          parsedItems.push(delLines[k]);
        }

        // Assign line numbers and append additions
        for (let k = 0; k < addLines.length; k++) {
          addLines[k].oldNum = '';
          addLines[k].newNum = insideHunk ? newLine++ : '';
          parsedItems.push(addLines[k]);
        }

        idx = aIdx;
        continue;
      }

      if (line.startsWith('+')) {
        parsedItems.push({
          type: 'add',
          text: line,
          rawIdx: idx,
          oldNum: '',
          newNum: insideHunk ? newLine++ : ''
        });
        idx++;
        continue;
      }

      // Context line
      parsedItems.push({
        type: 'context',
        text: line,
        rawIdx: idx,
        oldNum: insideHunk ? oldLine++ : '',
        newNum: insideHunk ? newLine++ : ''
      });
      idx++;
    }

    // Split Mode Rendering (Side-by-Side 2 columns)
    if (diffViewMode === 'split') {
      let splitHtml = '<table class="diff-split-table"><tbody>';
      for (let i = 0; i < parsedItems.length; i++) {
        const item = parsedItems[i];
        if (item.type === 'hunk_header') {
          splitHtml += `
            <tr class="diff-hunk-header">
              <td colspan="2" style="padding: 2px 8px;">
                <div style="display:flex; justify-content:space-between; align-items:center; width: 100%;">
                  <span class="diff-hunk-title">... ${escapeHtml(item.text)}</span>
                  <div class="diff-hunk-actions">
                    <button type="button" class="btn-hunk-action btn-hunk-discard" data-hunk-idx="${item.hunkIdx}" title="Discard this hunk">✗ Discard</button>
                  </div>
                </div>
              </td>
            </tr>
          `;
        } else if (item.type === 'meta') {
          splitHtml += `
            <tr class="diff-meta">
              <td colspan="2" style="padding: 1px 8px;">${escapeHtml(item.text)}</td>
            </tr>
          `;
        } else if (item.type === 'del') {
          const leftContent = item.spans ? renderWordDiffSpans(item.spans) : escapeHtml(item.text.slice(1));
          const leftGutter = `<span class="diff-split-gutter">${item.oldNum}</span>`;

          if (item.pairedAdd) {
            const paired = item.pairedAdd;
            paired._splitRendered = true;
            const rightContent = paired.spans ? renderWordDiffSpans(paired.spans) : escapeHtml(paired.text.slice(1));
            const rightGutter = `<span class="diff-split-gutter">${paired.newNum}</span>`;
            splitHtml += `
              <tr class="diff-split-row">
                <td class="diff-split-col diff-split-col-left diff-del">${leftGutter}<span class="diff-marker">-</span><span class="diff-line-code">${leftContent}</span></td>
                <td class="diff-split-col diff-add">${rightGutter}<span class="diff-marker">+</span><span class="diff-line-code editable-diff-line" contenteditable="true" spellcheck="false" data-raw-idx="${paired.rawIdx}" title="Click to edit line before accepting">${rightContent}</span></td>
              </tr>
            `;
          } else {
            splitHtml += `
              <tr class="diff-split-row">
                <td class="diff-split-col diff-split-col-left diff-del">${leftGutter}<span class="diff-marker">-</span><span class="diff-line-code">${leftContent}</span></td>
                <td class="diff-split-col" style="background: rgba(0,0,0,0.06);"><span class="diff-split-gutter"></span></td>
              </tr>
            `;
          }
        } else if (item.type === 'add') {
          if (item._splitRendered) {
            continue; // Already rendered side-by-side with paired deletion
          }
          const rightContent = item.spans ? renderWordDiffSpans(item.spans) : escapeHtml(item.text.slice(1));
          const rightGutter = `<span class="diff-split-gutter">${item.newNum}</span>`;
          splitHtml += `
            <tr class="diff-split-row">
              <td class="diff-split-col diff-split-col-left" style="background: rgba(0,0,0,0.06);"><span class="diff-split-gutter"></span></td>
              <td class="diff-split-col diff-add">${rightGutter}<span class="diff-marker">+</span><span class="diff-line-code editable-diff-line" contenteditable="true" spellcheck="false" data-raw-idx="${item.rawIdx}" title="Click to edit line before accepting">${rightContent}</span></td>
            </tr>
          `;
        } else if (item.type === 'conflict_marker') {
          splitHtml += `
            <tr class="diff-split-row diff-conflict-marker">
              <td colspan="2" style="padding: 3px 8px; font-weight: 700; color: #EF4444; background: rgba(239, 68, 68, 0.16); border-left: 3px solid #EF4444;">
                <span class="diff-marker">!</span> <span class="diff-line-code">${escapeHtml(item.text)}</span>
              </td>
            </tr>
          `;
        } else {
          const content = escapeHtml(item.text.startsWith(' ') ? item.text.slice(1) : item.text);
          const leftGutter = `<span class="diff-split-gutter">${item.oldNum}</span>`;
          const rightGutter = `<span class="diff-split-gutter">${item.newNum}</span>`;
          splitHtml += `
            <tr class="diff-split-row diff-context">
              <td class="diff-split-col diff-split-col-left">${leftGutter}<span class="diff-marker">&nbsp;</span><span class="diff-line-code">${content}</span></td>
              <td class="diff-split-col">${rightGutter}<span class="diff-marker">&nbsp;</span><span class="diff-line-code">${content}</span></td>
            </tr>
          `;
        }
      }
      splitHtml += '</tbody></table>';
      return splitHtml;
    }

    // Unified Mode Rendering
    return parsedItems.map(item => {
      if (item.type === 'hunk_header') {
        return `
          <div class="diff-line diff-hunk-header">
            <div class="diff-hunk-title-wrap">
              <span class="diff-line-gutter"><span>...</span><span>...</span></span>
              <span>${escapeHtml(item.text)}</span>
            </div>
            <div class="diff-hunk-actions">
              <button type="button" class="btn-hunk-action btn-hunk-discard" data-hunk-idx="${item.hunkIdx}" title="Discard this hunk">✗ Discard</button>
            </div>
          </div>
        `;
      }
      if (item.type === 'meta') {
        return `<div class="diff-line diff-meta"><span class="diff-line-gutter"><span></span><span></span></span><span>${escapeHtml(item.text)}</span></div>`;
      }

      if (item.type === 'conflict_marker') {
        return `<div class="diff-line diff-conflict-marker"><span class="diff-line-gutter"><span>!</span><span>!</span></span><span class="diff-line-content"><span class="diff-marker">!</span><span class="diff-line-code">${escapeHtml(item.text)}</span></span></div>`;
      }

      if (item.type === 'del') {
        const innerContent = item.spans ? renderWordDiffSpans(item.spans) : escapeHtml(item.text.slice(1));
        return `<div class="diff-line diff-del"><span class="diff-line-gutter"><span>${item.oldNum}</span><span></span></span><span class="diff-line-content"><span class="diff-marker">-</span><span class="diff-line-code">${innerContent}</span></span></div>`;
      }

      if (item.type === 'add') {
        const innerContent = item.spans ? renderWordDiffSpans(item.spans) : escapeHtml(item.text.slice(1));
        return `<div class="diff-line diff-add"><span class="diff-line-gutter"><span></span><span>${item.newNum}</span></span><span class="diff-line-content"><span class="diff-marker">+</span><span class="diff-line-code editable-diff-line" contenteditable="true" spellcheck="false" data-raw-idx="${item.rawIdx}" title="Click to edit line before accepting">${innerContent}</span></span></div>`;
      }

      const content = escapeHtml(item.text.startsWith(' ') ? item.text.slice(1) : item.text);
      return `<div class="diff-line diff-context"><span class="diff-line-gutter"><span>${item.oldNum}</span><span>${item.newNum}</span></span><span class="diff-line-content"><span class="diff-marker">&nbsp;</span><span class="diff-line-code">${content}</span></span></div>`;
    }).join('');
  }

  function formatMarkdown(text) {
    if (!text) return '';

    // Cleanly strip any legacy or model-emitted bare [plan] header at the start
    text = text.replace(/^\s*\[(?:plan|PLAN)\]\s*\r?\n?/, '');

    // 1. Stash and protect complete or streaming code blocks
    const codeBlocks = [];

    function createCodeBlockHtml(displayLang, code) {
      const isMermaid = displayLang === 'mermaid' ||
        displayLang === 'flowchart' ||
        displayLang === 'diagram' ||
        displayLang === 'architecture' ||
        (/^\s*(?:graph|flowchart)\s+(?:TD|TB|LR|BT|RL)\b/i.test(code)) ||
        (/^\s*(?:sequenceDiagram|classDiagram)\b/i.test(code));
      if (isMermaid) {
        return createMermaidDiagramHtml(code.trim());
      }

      const isDiff = displayLang === 'diff' ||
        displayLang === 'patch' ||
        code.includes('<<<<<<< SEARCH') ||
        (code.includes('--- ') && code.includes('+++ ') && code.includes('@@'));

      const diffBtnHtml = isDiff ? (
        `<button class="btn-chat-apply-diff" type="button" title="View & apply in Diff tab">` +
        `<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="12" y1="18" x2="12" y2="12"></line><line x1="9" y1="15" x2="15" y2="15"></line></svg>` +
        `<span>Diff Tab</span>` +
        `</button>`
      ) : '';

      return (
        `<div class="code-block-wrapper" data-is-diff="${isDiff ? 'true' : 'false'}">` +
        `<div class="code-block-header">` +
        `<span class="code-block-lang">${escapeHtml(displayLang)}</span>` +
        diffBtnHtml +
        `<button class="btn-copy-code" type="button" title="Copy code">` +
        `<svg class="icon-code-copy" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>` +
        `<span class="btn-copy-code-text">Copy</span>` +
        `</button>` +
        `</div>` +
        `<pre class="code-block-pre"><code>${escapeHtml(code)}</code></pre>` +
        `</div>`
      );
    }

    // Match static or historical <think>...</think> blocks
    let out = text.replace(/<(?:think|thinking)>([\s\S]*?)<\/(?:think|thinking)>/gi, function (match, thoughts) {
      const cleanThoughts = thoughts.trim();
      const index = codeBlocks.length;
      const drawerHtml = `
        <div class="thinking-drawer is-finished is-collapsed">
          <div class="thinking-drawer-header" onclick="this.parentElement.classList.toggle('is-collapsed')">
            <div class="thinking-header-left">
              <span class="thinking-glyph-icon" style="display:inline-flex;">
                <svg viewBox="0 0 10 10" width="8" height="8" fill="currentColor">
                  <polygon points="2,1 8,5 2,9"/>
                </svg>
              </span>
              <span class="thinking-header-title">Thought Process</span>
            </div>
            <div class="thinking-header-right">
              <button type="button" class="btn-thinking-toggle" title="Toggle thinking process" aria-label="Toggle thinking">
                <span class="thinking-toggle-label">Expand</span>
                <svg class="thinking-toggle-icon" viewBox="0 0 10 10" width="7" height="7" fill="currentColor">
                  <polygon points="1,7 9,7 5,2"/>
                </svg>
              </button>
            </div>
          </div>
          <div class="thinking-drawer-body">
            <pre class="thinking-thought-content">${escapeHtml(cleanThoughts)}</pre>
          </div>
        </div>
      `;
      codeBlocks.push(drawerHtml);
      return `\n__CODE_BLOCK_${index}__\n`;
    });

    // Match code blocks (handles: CRLF, 3 or more backticks, trailing spaces after language, and unclosed streaming blocks at end of text)
    out = out.replace(/(?:^|\n)[ \t]*(`{3,})([a-zA-Z0-9_-]*)[^\S\r\n]*\r?\n([\s\S]*?)(?:\r?\n[ \t]*\1[^\S\r\n]*(?:\r?\n|$)|$)/g, function (match, fence, lang, code) {
      if (!code && !match.endsWith(fence)) return match;
      const displayLang = (lang ? lang.trim().toLowerCase() : '') || 'code';
      const index = codeBlocks.length;
      codeBlocks.push(createCodeBlockHtml(displayLang, code.trimEnd()));
      return `\n__CODE_BLOCK_${index}__\n`;
    });

    // Also protect raw unfenced SEARCH/REPLACE blocks if emitted outside triple backticks
    out = out.replace(/(?:^|\n)(<{5,7}\s*SEARCH[\s\S]*?={5,7}[\s\S]*?>{5,7}\s*REPLACE)/g, function (m, block) {
      const index = codeBlocks.length;
      codeBlocks.push(createCodeBlockHtml('diff', block.trim()));
      return `\n__CODE_BLOCK_${index}__\n`;
    });

    // Also protect raw unfenced unified diff blocks if emitted outside triple backticks
    out = out.replace(/(?:^|\n)(--- (?:a\/)?[^\n\r]+\r?\n\+\+\+ (?:b\/)?[^\n\r]+\r?\n@@\s+-\d+[\s\S]*?)(?=(?:\r?\n\r?\n[^\s+-@])|(?:\r?\n(?=[A-Z][a-z]+:))|$)/g, function (m, diffText) {
      if (diffText.includes('__CODE_BLOCK_')) return m;
      const index = codeBlocks.length;
      codeBlocks.push(createCodeBlockHtml('diff', diffText.trim()));
      return `\n__CODE_BLOCK_${index}__\n`;
    });

    // Helper for inline elements (bold, italic, inline code)
    function formatInline(str) {
      if (!str) return '';
      let s = escapeHtml(str);
      // Allow safe HTML line breaks (e.g. from table cells or markdown <br>, <br/>, <br />)
      s = s.replace(/&lt;br\s*\/?&gt;/gi, '<br/>');
      // Inline code `...`
      s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
      // Bold **...**
      s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
      // Italic *...*
      s = s.replace(/(^|[^*])\*([^*]+)\*([^*]|$)/g, '$1<em>$2</em>$3');
      return s;
    }

    // 2. Parse Markdown Tables
    const tableBlocks = [];
    const lines = out.split('\n');
    const processedLines = [];
    let i = 0;

    while (i < lines.length) {
      const line = lines[i];
      const trimmed = line.trim();

      const nextLine = (i + 1 < lines.length) ? lines[i + 1].trim() : '';
      const isHeaderCandidate = trimmed.includes('|') && !trimmed.startsWith('__CODE_BLOCK_');
      const isDelimiter = isHeaderCandidate && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/.test(nextLine);

      if (isHeaderCandidate && isDelimiter) {
        const headerRow = trimmed;
        const delimRow = nextLine;
        i += 2;

        const delimCols = delimRow.replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
        const alignments = delimCols.map(col => {
          if (col.startsWith(':') && col.endsWith(':')) return 'center';
          if (col.endsWith(':')) return 'right';
          if (col.startsWith(':')) return 'left';
          return 'left';
        });

        const headerCols = headerRow.replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());

        const bodyRows = [];
        while (i < lines.length) {
          const bodyLine = lines[i].trim();
          if (!bodyLine || !bodyLine.includes('|') || bodyLine.startsWith('__CODE_BLOCK_')) {
            break;
          }
          const cells = bodyLine.replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
          bodyRows.push(cells);
          i++;
        }

        let tableHtml = '<div class="clay-table-wrapper"><table class="clay-table"><thead><tr>';
        headerCols.forEach((h, colIdx) => {
          const align = alignments[colIdx] || 'left';
          tableHtml += `<th style="text-align: ${align};">${formatInline(h)}</th>`;
        });
        tableHtml += '</tr></thead><tbody>';

        bodyRows.forEach(row => {
          tableHtml += '<tr>';
          for (let colIdx = 0; colIdx < headerCols.length; colIdx++) {
            const cell = row[colIdx] !== undefined ? row[colIdx] : '';
            const align = alignments[colIdx] || 'left';
            tableHtml += `<td style="text-align: ${align};">${formatInline(cell)}</td>`;
          }
          tableHtml += '</tr>';
        });
        tableHtml += '</tbody></table></div>';

        const tIdx = tableBlocks.length;
        tableBlocks.push(tableHtml);
        processedLines.push(`__TABLE_BLOCK_${tIdx}__`);
        continue;
      }

      processedLines.push(line);
      i++;
    }

    // 3. Process Headings, Lists, Blockquotes, HR, Paragraphs
    const renderedBlocks = [];
    let currentList = null;

    function flushList() {
      if (!currentList) return;
      const tag = currentList.type;
      const cls = tag === 'ul' ? 'clay-chat-ul' : 'clay-chat-ol';
      let listHtml = `<${tag} class="${cls}">`;
      currentList.items.forEach(it => {
        listHtml += `<li class="clay-chat-li">${formatInline(it)}</li>`;
      });
      listHtml += `</${tag}>`;
      renderedBlocks.push(listHtml);
      currentList = null;
    }

    for (let j = 0; j < processedLines.length; j++) {
      const raw = processedLines[j];
      const trim = raw.trim();

      if (trim.startsWith('__CODE_BLOCK_') || trim.startsWith('__TABLE_BLOCK_')) {
        flushList();
        renderedBlocks.push(trim);
        continue;
      }

      // Headings: # to ######
      const headingMatch = trim.match(/^(#{1,6})\s+(.+)$/);
      if (headingMatch) {
        flushList();
        const level = Math.min(headingMatch[1].length, 4);
        const headingText = headingMatch[2].replace(/#+\s*$/, '');
        renderedBlocks.push(`<h${level} class="clay-chat-h clay-chat-h${level}">${formatInline(headingText)}</h${level}>`);
        continue;
      }

      // Horizontal Rule
      if (/^(\*{3,}|-{3,}|_{3,})$/.test(trim)) {
        flushList();
        renderedBlocks.push('<hr class="clay-chat-hr"/>');
        continue;
      }

      // Blockquote
      const quoteMatch = trim.match(/^>\s*(.+)$/);
      if (quoteMatch) {
        flushList();
        renderedBlocks.push(`<blockquote class="clay-chat-quote">${formatInline(quoteMatch[1])}</blockquote>`);
        continue;
      }

      // Ordered List (1. item)
      const olMatch = trim.match(/^(\d+)\.\s+(.+)$/);
      if (olMatch) {
        if (!currentList || currentList.type !== 'ol') {
          flushList();
          currentList = { type: 'ol', items: [] };
        }
        currentList.items.push(olMatch[2]);
        continue;
      }

      // Unordered List (- item or * item)
      const ulMatch = trim.match(/^[-*+]\s+(.+)$/);
      if (ulMatch) {
        if (!currentList || currentList.type !== 'ul') {
          flushList();
          currentList = { type: 'ul', items: [] };
        }
        currentList.items.push(ulMatch[1]);
        continue;
      }

      // Normal paragraph or spacer
      flushList();
      if (!trim) {
        renderedBlocks.push('<div class="clay-chat-spacer"></div>');
      } else {
        renderedBlocks.push(`<div class="clay-chat-p">${formatInline(raw)}</div>`);
      }
    }
    flushList();

    let finalHtml = renderedBlocks.join('');

    // Restore tables
    tableBlocks.forEach((tblHtml, idx) => {
      finalHtml = finalHtml.replace(`__TABLE_BLOCK_${idx}__`, tblHtml);
    });

    // Restore code blocks
    codeBlocks.forEach((codeHtml, idx) => {
      finalHtml = finalHtml.replace(`__CODE_BLOCK_${idx}__`, codeHtml);
    });

    return finalHtml;
  }

  // Delegated dynamic copy button listener for code blocks
  document.addEventListener('click', (e) => {
    const copyBtn = e.target.closest('.btn-copy-code');
    if (!copyBtn) return;
    e.stopPropagation();

    const wrapper = copyBtn.closest('.code-block-wrapper');
    if (!wrapper) return;
    const codeEl = wrapper.querySelector('code');
    if (!codeEl) return;

    const textToCopy = codeEl.innerText || codeEl.textContent || '';
    const textSpan = copyBtn.querySelector('.btn-copy-code-text');
    const svgEl = copyBtn.querySelector('svg');

    copyBtn.classList.add('copied');
    if (textSpan) textSpan.textContent = 'Copied!';
    if (svgEl) {
      svgEl.innerHTML = '<polyline points="20 6 9 17 4 12"></polyline>';
    }

    const resetBtn = () => {
      setTimeout(() => {
        copyBtn.classList.remove('copied');
        if (textSpan) textSpan.textContent = 'Copy';
        if (svgEl) {
          svgEl.innerHTML = '<rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>';
        }
      }, 2000);
    };

    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(textToCopy).then(resetBtn).catch(() => {
        fallbackCopyText(textToCopy, resetBtn);
      });
    } else {
      fallbackCopyText(textToCopy, resetBtn);
    }
  });

  // Delegated dynamic diff apply button listener for chat code blocks
  document.addEventListener('click', async (e) => {
    const diffBtn = e.target.closest('.btn-chat-apply-diff');
    if (!diffBtn) return;
    e.stopPropagation();

    const wrapper = diffBtn.closest('.code-block-wrapper');
    if (!wrapper) return;
    const codeEl = wrapper.querySelector('code');
    if (!codeEl) return;

    const rawText = codeEl.innerText || codeEl.textContent || '';
    let target = extractDiffFilename(rawText);

    const isCreationIntent = /\b(create|new\s+file|put\s+.*in|write\s+.*in|save\s+as|in\s+a\s+new\s+file|make\s+(?:a\s+)?(?:new\s+)?file)\b/i.test(lastUserQuery || '');
    const queryTarget = getPrimaryRepoFile(lastUserQuery || '');
    const repoActive = getActiveEditorFileInRepo();
    const activeEditorFile = repoActive ? repoActive.replace(/\\/g, '/').split('/').pop() : null;

    if (!target) {
      if (isCreationIntent && queryTarget) {
        target = queryTarget;
      } else if (queryTarget && (!repoActive || !isSameFileReference(queryTarget, activeEditorFile))) {
        target = queryTarget;
      } else if (repoActive) {
        target = activeEditorFile;
      } else {
        target = queryTarget || getPrimaryRepoFile(rawText);
      }
    }

    if (target) {
      target = target.replace(/(\.[a-zA-Z0-9]+)\1+$/gi, '$1');
    }

    if (!target) {
      target = 'new_file.py';
    }

    const isTargetActive = activeEditorFile && isSameFileReference(target, activeEditorFile);
    const activeContentToSend = isTargetActive ? activeEditorContent : undefined;
    const isUnifiedDiff = rawText.startsWith('---') || rawText.includes('@@');
    const fileExists = currentRepoFilesList && currentRepoFilesList.some(f => f.name && f.name.toLowerCase() === target.toLowerCase());

    let finalDiff = rawText;
    let stagedFiles = null;

    if (!isUnifiedDiff && (isCreationIntent || !fileExists || (isTargetActive && (!activeEditorContent || !activeEditorContent.trim())))) {
      const rawLines = rawText.split('\n');
      finalDiff = `--- /dev/null\n+++ b/${target}\n@@ -0,0 +1,${rawLines.length} @@\n` + rawLines.map(l => `+${l}`).join('\n');
    } else {
      try {
        const res = await fetch(`${BACKEND_URL}/api/agent/compute-diff`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            workspace_path: currentRepoPath,
            target_file: target,
            code_or_snippet: rawText,
            active_file_content: activeContentToSend,
            preferred_line: isTargetActive ? activeEditorCursorLine : undefined
          })
        });
        if (res.ok) {
          const cData = await res.json();
          if (cData && cData.diff && cData.diff.trim().length > 0) {
            finalDiff = cData.diff;
            stagedFiles = cData.staged_files || null;
          }
        }
      } catch (err) {
        // fallback to rawText
      }
    }

    stageDiff(target, finalDiff, stagedFiles);
    switchTab('diff');
    showToast(`Staged changes for ${target} in Diff tab`, 'info');
  });

  // Delegated view toggle listener for mermaid architecture diagram cards
  document.addEventListener('click', (e) => {
    const toggleBtn = e.target.closest('.btn-mermaid-view-toggle');
    if (!toggleBtn) return;
    const card = toggleBtn.closest('.mermaid-diagram-card');
    if (!card) return;
    e.stopPropagation();

    const view = toggleBtn.getAttribute('data-view');
    const viewport = card.querySelector('.mermaid-diagram-viewport');
    const fallback = card.querySelector('.mermaid-code-fallback');
    const zoomPill = card.querySelector('.mermaid-zoom-pill');
    const allToggles = card.querySelectorAll('.btn-mermaid-view-toggle');

    allToggles.forEach(b => b.classList.remove('btn-mermaid-active'));
    toggleBtn.classList.add('btn-mermaid-active');

    if (view === 'code') {
      if (viewport) viewport.style.display = 'none';
      if (fallback) fallback.style.display = 'block';
      if (zoomPill) zoomPill.style.display = 'none';
    } else {
      if (viewport) viewport.style.display = 'block';
      if (fallback) fallback.style.display = 'none';
      if (zoomPill) zoomPill.style.display = 'inline-flex';
    }
  });

  // Delegated copy code button listener for mermaid architecture diagram cards
  document.addEventListener('click', (e) => {
    const copyBtn = e.target.closest('.btn-mermaid-copy-code');
    if (!copyBtn) return;
    const card = copyBtn.closest('.mermaid-diagram-card');
    if (!card) return;
    e.stopPropagation();

    const rawCode = card.getAttribute('data-raw-mermaid') || '';
    if (!rawCode) return;

    copyBtn.classList.add('copied');
    const origText = copyBtn.textContent;
    copyBtn.textContent = 'Copied!';
    const reset = () => {
      setTimeout(() => {
        copyBtn.classList.remove('copied');
        copyBtn.textContent = origText || 'Copy';
      }, 2000);
    };

    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(rawCode).then(reset).catch(() => fallbackCopyText(rawCode, reset));
    } else {
      fallbackCopyText(rawCode, reset);
    }
  });

  // Delegated copy SVG markup button listener for mermaid architecture diagram cards
  document.addEventListener('click', (e) => {
    const copySvgBtn = e.target.closest('.btn-mermaid-copy-svg');
    if (!copySvgBtn) return;
    const card = copySvgBtn.closest('.mermaid-diagram-card');
    if (!card) return;
    e.stopPropagation();

    const svgEl = card.querySelector('.mermaid-diagram-canvas svg');
    if (!svgEl) return;
    const svgMarkup = svgEl.outerHTML;

    copySvgBtn.classList.add('copied');
    const origText = copySvgBtn.textContent;
    copySvgBtn.textContent = 'Copied!';
    const reset = () => {
      setTimeout(() => {
        copySvgBtn.classList.remove('copied');
        copySvgBtn.textContent = origText || 'Copy SVG';
      }, 2000);
    };

    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(svgMarkup).then(reset).catch(() => fallbackCopyText(svgMarkup, reset));
    } else {
      fallbackCopyText(svgMarkup, reset);
    }
  });

  // Delegated zoom buttons (+ / 100% / -) for mermaid architecture diagram cards (30% to 1000%)
  document.addEventListener('click', (e) => {
    const zoomBtn = e.target.closest('.btn-mermaid-zoom');
    if (!zoomBtn) return;
    const card = zoomBtn.closest('.mermaid-diagram-card');
    if (!card) return;
    e.stopPropagation();

    const canvas = card.querySelector('.mermaid-diagram-canvas');
    const viewport = card.querySelector('.mermaid-diagram-viewport');
    if (!canvas) return;

    let currentZoom = parseFloat(canvas.dataset.zoom || '1.0');
    const baseW = parseFloat(canvas.dataset.baseWidth || '600');
    const baseH = parseFloat(canvas.dataset.baseHeight || '380');
    let isFitAction = false;

    if (zoomBtn.classList.contains('btn-mermaid-zoom-in')) {
      currentZoom = Math.min(10.0, Math.round((currentZoom + 0.25) * 100) / 100);
    } else if (zoomBtn.classList.contains('btn-mermaid-zoom-out')) {
      currentZoom = Math.max(0.3, Math.round((currentZoom - 0.25) * 100) / 100);
    } else if (zoomBtn.classList.contains('btn-mermaid-zoom-reset')) {
      currentZoom = 1.0;
    } else if (zoomBtn.classList.contains('btn-mermaid-zoom-fit')) {
      isFitAction = true;
      if (viewport && baseW > 0 && baseH > 0) {
        const availW = Math.max(100, viewport.clientWidth - 28);
        const availH = Math.max(100, viewport.clientHeight - 28);
        const fitScale = Math.min(availW / baseW, availH / baseH);
        currentZoom = Math.max(0.3, Math.min(10.0, Math.round(fitScale * 100) / 100));
      } else {
        currentZoom = 1.0;
      }
    }

    const targetW = Math.round(baseW * currentZoom);
    const targetH = Math.round(baseH * currentZoom);

    // Save previous scroll center ratio unless fitting to window
    let ratioX = 0.5;
    let ratioY = 0.5;
    if (!isFitAction && viewport && viewport.scrollWidth > 0 && viewport.scrollHeight > 0) {
      ratioX = (viewport.scrollLeft + viewport.clientWidth / 2) / viewport.scrollWidth;
      ratioY = (viewport.scrollTop + viewport.clientHeight / 2) / viewport.scrollHeight;
    }

    canvas.dataset.zoom = currentZoom.toString();
    canvas.style.width = targetW + 'px';
    canvas.style.minWidth = targetW + 'px';
    canvas.style.height = targetH + 'px';

    if (viewport) {
      if (isFitAction) {
        viewport.scrollLeft = 0;
        viewport.scrollTop = 0;
      } else {
        viewport.scrollLeft = ratioX * viewport.scrollWidth - viewport.clientWidth / 2;
        viewport.scrollTop = ratioY * viewport.scrollHeight - viewport.clientHeight / 2;
      }
    }

    const resetLabel = card.querySelector('.btn-mermaid-zoom-reset');
    if (resetLabel) {
      resetLabel.textContent = `${Math.round(currentZoom * 100)}%`;
    }
  });

  // Delegated pan-and-drag navigation for mermaid diagram viewports
  let activePanViewport = null;
  let panStartX = 0;
  let panStartY = 0;
  let panStartScrollLeft = 0;
  let panStartScrollTop = 0;

  document.addEventListener('mousedown', (e) => {
    const viewport = e.target.closest('.mermaid-diagram-viewport');
    if (!viewport) return;
    if (e.target.closest('button') || e.target.closest('a') || e.target.closest('input')) return;
    activePanViewport = viewport;
    panStartX = e.pageX;
    panStartY = e.pageY;
    panStartScrollLeft = viewport.scrollLeft;
    panStartScrollTop = viewport.scrollTop;
    viewport.classList.add('is-panning');

    // Prevent default browser text selection from starting inside the diagram viewport
    e.preventDefault();
    if (window.getSelection) {
      window.getSelection().removeAllRanges();
    }
  });

  window.addEventListener('mousemove', (e) => {
    if (!activePanViewport) return;
    // Suppress any stray text selection while dragging/panning
    if (window.getSelection && window.getSelection().rangeCount > 0) {
      window.getSelection().removeAllRanges();
    }
    const dx = e.pageX - panStartX;
    const dy = e.pageY - panStartY;
    activePanViewport.scrollLeft = panStartScrollLeft - dx;
    activePanViewport.scrollTop = panStartScrollTop - dy;
  });

  // Prevent browser native ghost drag of SVG or text elements inside mermaid diagrams
  document.addEventListener('dragstart', (e) => {
    if (e.target.closest('.mermaid-diagram-viewport')) {
      e.preventDefault();
    }
  });

  // Delegated click listener for interactive Call Hierarchy symbols in Repo tab
  document.addEventListener('click', (e) => {
    const symBtn = e.target.closest('.hierarchy-sym-btn');
    if (!symBtn) return;
    e.stopPropagation();

    const sym = symBtn.getAttribute('data-symbol');
    if (!sym) return;

    const meta = currentGraphNodesMeta ? currentGraphNodesMeta[sym] : null;
    const targetFile = meta && meta.file ? meta.file : (selectedRepoFile || activeEditorFile || '');
    const targetLine = meta && meta.line ? meta.line : 0;
    const displayName = meta && meta.name ? meta.name : sym;

    openFileInIdeOrSystem(targetFile, targetLine, displayName);
  });

  window.addEventListener('mouseup', () => {
    if (activePanViewport) {
      activePanViewport.classList.remove('is-panning');
      activePanViewport = null;
    }
  });

  function escapeHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  // ==========================================
  // 10. Theme Pill Switch: Light / Claude Dark
  // ==========================================
  const btnThemeToggle = document.getElementById('btn-theme-toggle');
  const iconThemeMoon = document.getElementById('icon-theme-moon');
  const iconThemeSun = document.getElementById('icon-theme-sun');
  const themeSwitchText = document.getElementById('theme-switch-text');

  function applyTheme(theme) {
    if (theme === 'dark') {
      document.body.classList.add('claude-dark');
      if (iconThemeMoon) iconThemeMoon.style.display = 'none';
      if (iconThemeSun) iconThemeSun.style.display = 'block';
      if (themeSwitchText) themeSwitchText.textContent = 'Light';
    } else {
      document.body.classList.remove('claude-dark');
      if (iconThemeMoon) iconThemeMoon.style.display = 'block';
      if (iconThemeSun) iconThemeSun.style.display = 'none';
      if (themeSwitchText) themeSwitchText.textContent = 'Dark';
    }
    try {
      localStorage.setItem('clay-theme', theme);
    } catch (e) { }

    // Re-render any visible mermaid diagrams to adapt to light / dark mode immediately
    refreshAllMermaidDiagrams();
  }

  // MutationObserver to automatically re-render diagrams whenever VS Code or CLAY switches theme
  let themeObserverDebounce = null;
  const themeObserver = new MutationObserver((mutations) => {
    let shouldRefresh = false;
    for (const m of mutations) {
      if (m.type === 'attributes' && (m.attributeName === 'class' || m.attributeName === 'data-vscode-theme-kind' || m.attributeName === 'style')) {
        shouldRefresh = true;
        break;
      }
    }
    if (shouldRefresh) {
      if (themeObserverDebounce) clearTimeout(themeObserverDebounce);
      themeObserverDebounce = setTimeout(() => {
        refreshAllMermaidDiagrams();
      }, 50);
    }
  });
  try {
    themeObserver.observe(document.body, { attributes: true, attributeFilter: ['class', 'data-vscode-theme-kind', 'style'] });
  } catch (e) { }

  if (btnThemeToggle) {
    btnThemeToggle.addEventListener('click', () => {
      const isDark = document.body.classList.contains('claude-dark');
      applyTheme(isDark ? 'light' : 'dark');
    });
  }

  // Initialize theme from saved preference
  try {
    const savedTheme = localStorage.getItem('clay-theme');
    if (savedTheme === 'dark') {
      applyTheme('dark');
    }
  } catch (e) { }

  // Initial sync with extension host
  if (isVsCode) {
    vscode.postMessage({ command: 'getInitialState', currentPath: currentRepoPath });
  }

  // Initial tree load
  setTimeout(() => {
    loadRepoTree();
  }, 100);
})();

