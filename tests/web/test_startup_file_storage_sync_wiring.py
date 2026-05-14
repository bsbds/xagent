from importlib import import_module
from unittest.mock import Mock

import asyncio
import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient


def test_startup_file_storage_sync_skips_when_disabled(monkeypatch):
    app_module = import_module("xagent.web.app")

    monkeypatch.setattr(
        app_module, "get_file_storage_startup_sync_enabled", lambda: False
    )
    sync_mock = Mock()
    monkeypatch.setattr(
        "xagent.web.services.startup_file_storage_sync.sync_registered_files_to_durable_storage",
        sync_mock,
    )

    app_module.run_startup_file_storage_sync()

    sync_mock.assert_not_called()


def test_startup_file_storage_sync_runs_when_enabled(monkeypatch):
    app_module = import_module("xagent.web.app")

    db = Mock()
    session_factory = Mock(return_value=db)
    get_session_local = Mock(return_value=session_factory)
    sync_mock = Mock()
    monkeypatch.setattr(
        app_module, "get_file_storage_startup_sync_enabled", lambda: True
    )
    monkeypatch.setattr(
        "xagent.web.models.database.get_session_local", get_session_local
    )
    monkeypatch.setattr(
        "xagent.web.services.startup_file_storage_sync.sync_registered_files_to_durable_storage",
        sync_mock,
    )

    app_module.run_startup_file_storage_sync()

    get_session_local.assert_called_once_with()
    session_factory.assert_called_once_with()
    sync_mock.assert_called_once_with(db)
    db.close.assert_called_once_with()


def test_startup_file_storage_sync_propagates_errors_and_closes_db(monkeypatch):
    app_module = import_module("xagent.web.app")

    db = Mock()
    session_factory = Mock(return_value=db)
    get_session_local = Mock(return_value=session_factory)
    monkeypatch.setattr(
        app_module, "get_file_storage_startup_sync_enabled", lambda: True
    )
    monkeypatch.setattr(
        "xagent.web.models.database.get_session_local", get_session_local
    )
    monkeypatch.setattr(
        "xagent.web.services.startup_file_storage_sync.sync_registered_files_to_durable_storage",
        Mock(side_effect=RuntimeError("s3 unavailable")),
    )

    with pytest.raises(RuntimeError, match="s3 unavailable"):
        app_module.run_startup_file_storage_sync()

    db.close.assert_called_once_with()


def test_startup_file_storage_sync_gate_waits_for_http_requests():
    app_module = import_module("xagent.web.app")

    test_app = FastAPI()
    test_app.add_middleware(app_module.FileStorageStartupSyncGateMiddleware)
    events: list[str] = []

    @test_app.on_event("startup")
    async def _startup() -> None:
        import asyncio

        async def _sync() -> None:
            events.append("sync-started")
            await asyncio.sleep(0.01)
            events.append("sync-completed")

        test_app.state.file_storage_startup_sync_task = asyncio.create_task(_sync())

    @test_app.get("/api/client")
    async def _client_route() -> dict[str, str]:
        events.append("route-called")
        return {"status": "served"}

    with TestClient(test_app) as client:
        response = client.get("/api/client")

    assert response.status_code == 200
    assert response.json() == {"status": "served"}
    assert events == ["sync-started", "sync-completed", "route-called"]


def test_startup_file_storage_sync_gate_exempts_health_while_sync_runs():
    app_module = import_module("xagent.web.app")

    test_app = FastAPI()
    test_app.add_middleware(app_module.FileStorageStartupSyncGateMiddleware)

    @test_app.on_event("startup")
    async def _startup() -> None:
        import asyncio

        async def _sync() -> None:
            await asyncio.sleep(10)

        test_app.state.file_storage_startup_sync_task = asyncio.create_task(_sync())

    @test_app.get("/health")
    async def _health() -> dict[str, str]:
        return {"status": "ok"}

    with TestClient(test_app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_startup_file_storage_sync_gate_returns_503_when_sync_fails():
    app_module = import_module("xagent.web.app")

    test_app = FastAPI()
    test_app.add_middleware(app_module.FileStorageStartupSyncGateMiddleware)

    @test_app.on_event("startup")
    async def _startup() -> None:
        import asyncio

        async def _sync() -> None:
            raise RuntimeError("s3 unavailable")

        test_app.state.file_storage_startup_sync_task = asyncio.create_task(_sync())

    @test_app.get("/api/client")
    async def _client_route() -> dict[str, str]:
        return {"status": "served"}

    with TestClient(test_app, raise_server_exceptions=False) as client:
        response = client.get("/api/client")

    assert response.status_code == 503
    assert response.json()["detail"] == "Startup file storage sync failed"


def test_startup_file_storage_sync_gate_waits_for_websocket_connections():
    app_module = import_module("xagent.web.app")

    test_app = FastAPI()
    test_app.add_middleware(app_module.FileStorageStartupSyncGateMiddleware)
    events: list[str] = []

    @test_app.on_event("startup")
    async def _startup() -> None:
        import asyncio

        async def _sync() -> None:
            events.append("sync-started")
            await asyncio.sleep(0.01)
            events.append("sync-completed")

        test_app.state.file_storage_startup_sync_task = asyncio.create_task(_sync())

    @test_app.websocket("/ws/client")
    async def _websocket_route(websocket: WebSocket) -> None:
        events.append("websocket-called")
        await websocket.accept()
        await websocket.send_text("served")
        await websocket.close()

    with TestClient(test_app) as client:
        with client.websocket_connect("/ws/client") as websocket:
            assert websocket.receive_text() == "served"

    assert events == ["sync-started", "sync-completed", "websocket-called"]


def test_startup_file_storage_sync_task_is_scheduled_async(monkeypatch):
    app_module = import_module("xagent.web.app")

    monkeypatch.setattr(
        app_module, "get_file_storage_startup_sync_enabled", lambda: True
    )
    to_thread_mock = Mock(return_value="to-thread-coro")
    sync_task = Mock()
    create_task_mock = Mock(return_value=sync_task)
    monkeypatch.setattr(app_module.asyncio, "to_thread", to_thread_mock)
    monkeypatch.setattr(app_module.asyncio, "create_task", create_task_mock)

    test_app = FastAPI()
    task = app_module.start_file_storage_startup_sync_task(test_app)

    assert task == sync_task
    to_thread_mock.assert_called_once_with(app_module.run_startup_file_storage_sync)
    create_task_mock.assert_called_once_with("to-thread-coro")
    sync_task.add_done_callback.assert_called_once()
    assert test_app.state.file_storage_startup_sync_task == sync_task


@pytest.mark.asyncio
async def test_startup_file_storage_sync_task_cancellation_is_not_recorded_as_failure(
    monkeypatch,
):
    app_module = import_module("xagent.web.app")

    test_app = FastAPI()
    monkeypatch.setattr(
        app_module, "get_file_storage_startup_sync_enabled", lambda: True
    )

    async def _never_finishes(_fn):
        await asyncio.Event().wait()

    monkeypatch.setattr(app_module.asyncio, "to_thread", _never_finishes)

    task = app_module.start_file_storage_startup_sync_task(test_app)
    assert task is not None

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert test_app.state.file_storage_startup_sync_error is None
    assert test_app.state.file_storage_startup_sync_completed is False
