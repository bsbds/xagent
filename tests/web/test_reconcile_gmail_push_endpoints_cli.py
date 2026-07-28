import json

from sqlalchemy import inspect

from xagent.web import reconcile_gmail_push_endpoints as cli
from xagent.web.models import database
from xagent.web.services.gmail_provisioning import (
    GmailPushEndpointReconciliation,
)


class FakeSession:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_cli_defaults_to_audit_mode(monkeypatch, capsys) -> None:
    session = FakeSession()
    calls: list[bool] = []
    configured: list[bool] = []
    monkeypatch.setattr(
        cli,
        "configure_db",
        lambda *, read_only: configured.append(read_only),
        raising=False,
    )
    monkeypatch.setattr(
        cli,
        "init_db",
        lambda: (_ for _ in ()).throw(AssertionError("audit initialized the schema")),
    )
    monkeypatch.setattr(cli, "get_session_local", lambda: lambda: session)
    monkeypatch.setattr(
        cli,
        "reconcile_gmail_push_endpoints",
        lambda _db, *, execute: (
            calls.append(execute)
            or GmailPushEndpointReconciliation(
                scanned=2,
                changed=1,
                unchanged=1,
                failed=0,
            )
        ),
    )

    assert cli.run([]) == 0
    assert configured == [True]
    assert calls == [False]
    assert session.closed is True
    assert json.loads(capsys.readouterr().out) == {
        "mode": "audit",
        "scanned": 2,
        "changed": 1,
        "unchanged": 1,
        "failed": 0,
        "errors": [],
    }


def test_cli_execute_returns_failure_when_any_subscription_fails(
    monkeypatch, capsys
) -> None:
    session = FakeSession()
    initialized: list[bool] = []
    monkeypatch.setattr(cli, "init_db", lambda: initialized.append(True))
    monkeypatch.setattr(
        cli,
        "configure_db",
        lambda: (_ for _ in ()).throw(
            AssertionError("execute bypassed schema initialization")
        ),
        raising=False,
    )
    monkeypatch.setattr(cli, "get_session_local", lambda: lambda: session)
    monkeypatch.setattr(
        cli,
        "reconcile_gmail_push_endpoints",
        lambda _db, *, execute: GmailPushEndpointReconciliation(
            scanned=1,
            changed=0,
            unchanged=0,
            failed=1,
            errors=("watch 9: permission denied",),
        ),
    )

    assert cli.run(["--execute"]) == 1
    assert initialized == [True]
    assert json.loads(capsys.readouterr().out)["mode"] == "execute"
    assert session.closed is True


def test_configure_db_does_not_create_or_seed_schema(tmp_path) -> None:
    database.configure_db(
        f"sqlite:///{tmp_path / 'audit.db'}",
        read_only=True,
    )

    assert inspect(database.get_engine()).get_table_names() == []
