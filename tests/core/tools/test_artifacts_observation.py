from xagent.core.tools.artifacts import format_tool_result_for_observation


def test_format_tool_result_for_observation_hides_image_path_when_artifact_exists():
    observation = format_tool_result_for_observation(
        "generate_image",
        {
            "success": True,
            "image_path": "/Users/example/uploads/generated_image.png",
            "file_id": "582e7b79-4de9-4905-b73b-7d5a70ad64fe",
            "artifacts": [
                {
                    "type": "image",
                    "file_id": "582e7b79-4de9-4905-b73b-7d5a70ad64fe",
                    "filename": "generated_image.png",
                    "mime_type": "image/png",
                    "display": "inline",
                }
            ],
        },
    )

    assert "/Users/example/uploads/generated_image.png" not in observation
    assert (
        "![generated_image.png](file:582e7b79-4de9-4905-b73b-7d5a70ad64fe)"
        in observation
    )
    assert "file preview service" in observation
    assert "/api/files/public/preview/" not in observation


def test_format_tool_result_for_observation_returns_plain_string_without_artifacts():
    result = {"success": True, "output": "done"}

    assert format_tool_result_for_observation("tool", result) == str(result)


def test_format_tool_result_for_observation_mentions_office_artifact_links():
    observation = format_tool_result_for_observation(
        "execute_python_code",
        {
            "success": True,
            "artifacts": [
                {
                    "type": "document",
                    "file_id": "doc-file-id",
                    "filename": "report.docx",
                    "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "display": "inline",
                },
                {
                    "type": "spreadsheet",
                    "file_id": "sheet-file-id",
                    "filename": "data.xlsx",
                    "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "display": "inline",
                },
                {
                    "type": "presentation",
                    "file_id": "slides-file-id",
                    "filename": "deck.pptx",
                    "mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    "display": "inline",
                },
            ],
        },
    )

    assert "[report.docx](file:doc-file-id)" in observation
    assert "[data.xlsx](file:sheet-file-id)" in observation
    assert "[deck.pptx](file:slides-file-id)" in observation
    assert "Markdown/chat file" in observation


def test_format_tool_result_for_observation_normalizes_artifact_type_case():
    observation = format_tool_result_for_observation(
        "generate_image",
        {
            "success": True,
            "artifacts": [
                {
                    "type": "Image",
                    "file_id": "image-file-id",
                    "filename": "plot.png",
                    "mime_type": "image/png",
                    "display": "inline",
                }
            ],
        },
    )

    assert "![plot.png](file:image-file-id)" in observation
    assert "Markdown/chat image" in observation
