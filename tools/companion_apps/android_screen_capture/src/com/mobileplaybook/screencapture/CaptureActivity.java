// Asks for screen-capture consent through the system prompt, as a malicious app would, and hands the grant to
// CaptureService.

package com.mobileplaybook.screencapture;

import android.app.Activity;
import android.content.Intent;
import android.media.projection.MediaProjectionManager;
import android.os.Bundle;

public class CaptureActivity extends Activity {
    private static final int REQUEST_CAPTURE = 1;

    // Clear the previous session's output and show the system's screen-capture consent prompt.
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        if (savedInstanceState != null) {
            return;
        }
        CaptureOutput.reset(this);
        CaptureOutput.writeStatus(this, "awaiting_consent", null);
        MediaProjectionManager manager = (MediaProjectionManager) getSystemService(MEDIA_PROJECTION_SERVICE);
        startActivityForResult(manager.createScreenCaptureIntent(), REQUEST_CAPTURE);
    }

    // Start the capture service with the granted consent, or record the refusal, then close.
    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == REQUEST_CAPTURE) {
            if (resultCode == RESULT_OK && data != null) {
                Intent service = new Intent(this, CaptureService.class)
                        .putExtra(CaptureService.EXTRA_RESULT_CODE, resultCode)
                        .putExtra(CaptureService.EXTRA_RESULT_DATA, data);
                startForegroundService(service);
            } else {
                CaptureOutput.writeStatus(this, "denied", "screen capture consent was not given");
            }
        }
        finish();
    }
}
