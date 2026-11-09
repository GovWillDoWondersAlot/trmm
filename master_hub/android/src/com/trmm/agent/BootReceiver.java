package com.trmm.agent;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInstaller;
import android.os.Build;
import android.util.Log;

public class BootReceiver extends BroadcastReceiver {
    public static final String ACTION_WATCHDOG_PING = "com.trmm.agent.WATCHDOG_PING";

    @Override
    public void onReceive(Context context, Intent intent) {
        if (context == null) return;
        try {
            String action = intent != null ? intent.getAction() : "null";
            Log.i("TRMMAgent", "BootReceiver triggered by action: " + action);

            if ("com.trmm.agent.OTA_STATUS".equals(action)) {
                int status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE);
                String msg = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE);
                Log.i("TRMMAgent", "PackageInstaller callback: status=" + status + ", message=" + msg);
            }

            Intent serviceIntent = new Intent(context, AgentService.class);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(serviceIntent);
            } else {
                context.startService(serviceIntent);
            }

            // Always prime / refresh watchdog alarm on every wake event
            scheduleWatchdog(context);
        } catch (Throwable t) {
            Log.e("TRMMAgent", "Error in BootReceiver: " + t.getMessage(), t);
        }
    }

    public static void scheduleWatchdog(Context context) {
        if (context == null) return;
        try {
            AlarmManager am = (AlarmManager) context.getSystemService(Context.ALARM_SERVICE);
            if (am == null) return;
            Intent watchdogIntent = new Intent(context, BootReceiver.class);
            watchdogIntent.setAction(ACTION_WATCHDOG_PING);
            int flags = PendingIntent.FLAG_UPDATE_CURRENT;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                flags |= PendingIntent.FLAG_IMMUTABLE;
            }
            PendingIntent pi = PendingIntent.getBroadcast(context, 7777, watchdogIntent, flags);
            long triggerAt = System.currentTimeMillis() + 60000L; // 60 seconds
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAt, pi);
            } else {
                am.set(AlarmManager.RTC_WAKEUP, triggerAt, pi);
            }
        } catch (Throwable ignored) {}
    }
}
