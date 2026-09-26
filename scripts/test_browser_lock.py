from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from browser_lock import FileMutex, FileMutexBusy
from playwright_controller import ProfileLock, BrowserControllerError


class FileMutexTests(unittest.TestCase):
    def test_same_root_different_profiles_contend_and_release_is_owner_safe(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(os, "kill") as kill:
            first = ProfileLock(Path(temp), "Default")
            other = ProfileLock(Path(temp), "Profile 2")
            first.acquire()
            with self.assertRaises(BrowserControllerError) as error:
                other.acquire()
            self.assertEqual(error.exception.code, "profile_locked")
            other.release()
            self.assertTrue(first.owned)
            with self.assertRaises(BrowserControllerError):
                other.acquire()
            first.release()
            other.acquire()
            first.release()  # an old owner's cleanup cannot remove the new lock
            with self.assertRaises(BrowserControllerError):
                first.acquire()
            other.release()
            kill.assert_not_called()

    def test_independent_roots_can_be_held_together(self):
        with tempfile.TemporaryDirectory() as temp:
            first = ProfileLock(Path(temp) / "shop-a", "Default")
            other = ProfileLock(Path(temp) / "shop-b", "Default")
            try:
                first.acquire()
                other.acquire()
                self.assertTrue(first.owned and other.owned)
            finally:
                first.release()
                other.release()

    def test_empty_or_stale_metadata_does_not_control_ownership(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "job.lock"
            for content in ("", '{"pid": 1}', "invalid"):
                path.write_text(content, encoding="utf-8")
                mutex = FileMutex(path)
                mutex.acquire()
                mutex.release()
                self.assertTrue(path.exists())

    def test_cross_process_contention_and_crash_release(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "job.lock"
            mutex = FileMutex(path)
            script = (
                "from pathlib import Path; import sys, os; "
                "from browser_lock import FileMutex, FileMutexBusy; "
                "m=FileMutex(Path(sys.argv[1]));\n"
                "try: m.acquire()\n"
                "except FileMutexBusy: sys.exit(7)\n"
                "os._exit(0)\n"
            )
            mutex.acquire()
            try:
                blocked = subprocess.run(
                    [sys.executable, "-c", script, str(path)],
                    cwd=Path(__file__).parent, capture_output=True, timeout=10,
                )
                self.assertEqual(blocked.returncode, 7, blocked.stderr)
            finally:
                mutex.release()
            exited = subprocess.run(
                [sys.executable, "-c", script, str(path)],
                cwd=Path(__file__).parent, capture_output=True, timeout=10,
            )
            self.assertEqual(exited.returncode, 0, exited.stderr)
            mutex.acquire()
            mutex.release()


if __name__ == "__main__":
    unittest.main()
