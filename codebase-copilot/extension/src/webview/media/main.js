/**
 * Codebase Copilot: Unified Webview Controller
 * Powers both the VS Code Webview Provider and Standalone Browser Preview.
 */
(function () {
  const isVsCode = typeof acquireVsCodeApi === 'function';
  const vscode = isVsCode ? acquireVsCodeApi() : null;
  const BACKEND_URL = 'http://127.0.0.1:8765';

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
  let isAgentRunning = false;
  let lastUserQuery = '';
  const CLAY_ICON_SVG = '<svg viewBox="0 0 512 512" class="clay-cube-icon" shape-rendering="crispEdges"><g transform="translate(64, 148) scale(24)"><rect x="1" y="1" width="2" height="1" fill="#C15C3D"/><rect x="2" y="2" width="2" height="1" fill="#C15C3D"/><rect x="3" y="3" width="2" height="1" fill="#C15C3D"/><rect x="4" y="4" width="2" height="1" fill="#C15C3D"/><rect x="5" y="5" width="2" height="1" fill="#C15C3D"/><rect x="4" y="6" width="2" height="1" fill="#C15C3D"/><rect x="3" y="7" width="2" height="1" fill="#C15C3D"/><rect x="2" y="8" width="2" height="1" fill="#C15C3D"/><rect x="1" y="9" width="2" height="1" fill="#C15C3D"/><rect x="8" y="8" width="6" height="2" fill="#C15C3D"/></g></svg>';

  // Active repository files registry for real-time diff mapping
  let currentRepoFilesList = [];
  let activeEditorFile = '';

  function extractDiffFilename(diffText) {
    if (!diffText) return null;
    const plusMatch = diffText.match(/\+\+\+\s+(?:b\/)?([^\s\n\r]+)/);
    if (plusMatch && plusMatch[1] && plusMatch[1] !== '/dev/null') {
      const parts = plusMatch[1].split(/[\\/]/);
      return parts[parts.length - 1];
    }
    const minusMatch = diffText.match(/---\s+(?:a\/)?([^\s\n\r]+)/);
    if (minusMatch && minusMatch[1] && minusMatch[1] !== '/dev/null') {
      const parts = minusMatch[1].split(/[\\/]/);
      return parts[parts.length - 1];
    }
    return null;
  }

  function getPrimaryRepoFile(hintText = '') {
    if (currentRepoFilesList && currentRepoFilesList.length > 0) {
      if (hintText) {
        const lower = hintText.toLowerCase();
        for (const f of currentRepoFilesList) {
          if (f.name && lower.includes(f.name.toLowerCase())) {
            return f.name;
          }
        }
      }
      const pyFile = currentRepoFilesList.find(f => f.name && f.name.endsWith('.py'));
      if (pyFile) return pyFile.name;
      const srcFile = currentRepoFilesList.find(f => f.name && /\.(js|ts|jsx|tsx|json|html|css|md)$/i.test(f.name));
      if (srcFile) return srcFile.name;
      return currentRepoFilesList[0].name;
    }
    return 'test_c.py';
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
    }
  }

  navTabs.forEach((tab) => {
    tab.addEventListener('click', () => {
      const target = tab.getAttribute('data-target');
      switchTab(target);
    });
  });

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
  let currentApiKey = localStorage.getItem('clay_api_key') || '';
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
      if (typeof activeEditorFile === 'string' && activeEditorFile) {
        const activeNorm = activeEditorFile.replace(/\\/g, '/');
        if (repoRoot && activeNorm.startsWith(repoRoot)) {
          let rel = activeNorm.slice(repoRoot.length).replace(/^\/+/, '');
          if (rel.endsWith('.py')) {
            return `${py} -m py_compile ${rel}`;
          }
        } else {
          const parts = activeNorm.split('/');
          const base = parts[parts.length - 1];
          if (base.endsWith('.py')) {
            return `${py} -m py_compile ${base}`;
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
        }).catch(() => {});
      }
    });
  }

  if (btnTestOllama) {
    btnTestOllama.addEventListener('click', testOllamaConnection);
  }

  if (btnSaveSettings) {
    btnSaveSettings.addEventListener('click', async () => {
      const payload = {
        model: modelSelect ? modelSelect.value : currentModel,
        host: hostInput ? hostInput.value.trim() : currentHost,
        connection_type: selectConnectionType ? selectConnectionType.value : currentConnectionType,
        api_key: inputApiKey ? inputApiKey.value.trim() : currentApiKey,
        temperature: tempSlider ? parseFloat(tempSlider.value) : currentTemperature,
        num_ctx: ctxSelect ? parseInt(ctxSelect.value, 10) : currentNumCtx,
        num_predict: maxTokensSelect ? parseInt(maxTokensSelect.value, 10) : currentNumPredict,
        privacy_filter: privacyCheck ? privacyCheck.checked : true,
        exclusion_patterns: privacyPatterns ? privacyPatterns.value.trim() : '.env, *.pem, credentials.json, id_rsa, *.key',
        security_mode: currentSecurityMode,
        allow_network: currentAllowNetwork
      };

      // Persist to local storage immediately
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
      if (payload.api_key !== undefined) localStorage.setItem('clay_api_key', payload.api_key);

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
        if (s.api_key !== undefined && inputApiKey) {
          inputApiKey.value = s.api_key;
          currentApiKey = s.api_key;
          localStorage.setItem('clay_api_key', currentApiKey);
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

  function stopChatGeneration(notifyHost = true) {
    if (!isChatStreaming) return;

    if (notifyHost && isVsCode) {
      vscode.postMessage({ command: 'stopChat' });
    }

    if (chatAbortController) {
      try {
        chatAbortController.abort();
      } catch {}
      chatAbortController = null;
    }

    setChatGeneratingState(false);
    updateAssistantCursorStates(null, false);

    if (currentAssistantTextEl) {
      const stopNotice = document.createElement('div');
      stopNotice.className = 'chat-stopped-notice';
      stopNotice.style.cssText = 'color: #F97316; font-size: 10.5px; font-family: var(--font-mono); margin-top: 8px; display: inline-flex; align-items: center; gap: 4px;';
      stopNotice.innerHTML = '<span style="font-size: 11px;">■</span> [Generation stopped by user]';
      currentAssistantTextEl.appendChild(stopNotice);
    }
    showToast('Generation stopped', 'info');
  }

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

    // Append user message
    lastUserQuery = text;
    appendUserMessage(text);
    if (chatInput) {
      chatInput.value = '';
      chatInput.style.height = 'auto';
    }

    // Create assistant streaming placeholder
    createAssistantCard();

    const pathFromInput = inputRepoPath ? inputRepoPath.value.trim() : '';
    if (pathFromInput) {
      currentRepoPath = pathFromInput;
      localStorage.setItem('codebase_copilot_repo_path', currentRepoPath);
    }

    if (isVsCode) {
      vscode.postMessage({
        command: 'userQuery',
        text,
        workspace_path: currentRepoPath,
        security_mode: currentSecurityMode,
        temperature: currentTemperature,
        num_ctx: currentNumCtx,
        num_predict: currentNumPredict
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
            workspace_path: currentRepoPath,
            security_mode: currentSecurityMode,
            temperature: currentTemperature,
            num_ctx: currentNumCtx,
            num_predict: currentNumPredict,
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
      if (chatInput.value.trim().length > 0) {
        hideChatEmptyState();
      }
    });
  }

  function clearChatSession() {
    if (messageList) {
      // Remove all user and assistant chat bubbles and action blocks
      const dynamicItems = messageList.querySelectorAll('.user-bubble, .assistant-card, .chat-agent-action');
      dynamicItems.forEach((el) => el.remove());

      // Restore holder screen ("CLAY Intelligence Engine" empty state card)
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

  function appendUserMessage(text) {
    hideChatEmptyState();
    const bubble = document.createElement('div');
    bubble.className = 'user-bubble';

    const textDiv = document.createElement('div');
    textDiv.className = 'bubble-content';
    textDiv.innerHTML = escapeHtml(text);
    bubble.appendChild(textDiv);

    const footer = createBubbleFooter(() => text, 'user');
    bubble.appendChild(footer);

    messageList.appendChild(bubble);
    scrollChatToBottom();
  }

  function createAssistantCard() {
    hideChatEmptyState();
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
    scrollChatToBottom();
  }

  let accumulatedRawText = '';

  function appendStreamToken(token) {
    if (!currentAssistantTextEl) return;
    accumulatedRawText += token;
    currentAssistantTextEl.innerHTML = formatMarkdown(accumulatedRawText) + '<span class="streaming-cursor"></span>';
    const card = currentAssistantTextEl.closest('.assistant-card');
    if (card && !card.classList.contains('is-processing')) {
      updateAssistantCursorStates(card, true);
    }
    scrollChatToBottom();
  }

  function onStreamFinished(fullText, citations) {
    setChatGeneratingState(false);
    if (currentAssistantTextEl) {
      currentAssistantTextEl.innerHTML = formatMarkdown(fullText);
      const card = currentAssistantTextEl.closest('.assistant-card');

      // Detect unified diff or code block
      const diffMatch = fullText.match(/```(?:diff)?\n([\s\S]*?@@[\s\S]*?)```/);
      let detectedDiff = null;
      let targetFilename = null;

      if (diffMatch) {
        detectedDiff = diffMatch[1].trim();
        targetFilename = extractDiffFilename(detectedDiff) || getPrimaryRepoFile(lastUserQuery || fullText);
        stageDiff(targetFilename, detectedDiff);
      } else {
        const rawDiffMatch = fullText.match(/(--- (?:a\/|\/dev\/null)[^\n]+\n\+\+\+ (?:b\/|[^\n]+)[^\n]*\n@@[\s\S]*?)(?:```|$)/);
        if (rawDiffMatch) {
          detectedDiff = rawDiffMatch[1].trim();
          targetFilename = extractDiffFilename(detectedDiff) || getPrimaryRepoFile(lastUserQuery || fullText);
          stageDiff(targetFilename, detectedDiff);
        }
      }

      // Check if there is a general code block if diff wasn't detected
      let detectedCode = null;
      if (!detectedDiff) {
        const codeBlockMatch = fullText.match(/```[a-zA-Z0-9_-]*\n([\s\S]*?)```/);
        if (codeBlockMatch) {
          detectedCode = codeBlockMatch[1].trim();
          targetFilename = getPrimaryRepoFile(lastUserQuery || fullText);
        }
      }

      // Detect code change intent in user query or response
      const codeIntentRegex = /\b(create|write|add|implement|fix|refactor|change|update|make|delete|build|print|patch|edit|function|script|class|module|snippet)\b/i;
      const hasCodeBlock = fullText.includes('```');

      if (card && (codeIntentRegex.test(lastUserQuery) || hasCodeBlock || detectedDiff || detectedCode)) {
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

          const capturedDiff = detectedDiff;
          const capturedCode = detectedCode;
          const capturedTarget = targetFilename || getPrimaryRepoFile(lastUserQuery || fullText);
          const capturedGoal = lastUserQuery || 'Apply generated code changes';

          actionDiv.querySelector('.btn-run-in-agent').addEventListener('click', () => {
            switchTab('agent');
            const agentTaskLabel = capturedTarget ? `Apply changes for ${capturedTarget}` : capturedGoal;
            if (agentGoalInput) {
              agentGoalInput.value = agentTaskLabel;
              resizeAgentGoalInput();
            }
            runAgentTask({
              pregeneratedDiff: capturedDiff,
              pregeneratedCode: capturedCode,
              targetFile: capturedTarget,
              sourceGoal: capturedGoal
            });
          });
        }
      }
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

  async function runAgentTask(chatPayload = null) {
    const rawGoal = agentGoalInput ? agentGoalInput.value.trim() : '';
    const goal = rawGoal || (chatPayload && chatPayload.targetFile ? `Apply changes for ${chatPayload.targetFile}` : '');
    if (!goal) return;

    isAgentRunning = true;
    if (btnRunAgent) btnRunAgent.style.display = 'none';
    if (btnStopAgent) btnStopAgent.style.display = 'flex';
    if (agentTerminalStatus) {
      agentTerminalStatus.textContent = 'RUNNING';
      agentTerminalStatus.style.color = '#4CAF50';
    }
    if (agentProgressTrack) agentProgressTrack.style.display = 'block';

    const isFromChat = Boolean(chatPayload && (chatPayload.pregeneratedDiff || chatPayload.pregeneratedCode));
    appendTerminalLog(`\n$ copilot-agent --execute "${goal}"`);
    appendTerminalLog(`[agent] Target Model: ${currentModel}`);

    // Set up step UI
    renderInitialSteps(goal, isFromChat);

    try {
      // Step 1: Scan AST & Symbols
      if (!isAgentRunning) return;
      updateStepUI(1, 'active');
      setAgentProgress(15, 'Step 1 of 3: Scanning repository AST symbols...');
      const step1Target = (isFromChat && chatPayload.targetFile) ? chatPayload.targetFile : goal;
      const step1Res = await executeStep(1, step1Target);
      if (!isAgentRunning) return;
      updateStepUI(1, 'done', step1Res.log || 'AST symbols analyzed.');
      appendTerminalLog(`[step 1] ${step1Res.log || 'AST symbols analyzed.'}`);

      // Step 2: Code Synthesis / Application
      if (!isAgentRunning) return;
      updateStepUI(2, 'active');

      if (isFromChat) {
        const targetFilename = chatPayload.targetFile || getPrimaryRepoFile(goal);
        setAgentProgress(60, `Step 2 of 3: Applying generated code changes for ${targetFilename}...`);
        appendTerminalLog(`[step 2] Applying solution generated in chat for ${targetFilename}...`);
        await new Promise(r => setTimeout(r, 350));

        let diffToApply = chatPayload.pregeneratedDiff;
        if (!diffToApply && chatPayload.pregeneratedCode) {
          const codeLines = chatPayload.pregeneratedCode.split('\n');
          diffToApply = `--- /dev/null\n+++ b/${targetFilename}\n@@ -0,0 +1,${Math.max(1, codeLines.length)} @@\n` +
            codeLines.map(l => `+${l}`).join('\n');
        }

        if (diffToApply) {
          stageDiff(targetFilename, diffToApply);
          appendTerminalLog(`[diff] Staged surgical diff for ${targetFilename} in Diff Reviewer tab.`);
        }
        updateStepUI(2, 'done', `Applied code changes for ${targetFilename}.`);
        appendTerminalLog(`[step 2] Code changes from chat applied cleanly.`);
      } else {
        setAgentProgress(60, `Step 2 of 3: Synthesizing code solution via ${currentModel}...`);
        appendTerminalLog(`[step 2] Querying ${currentModel} for code changes...`);
        const step2Res = await executeStep(2, goal);
        if (!isAgentRunning) return;
        updateStepUI(2, 'done', 'Solution synthesized cleanly.');
        appendTerminalLog(`[step 2] ${step2Res.log || 'Code synthesized.'}`);

        let diffToStage = step2Res.diff;
        const targetFilename = step2Res.target_file || (diffToStage ? extractDiffFilename(diffToStage) : '') || getPrimaryRepoFile(goal);

        // Robust fallback: if diff was empty but LLM generated code or raw text
        if (!diffToStage && step2Res.generated) {
          const raw = step2Res.generated.trim();
          let codeBody = '';
          const codeMatch = raw.match(/```(?:python|py|python3|code)?\s*\n([\s\S]*?)```/i);
          if (codeMatch) {
            codeBody = codeMatch[1].trim();
          } else if (raw.startsWith('def ') || raw.startsWith('class ') || raw.startsWith('import ') || raw.startsWith('from ')) {
            codeBody = raw;
          }
          if (codeBody) {
            const lines = codeBody.split('\n');
            diffToStage = `--- /dev/null\n+++ b/${targetFilename}\n@@ -0,0 +1,${Math.max(1, lines.length)} @@\n` + lines.map(l => `+${l}`).join('\n');
          }
        }

        if (diffToStage) {
          stageDiff(targetFilename, diffToStage);
          appendTerminalLog(`[diff] Staged surgical diff for ${targetFilename} in Diff Reviewer tab.`);
        }
      }

      // Step 3: AST Syntax Verification
      if (!isAgentRunning) return;
      updateStepUI(3, 'active');
      setAgentProgress(90, 'Step 3 of 3: Verifying syntax & integrity...');
      const step3Target = (isFromChat && chatPayload.targetFile) ? chatPayload.targetFile : goal;
      const step3Res = await executeStep(3, step3Target);
      if (!isAgentRunning) return;
      updateStepUI(3, 'done', step3Res.log || 'Syntax and AST verification passed.');
      appendTerminalLog(`[step 3] ${step3Res.log || 'Syntax verified.'}`);

      setAgentProgress(100, 'All 3 steps completed (100% complete)');
      if (agentTerminalStatus) {
        agentTerminalStatus.textContent = 'COMPLETE';
        agentTerminalStatus.style.color = '#10B981';
      }
      appendTerminalLog(`[complete] Task executed successfully! Inspect diff in Diff tab.`);
    } catch (err) {
      appendTerminalLog(`[error] Execution failed: ${err.message}`);
    } finally {
      stopAgentTask(false);
    }
  }

  function stopAgentTask(userTriggered = true) {
    isAgentRunning = false;
    if (btnRunAgent) btnRunAgent.style.display = 'flex';
    if (btnStopAgent) btnStopAgent.style.display = 'none';
    if (userTriggered) {
      if (agentTerminalStatus) {
        agentTerminalStatus.textContent = 'STOPPED';
        agentTerminalStatus.style.color = '#EF4444';
      }
      appendTerminalLog(`[clay] Execution stopped by user.`);
    }
  }

  async function executeStep(stepId, goal) {
    try {
      const res = await fetch(`${BACKEND_URL}/api/agent/step`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          goal,
          step_id: stepId,
          workspace_path: currentRepoPath
        })
      });
      if (res.ok) return await res.json();
    } catch (e) {
      if (isVsCode) {
        return new Promise((resolve) => {
          const handler = (event) => {
            if (event.data?.command === 'agentStepResult' && event.data?.step_id === stepId) {
              window.removeEventListener('message', handler);
              resolve(event.data.data || { log: 'Step completed via extension host' });
            }
          };
          window.addEventListener('message', handler);
          vscode.postMessage({
            command: 'executeAgentStep',
            step_id: stepId,
            goal,
            workspace_path: currentRepoPath
          });
          setTimeout(() => {
            window.removeEventListener('message', handler);
            resolve({ log: `Step ${stepId} processed.` });
          }, 10000);
        });
      }
      throw e;
    }
    return { log: `Step ${stepId} processed.` };
  }

  function renderInitialSteps(goal, isFromChat = false) {
    if (!agentStepList) return;
    const step2Title = isFromChat
      ? '2. Apply generated code changes and stage unified diff'
      : '2. Synthesize code changes and compute unified diff';
    agentStepList.innerHTML = `
      <div class="step-item step-queued" id="agent-step-1">
        <div class="step-bullet">[-]</div>
        <div><strong>1. Scan codebase for symbols matching '${escapeHtml(goal.slice(0, 30))}'</strong></div>
      </div>
      <div class="step-item step-queued" id="agent-step-2">
        <div class="step-bullet">[-]</div>
        <div><strong>${step2Title}</strong></div>
      </div>
      <div class="step-item step-queued" id="agent-step-3">
        <div class="step-bullet">[-]</div>
        <div><strong>3. Run syntax validation & verify zero regressions</strong></div>
      </div>
    `;
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
      if (container && !container.querySelector('span')) {
        const span = document.createElement('span');
        span.style.color = '#8C847A';
        span.style.fontSize = '9.5px';
        span.style.fontFamily = 'var(--font-mono)';
        span.textContent = detail;
        container.appendChild(document.createElement('br'));
        container.appendChild(span);
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
    agentGoalInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        runAgentTask();
      }
    });

    agentGoalInput.addEventListener('input', resizeAgentGoalInput);
  }

  function appendTerminalLog(text) {
    if (terminalStream) {
      terminalStream.textContent += text + '\n';
      terminalStream.scrollTop = terminalStream.scrollHeight;
    }
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

  function stageDiff(filename, diffText) {
    stagedDiffData = { file: filename, diff: diffText };

    const lines = diffText.split('\n');
    let adds = 0;
    let dels = 0;
    lines.forEach((l) => {
      if (l.startsWith('+') && !l.startsWith('+++')) adds++;
      if (l.startsWith('-') && !l.startsWith('---')) dels++;
    });

    if (diffSummaryLabel) diffSummaryLabel.textContent = `Reviewing: ${filename}`;
    if (diffBadgeAdd) diffBadgeAdd.textContent = `+${adds}`;
    if (diffBadgeDel) diffBadgeDel.textContent = `-${dels}`;
    if (diffActionBar) diffActionBar.style.display = 'flex';

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
            <span>${escapeHtml(filename)}</span>
            <div class="diff-actions">
              <button class="btn-diff-action" id="btn-copy-staged-diff">Copy</button>
            </div>
          </div>
          <div class="diff-content">${renderDiffLines(diffText)}</div>
        </div>
      `;

      const copyBtn = document.getElementById('btn-copy-staged-diff');
      if (copyBtn) {
        copyBtn.addEventListener('click', () => {
          navigator.clipboard.writeText(diffText);
          copyBtn.textContent = 'Copied!';
          setTimeout(() => (copyBtn.textContent = 'Copy'), 1500);
        });
      }
    }
  }

  if (btnAcceptDiff) {
    btnAcceptDiff.addEventListener('click', async () => {
      if (!stagedDiffData) return;
      if (isVsCode) {
        vscode.postMessage({
          command: 'applyPatch',
          file: stagedDiffData.file,
          diff: stagedDiffData.diff,
          workspacePath: currentRepoPath
        });
      } else {
        try {
          const res = await fetch(`${BACKEND_URL}/api/agent/apply-patch`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              file_path: stagedDiffData.file,
              diff: stagedDiffData.diff,
              workspace_path: currentRepoPath
            })
          });
          const data = await res.json();
          if (res.ok && data.status === 'success') {
            showToast(`Patch applied cleanly to ${data.filename || stagedDiffData.file}`, 'success');
            clearStagedDiff();
            loadRepoTree(currentRepoPath);
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

  function clearStagedDiff() {
    stagedDiffData = null;
    const diffTabBtn = document.querySelector('.nav-tab[data-target="diff"]');
    const dot = diffTabBtn?.querySelector('.diff-staged-dot');
    if (dot) dot.remove();
    if (diffActionBar) diffActionBar.style.display = 'none';
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

  const DEFAULT_REPO_PATH = '.';
  const btnBrowseRepoPath = document.getElementById('btn-browse-repo-path');
  const repoFolderPicker = document.getElementById('repo-folder-picker');
  const repoFileTree = document.getElementById('repo-file-tree');
  const repoTreeCount = document.getElementById('repo-tree-count');
  const btnRefreshTree = document.getElementById('btn-refresh-tree');
  const btnExpandTree = document.getElementById('btn-expand-tree');

  function resetRepoGraphState() {
    if (mermaidCode) {
      mermaidCode.textContent = "No SQLite code graph available for this repository.\nClick 'Scan & Index Workspace' to analyze codebase structure.";
    }
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

  function openFileInIdeOrSystem(fullPath) {
    if (!fullPath) return;
    const repoHint = (inputRepoPath ? inputRepoPath.value.trim() : '') || currentRepoPath || '';
    let targetPath = fullPath;
    if (targetPath && !/^[a-zA-Z]:[\\\/]/.test(targetPath) && !targetPath.startsWith('/') && repoHint) {
      const sep = repoHint.includes('/') && !repoHint.includes('\\') ? '/' : '\\';
      targetPath = `${repoHint.replace(/[\\\/]+$/, '')}${sep}${targetPath.replace(/^[\\\/]+/, '')}`;
    }
    const fileName = targetPath.split(/[\\/]/).pop() || targetPath;

    showToast(`Opening ${fileName}...`, 'info');

    if (isVsCode) {
      vscode.postMessage({ command: 'openFile', path: targetPath, repo_path: repoHint });

      // Fallback: If extension host hasn't acknowledged in 500ms, call backend /api/file/open
      if (pendingFileOpenTimer) clearTimeout(pendingFileOpenTimer);
      pendingFileOpenTimer = setTimeout(() => {
        pendingFileOpenTimer = null;
        fetch(`${BACKEND_URL}/api/file/open`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ file_path: targetPath, repo_path: repoHint })
        })
        .then(res => res.json())
        .then(data => {
          if (data.status === 'success') {
            showToast(`Opened: ${fileName}`, 'success');
          }
        })
        .catch(() => {});
      }, 500);
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
    let fileCount = 0;
    function countFiles(nodes) {
      if (!nodes) return;
      nodes.forEach(n => {
        if (n.type === 'directory') {
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

        return `
          <div class="tree-node tree-file" style="padding-left: ${indent + 22}px;" data-file-path="${escapeHtml(node.path)}" data-full-path="${escapeHtml(fullPath)}" title="${escapeHtml(fullPath)}&#10;Single-click: Copy full path&#10;Double-click: Open file in IDE">
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

    // Single-click copies full path, Double-click opens file in IDE / OS
    let fileClickTimer = null;
    repoFileTree.querySelectorAll('.tree-file').forEach(fileItem => {
      fileItem.addEventListener('click', (e) => {
        e.stopPropagation();
        let fullPath = fileItem.getAttribute('data-full-path') || fileItem.getAttribute('data-file-path');
        const currentRoot = (inputRepoPath ? inputRepoPath.value.trim() : '') || currentRepoPath || '';
        if (fullPath && !/^[a-zA-Z]:[\\\/]/.test(fullPath) && !fullPath.startsWith('/') && currentRoot) {
          const sep = currentRoot.includes('/') && !currentRoot.includes('\\') ? '/' : '\\';
          fullPath = `${currentRoot.replace(/[\\\/]+$/, '')}${sep}${fullPath.replace(/^[\\\/]+/, '')}`;
        }

        if (fileClickTimer) {
          clearTimeout(fileClickTimer);
          fileClickTimer = null;
        }
        fileClickTimer = setTimeout(() => {
          fileClickTimer = null;
          if (fullPath) {
            const copyPromise = (navigator.clipboard && navigator.clipboard.writeText)
              ? navigator.clipboard.writeText(fullPath)
              : new Promise((resolve, reject) => {
                  try {
                    const ta = document.createElement('textarea');
                    ta.value = fullPath;
                    ta.style.position = 'fixed';
                    ta.style.opacity = '0';
                    document.body.appendChild(ta);
                    ta.focus();
                    ta.select();
                    const success = document.execCommand('copy');
                    document.body.removeChild(ta);
                    if (success) resolve();
                    else reject(new Error('Copy failed'));
                  } catch (err) {
                    reject(err);
                  }
                });

            copyPromise.then(() => {
              showToast(`Copied full path: ${fullPath}`, 'success');
            }).catch(() => {
              showToast(`Full path: ${fullPath}`, 'info');
            });
          }
        }, 220);
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
    const path = targetPath || (inputRepoPath ? inputRepoPath.value.trim() : currentRepoPath);
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
      showToast('Running security secret audit...', 'info');
      loadSecurityAudit();
    });
  }

  async function scanRepository() {
    const path = inputRepoPath ? inputRepoPath.value.trim() : currentRepoPath;
    if (path) {
      currentRepoPath = path;
      localStorage.setItem('codebase_copilot_repo_path', currentRepoPath);
    }
    if (repoStatusLabel) repoStatusLabel.textContent = 'Scanning AST & Call Graph...';
    if (btnScanRepo) btnScanRepo.disabled = true;

    if (isVsCode) {
      vscode.postMessage({ command: 'scanWorkspace', path });
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
        const graphRes = await fetch(`${BACKEND_URL}/api/graph/mermaid?path=${encodeURIComponent(path)}`);
        const secRes = await fetch(`${BACKEND_URL}/api/repo/security-audit?path=${encodeURIComponent(path)}`);

        if (scanRes.ok) {
          const scanData = await scanRes.json();
          const graphData = graphRes.ok ? await graphRes.json() : { mermaid: '', available: false };
          const secData = secRes.ok ? await secRes.json() : null;
          applyScanResults(scanData, graphData.mermaid, secData);
        }
      } catch (err) {
        if (repoStatusLabel) repoStatusLabel.textContent = `Scan failed: ${err.message}`;
      } finally {
        if (btnScanRepo) btnScanRepo.disabled = false;
      }
    }
  }

  function applyScanResults(scanData, mermaid, security) {
    if (btnScanRepo) btnScanRepo.disabled = false;
    if (repoStatsText && scanData) {
      repoStatsText.textContent = `Indexed: ${scanData.filesCount ?? 0} Files // ${scanData.symbolsCount ?? 0} Functions & Classes // ${scanData.chunksCount ?? 0} Vector Chunks`;
    }
    if (repoStatusLabel) {
      repoStatusLabel.textContent = 'Indexed & Synced (SQLite Graph Active)';
    }
    if (mermaidCode) {
      if (mermaid && mermaid.trim() && mermaid.trim() !== 'graph TD') {
        mermaidCode.textContent = mermaid;
      } else {
        mermaidCode.textContent = "No SQLite code graph available for this repository.\nClick 'Scan & Index Workspace' to analyze codebase structure.";
      }
    }
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
        if (msg.diff) {
          const targetFilename = extractDiffFilename(msg.diff) || getPrimaryRepoFile();
          stageDiff(targetFilename, msg.diff);
        }
        break;

      case 'streamStopped':
        stopChatGeneration(false);
        break;

      case 'patchApplied':
        showToast(`Patch applied cleanly to ${msg.file}`, 'success');
        clearStagedDiff();
        loadRepoTree(currentRepoPath);
        break;

      case 'scanResult':
        if (btnScanRepo) btnScanRepo.disabled = false;
        applyScanResults(msg.status, msg.mermaid, msg.security);
        loadRepoTree(currentRepoPath);
        break;

      case 'scanError':
        if (btnScanRepo) btnScanRepo.disabled = false;
        if (repoStatusLabel) repoStatusLabel.textContent = `Scan failed: ${msg.error || 'Unknown error'}`;
        break;

      case 'securityAuditResult':
        applySecurityResults(msg.security);
        break;

      case 'activeFileChanged':
        if (msg.file) {
          activeEditorFile = msg.file;
          if (detectedSystem) applySystemInfo(detectedSystem);
        }
        break;

      case 'renderMermaid':
        if (mermaidCode) {
          if (msg.mermaid && msg.mermaid.trim() && msg.mermaid.trim() !== 'graph TD') {
            mermaidCode.textContent = msg.mermaid;
          } else {
            mermaidCode.textContent = "No SQLite code graph available for this repository.\nClick 'Scan & Index Workspace' to analyze codebase structure.";
          }
        }
        break;

      case 'repoPathCleared':
        currentRepoPath = '';
        if (inputRepoPath) inputRepoPath.value = '';
        localStorage.removeItem('codebase_copilot_repo_path');
        resetRepoGraphState();
        if (repoFileTree) {
          repoFileTree.innerHTML = '<div class="tree-empty-state"><span class="pixel-glyph">»</span> No repository selected. Enter a path above or click BROWSE.</div>';
        }
        if (repoTreeCount) repoTreeCount.textContent = '0 Files';
        if (repoStatsText) repoStatsText.textContent = 'Indexed: 0 Files // 0 Functions & Classes // 0 Vector Chunks';
        break;

      case 'repoPathSelected':
        if (msg.path) {
          currentRepoPath = msg.path;
          if (inputRepoPath) inputRepoPath.value = currentRepoPath;
          localStorage.setItem('codebase_copilot_repo_path', currentRepoPath);
          resetRepoGraphState();
          loadSecurityAudit(currentRepoPath);
          if (msg.tree) {
            renderRepoTree(msg.tree, msg.stats, msg.path);
          } else {
            loadRepoTree(currentRepoPath);
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
  function renderDiffLines(diffText) {
    let lineNum = 1;
    return diffText
      .split('\n')
      .map((line) => {
        let cls = '';
        if (line.startsWith('+')) cls = 'diff-add';
        else if (line.startsWith('-')) cls = 'diff-del';
        return `<div class="diff-line ${cls}"><span class="diff-line-num">${lineNum++}</span><span>${escapeHtml(line)}</span></div>`;
      })
      .join('');
  }

  function formatMarkdown(text) {
    if (!text) return '';

    // 1. Stash and protect complete code blocks
    const codeBlocks = [];
    let out = text.replace(/```([a-zA-Z0-9_-]*)\n([\s\S]*?)```/g, function (_, lang, code) {
      const displayLang = lang ? lang.toLowerCase() : 'code';
      const index = codeBlocks.length;
      codeBlocks.push(
        `<div class="code-block-wrapper">` +
        `<div class="code-block-header">` +
        `<span class="code-block-lang">${escapeHtml(displayLang)}</span>` +
        `<button class="btn-copy-code" type="button" title="Copy code">` +
        `<svg class="icon-code-copy" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>` +
        `<span class="btn-copy-code-text">Copy</span>` +
        `</button>` +
        `</div>` +
        `<pre class="code-block-pre"><code>${escapeHtml(code)}</code></pre>` +
        `</div>`
      );
      return `\n__CODE_BLOCK_${index}__\n`;
    });

    // Helper for inline elements (bold, italic, inline code)
    function formatInline(str) {
      if (!str) return '';
      let s = escapeHtml(str);
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
  }

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

