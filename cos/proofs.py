"""Bounded read-only outcome checks. Claimed evidence is never executable code."""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.request
from urllib.parse import urlsplit


def pr_identity(url):
    """Return the canonical GitHub PR URL, or None for unsupported identities."""
    if not isinstance(url, str):
        return None
    match = re.fullmatch(r'https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)/?', url)
    return 'https://github.com/' + '/'.join((match[1], match[2], 'pull', match[3])) if match else None


def verify(spec: dict) -> dict:
    result = {'verified': False, 'reason': 'invalid proof specification', 'checked_at': time.time(), 'details': {}}
    try:
        if not isinstance(spec, dict):
            return result
        kind = spec.get('kind')
        if kind == 'pr':
            url = pr_identity(spec.get('url'))
            if not url:
                return result
            row = _gh(['pr', 'view', url, '--json', 'state,mergedAt,mergeCommit,headRefOid'])
            result['details'] = {k: row.get(k) for k in ('state', 'mergedAt', 'mergeCommit', 'headRefOid')}
            ok = row.get('state') == 'MERGED' and bool(row.get('mergedAt')) and isinstance(row.get('mergeCommit'), dict) and bool(row['mergeCommit'].get('oid'))
            if spec.get('expected_head') is not None:
                ok = ok and row.get('headRefOid') == spec['expected_head']
            result.update(verified=bool(ok), reason='PR merged' if ok else 'PR merge or expected head not verified')
        elif kind == 'workflow':
            match = re.fullmatch(r'https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/actions/runs/([1-9][0-9]*)/?', str(spec.get('url', '')))
            if not match:
                return result
            row = _gh(['run', 'view', match[2], '--repo', match[1], '--json', 'status,conclusion,headSha'])
            ok = row.get('status') == 'completed' and row.get('conclusion') == 'success'
            if spec.get('expected_head') is not None:
                ok = ok and row.get('headSha') == spec['expected_head']
            result.update(verified=bool(ok), reason='Workflow completed successfully (not runtime health)' if ok else 'Workflow success not verified', details=row)
        elif kind == 'health':
            url = urlsplit(spec.get('url', ''))
            expected = spec.get('expected')
            if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or not isinstance(expected, dict) or not expected:
                return result
            with urllib.request.urlopen(spec['url'], timeout=10) as response:
                raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ValueError('oversized response')
            row = json.loads(raw)
            ok = isinstance(row, dict) and all(k in row and type(row[k]) is type(v) and row[k] == v for k, v in expected.items())
            result.update(verified=ok, reason='Expected health fields matched' if ok else 'Expected health fields did not match')
        return result
    except Exception:
        # Do not include exception text: HTTP errors may contain credentials or tokens.
        result.update(verified=False, reason='Proof source unavailable or invalid', details={})
        return result


def _gh(args):
    run = subprocess.run(['gh', *args], capture_output=True, text=True, timeout=20, check=True)
    row = json.loads(run.stdout)
    if not isinstance(row, dict):
        raise ValueError('invalid GitHub response')
    return row
