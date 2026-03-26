from __future__ import annotations

import copy
from typing import Any


def normalize_structured_output_schema(schema: Any) -> Any:
    """
    Normalize a JSON schema for strict structured-output providers such as
    OpenAI Responses/Codex.

    Rules applied recursively:
    - every object gets additionalProperties = False
    - every object's required contains every property key
    - property-level defaults are removed
    """

    def force(node: Any) -> Any:
        if isinstance(node, list):
            return [force(item) for item in node]
        if not isinstance(node, dict):
            return node

        # OpenAI structured outputs: $ref cannot have sibling keywords
        if "$ref" in node:
            return {"$ref": node["$ref"]}

        for key in ("anyOf", "oneOf", "allOf"):
            if isinstance(node.get(key), list):
                node[key] = [force(item) for item in node[key]]
        for key in ("not", "if", "then", "else", "items"):
            if key in node:
                node[key] = force(node[key])
        if isinstance(node.get("properties"), dict):
            node["properties"] = {
                key: force(value) for key, value in node["properties"].items()
            }
        if isinstance(node.get("patternProperties"), dict):
            node["patternProperties"] = {
                key: force(value) for key, value in node["patternProperties"].items()
            }
        if isinstance(node.get("$defs"), dict):
            node["$defs"] = {key: force(value) for key, value in node["$defs"].items()}
        if isinstance(node.get("additionalProperties"), dict):
            node["additionalProperties"] = force(node["additionalProperties"])
        if isinstance(node.get("prefixItems"), list):
            node["prefixItems"] = [force(item) for item in node["prefixItems"]]
        if node.get("type") == "object" or "properties" in node:
            node["additionalProperties"] = False
            if isinstance(node.get("properties"), dict):
                node["required"] = list(node["properties"].keys())
                for property_schema in node["properties"].values():
                    if isinstance(property_schema, dict):
                        property_schema.pop("default", None)
        return node

    if not isinstance(schema, (dict, list)):
        return schema
    return force(copy.deepcopy(schema))
