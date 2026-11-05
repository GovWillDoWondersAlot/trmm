package com.trmm.agent;

import android.app.admin.DeviceAdminReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.util.Log;

public class AdminReceiver extends DeviceAdminReceiver {
    @Override
    public void onEnabled(Context context, Intent intent) {
        super.onEnabled(context, intent);
        Log.i("TRMMAgent", "Device Admin / Owner enabled. Starting background service...");
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
    public void onProfileProvisioningComplete(Context context, Intent intent) {
        super.onProfileProvisioningComplete(context, intent);
        Log.i("TRMMAgent", "Profile / Device Owner provisioning complete.");
        if (context == null) return;
        try {
            Intent serviceIntent = new Intent(context, AgentService.class);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(serviceIntent);
            } else {
                context.startService(serviceIntent);
            }
        } catch (Throwable t) {
            Log.e("TRMMAgent", "Failed to start service after provisioning: " + t.getMessage(), t);
        }
    }
}
