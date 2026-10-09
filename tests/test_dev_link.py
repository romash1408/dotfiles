"""Exercise bounded connection setup and cleanup without SSH or live services."""
import importlib.machinery
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

SOURCE = Path(__file__).resolve().parents[1]/'dot_local/bin/executable_dev-link'
LINK = importlib.machinery.SourceFileLoader('dev_link', str(SOURCE)).load_module()


class DevLinkTests(unittest.TestCase):
    def test_hung_setup_is_bounded_and_child_is_reaped(self):
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory)/'pid'
            worker = ("import os,time,pathlib; pathlib.Path(" + repr(str(pidfile)) +
                      ").write_text(str(os.getpid())); time.sleep(60)")
            supervisor = LINK.Supervisor(setup_timeout=0.4)
            started = time.monotonic()
            result = supervisor.connection([sys.executable, '-c', worker])
            self.assertIn('setup timed out', result)
            self.assertLess(time.monotonic()-started, 3)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pidfile.read_text()), 0)

    def test_connected_child_receives_heartbeats_and_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory)/'heartbeat'
            pidfile = Path(directory)/'pid'
            worker = ("import os,pathlib,sys; pathlib.Path(" + repr(str(pidfile)) +
                      ").write_text(str(os.getpid())); print('DEV_LINK_READY',flush=True); "
                      "pathlib.Path(" + repr(str(capture)) +
                      ").write_text(sys.stdin.readline()); sys.stdin.read()")
            supervisor = LINK.Supervisor(setup_timeout=1)
            connected = threading.Event()
            supervisor.log = lambda _: connected.set()
            results = []
            thread = threading.Thread(target=lambda: results.append(
                supervisor.connection([sys.executable, '-c', worker])))
            thread.start()
            try:
                self.assertTrue(connected.wait(2))
                deadline = time.monotonic()+2
                while not capture.exists() and time.monotonic()<deadline:
                    time.sleep(0.02)
                self.assertEqual(capture.read_text(), 'ping\n')
            finally:
                supervisor.stop()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results, ['stopped'])
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pidfile.read_text()), 0)

    def test_failed_login_keeps_error_and_returns_for_retry(self):
        supervisor = LINK.Supervisor(setup_timeout=1)
        result = supervisor.connection([sys.executable, '-c',
            "import sys; print('forwarding port failed',file=sys.stderr); sys.exit(255)"])
        self.assertIn('forwarding port failed', result)


if __name__ == '__main__':
    unittest.main()
