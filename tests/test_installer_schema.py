"""Installer ownership rules, schema export and the CLI wiring."""

from __future__ import annotations

import json

import pytest

from flslacker import fl_build, installer
from flslacker.cli import main
from flslacker.contracts import schema_export
from flslacker.contracts.tools import TOOLS


@pytest.fixture
def fl_user(tmp_path):
    root = tmp_path / "FL Studio"
    (root / "Settings" / "Piano roll scripts").mkdir(parents=True)
    (root / "Settings" / "Hardware").mkdir(parents=True)
    return root


def test_install_update_conflict_uninstall(fl_user):
    items = installer.plan(fl_user)
    assert {i.path.name for i in items} == {"Slacker Capture.pyscript", "Slacker Apply.pyscript", "Slacker Probe.pyscript"}
    assert all(i.action == "create" for i in items)
    installer.apply(items)
    assert all(i.action == "unchanged" for i in installer.plan(fl_user))

    folder = installer.target_dirs(fl_user)["piano_roll"]
    (folder / "Slacker Probe.pyscript").write_text("# my edits\n", encoding="utf-8")
    plan = {i.path.name: i for i in installer.plan(fl_user)}
    assert plan["Slacker Probe.pyscript"].action == "conflict"
    with pytest.raises(RuntimeError):
        installer.apply(list(plan.values()))
    assert {i.path.name: i.action for i in installer.plan(fl_user, force=True)}["Slacker Probe.pyscript"] == "update"

    foreign = installer.target_dirs(fl_user)["midi"]
    foreign.mkdir(parents=True)
    (foreign / "device_Slacker.py").write_text("# someone else's script\n", encoding="utf-8")
    midi = [i for i in installer.plan(fl_user, include_midi=True, force=True) if i.path.name == "device_Slacker.py"][0]
    assert midi.action == "conflict" and midi.reason == "not written by FL Slacker"

    actions = dict((p.name, a) for p, a in installer.uninstall_plan(fl_user))
    assert actions["Slacker Capture.pyscript"] == "remove"
    assert actions["Slacker Probe.pyscript"].startswith("keep")
    installer.uninstall(fl_user)
    assert sorted(p.name for p in folder.iterdir()) == [".flslacker-manifest.json", "Slacker Probe.pyscript"]
    assert (foreign / "device_Slacker.py").exists()
    installer.uninstall(fl_user, force=True)
    assert not folder.exists()


def test_legacy_generated_files_are_updatable(fl_user):
    folder = installer.target_dirs(fl_user)["piano_roll"]
    folder.mkdir(parents=True)
    old = fl_build.render(fl_build.script("Slacker Capture.pyscript")).replace("SCRIPT_VERSION", "OLD_VERSION")
    (folder / "Slacker Capture.pyscript").write_text(old, encoding="utf-8")
    plan = {i.path.name: i.action for i in installer.plan(fl_user)}
    assert plan["Slacker Capture.pyscript"] == "update"


def test_invalid_fl_user_dir(tmp_path):
    with pytest.raises(ValueError):
        installer.plan(tmp_path / "nowhere")


def test_cli_install_and_status(fl_user, tmp_path, capsys):
    home = tmp_path / "home"
    assert main(["--home", str(home), "install-adapters", "--fl-user-dir", str(fl_user), "--dry-run"]) == 0
    assert "create" in capsys.readouterr().out
    assert main(["--home", str(home), "install-adapters", "--fl-user-dir", str(fl_user), "--yes", "--midi"]) == 0
    assert (fl_user / "Settings" / "Hardware" / "Slacker" / "device_Slacker.py").exists()
    assert json.loads((home / "config.json").read_text(encoding="utf-8"))["fl_user_dir"] == str(fl_user)
    assert main(["--home", str(home), "config", "ollama_model", "qwen3"]) == 0
    assert main(["--home", str(home), "config", "local_only", "false"]) == 0
    config = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert config["ollama_model"] == "qwen3" and config["local_only"] is False
    assert main(["--home", str(home), "uninstall-adapters", "--fl-user-dir", str(fl_user), "--yes"]) == 0
    assert not (fl_user / "Settings" / "Piano roll scripts" / "Slacker").exists()


def test_schema_export(tmp_path):
    written = schema_export.export(tmp_path)
    names = {p.name for p in written}
    assert {"tools.json", "openapi.json", "Plan.json", "Snapshot.json", "Receipt.json"} <= names
    tools = json.loads((tmp_path / "flslacker-1" / "tools.json").read_text(encoding="utf-8"))
    assert [t["name"] for t in tools] == [t.name for t in TOOLS]
    apply = next(t for t in tools if t["name"] == "fls_apply_plan")
    assert apply["inputSchema"]["additionalProperties"] is False
    assert apply["inputSchema"]["properties"]["expected_plan_hash"]["pattern"] == "^[a-f0-9]{64}$"
    assert apply["inputSchema"]["properties"]["idempotency_key"]["format"] == "uuid"
    openapi = json.loads((tmp_path / "flslacker-1" / "openapi.json").read_text(encoding="utf-8"))
    refs = json.dumps(openapi)
    for ref in set(part.split('"')[0] for part in refs.split("#/components/schemas/")[1:]):
        assert ref in openapi["components"]["schemas"], ref


def test_committed_schemas_are_current(tmp_path):
    from pathlib import Path

    fresh = tmp_path / "fresh"
    schema_export.export(fresh)
    committed = Path(__file__).resolve().parents[1] / "schemas" / "flslacker-1"
    for path in (fresh / "flslacker-1").iterdir():
        assert (committed / path.name).read_text(encoding="utf-8") == path.read_text(encoding="utf-8"), (
            f"schemas/{path.name} is stale: run `flslacker export-schemas`")
