import importlib.util
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1] / 'dot_local/libexec/mac-backup'
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('restore', ROOT / 'restore.py')
R = importlib.util.module_from_spec(spec); spec.loader.exec_module(R)


class RestoreTests(unittest.TestCase):
    def test_empty_mac_install_keeps_ssh_and_chezmoi_and_leaves_services_stopped(self):
        with tempfile.TemporaryDirectory() as temp:
            home=Path(temp)/'new';home.mkdir();archive=Path(temp)/'archive'
            (archive/'recovery').mkdir(parents=True);(archive/'home/.codex/sessions').mkdir(parents=True)
            (archive/'home/.codex/sessions/x.jsonl').write_text('original transcript\n')
            (archive/'recovery/manifest.json').write_text(json.dumps({'complete':True,'captured_at':R.SNAPSHOT,
                'source_home':str(home),'source_host':'old-mac','files':[]}))
            state=home/'state';state.mkdir();(home/'.ssh').mkdir();(home/'.ssh/config').write_text('new SSH')
            (home/'.config/chezmoi').mkdir(parents=True);(home/'.config/chezmoi/chezmoi.toml').write_text('new chezmoi')
            args=['restore.py','--from',str(archive),'--projects','unused','--empty-machine']
            with patch.object(sys,'argv',args), patch.object(R.b,'HOME',home), patch.object(R.b,'STATE',state), \
                    patch.object(R,'JOURNAL',state/'journal.json'), patch.object(R.socket,'gethostname',return_value='new-mac'), \
                    patch.object(R,'check_no_writers'), patch.object(R,'restore_older_claude'), \
                    patch.object(R,'restore_arc',return_value=({},[])), patch.object(R,'restore_projects'), \
                    patch.object(R,'run') as run:
                R.main()
            self.assertEqual((home/'.codex/sessions/x.jsonl').read_text(),'original transcript\n')
            self.assertEqual((home/'.ssh/config').read_text(),'new SSH')
            self.assertEqual((home/'.config/chezmoi/chezmoi.toml').read_text(),'new chezmoi')
            self.assertEqual(json.loads((state/'journal.json').read_text())['status'],'READY_TO_START_HERDR')
            run.assert_called_once()
            self.assertNotIn('herdr-service-run',str(run.call_args))

    def test_older_claude_transcripts_are_downloaded_and_verified_once(self):
        with tempfile.TemporaryDirectory() as temp:
            home=Path(temp);state=home/'state';state.mkdir();config=home/'config';config.mkdir()
            (config/'config.json').write_text('{}');relative='.claude/projects/old/uuid.jsonl';data=b'old transcript\n'
            baseline={'files':[{'path':relative,'sha256':__import__('hashlib').sha256(data).hexdigest()}]}
            def borg(_,*args,**kwargs):
                if '--stdout' in args:return SimpleNamespace(stdout=json.dumps(baseline).encode())
                f=kwargs['cwd']/'home'/relative;f.parent.mkdir(parents=True);f.write_bytes(data)
            journal={}
            with patch.object(R.b,'HOME',home), patch.object(R.b,'STATE',state), patch.object(R.b,'CONFIG',config), \
                    patch.object(R,'JOURNAL',state/'journal.json'),patch.object(R.b,'borg',side_effect=borg) as remote:
                R.restore_older_claude({'files':[]},journal)
                R.restore_older_claude({'files':[]},journal)
            self.assertEqual(remote.call_count,2)
            self.assertEqual((home/relative).read_bytes(),data)
    def test_remap_uses_path_boundary_and_longest_prefix(self):
        mapping = {'/old': '/new', '/old/nested': '/elsewhere'}
        self.assertEqual(R.mapped({'cwd': '/old/nested/direct', '/old': '/old2', 'text': 'See /old'}, mapping),
                         {'cwd': '/elsewhere/direct', '/new': '/old2', 'text': 'See /old'})

    def test_changed_files_restore_bytes_symlinks_and_deletions_without_staging(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / 'export'; target = root / 'mount'
            (source / 'files').mkdir(parents=True); target.mkdir()
            (source / 'files/x').write_bytes(b'dirty\x00bytes'); (target / 'removed').write_text('old')
            (source / 'files/link').symlink_to('x')
            rows = [{'path': 'x', 'deleted': False, 'sha256': R.b.digest(source / 'files/x')},
                    {'path': 'link', 'deleted': False, 'sha256': None},
                    {'path': 'removed', 'deleted': True, 'sha256': None}]
            (source / 'files.json').write_text(json.dumps(rows))
            R.restore_files(source, target)
            self.assertEqual((target / 'x').read_bytes(), b'dirty\x00bytes')
            self.assertTrue((target / 'link').is_symlink()); self.assertFalse((target / 'removed').exists())
            rows[0]['sha256'] = 'corrupted'; (source / 'files.json').write_text(json.dumps(rows))
            with self.assertRaisesRegex(RuntimeError, 'Corrupted'):
                R.restore_files(source, target)

    def test_changed_path_cannot_escape_worktree(self):
        with self.assertRaises(ValueError):
            R.safe_path(Path('/target'), '../escape')

    def test_missing_helpers_do_not_replace_new_mac_managed_scripts(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);src=root/'source';dst=root/'target';src.mkdir();dst.mkdir()
            (src/'managed').write_text('old');(dst/'managed').write_text('new')
            (src/'hook').write_text('missing helper')
            R.install_missing(src,dst)
            self.assertEqual((dst/'managed').read_text(),'new')
            self.assertEqual((dst/'hook').read_text(),'missing helper')

    def test_remap_changes_metadata_but_never_transcript_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            home=Path(temp);old='/Users/dubiner/arcadia-dev-recovery';new='/Users/dubiner/arcadia-wt/main-dev-recovery'
            (home/'.config/herdr').mkdir(parents=True)
            (home/'.config/herdr/session.json').write_text(json.dumps({'cwd':old+'/direct','uuid':'keep'}))
            (home/'.config/herdr-history').mkdir()
            (home/'.config/herdr-history/x.yaml').write_text('project_cwd: '+old+'/direct\n')
            project=home/'.claude/projects/-Users-dubiner-arcadia-dev-recovery-direct';project.mkdir(parents=True)
            transcript=b'{"cwd":"'+old.encode()+b'/direct"}\n';(project/'uuid.jsonl').write_bytes(transcript)
            (home/'.codex').mkdir()
            (home/'.codex/config.toml').write_text('[projects."'+old+'/direct"]\ntrust_level="trusted"\n')
            db=sqlite3.connect(home/'.codex/state_5.sqlite')
            db.execute('CREATE TABLE threads(cwd TEXT)');db.execute('INSERT INTO threads VALUES (?)',(old+'/direct',));db.commit();db.close()
            with patch.object(R.b,'HOME',home):R.remap_metadata({old:new})
            self.assertEqual(json.loads((home/'.config/herdr/session.json').read_text())['cwd'],new+'/direct')
            moved=home/'.claude/projects/-Users-dubiner-arcadia-wt-main-dev-recovery-direct/uuid.jsonl'
            self.assertEqual(moved.read_bytes(),transcript)
            self.assertIn('[projects."'+new+'/direct"]',(home/'.codex/config.toml').read_text())
            db=sqlite3.connect(home/'.codex/state_5.sqlite')
            self.assertEqual(db.execute('SELECT cwd FROM threads').fetchone()[0],new+'/direct');db.close()

    def test_arc_restore_creates_shared_cache_and_keeps_branch_head_files(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp); archive = home / 'archive'; state = home / 'state'; state.mkdir()
            records = []
            for name, target, branch in [('main', home / 'arcadia', 'trunk'),
                                         ('worker', home / 'arcadia-wt/worker', 'task')]:
                folder = archive / 'recovery/arc' / name; folder.mkdir(parents=True)
                rec = {'source':str(target), 'target':str(target), 'export':name, 'complete':True,
                       'info':{'hash':'a'*40, 'branch':branch}}
                records.append(rec)
                for file, value in [('manifest.json', rec), ('files.json', []), ('status.json', {'status':{}})]:
                    (folder / file).write_text(json.dumps(value))
                (folder / 'staged.patch').write_text('')
            mounts = []; infos = {}; commands = []
            def run(args, cwd=None, **kwargs):
                args = list(map(str, args)); commands.append(args)
                if args[1:4] == ['mount','--list','--json']:
                    return SimpleNamespace(stdout=json.dumps(mounts).encode())
                if args[1] == 'mount':
                    target=Path(args[2]);target.mkdir(parents=True,exist_ok=True)
                    shared=args[args.index('--object-store')+1] if '--object-store' in args else '/shared/cache'
                    mounts.append({'mount':str(target),'status':'mounted','object-store':shared})
                    infos[str(target)]={'hash':'b'*40,'branch':'trunk'}
                if args[1] == 'info':return SimpleNamespace(stdout=json.dumps(infos[str(cwd)]).encode())
                if args[1] == 'status':return SimpleNamespace(stdout=b'{"status":{}}')
                if args[1] == 'checkout':
                    if len(args[2]) == 40:infos[str(cwd)]={'hash':args[2],'branch':'hash-branch'}
                    else:infos[str(cwd)]['branch']=args[2]
                return SimpleNamespace(stdout=b'')
            with patch.object(R.b, 'HOME', home), patch.object(R, 'JOURNAL', state / 'journal.json'), \
                    patch.object(R, 'run', side_effect=run):
                _, active = R.restore_arc(archive, {})
            self.assertEqual(len(active), 2)
            self.assertIn(['arc','mount',str(home / 'arcadia-wt/worker'),'--object-store','/shared/cache'], commands)
            self.assertEqual(infos[str(home / 'arcadia-wt/worker')], {'hash':'a'*40,'branch':'task'})
            self.assertFalse(any('reset' in c or 'clean' in c for c in commands))

    def test_staged_archive_is_rejected_before_arc_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);folder=root/'recovery/arc/x';folder.mkdir(parents=True)
            (folder/'manifest.json').write_text(json.dumps({'source':'/source','target':'/target','export':'x','complete':True}))
            (folder/'staged.patch').write_text('staged changes')
            with patch.object(R,'run') as run:
                with self.assertRaisesRegex(RuntimeError,'staged'):
                    R.restore_arc(root,{})
                run.assert_not_called()


if __name__ == '__main__':unittest.main()
