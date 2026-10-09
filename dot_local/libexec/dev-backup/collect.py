#!/usr/bin/env python3
"""Immutable export of Herdr history, archive HTML and managed Codex conversations."""
import concurrent.futures
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import time
import uuid

os.umask(0o077)
HOME = Path.home()
RUN = HOME / '.cache/dev-backup' / ('run-' + uuid.uuid4().hex)
RUN.mkdir(parents=True)
REC = RUN / 'recovery'
REC.mkdir()
ERRORS, WARNINGS = [], []


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


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def sqlite_copy(source):
    relative = source.relative_to(HOME)
    target = RUN / 'home' / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    def progress(status, remaining, total):
        if time.monotonic() - started > 120:
            raise TimeoutError('SQLite online backup exceeded 120 seconds')
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=10)) as src:
        with closing(sqlite3.connect(target)) as dst:
            src.backup(dst, pages=1024, progress=progress, sleep=0.05)
            dst.execute('PRAGMA journal_mode=DELETE')
            if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('Invalid SQLite snapshot: ' + str(relative))
    return {'path': str(relative), 'size': target.stat().st_size, 'quick_check': 'ok'}


def safe_relative(name):
    p = Path(name)
    if p.is_absolute() or '..' in p.parts:
        raise ValueError('Unsafe relative path: ' + name)
    return p


def status_paths(value):
    if isinstance(value, dict):
        if isinstance(value.get('path'), str):
            yield value['path']
        for item in value.values():
            yield from status_paths(item)
    elif isinstance(value, list):
        for item in value:
            yield from status_paths(item)


def choose_live(record, agents):
    name, pane = record['agent'].get('name'), record['herdr'].get('agent_pane_id')
    matches = [a for a in agents if a.get('name') == name and a.get('pane_id') == pane]
    if len(matches) != 1 or matches[0].get('agent') != 'codex':
        raise RuntimeError('Cannot uniquely identify managed Codex agent: ' + str(name))
    sid = matches[0].get('agent_session', {}).get('value')
    if not sid or matches[0].get('agent_session', {}).get('kind') != 'id':
        raise RuntimeError('Missing native session ID: ' + str(name))
    uuid.UUID(sid)
    return matches[0], sid


def session_copy(source, sid):
    """Freeze the initial byte length and omit an unfinished final JSONL record."""
    target = RUN / 'home' / source.relative_to(HOME)
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open('rb') as src, target.open('wb') as dst:
        remaining = os.fstat(src.fileno()).st_size
        while remaining:
            block = src.read(min(remaining, 1024 * 1024))
            if not block:
                raise RuntimeError('Session truncated while copying: ' + sid)
            dst.write(block)
            remaining -= len(block)
    size, dropped = target.stat().st_size, 0
    with target.open('r+b') as f:
        end = size
        while end:
            start = max(0, end - 65536)
            f.seek(start)
            index = f.read(end - start).rfind(b'\n')
            if index >= 0:
                length = start + index + 1
                dropped = size - length
                f.truncate(length)
                break
            end = start
        if not end:
            raise RuntimeError('No complete session records: ' + sid)
    first, malformed = None, 0
    with target.open('rb') as f:
        for line in f:
            if line.strip():
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeError):
                    if first is None:
                        raise RuntimeError('Invalid session metadata: ' + sid)
                    malformed += 1
                    continue
                if first is None:
                    first = value
    if not first or first.get('type') != 'session_meta' or first.get('payload', {}).get('id') != sid:
        raise RuntimeError('Native session identity mismatch: ' + sid)
    if malformed:
        WARNINGS.append('Session ' + sid + ': ' + str(malformed) + ' pre-existing malformed JSONL lines preserved verbatim; native readability must be checked')
    return {'id': sid, 'path': str(source.relative_to(HOME)), 'sha256': digest(target),
            'size': target.stat().st_size, 'unfinished_bytes_omitted': dropped,
            'malformed_jsonl_lines': malformed}


def trim_database(path, ids):
    with closing(sqlite3.connect(path)) as db:
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('CREATE TEMP TABLE recovery_ids(id TEXT PRIMARY KEY)')
        db.executemany('INSERT INTO recovery_ids VALUES (?)', [(x,) for x in ids])
        for table, in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            quoted = '"' + table.replace('"', '""') + '"'
            columns = {x[1] for x in db.execute('PRAGMA table_info(' + quoted + ')')}
            if table == 'threads':
                db.execute('DELETE FROM threads WHERE id NOT IN (SELECT id FROM recovery_ids)')
            elif 'thread_id' in columns:
                db.execute('DELETE FROM ' + quoted + ' WHERE thread_id NOT IN (SELECT id FROM recovery_ids)')
            elif table == 'thread_spawn_edges':
                db.execute('DELETE FROM thread_spawn_edges WHERE parent_thread_id NOT IN (SELECT id FROM recovery_ids) OR child_thread_id NOT IN (SELECT id FROM recovery_ids)')
            elif table in ('remote_control_enrollments', 'external_agent_config_imports',
                           'rollout_migration_skipped_rollouts', 'project_idempotency_keys'):
                db.execute('DELETE FROM ' + quoted)
        db.commit()
        db.execute('VACUUM')
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('Database invalid after selecting conversations')


def export_arc(root, cwds):
    name = re.sub('[^a-zA-Z0-9._-]', '_', root.name)
    folder = REC / 'arc' / name
    folder.mkdir(parents=True)
    record = {'mount': str(root), 'name': name, 'warnings': [], 'diff_complete': False}
    try:
        info = json_command(['/usr/bin/arc', 'info', '--json'], root)
        if not info.get('hash') or not info.get('branch'):
            raise RuntimeError('Arc did not return branch and HEAD')
        record.update(info=info, head_complete=True)
        write_json(folder / 'info.json', info)
    except Exception as e:
        record.update(head_complete=False, error=str(e))
        write_json(folder / 'manifest.json', record)
        return record
    try:
        status = json_command(['/usr/bin/arc', 'status', '--json', '-u', 'all'], root)
        write_json(folder / 'status.json', status)
        for label, args in [('staged.patch', ['--cached']), ('unstaged.patch', [])]:
            (folder / label).write_bytes(command(['/usr/bin/arc', 'diff', '--git', '--binary', '--no-color', *args], root, timeout=90))
        files, total = [], 0
        for value in sorted(set(status_paths(status))):
            relative = safe_relative(value)
            source, target = root / relative, folder / 'files' / relative
            try:
                mode = source.lstat().st_mode
            except FileNotFoundError:
                files.append({'path': value, 'deleted': True})
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if stat.S_ISREG(mode):
                size = source.stat().st_size
                if size > 64 * 1024**2 or total + size > 256 * 1024**2:
                    raise RuntimeError('Optional diff exceeds size bound: ' + value)
                shutil.copy2(source, target)
                files.append({'path': value, 'sha256': digest(target), 'size': size})
                total += size
            elif stat.S_ISLNK(mode):
                target.symlink_to(os.readlink(source))
                files.append({'path': value, 'symlink': os.readlink(source)})
            else:
                raise RuntimeError('Unexpanded/non-file Arc change: ' + value)
        write_json(folder / 'files.json', files)
        record['files'] = len(files)
        for cwd in sorted(set(cwds)):
            relative = Path(cwd).relative_to(root)
            source = root / relative / '.herdr'
            if source.is_dir():
                shutil.copytree(source, folder / 'instructions' / relative / '.herdr', symlinks=True,
                                ignore=shutil.ignore_patterns('node_modules', '__pycache__', 'logs', 'log'))
        after = json_command(['/usr/bin/arc', 'info', '--json'], root)
        if info['hash'] != after['hash'] or info['branch'] != after['branch']:
            raise RuntimeError('HEAD changed during optional diff capture')
        record['diff_complete'] = True
    except Exception as e:
        record['warnings'].append(str(e))
    write_json(folder / 'manifest.json', record)
    return record


def manual_units():
    inventory, managed = [], set()
    chezmoi = shutil.which('chezmoi')
    if chezmoi:
        try:
            managed = set(command([chezmoi, 'managed', '--include', 'files']).decode().splitlines())
        except Exception as e:
            WARNINGS.append('Unit classification failed; preserve conservatively: ' + str(e))
    for source in sorted((HOME / '.config/systemd/user').rglob('*')):
        if not source.is_file() or '.wants' in str(source.parent):
            continue
        relative = str(source.relative_to(HOME))
        tracked, changed = relative in managed, False
        if tracked:
            try:
                changed = bool(command([chezmoi, 'diff', str(source)]))
            except Exception:
                changed = True
        entry = {'path': relative, 'chezmoi_managed': tracked, 'local_override': changed,
                 'saved': not tracked or changed}
        if entry['saved']:
            target = RUN / 'home' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            entry['sha256'] = digest(target)
            for token in re.findall(re.escape(str(HOME)) + r'/[^\s"\']+', source.read_text(errors='replace')):
                dependency = Path(token.rstrip(';'))
                rel = str(dependency.relative_to(HOME))
                if rel in managed or rel.startswith(('arcadia/', 'arcadia-wt/')):
                    continue
                if dependency.is_file() and dependency.stat().st_size <= 2 * 1024**2:
                    dst = RUN / 'home' / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dependency, dst)
                    entry.setdefault('dependencies', []).append(rel)
        inventory.append(entry)
    write_json(REC / 'manual-units.json', inventory)


def main():
    import yaml
    history = []
    source_history = HOME / '.config/herdr-history'
    if not source_history.is_dir():
        raise RuntimeError('Authoritative Herdr history missing')
    shutil.copytree(source_history, RUN / 'home/.config/herdr-history')
    for path in sorted((RUN / 'home/.config/herdr-history').glob('*.yaml')):
        history.append({'filename': path.name, 'record': yaml.safe_load(path.read_text())})
    write_json(REC / 'history.json', history)
    agents = json_command([str(HOME / '.local/bin/herdr'), 'agent', 'list'])
    workspaces = json_command([str(HOME / '.local/bin/herdr'), 'workspace', 'list'])
    write_json(REC / 'herdr-agents.json', agents)
    write_json(REC / 'herdr-workspaces.json', workspaces)
    plans, selected = [], set()
    for item in history:
        record = item['record']
        if record.get('lifecycle_state') != 'active' or record['agent'].get('kind') != 'codex':
            continue
        try:
            live, sid = choose_live(record, agents['result']['agents'])
            root = Path(record['vcs']['worktree_path'])
            cwd = Path(live.get('foreground_cwd') or live['cwd'])
            root.relative_to(HOME)
            relative = cwd.relative_to(root)
            plans.append({'history_file': item['filename'], 'agent_name': record['agent']['name'],
                          'session_id': sid, 'record_type': record['record_type'],
                          'source_root': str(root), 'source_cwd': str(cwd), 'cwd_relative': str(relative),
                          'story': record['story'], 'task': record['task'], 'project': record['project'],
                          'herdr': record['herdr'], 'agent': record['agent']})
            selected.add(sid)
        except Exception as e:
            ERRORS.append(str(e))
    if any(x['record'].get('lifecycle_state') in ('creating', 'archiving') for x in history):
        WARNINGS.append('Unfinished lifecycle operation in history: inspect before recovery')
    archives = HOME / '.local/share/herdr-transcripts'
    if archives.exists():
        shutil.copytree(archives, RUN / 'home/.local/share/herdr-transcripts')
    for item in history:
        record = item['record']
        if record.get('lifecycle_state') == 'archived' and not record.get('transcript'):
            ERRORS.append('Archived record without transcript: ' + item['filename'])
        for key, path_key, hash_key in [('transcript', 'local_html_path', 'sha256'),
                                       ('transcript_bundle', 'index_path', 'index_sha256')]:
            value = record.get(key) or {}
            if value.get(path_key):
                try:
                    relative = Path(value[path_key]).relative_to(HOME)
                    expected = value.get(hash_key) or value.get('sha256')
                    if not expected or digest(RUN / 'home' / relative) != expected:
                        raise RuntimeError('Archive checksum mismatch: ' + str(relative))
                except Exception as e:
                    ERRORS.append(str(e))
    databases = []
    for pattern in ('state_*.sqlite', 'thread_history_*.sqlite', 'goals_*.sqlite', 'queue_*.sqlite'):
        for path in sorted((HOME / '.codex').glob(pattern)):
            try:
                databases.append(sqlite_copy(path))
            except Exception as e:
                ERRORS.append(str(e))
    metadata = {}
    for entry in databases:
        if not Path(entry['path']).name.startswith('state_'):
            continue
        with closing(sqlite3.connect(RUN / 'home' / entry['path'])) as db:
            db.row_factory = sqlite3.Row
            metadata.update({row['id']: dict(row) for row in db.execute('SELECT * FROM threads')})
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'thread_spawn_edges' in tables:
                edges = list(db.execute('SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges'))
                while True:
                    added = {child for parent, child in edges if parent in selected} - selected
                    if not added:
                        break
                    selected.update(added)
    sessions = []
    for sid in sorted(selected):
        try:
            path = Path(metadata.get(sid, {}).get('rollout_path') or '')
            if not path.is_file() or not path.name.endswith(sid + '.jsonl'):
                candidates = list((HOME / '.codex/sessions').rglob('*' + sid + '.jsonl'))
                if len(candidates) != 1:
                    raise RuntimeError('Native rollout missing/ambiguous: ' + sid)
                path = candidates[0]
            sessions.append(session_copy(path, sid))
        except Exception as e:
            ERRORS.append('Session ' + sid + ': ' + str(e))
    for entry in databases:
        path = RUN / 'home' / entry['path']
        try:
            trim_database(path, selected)
            entry.update(size=path.stat().st_size, sha256=digest(path))
        except Exception as e:
            ERRORS.append(str(e))
    write_json(REC / 'sessions.json', sessions)
    write_json(REC / 'databases.json', databases)
    roots = sorted({p['source_root'] for p in plans})
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        exports = list(pool.map(lambda root: export_arc(Path(root), [p['source_cwd'] for p in plans if p['source_root'] == root]), roots))
    by_root = {x['mount']: x for x in exports}
    for entry in exports:
        if not entry.get('head_complete'):
            ERRORS.append(entry['error'])
        WARNINGS.extend(entry['warnings'])
    by_session = {s['id']: s for s in sessions}
    for plan in plans:
        arc = by_root[plan['source_root']]
        plan.update(arc_export=arc['name'], diff_complete=arc['diff_complete'],
                    arc={k: arc.get('info', {}).get(k) for k in ('branch', 'hash', 'repository')},
                    session=by_session.get(plan['session_id'], {}),
                    observed_model=metadata.get(plan['session_id'], {}).get('model'),
                    observed_reasoning_effort=metadata.get(plan['session_id'], {}).get('reasoning_effort'))
    write_json(REC / 'restore-plan.json', {'schema': 2, 'source_home': str(HOME), 'agents': plans})
    manual_units()
    source_code = globals().get('SOURCE_CODE')
    (REC / 'collector.py').write_text(source_code if source_code is not None else Path(__file__).read_text())
    manifest = {'schema': 2, 'captured_at': time.time(), 'source_home': str(HOME),
                'hostname': os.uname().nodename, 'scope': 'managed-codex-recovery',
                'history_records': len(history), 'managed_agents': len(plans),
                'sessions': sessions, 'databases': databases, 'arc_exports': exports,
                'errors': ERRORS, 'warnings': WARNINGS, 'complete': not ERRORS,
                'consistency': 'SQLite online snapshots; frozen JSONL byte lengths; Arc HEAD; optional dirty diffs; agents remain running'}
    write_json(REC / 'manifest.json', manifest)
    print(json.dumps({'run_dir': str(RUN), 'manifest': manifest}))


if __name__ == '__main__':
    main()
