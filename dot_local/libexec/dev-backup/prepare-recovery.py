#!/usr/bin/env python3
"""Prepare a NEW recovery tree and machine-readable Ops plan; never start agents."""
import argparse
import copy
from contextlib import closing
import datetime
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def safe_path(value):
    p = Path(value)
    if p.is_absolute() or '..' in p.parts:
        raise ValueError('Unsafe backup path: ' + value)
    return p


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def prepare(source, target, arc_root, worktrees_root, codex_home, host_codex_home):
    source, target = source.resolve(), target.resolve()
    if target.exists() or source == target or source in target.parents or target in source.parents:
        raise ValueError('Preparation target must be NEW and separate from the backup')
    for path in (arc_root, worktrees_root, codex_home):
        if path.exists():
            raise ValueError('Recovery destination already exists: ' + str(path))
        if path == Path.home() or path in target.parents or target == path or target in path.parents:
            raise ValueError('Unsafe/overlapping recovery destination: ' + str(path))
    manifest = json.loads((source / 'recovery/manifest.json').read_text())
    if manifest.get('schema') != 2 or not manifest.get('complete'):
        raise ValueError('Need a complete schema-2 managed Codex backup')
    raw_plan = json.loads((source / 'recovery/restore-plan.json').read_text())
    history = json.loads((source / 'recovery/history.json').read_text())
    source_home = raw_plan['source_home']
    agents = copy.deepcopy(raw_plan['agents'])
    names, ids = set(), set()
    roots = {}
    for agent in agents:
        if agent['agent_name'] in names or agent['session_id'] in ids:
            raise ValueError('Duplicate agent name or native session ID')
        names.add(agent['agent_name']); ids.add(agent['session_id'])
        if not agent.get('session') or not agent['arc'].get('hash') or not agent['arc'].get('branch'):
            raise ValueError('Missing session/HEAD for ' + agent['agent_name'])
        root = arc_root if agent['record_type'] == 'story_orchestrator' else worktrees_root / safe_path(agent['arc_export'])
        if agent['source_root'] in roots and roots[agent['source_root']] != root:
            raise ValueError('Ambiguous source worktree mapping')
        roots[agent['source_root']] = root
    mappings = {**{old: str(new) for old, new in roots.items()},
                source_home + '/.codex': str(codex_home), source_home: str(Path.home())}
    def relocate(value):
        if isinstance(value, dict):
            return {k: relocate(v) for k, v in value.items()}
        if isinstance(value, list):
            return [relocate(v) for v in value]
        if isinstance(value, str):
            for old in sorted(mappings, key=len, reverse=True):
                if value == old or value.startswith(old + '/'):
                    return mappings[old] + value[len(old):]
        return value
    # Verify all immutable selected files before creating preparation output.
    for item in [*manifest['sessions'], *manifest['databases']]:
        p = source / 'home' / safe_path(item['path'])
        if p.is_symlink() or not p.is_file() or digest(p) != item['sha256']:
            raise ValueError('Backup checksum mismatch: ' + item['path'])
    target.mkdir(parents=True, mode=0o700)
    prepared_codex = target / 'codex-home'
    shutil.copytree(source / 'home/.codex', prepared_codex)
    for database in manifest['databases']:
        if not Path(database['path']).name.startswith('state_'):
            continue
        path = prepared_codex / Path(database['path']).name
        with closing(sqlite3.connect(path)) as db:
            for sid, rollout, cwd in db.execute('SELECT id, rollout_path, cwd FROM threads').fetchall():
                db.execute('UPDATE threads SET rollout_path=?, cwd=? WHERE id=?',
                           (relocate(rollout), relocate(cwd), sid))
            # Projection offsets still refer to untouched JSONL bytes; never rewrite JSONL.
            db.commit()
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('Rebased SQLite is invalid')
    # Host-native settings/credentials/plugins, shared via links; no Linux config import.
    for name in ('config.toml', 'hooks.json', 'auth.json', 'skills', 'plugins',
                 'models_cache.json', 'herdr-agent-state.sh'):
        host = host_codex_home / name
        if host.exists():
            (prepared_codex / name).symlink_to(host)
    by_record = {a['history_file']: a for a in agents}
    for item in history:
        filename = safe_path(item['filename'])
        if len(filename.parts) != 1:
            raise ValueError('Unsafe history filename')
        original = item['record']
        record = relocate(original)
        agent = by_record.get(item['filename'])
        if agent:
            cwd = roots[agent['source_root']] / safe_path(agent['cwd_relative'])
            model = agent.get('observed_model') or record['agent'].get('model')
            if not model or model == 'default':
                raise ValueError('Exact model unresolved: ' + agent['agent_name'])
            effort = agent.get('observed_reasoning_effort') or (record['agent'].get('model_selection') or {}).get('reasoning_effort')
            args = ['resume', '-C', str(cwd), agent['session_id'], '--model', model]
            if effort and effort not in ('unknown', 'not_applicable'):
                args += ['-c', 'model_reasoning_effort=' + json.dumps(effort)]
            args += ['-c', 'features.fast_mode=false']
            selection = record['agent'].get('model_selection') or {}
            if selection.get('speed_tier') in ('fast', 'ultrafast'):
                raise ValueError('Explicit Fast/Ultrafast record needs host-specific validation: ' + agent['agent_name'])
            command = ['env', 'CODEX_HOME=' + str(codex_home), 'codex', *args]
            agent.update(target_root=str(roots[agent['source_root']]), target_cwd=str(cwd),
                         codex_home=str(codex_home), model=model, reasoning_effort=effort,
                         resume_argv=command, resume_shell=shlex.join(command),
                         parked=(record.get('watchdog_coordination') or {}).get('state') == 'parked')
            record['vcs']['branch'] = agent['arc']['branch']
            record['vcs']['worktree_path'] = agent['target_root']
            record['vcs']['backup_head'] = agent['arc']['hash']
            record['project_cwd'] = str(cwd)
            record['session'].update(id=agent['session_id'], path=str(codex_home / safe_path(agent['session']['path']).relative_to('.codex')))
            record['agent'].update(model=model, resume_args=args, launch_args=args)
            record.setdefault('recovery', {}).update(source_home=source_home, source_history_file=item['filename'],
                                                    captured_at=manifest['captured_at'], state='pending',
                                                    old_herdr_coordinates=original['herdr'])
            # Keep exact old IDs as provenance; Ops replaces them after actual allocation.
            agent['record_type'] = record['record_type']
            agent['permissions'] = record.get('permissions', {})
        dump(target / 'home/.config/herdr-history' / filename, record)  # JSON is valid YAML.
    archives = source / 'home/.local/share/herdr-transcripts'
    if archives.exists():
        shutil.copytree(archives, target / 'home/.local/share/herdr-transcripts')
    units = source / 'home/.config/systemd'
    if units.exists():
        shutil.copytree(units, target / 'manual-linux-config/systemd')
    # Workers first, managers last, so managers see the complete imported inventory.
    agents.sort(key=lambda a: (a['record_type'] == 'story_orchestrator', a['story']['id'], a['task']['id']))
    output = {'schema': 2, 'prepared_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'backup_source': str(source), 'target_home': str(Path.home()),
              'install_codex_home': str(codex_home), 'arc_roots': {k: str(v) for k, v in roots.items()},
              'agents': agents, 'warnings': manifest.get('warnings', []),
              'next': 'Follow RECOVERY.md: mount/checkout exact HEAD, install prepared data, validate history, allocate Herdr IDs, resume exact UUIDs; never launch archived or unmanaged agents.'}
    dump(target / 'ops-plan.json', output)
    return output


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--target', required=True, type=Path)
    parser.add_argument('--arc-root', type=Path, default=Path.home() / 'arcadia-dev-recovery')
    parser.add_argument('--worktrees-root', type=Path, default=Path.home() / 'arcadia-wt-dev-recovery')
    parser.add_argument('--codex-home', type=Path, default=Path.home() / '.codex-dev-recovery')
    parser.add_argument('--host-codex-home', type=Path, default=Path.home() / '.codex')
    args = parser.parse_args()
    plan = prepare(*(p.expanduser().absolute() for p in (args.source, args.target, args.arc_root,
                    args.worktrees_root, args.codex_home, args.host_codex_home)))
    print('Prepared', len(plan['agents']), 'Codex agents;', args.target / 'ops-plan.json')


if __name__ == '__main__':
    main()
