from xagent.core.artifact_refs.observation import format_tool_result_for_observation


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
