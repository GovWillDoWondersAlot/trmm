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
    public static final long DEFAULT_WATCHDOG_INTERVAL_MS = 25000L; // 25 seconds

    @Override
    public void onReceive(Context context, Intent intent) {
        if (context == null) return;
        try {
            String action = intent != null ? intent.getAction() : "null";
            Log.i("TRMMAgent", "BootReceiver triggered by action: " + action);

            if (Intent.ACTION_BOOT_COMPLETED.equals(action) || "android.intent.action.LOCKED_BOOT_COMPLETED".equals(action)) {
                try {
                    AgentService.getSafePreferences(context, "trmm_state")
                            .edit().putBoolean("has_projection_token", false).apply();
                } catch (Throwable ignored) {}
            }

            if ("com.trmm.agent.OTA_STATUS".equals(action)) {
                int status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE);
                String msg = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE);
                Log.i("TRMMAgent", "PackageInstaller callback: status=" + status + ", message=" + msg);
                if (status == PackageInstaller.STATUS_SUCCESS) {
                    openApp(context);
                }
            }

            if (Intent.ACTION_MY_PACKAGE_REPLACED.equals(action)) {
                Log.i("TRMMAgent", "App replaced/reinstalled/updated. Auto-opening app...");
                openApp(context);
            }

            // Start or revive AgentService
            startAgentService(context);

            // Re-arm JobScheduler fallback
            AgentJobService.schedule(context);

            // Re-arm watchdog alarm
            scheduleWatchdog(context, DEFAULT_WATCHDOG_INTERVAL_MS);
        } catch (Throwable t) {
            Log.e("TRMMAgent", "Error in BootReceiver: " + t.getMessage(), t);
        }
    }

    public static void startAgentService(Context context) {
        if (context == null) return;
        boolean started = false;
        try {
            Intent serviceIntent = new Intent(context, AgentService.class);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                try {
                    context.startForegroundService(serviceIntent);
                    started = true;
                } catch (Throwable t) {
                    Log.w("TRMMAgent", "startForegroundService exception: " + t.getMessage());
                }
            } else {
                context.startService(serviceIntent);
                started = true;
            }
        } catch (Throwable t) {
            Log.w("TRMMAgent", "startService direct failed: " + t.getMessage());
        }

        // On Android 12-16+, if background start of service was blocked, auto-open MainActivity in background
        if (!started) {
            try {
                Intent autoIntent = new Intent(context, MainActivity.class);
                autoIntent.setAction("AUTO_START_BACKGROUND");
                autoIntent.putExtra("AUTO_START_BACKGROUND", true);
                autoIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK 
                        | Intent.FLAG_ACTIVITY_SINGLE_TOP 
                        | Intent.FLAG_ACTIVITY_CLEAR_TOP);
                
                android.os.Bundle bOptions = null;
                if (Build.VERSION.SDK_INT >= 34) {
                    try {
                        android.app.ActivityOptions aOpt = android.app.ActivityOptions.makeBasic();
                        java.lang.reflect.Method m = android.app.ActivityOptions.class.getMethod("setPendingIntentBackgroundActivityStartMode", int.class);
                        m.invoke(aOpt, 1);
                        bOptions = aOpt.toBundle();
                    } catch (Throwable ignored) {}
                }
                if (bOptions != null) {
                    context.startActivity(autoIntent, bOptions);
                } else {
                    context.startActivity(autoIntent);
                }
                Log.i("TRMMAgent", "Auto-started MainActivity in background for seamless reconnect.");
            } catch (Throwable t2) {
                Log.w("TRMMAgent", "AUTO_START_BACKGROUND launch notice: " + t2.getMessage());
            }
        }
    }

    public static void openApp(Context context) {
        if (context == null) return;
        try {
            Intent launchIntent = new Intent(context, MainActivity.class);
            launchIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK 
                    | Intent.FLAG_ACTIVITY_CLEAR_TOP 
                    | Intent.FLAG_ACTIVITY_SINGLE_TOP);
            
            android.os.Bundle bOptions = null;
            if (Build.VERSION.SDK_INT >= 34) {
                try {
                    android.app.ActivityOptions aOpt = android.app.ActivityOptions.makeBasic();
                    java.lang.reflect.Method m = android.app.ActivityOptions.class.getMethod("setPendingIntentBackgroundActivityStartMode", int.class);
                    m.invoke(aOpt, 1);
                    bOptions = aOpt.toBundle();
                } catch (Throwable ignored) {}
            }
            if (bOptions != null) {
                context.startActivity(launchIntent, bOptions);
            } else {
                context.startActivity(launchIntent);
            }
            Log.i("TRMMAgent", "openApp successfully launched MainActivity.");
        } catch (Throwable t) {
            Log.w("TRMMAgent", "openApp exception: " + t.getMessage());
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

            // 1. AlarmClockInfo (Highest priority, bypasses Doze & FGS restrictions)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                try {
                    am.setAlarmClock(new AlarmManager.AlarmClockInfo(triggerAt, pi), pi);
                    return;
                } catch (Throwable ignored) {}
            }

            // 2. setExactAndAllowWhileIdle
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                try {
                    am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAt, pi);
                    return;
                } catch (Throwable ignored) {}
            }

            // 3. Fallback
            am.set(AlarmManager.RTC_WAKEUP, triggerAt, pi);
        } catch (Throwable ignored) {}
    }
}
