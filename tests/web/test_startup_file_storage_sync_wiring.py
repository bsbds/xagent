from importlib import import_module
from unittest.mock import Mock

import pytest


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
