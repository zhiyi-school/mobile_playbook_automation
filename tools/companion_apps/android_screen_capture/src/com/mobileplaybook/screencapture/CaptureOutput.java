// Writes the helper's status and frames to its folder in shared storage, where the test reads them over adb.

package com.mobileplaybook.screencapture;

import android.content.Context;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;

final class CaptureOutput {
    private CaptureOutput() {
    }

    // Return the output folder, /sdcard/Android/data/<package>/files/capture, creating it if needed.
    static File dir(Context context) {
        File dir = new File(context.getExternalFilesDir(null), "capture");
        dir.mkdirs();
        return dir;
    }

    // Delete the previous session's files so the test never reads a stale status or frame.
    static void reset(Context context) {
        File[] files = dir(context).listFiles();
        if (files == null) {
            return;
        }
        for (File file : files) {
            file.delete();
        }
    }

    // Record the session's state (awaiting_consent, recording, denied, error or stopped) with an optional reason.
    static void writeStatus(Context context, String state, String detail) {
        JSONObject status = new JSONObject();
        try {
            status.put("state", state);
            status.put("detail", detail == null ? JSONObject.NULL : detail);
            status.put("time_ms", System.currentTimeMillis());
        } catch (JSONException e) {
            throw new IllegalStateException(e);
        }
        writeJson(new File(dir(context), "status.json"), status);
    }

    // Write JSON to a temporary file and rename it, so a reader polling over adb never sees a half-written file.
    static void writeJson(File file, JSONObject json) {
        File temp = new File(file.getPath() + ".tmp");
        try (FileOutputStream out = new FileOutputStream(temp)) {
            out.write(json.toString().getBytes(StandardCharsets.UTF_8));
        } catch (IOException e) {
            throw new IllegalStateException("could not write " + file, e);
        }
        if (!temp.renameTo(file)) {
            throw new IllegalStateException("could not rename " + temp + " to " + file);
        }
    }
}
