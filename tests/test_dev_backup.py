"""Recovery/data-loss checks; no dev connection or active Mac config changes."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / 'dot_local/bin/executable_dev-backup'
RESTORE = ROOT / 'dot_local/libexec/dev-backup/restore-worktree.py'
COLLECT = ROOT / 'dot_local/libexec/dev-backup/collect.py'


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'export'
        self.target = self.root / 'new-tree'
        (self.source / 'files').mkdir(parents=True)
        self.target.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def records(self, value):
        (self.source / 'files.json').write_text(json.dumps(value))

    def restore(self, apply=False):
        return subprocess.run([sys.executable, str(RESTORE), str(self.source), str(self.target),
                               *(['--apply'] if apply else [])], capture_output=True, text=True)

    def test_binary_new_file_symlink_and_deletion(self):
        data = b'\x00\xff\x80binary\x00'
        (self.source / 'files/blob').write_bytes(data)
        (self.target / 'obsolete').write_text('old')
        self.records([{'path': 'blob', 'sha256': hashlib.sha256(data).hexdigest()},
                      {'path': 'link', 'symlink': 'blob'}, {'path': 'obsolete', 'deleted': True}])
        self.assertEqual(self.restore(True).returncode, 0)
        self.assertEqual((self.target / 'blob').read_bytes(), data)
        self.assertEqual(os.readlink(self.target / 'link'), 'blob')
        self.assertFalse((self.target / 'obsolete').exists())

    def test_preview_does_not_change_target(self):
        (self.source / 'files/new').write_text('new')
        self.records([{'path': 'new', 'sha256': hashlib.sha256(b'new').hexdigest()}])
        self.assertEqual(self.restore().returncode, 0)
        self.assertFalse((self.target / 'new').exists())

    def test_checksum_mismatch_refuses_restore(self):
        (self.source / 'files/new').write_text('corrupt')
        self.records([{'path': 'new', 'sha256': hashlib.sha256(b'expected').hexdigest()}])
        self.assertNotEqual(self.restore(True).returncode, 0)
        self.assertFalse((self.target / 'new').exists())

    def test_existing_symlink_cannot_escape_target(self):
        outside = self.root / 'outside'; outside.mkdir()
        (self.target / 'escape').symlink_to(outside)
        (self.source / 'files/escape').mkdir()
        (self.source / 'files/escape/new').write_text('new')
        self.records([{'path': 'escape/new', 'sha256': hashlib.sha256(b'new').hexdigest()}])
        self.assertNotEqual(self.restore(True).returncode, 0)
        self.assertFalse((outside / 'new').exists())

    def test_path_traversal_refused(self):
        self.records([{'path': '../outside', 'deleted': True}])
        self.assertNotEqual(self.restore(True).returncode, 0)

    def test_live_sqlite_wal_is_in_online_snapshot(self):
        home = self.root / 'home'; home.mkdir()
        code = '''
import importlib.util, pathlib, sqlite3
spec=importlib.util.spec_from_file_location('collector',__import__('sys').argv[1])
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
source=pathlib.Path.home()/'live.sqlite'
writer=sqlite3.connect(source)
writer.execute('PRAGMA journal_mode=WAL')
writer.execute('CREATE TABLE data(value TEXT)');writer.commit()
writer.execute("INSERT INTO data VALUES ('committed in WAL')");writer.commit()
result=module.sqlite_copy(source)
copy=module.RUN/'databases/home/live.sqlite'
with sqlite3.connect(copy) as restored:
 assert restored.execute('SELECT value FROM data').fetchone()[0]=='committed in WAL'
 assert restored.execute('PRAGMA quick_check').fetchone()[0]=='ok'
assert writer.execute('SELECT COUNT(*) FROM data').fetchone()[0]==1
writer.close()
'''
        p = subprocess.run([sys.executable, '-c', code, str(COLLECT)],
                           env={**os.environ, 'HOME': str(home)}, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_ssh_failure_keeps_last_success_and_preserves_partial(self):
        home = self.root / 'backup-home'; home.mkdir()
        cfg = home / '.config/dev-backup'; cfg.mkdir(parents=True)
        (cfg / 'password').write_text('temporary-test-password\n')
        repo = home / 'Backups/dev/repository'
        repo.parent.mkdir(parents=True)
        restic = ['/opt/homebrew/bin/restic', '-r', str(repo), '-p', str(cfg / 'password')]
        subprocess.run([*restic, 'init'], check=True, capture_output=True)
        stage = home / 'Backups/dev/.staging'
        for name in ['home', 'recovery', 'resolved-skills']:
            (stage / name).mkdir(parents=True)
        (stage / 'home/important').write_text('previous good copy')
        (stage / 'recovery/manifest.json').write_text(json.dumps({'complete': True}))
        subprocess.run([*restic, 'backup', '.', '--host', 'dev', '--tag', 'complete'],
                       cwd=stage, check=True, capture_output=True)
        good = json.loads(subprocess.run([*restic, 'snapshots', '--json'], check=True,
                                        capture_output=True, text=True).stdout)[0]['id']
        (cfg / 'status.json').write_text(json.dumps({'last_success': 'previous-success'}))
        tools = home / 'fake-bin'; tools.mkdir()
        (tools / 'ssh').write_text('#!/bin/sh\necho unavailable >&2\nexit 255\n')
        (tools / 'ssh').chmod(0o700)
        scripts = home / '.local/libexec/dev-backup'; scripts.mkdir(parents=True)
        (scripts / 'collect.py').write_text('unused during SSH failure')
        p = subprocess.run([sys.executable, str(BACKUP), 'run'],
                           env={**os.environ, 'HOME': str(home), 'PATH': str(tools) + ':' + os.environ['PATH']},
                           capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)
        status = json.loads((cfg / 'status.json').read_text())
        self.assertEqual(status['last_success'], 'previous-success')
        self.assertFalse(status['running'])
        self.assertIn('last_partial_snapshot', status)
        snapshots = json.loads(subprocess.run([*restic, 'snapshots', '--json'], check=True,
                                             capture_output=True, text=True).stdout)
        self.assertEqual(len(snapshots), 2)
        self.assertTrue(any('complete' in x['tags'] for x in snapshots))
        self.assertTrue(any('partial' in x['tags'] for x in snapshots))
        target = home / 'restored'
        restored = subprocess.run([sys.executable, str(BACKUP), 'restore', '--snapshot', good,
                                   '--target', str(target)], env={**os.environ, 'HOME': str(home)},
                                  capture_output=True, text=True)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual((target / 'home/important').read_text(), 'previous good copy')


if __name__ == '__main__':
    unittest.main()
