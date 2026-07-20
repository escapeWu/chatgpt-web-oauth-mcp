from __future__ import annotations

import os
from pathlib import Path
import subprocess

import chatgpt_web_oauth_mcp.search as search_module
from chatgpt_web_oauth_mcp.files import list_files, read_files
from chatgpt_web_oauth_mcp.response_budget import ResponseBudget, render_json_payload
from chatgpt_web_oauth_mcp.search import grep_files
from chatgpt_web_oauth_mcp.shell import run_commands


def _payload_tokens(payload: object, max_tokens: int) -> int:
    return ResponseBudget(max_tokens=max_tokens).count_tokens(render_json_payload(payload))


def test_search_streams_ripgrep_and_token_pages_without_skipping(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "many.txt"
    target.write_text(
        "".join(f"MATCH {index} " + "内容" * 40 + "\n" for index in range(20)),
        encoding="utf-8",
    )

    def forbidden_run(*args, **kwargs):
        raise AssertionError("grep search must stream through Popen, not subprocess.run")

    monkeypatch.setattr(search_module.subprocess, "run", forbidden_run)
    first = grep_files(
        tmp_path,
        pattern="MATCH",
        glob_pattern=None,
        output_mode="content",
        head_limit=20,
        offset=0,
        max_tokens=500,
    )
    second = grep_files(
        tmp_path,
        pattern="MATCH",
        glob_pattern=None,
        output_mode="content",
        head_limit=20,
        offset=first["next_offset"],
        max_tokens=500,
    )

    assert first["partial"] is True
    assert first["stop_reason"] == "token_budget"
    assert 0 < len(first["matches"]) < 20
    assert first["next_offset"] == len(first["matches"])
    assert _payload_tokens(first, 500) <= 500
    assert second["matches"][0]["line_number"] == len(first["matches"]) + 1


def test_search_bounds_context_and_pending_match_state(tmp_path: Path) -> None:
    target = tmp_path / "dense.txt"
    target.write_text("x" * 2000 + "\ncontext\n", encoding="utf-8")

    invalid_context = grep_files(
        tmp_path,
        pattern="x",
        glob_pattern=None,
        output_mode="content",
        before=1001,
        head_limit=20,
        offset=0,
    )
    bounded_pending = grep_files(
        tmp_path,
        pattern="x",
        glob_pattern=None,
        output_mode="content",
        after=1,
        only_matching=True,
        head_limit=0,
        offset=0,
    )

    assert invalid_context["success"] is False
    assert invalid_context["error"]["code"] == "context_limit_exceeded"
    assert bounded_pending["partial"] is True
    assert bounded_pending["stop_reason"] == "context_buffer_limit"


def test_read_files_uses_one_shared_batch_budget(tmp_path: Path) -> None:
    paths: list[Path] = []
    for index in range(20):
        path = tmp_path / f"{index:02}.txt"
        path.write_text((f"file-{index} " + "x" * 100 + "\n") * 40, encoding="utf-8")
        paths.append(path)

    result = read_files(
        paths,
        offset=1,
        limit=40,
        max_lines=200,
        max_bytes=32768,
        max_tokens=1000,
    )

    assert _payload_tokens(result, 1000) <= 1000
    assert result["partial"] is True
    assert result["stop_reason"] == "token_budget"
    assert result["next_offset"] is not None
    assert len(result["results"]) < len(paths)


def test_run_commands_uses_one_shared_batch_budget(tmp_path: Path) -> None:
    commands = [
        f"python3 -c \"print('{letter}' * 4000)\""
        for letter in ("a", "b", "c", "d")
    ]
    result = run_commands(
        commands=commands,
        cwd=tmp_path,
        timeout=5,
        max_tokens=1000,
        mode="parallel",
    )

    assert result["success"] is True
    assert result["partial"] is True
    assert result["stop_reason"] == "token_budget"
    assert _payload_tokens(result, 1000) <= 1000
    assert len(result["results"]) == len(commands)


def test_list_files_filters_sorts_and_token_pages_stably(tmp_path: Path) -> None:
    hidden = tmp_path / ".hidden"
    hidden.write_text("hidden", encoding="utf-8")
    directory = tmp_path / "folder"
    directory.mkdir()
    small = tmp_path / "small.txt"
    large = tmp_path / "large.txt"
    small.write_text("1", encoding="utf-8")
    large.write_text("2" * 200, encoding="utf-8")
    os.utime(small, (10, 10))
    os.utime(large, (20, 20))

    by_size = list_files(
        tmp_path,
        recursive=False,
        limit=20,
        sort="size",
        files_only=True,
        filter_mode="all",
    )
    directories = list_files(
        tmp_path,
        recursive=False,
        limit=20,
        dirs_only=True,
        filter_mode="all",
    )
    token_page = list_files(
        tmp_path,
        recursive=False,
        limit=20,
        filter_mode="all",
        max_tokens=250,
    )

    assert [entry["name"] for entry in by_size["entries"]][:2] == ["large.txt", ".hidden"]
    assert [entry["name"] for entry in directories["entries"]] == ["folder"]
    assert token_page["partial"] is True
    assert token_page["next_offset"] == len(token_page["entries"])
    assert _payload_tokens(token_page, 250) <= 250
