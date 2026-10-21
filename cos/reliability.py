"""Bounded, deterministic installation measurements; never calls an agent/model."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import re
import subprocess
import threading
from zoneinfo import ZoneInfo

from . import pause, failure_context
from .config import OWNER_MESSAGE_MARK
from .runner import cos_home, pid_alive

IDS = ('continuity', 'release_scope', 'evidence', 'scheduled_jobs', 'approvals', 'communication')
METRICS = ('abandoned_waiting_exits', 'manual_status_nudges', 'automatic_recoveries',
           'completion_refusals', 'evidence_refusals', 'declared_contracts', 'scheduled_watch_runs', 'scheduled_watch_failures', 'progress_replies', 'final_replies')
DEFINITIONS = {
    'abandoned_waiting_exits': 'Terminal Slack waiting result at least15min old at cohort end, no later prompt/recovery/result, no live pid/retry owner or pending continuation; bounded complete event scans only.',
    'manual_status_nudges': 'Slack owner prompt regex status/update/progress/continue/still working/what happened; strips owner marker; excludes known synthetic continuations.',
    'automatic_recoveries': 'Recovery attempts/restorations: positive integer auto_restart, waiting_continuation or continuation_recovered status markers. No proven outcome claim.',
    'completion_refusals': 'Exact data.completion_refused event marker.',
    'evidence_refusals': 'Timestamped loop proof refusal records in bounded evidence-refusals.jsonl tail.',
    'declared_contracts': 'Valid goal+steps task.json by contract created_at (epoch); legacy empty/missing-created_at contracts fall back to parent run created_at.',
    'scheduled_watch_runs': 'Terminal scheduled watch runs by metadata updated_at, fallback created_at.',
    'scheduled_watch_failures': 'Failed scheduled watch runs / scheduled_watch_runs; undefined with zero denominator.',
    'progress_replies': 'Slack run text events, output proxy, not delivery proof.',
    'final_replies': 'Slack run result events, output proxy, not delivery proof.',
}
MAX_RUNS = 2000
MAX_BYTES = 2_000_000
MAX_LINE = 65536
MAX_SCAN_BYTES = 32_000_000
_LOCK = threading.RLock()


@contextmanager
def locked():
    home = cos_home()
    home.mkdir(parents=True, exist_ok=True)
    with _LOCK, (home / 'reliability.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def status():
    try:
        return json.loads((cos_home() / 'reliability.json').read_text())
    except (OSError, ValueError):
        return {}


def save(data):
    path = cos_home() / 'reliability.json'
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def timestamp(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.replace(tzinfo=timezone.utc).timestamp() if dt.tzinfo is None else dt.timestamp()
    except ValueError:
        return 0


def measure(start, end):
    """Events in [start,end); metadata failures use updated_at or created_at."""
    out = dict(start=start, end=end, runs=0, events=0, oversized_lines=0,
               truncated_files=0, run_limit_reached=False, scan_budget_reached=False, metrics=dict.fromkeys(METRICS, 0), failures=[])
    root = cos_home() / 'runs'
    # Deterministic name order, fixed cap; disclose incomplete history rather than guess.
    import heapq
    paths = heapq.nlargest(MAX_RUNS + 1, root.glob('*/meta.json'), key=lambda p: p.parent.name)
    out['run_limit_reached'] = len(paths) > MAX_RUNS
    scanned = 0
    for path in paths[:MAX_RUNS]:
        if scanned >= MAX_SCAN_BYTES:
            out['scan_budget_reached'] = True
            break
        try:
            if path.stat().st_size > MAX_LINE:
                out['truncated_files'] += 1
                continue
            meta = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        human = meta.get('source') == 'slack' and meta.get('kind') not in ('watch', 'reliability')
        sampled = False
        contract_path = path.parent / 'task.json'
        try:
            if contract_path.stat().st_size <= MAX_LINE:
                contract = json.loads(contract_path.read_text())
                if isinstance(contract, dict) and (not contract or (contract.get('goal') and contract.get('steps'))):
                    declared_at = contract.get('created_at')
                    if isinstance(declared_at, (int, float)):
                        contract_time = declared_at
                    else:
                        contract_time = timestamp(declared_at or meta.get('created_at'))
                    if start <= contract_time < end:
                        out['metrics']['declared_contracts'] += 1
                        sampled = True
        except (OSError, ValueError):
            pass
        when = timestamp(meta.get('updated_at') or meta.get('created_at'))
        if meta.get('kind') == 'watch' and meta.get('source') == 'schedule' and meta.get('status') in ('done', 'failed', 'cancelled') and start <= when < end:
            out['metrics']['scheduled_watch_runs'] += 1
            sampled = True
            if meta.get('status') == 'failed':
                out['metrics']['scheduled_watch_failures'] += 1
                out['failures'].append({'id': path.parent.name, 'ts': when, 'title': str(meta.get('title', 'Watch'))[:120]})
        read = 0
        waiting_exit = None
        initial_oversized = out['oversized_lines']
        complete_scan = True
        try:
            with (path.parent / 'events.jsonl').open('rb') as fh:
                read = 0
                while read < min(MAX_BYTES, MAX_SCAN_BYTES - scanned):
                    line = fh.readline(min(MAX_LINE + 1, MAX_BYTES - read, MAX_SCAN_BYTES - scanned - read))
                    if not line:
                        break
                    read += len(line)
                    if not line.endswith(b'\n'):
                        out['oversized_lines'] += 1
                        while read < min(MAX_BYTES, MAX_SCAN_BYTES - scanned) and line and not line.endswith(b'\n'):
                            line = fh.readline(min(MAX_LINE, MAX_BYTES - read, MAX_SCAN_BYTES - scanned - read))
                            read += len(line)
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get('kind') not in ('prompt', 'result', 'status', 'error', 'text') or not start <= timestamp(event.get('ts')) < end:
                        continue
                    sampled = True
                    out['events'] += 1
                    body = str(event.get('body', ''))
                    data = event.get('data') if isinstance(event.get('data'), dict) else {}
                    kind = event['kind']
                    if kind in ('prompt', 'result') or (kind == 'status' and (data.get('waiting_continuation') or data.get('continuation_recovered') or (isinstance(data.get('auto_restart'), int) and data['auto_restart'] > 0))):
                        waiting_exit = None
                    m = out['metrics']
                    if kind == 'status' and (data.get('waiting_continuation') or (isinstance(data.get('auto_restart'), int) and not isinstance(data.get('auto_restart'), bool) and data['auto_restart'] > 0) or data.get('continuation_recovered')):
                        m['automatic_recoveries'] += 1
                    if data.get('completion_refused'):
                        m['completion_refusals'] += 1
                    if not human:
                        continue
                    if kind == 'prompt' and (OWNER_MESSAGE_MARK in body or (body.strip().lower() != 'continue' and not body.startswith('Your last turn ended with Status: waiting'))):
                        owner = body.rsplit(OWNER_MESSAGE_MARK, 1)[-1].strip()
                        if re.search(r'\b(status|update|progress|continue|still working|what happened)\b', owner, re.I):
                            m['manual_status_nudges'] += 1
                    if kind == 'result':
                        m['final_replies'] += 1
                        if re.search(r'^Status:\s*waiting\b', body, re.I | re.M):
                            waiting_exit = timestamp(event.get('ts'))
                    if kind == 'text':
                        m['progress_replies'] += 1
                if read >= min(MAX_BYTES, MAX_SCAN_BYTES - scanned) and fh.read(1):
                    out['truncated_files'] += 1
                    complete_scan = False
        except OSError:
            pass
        if (waiting_exit is not None and end - waiting_exit >= 900 and complete_scan
                and initial_oversized == out['oversized_lines'] and meta.get('status') in ('done', 'failed')
                and not meta.get('continuation') and not pid_alive(meta.get('pid'))
                and not pid_alive(meta.get('retry_owner'))
                and (not meta.get('conclusion') or meta['conclusion'].get('state') == 'waiting')):
            out['metrics']['abandoned_waiting_exits'] += 1
        scanned += read
        out['runs'] += int(sampled)
    # Independent loop proof refusals, bounded to the newest 2MB. Drop a partial first line.
    try:
        with (cos_home() / 'evidence-refusals.jsonl').open('rb') as fh:
            size = fh.seek(0, 2)
            fh.seek(max(0, size - MAX_BYTES))
            if size > MAX_BYTES:
                out['truncated_files'] += 1
                fh.readline(MAX_LINE)
            consumed = 0
            while consumed < MAX_BYTES:
                line = fh.readline(min(MAX_LINE + 1, MAX_BYTES - consumed))
                if not line:
                    break
                consumed += len(line)
                if not line.endswith(b'\n'):
                    out['oversized_lines'] += 1
                    while line and not line.endswith(b'\n') and consumed < MAX_BYTES:
                        line = fh.readline(min(MAX_LINE, MAX_BYTES - consumed))
                        consumed += len(line)
                    continue
                try:
                    incident = json.loads(line)
                except ValueError:
                    continue
                if isinstance(incident, dict) and start <= timestamp(incident.get('ts')) < end:
                    out['metrics']['evidence_refusals'] += 1
    except OSError:
        pass
    denominator = out['metrics']['scheduled_watch_runs']
    out['scheduled_watch_failure_rate'] = out['metrics']['scheduled_watch_failures'] / denominator if denominator else None
    return out


def configure(channel, thread, baseline_days=7, at='18:00', tz='Asia/Kolkata', *, now=None, commit=None):
    now = now or datetime.now(timezone.utc)
    ZoneInfo(tz)
    datetime.strptime(at, '%H:%M')
    if baseline_days <= 0 or not channel or not thread:
        raise ValueError('positive baseline_days and channel/thread required')
    with locked():
        data = status()
        if not data:
            activation = now.timestamp()
            if commit is None:
                commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
            data = dict(activated_at=activation, activation_commit=commit, baseline_days=baseline_days,
                        improvements={key: {'activated_at': activation, 'commit': commit} for key in IDS},
                        baseline=measure(activation - baseline_days * 86400, activation), sent={})
        data.update(channel=channel, thread=thread, at=at, timezone=tz, metric_definitions=DEFINITIONS)
        save(data)
        return data


def report(now=None):
    data = status()
    if not data:
        return {'error': 'not configured'}
    end = (now or datetime.now(timezone.utc)).timestamp()
    activation = data['activated_at']
    exposure = max(0, min(end - activation, data['baseline_days'] * 86400))
    before = measure(activation - exposure, activation)
    after = measure(max(activation, end - exposure), max(activation, end))
    return dict(baseline=before, post=after, exposure_seconds=exposure,
                assessment='insufficient data' if min(before['runs'], after['runs']) < 10 else 'descriptive comparison only',
                limitations='No causal attribution. Counts are proxies, not Slack delivery evidence. Stranded waiting is a15-minute-grace proxy using current metadata; historical live ownership cannot be reconstructed. Human nudge heuristic strips owner context marker and excludes known synthetic continuations and bot reports; unmarked Slack prompts assumed human. Bounded scans may undercount. Naive historical timestamps interpreted as UTC. First-week elapsed exposure compared to equal preceding baseline exposure; thereafter latest seven days compared to fixed seven-day pre-activation baseline. Recovery counts are attempts/restorations, not confirmed successful outcomes.')


def report_text(now=None):
    value = report(now)
    if 'error' in value:
        return value['error']
    a, b = value['baseline'], value['post']
    counts = ', '.join(f"{key if key != 'automatic_recoveries' else 'recovery_attempts/restorations'}: {a['metrics'][key]} → {b['metrics'][key]}" for key in METRICS)
    return (f"Installation reliability — {value['assessment']}\nEqual exposure {value['exposure_seconds'] / 3600:.1f}h; baseline/post runs {a['runs']}/{b['runs']}, events {a['events']}/{b['events']}. Bounded/truncated: {a['truncated_files']}/{b['truncated_files']}; run/scan caps: {a['run_limit_reached'] or a['scan_budget_reached']}/{b['run_limit_reached'] or b['scan_budget_reached']}.\n{counts}\nScheduled failure rate: {a['scheduled_watch_failure_rate']} → {b['scheduled_watch_failure_rate']} (null = no completed watch runs).\n{value['limitations']}")


async def tick(client, now=None):
    now = now or datetime.now(timezone.utc)
    paused = pause.is_paused()
    with locked():
        data = status()
        if not data:
            return
        data.update(last_tick_at=now.timestamp(), last_tick_paused=paused)
        save(data)
    if paused:
        return
    local = now.astimezone(ZoneInfo(data['timezone']))
    due = local.strftime('%H:%M') >= data['at']
    pending = []
    if due:
        key = 'daily:' + local.date().isoformat()
        if key not in data.get('sent', {}):
            pending.append(([key], await asyncio.to_thread(report_text, now)))
    failures = (await asyncio.to_thread(measure, max(data['activated_at'], now.timestamp() - 7 * 86400), now.timestamp()))['failures']
    unsent = [f for f in failures if 'watch:' + f['id'] not in data.get('sent', {})]
    batch = unsent[:20]
    if batch:
        lines = [f"• {f['title']}: {failure_context.safe_reason(cos_home() / 'runs' / f['id'])} "
                 f"{failure_context.trace_link(cos_home() / 'runs' / f['id'])}" for f in batch]
        pending.append((['watch:' + f['id'] for f in batch], 'Scheduled monitoring could not complete (not a confirmed product incident):\n' + '\n'.join(lines)))
    for keys, text in pending:
        if all(key in status().get('sent', {}) for key in keys):
            continue
        response = await client.chat_postMessage(channel=data['channel'], thread_ts=data['thread'], text=text, unfurl_links=False)
        if response is None or response.get('ok') is not True or not response.get('ts'):
            raise RuntimeError('Slack rejected reliability notice')
        with locked():
            current = status()
            for key in keys:
                current.setdefault('sent', {})[key] = now.timestamp()
            current['sent'] = dict(sorted(current['sent'].items(), key=lambda item: item[1])[-2000:])
            save(current)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['configure', 'status', 'report'])
    parser.add_argument('--channel')
    parser.add_argument('--thread')
    parser.add_argument('--baseline-days', type=int, default=7)
    parser.add_argument('--at', default='18:00')
    parser.add_argument('--tz', default='Asia/Kolkata')
    args = parser.parse_args()
    value = configure(args.channel, args.thread, args.baseline_days, args.at, args.tz) if args.command == 'configure' else status() if args.command == 'status' else report()
    print(json.dumps(value, indent=2))


if __name__ == '__main__':
    main()
