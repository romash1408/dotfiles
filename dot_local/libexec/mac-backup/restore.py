#!/usr/bin/env python3
"""Install the verified 2026-11-22 migration on an empty replacement Mac.

Never starts Herdr, agents, timers, resets an overlay, or modifies chezmoi/SSH.
The seven already-unmounted Arc stores remain preserved in the extracted archive.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
from contextlib import closing

import backup as b

SNAPSHOT = '2026-11-22T16:30:05.125545+00:00'
DATA = ['.codex', '.claude', '.claude.json', '.agents',
        'Library/Application Support/Codex', 'Library/Application Support/com.openai.codex',
        'Library/Application Support/Claude', '.config/herdr', '.config/herdr-history',
        '.config/herdr-web-ui', '.local/share/herdr-transcripts',
        '.local/state/herdr-ops-remote', '.local/state/herdr-watchers',
        '.local/state/direct-test', '.local/share/direct-test-transport']
JOURNAL = b.STATE / 'restore-install.json'
BASELINE = 'work-mac-20261122T125945Z-complete-pinned'


def run(args, cwd=None, timeout=180):
    try:
        return b.execute(list(map(str, args)), cwd, timeout)
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or b'').decode(errors='replace')[-2000:]
        raise RuntimeError(str(e) + '\n' + detail) from e


def mapped(value, mapping):
    if isinstance(value, str):
        for old, new in sorted(mapping.items(), key=lambda item: -len(item[0])):
            if value == old or value.startswith(old + '/'):
                return new + value[len(old):]
        return value
    if isinstance(value, dict):
        return {mapped(k, mapping): mapped(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [mapped(v, mapping) for v in value]
    return value


def canonical(value):
    if isinstance(value, dict):
        return {k: canonical(v) for k, v in value.items()}
    if isinstance(value, list):
        return sorted([canonical(v) for v in value], key=lambda v: json.dumps(v, sort_keys=True))
    return value


def safe_path(root, relative):
    p = Path(relative)
    if p.is_absolute() or '..' in p.parts:
        raise ValueError('Unsafe restore path: ' + str(p))
    return root / p


def install_tree(source, target):
    if not source.exists() and not source.is_symlink():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
    if source.is_symlink():
        target.symlink_to(os.readlink(source))
    elif source.is_dir():
        shutil.copytree(source, target, symlinks=True)
    else:
        shutil.copy2(source, target)


def install_missing(source, target):
    if not source.exists() and not source.is_symlink():
        return
    if not target.exists() and not target.is_symlink():
        install_tree(source, target)
    elif source.is_dir() and not source.is_symlink() and target.is_dir() and not target.is_symlink():
        for child in source.iterdir():
            install_missing(child, target / child.name)


def restore_older_claude(manifest, journal):
    if journal.get('baseline_supplement_done'):
        return
    config = json.loads((b.CONFIG / 'config.json').read_text())
    baseline = json.loads(b.borg(config, 'extract', '--stdout', '::' + BASELINE,
                                 'recovery/manifest.json', timeout=120).stdout)
    current = {r['path'] for r in manifest['files']}
    missing = [r for r in baseline['files'] if r['path'].startswith('.claude/')
               and r['path'].endswith('.jsonl') and r['path'] not in current]
    if missing:
        staging = b.STATE / 'older-claude-supplement'; staging.mkdir(exist_ok=True, mode=0o700)
        b.borg(config, 'extract', '--noxattrs', '::' + BASELINE,
               *['home/' + r['path'] for r in missing], cwd=staging, timeout=180)
        for row in missing:
            src = safe_path(staging / 'home', row['path']); dst = safe_path(b.HOME, row['path'])
            if b.digest(src) != row['sha256']:
                raise RuntimeError('Older Claude transcript checksum mismatch')
            if not dst.exists():
                install_tree(src, dst)
        print('Recovered older Claude transcripts absent from final snapshot: ' + str(len(missing)), flush=True)
    journal['baseline_supplement_done'] = True; b.dump(JOURNAL, journal)


def check_no_writers():
    import cutover
    remaining = cutover.writers()
    if remaining:
        raise RuntimeError('Close Codex/Claude/ChatGPT and Herdr on the NEW Mac first; PIDs: '
                           + ', '.join(str(v['pid']) for v in remaining))
    if cutover.herdr_running():
        raise RuntimeError('Stop the NEW Mac Herdr server before installing session databases')


def restore_files(folder, target):
    for row in json.loads((folder / 'files.json').read_text()):
        src = safe_path(folder / 'files', row['path'])
        dst = safe_path(target, row['path'])
        if row['deleted']:
            if dst.is_symlink() or dst.is_file():
                dst.unlink()
            elif dst.exists():
                raise RuntimeError('Refusing to remove a directory: ' + str(dst))
            continue
        if row.get('sha256') and b.digest(src) != row['sha256']:
            raise RuntimeError('Corrupted changed-file backup: ' + row['path'])
        install_tree(src, dst)


def restore_arc(archive, journal):
    root = archive / 'recovery/arc'
    records = [json.loads(p.read_text()) for p in root.glob('*/manifest.json')]
    active = [r for r in records if r.get('capture_mode') != 'unmounted-overlay']
    active.sort(key=lambda r: (r['target'] != str(b.HOME / 'arcadia'), r['target']))
    for r in active:
        if not r['complete'] or (root / r['export'] / 'staged.patch').stat().st_size:
            raise RuntimeError('Incomplete or staged Arc state requires separate restoration: ' + r['source'])
    shared = None
    for n, r in enumerate(active, 1):
        target = Path(r['target']); folder = root / r['export']; info = r['info']
        print(f"Arc {n}/{len(active)}: {target.name}", flush=True)
        if journal.setdefault('arc', {}).get(str(target)) == 'verified':
            current = json.loads(run(['arc', 'info', '--json'], target).stdout)
            if (current['hash'], current['branch']) != (info['hash'], info['branch']):
                raise RuntimeError('Previously restored Arc HEAD changed: ' + str(target))
        else:
            mounts = json.loads(run(['arc', 'mount', '--list', '--json']).stdout)
            entry = next((m for m in mounts if m['mount'] == str(target)), None)
            owned = journal['arc'].get(str(target)) == 'installing'
            if entry is None:
                if target.exists() and any(target.iterdir()):
                    raise RuntimeError('Nonempty unregistered Arc target: ' + str(target))
                journal['arc'][str(target)] = 'installing'; b.dump(JOURNAL, journal)
                target.parent.mkdir(parents=True, exist_ok=True)
                args = ['arc', 'mount', str(target)]
                if shared:
                    args += ['--object-store', shared]
                run(args)
                owned = True
            elif entry['status'] == 'unmounted':
                run(['arc', 'mount', str(target)])
            if not owned:
                state = json.loads(run(['arc', 'status', '--json', '-u', 'all'], target).stdout)
                if any(state.get('status', {}).values()):
                    raise RuntimeError('New Mac Arc copy has changes; refusing to replace: ' + str(target))
                journal['arc'][str(target)] = 'installing'; b.dump(JOURNAL, journal)
            current = json.loads(run(['arc', 'info', '--json'], target).stdout)
            if current['hash'] != info['hash'] or current['branch'] != info['branch']:
                run(['arc', 'checkout', info['hash']], target)
                current = json.loads(run(['arc', 'info', '--json'], target).stdout)
                if current['branch'] != info['branch']:
                    # Only a fresh/clean, explicitly empty replacement copy.
                    run(['arc', 'branch', '-f', info['branch'], info['hash']], target)
                    run(['arc', 'checkout', info['branch']], target)
            restore_files(folder, target)
        current = json.loads(run(['arc', 'info', '--json'], target).stdout)
        state = json.loads(run(['arc', 'status', '--json', '-u', 'all'], target).stdout)
        expected = json.loads((folder / 'status.json').read_text())
        if (current['hash'], current['branch']) != (info['hash'], info['branch']) or canonical(state) != canonical(expected):
            raise RuntimeError('Arc branch/HEAD/status verification failed: ' + str(target))
        for row in json.loads((folder / 'files.json').read_text()):
            dst = safe_path(target, row['path'])
            if row.get('sha256') and b.digest(dst) != row['sha256']:
                raise RuntimeError('Restored file checksum mismatch: ' + str(dst))
        journal['arc'][str(target)] = 'verified'; b.dump(JOURNAL, journal)
        mounts = json.loads(run(['arc', 'mount', '--list', '--json']).stdout)
        entry = next(m for m in mounts if m['mount'] == str(target))
        if shared is None:
            shared = entry['object-store']
        elif entry['object-store'] != shared:
            raise RuntimeError('Arc copy does not share the main object store: ' + str(target))
    return {r['source']: r['target'] for r in records if r['source'] != r['target']}, active


def restore_projects(inventory, journal):
    for project in json.loads(inventory.read_text()):
        target = safe_path(b.HOME, project['path'])
        print('Git project: ' + project['path'], flush=True)
        owned = journal.setdefault('git', {}).get(project['path']) == 'installing'
        if not target.exists():
            journal['git'][project['path']] = 'installing'; b.dump(JOURNAL, journal)
            target.parent.mkdir(parents=True, exist_ok=True)
            run(['git', 'clone', '--filter=blob:none', '--no-checkout', project['origin'], str(target)], timeout=1200)
            owned = True
        if owned:
            run(['git', 'checkout', '-f', '-B', project['branch'], project['head']], target, timeout=300)
        else:
            head = run(['git', 'rev-parse', 'HEAD'], target).stdout.decode().strip()
            if head != project['head']:
                raise RuntimeError('Existing Git checkout differs from migration source: ' + str(target))
        if run(['git', 'status', '--porcelain=v1', '-uall'], target).stdout.strip():
            raise RuntimeError('Git project verification found unexpected changes: ' + str(target))
        journal['git'][project['path']] = 'verified'; b.dump(JOURNAL, journal)


def remap_metadata(mapping):
    import yaml
    for rel in ['.config/herdr/session.json', '.claude.json']:
        p = b.HOME / rel
        if p.is_file():
            b.dump(p, mapped(json.loads(p.read_text()), mapping))
    for p in (b.HOME / '.config/herdr-history').glob('*.yaml'):
        p.write_text(yaml.safe_dump(mapped(yaml.safe_load(p.read_text()), mapping), allow_unicode=True, sort_keys=False))
    for database in (b.HOME / '.codex').glob('state_*.sqlite'):
        with closing(sqlite3.connect(database)) as db:
            for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                quoted = '"' + table.replace('"', '""') + '"'
                columns = [r[1] for r in db.execute('PRAGMA table_info(' + quoted + ')')]
                for col in ('cwd', 'project_cwd', 'root_path'):
                    if col in columns:
                        for (value,) in db.execute(f'SELECT DISTINCT "{col}" FROM {quoted}'):
                            dest = mapped(value, mapping)
                            if dest != value:
                                db.execute(f'UPDATE {quoted} SET "{col}"=? WHERE "{col}"=?', (dest, value))
            db.commit()
    config = b.HOME / '.codex/config.toml'
    if config.is_file():
        def project_key(match):
            old = json.loads('"' + match.group(2) + '"') if match.group(1) == '"' else match.group(2)
            return '[projects.' + json.dumps(mapped(old, mapping), ensure_ascii=False) + ']'
        text = re.sub(r'^\[projects\.([\"\x27])(.*?)\1\]', project_key,
                      config.read_text(), flags=re.MULTILINE)
        config.write_text(text)
    # Claude finds CLI conversations by encoded project directory, not only UUID.
    projects = b.HOME / '.claude/projects'
    encode = lambda value: re.sub(r'[^A-Za-z0-9]', '-', value)
    for p in sorted(projects.glob('*')):
        for old, new in mapping.items():
            prefix = encode(old)
            if p.name == prefix or p.name.startswith(prefix + '-'):
                target = p.with_name(encode(new) + p.name[len(prefix):])
                if target.exists():
                    raise RuntimeError('Claude project directory collision: ' + str(target))
                p.rename(target); break


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from', dest='archive', type=Path, required=True)
    parser.add_argument('--projects', type=Path, required=True)
    parser.add_argument('--empty-machine', action='store_true', required=True)
    args = parser.parse_args()
    archive = args.archive.expanduser().absolute()
    manifest = json.loads((archive / 'recovery/manifest.json').read_text())
    if not manifest['complete'] or manifest['captured_at'] != SNAPSHOT or manifest['source_home'] != str(b.HOME):
        raise RuntimeError('This installer requires the verified 2026-11-22 snapshot and /Users/dubiner')
    if socket.gethostname() == manifest['source_host']:
        raise RuntimeError('Refusing to install onto the SOURCE Mac')
    import yaml  # fail before mutations if chezmoi agent-tools venv is absent
    check_no_writers()
    b.STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    journal = json.loads(JOURNAL.read_text()) if JOURNAL.exists() else {'snapshot': SNAPSHOT}
    if journal['snapshot'] != SNAPSHOT:
        raise RuntimeError('Different restore operation already has a journal')
    if not JOURNAL.exists():
        for root in (b.HOME / '.codex/sessions', b.HOME / '.claude/projects'):
            if any(root.rglob('*.jsonl')):
                raise RuntimeError('Existing sessions found; this installer is for an empty Mac only')
        b.dump(JOURNAL, journal)
    if not journal.get('sessions_installed'):
        print('Installing Codex/Claude/Herdr data into standard directories', flush=True)
        for relative in DATA:
            install_tree(archive / 'home' / relative, b.HOME / relative)
        # Keep current chezmoi scripts, fill in hand-installed helpers from the
        # old Mac (hooks and native resume commands can depend on them).
        for relative in ('.local/bin', '.local/lib', '.local/libexec'):
            install_missing(archive / 'home' / relative, b.HOME / relative)
        journal['sessions_installed'] = True; b.dump(JOURNAL, journal)
    restore_older_claude(manifest, journal)
    mapping, active = restore_arc(archive, journal)
    restore_projects(args.projects, journal)
    if not journal.get('metadata_remapped'):
        remap_metadata(mapping)
        journal['metadata_remapped'] = True; b.dump(JOURNAL, journal)
    # Historical transcript paths stay intact; only structured metadata is remapped.
    for old, new in mapping.items():
        if not Path(new).exists():
            continue  # preserved inactive stores are not registered/mounted here
        source = Path(old)
        if not source.exists() and not source.is_symlink():
            source.parent.mkdir(parents=True, exist_ok=True); source.symlink_to(new)
    (b.HOME / 'Yandex.Disk.localized').mkdir(exist_ok=True)
    run([b.HOME / '.local/bin/herdr-ensure-arc-mounts'], timeout=300)
    journal.update(status='READY_TO_START_HERDR', at=b.now(), arc_copies=len(active))
    b.dump(JOURNAL, journal)
    print('READY_TO_START_HERDR\nNow run: herdr-service-run\nNo agent, timer or server was started.', flush=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, ImportError, subprocess.SubprocessError) as e:
        print('mac restore: ' + str(e), file=sys.stderr)
        sys.exit(1)
