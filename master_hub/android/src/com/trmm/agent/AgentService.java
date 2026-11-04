package com.trmm.agent;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.os.BatteryManager;
import android.os.Build;
import android.os.Environment;
import android.os.IBinder;
import android.os.PowerManager;
import android.os.StatFs;
import android.os.Vibrator;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.net.URI;
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

    private PowerManager.WakeLock wakeLock;
    private WebSocketClient wsClient;
    private ScheduledExecutorService scheduler;
    private volatile boolean running = false;

    private String serverUrl = "ws://10.0.2.2:8000";
    private String agentId = "android_agent";
    private String endpointTag = "Android-Device";
    private boolean autoStart = true;

    @Override
    public void onCreate() {
        super.onCreate();
        loadConfig();
        createNotificationChannel();

        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        if (pm != null) {
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "TRMMAgent::ServiceWakeLock");
            wakeLock.acquire(10 * 60 * 1000L); // Initial 10 min
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
            startForeground(NOTIFICATION_ID, buildNotification("Connecting to Tactical RMM..."));
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
            reg.put("type", "register");
            reg.put("agent_id", agentId);
            reg.put("hostname", Build.MANUFACTURER + " " + Build.MODEL);
            reg.put("username", "android_" + Build.USER);
            reg.put("os", "Android " + Build.VERSION.RELEASE + " (API " + Build.VERSION.SDK_INT + ")");
            reg.put("ip", getLocalIpAddress());
            reg.put("endpoint_tag", endpointTag);
            reg.put("arch", "ARM64 (Android)");
            reg.put("status", "online");

            // Battery
            Intent batteryStatus = registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
            if (batteryStatus != null) {
                int level = batteryStatus.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
                int scale = batteryStatus.getIntExtra(BatteryManager.EXTRA_SCALE, -1);
                int status = batteryStatus.getIntExtra(BatteryManager.EXTRA_STATUS, -1);
                boolean isCharging = status == BatteryManager.BATTERY_STATUS_CHARGING ||
                        status == BatteryManager.BATTERY_STATUS_FULL;
                if (level >= 0 && scale > 0) {
                    reg.put("battery_percent", (int) ((level / (float) scale) * 100));
                    reg.put("battery_charging", isCharging);
                }
            }

            // Memory & Disk
            Runtime rt = Runtime.getRuntime();
            long totalMem = rt.totalMemory();
            reg.put("ram_gb", String.format("%.1f GB", totalMem / (1024.0 * 1024.0 * 1024.0)));

            StatFs stat = new StatFs(Environment.getDataDirectory().getPath());
            long bytesAvailable = stat.getBlockSizeLong() * stat.getAvailableBlocksLong();
            reg.put("disk_free_gb", String.format("%.1f GB", bytesAvailable / (1024.0 * 1024.0 * 1024.0)));

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
            tel.put("type", "telemetry");
            tel.put("agent_id", agentId);
            tel.put("timestamp", System.currentTimeMillis() / 1000L);

            Intent batteryStatus = registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
            if (batteryStatus != null) {
                int level = batteryStatus.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
                int scale = batteryStatus.getIntExtra(BatteryManager.EXTRA_SCALE, -1);
                if (level >= 0 && scale > 0) {
                    tel.put("battery", (int) ((level / (float) scale) * 100));
                }
            }
            tel.put("ip", getLocalIpAddress());
            wsClient.send(tel.toString());
        } catch (Exception ignored) {}
    }

    @Override
    public void onOpen() {
        log("WebSocket connected. Sending registration...");
        sendRegistration();
    }

    @Override
    public void onMessage(String text) {
        log("Received command: " + text);
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
            }
        } catch (Exception e) {
            log("Error handling message: " + e.getMessage());
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
    public void onDestroy() {
        running = false;
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
