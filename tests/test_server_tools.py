from __future__ import annotations

import asyncio
import shlex
import sys
from pathlib import Path

from chatgpt_web_oauth_mcp.executors import ExecutorRegistry
from chatgpt_web_oauth_mcp.shell import MAX_COMMAND_TIMEOUT_SECONDS


def _call(tool, *args, **kwargs):
    fn = tool.fn if hasattr(tool, "fn") else tool
    result = fn(*args, **kwargs)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def _python_cmd(code: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def test_server_apply_patch_tool_updates_file(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    target = tmp_path / "note.txt"
    target.write_text("hello\nworld\n", encoding="utf-8")

    result = _call(
        server.apply_patch,
        patch="\n".join(
            [
                "*** Begin Patch",
                f"*** Update File: {target}",
                "@@",
                " hello",
                "-world",
                "+there",
                "*** End Patch",
            ]
        )
    )

    assert result["success"] is True
    assert target.read_text(encoding="utf-8") == "hello\nthere\n"


def test_server_read_text_tool_returns_multiple_file_results(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    first.write_text("alpha\n", encoding="utf-8")
    second.write_text("beta\n", encoding="utf-8")

    result = _call(server.read_text, paths=[str(first), str(second)])

    assert result["success"] is True
    assert result["mode"] == "batch"
    assert [item["content"] for item in result["results"]] == ["alpha", "beta"]


def test_server_search_tool_unifies_regex_text_and_glob(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    first = tmp_path / "one.py"
    second = tmp_path / "two.txt"
    first.write_text("alpha\nTODO: fix me\n", encoding="utf-8")
    second.write_text("beta\nTODO: docs\n", encoding="utf-8")

    glob_result = _call(server.search, mode="glob", path=str(tmp_path), pattern="*.py")
    text_result = _call(server.search, mode="text", path=str(tmp_path), query="TODO", limit=10)
    regex_result = _call(
        server.search,
        mode="regex",
        path=str(tmp_path),
        pattern=r"TODO:\s+\w+",
        output_mode="files_with_matches",
    )

    assert glob_result["success"] is True
    assert glob_result["mode"] == "glob"
    assert [Path(item["path"]).name for item in glob_result["matches"]] == ["one.py"]

    assert text_result["success"] is True
    assert text_result["mode"] == "text"
    assert len(text_result["matches"]) == 2

    assert regex_result["success"] is True
    assert regex_result["mode"] == "regex"
    assert {Path(path).name for path in regex_result["files"]} == {"one.py", "two.txt"}


def test_server_search_supports_batch_modes(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    first = tmp_path / "one.py"
    second = tmp_path / "two.txt"
    first.write_text("alpha\nTODO: fix me\n", encoding="utf-8")
    second.write_text("beta\nTODO: docs\n", encoding="utf-8")

    sequential = _call(
        server.search,
        mode="sequential",
        path=str(tmp_path),
        queries=[
            {"mode": "glob", "pattern": "*.py"},
            {"mode": "text", "query": "TODO", "limit": 10},
        ],
    )
    parallel = _call(
        server.search,
        mode="parallel",
        path=str(tmp_path),
        queries=[
            {"mode": "regex", "pattern": r"TODO:\s+\w+", "output_mode": "files_with_matches"},
            {"mode": "text", "path": str(first), "query": "alpha"},
        ],
        max_concurrency=2,
    )

    assert sequential["success"] is True
    assert sequential["mode"] == "batch"
    assert sequential["execution_mode"] == "sequential"
    assert sequential["max_concurrency"] == 1
    assert [item["index"] for item in sequential["results"]] == [0, 1]
    assert [Path(item["path"]).name for item in sequential["results"][0]["matches"]] == ["one.py"]
    assert len(sequential["results"][1]["matches"]) == 2

    assert parallel["success"] is True
    assert parallel["execution_mode"] == "parallel"
    assert parallel["max_concurrency"] == 2
    assert [item["index"] for item in parallel["results"]] == [0, 1]
    assert {Path(path).name for path in parallel["results"][0]["files"]} == {"one.py", "two.txt"}
    assert parallel["results"][1]["matches"][0]["path"] == str(first)


def test_server_search_batch_validates_inputs(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    empty = _call(server.search, mode="parallel", queries=[])
    too_much_concurrency = _call(
        server.search,
        mode="parallel",
        path=str(tmp_path),
        queries=[{"mode": "glob", "pattern": "*.py"}],
        max_concurrency=4,
    )

    assert empty["success"] is False
    assert empty["error"]["code"] == "invalid_arguments"
    assert too_much_concurrency["success"] is False
    assert too_much_concurrency["error"]["code"] == "invalid_arguments"


def test_server_run_command_supports_batch_modes(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    sequential = _call(
        server.run_command,
        commands=[_python_cmd("print('one')"), _python_cmd("print('two')")],
        cwd=str(tmp_path),
        timeout=5,
        mode="sequential",
    )
    parallel = _call(
        server.run_command,
        commands=[_python_cmd("print('red')"), _python_cmd("print('blue')")],
        cwd=str(tmp_path),
        timeout=5,
        mode="parallel",
        max_concurrency=2,
    )

    assert sequential["success"] is True
    assert sequential["mode"] == "batch"
    assert sequential["execution_mode"] == "sequential"
    assert [item["stdout"].strip() for item in sequential["results"]] == ["one", "two"]

    assert parallel["success"] is True
    assert parallel["execution_mode"] == "parallel"
    assert parallel["max_concurrency"] == 2
    assert [item["stdout"].strip() for item in parallel["results"]] == ["red", "blue"]


def test_server_run_command_timeout_limit_requires_force(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    rejected = _call(
        server.run_command,
        command="echo hi",
        cwd=str(tmp_path),
        timeout=MAX_COMMAND_TIMEOUT_SECONDS + 1,
    )
    forced = _call(
        server.run_command,
        command=_python_cmd("print('forced')"),
        cwd=str(tmp_path),
        timeout=MAX_COMMAND_TIMEOUT_SECONDS + 1,
        force=True,
    )

    assert rejected["success"] is False
    assert rejected["error"]["code"] == "timeout_exceeds_limit"
    assert rejected["error"]["approval_required"] is True
    assert forced["success"] is True
    assert forced["force"] is True
    assert forced["stdout"].strip() == "forced"


def test_server_read_text_supports_single_and_batch_modes(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    first.write_text("alpha\nbeta\n", encoding="utf-8")
    second.write_text("gamma\ndelta\n", encoding="utf-8")

    single = _call(server.read_text, path=str(first), start_line=2, line_limit=1)
    batch = _call(
        server.read_text,
        paths=[str(first), str(second)],
        start_line=1,
        line_limit=1,
    )

    assert single["success"] is True
    assert single["mode"] == "single"
    assert single["content"] == "beta"

    assert batch["success"] is True
    assert batch["mode"] == "batch"
    assert [item["content"] for item in batch["results"]] == ["alpha", "gamma"]


def test_server_read_text_can_include_line_numbers(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    first = tmp_path / "one.txt"
    first.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    single = _call(
        server.read_text,
        path=str(first),
        start_line=2,
        line_limit=2,
        include_line_numbers=True,
    )

    assert single["success"] is True
    assert single["mode"] == "single"
    assert single["content"] == "2: beta\n3: gamma"


def test_server_read_text_requires_exactly_one_path_argument(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    both_missing = _call(server.read_text)
    both_present = _call(server.read_text, path="one.txt", paths=["two.txt"])

    assert both_missing["success"] is False
    assert both_missing["error"]["code"] == "invalid_arguments"
    assert both_present["success"] is False
    assert both_present["error"]["code"] == "invalid_arguments"


def test_server_search_validates_mode_and_required_fields(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    invalid_mode = _call(server.search, mode="unknown", path=str(tmp_path))
    missing_regex_pattern = _call(server.search, mode="regex", path=str(tmp_path))
    missing_glob_pattern = _call(server.search, mode="glob", path=str(tmp_path))

    assert invalid_mode["success"] is False
    assert invalid_mode["error"]["code"] == "invalid_mode"
    assert missing_regex_pattern["success"] is False
    assert missing_regex_pattern["error"]["code"] == "missing_pattern"
    assert missing_glob_pattern["success"] is False
    assert missing_glob_pattern["error"]["code"] == "missing_pattern"


def test_server_search_supports_single_file_path_for_text_and_regex(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    target = tmp_path / "one.py"
    target.write_text("alpha\nTODO: fix me\n", encoding="utf-8")

    text_result = _call(server.search, mode="text", path=str(target), query="TODO")
    regex_result = _call(server.search, mode="regex", path=str(target), pattern=r"TODO:\s+\w+")

    assert text_result["success"] is True
    assert text_result["mode"] == "text"
    assert len(text_result["matches"]) == 1
    assert text_result["matches"][0]["path"] == str(target)

    assert regex_result["success"] is True
    assert regex_result["mode"] == "regex"
    assert len(regex_result["matches"]) == 1
    assert regex_result["matches"][0]["path"] == str(target)


def test_server_delegate_task_accepts_structured_fields(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    old_registry = server.registry
    try:
        server.registry = ExecutorRegistry(codex_command="python3 -c \"print('{\\\"ok\\\": true}')\"")
        result = _call(
            server.delegate_task,
            task="Implement the fallback flow",
            goal="Ship a working fallback task runner",
            cwd=str(tmp_path),
            acceptance_criteria=["Tool returns structured status"],
            verification_commands=["pytest -q"],
            commit_mode="allowed",
            output_schema={"type": "object"},
        )

        assert result["status"] == "succeeded"
        assert result["executor"] == "codex"
        assert result["serial"] is True
        assert result["structured_output"] == {"ok": True}
        assert result["output_schema"] == {"type": "object"}
        assert "task_id" not in result
    finally:
        server.registry = old_registry


def test_server_delegate_task_validation_errors_are_structured(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    old_registry = server.registry
    try:
        server.registry = ExecutorRegistry(codex_command=_python_cmd("print('should-not-run')"))
        missing_task = _call(server.delegate_task, cwd=str(tmp_path))
        invalid_commit_mode = _call(
            server.delegate_task,
            task="Implement the fallback flow",
            cwd=str(tmp_path),
            commit_mode="disallowed",
        )

        assert missing_task["success"] is False
        assert missing_task["status"] == "failed"
        assert missing_task["error"]["code"] == "missing_task_or_goal"
        assert invalid_commit_mode["success"] is False
        assert invalid_commit_mode["status"] == "failed"
        assert invalid_commit_mode["error"]["code"] == "unsupported_commit_mode"
    finally:
        server.registry = old_registry


def test_server_delegate_status_lists_recent_delegates(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    old_registry = server.registry
    try:
        server.registry = ExecutorRegistry(codex_command=_python_cmd("print('ok')"))
        result = _call(
            server.delegate_task,
            task="Run a short status-visible task",
            cwd=str(tmp_path),
        )
        status = _call(server.delegate_status)

        assert result["status"] == "succeeded"
        assert status["success"] is True
        assert status["latest"]["delegate_id"] == result["delegate_id"]
        assert status["latest"]["status"] == "succeeded"
        assert status["latest"]["logs"]["stdout"]
    finally:
        server.registry = old_registry


def test_server_apply_patch_tool_description_uses_generic_patch_language() -> None:
    from chatgpt_web_oauth_mcp import server

    async def scenario() -> str:
        list_tools = getattr(server.mcp, "_list_tools")
        try:
            tools = await list_tools()
        except TypeError:
            tools = await list_tools(None)
        apply_patch_tool = next(tool for tool in tools if tool.name == "apply_patch")
        return apply_patch_tool.description

    description = asyncio.run(scenario())

    assert "codex-style" not in description
    assert "*** Begin Patch" in description


def test_registered_tool_input_schemas_document_parameters() -> None:
    from chatgpt_web_oauth_mcp import server

    async def scenario() -> dict[str, dict[str, object]]:
        list_tools = getattr(server.mcp, "_list_tools")
        try:
            tools = await list_tools()
        except TypeError:
            tools = await list_tools(None)
        return {tool.name: tool.parameters for tool in tools}

    schemas = asyncio.run(scenario())
    missing = []
    for tool_name, schema in schemas.items():
        for param_name, spec in schema.get("properties", {}).items():
            if not spec.get("description"):
                missing.append(f"{tool_name}.{param_name}")

    assert missing == []
    assert schemas["delegate_task"]["properties"]["commit_mode"]["enum"] == [
        "allowed",
        "required",
        "forbidden",
    ]
    for name in ["command", "commands", "mode", "max_concurrency", "force"]:
        assert name in schemas["run_command"]["properties"]
    for name in ["queries", "mode", "max_concurrency"]:
        assert name in schemas["search"]["properties"]
    assert schemas["run_command"]["properties"]["mode"]["enum"] == [
        "sequential",
        "parallel",
    ]
    for name in ["task_id", "files_in_scope", "out_of_scope", "done_means"]:
        assert name in schemas["delegate_task"]["properties"]
    for name in ["delegate_id", "limit", "watch_seconds", "poll_seconds"]:
        assert name in schemas["delegate_status"]["properties"]


def test_server_tools_expose_chatgpt_compatible_annotations() -> None:
    from chatgpt_web_oauth_mcp import server

    async def scenario() -> dict[str, dict[str, object]]:
        list_tools = getattr(server.mcp, "_list_tools")
        try:
            tools = await list_tools()
        except TypeError:
            tools = await list_tools(None)
        return {
            tool.name: {
                "title": tool.title,
                "annotations": tool.annotations.model_dump(exclude_none=True),
            }
            for tool in tools
        }

    descriptors = asyncio.run(scenario())
    annotations = {name: value["annotations"] for name, value in descriptors.items()}

    assert annotations
    assert all(value["title"] for value in descriptors.values())
    assert all(value for value in annotations.values())
    assert annotations["server_info"]["readOnlyHint"] is True
    assert annotations["search"]["readOnlyHint"] is True
    assert annotations["write_file"]["readOnlyHint"] is False
    assert annotations["write_file"]["destructiveHint"] is True
    assert annotations["run_command"]["openWorldHint"] is True
    assert annotations["delegate_task"]["openWorldHint"] is True
    assert annotations["delegate_status"]["readOnlyHint"] is True
    for removed in [
        "run_command_stream",
        "get_task",
        "wait_task",
        "cancel_task",
        "purge_tasks",
        "taskboard_create",
        "taskboard_delegate",
        "taskboard_status",
        "list_skills",
    ]:
        assert removed not in annotations
