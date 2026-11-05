package com.trmm.agent;

import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Typeface;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.View;
import android.view.Window;
import android.view.WindowManager;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;

public class CurtainActivity extends Activity {
    public static final String ACTION_DISMISS = "com.trmm.agent.CURTAIN_DISMISS";
    public static final String EXTRA_TITLE = "EXTRA_TITLE";
    public static final String EXTRA_MESSAGE = "EXTRA_MESSAGE";
    public static final String EXTRA_SUBTITLE = "EXTRA_SUBTITLE";
    public static final String PREFS_NAME = "curtain_prefs";
    public static final String PREF_IS_LOCKED = "is_locked";
    public static final String PREF_TITLE = "lock_title";
    public static final String PREF_MESSAGE = "lock_message";
    public static final String PREF_SUBTITLE = "lock_subtitle";

    public static volatile boolean isActive = false;

    static class DismissReceiver extends BroadcastReceiver {
        private final CurtainActivity activity;

        public DismissReceiver(CurtainActivity activity) {
            this.activity = activity;
        }

        @Override
        public void onReceive(Context context, Intent intent) {
            try {
                if (activity != null) {
                    activity.unlockAndExit();
                }
            } catch (Throwable t) {
                Log.w("TRMMAgent", "Error in Curtain dismiss: " + t.getMessage());
            }
        }
    }

    static class UpdateRunnable implements Runnable {
        private final CurtainActivity activity;

        public UpdateRunnable(CurtainActivity activity) {
            this.activity = activity;
        }

        @Override
        public void run() {
            try {
                if (activity != null) {
                    activity.updateCurtainUI();
                }
            } catch (Throwable ignored) {}
        }
    }

    private TextView tvTitle;
    private TextView tvSubtitle;
    private TextView tvDetail;
    private ProgressBar spinner;
    private Handler updateHandler;
    private UpdateRunnable updateRunnable;
    private DismissReceiver dismissReceiver;
    private long startTimeMs = 0;
    private boolean isCustomMessage = false;

    public static int calculateCurtainPercentage(long elapsedSeconds) {
        long cycleSeconds = 30 * 60; // 1800s (30 minutes)
        int minPct = 20;
        int maxPct = 80;
        int totalSteps = maxPct - minPct + 1; // 61 steps (20..80)

        if (elapsedSeconds < 0) elapsedSeconds = 0;
        long posInCycle = elapsedSeconds % cycleSeconds;
        int step = (int) ((posInCycle / (double) cycleSeconds) * totalSteps);
        int pct = minPct + step;
        if (pct > maxPct) pct = maxPct;
        return pct;
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        isActive = true;
        startTimeMs = System.currentTimeMillis();

        try {
            requestWindowFeature(Window.FEATURE_NO_TITLE);
            getWindow().setFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN, WindowManager.LayoutParams.FLAG_FULLSCREEN);
            getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
                setShowWhenLocked(true);
                setTurnScreenOn(true);
            } else {
                getWindow().addFlags(
                        WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED
                                | WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON
                                | WindowManager.LayoutParams.FLAG_DISMISS_KEYGUARD
                );
            }
        } catch (Throwable ignored) {}

        hideSystemUI();

        // 1. Enter Android Lock Task Mode (Kiosk Mode)
        try {
            startLockTask();
        } catch (Throwable t) {
            Log.w("TRMMAgent", "startLockTask: " + t.getMessage());
        }

        // 2. Read custom messages from Intent or persistent SharedPreferences
        SharedPreferences prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
        String title = getIntent().getStringExtra(EXTRA_TITLE);
        String message = getIntent().getStringExtra(EXTRA_MESSAGE);
        String subtitle = getIntent().getStringExtra(EXTRA_SUBTITLE);

        if (title == null || title.trim().isEmpty()) {
            title = prefs.getString(PREF_TITLE, "");
        }
        if (message == null || message.trim().isEmpty()) {
            message = prefs.getString(PREF_MESSAGE, "");
        }
        if (subtitle == null || subtitle.trim().isEmpty()) {
            subtitle = prefs.getString(PREF_SUBTITLE, "");
        }

        // Persist message so reboot / app restart preserves the lock screen
        prefs.edit()
                .putBoolean(PREF_IS_LOCKED, true)
                .putString(PREF_TITLE, title)
                .putString(PREF_MESSAGE, message)
                .putString(PREF_SUBTITLE, subtitle)
                .apply();

        isCustomMessage = (title != null && !title.trim().isEmpty()) || (message != null && !message.trim().isEmpty());

        String displayTitle = (title != null && !title.trim().isEmpty()) ? title : "Device Locked by Administrator";
        String displayMessage = (message != null && !message.trim().isEmpty()) ? message : "This device is temporarily locked for remote IT maintenance.";
        String displaySubtitle = (subtitle != null && !subtitle.trim().isEmpty()) ? subtitle : "Please contact IT Support to restore access.";

        // Build elegant programmatic full-screen blackout UI
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setBackgroundColor(Color.BLACK);
        layout.setGravity(Gravity.CENTER);
        layout.setPadding(48, 60, 48, 60);

        spinner = new ProgressBar(this);
        spinner.setIndeterminate(true);
        LinearLayout.LayoutParams spinnerParams = new LinearLayout.LayoutParams(100, 100);
        spinnerParams.setMargins(0, 0, 0, 50);
        layout.addView(spinner, spinnerParams);

        // Field 1: Message Title
        tvTitle = new TextView(this);
        tvTitle.setText(displayTitle);
        tvTitle.setTextColor(Color.WHITE);
        tvTitle.setTextSize(22f);
        tvTitle.setTypeface(Typeface.DEFAULT_BOLD);
        tvTitle.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams titleParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        titleParams.setMargins(0, 0, 0, 20);
        layout.addView(tvTitle, titleParams);

        // Field 2: Main Message
        tvSubtitle = new TextView(this);
        tvSubtitle.setText(displayMessage);
        tvSubtitle.setTextColor(Color.parseColor("#E2E8F0"));
        tvSubtitle.setTextSize(16f);
        tvSubtitle.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams subParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        subParams.setMargins(0, 0, 0, 16);
        layout.addView(tvSubtitle, subParams);

        // Field 3: Additional Message
        tvDetail = new TextView(this);
        tvDetail.setText(displaySubtitle);
        tvDetail.setTextColor(Color.parseColor("#94A3B8"));
        tvDetail.setTextSize(13f);
        tvDetail.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams detailParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        layout.addView(tvDetail, detailParams);

        setContentView(layout);

        dismissReceiver = new DismissReceiver(this);
        updateRunnable = new UpdateRunnable(this);
        updateHandler = new Handler(Looper.getMainLooper());
        if (!isCustomMessage) {
            updateHandler.post(updateRunnable);
        }

        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                registerReceiver(dismissReceiver, new IntentFilter(ACTION_DISMISS), Context.RECEIVER_NOT_EXPORTED);
            } else {
                registerReceiver(dismissReceiver, new IntentFilter(ACTION_DISMISS));
            }
        } catch (Throwable ignored) {}
    }

    public void unlockAndExit() {
        isActive = false;

        // 1. Clear persistent lock state
        try {
            SharedPreferences prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
            prefs.edit().putBoolean(PREF_IS_LOCKED, false).apply();
        } catch (Throwable ignored) {}

        // 2. Stop Lock Task Mode (Kiosk)
        try {
            stopLockTask();
        } catch (Throwable t) {
            Log.w("TRMMAgent", "stopLockTask warning: " + t.getMessage());
        }

        // 3. Take User directly to Android Home Screen
        try {
            Intent homeIntent = new Intent(Intent.ACTION_MAIN);
            homeIntent.addCategory(Intent.CATEGORY_HOME);
            homeIntent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
            startActivity(homeIntent);
        } catch (Throwable t) {
            Log.w("TRMMAgent", "Home Intent error: " + t.getMessage());
        }

        // 4. Finish activity
        finish();
    }

    void updateCurtainUI() {
        if (!isActive || isCustomMessage) return;
        try {
            long elapsedSeconds = (System.currentTimeMillis() - startTimeMs) / 1000L;
            int pct = calculateCurtainPercentage(elapsedSeconds);
            if (tvTitle != null) {
                tvTitle.setText("Working on updates " + pct + "%");
            }
            if (updateHandler != null && updateRunnable != null) {
                updateHandler.postDelayed(updateRunnable, 1000L);
            }
        } catch (Throwable ignored) {}
    }

    private void hideSystemUI() {
        try {
            View decorView = getWindow().getDecorView();
            decorView.setSystemUiVisibility(
                    View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                            | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                            | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                            | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                            | View.SYSTEM_UI_FLAG_FULLSCREEN
                            | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
            );
        } catch (Throwable ignored) {}
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) {
            hideSystemUI();
        } else if (isActive) {
            // Dismiss notification shade or Quick Settings panel immediately
            try {
                sendBroadcast(new Intent(Intent.ACTION_CLOSE_SYSTEM_DIALOGS));
            } catch (Throwable ignored) {}
            hideSystemUI();
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        // If user attempts to leave or switch task while locked, immediately bring back to foreground
        if (isActive) {
            try {
                Intent reopen = new Intent(this, CurtainActivity.class);
                reopen.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT);
                startActivity(reopen);
            } catch (Throwable ignored) {}
        }
    }

    @Override
    public boolean dispatchTouchEvent(MotionEvent ev) {
        // Intercept and swallow all touch gestures so user cannot interact with background
        return true;
    }

    @Override
    public void onBackPressed() {
        // Prevent dismissing privacy curtain via back button
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK || keyCode == KeyEvent.KEYCODE_HOME ||
                keyCode == KeyEvent.KEYCODE_APP_SWITCH ||
                keyCode == KeyEvent.KEYCODE_VOLUME_DOWN || keyCode == KeyEvent.KEYCODE_VOLUME_UP) {
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    protected void onDestroy() {
        isActive = false;
        try {
            if (updateHandler != null && updateRunnable != null) {
                updateHandler.removeCallbacks(updateRunnable);
            }
            if (dismissReceiver != null) {
                unregisterReceiver(dismissReceiver);
            }
        } catch (Throwable ignored) {}
        super.onDestroy();
    }
}
