from __future__ import annotations

import argparse
import os
import select
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import TextIO

from .config import APP_NAME, GRACEFUL_SHUTDOWN_SECONDS, HOST, PORT, STATE_DIR, ensure_runtime_directories

SUPERVISOR_PID_FILENAME = "dev-tunnel-supervisor.pid"
DEFAULT_READY_TIMEOUT_SECONDS = float(
    os.environ.get("CHATGPT_MCP_RELOAD_READY_TIMEOUT_SECONDS", "15")
)


def default_pid_file() -> Path:
    return STATE_DIR / SUPERVISOR_PID_FILENAME


def _log(message: str, stream: TextIO) -> None:
    print(message, file=stream, flush=True)


def _write_pid_file(pid_file: Path) -> None:
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()), encoding="utf-8")


def _remove_pid_file(pid_file: Path) -> None:
    if not pid_file.exists():
        return
    try:
        if pid_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
            pid_file.unlink()
    except OSError:
        return


def _bind_listener(host: str, port: int) -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, port))
    listener.listen(socket.SOMAXCONN)
    listener.set_inheritable(True)
    return listener


def _terminate_process(
    process: subprocess.Popen[bytes],
    *,
    timeout: float,
    stream: TextIO,
    reason: str,
) -> None:
    if process.poll() is not None:
        return
    _log(f"[supervisor] stopping pid={process.pid} ({reason})", stream)
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _log(f"[supervisor] killing pid={process.pid} after {timeout:.1f}s timeout", stream)
        process.kill()
        process.wait(timeout=5)


def _wait_for_ready_pipe(
    process: subprocess.Popen[bytes],
    ready_read_fd: int,
    *,
    timeout: float,
) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"server process exited early with code {process.returncode}")
        remaining = max(0.0, deadline - time.time())
        readable, _, _ = select.select([ready_read_fd], [], [], min(0.25, remaining))
        if not readable:
            continue
        payload = os.read(ready_read_fd, 64)
        if payload.startswith(b"ready"):
            return
        raise RuntimeError(f"unexpected readiness payload: {payload!r}")
    raise RuntimeError(f"timed out waiting for server readiness after {timeout:.1f}s")


def _spawn_server(
    *,
    listener_fd: int,
    log_file: Path,
    ready_timeout: float,
    stream: TextIO,
) -> subprocess.Popen[bytes]:
    ready_read_fd, ready_write_fd = os.pipe()
    os.set_inheritable(ready_write_fd, True)
    env = os.environ.copy()
    env["CHATGPT_MCP_READY_FD"] = str(ready_write_fd)
    command = [
        sys.executable,
        "-m",
        "chatgpt_web_oauth_mcp.server",
        "--fd",
        str(listener_fd),
    ]
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("ab", buffering=0) as log_handle:
        process = subprocess.Popen(
            command,
            env=env,
            pass_fds=(listener_fd, ready_write_fd),
            stdout=log_handle,
            stderr=log_handle,
        )
    os.close(ready_write_fd)
    try:
        _wait_for_ready_pipe(process, ready_read_fd, timeout=ready_timeout)
    except Exception:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        raise
    finally:
        os.close(ready_read_fd)
    _log(f"[supervisor] server pid={process.pid} ready", stream)
    return process


class RollingServerSupervisor:
    def __init__(
        self,
        *,
        pid_file: Path,
        log_file: Path,
        host: str,
        port: int,
        ready_timeout: float,
        shutdown_timeout: float,
        stream: TextIO,
    ) -> None:
        self.pid_file = pid_file
        self.log_file = log_file
        self.host = host
        self.port = port
        self.ready_timeout = ready_timeout
        self.shutdown_timeout = shutdown_timeout
        self.stream = stream
        self.listener = _bind_listener(host, port)
        self.current: subprocess.Popen[bytes] | None = None
        self._reload_requested = False
        self._stop_requested = False

    def _handle_reload(self, signum: int, _frame) -> None:
        _log(f"[supervisor] received signal {signum}, scheduling reload", self.stream)
        self._reload_requested = True

    def _handle_stop(self, signum: int, _frame) -> None:
        _log(f"[supervisor] received signal {signum}, stopping", self.stream)
        self._stop_requested = True

    def _install_signal_handlers(self) -> None:
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, self._handle_reload)
        signal.signal(signal.SIGTERM, self._handle_stop)
        signal.signal(signal.SIGINT, self._handle_stop)

    def _start_initial_server(self) -> None:
        self.current = _spawn_server(
            listener_fd=self.listener.fileno(),
            log_file=self.log_file,
            ready_timeout=self.ready_timeout,
            stream=self.stream,
        )

    def _reload_server(self) -> None:
        if self.current is None:
            self._start_initial_server()
            return
        _log("[supervisor] starting rolling reload", self.stream)
        new_process = _spawn_server(
            listener_fd=self.listener.fileno(),
            log_file=self.log_file,
            ready_timeout=self.ready_timeout,
            stream=self.stream,
        )
        old_process = self.current
        self.current = new_process
        _terminate_process(
            old_process,
            timeout=self.shutdown_timeout,
            stream=self.stream,
            reason="post-reload drain",
        )

    def run(self) -> int:
        ensure_runtime_directories()
        _write_pid_file(self.pid_file)
        self._install_signal_handlers()
        _log(
            (
                f"[supervisor] starting {APP_NAME} on {self.host}:{self.port} "
                f"pid_file={self.pid_file} log_file={self.log_file}"
            ),
            self.stream,
        )
        try:
            self._start_initial_server()
            while not self._stop_requested:
                if self._reload_requested:
                    self._reload_requested = False
                    try:
                        self._reload_server()
                    except Exception as exc:
                        _log(f"[supervisor] reload failed: {exc}", self.stream)
                if self.current is not None and self.current.poll() is not None:
                    _log(
                        f"[supervisor] active server exited with code {self.current.returncode}",
                        self.stream,
                    )
                    return int(self.current.returncode or 0)
                time.sleep(0.1)
            return 0
        finally:
            if self.current is not None:
                _terminate_process(
                    self.current,
                    timeout=self.shutdown_timeout,
                    stream=self.stream,
                    reason="supervisor shutdown",
                )
            self.listener.close()
            _remove_pid_file(self.pid_file)
            _log("[supervisor] shutdown complete", self.stream)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run chatgpt-web-oauth-mcp behind a rolling-reload supervisor."
    )
    parser.add_argument(
        "--pid-file",
        type=Path,
        default=default_pid_file(),
        help="Path to the supervisor pid file.",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        required=True,
        help="Append child server stdout/stderr to this log file.",
    )
    parser.add_argument(
        "--ready-timeout",
        type=float,
        default=DEFAULT_READY_TIMEOUT_SECONDS,
        help="Seconds to wait for a freshly spawned server to report readiness.",
    )
    parser.add_argument(
        "--shutdown-timeout",
        type=float,
        default=float(GRACEFUL_SHUTDOWN_SECONDS + 5),
        help="Seconds to wait for the drained child to exit before force-killing it.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    supervisor = RollingServerSupervisor(
        pid_file=args.pid_file,
        log_file=args.log_file,
        host=HOST,
        port=PORT,
        ready_timeout=args.ready_timeout,
        shutdown_timeout=args.shutdown_timeout,
        stream=sys.stderr,
    )
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
