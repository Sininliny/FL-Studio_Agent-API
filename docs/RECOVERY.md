# Recovery guide

FL Slacker never claims an edit happened without a fresh capture proving it, and it never
retries a write on its own. This guide covers what to do when a job is not cleanly `applied`.

**Before your first editing session, use File > Save As in FL.** A receipt's before-image covers
only the captured notes; it is not a project backup. FL Slacker never saves projects.

## Where things are

- UI: `flslacker ui` → Jobs, Plans, Receipts.
- Journal: `<data folder>/data/flslacker.sqlite3`, table `journal` (append-only events:
  `apply_prepared`, `job_state`, `apply_response`, `receipt`, `message_rejected`, `tool_call`, …).
- Before-images: table `before_images` (captured records of every note a job touches).
- Rejected mailbox files: `mailbox/sessions/<id>/rejected/` with a `.reason.txt` beside each.
- Logs: `<data folder>/logs/flslacker.log`.

## By job state

| State | Meaning | What to do |
|---|---|---|
| `awaiting_fl_action` | Nothing happened yet | Run the script named in the job, or cancel it. |
| `applying` | FL claimed the job but has not reported | Wait. If FL crashed, see `outcome_unknown`. |
| `awaiting_verification` | FL applied it; its own read-back matched | Run Slacker Capture and choose the Verify request (any capture of that target also works). |
| `outcome_unknown` | FL claimed the job and went silent past the timeout | **Do not retry.** Capture the target. If it still equals the base, the job becomes `failed` (not applied) and its budget is refunded. If it matches the plan, it becomes `applied`. Otherwise it becomes `verification_failed`. |
| `partial_apply` | FL hit an error after changing some notes | Capture the target to record the state. Inspect the notes in FL. Edit > Undo may revert the script run (unverified per build). Then acknowledge the job. |
| `verification_failed` | The fresh capture differs from the prediction | Compare the job's `error.details.mismatches` with FL. You may have captured another Piano Roll, changed the selection, or edited meanwhile. Capture again to re-verify, or fix things in FL and acknowledge. |
| `failed` with `STALE_SNAPSHOT` | The live notes differed; FL changed nothing | Capture again and propose a new plan. |
| `cancelled` / `expired` | FL never acted | Nothing to recover. |

Jobs in `outcome_unknown`, `partial_apply` or `verification_failed` hold the writer lock for
their target, so no new edit can start on it. **Acknowledge** (UI, Jobs) releases the lock
after you have checked FL yourself; the job is then recorded as `failed` with your note.

## Undoing a verified edit

- **Restore** (Receipts → *Prepare restore*, or `fls_restore_edit`) builds an inverse plan:
  updates are set back, inserted notes are deleted, deleted notes are re-inserted from their
  before-image. It is only offered while the latest capture of the target equals the receipt's
  after-state, so it cannot overwrite later work. Review and apply it like any plan, then
  verify.
- **FL's Undo** (Ctrl+Z) is independent of FL Slacker. After using it, capture again before any
  further plan; old plans become stale automatically.

Restore is unavailable when a deleted note had unreadable properties, when old values are
outside what the baseline writer may set, or when the note is outside the verified capture's
scope (capture with `all_exposed` and try again).

## Sessions and restarts

- Stopping `flslacker serve` ends the session: unclaimed requests expire and FL scripts report
  that the companion is not paired. Results FL wrote before the stop are still ingested on the
  next start.
- *New session* in the UI (or a project load reported by the MIDI adapter) invalidates the
  current session. Plans from an ended session can no longer be applied; capture again.
- If a crash left a job `queued`, the next start marks it `failed` unless its request file exists,
  in which case it continues as `awaiting_fl_action`.

## Resetting

- Remove FL-side files: `flslacker uninstall-adapters`.
- Delete all history: `flslacker uninstall-adapters --purge-data` (asks first), or delete the
  data folder while the companion is stopped.
- Rotate tokens: stop the companion and delete `credentials.json` and `ui-credentials.json`;
  `serve` creates new ones. Reconfigure MCP clients only if you changed the port.
