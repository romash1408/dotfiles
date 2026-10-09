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
copy=module.RUN/'home/live.sqlite'
with sqlite3.connect(copy) as restored:
 assert restored.execute('SELECT value FROM data').fetchone()[0]=='committed in WAL'
 assert restored.execute('PRAGMA quick_check').fetchone()[0]=='ok'
assert writer.execute('SELECT COUNT(*) FROM data').fetchone()[0]==1
writer.close()
'''
        p = subprocess.run([sys.executable, '-c', code, str(COLLECT)],
                           env={**os.environ, 'HOME': str(home)}, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_ssh_failure_keeps_last_success_without_stale_partial(self):
        home = self.root / 'backup-home'; home.mkdir()
        cfg = home / '.config/dev-backup'; cfg.mkdir(parents=True)
        (cfg / 'password').write_text('temporary-test-password\n')
        repo = home / 'Backups/dev/repository'
        repo.parent.mkdir(parents=True)
        restic = ['/opt/homebrew/bin/restic', '-r', str(repo), '-p', str(cfg / 'password')]
        subprocess.run([*restic, 'init'], check=True, capture_output=True)
        stage = home / 'Backups/dev/.staging-v2'
        for name in ['home', 'recovery', 'resolved-skills']:
            (stage / name).mkdir(parents=True)
        (stage / 'home/important').write_text('previous good copy')
        (stage / 'recovery/manifest.json').write_text(json.dumps({'complete': True}))
        subprocess.run([*restic, 'backup', '.', '--host', 'dev', '--tag', 'complete', '--tag', 'managed-codex'],
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
        self.assertNotIn('last_partial_snapshot', status)
        snapshots = json.loads(subprocess.run([*restic, 'snapshots', '--json'], check=True,
                                             capture_output=True, text=True).stdout)
        self.assertEqual(len(snapshots), 1)
        self.assertTrue(any('complete' in x['tags'] for x in snapshots))
        target = home / 'restored'
        restored = subprocess.run([sys.executable, str(BACKUP), 'restore', '--snapshot', good,
                                   '--target', str(target)], env={**os.environ, 'HOME': str(home)},
                                  capture_output=True, text=True)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual((target / 'home/important').read_text(), 'previous good copy')

class ScopedCollectorTests(unittest.TestCase):
    def run_collector_test(self, body):
        with tempfile.TemporaryDirectory() as temporary:
            code = "import pathlib,sys,json,sqlite3\nnamespace={'__file__':sys.argv[1],'__name__':'collector_test'}\nexec(compile(pathlib.Path(sys.argv[1]).read_text(),sys.argv[1],'exec'),namespace)\n" + body
            p = subprocess.run([sys.executable, '-c', code, str(COLLECT)],
                               env={**os.environ, 'HOME': temporary, 'PYTHONDONTWRITEBYTECODE': '1'},
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)

    def test_scope_removes_unmanaged_threads_and_queue_rows(self):
        self.run_collector_test('''
p=pathlib.Path.home()/'state.sqlite'
with sqlite3.connect(p) as db:
 db.execute('CREATE TABLE threads(id TEXT PRIMARY KEY)')
 db.executemany('INSERT INTO threads VALUES (?)',[('worker',),('child',),('ops',)])
 db.execute('CREATE TABLE queued_items(thread_id TEXT,payload_json TEXT)')
 db.executemany('INSERT INTO queued_items VALUES (?,?)',[('worker','keep'),('ops','remove')])
 db.execute('CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT)')
 db.executemany('INSERT INTO thread_spawn_edges VALUES (?,?)',[('worker','child'),('ops','worker')])
namespace['trim_database'](p,{'worker','child'})
with sqlite3.connect(p) as db:
 assert {x[0] for x in db.execute('SELECT id FROM threads')}=={'worker','child'}
 assert list(db.execute('SELECT * FROM queued_items'))==[('worker','keep')]
 assert list(db.execute('SELECT * FROM thread_spawn_edges'))==[('worker','child')]
 assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
''')

    def test_jsonl_preserves_interior_bytes_but_omits_unfinished_tail(self):
        self.run_collector_test('''
sid='00000000-0000-4000-8000-000000000001'
p=pathlib.Path.home()/'.codex/sessions/rollout.jsonl';p.parent.mkdir(parents=True)
head=json.dumps({'type':'session_meta','payload':{'id':sid}}).encode()+b'\\n'
data=head+b'{bad historical record}\\n'+b'{"unfinished":'
p.write_bytes(data)
r=namespace['session_copy'](p,sid)
assert (namespace['RUN']/'home'/r['path']).read_bytes()==head+b'{bad historical record}\\n'
assert r['malformed_jsonl_lines']==1
assert r['unfinished_bytes_omitted']==len(b'{"unfinished":')
assert p.read_bytes()==data
''')

    def test_history_name_match_cannot_select_a_different_pane(self):
        self.run_collector_test('''
r={'agent':{'name':'worker'},'herdr':{'agent_pane_id':'w1:p1'}}
a=[{'name':'worker','pane_id':'w1:p2','agent':'codex','agent_session':{'kind':'id','value':'00000000-0000-4000-8000-000000000001'}}]
try: namespace['choose_live'](r,a)
except RuntimeError: pass
else: raise AssertionError('Selected a different pane')
''')


class TransferFailureTests(unittest.TestCase):
    def test_fresh_transfer_failure_keeps_previous_snapshot_and_current_partial(self):
        with tempfile.TemporaryDirectory() as temporary:
            code = """
import pathlib,sys,json,subprocess
ns={'__file__':sys.argv[1],'__name__':'backup_test'}
exec(compile(pathlib.Path(sys.argv[1]).read_text(),sys.argv[1],'exec'),ns)
h=pathlib.Path.home();cfg=ns['CONFIG'];cfg.mkdir(parents=True)
(cfg/'password').write_text('test-only-password')
ns['REPO'].parent.mkdir(parents=True)
subprocess.run([ns['RESTIC'],'-r',str(ns['REPO']),'-p',str(cfg/'password'),'init'],check=True,capture_output=True)
for part in ['home','recovery']:(ns['STAGE']/part).mkdir(parents=True)
(ns['STAGE']/'home/important').write_text('previous good data')
(ns['STAGE']/'recovery/manifest.json').write_text(json.dumps({'complete':True}))
ns['snapshot']('complete')
(cfg/'status.json').write_text(json.dumps({'last_success':'previous-success'}))
collector=h/'.local/libexec/dev-backup/collect.py';collector.parent.mkdir(parents=True);collector.write_text('fake collector')
manifest={'source_home':str(h),'complete':True,'capture':'fresh'}
remote=str(h/'.cache/dev-backup/run-')+'a'*32
original_run=ns['run']
def run(args,**kwargs):
 if args[0]=='ssh':return subprocess.CompletedProcess(args,0,stdout=json.dumps({'run_dir':remote,'manifest':manifest}).encode())
 return original_run(args,**kwargs)
def sync(source,target,**kwargs):
 if source.endswith('/recovery/'):
  (target/'manifest.json').write_text(json.dumps(manifest))
 else:raise subprocess.CalledProcessError(23,['rsync','fresh-export'])
ns['run']=run;ns['sync']=sync;sys.argv=['dev-backup','run']
try:ns['main']()
except subprocess.CalledProcessError:pass
else:raise AssertionError('Transfer failure was ignored')
status=json.loads((cfg/'status.json').read_text())
assert status['last_success']=='previous-success'
assert status.get('last_partial_snapshot')
assert json.loads((ns['STAGE']/'recovery/manifest.json').read_text())['capture']=='fresh'
snapshots=json.loads(subprocess.check_output([ns['RESTIC'],'-r',str(ns['REPO']),'-p',str(cfg/'password'),'snapshots','--json']))
assert len(snapshots)==2
assert any('complete' in s['tags'] for s in snapshots)
assert any('partial' in s['tags'] for s in snapshots)
"""
            result=subprocess.run([sys.executable,'-c',code,str(BACKUP)],
                                  env={**os.environ,'HOME':temporary,'PYTHONDONTWRITEBYTECODE':'1'},capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)


if __name__ == '__main__':
    unittest.main()
