"""Restart-safe, coalesced Slack task updates; no model calls or execution control."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import time
import uuid

INTERVAL = 900

@contextmanager
def _state(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'task-updates.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / 'task-updates.json'
        try:
            state = json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            state = {}
        before = dict(state)
        yield state
        if state == before:
            return
        temp = directory / ('task-updates.' + uuid.uuid4().hex + '.tmp')
        temp.write_text(json.dumps(state))
        os.replace(temp, path)


def begin(directory, *, now=None, direct=False):
    """Start the owner's notification clock before dispatch or the direct answer."""
    token = uuid.uuid4().hex
    with _state(directory) as state:
        state.clear()
        state.update(token=token, direct_pending=direct, due_at=(time.time() if now is None else now)+INTERVAL)
    return token


def current(directory):
    with _state(directory) as state:
        return state.get('token')


def queue(directory, body, *, token, pending=True):
    with _state(directory) as state:
        if token and state.get('token') == token:
            state.update(body=body, pending=pending, revision=state.get('revision',0)+1)


def clear(directory):
    with _state(directory) as state:
        state.clear()


def due(directory, *, active, enabled=True, now=None):
    now = time.time() if now is None else now
    with _state(directory) as state:
        if (not enabled or not state.get('body') or now < state.get('due_at', float('inf'))
                or not (active or state.get('pending'))):
            return None
        return dict(state)


def ack(directory, item, *, now=None):
    with _state(directory) as state:
        if state.get('token') == item.get('token'):
            state['due_at'] = (time.time() if now is None else now)+INTERVAL
            if state.get('revision') == item.get('revision'):
                state['pending'] = False


def direct_pending(directory, token):
    with _state(directory) as state:
        return bool(state.get('token') == token and state.get('direct_pending'))


def answered(directory, token):
    with _state(directory) as state:
        if state.get('token') == token:
            state['direct_pending'] = False
