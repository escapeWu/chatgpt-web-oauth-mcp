from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


def _descriptors() -> dict[str, dict[str, object]]:
    from chatgpt_web_oauth_mcp import server

    async def scenario() -> dict[str, dict[str, object]]:
        list_tools = getattr(server.mcp, "_list_tools")
        try:
            tools = await list_tools()
        except TypeError:
            tools = await list_tools(None)
        return {
            tool.name: {
                "parameters": tool.parameters,
                "annotations": tool.annotations.model_dump(exclude_none=True),
            }
            for tool in tools
        }

    return asyncio.run(scenario())


def _fn(name: str):
    from chatgpt_web_oauth_mcp import server

    tool = getattr(server, name)
    return tool.fn if hasattr(tool, "fn") else tool


def test_plm_web_tools_are_registered_as_narrow_non_destructive_tools() -> None:
    descriptors = _descriptors()
    for name in ("plm_skeleton", "plm_check"):
        annotations = descriptors[name]["annotations"]
        assert annotations["readOnlyHint"] is False
        assert annotations["destructiveHint"] is False
        assert annotations["openWorldHint"] is False
    assert descriptors["plm_skeleton"]["parameters"]["properties"]["result"]["enum"] == [
        "KEEP",
        "UPDATE",
        "INVALIDATE",
        "INITIAL",
        "BLOCKED",
    ]
    assert set(descriptors["plm_check"]["parameters"]["required"]) == {"context", "symbol", "decision"}


def _stub_scripts(tmp_path: Path) -> Path:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "web_compile.py").write_text(
        "def execute(command, context, symbol, **kw):\n"
        "    return {'ok': True, 'command': command, 'context': context, 'symbol': symbol, 'kw': kw}\n"
    )
    return scripts


def test_plm_tools_delegate_to_the_skill_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHATGPT_MCP_PLM_WEB_SCRIPTS", str(_stub_scripts(tmp_path)))

    skeleton = _fn("plm_skeleton")("/ctx", "ALABUSDT", "KEEP")
    assert skeleton["command"] == "skeleton" and skeleton["kw"] == {"result": "KEEP"}

    check = _fn("plm_check")("/ctx", "ALABUSDT", {"result": "KEEP"})
    assert check["command"] == "check"
    assert check["kw"]["bundle"] == {"decision": {"result": "KEEP"}, "battlemap_patch": None, "battlemap": None}


def test_plm_tools_report_a_missing_skill_instead_of_raising(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHATGPT_MCP_PLM_WEB_SCRIPTS", str(tmp_path / "nowhere"))
    result = _fn("plm_check")("/ctx", "ALABUSDT", {})
    assert result["ok"] is False
    assert result["error"].startswith("PLM_WEB_TOOLS_UNAVAILABLE")
    json.dumps(result)
