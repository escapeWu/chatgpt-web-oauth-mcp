from __future__ import annotations

from dataclasses import dataclass
import json
import re
import ssl
from typing import Any
from urllib import error as urlerror
from urllib import parse, request


class ObsidianConfigError(RuntimeError):
    pass


class ObsidianRequestError(RuntimeError):
    pass


@dataclass(frozen=True)
class ObsidianConfig:
    api_key: str
    host: str = "127.0.0.1"
    port: int = 27124
    protocol: str = "https"
    verify_ssl: bool = False
    timeout_seconds: int = 10

    @property
    def base_url(self) -> str:
        protocol = "http" if self.protocol.lower() == "http" else "https"
        return f"{protocol}://{self.host}:{self.port}"


def configured(config: ObsidianConfig) -> bool:
    return bool(config.api_key.strip())


class ObsidianClient:
    def __init__(self, config: ObsidianConfig) -> None:
        self.config = config
        if not configured(config):
            raise ObsidianConfigError(
                "OBSIDIAN_API_KEY is not configured. Enable the Obsidian Local REST API plugin "
                "and set OBSIDIAN_API_KEY in .env."
            )
        self._ssl_context = None
        if self.config.protocol.lower() != "http" and not self.config.verify_ssl:
            self._ssl_context = ssl._create_unverified_context()

    def _vault_url(self, filepath: str = "", *, directory: bool = False) -> str:
        normalized = (filepath or "").strip().lstrip("/")
        quoted = quote_vault_path(normalized)
        suffix = "/" if directory and quoted and not quoted.endswith("/") else ""
        return f"{self.config.base_url}/vault/{quoted}{suffix}"

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        if extra:
            headers.update(extra)
        return headers

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        req = request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with request.urlopen(
                req,
                timeout=self.config.timeout_seconds,
                context=self._ssl_context,
            ) as response:
                return response.status, dict(response.headers.items()), response.read()
        except urlerror.HTTPError as exc:
            payload = exc.read()
            message = parse_error_payload(payload) or exc.reason or "HTTP error"
            raise ObsidianRequestError(f"HTTP {exc.code}: {message}") from exc
        except urlerror.URLError as exc:
            raise ObsidianRequestError(f"Request failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ObsidianRequestError("Request timed out") from exc

    def _json_request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> Any:
        _, _, payload = self._request(method, url, headers=headers, body=body)
        if not payload:
            return None
        return json.loads(payload.decode("utf-8"))

    def status(self) -> dict[str, object]:
        status, headers, payload = self._request("GET", self.config.base_url, headers=self._headers())
        text = payload.decode("utf-8", errors="replace")
        parsed: object
        try:
            parsed = json.loads(text) if text else None
        except json.JSONDecodeError:
            parsed = text[:500]
        return {"status_code": status, "content_type": headers.get("Content-Type"), "body": parsed}

    def list_files_in_vault(self) -> list[object]:
        payload = self._json_request("GET", self._vault_url(directory=True), headers=self._headers())
        return list(payload.get("files", [])) if isinstance(payload, dict) else []

    def list_files_in_dir(self, dirpath: str) -> list[object]:
        payload = self._json_request("GET", self._vault_url(dirpath, directory=True), headers=self._headers())
        return list(payload.get("files", [])) if isinstance(payload, dict) else []

    def get_file_contents(self, filepath: str) -> str:
        _, _, payload = self._request("GET", self._vault_url(filepath), headers=self._headers())
        return payload.decode("utf-8")

    def batch_get_file_contents(self, filepaths: list[str]) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for filepath in filepaths:
            try:
                results.append({"filepath": filepath, "success": True, "content": self.get_file_contents(filepath)})
            except Exception as exc:  # keep batch reads best-effort
                results.append({"filepath": filepath, "success": False, "error": str(exc)})
        return results

    def simple_search(self, query: str, context_length: int = 100) -> Any:
        params = parse.urlencode({"query": query, "contextLength": int(context_length)})
        return self._json_request(
            "POST",
            f"{self.config.base_url}/search/simple/?{params}",
            headers=self._headers(),
        )

    def complex_search(self, query: dict[str, object]) -> Any:
        return self._json_request(
            "POST",
            f"{self.config.base_url}/search/",
            headers=self._headers({"Content-Type": "application/vnd.olrapi.jsonlogic+json"}),
            body=json.dumps(query).encode("utf-8"),
        )

    def append_content(self, filepath: str, content: str) -> None:
        self._request(
            "POST",
            self._vault_url(filepath),
            headers=self._headers({"Content-Type": "text/markdown; charset=utf-8"}),
            body=content.encode("utf-8"),
        )

    def put_content(self, filepath: str, content: str) -> None:
        self._request(
            "PUT",
            self._vault_url(filepath),
            headers=self._headers({"Content-Type": "text/markdown; charset=utf-8"}),
            body=content.encode("utf-8"),
        )

    def patch_content(self, filepath: str, operation: str, target_type: str, target: str, content: str) -> None:
        try:
            self._patch_content_raw(filepath, operation, target_type, target, content)
        except ObsidianRequestError as exc:
            if target_type != "heading" or "::" in target or "40080" not in str(exc):
                raise
            candidates = find_heading_paths(self.get_file_contents(filepath), target)
            if len(candidates) == 1:
                self._patch_content_raw(filepath, operation, target_type, candidates[0], content)
                return
            if len(candidates) > 1:
                raise ObsidianRequestError(
                    f"Ambiguous heading {target!r}. Candidates: {', '.join(candidates)}. "
                    "Use the qualified path joined with '::'."
                ) from exc
            raise

    def _patch_content_raw(self, filepath: str, operation: str, target_type: str, target: str, content: str) -> None:
        self._request(
            "PATCH",
            self._vault_url(filepath),
            headers=self._headers(
                {
                    "Content-Type": "text/markdown",
                    "Operation": operation,
                    "Target-Type": target_type,
                    "Target": parse.quote(target),
                }
            ),
            body=content.encode("utf-8"),
        )

    def delete_file(self, filepath: str) -> None:
        self._request("DELETE", self._vault_url(filepath), headers=self._headers())

    def get_frontmatter(self, filepath: str) -> dict[str, object]:
        payload = self._json_request(
            "GET",
            self._vault_url(filepath),
            headers=self._headers({"Accept": "application/vnd.olrapi.note+json"}),
        )
        if isinstance(payload, dict):
            frontmatter = payload.get("frontmatter")
            return frontmatter if isinstance(frontmatter, dict) else {}
        return {}

    def search_by_tag(self, tag: str, dirpath: str | None = None) -> list[str]:
        tag_query: dict[str, object] = {"in": [tag.lstrip("#"), {"var": "tags"}]}
        query: dict[str, object]
        if dirpath:
            prefix = dirpath.strip().strip("/") + "/"
            query = {"and": [tag_query, {"glob": [f"{prefix}*", {"var": "path"}]}]}
        else:
            query = tag_query
        results = self.complex_search(query)
        if not isinstance(results, list):
            return []
        return [str(item.get("filename")) for item in results if isinstance(item, dict) and item.get("filename")]

    def get_periodic_note(self, period: str, note_type: str = "content") -> str:
        headers = self._headers()
        if note_type == "metadata":
            headers["Accept"] = "application/vnd.olrapi.note+json"
        _, _, payload = self._request("GET", f"{self.config.base_url}/periodic/{period}/", headers=headers)
        return payload.decode("utf-8")

    def get_recent_periodic_notes(self, period: str, limit: int = 5, include_content: bool = False) -> Any:
        params = parse.urlencode({"limit": int(limit), "includeContent": str(bool(include_content)).lower()})
        return self._json_request(
            "GET",
            f"{self.config.base_url}/periodic/{period}/recent?{params}",
            headers=self._headers(),
        )

    def get_recent_changes(self, limit: int = 10, days: int = 90) -> Any:
        dql_query = "\n".join(
            [
                "TABLE file.mtime",
                f"WHERE file.mtime >= date(today) - dur({int(days)} days)",
                "SORT file.mtime DESC",
                f"LIMIT {int(limit)}",
            ]
        )
        return self._json_request(
            "POST",
            f"{self.config.base_url}/search/",
            headers=self._headers({"Content-Type": "application/vnd.olrapi.dataview.dql+txt"}),
            body=dql_query.encode("utf-8"),
        )


def quote_vault_path(path: str) -> str:
    return "/".join(parse.quote(part) for part in path.split("/") if part)


def parse_error_payload(payload: bytes) -> str | None:
    if not payload:
        return None
    text = payload.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text[:500]
    if isinstance(parsed, dict):
        code = parsed.get("errorCode")
        message = parsed.get("message") or parsed.get("error")
        if code is not None and message:
            return f"Error {code}: {message}"
        if message:
            return str(message)
    return text[:500]


_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def find_heading_paths(content: str, target: str) -> list[str]:
    in_fence = False
    stack: list[tuple[int, str]] = []
    matches: list[str] = []
    target_lower = target.lower()

    for line in content.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING_RE.match(line)
        if not match:
            continue
        level = len(match.group(1))
        text = re.sub(r"\s+#+\s*$", "", match.group(2)).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, text))
        if text.lower() == target_lower:
            matches.append("::".join(item for _, item in stack))
    return matches


def ok(data: object | None = None, **extra: object) -> dict[str, object]:
    result: dict[str, object] = {"success": True}
    if data is not None:
        result["data"] = data
    result.update(extra)
    return result


def fail(exc: Exception) -> dict[str, object]:
    code = "obsidian_not_configured" if isinstance(exc, ObsidianConfigError) else "obsidian_request_failed"
    return {"success": False, "error": {"code": code, "message": str(exc)}}
