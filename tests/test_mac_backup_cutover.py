import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / 'dot_local/libexec/mac-backup'
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('cutover', ROOT / 'cutover.py')
cutover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cutover)


class CutoverTest(unittest.TestCase):
    def test_partial_and_missing_archive_cannot_publish_ready(self):
        for value in ({'complete': False, 'archive': 'x-partial'},
                      {'complete': True, 'archive': 'x-complete', 'errors': ['changed']},
                      {'complete': True},
                      {'complete': True, 'running': True, 'archive': 'x-complete'}):
            with self.assertRaises(RuntimeError):
                cutover.check_archive(value)
        self.assertEqual(cutover.check_archive({'complete': True, 'archive': 'x-complete', 'errors': []}), 'x-complete')

    def test_writers_ignore_tool_runtimes_and_find_real_apps(self):
        output = (b' 1 /sbin/launchd\n 2 /Applications/ChatGPT.app/Contents/Resources/cua_node\n'
                  b' 3 /Applications/Claude.app/Contents/MacOS/Claude\n'
                  b' 4 /Users/u/.local/share/claude/versions/2.0\n 5 /bin/codex\n')
        with patch.object(cutover, 'call') as call:
            call.return_value.stdout = output
            self.assertEqual([r['pid'] for r in cutover.writers()], [3, 4, 5])

    def test_loaded_service_must_disappear_after_bootout(self):
        with patch.object(cutover.subprocess, 'run') as run, patch.object(cutover, 'call') as call:
            run.return_value.returncode = 0
            with self.assertRaises(RuntimeError):
                cutover.stop_job('local.herdr')
            self.assertEqual(call.call_args.args[0][-2:], ['bootout', cutover.DOMAIN + '/local.herdr'])


if __name__ == '__main__':
    unittest.main()
