from __future__ import annotations

import asyncio
from pathlib import Path

from chatgpt_web_oauth_mcp.code_map import (
    code_map_imports,
    code_map_references,
    code_map_symbols,
)
from chatgpt_web_oauth_mcp.response_budget import ResponseBudget, render_json_payload


def _call(tool, *args, **kwargs):
    fn = tool.fn if hasattr(tool, "fn") else tool
    result = fn(*args, **kwargs)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def test_python_symbols_include_functions_classes_methods_and_async(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text(
        "\n".join(
            [
                "class Alpha:",
                "    def method(self):",
                "        def nested():",
                "            return 1",
                "        return nested()",
                "",
                "async def fetch():",
                "    return 2",
                "",
                "def build():",
                "    return Alpha()",
            ]
        ),
        encoding="utf-8",
    )

    result = code_map_symbols(path=tmp_path, language="python", limit=20)

    assert result["success"] is True
    by_name = {item["name"]: item for item in result["symbols"]}
    assert by_name["Alpha"]["kind"] == "class"
    assert by_name["method"]["kind"] == "method"
    assert by_name["nested"]["kind"] == "function"
    assert by_name["fetch"]["kind"] == "async_function"
    assert by_name["build"]["kind"] == "function"
    assert by_name["Alpha"]["line"] == 1


def test_python_import_extraction(tmp_path: Path) -> None:
    source = tmp_path / "imports.py"
    source.write_text(
        "\n".join(
            [
                "import os",
                "import sys as system",
                "from pathlib import Path",
                "from .local import thing as renamed",
                "from . import config",
            ]
        ),
        encoding="utf-8",
    )

    result = code_map_imports(path=tmp_path, language="python", limit=10)

    assert result["success"] is True
    assert result["imports"] == [
        {
            "file": str(source),
            "imports": ["os", "sys", "pathlib.Path", ".local.thing", ".config"],
        }
    ]


def test_typescript_symbols_and_imports(tmp_path: Path) -> None:
    source = tmp_path / "widget.ts"
    source.write_text(
        "\n".join(
            [
                "import React from 'react';",
                "import './side-effect';",
                "const fs = require('fs');",
                "export class Widget {}",
                "export function makeWidget() {}",
                "function helper() {}",
                "export const runWidget = () => {};",
            ]
        ),
        encoding="utf-8",
    )

    symbols = code_map_symbols(path=tmp_path, language="typescript", limit=20)
    imports = code_map_imports(path=tmp_path, language="typescript", limit=20)

    assert symbols["success"] is True
    names = {item["name"]: item["kind"] for item in symbols["symbols"]}
    assert names["Widget"] == "class"
    assert names["makeWidget"] == "function"
    assert names["helper"] == "function"
    assert names["runWidget"] == "constant"

    assert imports["success"] is True
    assert imports["imports"] == [
        {
            "file": str(source),
            "imports": ["react", "./side-effect", "fs"],
        }
    ]


def test_javascript_symbols_and_imports(tmp_path: Path) -> None:
    source = tmp_path / "app.js"
    source.write_text(
        "\n".join(
            [
                "import api from './api.js';",
                "const path = require('path');",
                "class App {}",
                "const start = function () {};",
            ]
        ),
        encoding="utf-8",
    )

    symbols = code_map_symbols(path=tmp_path, language="javascript", limit=20)
    imports = code_map_imports(path=tmp_path, language="javascript", limit=20)

    assert symbols["success"] is True
    assert {item["name"] for item in symbols["symbols"]} == {"App", "path", "start"}
    assert imports["imports"][0]["imports"] == ["./api.js", "path"]


def test_references_use_word_boundary(tmp_path: Path) -> None:
    source = tmp_path / "labels.py"
    source.write_text(
        "\n".join(
            [
                "def label_events():",
                "    return []",
                "labels = label_events()",
                "label_events_extra = []",
                "my_label_events = []",
            ]
        ),
        encoding="utf-8",
    )

    result = code_map_references(path=tmp_path, symbol="label_events", glob_pattern="*.py", limit=10)

    assert result["success"] is True
    assert [item["line"] for item in result["references"]] == [1, 3]
    assert all("label_events_extra" not in item["text"] for item in result["references"])
    assert all("my_label_events" not in item["text"] for item in result["references"])


def test_limit_sets_truncated_true(tmp_path: Path) -> None:
    source = tmp_path / "many.py"
    source.write_text("def one(): pass\ndef two(): pass\ndef three(): pass\n", encoding="utf-8")

    result = code_map_symbols(path=source, language="python", limit=2)

    assert result["success"] is True
    assert [item["name"] for item in result["symbols"]] == ["one", "two"]
    assert result["truncated"] is True


def test_code_map_symbols_token_budget_has_lossless_offset(tmp_path: Path) -> None:
    target = tmp_path / "many.py"
    target.write_text(
        "\n".join(f"def function_{index}_with_a_long_name(): pass" for index in range(100)),
        encoding="utf-8",
    )

    result = code_map_symbols(path=tmp_path, limit=100, max_tokens=400)

    assert result["partial"] is True
    assert result["next_offset"] == len(result["symbols"])
    assert ResponseBudget(max_tokens=400).count_tokens(render_json_payload(result)) <= 400


def test_unsupported_language_returns_structured_error(tmp_path: Path) -> None:
    result = code_map_symbols(path=tmp_path, language="ruby", limit=10)

    assert result["success"] is False
    assert result["error"]["code"] == "unsupported_language"


def test_code_map_tools_are_registered_with_schema_and_annotations() -> None:
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
    for name in ["code_map_symbols", "code_map_references", "code_map_imports"]:
        assert name in descriptors
        assert descriptors[name]["annotations"]["readOnlyHint"] is True

    assert descriptors["code_map_symbols"]["parameters"]["properties"]["language"]["enum"] == [
        "python",
        "typescript",
        "javascript",
    ]
    assert "symbol" in descriptors["code_map_references"]["parameters"]["properties"]
    assert "glob" in descriptors["code_map_references"]["parameters"]["properties"]


def test_server_info_includes_code_map_tools() -> None:
    from chatgpt_web_oauth_mcp import server

    payload = _call(server.server_info)

    for name in ["code_map_symbols", "code_map_references", "code_map_imports"]:
        assert name in payload["tools"]
