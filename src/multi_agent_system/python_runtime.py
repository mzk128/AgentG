"""Bounded, disposable Python execution for the Python tool.

Process isolation protects the web process's globals, cwd and stdout. The child
also applies a Python audit policy, but this is deliberately not described as an
OS sandbox: Python introspection and native extensions can bypass that policy.
"""

from __future__ import annotations

import codecs
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import sysconfig
import threading
import time
from typing import Any

from .memory_paths import protected_memory_roots


def _result(ok: bool, *, data: dict[str, Any] | None = None,
            error_code: str = "", message: str = "", retryable: bool = False) -> dict[str, Any]:
    return {
        "ok": ok,
        "data": data or {"stdout": "", "stderr": "", "output_truncated": False, "exit_code": None},
        "error_code": error_code,
        "message": message,
        "retryable": retryable,
    }


class _OutputCapture:
    """Drain both pipes continuously while retaining a shared character budget."""

    def __init__(self, limit: int):
        self.remaining = limit
        self.parts: dict[str, list[str]] = {"stdout": [], "stderr": []}
        self.truncated = False
        self.lock = threading.Lock()

    def append(self, stream: str, value: str) -> None:
        with self.lock:
            kept = value[: self.remaining]
            if kept:
                self.parts[stream].append(kept)
                self.remaining -= len(kept)
            if len(kept) < len(value):
                self.truncated = True

    def drain(self, stream: str, pipe: Any) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while chunk := pipe.read(4096):
                self.append(stream, decoder.decode(chunk))
            self.append(stream, decoder.decode(b"", final=True))
        except (OSError, ValueError):
            pass

    def data(self, exit_code: int | None) -> dict[str, Any]:
        with self.lock:
            return {
                "stdout": "".join(self.parts["stdout"]),
                "stderr": "".join(self.parts["stderr"]),
                "output_truncated": self.truncated,
                "exit_code": exit_code,
            }


def _child_environment(workspace: str) -> dict[str, str]:
    # An allowlist prevents API keys, Redis credentials, proxy URLs and arbitrary
    # parent environment variables from entering generated code's process.
    environment = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHON_DOTENV_DISABLED": "1",
        "TMP": workspace,
        "TEMP": workspace,
        "TMPDIR": workspace,
    }
    if os.name == "nt":
        for name in ("SYSTEMROOT", "WINDIR"):
            if name in os.environ:
                environment[name] = os.environ[name]
        system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        environment["PATH"] = os.pathsep.join([
            str(Path(sys.executable).parent),
            str(Path(sys.prefix) / "Library" / "bin"),
            str(Path(system_root) / "System32"),
        ])
    return environment


def execute_python(code: str, workspace_path: str = "", timeout_seconds: float = 15,
                   max_output_chars: int = 12000, *, denied_paths: list[str] | None = None) -> dict[str, Any]:
    """Run one script with fresh state, a hard deadline and bounded output.

    Output is ``{ok, data, error_code, message, retryable}``; ``data`` contains
    stdout, stderr, output_truncated and exit_code. The output limit is shared by
    stdout and stderr. Ordinary filesystem writes stay in the selected workspace;
    protected .env paths, network operations and descendant processes are denied
    by the child's audit hook. These checks are defense in depth, not an OS-level
    guarantee for hostile code. Calls never preserve Python variables.
    """
    if not isinstance(code, str) or not code.strip() or len(code) > 1_000_000:
        return _result(False, error_code="python_invalid_input", message="Python 代码不能为空或超过 100 万字符。")
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
        return _result(False, error_code="python_invalid_input", message="Python 超时必须是有限正数。")
    if isinstance(max_output_chars, bool) or not isinstance(max_output_chars, int) or max_output_chars <= 0:
        return _result(False, error_code="python_invalid_input", message="输出长度上限必须是正整数。")
    try:
        workspace = Path(workspace_path or Path.cwd()).resolve(strict=True)
        if not workspace.is_dir() or any(part.lower().rstrip(" .") == ".env" for part in workspace.parts):
            raise ValueError("invalid workspace")
    except (OSError, TypeError, ValueError):
        return _result(False, error_code="python_workspace_invalid", message="工作目录必须是已存在且允许访问的目录。")

    runner = Path(__file__).with_name("python_runner.py")
    libraries = list(dict.fromkeys([
        sysconfig.get_path("purelib"), sysconfig.get_path("platlib"),
    ]))
    payload = json.dumps({"code": code, "workspace": str(workspace), "libraries": libraries,
                          "denied_paths": list(dict.fromkeys([
                              *protected_memory_roots(), *(denied_paths or []),
                          ]))}).encode("utf-8")
    capture = _OutputCapture(max_output_chars)
    deadline = time.monotonic() + float(timeout_seconds)
    process = None
    readers: list[threading.Thread] = []
    try:
        # -I ignores PYTHONPATH/current-directory imports; -S avoids executing
        # sitecustomize and .pth startup hooks before the audit policy is active.
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", "-u", str(runner)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(workspace), env=_child_environment(str(workspace)),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        for name in ("stdout", "stderr"):
            reader = threading.Thread(target=capture.drain, args=(name, getattr(process, name)), daemon=True)
            reader.start()
            readers.append(reader)

        def send_input() -> None:
            try:
                process.stdin.write(payload)
                process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass

        sender = threading.Thread(target=send_input, daemon=True)
        sender.start()
        timed_out = False
        try:
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "nt":
                process.kill()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
        sender.join(timeout=1)
        for reader in readers:
            reader.join(timeout=1)
        data = capture.data(process.returncode)
        if timed_out:
            return _result(False, data=data, error_code="python_timeout", message="Python 执行超过时限，进程已终止。")
        if process.returncode == 77:
            return _result(False, data=data, error_code="python_policy_denied", message="Python 操作被工作目录、保护文件、网络或子进程策略拒绝。")
        if process.returncode != 0:
            return _result(False, data=data, error_code="python_execution_error", message="Python 执行失败，请根据 stderr 修正代码。")
        message = "输出超过长度上限，已截断。" if data["output_truncated"] else "Python 执行完成。"
        return _result(True, data=data, message=message)
    except OSError as exc:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        return _result(False, data=capture.data(process.returncode if process else None),
                       error_code="python_process_error", message=f"无法运行 Python 进程：{type(exc).__name__}。", retryable=True)
    finally:
        if process is not None:
            # A daemon reader may still own a pipe only if hostile native code
            # bypassed the no-descendants policy. Do not wait on that pipe lock.
            for name, reader in zip(("stdout", "stderr"), readers):
                if not reader.is_alive():
                    getattr(process, name).close()
