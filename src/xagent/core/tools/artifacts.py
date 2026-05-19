"""Helpers for exposing generated files as displayable tool artifacts."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

from ..file_ref import build_workspace_file_ref, guess_mime_type

logger = logging.getLogger(__name__)

GENERATED_ARTIFACT_EXTENSIONS = {
    ".csv",
    ".docx",
    ".gif",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".pptx",
    ".svg",
    ".webp",
    ".xls",
    ".xlsx",
}

GeneratedArtifactSnapshot = dict[Path, tuple[int, int]]


def artifact_type_for_filename(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}:
        return "image"
    if suffix in {".ppt", ".pptx"}:
        return "presentation"
    if suffix == ".docx":
        return "document"
    if suffix in {".csv", ".xls", ".xlsx"}:
        return "spreadsheet"
    return "file"


def build_inline_artifact(file_ref: dict[str, Any]) -> dict[str, str]:
    filename = str(file_ref.get("filename") or "artifact")
    return {
        "type": artifact_type_for_filename(filename),
        "file_id": str(file_ref.get("file_id") or ""),
        "filename": filename,
        "mime_type": str(file_ref.get("mime_type") or guess_mime_type(filename)),
        "display": "inline",
    }


def markdown_reference_for_artifact(artifact: dict[str, Any]) -> str | None:
    file_id = artifact.get("file_id")
    if not file_id:
        return None

    filename = str(artifact.get("filename") or "artifact")
    markdown_ref = f"file:{file_id}"
    artifact_type = str(
        artifact.get("type") or artifact_type_for_filename(filename)
    ).lower()
    if artifact_type == "image":
        return f"![{filename}]({markdown_ref})"
    return f"[{filename}]({markdown_ref})"


def format_tool_result_for_observation(tool_name: str, result: Any) -> str:
    """Format tool results for model-facing observations.

    The formatter may expose artifact usage conventions, but it stays transport
    neutral: concrete browser routes are a frontend/web concern.
    """
    if not isinstance(result, dict):
        return str(result)

    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return str(result)

    sanitized = dict(result)
    if sanitized.get("file_id"):
        sanitized.pop("image_path", None)

    artifact_lines = _format_artifact_lines(artifacts)
    if not artifact_lines:
        return str(sanitized)

    return (
        f"Tool '{tool_name}' produced displayable artifact(s):\n"
        + "\n".join(artifact_lines)
        + "\nUse the Markdown/chat form in assistant messages. "
        + "When writing HTML for Xagent preview, reference the same file_id "
        + "through the file preview service instead of local filesystem paths. "
        + f"Sanitized result metadata: {sanitized}"
    )


def _format_artifact_lines(artifacts: list[Any]) -> list[str]:
    lines: list[str] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        file_id = artifact.get("file_id")
        if not file_id:
            continue
        filename = artifact.get("filename") or "generated image"
        markdown_ref = markdown_reference_for_artifact(artifact)
        if not markdown_ref:
            continue
        artifact_type = str(artifact.get("type") or "").lower()
        markdown_label = (
            "Markdown/chat image" if artifact_type == "image" else "Markdown/chat file"
        )
        lines.append(
            "\n".join(
                [
                    f"- {filename}",
                    f"  file_id: {file_id}",
                    f"  {markdown_label}: {markdown_ref}",
                    "  HTML preview: use the file preview service for this file_id",
                ]
            )
        )
    return lines


def build_generated_file_metadata(
    *,
    workspace: Any,
    file_paths: Iterable[str | Path],
) -> dict[str, list[Any]]:
    file_refs: list[dict[str, Any]] = []
    artifacts: list[dict[str, str]] = []
    generated_files: list[str] = []

    for file_path in sorted({Path(path).resolve() for path in file_paths}):
        if not file_path.exists() or not file_path.is_file():
            continue
        if file_path.suffix.lower() not in GENERATED_ARTIFACT_EXTENSIONS:
            continue
        try:
            file_ref = build_workspace_file_ref(
                workspace=workspace, file_path=file_path
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to build FileRef for generated file %s: %s", file_path, exc
            )
            continue
        file_refs.append(file_ref)
        artifacts.append(build_inline_artifact(file_ref))
        generated_files.append(file_ref["filename"])

    return {
        "generated_files": generated_files,
        "file_refs": file_refs,
        "artifacts": artifacts,
    }


def scan_generated_artifact_files(root: str | Path) -> set[Path]:
    return set(snapshot_generated_artifact_files(root))


def snapshot_generated_artifact_files(root: str | Path) -> GeneratedArtifactSnapshot:
    root_path = Path(root)
    if not root_path.exists():
        return {}

    snapshot: GeneratedArtifactSnapshot = {}
    for file_path in root_path.rglob("*"):
        if (
            not file_path.is_file()
            or any(part.startswith(".") for part in file_path.parts)
            or file_path.suffix.lower() not in GENERATED_ARTIFACT_EXTENSIONS
        ):
            continue
        file_stat = file_path.stat()
        snapshot[file_path] = (file_stat.st_mtime_ns, file_stat.st_size)

    return snapshot


def changed_generated_artifact_files(
    before: GeneratedArtifactSnapshot,
    after: GeneratedArtifactSnapshot,
) -> set[Path]:
    return {
        file_path
        for file_path, file_snapshot in after.items()
        if before.get(file_path) != file_snapshot
    }
