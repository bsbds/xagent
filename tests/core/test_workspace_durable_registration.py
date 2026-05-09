import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from xagent.core.file_storage.factory import get_file_storage
from xagent.core.workspace import TaskWorkspace
from xagent.web.models import Base
from xagent.web.models.task import Task
from xagent.web.models.uploaded_file import UploadedFile
from xagent.web.models.user import User


def test_workspace_register_file_writes_durable_storage(
    monkeypatch, tmp_path, mock_workspace_db
):
    # Override the global autouse fixture from tests/conftest.py for this module.
    del mock_workspace_db
    object_root = tmp_path / "objects"
    monkeypatch.setenv("XAGENT_FILE_STORAGE_URI", object_root.as_uri())
    get_file_storage.cache_clear()

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        user = User(username="workspace-user", password_hash="hash")
        db.add(user)
        db.flush()
        task = Task(id=123, user_id=user.id, title="Workspace task")
        db.add(task)
        db.commit()

        workspace = TaskWorkspace(
            id="web_task_123", base_dir=str(tmp_path / "workspaces")
        )
        output_path = workspace.output_dir / "report.txt"
        output_path.write_text("workspace output", encoding="utf-8")

        file_id = workspace.register_file(str(output_path), db_session=db)
        db.commit()

        record = db.query(UploadedFile).filter(UploadedFile.file_id == file_id).one()
        assert record.storage_status == "available"
        assert record.storage_backend == "file"
        assert record.storage_key == (
            f"users/{user.id}/tasks/123/outputs/{file_id}/output/report.txt"
        )
        assert record.workspace_relative_path == "output/report.txt"
        assert record.workspace_category == "output"

        object_files = [path for path in object_root.rglob("*") if path.is_file()]
        assert len(object_files) == 1
        assert object_files[0].read_text(encoding="utf-8") == "workspace output"
    finally:
        db.close()
        engine.dispose()


@pytest.fixture
def mock_workspace_db():
    yield
