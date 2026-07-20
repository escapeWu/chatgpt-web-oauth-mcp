from __future__ import annotations

import ast
import os
from pathlib import Path
import re

from .files import DEFAULT_EXCLUDE_DIR_NAMES
from .search import grep_files
from .response_budget import (
    DEFAULT_TOOL_OUTPUT_TOKEN_BUDGET,
    ResponseBudget,
    with_budget_metadata,
)


SUPPORTED_LANGUAGES = {"python", "typescript", "javascript"}
LANGUAGE_EXTENSIONS = {
    "python": (".py",),
    "typescript": (".ts", ".tsx"),
    "javascript": (".js", ".jsx", ".mjs", ".cjs"),
}

JS_IDENTIFIER = r"[A-Za-z_$][A-Za-z0-9_$]*"
JS_SYMBOL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "class",
        re.compile(rf"\b(?:export\s+default\s+|export\s+)?class\s+({JS_IDENTIFIER})"),
    ),
    (
        "function",
        re.compile(rf"\b(?:export\s+default\s+|export\s+)?(?:async\s+)?function\s+({JS_IDENTIFIER})"),
    ),
    (
        "constant",
        re.compile(rf"\b(?:export\s+)?const\s+({JS_IDENTIFIER})\s*="),
    ),
)
JS_IMPORT_FROM_RE = re.compile(r"\bimport\s+.+?\s+from\s+['\"]([^'\"]+)['\"]")
JS_IMPORT_SIDE_EFFECT_RE = re.compile(r"\bimport\s+['\"]([^'\"]+)['\"]")
JS_REQUIRE_RE = re.compile(r"\brequire\(\s*['\"]([^'\"]+)['\"]\s*\)")


def _error(code: str, message: str, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "success": False,
        "error": {
            "code": code,
            "message": message,
        },
    }
    payload.update(extra)
    return payload


def _validate_language(language: str) -> dict[str, object] | None:
    if language not in SUPPORTED_LANGUAGES:
        return _error(
            "unsupported_language",
            "language must be one of: python, typescript, javascript.",
            language=language,
        )
    return None


def _validate_limit(limit: int) -> dict[str, object] | None:
    if limit < 1:
        return _error("invalid_arguments", "limit must be greater than or equal to 1.", limit=limit)
    return None


def _validate_path(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return _error("path_not_found", f"Path not found: {path}", path=str(path))
    if not path.is_file() and not path.is_dir():
        return _error("unsupported_path", f"Path is not a regular file or directory: {path}", path=str(path))
    return None


def _is_source_file(path: Path, *, language: str) -> bool:
    return path.suffix in LANGUAGE_EXTENSIONS[language]


def _iter_source_files(path: Path, *, language: str) -> list[Path]:
    if path.is_file():
        return [path] if _is_source_file(path, language=language) else []

    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames[:] = [
            dirname
            for dirname in sorted(dirnames)
            if not dirname.startswith(".") and dirname not in DEFAULT_EXCLUDE_DIR_NAMES
        ]
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            source = Path(dirpath) / filename
            if _is_source_file(source, language=language):
                files.append(source)
    return files


def _read_text(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:1024]:
        return None
    return raw.decode("utf-8", errors="replace")


class _PythonSymbolVisitor(ast.NodeVisitor):
    def __init__(self, *, file: Path) -> None:
        self.file = file
        self.symbols: list[dict[str, object]] = []
        self._class_depth = 0
        self._function_depth = 0

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.symbols.append(
            {
                "name": node.name,
                "kind": "class",
                "file": str(self.file),
                "line": node.lineno,
            }
        )
        self._class_depth += 1
        self.generic_visit(node)
        self._class_depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node, async_function=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node, async_function=True)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, *, async_function: bool) -> None:
        is_method = self._class_depth > 0 and self._function_depth == 0
        if is_method:
            kind = "method"
        else:
            kind = "async_function" if async_function else "function"
        self.symbols.append(
            {
                "name": node.name,
                "kind": kind,
                "file": str(self.file),
                "line": node.lineno,
            }
        )
        self._function_depth += 1
        self.generic_visit(node)
        self._function_depth -= 1


def _python_symbols(path: Path, text: str) -> tuple[list[dict[str, object]], dict[str, object] | None]:
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        return [], {
            "file": str(path),
            "line": exc.lineno,
            "message": exc.msg,
        }
    visitor = _PythonSymbolVisitor(file=path)
    visitor.visit(tree)
    return visitor.symbols, None


def _js_symbols(path: Path, text: str) -> list[dict[str, object]]:
    symbols: list[dict[str, object]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in JS_SYMBOL_PATTERNS:
            for match in pattern.finditer(line):
                symbols.append(
                    {
                        "name": match.group(1),
                        "kind": kind,
                        "file": str(path),
                        "line": line_number,
                    }
                )
    return symbols


def code_map_symbols(
    *,
    path: Path,
    language: str = "python",
    limit: int = 500,
    offset: int = 0,
    max_tokens: int = DEFAULT_TOOL_OUTPUT_TOKEN_BUDGET,
) -> dict[str, object]:
    language_error = _validate_language(language)
    if language_error:
        return language_error
    limit_error = _validate_limit(limit)
    if limit_error:
        return limit_error
    path_error = _validate_path(path)
    if path_error:
        return path_error

    symbols: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []
    truncated = False
    scanned_files = 0
    seen = 0
    start = max(offset, 0)
    budget = ResponseBudget(max_tokens=max_tokens)
    for source in _iter_source_files(path, language=language):
        scanned_files += 1
        text = _read_text(source)
        if text is None:
            continue
        parse_error = None
        if language == "python":
            file_symbols, parse_error = _python_symbols(source, text)
            if parse_error:
                errors.append(parse_error)
        else:
            file_symbols = _js_symbols(source, text)

        if errors:
            error_candidate = {
                "success": True,
                "path": str(path),
                "language": language,
                "symbols": symbols,
                "scanned_files": scanned_files,
                "errors": errors,
                "next_offset": start + len(symbols),
            }
            _rendered, error_measurement = with_budget_metadata(
                error_candidate,
                budget=budget,
                truncated=True,
                stop_reason="token_budget",
            )
            if not error_measurement.fits:
                if parse_error:
                    errors.pop()
                truncated = True
                break

        for symbol in file_symbols:
            if seen < start:
                seen += 1
                continue
            if len(symbols) >= limit:
                truncated = True
                break
            candidate = {
                "success": True,
                "path": str(path),
                "language": language,
                "symbols": [*symbols, symbol],
                "scanned_files": scanned_files,
                "errors": errors,
                "next_offset": start + len(symbols) + 1,
            }
            _rendered, measurement = with_budget_metadata(
                candidate,
                budget=budget,
                truncated=True,
                stop_reason="token_budget",
            )
            if not measurement.fits:
                truncated = True
                break
            symbols.append(symbol)
            seen += 1
        if truncated:
            break

    payload = {
        "success": True,
        "path": str(path),
        "language": language,
        "symbols": symbols,
        "truncated": truncated,
        "scanned_files": scanned_files,
        "errors": errors,
        "next_offset": start + len(symbols) if truncated else None,
    }
    rendered, _measurement = with_budget_metadata(
        payload,
        budget=budget,
        truncated=truncated,
        stop_reason="token_budget" if truncated and len(symbols) < limit else ("limit" if truncated else "end_of_results"),
    )
    return rendered


def _reference_pattern(symbol: str) -> str:
    escaped = re.escape(symbol)
    return rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])"


def code_map_references(
    *,
    path: Path,
    symbol: str,
    glob_pattern: str | None = "*.py",
    limit: int = 200,
    offset: int = 0,
    max_tokens: int = DEFAULT_TOOL_OUTPUT_TOKEN_BUDGET,
) -> dict[str, object]:
    normalized_symbol = symbol.strip()
    if not normalized_symbol:
        return _error("invalid_arguments", "symbol must be a non-empty string.")
    limit_error = _validate_limit(limit)
    if limit_error:
        return limit_error

    result = grep_files(
        path,
        pattern=_reference_pattern(normalized_symbol),
        glob_pattern=glob_pattern,
        output_mode="content",
        head_limit=limit,
        offset=offset,
        include_hidden=False,
        respect_gitignore=True,
        max_tokens=max_tokens,
    )
    if not result.get("success"):
        return result

    references = [
        {
            "file": item["path"],
            "line": item["line_number"],
            "text": item["line"],
        }
        for item in result.get("matches", [])
        if isinstance(item, dict)
    ]
    payload = {
        "success": True,
        "path": str(path),
        "symbol": normalized_symbol,
        "glob": glob_pattern,
        "references": references,
        "truncated": bool(result.get("truncated")),
        "next_offset": result.get("next_offset"),
    }
    rendered, _measurement = with_budget_metadata(
        payload,
        budget=ResponseBudget(max_tokens=max_tokens),
        truncated=bool(result.get("truncated")),
        stop_reason=str(result.get("stop_reason", "end_of_results")),
    )
    return rendered


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _python_imports(text: str, *, path: Path) -> tuple[list[str], dict[str, object] | None]:
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        return [], {
            "file": str(path),
            "line": exc.lineno,
            "message": exc.msg,
        }

    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _append_unique(imports, alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            for alias in node.names:
                if alias.name == "*":
                    value = f"{module}.*" if module else "*"
                elif module:
                    separator = "" if module.endswith(".") else "."
                    value = f"{module}{separator}{alias.name}"
                else:
                    value = "." * node.level + alias.name
                _append_unique(imports, value)
    return imports, None


def _js_imports(text: str) -> list[str]:
    imports: list[str] = []
    for line in text.splitlines():
        for pattern in (JS_IMPORT_FROM_RE, JS_IMPORT_SIDE_EFFECT_RE, JS_REQUIRE_RE):
            for match in pattern.finditer(line):
                _append_unique(imports, match.group(1))
    return imports


def code_map_imports(
    *,
    path: Path,
    language: str = "python",
    limit: int = 500,
    offset: int = 0,
    max_tokens: int = DEFAULT_TOOL_OUTPUT_TOKEN_BUDGET,
) -> dict[str, object]:
    language_error = _validate_language(language)
    if language_error:
        return language_error
    limit_error = _validate_limit(limit)
    if limit_error:
        return limit_error
    path_error = _validate_path(path)
    if path_error:
        return path_error

    imports_by_file: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []
    truncated = False
    scanned_files = 0
    seen = 0
    start = max(offset, 0)
    budget = ResponseBudget(max_tokens=max_tokens)
    for source in _iter_source_files(path, language=language):
        scanned_files += 1
        text = _read_text(source)
        if text is None:
            continue
        parse_error = None
        if language == "python":
            imports, parse_error = _python_imports(text, path=source)
            if parse_error:
                errors.append(parse_error)
        else:
            imports = _js_imports(text)

        if errors:
            error_candidate = {
                "success": True,
                "path": str(path),
                "language": language,
                "imports": imports_by_file,
                "scanned_files": scanned_files,
                "errors": errors,
                "next_offset": start + len(imports_by_file),
            }
            _rendered, error_measurement = with_budget_metadata(
                error_candidate,
                budget=budget,
                truncated=True,
                stop_reason="token_budget",
            )
            if not error_measurement.fits:
                if parse_error:
                    errors.pop()
                truncated = True
                break

        if not imports:
            continue
        if seen < start:
            seen += 1
            continue
        if len(imports_by_file) >= limit:
            truncated = True
            break
        item = {"file": str(source), "imports": imports}
        candidate = {
            "success": True,
            "path": str(path),
            "language": language,
            "imports": [*imports_by_file, item],
            "scanned_files": scanned_files,
            "errors": errors,
            "next_offset": start + len(imports_by_file) + 1,
        }
        _rendered, measurement = with_budget_metadata(
            candidate,
            budget=budget,
            truncated=True,
            stop_reason="token_budget",
        )
        if not measurement.fits:
            truncated = True
            break
        imports_by_file.append(item)
        seen += 1

    payload = {
        "success": True,
        "path": str(path),
        "language": language,
        "imports": imports_by_file,
        "truncated": truncated,
        "scanned_files": scanned_files,
        "errors": errors,
        "next_offset": start + len(imports_by_file) if truncated else None,
    }
    rendered, _measurement = with_budget_metadata(
        payload,
        budget=budget,
        truncated=truncated,
        stop_reason="token_budget" if truncated and len(imports_by_file) < limit else ("limit" if truncated else "end_of_results"),
    )
    return rendered
