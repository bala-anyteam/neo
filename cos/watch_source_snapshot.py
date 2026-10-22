"""Trusted, bounded read-only snapshots for built-in monitoring watches.

Model-generated commands never reach these collectors. Source errors are explicit;
missing data must never be interpreted as a clean check.
"""
from __future__ import annotations
import json
import configparser
from pathlib import Path
import urllib.request
import urllib.parse
import os
import re
import subprocess
from datetime import datetime, timezone

SUPPORTED = frozenset({'sentry', 'prod-logs', 'desk-ci-fixes'})
REPO = 'AnyTeamAI/anyteam-deskie'


def _read(args: list[str], *, json_output: bool = True):
    # No shell, stdin, or inherited model commands. CLI output and model input bounded.
    p = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    if p.returncode:
        raise RuntimeError('read-only CLI request failed')
    if len(p.stdout) > 2_000_000:
        raise RuntimeError('source response exceeds snapshot limit')
    text = p.stdout.decode('utf-8', errors='replace')
    return json.loads(text) if json_output else text[:24000]


def _sentry(path: str):
    token = os.environ.get("SENTRY_AUTH_TOKEN", "").strip()
    if not token:
        config = configparser.ConfigParser(interpolation=None)
        config.read(Path.home() / ".sentryclirc")
        token = config.get("auth", "token", fallback="").strip()
    if not token:
        raise RuntimeError("Sentry authentication unavailable")
    request = urllib.request.Request("https://sentry.io/api/0/" + path,
                                    headers={"Authorization": "Bearer " + token}, method="GET")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None  # Never forward the Sentry credential to a redirect target.
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(request, timeout=30) as response:
        body = response.read(2_000_001)
    if len(body) > 2_000_000:
        raise RuntimeError("Sentry response exceeds snapshot limit")
    return json.loads(body)


def _slack_history(watch: dict) -> dict:
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Slack authentication unavailable")
    try:
        oldest = datetime.fromisoformat(watch.get("last_ok_at", "")).timestamp()
    except (ValueError, TypeError):
        oldest = datetime.now(timezone.utc).timestamp() - 86400
    query = urllib.parse.urlencode({"channel": "C0ALVGSDACF", "oldest": oldest, "limit": 100})
    request = urllib.request.Request("https://slack.com/api/conversations.history?" + query,
                                    headers={"Authorization": "Bearer " + token}, method="GET")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
        body = response.read(2_000_001)
    if len(body) > 2_000_000:
        raise RuntimeError("Slack response exceeds snapshot limit")
    result = json.loads(body)
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("Slack history read failed")
    return result


def collect(watch: dict) -> dict:
    kind = watch['id']
    if kind not in SUPPORTED:
        raise ValueError('No trusted snapshot collector for this watch')
    snapshot = {'watch_id': kind, 'fetched_at': datetime.now(timezone.utc).isoformat(),
                'source_errors': [], 'sources': {}, 'limits': 'Bounded sample; not exhaustive. Individual failed-job logs are limited to the first 24000 characters.'}
    def read(name, args, **kwargs):
        try:
            value = _read(args, **kwargs)
            snapshot['sources'][name] = value
            return value
        except Exception:
            # Never reflect credential-bearing stderr/exception text into model input.
            snapshot['source_errors'].append(name + ': unavailable; check local CLI authentication/access or response limits')
            return None
    now = datetime.now(timezone.utc)
    try:
        since = datetime.fromisoformat(watch.get('last_ok_at', ''))
        if since.tzinfo is None:
            since = since.astimezone(timezone.utc)
    except (ValueError, TypeError):
        since = datetime.fromtimestamp(now.timestamp() - 86400, timezone.utc)
    snapshot['window'] = {'since': since.isoformat(), 'until': now.isoformat()}
    if kind == 'sentry':
        query = urllib.parse.urlencode({"query": "is:unresolved environment:[production,prod] lastSeen:>=" + since.isoformat(),
                                        "limit": 30, "statsPeriod": "14d"})
        try:
            issues = _sentry("organizations/anyteam-ai/issues/?" + query)
            if not isinstance(issues, list):
                raise ValueError("Unexpected Sentry issue response")
            snapshot['sources']['sentry_issues'] = issues
            if len(issues) >= 30:
                snapshot['source_errors'].append('Sentry issue sample saturated at 30; more issues may be omitted')
            if len(issues) > 3:
                snapshot['source_errors'].append('Sentry latest-event details omitted for issues after the first 3')
            snapshot['limits'] += '; Sentry latest-event detail for first 3 issues only'
            for issue in issues[:3]:
                issue_id = str(issue.get('id', ''))
                if not issue_id.isdigit():
                    snapshot['source_errors'].append('Sentry issue has no valid numeric id for latest event')
                    continue
                try:
                    snapshot['sources']['sentry_event_' + issue_id] = _sentry("issues/" + issue_id + "/events/latest/")
                except Exception:
                    snapshot['source_errors'].append('Sentry latest event ' + issue_id + ': unavailable; check read access')
        except Exception:
            snapshot['source_errors'].append('Sentry issues: unavailable; check authentication/read access or response limits')
    elif kind == 'prod-logs':
        match = re.search(r'--project(?:=|\s+)([\w.-]+)', watch.get('instructions', ''))
        project = match.group(1) if match else os.environ.get('NEO_GCP_PROD_PROJECT', '').strip()
        if not re.fullmatch(r'[a-z][a-z0-9.-]{3,100}', project):
            snapshot['source_errors'].append('production_logs: no valid explicit GCP project configured')
        else:
            logs = read('production_logs', ['gcloud', 'logging', 'read',
                 'severity>=ERROR AND resource.type="k8s_container" AND NOT '
                 'resource.labels.namespace_name=("monitoring" OR "kube-system" OR "gke-managed-system" OR "gmp-system") '
                 'AND NOT textPayload=~"^(INFO|DEBUG|WARNING)[: ]" AND NOT textPayload=~"^[[:space:]^]" '
                 'AND (textPayload=~"(Error|Exception|error|failed|Failed|FATAL|panic)" OR '
                 'jsonPayload.message=~"(Error|Exception|error|failed)") '
                 + 'AND timestamp>="' + since.isoformat() + '"', '--project', project,
                 '--limit=100', '--format=json'])
            if isinstance(logs, list) and len(logs) >= 100:
                snapshot['source_errors'].append('Production logs sample saturated at 100; window coverage incomplete')
    else:
        runs = read('failed_runs', ['gh', 'run', 'list', '--repo', REPO, '--status', 'failure',
                    '--limit', '10', '--json', 'databaseId,headSha,name,conclusion,createdAt,url'])
        prs = read('open_prs', ['gh', 'pr', 'list', '--repo', REPO, '--state', 'all', '--limit', '30',
             '--json', 'number,title,url,headRefName,statusCheckRollup'])
        if isinstance(prs,list) and len(prs)>=30:
            snapshot['source_errors'].append('PR sample saturated at 30; older fixes may be omitted')
        linked_runs = []
        try:
            history = _slack_history(watch)
            snapshot['sources']['deskie_e2e_slack'] = history.get('messages', [])
            for message in history.get('messages', []):
                for rid in re.findall(r'https://github\.com/AnyTeamAI/anyteam-deskie/actions/runs/(\d+)(?![\w/])', json.dumps(message)):
                    if rid not in linked_runs:
                        linked_runs.append(rid)
            if history.get('has_more'):
                snapshot['source_errors'].append('Deskie e2e Slack: history truncated at 100 messages')
        except Exception:
            snapshot['source_errors'].append('Deskie e2e Slack: unavailable; check bot authentication/channel access')
        run_ids = linked_runs[:10]
        if len(linked_runs) > 10:
            snapshot['source_errors'].append('Deskie Slack linked-run details truncated at 10')
        if isinstance(runs, list):
            if len(runs) >= 10:
                snapshot['source_errors'].append('Failed run sample saturated at 10; more failures may be omitted')
            for run in runs:
                rid = str(run.get('databaseId', ''))
                if rid.isdigit() and rid not in run_ids:
                    run_ids.append(rid)
        for rid in run_ids[:20]:
            read('run_' + rid, ['gh', 'run', 'view', rid, '--repo', REPO, '--json', 'jobs,headSha,conclusion,url'])
            read('log_' + rid, ['gh', 'run', 'view', rid, '--repo', REPO, '--log-failed'], json_output=False)
    return snapshot
