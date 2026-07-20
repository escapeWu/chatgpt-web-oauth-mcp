"""Unified text, image, PDF, and binary reader."""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
import re
from typing import Any

from .content_io import TextDecodingError, decode_text_bytes
from .response_budget import (
    DEFAULT_TOOL_OUTPUT_TOKEN_BUDGET,
    ResponseBudget,
    with_budget_metadata,
)


READ_MODES = {"auto", "text", "image", "pdf", "hex"}


def _error(code: str, message: str, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "success": False,
        "error": {"code": code, "message": message},
    }
    payload.update(extra)
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _base_metadata(path: Path) -> dict[str, object]:
    mime_type, _compression = mimetypes.guess_type(str(path))
    return {
        "path": str(path),
        "size": path.stat().st_size,
        "mime_type": mime_type or "application/octet-stream",
        "suffix": path.suffix.lower(),
        "sha256": _sha256(path),
    }


def _auto_mode(path: Path, mime_type: str) -> str:
    if mime_type == "application/pdf" or path.suffix.lower() == ".pdf":
        return "pdf"
    if mime_type.startswith("image/"):
        return "image"
    with path.open("rb") as handle:
        prefix = handle.read(4096)
    if b"\x00" in prefix and not any(prefix.startswith(marker) for marker in (
        b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff", b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"
    )):
        return "hex"
    return "text"


def _parse_pages(spec: str | None, page_count: int) -> list[int]:
    if not spec:
        return list(range(min(page_count, 4)))
    selected: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not match:
            raise ValueError("pages must use 1-based values such as '1-5,8'.")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start or end > page_count:
            raise ValueError(f"pages entry {part!r} is outside 1-{page_count}.")
        selected.extend(range(start - 1, end))
    deduplicated = list(dict.fromkeys(selected))
    if len(deduplicated) > 20:
        raise ValueError("At most 20 PDF pages may be read in one call.")
    return deduplicated


def _read_text(
    path: Path,
    *,
    metadata: dict[str, object],
    encoding: str | None,
    start_line: int,
    line_limit: int,
    include_line_numbers: bool,
    budget: ResponseBudget,
) -> dict[str, object]:
    try:
        decoded = decode_text_bytes(path.read_bytes(), encoding=encoding)
    except TextDecodingError as exc:
        return _error(
            "encoding_error",
            str(exc),
            resolved_path=str(path),
            encoding_candidates=exc.candidates,
        )

    lines = decoded.text.splitlines()
    start = max(start_line, 1)
    requested = lines[start - 1 : start - 1 + max(line_limit, 1)]
    base: dict[str, Any] = {
        "success": True,
        "mode": "text",
        "file": {
            **metadata,
            "encoding": decoded.encoding,
            "bom": decoded.bom,
            "newline": decoded.newline,
            "file_type": "text",
        },
        "content": "",
        "offset_unit": "lines",
        "start_line": start,
        "end_line": start - 1,
        "next_offset": None,
    }
    rendered_lines: list[str] = []
    stop_reason = "end_of_file"
    for index, line in enumerate(requested):
        rendered_line = f"{start + index}: {line}" if include_line_numbers else line
        candidate = dict(base)
        candidate["content"] = "\n".join([*rendered_lines, rendered_line])
        candidate["end_line"] = start + index
        candidate["next_offset"] = start + index + 1
        _rendered, measurement = with_budget_metadata(
            candidate,
            budget=budget,
            truncated=True,
            stop_reason="token_budget",
        )
        if not measurement.fits:
            stop_reason = measurement.stop_reason or "token_budget"
            break
        rendered_lines.append(rendered_line)

    returned = len(rendered_lines)
    has_more = start - 1 + returned < len(lines)
    if has_more and returned == len(requested):
        stop_reason = "line_limit"
    base["content"] = "\n".join(rendered_lines)
    base["end_line"] = start + returned - 1
    base["next_offset"] = start + returned if has_more else None
    result, _measurement = with_budget_metadata(
        base,
        budget=budget,
        truncated=has_more,
        stop_reason=stop_reason if has_more else "end_of_file",
    )
    return result


def _read_hex(
    path: Path,
    *,
    metadata: dict[str, object],
    byte_offset: int,
    byte_limit: int,
    budget: ResponseBudget,
) -> dict[str, object]:
    start = max(byte_offset, 0)
    limit = min(max(byte_limit, 1), 256 * 1024)
    with path.open("rb") as handle:
        handle.seek(start)
        raw = handle.read(limit)
    base: dict[str, Any] = {
        "success": True,
        "mode": "hex",
        "file": {**metadata, "file_type": "binary"},
        "content": "",
        "offset_unit": "bytes",
        "start_offset": start,
        "end_offset": start,
        "next_offset": None,
    }
    low, high = 0, len(raw)
    while low < high:
        midpoint = (low + high + 1) // 2
        candidate = dict(base)
        candidate["content"] = raw[:midpoint].hex(" ")
        candidate["end_offset"] = start + midpoint
        candidate["next_offset"] = start + midpoint
        _rendered, measurement = with_budget_metadata(
            candidate,
            budget=budget,
            truncated=True,
            stop_reason="token_budget",
        )
        if measurement.fits:
            low = midpoint
        else:
            high = midpoint - 1
    returned = low
    has_more = start + returned < int(metadata["size"])
    base["content"] = raw[:returned].hex(" ")
    base["end_offset"] = start + returned
    base["next_offset"] = start + returned if has_more else None
    reason = "token_budget" if returned < len(raw) else ("byte_limit" if has_more else "end_of_file")
    result, _measurement = with_budget_metadata(
        base,
        budget=budget,
        truncated=has_more,
        stop_reason=reason,
    )
    return result


def _read_image(
    path: Path,
    *,
    metadata: dict[str, object],
    budget: ResponseBudget,
) -> dict[str, object]:
    try:
        from PIL import Image
    except ImportError:
        return _error("dependency_unavailable", "Image reads require Pillow.")
    try:
        with Image.open(path) as image:
            image_metadata = {
                "format": image.format,
                "width": image.width,
                "height": image.height,
                "mode": image.mode,
                "frames": int(getattr(image, "n_frames", 1)),
            }
    except Exception as exc:
        return _error("invalid_image", f"Could not inspect image: {exc}", resolved_path=str(path))
    payload = {
        "success": True,
        "mode": "image",
        "file": {**metadata, "file_type": "image"},
        "image": {
            **image_metadata,
            "content_reference": {
                "path": str(path),
                "uri": path.as_uri(),
                "mime_type": metadata["mime_type"],
            },
        },
        "next_offset": None,
    }
    result, _measurement = with_budget_metadata(
        payload,
        budget=budget,
        truncated=False,
        stop_reason="complete",
    )
    return result


def _read_pdf(
    path: Path,
    *,
    metadata: dict[str, object],
    pages: str | None,
    budget: ResponseBudget,
) -> dict[str, object]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return _error("dependency_unavailable", "PDF reads require pypdf.")
    try:
        reader = PdfReader(str(path))
        selected = _parse_pages(pages, len(reader.pages))
    except Exception as exc:
        return _error("invalid_pdf", str(exc), resolved_path=str(path))

    base: dict[str, Any] = {
        "success": True,
        "mode": "pdf",
        "file": {**metadata, "file_type": "pdf"},
        "page_count": len(reader.pages),
        "pages": [],
        "offset_unit": "pages",
        "next_offset": None,
    }
    returned_pages: list[dict[str, object]] = []
    stopped = False
    for page_index in selected:
        try:
            page_text = reader.pages[page_index].extract_text() or ""
        except Exception as exc:
            return _error(
                "pdf_read_error",
                f"Could not extract PDF page {page_index + 1}: {exc}",
                resolved_path=str(path),
                page=page_index + 1,
            )
        page_item = {"page": page_index + 1, "text": page_text}
        candidate = dict(base)
        candidate["pages"] = [*returned_pages, page_item]
        candidate["next_offset"] = page_index + 1
        _rendered, measurement = with_budget_metadata(
            candidate,
            budget=budget,
            truncated=True,
            stop_reason="token_budget",
        )
        if not measurement.fits:
            stopped = True
            break
        returned_pages.append(page_item)
    base["pages"] = returned_pages
    base["next_offset"] = (
        selected[len(returned_pages)] + 1 if stopped else None
    )
    result, _measurement = with_budget_metadata(
        base,
        budget=budget,
        truncated=stopped,
        stop_reason="token_budget" if stopped else "complete",
    )
    return result


def read_path(
    path: Path,
    *,
    mode: str = "auto",
    encoding: str | None = None,
    start_line: int = 1,
    line_limit: int = 200,
    include_line_numbers: bool = False,
    pages: str | None = None,
    byte_offset: int = 0,
    byte_limit: int = 4096,
    max_tokens: int = DEFAULT_TOOL_OUTPUT_TOKEN_BUDGET,
) -> dict[str, object]:
    if not path.exists():
        return _error("file_not_found", f"File not found: {path}", resolved_path=str(path))
    if not path.is_file():
        return _error("not_a_file", f"Path is not a file: {path}", resolved_path=str(path))
    if mode not in READ_MODES:
        return _error("invalid_mode", f"mode must be one of: {', '.join(sorted(READ_MODES))}.")

    try:
        metadata = _base_metadata(path)
        effective_mode = _auto_mode(path, str(metadata["mime_type"])) if mode == "auto" else mode
        budget = ResponseBudget(max_tokens=max_tokens)
        if effective_mode == "text":
            return _read_text(
                path,
                metadata=metadata,
                encoding=encoding,
                start_line=start_line,
                line_limit=line_limit,
                include_line_numbers=include_line_numbers,
                budget=budget,
            )
        if effective_mode == "hex":
            return _read_hex(
                path,
                metadata=metadata,
                byte_offset=byte_offset,
                byte_limit=byte_limit,
                budget=budget,
            )
        if effective_mode == "image":
            return _read_image(path, metadata=metadata, budget=budget)
        return _read_pdf(path, metadata=metadata, pages=pages, budget=budget)
    except OSError as exc:
        return _error(
            "read_failed",
            f"File changed or became unreadable during the read: {exc}",
            resolved_path=str(path),
        )
