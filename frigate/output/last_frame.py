"""Persist the last camera frame for intentionally disabled cameras."""

import logging
import os
from pathlib import Path

import cv2
import numpy as np

from frigate.const import CLIPS_DIR

logger = logging.getLogger(__name__)

LAST_FRAME_DIR = os.path.join(CLIPS_DIR, "last_frames")
LAST_FRAME_EXTENSION = "webp"
LAST_FRAME_QUALITY = 90


def get_last_frame_path(camera: str) -> str:
    """Return the persistent last-frame path for a camera."""
    return os.path.join(LAST_FRAME_DIR, f"{camera}.{LAST_FRAME_EXTENSION}")


def save_last_frame(camera: str, frame: np.ndarray | None) -> bool:
    """Atomically persist a BGR camera frame as WebP.

    Returns True when a frame was written successfully. Invalid or missing frames
    are ignored so a failed capture never prevents the camera from being disabled.
    """
    if frame is None or frame.size == 0:
        return False

    Path(LAST_FRAME_DIR).mkdir(parents=True, exist_ok=True)

    success, encoded = cv2.imencode(
        f".{LAST_FRAME_EXTENSION}",
        frame,
        [int(cv2.IMWRITE_WEBP_QUALITY), LAST_FRAME_QUALITY],
    )
    if not success:
        logger.warning("Unable to encode retained frame for %s", camera)
        return False

    path = get_last_frame_path(camera)
    temp_path = f"{path}.tmp"

    try:
        with open(temp_path, "wb") as temp_file:
            temp_file.write(encoded.tobytes())
        os.replace(temp_path, path)
    except OSError:
        logger.exception("Unable to persist retained frame for %s", camera)
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        return False

    return True


def load_last_frame(camera: str) -> np.ndarray | None:
    """Load the persistent retained frame for a camera, if one exists."""
    path = get_last_frame_path(camera)
    if not os.path.exists(path):
        return None

    frame = cv2.imread(path, cv2.IMREAD_COLOR)
    if frame is None:
        logger.warning("Unable to read retained frame for %s", camera)
        return None

    return frame
