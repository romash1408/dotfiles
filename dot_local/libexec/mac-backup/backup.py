#!/usr/bin/env python3
"""Immutable local agent/workspace exports, encrypted by Borg 1.x on backup.

No inference, agent restarts, mounts, checkout changes or remote service updates.
"""
import argparse
from contextlib import closing
import datetime
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import uuid

HOME = Path.home()
CONFIG = HOME / '.config/mac-backup'
STATE = HOME / '.local/state/mac-backup'
SKIP_NAMES = {'SingletonLock', 'SingletonSocket', 'SingletonCookie', 'LOCK', '.DS_Store'}
SKIP_DIRS = {'node_modules', '__pycache__', '.venv', 'venv', 'Cache', 'Code Cache',
             'GPUCache', 'GPUPersistentCache', 'GraphiteDawnCache', 'DawnGraphiteCache',
             'DawnWebGPUCache', 'Crashpad', 'component_crx_cache', 'extensions_crx_cache'}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.next')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def execute(argv, cwd=None, timeout=120, **kwargs):
    # chdir after exec: a FUSE lookup must not hang Popen's pre-exec error pipe.
    if cwd is not None:
        argv = [sys.executable, '-c',
                'import os,sys; os.chdir(sys.argv[1]); os.execvp(sys.argv[2],sys.argv[2:])',
                str(cwd), *map(str, argv)]
    return subprocess.run(argv, capture_output=True, timeout=timeout, check=True, **kwargs)


def sqlite_backup(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    def progress(*_):
        if time.monotonic() - start > 10:
            raise TimeoutError('SQLite online backup exceeded 10s')
    try:
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=2)) as src:
            with closing(sqlite3.connect(target)) as dst:
                src.backup(dst, pages=1024, progress=progress, sleep=.05)
    except (sqlite3.OperationalError, TimeoutError):
        # Some Chromium databases hold an exclusive lock even while idle. Copy
        # their *stable set* of DB/WAL/journal bytes, then recover only the copy.
        suffixes = ['', '-wal', '-journal']
        def fingerprint():
            result = {}
            for suffix in suffixes:
                p = source.with_name(source.name + suffix)
                if p.exists():
                    st = p.stat(); result[suffix] = (st.st_size, st.st_mtime_ns)
            return result
        for _ in range(3):
            before = fingerprint()
            for suffix in suffixes + ['-shm']:
                target.with_name(target.name + suffix).unlink(missing_ok=True)
            for suffix in before:
                shutil.copy2(source.with_name(source.name + suffix), target.with_name(target.name + suffix))
            if fingerprint() == before:
                break
        else:
            raise RuntimeError('Locked SQLite files kept changing; no consistent copy')
    with closing(sqlite3.connect(target)) as dst:
        if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('SQLite quick_check failed')
        dst.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        dst.execute('PRAGMA journal_mode=DELETE')
    for suffix in ['-wal','-shm','-journal']:
        target.with_name(target.name + suffix).unlink(missing_ok=True)


class Capture:
    def __init__(self, home, stage, mounts=()):
        self.home, self.stage = Path(home), Path(stage)
        self.mounts = {os.path.normpath(str(p)) for p in mounts}
        self.files, self.errors, self.warnings = [], [], []
        self.counters = {'codex_rollouts': 0, 'claude_cli_jsonl': 0, 'sqlite': 0}

    def ignored(self, path):
        rel = path.relative_to(self.home)
        if os.path.normpath(str(path)) in self.mounts:
            return True
        if path.name in SKIP_NAMES or path.name in SKIP_DIRS:
            return True
        if path.name.endswith(('-wal', '-shm', '-journal')):
            # Skip sidecars only for a SQLite file handled via its backup API.
            base = path.with_name(path.name.rsplit('-', 1)[0])
            if base.is_file():
                try:
                    with base.open('rb') as f:
                        if f.read(16) == b'SQLite format 3\x00':
                            return True
                except OSError:
                    pass
        text = str(rel)
        return any(text == p or text.startswith(p + '/') for p in (
            '.codex/tmp', '.codex/.tmp', '.codex/ipc', '.codex/node_repl',
            '.codex/thread-writer-locks', '.codex/project-metadata-locks',
            '.codex/app-server-control', '.codex/app-server-daemon', '.codex/cache',
            '.codex/packages/app-server-daemon/releases',
            '.claude/daemon', '.claude/ide', '.claude/session-env',
            'Library/Application Support/Claude/vm_bundles',
            'Library/Application Support/Claude/claude-code-vm',
            'Library/Application Support/Claude/claude-code',
            '.config/herdr/session-snapshots', '.local/share/mac-backup',
            '.local/bin/dev-backup', '.local/libexec/dev-backup', '.config/dev-backup',
            '.local/share/dev-backup/validation-venv')) or (
                text.startswith('.local/state/herdr-watchers/')
                and ('/payload/' in text or text.endswith('/payload'))) or (
                str(rel.parent) in ('.config/herdr', '.config/herdr-web-ui')
                and (path.name.endswith('.log') or '.sock' in path.name)) or (
                path.name.endswith('.lock') and str(rel).startswith(('.codex/', '.claude/')))

    def paths(self, root):
        if self.ignored(root):
            return
        mode = root.lstat().st_mode
        if stat.S_ISLNK(mode) or stat.S_ISREG(mode):
            yield root
        elif stat.S_ISDIR(mode):
            for p in sorted(root.iterdir()):
                yield from self.paths(p)

    def copy(self, source):
        rel = source.relative_to(self.home)
        target = self.stage / 'home' / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                target.symlink_to(os.readlink(source))
                self.files.append({'path': str(rel), 'symlink': os.readlink(source)})
                return
            with source.open('rb') as f:
                is_db = f.read(16) == b'SQLite format 3\x00'
            if is_db:
                sqlite_backup(source, target)
                self.counters['sqlite'] += 1
            elif source.suffix == '.jsonl':
                # Append-only transcripts: freeze size and omit unfinished last line.
                with source.open('rb') as src, target.open('wb') as dst:
                    left = os.fstat(src.fileno()).st_size
                    while left:
                        b = src.read(min(left, 1024 * 1024))
                        if not b:
                            raise RuntimeError('Transcript shrank during capture')
                        dst.write(b); left -= len(b)
                with target.open('rb+') as f:
                    size = f.seek(0, 2)
                    if size:
                        f.seek(size - 1)
                        if f.read(1) != b'\n':
                            pos = size
                            while pos:
                                start = max(0, pos - 65536)
                                f.seek(start); data = f.read(pos - start)
                                i = data.rfind(b'\n')
                                if i >= 0:
                                    f.truncate(start + i + 1); break
                                pos = start
                            else:
                                f.truncate(0)
                shutil.copystat(source, target)
            else:
                # Fail rather than silently calling an unstable file a full snapshot.
                for attempt in range(3):
                    a = source.stat()
                    shutil.copy2(source, target)
                    b = source.stat()
                    if (a.st_size, a.st_mtime_ns) == (b.st_size, b.st_mtime_ns):
                        break
                else:
                    raise RuntimeError('File kept changing during capture')
            self.files.append({'path': str(rel), 'size': target.stat().st_size,
                               'sha256': digest(target), 'sqlite': is_db})
            s = str(rel)
            if s.startswith(('.codex/sessions/', '.codex/archived_sessions/')) and source.suffix == '.jsonl':
                self.counters['codex_rollouts'] += 1
            if s.startswith('.claude/') and source.suffix == '.jsonl':
                self.counters['claude_cli_jsonl'] += 1
        except (OSError, ValueError, sqlite3.Error, RuntimeError, TimeoutError) as e:
            self.errors.append({'path': str(rel), 'error': str(e)})

    def collect(self, roots):
        paths = set()
        for rel in roots:
            p = self.home / rel
            if not p.exists() and not p.is_symlink():
                self.warnings.append('Absent optional source: ' + rel)
                continue
            try:
                paths.update(self.paths(p))
            except OSError as e:
                self.errors.append({'path': rel, 'error': str(e)})
        # Index snapshots first, then transcripts at least as recent as those indexes.
        for p in sorted(paths, key=lambda p: (not p.name.endswith(('.sqlite', '.db')), str(p))):
            self.copy(p)


def capture_arc(stage):
    arc = shutil.which('arc')
    if not arc:
        raise RuntimeError('Arc executable is missing')
    inventory = stage / 'recovery/arc-mounts.json'
    mounts = json.loads(inventory.read_text())
    exports, errors = [], []
    targets = set()
    for m in mounts:
        source = Path(m['mount'])
        if source == HOME / 'arcadia':
            target = HOME / 'arcadia'
        else:
            name = source.name if source.name != 'arcadia-dev-recovery' else 'main-dev-recovery'
            target = HOME / 'arcadia-wt' / name
        if str(target) in targets:
            target = target.with_name(target.name + '-' + hashlib.sha256(str(source).encode()).hexdigest()[:8])
        targets.add(str(target))
        name = hashlib.sha256(str(source).encode()).hexdigest()[:16]
        folder = stage / 'recovery/arc' / name
        folder.mkdir(parents=True)
        record = {'source': str(source), 'target': str(target), 'export': name, 'complete': False}
        try:
            if m.get('status') == 'unmounted':
                # These are immutable while unmounted. Keep overlays/refs (local
                # changes), never the remotely recoverable object cache.
                store = Path(m['store'])
                head = (store / '.arc/HEAD').read_text()
                symbolic = re.search(r'Symbolic:\s*"([^"]+)"', head)
                branch = symbolic.group(1) if symbolic else m.get('branch')
                if symbolic:
                    rel = Path('refs/heads') / branch
                    if '..' in rel.parts or Path(branch).is_absolute():
                        raise RuntimeError('Unsafe Arc HEAD reference')
                    ref = (store / '.arc' / rel).read_text()
                else:
                    ref = head
                match = re.search(r'Id:\s*"([0-9a-f]{40,64})"', ref)
                if not match or not branch:
                    raise RuntimeError('Cannot resolve unmounted branch/HEAD')
                shutil.copytree(store, folder / 'unmounted-store', symlinks=True,
                                ignore=shutil.ignore_patterns('objects','logs','traces','arc.pid','port','LOCK*'))
                if (store / '.arc/HEAD').read_text() != head:
                    raise RuntimeError('Unmounted HEAD changed during export')
                record.update(complete=True, capture_mode='unmounted-overlay',
                              info={'hash':match.group(1),'branch':branch},
                              source_store=str(store))
                dump(folder / 'manifest.json', record)
                exports.append(record)
                continue
            info = json.loads(execute([arc, 'info', '--json'], source, 20).stdout)
            if not info.get('hash') or not info.get('branch'):
                raise RuntimeError('Missing branch or HEAD')
            record['info'] = info
            status = json.loads(execute([arc, 'status', '--json', '-u', 'all'], source, 30).stdout)
            dump(folder / 'status.json', status)
            for file, opts in [('staged.patch', ['--cached']), ('unstaged.patch', [])]:
                (folder / file).write_bytes(execute([arc, 'diff', '--git', '--binary', '--no-color', *opts], source, 60).stdout)
            def paths(v):
                if isinstance(v, dict):
                    if isinstance(v.get('path'), str): yield v['path']
                    for x in v.values(): yield from paths(x)
                elif isinstance(v, list):
                    for x in v: yield from paths(x)
            changes = []
            for p in sorted(set(paths(status))):
                rel = Path(p)
                if rel.is_absolute() or '..' in rel.parts:
                    raise RuntimeError('Unsafe change path')
                a, b = source / rel, folder / 'files' / rel
                b.parent.mkdir(parents=True, exist_ok=True)
                if a.is_symlink(): b.symlink_to(os.readlink(a))
                elif a.is_file(): shutil.copy2(a, b)
                elif a.exists(): raise RuntimeError('Unexpanded changed directory: ' + p)
                changes.append({'path': p, 'deleted': not a.exists() and not a.is_symlink(),
                                'sha256': digest(b) if b.is_file() and not b.is_symlink() else None})
            dump(folder / 'files.json', changes)
            after = json.loads(execute([arc, 'info', '--json'], source, 20).stdout)
            status_after = json.loads(execute([arc, 'status', '--json', '-u', 'all'], source, 30).stdout)
            if (info['hash'], info['branch'], status) != (after['hash'], after['branch'], status_after):
                raise RuntimeError('Worktree changed during capture; retry after its task settles')
            record['complete'] = True
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
            record['error'] = str(e)
            errors.append({'path': str(source), 'error': str(e)})
        dump(folder / 'manifest.json', record)
        exports.append(record)
    return mounts, exports, errors


def check_codex_index(stage, files):
    """Catch a thread created after directory enumeration but before DB backup."""
    available = {row['path'] for row in files if 'sha256' in row}
    errors = []
    for path in (stage / 'home/.codex').glob('state_*.sqlite'):
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='threads'").fetchone():
                continue
            for (rollout,) in db.execute('SELECT rollout_path FROM threads'):
                try:
                    rel = str(Path(rollout).relative_to(HOME))
                except (TypeError, ValueError):
                    errors.append({'path':str(path.name), 'error':'Thread rollout outside the captured HOME'})
                    continue
                if rel not in available:
                    errors.append({'path':rel, 'error':'Indexed Codex transcript missing from export'})
    return errors


def capture(config):
    for rel in config.get('required_sources', ['.codex/sessions', '.claude/projects']):
        if not (HOME / rel).is_dir():
            raise RuntimeError('Required session source is missing: ' + rel)
    stage = STATE / ('capture-' + uuid.uuid4().hex)
    stage.mkdir(parents=True, mode=0o700)
    recovery = stage / 'recovery'
    recovery.mkdir()
    arc = shutil.which('arc')
    mounts = json.loads(execute([arc or 'arc', 'mount', '--list', '--json'], timeout=20).stdout)
    dump(recovery / 'arc-mounts.json', mounts)
    errors = []
    try:
        # All FUSE operations are isolated: a blocked stat/copy cannot hang the timer.
        execute([sys.executable, __file__, 'arc-capture', '--target', str(stage)], timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        errors.append({'path': 'arc', 'error': str(e)})
    arcs = [json.loads(p.read_text()) for p in sorted((recovery / 'arc').glob('*/manifest.json'))]
    errors.extend({'path':a['source'],'error':a.get('error','Incomplete Arc export')} for a in arcs if not a['complete'])
    if len(arcs) != len(mounts):
        errors.append({'path':'arc','error':'Not every registered worktree was exported'})
    c = Capture(HOME, stage, [x['mount'] for x in mounts])
    c.collect(config['sources'])
    errors.extend(check_codex_index(stage, c.files))
    # A copy of the collector and configuration makes a snapshot self-describing.
    shutil.copy2(__file__, recovery / 'backup.py')
    dump(recovery / 'config.json', config)
    result = {'schema': 1, 'captured_at': now(), 'source_home': str(HOME),
              'source_host': socket.gethostname(), 'scope': 'all-local-cli-and-desktop-agent-data',
              'complete': not (errors or c.errors), 'files': c.files,
              'counters': c.counters, 'arc': arcs, 'errors': errors + c.errors,
              'warnings': c.warnings,
              'consistency': 'online SQLite backup; bounded JSONL; per-file stable copy; final cutover requires quiescence'}
    dump(recovery / 'manifest.json', result)
    return stage, result


def borg(config, *args, cwd=None, timeout=7200):
    env = dict(os.environ, BORG_REPO=config['repository'],
               BORG_PASSCOMMAND='cat ' + shlex.quote(str(CONFIG / 'passphrase')),
               BORG_RSH=shlex.quote(str(HOME / '.local/libexec/mac-backup/ssh')),
               BORG_RELOCATED_REPO_ACCESS_IS_OK='no')
    return execute([config.get('borg', '/opt/homebrew/bin/borg'), *args], cwd, timeout, env=env)


def run_backup(config, prepared=None):
    dump(STATE / 'status.json', {'running': True, 'attempt_at': now()})
    stage = None
    try:
        pending = STATE / 'pending.json'
        if prepared is None and pending.exists():
            prepared = Path(json.loads(pending.read_text())['stage'])
            print('Retrying the retained immutable export', flush=True)
        if prepared is None:
            # Do not fill the local disk with hourly exports while offline.
            borg(config, 'info', '--lock-wait', '15', timeout=90)
            print('Repository reachable; capturing local state', flush=True)
            stage, manifest = capture(config)
        else:
            stage = prepared.resolve()
            if stage.parent != STATE.resolve() or not stage.name.startswith('capture-'):
                raise ValueError('Prepared export must be this machine\'s private capture directory')
            verify_export(stage)
            manifest = json.loads((stage/'recovery/manifest.json').read_text())
        kind = 'complete' if manifest['complete'] else 'partial'
        saved = json.loads(pending.read_text()) if pending.exists() else {}
        name = saved.get('archive') if saved.get('stage') == str(stage) else None
        if not name:
            name = 'work-mac-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + kind
        dump(pending, {'stage':str(stage), 'archive':name})
        result = None
        for attempt in range(3):
            try:
                # A disconnected client may have lost the successful response.
                archives = json.loads(borg(config, 'list', '--json', '--lock-wait', '90', timeout=180).stdout)
                if any(a['name'] == name for a in archives['archives']):
                    result = {'archive':{'name':name, 'recovered_after_disconnect':True}}
                else:
                    print('Uploading ' + name, flush=True)
                    r = borg(config, 'create', '--lock-wait', '90', '--compression', 'lz4', '--stats', '--json',
                             '::' + name, 'home', 'recovery', cwd=stage)
                    result = json.loads(r.stdout)
                break
            except (OSError, subprocess.SubprocessError):
                if attempt == 2:
                    raise
                print('Transfer interrupted; retrying the same export', flush=True)
                time.sleep(5 * (attempt + 1))
        previous = json.loads((STATE / 'last-success.json').read_text()) if (STATE / 'last-success.json').exists() else None
        status = {'running': False, 'finished_at': now(), 'archive': name, 'complete': manifest['complete'],
                  'counters': manifest['counters'], 'arc_count': len(manifest['arc']),
                  'errors': manifest['errors'], 'warnings': manifest['warnings'],
                  'previous_success': previous, 'borg': result.get('archive', {})}
        dump(STATE / 'status.json', status)
        pending.unlink(missing_ok=True)
        if manifest['complete']:
            dump(STATE / 'last-success.json', {'at': now(), 'archive': name})
        print(json.dumps({k:status[k] for k in ['archive','complete','counters','arc_count','errors']},ensure_ascii=False), flush=True)
        if not manifest['complete']:
            raise RuntimeError('Partial snapshot saved; inspect mac-backup status')
        # Retention removes only this tool's complete archives; partials remain for explicit review.
        borg(config, 'prune', '--lock-wait', '90', '--glob-archives', 'work-mac-*-complete',
             '--keep-hourly', '48', '--keep-daily', '30', '--keep-weekly', '8', '--keep-monthly', '12')
        compact = STATE / 'last-compact.json'
        if not compact.exists() or time.time() - compact.stat().st_mtime > 7 * 86400:
            borg(config, 'compact', '--lock-wait', '90')
            dump(compact, {'at':now()})
    except Exception as e:
        status = json.loads((STATE / 'status.json').read_text())
        status.update(running=False, error=str(e), failed_at=now())
        dump(STATE / 'status.json', status)
        raise
    finally:
        # Failed transfers retain the immutable export for an operator/retry.
        if stage and (STATE / 'status.json').exists():
            status = json.loads((STATE / 'status.json').read_text())
            if status.get('archive') and status.get('finished_at'):
                shutil.rmtree(stage)


def verify_export(target):
    m = json.loads((target / 'recovery/manifest.json').read_text())
    for row in m['files']:
        rel = Path(row['path'])
        if rel.is_absolute() or '..' in rel.parts:
            raise ValueError('Unsafe manifest path')
        p = target / 'home' / rel
        if 'symlink' in row:
            if not p.is_symlink() or os.readlink(p) != row['symlink']:
                raise ValueError('Symlink verification failed: ' + str(rel))
        elif p.is_symlink() or digest(p) != row['sha256']:
            raise ValueError('Checksum mismatch: ' + str(rel))
    for p in (target / 'home').rglob('*'):
        if not p.is_symlink() and p.is_file() and p.suffix in ('.sqlite', '.db'):
            with p.open('rb') as f: is_db = f.read(16) == b'SQLite format 3\x00'
            if is_db:
                with closing(sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)) as db:
                    if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise ValueError('Invalid database: ' + str(p))
    print(json.dumps({'complete':m['complete'],'files_verified':len(m['files']),'counters':m['counters']}))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['run','send','capture','status','list','check','restore','verify','arc-capture'])
    parser.add_argument('--target',type=Path)
    parser.add_argument('--archive')
    args = parser.parse_args()
    if args.action=='arc-capture':
        if not args.target:parser.error('arc-capture needs --target')
        capture_arc(args.target);return
    STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    if args.action=='verify':
        if not args.target:parser.error('verify needs --target')
        verify_export(args.target);return
    if args.action=='status':
        p=STATE/'status.json';print(p.read_text() if p.exists() else 'No backup yet');return
    config=json.loads((CONFIG/'config.json').read_text())
    with (STATE/'lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('Another mac-backup operation is running')
        if args.action=='run':run_backup(config)
        elif args.action=='send':
            if not args.target:parser.error('send needs --target CAPTURE')
            run_backup(config,args.target)
        elif args.action=='capture':
            stage,m=capture(config);print(json.dumps({'stage':str(stage),'complete':m['complete'],'errors':m['errors']}))
        elif args.action=='list':print(borg(config,'list','--json').stdout.decode())
        elif args.action=='check':borg(config,'check','--verify-data','--lock-wait','90')
        elif args.action=='restore':
            if not args.target or not args.archive:parser.error('restore needs --target NEW and --archive NAME')
            p=args.target.expanduser().absolute()
            if p.exists():raise ValueError('Restore target must not exist; never overwrite live sessions')
            p.mkdir(parents=True,mode=0o700)
            borg(config,'extract','--lock-wait','90','::'+args.archive,cwd=p)
            verify_export(p)


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as e:
        print('mac-backup: '+str(e),file=sys.stderr)
        if isinstance(e,subprocess.CalledProcessError) and e.stderr:
            print(e.stderr.decode(errors='replace')[-2000:],file=sys.stderr)
        sys.exit(1)
