"""Contract for the provider-abstracted speech nodes.

The interesting tests here are the wire-shape ones. Every provider in the v1
set diverges from the others on auth, request transport and response
transport, and three of those divergences fail *silently* when got wrong:
ElevenLabs ignores a body-placed ``output_format`` and returns the default,
Deepgram ignores body-placed options entirely, and Sarvam accepts a request
whose base64 array is then mis-parsed. None of them produce an error, so
only an assertion on the outgoing request catches a regression.
"""

from __future__ import annotations

import base64
import struct
import subprocess
import sys
import wave
from pathlib import Path

import httpx
import pytest
import respx

from ._mocks import patched_container, patched_pricing

pytestmark = pytest.mark.node_contract

SERVER_DIR = Path(__file__).resolve().parents[2]

_KEYS = {
    "openai": "sk-test",
    "groq": "gsk-test",
    "elevenlabs": "el-test",
    "deepgram": "dg-test",
    "sarvam": "sv-test",
}


def _wav(seconds: float = 0.5, rate: int = 8000) -> bytes:
    import io

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(struct.pack("<h", 0) * int(rate * seconds))
    return buffer.getvalue()


def _clip(harness, tmp_path, *, payload: bytes | None = None, name: str = "a.wav"):
    """Put a clip inside the workspace and return ``(context, relative_name)``.

    Audio has to live *in* the workspace to be readable: containment refuses
    absolute paths outside it, which is the whole point of the design. Tests
    that hand the node a bare ``tmp_path`` are testing the guard, not the
    provider.
    """
    (tmp_path / name).write_bytes(payload if payload is not None else _wav())
    return harness.build_context(workspace_dir=str(tmp_path)), name


# ============================================================================
# Registry — direction membership IS the capability
# ============================================================================


class TestDirectionMembership:
    def test_transcription_only_provider_is_absent_from_synthesis(self):
        from nodes.speech._registry import tts_providers

        assert "deepgram" not in tts_providers()
        assert "groq" not in tts_providers()

    def test_synthesis_only_provider_is_absent_from_transcription(self):
        from nodes.speech._registry import stt_providers

        assert "elevenlabs" not in stt_providers()

    def test_dual_direction_provider_is_in_both(self):
        from nodes.speech._registry import stt_providers, tts_providers

        for provider in ("openai", "sarvam"):
            assert provider in tts_providers()
            assert provider in stt_providers()

    def test_every_provider_resolves_to_a_registered_credential(self):
        """The layering contract: config names credential *ids*, not classes."""
        from services.plugin.credential import CREDENTIAL_REGISTRY

        from nodes.speech import _config
        from nodes.speech._registry import stt_providers, tts_providers

        for provider in set(tts_providers()) | set(stt_providers()):
            credential = _config.credential_id(provider)
            assert credential in CREDENTIAL_REGISTRY, (
                f"{provider} maps to credential {credential!r}, which is not registered"
            )


# ============================================================================
# Config — the per-model override ladder
# ============================================================================


class TestCapabilityResolution:
    def test_exact_model_override_wins(self):
        from nodes.speech import _config

        assert "verbose_json" in _config.response_formats("openai", "whisper-1")
        assert _config.response_formats("openai", "gpt-4o-transcribe") == [
            "json",
            "text",
        ]

    def test_prefix_match_covers_dated_snapshots(self):
        """A dated release must not silently fall through to _default."""
        from nodes.speech import _config

        assert _config.response_formats(
            "openai", "gpt-4o-mini-transcribe-2025-12-15"
        ) == ["json", "text"]

    def test_declared_null_is_distinguishable_from_absent(self, monkeypatch):
        """"Declared as unknown" and "never configured" are different facts.

        Exercised against a synthetic block rather than a real provider, so
        the mechanism stays tested when a vendor publishes a limit it
        previously did not.
        """
        from nodes.speech import _config

        monkeypatch.setitem(
            _config.SPEECH_DEFAULTS["providers"],
            "_probe",
            {"stt": {"declared_null": None}},
        )
        assert _config.capability("_probe", "stt", "declared_null", default="ABSENT") is None
        assert _config.capability("_probe", "stt", "absent", default="ABSENT") == "ABSENT"

    def test_deepgram_limits_match_the_published_ones(self):
        """Both are documented, and the duration one bites in practice.

        An earlier revision asserted Deepgram published no cap at all. It
        does: 2 GB, and a 10-minute synchronous ceiling above which the API
        returns 504 rather than a transcript.
        """
        from nodes.speech import _config

        assert _config.capability("deepgram", "stt", "max_upload_bytes") == 2 * 1024**3
        assert _config.capability("deepgram", "stt", "max_duration_seconds") == 600

    def test_boolean_capabilities_default_permissive(self):
        from nodes.speech import _config

        assert _config.supports("openai", "tts", "never_declared_flag") is True

    def test_disjoint_voice_sets_per_model(self):
        from nodes.speech import _config

        v2 = set(_config.voices("sarvam", model="bulbul:v2"))
        v3 = set(_config.voices("sarvam", model="bulbul:v3"))
        assert v2 and v3 and not (v2 & v3)


# ============================================================================
# Wire shapes — the silent-failure guards
# ============================================================================


class TestElevenLabsWireShape:
    @respx.mock
    async def test_output_format_rides_the_query_string_not_the_body(
        self, harness, tmp_path
    ):
        """A body-placed output_format is ignored and the default returned."""
        route = respx.post(
            url__regex=r"https://api\.elevenlabs\.io/v1/text-to-speech/.*"
        ).mock(return_value=httpx.Response(200, content=b"ID3audio"))

        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "textToSpeech",
                {
                    "provider": "elevenlabs",
                    "text": "hello",
                    "voice": "voice123",
                    "output_format": "mp3_22050_32",
                },
                context=harness.build_context(workspace_dir=str(tmp_path)),
            )

        harness.assert_envelope(result, success=True)
        request = route.calls.last.request
        assert request.url.params["output_format"] == "mp3_22050_32"
        assert b"output_format" not in request.content

    @respx.mock
    async def test_voice_is_a_path_segment_and_auth_has_no_scheme(self, harness):
        route = respx.post(
            url__regex=r"https://api\.elevenlabs\.io/v1/text-to-speech/.*"
        ).mock(return_value=httpx.Response(200, content=b"audio"))

        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            await harness.execute(
                "textToSpeech",
                {"provider": "elevenlabs", "text": "hi", "voice": "abc123"},
            )

        request = route.calls.last.request
        assert request.url.path.endswith("/v1/text-to-speech/abc123")
        assert request.headers["xi-api-key"] == "el-test"
        assert "authorization" not in request.headers

    @respx.mock
    async def test_speed_is_clamped_to_the_api_range(self, harness):
        route = respx.post(
            url__regex=r"https://api\.elevenlabs\.io/v1/text-to-speech/.*"
        ).mock(return_value=httpx.Response(200, content=b"audio"))

        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            await harness.execute(
                "textToSpeech",
                {
                    "provider": "elevenlabs",
                    "text": "hi",
                    "voice": "v",
                    "speed": 9.0,
                },
            )

        import json as _json

        body = _json.loads(route.calls.last.request.content)
        assert body["voice_settings"]["speed"] == 2.0

    async def test_missing_voice_is_a_user_error(self, harness):
        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "textToSpeech", {"provider": "elevenlabs", "text": "hi"}
            )

        harness.assert_envelope(result, success=False)
        assert "voice" in result["error"].lower()


class TestDeepgramWireShape:
    URL = "https://api.deepgram.com/v1/listen"

    def _response(self, transcript: str = "hello there", duration: float = 12.0):
        return httpx.Response(
            200,
            json={
                "metadata": {"request_id": "dg-1", "duration": duration},
                "results": {
                    "channels": [
                        {
                            "detected_language": "en",
                            "language_confidence": 0.99,
                            "alternatives": [
                                {
                                    "transcript": transcript,
                                    "words": [
                                        {
                                            "word": "hello",
                                            "start": 0.1,
                                            "end": 0.4,
                                            "confidence": 0.9,
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                },
            },
        )

    @respx.mock
    async def test_auth_uses_token_not_bearer(self, harness, tmp_path):
        ctx, name = _clip(harness, tmp_path)
        route = respx.post(self.URL).mock(return_value=self._response())

        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            await harness.execute(
                "speechToText",
                {"provider": "deepgram", "audio_file": name},
                context=ctx,
            )

        assert route.calls.last.request.headers["authorization"] == "Token dg-test"

    @respx.mock
    async def test_options_are_query_params_and_audio_is_the_raw_body(
        self, harness, tmp_path
    ):
        payload = _wav()
        ctx, name = _clip(harness, tmp_path, payload=payload)
        route = respx.post(self.URL).mock(return_value=self._response())

        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            await harness.execute(
                "speechToText",
                {
                    "provider": "deepgram",
                    "audio_file": name,
                    "stt_model": "nova-3",
                    "diarize": True,
                },
                context=ctx,
            )

        request = route.calls.last.request
        assert request.url.params["model"] == "nova-3"
        assert request.url.params["diarize"] == "true"
        # Raw body, not multipart -- no boundary, and the bytes are verbatim.
        assert not request.headers["content-type"].startswith("multipart/")
        assert request.content == payload

    @respx.mock
    async def test_transcript_is_read_from_the_nested_path(self, harness, tmp_path):
        ctx, name = _clip(harness, tmp_path)
        respx.post(self.URL).mock(return_value=self._response("the actual transcript"))

        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "speechToText",
                {"provider": "deepgram", "audio_file": name},
                context=ctx,
            )

        harness.assert_envelope(result, success=True)
        assert result["result"]["transcript"] == "the actual transcript"
        assert result["result"]["language"] == "en"
        assert result["result"]["words"][0]["word"] == "hello"


class TestSarvamWireShape:
    URL = "https://api.sarvam.ai/text-to-speech"

    @respx.mock
    async def test_base64_array_becomes_one_file_per_chunk(self, harness, tmp_path):
        clip = base64.b64encode(_wav()).decode()
        respx.post(self.URL).mock(
            return_value=httpx.Response(
                200, json={"request_id": "sv-1", "audios": [clip, clip, clip]}
            )
        )

        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "textToSpeech",
                {"provider": "sarvam", "text": "namaste"},
                context=harness.build_context(workspace_dir=str(tmp_path)),
            )

        harness.assert_envelope(result, success=True)
        payload = result["result"]
        assert payload["chunk_count"] == 3
        assert len(payload["files"]) == 3
        # Distinct paths -- concatenating them would be wrong, so they must
        # not collide either.
        assert len({f["path"] for f in payload["files"]}) == 3
        assert "3 clips" in (payload["note"] or "")

    @respx.mock
    async def test_v2_only_params_are_not_sent_to_v3(self, harness):
        respx.post(self.URL).mock(
            return_value=httpx.Response(
                200, json={"audios": [base64.b64encode(_wav()).decode()]}
            )
        )

        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            await harness.execute(
                "textToSpeech",
                {
                    "provider": "sarvam",
                    "text": "hi",
                    "tts_model": "bulbul:v3",
                    "provider_options": {"pitch": 0.5, "temperature": 0.7},
                },
            )

        import json as _json

        body = _json.loads(respx.calls.last.request.content)
        assert "pitch" not in body
        assert body["temperature"] == 0.7

    @respx.mock
    async def test_auth_uses_the_native_subscription_header(self, harness):
        respx.post(self.URL).mock(
            return_value=httpx.Response(
                200, json={"audios": [base64.b64encode(_wav()).decode()]}
            )
        )

        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            await harness.execute("textToSpeech", {"provider": "sarvam", "text": "hi"})

        assert respx.calls.last.request.headers["api-subscription-key"] == "sv-test"

    async def test_over_cap_text_is_refused_before_the_call(self, harness):
        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "textToSpeech",
                {"provider": "sarvam", "text": "x" * 3000, "tts_model": "bulbul:v3"},
            )

        harness.assert_envelope(result, success=False)
        assert "2500" in result["error"]


class TestOpenAIWireShape:
    @respx.mock
    async def test_synthesis_returns_raw_bytes_and_writes_one_ref(
        self, harness, tmp_path
    ):
        respx.post("https://api.openai.com/v1/audio/speech").mock(
            return_value=httpx.Response(200, content=b"ID3rawaudio")
        )

        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "textToSpeech",
                {"provider": "openai", "text": "hello"},
                context=harness.build_context(workspace_dir=str(tmp_path)),
            )

        harness.assert_envelope(result, success=True)
        audio = result["result"]["audio"]
        assert audio["kind"] == "audio"
        assert audio["path"].startswith("audio/")
        assert audio["size_bytes"] == len(b"ID3rawaudio")

    @respx.mock
    async def test_response_format_downgrades_on_a_gated_model(self, harness, tmp_path):
        """verbose_json on a 4o model is a 400; the node must not send it."""
        ctx, name = _clip(harness, tmp_path)
        route = respx.post("https://api.openai.com/v1/audio/transcriptions").mock(
            return_value=httpx.Response(200, json={"text": "hi"})
        )

        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            await harness.execute(
                "speechToText",
                {
                    "provider": "openai",
                    "audio_file": name,
                    "stt_model": "gpt-4o-transcribe",
                    "timestamps": True,
                },
                context=ctx,
            )

        body = route.calls.last.request.content
        assert b"verbose_json" not in body
        assert b"timestamp_granularities" not in body

    @respx.mock
    async def test_groq_transcription_hits_its_own_host(self, harness, tmp_path):
        ctx, name = _clip(harness, tmp_path)
        route = respx.post(
            "https://api.groq.com/openai/v1/audio/transcriptions"
        ).mock(return_value=httpx.Response(200, json={"text": "hi", "duration": 3.0}))

        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "speechToText",
                {"provider": "groq", "audio_file": name},
                context=ctx,
            )

        harness.assert_envelope(result, success=True)
        assert route.called
        assert route.calls.last.request.headers["authorization"] == "Bearer gsk-test"


# ============================================================================
# Node-level contract
# ============================================================================


class TestNodeContract:
    def test_neither_node_names_a_field_model_or_api_key(self):
        """Regression guard for the parameter-panel clobber.

        ParameterRenderer overwrites any field literally named ``model`` or
        ``api_key`` with chat-model data whenever a sibling ``provider``
        field exists, and it never checks that the provider is an LLM
        provider. A field with either name here would be cleared the moment
        a user picked a speech provider.
        """
        from nodes.speech.speech_to_text import SpeechToTextParams
        from nodes.speech.text_to_speech import TextToSpeechParams

        for params in (TextToSpeechParams, SpeechToTextParams):
            assert "model" not in params.model_fields
            assert "api_key" not in params.model_fields

    def test_neither_node_uses_declarative_routing(self):
        """``routing=`` resolves credentials[0], which ignores `provider`."""
        from nodes.speech.speech_to_text import SpeechToTextNode
        from nodes.speech.text_to_speech import TextToSpeechNode

        for node in (TextToSpeechNode, SpeechToTextNode):
            for spec in node._operations.values():
                assert spec.routing is None, (
                    f"{node.type}.{spec.name} declares routing=, which would pin "
                    "every provider to the first credential in the tuple"
                )

    def test_canvas_handles_stay_visible(self):
        """`usable_as_tool` auto-hides both handles unless declared False."""
        from nodes.speech.speech_to_text import SpeechToTextNode
        from nodes.speech.text_to_speech import TextToSpeechNode

        for node in (TextToSpeechNode, SpeechToTextNode):
            assert node.hide_input_handle is False
            assert node.hide_output_handle is False

    def test_declared_credentials_cover_every_registered_provider(self):
        from nodes.speech import _config
        from nodes.speech._registry import stt_providers, tts_providers
        from nodes.speech.speech_to_text import SpeechToTextNode
        from nodes.speech.text_to_speech import TextToSpeechNode

        for node, providers in (
            (TextToSpeechNode, tts_providers()),
            (SpeechToTextNode, stt_providers()),
        ):
            declared = {c.id for c in node.credentials}
            for provider in providers:
                assert _config.credential_id(provider) in declared, (
                    f"{node.type} cannot authenticate {provider}"
                )

    async def test_unknown_provider_is_a_user_error_listing_the_real_ones(
        self, harness
    ):
        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "textToSpeech", {"provider": "nope", "text": "hi"}
            )

        harness.assert_envelope(result, success=False)
        assert "elevenlabs" in result["error"]


class TestPanelBlankCoercion:
    """The parameter panel stores "" for any field the user cleared.

    Against `str` that is harmless, but against `Optional[float]`, `bool` or
    `Dict[str, Any]` it is a hard validation error — which is how this first
    surfaced, as "Invalid parameters: Input should be a valid dictionary" on
    every run of a freshly-dropped node. Six fields across the two nodes are
    affected, so the fix is general rather than per-field.
    """

    @pytest.mark.parametrize(
        "params_cls,payload",
        [
            (
                "tts",
                {"text": "hi", "speed": "", "sample_rate": "", "provider_options": ""},
            ),
            (
                "stt",
                {
                    "audio_file": "a.wav",
                    "translate": "",
                    "diarize": "",
                    "timestamps": "",
                    "provider_options": "",
                },
            ),
        ],
    )
    def test_blank_strings_fall_back_to_defaults(self, params_cls, payload):
        from nodes.speech.speech_to_text import SpeechToTextParams
        from nodes.speech.text_to_speech import TextToSpeechParams

        cls = TextToSpeechParams if params_cls == "tts" else SpeechToTextParams
        model = cls(**payload)
        assert model.provider_options == {}

    def test_json_object_string_is_parsed(self):
        """The panel has no object widget, so this arrives as typed text."""
        from nodes.speech.text_to_speech import TextToSpeechParams

        model = TextToSpeechParams(
            text="hi", provider_options='{"instructions": "cheerful"}'
        )
        assert model.provider_options == {"instructions": "cheerful"}

    def test_a_real_dict_passes_through(self):
        from nodes.speech.text_to_speech import TextToSpeechParams

        assert TextToSpeechParams(
            text="hi", provider_options={"pitch": 0.2}
        ).provider_options == {"pitch": 0.2}

    @pytest.mark.parametrize("bad", ["[1, 2]", "not json at all", '"a string"'])
    def test_non_object_json_is_rejected_with_a_usable_message(self, bad):
        from pydantic import ValidationError

        from nodes.speech.text_to_speech import TextToSpeechParams

        with pytest.raises(ValidationError, match="JSON object"):
            TextToSpeechParams(text="hi", provider_options=bad)

    def test_string_fields_are_untouched(self):
        """Only fields that CANNOT hold a string get the blank treatment.

        Dropping a blank `text` would turn a min_length error into a
        confusing "field required".
        """
        from pydantic import ValidationError

        from nodes.speech.text_to_speech import TextToSpeechParams

        with pytest.raises(ValidationError, match="at least 1 character"):
            TextToSpeechParams(text="")

    def test_a_dropped_node_validates_with_its_own_defaults(self):
        """Regression for the reported failure: drop node, hit Run."""
        from services.node_registry import get_node_class

        defaults = {
            name: field.default
            for name, field in get_node_class("textToSpeech").Params.model_fields.items()
        }
        defaults["text"] = "hello"
        # PydanticUndefined for default_factory fields is what the panel would
        # store as "" — simulate that rather than the sentinel.
        payload = {
            k: ("" if repr(v).startswith("PydanticUndefined") else v)
            for k, v in defaults.items()
        }
        get_node_class("textToSpeech").Params(**payload)


class TestAudioInputContainment:
    """The Sarvam STT node read the credential store. This must not."""

    @pytest.mark.parametrize(
        "attack", ["../../credentials.db", "..\\..\\credentials.db", "~/secrets"]
    )
    async def test_traversal_is_refused(self, harness, tmp_path, attack):
        (tmp_path.parent / "credentials.db").write_bytes(b"SECRET")
        ctx = harness.build_context(workspace_dir=str(tmp_path))

        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "speechToText",
                {"provider": "openai", "audio_file": attack},
                context=ctx,
            )

        harness.assert_envelope(result, success=False)

    async def test_missing_file_is_a_user_error(self, harness):
        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "speechToText",
                {"provider": "openai", "audio_file": "definitely/absent.wav"},
            )

        harness.assert_envelope(result, success=False)


class TestBilling:
    @respx.mock
    async def test_duration_is_measured_not_assumed(self, harness, tmp_path):
        """The old Sarvam node billed every clip as a flat 30 seconds."""
        ctx, name = _clip(harness, tmp_path, payload=_wav(seconds=2.0, rate=8000))
        respx.post("https://api.sarvam.ai/speech-to-text").mock(
            return_value=httpx.Response(200, json={"transcript": "ok"})
        )

        with patched_container(auth_api_keys=_KEYS), patched_pricing():
            result = await harness.execute(
                "speechToText",
                {"provider": "sarvam", "audio_file": name},
                context=ctx,
            )

        harness.assert_envelope(result, success=True)
        duration = result["result"]["duration_seconds"]
        assert duration == pytest.approx(2.0, abs=0.1)
        assert duration != 30


# ============================================================================
# Boot-path purity
# ============================================================================


class TestLazySdkImports:
    """Registering speech providers must not import a heavy SDK.

    Ported from ``tests/llm/test_lazy_sdk_imports.py``. Runs in a clean
    interpreter because the pytest process already has SDKs loaded by other
    tests. This is the only thing preventing a future provider from
    reintroducing the eager-import-at-boot anti-pattern.
    """

    HEAVY = ("openai", "anthropic", "google.genai")

    def _probe(self, code: str) -> str:
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=SERVER_DIR,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, (
            f"probe subprocess failed (rc={result.returncode}):\n{result.stderr}"
        )
        return result.stdout

    def test_registering_providers_imports_no_sdk(self):
        out = self._probe(
            "import sys\n"
            "import nodes.speech\n"
            f"leaked = [m for m in {self.HEAVY!r} if m in sys.modules]\n"
            "print('LEAKED=' + ','.join(leaked))\n"
        )
        assert out.strip().endswith("LEAKED="), (
            f"speech provider registration imported heavy SDKs: {out!r}"
        )

    def test_every_declared_exception_ref_resolves(self):
        """Typo guard — a bad ref only fails when an error occurs otherwise."""
        import nodes.speech  # noqa: F401

        from nodes.speech._registry import (
            get_stt_provider,
            get_tts_provider,
            stt_providers,
            tts_providers,
        )

        for name in tts_providers():
            assert get_tts_provider(name).sdk_exception_types
        for name in stt_providers():
            assert get_stt_provider(name).sdk_exception_types
