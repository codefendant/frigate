"""Tests for persistent retained camera frames."""

import os
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from frigate.output import last_frame


class TestLastFramePersistence(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_patcher = patch.object(
            last_frame, "LAST_FRAME_DIR", self.temp_dir.name
        )
        self.dir_patcher.start()

    def tearDown(self) -> None:
        self.dir_patcher.stop()
        self.temp_dir.cleanup()

    def test_save_and_load_frame(self) -> None:
        frame = np.full((24, 32, 3), (10, 80, 200), dtype=np.uint8)

        self.assertTrue(last_frame.save_last_frame("front_door", frame))

        path = last_frame.get_last_frame_path("front_door")
        self.assertTrue(os.path.exists(path))

        loaded = last_frame.load_last_frame("front_door")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.shape, frame.shape)
        self.assertTrue(np.allclose(loaded, frame, atol=8))

    def test_save_overwrites_previous_frame(self) -> None:
        first = np.full((16, 16, 3), 20, dtype=np.uint8)
        second = np.full((16, 16, 3), 220, dtype=np.uint8)

        self.assertTrue(last_frame.save_last_frame("front_door", first))
        self.assertTrue(last_frame.save_last_frame("front_door", second))

        loaded = last_frame.load_last_frame("front_door")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertGreater(float(loaded.mean()), 200)

    def test_missing_frame_returns_none(self) -> None:
        self.assertIsNone(last_frame.load_last_frame("missing"))

    def test_none_frame_is_not_saved(self) -> None:
        self.assertFalse(last_frame.save_last_frame("front_door", None))
        self.assertFalse(os.path.exists(last_frame.get_last_frame_path("front_door")))

    def test_empty_frame_is_not_saved(self) -> None:
        frame = np.empty((0, 0, 3), dtype=np.uint8)

        self.assertFalse(last_frame.save_last_frame("front_door", frame))
        self.assertFalse(os.path.exists(last_frame.get_last_frame_path("front_door")))

    def test_corrupt_persisted_frame_returns_none(self) -> None:
        path = last_frame.get_last_frame_path("front_door")
        with open(path, "wb") as file:
            file.write(b"not an image")

        self.assertIsNone(last_frame.load_last_frame("front_door"))

    def test_temp_file_is_replaced_atomically(self) -> None:
        frame = np.full((16, 16, 3), 100, dtype=np.uint8)

        self.assertTrue(last_frame.save_last_frame("front_door", frame))

        path = last_frame.get_last_frame_path("front_door")
        self.assertTrue(os.path.exists(path))
        self.assertFalse(os.path.exists(f"{path}.tmp"))


if __name__ == "__main__":
    unittest.main()
