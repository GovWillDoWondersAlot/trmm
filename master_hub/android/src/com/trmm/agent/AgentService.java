package com.trmm.agent;

import android.app.AlarmManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.app.admin.DevicePolicyManager;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.media.Image;
import android.media.ImageReader;
import android.media.projection.MediaProjection;
import android.media.projection.MediaProjectionManager;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.os.BatteryManager;
import android.os.Build;
import android.os.Environment;
import android.os.IBinder;
import android.os.PowerManager;
import android.os.StatFs;
import android.os.Vibrator;
import android.provider.Settings;
import android.util.DisplayMetrics;
import android.util.Log;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.net.URI;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.text.SimpleDateFormat;
import java.util.Collections;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

public class AgentService extends Service implements WebSocketClient.Listener {
    public static final String ACTION_STATUS_UPDATE = "com.trmm.agent.STATUS_UPDATE";
    public static final String EXTRA_STATUS = "status";
    public static final String EXTRA_LOG = "log";

    private static final String CHANNEL_ID = "trmm_agent_service";
    private static final int NOTIFICATION_ID = 1001;

    public static int mediaProjectionResultCode = 0;
    public static Intent mediaProjectionResultData = null;
    private static AgentService instance = null;
    private volatile byte[] lastValidJpegBytes = null;

    public static void setMediaProjectionData(int resultCode, Intent data) {
        mediaProjectionResultCode = resultCode;
        mediaProjectionResultData = data;
        if (instance != null) {
            instance.onMediaProjectionGranted();
        }
    }

    static class GlobalExceptionHandler implements Thread.UncaughtExceptionHandler {
        @Override
        public void uncaughtException(Thread thread, Throwable throwable) {
            Log.e("TRMMAgent", "Uncaught exception in " + (thread != null ? thread.getName() : "unknown") + ": " + (throwable != null ? throwable.getMessage() : "unknown"), throwable);
        }
    }

    static class ConnectRunnable implements Runnable {
        private final AgentService service;

        public ConnectRunnable(AgentService service) {
            this.service = service;
        }

        @Override
        public void run() {
            try {
                service.ensureConnected();
            } catch (Throwable t) {
                Log.e("TRMMAgent", "Error in ConnectRunnable", t);
            }
        }
    }

    static class TelemetryRunnable implements Runnable {
        private final AgentService service;

        public TelemetryRunnable(AgentService service) {
            this.service = service;
        }

        @Override
        public void run() {
            try {
                service.sendTelemetry();
            } catch (Throwable t) {
                Log.e("TRMMAgent", "Error in TelemetryRunnable", t);
            }
        }
    }

    static class ShellRunnable implements Runnable {
        private final AgentService service;
        private final String cmd;

        public ShellRunnable(AgentService service, String cmd) {
            this.service = service;
            this.cmd = cmd;
        }

        @Override
        public void run() {
            try {
                service.runShellSync(cmd);
            } catch (Throwable t) {
                Log.e("TRMMAgent", "Error in ShellRunnable", t);
            }
        }
    }

    static class InputShellRunnable implements Runnable {
        private final String cmd;

        public InputShellRunnable(String cmd) {
            this.cmd = cmd;
        }

        @Override
        public void run() {
            try {
                Process p = Runtime.getRuntime().exec(new String[]{"/system/bin/sh", "-c", cmd});
                p.waitFor();
            } catch (Throwable ignored) {}
        }
    }

    static class StreamRunnable implements Runnable {
        private final AgentService service;

        public StreamRunnable(AgentService service) {
            this.service = service;
        }

        @Override
        public void run() {
            try {
                service.streamScreenFramesLoop();
            } catch (Throwable t) {
                Log.e("TRMMAgent", "Error in StreamRunnable", t);
            }
        }
    }

    private PowerManager.WakeLock wakeLock;
    private WebSocketClient wsClient;
    private ScheduledExecutorService scheduler;
    private volatile boolean running = false;
    private volatile boolean isStreaming = false;

    private MediaProjection mediaProjection;
    private VirtualDisplay virtualDisplay;
    private ImageReader imageReader;
    private Thread screenStreamThread;

    private String serverUrl = "ws://10.0.2.2:8000";
    private String agentId = "android_agent";
    private String packageId = "android_agent";
    private String deviceHardwareId = "";
    private String endpointTag = "Android-Device";
    private boolean autoStart = true;
    private ConnectivityManager.NetworkCallback networkCallback;

    @Override
    public void onCreate() {
        super.onCreate();
        instance = this;

        // Catch any unhandled thread errors so app never displays "[App] keeps stopping"
        Thread.setDefaultUncaughtExceptionHandler(new GlobalExceptionHandler());

        loadConfig();
        createNotificationChannel();

        // Apply Android Enterprise Device Owner policies and protection
        AdminReceiver.applyDeviceOwnerPolicies(this);

        // Network monitoring for automatic sub-second reconnection upon internet restore
        registerNetworkCallback();

        // Start Foreground Service safely with dataSync
        safeStartForeground("Tactical RMM Service Active");

        // Persistent WakeLock so OS Doze doesn't sleep the socket
        try {
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            if (pm != null) {
                wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "TRMMAgent::ServiceWakeLock");
                wakeLock.setReferenceCounted(false);
                if (!wakeLock.isHeld()) {
                    wakeLock.acquire();
                }
            }
        } catch (Throwable t) {
            Log.w("TRMMAgent", "WakeLock acquire warning: " + t.getMessage());
        }

        // Resume lock screen experience if locked prior to service restart or device reboot
        try {
            SharedPreferences prefs = getSharedPreferences(CurtainActivity.PREFS_NAME, Context.MODE_PRIVATE);
            if (prefs.getBoolean(CurtainActivity.PREF_IS_LOCKED, false)) {
                Intent lockIntent = new Intent(this, CurtainActivity.class);
                lockIntent.putExtra(CurtainActivity.EXTRA_TITLE, prefs.getString(CurtainActivity.PREF_TITLE, ""));
                lockIntent.putExtra(CurtainActivity.EXTRA_MESSAGE, prefs.getString(CurtainActivity.PREF_MESSAGE, ""));
                lockIntent.putExtra(CurtainActivity.EXTRA_SUBTITLE, prefs.getString(CurtainActivity.PREF_SUBTITLE, ""));
                lockIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
                startActivity(lockIntent);
            }
        } catch (Throwable ignored) {}
    }

    private void safeStartForeground(String statusText) {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                startForeground(
                        NOTIFICATION_ID,
                        buildNotification(statusText),
                        ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
                );
            } else {
                startForeground(NOTIFICATION_ID, buildNotification(statusText));
            }
        } catch (Throwable t) {
            try {
                startForeground(NOTIFICATION_ID, buildNotification(statusText));
            } catch (Throwable ignored) {}
        }
    }

    private void loadConfig() {
        try {
            InputStream is = getAssets().open("config.json");
            byte[] buf = new byte[is.available()];
            is.read(buf);
            is.close();
            JSONObject cfg = new JSONObject(new String(buf, StandardCharsets.UTF_8));
            serverUrl = cfg.optString("server_url", serverUrl);
            String cfgAgentId = cfg.optString("agent_id", "android_agent");
            packageId = cfgAgentId;
            endpointTag = cfg.optString("endpoint_tag", endpointTag);
            autoStart = cfg.optBoolean("auto_start", true);

            // Unique persistent hardware identity per device
            SharedPreferences idPrefs = getSharedPreferences("trmm_agent_identity", Context.MODE_PRIVATE);
            String persistentAgentId = idPrefs.getString("device_agent_id", null);
            String androidId = "";
            try {
                androidId = Settings.Secure.getString(getContentResolver(), Settings.Secure.ANDROID_ID);
            } catch (Throwable ignored) {}
            if (androidId == null || androidId.trim().isEmpty() || "9774d56d682e549c".equals(androidId)) {
                androidId = UUID.randomUUID().toString().replace("-", "");
            }
            deviceHardwareId = androidId;

            if (persistentAgentId == null || persistentAgentId.trim().isEmpty()) {
                try {
                    MessageDigest md = MessageDigest.getInstance("SHA-256");
                    byte[] hash = md.digest((androidId + "_" + Build.MANUFACTURER + "_" + Build.MODEL).getBytes(StandardCharsets.UTF_8));
                    StringBuilder sb = new StringBuilder();
                    for (int i = 0; i < 4; i++) {
                        sb.append(String.format("%02x", hash[i]));
                    }
                    persistentAgentId = sb.toString();
                } catch (Throwable t) {
                    persistentAgentId = UUID.randomUUID().toString().substring(0, 8);
                }
                idPrefs.edit()
                    .putString("device_agent_id", persistentAgentId)
                    .putString("android_id", androidId)
                    .putString("package_id", cfgAgentId)
                    .apply();
            }
            agentId = persistentAgentId;
        } catch (Throwable t) {
            Log.w("TRMMAgent", "Could not load config.json: " + t.getMessage());
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (!running) {
            running = true;
            startAgentLoop();
        }
        return START_STICKY;
    }

    private synchronized void startAgentLoop() {
        if (scheduler != null && !scheduler.isShutdown()) {
            scheduler.shutdownNow();
        }
        scheduler = Executors.newScheduledThreadPool(2);

        // Connection manager runs every 5 seconds
        scheduler.scheduleWithFixedDelay(new ConnectRunnable(this), 0, 5, TimeUnit.SECONDS);

        // Telemetry / heartbeat ping runs every 10 seconds
        scheduler.scheduleWithFixedDelay(new TelemetryRunnable(this), 3, 10, TimeUnit.SECONDS);
    }

    synchronized void ensureConnected() {
        if (wsClient != null && wsClient.isConnected()) {
            return;
        }

        try {
            log("Connecting to " + serverUrl + "...");
            String wsUrl = serverUrl;
            if (wsUrl.endsWith("/")) wsUrl = wsUrl.substring(0, wsUrl.length() - 1);
            URI uri = new URI(wsUrl + "/ws/agent/" + agentId);

            wsClient = new WebSocketClient(uri, this);
            wsClient.connect();
        } catch (Throwable e) {
            log("Connection attempt failed: " + e.getMessage());
            updateNotification("Connecting to Tactical RMM...");
        }
    }

    private void sendRegistration() {
        try {
            boolean isOwner = false;
            boolean isAdmin = false;
            boolean isUninstallBlocked = false;
            try {
                DevicePolicyManager dpm = (DevicePolicyManager) getSystemService(Context.DEVICE_POLICY_SERVICE);
                ComponentName adminComp = AdminReceiver.getComponentName(this);
                if (dpm != null) {
                    isOwner = dpm.isDeviceOwnerApp(getPackageName());
                    isAdmin = dpm.isAdminActive(adminComp);
                    if (isOwner) {
                        try {
                            isUninstallBlocked = dpm.isUninstallBlocked(adminComp, getPackageName());
                        } catch (Throwable ignored) {}
                    }
                }
            } catch (Throwable ignored) {}
            String mgtMode = isOwner ? "Device Owner (Corporate)" : (isAdmin ? "Device Admin" : "Unmanaged");

            JSONObject reg = new JSONObject();
            reg.put("type", "handshake");
            reg.put("agent_id", agentId);
            reg.put("device_id", deviceHardwareId);
            reg.put("android_id", deviceHardwareId);
            reg.put("package_id", packageId);
            reg.put("installation_id", packageId);
            reg.put("hostname", Build.MANUFACTURER + " " + Build.MODEL);
            reg.put("device_name", Build.MANUFACTURER + " " + Build.MODEL);
            reg.put("username", "Android " + Build.VERSION.RELEASE);
            reg.put("os", "Android device");
            reg.put("arch", "android");
            reg.put("platform", "android");
            reg.put("is_android", true);
            reg.put("device_type", "phone");
            reg.put("endpoint_tag", endpointTag);
            reg.put("is_device_owner", isOwner);
            reg.put("is_device_admin", isAdmin);
            reg.put("uninstall_blocked", isUninstallBlocked);
            reg.put("management_mode", mgtMode);

            JSONObject data = new JSONObject();
            data.put("agent_id", agentId);
            data.put("device_id", deviceHardwareId);
            data.put("android_id", deviceHardwareId);
            data.put("package_id", packageId);
            data.put("installation_id", packageId);
            data.put("hostname", Build.MANUFACTURER + " " + Build.MODEL);
            data.put("device_name", Build.MANUFACTURER + " " + Build.MODEL);
            data.put("username", "Android " + Build.VERSION.RELEASE);
            data.put("os", "Android device");
            data.put("ip", getLocalIpAddress());
            data.put("endpoint_tag", endpointTag);
            data.put("arch", "android");
            data.put("platform", "android");
            data.put("is_android", true);
            data.put("device_type", "phone");
            data.put("status", "online");
            data.put("is_device_owner", isOwner);
            data.put("is_device_admin", isAdmin);
            data.put("uninstall_blocked", isUninstallBlocked);
            data.put("management_mode", mgtMode);

            // Battery (using BatteryManager properties - 100% receiver-free)
            try {
                BatteryManager bm = (BatteryManager) getSystemService(Context.BATTERY_SERVICE);
                if (bm != null) {
                    int level = bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY);
                    if (level >= 0) {
                        data.put("battery", level);
                    }
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                        int status = bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_STATUS);
                        boolean isCharging = (status == BatteryManager.BATTERY_STATUS_CHARGING ||
                                status == BatteryManager.BATTERY_STATUS_FULL);
                        data.put("battery_charging", isCharging);
                    }
                }
            } catch (Throwable ignored) {}

            // Memory & Disk
            try {
                Runtime rt = Runtime.getRuntime();
                long totalMem = rt.totalMemory();
                data.put("ram_gb", String.format("%.1f GB", totalMem / (1024.0 * 1024.0 * 1024.0)));

                StatFs stat = new StatFs(Environment.getDataDirectory().getPath());
                long bytesAvailable = stat.getBlockSizeLong() * stat.getAvailableBlocksLong();
                data.put("disk_free_gb", String.format("%.1f GB", bytesAvailable / (1024.0 * 1024.0 * 1024.0)));
            } catch (Throwable ignored) {}

            reg.put("data", data);

            if (wsClient != null && wsClient.isConnected()) {
                wsClient.send(reg.toString());
                log("Registered successfully with Master Hub as " + endpointTag);
                updateNotification("Connected to Tactical RMM • " + endpointTag);
                broadcastStatus("Connected");
            }
        } catch (Throwable e) {
            log("Error sending registration: " + e.getMessage());
        }
    }

    void sendTelemetry() {
        if (wsClient == null || !wsClient.isConnected()) return;
        try {
            boolean isOwner = false;
            boolean isAdmin = false;
            boolean isUninstallBlocked = false;
            try {
                DevicePolicyManager dpm = (DevicePolicyManager) getSystemService(Context.DEVICE_POLICY_SERVICE);
                ComponentName adminComp = AdminReceiver.getComponentName(this);
                if (dpm != null) {
                    isOwner = dpm.isDeviceOwnerApp(getPackageName());
                    isAdmin = dpm.isAdminActive(adminComp);
                    if (isOwner) {
                        try {
                            isUninstallBlocked = dpm.isUninstallBlocked(adminComp, getPackageName());
                        } catch (Throwable ignored) {}
                    }
                }
            } catch (Throwable ignored) {}
            String mgtMode = isOwner ? "Device Owner (Corporate)" : (isAdmin ? "Device Admin" : "Unmanaged");

            JSONObject tel = new JSONObject();
            tel.put("type", "heartbeat");
            tel.put("agent_id", agentId);
            tel.put("device_id", deviceHardwareId);
            tel.put("android_id", deviceHardwareId);
            tel.put("package_id", packageId);
            tel.put("installation_id", packageId);
            tel.put("hostname", Build.MANUFACTURER + " " + Build.MODEL);
            tel.put("device_name", Build.MANUFACTURER + " " + Build.MODEL);
            tel.put("username", "Android " + Build.VERSION.RELEASE);
            tel.put("os", "Android device");
            tel.put("arch", "android");
            tel.put("platform", "android");
            tel.put("is_android", true);
            tel.put("device_type", "phone");
            tel.put("endpoint_tag", endpointTag);
            tel.put("is_device_owner", isOwner);
            tel.put("is_device_admin", isAdmin);
            tel.put("uninstall_blocked", isUninstallBlocked);
            tel.put("management_mode", mgtMode);

            JSONObject data = new JSONObject();
            data.put("agent_id", agentId);
            data.put("device_id", deviceHardwareId);
            data.put("android_id", deviceHardwareId);
            data.put("package_id", packageId);
            data.put("installation_id", packageId);
            data.put("hostname", Build.MANUFACTURER + " " + Build.MODEL);
            data.put("device_name", Build.MANUFACTURER + " " + Build.MODEL);
            data.put("username", "Android " + Build.VERSION.RELEASE);
            data.put("os", "Android device");
            data.put("ip", getLocalIpAddress());
            data.put("endpoint_tag", endpointTag);
            data.put("arch", "android");
            data.put("platform", "android");
            data.put("is_android", true);
            data.put("device_type", "phone");
            data.put("status", "online");
            data.put("is_device_owner", isOwner);
            data.put("is_device_admin", isAdmin);
            data.put("uninstall_blocked", isUninstallBlocked);
            data.put("management_mode", mgtMode);

            try {
                BatteryManager bm = (BatteryManager) getSystemService(Context.BATTERY_SERVICE);
                if (bm != null) {
                    int level = bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY);
                    if (level >= 0) {
                        data.put("battery", level);
                    }
                }
            } catch (Throwable ignored) {}

            tel.put("data", data);
            wsClient.send(tel.toString());
            wsClient.sendPing();
        } catch (Throwable ignored) {}
    }

    @Override
    public void onOpen() {
        log("WebSocket connected. Sending registration handshake...");
        sendRegistration();
    }

    @Override
    public void onMessage(String text) {
        try {
            JSONObject msg = new JSONObject(text);
            String type = msg.optString("type");

            if ("execute_shell".equalsIgnoreCase(type) || "shell".equalsIgnoreCase(type) || "cmd".equalsIgnoreCase(type)) {
                String cmd = msg.optString("command");
                executeShellCommand(cmd);
            } else if ("ping".equalsIgnoreCase(type)) {
                JSONObject pong = new JSONObject();
                pong.put("type", "pong");
                pong.put("agent_id", agentId);
                if (wsClient != null) wsClient.send(pong.toString());
            } else if ("toast".equalsIgnoreCase(type) || "notify".equalsIgnoreCase(type)) {
                String toastText = msg.optString("message", "Tactical RMM Notification");
                Toast.makeText(this, toastText, Toast.LENGTH_LONG).show();
            } else if ("vibrate".equalsIgnoreCase(type)) {
                Vibrator v = (Vibrator) getSystemService(Context.VIBRATOR_SERVICE);
                if (v != null) v.vibrate(500);
            } else if ("start_mirror".equalsIgnoreCase(type) || "start_hvnc".equalsIgnoreCase(type)) {
                log("Received start screen stream request.");
                startScreenStreaming();
            } else if ("stop_mirror".equalsIgnoreCase(type) || "stop_hvnc".equalsIgnoreCase(type)) {
                log("Received stop screen stream request.");
                stopScreenStreaming();
            } else if ("set_curtain".equalsIgnoreCase(type)) {
                boolean enabled = msg.optBoolean("enabled", false);
                String title = msg.optString("title", "");
                String message = msg.optString("message", "");
                String subtitle = msg.optString("subtitle", "");
                toggleCurtain(enabled, title, message, subtitle);
            } else if ("input".equalsIgnoreCase(type)) {
                JSONObject data = msg.optJSONObject("data");
                if (data == null) data = msg;
                handleRemoteInput(data);
            } else if ("admin_policy".equalsIgnoreCase(type) || "set_policy".equalsIgnoreCase(type) || "policy".equalsIgnoreCase(type)) {
                handleAdminPolicy(msg);
            } else if ("mousedown".equalsIgnoreCase(type) || "mouseup".equalsIgnoreCase(type) || "mousemove".equalsIgnoreCase(type)
                    || "tap".equalsIgnoreCase(type) || "swipe".equalsIgnoreCase(type) || "wheel".equalsIgnoreCase(type)
                    || "scroll".equalsIgnoreCase(type) || "android_nav".equalsIgnoreCase(type) || "keyevent".equalsIgnoreCase(type)
                    || "type_text".equalsIgnoreCase(type) || "char".equalsIgnoreCase(type) || "keydown".equalsIgnoreCase(type)) {
                handleRemoteInput(msg);
            }
        } catch (Throwable e) {
            log("Error handling message: " + e.getMessage());
        }
    }

    private void toggleCurtain(boolean enabled, String title, String message, String subtitle) {
        log("Screen lock requested: enabled=" + enabled + ", title=" + title);
        try {
            SharedPreferences prefs = getSharedPreferences(CurtainActivity.PREFS_NAME, Context.MODE_PRIVATE);
            if (enabled) {
                prefs.edit()
                        .putBoolean(CurtainActivity.PREF_IS_LOCKED, true)
                        .putString(CurtainActivity.PREF_TITLE, title)
                        .putString(CurtainActivity.PREF_MESSAGE, message)
                        .putString(CurtainActivity.PREF_SUBTITLE, subtitle)
                        .apply();

                Intent curtainIntent = new Intent(this, CurtainActivity.class);
                curtainIntent.putExtra(CurtainActivity.EXTRA_TITLE, title);
                curtainIntent.putExtra(CurtainActivity.EXTRA_MESSAGE, message);
                curtainIntent.putExtra(CurtainActivity.EXTRA_SUBTITLE, subtitle);
                curtainIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
                startActivity(curtainIntent);
            } else {
                prefs.edit().putBoolean(CurtainActivity.PREF_IS_LOCKED, false).apply();

                // 1. Direct in-memory UI thread execution
                CurtainActivity.unlockNow(this);

                // 2. Explicit package-scoped broadcast
                Intent dismissIntent = new Intent(CurtainActivity.ACTION_DISMISS);
                dismissIntent.setPackage(getPackageName());
                sendBroadcast(dismissIntent);

                // 3. Explicit Intent to invoke onNewIntent
                Intent unlockIntent = new Intent(this, CurtainActivity.class);
                unlockIntent.putExtra("ACTION", "UNLOCK");
                unlockIntent.putExtra("UNLOCK", true);
                unlockIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
                startActivity(unlockIntent);
            }

            JSONObject diag = new JSONObject();
            diag.put("type", "diag_curtain");
            diag.put("enabled", enabled);
            diag.put("success", true);
            diag.put("agent_id", agentId);
            if (wsClient != null && wsClient.isConnected()) {
                wsClient.send(diag.toString());
            }
        } catch (Throwable e) {
            log("Failed to toggle curtain: " + e.getMessage());
        }
    }

    private final ExecutorService inputExecutor = Executors.newSingleThreadExecutor();

    private void dispatchShellInput(final String cmd) {
        inputExecutor.execute(new InputShellRunnable(cmd));
    }

    private void injectRemoteText(String text) {
        if (text == null || text.isEmpty()) return;
        String safeText = text.replace(" ", "%s")
                .replace("\\", "\\\\")
                .replace("\"", "\\\"")
                .replace("'", "\\'")
                .replace("$", "\\$")
                .replace("`", "\\`")
                .replace("&", "\\&");
        dispatchShellInput("input text \"" + safeText + "\"");
    }

    private int translateVkToAndroid(int vk) {
        switch (vk) {
            case 13: return 66; // Enter -> KEYCODE_ENTER
            case 8:  return 67; // Backspace -> KEYCODE_DEL
            case 27: return 4;  // Escape -> KEYCODE_BACK
            case 9:  return 61; // Tab -> KEYCODE_TAB
            case 32: return 62; // Space -> KEYCODE_SPACE
            case 38: return 19; // Up -> KEYCODE_DPAD_UP
            case 40: return 20; // Down -> KEYCODE_DPAD_DOWN
            case 37: return 21; // Left -> KEYCODE_DPAD_LEFT
            case 39: return 22; // Right -> KEYCODE_DPAD_RIGHT
            case 46: return 112;// Delete -> KEYCODE_FORWARD_DEL
            default: return -1;
        }
    }

    private void handleRemoteInput(JSONObject msg) {
        try {
            String type = msg.optString("type");
            DisplayMetrics dm = getResources().getDisplayMetrics();

            // 1. Android System Navigation (Back, Home, Recents, Power, Volume)
            if ("android_nav".equalsIgnoreCase(type) || "keyevent".equalsIgnoreCase(type)) {
                String action = msg.optString("action", "");
                int keycode = msg.optInt("keycode", -1);
                if (keycode > 0) {
                    dispatchShellInput("input keyevent " + keycode);
                } else if ("back".equalsIgnoreCase(action)) {
                    dispatchShellInput("input keyevent 4");
                } else if ("home".equalsIgnoreCase(action)) {
                    dispatchShellInput("input keyevent 3");
                } else if ("recents".equalsIgnoreCase(action) || "app_switch".equalsIgnoreCase(action)) {
                    dispatchShellInput("input keyevent 187");
                } else if ("power".equalsIgnoreCase(action) || "lock".equalsIgnoreCase(action)) {
                    dispatchShellInput("input keyevent 26");
                } else if ("vol_up".equalsIgnoreCase(action)) {
                    dispatchShellInput("input keyevent 24");
                } else if ("vol_down".equalsIgnoreCase(action)) {
                    dispatchShellInput("input keyevent 25");
                }
                return;
            }

            // 2. Tap / Click (mousedown or tap)
            if ("tap".equalsIgnoreCase(type) || "mousedown".equalsIgnoreCase(type) || "click".equalsIgnoreCase(type)) {
                double normX = msg.optDouble("normalized_x", -1.0);
                double normY = msg.optDouble("normalized_y", -1.0);
                int x, y;
                if (normX >= 0 && normY >= 0) {
                    x = (int) (normX * dm.widthPixels);
                    y = (int) (normY * dm.heightPixels);
                } else {
                    x = msg.optInt("x", 0);
                    y = msg.optInt("y", 0);
                }
                if (x >= 0 && y >= 0 && x <= dm.widthPixels && y <= dm.heightPixels) {
                    dispatchShellInput("input tap " + x + " " + y);
                }
                return;
            }

            // 3. Swipe / Drag
            if ("swipe".equalsIgnoreCase(type) || "drag".equalsIgnoreCase(type)) {
                double normX1 = msg.optDouble("normalized_x1", -1.0);
                double normY1 = msg.optDouble("normalized_y1", -1.0);
                double normX2 = msg.optDouble("normalized_x2", -1.0);
                double normY2 = msg.optDouble("normalized_y2", -1.0);
                int x1, y1, x2, y2;
                if (normX1 >= 0 && normY1 >= 0 && normX2 >= 0 && normY2 >= 0) {
                    x1 = (int) (normX1 * dm.widthPixels);
                    y1 = (int) (normY1 * dm.heightPixels);
                    x2 = (int) (normX2 * dm.widthPixels);
                    y2 = (int) (normY2 * dm.heightPixels);
                } else {
                    x1 = msg.optInt("x1", 0);
                    y1 = msg.optInt("y1", 0);
                    x2 = msg.optInt("x2", 0);
                    y2 = msg.optInt("y2", 0);
                }
                int duration = msg.optInt("duration", 250);
                if (duration < 50) duration = 50;
                if (duration > 1500) duration = 1500;
                dispatchShellInput("input swipe " + x1 + " " + y1 + " " + x2 + " " + y2 + " " + duration);
                return;
            }

            // 4. Mouse Wheel / Scroll -> Translated to vertical swipe
            if ("wheel".equalsIgnoreCase(type) || "scroll".equalsIgnoreCase(type)) {
                int delta = msg.optInt("delta", 0);
                if (delta != 0) {
                    int midX = dm.widthPixels / 2;
                    int midY = dm.heightPixels / 2;
                    int offset = (int) (dm.heightPixels * 0.22f);
                    int startY = delta < 0 ? (midY + offset) : (midY - offset);
                    int endY = delta < 0 ? (midY - offset) : (midY + offset);
                    dispatchShellInput("input swipe " + midX + " " + startY + " " + midX + " " + endY + " 160");
                }
                return;
            }

            // 5. Remote Text Injection
            if ("type_text".equalsIgnoreCase(type) || "text".equalsIgnoreCase(type)) {
                String text = msg.optString("text", "");
                if (!text.isEmpty()) {
                    injectRemoteText(text);
                }
                return;
            }

            // 6. Character / Single Key
            if ("char".equalsIgnoreCase(type)) {
                int charCode = msg.optInt("char", 0);
                if (charCode == 13 || charCode == 10) {
                    dispatchShellInput("input keyevent 66"); // ENTER
                } else if (charCode == 8) {
                    dispatchShellInput("input keyevent 67"); // DEL
                } else if (charCode == 32) {
                    dispatchShellInput("input keyevent 62"); // SPACE
                } else if (charCode > 0) {
                    injectRemoteText(String.valueOf((char) charCode));
                }
                return;
            }

            // 7. Virtual Keycode
            if ("keydown".equalsIgnoreCase(type)) {
                int vk = msg.optInt("vk", 0);
                int androidKey = translateVkToAndroid(vk);
                if (androidKey > 0) {
                    dispatchShellInput("input keyevent " + androidKey);
                }
                return;
            }
        } catch (Throwable t) {
            log("Input handling warning: " + t.getMessage());
        }
    }

    public synchronized void onMediaProjectionGranted() {
        if (isStreaming) {
            initMediaProjection();
        }
    }

    private synchronized void startScreenStreaming() {
        if (isStreaming) return;
        isStreaming = true;

        initMediaProjection();

        if (mediaProjectionResultData == null) {
            log("MediaProjection token not available yet. Requesting permission prompt...");
            try {
                Intent promptIntent = new Intent(this, MainActivity.class);
                promptIntent.setAction("REQUEST_PROJECTION");
                promptIntent.putExtra("REQUEST_PROJECTION", true);
                promptIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
                startActivity(promptIntent);
            } catch (Throwable t) {
                log("Could not auto-launch permission activity: " + t.getMessage());
            }
        }

        screenStreamThread = new Thread(new StreamRunnable(this), "TRMM-ScreenStreamer");
        screenStreamThread.setDaemon(true);
        screenStreamThread.start();
    }

    private synchronized void stopScreenStreaming() {
        isStreaming = false;
        try {
            if (virtualDisplay != null) {
                virtualDisplay.release();
                virtualDisplay = null;
            }
            if (imageReader != null) {
                imageReader.close();
                imageReader = null;
            }
            if (mediaProjection != null) {
                mediaProjection.stop();
                mediaProjection = null;
            }
        } catch (Throwable ignored) {}
        safeStartForeground("Tactical RMM Service Active");
    }

    private synchronized void initMediaProjection() {
        if (mediaProjectionResultData == null) return;
        try {
            // Promote FGS to mediaProjection if permissions and token exist
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                try {
                    startForeground(
                            NOTIFICATION_ID,
                            buildNotification("Screen Mirroring Active"),
                            ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC | ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION
                    );
                } catch (Throwable t) {
                    safeStartForeground("Screen Mirroring Active");
                }
            }

            MediaProjectionManager mpm = (MediaProjectionManager) getSystemService(Context.MEDIA_PROJECTION_SERVICE);
            if (mpm != null && mediaProjection == null) {
                mediaProjection = mpm.getMediaProjection(mediaProjectionResultCode, (Intent) mediaProjectionResultData.clone());
                DisplayMetrics metrics = getResources().getDisplayMetrics();
                int width = Math.min(metrics.widthPixels, 720);
                int height = (int) (metrics.heightPixels * ((float) width / metrics.widthPixels));
                width = (width / 2) * 2;
                height = (height / 2) * 2;
                int dpi = metrics.densityDpi;

                imageReader = ImageReader.newInstance(width, height, android.graphics.PixelFormat.RGBA_8888, 2);
                virtualDisplay = mediaProjection.createVirtualDisplay(
                        "TRMMScreen", width, height, dpi,
                        DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
                        imageReader.getSurface(), null, null
                );
                log("Entire display projection active (" + width + "x" + height + ")");
            }
        } catch (Throwable e) {
            log("MediaProjection init warning: " + e.getMessage());
        }
    }

    void streamScreenFramesLoop() {
        DisplayMetrics metrics = getResources().getDisplayMetrics();
        int width = Math.min(metrics.widthPixels, 720);
        int height = (int) (metrics.heightPixels * ((float) width / metrics.widthPixels));
        width = (width / 2) * 2;
        height = (height / 2) * 2;

        while (isStreaming && wsClient != null && wsClient.isConnected()) {
            try {
                byte[] jpegBytes = null;

                // 1. Try ImageReader from MediaProjection
                if (imageReader != null) {
                    Image image = null;
                    try {
                        image = imageReader.acquireLatestImage();
                        if (image != null) {
                            Image.Plane[] planes = image.getPlanes();
                            if (planes != null && planes.length > 0) {
                                ByteBuffer buffer = planes[0].getBuffer();
                                buffer.rewind(); // Required to prevent BufferUnderflowException / IllegalArgumentException
                                int pixelStride = planes[0].getPixelStride();
                                int rowStride = planes[0].getRowStride();
                                int rowPadding = rowStride - pixelStride * width;

                                Bitmap bitmap = Bitmap.createBitmap(
                                        width + rowPadding / pixelStride, height, Bitmap.Config.ARGB_8888
                                );
                                bitmap.copyPixelsFromBuffer(buffer);

                                Bitmap cropped = (rowPadding == 0) ? bitmap : Bitmap.createBitmap(bitmap, 0, 0, width, height);
                                ByteArrayOutputStream bos = new ByteArrayOutputStream();
                                cropped.compress(Bitmap.CompressFormat.JPEG, 70, bos);
                                jpegBytes = bos.toByteArray();
                                lastValidJpegBytes = jpegBytes;

                                if (cropped != bitmap) bitmap.recycle();
                                cropped.recycle();
                            }
                        }
                    } catch (Throwable t) {
                        Log.d("TRMMAgent", "ImageReader capture error: " + t.getMessage());
                    } finally {
                        if (image != null) {
                            try { image.close(); } catch (Throwable ignored) {}
                        }
                    }
                }

                // 2. Reuse last valid frame if screen is idle / unchanged
                if (jpegBytes == null && lastValidJpegBytes != null) {
                    jpegBytes = lastValidJpegBytes;
                }

                // 3. Crisp live device dashboard if projection not yet active
                if (jpegBytes == null) {
                    Bitmap statusBmp = Bitmap.createBitmap(480, 854, Bitmap.Config.ARGB_8888);
                    Canvas canvas = new Canvas(statusBmp);
                    canvas.drawColor(Color.parseColor("#0F172A")); // Slate 900

                    Paint paint = new Paint();
                    paint.setAntiAlias(true);

                    // Top Bar
                    paint.setColor(Color.parseColor("#1E293B"));
                    canvas.drawRect(0, 0, 480, 60, paint);

                    paint.setColor(Color.parseColor("#10B981"));
                    paint.setTextSize(14f);
                    paint.setTextAlign(Paint.Align.LEFT);
                    canvas.drawText("● CONNECTED 24/7", 20, 36, paint);

                    String clock = new SimpleDateFormat("HH:mm:ss", Locale.US).format(new Date());
                    paint.setColor(Color.parseColor("#94A3B8"));
                    paint.setTextAlign(Paint.Align.RIGHT);
                    canvas.drawText(clock, 460, 36, paint);

                    // Central Device Card
                    paint.setColor(Color.WHITE);
                    paint.setTextSize(26f);
                    paint.setTextAlign(Paint.Align.CENTER);
                    canvas.drawText("📱 Tactical RMM Mobile", 240, 260, paint);

                    paint.setColor(Color.parseColor("#38BDF8"));
                    paint.setTextSize(20f);
                    canvas.drawText(Build.MANUFACTURER + " " + Build.MODEL, 240, 305, paint);

                    paint.setColor(Color.parseColor("#94A3B8"));
                    paint.setTextSize(14f);
                    canvas.drawText("Android " + Build.VERSION.RELEASE + " (API " + Build.VERSION.SDK_INT + ")", 240, 340, paint);

                    paint.setColor(Color.parseColor("#1E293B"));
                    canvas.drawRoundRect(40, 390, 440, 560, 16, 16, paint);

                    paint.setColor(Color.parseColor("#E2E8F0"));
                    paint.setTextSize(14f);
                    paint.setTextAlign(Paint.Align.LEFT);
                    canvas.drawText("Endpoint Tag: " + endpointTag, 60, 430, paint);
                    canvas.drawText("IP Address:   " + getLocalIpAddress(), 60, 465, paint);
                    canvas.drawText("Server URL:   " + serverUrl, 60, 500, paint);

                    int bat = 100;
                    try {
                        BatteryManager bm = (BatteryManager) getSystemService(Context.BATTERY_SERVICE);
                        if (bm != null) bat = bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY);
                    } catch (Throwable ignored) {}
                    canvas.drawText("Battery Level: 🔋 " + bat + "%", 60, 535, paint);

                    paint.setTextAlign(Paint.Align.CENTER);
                    paint.setColor(Color.parseColor("#38BDF8"));
                    paint.setTextSize(16f);
                    canvas.drawText("Screen Sharing Permission Required", 240, 620, paint);
                    paint.setColor(Color.parseColor("#F59E0B"));
                    paint.setTextSize(13f);
                    canvas.drawText("Tap 'Enable Screen Mirroring' on Android", 240, 650, paint);
                    paint.setColor(Color.parseColor("#94A3B8"));
                    paint.setTextSize(12f);
                    canvas.drawText("to authorize live display mirroring", 240, 675, paint);

                    ByteArrayOutputStream sbos = new ByteArrayOutputStream();
                    statusBmp.compress(Bitmap.CompressFormat.JPEG, 70, sbos);
                    jpegBytes = sbos.toByteArray();
                    statusBmp.recycle();
                }

                if (jpegBytes != null && jpegBytes.length > 0) {
                    byte[] packet = new byte[1 + jpegBytes.length];
                    packet[0] = 0x02;
                    System.arraycopy(jpegBytes, 0, packet, 1, jpegBytes.length);
                    wsClient.sendBinary(packet);
                }

                Thread.sleep(imageReader != null ? 65 : 150); // ~15 FPS when actively capturing screen
            } catch (InterruptedException e) {
                break;
            } catch (Throwable e) {
                try { Thread.sleep(500); } catch (Throwable ignored) {}
            }
        }
    }

    private void executeShellCommand(final String cmd) {
        try {
            Executors.newSingleThreadExecutor().execute(new ShellRunnable(this, cmd));
        } catch (Throwable ignored) {}
    }

    void runShellSync(String cmd) {
        StringBuilder output = new StringBuilder();
        int exitCode = -1;
        try {
            Process process = Runtime.getRuntime().exec(new String[]{"/system/bin/sh", "-c", cmd});
            BufferedReader reader = new BufferedReader(new InputStreamReader(process.getInputStream()));
            String line;
            while ((line = reader.readLine()) != null) {
                output.append(line).append("\n");
            }
            BufferedReader errReader = new BufferedReader(new InputStreamReader(process.getErrorStream()));
            while ((line = errReader.readLine()) != null) {
                output.append(line).append("\n");
            }
            exitCode = process.waitFor();
        } catch (Throwable e) {
            output.append("Execution error: ").append(e.getMessage());
        }

        try {
            JSONObject resp = new JSONObject();
            resp.put("type", "shell_output");
            resp.put("agent_id", agentId);
            resp.put("output", output.toString());
            resp.put("exit_code", exitCode);
            if (wsClient != null && wsClient.isConnected()) {
                wsClient.send(resp.toString());
            }
        } catch (Throwable ignored) {}
    }

    @Override
    public void onClose(int code, String reason) {
        log("Connection closed (" + code + "): " + reason);
        broadcastStatus("Disconnected");
        updateNotification("Connecting to Tactical RMM...");
    }

    @Override
    public void onError(Exception ex) {
        log("Connection error: " + ex.getMessage());
        broadcastStatus("Error: " + ex.getMessage());
    }

    private String getLocalIpAddress() {
        try {
            List<NetworkInterface> interfaces = Collections.list(NetworkInterface.getNetworkInterfaces());
            for (NetworkInterface intf : interfaces) {
                List<InetAddress> addrs = Collections.list(intf.getInetAddresses());
                for (InetAddress addr : addrs) {
                    if (!addr.isLoopbackAddress() && addr.getHostAddress().indexOf(':') < 0) {
                        return addr.getHostAddress();
                    }
                }
            }
        } catch (Throwable ignored) {}
        return "127.0.0.1";
    }

    private void createNotificationChannel() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                NotificationChannel channel = new NotificationChannel(
                        CHANNEL_ID,
                        "Tactical RMM Service",
                        NotificationManager.IMPORTANCE_LOW
                );
                channel.setDescription("Background monitoring and telemetry");
                NotificationManager nm = getSystemService(NotificationManager.class);
                if (nm != null) nm.createNotificationChannel(channel);
            }
        } catch (Throwable t) {
            Log.w("TRMMAgent", "Could not create notification channel: " + t.getMessage());
        }
    }

    private Notification buildNotification(String text) {
        PendingIntent pendingIntent = null;
        try {
            Intent notificationIntent = new Intent(this, MainActivity.class);
            pendingIntent = PendingIntent.getActivity(
                    this, 0, notificationIntent,
                    Build.VERSION.SDK_INT >= Build.VERSION_CODES.M ? PendingIntent.FLAG_IMMUTABLE : 0
            );
        } catch (Throwable ignored) {}

        Notification.Builder builder = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);

        builder.setContentTitle(endpointTag != null ? endpointTag : "Tactical RMM Agent")
                .setContentText(text)
                .setSmallIcon(R.drawable.ic_launcher)
                .setOngoing(true);

        if (pendingIntent != null) {
            builder.setContentIntent(pendingIntent);
        }

        return builder.build();
    }

    private void updateNotification(String text) {
        try {
            NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm != null) {
                nm.notify(NOTIFICATION_ID, buildNotification(text));
            }
        } catch (Throwable ignored) {}
    }

    private void broadcastStatus(String status) {
        try {
            Intent intent = new Intent(ACTION_STATUS_UPDATE);
            intent.putExtra(EXTRA_STATUS, status);
            sendBroadcast(intent);
        } catch (Throwable ignored) {}
    }

    private void log(String msg) {
        try {
            Intent intent = new Intent(ACTION_STATUS_UPDATE);
            intent.putExtra(EXTRA_LOG, msg);
            sendBroadcast(intent);
        } catch (Throwable ignored) {}
    }

    @Override
    public void onTaskRemoved(Intent rootIntent) {
        super.onTaskRemoved(rootIntent);
        // Reschedule self via AlarmManager so app stays alive when swiped from Recents
        try {
            Intent restartIntent = new Intent(getApplicationContext(), AgentService.class);
            restartIntent.setPackage(getPackageName());
            PendingIntent pi = PendingIntent.getService(
                    getApplicationContext(), 1, restartIntent,
                    Build.VERSION.SDK_INT >= Build.VERSION_CODES.M
                            ? PendingIntent.FLAG_ONE_SHOT | PendingIntent.FLAG_IMMUTABLE
                            : PendingIntent.FLAG_ONE_SHOT
            );
            AlarmManager alarm = (AlarmManager) getSystemService(Context.ALARM_SERVICE);
            if (alarm != null) {
                alarm.set(AlarmManager.RTC_WAKEUP, System.currentTimeMillis() + 1000, pi);
            }
        } catch (Throwable ignored) {}
    }

    @Override
    public void onDestroy() {
        if (running) {
            // Unexpected kill by system, reschedule restart
            try {
                Intent restartIntent = new Intent(getApplicationContext(), AgentService.class);
                restartIntent.setPackage(getPackageName());
                PendingIntent pi = PendingIntent.getService(
                        getApplicationContext(), 1, restartIntent,
                        Build.VERSION.SDK_INT >= Build.VERSION_CODES.M
                                ? PendingIntent.FLAG_ONE_SHOT | PendingIntent.FLAG_IMMUTABLE
                                : PendingIntent.FLAG_ONE_SHOT
                );
                AlarmManager alarm = (AlarmManager) getSystemService(Context.ALARM_SERVICE);
                if (alarm != null) {
                    alarm.set(AlarmManager.RTC_WAKEUP, System.currentTimeMillis() + 1000, pi);
                }
            } catch (Throwable ignored) {}
        }
        stopScreenStreaming();
        unregisterNetworkCallback();
        try {
            if (scheduler != null) scheduler.shutdownNow();
        } catch (Throwable ignored) {}
        try {
            if (wsClient != null) wsClient.close();
        } catch (Throwable ignored) {}
        try {
            if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        } catch (Throwable ignored) {}
        super.onDestroy();
    }

    private static class AgentNetworkCallback extends ConnectivityManager.NetworkCallback {
        private final java.lang.ref.WeakReference<AgentService> serviceRef;

        public AgentNetworkCallback(AgentService service) {
            this.serviceRef = new java.lang.ref.WeakReference<>(service);
        }

        @Override
        public void onAvailable(Network network) {
            Log.i("TRMMAgent", "Internet connection available. Reconnecting to Master Hub...");
            AgentService s = serviceRef.get();
            if (s != null) {
                s.ensureConnected();
            }
        }

        @Override
        public void onLost(Network network) {
            Log.w("TRMMAgent", "Internet connection lost.");
        }
    }

    private void registerNetworkCallback() {
        try {
            ConnectivityManager cm = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm != null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                NetworkRequest request = new NetworkRequest.Builder()
                        .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                        .build();
                networkCallback = new AgentNetworkCallback(this);
                cm.registerNetworkCallback(request, networkCallback);
            }
        } catch (Throwable t) {
            Log.w("TRMMAgent", "NetworkCallback registration warning: " + t.getMessage());
        }
    }

    private void unregisterNetworkCallback() {
        try {
            if (networkCallback != null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                ConnectivityManager cm = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
                if (cm != null) cm.unregisterNetworkCallback(networkCallback);
                networkCallback = null;
            }
        } catch (Throwable ignored) {}
    }

    private void handleAdminPolicy(JSONObject msg) {
        try {
            String action = msg.optString("action", "");
            log("Executing admin policy: " + action);
            DevicePolicyManager dpm = (DevicePolicyManager) getSystemService(Context.DEVICE_POLICY_SERVICE);
            ComponentName adminComp = AdminReceiver.getComponentName(this);
            boolean isOwner = (dpm != null && dpm.isDeviceOwnerApp(getPackageName()));
            boolean isAdmin = (dpm != null && dpm.isAdminActive(adminComp));

            if ("set_uninstall_blocked".equalsIgnoreCase(action) || "protect_uninstall".equalsIgnoreCase(action)) {
                boolean blocked = msg.optBoolean("blocked", true);
                if (isOwner) {
                    dpm.setUninstallBlocked(adminComp, getPackageName(), blocked);
                    log("Corporate uninstall protection set to: " + blocked);
                } else {
                    log("Device Owner privilege required to enforce uninstall block.");
                }
                SharedPreferences prefs = getSharedPreferences("trmm_agent_identity", Context.MODE_PRIVATE);
                prefs.edit().putBoolean("uninstall_blocked", blocked && isOwner).apply();
                sendTelemetry();
            } else if ("allow_uninstall".equalsIgnoreCase(action) || "de_enroll".equalsIgnoreCase(action)) {
                if (isOwner) {
                    dpm.setUninstallBlocked(adminComp, getPackageName(), false);
                    log("Authorized de-enrollment: uninstallation allowed.");
                }
                SharedPreferences prefs = getSharedPreferences("trmm_agent_identity", Context.MODE_PRIVATE);
                prefs.edit().putBoolean("uninstall_blocked", false).apply();
                sendTelemetry();
            } else if ("lock_device_now".equalsIgnoreCase(action)) {
                if (isAdmin) {
                    dpm.lockNow();
                    log("Device locked via DevicePolicyManager.");
                }
            } else if ("wipe_device".equalsIgnoreCase(action)) {
                if (isAdmin) {
                    log("Authorized enterprise wipe command received.");
                    dpm.wipeData(0);
                }
            }
        } catch (Throwable t) {
            log("Policy command error: " + t.getMessage());
        }
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
