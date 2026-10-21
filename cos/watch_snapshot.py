"""Read-only GitHub evidence for network-isolated issue watches.

Only this trusted collector has network access; generated commands never do.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone, timedelta

ISSUE_FIELDS = '''number title url body createdAt updatedAt
assignees(first:100){nodes{login}}
labels(first:100){nodes{name}}
comments(first:100){nodes{author{login} body createdAt url} pageInfo{hasNextPage endCursor}}
timelineItems(first:100,itemTypes:[CROSS_REFERENCED_EVENT]){
 nodes{... on CrossReferencedEvent{createdAt source{... on PullRequest{
 number title url state mergedAt author{login} baseRefName headRefName repository{nameWithOwner}
 }}}} pageInfo{hasNextPage endCursor}}
'''


def gh(*args):
    proc = subprocess.run(['gh', *args], capture_output=True, text=True, timeout=180)
    if proc.returncode:
        raise RuntimeError(f'GitHub snapshot read failed: {proc.stderr[:1000]}')
    result = json.loads(proc.stdout)
    if isinstance(result, dict) and result.get('errors'):
        raise RuntimeError(f'GitHub snapshot query failed: {result["errors"]}')
    return result


def graphql(query, **variables):
    args = ['api', 'graphql', '-f', 'query=' + query]
    for key, value in variables.items():
        if value is not None:
            args.extend(['-F', f'{key}={value}'])
    return gh(*args)['data']['repository']


def collect(repo: str) -> dict:
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', repo):
        raise ValueError('issue_repo must be owner/repo')
    owner, name = repo.split('/')
    query = '''query($owner:String!,$name:String!,$cursor:String){
 repository(owner:$owner,name:$name){issues(first:20,after:$cursor,states:OPEN,labels:["bug"]){
 nodes{''' + ISSUE_FIELDS + '''} pageInfo{hasNextPage endCursor}}}}'''
    issues, cursor = [], None
    while True:
        page = graphql(query, owner=owner, name=name, cursor=cursor)['issues']
        issues.extend(page['nodes'])
        if not page['pageInfo']['hasNextPage']:
            break
        cursor = page['pageInfo']['endCursor']
    # Fully page comments and linked PR history, including old issues with long bot histories.
    for issue in issues:
        for field, selection in [
            ('comments', 'comments(first:100,after:$cursor){nodes{author{login} body createdAt url} pageInfo{hasNextPage endCursor}}'),
            ('timelineItems', 'timelineItems(first:100,after:$cursor,itemTypes:[CROSS_REFERENCED_EVENT]){nodes{... on CrossReferencedEvent{createdAt source{... on PullRequest{number title url state mergedAt author{login} baseRefName headRefName repository{nameWithOwner}}}}} pageInfo{hasNextPage endCursor}}'),
        ]:
            connection = issue[field]
            while connection['pageInfo']['hasNextPage']:
                q = 'query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name){issue(number:$number){' + selection + '}}}'
                more = graphql(q, owner=owner, name=name, number=issue['number'], cursor=connection['pageInfo']['endCursor'])['issue'][field]
                connection['nodes'].extend(more['nodes'])
                connection['pageInfo'] = more['pageInfo']
    snapshot = {'repo':repo, 'fetched_at':datetime.now(timezone.utc).isoformat(), 'issues':issues, 'source_errors':[]}
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
        snapshot['recently_closed'] = gh('issue','list','--repo',repo,'--state','closed',
            '--search','closed:>=' + since,'--limit','1000','--json',
            'number,title,url,closedAt,createdAt,labels,assignees,body,comments')
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        snapshot['source_errors'].append(str(exc))
    if owner == 'AnyTeamAI':
        try:
            project = gh('project','item-list','15','--owner',owner,'--limit','10000','--format','json')
            snapshot['project_items'] = [i for i in project['items'] if (i.get('content') or {}).get('repository','').lower() == repo.lower()]
            if project.get('totalCount', 0) > len(project['items']):
                snapshot['source_errors'].append('Project snapshot truncated at 10000 items; board metadata may be incomplete.')
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            snapshot['source_errors'].append(str(exc))
        try:
            snapshot['recent_prod_deploys'] = gh('pr','list','--repo','AnyTeamAI/anyteam-infrie','--state','merged','--search','to prod','--limit','80','--json','title,url,mergedAt,body')
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            snapshot['source_errors'].append(str(exc))
    return snapshot
