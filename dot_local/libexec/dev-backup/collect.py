#!/usr/bin/env python3
"""Read-only recovery export on dev; invoked over SSH by dev-backup."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import uuid

os.umask(0o077)
HOME = Path.home()
RUN = HOME / '.cache/dev-backup' / ('run-' + uuid.uuid4().hex)
RUN.mkdir(parents=True)
REC = RUN / 'recovery'
REC.mkdir()
ERRORS = []
AGENT_CWDS = []


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def command(args, cwd=None, timeout=60):
    p = subprocess.run(args, cwd=cwd, capture_output=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError(str(args) + ': ' + p.stderr.decode(errors='replace')[-1500:])
    return p.stdout


def json_command(args, cwd=None):
    return json.loads(command(args, cwd))


def sqlite_copy(source):
    relative = source.relative_to(HOME)
    target = RUN / 'databases/home' / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    def progress(status, remaining, total):
        if time.monotonic() - started > 120:
            raise TimeoutError('SQLite online backup exceeded 120 seconds')
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=10) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst, pages=1024, progress=progress, sleep=0.05)
            result = dst.execute('PRAGMA quick_check').fetchone()[0]
            if result != 'ok':
                raise RuntimeError(str(relative) + ': ' + result)
    return {'path': str(relative), 'size': target.stat().st_size, 'quick_check': 'ok'}


def safe_relative(name):
    path = Path(name)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('Unsafe Arc path: ' + name)
    return path


def status_paths(value):
    if isinstance(value, dict):
        if isinstance(value.get('path'), str):
            yield value['path']
        for x in value.values():
            yield from status_paths(x)
    elif isinstance(value, list):
        for x in value:
            yield from status_paths(x)


def export_mount(mount):
    root = Path(mount['mount'])
    name = re.sub('[^a-zA-Z0-9._-]', '_', mount['name'])
    folder = REC / 'arc' / name
    folder.mkdir(parents=True)
    record = {'mount': str(root), 'store': mount['store'], 'name': name, 'errors': []}
    try:
        info = json_command(['/usr/bin/arc', 'info', '--json'], root)
        record['info'] = info
        write_json(folder / 'info.json', info)
        status = json_command(['/usr/bin/arc', 'status', '--json', '-u', 'all'], root)
        write_json(folder / 'status.json', status)
        # All commands are read-only. Never stash, checkout, commit, or push agents' work.
        write_json(folder / 'branches.json', json_command(['/usr/bin/arc', 'branch', '--json'], root))
        for label, args in [('staged.patch', ['--cached']), ('unstaged.patch', [])]:
            (folder / label).write_bytes(command(['/usr/bin/arc', 'diff', '--git', '--binary', '--no-color', *args], root))
        # Old parked worktrees may have a trunk ref months behind their HEAD.
        # Recovery can start from the published branch revision; export only local
        # commits after it. This avoids fetching unrelated months of trunk history.
        base_ref = info.get('remote_head') or 'trunk'
        base = info['hash'] if base_ref == info['hash'] else command(['/usr/bin/arc', 'merge-base', base_ref, info['hash']], root).decode().strip()
        record['base'] = base
        record['base_kind'] = 'published_remote_head' if info.get('remote_head') else 'trunk_merge_base'
        (folder / 'committed.patch').write_bytes(command(['/usr/bin/arc', 'diff', '--git', '--binary', '--no-color', base, info['hash']], root, timeout=300))
        history = json_command(['/usr/bin/arc', 'log', '--json', '-n', '1000', base + '..' + info['hash']], root)
        write_json(folder / 'commits.json', history)
        if len(history) >= 1000:
            raise RuntimeError('Commit export reached 1000-commit bound')
        changed = json_command(['/usr/bin/arc', 'diff', '--name-status', '--json', base, info['hash']], root)
        write_json(folder / 'committed-files.json', changed)
        paths = sorted(set(status_paths(status)) | set(status_paths(changed)))
        files = []
        for value in paths:
            relative = safe_relative(value)
            source = root / relative
            target = folder / 'files' / relative
            try:
                mode = source.lstat().st_mode
            except FileNotFoundError:
                files.append({'path': value, 'deleted': True})
                continue
            if stat.S_ISDIR(mode):
                raise RuntimeError('Unexpanded untracked directory: ' + value)
            target.parent.mkdir(parents=True, exist_ok=True)
            if stat.S_ISLNK(mode):
                target.symlink_to(os.readlink(source))
                files.append({'path': value, 'symlink': os.readlink(source)})
            elif stat.S_ISREG(mode):
                shutil.copy2(source, target)
                digest_state = hashlib.sha256()
                with target.open('rb') as f:
                    for block in iter(lambda: f.read(1024 * 1024), b''):
                        digest_state.update(block)
                digest = digest_state.hexdigest()
                files.append({'path': value, 'sha256': digest, 'size': target.stat().st_size})
            else:
                raise RuntimeError('Unsupported changed file: ' + value)
        write_json(folder / 'files.json', files)
        record['files'] = len(files)
        # Ignored role checkpoints and local instructions are recovery data too.
        for cwd in sorted(set([str(root), *AGENT_CWDS])):
            cwd_path = Path(cwd)
            try:
                cwd_relative = cwd_path.relative_to(root)
            except ValueError:
                continue
            for item in ['.herdr', '.agents', '.claude', '.codex', 'AGENTS.md', 'CLAUDE.md']:
                source = cwd_path / item
                target = folder / 'instructions' / cwd_relative / item
                if source.is_dir():
                    shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True,
                                    ignore=shutil.ignore_patterns('node_modules', '__pycache__', 'logs', 'log'))
                elif source.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
        after = json_command(['/usr/bin/arc', 'info', '--json'], root)
        if info['hash'] != after['hash'] or info['branch'] != after['branch']:
            raise RuntimeError('Agent changed HEAD during export; retry next run')
        record['complete'] = True
    except Exception as e:
        record['complete'] = False
        record['errors'].append(str(e))
    write_json(folder / 'manifest.json', record)
    return record


def main():
    global AGENT_CWDS
    roots = ['.agents', '.claude', '.codex', '.config', '.local/bin', '.local/lib',
             '.local/libexec', '.local/state', '.local/share/chezmoi',
             '.local/share/opencode', '.local/share/herdr-transcripts',
             '.local/share/herdr-watchdog', '.local/share/herdr-sherpa',
             '.local/share/tools-sync', '.ssh', '.ssl', '.certs', '.gnupg',
             '.direct-tokens', '.yt', '.yql', '.mcp', '.mcp_store', '.aisuite',
             '.sherpa', '.arc', 'projects/arc_obj_store', '.dotfiles.git',
             '.oh-my-zsh.git', '.zsh-autosuggestions.git', 'dotfiles.git',
             'oh-my-zsh.git', 'zsh-autosuggestions.git', 'zsh-custom',
             'grut', 'hatter', 'logos_direct', 'umr', 'opencode', '.git']
    roots = [p for p in roots if (HOME / p).exists() or (HOME / p).is_symlink()]
    skipped = []
    for p in HOME.iterdir():
        if p.is_symlink() or not p.is_file():
            continue
        if p.stat().st_size > 64 * 1024**2 or p.suffix in ('.zip', '.gz', '.vsix'):
            skipped.append(p.name)
        else:
            roots.append(p.name)
    write_json(REC / 'source-roots.json', sorted(set(roots)))
    for key, args in [('agents', [str(HOME / '.local/bin/herdr'), 'agent', 'list']),
                      ('workspaces', [str(HOME / '.local/bin/herdr'), 'workspace', 'list'])]:
        try:
            value = json_command(args)
            write_json(REC / ('herdr-' + key + '.json'), value)
            if key == 'agents':
                AGENT_CWDS = [a.get('foreground_cwd') or a.get('cwd') for a in value['result']['agents']
                             if (a.get('foreground_cwd') or a.get('cwd', '')).startswith(str(HOME) + '/')
                             and not (a.get('foreground_cwd') or a.get('cwd', '')).endswith(' (deleted)')]
        except Exception as e:
            ERRORS.append(str(e))
    # Running databases are copied using SQLite's online backup API, including WAL data.
    databases = []
    db_roots = ['.codex', '.config', '.local/state', '.local/share/opencode',
                '.local/share/herdr-transcripts', '.local/share/herdr-watchdog',
                '.sherpa', '.ssl', '.certs', '.mcp_store']
    skipped_db_dirs = ['.local/state/ci-check', '.config/herdr-web-ui', '.config/Code',
                       '.config/go', '.codex/plugins/cache', '.codex/plugins/marketplaces']
    for relative in db_roots:
        root = HOME / relative
        if not root.exists():
            continue
        for directory, dirs, names in os.walk(root, followlinks=False):
            current = Path(directory)
            dirs[:] = [name for name in dirs if name not in ('node_modules', '.build', '__pycache__')
                       and str((current / name).relative_to(HOME)) not in skipped_db_dirs]
            for name in names:
                p = current / name
                if p.is_symlink() or p.suffix not in ('.sqlite', '.sqlite3', '.db'):
                    continue
                try:
                    with p.open('rb') as f:
                        header = f.read(16)
                    if header == b'SQLite format 3\x00':
                        databases.append(sqlite_copy(p))
                    else:
                        target = RUN / 'databases/home' / p.relative_to(HOME)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(p, target)
                        databases.append({'path': str(p.relative_to(HOME)), 'size': target.stat().st_size,
                                          'kind': 'non-sqlite-file-copy'})
                except Exception as e:
                    ERRORS.append('Database ' + str(p.relative_to(HOME)) + ': ' + str(e))
    write_json(REC / 'databases.json', databases)
    mounts = []
    try:
        mounts = json_command(['/usr/bin/arc', 'mount', '--list', '--json'], HOME / 'arcadia')
        write_json(REC / 'arc-mounts.json', mounts)
    except Exception as e:
        ERRORS.append('Arc inventory: ' + str(e))
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        exports = list(pool.map(export_mount, [m for m in mounts if m['status'] == 'mounted']))
    for item in exports:
        if not item['complete']:
            ERRORS.extend(item['errors'])
    # Keep the recovery tools themselves independently of the repository/Arc mount.
    source_code = globals().get('SOURCE_CODE')
    if source_code is None:
        source_code = Path(__file__).read_text()
    (REC / 'collector.py').write_text(source_code)
    manifest = {'schema': 1, 'captured_at': time.time(), 'source_home': str(HOME),
                'hostname': os.uname().nodename, 'roots': sorted(set(roots)),
                'databases': databases, 'arc_exports': exports,
                'skipped_large_home_files': skipped, 'errors': ERRORS,
                'complete': not ERRORS,
                'consistency': 'SQLite online snapshots; Arc HEAD checked before/after; live files captured without stopping agents'}
    write_json(REC / 'manifest.json', manifest)
    print(json.dumps({'run_dir': str(RUN), 'manifest': manifest}))


if __name__ == '__main__':
    main()
