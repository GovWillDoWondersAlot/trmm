package com.trmm.agent;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInstaller;
import android.os.Build;
import android.util.Log;

public class BootReceiver extends BroadcastReceiver {
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
        } catch (Throwable t) {
            Log.e("TRMMAgent", "Error starting service from BootReceiver: " + t.getMessage(), t);
        }
    }
}
