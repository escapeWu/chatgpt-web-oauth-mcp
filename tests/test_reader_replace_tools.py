from __future__ import annotations

import codecs
import asyncio
import hashlib
import multiprocessing
from pathlib import Path
import stat

from PIL import Image
from pypdf import PdfWriter

from chatgpt_web_oauth_mcp.reader import read_path
from chatgpt_web_oauth_mcp.replacing import replace_files
import chatgpt_web_oauth_mcp.replacing as replacing_module
from chatgpt_web_oauth_mcp.response_budget import ResponseBudget, render_json_payload


def _replace_worker(path: str, expected_revision: str, queue) -> None:
    result = replace_files(
        [
            {
                "path": Path(path),
                "expected_revision": expected_revision,
                "rules": [{"pattern": "before", "replacement": "after"}],
            }
        ]
    )
    queue.put(result)


def test_read_text_reports_bom_newline_and_strict_encoding(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_bytes(codecs.BOM_UTF8 + "one\r\ntwo\r\n".encode("utf-8"))

    result = read_path(target, mode="text", max_tokens=600)

    assert result["success"] is True
    assert result["content"] == "one\ntwo"
    assert result["file"]["encoding"] == "utf-8"
    assert result["file"]["bom"] == "utf-8"
    assert result["file"]["newline"] == "crlf"
    assert result["complete"] is True
    assert result["estimated_tokens"] == ResponseBudget().count_tokens(
        render_json_payload(result)
    )


def test_read_text_reports_ambiguous_encoding_and_accepts_explicit_codec(tmp_path: Path) -> None:
    target = tmp_path / "legacy.txt"
    target.write_bytes(b"caf\xe9\r\n")

    automatic = read_path(target, mode="text")
    explicit = read_path(target, mode="text", encoding="windows-1252")

    assert automatic["success"] is False
    assert automatic["error"]["code"] == "encoding_error"
    assert "windows-1252" in automatic["encoding_candidates"]
    assert explicit["success"] is True
    assert explicit["content"] == "café"
    assert explicit["file"]["encoding"] == "cp1252"


def test_read_hex_uses_lossless_token_pagination(tmp_path: Path) -> None:
    target = tmp_path / "data.bin"
    target.write_bytes(bytes(range(256)) * 4)

    result = read_path(target, mode="hex", byte_limit=1024, max_tokens=300)

    rendered_tokens = ResponseBudget(max_tokens=300).count_tokens(render_json_payload(result))
    assert rendered_tokens <= 300
    assert result["partial"] is True
    assert result["next_offset"] == result["end_offset"]
    returned = bytes.fromhex(result["content"])
    assert returned == target.read_bytes()[: len(returned)]


def test_read_image_and_pdf_metadata(tmp_path: Path) -> None:
    image_path = tmp_path / "pixel.png"
    Image.new("RGB", (3, 2), color=(10, 20, 30)).save(image_path)
    image = read_path(image_path, mode="auto")

    pdf_path = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with pdf_path.open("wb") as handle:
        writer.write(handle)
    pdf = read_path(pdf_path, mode="pdf", pages="1")

    assert image["mode"] == "image"
    assert image["image"]["width"] == 3
    assert image["image"]["height"] == 2
    assert image["image"]["content_reference"]["uri"].startswith("file:")
    assert pdf["success"] is True
    assert pdf["page_count"] == 1
    assert pdf["pages"] == [{"page": 1, "text": ""}]


def test_replace_preserves_bom_crlf_permissions_and_returns_revisions(tmp_path: Path) -> None:
    target = tmp_path / "script.txt"
    target.write_bytes(codecs.BOM_UTF8 + b"alpha\r\nalpha\r\n")
    target.chmod(0o640)

    result = replace_files(
        [
            {
                "path": target,
                "rules": [
                    {"pattern": "alpha", "replacement": "beta\nnext", "literal": True}
                ],
            }
        ],
        max_replacements=2,
    )

    assert result["success"] is True
    assert result["total_replacements"] == 2
    assert result["files"][0]["before_revision"] != result["files"][0]["after_revision"]
    assert target.read_bytes() == codecs.BOM_UTF8 + b"beta\r\nnext\r\nbeta\r\nnext\r\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert not list(tmp_path.glob("*.replace-tmp"))


def test_replace_dry_run_and_revision_conflict_do_not_write(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("before\n", encoding="utf-8")

    dry_run = replace_files(
        [{"path": target, "rules": [{"pattern": "before", "replacement": "after"}]}],
        dry_run=True,
    )
    conflict = replace_files(
        [
            {
                "path": target,
                "expected_revision": "0" * 64,
                "rules": [{"pattern": "before", "replacement": "after"}],
            }
        ]
    )

    assert dry_run["success"] is True
    assert dry_run["files"][0]["changed"] is True
    assert conflict["success"] is False
    assert conflict["error"]["code"] == "revision_conflict"
    assert target.read_text(encoding="utf-8") == "before\n"


def test_replace_batch_max_replacements_is_all_or_nothing(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("x x\n", encoding="utf-8")
    second.write_text("x x\n", encoding="utf-8")

    result = replace_files(
        [
            {"path": first, "rules": [{"pattern": "x", "replacement": "y"}]},
            {"path": second, "rules": [{"pattern": "x", "replacement": "y"}]},
        ],
        max_replacements=3,
    )

    assert result["success"] is False
    assert result["error"]["code"] == "max_replacements_exceeded"
    assert first.read_text(encoding="utf-8") == "x x\n"
    assert second.read_text(encoding="utf-8") == "x x\n"


def test_replace_supports_multiple_files_and_ordered_rules(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("Foo 123\n", encoding="utf-8")
    second.write_text("Foo 456\n", encoding="utf-8")

    result = replace_files(
        [
            {
                "path": path,
                "rules": [
                    {
                        "pattern": "foo",
                        "replacement": r"literal\g",
                        "literal": True,
                        "ignore_case": True,
                    },
                    {"pattern": r"\d+", "replacement": "#", "literal": False},
                ],
            }
            for path in (first, second)
        ]
    )

    assert result["success"] is True
    assert result["changed_files"] == 2
    assert result["total_replacements"] == 4
    assert first.read_text(encoding="utf-8") == "literal\\g #\n"
    assert second.read_text(encoding="utf-8") == "literal\\g #\n"


def test_replace_cross_process_lock_and_cas_allow_only_one_writer(tmp_path: Path) -> None:
    target = tmp_path / "shared.txt"
    target.write_text("before\n", encoding="utf-8")
    expected = hashlib.sha256(target.read_bytes()).hexdigest()
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    workers = [
        context.Process(target=_replace_worker, args=(str(target), expected, queue))
        for _ in range(2)
    ]

    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0
    results = [queue.get(timeout=2) for _ in workers]

    assert sum(result["success"] is True for result in results) == 1
    assert sum(result.get("error", {}).get("code") == "revision_conflict" for result in results) == 1
    assert target.read_text(encoding="utf-8") == "after\n"


def test_replace_rolls_back_current_file_when_atomic_write_raises_after_replace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "rollback.txt"
    target.write_text("before\n", encoding="utf-8")
    original_atomic_write = replacing_module._atomic_write
    calls = 0

    def flaky_atomic_write(path: Path, raw: bytes, *, mode: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            original_atomic_write(path, raw, mode=mode)
            raise OSError("simulated directory fsync failure")
        original_atomic_write(path, raw, mode=mode)

    monkeypatch.setattr(replacing_module, "_atomic_write", flaky_atomic_write)
    result = replace_files(
        [{"path": target, "rules": [{"pattern": "before", "replacement": "after"}]}]
    )

    assert result["success"] is False
    assert result["rolled_back"] is True
    assert target.read_text(encoding="utf-8") == "before\n"


def test_unified_read_and_replace_are_registered() -> None:
    from chatgpt_web_oauth_mcp import server

    async def descriptors() -> dict[str, object]:
        list_tools = getattr(server.mcp, "_list_tools")
        try:
            tools = await list_tools()
        except TypeError:
            tools = await list_tools(None)
        return {tool.name: tool for tool in tools}

    tools = asyncio.run(descriptors())

    assert tools["read"].annotations.readOnlyHint is True
    assert tools["replace"].annotations.destructiveHint is True
    assert "operations" in tools["replace"].parameters["properties"]
