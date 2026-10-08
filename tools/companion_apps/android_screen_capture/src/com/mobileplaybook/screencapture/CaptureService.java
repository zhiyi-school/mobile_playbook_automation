// Records the screen through MediaProjection and, on request over adb, saves the latest frame with how much of it
// is blank; a window with FLAG_SECURE comes out black in the recording.

package com.mobileplaybook.screencapture;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.ServiceInfo;
import android.graphics.Bitmap;
import android.graphics.PixelFormat;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.media.Image;
import android.media.ImageReader;
import android.media.projection.MediaProjection;
import android.media.projection.MediaProjectionManager;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.util.DisplayMetrics;
import android.view.WindowManager;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.util.HashSet;
import java.util.Set;

public class CaptureService extends Service {
    static final String EXTRA_RESULT_CODE = "result_code";
    static final String EXTRA_RESULT_DATA = "result_data";
    static final String ACTION_SNAP = "com.mobileplaybook.screencapture.SNAP";
    static final String ACTION_STOP = "com.mobileplaybook.screencapture.STOP";
    static final String EXTRA_LABEL = "label";

    private static final String CHANNEL_ID = "capture";
    private static final int NOTIFICATION_ID = 1;
    // Record at half resolution; enough to tell a blank frame from app content, and cheaper to copy.
    private static final int SCALE = 2;
    // A pixel counts as dark when no channel exceeds this; FLAG_SECURE content is recorded as pure black.
    private static final int DARK_LIMIT = 12;
    private static final int SAMPLE_STEP = 4;
    private static final int MAX_DISTINCT_COLORS = 4096;

    private HandlerThread thread;
    private Handler handler;
    private MediaProjection projection;
    private VirtualDisplay display;
    private ImageReader reader;
    private Image latest;
    private boolean receiverRegistered;
    private boolean released;

    private final BroadcastReceiver commands = new BroadcastReceiver() {
        // Run snap and stop requests from adb on the capture thread, in order with frame updates.
        @Override
        public void onReceive(Context context, Intent intent) {
            if (ACTION_SNAP.equals(intent.getAction())) {
                String label = intent.getStringExtra(EXTRA_LABEL);
                handler.post(() -> snap(label == null ? "frame" : label));
            } else if (ACTION_STOP.equals(intent.getAction())) {
                handler.post(() -> release("stopped on request"));
            }
        }
    };

    // The service is started, never bound.
    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    // Go foreground as a media-projection service, which Android requires before capture starts, then start recording.
    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (projection != null || intent == null) {
            return START_NOT_STICKY;
        }
        startAsForeground();
        try {
            startCapture(intent.getIntExtra(EXTRA_RESULT_CODE, 0), intent.getParcelableExtra(EXTRA_RESULT_DATA));
            CaptureOutput.writeStatus(this, "recording", null);
        } catch (RuntimeException e) {
            CaptureOutput.writeStatus(this, "error", e.toString());
            release(null);
        }
        return START_NOT_STICKY;
    }

    // Release the recording if the system destroys the service before a stop request.
    @Override
    public void onDestroy() {
        release("service destroyed");
        super.onDestroy();
    }

    // Show the ongoing-recording notification Android requires for a media-projection foreground service.
    private void startAsForeground() {
        NotificationManager notifications = getSystemService(NotificationManager.class);
        notifications.createNotificationChannel(new NotificationChannel(CHANNEL_ID, "Screen capture", NotificationManager.IMPORTANCE_LOW));
        Notification notification = new Notification.Builder(this, CHANNEL_ID)
                .setSmallIcon(android.R.drawable.ic_menu_camera)
                .setContentTitle("Recording the screen")
                .setContentText("Playbook screen capture test")
                .build();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION);
        } else {
            startForeground(NOTIFICATION_ID, notification);
        }
    }

    // Mirror the screen into an ImageReader and start listening for snap and stop requests.
    private void startCapture(int resultCode, Intent resultData) {
        if (resultData == null) {
            throw new IllegalStateException("no screen capture consent was passed to the service");
        }
        thread = new HandlerThread("capture");
        thread.start();
        handler = new Handler(thread.getLooper());
        MediaProjectionManager manager = getSystemService(MediaProjectionManager.class);
        projection = manager.getMediaProjection(resultCode, resultData);
        if (projection == null) {
            throw new IllegalStateException("the system returned no media projection");
        }
        // Android 14 requires a callback before the virtual display is created.
        projection.registerCallback(new MediaProjection.Callback() {
            // Record that the system or the user ended the recording, for example from the notification.
            @Override
            public void onStop() {
                handler.post(() -> release("recording stopped by the system"));
            }
        }, handler);

        DisplayMetrics metrics = new DisplayMetrics();
        getSystemService(WindowManager.class).getDefaultDisplay().getRealMetrics(metrics);
        int width = metrics.widthPixels / SCALE;
        int height = metrics.heightPixels / SCALE;
        // Three buffers: one held as the latest frame, and room for acquireLatestImage to take the next.
        reader = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 3);
        reader.setOnImageAvailableListener(this::keepLatest, handler);
        display = projection.createVirtualDisplay("playbook-capture", width, height, Math.max(1, metrics.densityDpi / SCALE),
                DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, reader.getSurface(), null, handler);

        IntentFilter filter = new IntentFilter();
        filter.addAction(ACTION_SNAP);
        filter.addAction(ACTION_STOP);
        // Exported, because the requests come from adb's shell user.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(commands, filter, Context.RECEIVER_EXPORTED);
        } else {
            registerReceiver(commands, filter);
        }
        receiverRegistered = true;
    }

    // Hold on to the newest frame; the virtual display only sends frames when the screen changes, so a still screen
    // must be answered from the last frame received.
    private void keepLatest(ImageReader source) {
        Image next = source.acquireLatestImage();
        if (next == null) {
            return;
        }
        if (latest != null) {
            latest.close();
        }
        latest = next;
    }

    // Save the latest frame as <label>.png and its blankness measurements as <label>.json.
    private void snap(String label) {
        File dir = CaptureOutput.dir(this);
        JSONObject result = new JSONObject();
        try {
            result.put("label", label);
            result.put("time_ms", System.currentTimeMillis());
            if (latest == null) {
                result.put("error", "no frame has been recorded yet");
            } else {
                Bitmap bitmap = toBitmap(latest);
                measure(bitmap, result);
                File png = new File(dir, label + ".png");
                try (FileOutputStream out = new FileOutputStream(png)) {
                    bitmap.compress(Bitmap.CompressFormat.PNG, 100, out);
                }
                result.put("png", png.getName());
            }
        } catch (JSONException | IOException | RuntimeException e) {
            try {
                result.put("error", e.toString());
            } catch (JSONException ignored) {
                // The label and time are already set; the error text is best-effort.
            }
        }
        CaptureOutput.writeJson(new File(dir, label + ".json"), result);
    }

    // Copy an RGBA frame into a bitmap, dropping the row padding some devices add.
    private static Bitmap toBitmap(Image image) {
        Image.Plane plane = image.getPlanes()[0];
        ByteBuffer buffer = plane.getBuffer();
        buffer.rewind();
        int paddedWidth = plane.getRowStride() / plane.getPixelStride();
        Bitmap padded = Bitmap.createBitmap(paddedWidth, image.getHeight(), Bitmap.Config.ARGB_8888);
        padded.copyPixelsFromBuffer(buffer);
        return Bitmap.createBitmap(padded, 0, 0, image.getWidth(), image.getHeight());
    }

    // Measure the share of dark pixels and the number of distinct colours in the middle 80% of the frame, which
    // leaves out the status and navigation bars that stay visible over a secure window.
    private static void measure(Bitmap bitmap, JSONObject result) throws JSONException {
        int width = bitmap.getWidth();
        int height = bitmap.getHeight();
        int top = height / 10;
        int bottom = height - height / 10;
        int sampled = 0;
        int dark = 0;
        Set<Integer> colors = new HashSet<>();
        for (int y = top; y < bottom; y += SAMPLE_STEP) {
            for (int x = 0; x < width; x += SAMPLE_STEP) {
                int pixel = bitmap.getPixel(x, y);
                int red = (pixel >> 16) & 0xff;
                int green = (pixel >> 8) & 0xff;
                int blue = pixel & 0xff;
                sampled++;
                if (Math.max(red, Math.max(green, blue)) <= DARK_LIMIT) {
                    dark++;
                }
                if (colors.size() < MAX_DISTINCT_COLORS) {
                    colors.add(pixel);
                }
            }
        }
        result.put("width", width);
        result.put("height", height);
        result.put("sampled_pixels", sampled);
        result.put("dark_ratio", sampled == 0 ? 0 : (double) dark / sampled);
        result.put("distinct_colors", colors.size());
    }

    // Stop recording and the service once, whichever of a stop request, the system or onDestroy comes first.
    private void release(String reason) {
        if (released) {
            return;
        }
        released = true;
        if (receiverRegistered) {
            unregisterReceiver(commands);
        }
        if (display != null) {
            display.release();
        }
        if (latest != null) {
            latest.close();
            latest = null;
        }
        if (reader != null) {
            reader.close();
        }
        if (projection != null) {
            projection.stop();
        }
        if (reason != null) {
            CaptureOutput.writeStatus(this, "stopped", reason);
        }
        stopForeground(STOP_FOREGROUND_REMOVE);
        stopSelf();
        if (thread != null) {
            thread.quitSafely();
        }
    }
}
