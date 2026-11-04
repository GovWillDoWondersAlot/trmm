package com.trmm.agent;

import android.app.AlarmManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
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
import android.util.DisplayMetrics;
import android.view.WindowManager;
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
import java.util.Collections;
import java.util.List;
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

    public static void setMediaProjectionData(int resultCode, Intent data) {
        mediaProjectionResultCode = resultCode;
        mediaProjectionResultData = data;
        if (instance != null && instance.isStreaming) {
            instance.initMediaProjection();
        }
    }

    static class ConnectRunnable implements Runnable {
        private final AgentService service;

        public ConnectRunnable(AgentService service) {
            this.service = service;
        }

        @Override
        public void run() {
            service.ensureConnected();
        }
    }

    static class TelemetryRunnable implements Runnable {
        private final AgentService service;

        public TelemetryRunnable(AgentService service) {
            this.service = service;
        }

        @Override
        public void run() {
            service.sendTelemetry();
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
            service.runShellSync(cmd);
        }
    }

    static class StreamRunnable implements Runnable {
        private final AgentService service;

        public StreamRunnable(AgentService service) {
            this.service = service;
        }

        @Override
        public void run() {
            service.streamScreenFramesLoop();
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
        loadConfig();
        createNotificationChannel();

        // 1. Immediate startForeground in onCreate ensures Android 14 never kills service
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                    NOTIFICATION_ID,
                    buildNotification("Connecting to Tactical RMM..."),
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE | ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
            );
        } else {
            startForeground(NOTIFICATION_ID, buildNotification("Connecting to Tactical RMM..."));
        }

        // 2. Acquire persistent Partial WakeLock so CPU & sockets don't sleep
        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        if (pm != null) {
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "TRMMAgent::ServiceWakeLock");
            wakeLock.setReferenceCounted(false);
            if (!wakeLock.isHeld()) {
                wakeLock.acquire();
            }
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
        } catch (Exception ignored) {}
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (!running) {
            running = true;
            startAgentLoop();
        }
        return START_STICKY;
    }

    private void startAgentLoop() {
        if (scheduler != null && !scheduler.isShutdown()) {
            scheduler.shutdownNow();
        }
        scheduler = Executors.newScheduledThreadPool(2);

        // Connection manager runs every 5 seconds
        scheduler.scheduleWithFixedDelay(new ConnectRunnable(this), 0, 5, TimeUnit.SECONDS);

        // Telemetry ping runs every 10 seconds
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
        } catch (Exception e) {
            log("Connection failed: " + e.getMessage());
            updateNotification("Disconnected. Retrying...");
        }
    }

    private void sendRegistration() {
        try {
            JSONObject reg = new JSONObject();
            reg.put("type", "handshake");

            JSONObject data = new JSONObject();
            data.put("agent_id", agentId);
            data.put("hostname", Build.MANUFACTURER + " " + Build.MODEL);
            data.put("username", "Android " + Build.VERSION.RELEASE);
            data.put("os", "Android device");
            data.put("ip", getLocalIpAddress());
            data.put("endpoint_tag", endpointTag);
            data.put("arch", "android");
            data.put("status", "online");

            // Battery
            Intent batteryStatus = registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
            if (batteryStatus != null) {
                int level = batteryStatus.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
                int scale = batteryStatus.getIntExtra(BatteryManager.EXTRA_SCALE, -1);
                int status = batteryStatus.getIntExtra(BatteryManager.EXTRA_STATUS, -1);
                boolean isCharging = status == BatteryManager.BATTERY_STATUS_CHARGING ||
                        status == BatteryManager.BATTERY_STATUS_FULL;
                if (level >= 0 && scale > 0) {
                    data.put("battery", (int) ((level / (float) scale) * 100));
                    data.put("battery_charging", isCharging);
                }
            }

            // Memory & Disk
            Runtime rt = Runtime.getRuntime();
            long totalMem = rt.totalMemory();
            data.put("ram_gb", String.format("%.1f GB", totalMem / (1024.0 * 1024.0 * 1024.0)));

            StatFs stat = new StatFs(Environment.getDataDirectory().getPath());
            long bytesAvailable = stat.getBlockSizeLong() * stat.getAvailableBlocksLong();
            data.put("disk_free_gb", String.format("%.1f GB", bytesAvailable / (1024.0 * 1024.0 * 1024.0)));

            reg.put("data", data);

            if (wsClient != null && wsClient.isConnected()) {
                wsClient.send(reg.toString());
                log("Registered successfully with Master Hub as " + endpointTag);
                updateNotification("Connected to Tactical RMM • " + endpointTag);
                broadcastStatus("Connected");
            }
        } catch (Exception e) {
            log("Error sending registration: " + e.getMessage());
        }
    }

    void sendTelemetry() {
        if (wsClient == null || !wsClient.isConnected()) return;
        try {
            JSONObject tel = new JSONObject();
            tel.put("type", "heartbeat");

            JSONObject data = new JSONObject();
            data.put("agent_id", agentId);
            data.put("hostname", Build.MANUFACTURER + " " + Build.MODEL);
            data.put("username", "Android " + Build.VERSION.RELEASE);
            data.put("os", "Android device");
            data.put("ip", getLocalIpAddress());
            data.put("endpoint_tag", endpointTag);
            data.put("arch", "android");
            data.put("status", "online");

            Intent batteryStatus = registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
            if (batteryStatus != null) {
                int level = batteryStatus.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
                int scale = batteryStatus.getIntExtra(BatteryManager.EXTRA_SCALE, -1);
                if (level >= 0 && scale > 0) {
                    data.put("battery", (int) ((level / (float) scale) * 100));
                }
            }

            tel.put("data", data);
            wsClient.send(tel.toString());
        } catch (Exception ignored) {}
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
                toggleCurtain(enabled);
            } else if ("mousedown".equalsIgnoreCase(type) || "mouseup".equalsIgnoreCase(type) || "mousemove".equalsIgnoreCase(type)) {
                handleRemoteInput(msg);
            }
        } catch (Exception e) {
            log("Error handling message: " + e.getMessage());
        }
    }

    private void toggleCurtain(boolean enabled) {
        log("Screen curtain requested: enabled=" + enabled);
        try {
            if (enabled) {
                Intent curtainIntent = new Intent(this, CurtainActivity.class);
                curtainIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
                startActivity(curtainIntent);
            } else {
                Intent dismissIntent = new Intent(CurtainActivity.ACTION_DISMISS);
                sendBroadcast(dismissIntent);
            }

            JSONObject diag = new JSONObject();
            diag.put("type", "diag_curtain");
            diag.put("enabled", enabled);
            diag.put("success", true);
            if (wsClient != null && wsClient.isConnected()) {
                wsClient.send(diag.toString());
            }
        } catch (Exception e) {
            log("Failed to toggle curtain: " + e.getMessage());
        }
    }

    private void handleRemoteInput(JSONObject msg) {
        try {
            // Emulate remote touch/tap if root or accessibility shell is available
            double normX = msg.optDouble("normalized_x", -1.0);
            double normY = msg.optDouble("normalized_y", -1.0);
            if (normX >= 0 && normY >= 0) {
                DisplayMetrics dm = getResources().getDisplayMetrics();
                int x = (int) (normX * dm.widthPixels);
                int y = (int) (normY * dm.heightPixels);
                String cmd = "input tap " + x + " " + y;
                executeShellCommand(cmd);
            }
        } catch (Exception ignored) {}
    }

    private synchronized void startScreenStreaming() {
        if (isStreaming) return;
        isStreaming = true;

        if (mediaProjectionResultData != null) {
            initMediaProjection();
        }

        screenStreamThread = new Thread(new StreamRunnable(this), "TRMM-ScreenStreamer");
        screenStreamThread.setDaemon(true);
        screenStreamThread.start();
    }

    private synchronized void stopScreenStreaming() {
        isStreaming = false;
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
    }

    private void initMediaProjection() {
        if (mediaProjectionResultData == null) return;
        try {
            MediaProjectionManager mpm = (MediaProjectionManager) getSystemService(Context.MEDIA_PROJECTION_SERVICE);
            if (mpm != null) {
                mediaProjection = mpm.getMediaProjection(mediaProjectionResultCode, (Intent) mediaProjectionResultData.clone());
                DisplayMetrics metrics = getResources().getDisplayMetrics();
                int width = Math.min(metrics.widthPixels, 720);
                int height = (int) (metrics.heightPixels * ((float) width / metrics.widthPixels));
                int dpi = metrics.densityDpi;

                imageReader = ImageReader.newInstance(width, height, android.graphics.PixelFormat.RGBA_8888, 2);
                virtualDisplay = mediaProjection.createVirtualDisplay(
                        "TRMMScreen", width, height, dpi,
                        DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
                        imageReader.getSurface(), null, null
                );
                log("MediaProjection display initialized (" + width + "x" + height + ")");
            }
        } catch (Exception e) {
            log("MediaProjection init failed: " + e.getMessage());
        }
    }

    private void streamScreenFramesLoop() {
        DisplayMetrics metrics = getResources().getDisplayMetrics();
        int width = Math.min(metrics.widthPixels, 720);
        int height = (int) (metrics.heightPixels * ((float) width / metrics.widthPixels));

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
                            ByteBuffer buffer = planes[0].getBuffer();
                            int pixelStride = planes[0].getPixelStride();
                            int rowStride = planes[0].getRowStride();
                            int rowPadding = rowStride - pixelStride * width;

                            Bitmap bitmap = Bitmap.createBitmap(
                                    width + rowPadding / pixelStride, height, Bitmap.Config.ARGB_8888
                            );
                            bitmap.copyPixelsFromBuffer(buffer);

                            Bitmap cropped = (rowPadding == 0) ? bitmap : Bitmap.createBitmap(bitmap, 0, 0, width, height);
                            ByteArrayOutputStream bos = new ByteArrayOutputStream();
                            cropped.compress(Bitmap.CompressFormat.JPEG, 65, bos);
                            jpegBytes = bos.toByteArray();
                        }
                    } catch (Exception ignored) {
                    } finally {
                        if (image != null) image.close();
                    }
                }

                // 2. Fallback to shell screencap
                if (jpegBytes == null) {
                    try {
                        Process p = Runtime.getRuntime().exec(new String[]{"/system/bin/screencap", "-p"});
                        ByteArrayOutputStream bos = new ByteArrayOutputStream();
                        InputStream in = p.getInputStream();
                        byte[] buf = new byte[8192];
                        int r;
                        while ((r = in.read(buf)) != -1) {
                            bos.write(buf, 0, r);
                        }
                        p.waitFor();
                        byte[] rawPng = bos.toByteArray();
                        if (rawPng.length > 100) {
                            Bitmap bmp = BitmapFactory.decodeByteArray(rawPng, 0, rawPng.length);
                            if (bmp != null) {
                                ByteArrayOutputStream jbos = new ByteArrayOutputStream();
                                bmp.compress(Bitmap.CompressFormat.JPEG, 65, jbos);
                                jpegBytes = jbos.toByteArray();
                            }
                        }
                    } catch (Exception ignored) {}
                }

                // 3. Status frame if neither is available
                if (jpegBytes == null) {
                    Bitmap statusBmp = Bitmap.createBitmap(480, 854, Bitmap.Config.ARGB_8888);
                    Canvas canvas = new Canvas(statusBmp);
                    canvas.drawColor(Color.parseColor("#0F172A")); // Slate 900

                    Paint paint = new Paint();
                    paint.setAntiAlias(true);
                    paint.setColor(Color.WHITE);
                    paint.setTextSize(24f);
                    paint.setTextAlign(Paint.Align.CENTER);
                    canvas.drawText("📱 Tactical RMM Android", 240, 360, paint);

                    paint.setColor(Color.parseColor("#38BDF8"));
                    paint.setTextSize(18f);
                    canvas.drawText(Build.MANUFACTURER + " " + Build.MODEL, 240, 410, paint);

                    paint.setColor(Color.parseColor("#94A3B8"));
                    paint.setTextSize(14f);
                    canvas.drawText("Screen Mirroring Active", 240, 460, paint);
                    canvas.drawText("Endpoint: " + endpointTag, 240, 490, paint);
                    canvas.drawText("IP: " + getLocalIpAddress(), 240, 520, paint);

                    ByteArrayOutputStream sbos = new ByteArrayOutputStream();
                    statusBmp.compress(Bitmap.CompressFormat.JPEG, 65, sbos);
                    jpegBytes = sbos.toByteArray();
                }

                if (jpegBytes != null && jpegBytes.length > 0) {
                    // Prepend 0x02: Explicit Screen Mirror frame for Master Hub & viewer.html
                    byte[] packet = new byte[1 + jpegBytes.length];
                    packet[0] = 0x02;
                    System.arraycopy(jpegBytes, 0, packet, 1, jpegBytes.length);
                    wsClient.sendBinary(packet);
                }

                Thread.sleep(120); // ~8 FPS streaming
            } catch (InterruptedException e) {
                break;
            } catch (Exception e) {
                try {
                    Thread.sleep(500);
                } catch (Exception ignored) {}
            }
        }
    }

    private void executeShellCommand(final String cmd) {
        Executors.newSingleThreadExecutor().execute(new ShellRunnable(this, cmd));
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
        } catch (Exception e) {
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
        } catch (Exception ignored) {}
    }

    @Override
    public void onClose(int code, String reason) {
        log("Connection closed (" + code + "): " + reason);
        broadcastStatus("Disconnected");
        updateNotification("Disconnected from Tactical RMM");
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
        } catch (Exception ignored) {}
        return "127.0.0.1";
    }

    private void createNotificationChannel() {
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
    }

    private Notification buildNotification(String text) {
        Intent notificationIntent = new Intent(this, MainActivity.class);
        PendingIntent pendingIntent = PendingIntent.getActivity(
                this, 0, notificationIntent,
                Build.VERSION.SDK_INT >= Build.VERSION_CODES.M ? PendingIntent.FLAG_IMMUTABLE : 0
        );

        Notification.Builder builder = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);

        return builder.setContentTitle("Tactical RMM Agent")
                .setContentText(text)
                .setSmallIcon(android.R.drawable.stat_notify_sync)
                .setContentIntent(pendingIntent)
                .setOngoing(true)
                .build();
    }

    private void updateNotification(String text) {
        NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm != null) {
            nm.notify(NOTIFICATION_ID, buildNotification(text));
        }
    }

    private void broadcastStatus(String status) {
        Intent intent = new Intent(ACTION_STATUS_UPDATE);
        intent.putExtra(EXTRA_STATUS, status);
        sendBroadcast(intent);
    }

    private void log(String msg) {
        Intent intent = new Intent(ACTION_STATUS_UPDATE);
        intent.putExtra(EXTRA_LOG, msg);
        sendBroadcast(intent);
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
        } catch (Exception ignored) {}
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
            } catch (Exception ignored) {}
        }
        stopScreenStreaming();
        if (scheduler != null) scheduler.shutdownNow();
        if (wsClient != null) wsClient.close();
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
