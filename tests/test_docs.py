"""Documentation that other components depend on stays in sync."""

from __future__ import annotations

import re
from pathlib import Path

from flslacker.contracts.errors import ErrorCode
from flslacker.contracts.tools import TOOLS
from flslacker.providers.ollama import system_prompt

DOCS = Path(__file__).resolve().parents[1] / "docs"


def test_agent_guide_binding_block_matches_system_prompt():
    text = (DOCS / "AGENT_GUIDE.md").read_text(encoding="utf-8")
    block = re.search(r"<!-- BEGIN BINDING.*?-->\s*```text\n(.*?)```\s*<!-- END BINDING -->", text, re.S).group(1)
    assert block.strip() == system_prompt().strip()


def test_api_doc_lists_every_tool_and_error_code():
    text = (DOCS / "API.md").read_text(encoding="utf-8")
    for tool in TOOLS:
        assert f"`{tool.name}`" in text, tool.name
    for code in ErrorCode:
        assert f"`{code.value}`" in text, code.value


def test_required_docs_exist():
    for name in ("SPEC.md", "INSTALL.md", "API.md", "AGENT_GUIDE.md", "COMPATIBILITY.md", "RECOVERY.md"):
        assert (DOCS / name).stat().st_size > 1000, name
