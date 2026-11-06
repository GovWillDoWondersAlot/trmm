/**
 * Tactical RMM — Master Dashboard & HVNC Controller App
 * Real-time event-driven endpoint management with instant auto-discovery.
 */

// State Management
let dashboardWs = null;
let currentAgents = [];
let previousAgentMap = new Map();
let activeFilter = 'all';
let isInitialLoad = true;
let pollTimer = null;

// HVNC Session State
let viewerWs = null;
let activeAgentId = null;
let hvncCanvas = null;
let hvncCtx = null;
let hasHvncFocus = false;
let hvncFrameCount = 0;
let lastFpsTime = performance.now();
let isCanvasFullscreen = false;
let hvncDisplayMode = 'fit'; // 'fit' | 'fill' | 'native'
let isRenderingFrame = false;
let pendingFrameBlob = null;
let lastMouseMoveTime = 0;
let pendingMouseMove = null;

// Initialize Dashboard on DOM Ready
document.addEventListener("DOMContentLoaded", () => {
    const toastBox = document.getElementById("toastContainer");
    if (toastBox) toastBox.innerHTML = "";

    hvncCanvas = document.getElementById("hvncCanvas");
    if (hvncCanvas) {
        hvncCtx = hvncCanvas.getContext("2d");
        initHvncCanvasListeners();
    }

    // Set default WebSocket server URL
    initDefaultServerUrl();

    // Start Live Real-time Detection
    initDashboardSocket();
    startSmartPolling();
    initInstallationsTab();
    loadGeneratedPackagesCache();

    // Global Dismiss Handlers (Esc key)
    initKeyboardShortcuts();

    // Window and Fullscreen Resize Handlers for HVNC Canvas
    window.addEventListener("resize", () => {
        if (activeAgentId) resizeHvncDisplay();
    });

    document.addEventListener("fullscreenchange", () => {
        const isFs = !!document.fullscreenElement;
        isCanvasFullscreen = isFs;
        const expIcon = document.getElementById("fsIconExpand");
        const compIcon = document.getElementById("fsIconCompress");
        if (expIcon && compIcon) {
            expIcon.style.display = isFs ? "none" : "block";
            compIcon.style.display = isFs ? "block" : "none";
        }
        resizeHvncDisplay();
    });
});

/**
 * Automatically sets the default server WebSocket URL based on host
 */
function initDefaultServerUrl() {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = window.location.host;
    const serverUrlInput = document.getElementById("genServerUrl");
    if (serverUrlInput) {
        serverUrlInput.value = `${proto}//${host}`;
    }
}

/**
 * 1-Click Detect LAN IP from Server for easy multi-machine deployment
 */
async function detectLanIp(forceLocal = false) {
    const host = window.location.hostname;
    const isLocalhost = host === "localhost" || host === "127.0.0.1" || host === "[::1]";

    // If we are already on a public live server (e.g. rmm.swiftvtu.com), preserve that public host unless forceLocal is requested
    if (!isLocalhost && !forceLocal) {
        initDefaultServerUrl();
        return;
    }

    try {
        const res = await fetch("/api/system/info");
        if (!res.ok) throw new Error('Server address lookup failed');
        const data = await res.json();
        if (data.local_ips && data.local_ips.length > 0) {
            const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
            const chosenIp = data.local_ips[0];
            const port = data.default_port || 8000;
            const targetUrl = `${proto}//${chosenIp}:${port}`;
            
            const serverInput = document.getElementById("genServerUrl");
            if (serverInput) {
                serverInput.value = targetUrl;
            }
            if (forceLocal) {
                showToast("Network IP Detected", `Set server URL to: ${targetUrl}`, "info");
            }
        } else {
            initDefaultServerUrl();
        }
    } catch (e) {
        initDefaultServerUrl();
    }
}

/**
 * Primary Real-Time WebSocket Tunnel for Dashboard Live Telemetry
 */
function initDashboardSocket() {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${window.location.host}/ws/dashboard`;

    try {
        dashboardWs = new WebSocket(url);

        dashboardWs.onopen = () => {
            setServerStatus(true);
        };

        dashboardWs.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                if (data.type === "agents_update") {
                    handleIncomingAgentList(data.agents || []);
                }
            } catch (e) {
                console.error("Dashboard WS parse error:", e);
            }
        };

        dashboardWs.onclose = (evt) => {
            setServerStatus(false);
            if (evt && evt.code === 4001) {
                // Session unauthorized / logged out
                window.location.href = "/login";
                return;
            }
            // Reconnect after 2 seconds
            setTimeout(initDashboardSocket, 2000);
        };

        dashboardWs.onerror = () => {
            setServerStatus(false);
        };
    } catch (e) {
        setServerStatus(false);
        setTimeout(initDashboardSocket, 3000);
    }
}

/**
 * Fallback Smart Polling to guarantee 100% zero-reload detection
 */
function startSmartPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
        try {
            const res = await fetch("/api/agents");
            if (res.ok) {
                const data = await res.json();
                handleIncomingAgentList(data.agents || []);
            }
        } catch (e) {
            // Polling error handled gracefully
        }
    }, 3000);
}

const machineAlertThrottle = new Map();

/**
 * Handles incoming agents list, triggers alerts for new connections, and re-renders
 */
function handleIncomingAgentList(agents) {
    const now = Date.now();

    // Check for newly connected endpoints with 60-second machine-level debounce
    agents.forEach(agent => {
        const machineKey = (agent.hostname || agent.local_ip || agent.agent_id || 'unknown').toLowerCase().trim();
        const lastAlertTime = machineAlertThrottle.get(machineKey) || 0;
        const prev = previousAgentMap.get(agent.agent_id);

        if (!isInitialLoad && agent.status === "online") {
            const isFreshConnect = !prev || prev.status === "offline";
            // Only alert if it's a genuine transition AND at least 60s since last notification for this machine
            if (isFreshConnect && (now - lastAlertTime > 60000)) {
                machineAlertThrottle.set(machineKey, now);
                showToast("🟢 Target Endpoint Online!", `${agent.hostname || agent.endpoint_tag} (${agent.local_ip || 'Local'}) connected.`, "success");
                agent._justConnected = true;
            }
        }
    });

    // Sort stably: Online first, then by tag/hostname/id (prevents reshuffling on heartbeats)
    agents.sort((a, b) => {
        const aOnline = a.status === "online" ? 0 : 1;
        const bOnline = b.status === "online" ? 0 : 1;
        if (aOnline !== bOnline) return aOnline - bOnline;

        const aTag = (a.endpoint_tag || a.hostname || a.agent_id || "").toLowerCase();
        const bTag = (b.endpoint_tag || b.hostname || b.agent_id || "").toLowerCase();
        if (aTag !== bTag) return aTag.localeCompare(bTag);

        return (a.agent_id || "").localeCompare(b.agent_id || "");
    });

    currentAgents = agents;
    isInitialLoad = false;

    renderAgentTable();
    updateStats();
}

function setServerStatus(isOnline) {
    const indicator = document.getElementById("wsIndicator");
    const statusText = document.getElementById("wsStatusText");
    const pingText = document.getElementById("wsPing");

    if (isOnline) {
        if (indicator) indicator.className = "status-indicator-dot online";
        if (statusText) statusText.innerText = "Master Live";
        if (pingText) pingText.innerText = "Sync: Realtime Active";
    } else {
        if (indicator) indicator.className = "status-indicator-dot offline";
        if (statusText) statusText.innerText = "Reconnecting...";
        if (pingText) pingText.innerText = "Sync: Offline Fallback";
    }
}

function manualRefresh() {
    fetch("/api/agents")
        .then(r => r.json())
        .then(data => {
            handleIncomingAgentList(data.agents || []);
            showToast("Synced", "Endpoint directory refreshed.", "info");
        })
        .catch(err => {
            showToast("Sync Failed", err.message, "error");
        });
}

function updateStats() {
    const total = currentAgents.length;
    const online = currentAgents.filter(a => a.status === "online").length;
    const offline = total - online;
    const sessions = currentAgents.filter(a => a.is_streaming).length;

    document.getElementById("statTotal").innerText = total;
    document.getElementById("statOnline").innerText = online;
    document.getElementById("statSessions").innerText = sessions;

    document.getElementById("countAll").innerText = total;
    document.getElementById("countOnline").innerText = online;
    document.getElementById("countOffline").innerText = offline;
}

/**
 * Renders the table with animations, status badges, and action buttons
 */
function renderAgentTable() {
    const tbody = document.getElementById("agentTableBody");
    const emptyState = document.getElementById("emptyState");
    const summaryText = document.getElementById("agentCountSummary");
    const searchVal = document.getElementById("searchInput").value.toLowerCase().trim();

    tbody.innerHTML = "";

    const filtered = currentAgents.filter(agent => {
        const matchesFilter = activeFilter === "all" || 
                             (activeFilter === "online" && agent.status === "online") ||
                             (activeFilter === "offline" && agent.status !== "online");

        const matchesSearch = !searchVal ||
            (agent.hostname && agent.hostname.toLowerCase().includes(searchVal)) ||
            (agent.endpoint_tag && agent.endpoint_tag.toLowerCase().includes(searchVal)) ||
            (agent.agent_id && agent.agent_id.toLowerCase().includes(searchVal)) ||
            (agent.local_ip && agent.local_ip.includes(searchVal)) ||
            (agent.username && agent.username.toLowerCase().includes(searchVal)) ||
            (agent.os && agent.os.toLowerCase().includes(searchVal));

        return matchesFilter && matchesSearch;
    });

    if (summaryText) {
        summaryText.innerText = `Showing ${filtered.length} of ${currentAgents.length} endpoints`;
    }

    if (filtered.length === 0) {
        emptyState.style.display = "block";
    } else {
        emptyState.style.display = "none";
    }

    filtered.forEach(agent => {
        const tr = document.createElement("tr");
        if (agent._justConnected) {
            tr.classList.add("glow-new-agent");
            setTimeout(() => tr.classList.remove("glow-new-agent"), 3500);
            delete agent._justConnected;
        }

        const isOnline = agent.status === "online";
        const statusBadge = isOnline
            ? `<span class="status-badge online"><span class="dot"></span> Online</span>`
            : `<span class="status-badge offline"><span class="dot"></span> Offline</span>`;

        const lastSeen = agent.last_heartbeat 
            ? formatTimeAgo(agent.last_heartbeat)
            : 'Just now';

        const tagDisplay = escapeHtml(agent.endpoint_tag || agent.hostname || 'Endpoint');
        const hostnameDisplay = escapeHtml(agent.hostname || 'DESKTOP-TARGET');
        const userDisplay = escapeHtml(agent.username || 'SYSTEM');

        const osLower = (agent.os || '').toLowerCase();
        const archLower = (agent.arch || '').toLowerCase();
        const platformLower = (agent.platform || '').toLowerCase();
        const deviceTypeLower = (agent.device_type || '').toLowerCase();

        // Check if agent_id was generated as an Android package in package history
        const matchingPkg = Array.isArray(allGeneratedPackages) ? allGeneratedPackages.find(p => p.agent_id === agent.agent_id) : null;
        const pkgIsAndroid = matchingPkg && (
            (matchingPkg.arch && matchingPkg.arch.toLowerCase().includes('android')) ||
            Boolean(matchingPkg.apk_filename) ||
            Boolean(matchingPkg.apk_url) ||
            Boolean(matchingPkg.sh_filename)
        );

        // Strict Android vs Windows determination:
        // Android if flagged as is_android, or arch/os/platform includes android, or matches an Android package
        const isAndroidAgent = Boolean(
            agent.is_android === true || 
            archLower.includes('android') || 
            osLower.includes('android') || 
            platformLower.includes('android') ||
            deviceTypeLower.includes('phone') ||
            deviceTypeLower.includes('android') ||
            pkgIsAndroid ||
            (!osLower.includes('win') && (osLower.includes('droid') || archLower.includes('arm')))
        );

        // Backstage is strictly reserved for Windows endpoints ONLY
        const isWindowsAgent = !isAndroidAgent && (
            osLower.includes('win') ||
            (!agent.os && !archLower.includes('android'))
        );

        const deviceName = escapeHtml(agent.device_name || agent.hostname || agent.model || agent.endpoint_tag || 'Android Device');
        const isCurtainLocked = (agent.curtain_active !== undefined) ? Boolean(agent.curtain_active) : Boolean(agentCurtainStates[agent.agent_id]);

        tr.innerHTML = `
            <td>${statusBadge}</td>
            <td>
                <div class="tag-name">
                    ${isAndroidAgent ? `<span style="color: #34d399; font-weight: 600;">📱 ( Phone )</span>` : `<span>${tagDisplay}</span>`}
                </div>
                <span class="agent-id-pill" title="Click to copy Agent ID" onclick="copyText('${agent.agent_id}', 'Agent ID copied!')">ID: ${escapeHtml(agent.agent_id)}</span>
            </td>
            <td>
                ${isAndroidAgent ? `
                <div class="hostname-title" style="font-weight: 600; color: #f3f4f6;">${deviceName}</div>
                <div class="user-sub" style="color: #94a3b8;">
                    <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2"><rect width="14" height="20" x="5" y="2" rx="2" ry="2"/><path d="M12 18h.01"/></svg>
                    <span>${escapeHtml(agent.endpoint_tag || deviceName)}</span>
                </div>
                ` : `
                <div class="hostname-title">${hostnameDisplay}</div>
                <div class="user-sub">
                    <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
                    <span>${userDisplay}</span>
                </div>
                `}
            </td>
            <td>
                ${isAndroidAgent ? `
                <div style="display: flex; flex-direction: column; gap: 4px;">
                    <span class="os-pill" style="border-color: rgba(16, 185, 129, 0.4); color: #34d399; font-weight: 500;">
                        <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2"><rect width="14" height="20" x="5" y="2" rx="2" ry="2"/><path d="M12 18h.01"/></svg>
                        Android device
                    </span>
                    ${agent.is_device_owner ? `
                        <span class="arch-chip" style="background: rgba(16, 185, 129, 0.15); border: 1px solid rgba(16, 185, 129, 0.4); color: #10b981; font-weight: 600;" title="Android Enterprise Corporate Device Owner Provisioned (Uninstall Blocked)">
                            🛡️ Corporate Device Owner
                        </span>
                    ` : (agent.is_device_admin ? `
                        <span class="arch-chip" style="background: rgba(56, 189, 248, 0.15); border: 1px solid rgba(56, 189, 248, 0.4); color: #38bdf8;" title="Device Administrator Active">
                            🛡️ Device Admin
                        </span>
                    ` : '')}
                </div>
                ` : `
                <span class="os-pill">
                    <svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor"><path d="M0 3.449L9.75 2.1v9.451H0m10.949-9.602L24 0v11.4H10.949M0 12.6h9.75v9.451L0 20.699M10.949 12.6H24V24l-12.901-1.799"/></svg>
                    ${escapeHtml(agent.os || 'Windows')}
                </span>
                <span class="arch-chip">${escapeHtml(agent.arch || 'x64')}</span>
                `}
            </td>
            <td>
                <span class="ip-link" title="Click to copy IP" onclick="copyText('${agent.local_ip || '127.0.0.1'}', 'IP Address copied!')">${escapeHtml(agent.local_ip || '127.0.0.1')}</span>
            </td>
            <td>
                <div class="specs-info">
                    <strong>${isAndroidAgent ? (agent.battery !== undefined ? '🔋 ' + agent.battery + '% Battery' : (agent.ram_gb ? agent.ram_gb + ' GB RAM' : 'Mobile Active')) : (agent.ram_gb ? agent.ram_gb + ' GB RAM' : 'RAM: Active')}</strong>
                </div>
            </td>
            <td>
                <div class="build-info-cell">
                    <span class="version-chip ${agent.has_update ? 'update-available' : 'latest'}">
                        ${escapeHtml(agent.update_version || '2.8.0')}
                    </span>
                    ${agent.has_update ? `
                        <button class="badge-update-available" onclick="promptAgentUpdate('${agent.agent_id}', event)" title="Click to review and apply update">
                            <span class="dot-amber"></span> Update Available
                        </button>
                    ` : `
                        <span class="badge-up-to-date">Up to date</span>
                    `}
                </div>
            </td>
            <td>
                <span class="time-seen">${lastSeen}</span>
            </td>
            <td style="text-align: right;">
                <div class="action-btn-group">
                    ${isAndroidAgent ? `
                        <button class="btn-action-control btn-action-update ${agent.has_update ? 'btn-has-update' : ''}" onclick="pushAgentUpdate('${agent.agent_id}', event)" title="${isOnline ? (agent.has_update ? 'Update Available: Click to push new build to device' : 'Push latest updated build to this Android device') : 'Device offline: Click to queue automatic update upon reconnection'}">
                            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                            <span>${agent.has_update ? 'Update App' : 'Push Update'}</span>
                        </button>
                    ` : (agent.has_update ? `
                        <button class="btn-action-control btn-action-update" onclick="promptAgentUpdate('${agent.agent_id}', event)" title="Update Available: ${escapeHtml(agent.update_description || 'New features ready')}">
                            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21.5 2v6h-6M2.5 22v-6h6M2 11.5a10 10 0 0 1 18.8-4.3M22 12.5a10 10 0 0 1-18.8 4.3"/></svg>
                            <span>Update</span>
                        </button>
                    ` : `
                        <button class="btn-action-control btn-action-sync" ${!isOnline ? 'disabled' : ''} onclick="syncAgentCode('${agent.agent_id}', event)" title="Sync latest configuration">
                            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21.5 2v6h-6M2.5 22v-6h6M2 11.5a10 10 0 0 1 18.8-4.3M22 12.5a10 10 0 0 1-18.8 4.3"/></svg>
                            <span>Sync</span>
                        </button>
                    `)}
                    <button class="btn-action-control btn-action-mirror" ${!isOnline ? 'disabled' : ''} onclick="openViewerTab('${agent.agent_id}', 'mirror')" title="${isAndroidAgent ? 'Take Control: Remote Control & Shell' : 'Take Control: Mirror real desktop with physical mouse & keyboard'}">
                        <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.5"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"></rect><line x1="8" y1="21" x2="16" y2="21"></line><line x1="12" y1="17" x2="12" y2="21"></line></svg>
                        <span>Control</span>
                    </button>
                    ${isAndroidAgent ? `
                    <button class="btn-action-control btn-action-lock ${isCurtainLocked ? 'btn-active-curtain' : ''}" ${!isOnline ? 'disabled' : ''} onclick="${isCurtainLocked ? `unlockAndroidDevice('${agent.agent_id}', event)` : `openLockDeviceModal('${agent.agent_id}', event)`}" title="${isCurtainLocked ? 'Unlock Android Device (Restore Home Screen)' : 'Lock Android Device (Display Custom Message)'}">
                        <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.5"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path></svg>
                        <span>${isCurtainLocked ? 'Unlock' : 'Lock Device'}</span>
                    </button>
                    ${agent.is_device_owner ? `
                    <button class="btn-action-control" ${!isOnline ? 'disabled' : ''} onclick="toggleUninstallProtection('${agent.agent_id}', ${agent.uninstall_blocked ? 'false' : 'true'}, event)" title="${agent.uninstall_blocked ? 'Uninstall Protection Active (Device Owner blocks removal). Click to allow uninstallation.' : 'Uninstall Protection Inactive. Click to block uninstallation.'}" style="color: ${agent.uninstall_blocked ? '#34d399' : '#f59e0b'}; border-color: ${agent.uninstall_blocked ? 'rgba(16, 185, 129, 0.4)' : 'rgba(245, 158, 11, 0.4)'};">
                        <span>${agent.uninstall_blocked ? '🛡️ Protected' : '⚠️ Allow Uninstall'}</span>
                    </button>
                    ` : ''}
                    ` : `
                    <button class="btn-action-control btn-action-backstage" ${!isOnline ? 'disabled' : ''} onclick="openViewerTab('${agent.agent_id}', 'backstage')" title="Backstage: Hidden virtual desktop session (invisible to user)">
                        <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                        <span>Backstage</span>
                    </button>
                    `}
                    <button class="btn-action-delete" onclick="deleteAgentSession('${agent.agent_id}', event)" title="Remove endpoint">
                        <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18m-2 0v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6m3 0V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/></svg>
                    </button>
                </div>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

let pendingUpdateAgentId = null;

function promptAgentUpdate(agentId, event) {
    if (event) event.stopPropagation();
    const agent = currentAgents.find(a => a.agent_id === agentId);
    if (!agent) return;

    pendingUpdateAgentId = agentId;
    const tag = agent.endpoint_tag || agent.hostname || agentId;
    const targetMetaEl = document.getElementById("updateTargetMeta");
    if (targetMetaEl) targetMetaEl.innerText = `Target: ${tag} (${agent.local_ip || '127.0.0.1'})`;
    
    const versionEl = document.getElementById("updateVersionBadge");
    if (versionEl) versionEl.innerText = agent.update_version || "3.3.0-live";
    
    const descEl = document.getElementById("updateDescriptionText");
    if (descEl) descEl.innerText = agent.update_description || "Dual-mode Backstage and Take Control reliability fixes with clean windowless background execution.";

    const modal = document.getElementById("otaModal") || document.getElementById("updateModal");
    if (modal) {
        modal.style.display = "flex";
        modal.classList.add("active");
    }
}

function closeUpdateModal() {
    const modal = document.getElementById("otaModal") || document.getElementById("updateModal");
    if (modal) {
        modal.classList.remove("active");
        modal.style.display = "none";
    }
    pendingUpdateAgentId = null;
}

function closeOtaModal() {
    closeUpdateModal();
}

async function confirmPushUpdate() {
    if (!pendingUpdateAgentId) return;
    const targetId = pendingUpdateAgentId;
    closeUpdateModal();
    await pushAgentUpdate(targetId, null);
}

async function pushAgentUpdate(agentId, event) {
    if (event) event.stopPropagation();
    const agent = currentAgents.find(a => a.agent_id === agentId);
    const tag = agent ? (agent.endpoint_tag || agent.hostname || agentId) : agentId;

    try {
        showToast("Deploying Update", `Initiating auto-update for [${tag}]...`, "info");
        const res = await fetch(`/api/agents/${encodeURIComponent(agentId)}/update`, { method: "POST" });
        const data = await res.json();
        if (res.ok && data.success) {
            if (data.status === "update_queued") {
                showToast("Update Queued (Offline)", `[${tag}] is offline. Update will install automatically upon connection.`, "info");
            } else {
                showToast("Update Dispatched", `Latest app build dispatched to [${tag}]. Auto-update is executing in background.`, "success");
            }
            setTimeout(manualRefresh, 1200);
        } else {
            showToast("Update Notice", data.message || data.detail || "Update processed.", "info");
        }
    } catch (e) {
        showToast("Update Error", e.message, "error");
    }
}

async function pushAllUpdates() {
    if (!confirm("Push the latest app update to all endpoints?\n\n• Online devices will automatically download and update in the background.\n• Offline devices will update automatically upon reconnection.")) {
        return;
    }
    try {
        showToast("Deploying All Updates", "Pushing latest builds to all endpoints...", "info");
        const res = await fetch("/api/agents/update-all", { method: "POST" });
        const data = await res.json();
        if (res.ok && data.success) {
            showToast("Updates Dispatched", data.message || "All devices notified.", "success");
            setTimeout(manualRefresh, 1500);
        } else {
            showToast("Update Error", data.detail || "Failed to dispatch updates.", "error");
        }
    } catch (e) {
        showToast("Update Error", e.message, "error");
    }
}

async function deleteAllDevices() {
    const count = currentAgents.length;
    if (!confirm(`Are you sure you want to delete and unbind ALL ${count} device(s)?\n\n• All active agent sessions will be disconnected and terminated.\n• All offline and registered endpoints will be removed from Managed Endpoints.\n• This action cannot be undone.`)) {
        return;
    }
    try {
        showToast("Deleting Endpoints", "Removing all connected and enrolled devices...", "info");
        const res = await fetch("/api/agents", { method: "DELETE" });
        const data = await res.json();
        if (res.ok && data.success) {
            currentAgents = [];
            updateStats();
            renderAgentTable();
            showToast("Endpoints Cleared", data.message || "All devices have been successfully deleted.", "success");
            setTimeout(manualRefresh, 500);
        } else {
            showToast("Delete Error", data.detail || data.message || "Failed to delete devices.", "error");
        }
    } catch (e) {
        showToast("Delete Error", e.message, "error");
    }
}

async function syncAgentCode(agentId, event, force = false) {
    if (event) event.stopPropagation();
    try {
        const agent = currentAgents.find(a => a.agent_id === agentId);
        // If an update is available and this was clicked without prompt, open prompt
        if (!force && agent && agent.has_update) {
            promptAgentUpdate(agentId, event);
            return;
        }

        const res = await fetch(`/api/agent/${agentId}/sync`, { method: "POST" });
        if (res.ok) {
            const data = await res.json();
            if (data.status === "no_update" || data.has_update === false) {
                showToast("Already Up to Date", "Endpoint is already running the latest build.", "info");
            } else {
                showToast("Update Deployed", data.description || "OTA update applied successfully.", "success");
                // Refresh list to update badge
                setTimeout(manualRefresh, 1000);
            }
        } else {
            const err = await res.json();
            showToast("Update Error", err.detail || "Failed to trigger update", "error");
        }
    } catch (e) {
        showToast("Sync Error", e.message, "error");
    }
}

async function deleteAgentSession(agentId, event) {
    if (event) event.stopPropagation();
    if (!confirm(`Are you sure you want to terminate and disconnect endpoint [${agentId}]?`)) return;
    try {
        const res = await fetch(`/api/agents/${agentId}`, { method: 'DELETE' });
        if (res.ok) {
            currentAgents = currentAgents.filter(a => a.agent_id !== agentId);
            updateStats();
            renderAgentTable();
            showToast("Session Removed", `Endpoint session [${agentId}] removed & service stopped.`, "info");
        } else {
            showToast("Error", "Could not remove session.", "error");
        }
    } catch (e) {
        console.error(e);
        showToast("Error", "Failed to contact server.", "error");
    }
}

const agentCurtainStates = {};

function openLockDeviceModal(agentId, event) {
    if (event) event.stopPropagation();
    const modal = document.getElementById("lockDeviceModal");
    if (!modal) return;

    document.getElementById("lockTargetAgentId").value = agentId;
    const metaEl = document.getElementById("lockTargetMeta");
    if (metaEl) {
        metaEl.innerText = `Target Android Device: ${agentId}`;
    }

    modal.classList.add("active");
    modal.style.display = "flex";
    modal.style.opacity = "1";
    setTimeout(() => {
        const titleInput = document.getElementById("lockTitleInput");
        if (titleInput) {
            titleInput.focus();
            titleInput.select();
        }
    }, 100);
}

function closeLockDeviceModal() {
    const modal = document.getElementById("lockDeviceModal");
    if (modal) {
        modal.classList.remove("active");
        modal.style.display = "none";
        modal.style.opacity = "0";
    }
}

async function submitLockDevice() {
    const agentId = document.getElementById("lockTargetAgentId").value.trim();
    if (!agentId) return;

    const title = (document.getElementById("lockTitleInput").value || "").trim();
    const message = (document.getElementById("lockMessageInput").value || "").trim();
    const subtitle = (document.getElementById("lockSubtitleInput").value || "").trim();

    const btn = document.getElementById("btnConfirmLockDevice");
    if (btn) btn.disabled = true;

    try {
        const res = await fetch(`/api/agent/${encodeURIComponent(agentId)}/curtain`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                enabled: true,
                title: title,
                message: message,
                subtitle: subtitle
            })
        });
        const data = await res.json();
        if (res.ok) {
            agentCurtainStates[agentId] = true;
            closeLockDeviceModal();
            showToast("Device Locked", `Lock activated with custom message on [${agentId}].`, "success");
            renderAgentTable();
        } else {
            showToast("Lock Error", data.detail || "Failed to dispatch lock command.", "error");
        }
    } catch (err) {
        console.error(err);
        showToast("Error", "Failed to communicate with Master Hub.", "error");
    } finally {
        if (btn) btn.disabled = false;
    }
}

async function unlockAndroidDevice(agentId, event) {
    if (event) event.stopPropagation();
    try {
        const res = await fetch(`/api/agent/${encodeURIComponent(agentId)}/curtain`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled: false })
        });
        const data = await res.json();
        if (res.ok) {
            agentCurtainStates[agentId] = false;
            showToast("Device Unlocked", `Lock dismissed on [${agentId}]. User taken to Android Home Screen.`, "info");
            renderAgentTable();
        } else {
            showToast("Unlock Error", data.detail || "Failed to unlock device.", "error");
        }
    } catch (err) {
        console.error(err);
        showToast("Error", "Failed to communicate with Master Hub.", "error");
    }
}

function lockAndroidDevice(agentId, event) {
    if (agentCurtainStates[agentId]) {
        unlockAndroidDevice(agentId, event);
    } else {
        openLockDeviceModal(agentId, event);
    }
}

async function toggleUninstallProtection(agentId, block, event) {
    if (event) event.stopPropagation();
    try {
        const action = block ? "set_uninstall_blocked" : "allow_uninstall";
        const res = await fetch(`/api/agent/${encodeURIComponent(agentId)}/policy`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: action, blocked: block })
        });
        const data = await res.json();
        if (res.ok) {
            showToast("Policy Dispatched", `Uninstall protection ${block ? 'enabled' : 'disabled'} for [${agentId}].`, "success");
            setTimeout(loadAgents, 800);
        } else {
            showToast("Policy Error", data.detail || "Failed to dispatch uninstall protection policy.", "error");
        }
    } catch (err) {
        console.error(err);
        showToast("Error", "Failed to communicate policy to Master Hub.", "error");
    }
}

function setFilter(filterType, el) {
    activeFilter = filterType;
    document.querySelectorAll(".filter-pills .pill").forEach(p => p.classList.remove("active"));
    el.classList.add("active");
    renderAgentTable();
}

function filterAgents() {
    const input = document.getElementById("searchInput");
    const clearBtn = document.getElementById("clearSearchBtn");
    if (input.value.trim().length > 0) {
        clearBtn.style.display = "block";
    } else {
        clearBtn.style.display = "none";
    }
    renderAgentTable();
}

function clearSearch() {
    const input = document.getElementById("searchInput");
    input.value = "";
    document.getElementById("clearSearchBtn").style.display = "none";
    renderAgentTable();
}

function onArchChanged(val) {
    const btn = document.getElementById("btnBuildAgent");
    if (!btn) return;
    if (val === "android") {
        btn.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><rect width="14" height="20" x="5" y="2" rx="2" ry="2"/><path d="M12 18h.01"/></svg><span>Build & Download APK</span>`;
    } else {
        btn.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><path d="m5 12 5 5L20 7"/></svg><span>Build & Download Exe</span>`;
    }
}

// -------------------------------------------------------------
// Generator Modal Management (Dismissable: Backdrop, Esc, X)
// -------------------------------------------------------------
function openGeneratorModal() {
    console.log("[Modal] Opening Agent Generator Modal...");
    const modal = document.getElementById("generatorModal");
    if (modal) {
        modal.style.display = "flex";
        modal.style.opacity = "1";
        modal.style.zIndex = "9999";
        modal.classList.add("active");
    } else {
        console.error("[Modal] generatorModal element not found in DOM!");
    }
    
    const buildCard = document.getElementById("buildResultCard");
    if (buildCard) buildCard.style.display = "none";
    
    const checkedArch = document.querySelector('input[name="genArch"]:checked');
    if (checkedArch) {
        onArchChanged(checkedArch.value);
    } else {
        const btn = document.getElementById("btnBuildAgent");
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><path d="m5 12 5 5L20 7"/></svg><span>Build & Generate Package</span>`;
        }
    }

    if (typeof detectLanIp === "function") {
        detectLanIp();
    }
}

function closeGeneratorModal() {
    const modal = document.getElementById("generatorModal");
    if (modal) {
        modal.classList.remove("active");
        modal.style.display = "none";
        modal.style.opacity = "0";
    }
}

function handleBackdropClick(event, modalId) {
    // If user clicked directly on the outer backdrop (not child modal card), dismiss
    if (event.target.id === modalId) {
        if (modalId === 'generatorModal') {
            closeGeneratorModal();
        } else if (modalId === 'allAgentsModal') {
            closeAllAgentsModal();
        } else if (modalId === 'hvncModal') {
            closeHvncSession();
        } else if (modalId === 'otaModal' || modalId === 'updateModal') {
            closeOtaModal();
        } else if (modalId === 'lockDeviceModal') {
            closeLockDeviceModal();
        }
    }
}

let currentAgentIconBase64 = null;

function previewAgentIcon(event) {
    const file = event.target.files && event.target.files[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onload = function(e) {
        currentAgentIconBase64 = e.target.result;
        window.currentAgentIconBase64 = e.target.result;
        const previewImg = document.getElementById("iconPreviewImg");
        const container = document.getElementById("iconPreviewContainer");
        const nameSpan = document.getElementById("iconFileName");

        if (previewImg) previewImg.src = currentAgentIconBase64;
        if (nameSpan) {
            nameSpan.innerText = file.name;
            nameSpan.title = file.name;
        }
        if (container) container.style.display = "inline-flex";
    };
    reader.readAsDataURL(file);
}

function clearAgentIcon() {
    currentAgentIconBase64 = null;
    window.currentAgentIconBase64 = null;
    const fileInput = document.getElementById("genIconFile");
    if (fileInput) fileInput.value = "";
    const container = document.getElementById("iconPreviewContainer");
    if (container) container.style.display = "none";
}

function copyOneLiner() {
    const input = document.getElementById("oneLinerCode");
    if (!input || !input.value) return;
    input.select();
    navigator.clipboard.writeText(input.value).then(() => {
        showToast("Copied to Clipboard", "PowerShell 1-click command copied.", "success");
    }).catch(() => {
        document.execCommand("copy");
        showToast("Copied to Clipboard", "PowerShell 1-click command copied.", "success");
    });
}

async function submitGenerateAgent() {
    const btn = document.getElementById("btnBuildAgent");
    btn.disabled = true;
    btn.innerHTML = `<span class="pulse-indicator"></span><span>Compiling Agent Package...</span>`;

    const tag = document.getElementById("genTag").value.trim() || "Workstation-01";
    const customName = document.getElementById("genCustomName") ? document.getElementById("genCustomName").value.trim() : "";
    const arch = document.querySelector('input[name="genArch"]:checked').value;
    const serverUrl = document.getElementById("genServerUrl").value.trim();
    const autoStart = document.getElementById("genAutoStart").checked;
    const hiddenMode = document.getElementById("genHiddenMode").checked;
    const iconData = window.currentAgentIconBase64 || currentAgentIconBase64 || null;

    try {
        const res = await fetch("/api/agents/generate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                endpoint_tag: tag,
                custom_name: customName || null,
                icon_base64: iconData,
                arch: arch,
                server_url: serverUrl,
                auto_start: autoStart,
                hidden_mode: hiddenMode
            })
        });

        const data = await res.json();
        if (data.success) {
            const isAndroid = (data.arch === 'android' || arch === 'android');
            const winGrid = document.getElementById("windowsDownloadGrid");
            const androidGrid = document.getElementById("androidDownloadGrid");
            const androidHint = document.getElementById("androidEnterpriseHint");
            const oneLinerLabel = document.getElementById("oneLinerLabel");

            if (isAndroid) {
                if (winGrid) winGrid.style.display = "none";
                if (androidGrid) androidGrid.style.display = "grid";
                if (androidHint) androidHint.style.display = "block";
                if (oneLinerLabel) oneLinerLabel.innerText = "1-Click Android Termux / Shell Deployment Command";

                document.getElementById("builtFilename").innerText = data.apk_filename || data.sh_filename || "android_agent";

                const apkLink = document.getElementById("downloadApkLink");
                if (apkLink) {
                    if (data.apk_download_url) {
                        apkLink.href = data.apk_download_url;
                        apkLink.setAttribute("download", data.apk_filename || "android_agent.apk");
                        apkLink.style.display = "inline-flex";
                    } else {
                        apkLink.style.display = "none";
                    }
                }

                const shLink = document.getElementById("downloadShLink");
                if (shLink) {
                    shLink.href = data.sh_download_url || data.bootstrap_download_url;
                    shLink.setAttribute("download", data.sh_filename || data.bootstrap_filename || "install.sh");
                }

                const androidZipLink = document.getElementById("downloadAndroidZipLink");
                if (androidZipLink) {
                    androidZipLink.href = data.zip_download_url;
                    androidZipLink.setAttribute("download", data.zip_filename);
                }

                document.getElementById("oneLinerCode").value = data.one_liner || "";

                document.getElementById("buildResultCard").style.display = "block";
                if (typeof loadAllGeneratedAgents === "function") {
                    loadAllGeneratedAgents(false);
                }

                // Auto-trigger direct APK download to user device
                if (data.apk_download_url) {
                    const dlA = document.createElement("a");
                    dlA.href = data.apk_download_url;
                    dlA.download = data.apk_filename || "android_agent.apk";
                    document.body.appendChild(dlA);
                    dlA.click();
                    document.body.removeChild(dlA);
                }

                btn.innerHTML = `<span>✓ APK Download Ready</span>`;
                showToast("Android APK Ready", `Downloading ${data.apk_filename || 'APK'}. Install on device to connect to RMM.`, "success");
                btn.disabled = false;
            } else {
                if (winGrid) winGrid.style.display = "grid";
                if (androidGrid) androidGrid.style.display = "none";
                if (androidHint) androidHint.style.display = "none";
                if (oneLinerLabel) oneLinerLabel.innerText = "1-Click PowerShell Deployment Command";

                document.getElementById("builtFilename").innerText = data.exe_filename || data.bootstrap_filename;
                
                // 1. Standalone Executable Setup Link
                const exeLink = document.getElementById("downloadExeLink");
                if (data.exe_download_url) {
                    exeLink.href = data.exe_download_url;
                    exeLink.setAttribute("download", data.exe_filename);
                    exeLink.style.display = "inline-flex";
                } else {
                    exeLink.href = "#";
                    exeLink.removeAttribute("download");
                    exeLink.style.display = "none";
                }

                // 2. Single Batch Installer
                const scriptLink = document.getElementById("downloadScriptLink");
                scriptLink.href = data.bootstrap_download_url;
                scriptLink.setAttribute("download", data.bootstrap_filename);

                // 3. VBScript Silent Installer
                const vbsLink = document.getElementById("downloadVbsLink");
                if (vbsLink) {
                    if (data.vbs_download_url) {
                        vbsLink.href = data.vbs_download_url;
                        vbsLink.setAttribute("download", data.vbs_filename || "install.vbs");
                        vbsLink.onclick = null;
                        vbsLink.style.display = "inline-flex";
                    } else {
                        vbsLink.href = "#";
                        vbsLink.removeAttribute("download");
                        vbsLink.onclick = null;
                        vbsLink.style.display = "none";
                    }
                }

                // 4. PowerShell One-Liner
                document.getElementById("oneLinerCode").value = data.one_liner;

                // 5. Offline Full Zip Package
                const zipLink = document.getElementById("downloadZipLink");
                zipLink.href = data.zip_download_url;
                zipLink.setAttribute("download", data.zip_filename);

                document.getElementById("buildResultCard").style.display = "block";
                if (typeof loadAllGeneratedAgents === "function") {
                    loadAllGeneratedAgents(false);
                }
                if (data.exe_download_url) {
                    btn.innerHTML = `<span>✓ Executable Ready</span>`;
                    showToast("Executable Ready", `Download ${data.exe_filename} for 1-click single-file deployment.`, "success");
                } else {
                    btn.innerHTML = `<span>ZIP Ready — Standalone EXE Fallback</span>`;
                    showToast("Package Ready", data.installer_error || "The deployment package is ready.", "info");
                }
                btn.disabled = false;
            }
        } else {
            showToast("Build Failed", data.detail || "Unknown error", "error");
            btn.disabled = false;
            btn.innerHTML = `<span>Build & Generate Package</span>`;
        }
    } catch (e) {
        showToast("Build Error", e.message, "error");
        btn.disabled = false;
        btn.innerHTML = `<span>Build & Generate Package</span>`;
    }
}

function copyOneLiner() {
    const input = document.getElementById("oneLinerCode");
    input.select();
    navigator.clipboard.writeText(input.value);
    
    const copyTextEl = document.getElementById("copyBtnText");
    copyTextEl.innerText = "Copied!";
    setTimeout(() => copyTextEl.innerText = "Copy", 2000);
    showToast("Command Copied", "Paste into PowerShell (Admin) on the target machine.", "success");
}

function copyText(text, successMsg) {
    navigator.clipboard.writeText(text);
    showToast("Copied", successMsg || "Copied to clipboard", "info");
}

// -------------------------------------------------------------
// All Previously Generated Agents Modal & Registry Management
// -------------------------------------------------------------
let allGeneratedPackages = [];

async function loadGeneratedPackagesCache() {
    try {
        const res = await fetch("/api/agents/history");
        if (res.ok) {
            const data = await res.json();
            if (data.success && Array.isArray(data.packages)) {
                allGeneratedPackages = data.packages;
                renderAgentTable();
            }
        }
    } catch (e) {
        console.warn("Could not preload agent package history:", e);
    }
}

function openAllAgentsModal() {
    const modal = document.getElementById("allAgentsModal");
    if (modal) {
        modal.style.display = "flex";
        modal.style.opacity = "1";
        modal.style.zIndex = "9999";
        modal.classList.add("active");
    }
    const searchInput = document.getElementById("allAgentsSearchInput");
    if (searchInput) searchInput.value = "";
    loadAllGeneratedAgents(false);
}

function closeAllAgentsModal() {
    const modal = document.getElementById("allAgentsModal");
    if (modal) {
        modal.classList.remove("active");
        modal.style.display = "none";
        modal.style.opacity = "0";
    }
}

function formatBytes(bytes) {
    if (!bytes || bytes <= 0) return "0 B";
    const k = 1024;
    const sizes = ["B", "KB", "MB", "GB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + " " + sizes[i];
}

function escapeHtml(str) {
    if (!str) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

async function loadAllGeneratedAgents(forceToast = false) {
    const tableBody = document.getElementById("allAgentsTableBody");
    const countBadge = document.getElementById("allAgentsCountBadge");
    if (tableBody) {
        tableBody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: var(--text-muted); padding: 30px;">
            <svg class="spin" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" style="margin-right: 6px; vertical-align: middle;"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>
            Loading previously generated agents...
        </td></tr>`;
    }

    try {
        const res = await fetch("/api/agents/history");
        if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`);
        const data = await res.json();
        if (data.success && Array.isArray(data.packages)) {
            allGeneratedPackages = data.packages;
            if (countBadge) {
                countBadge.innerText = `${allGeneratedPackages.length} Package${allGeneratedPackages.length === 1 ? '' : 's'}`;
            }
            renderAllAgentsTable(allGeneratedPackages);
            if (forceToast) {
                showToast("Packages Refreshed", `Found ${allGeneratedPackages.length} generated agent package(s).`, "info");
            }
        } else {
            throw new Error(data.detail || "Failed to parse package history");
        }
    } catch (e) {
        if (tableBody) {
            tableBody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: #ef4444; padding: 24px;">Failed to load agents: ${escapeHtml(e.message)}</td></tr>`;
        }
        if (forceToast) {
            showToast("Failed to Load Packages", e.message, "error");
        }
    }
}

async function downloadVbsFile(url, filename) {
    if (!url || url === "#") return;
    try {
        showToast("Downloading VBScript", `Downloading ${filename}...`, "info");
        const res = await fetch(url);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const text = await res.text();
        const blob = new Blob([text], { type: "text/vbscript;charset=utf-8" });
        const blobUrl = window.URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = blobUrl;
        a.download = filename || "install.vbs";
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(() => window.URL.revokeObjectURL(blobUrl), 3000);
        showToast("Download Complete", `${filename} is ready in your Downloads folder.`, "success");
    } catch (e) {
        window.location.href = url;
    }
}

function renderAllAgentsTable(packages) {
    const tableBody = document.getElementById("allAgentsTableBody");
    if (!tableBody) return;

    if (!packages || packages.length === 0) {
        tableBody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: var(--text-muted); padding: 36px;">
            No agent packages found. Click <strong>"+ Build New Agent Package"</strong> below to create your first package.
        </td></tr>`;
        return;
    }

    tableBody.innerHTML = packages.map(pkg => {
        const dateStr = pkg.created_at ? new Date(pkg.created_at).toLocaleString() : "Unknown";
        const sizeStr = pkg.size_bytes ? formatBytes(pkg.size_bytes) : "-";
        const tag = escapeHtml(pkg.endpoint_tag || pkg.app_name || "Agent");
        const appName = escapeHtml(pkg.app_name || "TRMMAgent");
        const agentId = escapeHtml(pkg.agent_id || "");
        const serverUrl = escapeHtml(pkg.server_url || "");

        const isAndroid = (pkg.arch && pkg.arch.toLowerCase().includes('android')) ||
                          !!(pkg.apk_filename || pkg.apk_url || pkg.sh_filename || (pkg.zip_filename && pkg.zip_filename.includes('_android.zip')));

        // Build Download Links
        const links = [];
        if (isAndroid) {
            const apkUrl = pkg.apk_download_url || pkg.apk_url;
            if (apkUrl) {
                links.push(`<a href="${apkUrl}" download="${escapeHtml(pkg.apk_filename || 'agent.apk')}" class="btn btn-xs btn-primary" style="background: #059669; border-color: #10b981;" title="Download Android App (.apk)">APK</a>`);
            }
            const shUrl = pkg.sh_download_url || pkg.sh_url || pkg.bootstrap_download_url;
            if (shUrl) {
                links.push(`<a href="${shUrl}" download="${escapeHtml(pkg.sh_filename || 'install.sh')}" class="btn btn-xs btn-secondary" title="Download Shell Installer (.sh)">SH</a>`);
            }
            const zipUrl = pkg.zip_download_url || pkg.zip_url;
            if (zipUrl) {
                links.push(`<a href="${zipUrl}" download="${escapeHtml(pkg.zip_filename || 'agent_android.zip')}" class="btn btn-xs btn-outline" title="Download Full Offline Mobile Bundle (.zip)">ZIP</a>`);
            }
            if (pkg.one_liner) {
                links.push(`<button type="button" class="btn btn-xs btn-outline" onclick="copyPkgOneLiner('${agentId}')" title="Copy 1-Click Android Termux / Shell Command">CURL</button>`);
            }
        } else {
            const exeUrl = pkg.exe_download_url || pkg.exe_url;
            if (exeUrl) {
                links.push(`<a href="${exeUrl}" download="${escapeHtml(pkg.exe_filename || 'agent_setup.exe')}" class="btn btn-xs btn-primary" title="Download Standalone EXE Setup (.exe)">EXE</a>`);
            }
            const batUrl = pkg.bat_download_url || pkg.bat_url || pkg.bootstrap_download_url;
            if (batUrl) {
                links.push(`<a href="${batUrl}" download="${escapeHtml(pkg.bat_filename || 'install.bat')}" class="btn btn-xs btn-secondary" title="Download Batch Installer (.bat)">BAT</a>`);
            }
            const vbsUrl = pkg.vbs_download_url || pkg.vbs_url;
            if (vbsUrl) {
                const vbsName = escapeHtml(pkg.vbs_filename || 'install.vbs');
                links.push(`<a href="${vbsUrl}" download="${vbsName}" class="btn btn-xs btn-secondary" style="background: #1e293b; border-color: #3b82f6; color: #60a5fa;" title="Download Silent VBScript Installer (.vbs)">VBS</a>`);
            }
            const zipUrl = pkg.zip_download_url || pkg.zip_url;
            if (zipUrl) {
                links.push(`<a href="${zipUrl}" download="${escapeHtml(pkg.zip_filename || 'agent.zip')}" class="btn btn-xs btn-outline" title="Download Full Offline Package (.zip)">ZIP</a>`);
            }
            if (pkg.one_liner) {
                links.push(`<button type="button" class="btn btn-xs btn-outline" onclick="copyPkgOneLiner('${agentId}')" title="Copy 1-Click PowerShell Deployment Command">PS1</button>`);
            }
        }

        const tagBadge = isAndroid
            ? `<span class="status-tag-ready" style="font-size: 10px; padding: 1px 6px; background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3);">🤖 ${tag}</span>`
            : `<span class="status-tag-ready" style="font-size: 10px; padding: 1px 6px;">${tag}</span>`;

        return `
            <tr>
                <td>
                    <div style="font-weight: 600; color: #f3f4f6; display: flex; align-items: center; gap: 6px;">
                        ${tagBadge}
                        <span>${appName}</span>
                    </div>
                    <div style="font-size: 11px; color: var(--text-muted); margin-top: 2px;">
                        <span>Target: ${serverUrl}</span>
                    </div>
                </td>
                <td>
                    <code style="font-size: 11px; color: #38bdf8; background: rgba(56, 189, 248, 0.08); padding: 2px 6px; border-radius: 3px;">${agentId ? agentId.substring(0, 16) + '...' : '-'}</code>
                </td>
                <td style="color: var(--text-secondary); font-size: 11px; white-space: nowrap;">
                    ${dateStr}
                </td>
                <td style="color: var(--text-muted); font-size: 11px; white-space: nowrap;">
                    ${sizeStr}
                </td>
                <td>
                    <div style="display: flex; gap: 4px; align-items: center; flex-wrap: wrap;">
                        ${links.join("")}
                    </div>
                </td>
                <td style="text-align: right;">
                    <button type="button" class="btn btn-xs btn-outline" style="color: #ef4444; border-color: rgba(239, 68, 68, 0.35);" onclick="deleteAgentPackage('${agentId}', '${tag}')" title="Delete Package">
                        <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                    </button>
                </td>
            </tr>
        `;
    }).join("");
}

function filterAllAgentsTable() {
    const search = (document.getElementById("allAgentsSearchInput")?.value || "").trim().toLowerCase();
    if (!search) {
        renderAllAgentsTable(allGeneratedPackages);
        return;
    }

    const filtered = allGeneratedPackages.filter(pkg => {
        const id = (pkg.agent_id || "").toLowerCase();
        const tag = (pkg.endpoint_tag || "").toLowerCase();
        const app = (pkg.app_name || "").toLowerCase();
        const srv = (pkg.server_url || "").toLowerCase();
        return id.includes(search) || tag.includes(search) || app.includes(search) || srv.includes(search);
    });

    renderAllAgentsTable(filtered);
}

function copyPkgOneLiner(agentId) {
    const pkg = allGeneratedPackages.find(p => p.agent_id === agentId);
    if (!pkg || !pkg.one_liner) {
        showToast("Copy Failed", "PowerShell one-liner command not available for this package.", "warning");
        return;
    }
    navigator.clipboard.writeText(pkg.one_liner);
    showToast("Command Copied", `PowerShell 1-Click command copied for ${pkg.endpoint_tag || pkg.app_name}!`, "success");
}

async function deleteAgentPackage(agentId, label) {
    if (!confirm(`Are you sure you want to delete the package for "${label}" (${agentId})? This will delete the generated zip, exe, bat, and vbs files from the server.`)) {
        return;
    }

    try {
        const res = await fetch(`/api/agents/history/${encodeURIComponent(agentId)}`, {
            method: "DELETE"
        });
        const data = await res.json();
        if (res.ok && data.success) {
            showToast("Package Deleted", `Successfully removed package for ${label}.`, "success");
            loadAllGeneratedAgents(false);
        } else {
            throw new Error(data.detail || "Failed to delete package");
        }
    } catch (e) {
        showToast("Delete Failed", e.message, "error");
    }
}

// -------------------------------------------------------------
// HVNC Remote Viewer (Dismissable: Esc, Disconnect Button)
// -------------------------------------------------------------
function setDisplayMode(mode) {
    hvncDisplayMode = mode;
    ['modeFitBtn', 'modeFillBtn', 'modeNativeBtn'].forEach(id => {
        const btn = document.getElementById(id);
        if (btn) btn.classList.remove('active');
    });
    if (mode === 'fit') document.getElementById('modeFitBtn')?.classList.add('active');
    else if (mode === 'fill') document.getElementById('modeFillBtn')?.classList.add('active');
    else if (mode === 'native') document.getElementById('modeNativeBtn')?.classList.add('active');
    resizeHvncDisplay();
}

function resizeHvncDisplay() {
    if (!hvncCanvas) return;
    const container = document.getElementById("hvncCanvasContainer");
    const viewport = document.getElementById("canvasViewport");
    if (!container || !viewport) return;

    const isFs = !!document.fullscreenElement;
    const availWidth = container.clientWidth - (isFs ? 0 : 16);
    const availHeight = container.clientHeight - (isFs ? 0 : 16);

    const nativeW = hvncCanvas.width || 1280;
    const nativeH = hvncCanvas.height || 720;

    if (hvncDisplayMode === 'fit') {
        const aspect = nativeW / nativeH;
        let w = availWidth;
        let h = Math.floor(availWidth / aspect);
        if (h > availHeight) {
            h = availHeight;
            w = Math.floor(availHeight * aspect);
        }
        w = Math.max(320, w);
        h = Math.max(180, h);
        hvncCanvas.style.width = `${w}px`;
        hvncCanvas.style.height = `${h}px`;
        viewport.style.width = `${w}px`;
        viewport.style.height = `${h}px`;
    } else if (hvncDisplayMode === 'fill') {
        hvncCanvas.style.width = `${availWidth}px`;
        hvncCanvas.style.height = `${availHeight}px`;
        viewport.style.width = `${availWidth}px`;
        viewport.style.height = `${availHeight}px`;
    } else if (hvncDisplayMode === 'native') {
        hvncCanvas.style.width = `${nativeW}px`;
        hvncCanvas.style.height = `${nativeH}px`;
        viewport.style.width = `${nativeW}px`;
        viewport.style.height = `${nativeH}px`;
    }
}

function openViewerTab(agentId, mode = 'backstage') {
    window.open(`/viewer/${agentId}?mode=${mode}`, '_blank');
}

function openHvncTab(agentId) {
    openViewerTab(agentId, 'backstage');
}

function openHvncSession(agentId, tagName, os, ip) {
    activeAgentId = agentId;
    document.getElementById("hvncTargetTitle").innerText = `Target: ${tagName || agentId}`;
    document.getElementById("hvncTargetMeta").innerText = `${os || 'Windows'} • ${ip || '127.0.0.1'}`;
    
    const modal = document.getElementById("hvncModal");
    modal.classList.add("active");

    // Recalculate canvas fit on open
    setTimeout(resizeHvncDisplay, 50);

    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${window.location.host}/ws/viewer/${agentId}`;

    viewerWs = new WebSocket(url);
    viewerWs.binaryType = "blob";

    viewerWs.onopen = () => {
        showToast("HVNC Stream Connected", `Live session established with ${tagName}`, "success");
    };

    // Zero-lag real-time frame receiver with single-slot frame dropping
    viewerWs.onmessage = (event) => {
        if (event.data instanceof Blob) {
            pendingFrameBlob = event.data;
            if (!isRenderingFrame) {
                processNextFrame();
            }
        } else if (typeof event.data === "string") {
            try {
                const data = JSON.parse(event.data);
                console.log("[HVNC DIAG EVENT]", data);
                if (data.type === "diag_launch") {
                    if (data.success) {
                        showToast("App Launched", `Spawned ${data.app.toUpperCase()} (PID ${data.pid})`, "success");
                    } else {
                        showToast("Launch Error", `${data.app.toUpperCase()}: ${data.error}`, "error");
                    }
                }
            } catch (err) {}
        }
    };

    viewerWs.onclose = () => {
        console.log("Viewer WS closed");
    };
}

async function processNextFrame() {
    if (!pendingFrameBlob) return;
    isRenderingFrame = true;
    const blobToRender = pendingFrameBlob;
    pendingFrameBlob = null; // Cleared so any newer incoming frame takes latest slot

    try {
        if (window.createImageBitmap) {
            const bitmap = await createImageBitmap(blobToRender);
            if (hvncCanvas.width !== bitmap.width || hvncCanvas.height !== bitmap.height) {
                hvncCanvas.width = bitmap.width;
                hvncCanvas.height = bitmap.height;
                const resEl = document.getElementById("viewerRes");
                if (resEl) resEl.innerText = `${bitmap.width}x${bitmap.height}`;
                resizeHvncDisplay();
            }
            hvncCtx.drawImage(bitmap, 0, 0);
            bitmap.close();
        } else {
            // Fallback decoder
            await new Promise((resolve) => {
                const blobUrl = URL.createObjectURL(blobToRender);
                const img = new Image();
                img.onload = () => {
                    if (hvncCanvas.width !== img.width || hvncCanvas.height !== img.height) {
                        hvncCanvas.width = img.width;
                        hvncCanvas.height = img.height;
                        const resEl = document.getElementById("viewerRes");
                        if (resEl) resEl.innerText = `${img.width}x${img.height}`;
                        resizeHvncDisplay();
                    }
                    hvncCtx.drawImage(img, 0, 0);
                    URL.revokeObjectURL(blobUrl);
                    resolve();
                };
                img.onerror = () => {
                    URL.revokeObjectURL(blobUrl);
                    resolve();
                };
                img.src = blobUrl;
            });
        }
        updateViewerFps();
    } catch (err) {
        console.error("Frame render error:", err);
    } finally {
        isRenderingFrame = false;
        // If a fresher frame arrived while drawing, render it immediately
        if (pendingFrameBlob) {
            requestAnimationFrame(processNextFrame);
        }
    }
}

function updateViewerFps() {
    hvncFrameCount++;
    const now = performance.now();
    if (now - lastFpsTime >= 1000) {
        const fps = Math.round((hvncFrameCount * 1000) / (now - lastFpsTime));
        const fpsEl = document.getElementById("viewerFps");
        if (fpsEl) fpsEl.innerText = `FPS: ${fps}`;
        hvncFrameCount = 0;
        lastFpsTime = now;
    }
}

function closeHvncSession() {
    if (viewerWs) {
        viewerWs.close();
        viewerWs = null;
    }
    activeAgentId = null;
    const modal = document.getElementById("hvncModal");
    modal.classList.remove("active");
    if (document.fullscreenElement) {
        exitFullscreen();
    }
}

function sendViewerLaunch(appName) {
    if (viewerWs && viewerWs.readyState === WebSocket.OPEN) {
        viewerWs.send(JSON.stringify({
            type: "launch",
            app: appName
        }));
        showToast("Application Spawned", `Launched ${appName} in target's hidden desktop.`, "info");
    } else {
        showToast("Not Connected", "HVNC session is not active.", "warning");
    }
}

function sendViewerInput(payload) {
    if (viewerWs && viewerWs.readyState === WebSocket.OPEN) {
        viewerWs.send(JSON.stringify({
            type: "input",
            data: payload
        }));
    }
}

function toggleCanvasFullscreen() {
    const modal = document.getElementById("hvncModal");
    if (!document.fullscreenElement) {
        if (modal.requestFullscreen) {
            modal.requestFullscreen().catch(err => console.error(err));
        } else if (modal.webkitRequestFullscreen) {
            modal.webkitRequestFullscreen();
        }
    } else {
        if (document.exitFullscreen) {
            document.exitFullscreen();
        } else if (document.webkitExitFullscreen) {
            document.webkitExitFullscreen();
        }
    }
}

function exitFullscreen() {
    if (document.fullscreenElement) {
        if (document.exitFullscreen) {
            document.exitFullscreen();
        } else if (document.webkitExitFullscreen) {
            document.webkitExitFullscreen();
        }
    }
}

function getHvncCoordinates(event) {
    const rect = hvncCanvas.getBoundingClientRect();
    const scaleX = hvncCanvas.width / rect.width;
    const scaleY = hvncCanvas.height / rect.height;
    return {
        x: Math.round((event.clientX - rect.left) * scaleX),
        y: Math.round((event.clientY - rect.top) * scaleY)
    };
}

function initHvncCanvasListeners() {
    const prompt = document.getElementById("clickToFocus");

    hvncCanvas.addEventListener("focus", () => {
        hasHvncFocus = true;
        if (prompt) prompt.style.opacity = "0";
    });

    hvncCanvas.addEventListener("blur", () => {
        hasHvncFocus = false;
        if (prompt) prompt.style.opacity = "1";
    });

    // Throttled mousemove to prevent network congestion
    hvncCanvas.addEventListener("mousemove", (e) => {
        const coords = getHvncCoordinates(e);
        const now = performance.now();
        pendingMouseMove = coords;

        if (now - lastMouseMoveTime >= 25) { // max ~40 updates/sec
            lastMouseMoveTime = now;
            sendViewerInput({ type: "mousemove", ...coords });
            pendingMouseMove = null;
        }
    });

    hvncCanvas.addEventListener("mouseleave", () => {
        if (pendingMouseMove) {
            sendViewerInput({ type: "mousemove", ...pendingMouseMove });
            pendingMouseMove = null;
        }
    });

    hvncCanvas.addEventListener("mousedown", (e) => {
        const { x, y } = getHvncCoordinates(e);
        const btn = e.button === 2 ? "right" : (e.button === 1 ? "middle" : "left");
        sendViewerInput({ type: "mousedown", x, y, button: btn });
        hvncCanvas.focus();
    });

    hvncCanvas.addEventListener("mouseup", (e) => {
        const { x, y } = getHvncCoordinates(e);
        const btn = e.button === 2 ? "right" : (e.button === 1 ? "middle" : "left");
        sendViewerInput({ type: "mouseup", x, y, button: btn });
    });

    hvncCanvas.addEventListener("dblclick", (e) => {
        const { x, y } = getHvncCoordinates(e);
        sendViewerInput({ type: "dblclick", x, y });
    });

    hvncCanvas.addEventListener("contextmenu", (e) => {
        e.preventDefault();
    });

    hvncCanvas.addEventListener("wheel", (e) => {
        e.preventDefault();
        const { x, y } = getHvncCoordinates(e);
        // Leverage host machine's actual scroll configuration and trackpad physics
        let delta = 0;
        if (e.deltaMode === 0) {
            delta = Math.round(-e.deltaY * 1.5);
        } else if (e.deltaMode === 1) {
            delta = Math.round(-e.deltaY * 40);
        } else if (e.deltaMode === 2) {
            delta = Math.round(-e.deltaY * 120);
        } else {
            delta = e.deltaY < 0 ? 120 : -120;
        }

        if (delta !== 0) {
            sendViewerInput({ type: "wheel", x, y, delta });
        }
    }, { passive: false });

    window.addEventListener("keydown", (e) => {
        if (!hasHvncFocus) return;
        if (["Tab", "Alt", "Control", "Backspace", "Delete", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(e.key)) {
            e.preventDefault();
        }

        const isShortcut = e.ctrlKey || e.altKey || e.metaKey;
        const isPrintable = e.key.length === 1 && !isShortcut;

        if (isPrintable) {
            sendViewerInput({ type: "char", char: e.key.charCodeAt(0) });
        } else {
            sendViewerInput({ type: "keydown", vk: e.keyCode });
        }
    });

    window.addEventListener("keyup", (e) => {
        if (!hasHvncFocus) return;
        const isShortcut = e.ctrlKey || e.altKey || e.metaKey;
        const isPrintable = e.key.length === 1 && !isShortcut;
        if (!isPrintable) {
            sendViewerInput({ type: "keyup", vk: e.keyCode });
        }
    });
}

/**
 * Global Keyboard Shortcuts (Esc to dismiss open modals)
 */
function initKeyboardShortcuts() {
    window.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
            const genModal = document.getElementById("generatorModal");
            const hvncModal = document.getElementById("hvncModal");
            const otaModal = document.getElementById("otaModal") || document.getElementById("updateModal");

            if (hvncModal && hvncModal.classList.contains("active")) {
                closeHvncSession();
            } else if (genModal && genModal.classList.contains("active")) {
                closeGeneratorModal();
            } else if (otaModal && otaModal.classList.contains("active")) {
                closeOtaModal();
            } else {
                const lockModal = document.getElementById("lockDeviceModal");
                if (lockModal && (lockModal.classList.contains("active") || lockModal.style.display === "flex")) {
                    closeLockDeviceModal();
                }
            }
        }
    });
}

const recentToastDedupe = new Map();

// -------------------------------------------------------------
// Toast Notification Engine
// -------------------------------------------------------------
function showToast(title, message, type = "info") {
    const container = document.getElementById("toastContainer");
    if (!container) return;

    // Suppress repetitive identical toast messages within 15 seconds
    const dedupeKey = `${title}:::${message}`;
    const now = Date.now();
    const lastShown = recentToastDedupe.get(dedupeKey) || 0;
    if (now - lastShown < 15000) {
        return;
    }
    recentToastDedupe.set(dedupeKey, now);

    // Limit visible toasts on screen to at most 2, dismissing older ones
    while (container.children.length >= 2) {
        container.removeChild(container.firstChild);
    }

    const toast = document.createElement("div");
    toast.className = `toast ${type}`;

    let icon = "⚡";
    if (type === "success") icon = "✓";
    else if (type === "error") icon = "✕";
    else if (type === "warning") icon = "⚠️";

    toast.innerHTML = `
        <div class="toast-content">
            <span class="toast-icon">${icon}</span>
            <div class="toast-text">
                <span class="toast-title">${escapeHtml(title)}</span>
                <span class="toast-message">${escapeHtml(message)}</span>
            </div>
        </div>
        <button class="toast-close" onclick="dismissToast(this.parentElement)">&times;</button>
    `;

    container.appendChild(toast);

    // Auto-remove after 4 seconds
    setTimeout(() => {
        dismissToast(toast);
    }, 4000);
}

function dismissToast(toastEl) {
    if (!toastEl || toastEl.classList.contains("removing")) return;
    toastEl.classList.add("removing");
    setTimeout(() => {
        if (toastEl.parentElement) {
            toastEl.parentElement.removeChild(toastEl);
        }
    }, 200);
}

function formatTimeAgo(timestamp) {
    const seconds = Math.floor((Date.now() / 1000) - timestamp);
    if (seconds < 5) return "Just now";
    if (seconds < 60) return `${seconds}s ago`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    return `${hours}h ago`;
}

function escapeHtml(text) {
    if (!text) return "";
    const div = document.createElement("div");
    div.innerText = String(text);
    return div.innerHTML;
}

async function logout() {
    try {
        await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
    } catch (e) {
        console.error("Logout request failed:", e);
    }
    // Delete session cookie on client side as well
    document.cookie = "trmm_session=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;";
    if (dashboardWs) {
        try { dashboardWs.close(); } catch (e) {}
    }
    if (viewerWs) {
        try { viewerWs.close(); } catch (e) {}
    }
    window.location.href = "/login";
}

/**
 * Installation Telemetry Dashboard UI Logic
 */
let installationPollTimer = null;
let installationRequestPending = false;

function installationText(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
}

async function installationRequest(url, options = {}) {
    const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store', ...options,
        headers: {'Content-Type': 'application/json', 'X-TRMM-Request': 'installations', ...(options.headers || {})}});
    const data = await response.json();
    if (!response.ok) {
        const detail = data.detail || data.error || `HTTP ${response.status}`;
        throw new Error(typeof detail === 'string' ? detail : (detail.code || 'Installation request failed'));
    }
    return data;
}

function initInstallationsTab() {
    fetchInstallationsData();
    if (!installationPollTimer) {
        installationPollTimer = setInterval(fetchInstallationsData, 3000);
    }
}

async function fetchInstallationsData() {
    if (installationRequestPending || document.hidden) return;
    installationRequestPending = true;
    try {
        const [availability, attData] = await Promise.all([
            installationRequest('/api/installations/status'),
            installationRequest('/api/installations/attempts')
        ]);
        document.getElementById('installationAvailability').textContent = availability.ready
            ? 'Installer available.' : 'Distribution unavailable: ' + (availability.blockers || []).join(' ');
        document.getElementById('createInstallationInvitationBtn').disabled = !availability.ready;
        document.getElementById('installationApiError').hidden = true;
        renderInstallationsDashboard([], attData.attempts || []);
    } catch (err) {
        const element = document.getElementById('installationApiError');
        element.textContent = 'Installation status unavailable: ' + err.message;
        element.hidden = false;
        document.getElementById('createInstallationInvitationBtn').disabled = true;
    } finally {
        installationRequestPending = false;
    }
}

function renderInstallationsDashboard(invitations, attempts) {
    const tableBody = document.getElementById("installationsTableBody");
    if (!tableBody) return;

    if (attempts.length === 0) {
        tableBody.innerHTML = `<tr><td colspan="6" class="text-center text-muted">No active or past installation attempts recorded.</td></tr>`;
        return;
    }

    tableBody.innerHTML = attempts.map(att => {
        let badgeClass = 'bg-secondary';
        if (att.current_stage === 'INSTALLED' || att.current_stage === 'ONLINE') badgeClass = 'bg-success';
        else if (att.current_stage === 'FAILED' || att.current_stage === 'ADMIN_REQUIRED') badgeClass = 'bg-danger';
        else if (att.current_stage === 'DOWNLOADING' || att.current_stage === 'INSTALLING') badgeClass = 'bg-primary';

        let progressPct = 0;
        if (att.total_bytes > 0) {
            progressPct = Math.max(0, Math.min(100, Math.round((Number(att.download_bytes) / Number(att.total_bytes)) * 100)));
            if (!Number.isFinite(progressPct)) progressPct = 0;
        }

        return `
            <tr>
                <td><code>${installationText(String(att.id).substring(0, 8))}...</code></td>
                <td>${installationText(att.architecture)} (${installationText(att.release_version)})</td>
                <td><span class="badge ${badgeClass}">${installationText(att.current_stage)}</span><br>${installationText(att.connection_status)}</td>
                <td>
                    <div class="progress" style="height: 15px;">
                        <div class="progress-bar" role="progressbar" style="width: ${progressPct}%;">${progressPct}%</div>
                    </div>
                </td>
                <td>${att.error_code ? `<span class="text-danger">${installationText(att.error_code)}: ${installationText(att.error_detail)}</span>` : '<span class="text-muted">None</span>'}<br>${installationText(new Date(att.updated_at).toLocaleString())}</td>
                <td><button class="btn btn-outline" data-installation-attempt="${installationText(att.id)}">Timeline</button></td>
            </tr>
        `;
    }).join('');
    tableBody.querySelectorAll?.('[data-installation-attempt]').forEach(button => {
        button.addEventListener('click', async () => {
            const output = document.getElementById('installationEventDetails');
            try {
                const data = await installationRequest('/api/installations/attempts/' + encodeURIComponent(button.dataset.installationAttempt) + '/events');
                output.textContent = data.events.length ? data.events.map(e => `${e.created_at} | ${e.stage}\n${JSON.stringify(e.payload)}`).join('\n') : 'No verified events recorded.';
            } catch (error) { output.textContent = error.message; }
        });
    });
}

async function createInstallationInvitationCode() {
    try {
        const result = await installationRequest('/api/installations/invitations', {method: 'POST', body: '{}'});
        document.getElementById('installationInvitationResult').textContent = result.code || 'Invitation created.';
        fetchInstallationsData();
    } catch (e) {
        document.getElementById('installationInvitationResult').textContent = 'Cannot create invitation: ' + e.message;
    }
}
