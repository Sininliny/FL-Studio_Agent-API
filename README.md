# FL Slacker

Agent-safe capture, analysis and **verified** editing of FL Studio Piano Roll notes.

FL Slacker is a local Python companion, a set of small FL-side scripts, and one tool API that
a browser UI, a local Ollama model and external MCP agents all share. The workflow is
**capture → analyze → preview → apply → verify**. Nothing is reported as changed until a fresh
capture from FL matches the predicted result.

> **Status (0.1.3):** everything below runs and is tested against a simulated Piano Roll and FL
> Studio's own embedded Python interpreter. **Inside FL Studio 26.1.6.5639 the file bridge does
> not work:** FL refuses file access to Piano Roll scripts in the FL Slacker folder, so capture and
> apply are unsupported on that build (the scripts say so and change nothing). On other builds the
> note writes stay `unavailable` until you record M0. See
> [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md).

## How it works

```text
Local UI / CLI          External agent          Ollama provider
       \                 MCP stdio                 /
        +----------- Tool dispatcher ------------+
                          |
             Policy + schema + capability checks
                          |
         Snapshot store -> analysis -> immutable plan
                          |
               Serialized execution coordinator
                          |
          signed file mailbox (per-user, not synced)
                          |
   Slacker Capture / Slacker Apply  (run by you inside FL's Piano Roll)
```

- **You stay in control.** FL only changes when you run *Slacker Apply* in the Piano Roll,
  pick the exact job, and confirm the target. Agents can propose; only grants you issue in the
  UI let a plan be applied, and grants are scoped, budgeted and expire.
- **No hidden automation.** No UI automation, no `.flp` rewriting, no process-memory access, no
  undocumented FL APIs. Unsupported things return `UNSUPPORTED_CAPABILITY`.
- **Exact and guarded.** Plans are immutable and hash-locked. FL refuses a plan if the live notes
  differ from the capture in any way. Every write is journaled first, verified afterwards, and
  can be undone with a guarded inverse plan while the score is unchanged.

## Quick start (Windows, Python 3.11+)

In an activated virtual environment (keep it outside OneDrive or other synced folders):

```bash
python -m pip install -r requirements.lock
```

```bash
python -m pip install --no-deps -e .
```

```bash
flslacker doctor
```

```bash
flslacker install-adapters
```

```bash
flslacker serve
```

In a second terminal, open the UI (prints a one-time login code):

```bash
flslacker ui
```

Try it without FL first: `flslacker serve --fake` runs a simulated Piano Roll with separate data.

Full instructions: [docs/INSTALL.md](docs/INSTALL.md).

## Commands

| Command | Purpose |
|---|---|
| `flslacker serve` | Companion: HTTP API + UI on `127.0.0.1:8765`, FL mailbox bridge |
| `flslacker ui` | One-time UI login code, opens the browser |
| `flslacker doctor` | Environment, FL install, adapters, bridge self-test, compatibility evidence |
| `flslacker install-adapters` / `uninstall-adapters` | Add/remove only FL Slacker's own FL-side files |
| `flslacker m0` | Guided host checklist; records verified capabilities for your FL build |
| `flslacker import-report` | Import a Slacker Probe report that FL printed instead of saving |
| `flslacker mcp` | MCP stdio server for external agents (`{"command": "flslacker", "args": ["mcp"]}`) |
| `flslacker agent "…"` | Ask the local Ollama agent |
| `flslacker build-fl-scripts` / `export-schemas` | Regenerate `fl_scripts/` and `schemas/` |

## Documentation

- [docs/SPEC.md](docs/SPEC.md) – specification and implementation notes
- [docs/INSTALL.md](docs/INSTALL.md) – install, FL setup, MCP and Ollama configuration
- [docs/API.md](docs/API.md) – HTTP/MCP tools, data contracts, errors, bridge protocol
- [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md) – binding instructions for agents
- [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) – evidence, known limits, M0 checklist
- [docs/RECOVERY.md](docs/RECOVERY.md) – what to do when an edit is uncertain

## Development

```bash
python -m pytest
```

Tests exercise the real generated FL scripts against a fake `flpianoroll` module (also under
FL's embedded `python.exe` when FL is installed). Mocks cannot certify FL behaviour; that is
what `flslacker m0` is for.

## Layout

```text
src/flslacker/   contracts, service, storage, analysis, providers, adapters, transports, ui, fl_src
fl_scripts/      generated FL-side scripts (Slacker Capture/Apply/Probe, device_Slacker.py)
schemas/         exported JSON Schemas, tool definitions and OpenAPI
docs/            specification and guides
tests/           unit, contract, bridge and host-fixture tests
```

License: see [LICENSE](LICENSE).
