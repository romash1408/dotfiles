#!/usr/bin/env python3
"""Read restored conversations with native Codex, without resume or inference."""
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time


def verify(codex_home, plan_path):
    plan = json.loads(plan_path.read_text())
    responses = queue.Queue()
    with tempfile.TemporaryFile(mode='w+b') as log:
        env = {**os.environ, 'CODEX_HOME': str(codex_home)}
        process = subprocess.Popen(['codex', 'app-server', '--stdio', '-c', 'analytics.enabled=false'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                   text=True, env=env)
        def reader():
            try:
                for line in process.stdout:
                    responses.put(json.loads(line))
            except Exception:
                responses.put({'error': {'message': 'Native protocol reader failed'}})
        thread = threading.Thread(target=reader, daemon=True); thread.start()
        request_id = 0
        def request(method, params):
            nonlocal request_id
            request_id += 1
            process.stdin.write(json.dumps({'id': request_id, 'method': method, 'params': params}) + '\n')
            process.stdin.flush()
            deadline = time.monotonic() + 60
            while True:
                response = responses.get(timeout=max(0.01, deadline - time.monotonic()))
                if response.get('id') != request_id:
                    if time.monotonic() > deadline:
                        raise TimeoutError(method)
                    continue
                if response.get('error'):
                    raise RuntimeError(method + ': ' + response['error'].get('message', 'native error'))
                return response['result']
        results = []
        try:
            request('initialize', {'clientInfo': {'name': 'dev_backup_verify', 'version': '1.0'},
                                    'capabilities': {'experimentalApi': True, 'explicitGatewayOauth': True}})
            process.stdin.write(json.dumps({'method': 'initialized', 'params': {}}) + '\n'); process.stdin.flush()
            for agent in plan['agents']:
                sid = agent['session_id']
                native = request('thread/read', {'threadId': sid, 'includeTurns': False})['thread']
                if native['id'] != sid:
                    raise RuntimeError('Native thread identity mismatch')
                if native.get('cwd') != agent['target_cwd']:
                    raise RuntimeError('Native cwd was not relocated: ' + agent['agent_name'])
                history = request('thread/turns/list', {'threadId': sid, 'limit': 1,
                                                      'sortDirection': 'desc', 'itemsView': 'full'})
                data = history.get('data', [])
                if not data:
                    raise RuntimeError('Native history has no readable turns: ' + agent['agent_name'])
                result = {'agent': agent['agent_name'], 'session_id': sid, 'native_cwd': native.get('cwd'),
                          'last_turn_read': True, 'last_turn_items': len(data[0].get('items', []))}
                print(json.dumps(result), flush=True)
                results.append(result)
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
            process.stdin.close(); process.stdout.close(); thread.join(timeout=1)
        return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-home', required=True, type=Path)
    parser.add_argument('--plan', required=True, type=Path)
    args = parser.parse_args()
    results = verify(args.codex_home.expanduser().resolve(), args.plan.expanduser().resolve())
    print('Native Codex conversation read OK:', len(results))


if __name__ == '__main__':
    main()
