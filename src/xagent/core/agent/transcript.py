"""Helpers for building and normalizing persisted chat transcripts."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def build_assistant_transcript_content(
    content: str, interactions: Optional[List[Any]] = None
) -> str:
    """Build assistant transcript text that preserves interactive prompts."""
    content_parts = [content]

    if interactions:
        content_parts.append("\n\nPlease answer the following questions:")
        for interaction in interactions:
            interaction_type = _get_interaction_attr(interaction, "type")
            options = _get_interaction_attr(interaction, "options") or []
            label = _get_interaction_attr(interaction, "label")
            placeholder = _get_interaction_attr(interaction, "placeholder")
            accept = _get_interaction_attr(interaction, "accept") or []
            multiple = bool(_get_interaction_attr(interaction, "multiple"))
            default = _get_interaction_attr(interaction, "default")
            minimum = _get_interaction_attr(interaction, "min")
            maximum = _get_interaction_attr(interaction, "max")

            if interaction_type == "select_one":
                options_desc = ", ".join(
                    [
                        f"{_safe_option_value(option, 'value')}: {_safe_option_value(option, 'label')}"
                        for option in options
                    ]
                )
                content_parts.append(f"- {label or 'Select'}: {options_desc}")
            elif interaction_type == "select_multiple":
                options_desc = ", ".join(
                    [
                        f"{_safe_option_value(option, 'value')}: {_safe_option_value(option, 'label')}"
                        for option in options
                    ]
                )
                content_parts.append(
                    f"- {label or 'Select multiple options'}: {options_desc}"
                )
            elif interaction_type == "text_input":
                content_parts.append(
                    f"- {label or 'Enter text'}: {placeholder or 'text input'}"
                )
            elif interaction_type == "file_upload":
                accept_desc = ", ".join(str(item) for item in accept) if accept else "any file"
                multiple_desc = "multiple files allowed" if multiple else "single file"
                content_parts.append(
                    f"- {label or 'Upload file'}: {accept_desc} ({multiple_desc})"
                )
            elif interaction_type == "confirm":
                default_desc = "Default: yes" if default else "Default: no"
                content_parts.append(f"- {label or 'Confirm'} ({default_desc})")
            elif interaction_type == "number_input":
                range_desc = ""
                if minimum is not None and maximum is not None:
                    range_desc = f" (range: {minimum}-{maximum})"
                content_parts.append(f"- {label or 'Enter number'}{range_desc}")

    return "\n".join(content_parts)


def normalize_transcript_messages(
    messages: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    """Normalize transcript messages for use by LLM-backed chat patterns."""
    normalized: List[Dict[str, str]] = []
    for message in messages:
        role = str(message.get("role", "")).strip().lower()
        content = str(message.get("content", "")).strip()
        if role not in {"user", "assistant", "system"} or not content:
            continue
        normalized.append({"role": role, "content": content})
    return normalized


def _get_interaction_attr(interaction: Any, key: str) -> Any:
    if isinstance(interaction, dict):
        return interaction.get(key)
    return getattr(interaction, key, None)


def _safe_option_value(option: Any, key: str) -> str:
    if isinstance(option, dict):
        return str(option.get(key, ""))
    return str(getattr(option, key, ""))
