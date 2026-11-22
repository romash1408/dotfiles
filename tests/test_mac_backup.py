import importlib.util
import fnmatch
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / 'dot_local/libexec/mac-backup/backup.py'
spec = importlib.util.spec_from_file_location('mac_backup', SOURCE)
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)


class CaptureTests(unittest.TestCase):
    def test_arc_read_retries_timeout_once_but_does_not_hide_failure(self):
        cmd = ['/fake/arc', 'info', '--json']
        timeout = subprocess.TimeoutExpired(cmd, 20)
        result = subprocess.CompletedProcess(cmd, 0, b'{}')
        with mock.patch.object(B, 'execute', side_effect=[timeout, result]) as execute, \
                mock.patch.object(B.time, 'sleep'):
            self.assertIs(B.arc_read(cmd, Path('/source'), 20), result)
            self.assertEqual(execute.call_count, 2)
        with mock.patch.object(B, 'execute', side_effect=timeout) as execute, \
                mock.patch.object(B.time, 'sleep'):
            with self.assertRaises(subprocess.TimeoutExpired):
                B.arc_read(cmd, Path('/source'), 20)
            self.assertEqual(execute.call_count, 2)

    def test_first_success_is_automatically_pinned(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp);stage=state/'capture-first';(stage/'home').mkdir(parents=True)
            B.dump(stage/'recovery/manifest.json',{'files':[],'complete':True,'counters':{},'arc':[],'errors':[],'warnings':[]})
            def remote(config,*args,**kwargs):
                value={'archives':[]} if args[0]=='list' else {'archive':{}}
                return subprocess.CompletedProcess([],0,json.dumps(value).encode())
            with mock.patch.object(B,'STATE',state), mock.patch.object(B,'borg',side_effect=remote):
                B.run_backup({},stage)
            name=json.loads((state/'last-success.json').read_text())['archive']
            self.assertTrue(name.endswith('-complete-pinned'))
            self.assertEqual(json.loads((state/'status.json').read_text())['archive'],name)
            self.assertFalse(stage.exists())

    def test_pinned_migration_archive_survives_retention_and_updates_pointers(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp);name = 'work-mac-20261122T125945Z-complete'
            B.dump(state/'last-success.json', {'archive':name})
            B.dump(state/'status.json', {'archive':'new-partial','previous_success':{'archive':name}})
            B.dump(state/'validation.json', {'verified_archive':name,'borg_roundtrip':'passed'})
            with mock.patch.object(B,'STATE',state), mock.patch.object(B,'borg'):
                protected=B.pin_archive({},name)
            self.assertFalse(fnmatch.fnmatchcase(protected,'work-mac-*-complete'))
            self.assertEqual(json.loads((state/'last-success.json').read_text())['archive'],protected)
            self.assertEqual(json.loads((state/'validation.json').read_text())['verified_archive'],protected)
            self.assertEqual(json.loads((state/'status.json').read_text())['previous_success']['archive'],protected)

    def test_failed_pin_keeps_existing_recovery_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp);name='work-mac-test-complete'
            B.dump(state/'last-success.json',{'archive':name})
            with mock.patch.object(B,'STATE',state), mock.patch.object(B,'borg',side_effect=subprocess.CalledProcessError(2,['borg'])):
                with self.assertRaises(subprocess.CalledProcessError):B.pin_archive({},name)
            self.assertEqual(json.loads((state/'last-success.json').read_text())['archive'],name)

    def test_new_indexed_thread_without_rollout_is_not_a_complete_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'home'; stage = Path(tmp) / 'stage'
            p = stage / 'home/.codex/state_5.sqlite'; p.parent.mkdir(parents=True)
            rollout = '.codex/sessions/new.jsonl'
            with sqlite3.connect(p) as db:
                db.execute('CREATE TABLE threads(rollout_path TEXT)')
                db.execute('INSERT INTO threads VALUES (?)', (str(home / rollout),))
            with mock.patch.object(B, 'HOME', home):
                self.assertEqual(B.check_codex_index(stage, [])[0]['path'], rollout)
                self.assertFalse(B.check_codex_index(stage, [{'path':rollout, 'sha256':'test'}]))

    def test_offline_run_does_not_capture_or_change_last_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            B.dump(state / 'last-success.json', {'archive':'previous'})
            with mock.patch.object(B, 'STATE', state), mock.patch.object(B, 'capture') as capture, \
                 mock.patch.object(B, 'borg', side_effect=subprocess.CalledProcessError(255, ['ssh'])):
                with self.assertRaises(subprocess.CalledProcessError):
                    B.run_backup({})
            capture.assert_not_called()
            self.assertEqual(json.loads((state / 'last-success.json').read_text()), {'archive':'previous'})

    def test_failed_transfer_retains_one_export_and_reuses_committed_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp); stage = state / 'capture-test'; stage.mkdir()
            manifest = {'complete':True, 'files':[], 'counters':{}, 'arc':[], 'errors':[], 'warnings':[]}
            B.dump(stage / 'recovery/manifest.json', manifest)
            (stage / 'home').mkdir()
            B.dump(state / 'last-success.json', {'archive':'previous'})
            calls = []
            def failed(config, *args, **kwargs):
                calls.append(args[0])
                if args[0] == 'list':
                    return subprocess.CompletedProcess([], 0, b'{"archives":[]}')
                raise subprocess.CalledProcessError(255, ['ssh'])
            with mock.patch.object(B, 'STATE', state), mock.patch.object(B, 'borg', side_effect=failed), \
                 mock.patch.object(B.time, 'sleep'):
                with self.assertRaises(subprocess.CalledProcessError):
                    B.run_backup({}, stage)
            self.assertTrue(stage.exists())
            self.assertNotIn('prune', calls)
            name = json.loads((state / 'pending.json').read_text())['archive']
            self.assertEqual(json.loads((state / 'last-success.json').read_text())['archive'], 'previous')
            def reconnected(config, *args, **kwargs):
                calls.append(args[0])
                if args[0] == 'list':
                    return subprocess.CompletedProcess([], 0, json.dumps({'archives':[{'name':name}]}).encode())
                self.assertNotEqual(args[0], 'create')
                return subprocess.CompletedProcess([], 0, b'')
            with mock.patch.object(B, 'STATE', state), mock.patch.object(B, 'borg', side_effect=reconnected), \
                 mock.patch.object(B, 'capture') as capture:
                B.run_backup({})
            capture.assert_not_called()
            self.assertFalse(stage.exists())
            self.assertFalse((state / 'pending.json').exists())
            self.assertEqual(json.loads((state / 'last-success.json').read_text())['archive'], name)

    def test_locked_sqlite_fallback_recovers_only_copy_and_leaves_writer_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / 'live.db', Path(tmp) / 'copy.db'
            connect = sqlite3.connect
            with connect(source) as writer:
                writer.execute('CREATE TABLE records(value)')
                writer.execute('INSERT INTO records VALUES (1)'); writer.commit()
                writer.execute('BEGIN EXCLUSIVE')
                writer.execute('INSERT INTO records VALUES (99)')
                def locked_source(path, *args, **kwargs):
                    if isinstance(path, str) and path.startswith(source.as_uri()):
                        raise sqlite3.OperationalError('database is locked')
                    return connect(path, *args, **kwargs)
                with mock.patch.object(B.sqlite3, 'connect', side_effect=locked_source):
                    B.sqlite_backup(source, target)
                with connect(target) as copy:
                    self.assertEqual(copy.execute('SELECT value FROM records').fetchall(), [(1,)])
                self.assertEqual(writer.execute('SELECT COUNT(*) FROM records').fetchone()[0], 2)
                writer.rollback()

    def test_unmounted_arc_keeps_overlay_and_refs_without_object_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'home'; home.mkdir()
            store = home / 'store'; arc = store / '.arc'
            (arc / 'refs/heads').mkdir(parents=True)
            (arc / 'HEAD').write_text('Symbolic: "task"')
            (arc / 'refs/heads/task').write_text('Id: "' + 'a'*40 + '"')
            (arc / 'objects').mkdir(); (arc / 'objects/cache').write_text('excluded')
            (store / '.overlay_v2').mkdir(); (store / '.overlay_v2/changes').write_text('dirty')
            stage = Path(tmp) / 'stage'
            B.dump(stage / 'recovery/arc-mounts.json', [{'mount':str(home/'old-root'),
                   'status':'unmounted','store':str(store),'branch':'task'}])
            with mock.patch.object(B, 'HOME', home), mock.patch.object(B.shutil, 'which',return_value='/fake/arc'):
                _, exports, errors = B.capture_arc(stage)
            self.assertFalse(errors)
            self.assertTrue(exports[0]['complete'])
            p = stage / 'recovery/arc' / exports[0]['export'] / 'unmounted-store'
            self.assertEqual((p / '.overlay_v2/changes').read_text(), 'dirty')
            self.assertFalse((p / '.arc/objects').exists())

    def test_sqlite_online_backup_includes_committed_wal(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'home'; home.mkdir()
            db = home / 'state.sqlite'
            with sqlite3.connect(db) as writer:
                writer.execute('PRAGMA journal_mode=WAL')
                writer.execute('CREATE TABLE records(value)')
                writer.execute('INSERT INTO records VALUES (42)'); writer.commit()
                stage = Path(tmp) / 'stage'
                c = B.Capture(home, stage)
                c.collect(['state.sqlite'])
                self.assertFalse(c.errors)
                with sqlite3.connect(stage / 'home/state.sqlite') as copy:
                    self.assertEqual(copy.execute('SELECT value FROM records').fetchall(), [(42,)])
                    self.assertEqual(copy.execute('PRAGMA quick_check').fetchone()[0], 'ok')

    def test_jsonl_omits_only_unfinished_line_and_keeps_large_complete_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'home'; home.mkdir()
            data = json.dumps({'text':'x' * 100000}).encode() + b'\n'
            p = home / 'session.jsonl'; p.write_bytes(data + b'{"partial":')
            c = B.Capture(home, Path(tmp) / 'stage'); c.collect(['session.jsonl'])
            self.assertFalse(c.errors)
            self.assertEqual((c.stage / 'home/session.jsonl').read_bytes(), data)

    def test_does_not_traverse_arc_or_symlink_and_preserves_package_locks(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'home'; home.mkdir()
            (home / 'arc').mkdir(); (home / 'arc/secret').write_text('must not visit')
            (home / 'alias').symlink_to(home / 'arc')
            (home / 'uv.lock').write_text('source lockfile')
            c = B.Capture(home, Path(tmp) / 'stage', [home / 'arc'])
            c.collect(['arc', 'alias', 'uv.lock'])
            self.assertFalse(c.errors)
            self.assertEqual({r['path'] for r in c.files}, {'alias', 'uv.lock'})
            self.assertTrue((c.stage / 'home/alias').is_symlink())

    def test_restore_verifier_detects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'home'; home.mkdir()
            (home / 'file').write_text('original')
            c = B.Capture(home, Path(tmp) / 'stage'); c.collect(['file'])
            B.dump(c.stage / 'recovery/manifest.json', {'files':c.files,'complete':True,'counters':{}})
            B.verify_export(c.stage)
            (c.stage / 'home/file').write_text('changed')
            with self.assertRaisesRegex(ValueError, 'Checksum mismatch'):
                B.verify_export(c.stage)


if __name__ == '__main__':
    unittest.main()
