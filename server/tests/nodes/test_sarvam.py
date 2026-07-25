"""Contract tests for the five Sarvam service nodes.

sarvamTranslate / sarvamTransliterate / sarvamDetectLanguage /
sarvamSpeechToText / sarvamTextToSpeech.

All five authenticate through ``ctx.connection("sarvam")``, which injects
the ``api-subscription-key`` header from the shared credential — the same
key the OpenAI-compatible ``sarvamChatModel`` uses. httpx is mocked with
respx so no network is touched.
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
# sarvamSpeechToText
# ============================================================================


class TestSarvamSpeechToText:
    URL = f"{_BASE}/speech-to-text"

    @respx.mock
    async def test_accepts_a_file_path(self, harness, tmp_path):
        clip = tmp_path / "clip.wav"
        clip.write_bytes(b"RIFF....WAVE")
        respx.post(self.URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "transcript": "hello",
                    "language_code": "en-IN",
                    "language_probability": 0.98,
                },
            )
        )

        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "sarvamSpeechToText", {"audio_file": str(clip)}
            )

        harness.assert_envelope(result, success=True)
        assert result["result"]["transcript"] == "hello"

        sent = respx.calls.last.request
        assert sent.headers["content-type"].startswith("multipart/form-data")
        assert b"RIFF" in sent.content

    @respx.mock
    async def test_accepts_the_file_widget_upload_envelope(self, harness):
        """The file widget sends {type, data, filename, mimeType}, not a path."""
        respx.post(self.URL).mock(
            return_value=httpx.Response(200, json={"transcript": "ok"})
        )

        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "sarvamSpeechToText",
                {
                    "audio_file": {
                        "type": "upload",
                        "data": base64.b64encode(b"AUDIOBYTES").decode(),
                        "filename": "voice.mp3",
                        "mimeType": "audio/mpeg",
                    }
                },
            )

        harness.assert_envelope(result, success=True)
        assert b"AUDIOBYTES" in respx.calls.last.request.content

    @respx.mock
    async def test_relative_path_resolves_against_the_workspace(self, harness, tmp_path):
        (tmp_path / "audio").mkdir()
        (tmp_path / "audio" / "a.wav").write_bytes(b"WAV")
        respx.post(self.URL).mock(
            return_value=httpx.Response(200, json={"transcript": "ok"})
        )

        ctx = harness.build_context(workspace_dir=str(tmp_path))
        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "sarvamSpeechToText", {"audio_file": "audio/a.wav"}, context=ctx
            )

        harness.assert_envelope(result, success=True)

    async def test_missing_file_is_a_user_error(self, harness):
        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "sarvamSpeechToText", {"audio_file": "/nope/missing.wav"}
            )

        harness.assert_envelope(result, success=False)
        assert "not found" in result["error"].lower()

    async def test_empty_audio_file_param_is_a_user_error(self, harness):
        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute("sarvamSpeechToText", {})

        harness.assert_envelope(result, success=False)
        assert "audio_file is required" in result["error"]

    async def test_advanced_mode_requires_saaras_v3(self, harness, tmp_path):
        clip = tmp_path / "c.wav"
        clip.write_bytes(b"WAV")

        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "sarvamSpeechToText",
                {
                    "audio_file": str(clip),
                    "model": "saarika:v2.5",
                    "mode": "codemix",
                },
            )

        harness.assert_envelope(result, success=False)
        assert "saaras:v3" in result["error"]


# ============================================================================
# sarvamTextToSpeech
# ============================================================================


class TestSarvamTextToSpeech:
    URL = f"{_BASE}/text-to-speech"

    @staticmethod
    def _audio_response(chunks: int = 1) -> httpx.Response:
        blob = base64.b64encode(b"RIFFfakeWAVEdata").decode()
        return httpx.Response(
            200, json={"request_id": "r", "audios": [blob] * chunks}
        )

    @respx.mock
    async def test_writes_to_the_workspace_by_default(self, harness, tmp_path):
        respx.post(self.URL).mock(return_value=self._audio_response())

        ctx = harness.build_context(workspace_dir=str(tmp_path))
        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "sarvamTextToSpeech",
                {"text": "Namaste", "target_language_code": "hi-IN"},
                context=ctx,
            )

        harness.assert_envelope(result, success=True)
        payload = result["result"]
        written = Path(payload["file_path"])
        assert written.is_file()
        assert written.parent == tmp_path / "audio"
        assert written.read_bytes() == b"RIFFfakeWAVEdata"
        assert payload["chunk_count"] == 1
        assert payload["files"] == [str(written)]
        # The DB/WS bloat guard: no inline base64 unless asked for.
        assert payload.get("audio_base64") is None

    @respx.mock
    async def test_base64_mode_returns_inline_and_writes_nothing(self, harness, tmp_path):
        respx.post(self.URL).mock(return_value=self._audio_response())

        ctx = harness.build_context(workspace_dir=str(tmp_path))
        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "sarvamTextToSpeech",
                {"text": "Namaste", "return_audio": "base64"},
                context=ctx,
            )

        payload = result["result"]
        assert payload["audio_base64"] == base64.b64encode(b"RIFFfakeWAVEdata").decode()
        assert payload.get("file_path") is None
        assert not (tmp_path / "audio").exists()

    @respx.mock
    async def test_multi_chunk_writes_one_file_per_chunk(self, harness, tmp_path):
        respx.post(self.URL).mock(return_value=self._audio_response(chunks=3))

        ctx = harness.build_context(workspace_dir=str(tmp_path))
        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "sarvamTextToSpeech", {"text": "long text"}, context=ctx
            )

        payload = result["result"]
        assert payload["chunk_count"] == 3
        assert len(payload["files"]) == 3
        assert all(Path(p).is_file() for p in payload["files"])
        assert payload["file_path"] == payload["files"][0]
        assert "3 audio chunks" in payload["note"]

    @respx.mock
    async def test_oversize_inline_audio_is_suppressed_with_a_note(
        self, harness, tmp_path, monkeypatch
    ):
        """Never silently truncate — say why and point at the file mode."""
        monkeypatch.setattr(
            "nodes.sarvam.sarvam_text_to_speech._MAX_INLINE_B64", 4
        )
        respx.post(self.URL).mock(return_value=self._audio_response())

        ctx = harness.build_context(workspace_dir=str(tmp_path))
        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "sarvamTextToSpeech",
                {"text": "hi", "return_audio": "base64"},
                context=ctx,
            )

        payload = result["result"]
        assert payload["audio_base64"] is None
        assert "return_audio='file'" in payload["note"]

    @respx.mock
    async def test_v2_only_params_are_not_sent_for_v3(self, harness, tmp_path):
        import json

        respx.post(self.URL).mock(return_value=self._audio_response())

        ctx = harness.build_context(workspace_dir=str(tmp_path))
        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            await harness.execute(
                "sarvamTextToSpeech",
                {"text": "hi", "model": "bulbul:v3", "speaker": "shubh"},
                context=ctx,
            )

        body = json.loads(respx.calls.last.request.content)
        assert "pitch" not in body
        assert "loudness" not in body
        assert "enable_preprocessing" not in body

    @respx.mock
    async def test_v2_params_are_sent_for_v2(self, harness, tmp_path):
        import json

        respx.post(self.URL).mock(return_value=self._audio_response())

        ctx = harness.build_context(workspace_dir=str(tmp_path))
        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            await harness.execute(
                "sarvamTextToSpeech",
                {
                    "text": "hi",
                    "model": "bulbul:v2",
                    "speaker": "anushka",
                    "pitch": 0.2,
                },
                context=ctx,
            )

        body = json.loads(respx.calls.last.request.content)
        assert body["pitch"] == 0.2
        assert "temperature" not in body

    async def test_speaker_from_the_wrong_model_is_a_user_error(self, harness):
        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "sarvamTextToSpeech",
                {"text": "hi", "model": "bulbul:v3", "speaker": "anushka"},
            )

        harness.assert_envelope(result, success=False)
        assert "not a bulbul:v3 voice" in result["error"]

    async def test_text_over_model_cap_is_a_user_error(self, harness):
        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "sarvamTextToSpeech",
                {"text": "x" * 1501, "model": "bulbul:v2", "speaker": "anushka"},
            )

        harness.assert_envelope(result, success=False)
        assert "1500" in result["error"]

    async def test_v3_pace_out_of_range_is_a_user_error(self, harness):
        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "sarvamTextToSpeech", {"text": "hi", "pace": 2.5}
            )

        harness.assert_envelope(result, success=False)
        assert "0.5-2.0" in result["error"]

    @respx.mock
    async def test_empty_audio_array_is_a_user_error(self, harness, tmp_path):
        respx.post(self.URL).mock(return_value=httpx.Response(200, json={"audios": []}))

        ctx = harness.build_context(workspace_dir=str(tmp_path))
        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "sarvamTextToSpeech", {"text": "hi"}, context=ctx
            )

        harness.assert_envelope(result, success=False)
        assert "no audio" in result["error"].lower()


# ============================================================================
# Cross-node contracts
# ============================================================================


class TestSarvamPluginContract:
    NODE_TYPES = (
        "sarvamTranslate",
        "sarvamTransliterate",
        "sarvamDetectLanguage",
        "sarvamSpeechToText",
        "sarvamTextToSpeech",
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
