package com.trmm.agent;

import android.Manifest;
import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.media.projection.MediaProjectionConfig;
import android.media.projection.MediaProjectionManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.Settings;
import android.util.Log;
import android.view.View;
import android.widget.Button;
import android.widget.ScrollView;
import android.widget.TextView;

import org.json.JSONObject;

import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

public class MainActivity extends Activity implements View.OnClickListener {
    private static final int REQUEST_SCREEN_CAPTURE = 1002;
    private static final int REQUEST_POST_NOTIF = 1003;

    TextView tvStatus;
    TextView tvServer;
    TextView tvTag;
    TextView tvLogs;
    ScrollView svLogs;
    Button btnToggle;
    boolean isServiceRunning = false;
    private Handler handler;

    static class StatusReceiver extends BroadcastReceiver {
        private final MainActivity activity;

        public StatusReceiver(MainActivity activity) {
            this.activity = activity;
        }

        @Override
        public void onReceive(Context context, Intent intent) {
            if (activity == null) return;
            try {
                String status = intent.getStringExtra(AgentService.EXTRA_STATUS);
                if (status != null && activity.tvStatus != null) {
                    activity.tvStatus.setText("Status: " + status);
                }
                String log = intent.getStringExtra(AgentService.EXTRA_LOG);
                if (log != null) {
                    activity.appendLog(log);
                }
            } catch (Throwable t) {
                Log.w("TRMMAgent", "Error receiving status: " + t.getMessage());
            }
        }
    }

    static class GlobalExceptionHandler implements Thread.UncaughtExceptionHandler {
        @Override
        public void uncaughtException(Thread thread, Throwable throwable) {
            Log.e("TRMMAgent", "Uncaught exception in " + (thread != null ? thread.getName() : "unknown") + ": " + (throwable != null ? throwable.getMessage() : "unknown"), throwable);
        }
    }

    static class ScreenCaptureRunnable implements Runnable {
        private final MainActivity activity;

        public ScreenCaptureRunnable(MainActivity activity) {
            this.activity = activity;
        }

        @Override
        public void run() {
            try {
                if (activity != null) {
                    activity.requestEntireScreenCapture();
                }
            } catch (Throwable ignored) {}
        }
    }

    static class BatteryExemptionRunnable implements Runnable {
        private final MainActivity activity;

        public BatteryExemptionRunnable(MainActivity activity) {
            this.activity = activity;
        }

        @Override
        public void run() {
            try {
                if (activity != null) {
                    activity.requestBatteryOptimizationExemption();
                }
            } catch (Throwable ignored) {}
        }
    }

    static class ScrollRunnable implements Runnable {
        private final ScrollView scrollView;

        public ScrollRunnable(ScrollView scrollView) {
            this.scrollView = scrollView;
        }

        @Override
        public void run() {
            try {
                if (scrollView != null) {
                    scrollView.fullScroll(View.FOCUS_DOWN);
                }
            } catch (Throwable ignored) {}
        }
    }

    private StatusReceiver statusReceiver;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // Global uncaught exception protection
        Thread.setDefaultUncaughtExceptionHandler(new GlobalExceptionHandler());

        setContentView(R.layout.activity_main);

        tvStatus = (TextView) findViewById(R.id.tvStatus);
        tvServer = (TextView) findViewById(R.id.tvServer);
        tvTag = (TextView) findViewById(R.id.tvTag);
        tvLogs = (TextView) findViewById(R.id.tvLogs);
        svLogs = (ScrollView) findViewById(R.id.svLogs);
        btnToggle = (Button) findViewById(R.id.btnToggle);

        statusReceiver = new StatusReceiver(this);
        handler = new Handler(Looper.getMainLooper());

        loadDisplayConfig();
        btnToggle.setOnClickListener(this);

        // 1. Immediately launch background service
        startAgentService();

        // 2. Request Notification Permission (Android 13+)
        requestNotificationPermission();

        // 3. Automatically request Entire Screen Share after smooth delay
        handler.postDelayed(new ScreenCaptureRunnable(this), 600L);

        // 4. Request Battery Optimization Exemption
        handler.postDelayed(new BatteryExemptionRunnable(this), 1200L);
    }

    private void requestNotificationPermission() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                    requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQUEST_POST_NOTIF);
                }
            }
        } catch (Throwable t) {
            Log.w("TRMMAgent", "Notification permission error: " + t.getMessage());
        }
    }

    private void requestEntireScreenCapture() {
        try {
            MediaProjectionManager mpm = (MediaProjectionManager) getSystemService(Context.MEDIA_PROJECTION_SERVICE);
            if (mpm != null && AgentService.mediaProjectionResultData == null) {
                // In Android 14+ (API 34), createConfigForDefaultDisplay() automatically configures the prompt
                // for the ENTIRE SCREEN, eliminating the confusing "Share one app" vs "Share entire screen" dropdown!
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
                    MediaProjectionConfig config = MediaProjectionConfig.createConfigForDefaultDisplay();
                    Intent captureIntent = mpm.createScreenCaptureIntent(config);
                    startActivityForResult(captureIntent, REQUEST_SCREEN_CAPTURE);
                } else {
                    Intent captureIntent = mpm.createScreenCaptureIntent();
                    startActivityForResult(captureIntent, REQUEST_SCREEN_CAPTURE);
                }
            }
        } catch (Throwable t) {
            Log.w("TRMMAgent", "Screen capture request warning: " + t.getMessage());
        }
    }

    private void requestBatteryOptimizationExemption() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
                if (pm != null && !pm.isIgnoringBatteryOptimizations(getPackageName())) {
                    Intent intent = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS);
                    intent.setData(Uri.parse("package:" + getPackageName()));
                    startActivity(intent);
                }
            }
        } catch (Throwable t) {
            Log.w("TRMMAgent", "Battery optimization prompt warning: " + t.getMessage());
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQUEST_SCREEN_CAPTURE) {
            if (resultCode == RESULT_OK && data != null) {
                AgentService.setMediaProjectionData(resultCode, data);
                appendLog("Entire screen capture permission active.");
            } else {
                appendLog("Screen capture cancelled. Shell fallback active.");
            }
        }
    }

    @Override
    public void onClick(View v) {
        if (v == btnToggle) {
            if (isServiceRunning) {
                stopAgentService();
            } else {
                startAgentService();
            }
        }
    }

    private void loadDisplayConfig() {
        try {
            InputStream is = getAssets().open("config.json");
            byte[] buf = new byte[is.available()];
            is.read(buf);
            is.close();
            JSONObject cfg = new JSONObject(new String(buf, StandardCharsets.UTF_8));
            tvServer.setText("Server: " + cfg.optString("server_url", "ws://127.0.0.1:8000"));
            tvTag.setText("Device: " + cfg.optString("endpoint_tag", "Android-Device") + " (" + Build.MANUFACTURER + " " + Build.MODEL + ")");
        } catch (Throwable e) {
            tvServer.setText("Server: Standard Auto-Detect");
            tvTag.setText("Device: " + Build.MANUFACTURER + " " + Build.MODEL);
        }
    }

    private void startAgentService() {
        try {
            Intent intent = new Intent(this, AgentService.class);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                startForegroundService(intent);
            } else {
                startService(intent);
            }
            isServiceRunning = true;
            btnToggle.setText("Stop Agent Service");
            tvStatus.setText("Status: Connecting Service...");
            appendLog("Persistent background service started.");
        } catch (Throwable t) {
            Log.e("TRMMAgent", "Failed to start service: " + t.getMessage(), t);
            appendLog("Service start warning: " + t.getMessage());
        }
    }

    private void stopAgentService() {
        try {
            Intent intent = new Intent(this, AgentService.class);
            stopService(intent);
            isServiceRunning = false;
            btnToggle.setText("Start Agent Service");
            tvStatus.setText("Status: Stopped");
            appendLog("Agent service stopped by user.");
        } catch (Throwable t) {
            Log.e("TRMMAgent", "Error stopping service: " + t.getMessage(), t);
        }
    }

    void appendLog(String msg) {
        if (tvLogs == null) return;
        try {
            String time = new SimpleDateFormat("HH:mm:ss", Locale.US).format(new Date());
            tvLogs.append("[" + time + "] " + msg + "\n");
            if (svLogs != null) {
                svLogs.post(new ScrollRunnable(svLogs));
            }
        } catch (Throwable ignored) {}
    }

    @Override
    public void onBackPressed() {
        // Move to background instead of finishing, maintaining 24/7 background presence
        moveTaskToBack(true);
    }

    @Override
    protected void onResume() {
        super.onResume();
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                registerReceiver(statusReceiver, new IntentFilter(AgentService.ACTION_STATUS_UPDATE), Context.RECEIVER_NOT_EXPORTED);
            } else {
                registerReceiver(statusReceiver, new IntentFilter(AgentService.ACTION_STATUS_UPDATE));
            }
        } catch (Throwable ignored) {}
    }

    @Override
    protected void onPause() {
        super.onPause();
        try {
            unregisterReceiver(statusReceiver);
        } catch (Throwable ignored) {}
    }
}
