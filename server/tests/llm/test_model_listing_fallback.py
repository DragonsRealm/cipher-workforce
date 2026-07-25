"""Contract for the JSON-driven ``supports_model_listing`` flag.

A provider can speak the OpenAI wire format for chat completions and still
ship no model-list route. Sarvam is the first: calling ``models.list()``
against it 404s, and because that 404 surfaces as an ``openai.OpenAIError``
the unifier turns it into ``NodeUserError`` and ``AIService.fetch_models``
re-raises before reaching its curated fallback — so credential validation
and the model dropdown would both fail for a perfectly valid key.

``OpenAIProvider.fetch_models`` therefore reads
``providers.<name>.supports_model_listing`` from llm_defaults.json. These
tests pin both halves of that contract: the opt-out behaves, and — the part
that actually protects the other twelve providers — nobody else takes the
branch.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import services.llm  # noqa: F401 — side-effect import populates the registry
from services.llm.config import (
    LLM_DEFAULTS,
    curated_models,
    supports_model_listing,
)
from services.llm.providers.openai import OpenAIProvider
from services.llm.registry import all_providers


def _provider_with_mock_client(provider_name: str) -> OpenAIProvider:
    with patch("openai.AsyncOpenAI"):
        provider = OpenAIProvider("key", provider_name=provider_name)
    provider._client = MagicMock(name="AsyncOpenAI")
    provider._client.models.list = AsyncMock()
    provider._client.chat.completions.create = AsyncMock()
    return provider


class TestFlagDefaults:
    def test_absent_key_means_listing_is_supported(self):
        assert supports_model_listing("provider-that-does-not-exist") is True

    def test_sarvam_is_the_only_opt_out(self):
        """Guards the other twelve providers against a behaviour change.

        If this ever fails, some provider gained the flag — confirm that
        was deliberate before updating the expectation.
        """
        opted_out = {p for p in all_providers() if not supports_model_listing(p)}
        assert opted_out == {"sarvam"}


class TestListingSupported:
    """The default path is untouched: hit the real models endpoint."""

    @pytest.mark.parametrize(
        "provider_name",
        sorted(p for p in all_providers() if supports_model_listing(p)),
    )
    async def test_calls_models_list_and_sorts(self, provider_name):
        provider = _provider_with_mock_client(provider_name)
        provider._client.models.list.return_value = SimpleNamespace(
            data=[SimpleNamespace(id="z-model"), SimpleNamespace(id="a-model")]
        )

        models = await provider.fetch_models("key")

        assert models == ["a-model", "z-model"]
        provider._client.models.list.assert_awaited_once()
        provider._client.chat.completions.create.assert_not_awaited()


class TestListingUnsupported:
    """The opt-out path: curated list + a one-token key probe."""

    async def test_returns_curated_list_without_listing_models(self):
        provider = _provider_with_mock_client("sarvam")

        models = await provider.fetch_models("key")

        assert models == curated_models("sarvam")
        assert models == ["sarvam-105b", "sarvam-30b"]
        provider._client.models.list.assert_not_awaited()

    async def test_probes_the_key_with_a_one_token_completion(self):
        provider = _provider_with_mock_client("sarvam")

        await provider.fetch_models("key")

        provider._client.chat.completions.create.assert_awaited_once()
        kwargs = provider._client.chat.completions.create.await_args.kwargs
        assert kwargs["max_tokens"] == 1
        assert kwargs["model"] == "sarvam-105b"

    async def test_probe_failure_propagates_as_the_typed_sdk_error(self):
        """An invalid key must still fail validation, not silently pass."""
        import openai

        provider = _provider_with_mock_client("sarvam")
        provider._client.chat.completions.create.side_effect = (
            openai.AuthenticationError(
                "bad key",
                response=MagicMock(status_code=401, headers={}),
                body=None,
            )
        )

        with pytest.raises(openai.OpenAIError):
            await provider.fetch_models("key")

    async def test_empty_curated_list_fails_loudly(self, monkeypatch):
        """A misedited JSON block must not hand the modal an empty dropdown."""
        blocks = dict(LLM_DEFAULTS["providers"])
        blocks["sarvam"] = {
            **blocks["sarvam"],
            "popular_models": [],
            "max_output_tokens": {},
        }
        monkeypatch.setitem(LLM_DEFAULTS, "providers", blocks)

        provider = _provider_with_mock_client("sarvam")
        with pytest.raises(ValueError, match="declares no models"):
            await provider.fetch_models("key")


class TestCuratedModels:
    def test_prefers_popular_models_when_present(self):
        assert curated_models("openai") == LLM_DEFAULTS["providers"]["openai"]["popular_models"]

    def test_falls_back_to_max_output_token_keys(self):
        """Sarvam carries an empty popular_models under the >=1M policy."""
        assert LLM_DEFAULTS["providers"]["sarvam"]["popular_models"] == []
        assert curated_models("sarvam") == ["sarvam-105b", "sarvam-30b"]
        assert "_default" not in curated_models("sarvam")

    def test_unknown_provider_is_empty(self):
        assert curated_models("nope") == []

    def test_ai_service_delegates_to_the_shared_helper(self):
        """Both callers must serve the same list or the dropdown drifts."""
        from services.ai import AIService

        service = AIService.__new__(AIService)
        for provider in ("openai", "sarvam", "mistral"):
            assert service._get_curated_models(provider) == curated_models(provider)
