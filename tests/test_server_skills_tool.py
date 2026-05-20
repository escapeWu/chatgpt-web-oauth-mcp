from __future__ import annotations

import asyncio
from pathlib import Path


def _call(tool, *args, **kwargs):
    fn = tool.fn if hasattr(tool, "fn") else tool
    result = fn(*args, **kwargs)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def test_server_list_skills_tool_returns_structured_summary(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    workspace_root = tmp_path / "workspace"
    project_skill = workspace_root / ".agents" / "skills" / "project-helper" / "SKILL.md"
    project_skill.parent.mkdir(parents=True, exist_ok=True)
    project_skill.write_text(
        "\n".join(
            [
                "---",
                "name: project-helper",
                "description: Project scoped helper",
                "---",
            ]
        ),
        encoding="utf-8",
    )

    server.WORKSPACE_ROOT = workspace_root
    result = _call(server.list_skills, include_global=False)

    assert result["success"] is True
    assert result["skills"] == [
        {
            "name": "project-helper",
            "description": "Project scoped helper",
            "preferred_path": str(project_skill),
            "sources": [
                {
                    "scope": "project",
                    "namespace": "agents",
                    "path": str(project_skill),
                }
            ],
        }
    ]


def test_server_list_skills_tool_forwards_new_filter_arguments(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    captured: dict[str, object] = {}

    def fake_list_skills_impl(**kwargs):
        captured.update(kwargs)
        return {"success": True, "skills": [], "scanned_roots": [], "filters": kwargs}

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True, exist_ok=True)
    server.WORKSPACE_ROOT = workspace_root
    server.list_skills_impl = fake_list_skills_impl

    result = _call(
        server.list_skills,
        include_project=False,
        include_global=True,
        namespace="claude",
        name_pattern="claude-*",
        description_max_length=42,
    )

    assert result["success"] is True
    assert captured == {
        "workspace_root": workspace_root,
        "include_project": False,
        "include_global": True,
        "namespace": "claude",
        "name_pattern": "claude-*",
        "description_max_length": 42,
    }
