"""
Bounds synthetic video-writer readiness waits used by native screen-capture tests.
"""

import time

import pytest


# Wait for video input readiness, cancelling the writer when the deadline is exceeded.
def wait_for_video_input(writer, writer_input, frame_index: int, timeout_seconds: float = 10) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not writer_input.isReadyForMoreMediaData():
        if time.monotonic() >= deadline:
            writer.cancelWriting()
            pytest.fail(f"Synthetic video writer was not ready for frame {frame_index} within {timeout_seconds:g} seconds")
        time.sleep(0.01)
