/**
 * Codebase Copilot: Unified Webview Controller
 * Powers both the VS Code Webview Provider and Standalone Browser Preview.
 */
(function () {
  const isVsCode = typeof acquireVsCodeApi === 'function';
  const vscode = isVsCode ? acquireVsCodeApi() : null;
  const BACKEND_URL = 'http://127.0.0.1:8765';

  // State
  let currentModel = 'gemma4:31b-cloud';
  let currentHost = 'http://127.0.0.1:11434';
  let currentTemperature = 0.2;
  let currentNumCtx = 32768;
  let currentNumPredict = 2048;
  let currentRepoPath = localStorage.getItem('codebase_copilot_repo_path') || '/home/ishan/.gemini/antigravity-ide/scratch/codebase-copilot/backend';
  let stagedDiffData = null;
  let isAgentRunning = false;
  let lastUserQuery = '';
  const CLAY_ICON_SVG = '<svg viewBox="0 0 163 178" class="clay-cube-icon" shape-rendering="crispEdges"><path fill="#C15C3D" d="M82,6h1v6h-1z M75,7h7v5h-7z M83,7h5v5h-5z M69,10h6v3h-6z M88,10h6v3h-6z M68,11h1v5h-1z M94,12h1v4h-1z M62,13h6v4h-6z M69,13h5v3h-5z M89,13h5v3h-5z M95,13h6v4h-6z M61,16h1v3h-1z M101,16h1v3h-1z M55,17h6v5h-6z M62,17h5v2h-5z M96,17h5v2h-5z M102,17h6v5h-6z M54,19h1v4h-1z M50,20h4v6h-4z M108,20h5v3h-5z M55,22h5v1h-5z M103,22h5v1h-5z M49,23h1v3h-1z M109,23h10v3h-10z M43,24h6v2h-6z M119,24h1v5h-1z M42,26h6v3h-6z M115,26h4v3h-4z M120,26h1v4h-1z M38,27h4v6h-4z M121,27h4v6h-4z M31,30h7v3h-7z M125,30h7v3h-7z M30,33h5v3h-5z M127,33h6v1h-6z M25,34h5v2h-5z M128,34h11v2h-11z M24,35h1v4h-1z M25,36h4v3h-4z M134,36h5v1h-5z M18,37h6v2h-6z M135,37h10v2h-10z M17,39h6v1h-6z M140,39h6v1h-6z M12,40h2v10h-2z M15,40h7v2h-7z M141,40h11v2h-11z M11,41h1v9h-1z M14,41h1v11h-1z M15,42h6v1h-6z M142,42h10v1h-10z M10,43h1v7h-1z M148,43h5v95h-5z M6,44h4v90h-4z M153,44h4v90h-4z M15,47h5v6h-5z M143,47h5v48h-5z M142,48h1v47h-1z M20,50h1v4h-1z M141,50h1v45h-1z M21,51h6v5h-6z M136,51h5v26h-5z M27,54h6v3h-6z M130,54h6v20h-6z M33,56h1v4h-1z M129,56h1v19h-1z M28,57h5v3h-5z M34,57h6v5h-6z M128,57h1v18h-1z M123,58h5v17h-5z M40,60h1v3h-1z M122,60h1v91h-1z M41,61h6v5h-6z M116,61h6v93h-6z M35,62h5v1h-5z M47,64h7v3h-7z M109,64h7v18h-7z M48,67h7v3h-7z M108,67h1v17h-1z M55,68h6v5h-6z M102,68h6v17h-6z M21,71h2v6h-2z M61,71h6v4h-6z M96,71h6v20h-6z M23,72h2v8h-2z M25,73h2v7h-2z M67,73h1v4h-1z M95,73h1v41h-1z M27,74h2v10h-2z M68,74h6v6h-6z M89,74h6v41h-6z M130,74h1v1h-1z M134,74h2v1h-2z M29,75h1v14h-1z M62,75h5v2h-5z M123,75h4v1h-4z M123,76h2v1h-2z M30,77h1v13h-1z M74,77h1v3h-1z M87,77h2v95h-2z M123,77h1v74h-1z M138,77h3v17h-3z M31,78h2v15h-2z M75,78h12v5h-12z M33,79h1v18h-1z M26,80h1v3h-1z M34,81h1v17h-1z M109,82h3v1h-3z M114,82h2v1h-2z M76,83h11v1h-11z M109,83h2v1h-2z M115,83h1v72h-1z M28,84h1v3h-1z M35,84h1v16h-1z M79,84h8v88h-8z M126,84h3v40h-3z M36,85h1v15h-1z M102,85h3v2h-3z M124,85h2v41h-2z M102,87h1v3h-1z M37,88h3v13h-3z M114,89h1v44h-1z M40,90h1v28h-1z M41,91h1v26h-1z M96,91h5v3h-5z M111,91h3v41h-3z M110,92h1v41h-1z M32,93h1v1h-1z M109,93h1v40h-1z M42,94h1v23h-1z M96,94h4v16h-4z M139,94h2v1h-2z M43,95h1v22h-1z M144,95h4v43h-4z M129,97h1v14h-1z M44,98h1v16h-1z M130,99h1v8h-1z M45,100h1v14h-1z M131,100h1v5h-1z M38,101h2v18h-2z M46,101h1v13h-1z M132,101h1v3h-1z M133,102h1v1h-1z M143,102h1v39h-1z M141,103h2v38h-2z M47,105h2v5h-2z M140,105h1v39h-1z M37,106h1v13h-1z M49,106h1v4h-1z M139,106h1v38h-1z M36,107h1v14h-1z M108,107h1v13h-1z M138,107h1v37h-1z M35,108h1v13h-1z M34,109h1v12h-1z M107,109h1v10h-1z M31,110h3v12h-3z M47,110h1v1h-1z M96,110h3v1h-3z M30,111h1v13h-1z M96,111h2v3h-2z M29,112h1v14h-1z M106,112h1v6h-1z M28,113h1v13h-1z M27,114h1v12h-1z M105,114h1v3h-1z M24,115h3v10h-3z M89,115h5v53h-5z M23,116h1v8h-1z M21,117h2v7h-2z M94,118h1v48h-1z M31,122h2v2h-2z M96,122h2v43h-2z M95,123h1v42h-1z M98,123h1v42h-1z M99,124h1v41h-1z M126,124h2v1h-2z M25,125h2v1h-2z M136,125h2v19h-2z M124,126h1v1h-1z M135,128h1v17h-1z M45,129h2v7h-2z M134,129h1v19h-1z M47,130h3v8h-3z M133,130h1v18h-1z M50,131h1v8h-1z M10,132h5v6h-5z M51,132h3v7h-3z M111,132h1v1h-1z M132,132h1v16h-1z M54,133h1v8h-1z M129,133h3v15h-3z M7,134h3v3h-3z M55,134h2v7h-2z M126,134h3v16h-3z M153,134h3v2h-3z M15,135h3v3h-3z M57,135h3v8h-3z M124,135h2v16h-2z M18,136h2v5h-2z M46,136h1v1h-1z M60,136h1v8h-1z M153,136h2v2h-2z M8,137h2v1h-2z M61,137h3v8h-3z M16,138h2v3h-2z M20,138h1v4h-1z M64,138h1v8h-1z M144,138h3v3h-3z M21,139h6v6h-6z M52,139h2v1h-2z M65,139h2v7h-2z M53,140h1v1h-1z M67,140h1v2h-1z M100,140h1v25h-1z M113,140h2v18h-2z M56,141h1v1h-1z M101,141h1v22h-1z M112,141h1v17h-1z M141,141h1v3h-1z M27,142h6v3h-6z M102,142h1v20h-1z M108,142h4v16h-4z M103,143h1v18h-1z M107,143h1v18h-1z M33,144h1v4h-1z M104,144h3v17h-3z M28,145h5v3h-5z M34,145h1v5h-1z M36,145h4v6h-4z M63,145h1v1h-1z M35,146h1v5h-1z M66,146h1v1h-1z M40,147h1v4h-1z M41,149h6v4h-6z M126,150h2v1h-2z M47,152h7v3h-7z M42,153h5v2h-5z M116,154h5v1h-5z M48,155h7v3h-7z M55,156h6v5h-6z M54,158h1v1h-1z M108,158h1v1h-1z M61,159h6v4h-6z M67,160h1v5h-1z M60,161h1v1h-1z M68,162h1v4h-1z M62,163h5v2h-5z M69,163h3v5h-3z M72,164h1v4h-1z M78,165h1v7h-1z M73,166h5v5h-5z M89,168h1v3h-1z M74,171h4v1h-4z"/></svg>';


  // Minimal non-blocking Toast System
  function showToast(message, type = 'success') {
    let container = document.getElementById('copilot-toast-container');
    if (!container) {
      container = document.createElement('div');
      container.id = 'copilot-toast-container';
      container.className = 'toast-container';
      document.body.appendChild(container);
    }
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    const icon = type === 'success' ? '✓' : '⚠';
    toast.innerHTML = `<span style="font-weight: bold; margin-right: 4px;">${icon}</span><span>${escapeHtml(message)}</span>`;
    container.appendChild(toast);
    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transform = 'translateY(6px)';
      toast.style.transition = 'all 0.2s ease';
      setTimeout(() => toast.remove(), 200);
    }, 2800);
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
  let currentQueuedMode = localStorage.getItem('clay_queued_mode') || 'queue';
  let currentSecurityMode = localStorage.getItem('clay_security_mode') || 'sandboxed';
  let currentAutoExecution = localStorage.getItem('clay_auto_exec') || 'always';
  let currentAllowNetwork = localStorage.getItem('clay_allow_net') !== 'false';
  let detectedSystem = { os: 'linux', is_windows: false, shell: 'bash' };

  // 1. Queued Messages Segmented Toggle
  const queuedToggle = document.getElementById('toggle-queued-messages');
  if (queuedToggle) {
    const buttons = queuedToggle.querySelectorAll('.segmented-toggle-btn');
    buttons.forEach((btn) => {
      if (btn.getAttribute('data-value') === currentQueuedMode) {
        btn.classList.add('active');
      } else {
        btn.classList.remove('active');
      }
      btn.addEventListener('click', () => {
        buttons.forEach((b) => b.classList.remove('active'));
        btn.classList.add('active');
        currentQueuedMode = btn.getAttribute('data-value');
        localStorage.setItem('clay_queued_mode', currentQueuedMode);
        showToast(`Queued Messages: ${btn.textContent}`, 'info');
      });
    });
  }

  // 2. 3-Card Security Mode Grid
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
        }).catch(() => {});
      }
    });
  });

  // 3. Terminal Controls
  const selectAutoExec = document.getElementById('select-auto-execution');
  if (selectAutoExec) {
    selectAutoExec.value = currentAutoExecution;
    selectAutoExec.addEventListener('change', () => {
      currentAutoExecution = selectAutoExec.value;
      localStorage.setItem('clay_auto_exec', currentAutoExecution);
    });
  }

  const chkSandboxNetwork = document.getElementById('chk-sandbox-network');
  if (chkSandboxNetwork) {
    chkSandboxNetwork.checked = currentAllowNetwork;
    chkSandboxNetwork.addEventListener('change', () => {
      currentAllowNetwork = chkSandboxNetwork.checked;
      localStorage.setItem('clay_allow_net', currentAllowNetwork ? 'true' : 'false');
    });
  }

  // 4. Keyboard Shortcuts Modal / Toast
  const btnKbShortcuts = document.getElementById('btn-kb-shortcuts');
  if (btnKbShortcuts) {
    btnKbShortcuts.addEventListener('click', () => {
      showToast('Shortcuts: Enter = Send, Shift+Enter = Newline, Ctrl+C = Interrupt Agent', 'info');
    });
  }

  // 5. Host OS Autodetection & Dynamic CLI Chips
  async function detectSystemAndInitChips() {
    try {
      const res = await fetch(`${BACKEND_URL}/api/system/info`);
      if (res.ok) {
        const info = await res.json();
        detectedSystem = info;
        const osBadge = document.getElementById('terminal-os-badge');
        if (osBadge) {
          osBadge.textContent = `${info.os.toUpperCase()} : ${info.shell}`;
        }

        // Dynamically populate CLI chips for host OS
        const quickChipsContainer = document.getElementById('terminal-quick-chips');
        if (quickChipsContainer) {
          quickChipsContainer.innerHTML = '<span class="terminal-quick-chips-label">Quick:</span>';
          const chips = info.is_windows ? [
            { label: 'syntax check', cmd: 'python -m py_compile backend/app/main.py' },
            { label: 'pytest', cmd: 'python -m pytest' },
            { label: 'git status', cmd: 'git status' },
            { label: 'dir', cmd: 'dir' }
          ] : [
            { label: 'syntax check', cmd: 'python3 -m py_compile backend/app/main.py' },
            { label: 'pytest', cmd: 'pytest' },
            { label: 'git status', cmd: 'git status' },
            { label: 'ls -la', cmd: 'ls -la' }
          ];

          chips.forEach((c) => {
            const chipEl = document.createElement('span');
            chipEl.className = 'cli-chip';
            chipEl.setAttribute('data-cmd', c.cmd);
            chipEl.textContent = c.label;
            chipEl.addEventListener('click', () => executeCliCommand(c.cmd));
            quickChipsContainer.appendChild(chipEl);
          });
        }
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
        const currentVal = modelSelect.value || currentModel;
        modelSelect.innerHTML = '';
        data.models.forEach((m) => {
          const opt = document.createElement('option');
          opt.value = m;
          opt.textContent = m;
          if (m === currentVal || (m.includes('gemma4') && currentVal.includes('gemma4'))) {
            opt.selected = true;
          }
          modelSelect.appendChild(opt);
        });
        currentModel = modelSelect.value;
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
        exclusion_patterns: privacyPatterns ? privacyPatterns.value.trim() : '.env, *.pem',
        security_mode: currentSecurityMode,
        auto_execution: currentAutoExecution,
        allow_network: currentAllowNetwork
      };

      if (isVsCode) {
        vscode.postMessage({ command: 'saveSettings', settings: payload });
      } else {
        try {
          const res = await fetch(`${BACKEND_URL}/api/settings`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
          });
          if (res.ok) {
            btnSaveSettings.textContent = 'Settings Saved!';
            setTimeout(() => (btnSaveSettings.textContent = 'Save All Settings'), 2000);
            updateActiveModelDisplays(payload.model);
            showToast('Settings saved successfully', 'success');
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
          currentTemperature = s.temperature;
        }
        if (s.num_ctx && ctxSelect) {
          ctxSelect.value = s.num_ctx.toString();
          currentNumCtx = s.num_ctx;
        }
        if (s.num_predict && maxTokensSelect) {
          maxTokensSelect.value = s.num_predict.toString();
          currentNumPredict = s.num_predict;
        }
        if (s.privacy_filter !== undefined && privacyCheck) {
          privacyCheck.checked = s.privacy_filter;
        }
        if (s.exclusion_patterns && privacyPatterns) {
          privacyPatterns.value = s.exclusion_patterns;
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
        if (s.auto_execution && selectAutoExec) {
          selectAutoExec.value = s.auto_execution;
          currentAutoExecution = s.auto_execution;
          localStorage.setItem('clay_auto_exec', currentAutoExecution);
        }
        if (s.allow_network !== undefined && chkSandboxNetwork) {
          chkSandboxNetwork.checked = s.allow_network;
          currentAllowNetwork = s.allow_network;
          localStorage.setItem('clay_allow_net', currentAllowNetwork ? 'true' : 'false');
        }
        if (s.queued_mode && queuedToggle) {
          currentQueuedMode = s.queued_mode;
          localStorage.setItem('clay_queued_mode', currentQueuedMode);
          queuedToggle.querySelectorAll('.segmented-toggle-btn').forEach((b) => {
            if (b.getAttribute('data-value') === currentQueuedMode) {
              b.classList.add('active');
            } else {
              b.classList.remove('active');
            }
          });
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
  let currentAssistantTextEl = null;

  let isChatStreaming = false;
  const messageQueue = [];

  async function sendChatMessage() {
    if (!chatInput) return;
    const text = chatInput.value.trim();
    if (!text) return;

    // If currently streaming and queued mode is active, queue the follow-up message
    if (isChatStreaming && currentQueuedMode === 'queue') {
      messageQueue.push(text);
      chatInput.value = '';
      chatInput.style.height = 'auto';
      showToast(`Follow-up message queued (Queue depth: ${messageQueue.length})`, 'info');
      return;
    }

    await dispatchChatMessage(text);
  }

  async function dispatchChatMessage(text) {
    if (!text) return;
    isChatStreaming = true;

    // Hide empty state
    if (chatEmptyState) chatEmptyState.style.display = 'none';

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
        security_mode: currentSecurityMode
      });
      isChatStreaming = false;
    } else {
      try {
        const response = await fetch(`${BACKEND_URL}/api/chat`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            query: text,
            workspace_path: currentRepoPath,
            security_mode: currentSecurityMode,
            stream: true
          })
        });

        if (!response.ok) {
          appendStreamToken(`\n[Backend Error ${response.status}: Failed to stream]`);
          isChatStreaming = false;
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
        appendStreamToken(`\n[Connection Error: ${err.message}]`);
      } finally {
        isChatStreaming = false;
        // Process next message in queue if any
        if (messageQueue.length > 0 && currentQueuedMode === 'queue') {
          const nextText = messageQueue.shift();
          setTimeout(() => dispatchChatMessage(nextText), 350);
        }
      }
    }
  }

  if (sendBtn) sendBtn.addEventListener('click', sendChatMessage);

  if (chatInput) {
    chatInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendChatMessage();
      }
    });

    chatInput.addEventListener('input', () => {
      chatInput.style.height = 'auto';
      chatInput.style.height = Math.min(chatInput.scrollHeight, 120) + 'px';
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
    const card = document.createElement('div');
    card.className = 'assistant-card';

    card.innerHTML = `
      <div class="assistant-header">
        <div class="pixel-prompt-badge" title="CLAY">
          <svg class="pixel-prompt-glyph" viewBox="0 0 24 14" width="22" height="12" fill="currentColor" shape-rendering="crispEdges">
            <path d="M1,2 h2 v2 h-2 z M3,4 h2 v2 h-2 z M5,6 h2 v2 h-2 z M3,8 h2 v2 h-2 z M1,10 h2 v2 h-2 z M9,10 h10 v2 h-10 z"/>
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
    scrollChatToBottom();
  }

  let accumulatedRawText = '';

  function appendStreamToken(token) {
    if (!currentAssistantTextEl) return;
    accumulatedRawText += token;
    currentAssistantTextEl.innerHTML = formatMarkdown(accumulatedRawText) + '<span class="streaming-cursor"></span>';
    scrollChatToBottom();
  }

  function onStreamFinished(fullText, citations) {
    if (currentAssistantTextEl) {
      currentAssistantTextEl.innerHTML = formatMarkdown(fullText);
      const card = currentAssistantTextEl.closest('.assistant-card');

      // Detect unified diff
      const diffMatch = fullText.match(/```(?:diff)?\n([\s\S]*?@@[\s\S]*?)```/);
      if (diffMatch) {
        const rawDiff = diffMatch[1];
        stageDiff('solution.py', rawDiff);
      }

      // Detect code change intent in user query or response
      const codeIntentRegex = /\b(create|write|add|implement|fix|refactor|change|update|make|delete|build|print|patch|edit|function|script|class|module|snippet)\b/i;
      const hasCodeBlock = fullText.includes('```');

      if (card && (codeIntentRegex.test(lastUserQuery) || hasCodeBlock)) {
        if (!card.querySelector('.btn-run-in-agent')) {
          const actionDiv = document.createElement('div');
          actionDiv.className = 'chat-agent-action';
          const goalTask = lastUserQuery || 'Implement requested code changes';
          actionDiv.innerHTML = `
            <button class="btn-run-in-agent" title="Execute these exact changes in the Agent tab">
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

          actionDiv.querySelector('.btn-run-in-agent').addEventListener('click', () => {
            switchTab('agent');
            if (agentGoalInput) {
              agentGoalInput.value = goalTask;
            }
            runAgentTask();
          });
        }
      }
    }
    accumulatedRawText = '';
    scrollChatToBottom();
  }

  function scrollChatToBottom() {
    const container = document.getElementById('view-chat');
    if (container) container.scrollTop = container.scrollHeight;
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

  async function runAgentTask() {
    const goal = agentGoalInput ? agentGoalInput.value.trim() : '';
    if (!goal) return;

    isAgentRunning = true;
    if (btnRunAgent) btnRunAgent.style.display = 'none';
    if (btnStopAgent) btnStopAgent.style.display = 'flex';
    if (agentTerminalStatus) {
      agentTerminalStatus.textContent = 'RUNNING';
      agentTerminalStatus.style.color = '#4CAF50';
    }
    if (agentProgressTrack) agentProgressTrack.style.display = 'block';

    appendTerminalLog(`\n$ copilot-agent --execute "${goal}"`);
    appendTerminalLog(`[agent] Target Model: ${currentModel}`);

    // Set up step UI
    renderInitialSteps(goal);

    try {
      // Step 1: Scan AST & Symbols
      if (!isAgentRunning) return;
      updateStepUI(1, 'active');
      setAgentProgress(15, 'Step 1 of 3: Scanning repository AST symbols...');
      const step1Res = await executeStep(1, goal);
      if (!isAgentRunning) return;
      updateStepUI(1, 'done', step1Res.log);
      appendTerminalLog(`[step 1] ${step1Res.log || 'AST symbols analyzed.'}`);

      // Step 2: Code Synthesis via LLM
      if (!isAgentRunning) return;
      updateStepUI(2, 'active');
      setAgentProgress(60, 'Step 2 of 3: Synthesizing code solution via Gemma 4...');
      appendTerminalLog(`[step 2] Querying ${currentModel} for code changes...`);
      const step2Res = await executeStep(2, goal);
      if (!isAgentRunning) return;
      updateStepUI(2, 'done', 'Solution synthesized cleanly.');
      appendTerminalLog(`[step 2] ${step2Res.log || 'Code synthesized.'}`);
      if (step2Res.diff) {
        stageDiff('solution.py', step2Res.diff);
        appendTerminalLog(`[diff] Staged surgical diff in Diff Reviewer tab.`);
      }

      // Step 3: AST Syntax Verification
      if (!isAgentRunning) return;
      updateStepUI(3, 'active');
      setAgentProgress(90, 'Step 3 of 3: Verifying syntax & integrity...');
      const step3Res = await executeStep(3, goal);
      if (!isAgentRunning) return;
      updateStepUI(3, 'done', step3Res.log);
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
    const res = await fetch(`${BACKEND_URL}/api/agent/step`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        goal,
        step_id: stepId,
        workspace_path: currentRepoPath
      })
    });
    if (!res.ok) throw new Error(`Step ${stepId} HTTP error ${res.status}`);
    return await res.json();
  }

  function renderInitialSteps(goal) {
    if (!agentStepList) return;
    agentStepList.innerHTML = `
      <div class="step-item step-queued" id="agent-step-1">
        <div class="step-bullet">[-]</div>
        <div><strong>1. Scan codebase for symbols matching '${escapeHtml(goal.slice(0, 30))}'</strong></div>
      </div>
      <div class="step-item step-queued" id="agent-step-2">
        <div class="step-bullet">[-]</div>
        <div><strong>2. Synthesize code changes and compute unified diff</strong></div>
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

  if (btnRunAgent) btnRunAgent.addEventListener('click', runAgentTask);
  if (btnStopAgent) btnStopAgent.addEventListener('click', () => stopAgentTask(true));

  if (agentGoalInput) {
    agentGoalInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        runAgentTask();
      }
    });
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

    // Auto-Execution Approval Check
    if (currentAutoExecution === 'prompt') {
      const proceed = window.confirm(`[CLAY Command Review]\nExecute shell command in workspace?\n\n$ ${command}`);
      if (!proceed) {
        appendTerminalLog(`[aborted] User rejected execution of '$ ${command}'`);
        return;
      }
    }

    if (agentTerminalStatus) {
      agentTerminalStatus.textContent = 'RUNNING';
      agentTerminalStatus.style.color = '#F59E0B';
    }
    appendTerminalLog(`[${currentSecurityMode.toUpperCase()}] $ ${command}`);
    try {
      const res = await fetch(`${BACKEND_URL}/api/agent/terminal/execute`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          command,
          workspace_path: currentRepoPath,
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
      executeCliCommand(inputTerminalCmd.value);
      inputTerminalCmd.value = '';
    });
    inputTerminalCmd.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        executeCliCommand(inputTerminalCmd.value);
        inputTerminalCmd.value = '';
      }
    });
  }

  document.querySelectorAll('.cli-chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      const cmd = chip.getAttribute('data-cmd');
      if (cmd) executeCliCommand(cmd);
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
          diff: stagedDiffData.diff
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
    if (diffActionBar) diffActionBar.style.display = 'none';
    if (diffSummaryLabel) diffSummaryLabel.textContent = 'Staged Changes // 0 files';
    if (diffBadgeAdd) diffBadgeAdd.textContent = '+0';
    if (diffBadgeDel) diffBadgeDel.textContent = '-0';
    if (diffStagedContainer) {
      diffStagedContainer.innerHTML = `
        <div style="text-align: center; padding: 36px 12px; color: var(--text-muted);">
          <svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" stroke-width="1.5" style="margin-bottom: 8px; opacity: 0.5;"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
          <div style="font-weight: 600; font-size: 12px; color: var(--text-secondary);">No Staged Changes</div>
          <div style="font-size: 11px; margin-top: 4px;">Changes proposed by Chat or Agent will appear here for review.</div>
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
  const mermaidCode = document.getElementById('mermaid-code');

  if (inputRepoPath) {
    inputRepoPath.value = currentRepoPath;
    const syncRepoPath = () => {
      currentRepoPath = inputRepoPath.value.trim();
      localStorage.setItem('codebase_copilot_repo_path', currentRepoPath);
      if (!isVsCode && currentRepoPath) {
        fetch(`${BACKEND_URL}/api/settings`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ repo_path: currentRepoPath })
        }).catch(() => {});
      }
    };
    inputRepoPath.addEventListener('input', syncRepoPath);
    inputRepoPath.addEventListener('change', syncRepoPath);
  }

  if (btnResetRepoPath) {
    btnResetRepoPath.addEventListener('click', () => {
      currentRepoPath = '/home/ishan/.gemini/antigravity-ide/scratch/codebase-copilot/backend';
      if (inputRepoPath) inputRepoPath.value = currentRepoPath;
      localStorage.setItem('codebase_copilot_repo_path', currentRepoPath);
    });
  }

  async function scanRepository() {
    const path = inputRepoPath ? inputRepoPath.value.trim() : currentRepoPath;
    if (repoStatusLabel) repoStatusLabel.textContent = 'Scanning AST & Call Graph...';
    if (btnScanRepo) btnScanRepo.disabled = true;

    if (isVsCode) {
      vscode.postMessage({ command: 'scanWorkspace', path });
    } else {
      try {
        const scanRes = await fetch(`${BACKEND_URL}/api/index/scan`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ workspace_path: path })
        });
        const graphRes = await fetch(`${BACKEND_URL}/api/graph/mermaid`);

        if (scanRes.ok) {
          const scanData = await scanRes.json();
          const graphData = graphRes.ok ? await graphRes.json() : { mermaid: '' };
          applyScanResults(scanData, graphData.mermaid);
        }
      } catch (err) {
        if (repoStatusLabel) repoStatusLabel.textContent = `Scan failed: ${err.message}`;
      } finally {
        if (btnScanRepo) btnScanRepo.disabled = false;
      }
    }
  }

  function applyScanResults(scanData, mermaid) {
    if (repoStatsText) {
      repoStatsText.textContent = `Indexed: ${scanData.filesCount} Files // ${scanData.symbolsCount} Functions & Classes // ${scanData.chunksCount} Vector Chunks`;
    }
    if (repoStatusLabel) {
      repoStatusLabel.textContent = 'Indexed & Synced (SQLite Graph Active)';
    }
    if (mermaidCode && mermaid) {
      mermaidCode.textContent = mermaid;
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
      case 'ollamaStatusResult':
        applyOllamaStatus(msg.data);
        break;

      case 'settingsSaved':
        testOllamaConnection();
        break;

      case 'streamToken':
        appendStreamToken(msg.token);
        break;

      case 'streamComplete':
        onStreamFinished(accumulatedRawText, msg.citations);
        if (msg.diff) stageDiff('patch.diff', msg.diff);
        break;

      case 'scanResult':
        applyScanResults(msg.status, msg.mermaid);
        break;

      case 'renderMermaid':
        if (mermaidCode) mermaidCode.textContent = msg.mermaid;
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
    let out = escapeHtml(text);

    // Code blocks
    out = out.replace(/```([a-z]*)\n([\s\S]*?)```/g, function (_, lang, code) {
      return `<pre style="background:var(--surface-subtle); padding:8px; border-radius:8px; font-family:var(--font-mono); font-size:11px; margin:6px 0; overflow-x:auto;"><code>${code}</code></pre>`;
    });

    // Inline code
    out = out.replace(/`([^`]+)`/g, '<code>$1</code>');

    // Bold
    out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

    // Italics
    out = out.replace(/\*([^*]+)\*/g, '<em>$1</em>');

    // Line breaks
    out = out.replace(/\n/g, '<br/>');

    return out;
  }

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
    } catch (e) {}
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
  } catch (e) {}
})();

