# Compatibility and host evidence

FL Slacker reports a capability as verified only when it has been observed on the exact FL
build that is running. Records live in `src/flslacker/compatibility.json` (shipped) and
`<data folder>/compatibility.local.json` (written by `flslacker m0` on your machine). `doctor`
and `fls_get_capabilities` show which record applies.

Capability states: `supported`, `requires_user_action` (works, but you run a script in FL),
`not_connected` (FL has not reported in this session), `unavailable` (with a reason).

## Recorded evidence: FL Studio 26.1.6.5639 (Windows 11 Pro 10.0.26200)

Recorded 2026-09-17, adapter 0.1.0. **No script has run inside FL for this record.**

| Item | Evidence | Status |
|---|---|---|
| Installed Piano Roll reference | Lists `getTimelineSelection`, `setTimelineSelection`, `getDefaultNoteProperties`, `getNextFreeGroupIndex`, `addInputSurface`, `ScriptDialog.execute()`, `Note.clone()`; note fields as used by FL Slacker | documented |
| Factory scripts | Use `flp.Note()`, `note.clone()`, `score.addNote()`, `AddInputCombo`, `AddInputCheckbox`, `AddInputKnob(Int)`, `GetInputValue` (capitalized forms) | documented |
| Embedded interpreter | `Shared/Python/python.exe` is CPython 3.12.1; `json`, `hmac`, `hashlib`, `os`, `uuid`, `calendar`, `ctypes` import | verified outside FL |
| Mailbox prerequisites | Signing, tamper rejection, atomic write and exclusive claim pass on that interpreter (`flslacker doctor`) | verified outside FL |
| Script logic | Capture/Apply/Probe round trip passes on that interpreter against a mocked `flpianoroll` (`tests/embedded_smoke.py`) | mock only |
| `bridge.mailbox` inside FL | – | unverified |
| `notes.capture`, `notes.verify` | – | unverified |
| `notes.patch.update/insert/delete` | – | unverified |
| Selection semantics (`noteCount` with a selection) | – | unverified |
| Script runs once per invocation, not on menu scan | – | unverified |
| Dialog cancel writes nothing | – | unverified |
| Undo grouping of one script run | – | unverified |
| Note order after time edits; proxy stability | – | unverified (see M0 mutation probe) |
| Float quantization of velocity/pan/etc. | – | unverified (verification tolerates ±0.01) |
| `project.metadata` (MIDI adapter) | – | unverified; needs a MIDI input |

Because the write capabilities are unverified, `flslacker serve` reports them as
`unavailable` until a local M0 record exists. `--allow-unverified-host` enables them as
`EXPERIMENTAL`, intended only for running M0.

## M0 checklist

Save your work first. Use a **new, disposable** FL project. Nothing in M0 touches other
projects, but Apply edits the notes in whatever Piano Roll you run it in.

### Guided (recommended)

```bash
flslacker install-adapters
```

```bash
flslacker serve --allow-unverified-host
```

In a second terminal:

```bash
flslacker m0
```

The wizard walks through the steps below, checks each result through the companion, and writes
`compatibility.local.json` with the evidence (job and snapshot IDs, probe report names). It
records a capability as `verified` only if the step succeeded, and as `failed` otherwise.
Afterwards restart `flslacker serve` without the flag.

### Steps (what the wizard asks you to do)

1. **Prepare.** File > New. Draw 6 notes at different positions. Give one note a non-default
   velocity and pan. Select exactly 3 notes.
2. **Probe.** Piano Roll > Tools > Scripting > Slacker > Slacker Probe. The report
   (`<data>/probe/probe-*.json`) records FL's build, Python version, available modules, note
   attributes and whether `noteCount` counts all notes or only the selection. Also check: the
   script ran once, and no Slacker dialog appeared just from opening the menu.
3. **Capture.** Run Slacker Capture, choose the request, type the label `M0 test`, tick the
   confirmation. Expect 3 notes in scope and no unreadable fields.
4. **Cancel.** Run Slacker Capture and press Cancel. Expect nothing written and the request still
   pending.
5. **Update.** Approve and apply a plan that raises one note's pitch, sets its velocity to 0.5
   and moves another note by one quarter. Run Slacker Apply, then the Verify capture. Expect
   `applied`.
6. **Undo.** Press Ctrl+Z once, then capture again. The wizard records whether one undo step
   reverts the whole script run.
7. **Insert and delete.** Reselect 3 notes if needed; apply a plan that inserts one note and
   deletes one; verify. Then apply the offered restore plan and verify it.
8. **Stale state.** Before running Apply for the next plan, drag a selected note slightly. Expect
   Apply to refuse with `STALE_SNAPSHOT` and change nothing.

### Manual developer probes

`flslacker install-adapters --dev` adds **Slacker M0 Mutation Probe** (disposable projects
only). It adds, edits and deletes probe notes (pitches 61, 62, 70–72) and records write
quantization, insertion order, whether proxies follow a note after a time change, index shifts
after deletion, `clone()` fidelity and out-of-range handling. Run Slacker Probe afterwards to see
the committed state. `flslacker doctor -v` summarizes the latest reports.

## Known limits

1. No interface used here is a transactional FL project object model. Each exposed operation
   needs a host mapping and an integration test.
2. Piano Roll access is limited to what FL exposes to that invocation. There is no documented
   remote trigger, so you always start Capture and Apply yourself.
3. MIDI controller scripts, Piano Roll scripts and plugins run in separate contexts; nothing that
   spans them is atomic.
4. Target identity is user-attested. FL Slacker cannot tell two patterns with identical notes
   apart; that is why Apply asks you to confirm the target every time.
5. Live MIDI output is not Piano Roll data.
6. A hosted plugin is not a Python extension loader; no plugin shell ships in 0.1.0.
7. Analysis cannot determine artistic intent; suggestions are conservative and never applied
   without a grant.
8. Markers are read-only. Pattern-local ticks are not converted to Playlist positions.
