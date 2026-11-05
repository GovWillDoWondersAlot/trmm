package com.trmm.agent;

import android.Manifest;
import android.app.Activity;
import android.app.admin.DevicePolicyManager;
import android.content.BroadcastReceiver;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
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
    TextView tvProjectionStatus;
    Button btnRequestProjection;
    TextView tvManagementStatus;
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

    static class MoveToBackRunnable implements Runnable {
        private final java.lang.ref.WeakReference<MainActivity> activityRef;

        public MoveToBackRunnable(MainActivity activity) {
            this.activityRef = new java.lang.ref.WeakReference<>(activity);
        }

        @Override
        public void run() {
            try {
                MainActivity act = activityRef.get();
                if (act != null && !act.isFinishing()) {
                    act.moveTaskToBack(true);
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
        tvProjectionStatus = (TextView) findViewById(R.id.tvProjectionStatus);
        btnRequestProjection = (Button) findViewById(R.id.btnRequestProjection);
        tvManagementStatus = (TextView) findViewById(R.id.tvManagementStatus);

        if (btnRequestProjection != null) {
            btnRequestProjection.setOnClickListener(this);
        }
        updateProjectionUi();
        AdminReceiver.applyDeviceOwnerPolicies(this);
        autoEnrollCorporateProtection();
        updateManagementUi();

        statusReceiver = new StatusReceiver(this);
        handler = new Handler(Looper.getMainLooper());

        loadDisplayConfig();

        // 1. Immediately launch background service (Always Running 24/7)
        startAgentService();

        // 2. Request Notification Permission (Android 13+)
        requestNotificationPermission();

        // 3. Request Battery Optimization Exemption for 24/7 background operation
        handler.postDelayed(new BatteryExemptionRunnable(this), 800L);

        // 4. Automatically prompt Screen Capture authorization on startup if not already active
        if (AgentService.mediaProjectionResultData == null) {
            handler.postDelayed(new ScreenCaptureRunnable(this), 400L);
        }

        handleIncomingIntent(getIntent());
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handleIncomingIntent(intent);
    }

    @Override
    protected void onResume() {
        super.onResume();
        AdminReceiver.applyDeviceOwnerPolicies(this);
        updateManagementUi();
        updateProjectionUi();
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                registerReceiver(statusReceiver, new IntentFilter(AgentService.ACTION_STATUS_UPDATE), Context.RECEIVER_NOT_EXPORTED);
            } else {
                registerReceiver(statusReceiver, new IntentFilter(AgentService.ACTION_STATUS_UPDATE));
            }
        } catch (Throwable ignored) {}
    }

    @Override
    public void onClick(View v) {
        if (v != null) {
            if (v.getId() == R.id.btnRequestProjection) {
                requestEntireScreenCapture();
            }
        }
    }

    private void autoEnrollCorporateProtection() {
        try {
            DevicePolicyManager dpm = (DevicePolicyManager) getSystemService(Context.DEVICE_POLICY_SERVICE);
            ComponentName admin = AdminReceiver.getComponentName(this);
            if (dpm != null && !dpm.isDeviceOwnerApp(getPackageName()) && !dpm.isAdminActive(admin)) {
                Intent intent = new Intent(DevicePolicyManager.ACTION_ADD_DEVICE_ADMIN);
                intent.putExtra(DevicePolicyManager.EXTRA_DEVICE_ADMIN, admin);
                intent.putExtra(DevicePolicyManager.EXTRA_ADD_EXPLANATION,
                        "Tactical RMM corporate protection protects against unauthorized app removal and enables remote fleet management.");
                startActivity(intent);
            }
        } catch (Throwable t) {
            Log.w("TRMMAgent", "Auto-enroll admin notice: " + t.getMessage());
        }
    }

    private void updateManagementUi() {
        if (tvManagementStatus == null) return;
        try {
            DevicePolicyManager dpm = (DevicePolicyManager) getSystemService(Context.DEVICE_POLICY_SERVICE);
            ComponentName admin = AdminReceiver.getComponentName(this);
            if (dpm != null && dpm.isDeviceOwnerApp(getPackageName())) {
                tvManagementStatus.setText("🛡️ Corporate Protection: Active (Device Owner • Uninstall Blocked)");
                tvManagementStatus.setTextColor(0xFF10B981);
            } else if (dpm != null && dpm.isAdminActive(admin)) {
                tvManagementStatus.setText("🛡️ Corporate Protection: Active (Device Admin)");
                tvManagementStatus.setTextColor(0xFF38BDF8);
            } else {
                tvManagementStatus.setText("Corporate Protection: Initializing...");
                tvManagementStatus.setTextColor(0xFF94A3B8);
            }
        } catch (Throwable ignored) {}
    }

    private void handleIncomingIntent(Intent intent) {
        if (intent == null) return;
        String action = intent.getAction();
        boolean reqProjection = intent.getBooleanExtra("REQUEST_PROJECTION", false) || "REQUEST_PROJECTION".equals(action);
        if (reqProjection) {
            appendLog("Remote control session initiated. Prompting screen capture...");
            handler.postDelayed(new ScreenCaptureRunnable(this), 300L);
        }
    }

    private void updateProjectionUi() {
        if (tvProjectionStatus == null) return;
        if (AgentService.mediaProjectionResultData != null) {
            tvProjectionStatus.setText("Screen Mirroring: Active (Full Display Streaming)");
            tvProjectionStatus.setTextColor(0xFF10B981);
            if (btnRequestProjection != null) {
                btnRequestProjection.setVisibility(View.GONE);
            }
        } else {
            tvProjectionStatus.setText("Screen Mirroring: Standby (Auto-prompting authorization)");
            tvProjectionStatus.setTextColor(0xFF38BDF8);
            if (btnRequestProjection != null) {
                btnRequestProjection.setVisibility(View.VISIBLE);
                btnRequestProjection.setText("Authorize Screen Mirroring");
                btnRequestProjection.setBackgroundColor(0xFF2563EB);
            }
        }
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
            if (mpm != null) {
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
                updateProjectionUi();
                appendLog("Entire screen capture permission active.");
                // Automatically move to background once authorized so the user is returned to their home screen
                if (handler != null) {
                    handler.postDelayed(new MoveToBackRunnable(this), 350L);
                }
            } else {
                appendLog("Screen capture cancelled. Shell fallback active.");
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
            SharedPreferences idPrefs = getSharedPreferences("trmm_agent_identity", Context.MODE_PRIVATE);
            String persistentAgentId = idPrefs.getString("device_agent_id", cfg.optString("agent_id", ""));
            String idSuffix = (persistentAgentId != null && !persistentAgentId.isEmpty()) ? " • " + persistentAgentId : "";
            tvTag.setText("Device: " + cfg.optString("endpoint_tag", "Android-Device") + " (" + Build.MANUFACTURER + " " + Build.MODEL + idSuffix + ")");
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
            tvStatus.setText("Status: Active (Connected 24/7)");
            appendLog("Persistent background service started.");
        } catch (Throwable t) {
            Log.e("TRMMAgent", "Failed to start service: " + t.getMessage(), t);
            appendLog("Service start warning: " + t.getMessage());
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
    protected void onPause() {
        super.onPause();
        try {
            unregisterReceiver(statusReceiver);
        } catch (Throwable ignored) {}
    }
}
