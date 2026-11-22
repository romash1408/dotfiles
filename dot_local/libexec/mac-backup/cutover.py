#!/usr/bin/env python3
"""User-triggered, one-shot final backup, independent of the Herdr process tree."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import time

import backup as b

LABEL = 'local.mac-backup-cutover'
JOB = b.STATE / 'cutover.plist'
STATUS = b.STATE / 'cutover.json'
LOG = b.HOME / 'Library/Logs/mac-backup-cutover.log'
HERDR = '/opt/homebrew/bin/herdr'
DOMAIN = 'gui/' + str(os.getuid())


def call(args, timeout=120, **kwargs):
    return b.execute(args, timeout=timeout, **kwargs)


def phase(name, **values):
    b.dump(STATUS, {'at': b.now(), **values, 'phase': name})
    print(name, flush=True)


def writers():
    rows = []
    for line in call(['/bin/ps', '-axo', 'pid=,comm=']).stdout.decode().splitlines():
        pid, _, comm = line.strip().partition(' ')
        comm = comm.strip()
        if (Path(comm).name in ('codex', 'codex-app-server', 'claude', 'claude-cli', 'opencode')
                or '/.local/share/claude/versions/' in comm
                or any(comm.endswith('/' + n + '.app/Contents/MacOS/' + n)
                       for n in ('Claude', 'Codex', 'ChatGPT'))):
            rows.append({'pid': int(pid), 'executable': comm})
    return rows


def stop_job(label, timeout=75):
    target = DOMAIN + '/' + label
    deadline = time.monotonic() + timeout
    present = subprocess.run(['/bin/launchctl', 'print', target],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if present:
        call(['/bin/launchctl', 'bootout', target], timeout=timeout)
    # bootout can return while launchd is still draining the job/process group.
    # KeepAlive services disappear only after that asynchronous shutdown finishes.
    while subprocess.run(['/bin/launchctl', 'print', target], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL).returncode == 0:
        if time.monotonic() >= deadline:
            raise RuntimeError('LaunchAgent did not finish unloading within ' + str(timeout) + 's: ' + label)
        time.sleep(.25)


def wait_for_backup(lock, timeout=21600):
    deadline = time.monotonic() + timeout
    reported = False
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if not reported:
                phase('waiting-for-current-backup')
                reported = True
            if time.monotonic() >= deadline:
                raise RuntimeError('Current backup did not finish within 6 hours; it was not interrupted')
            time.sleep(2)


def clear_finished_job():
    result = subprocess.run(['/bin/launchctl', 'print', DOMAIN + '/' + LABEL],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    if result.returncode == 0:
        if re.search(r'^\s*pid = \d+', result.stdout, re.MULTILINE):
            raise RuntimeError('Cutover is already running; inspect status and log')
        stop_job(LABEL)


def check_archive(status):
    if status.get('running') or not status.get('complete') or status.get('errors'):
        raise RuntimeError('Final snapshot is incomplete; READY will not be published')
    if not status.get('archive', '').endswith('-complete'):
        raise RuntimeError('Unexpected final archive name')
    return status['archive']


def herdr_running():
    result = subprocess.run([HERDR, '--session', 'default', 'status', 'server'],
                            capture_output=True, timeout=15)
    if b'status: running' in result.stdout:
        return True
    if b'status:' not in result.stdout and (b.HOME / '.config/herdr/herdr.sock').exists():
        raise RuntimeError('Cannot determine Herdr state; refusing to reuse an older pane map')
    return False


def stopped_herdr_capture():
    root = b.CONFIG / 'cutover'
    native = b.HOME / '.config/herdr/session.json'
    for saved in sorted(root.glob('*'), reverse=True):
        if saved.is_symlink() or not saved.is_dir():
            continue
        after = saved / 'session-after-stop.json'
        if not after.is_file():
            continue
        if not native.is_file() or b.digest(native) != b.digest(after):
            raise RuntimeError('Stopped Herdr state changed since the saved pane map; inspect before continuing')
        for name in ('agents.json', 'metadata.json', 'session-before-stop.json', 'session-after-stop.json'):
            json.loads((saved / name).read_text())
        return saved
    raise RuntimeError('No saved state from a stopped cutover; start Herdr before the first cutover')


def preflight_arc():
    arc = shutil.which('arc')
    if not arc:
        raise RuntimeError('Arc executable is missing; Herdr was not stopped')
    mounts = json.loads(b.execute([arc, 'mount', '--list', '--json'], timeout=20).stdout)
    for mount in mounts:
        if mount.get('status') == 'unmounted':
            continue
        try:
            info = json.loads(b.arc_read([arc, 'info', '--json'], Path(mount['mount']), 20).stdout)
            if not info.get('hash') or not info.get('branch'):
                raise ValueError('Missing branch or HEAD')
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            detail = (getattr(e, 'stderr', None) or b'').decode(errors='replace')[-1200:]
            raise RuntimeError('Arc mount unavailable; Herdr was not stopped: ' + mount['mount']
                               + '; ' + str(e) + '; ' + detail) from e


def publish(value):
    # No raw transcripts, API snapshots or unencrypted secrets in the handoff.
    script = ('import json,os,sys; from pathlib import Path; os.umask(0o077); '
              'v=json.load(sys.stdin); p=Path.home()/"mac-recovery"/"FINAL.json"; '
              'p.parent.mkdir(parents=True,exist_ok=True); '
              'q=p.with_suffix(".next"); q.write_text(json.dumps(v,ensure_ascii=False,indent=2)+"\\n"); '
              'q.replace(p)')
    import shlex
    call(['/usr/bin/ssh', '-o', 'BatchMode=yes', 'backup',
          'python3 -c ' + shlex.quote(script)], input=json.dumps(value).encode(), timeout=90)


def worker():
    # Give the invoking terminal time to print its status command before shutdown.
    time.sleep(3)
    # Disable future hourly starts, but let the current writer finish normally.
    call(['/bin/launchctl', 'disable', DOMAIN + '/local.mac-backup'])
    (b.CONFIG / 'enabled').unlink(missing_ok=True)
    with (b.STATE / 'lock').open('a') as lock:
        wait_for_backup(lock)
        config = json.loads((b.CONFIG / 'config.json').read_text())
        if (b.STATE / 'pending.json').exists():
            phase('finishing-retained-backup')
            b.run_backup(config)
        b.borg(config, 'info', '--lock-wait', '15', timeout=90)
        phase('checking-arc-before-stop')
        preflight_arc()
        cli = [HERDR, '--session', 'default']
        stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
        if herdr_running():
            phase('saving-herdr')
            stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
            saved = b.CONFIG / 'cutover' / stamp
            saved.mkdir(parents=True, mode=0o700)
            # Explicit local/default target, never a focused or remote machine.
            cli = [HERDR, '--session', 'default']
            for name, args in [('live-api', ['api', 'snapshot']),
                               ('agents', ['agent', 'list'])]:
                result = json.loads(call(cli + args, timeout=30).stdout)
                if result.get('error'):
                    raise RuntimeError('Cannot save Herdr ' + name)
                b.dump(saved / (name + '.json'), result)
            agents = json.loads((saved / 'agents.json').read_text())['result']['agents']
            if any(a.get('agent_status') == 'working' for a in agents):
                raise RuntimeError('An agent is still working; wait until it finishes before starting cutover')
            native = b.HOME / '.config/herdr/session.json'
            text = native.read_text()
            json.loads(text)
            missing = [a['pane_id'] for a in agents if (a.get('agent_session') or {}).get('value')
                       and a['agent_session']['value'] not in text]
            if missing:
                raise RuntimeError('Live conversations absent from native Herdr state: ' + ', '.join(missing))
            shutil.copy2(native, saved / 'session-before-stop.json')
            b.dump(saved / 'metadata.json', {'at': b.now(), 'agents': len(agents),
                   'source_commit': call(['git', '-C', str(b.HOME / '.local/share/chezmoi'),
                                          'rev-parse', 'HEAD']).stdout.decode().strip()})
        else:
            saved = stopped_herdr_capture()
            phase('resuming-stopped-cutover', saved=str(saved))
        publish({'ready': False, 'started_at': b.now(), 'source': 'old work Mac',
                 'note': 'Cutover in progress; wait for ready=true and a verified pinned archive'})
        phase('stopping-old-services', saved=str(saved))
        call(['/bin/launchctl', 'disable', DOMAIN + '/local.mac-backup'])
        (b.CONFIG / 'enabled').unlink(missing_ok=True)
        stop_job('local.mac-backup')
        stop_job('local.herdr-web-ui')
        stop_job('local.herdr')
        # Native stop is normally covered by launchd's SIGTERM. If the server was
        # started manually instead, use its public stop command, never kill it.
        sock = b.HOME / '.config/herdr/herdr.sock'
        if sock.exists():
            call(cli + ['server', 'stop'], timeout=45)
        deadline = time.monotonic() + 45
        while writers() and time.monotonic() < deadline:
            time.sleep(1)
        remaining = writers()
        if remaining:
            b.dump(b.STATE / 'cutover-remaining-writers.json', remaining)
            raise RuntimeError('Provider processes remain: close them, then retry; see cutover-remaining-writers.json')
        native = b.HOME / '.config/herdr/session.json'
        if native.exists() and not (saved / 'session-after-stop.json').exists():
            shutil.copy2(native, saved / 'session-after-stop.json')
        phase('capturing-and-uploading', saved=str(saved))
        # No pending snapshot can be reused: capture happens after all writers stop.
        b.run_backup(config)
        status = json.loads((b.STATE / 'status.json').read_text())
        archive = b.pin_archive(config, check_archive(status))
        phase('restoring-for-verification', archive=archive, saved=str(saved))
        restored = b.STATE / ('cutover-restore-' + stamp)
        restored.mkdir(mode=0o700)
        b.borg(config, 'extract', '--lock-wait', '90', '--progress', '--log-json',
               '--noxattrs', '::' + archive, cwd=restored)
        b.verify_export(restored)
        manifest = json.loads((restored / 'recovery/manifest.json').read_text())
        evidence = b.STATE / ('cutover-evidence-' + stamp)
        evidence.mkdir(mode=0o700)
        for name in ('manifest.json', 'arc-mounts.json'):
            shutil.copy2(restored / 'recovery' / name, evidence / name)
        shutil.rmtree(restored)
        ready = {'ready': True, 'at': b.now(), 'archive': archive,
                 'baseline_archive': 'work-mac-20261122T125945Z-complete-pinned',
                 'restore_sha256_sqlite': 'passed', 'counters': manifest['counters'],
                 'arc_count': len(manifest['arc']), 'herdr_state': str(saved.relative_to(b.HOME)),
                 'source_commit': call(['git', '-C', str(b.HOME / '.local/share/chezmoi'), 'rev-parse', 'HEAD']).stdout.decode().strip(),
                 'old_mac_schedule': 'disabled', 'old_mac_herdr_ovis': 'stopped'}
        phase('publishing-handoff', archive=archive)
        publish(ready)
        phase('READY', **ready)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'status', 'worker'])
    args = parser.parse_args()
    b.STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.action == 'status':
        print(STATUS.read_text() if STATUS.exists() else 'Not started')
    elif args.action == 'start':
        clear_finished_job()
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with JOB.open('wb') as f:
            plistlib.dump({'Label': LABEL, 'RunAtLoad': True,
                          'ProgramArguments': [sys.executable, '-u', str(Path(__file__).resolve()), 'worker'],
                          'WorkingDirectory': str(b.HOME),
                          'EnvironmentVariables': {'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'},
                          'StandardOutPath': str(LOG), 'StandardErrorPath': str(LOG)}, f)
        phase('scheduled')
        call(['/bin/launchctl', 'bootstrap', DOMAIN, str(JOB)])
        print('Herdr and Ovis will stop. Check: mac-backup-cutover status\nLog: ' + str(LOG))
    else:
        try:
            worker()
        except Exception as e:
            phase('FAILED', error=str(e), log=str(LOG))
            raise


if __name__ == '__main__':
    main()
