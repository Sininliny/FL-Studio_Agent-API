"""Command line: serve, doctor, install-adapters, uninstall-adapters, mcp, ui, agent, m0 and build tools."""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import sys
import threading
import webbrowser
from pathlib import Path

from flslacker import __version__


def _settings(args, **overrides):
    from flslacker.config import Settings

    return Settings.load(Path(args.home) if args.home else None, **overrides)


def _fl_user_dir(args, settings) -> Path:
    from flslacker.config import default_fl_user_dir

    chosen = args.fl_user_dir or settings.fl_user_dir or default_fl_user_dir()
    if chosen is None:
        answer = input("FL Studio user data folder (Options > File settings > User data folder): ").strip()
        chosen = answer.strip('"')
    return Path(chosen)


def _setup_logging(settings, level: str) -> None:
    settings.ensure_dirs()
    handler = logging.handlers.RotatingFileHandler(settings.log_dir / "flslacker.log", maxBytes=2_000_000,
                                                   backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.WARNING)
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), handlers=[handler, console])


# ------------------------------------------------------------------ commands


def cmd_serve(args) -> int:
    from flslacker import hostinfo
    from flslacker.config import Credentials, is_synchronized_path
    from flslacker.service.core import Actor, Service
    from flslacker.service.dispatcher import Dispatcher
    from flslacker.transports.client import LocalToolClient
    from flslacker.transports.http import App, Server

    settings = _settings(args, port=args.port, ollama_model=args.ollama_model)
    if args.allow_unverified_host or args.fake:
        settings.allow_unverified_host = True
    if args.fake:
        settings.home = settings.home / "fake-demo"
    if is_synchronized_path(settings.home):
        print(f"Refusing to use {settings.home}: it is inside a synchronized folder. Set --home or FLSLACKER_HOME.",
              file=sys.stderr)
        return 2
    _setup_logging(settings, args.log_level)
    credentials = Credentials.load_or_create(settings)
    service = Service(settings)
    installs = hostinfo.installed_fl()
    service.installed_fl_build = installs[0].version if installs else None
    session = service.start()

    agents = None
    if settings.ollama_model:
        from flslacker.providers.ollama import AgentLoop, AgentRuns, OllamaProvider

        dispatcher = Dispatcher(service)

        def factory():
            provider = OllamaProvider(settings.ollama_url, settings.ollama_model, local_only=settings.local_only)
            return AgentLoop(provider, LocalToolClient(dispatcher, Actor("ollama", "agent")))

        agents = AgentRuns(factory)
    app = App(service, credentials, settings, agents)
    try:
        server = Server(app)
    except OSError as exc:
        service.stop()
        print(f"Cannot listen on 127.0.0.1:{settings.port}: {exc}", file=sys.stderr)
        return 2
    service.run_background()

    stop = threading.Event()
    if args.fake:
        from flslacker.adapters.fake import FakeFl

        fake = FakeFl(settings.home)

        def fake_loop() -> None:
            while not stop.wait(args.fake_delay):
                try:
                    fake.process_pending()
                except Exception:
                    logging.getLogger("flslacker.fake").exception("fake FL failed")

        threading.Thread(target=fake_loop, name="flslacker-fake-fl", daemon=True).start()

    print(f"FL Slacker {__version__} companion on http://127.0.0.1:{server.port}/ui/")
    print(f"  session {session.session_id} (epoch {session.adapter_epoch}); data in {settings.home}")
    if args.fake:
        print("  FAKE FL MODE: a simulated Piano Roll answers requests automatically. Nothing touches FL Studio.")
    elif settings.allow_unverified_host:
        print("  WARNING: --allow-unverified-host enables writes that are not verified on this FL build (M0 mode).")
    print("  Open the UI: run `flslacker ui` in another terminal (prints a one-time login code).")
    print('  MCP clients: {"command": "flslacker", "args": ["mcp"]}')
    print("  Press Ctrl+C to stop.")
    if args.open_ui:
        webbrowser.open(f"http://127.0.0.1:{server.port}/ui/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.httpd.server_close()
        service.stop()
        service.db.close()
    return 0


def cmd_doctor(args) -> int:
    from flslacker import doctor

    settings = _settings(args)
    checks = doctor.run(settings, Path(args.fl_user_dir) if args.fl_user_dir else None,
                        self_test=not args.no_self_test)
    report = doctor.write_report(settings, checks)
    if args.json:
        print(json.dumps([c.__dict__ for c in checks], indent=2, default=str))
    else:
        marks = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL", "info": "INFO"}
        for check in checks:
            print(f"[{marks[check.status]}] {check.name}: {check.detail}")
            if args.verbose and check.data is not None:
                print("       " + json.dumps(check.data, default=str)[:2000])
        print(f"\nFull report: {report}")
    return 1 if any(c.status == "fail" for c in checks) else 0


def cmd_install(args) -> int:
    from flslacker import installer

    settings = _settings(args)
    target = _fl_user_dir(args, settings)
    try:
        items = installer.plan(target, include_midi=args.midi, include_dev=args.dev, force=args.force)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"FL user data folder: {target}")
    for item in items:
        note = f" ({item.reason})" if item.reason else ""
        print(f"  {item.action:9} {item.path}{note}")
    if any(i.action == "conflict" for i in items):
        print("Conflicts found; nothing was changed. Move the files aside or use --force for files FL Slacker wrote.")
        return 1
    if args.dry_run or all(i.action == "unchanged" for i in items):
        return 0
    if not args.yes and not input("Install these files? [y/N] ").strip().lower().startswith("y"):
        print("Nothing was changed.")
        return 1
    installer.apply(items)
    settings.fl_user_dir = str(target)
    settings.save()
    print("Installed. In FL the scripts appear under Piano Roll > Tools > Scripting > Slacker (restart FL if not).")
    if args.midi:
        print("MIDI adapter: select 'FL Slacker (metadata)' as the controller type of a dedicated MIDI input in "
              "Options > MIDI settings yourself; FL Slacker does not change MIDI settings.")
    return 0


def cmd_uninstall(args) -> int:
    import shutil

    from flslacker import installer

    settings = _settings(args)
    target = _fl_user_dir(args, settings)
    actions = installer.uninstall_plan(target, force=args.force)
    for path, action in actions:
        print(f"  {action:6} {path}")
    if not actions:
        print("No FL Slacker files are installed there.")
    if actions and (args.yes or input("Remove these files? [y/N] ").strip().lower().startswith("y")):
        installer.uninstall(target, force=args.force)
        print("Removed FL Slacker's files.")
    if args.purge_data:
        if args.yes or input(f"Also delete history and data in {settings.home}? [y/N] ").strip().lower().startswith("y"):
            shutil.rmtree(settings.home, ignore_errors=True)
            print("History deleted.")
    else:
        print(f"History and journals were kept in {settings.home} (use --purge-data to delete).")
    return 0


def cmd_mcp(args) -> int:
    from flslacker.transports.client import HttpToolClient
    from flslacker.transports.mcp import run_stdio

    run_stdio(HttpToolClient(_settings(args), client_name=args.client_name))
    return 0


def cmd_ui(args) -> int:
    import httpx

    from flslacker.config import Credentials

    settings = _settings(args)
    creds = Credentials.load(settings)
    if creds is None:
        print("Start the companion first: flslacker serve", file=sys.stderr)
        return 2
    try:
        response = httpx.post(f"http://127.0.0.1:{creds.port}/v1/ui/login-code", trust_env=False, timeout=5,
                              headers={"Authorization": f"Bearer {creds.ui_token}"})
        code = response.json()["code"]
    except (httpx.HTTPError, KeyError, ValueError):
        print(f"The companion is not answering on port {creds.port}. Is `flslacker serve` running?", file=sys.stderr)
        return 2
    url = f"http://127.0.0.1:{creds.port}/ui/"
    print(f"Open {url} and enter the one-time code: {code}  (valid 5 minutes)")
    if not args.no_browser:
        webbrowser.open(url)
    return 0


def cmd_agent(args) -> int:
    from flslacker.contracts.errors import FlsError
    from flslacker.providers.ollama import AgentLoop, OllamaProvider
    from flslacker.transports.client import HttpToolClient

    settings = _settings(args)
    model = args.model or settings.ollama_model
    try:
        provider = OllamaProvider(settings.ollama_url, model or "", local_only=settings.local_only)
    except FlsError as exc:
        print(exc.message, file=sys.stderr)
        return 2
    transcript = AgentLoop(provider, HttpToolClient(settings, client_name="ollama-cli")).run(args.prompt)
    for call in transcript.tool_calls:
        print(f"  tool {call['tool']}: {'ok' if call['ok'] else call['code']}")
    if transcript.error:
        print(f"Stopped: {transcript.error['code']}: {transcript.error['message']}", file=sys.stderr)
        return 1
    print(transcript.final or f"(stopped: {transcript.stop_reason})")
    return 0


def cmd_m0(args) -> int:
    from flslacker.m0 import ConsoleOperator, Wizard

    path = Wizard(_settings(args), ConsoleOperator()).run()
    print(path)
    return 0


def cmd_build_scripts(args) -> int:
    from flslacker import fl_build

    out = Path(args.out)
    rendered = fl_build.render_all()
    if args.check:
        stale = [name for name, text in rendered.items()
                 if not (out / name).exists() or (out / name).read_text(encoding="utf-8") != text]
        for name in stale:
            print(f"out of date: {out / name}")
        return 1 if stale else 0
    for path in fl_build.write_all(out):
        print(path)
    return 0


def cmd_export_schemas(args) -> int:
    from flslacker.contracts import schema_export

    for path in schema_export.export(Path(args.out)):
        print(path)
    return 0


def cmd_config(args) -> int:
    settings = _settings(args)
    if args.key is None:
        print(json.dumps({k: getattr(settings, k) for k in settings.CONFIG_KEYS}, indent=2))
        return 0
    if args.key not in settings.CONFIG_KEYS:
        print(f"Unknown key. Choose from: {', '.join(settings.CONFIG_KEYS)}", file=sys.stderr)
        return 2
    current = getattr(settings, args.key)
    value: object = args.value
    if isinstance(current, bool) or args.key in ("local_only", "allow_unverified_host"):
        value = str(args.value).lower() in ("1", "true", "yes", "on")
    elif isinstance(current, int) or args.key.endswith("_minutes") or args.key == "port":
        value = int(args.value)
    elif args.value in ("", "none", "null"):
        value = None
    setattr(settings, args.key, value)
    settings.save()
    print(f"{args.key} = {value!r}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flslacker", description="FL Slacker companion")
    parser.add_argument("--version", action="version", version=f"flslacker {__version__}")
    parser.add_argument("--home", help="private data folder (default: per-user local app data)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the companion (HTTP API, UI and FL bridge)")
    p.add_argument("--port", type=int)
    p.add_argument("--ollama-model")
    p.add_argument("--allow-unverified-host", action="store_true",
                   help="enable writes on an FL build without a verified M0 record (for running M0)")
    p.add_argument("--fake", action="store_true", help="demo mode with a simulated Piano Roll (separate data)")
    p.add_argument("--fake-delay", type=float, default=1.5, help=argparse.SUPPRESS)
    p.add_argument("--open-ui", action="store_true")
    p.add_argument("--log-level", default="INFO")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("doctor", help="check environment, adapters and compatibility evidence")
    p.add_argument("--json", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--fl-user-dir")
    p.add_argument("--no-self-test", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("install-adapters", help="install the Slacker scripts into FL's user data folder")
    p.add_argument("--fl-user-dir")
    p.add_argument("--midi", action="store_true", help="also install the optional MIDI metadata script")
    p.add_argument("--dev", action="store_true", help="also install the M0 mutation probe")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="overwrite FL Slacker files that were edited locally")
    p.add_argument("--yes", "-y", action="store_true")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("uninstall-adapters", help="remove only files FL Slacker installed")
    p.add_argument("--fl-user-dir")
    p.add_argument("--force", action="store_true")
    p.add_argument("--purge-data", action="store_true", help="also delete history (asks first)")
    p.add_argument("--yes", "-y", action="store_true")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("mcp", help="MCP stdio server for external agents (needs `serve` running)")
    p.add_argument("--client-name", default="mcp")
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("ui", help="print a one-time UI login code and open the browser")
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("agent", help="ask the local Ollama agent (needs `serve` running)")
    p.add_argument("prompt")
    p.add_argument("--model")
    p.set_defaults(func=cmd_agent)

    p = sub.add_parser("m0", help="guided M0 host checklist (needs `serve --allow-unverified-host`)")
    p.set_defaults(func=cmd_m0)

    p = sub.add_parser("build-fl-scripts", help="render the FL-side scripts")
    p.add_argument("--out", default="fl_scripts")
    p.add_argument("--check", action="store_true")
    p.set_defaults(func=cmd_build_scripts)

    p = sub.add_parser("export-schemas", help="write JSON Schemas, tool definitions and OpenAPI")
    p.add_argument("--out", default="schemas")
    p.set_defaults(func=cmd_export_schemas)

    p = sub.add_parser("config", help="show or set a config value")
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.set_defaults(func=cmd_config)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
