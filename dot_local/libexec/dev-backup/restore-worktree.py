#!/usr/bin/env python3
"""Restore exported files into a NEW Arc worktree, after checkout/patch application."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('export', type=Path, help='Recovered recovery/arc/<name> directory')
    parser.add_argument('target', type=Path, help='NEW dedicated Arc worktree root')
    parser.add_argument('--apply', action='store_true', help='Explicitly write files/delete recorded paths; default is preview')
    args = parser.parse_args()
    source = args.export.expanduser().resolve()
    target = args.target.expanduser().resolve()
    if source == target or source in target.parents or target in source.parents or target == Path.home():
        parser.error('Source and target must be separate; target cannot be your home')
    records = json.loads((source / 'files.json').read_text())
    for record in records:
        relative = Path(record['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Unsafe exported path')
        src = source / 'files' / relative
        dst = target / relative
        # Do not traverse pre-existing symlinks out of the new tree.
        if not dst.parent.resolve().is_relative_to(target):
            raise ValueError('Target symlink escapes worktree: ' + str(relative))
        if 'sha256' in record and digest(src) != record['sha256']:
            raise ValueError('Backup checksum mismatch: ' + str(relative))
        print('delete' if record.get('deleted') else 'write', relative)
        if not args.apply:
            continue
        if dst.exists() and dst.is_dir() and not dst.is_symlink():
            raise ValueError('Refusing to replace directory: ' + str(dst))
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        if record.get('deleted'):
            continue
        if 'symlink' in record:
            dst.symlink_to(record['symlink'])
        else:
            shutil.copy2(src, dst)
    if args.apply:
        instructions = source / 'instructions'
        if instructions.exists():
            # Do not replace existing role/settings files silently.
            for src in instructions.rglob('*'):
                if src.is_dir() or src.is_symlink():
                    continue
                dst = target / src.relative_to(instructions)
                if dst.exists():
                    print('Existing instruction preserved:', dst)
                    continue
                if not dst.parent.resolve().is_relative_to(target):
                    raise ValueError('Instruction path escapes worktree')
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)


if __name__ == '__main__':
    main()
