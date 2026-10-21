"""Persistent acceptance contracts for Slack work. CLI is usable from any project cwd."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
import re
from pathlib import Path
import sys
import threading
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = 'cos'

from . import proofs

_lock = threading.RLock()


def _path(run_id):
    from .runner import cos_home, valid_run_id
    if not valid_run_id(run_id):
        raise ValueError('invalid run id')
    directory = cos_home() / 'runs' / run_id
    if not (directory / 'meta.json').is_file():
        raise ValueError('unknown run')
    return directory / 'task.json'


def show(run_id):
    path = _path(run_id)
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError('invalid task contract')
    return data


@contextmanager
def _edit(run_id):
    path = _path(run_id)
    with _lock, path.with_suffix('.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            data = show(run_id)
            yield data
            data['updated_at'] = time.time()
            data['revision'] = data.get('revision', 0) + 1
            temporary = path.with_suffix(f'.{os.getpid()}.{threading.get_ident()}.tmp')
            temporary.write_text(json.dumps(data, indent=2))
            temporary.replace(path)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def contract(run_id, goal, steps, approval=''):
    if not isinstance(goal, str) or not goal.strip() or not isinstance(steps, list) or not 1 <= len(steps) <= 20:
        raise ValueError('goal and 1..20 acceptance steps required')
    ids = set()
    for step in steps:
        if (not isinstance(step, dict) or not isinstance(step.get('id'), str) or not step['id'].strip()
                or step['id'] in ids or not step.get('title') or not isinstance(step.get('proofs'), list)
                or not 1 <= len(step['proofs']) <= 5
                or any(not isinstance(p, dict) or p.get('kind') not in ('pr','workflow','health') for p in step['proofs'])):
            raise ValueError('steps need unique IDs, titles and 1..5 independently checkable proofs')
        ids.add(step['id'])
    with _edit(run_id) as data:
        if data.get('goal') and (data['goal'] != goal or data['steps'] != steps) and not approval.strip():
            raise ValueError('replacing an existing contract needs the owner approval text')
        data.update(goal=goal.strip(), steps=steps, status='open', checks={}, created_at=data.get('created_at', time.time()))
        if approval:
            data.setdefault('scope_approvals', []).append({'at':time.time(),'approval':approval[:1000]})
    return show(run_id)


def _verify(data, spec):
    expected = spec.get('expected')
    version = expected.get('version') if isinstance(expected,dict) else None
    if data.get('release') and spec.get('kind') == 'health' and (not version or version != data['release']['tag']):
        return {'verified':False,'reason':'health proof differs from pinned release tag','checked_at':time.time()}
    return proofs.verify(spec)


def check(run_id, step_id):
    data = show(run_id)
    step = next((s for s in data.get('steps', []) if s['id'] == step_id), None)
    if step is None:
        raise ValueError('unknown acceptance step')
    results = [_verify(data,p) for p in step['proofs']]
    result = {'verified':all(r.get('verified') for r in results), 'checked_at':time.time(), 'proofs':results}
    with _edit(run_id) as current:
        # A slow network check cannot certify a replaced release/contract.
        if current.get('revision') != data.get('revision'):
            raise ValueError('contract changed while evidence was checked; check again')
        current.setdefault('checks', {})[step_id] = result
        if not result['verified']:
            current['status'] = 'open'
            current.pop('completed_at',None)
    return result


def unmet(run_id):
    data = show(run_id)
    if data.get('block'):
        return ['human gate: ' + data['block']]
    out = []
    for step in data.get('steps', []):
        for spec in step['proofs']:
            result = _verify(data,spec)
            if not result.get('verified'):
                out.append(step['id'] + ': ' + str(result.get('reason') or 'evidence unavailable'))
    if show(run_id).get('revision') != data.get('revision'):
        out.append('contract changed during verification')
    return out


def complete(run_id, revision):
    with _edit(run_id) as data:
        if data.get('revision') != revision:
            return False
        data['status'] = 'completed'
        data['completed_at'] = time.time()
    return True


def release(run_id, scope, approval=''):
    if (not isinstance(scope, dict) or not all(k in scope for k in ('tag','source_sha','native','migrations','gates'))
            or not isinstance(scope['tag'], str) or not scope['tag'].strip()
            or scope['tag'].strip().lower() in ('latest','main','master','head')
            or not isinstance(scope['source_sha'], str) or not re.fullmatch(r'(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})',scope['source_sha'])
            or not isinstance(scope['native'], dict) or not isinstance(scope['migrations'], list)
            or not isinstance(scope['gates'], list)):
        raise ValueError('release scope requires exact tag, source_sha, native pins, migrations and sourced gates')
    for gate in scope['gates']:
        if not isinstance(gate, dict) or not gate.get('source') or not gate.get('name'):
            raise ValueError('each release gate needs a name and policy source')
    with _edit(run_id) as data:
        if data.get('release') and data['release'] != scope and not approval.strip():
            raise ValueError('release scope changed; record explicit owner approval')
        if data.get('release') != scope:
            data.update(status='open',checks={})
            data.pop('completed_at',None)
        data['release'] = scope
        if approval:
            data.setdefault('scope_approvals', []).append({'at':time.time(),'approval':approval[:1000]})
    return show(run_id)


def block(run_id, reason):
    if not reason.strip():
        raise ValueError('name the human and concrete gate')
    with _edit(run_id) as data:
        data['block'] = reason[:1000]
    return show(run_id)


def unblock(run_id, approval):
    if not approval.strip():
        raise ValueError('owner approval text required')
    with _edit(run_id) as data:
        data.pop('block', None)
        data.setdefault('scope_approvals', []).append({'at':time.time(),'approval':approval[:1000]})
    return show(run_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('contract','check','show','release','block','unblock'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--run', default=os.environ.get('NEO_RUN_ID'))
        if name == 'contract':
            cmd.add_argument('--goal', required=True)
            cmd.add_argument('--steps', required=True, type=json.loads)
        if name == 'check': cmd.add_argument('--step', required=True)
        if name == 'release': cmd.add_argument('--scope', required=True, type=json.loads)
        if name == 'block': cmd.add_argument('--reason', required=True)
        if name in ('contract','release','unblock'): cmd.add_argument('--approval', default='')
    args = parser.parse_args()
    try:
        if args.command == 'contract': result = contract(args.run,args.goal,args.steps,args.approval)
        elif args.command == 'check': result = check(args.run,args.step)
        elif args.command == 'release': result = release(args.run,args.scope,args.approval)
        elif args.command == 'block': result = block(args.run,args.reason)
        elif args.command == 'unblock': result = unblock(args.run,args.approval)
        else: result = {**show(args.run), 'unmet': unmet(args.run)}
        print(json.dumps(result, indent=2))
        return 1 if args.command == 'check' and not result['verified'] else 0
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
