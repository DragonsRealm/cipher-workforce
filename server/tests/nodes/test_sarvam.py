"""Contract tests for the three Sarvam text service nodes.

sarvamTranslate / sarvamTransliterate / sarvamDetectLanguage.

All three authenticate through ``ctx.connection("sarvam")``, which injects
the ``api-subscription-key`` header from the shared credential — the same
key the OpenAI-compatible ``sarvamChatModel`` uses. httpx is mocked with
respx so no network is touched.

Sarvam's speech endpoints are covered by ``test_speech.py`` instead: they
are reached through the provider-abstracted ``textToSpeech`` /
``speechToText`` nodes now, with Sarvam as one provider among several.
"""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest
import respx

from tests.nodes._mocks import patched_container, patched_pricing


pytestmark = pytest.mark.node_contract

_KEYS = {"sarvam": "sk_sarvam_test"}
_BASE = "https://api.sarvam.ai"


# ============================================================================
# sarvamTranslate
# ============================================================================


class TestSarvamTranslate:
    URL = f"{_BASE}/translate"

    @respx.mock
    async def test_happy_path(self, harness):
        respx.post(self.URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "request_id": "req-1",
                    "translated_text": "नमस्ते दुनिया",
                    "source_language_code": "en-IN",
                },
            )
        )

        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "sarvamTranslate",
                {
                    "input": "Hello world",
                    "source_language_code": "auto",
                    "target_language_code": "hi-IN",
                },
            )

        harness.assert_envelope(result, success=True)
        payload = result["result"]
        assert payload["translated_text"] == "नमस्ते दुनिया"
        assert payload["source_language_code"] == "en-IN"

        sent = respx.calls.last.request
        assert sent.headers["api-subscription-key"] == "sk_sarvam_test"

    @respx.mock
    async def test_unset_optionals_are_omitted_not_nulled(self, harness):
        """Sarvam 422s on explicit nulls for several optional fields."""
        import json

        respx.post(self.URL).mock(
            return_value=httpx.Response(
                200, json={"translated_text": "x", "source_language_code": "en-IN"}
            )
        )

        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            await harness.execute(
                "sarvamTranslate",
                {"input": "hi", "target_language_code": "ta-IN"},
            )

        body = json.loads(respx.calls.last.request.content)
        assert "speaker_gender" not in body
        assert "output_script" not in body
        assert body["model"] == "sarvam-translate:v1"

    async def test_input_over_model_cap_is_a_user_error(self, harness):
        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "sarvamTranslate",
                {
                    "input": "x" * 1001,
                    "model": "mayura:v1",
                    "target_language_code": "hi-IN",
                },
            )

        harness.assert_envelope(result, success=False)
        assert "1001 characters" in result["error"]
        assert "mayura:v1" in result["error"]

    async def test_identical_language_pair_is_a_user_error(self, harness):
        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "sarvamTranslate",
                {
                    "input": "hi",
                    "source_language_code": "hi-IN",
                    "target_language_code": "hi-IN",
                },
            )

        harness.assert_envelope(result, success=False)
        assert "identical" in result["error"]

    @respx.mock
    async def test_api_error_surfaces_sarvam_message(self, harness):
        respx.post(self.URL).mock(
            return_value=httpx.Response(
                422, json={"error": {"message": "unsupported language pair"}}
            )
        )

        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "sarvamTranslate",
                {"input": "hi", "target_language_code": "hi-IN"},
            )

        harness.assert_envelope(result, success=False)
        assert "unsupported language pair" in result["error"]
        assert "422" in result["error"]


# ============================================================================
# sarvamTransliterate
# ============================================================================


class TestSarvamTransliterate:
    URL = f"{_BASE}/transliterate"

    @respx.mock
    async def test_happy_path(self, harness):
        respx.post(self.URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "transliterated_text": "नमस्ते",
                    "source_language_code": "en-IN",
                },
            )
        )

        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "sarvamTransliterate",
                {"input": "namaste", "target_language_code": "hi-IN"},
            )

        harness.assert_envelope(result, success=True)
        assert result["result"]["transliterated_text"] == "नमस्ते"

    @respx.mock
    async def test_spoken_form_numerals_only_sent_when_spoken_form_on(self, harness):
        import json

        respx.post(self.URL).mock(
            return_value=httpx.Response(
                200, json={"transliterated_text": "x", "source_language_code": "en-IN"}
            )
        )

        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            await harness.execute(
                "sarvamTransliterate",
                {"input": "42", "target_language_code": "hi-IN", "spoken_form": False},
            )

        body = json.loads(respx.calls.last.request.content)
        assert body["spoken_form"] is False
        assert "spoken_form_numerals_language" not in body


# ============================================================================
# sarvamDetectLanguage
# ============================================================================


class TestSarvamDetectLanguage:
    URL = f"{_BASE}/text-lid"

    @respx.mock
    async def test_happy_path(self, harness):
        respx.post(self.URL).mock(
            return_value=httpx.Response(
                200,
                json={"request_id": "r", "language_code": "hi-IN", "script_code": "Deva"},
            )
        )

        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "sarvamDetectLanguage", {"input": "नमस्ते"}
            )

        harness.assert_envelope(result, success=True)
        assert result["result"]["language_code"] == "hi-IN"
        assert result["result"]["script_code"] == "Deva"

    @respx.mock
    async def test_nulls_survive_as_none(self, harness):
        """Sarvam returns nulls for unclassifiable input; don't coerce them."""
        respx.post(self.URL).mock(
            return_value=httpx.Response(
                200, json={"language_code": None, "script_code": None}
            )
        )

        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute("sarvamDetectLanguage", {"input": "?!"})

        harness.assert_envelope(result, success=True)
        assert result["result"]["language_code"] is None

    async def test_over_cap_input_is_a_user_error(self, harness):
        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "sarvamDetectLanguage", {"input": "x" * 1001}
            )

        harness.assert_envelope(result, success=False)
        assert "1000" in result["error"]


# ============================================================================
# Cross-node contracts
# ============================================================================


class TestSarvamPluginContract:
    NODE_TYPES = (
        "sarvamTranslate",
        "sarvamTransliterate",
        "sarvamDetectLanguage",
    )

    @pytest.mark.parametrize("node_type", NODE_TYPES)
    def test_registered_in_the_language_group_and_usable_as_tool(self, node_type):
        from services.node_registry import get_node_class

        cls = get_node_class(node_type)
        assert cls is not None
        assert cls.group == ("language", "tool")
        assert cls.usable_as_tool is True
        assert cls.task_queue == "rest-api"

    @pytest.mark.parametrize("node_type", NODE_TYPES)
    def test_tool_name_is_the_snake_case_node_type(self, node_type):
        """Skill `allowed-tools` resolution keys on this exact mapping."""
        import re

        from services.node_registry import get_node_class

        expected = re.sub(r"(?<!^)(?=[A-Z])", "_", node_type).lower()
        assert get_node_class(node_type).tool_name == expected

    @pytest.mark.parametrize("node_type", NODE_TYPES)
    def test_tool_schema_has_no_refs(self, node_type):
        """LLM tool schemas must be flat — no $defs/$ref survive inlining.

        Mirrors what ``AIService._get_tool_schema`` does for a plugin
        exposed as a tool: inline the Params JSON schema.
        """
        from services.node_registry import get_node_class
        from services.plugin.tool import inline_schema_refs

        schema = inline_schema_refs(
            get_node_class(node_type).Params.model_json_schema()
        )
        rendered = repr(schema)
        assert "$defs" not in rendered
        assert "$ref" not in rendered

    @pytest.mark.parametrize("node_type", NODE_TYPES)
    def test_shares_the_single_sarvam_credential(self, node_type):
        from nodes.model._credentials import SarvamCredential
        from services.node_registry import get_node_class

        assert get_node_class(node_type).credentials == (SarvamCredential,)

    def test_credential_injects_sarvams_native_header(self):
        """Chat rides Bearer via the openai SDK; these APIs need this header."""
        from nodes.model._credentials import SarvamCredential

        req = SarvamCredential.inject({"api_key": "k"}, {"headers": {}})
        assert req["headers"] == {"api-subscription-key": "k"}

    @pytest.mark.parametrize("node_type", NODE_TYPES)
    def test_output_schema_is_registered(self, node_type):
        """Drives the parameter panel's "Input Data & Variables" listing."""
        from services.node_output_schemas import get_node_output_schema

        schema = get_node_output_schema(node_type)
        assert schema is not None
        assert schema.get("properties")
