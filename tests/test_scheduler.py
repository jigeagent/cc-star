"""Tests for cc-star scheduler module — Windows Task Scheduler integration."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cc_star.scheduler import (
    TASK_NAME,
    _python_exe,
    _run_schtasks,
    _worker_path,
    register,
    status,
    unregister,
)


class TestSchedulerPaths:
    """Test path resolution."""

    def test_worker_path_resolves(self):
        """Test worker path resolves under ~/.cc-star/worker/."""
        path = _worker_path()
        assert str(path).endswith("consolidation_worker.py")
        assert ".cc-star" in str(path)
        assert "worker" in str(path)

    def test_python_exe_returns_string(self):
        """Test python exe resolution."""
        exe = _python_exe()
        assert isinstance(exe, str)
        assert exe.endswith("python.exe") or exe.endswith("python")


class TestRunSchtasks:
    """Test schtasks execution."""

    def test_run_schtasks_returns_completed_process(self):
        """Test _run_schtasks returns a CompletedProcess."""
        result = _run_schtasks(["/?"])
        assert result.returncode in (0, 1)
        assert hasattr(result, "stdout")
        assert hasattr(result, "stderr")


class TestRegister:
    """Test register function."""

    @patch("cc_star.scheduler._run_schtasks")
    def test_register_success(self, mock_run):
        """Test successful registration."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        result = register()
        assert result["ok"] is True
        assert "已注册" in result["message"]

    @patch("cc_star.scheduler._run_schtasks")
    def test_register_failure(self, mock_run):
        """Test registration failure."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Access denied",
        )
        result = register()
        assert result["ok"] is False
        assert "失败" in result["message"]

    def test_register_no_worker_file(self, tmp_path):
        """Test register when worker file doesn't exist."""
        with (
            patch("cc_star.scheduler._worker_path",
                  return_value=tmp_path / "nonexistent.py"),
        ):
            result = register()
            assert result["ok"] is False
            assert "not found" in result["message"].lower()


class TestUnregister:
    """Test unregister function."""

    @patch("cc_star.scheduler._run_schtasks")
    def test_unregister_success(self, mock_run):
        """Test successful unregistration."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        result = unregister()
        assert result["ok"] is True
        assert "已删除" in result["message"]

    @patch("cc_star.scheduler._run_schtasks")
    def test_unregister_not_exists(self, mock_run):
        """Test unregister when task doesn't exist."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="系统找不到指定的文件",
        )
        result = unregister()
        assert result["ok"] is True
        assert "不存在" in result["message"]


class TestStatus:
    """Test status function."""

    @patch("cc_star.scheduler._run_schtasks")
    def test_status_registered(self, mock_run):
        """Test status when task is registered."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='"Host","Task"\r\n"cc-star-consolidation","python worker.py"\r\n',
            stderr="",
        )
        result = status()
        assert result["ok"] is True
        assert result["registered"] is True

    @patch("cc_star.scheduler._run_schtasks")
    def test_status_not_registered(self, mock_run):
        """Test status when task is not registered."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="系统找不到指定的文件",
        )
        result = status()
        assert result["ok"] is True
        assert result["registered"] is False
        assert "未注册" in result["message"]

    @patch("cc_star.scheduler._run_schtasks")
    def test_status_error(self, mock_run):
        """Test status on unexpected error."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=2, stdout="", stderr="Access denied",
        )
        result = status()
        assert result["ok"] is False
