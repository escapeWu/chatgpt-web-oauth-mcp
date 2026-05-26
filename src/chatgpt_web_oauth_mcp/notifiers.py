from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Mapping, Protocol


TELEGRAM_MESSAGE_MAX_CHARS = 4096
_TRUNCATION_NOTICE_RESERVE = 96
_LINE_MAX_CHARS = 240


class TaskBoardNotifier(Protocol):
    def notify_task_terminal(
        self,
        *,
        board: Mapping[str, object],
        task: Mapping[str, object],
    ) -> None: ...


def _one_line(value: object | None, *, default: str = "") -> str:
    text = default if value is None else str(value)
    return " ".join(text.split())


def _task_marker(status: object | None) -> str:
    value = str(status or "pending")
    if value == "succeeded":
        return "[x]"
    if value == "failed":
        return "[!]"
    if value == "cancelled":
        return "[-]"
    return "[ ]"


def _clip_line(text: str, max_chars: int = _LINE_MAX_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[: max(max_chars - 1, 0)]}…"


def _truncation_notice(omitted_count: int) -> str:
    return f"… truncated {omitted_count} task(s) to fit Telegram's 4096 character limit"


def format_taskboard_terminal_message(
    *,
    board: Mapping[str, object],
    task: Mapping[str, object],
) -> str:
    board_id = _one_line(board.get("board_id"), default="unknown")
    board_title = _one_line(board.get("title"), default="Untitled board")
    task_id = _one_line(task.get("task_id"), default="unknown")
    task_title = _one_line(task.get("title"), default="Untitled task")
    task_status = _one_line(task.get("status"), default="unknown")

    lines = [
        f"TaskBoard task {task_status}",
        _clip_line(f"Current task: {_task_marker(task_status)} {task_title} ({task_id}) - {task_status}"),
        _clip_line(f"Board: {board_title} ({board_id})"),
        "",
        "TaskBoard checklist:",
    ]
    items = [item for item in list(board.get("tasks") or []) if isinstance(item, Mapping)]
    for index, item in enumerate(items):
        item_status = _one_line(item.get("status"), default="pending")
        item_title = _one_line(item.get("title"), default="Untitled task")
        item_id = _one_line(item.get("task_id"), default="unknown")
        line = _clip_line(f"{_task_marker(item_status)} {item_title} ({item_id}) - {item_status}")
        candidate = lines + [line]
        if len("\n".join(candidate)) > TELEGRAM_MESSAGE_MAX_CHARS - _TRUNCATION_NOTICE_RESERVE:
            lines.append(_truncation_notice(len(items) - index))
            break
        lines.append(line)
    return "\n".join(lines)[:TELEGRAM_MESSAGE_MAX_CHARS]


@dataclass(frozen=True)
class TelegramTaskBoardNotifier:
    bot_token: str
    receiver_id: str
    timeout_seconds: float = 5.0

    def notify_task_terminal(
        self,
        *,
        board: Mapping[str, object],
        task: Mapping[str, object],
    ) -> None:
        text = format_taskboard_terminal_message(board=board, task=task)
        self._send_message(text)

    def _send_message(self, text: str) -> None:
        body = json.dumps(
            {
                "chat_id": self.receiver_id,
                "text": text,
                "disable_web_page_preview": True,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        response = urllib.request.urlopen(request, timeout=self.timeout_seconds)
        try:
            response.read()
        finally:
            response.close()


def build_telegram_notifier(
    *,
    bot_token: str | None,
    receiver_id: str | None,
    timeout_seconds: float = 5.0,
) -> TaskBoardNotifier | None:
    token = (bot_token or "").strip()
    chat_id = (receiver_id or "").strip()
    if not token or not chat_id:
        return None
    return TelegramTaskBoardNotifier(
        bot_token=token,
        receiver_id=chat_id,
        timeout_seconds=max(float(timeout_seconds), 0.1),
    )
