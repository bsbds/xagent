from __future__ import annotations

from typing import Any


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

    artifact_lines = _format_image_artifact_lines(artifacts)
    if not artifact_lines:
        return str(sanitized)

    return (
        f"Tool '{tool_name}' produced image artifact(s):\n"
        + "\n".join(artifact_lines)
        + "\nUse the Markdown/chat image form in assistant messages. "
        + "When writing HTML for Xagent preview, reference the same file_id "
        + "through the file preview service instead of local filesystem paths. "
        + f"Sanitized result metadata: {sanitized}"
    )


def _format_image_artifact_lines(artifacts: list[Any]) -> list[str]:
    lines: list[str] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or artifact.get("type") != "image":
            continue
        file_id = artifact.get("file_id")
        if not file_id:
            continue
        filename = artifact.get("filename") or "generated image"
        markdown_ref = f"file:{file_id}"
        lines.append(
            "\n".join(
                [
                    f"- {filename}",
                    f"  file_id: {file_id}",
                    f"  Markdown/chat image: ![{filename}]({markdown_ref})",
                    "  HTML preview: use the file preview service for this file_id",
                ]
            )
        )
    return lines
