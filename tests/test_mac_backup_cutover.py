import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / 'dot_local/libexec/mac-backup'
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('cutover', ROOT / 'cutover.py')
cutover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cutover)


class CutoverTest(unittest.TestCase):
    def test_final_ready_accepts_timestamp_from_published_handoff(self):
        with tempfile.TemporaryDirectory() as temp:
            status = Path(temp) / 'cutover.json'
            with patch.object(cutover, 'STATUS', status):
                cutover.phase('READY', ready=True, at='verified-at', archive='work-mac-final-complete-pinned')
            self.assertEqual(json.loads(status.read_text()), {
                'phase': 'READY', 'ready': True, 'at': 'verified-at', 'archive': 'work-mac-final-complete-pinned'})

    def test_successful_worker_publishes_and_records_ready_after_restore(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp); state = home / 'state'; state.mkdir()
            config = home / 'config'; config.mkdir(); (config / 'config.json').write_text('{}')
            saved = config / 'cutover/saved'; saved.mkdir(parents=True)
            native = home / '.config/herdr/session.json'; native.parent.mkdir(parents=True)
            native.write_text('{}'); (saved / 'session-after-stop.json').write_text('{}')
            manifest = {'complete': True, 'files': [], 'counters': {'sqlite': 1}, 'arc': [], 'errors': []}
            def upload(_):
                cutover.b.dump(state / 'status.json', {'complete': True, 'archive': 'work-mac-final-complete', 'errors': []})
            def borg(_, *args, **kwargs):
                if args[0] == 'extract':
                    root = kwargs['cwd']; (root / 'home').mkdir()
                    cutover.b.dump(root / 'recovery/manifest.json', manifest)
                    cutover.b.dump(root / 'recovery/arc-mounts.json', [])
            with patch.object(cutover.b, 'HOME', home), patch.object(cutover.b, 'STATE', state), \
                    patch.object(cutover.b, 'CONFIG', config), patch.object(cutover, 'STATUS', state / 'cutover.json'), \
                    patch.object(cutover, 'call', return_value=SimpleNamespace(stdout=b'current-commit\n')), \
                    patch.object(cutover.time, 'sleep'), patch.object(cutover, 'preflight_arc'), \
                    patch.object(cutover, 'herdr_running', return_value=False), \
                    patch.object(cutover, 'stopped_herdr_capture', return_value=saved), \
                    patch.object(cutover, 'stop_job'), patch.object(cutover, 'writers', return_value=[]), \
                    patch.object(cutover.b, 'run_backup', side_effect=upload), \
                    patch.object(cutover.b, 'pin_archive', return_value='work-mac-final-complete-pinned'), \
                    patch.object(cutover.b, 'borg', side_effect=borg), patch.object(cutover, 'publish') as publish:
                cutover.worker()
            final = json.loads((state / 'cutover.json').read_text())
            self.assertEqual(final['phase'], 'READY')
            self.assertTrue(final['ready'])
            self.assertEqual(final['source_commit'], 'current-commit')
            self.assertEqual(publish.call_count, 2)
            self.assertEqual(publish.call_args.args[0]['archive'], final['archive'])
            self.assertFalse(list(state.glob('cutover-restore-*')))

    def test_arc_preflight_failure_never_stops_herdr(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'config.json').write_text('{}')
            with patch.object(cutover.b, 'CONFIG', root), patch.object(cutover.b, 'STATE', root), \
                    patch.object(cutover.b, 'borg'), patch.object(cutover, 'call'), \
                    patch.object(cutover.time, 'sleep'), patch.object(cutover, 'phase'), \
                    patch.object(cutover, 'preflight_arc', side_effect=RuntimeError('Arc unavailable')), \
                    patch.object(cutover, 'stop_job') as stop:
                with self.assertRaisesRegex(RuntimeError, 'Arc unavailable'):
                    cutover.worker()
                stop.assert_not_called()

    def test_stopped_resume_preserves_uuid_map_and_rejects_changed_native_state(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp); config = home / '.config/mac-backup'
            saved = config / 'cutover/20261122T152509Z'; saved.mkdir(parents=True)
            native = home / '.config/herdr/session.json'; native.parent.mkdir()
            native.write_text('{"workspaces": []}')
            for name in ('session-after-stop.json', 'session-before-stop.json'):
                (saved / name).write_text(native.read_text())
            for name in ('agents.json', 'metadata.json'):
                (saved / name).write_text('{}')
            with patch.object(cutover.b, 'HOME', home), patch.object(cutover.b, 'CONFIG', config):
                self.assertEqual(cutover.stopped_herdr_capture(), saved)
                native.write_text('{"workspaces": ["changed"]}')
                with self.assertRaisesRegex(RuntimeError, 'changed'):
                    cutover.stopped_herdr_capture()

    def test_busy_backup_is_waited_for_without_interrupting_it(self):
        with patch.object(cutover.fcntl, 'flock', side_effect=[BlockingIOError(35, 'busy'), None]) as flock, \
                patch.object(cutover.time, 'sleep') as sleep, patch.object(cutover, 'phase') as phase:
            cutover.wait_for_backup(123)
            self.assertEqual(flock.call_count, 2)
            sleep.assert_called_once_with(2)
            phase.assert_called_once_with('waiting-for-current-backup')

    def test_finished_job_can_be_retried_but_running_job_cannot(self):
        with patch.object(cutover.subprocess, 'run') as run, patch.object(cutover, 'stop_job') as stop:
            run.return_value.returncode = 0
            run.return_value.stdout = 'state = not running\nlast exit code = 1\n'
            cutover.clear_finished_job()
            stop.assert_called_once_with(cutover.LABEL)
            stop.reset_mock()
            run.return_value.stdout = 'state = running\n\tpid = 123\n'
            with self.assertRaises(RuntimeError):
                cutover.clear_finished_job()
            stop.assert_not_called()

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
        with patch.object(cutover.subprocess, 'run') as run, patch.object(cutover, 'call') as call, \
                patch.object(cutover.time, 'monotonic', side_effect=[0, 100]):
            run.return_value.returncode = 0
            with self.assertRaises(RuntimeError):
                cutover.stop_job('local.herdr')
            self.assertEqual(call.call_args.args[0][-2:], ['bootout', cutover.DOMAIN + '/local.herdr'])

    def test_asynchronous_bootout_waits_until_job_disappears(self):
        from types import SimpleNamespace
        with patch.object(cutover.subprocess, 'run', side_effect=[
                SimpleNamespace(returncode=n) for n in [0, 0, 0, 113]]) as run, \
                patch.object(cutover, 'call') as call, patch.object(cutover.time, 'sleep') as sleep:
            cutover.stop_job('local.herdr')
            self.assertEqual(run.call_count, 4)
            self.assertEqual(sleep.call_count, 2)
            call.assert_called_once()


if __name__ == '__main__':
    unittest.main()
