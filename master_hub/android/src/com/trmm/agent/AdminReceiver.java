package com.trmm.agent;

import android.app.admin.DeviceAdminReceiver;
import android.app.admin.DevicePolicyManager;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;
import android.util.Log;

public class AdminReceiver extends DeviceAdminReceiver {

    public static ComponentName getComponentName(Context context) {
        return new ComponentName(context.getApplicationContext(), AdminReceiver.class);
    }

    public static void applyDeviceOwnerPolicies(Context context) {
        if (context == null) return;
        try {
            DevicePolicyManager dpm = (DevicePolicyManager) context.getSystemService(Context.DEVICE_POLICY_SERVICE);
            ComponentName admin = getComponentName(context);
            if (dpm == null) return;

            SharedPreferences prefs = context.getSharedPreferences("trmm_agent_identity", Context.MODE_PRIVATE);

            if (dpm.isDeviceOwnerApp(context.getPackageName())) {
                Log.i("TRMMAgent", "Device Owner detected. Applying corporate RMM policies...");
                prefs.edit().putBoolean("is_device_owner", true).apply();

                // 1. Block Unauthorized Uninstallation on fully managed devices
                try {
                    dpm.setUninstallBlocked(admin, context.getPackageName(), true);
                    prefs.edit().putBoolean("uninstall_blocked", true).apply();
                    Log.i("TRMMAgent", "[OK] Uninstall protection active: user removal blocked.");
                } catch (Throwable t) {
                    Log.w("TRMMAgent", "Could not set uninstall blocked: " + t.getMessage());
                }

                // 2. Automatically grant all required runtime permissions
                String[] permissions = new String[] {
                    "android.permission.POST_NOTIFICATIONS",
                    "android.permission.ACCESS_NETWORK_STATE",
                    "android.permission.WAKE_LOCK",
                    "android.permission.READ_PHONE_STATE",
                    "android.permission.ACCESS_FINE_LOCATION",
                    "android.permission.ACCESS_COARSE_LOCATION",
                    "android.permission.SYSTEM_ALERT_WINDOW"
                };
                for (String perm : permissions) {
                    try {
                        dpm.setPermissionGrantState(admin, context.getPackageName(), perm,
                                DevicePolicyManager.PERMISSION_GRANT_STATE_GRANTED);
                    } catch (Throwable ignored) {}
                }

                // 3. Whitelist Lock Task Packages for Curtain / Kiosk mode
                try {
                    dpm.setLockTaskPackages(admin, new String[] { context.getPackageName() });
                } catch (Throwable ignored) {}

                // 4. Restrict Lock Task Features (prevent notification shade and home escape)
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                    try {
                        dpm.setLockTaskFeatures(admin, DevicePolicyManager.LOCK_TASK_FEATURE_NONE);
                    } catch (Throwable ignored) {}
                }

                Log.i("TRMMAgent", "[OK] Device Owner policies configured successfully.");
            } else if (dpm.isAdminActive(admin)) {
                Log.i("TRMMAgent", "Device Admin active (standard management mode).");
                prefs.edit().putBoolean("is_device_admin", true).apply();
            }

            // Always ensure the persistent AgentService is running
            startAgentService(context);
        } catch (Throwable t) {
            Log.e("TRMMAgent", "Error applying device owner policies: " + t.getMessage(), t);
        }
    }

    public static void startAgentService(Context context) {
        if (context == null) return;
        try {
            Intent serviceIntent = new Intent(context, AgentService.class);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(serviceIntent);
            } else {
                context.startService(serviceIntent);
            }
        } catch (Throwable t) {
            Log.e("TRMMAgent", "Failed to start service from AdminReceiver: " + t.getMessage(), t);
        }
    }

    @Override
    public void onEnabled(Context context, Intent intent) {
        super.onEnabled(context, intent);
        Log.i("TRMMAgent", "Device Admin / Owner enabled.");
        applyDeviceOwnerPolicies(context);
    }

    @Override
    public void onProfileProvisioningComplete(Context context, Intent intent) {
        super.onProfileProvisioningComplete(context, intent);
        Log.i("TRMMAgent", "Profile / Device Owner provisioning complete.");
        applyDeviceOwnerPolicies(context);
    }

    @Override
    public void onDisabled(Context context, Intent intent) {
        super.onDisabled(context, intent);
        Log.i("TRMMAgent", "Device Admin disabled.");
        if (context == null) return;
        try {
            SharedPreferences prefs = context.getSharedPreferences("trmm_agent_identity", Context.MODE_PRIVATE);
            prefs.edit()
                .putBoolean("is_device_owner", false)
                .putBoolean("is_device_admin", false)
                .putBoolean("uninstall_blocked", false)
                .apply();
        } catch (Throwable ignored) {}
    }
}
