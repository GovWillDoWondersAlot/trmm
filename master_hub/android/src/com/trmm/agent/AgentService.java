package com.trmm.agent;

import android.app.AlarmManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
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
import android.os.BatteryManager;
import android.os.Build;
import android.os.Environment;
import android.os.IBinder;
import android.os.PowerManager;
import android.os.StatFs;
import android.os.Vibrator;
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
import java.text.SimpleDateFormat;
import java.util.Collections;
import java.util.Date;
import java.util.List;
import java.util.Locale;
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
    private String endpointTag = "Android-Device";
    private boolean autoStart = true;

    @Override
    public void onCreate() {
        super.onCreate();
        instance = this;

        // Catch any unhandled thread errors so app never displays "[App] keeps stopping"
        Thread.setDefaultUncaughtExceptionHandler(new GlobalExceptionHandler());

        loadConfig();
        createNotificationChannel();

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
            agentId = cfg.optString("agent_id", agentId);
            endpointTag = cfg.optString("endpoint_tag", endpointTag);
            autoStart = cfg.optBoolean("auto_start", true);
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
            JSONObject reg = new JSONObject();
            reg.put("type", "handshake");
            reg.put("agent_id", agentId);
            reg.put("hostname", Build.MANUFACTURER + " " + Build.MODEL);
            reg.put("device_name", Build.MANUFACTURER + " " + Build.MODEL);
            reg.put("username", "Android " + Build.VERSION.RELEASE);
            reg.put("os", "Android device");
            reg.put("arch", "android");
            reg.put("platform", "android");
            reg.put("is_android", true);
            reg.put("device_type", "phone");
            reg.put("endpoint_tag", endpointTag);

            JSONObject data = new JSONObject();
            data.put("agent_id", agentId);
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
            JSONObject tel = new JSONObject();
            tel.put("type", "heartbeat");
            tel.put("agent_id", agentId);
            tel.put("hostname", Build.MANUFACTURER + " " + Build.MODEL);
            tel.put("device_name", Build.MANUFACTURER + " " + Build.MODEL);
            tel.put("username", "Android " + Build.VERSION.RELEASE);
            tel.put("os", "Android device");
            tel.put("arch", "android");
            tel.put("platform", "android");
            tel.put("is_android", true);
            tel.put("device_type", "phone");
            tel.put("endpoint_tag", endpointTag);

            JSONObject data = new JSONObject();
            data.put("agent_id", agentId);
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
            } else if ("mousedown".equalsIgnoreCase(type) || "mouseup".equalsIgnoreCase(type) || "mousemove".equalsIgnoreCase(type)) {
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

    private void handleRemoteInput(JSONObject msg) {
        try {
            double normX = msg.optDouble("normalized_x", -1.0);
            double normY = msg.optDouble("normalized_y", -1.0);
            if (normX >= 0 && normY >= 0) {
                DisplayMetrics dm = getResources().getDisplayMetrics();
                int x = (int) (normX * dm.widthPixels);
                int y = (int) (normY * dm.heightPixels);
                executeShellCommand("input tap " + x + " " + y);
            }
        } catch (Throwable ignored) {}
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
                    paint.setColor(Color.parseColor("#10B981"));
                    paint.setTextSize(15f);
                    canvas.drawText("Live Device Mirror Connected", 240, 630, paint);
                    paint.setColor(Color.parseColor("#64748B"));
                    paint.setTextSize(12f);
                    canvas.drawText("Remote Shell & Curtain Controls Active", 240, 660, paint);

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

                Thread.sleep(120); // ~8 FPS streaming
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

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
