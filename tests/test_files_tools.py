import subprocess
from pathlib import Path

from chatgpt_web_oauth_mcp.files import list_files, read_file, read_files, replace_in_file, write_file
from chatgpt_web_oauth_mcp.pathing import resolve_path


def test_resolve_path_uses_workspace_root_for_relative_paths(tmp_path: Path) -> None:
    resolved = resolve_path("src/app.py", tmp_path)
    assert resolved == (tmp_path / "src/app.py").resolve()


def test_list_files_returns_direct_children(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "nested").mkdir()

    result = list_files(tmp_path, recursive=False, limit=20)

    assert result["success"] is True
    assert {entry["name"] for entry in result["entries"]} == {"a.txt", "nested"}
    assert result["truncated"] is False


def test_list_files_entries_include_size_and_mtime(tmp_path: Path) -> None:
    target = tmp_path / "data.txt"
    target.write_text("hello", encoding="utf-8")

    result = list_files(tmp_path, recursive=False, limit=20)

    entry = next(e for e in result["entries"] if e["name"] == "data.txt")
    assert entry["size"] == 5
    assert entry["is_dir"] is False
    assert entry["mtime"] is not None


def test_list_files_supports_offset_pagination(tmp_path: Path) -> None:
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text(name, encoding="utf-8")

    result = list_files(tmp_path, recursive=False, limit=1, offset=1)

    assert result["success"] is True
    assert [entry["name"] for entry in result["entries"]] == ["b.txt"]
    assert result["truncated"] is True
    assert result["next_offset"] == 2


def test_list_files_hides_hidden_entries_by_default(tmp_path: Path) -> None:
    (tmp_path / ".hidden").write_text("x", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("y", encoding="utf-8")

    default = list_files(tmp_path, recursive=False, limit=20)
    assert {e["name"] for e in default["entries"]} == {"visible.txt"}

    including = list_files(tmp_path, recursive=False, limit=20, include_hidden=True)
    assert {e["name"] for e in including["entries"]} == {".hidden", "visible.txt"}


def test_list_files_prunes_default_junk_dirs_when_recursive(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print()", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("x", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.pyc").write_text("x", encoding="utf-8")

    result = list_files(tmp_path, recursive=True, limit=200)

    names = {Path(e["path"]).name for e in result["entries"]}
    assert "app.py" in names
    assert "node_modules" not in names
    assert "__pycache__" not in names
    assert "junk.js" not in names


def test_list_files_respects_gitignore_when_inside_git_repo(tmp_path: Path) -> None:
    # Initialize a tiny git repo with a .gitignore that excludes build/.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("secrets.txt\n", encoding="utf-8")
    (tmp_path / "tracked.py").write_text("print()", encoding="utf-8")
    (tmp_path / "secrets.txt").write_text("PASSWORD=1", encoding="utf-8")

    result = list_files(tmp_path, recursive=True, limit=200)

    names = {Path(e["path"]).name for e in result["entries"]}
    assert "tracked.py" in names
    assert "secrets.txt" not in names
    assert result["filters"]["gitignore_applied"] is True

    # Disabling respect_gitignore should surface the ignored file.
    disabled = list_files(tmp_path, recursive=True, limit=200, respect_gitignore=False)
    names_disabled = {Path(e["path"]).name for e in disabled["entries"]}
    assert "secrets.txt" in names_disabled
    assert disabled["filters"]["gitignore_applied"] is False


def test_list_files_accepts_custom_exclude_patterns(tmp_path: Path) -> None:
    (tmp_path / "keep.py").write_text("x", encoding="utf-8")
    (tmp_path / "skip.log").write_text("y", encoding="utf-8")

    result = list_files(
        tmp_path, recursive=False, limit=20, exclude_patterns=["*.log"]
    )

    names = {e["name"] for e in result["entries"]}
    assert names == {"keep.py"}


def test_read_file_supports_offset_and_limit(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")

    result = read_file(target, offset=2, limit=2, max_lines=50, max_bytes=4096)

    assert result["success"] is True
    assert result["content"] == "two\nthree"
    assert result["next_offset"] == 4


def test_read_files_returns_multiple_results_in_order(tmp_path: Path) -> None:
    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    first.write_text("alpha\nbeta\n", encoding="utf-8")
    second.write_text("gamma\ndelta\n", encoding="utf-8")

    result = read_files([first, second], offset=1, limit=1, max_lines=50, max_bytes=4096)

    assert result["success"] is True
    assert [item["path"] for item in result["results"]] == [str(first), str(second)]
    assert [item["content"] for item in result["results"]] == ["alpha", "gamma"]


def test_read_file_reports_line_unit_and_language(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = read_file(target, offset=2, limit=1, max_lines=50, max_bytes=4096)

    assert result["success"] is True
    assert result["content"] == "two"
    assert result["offset_unit"] == "lines"
    assert result["start_line"] == 2
    assert result["end_line"] == 2
    assert result["language"] in {"python", "x-python"}


def test_read_file_can_include_line_numbers(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    result = read_file(
        target,
        offset=2,
        limit=2,
        max_lines=50,
        max_bytes=4096,
        include_line_numbers=True,
    )

    assert result["success"] is True
    assert result["content"] == "2: beta\n3: gamma"
    assert result["start_line"] == 2
    assert result["end_line"] == 3


def test_write_file_creates_parent_directories(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "file.txt"

    result = write_file(target, content="hello")

    assert result["success"] is True
    assert target.read_text(encoding="utf-8") == "hello"
    assert result["bytes_written"] == 5


def test_write_file_dry_run_does_not_touch_disk(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "file.txt"

    result = write_file(target, content="hello", dry_run=True)

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["written"] is False
    assert target.exists() is False


def test_replace_in_file_requires_unique_match(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("print('before')\n", encoding="utf-8")

    result = replace_in_file(target, old_text="before", new_text="after")

    assert result["success"] is True
    assert "after" in target.read_text(encoding="utf-8")
    assert result["replacements"] == 1


def test_replace_in_file_returns_candidates_when_not_found(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text(
        "def greet(name):\n    print('hello ' + name)\n\n"
        "def farewell(name):\n    print('bye ' + name)\n",
        encoding="utf-8",
    )

    result = replace_in_file(target, old_text="print('hi ' + name)", new_text="x")

    assert result["success"] is False
    assert result["error"]["code"] == "match_not_found"
    candidates = result["candidates"]
    assert isinstance(candidates, list) and candidates
    top = candidates[0]
    assert {"line", "similarity", "snippet"} <= set(top)
    # Top suggestion should point at one of the two print(...) lines.
    assert "print(" in top["snippet"]


def test_replace_in_file_returns_match_lines_when_not_unique(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("x\nTODO\ny\nTODO\nz\n", encoding="utf-8")

    result = replace_in_file(target, old_text="TODO", new_text="DONE")

    assert result["success"] is False
    assert result["error"]["code"] == "match_not_unique"
    assert result["occurrences"] == 2
    assert result["match_lines"] == [2, 4]


def test_replace_in_file_rejects_empty_old_text(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("hi\n", encoding="utf-8")

    result = replace_in_file(target, old_text="", new_text="x")

    assert result["success"] is False
    assert result["error"]["code"] == "empty_old_text"


def test_replace_in_file_can_replace_all_matches(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("before\nbefore\n", encoding="utf-8")

    result = replace_in_file(target, old_text="before", new_text="after", replace_all=True)

    assert result["success"] is True
    assert target.read_text(encoding="utf-8") == "after\nafter\n"
    assert result["replacements"] == 2


def test_replace_in_file_dry_run_keeps_original_content(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("before\n", encoding="utf-8")

    result = replace_in_file(target, old_text="before", new_text="after", dry_run=True)

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["written"] is False
    assert target.read_text(encoding="utf-8") == "before\n"
