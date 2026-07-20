from __future__ import annotations

import subprocess
from pathlib import Path

from chatgpt_web_oauth_mcp.gitops import (
    git_blame,
    git_commit,
    git_diff,
    git_log,
    git_show,
    git_status,
)
from chatgpt_web_oauth_mcp.response_budget import ResponseBudget, render_json_payload


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True, capture_output=True, text=True)


def test_git_status_reports_staged_unstaged_and_untracked(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)

    tracked.write_text("two\n", encoding="utf-8")
    staged = tmp_path / "staged.txt"
    staged.write_text("stage me\n", encoding="utf-8")
    subprocess.run(["git", "add", "staged.txt"], cwd=tmp_path, check=True, capture_output=True, text=True)
    (tmp_path / "new.txt").write_text("new\n", encoding="utf-8")

    result = git_status(cwd=tmp_path)

    assert result["success"] is True
    assert result["clean"] is False
    assert "tracked.txt" in result["unstaged"]
    assert "staged.txt" in result["staged"]
    assert "new.txt" in result["untracked"]


def test_git_diff_returns_unified_diff(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    target = tmp_path / "app.py"
    target.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    target.write_text("after\n", encoding="utf-8")

    result = git_diff(cwd=tmp_path)

    assert result["success"] is True
    assert result["files"] == ["app.py"]
    assert "-before" in result["diff"]
    assert "+after" in result["diff"]


def test_git_commit_can_stage_paths_and_return_commit_metadata(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    target = tmp_path / "feature.txt"
    target.write_text("hello\n", encoding="utf-8")

    result = git_commit(cwd=tmp_path, message="feat: add feature file", paths=["feature.txt"])

    assert result["success"] is True
    assert len(result["commit"]) == 40
    assert result["summary"] == "feat: add feature file"


def test_git_log_returns_recent_commits(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    target = tmp_path / "note.txt"
    target.write_text("v1\n", encoding="utf-8")
    git_commit(cwd=tmp_path, message="feat: add note", paths=["note.txt"])
    target.write_text("v2\n", encoding="utf-8")
    git_commit(cwd=tmp_path, message="fix: update note", paths=["note.txt"])

    result = git_log(cwd=tmp_path, limit=2)

    assert result["success"] is True
    assert [entry["summary"] for entry in result["entries"]] == ["fix: update note", "feat: add note"]


def test_git_diff_reports_per_file_stats(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("a1\na2\n", encoding="utf-8")
    b.write_text("b1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    a.write_text("a1\nCHANGED\n", encoding="utf-8")
    b.write_text("b1\nEXTRA\n", encoding="utf-8")

    result = git_diff(cwd=tmp_path)

    assert result["success"] is True
    assert sorted(result["files"]) == ["a.txt", "b.txt"]
    by_path = {entry["path"]: entry for entry in result["file_diffs"]}
    assert by_path["a.txt"]["added"] == 1 and by_path["a.txt"]["removed"] == 1
    assert by_path["b.txt"]["added"] == 1 and by_path["b.txt"]["removed"] == 0
    assert by_path["a.txt"]["diff"].startswith("diff --git ")
    assert by_path["b.txt"]["truncated"] is False


def test_git_diff_per_file_truncation_is_independent(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("seed\n", encoding="utf-8")
    b.write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    a.write_text("seed\n" + "huge line " * 200 + "\n", encoding="utf-8")
    b.write_text("seed\nsmall change\n", encoding="utf-8")

    result = git_diff(cwd=tmp_path, per_file_max_bytes=200)

    by_path = {entry["path"]: entry for entry in result["file_diffs"]}
    assert by_path["a.txt"]["truncated"] is True
    assert by_path["b.txt"]["truncated"] is False
    # Small file's change is still visible after large file was truncated.
    assert "small change" in by_path["b.txt"]["diff"]


def test_git_diff_token_budget_returns_byte_continuation(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    target = tmp_path / "large.txt"
    target.write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    target.write_text("seed\n" + "changed 内容\n" * 500, encoding="utf-8")

    result = git_diff(cwd=tmp_path, max_tokens=500, max_bytes=1_000_000)
    full_diff = subprocess.run(
        ["git", "diff", "--no-color"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    ).stdout

    assert ResponseBudget(max_tokens=500).count_tokens(render_json_payload(result)) <= 500
    assert result["partial"] is True
    assert result["stop_reason"] == "token_budget"
    assert result["next_offset"] == len(result["diff"].encode("utf-8"))
    assert full_diff.startswith(result["diff"].encode("utf-8"))


def test_git_commit_amend_updates_head(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    target = tmp_path / "note.txt"
    target.write_text("v1\n", encoding="utf-8")
    first = git_commit(cwd=tmp_path, message="feat: add note", paths=["note.txt"])
    target.write_text("v2\n", encoding="utf-8")

    amended = git_commit(
        cwd=tmp_path,
        message="feat: add note (amended)",
        paths=["note.txt"],
        amend=True,
    )

    assert amended["success"] is True
    assert amended["amended"] is True
    assert amended["commit"] != first["commit"]
    log = git_log(cwd=tmp_path, limit=5)
    assert [entry["summary"] for entry in log["entries"]] == ["feat: add note (amended)"]


def test_git_commit_allow_empty_creates_commit_without_changes(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "seed.txt").write_text("s\n", encoding="utf-8")
    git_commit(cwd=tmp_path, message="init", paths=["seed.txt"])

    result = git_commit(cwd=tmp_path, message="chore: empty", allow_empty=True)

    assert result["success"] is True
    assert result["allow_empty"] is True
    assert result["summary"] == "chore: empty"


def test_git_commit_rejects_empty_when_allow_empty_false(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "seed.txt").write_text("s\n", encoding="utf-8")
    git_commit(cwd=tmp_path, message="init", paths=["seed.txt"])

    result = git_commit(cwd=tmp_path, message="chore: empty")

    assert result["success"] is False
    assert result["error"]["code"] == "nothing_to_commit"


def test_git_commit_dry_run_reports_plan_without_creating_commit(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    target = tmp_path / "note.txt"
    target.write_text("v1\n", encoding="utf-8")

    result = git_commit(
        cwd=tmp_path,
        message="feat: dry run",
        paths=["note.txt"],
        dry_run=True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["summary"] == "feat: dry run"
    assert "note.txt" in result["files"]
    assert "commit" not in result


def test_git_commit_respects_custom_author(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "file.txt").write_text("x\n", encoding="utf-8")

    result = git_commit(
        cwd=tmp_path,
        message="feat: thing",
        paths=["file.txt"],
        author="Alice <alice@example.com>",
    )

    assert result["success"] is True
    log = git_log(cwd=tmp_path, limit=1)
    assert log["entries"][0]["author"] == "Alice"


def test_git_show_returns_commit_metadata_and_diff(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    target = tmp_path / "note.txt"
    target.write_text("v1\n", encoding="utf-8")
    git_commit(cwd=tmp_path, message="init", paths=["note.txt"])
    target.write_text("v2\n", encoding="utf-8")
    committed = git_commit(cwd=tmp_path, message="fix: update note", paths=["note.txt"])

    result = git_show(cwd=tmp_path, ref=committed["commit"])

    assert result["success"] is True
    assert result["commit"] == committed["commit"]
    assert result["summary"] == "fix: update note"
    assert result["files"] == ["note.txt"]
    assert "-v1" in result["diff"]
    assert "+v2" in result["diff"]


def test_git_show_fails_for_unknown_ref(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    git_commit(cwd=tmp_path, message="init", paths=["a.txt"])

    result = git_show(cwd=tmp_path, ref="does-not-exist")

    assert result["success"] is False
    assert result["error"]["code"] == "git_show_failed"
    assert result["ref"] == "does-not-exist"


def test_git_blame_returns_per_line_commit_info(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    target = tmp_path / "poem.txt"
    target.write_text("roses\nviolets\n", encoding="utf-8")
    git_commit(cwd=tmp_path, message="init", paths=["poem.txt"])
    target.write_text("roses\nviolets\nsugar\n", encoding="utf-8")
    second = git_commit(cwd=tmp_path, message="add sugar", paths=["poem.txt"])

    result = git_blame(cwd=tmp_path, path="poem.txt")

    assert result["success"] is True
    assert len(result["entries"]) == 3
    last = result["entries"][-1]
    assert last["line"] == 3
    assert last["content"] == "sugar"
    assert last["commit"] == second["commit"]
    assert last["summary"] == "add sugar"


def test_git_blame_line_range_restricts_entries(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    target = tmp_path / "lines.txt"
    target.write_text("a\nb\nc\nd\n", encoding="utf-8")
    git_commit(cwd=tmp_path, message="init", paths=["lines.txt"])

    result = git_blame(cwd=tmp_path, path="lines.txt", start_line=2, end_line=3)

    assert result["success"] is True
    assert [entry["line"] for entry in result["entries"]] == [2, 3]
    assert [entry["content"] for entry in result["entries"]] == ["b", "c"]
