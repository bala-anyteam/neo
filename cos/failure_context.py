"""Bounded, allowlisted failure explanations; never reflect raw logs into Slack."""
import json
import os
from pathlib import Path
import re


def _directory(run):
    return Path(run.dir) if hasattr(run, 'dir') else Path(run)


def safe_reason(run, error=None):
    directory=_directory(run)
    try:
        with (directory/'meta.json').open('rb') as f:
            raw=f.read(262145)
        meta=json.loads(raw) if len(raw)<=262144 else {}
        if not isinstance(meta,dict): meta={}
    except (OSError, ValueError):
        meta={}
    engine=meta.get('engine') if meta.get('engine') in ('claude','codex') else 'Agent'
    bodies=[]
    try:
        with (directory/'events.jsonl').open('rb') as f:
            size=f.seek(0,2)
            f.seek(max(0,size-65536))
            if size>65536: f.readline()
            for line in f.read(65536).splitlines():
                try: row=json.loads(line)
                except (ValueError,UnicodeError): continue
                if isinstance(row,dict) and row.get('kind') in ('error','result'):
                    if isinstance(meta.get('turns'),int) and row.get('turn') != meta['turns']:
                        continue
                    bodies.append(str(row.get('body',''))[:8000])
    except OSError:
        pass
    if error is not None:
        bodies.append(str(error)[:8000])
    for body in reversed(bodies):
        text=body.lower()
        if 'already executing a turn' in text:
            return 'The existing task is already running; this follow-up was not started. Its execution continues.'
        if 'weekly limit' in text or 'weekly usage limit' in text:
            reset=re.search(r'resets?\s+([A-Za-z]{3,9}\s+\d{1,2}\s+at\s+\d{1,2}:\d{2}(?:am|pm)?(?:\s*\([A-Za-z/_]+\))?)',body,re.I)
            when=' Reported reset: '+reset[1]+'.' if reset else ''
            return f'{engine.title()} weekly usage limit reached; this check could not run.'+when+' Retry after reset or use an authorized engine with capacity.'
        if any(word in text for word in ('not logged in','authentication failed','invalid api key','missing credentials','unauthorized')):
            return f'{engine.title()} authentication or credentials failed; restore its sign-in/credentials, then retry.'
        if 'issue_repo' in text or 'issue repository scope' in text:
            return 'Required issue-repository scope is missing; configure the authorized repository before retrying.'
        if any(word in text for word in ('stream disconnected','connection lost','network error','timed out','overloaded','service unavailable')):
            return f'{engine.title()} connection or provider request failed; inspect connectivity and retry within the existing task.'
        if any(word in text for word in ('executable not found','cli not found','no such file or directory')):
            return 'Required executable or file is unavailable; inspect the trace, restore the dependency/path, then retry.'
    return 'The failure cause is not identified in the recorded engine errors; inspect the trace before retrying.'


def trace_link(run, base=None):
    run_id=_directory(run).name
    if not re.fullmatch(r'[0-9]{8}-[0-9]{6}-[0-9a-f]{6}',run_id):
        return 'Trace unavailable'
    base=base or os.environ.get('NEO_DASHBOARD_URL')
    if not base and os.environ.get('NEO_DASHBOARD_PORT'):
        base='http://'+os.environ.get('NEO_DASHBOARD_HOST','127.0.0.1')+':'+os.environ['NEO_DASHBOARD_PORT']
    return f'<{base.rstrip("/")}/#{run_id}|trace>' if base else f'Run `{run_id}`'
