"""Download exported Google Workspace files through the Drive LRO API."""

from __future__ import annotations

import logging
from pathlib import Path
from time import monotonic, sleep
from typing import Any, cast

from google.auth.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession

logger = logging.getLogger(__name__)

_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
_DOWNLOAD_HTTP_TIMEOUT_SECONDS = 60


class GoogleDriveDownloadError(RuntimeError):
    """Report a failed or invalid Drive download operation."""


class GoogleDriveDownloadTimeout(GoogleDriveDownloadError):
    """Report a Drive operation that exceeded the caller's wait limit."""


def _update_resource_key_header(
    headers: dict[str, str],
    *,
    file_id: str,
    operation: dict[str, Any],
) -> None:
    """Apply the resource key returned in Drive operation metadata."""
    metadata = operation.get("metadata")
    if not isinstance(metadata, dict):
        return
    resource_key = metadata.get("resourceKey")
    if resource_key:
        headers["X-Goog-Drive-Resource-Keys"] = f"{file_id}/{resource_key}"


def download_google_workspace_file(
    *,
    service: Any,
    credentials: Credentials,
    file_id: str,
    mime_type: str,
    destination: Path,
    timeout_seconds: float,
    resource_key: str | None = None,
) -> None:
    """Export a Google Workspace file and stream it to ``destination``.

    Drive returns an operation instead of file bytes. This synchronous helper
    owns that protocol so callers can move the complete blocking operation to a
    worker thread.
    """
    headers: dict[str, str] = {}
    if resource_key:
        headers["X-Goog-Drive-Resource-Keys"] = f"{file_id}/{resource_key}"

    request = service.files().download(fileId=file_id, mimeType=mime_type)
    request.headers.update(headers)
    operation = cast(dict[str, Any], request.execute())
    _update_resource_key_header(headers, file_id=file_id, operation=operation)
    started_at = monotonic()
    deadline = started_at + timeout_seconds
    logger.info(
        "Drive download operation started file_id=%s operation_name=%s",
        file_id,
        operation.get("name", "completed-inline"),
    )
    poll_delay_seconds = 10.0
    while operation.get("done") is not True:
        operation_name = operation.get("name")
        if not operation_name:
            raise GoogleDriveDownloadError(
                "Drive returned a pending operation without a name"
            )
        remaining_seconds = deadline - monotonic()
        if remaining_seconds <= 0:
            raise GoogleDriveDownloadTimeout(
                f"Drive download for {file_id} did not finish within "
                f"{timeout_seconds:g} seconds"
            )
        sleep(min(poll_delay_seconds, remaining_seconds))
        poll_request = service.operations().get(
            name=str(operation_name).removeprefix("operations/")
        )
        poll_request.headers.update(headers)
        operation = cast(dict[str, Any], poll_request.execute())
        _update_resource_key_header(headers, file_id=file_id, operation=operation)
        poll_delay_seconds = min(poll_delay_seconds * 2, 60.0)

    if "error" in operation:
        error = cast(dict[str, Any], operation["error"])
        code = error.get("code", "unknown")
        message = error.get("message", "Unknown Drive operation error")
        raise GoogleDriveDownloadError(f"Drive operation failed ({code}): {message}")

    logger.info(
        "Drive download operation completed file_id=%s elapsed_seconds=%.3f",
        file_id,
        monotonic() - started_at,
    )

    response_metadata = operation.get("response")
    if not isinstance(response_metadata, dict):
        raise GoogleDriveDownloadError(
            "Drive completed the operation without a response"
        )
    download_uri = response_metadata.get("downloadUri")
    if not download_uri:
        raise GoogleDriveDownloadError(
            "Drive completed the operation without a download URI"
        )

    try:
        with AuthorizedSession(credentials) as session:
            with session.get(
                download_uri,
                stream=True,
                timeout=_DOWNLOAD_HTTP_TIMEOUT_SECONDS,
                headers=headers,
            ) as response:
                try:
                    response.raise_for_status()
                except Exception as exc:
                    status_code = getattr(response, "status_code", "unknown")
                    raise GoogleDriveDownloadError(
                        f"Final Drive download failed with HTTP status {status_code}"
                    ) from exc
                try:
                    output_file = destination.open("wb")
                except OSError as exc:
                    reason = exc.strerror or "local filesystem error"
                    raise GoogleDriveDownloadError(
                        f"Failed to write Drive download: {reason}"
                    ) from exc

                with output_file as output:
                    for chunk in response.iter_content(chunk_size=_DOWNLOAD_CHUNK_SIZE):
                        if not chunk:
                            continue
                        try:
                            output.write(chunk)
                        except OSError as exc:
                            reason = exc.strerror or "local filesystem error"
                            raise GoogleDriveDownloadError(
                                f"Failed to write Drive download: {reason}"
                            ) from exc
    except GoogleDriveDownloadError:
        raise
    except Exception as exc:
        raise GoogleDriveDownloadError("Final Drive download request failed") from exc
