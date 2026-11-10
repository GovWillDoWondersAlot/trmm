package com.trmm.agent;

import android.app.job.JobInfo;
import android.app.job.JobParameters;
import android.app.job.JobScheduler;
import android.app.job.JobService;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.util.Log;

public class AgentJobService extends JobService {
    public static final int JOB_ID = 8888;

    @Override
    public boolean onStartJob(JobParameters params) {
        Log.i("TRMMAgent", "AgentJobService executed. Ensuring 24/7 background agent presence...");
        try {
            BootReceiver.startAgentService(this);
            BootReceiver.scheduleWatchdog(this);
            schedule(this);
        } catch (Throwable t) {
            Log.e("TRMMAgent", "Error in AgentJobService: " + t.getMessage(), t);
        }
        return false; // Work completed
    }

    @Override
    public boolean onStopJob(JobParameters params) {
        return true; // Reschedule if aborted
    }

    public static void schedule(Context context) {
        if (context == null) return;
        try {
            JobScheduler js = (JobScheduler) context.getSystemService(Context.JOB_SCHEDULER_SERVICE);
            if (js == null) return;

            ComponentName cn = new ComponentName(context, AgentJobService.class);
            JobInfo.Builder builder = new JobInfo.Builder(JOB_ID, cn)
                    .setPeriodic(15 * 60 * 1000L) // Minimum 15-minute periodic interval
                    .setPersisted(true) // Survives device reboot
                    .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY);

            js.schedule(builder.build());
            Log.d("TRMMAgent", "Persistent JobScheduler watchdog scheduled.");
        } catch (Throwable t) {
            Log.w("TRMMAgent", "JobScheduler notice: " + t.getMessage());
        }
    }
}

