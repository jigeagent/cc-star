"""Windows Task Scheduler integration for cc-star consolidation worker.

Manages a daily 3:00 AM scheduled task that runs consolidation_worker.py.
Uses native schtasks.exe — zero dependencies.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

TASK_NAME = "cc-star-consolidation"
TASK_DESC = "cc-star v0.7.0 nightly memory consolidation — graph extraction + task state detection"
WORKER_RELPATH = "consolidation_worker.py"


def _worker_path() -> Path:
    """Resolve the consolidation_worker.py path under ~/.cc-star/worker/."""
    return Path.home() / ".cc-star" / "worker" / WORKER_RELPATH


def _python_exe() -> str:
    """Return the Python executable path, always forward-slash for schtasks."""
    return sys.executable.replace("\\", "/")


def _run_schtasks(args: list[str]) -> subprocess.CompletedProcess:
    """Run schtasks.exe with the given arguments."""
    cmd = ["schtasks.exe"] + args
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def register() -> dict[str, str | bool]:
    """Register the scheduled task to run consolidation_worker.py daily at 3:00 AM.

    Returns a status dict with 'ok' and 'message' keys.
    """
    worker = _worker_path()
    python = _python_exe()

    if not worker.is_file():
        return {
            "ok": False,
            "message": f"Worker script not found: {worker} — 请先运行 cc-star init",
        }

    # Build the command string for schtasks /tr
    # schtasks needs backslashes in paths, but python path must use forward slashes
    # (Windows Task Scheduler handles both fine in /tr argument)
    cmd_str = f"{python} {worker.as_posix()}"

    result = _run_schtasks([
        "/create",
        "/tn", TASK_NAME,
        "/tr", cmd_str,
        "/sc", "daily",
        "/st", "03:00",
        "/f",  # Force overwrite if exists
        "/ru", "SYSTEM",
    ])

    if result.returncode == 0:
        return {
            "ok": True,
            "message": (
                f"✅ 已注册计划任务 {TASK_NAME}\n"
                f"   执行: {cmd_str}\n"
                f"   时间: 每天 03:00\n"
                f"   账户: SYSTEM"
            ),
        }
    else:
        return {
            "ok": False,
            "message": f"❌ 注册失败 (schtasks exit {result.returncode}): {result.stderr.strip()}",
        }


def unregister() -> dict[str, str | bool]:
    """Remove the scheduled task."""
    result = _run_schtasks(["/delete", "/tn", TASK_NAME, "/f"])

    if result.returncode == 0:
        return {
            "ok": True,
            "message": f"✅ 已删除计划任务 {TASK_NAME}",
        }
    else:
        stderr = result.stderr.strip()
        missing_markers = ["does not exist", "系统找不到"]
        if any(m in stderr.lower() for m in missing_markers):
            return {
                "ok": True,
                "message": f"⏭️  计划任务 {TASK_NAME} 不存在，无需删除",
            }
        return {
            "ok": False,
            "message": f"❌ 删除失败 (schtasks exit {result.returncode}): {stderr}",
        }


def status() -> dict[str, str | bool | dict]:
    """Check if the scheduled task exists and show its details.

    Returns a status dict with 'ok', 'message', and optional 'task' details.
    """
    result = _run_schtasks(["/query", "/tn", TASK_NAME, "/v", "/fo", "csv"])

    if result.returncode != 0:
        stderr = result.stderr.strip()
        # schtasks returns error + "does not exist" (EN) / "系统找不到" (中文) when task is missing
        missing_markers = ["does not exist", "系统找不到"]
        if any(m in stderr.lower() for m in missing_markers):
            return {
                "ok": True,
                "registered": False,
                "message": f"⏭️  计划任务 {TASK_NAME} 未注册",
            }
        return {
            "ok": False,
            "registered": False,
            "message": f"❌ 查询失败 (schtasks exit {result.returncode}): {stderr}",
        }

    # Parse CSV output: header line + data line
    lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
    if len(lines) < 2:
        return {
            "ok": True,
            "registered": True,
            "message": f"✅ 计划任务 {TASK_NAME} 已注册（详情解析异常）",
        }

    fields = [f.strip().strip('"') for f in lines[1].split('","')]
    schedule = ""
    task_path = ""
    for i, header in enumerate([h.strip().strip('"') for h in lines[0].split('","')]):
        if i < len(fields):
            if "schedule" in header.lower():
                schedule = fields[i]
            elif "task to run" in header.lower():
                task_path = fields[i]

    return {
        "ok": True,
        "registered": True,
        "message": (
            f"✅ 计划任务 {TASK_NAME} 已注册\n"
            f"   执行: {task_path or '（见任务计划程序）'}\n"
            f"   时间: 每天 03:00"
        ),
    }
