import pytest

from services.llm.schema import compile_tool_schema


def test_schema_compiler_inlines_pydantic_refs_without_mutating_input():
    schema = {
        "$defs": {
            "Location": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            }
        },
        "type": "object",
        "properties": {
            "location": {
                "$ref": "#/$defs/Location",
                "description": "Where to search",
            }
        },
    }

    compiled = compile_tool_schema(schema, provider="anthropic")
    assert "$defs" in schema
    assert "$defs" not in compiled
    assert compiled["properties"]["location"] == {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
        "description": "Where to search",
    }


def test_gemini_schema_normalizes_nullable_and_drops_unsupported_metadata():
    compiled = compile_tool_schema(
        {
            "type": "object",
            "properties": {
                "unit": {
                    "type": ["string", "null"],
                    "default": None,
                    "examples": ["celsius"],
                    "displayOptions": {"show": {"operation": ["convert"]}},
                    "placeholder": "celsius",
                    "rows": 3,
                },
                # Property names are data, not schema keywords. A
                # structure-aware filter must retain this field even though
                # the Gemini Schema model does not accept the ``default``
                # annotation keyword.
                "default": {
                    "type": "string",
                    "displayOptions": {"show": {"advanced": [True]}},
                },
                "tags": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "widget": "tags",
                        "displayOptions": {"show": {"advanced": [True]}},
                    },
                },
                "settings": {
                    "type": "object",
                    "additionalProperties": False,
                },
            },
        },
        provider="gemini",
    )
    unit = compiled["properties"]["unit"]
    assert unit == {"type": "string", "nullable": True}
    assert compiled["properties"]["default"] == {"type": "string"}
    assert compiled["properties"]["tags"] == {
        "type": "array",
        "items": {"type": "string"},
    }
    assert compiled["properties"]["settings"]["additionalProperties"] is False


def test_gemini_compiled_pydantic_ui_schema_is_accepted_by_sdk():
    """Regression: real Pydantic schemas must form a valid Gemini config."""

    from typing import Literal

    from google.genai import types
    from pydantic import BaseModel, Field

    class FileModifyParams(BaseModel):
        operation: str = "write"
        content: str = Field(
            default="",
            json_schema_extra={
                "displayOptions": {"show": {"operation": ["write"]}},
                "rows": 6,
                "placeholder": "File contents",
            },
        )
        replace_all: bool = Field(
            default=False,
            json_schema_extra={
                "displayOptions": {"show": {"operation": ["edit"]}},
                "hidden": True,
            },
        )
        memory: Literal[128, 256] = 128

    parameters = compile_tool_schema(
        FileModifyParams.model_json_schema(),
        provider="gemini",
    )
    config = types.GenerateContentConfig(
        tools=[
            {
                "function_declarations": [
                    {
                        "name": "file_modify",
                        "description": "Write or edit a file",
                        "parameters": parameters,
                    }
                ]
            }
        ]
    )

    declaration = config.tools[0].function_declarations[0]
    assert declaration.parameters_json_schema is None
    schema = declaration.parameters
    assert schema is not None
    assert set(schema.properties) == {
        "operation",
        "content",
        "replace_all",
        "memory",
    }
    assert schema.properties["memory"].enum == ["128", "256"]


def test_gemini_schema_preserves_literals_enums_and_nested_nullable_unions():
    compiled = compile_tool_schema(
        {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "const": "lookup",
                },
                "options": {
                    "type": "object",
                    "properties": {
                        "mode": {
                            "anyOf": [
                                {"type": "string", "const": "fast"},
                                {"type": "null"},
                            ]
                        },
                        "unit": {
                            "type": "string",
                            "enum": ["celsius", "fahrenheit"],
                        },
                    },
                },
            },
        },
        provider="gemini",
    )

    assert compiled["properties"]["operation"] == {
        "type": "string",
        "enum": ["lookup"],
    }
    options = compiled["properties"]["options"]["properties"]
    assert options["mode"] == {
        "anyOf": [{"type": "string", "enum": ["fast"]}],
        "nullable": True,
    }
    assert options["unit"]["enum"] == ["celsius", "fahrenheit"]


def test_schema_compiler_rejects_external_and_recursive_refs():
    with pytest.raises(ValueError, match="Only local"):
        compile_tool_schema(
            {"$ref": "https://example.com/schema.json"},
            provider="openai",
        )

    recursive = {
        "$defs": {"Node": {"$ref": "#/$defs/Node"}},
        "$ref": "#/$defs/Node",
    }
    with pytest.raises(ValueError, match="Recursive"):
        compile_tool_schema(recursive, provider="openai")
