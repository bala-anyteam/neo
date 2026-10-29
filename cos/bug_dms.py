#!/usr/bin/env python3
"""Daily Nio DMs for assigned anyteam-design bugs idle over fourteen days.

Run by launchd on weekdays alongside the 10:00 triage. At most one DM per owner per UTC day;
bot comments do not reset the human-update clock. Successful sends are persisted for retries.

  bug_dms.py --dry-run          write the messages to dms-<date>.json, send nothing
  bug_dms.py --only anil-anyteam  send to one teammate (for testing)
"""
import argparse, datetime as dt, fcntl, json, re, sys, urllib.request
from collections import defaultdict
from pathlib import Path

from .runner import cos_home

HERE = cos_home() / "triage"
STATE = HERE / "bug-followup-state.json"  # Successful deliveries keyed by UTC day and owner.
REPO = "AnyTeamAI/anyteam-design"
ENV = Path.home() / "neo" / ".env"
CONFIG = Path.home() / ".config" / "chakor" / "config.json"
TEAMMATE_ALIASES = {  # GitHub login -> configured Chakor destination
    "sachin-anyteam": "sachin", "praveen-anyteam": "praveen", "chrislarson1": "chris",
    "utkarsh-anyteam": "utkarsh", "farhan-anyteam": "farhan", "shubham-anyteam": "shubham",
    "anil-anyteam": "anil", "satish-anyteam": "satish", "jai-anyteam": "jai", "theo165": "theo",
    "james-anyteam": "james", "yosh-anyteam": "yosh", "sejal-anyteam": "sejal",
}
FIRST = {"sachin-anyteam": "Sachin", "praveen-anyteam": "Praveen", "chrislarson1": "Chris",
         "utkarsh-anyteam": "Utkarsh", "farhan-anyteam": "Farhan", "shubham-anyteam": "Shubham", "anil-anyteam": "Anil",
         "satish-anyteam": "Satish", "jai-anyteam": "Jai", "theo165": "Theo", "james-anyteam": "James",
         "yosh-anyteam": "Yosh", "sejal-anyteam": "Sejal"}
RANK = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "Untriaged": 4}
OLD_DAYS = 14


def configured_destinations():
    destinations = json.loads(CONFIG.read_text())["parts"]["slack"]["destinations"]
    return {login: destinations[alias] for login, alias in TEAMMATE_ALIASES.items() if alias in destinations}


def severity(issue):
    project_items = issue.get("projectItems", [])
    if isinstance(project_items, dict):
        project_items = project_items.get("nodes", [])
    for item in project_items:
        if (item.get("project") or {}).get("number") != 15:
            continue
        values = item.get("fieldValues", [])
        if isinstance(values, dict):
            values = values.get("nodes", [])
        for value in values:
            if (value.get("field") or {}).get("name") == "Priority":
                priority = value.get("name")
                return priority if priority in RANK else "Untriaged"
    return "Untriaged"


def short_title(t):
    t = re.sub(r"\[[^\]]*\]\s*", "", t)
    t = re.sub(r"^(Bug:|Mac\s*-|Win\s*-|Windows\s*-|Windows/Mac:)\s*", "", t.strip(), flags=re.I)
    parts = [p.strip() for p in re.split(r"\s+[—–]\s+", t) if p.strip()] or [t]
    t = parts[1] if len(parts) > 1 and ("·" in parts[0] or (len(parts[1]) > len(parts[0]) and len(parts[0]) < 30)) else parts[0]
    head = re.split(r"\s+\(", t)[0]
    t = head if len(head) >= 22 else t
    t = t.strip(" -—(·:")
    t = t[:1].upper() + t[1:]
    if len(t) > 64:
        t = t[:64].rsplit(" ", 1)[0].rstrip(",;:—-") + "…"
    return t


def url(n): return f"https://github.com/{REPO}/issues/{n}"


def timestamp(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def human(author):
    author = author or {}
    login = author.get("login", "")
    return bool(login) and author.get("__typename") != "Bot" and not login.endswith(("[bot]", "-bot")) and login not in {"anyteam-devsvcs"}


def last_human_update(issue):
    last = timestamp(issue["createdAt"])
    if issue.get("lastEditedAt") and human(issue.get("editor")):
        last = max(last, timestamp(issue["lastEditedAt"]))
    comments = issue.get("comments", [])
    if isinstance(comments, dict):
        comments = comments.get("nodes", [])
    for comment in comments:
        if human(comment.get("author")):
            last = max(last, timestamp(comment["createdAt"]))
        if comment.get("lastEditedAt") and human(comment.get("editor")):
            last = max(last, timestamp(comment["lastEditedAt"]))
    return last


def eligible(issue, now):
    return (issue.get("state", "OPEN") == "OPEN"
            and (issue.get("issueType") or {}).get("name") == "Bug"
            and now - last_human_update(issue) > dt.timedelta(days=OLD_DAYS))


def message(who, bugs):
    lines = [f"Hi {FIRST.get(who, who)} — this is Nio, following up on Bala's behalf for daily bug triage.",
             "", "These assigned bugs have had **no human update for more than 14 days**:"]
    lines += [f"• [#{b['n']}]({url(b['n'])}) {b['title']} · {b['age']} days without a human update" for b in bugs]
    lines += ["", "Please comment on each issue with the next step, any blocker, and expected release/date. "
              "If it no longer reproduces, add the verification and close it. If ownership has changed, let Bala know."]
    return "\n".join(lines)


def bot_token():
    for raw in ENV.read_text().splitlines():
        if raw.startswith("SLACK_BOT_TOKEN="):
            return raw.split("=", 1)[1].strip().strip('"')
    sys.exit("SLACK_BOT_TOKEN not found in ~/neo/.env")


def send(token, user, text):
    body = json.dumps({"channel": user, "text": text[:3000], "unfurl_links": False,
                       "blocks": [{"type": "markdown", "text": text}]}).encode()
    req = urllib.request.Request("https://slack.com/api/chat.postMessage", data=body, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only")
    args = ap.parse_args()
    now = dt.datetime.now(dt.timezone.utc)
    from .watch_snapshot import collect
    raw = collect(REPO, include_metadata=False)["issues"]
    destinations = configured_destinations()
    per = defaultdict(list)
    for i in raw:
        if isinstance(i["labels"], dict):
            i["labels"] = i["labels"]["nodes"]
        if isinstance(i["assignees"], dict):
            i["assignees"] = i["assignees"]["nodes"]
        if not eligible(i, now):
            continue
        b = {"n": i["number"], "sev": severity(i), "title": short_title(i["title"]),
             "age": (now - last_human_update(i)).days}
        for a in i["assignees"]:
            if a["login"] in destinations:
                per[a["login"]].append(b)
    HERE.mkdir(parents=True, exist_ok=True)
    prev = json.loads(STATE.read_text()) if STATE.exists() else {}
    stamp = now.date().isoformat()
    out, log = {}, []
    for who, bugs in sorted(per.items()):
        if args.only and who != args.only:
            continue
        bugs.sort(key=lambda b: (RANK[b["sev"]], -b["age"]))
        if not args.dry_run and prev.get(stamp + ":" + who):
            continue
        out[who] = {"slack": destinations[who], "issues": [b["n"] for b in bugs], "text": message(who, bugs)}
    (HERE / f"dms-{stamp}.json").write_text(json.dumps(out, indent=1))
    if args.dry_run:
        print(f"dry run: {len(out)} messages in dms-{stamp}.json")
        return
    token = bot_token()
    for who, v in out.items():
        if not v["slack"]:
            log.append(f"{who}: no Slack id, skipped")
            continue
        r = send(token, v["slack"], v["text"])
        log.append(f"{who}: {'sent' if r.get('ok') else 'FAILED ' + str(r.get('error'))}")
        if r.get("ok"):
            prev[stamp + ":" + who] = {"issues": v["issues"], "channel": r.get("channel"), "ts": r.get("ts")}
            temporary = STATE.with_suffix(".tmp")
            temporary.write_text(json.dumps(prev, indent=2))
            temporary.replace(STATE)

    print(f"{dt.datetime.now():%F %T} " + "; ".join(log))


def main():
    HERE.mkdir(parents=True, exist_ok=True)
    with (HERE / "bug-followups.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another bug follow-up run is already active; skipped")
            return
        _main()


if __name__ == "__main__":
    main()
