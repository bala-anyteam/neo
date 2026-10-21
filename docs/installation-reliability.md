# Installation reliability register

`COS_HOME/reliability.json` retains activation time, source commit, a frozen seven-day baseline, authorized Slack destination, and six improvement records: `continuity`, `release_scope`, `evidence`, `scheduled_jobs`, `approvals`, `communication`. Configuration is idempotent: changing delivery settings preserves activation and baseline. Updates use a process lock and atomic replacement.

Configure after installing the intended commit (does not send Slack messages):

```
python -m cos.reliability configure --channel C0C8PAUC7DK --thread 1792548996.013219 --at 18:00 --tz Asia/Kolkata
python -m cos.reliability status
python -m cos.reliability report
```

The daily deterministic comparison runs inside chief, using the existing Slack client. It performs no model calls. Delivery targets the configured thread, observes notification pause, and records daily/watch IDs only after successful delivery. Failed sends retry the next minute. A restart preserves delivered IDs. Scheduled watch failures from the preceding seven days, after activation, are grouped into one message with up to twenty unseen run IDs per pass. Each incident includes its source timestamp; already acknowledged IDs are filtered before selecting the batch. Delivery requires Slack ok=true and a nonempty message timestamp. Reports themselves do not create runs or human prompts.

The comparison uses event timestamps in half-open windows: baseline `[activation - exposure, activation)` and post `[activation, activation + exposure)`. Exposure is elapsed time since activation capped at seven days. The first week grows comparably. Thereafter post is the latest seven-day window, while baseline remains the fixed seven days before activation. The frozen full-week baseline remains available separately. All windows are epoch seconds; naive historical timestamps are assumed UTC. Daily delivery is explicitly Asia/Kolkata by default, not machine-local time.

Metrics are descriptive proxies:

- `abandoned_waiting_exits`: a terminal Slack run whose latest in-window waiting result is at least fifteen minutes old at cohort end, with no subsequent in-window prompt, recovery marker or result; no live process/retry owner or pending continuation; current conclusion absent or waiting. Partial/oversized event scans are excluded. This is a stranded-work proxy: historical process ownership cannot be reconstructed, and out-of-window continuations can affect interpretation.
- `manual_status_nudges`: Slack prompt events mentioning status/update/progress/continue/still working/what happened, after stripping everything through the final owner-message context marker. Exact synthetic `continue` and known waiting-continuation prompts are excluded. Unmarked remaining Slack prompts are assumed human. This heuristic can misclassify intent.
- `automatic_recoveries`: status events with `waiting_continuation`, `auto_restart` or `continuation_recovered` data; auto_restart must be a positive integer, so exhausted notices do not count. These count recovery attempts/restorations, not proven successful task completion.
- `completion_refusals`: events with exact `data.completion_refused` marker.
- `evidence_refusals`: timestamped loop PR/runtime proof refusals from the bounded tail of `evidence-refusals.jsonl`, separate from task completion refusals.
- `declared_contracts`: runs with valid goal/steps in `task.json`, assigned by contract creation epoch timestamp rather than reused Slack-thread run creation time. Legacy empty contracts or missing contract timestamps use parent run creation time; this is contract coverage, not proof of correctness.
- `scheduled_watch_runs`: terminal (done/failed/cancelled) scheduled watch runs in the window, the denominator for failure rate (null when no samples).
- `scheduled_watch_failures`: watch metadata with source `schedule` and terminal `failed`, using updated_at (fallback created_at), once per run.
- `progress_replies` / `final_replies`: Slack run text / result events; these count generated output, not Slack delivery acknowledgments or quality.

Every report includes run/event sample counts. Fewer than ten sampled runs in either window produces `insufficient data`; larger samples remain descriptive and establish no causality. Release scope, evidence correctness and approval appropriateness require review beyond these counters; no invented numerical effectiveness score is assigned to them.

Reads retain at most the newest 2,000 run directories by ID, 64 KiB metadata per run, and 2 MB events per run, with a 32 MB total run-event scan budget per cohort plus a 2 MB bounded tail of loop evidence refusals. Event lines over 64 KiB are discarded in bounded chunks; tool bodies are never included in measurement. Reports expose oversized-line, truncated-file and run-cap counts in JSON. Bounds and missing/malformed logs can undercount history. The sent ledger retains the newest 2,000 acknowledgments; scheduling scans a bounded seven-day failure window.

Installation authorization covers this reliability installation, not arbitrary product deployments. Continue already authorized work from persistent checkpoints, retain exact release pins, require source-backed proof, surface real blockers, and keep progress concise. Preserve these rules in the installed operating prompt and durable task contracts.

## Typed blockers
New CLI blocks require `--kind access`, `--kind evidence`, or `--kind human`.
Human blocks also require `--source`, `--approver`, and `--decision`. Technical
blocks report `blocked`, not `needs-you`, including after restart. Pending automated
work stays `waiting` and uses durable continuation rather than permanent blocks.
Future release gates belong in release scope until their action is reached; they do
not prohibit earlier authorized preparation. Existing untyped blocks remain human
gates for compatibility and must be reviewed rather than automatically reclassified.
This validates structure, not the truth of an agent's cited policy or exhaustion of
access paths; incorrect classification still requires investigation.

Clear a resolved technical blocker with `tasks resolve --run ID --evidence "verified resolution source"`; no owner approval is implied. Human and legacy gates reject this command and require actual sourced approval via unblock.

Waiting Slack tasks automatically resume after 15s, 60s and 180s, then every 300s
indefinitely while waiting. Human/access/evidence blockers, cancellation and real
engine failures still stop the loop; API failure retries retain their separate limits.
When the owner asks to stop automatic updates for a task, persist
`tasks updates --run ID --enabled false --source "owner message permalink or exact instruction"`.
Use `--enabled true` when requested again. This mutes automatic background result
messages, including completion/failure notices, without stopping internal work.
Direct responses to a new owner message still appear. Never treat muting as cancel.

### Slack task update cadence

Direct answers to owner requests remain immediate. Automatic background summaries are
coalesced per task and delivered first after 15 minutes from the request, then at
15-minute intervals while work continues. Terminal background results use the same
cadence and are sent once. The latest summary and delivery clock survive restarts;
new accepted owner requests and cancellation invalidate old queued summaries.
`automatic_updates=false` and global pause suppress automatic delivery. A successful
Slack receipt is required before advancing the notification clock; failed delivery
is retried by the scheduler. Internal execution continues on its separate schedule.
Notification state is stored in each run's `task-updates.json`.
