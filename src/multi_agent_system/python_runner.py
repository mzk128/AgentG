"""Internal child entrypoint; not a general-purpose security sandbox.

The parent launches this with -I -S and no inherited credentials. Python audit
hooks cover ordinary Python IO and process/network APIs; Python introspection
or native code can bypass them. Untrusted code still needs an OS/container boundary.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from types import ModuleType


class PolicyDenied(PermissionError):
    pass


def _install_policy(workspace: Path, library_paths: list[str], denied_paths: list[str]) -> None:
    path_type, decode_path, denied = Path, os.fsdecode, PolicyDenied
    read_roots = [workspace, Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve()]
    read_roots.extend(Path(path).resolve() for path in library_paths)
    denied_roots = [Path(path).resolve() for path in denied_paths]

    def inside(path: Path, root: Path) -> bool:
        return path == root or root in path.parents

    def check_path(value, *, write: bool = False, directory_fd: int | None = None) -> None:
        if isinstance(value, int):
            # No inherited file descriptors other than stdin/stdout/stderr are
            # passed by the parent. File descriptors cannot establish new paths.
            return
        if directory_fd is not None and directory_fd != -1:
            raise denied("不支持通过目录描述符绕过工作目录路径校验。")
        path = path_type(decode_path(value))

        def reject_protected(candidate: Path) -> None:
            if any(part.split(":", 1)[0].lower().rstrip(" .") == ".env" for part in candidate.parts):
                raise denied("禁止访问保护文件 .env。")

        reject_protected(path)
        resolved = path.resolve()
        reject_protected(resolved)
        if any(inside(resolved, root) for root in denied_roots):
            raise denied("禁止访问长期记忆存储目录。")
        if write and any(inside(root, resolved) for root in denied_roots):
            raise denied("禁止改写或移动长期记忆存储目录的祖先。")
        roots = [workspace] if write else read_roots
        if not any(inside(resolved, root) for root in roots):
            raise denied("文件路径不在允许的工作目录或 Python 库目录内。")

    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

    def audit(event: str, args: tuple) -> None:
        if event == "open":
            path, mode, flags = args
            write = bool(flags & write_flags) or bool(mode and any(c in mode for c in "wax+"))
            check_path(path, write=write)
        elif event == "sqlite3.connect":
            database = os.fsdecode(args[0])
            if database != ":memory:":
                if database.startswith("file:"):
                    raise denied("Python 工具不接受 SQLite URI 路径。")
                check_path(database, write=True)
        elif event in {"os.listdir", "os.scandir"}:
            check_path(args[0] if args[0] is not None else ".")
        elif event == "os.chdir":
            check_path(args[0], write=True)
        elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.utime", "os.truncate"}:
            directory_fd = args[1] if event in {"os.remove", "os.rmdir"} else None
            if event in {"os.mkdir", "os.chmod"}:
                directory_fd = args[2]
            if event in {"os.chown", "os.utime"}:
                directory_fd = args[3]
            check_path(args[0], write=True, directory_fd=directory_fd)
        elif event in {"os.rename", "os.link"}:
            check_path(args[0], write=True, directory_fd=args[2])
            check_path(args[1], write=True, directory_fd=args[3])
        elif event == "os.symlink":
            # A new link can change the meaning of a previously checked path.
            raise denied("Python 工具不能创建符号链接。")
        elif event.startswith("socket."):
            raise denied("Python 工具禁止网络操作，请使用专用研究工具。")
        elif event.startswith("subprocess.") or event in {
            "os.system", "os.fork", "os.forkpty", "os.posix_spawn", "os.exec",
            "os.spawn", "os.startfile", "os.startfile/2", "os.kill", "os.killpg",
        }:
            raise denied("Python 工具禁止派生进程或控制其他进程。")

    sys.addaudithook(audit)


def main() -> int:
    payload = json.loads(sys.stdin.buffer.read())
    workspace = Path(payload["workspace"]).resolve()
    library_paths = payload["libraries"]
    sys.dont_write_bytecode = True
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", newline="\n")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", newline="\n")
    original_stderr = sys.stderr
    _install_policy(workspace, library_paths, payload.get("denied_paths", []))
    # Expose installed packages without running arbitrary .pth/sitecustomize.
    sys.path.extend(path for path in library_paths if path not in sys.path)
    try:
        # Normal `import __main__` must see generated code's namespace, not
        # runner helpers. This is hygiene, not protection from introspection.
        generated_module = ModuleType("__main__")
        generated_module.__file__ = "<agentg-python>"
        sys.modules["__main__"] = generated_module
        namespace = generated_module.__dict__
        exec(compile(payload["code"], "<agentg-python>", "exec"), namespace, namespace)
        return 0
    except BaseException as exc:
        try:
            original_stderr.write(f"{type(exc).__name__}: {exc}\n")
            original_stderr.flush()
        except BaseException:
            pass
        return 77 if isinstance(exc, PolicyDenied) else 1


if __name__ == "__main__":
    raise SystemExit(main())
