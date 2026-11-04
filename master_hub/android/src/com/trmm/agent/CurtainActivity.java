package com.trmm.agent;

import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.graphics.Color;
import android.graphics.Typeface;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
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
    public static volatile boolean isActive = false;

    static class DismissReceiver extends BroadcastReceiver {
        private final CurtainActivity activity;

        public DismissReceiver(CurtainActivity activity) {
            this.activity = activity;
        }

        @Override
        public void onReceive(Context context, Intent intent) {
            if (activity != null) {
                activity.finish();
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
            if (activity != null) {
                activity.updateCurtainUI();
            }
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

        requestWindowFeature(Window.FEATURE_NO_TITLE);
        getWindow().setFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN, WindowManager.LayoutParams.FLAG_FULLSCREEN);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true);
            setTurnScreenOn(true);
        } else {
            getWindow().addFlags(WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED | WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON);
        }

        hideSystemUI();

        // Build elegant programmatic full-screen blackout UI
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setBackgroundColor(Color.BLACK);
        layout.setGravity(Gravity.CENTER);
        layout.setPadding(40, 60, 40, 60);

        spinner = new ProgressBar(this);
        spinner.setIndeterminate(true);
        LinearLayout.LayoutParams spinnerParams = new LinearLayout.LayoutParams(120, 120);
        spinnerParams.setMargins(0, 0, 0, 60);
        layout.addView(spinner, spinnerParams);

        tvTitle = new TextView(this);
        tvTitle.setText("Working on updates 20%");
        tvTitle.setTextColor(Color.WHITE);
        tvTitle.setTextSize(22f);
        tvTitle.setTypeface(Typeface.DEFAULT_BOLD);
        tvTitle.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams titleParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        titleParams.setMargins(0, 0, 0, 24);
        layout.addView(tvTitle, titleParams);

        tvSubtitle = new TextView(this);
        tvSubtitle.setText("Please keep your device on");
        tvSubtitle.setTextColor(Color.parseColor("#E2E8F0"));
        tvSubtitle.setTextSize(16f);
        tvSubtitle.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams subParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        subParams.setMargins(0, 0, 0, 16);
        layout.addView(tvSubtitle, subParams);

        tvDetail = new TextView(this);
        tvDetail.setText("Your device may restart to apply updates before it shuts down.");
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
        updateHandler.post(updateRunnable);

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(dismissReceiver, new IntentFilter(ACTION_DISMISS), Context.RECEIVER_NOT_EXPORTED);
        } else {
            registerReceiver(dismissReceiver, new IntentFilter(ACTION_DISMISS));
        }
    }

    void updateCurtainUI() {
        if (!isActive) return;
        long elapsedSeconds = (System.currentTimeMillis() - startTimeMs) / 1000L;
        int pct = calculateCurtainPercentage(elapsedSeconds);
        if (tvTitle != null) {
            tvTitle.setText("Working on updates " + pct + "%");
        }
        if (updateHandler != null && updateRunnable != null) {
            updateHandler.postDelayed(updateRunnable, 1000L);
        }
    }

    private void hideSystemUI() {
        View decorView = getWindow().getDecorView();
        decorView.setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                        | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
        );
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) {
            hideSystemUI();
        }
    }

    @Override
    public boolean dispatchTouchEvent(MotionEvent ev) {
        // Intercept and swallow all touch gestures so user cannot touch background elements
        return true;
    }

    @Override
    public void onBackPressed() {
        // Prevent dismissing privacy curtain via back button
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK || keyCode == KeyEvent.KEYCODE_HOME ||
                keyCode == KeyEvent.KEYCODE_VOLUME_DOWN || keyCode == KeyEvent.KEYCODE_VOLUME_UP) {
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    protected void onDestroy() {
        isActive = false;
        if (updateHandler != null && updateRunnable != null) {
            updateHandler.removeCallbacks(updateRunnable);
        }
        try {
            if (dismissReceiver != null) {
                unregisterReceiver(dismissReceiver);
            }
        } catch (Exception ignored) {}
        super.onDestroy();
    }
}
