package com.trmm.agent;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.util.Log;

public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        if (context == null) return;
        try {
            Log.i("TRMMAgent", "BootReceiver triggered by action: " + (intent != null ? intent.getAction() : "null"));
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
