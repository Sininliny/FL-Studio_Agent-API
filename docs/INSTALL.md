# Installing FL Slacker

## Requirements

- Windows 10/11 with FL Studio 2026 (tested installation: build 26.1.6.5639). macOS paths exist
  but are untested.
- Python 3.11 or newer for the companion (tested with 3.12). FL's own embedded interpreter is used
  only for the FL-side scripts; they need no extra packages.
- Optional: [Ollama](https://ollama.com) with a tool-capable model, for the local agent.

## 1. Install the companion

Create a virtual environment **outside** OneDrive or any other synchronized folder, activate it,
then install the pinned dependencies and the package:

```bash
python -m pip install -r requirements.lock
```

```bash
python -m pip install --no-deps -e .
```

`flslacker` is only on `PATH` while that environment is active, so activate it in every new
terminal. In PowerShell, where running `Activate.ps1` is blocked by default, allow it for the
current window only:

```powershell
Set-ExecutionPolicy -Scope Process Bypass; & "$env:USERPROFILE\.venvs\fl-slacker\Scripts\Activate.ps1"
```

(Replace the path with your environment.) Alternatively, call the executable directly:
`& "<venv>\Scripts\flslacker.exe" doctor`.

Check the environment:

```bash
flslacker doctor
```

`doctor` finds FL installations, checks the installed Piano Roll reference, runs a bridge
self-test with FL's embedded `python.exe`, reports installed adapters, probe reports and the
compatibility record for your build. It writes `doctor-report.json` to the data folder.

### Data folder

Runtime data lives in a private per-user folder, never beside `.flp` projects:

| OS | Default |
|---|---|
| Windows | `%LOCALAPPDATA%\FLSlacker` |
| macOS | `~/Library/Application Support/FLSlacker` |

Override it with `--home` or the `FLSLACKER_HOME` environment variable. FL-side scripts use the
same rule, so if you override it, FL must see the same variable. `serve` refuses to run from a
synchronized folder.

Contents: `config.json`, `credentials.json` (agent token), `ui-credentials.json` (UI token),
`bridge/pairing.json`, `mailbox/`, `data/flslacker.sqlite3` (journal, snapshots, plans, receipts),
`probe/`, `logs/`, and after M0 `compatibility.local.json`.

## 2. Install the FL-side scripts

```bash
flslacker install-adapters
```

The installer looks for FL's user data folder (default `Documents\Image-Line\FL Studio`; check
FL's *Options > File settings > User data folder*) and asks when it cannot find it
(`--fl-user-dir` sets it explicitly). It shows every file it will write and asks before writing:

```text
<FL user data>/Settings/Piano roll scripts/Slacker/Slacker Capture.pyscript
<FL user data>/Settings/Piano roll scripts/Slacker/Slacker Apply.pyscript
<FL user data>/Settings/Piano roll scripts/Slacker/Slacker Probe.pyscript
```

It never overwrites files it did not write (a manifest records its own files) and never changes
factory scripts or MIDI settings. Options: `--dry-run`, `--yes`, `--midi` (optional metadata
adapter), `--dev` (M0 mutation probe, disposable projects only), `--force` (replace FL Slacker
files you edited).

In FL, the scripts appear in the Piano Roll menu under **Tools > Scripting > Slacker**. Restart
FL if they do not show up.

### Optional MIDI metadata adapter

`--midi` installs `Settings/Hardware/Slacker/device_Slacker.py`. It is read-only: it reports
tempo, transport state and channel/pattern/mixer names, and tells the companion when a project
loads. To use it you need a dedicated MIDI input (for example a virtual port created with
loopMIDI on Windows). In FL, open *Options > MIDI settings*, enable that input and choose
**FL Slacker (metadata)** as its controller type. FL Slacker never changes MIDI settings for you.
It stays `unavailable` in capabilities until it is verified on your build.

## 3. Run the companion

```bash
flslacker serve
```

It listens on `http://127.0.0.1:8765` only (`--port` to change). Every start creates a new
session. Stop it with Ctrl+C; unclaimed FL requests then expire.

Open the UI from another terminal. The command prints a one-time code and opens the browser:

```bash
flslacker ui
```

### Try it without FL

```bash
flslacker serve --fake
```

Demo mode uses a separate data folder (`<home>/fake-demo`) and a simulated Piano Roll that
answers requests automatically. Use `flslacker --home <home>/fake-demo ui` to sign in.

### Enable writes for your FL build

Out of the box, note writes are `unavailable` because FL Slacker has not been verified inside
your FL build. Run the M0 checklist once (see [COMPATIBILITY.md](COMPATIBILITY.md)):

```bash
flslacker serve --allow-unverified-host
```

```bash
flslacker m0
```

Then restart `flslacker serve` normally. Save your work before M0 and use a new, disposable
project.

### "UNSUPPORTED_CAPABILITY (bridge.mailbox)" in FL

Some FL builds (observed on 26.1.6.5639) refuse file access to Piano Roll scripts. The Slacker
scripts then show this message before opening a dialog and change nothing; before 0.1.1 the same
condition appeared as a `SystemError ... returned NULL without setting an exception` traceback.
The file bridge cannot work on such a build, and M0 cannot run there. `flslacker serve` and
`flslacker doctor` say so. To record exactly what FL allows, run **Slacker Probe** and, if it
reports that it could not save the report, copy the printed block from *VIEW > Script output*
into a text file you create (here `probe-copy.txt` in the current folder) and import it:

```bash
flslacker import-report probe-copy.txt
```

Or skip the file: run `flslacker import-report` with no argument, paste the block, then press
Ctrl+Z and Enter (Ctrl+D on macOS). Then `flslacker doctor` shows the result under `fl.bridge_access`. See
[COMPATIBILITY.md](COMPATIBILITY.md).

## 4. Connect external agents (MCP)

`flslacker mcp` is an MCP stdio server that forwards to the running companion with the agent
token. Generic configuration:

```json
{"command": "flslacker", "args": ["mcp"]}
```

Use the full path to `flslacker.exe` in your virtual environment if it is not on `PATH`.

Claude Desktop (`claude_desktop_config.json`):

```json
{"mcpServers": {"flslacker": {"command": "C:\\path\\to\\venv\\Scripts\\flslacker.exe", "args": ["mcp"]}}}
```

Claude Code:

```bash
claude mcp add flslacker -- flslacker mcp
```

Give agents [AGENT_GUIDE.md](AGENT_GUIDE.md). Agents can read and propose; to apply, you approve a
plan in the UI or issue a bounded grant there.

## 5. Local Ollama agent (optional)

```bash
ollama pull qwen3
```

```bash
flslacker config ollama_model qwen3
```

Restart `flslacker serve`; the UI then shows a *Local agent* panel. From the terminal:

```bash
flslacker agent "Capture my selected notes and suggest fixes for notes that break the motif"
```

Only loopback Ollama URLs are allowed while `local_only` is true (the default). The model must
support tool calling. Model installation is always your own action.

## Uninstall

```bash
flslacker uninstall-adapters
```

This removes only files FL Slacker installed and unmodified (`--force` also removes ones you
edited). History is kept unless you add `--purge-data`. Then remove the virtual environment.

## Configuration keys

`flslacker config` shows the current values; `flslacker config <key> <value>` sets one.

| Key | Default | Meaning |
|---|---|---|
| `port` | 8765 | Loopback HTTP port |
| `fl_user_dir` | detected | FL user data folder |
| `ollama_url` | `http://127.0.0.1:11434` | Ollama endpoint |
| `ollama_model` | none | Installed tool-capable model |
| `local_only` | true | Refuse non-loopback model endpoints |
| `allow_unverified_host` | false | Enable writes without a verified M0 record |
| `capture_ttl_minutes` | 30 | Capture request lifetime |
| `apply_ttl_minutes` | 20 | Apply request lifetime |
| `plan_ttl_minutes` | 30 | Plan lifetime |
| `outcome_timeout_minutes` | 10 | Claimed-but-silent FL work becomes `outcome_unknown` |
