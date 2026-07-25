# Sarvam AI Service

Indic-first AI: two chat models plus five REST APIs for translation,
transliteration, language identification, speech-to-text and text-to-speech.

Sarvam is unusual in this codebase because **one API key spans two very
different auth styles**. Its chat endpoint is OpenAI-compatible and accepts
`Authorization: Bearer`; every other endpoint accepts only
`api-subscription-key`. Both are served by a single stored credential.

| | |
|---|---|
| **Provider id** | `sarvam` |
| **Base URL** | `https://api.sarvam.ai` (chat at `/v1`) |
| **Auth** | `api-subscription-key: <key>`; the chat route also accepts `Authorization: Bearer <key>` |
| **Credential class** | [`SarvamCredential`](../server/nodes/model/_credentials.py) (`_LLMApiKey` → `ApiKeyCredential`) |
| **Chat plugin** | [`server/nodes/model/sarvam_chat_model/`](../server/nodes/model/sarvam_chat_model/) |
| **Service plugins** | [`server/nodes/sarvam/`](../server/nodes/sarvam/) |
| **Palette groups** | `model` (chat), `language` (services) |
| **Official docs** | <https://docs.sarvam.ai> |

---

## Chat models — an OpenAI-compat provider

`sarvamChatModel` is a plain `ChatModelBase` subclass. The provider itself is
registered declaratively in
[`_compat.py`](../server/services/llm/providers/_compat.py) — one entry in
`_COMPAT_PROVIDERS`, `factory=OpenAIProvider`, `base_url` pinned from
`llm_defaults.json`. No `SarvamProvider` class exists and none is needed.

| Model | Context | `max_output_tokens` in JSON | Notes |
|---|---|---|---|
| `sarvam-105b` | 131072 | 65536 | Flagship, `default_model` |
| `sarvam-30b` | 65536 | 32768 | Cheaper tier |

`sarvam-m` is deprecated and removed from the API; it is deliberately absent.

**`max_output_tokens` is half the context window, not Sarvam's documented
ceiling.** Sarvam publishes per-tier output caps (Business: 128000 for the
105b, 64000 for the 30b), but those are ~98% of the context window, and
`resolve_max_tokens` uses this value as *both* the default budget when a node
leaves `max_tokens` unset *and* the clamp ceiling — a request at the documented
ceiling could not fit any prompt. Half the window keeps paid tiers unclamped
and usable. The tier table is recorded in `_max_output_note`.

### Reasoning is entirely JSON-driven

Sarvam runs reasoning **on by default** at `reasoning_effort: "medium"` and
returns the trace in `message.reasoning_content`. Both halves are already
handled generically:

- `thinking_type: "effort"` → [`openai.py:99-100`](../server/services/llm/providers/openai.py) sets `params["reasoning_effort"]`
- `reasoning_content` → [`openai.py:315-322`](../server/services/llm/providers/openai.py) lifts it into a `reasoning` `ContentBlock`, surfacing as `thinking`

Two keys are **deliberately omitted** from the JSON block, and re-adding either
is a bug:

| Key | Why it must stay out |
|---|---|
| `thinking_default_on` | Emits Moonshot's proprietary `extra_body.thinking = {"type": "disabled"}`. Sarvam documents no way to disable reasoning and never defined that field. |
| `reasoning_models` | Would set `temperature_allowed = False` and pin `temperature` to 1.0. Sarvam accepts temperature alongside reasoning (its own default is 0.5 with reasoning on, 0.2 without). |

With both omitted the behaviour is right on **both** toggle states: thinking
off sends no `reasoning_effort` and Sarvam applies its own default; thinking on
sends ours.

### No model-list endpoint — the `supports_model_listing` flag

**Sarvam ships no `/v1/models` route.** Verified against
<https://docs.sarvam.ai/openapi.json>: 25 paths, none for model listing.

This matters more than it sounds. `OpenAIProvider.fetch_models` calls
`client.models.list()`; the resulting 404 arrives as an `openai.OpenAIError`,
which `ChatUnifier.fetch_models` converts to `NodeUserError`, which
`AIService.fetch_models` **re-raises before reaching its curated fallback**
([`ai.py:1232`](../server/services/ai.py)). Unhandled, a perfectly valid Sarvam
key would fail validation in the Credentials modal *and* leave the model
dropdown empty.

The fix is a generic JSON flag rather than a Sarvam-shaped subclass:

```json
"supports_model_listing": false
```

[`OpenAIProvider.fetch_models`](../server/services/llm/providers/openai.py)
reads it via `services.llm.config.supports_model_listing(provider)` and, when
false, serves `curated_models(provider)` and validates the key with a
one-token chat completion instead. An invalid key still raises the typed SDK
error the unifier knows how to translate, so it remains a real credential
check.

The flag **defaults to `true`**, so the other twelve providers are untouched —
`tests/llm/test_model_listing_fallback.py` asserts exactly that, and that
Sarvam is the only opt-out.

`popular_models` is `[]` per the repo's ≥1M-context policy (Sarvam tops out at
131072), so `curated_models` falls through to the `max_output_tokens` keys.
Both the per-node dropdown and the global model selector still list both
models.

---

## Service nodes — `server/nodes/sarvam/`

Five stateless REST nodes, palette group `language`, all
`usable_as_tool = True` and `TaskQueue.REST_API`. There is no `_service.py`:
nothing here owns long-lived state, so the folder is helpers plus node files.

| Node type | Tool name | Endpoint | Billing unit |
|---|---|---|---|
| `sarvamTranslate` | `sarvam_translate` | `POST /translate` | characters |
| `sarvamTransliterate` | `sarvam_transliterate` | `POST /transliterate` | characters |
| `sarvamDetectLanguage` | `sarvam_detect_language` | `POST /text-lid` | characters |
| `sarvamSpeechToText` | `sarvam_speech_to_text` | `POST /speech-to-text` (multipart) | seconds |
| `sarvamTextToSpeech` | `sarvam_text_to_speech` | `POST /text-to-speech` | characters |

All authenticate through `ctx.connection("sarvam")`, which injects
`api-subscription-key` via `ApiKeyCredential.inject` — no per-node auth code.

### Translate vs transliterate

Routinely confused, and the skill spells it out for the LLM: translate changes
the **language** ("Hello" → "नमस्ते"); transliterate changes the **script**
while preserving the words ("namaste" → "नमस्ते"). The nodes reject an
identical source/target pair rather than burning a paid call on a no-op.

Per-model input caps are enforced client-side (`mayura:v1` 1000 chars,
`sarvam-translate:v1` 2000, `/text-lid` 1000) so the LLM gets an actionable
message instead of an opaque 422.

### Speech-to-text: multipart and the 30-second wall

This is the repo's first multipart upload. Rather than bypass the authed
facade, `Connection.request` gained a generic `files=` parameter
([`connection.py`](../server/services/plugin/connection.py)) — threaded into
**both** the initial request and the auth-retry rebuild, so a 401 replays the
file parts instead of an empty body. Pass file *bytes*, never an open handle,
for exactly that reason.

`audio_file` accepts **either** shape the frontend can produce: a path string
(absolute, or relative to the workflow workspace) or the file widget's
`{"type": "upload", "data": "<base64>", "filename", "mimeType"}` envelope.
Typing it as `Union[str, Dict]` is deliberate — `whatsapp_send` declares the
same field as `str` and only survives because its handler reads the raw dict.

**The synchronous endpoint caps at 30 seconds of audio.** Longer recordings
need Sarvam's Batch API, a separate job-based surface that is out of scope.
Diarization is Batch-only too, so `diarized_transcript` is declared on the
output model but will be `None` here.

### Text-to-speech: files, not base64

Sarvam returns audio as base64 inside the JSON body. A 2500-character v3
request is roughly **12 MB of base64** — which would land in the
`node_outputs` JSON column, ride a status-WebSocket broadcast, and (because the
node is tool-exposed) be serialized into an LLM message.

So the default is `return_audio: "file"`: audio is written under
`<workspace>/audio/` and the node returns `file_path`. `return_audio: "base64"`
is opt-in and capped at `_MAX_INLINE_B64 = 1_000_000`; over the cap
`audio_base64` stays `None` and `note` explains why and points at the file
mode. It never silently truncates.

Sarvam may split long text into several chunks in `audios[]`. Each chunk is a
standalone container with its own header, so **concatenating them byte-wise
produces an unplayable file**. The node writes one file per chunk, points
`file_path` at the first, lists all of them in `files`, and sets `note`.

Voices are per model and do not overlap — 37 for `bulbul:v3`, 7 for
`bulbul:v2`. Asking for a v2 voice on v3 is a `NodeUserError`, not a silent
fallback. v2-only params (`pitch`, `loudness`, `enable_preprocessing`) and
v3-only params (`temperature`, `dict_id`) are gated by `displayOptions` in the
UI *and* filtered from the request body, since v3 rejects the v2 fields.

---

## Cost tracking

`@Operation(cost=...)` is declarative metadata that nothing reads at runtime,
so attribution is an explicit `track_sarvam_usage()` call per operation —
the same shape as [`nodes/twitter/_base.py`](../server/nodes/twitter/_base.py).
It never raises: a metrics failure must not fail a successful API call.

Sarvam publishes INR list prices. [`pricing.json`](../server/config/pricing.json)
stores USD converted at **1 INR = 0.0113 USD (~88.5 INR/USD, 2026-07)**, with
the rate and the source figures recorded in a `_note` key. Both an
`api.sarvam` block and an `operation_map.sarvam` block are required —
`calculate_api_cost` returns 0 without the latter.

Token pricing for the chat models lives separately under `llm.sarvam`.

---

## Icons

`@lobehub/icons` has no Sarvam brand (278 entries, none matching), so the
usual `lobehub:<brand>` route in `visuals.json` is unavailable. Instead the
mark ships as SVG and is served by the backend:

- `server/nodes/model/sarvam_chat_model/icon.svg` and `server/nodes/sarvam/icon.svg` → `GET /api/schemas/nodes/<type>/icon`
- `server/credentials/icons/sarvam.svg` → `GET /api/schemas/credentials/sarvam/icon`

The frontend's `/api/` branch in
[`assets/icons/index.ts`](../client/src/assets/icons/index.ts) already resolves
that wire format, so no icon-registry edit was needed — only an `<img>` wrapper
in `AIProviderIcons.tsx` for the direct-FC consumers. Skills inherit the same
icons automatically: `_parse_skill_metadata` tries `get_plugin_icon_path`
before `visuals.json`.

### The mark itself

The three SVGs carry Sarvam's **official mandala mark**, taken verbatim from
their published brand asset
(`https://assets.sarvam.ai/assets/brand/logos/sarvam-logo-black.svg`) — the
same geometry they ship as `sarvam.ai/favicon.svg`. Path data was extracted
programmatically, not redrawn.

Sarvam is a **monochrome-logo brand**: the mark is pure black or pure white,
with no coloured variant. That is a problem here, because the frontend
requests `/api/schemas/nodes/<type>/icon` with **no `?variant=` parameter**
(the backend supports `light` / `dark` and `icon.dark.svg`, but nothing calls
it). A single monochrome file would therefore be invisible on roughly half of
the twelve themes.

So the mark is composed onto a rounded tile in Sarvam's primary navy
`#1E2033`, drawn in white — the same "avatar" treatment `@lobehub/icons` uses
for OpenAI, Groq, OpenRouter, Ollama and LM Studio, five of which this repo
already renders. That gives guaranteed contrast on every theme from one file,
with no reliance on `prefers-color-scheme` (which tracks the OS, not the app's
`data-theme`).

If the frontend ever starts requesting `?variant=`, dropping Sarvam's white
logo in as `icon.dark.svg` and the navy one as `icon.svg` would be the more
faithful treatment.

### Colours

| Token | Value | Source |
|---|---|---|
| Icon tile | `#1E2033` | Sarvam's primary navy — their body text colour and hero gradient base |
| Icon mark | `#FFFFFF` | The official white logo |
| Node accent / palette group / catalogue | `#6A88E2` | Sarvam's interactive accent (their `#4250D5` → `#6A88E2` control gradient) |

The navy is deliberately *not* reused as the node accent: `--node-color`
drives the canvas node's border, and `#1E2033` is darker than the dark-theme
canvas background, so the border would disappear. The accent blue reads on
both light and dark surfaces.

---

## Registration checklist

Everything Sarvam touches, for reference when adding the next provider:

| File | What |
|---|---|
| [`llm_defaults.json`](../server/config/llm_defaults.json) | `providers.sarvam` block (`base_url` required, `supports_model_listing: false`) |
| [`_compat.py`](../server/services/llm/providers/_compat.py) | `"sarvam"` in `_COMPAT_PROVIDERS` |
| [`llm/config.py`](../server/services/llm/config.py) | `curated_models()` / `supports_model_listing()` helpers |
| [`llm/providers/openai.py`](../server/services/llm/providers/openai.py) | generic no-listing branch in `fetch_models` |
| [`constants.py`](../server/constants.py) | `AI_CHAT_MODEL_TYPES` + a `detect_ai_provider` branch — **without the latter the node silently calls api.openai.com** |
| [`llm/protocol.py`](../server/services/llm/protocol.py) | `provider_names["sarvam"]` for user-facing errors |
| [`model_registry.py`](../server/services/model_registry.py) | `DEFAULT_TEMP_RANGES["sarvam"]` |
| [`node_output_schemas.py`](../server/services/node_output_schemas.py) | `_CHAT_MODEL_TYPES` |
| [`nodes/model/_credentials.py`](../server/nodes/model/_credentials.py) | `SarvamCredential` |
| [`credential_providers.json`](../server/config/credential_providers.json) | catalogue entry (`extends: "_ai_base"`) |
| [`node_allowlist.json`](../server/config/node_allowlist.json) | `sarvamChatModel` in `enabled_nodes` (normal-mode visible) |
| [`groups.py`](../server/nodes/groups.py) | `language` palette group |
| [`pricing.json`](../server/config/pricing.json) | `llm.sarvam` + `api.sarvam` + `operation_map.sarvam` |
| 3× agent `Literal` | [ai_agent](../server/nodes/agent/ai_agent/__init__.py), [chat_agent](../server/nodes/agent/chat_agent/__init__.py), [_specialized](../server/nodes/agent/_specialized.py) — `test_plugin_shape.py` asserts **exact set equality** with the registry; miss one and CI fails |
| Frontend | [`aiModelProviders.ts`](../client/src/lib/aiModelProviders.ts), [`AIProviderIcons.tsx`](../client/src/components/icons/AIProviderIcons.tsx) |

## Tests

| File | Locks |
|---|---|
| [`tests/llm/test_model_listing_fallback.py`](../server/tests/llm/test_model_listing_fallback.py) | The `supports_model_listing` contract both ways, incl. that Sarvam is the only opt-out and every other provider still calls `models.list()` |
| [`tests/nodes/test_sarvam.py`](../server/tests/nodes/test_sarvam.py) | All five nodes: happy paths, `None`-stripping, per-model caps, v2/v3 param gating, TTS file-vs-base64 and multi-chunk, STT dual-shaped input |
| [`tests/services/test_connection_multipart.py`](../server/tests/services/test_connection_multipart.py) | `Connection.request(files=)` incl. the auth-retry replay |
| `tests/llm/test_provider_self_registration.py` | Sarvam in the compat matrix + its pinned `base_url` |
| `tests/llm/test_live_providers.py` | Opt-in live smoke, gated on `SARVAM_API_KEY` |

## Known limits

- Speech-to-text: 30-second clips only (Batch API not wired); no diarization.
- No streaming for chat, TTS or STT — the WebSocket/HTTP-stream surfaces are not wired.
- Document digitization, pronunciation dictionaries and voice cloning are out of scope.
- `wiki_grounding` (a Sarvam-specific chat param) is **not exposed**: an extra
  `Params` field cannot reach the API today, because `execute_chat` reads a
  closed key set off `flattened` and drops the rest. Adding it needs a generic
  `provider_options` passthrough seam through `ChatUnifier` → `OpenAIProvider`
  `extra_body`; shipping the field without that seam would render a checkbox
  that does nothing.
