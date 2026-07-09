from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from chatgpt_web_oauth_mcp.envtools import env_diff, env_snapshot


def _call(tool, *args, **kwargs):
    fn = tool.fn if hasattr(tool, "fn") else tool
    result = fn(*args, **kwargs)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def test_env_snapshot_returns_basic_fields_and_file_hash(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[project]\nname = 'demo'\n", encoding="utf-8")

    result = env_snapshot(cwd=tmp_path)

    assert result["success"] is True
    assert result["cwd"] == str(tmp_path)
    assert result["platform"]["system"]
    assert result["python"]["executable"]
    assert result["python"]["version"]
    assert "is_venv" in result["python"]
    assert result["git"]["inside_repo"] is False
    assert "available" in result["node"]
    assert "available" in result["java"]
    assert result["files"]["pyproject.toml"]["exists"] is True
    assert result["files"]["pyproject.toml"]["sha256"] == hashlib.sha256(pyproject.read_bytes()).hexdigest()
    assert result["files"]["requirements.txt"]["exists"] is False
    assert result["packages"] == {"included": False}


def test_env_snapshot_in_non_git_directory_does_not_fail(tmp_path: Path) -> None:
    result = env_snapshot(cwd=tmp_path)

    assert result["success"] is True
    assert result["git"]["inside_repo"] is False


def test_env_diff_reports_nested_changes_with_dot_paths() -> None:
    left = {
        "python": {"executable": "/usr/bin/python3", "version": "3.9.6"},
        "files": {"pyproject.toml": {"sha256": "aaa"}},
    }
    right = {
        "python": {"executable": "/repo/.venv/bin/python", "version": "3.11.8"},
        "files": {"pyproject.toml": {"sha256": "bbb"}},
    }

    result = env_diff(left=left, right=right)

    assert result == {
        "success": True,
        "changed": True,
        "changes": {
            "files.pyproject.toml.sha256": ["aaa", "bbb"],
            "python.executable": ["/usr/bin/python3", "/repo/.venv/bin/python"],
            "python.version": ["3.9.6", "3.11.8"],
        },
    }


def test_env_diff_returns_unchanged_for_identical_snapshot() -> None:
    snapshot = {"python": {"version": "3.11.8"}}

    result = env_diff(left=snapshot, right=dict(snapshot))

    assert result["success"] is True
    assert result["changed"] is False
    assert result["changes"] == {}


def test_env_tools_are_registered_with_schema_and_annotations() -> None:
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

    descriptors = asyncio.run(scenario())
    assert descriptors["env_snapshot"]["annotations"]["readOnlyHint"] is True
    assert descriptors["env_diff"]["annotations"]["readOnlyHint"] is True
    assert "cwd" in descriptors["env_snapshot"]["parameters"]["properties"]
    assert "include_packages" in descriptors["env_snapshot"]["parameters"]["properties"]
    assert "left" in descriptors["env_diff"]["parameters"]["properties"]
    assert "right" in descriptors["env_diff"]["parameters"]["properties"]


def test_server_info_includes_env_tools() -> None:
    from chatgpt_web_oauth_mcp import server

    payload = _call(server.server_info)

    assert "env_snapshot" in payload["tools"]
    assert "env_diff" in payload["tools"]
