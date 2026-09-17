# Compatibility and host evidence

FL Slacker reports a capability as verified only when it has been observed on the exact FL
build that is running. Records live in `src/flslacker/compatibility.json` (shipped) and
`<data folder>/compatibility.local.json` (written by `flslacker m0` on your machine). `doctor`
and `fls_get_capabilities` show which record applies.

Capability states: `supported`, `requires_user_action` (works, but you run a script in FL),
`not_connected` (FL has not reported in this session), `unavailable` (with a reason).

Record statuses: `verified` (observed working), `unverified` (not yet observed; enabled only with
`--allow-unverified-host`), `failed` (an M0 step did not pass) and `unsupported` (observed to be
impossible on that build; `--allow-unverified-host` does **not** enable it).

## Recorded evidence: FL Studio 26.1.6.5639 (Windows 11 Pro 10.0.26200)

Recorded 2026-09-17, adapter 0.1.3. **The file bridge does not work on this build.** FL refuses
file access to Piano Roll scripts, and repeated refused calls crash FL, so capture, verify and
apply are `unsupported`: FL Slacker cannot read or edit notes in this FL build.

| Item | Evidence | Status |
|---|---|---|
| File access in the FL Slacker folder (`%LOCALAPPDATA%\FLSlacker`) | Inside FL, Slacker Capture and Apply failed reading `bridge\pairing.json` with `SystemError: <class '_io.FileIO'> returned NULL without setting an exception`; the probe's folder-creation failed the same way | **refused inside FL** |
| File access per folder (Slacker Probe 0.1.1, inside FL) | FL Slacker folder: list ok, read `SystemError`, create `TypeError`/`SystemError`, mkdir `SystemError`. FL's script folder (`Settings\Piano roll scripts\Slacker`): list ok, **read ok**, create `SystemError`. FL's own Chord progression scripts also only read files. | observed inside FL |
| Repeated refused calls crash FL | Slacker Probe 0.1.2, which attempted several writes per folder (~10 refused calls), crashed FL64.exe with **heap corruption** (APPCRASH `0xc0000374`). A refused call returns NULL to CPython without setting an exception, corrupting the interpreter; ~5 such calls (Probe 0.1.1) survived, ~10 did not. | observed inside FL |
| Cause | `FLEngine_x64.dll` imports `PySys_AddAuditHook` and runs scripts in sub-interpreters; the refusals and the crash are what a native audit hook produces when it blocks a call without setting an exception. Image-Line does not document this restriction. | inferred |
| Is there a workable bridge folder? | No. FL can't read the companion's folder, and can't write a reply anywhere it can read (its own script folder allows reads but refuses writes). So the file mailbox has neither an inbound nor an outbound channel. | **no** |
| `bridge.mailbox`, `notes.capture`, `notes.verify`, `notes.patch.update/insert/delete` | Depend on the file bridge | **unsupported** |
| Standard-library imports inside FL | `json`, `hmac`, `hashlib`, `os`, `uuid`, `time`, `calendar` imported (the scripts got as far as reading the pairing file) | observed inside FL |
| Installed Piano Roll reference | Lists `getTimelineSelection`, `setTimelineSelection`, `getDefaultNoteProperties`, `getNextFreeGroupIndex`, `addInputSurface`, `ScriptDialog.execute()`, `Note.clone()`; note fields as used by FL Slacker | documented |
| Factory scripts | Use `flp.Note()`, `note.clone()`, `score.addNote()`, `AddInputCombo`, `AddInputCheckbox`, `AddInputKnob(Int)`, `GetInputValue` (capitalized forms) | documented |
| Embedded interpreter outside FL | `Shared/Python/python.exe` is CPython 3.12.1; signing, atomic write and exclusive claim pass there (`flslacker doctor`), and the script round trip passes against a mocked `flpianoroll` (`tests/embedded_smoke.py`). This does not show what FL allows. | outside FL only |
| Selection semantics, run-once behaviour, dialog cancel, undo grouping, note order after edits, float quantization | – | unverified (M0 cannot run without the bridge) |
| `project.metadata` (MIDI adapter) | MIDI controller scripts may be restricted differently; not run | unverified |

What this means in practice:

- The Slacker scripts detect the refusal before opening any dialog and show an
  `UNSUPPORTED_CAPABILITY (bridge.mailbox)` message. Nothing is recorded or changed.
- **Slacker Probe is read-only** (from 0.1.3): it lists and makes at most one read per folder, plus
  one write attempt (saving its own report). It never writes into a folder FL watches. This is a
  direct response to the 0.1.2 crash — probing writes inside FL is not safe on this build.
- If FL refuses that report save, the probe reports the bridge as **BLOCKED**, even before the
  companion is paired, because the mailbox writes in the same folder. `doctor` applies the same
  rule to reports printed by older probes.
- `flslacker serve` prints a note at startup, `fls_get_capabilities` reports the FL capabilities
  as `unavailable` with the reason, and `flslacker doctor` fails the `compatibility` check.
- The spec (§5.3) requires stopping here rather than silently switching to another control
  mechanism. FL Slacker does not try to get around FL's restriction (for example through
  `ctypes`, Win32 calls, sockets or the registry).

If a later FL build or a Slacker Probe report shows that file access works, `flslacker m0` can
record that locally (`compatibility.local.json` takes precedence over the shipped record).

### Reports FL cannot save

When FL refuses the write, Slacker Probe (and the M0 mutation probe) print the report to FL's
Script output window (VIEW > Script output) between `FLSLACKER-REPORT-BEGIN` and `FLSLACKER-REPORT-END` lines (base64 with
a SHA-256 checksum). Copy the whole block into a text file you create (here `probe-copy.txt`), then:

```bash
flslacker import-report probe-copy.txt
```

Without a file argument, `import-report` reads the pasted block from the terminal (end with
Ctrl+Z and Enter on Windows, Ctrl+D elsewhere).

The importer checks the length and checksum and saves the report in `<data>/probe/`, where
`doctor` summarizes it (`fl.bridge_access`).

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
