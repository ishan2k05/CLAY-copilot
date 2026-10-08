"""
Unit tests for CLAY Backend LifecycleManager and Abort/Cancellation Architecture.
Validates async task cancellation, process tree termination, SQLite WAL flushing,
and FastAPI endpoint handlers.
"""

import asyncio
import os
import sqlite3
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from app.core.lifecycle import lifecycle_manager
from app.main import app

client = TestClient(app)


def test_lifecycle_task_registration_and_cancellation():
    """Verify async tasks can be registered, tracked, and cancelled safely."""
    async def sample_coroutine():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass

    async def run_test():
        task = asyncio.create_task(sample_coroutine())
        lifecycle_manager.register_task("chat", "test_task_1", task)

        assert "test_task_1" in lifecycle_manager._active_tasks
        assert not task.done()

        cancelled = lifecycle_manager.cancel_task("test_task_1")
        assert cancelled is True
        await asyncio.sleep(0.01)
        assert task.done()
        assert "test_task_1" not in lifecycle_manager._active_tasks

    asyncio.run(run_test())


def test_lifecycle_process_registration_and_termination():
    """Verify child processes can be tracked and terminated."""
    mock_proc = MagicMock()
    mock_proc.pid = 99999
    mock_proc.poll.return_value = None

    lifecycle_manager.register_process("cmd_1", mock_proc)
    assert "cmd_1" in lifecycle_manager._active_processes

    # Terminate single process
    stopped = lifecycle_manager.terminate_process("cmd_1")
    assert stopped is True
    assert mock_proc.terminate.called or mock_proc.kill.called
    assert "cmd_1" not in lifecycle_manager._active_processes


def test_lifecycle_cancel_category():
    """Verify cancelling all tasks in a specific category (e.g. agent)."""
    async def sample_coro():
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass

    async def run_test():
        t1 = asyncio.create_task(sample_coro())
        t2 = asyncio.create_task(sample_coro())
        lifecycle_manager.register_task("agent", "agent_1", t1)
        lifecycle_manager.register_task("agent", "agent_2", t2)
        lifecycle_manager.register_task("chat", "chat_1", asyncio.create_task(sample_coro()))

        count = lifecycle_manager.cancel_tasks_by_category("agent")
        assert count == 2
        await asyncio.sleep(0.01)
        assert t1.done()
        assert t2.done()
        assert "chat_1" in lifecycle_manager._active_tasks

        # Clean up remaining
        lifecycle_manager.cancel_task("chat_1")

    asyncio.run(run_test())


def test_sqlite_wal_flush(tmp_path):
    """Verify SQLite WAL buffer checkpoint and flush executes without errors."""
    db_file = tmp_path / "test_store.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("CREATE TABLE kv (k TEXT, v TEXT);")
    conn.execute("INSERT INTO kv VALUES ('alpha', 'beta');")
    conn.commit()
    conn.close()

    result = lifecycle_manager.flush_sqlite_wal(str(db_file))
    assert result is True


def test_api_abort_endpoints():
    """Verify FastAPI abort endpoints respond with valid JSON status."""
    # Test chat abort
    res_chat = client.post("/api/chat/abort")
    assert res_chat.status_code == 200
    data_chat = res_chat.json()
    assert data_chat["status"] == "aborted"
    assert "cancelled_tasks" in data_chat

    # Test agent abort
    res_agent = client.post("/api/agent/abort")
    assert res_agent.status_code == 200
    data_agent = res_agent.json()
    assert data_agent["status"] == "aborted"

    # Test terminal abort
    res_term = client.post("/api/agent/terminal/abort")
    assert res_term.status_code == 200
    data_term = res_term.json()
    assert data_term["status"] == "aborted"

    # Test system reload
    res_reload = client.post("/api/system/reload")
    assert res_reload.status_code == 200
    data_reload = res_reload.json()
    assert data_reload["status"] == "reloaded"


def test_health_check_endpoints():
    """Verify /health and /api/health return healthy status for sidecar and watchdog."""
    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "healthy"
    assert "pid" in data

    res_api = client.get("/api/health")
    assert res_api.status_code == 200
    assert res_api.json()["status"] == "healthy"


def test_system_restart_endpoint():
    """Verify /api/system/restart schedules a restart and returns valid status."""
    with patch("os._exit"), patch("subprocess.Popen"):
        res = client.post("/api/system/restart")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "restarting"
        assert "pid" in data


