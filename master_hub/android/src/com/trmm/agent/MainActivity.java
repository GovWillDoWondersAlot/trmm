package com.trmm.agent;

import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.os.Build;
import android.os.Bundle;
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
    TextView tvStatus;
    TextView tvServer;
    TextView tvTag;
    TextView tvLogs;
    ScrollView svLogs;
    Button btnToggle;
    boolean isServiceRunning = false;

    static class StatusReceiver extends BroadcastReceiver {
        private final MainActivity activity;

        public StatusReceiver(MainActivity activity) {
            this.activity = activity;
        }

        @Override
        public void onReceive(Context context, Intent intent) {
            if (activity == null) return;
            String status = intent.getStringExtra(AgentService.EXTRA_STATUS);
            if (status != null && activity.tvStatus != null) {
                activity.tvStatus.setText("Status: " + status);
            }
            String log = intent.getStringExtra(AgentService.EXTRA_LOG);
            if (log != null) {
                activity.appendLog(log);
            }
        }
    }

    static class ScrollRunnable implements Runnable {
        private final ScrollView scrollView;

        public ScrollRunnable(ScrollView scrollView) {
            this.scrollView = scrollView;
        }

        @Override
        public void run() {
            if (scrollView != null) {
                scrollView.fullScroll(View.FOCUS_DOWN);
            }
        }
    }

    private StatusReceiver statusReceiver;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        tvStatus = (TextView) findViewById(R.id.tvStatus);
        tvServer = (TextView) findViewById(R.id.tvServer);
        tvTag = (TextView) findViewById(R.id.tvTag);
        tvLogs = (TextView) findViewById(R.id.tvLogs);
        svLogs = (ScrollView) findViewById(R.id.svLogs);
        btnToggle = (Button) findViewById(R.id.btnToggle);

        statusReceiver = new StatusReceiver(this);

        loadDisplayConfig();

        btnToggle.setOnClickListener(this);

        startAgentService();
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
        } catch (Exception e) {
            tvServer.setText("Server: Standard Auto-Detect");
            tvTag.setText("Device: " + Build.MANUFACTURER + " " + Build.MODEL);
        }
    }

    private void startAgentService() {
        Intent intent = new Intent(this, AgentService.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent);
        } else {
            startService(intent);
        }
        isServiceRunning = true;
        btnToggle.setText("Stop Agent Service");
        tvStatus.setText("Status: Starting Service...");
        appendLog("Agent background service started.");
    }

    private void stopAgentService() {
        Intent intent = new Intent(this, AgentService.class);
        stopService(intent);
        isServiceRunning = false;
        btnToggle.setText("Start Agent Service");
        tvStatus.setText("Status: Stopped");
        appendLog("Agent service stopped by user.");
    }

    void appendLog(String msg) {
        if (tvLogs == null) return;
        String time = new SimpleDateFormat("HH:mm:ss", Locale.US).format(new Date());
        tvLogs.append("[" + time + "] " + msg + "\n");
        if (svLogs != null) {
            svLogs.post(new ScrollRunnable(svLogs));
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(statusReceiver, new IntentFilter(AgentService.ACTION_STATUS_UPDATE), Context.RECEIVER_NOT_EXPORTED);
        } else {
            registerReceiver(statusReceiver, new IntentFilter(AgentService.ACTION_STATUS_UPDATE));
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        try {
            unregisterReceiver(statusReceiver);
        } catch (Exception ignored) {}
    }
}
