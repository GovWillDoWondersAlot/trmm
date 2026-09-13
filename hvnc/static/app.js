/**
 * HVNC Client Engine
 * Handles real-time video stream rendering, mouse tracking, and keyboard dispatching over WebSocket.
 */

let ws = null;
const canvas = document.getElementById("screenCanvas");
const ctx = canvas.getContext("2d");
const statusIndicator = document.getElementById("statusIndicator");
const statusText = document.getElementById("statusText");
const fpsCounter = document.getElementById("fpsCounter");
const focusPrompt = document.getElementById("focusPrompt");

let frameCount = 0;
let lastFpsUpdate = performance.now();
let hasKeyboardFocus = false;

// Initialize WebSocket connection
function connectWebSocket() {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${protocol}//${window.location.host}/ws/stream`;

    statusText.innerText = "Connecting...";
    statusIndicator.className = "status-indicator";

    ws = new WebSocket(wsUrl);
    ws.binaryType = "blob";

    ws.onopen = () => {
        statusText.innerText = "Live Session Active";
        statusIndicator.className = "status-indicator";
    };

    ws.onclose = () => {
        statusText.innerText = "Disconnected";
        statusIndicator.className = "status-indicator offline";
        setTimeout(connectWebSocket, 2000);
    };

    ws.onerror = (err) => {
        console.error("WebSocket error:", err);
    };

    ws.onmessage = (event) => {
        if (event.data instanceof Blob) {
            const blobUrl = URL.createObjectURL(event.data);
            const img = new Image();
            img.onload = () => {
                ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
                URL.revokeObjectURL(blobUrl);
                updateFps();
            };
            img.src = blobUrl;
        }
    };
}

function updateFps() {
    frameCount++;
    const now = performance.now();
    if (now - lastFpsUpdate >= 1000) {
        fpsCounter.innerText = `FPS: ${frameCount}`;
        frameCount = 0;
        lastFpsUpdate = now;
    }
}

// Convert canvas click coordinates to virtual desktop coordinates
function getCanvasCoordinates(event) {
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;

    const x = Math.round((event.clientX - rect.left) * scaleX);
    const y = Math.round((event.clientY - rect.top) * scaleY);
    return { x, y };
}

function sendWsMessage(payload) {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(payload));
    }
}

// Mouse Event Listeners
canvas.addEventListener("mousemove", (e) => {
    const { x, y } = getCanvasCoordinates(e);
    sendWsMessage({ type: "mousemove", x, y });
});

canvas.addEventListener("mousedown", (e) => {
    const { x, y } = getCanvasCoordinates(e);
    const btn = e.button === 2 ? "right" : (e.button === 1 ? "middle" : "left");
    sendWsMessage({ type: "mousedown", x, y, button: btn });
    canvas.focus();
});

canvas.addEventListener("mouseup", (e) => {
    const { x, y } = getCanvasCoordinates(e);
    const btn = e.button === 2 ? "right" : (e.button === 1 ? "middle" : "left");
    sendWsMessage({ type: "mouseup", x, y, button: btn });
});

canvas.addEventListener("dblclick", (e) => {
    const { x, y } = getCanvasCoordinates(e);
    sendWsMessage({ type: "dblclick", x, y });
});

canvas.addEventListener("contextmenu", (e) => {
    e.preventDefault();
});

canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    const { x, y } = getCanvasCoordinates(e);
    const delta = e.deltaY < 0 ? 120 : -120;
    sendWsMessage({ type: "wheel", x, y, delta });
}, { passive: false });

// Keyboard Event Listeners
canvas.addEventListener("focus", () => {
    hasKeyboardFocus = true;
    focusPrompt.style.opacity = "0";
});

canvas.addEventListener("blur", () => {
    hasKeyboardFocus = false;
    focusPrompt.style.opacity = "1";
});

window.addEventListener("keydown", (e) => {
    if (!hasKeyboardFocus) return;
    if (e.key === "Tab" || e.key === "Alt" || e.key === "Control") {
        e.preventDefault();
    }

    sendWsMessage({ type: "keydown", vk: e.keyCode });

    if (e.key.length === 1) {
        sendWsMessage({ type: "char", char: e.key.charCodeAt(0) });
    }
});

window.addEventListener("keyup", (e) => {
    if (!hasKeyboardFocus) return;
    sendWsMessage({ type: "keyup", vk: e.keyCode });
});

// Quick Launch Functions
async function launchApp(appName, url = null) {
    try {
        const res = await fetch("/api/launch", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ app_name: appName, url: url })
        });
        const data = await res.json();
        if (data.success) {
            console.log(`Launched ${appName} (PID: ${data.pid})`);
        } else {
            alert(`Could not launch ${appName}: ${data.error}`);
        }
    } catch (err) {
        console.error("Launch error:", err);
    }
}

// Custom Modal Controls
function openCustomModal() {
    document.getElementById("customModal").classList.add("active");
}

function closeCustomModal() {
    document.getElementById("customModal").classList.remove("active");
}

async function submitCustomLaunch() {
    const path = document.getElementById("customPathInput").value.trim();
    const args = document.getElementById("customArgsInput").value.trim();
    if (!path) return;

    try {
        const res = await fetch("/api/launch", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ app_name: "custom", custom_path: path, args: args })
        });
        const data = await res.json();
        if (data.success) {
            closeCustomModal();
        } else {
            alert("Launch failed: " + data.error);
        }
    } catch (err) {
        alert("Request error: " + err.message);
    }
}

// Start WebSocket connection on page load
window.addEventListener("DOMContentLoaded", () => {
    connectWebSocket();
});
