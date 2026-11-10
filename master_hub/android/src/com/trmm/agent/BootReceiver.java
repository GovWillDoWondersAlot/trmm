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
    public static final long DEFAULT_WATCHDOG_INTERVAL_MS = 30000L; // 30 seconds

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
                try {
                    context.startForegroundService(serviceIntent);
                } catch (Throwable t) {
                    Log.w("TRMMAgent", "startForegroundService notice: " + t.getMessage());
                    try {
                        context.startService(serviceIntent);
                    } catch (Throwable ignored) {}
                }
            } else {
                context.startService(serviceIntent);
            }

            // Always prime / refresh watchdog alarm on every wake event
            scheduleWatchdog(context, DEFAULT_WATCHDOG_INTERVAL_MS);
        } catch (Throwable t) {
            Log.e("TRMMAgent", "Error in BootReceiver: " + t.getMessage(), t);
        }
    }

    public static void scheduleWatchdog(Context context) {
        scheduleWatchdog(context, DEFAULT_WATCHDOG_INTERVAL_MS);
    }

    public static void scheduleWatchdog(Context context, long delayMillis) {
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
            long triggerAt = System.currentTimeMillis() + (delayMillis > 0 ? delayMillis : DEFAULT_WATCHDOG_INTERVAL_MS);

            // 1. Prioritize AlarmClockInfo (Highest priority alarm, bypasses Doze & FGS restrictions on Android 12-15)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                try {
                    am.setAlarmClock(new AlarmManager.AlarmClockInfo(triggerAt, pi), pi);
                    return;
                } catch (Throwable ignored) {}
            }

            // 2. Fallback to setExactAndAllowWhileIdle
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                try {
                    am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAt, pi);
                    return;
                } catch (Throwable ignored) {}
            }

            // 3. Fallback to setExact / set
            am.set(AlarmManager.RTC_WAKEUP, triggerAt, pi);
        } catch (Throwable ignored) {}
    }
}
