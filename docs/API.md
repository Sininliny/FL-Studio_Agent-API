# FL Slacker API (protocol `flslacker/1`)

One dispatcher serves the HTTP API, the MCP shim and the local Ollama loop. Machine-readable
contracts are in [`schemas/flslacker-1/`](../schemas/flslacker-1) (`tools.json`, `openapi.json`,
one JSON Schema per model) and live at `GET /v1/schema`. Regenerate them with
`flslacker export-schemas`.

## HTTP

The server binds to `127.0.0.1` only. Every `/v1/` route needs authentication:

- `Authorization: Bearer <agent_token>` (from `credentials.json`) for agents. Optional
  `X-FLS-Client: <name>` labels the caller (`agent:<name>` in journals and job ownership).
- `Authorization: Bearer <ui_token>` (from `ui-credentials.json`) or the UI session cookie for
  the local UI. Cookie-authenticated `POST`s also need `X-FLS-CSRF: 1` and a same-origin
  `Origin` header.

The `Host` header must be `127.0.0.1:<port>` or `localhost:<port>` (else 421). A foreign
`Origin` or `Sec-Fetch-Site: cross-site` is refused (403). No CORS headers are ever sent.
Bodies must be `application/json` and at most 4 MiB. Tokens never appear in URLs or logs.

| Method and path | Purpose |
|---|---|
| `GET /v1/health` | `{ok, protocol, version, session_active}` |
| `GET /v1/capabilities` | Result of `fls_get_capabilities` |
| `POST /v1/tools/{tool_name}` | Validated tool invocation (body = arguments) |
| `GET /v1/jobs/{job_id}` | Result of `fls_get_job` |
| `POST /v1/jobs/{job_id}/cancel` | Result of `fls_cancel_job` |
| `GET /v1/schema` | Tools, model schemas and OpenAPI |
| `GET /v1/openapi.json` | OpenAPI 3.1 document |

UI-only routes (UI token or cookie): `POST /v1/ui/login-code` (UI token only),
`POST /v1/ui/login`, `POST /v1/ui/logout`, `GET /v1/ui/state`, `GET /v1/ui/snapshots/{id}`,
`GET /v1/ui/plans/{id}`, `POST /v1/ui/plans/{id}/approve`, `POST /v1/ui/plans/{id}/apply`,
`POST /v1/ui/receipts/{id}/restore-plan`, `POST /v1/ui/grants`,
`POST /v1/ui/grants/{id}/revoke`, `POST /v1/ui/jobs/{id}/acknowledge`,
`POST /v1/ui/recipes/{id}/run`, `POST /v1/ui/session/new`, `POST /v1/ui/agent`,
`GET /v1/ui/agent/{run_id}`. Agent tokens get 403 on all of them.

### Status codes

| Status | When |
|---|---|
| 200 | Success |
| 202 | Accepted job (`fls_capture_score`, `fls_apply_plan`, `fls_restore_edit`). A queued job is never an applied edit. |
| 400 | `INVALID_ARGUMENT`, `UNSUPPORTED_CAPABILITY` |
| 401 | Missing or invalid authentication |
| 403 | `PERMISSION_DENIED`, cross-origin or missing CSRF header |
| 404 | Unknown tool, job, plan, snapshot or receipt (`INVALID_ARGUMENT` with `details.reason = "not_found"`) |
| 409 | `STALE_SNAPSHOT`, `TARGET_MISMATCH`, `BUSY`, `USER_ACTION_REQUIRED`, hash or idempotency conflicts |
| 410 | `EXPIRED` |
| 413 | `LIMIT_EXCEEDED`, oversized body |
| 415 | Body is not JSON |
| 421 | Unexpected `Host` |
| 500 | Internal error (`OUTCOME_UNKNOWN`: assume nothing; check the job or recapture) |
| 503 | `ADAPTER_OFFLINE`, `MODEL_UNAVAILABLE` |

## Result envelope

Every tool returns:

```json
{"ok": true, "request_id": "<uuid>", "data": {...}, "error": null}
{"ok": false, "request_id": "<uuid>", "data": null,
 "error": {"code": "STALE_SNAPSHOT", "message": "...", "retryable": true, "details": {}, "required_action": null}}
```

Exactly one of `data` and `error` is non-null. MCP returns the same object as
`structuredContent` (and as JSON text) with `isError` set for failures.

Error codes: `INVALID_ARGUMENT`, `UNSUPPORTED_CAPABILITY`, `ADAPTER_OFFLINE`,
`USER_ACTION_REQUIRED`, `PERMISSION_DENIED`, `STALE_SNAPSHOT`, `TARGET_MISMATCH`, `BUSY`,
`EXPIRED`, `LIMIT_EXCEEDED`, `MODEL_UNAVAILABLE`, `PARTIAL_APPLY`, `OUTCOME_UNKNOWN`,
`VERIFICATION_FAILED`. `required_action` is `{actor, where, instruction, script?, request_short_id?}`
where `where` is `fl_piano_roll`, `companion_ui` or `agent`.

## Tools

| Tool | Arguments | Result | Permission |
|---|---|---|---|
| `fls_get_capabilities` | – | `CapabilitiesView` | read |
| `fls_get_project_summary` | `session_id` | `ProjectSummary` (every field has status, source, age) | read |
| `fls_capture_score` | `session_id`, `scope` (`selected` or `all_exposed`), `target_label?` | `Job` (202) | read |
| `fls_get_snapshot` | `snapshot_id`, `include_notes?` | `Snapshot` with `freshness` | read |
| `fls_analyze_score` | `snapshot_id`, `analyses` (`motifs`, `key`, `chords`, `pitch_outliers`), `parameters?` | `Analysis` | read |
| `fls_propose_patch` | `snapshot_id`, `operations` (1–1000), `rationale` | `Plan` | propose |
| `fls_preview_plan` | `plan_id` | `PlanPreview` (diff text, warnings, staleness, grant coverage) | read |
| `fls_apply_plan` | `plan_id`, `expected_plan_hash`, `idempotency_key` | `Job` (202) | scoped write |
| `fls_get_job` | `job_id` | `JobView` (`job`, `receipt`) | read |
| `fls_cancel_job` | `job_id` | `JobView` | owning client or UI |
| `fls_restore_edit` | `receipt_id`, `idempotency_key` | `Job` (202) | scoped write |

All argument objects reject unknown properties. IDs are lowercase UUIDs; hashes are 64
lowercase hex characters.

### Analysis parameters

`key_hint` (user constraint, e.g. `"C major"`, `"F# minor"`, `"Bbm"`), `transpose_invariant`
(true), `min_motif_notes` (4), `max_motif_notes` (12), `min_occurrences` (2),
`onset_tolerance_ticks` (PPQ/8), `allow_overlap` (false), `voice` (`skyline` or `all`),
`exclude_note_ids`, `pitch_range` (`[low, high]`), `chord_window_beats` (4 quarter notes),
`max_findings` (50 per kind), `in_scope_only` (true).

Conventions: onsets are quantized by rounding half up to the tolerance grid; quarter notes are
`ticks / PPQ` and are not assumed to be time-signature beats; pitch names use FL's display
(60 = C5) and are presentation only. Findings carry `kind`, `severity` (`info` or
`suggestion`), `confidence`, `note_ids`, `evidence`, and for suggestions a ready-to-propose
`suggestion` operation.

## Data contracts

- **Snapshot**: `snapshot_id`, `session_id`, `target` (`target_id`, `binding`, `label`),
  `purpose` (`capture`, `verify`, `adhoc`), `captured_at`, `received_at`, `coverage`
  (`exposed_score`), `scope`, `ppq`, `time_signature`, `notes`, `markers` (read-only),
  `selection`, `in_scope_count`, `supported_fields`, `unsupported_fields`, `default_note`,
  `state_hash`, `content_hash`, `host`, `freshness`.
- **Note**: `note_id` (`n<ordinal>`, snapshot-local), `ordinal`, `pitch`, `start_tick`,
  `duration_tick`, `velocity`, `fl` (every other readable property in native units: `group`,
  `pan`, `release`, `color`, `fcut`, `fres`, `pitchofs`, `slide`, `porta`, `muted`, `selected`,
  `repeats`), `in_scope`, `fingerprint`. Unusual existing values are preserved as read.
- **Plan**: `plan_id`, `kind` (`edit` or `restore`), base `snapshot_id` and `base_state_hash`,
  target, `scope`, `operations`, `resolved_operations` (FL-native), `diff`, `limits`,
  `predicted_content_hash`, `rationale` (untrusted text), `warnings`, `created_by`,
  `expires_at`, and the immutable `plan_hash` over everything else.
- **Job**: `job_id`, `kind` (`capture` or `apply`), `purpose`, `state`, `plan_id`, `plan_hash`,
  `verify_job_id`, `request_short_id`, `snapshot_id`, `receipt_id`, `next_action`, `error`,
  `history`, timestamps.
- **Receipt**: before and after snapshot IDs and hashes, `applied_operation_count`,
  `verification: "verified"`, `recovery_available`, `recovery_note`, `changes`.
- **Grant** (UI only): `kind` (`plan` or `recipe`), `session_id`, `target_label`, `plan_hash`,
  `constraints` (`scope`, `operation_kinds`, `fields`, `max_notes`, `max_pitch_delta`,
  `max_time_delta_ticks`, `max_velocity_delta`), `expires_at`, `budget_used`, `uses`,
  `max_uses`, `revoked`.

Timestamps are UTC ISO 8601.

## Job state machine

```text
queued -> awaiting_fl_action -> applying -> awaiting_verification -> applied
   \-> cancelled / expired / failed                   \-> verification_failed
                                applying -> partial_apply
                                applying -(silent past timeout)-> outcome_unknown
capture jobs: queued -> awaiting_fl_action -> completed | cancelled | expired | failed
```

- `awaiting_fl_action`: the request is in the mailbox; `next_action` says what to run in FL.
- `applying`: FL claimed the request. Cancellation is now only a request; nothing rolls back.
- `awaiting_verification`: FL applied the plan and its in-script read-back matched. A Verify
  capture was requested; any fresh capture of the same target also verifies.
- `applied`: a fresh capture matched the prediction; a receipt exists.
- `outcome_unknown`, `partial_apply`, `verification_failed`: uncertain. The target's writer lock
  stays held until a capture reconciles the job or the user acknowledges it in the UI.

## Canonical JSON and hashes

Canonical JSON: keys sorted, separators `,` and `:`, ASCII escapes, UTF-8, integers in decimal,
floats as Python's shortest round-trip `repr`, `-0.0` written as `0.0`, NaN and infinities
rejected, `bool` never treated as an integer. `sha256` is taken over those bytes.

- `note_fingerprint = sha256({"v":1,"kind":"note","record":<FL record>})`
- `state_hash = sha256({"v":1,"kind":"state","ppq":…,"notes":[records in ordinal order],"markers":[…]})`
- `content_hash = sha256({"v":1,"kind":"content","ppq":…,"notes":sorted(canonical(record minus "selected")),"markers":sorted(canonical(marker))})`
- `plan_hash = sha256(plan without plan_hash, JSON mode)`

FL records use FL's attribute names: `number`, `time`, `length`, `group`, `pan`, `velocity`,
`release`, `color`, `fcut`, `fres`, `pitchofs`, `slide`, `porta`, `muted`, `selected`,
`repeats`. Test vectors: [`tests/vectors/canonical_v1.json`](../tests/vectors/canonical_v1.json).

## Mailbox bridge

Layout under the data folder:

```text
bridge/pairing.json                             {protocol, session_id, secret, adapter_epoch, ...}
mailbox/sessions/<session_id>/requests/<request_id>.json    companion -> FL
mailbox/sessions/<session_id>/snapshots/<uuid>.json         FL -> companion
mailbox/sessions/<session_id>/responses/<uuid>.json         FL -> companion
mailbox/sessions/<session_id>/claims/<request_id>.claim     exclusive ownership
```

Envelope (unknown fields rejected):

```json
{"protocol": "flslacker/1", "kind": "apply_request", "session_id": "<uuid>", "request_id": "<uuid>",
 "in_reply_to": null, "sequence": 42, "sender": "companion", "created_at": "2026-09-17T12:00:00Z",
 "expires_at": "2026-09-17T12:20:00Z", "body": {...}, "auth": "<hmac-sha256 hex>"}
```

`auth` is HMAC-SHA256 with the session secret over the canonical JSON of the envelope without
`auth`. Kinds: `capture_request`, `apply_request` (companion); `snapshot`, `capture_error`,
`apply_response`, `midi_status` (FL). Receivers reject wrong protocol, kind, sender or session,
bad UUIDs, bad timestamps, expiry, creation more than 5 minutes in the future, bad tags,
replayed request IDs and non-increasing FL sequence numbers. Files are written to an exclusive
temporary name and renamed. Limits: 4 MiB per message, 10,000 notes per snapshot, 1,000
operations per patch; larger work is refused, not truncated.

`apply_request.body`: `job_id`, `plan_id`, `plan_hash`, `snapshot_id`, `target_label`, `scope`,
`base_state_hash`, `base_note_count`, `ppq`, `operations`, `summary`.

`apply_response.body`: `job_id`, `plan_hash`, `status` (`applied_provisional`, `failed`,
`rejected`, `partial_apply`, `verification_mismatch`), `error`, `applied_operations`,
`before_state_hash`, `after_state_hash`, `after_content_hash`, `after_note_count`,
`mismatches`, `host`.

Before opening a dialog, the FL scripts list `requests/` and create, rename and remove a scratch
file in the session folder. If FL refuses any mailbox file operation (a `SystemError` or
`RuntimeError` from its script sandbox, as on FL 26.1.6.5639), the script stops with
`UNSUPPORTED_CAPABILITY (bridge.mailbox)` and writes nothing. Ordinary I/O errors are reported
as they are.

Probe reports that FL refuses to save are printed to its Script output instead:

```text
FLSLACKER-REPORT-BEGIN <probe|m0> <byte length>
<base64 of the canonical JSON report, 160 characters per line>
FLSLACKER-REPORT-END <sha256 hex of the decoded bytes>
```

`flslacker import-report` checks the length and checksum and stores the report in `probe/`.
