"""Real, offline child-process tests; never open or create any .env file."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import time

import pytest

from multi_agent_system import python_runtime
from multi_agent_system.python_runtime import execute_python


def test_execution_has_fresh_globals_and_does_not_change_parent(tmp_path):
    original_cwd, original_stdout = Path.cwd(), sys.stdout
    first = execute_python("private_value = 42\nprint(private_value)", str(tmp_path))
    second = execute_python("print('private_value' in globals())", str(tmp_path))
    assert first["ok"] and first["data"]["stdout"] == "42\n"
    assert second["ok"] and second["data"]["stdout"] == "False\n"
    assert Path.cwd() == original_cwd
    assert sys.stdout is original_stdout


def test_main_module_is_generated_namespace_not_runner_internals(tmp_path):
    result = execute_python(
        "import __main__\nvalue = 42\nprint(__main__.value)\nprint(hasattr(__main__, '_install_policy'))", str(tmp_path),
    )
    assert result["ok"]
    assert result["data"]["stdout"] == "42\nFalse\n"


@pytest.mark.parametrize("operation", ["read", "write", "list", "sqlite", "wal", "rename_parent"])
def test_memory_directory_is_denied_even_inside_workspace(tmp_path, operation):
    memory = tmp_path / "private" / "memory"
    memory.mkdir(parents=True)
    database = memory / "memory.sqlite3"
    database.write_text("synthetic-memory-only")
    paths = {"db": str(database), "root": str(memory), "parent": str(memory.parent),
             "wal": str(database) + "-wal", "move": str(tmp_path / "moved")}
    scripts = {
        "read": f"print(open({paths['db']!r}).read())",
        "write": f"open({paths['db']!r}, 'w').write('changed')",
        "list": f"import os; print(os.listdir({paths['root']!r}))",
        "sqlite": f"import sqlite3; sqlite3.connect({paths['db']!r})",
        "wal": f"open({paths['wal']!r}, 'w').write('changed')",
        "rename_parent": f"import os; os.rename({paths['parent']!r}, {paths['move']!r})",
    }
    result = execute_python(scripts[operation], str(tmp_path), denied_paths=[str(memory)])
    assert not result["ok"] and result["error_code"] == "python_policy_denied"
    assert database.read_text() == "synthetic-memory-only"
    assert not Path(paths["wal"]).exists()


def test_memory_protection_does_not_deny_unrelated_workspace_files(tmp_path):
    memory = tmp_path / "private" / "memory"
    memory.mkdir(parents=True)
    result = execute_python("open('allowed.txt', 'w').write('ok')\nprint('ok')", str(tmp_path),
                            denied_paths=[str(memory)])
    assert result["ok"] and tmp_path.joinpath("allowed.txt").read_text() == "ok"


def test_workspace_reads_writes_and_parallel_calls_are_isolated(tmp_path):
    workspaces = [tmp_path / "first", tmp_path / "second"]
    for workspace in workspaces:
        workspace.mkdir()
    script = "from pathlib import Path\np = Path('result.txt')\np.write_text(Path.cwd().name)\nprint(p.read_text())"
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda directory: execute_python(script, str(directory)), workspaces))
    assert all(result["ok"] for result in results)
    assert [result["data"]["stdout"].strip() for result in results] == ["first", "second"]
    assert [path.joinpath("result.txt").read_text() for path in workspaces] == ["first", "second"]


def test_credentials_and_proxy_configuration_are_not_inherited(tmp_path, monkeypatch):
    for key in ("OPENAI_API_KEY", "REDIS_URL", "HTTP_PROXY", "AGENTG_PRIVATE_VALUE"):
        monkeypatch.setenv(key, "synthetic-test-only")
    result = execute_python(
        "import os\nprint([key for key in ['OPENAI_API_KEY', 'REDIS_URL', 'HTTP_PROXY', 'AGENTG_PRIVATE_VALUE'] if key in os.environ])",
        str(tmp_path),
    )
    assert result["ok"]
    assert result["data"]["stdout"] == "[]\n"


def test_hard_timeout_kills_and_reaps_child(tmp_path, monkeypatch):
    processes = []
    original_popen = python_runtime.subprocess.Popen

    def record_popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(python_runtime.subprocess, "Popen", record_popen)
    started = time.monotonic()
    result = execute_python("while True:\n    pass", str(tmp_path), timeout_seconds=0.6)
    assert not result["ok"] and result["error_code"] == "python_timeout"
    assert time.monotonic() - started < 5
    assert len(processes) == 1 and processes[0].poll() is not None


def test_large_stdout_and_stderr_are_drained_with_one_shared_limit(tmp_path):
    result = execute_python(
        "import sys\nprint('你' * 200000)\nsys.stderr.write('x' * 200000)",
        str(tmp_path), max_output_chars=128,
    )
    data = result["data"]
    assert result["ok"]
    assert data["output_truncated"]
    assert len(data["stdout"]) + len(data["stderr"]) == 128
    assert "\ufffd" not in data["stdout"]


def test_infinite_output_does_not_block_timeout(tmp_path):
    result = execute_python("while True:\n    print('x' * 4096)", str(tmp_path), timeout_seconds=0.6, max_output_chars=64)
    assert result["error_code"] == "python_timeout"
    assert result["data"]["output_truncated"]
    assert len(result["data"]["stdout"]) <= 64


@pytest.mark.parametrize("code", [
    "open('.env').read()",
    "open('.env', 'w').write('forbidden')",
    "from pathlib import Path\nPath('.env').unlink()",
])
def test_nonexistent_protected_env_path_is_denied_before_io(tmp_path, code):
    result = execute_python(code, str(tmp_path))
    assert result["error_code"] == "python_policy_denied"
    assert "PolicyDenied" in result["data"]["stderr"]
    assert "FileNotFoundError" not in result["data"]["stderr"]
    assert not (tmp_path / ".env").exists()


def test_write_and_read_outside_workspace_are_denied(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    for code in (f"open({str(outside)!r}, 'w').write('forbidden')", f"open({str(outside)!r}).read()"):
        result = execute_python(code, str(workspace))
        assert result["error_code"] == "python_policy_denied"
    assert not outside.exists()


def test_truncate_outside_workspace_is_denied(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep this test data")
    result = execute_python(f"import os\nos.truncate({str(outside)!r}, 0)", str(workspace))
    assert result["error_code"] == "python_policy_denied"
    assert outside.read_text() == "keep this test data"


def test_symlink_target_outside_workspace_is_denied(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (workspace / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not permit creating the test symlink")
    result = execute_python("open('link/result.txt', 'w').write('forbidden')", str(workspace))
    assert result["error_code"] == "python_policy_denied"
    assert not (outside / "result.txt").exists()


@pytest.mark.parametrize("code", [
    "import socket\nsocket.socket()",
    "import subprocess, sys\nsubprocess.run([sys.executable, '-c', 'print(1)'])",
    "import os\nos.system('echo forbidden')",
])
def test_network_and_descendant_processes_are_denied_before_execution(tmp_path, code):
    result = execute_python(code, str(tmp_path))
    assert result["error_code"] == "python_policy_denied"
    assert not result["ok"]


def test_common_data_libraries_work_without_site_startup_hooks(tmp_path):
    result = execute_python(
        "import numpy as np\nimport pandas as pd\n"
        "frame = pd.DataFrame({'value': np.array([1, 2, 3])})\n"
        "frame.to_csv('data.csv', index=False)\nprint(int(pd.read_csv('data.csv')['value'].sum()))",
        str(tmp_path), timeout_seconds=20,
    )
    assert result["ok"], result
    assert result["data"]["stdout"] == "6\n"


@pytest.mark.parametrize("code", ["raise ValueError('test failure')", "raise SystemExit(0)"])
def test_script_errors_and_exit_do_not_terminate_parent(tmp_path, code):
    result = execute_python(code, str(tmp_path))
    assert not result["ok"] and result["error_code"] == "python_execution_error"
    assert result["data"]["stderr"]


@pytest.mark.parametrize("kwargs", [
    {"code": ""}, {"code": None}, {"timeout_seconds": 0},
    {"timeout_seconds": float("inf")}, {"max_output_chars": 0},
])
def test_invalid_inputs_are_rejected_before_process_start(tmp_path, kwargs):
    options = {"code": "print(1)", "workspace_path": str(tmp_path), **kwargs}
    assert execute_python(**options)["error_code"] == "python_invalid_input"


def test_missing_workspace_is_not_created(tmp_path):
    missing = tmp_path / "missing"
    assert execute_python("print(1)", str(missing))["error_code"] == "python_workspace_invalid"
    assert not missing.exists()
