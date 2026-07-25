# RFC: Multi-provider Speech (TTS / STT)

**Status:** accepted, implementation in progress
**Date:** 2026-07-25
**Supersedes:** the vendor-locked `sarvamTextToSpeech` / `sarvamSpeechToText` nodes

---

## 1. Problem

Speech exists in exactly one place today: two Sarvam nodes. The pattern does not generalise — a
second vendor means a second pair of nodes, a third a third pair, each with its own params, its own
credential, its own skill file. Chat models solved this years ago with `services/llm/`; speech has
no equivalent.

Adding one is not just a refactor, because **audio is big and the execution engine is not built for
big**. That constraint, measured rather than assumed, drives nearly every decision below.

---

## 2. The measured constraint

| Limit | Value | Source |
|---|---|---|
| Temporal blob **error** — activity result, activity **input**, workflow result | **2,097,152 B** | server default; no custom `DataConverter` or `PayloadCodec` anywhere in the repo |
| Temporal blob **warning** | 524,288 B → `PayloadSizeWarning` | not covered by `pyproject.toml` `filterwarnings`, so it surfaces |
| Retries burned before the failure is reported | **3** | `_PayloadSizeError` absent from `NON_RETRYABLE_ERROR_TYPES` ([`_retry_policies.py:51`](../server/services/temporal/_retry_policies.py)) |
| Legacy `execute_node_activity` internal WS | 4 MiB inbound (aiohttp default, `max_msg_size` unset) | [`activities.py:242`](../server/services/temporal/activities.py) |
| `node_outputs.data` | no cap; written **3×** per store (`output_main`/`output_top`/`output_0`) | [`activities.py:486`](../server/services/temporal/activities.py) |
| WS broadcast | no size guard; retained in `_status` **forever** and replayed to every newly connecting client | [`status_broadcaster.py:87,688`](../server/services/status_broadcaster.py) |
| In-memory `_outputs` | never evicted except by `clear_all_outputs` | [`workflow.py:577`](../server/services/workflow.py) |

### What actually happens to a 12 MB base64 TTS result

1. The activity result exceeds 2 MiB → temporalio raises `_PayloadSizeError` at the converter.
2. It is not marked non-retryable → **3 attempts**, each re-invoking the provider and re-billing it.
3. The user sees a generic activity failure, not "audio too large".

And the same limit applies to activity **inputs**, so a ~2 MB audio upload fails before the node
runs at all — today's only file-input path is base64-through-parameters.

### The multiplication

One audio payload is copied at least six ways: `_serialize_result` → `node_outputs` ×3 → WS
broadcast ×2 → `_status` cache (forever) → Temporal activity result → `MachinaWorkflow` aggregate →
every downstream activity input → and, because these nodes are `usable_as_tool`, verbatim into an
LLM message.

**Conclusion: audio cannot travel as bytes.** Not "should not" — the engine will not carry it.

---

## 3. Decisions

### D1 — `AudioRef` is structurally incapable of carrying bytes

A Pydantic model with `extra="forbid"`, a workspace-**relative** path, and no base64 field. Ever.
It serializes to ~400 B, i.e. ~5,200 refs before approaching the Temporal error limit.

Rejected alternative: a capped inline-base64 opt-in, as the current Sarvam TTS node has
(`_MAX_INLINE_B64 = 1_000_000`). The decisive argument is `usable_as_tool` — `execute_as_tool`
returns the flat `Output` dict straight into an LLM message and there is no mechanism to tell a
model "skip this field". A capped opt-in is *sometimes* catastrophic, which is worse than
always-broken because it passes review.

Relative paths (not absolute) because absolute paths embed the mutable workflow slug, leak the
operator's home directory into the DB / WebSocket / LLM context, and cannot be safely served over
HTTP.

### D2 — Two Protocols and two registries, not one `SpeechProvider`

AssemblyAI is STT-only; Cartesia is TTS-only. A single Protocol forces those vendors to ship a dead
method that raises — invisible to the node layer, so the provider would appear in the dropdown and
fail at runtime.

With `TtsProvider` and `SttProvider` registries, **direction capability is registry membership**:
the TTS node's provider enum is literally `tts_registry.all_providers()`. Zero extra machinery.

### D3 — Capability-driven common params + a `provider_options` escape hatch

The verified provider surface diverges much harder than the chat-completions surface does:

| | OpenAI | ElevenLabs | Deepgram |
|---|---|---|---|
| Auth header | `Authorization: Bearer` | `xi-api-key` | `Authorization: Token` |
| Voice selection | body param `voice` | **URL path segment** | encoded in the model name (`aura-2-thalia-en`) |
| Output format | `response_format` enum (`mp3\|opus\|aac\|flac\|wav\|pcm`) | **query** `output_format=mp3_44100_128` (container+rate+bitrate in one token) | separate `encoding` + `container` + `sample_rate` |
| Tuning | `instructions` (free text), `speed` | `voice_settings{stability, similarity_boost, style, use_speaker_boost, speed}` | — |

A per-provider `displayOptions` matrix over 8 providers explodes combinatorially and *still* cannot
express "ElevenLabs `stability` applies only to `eleven_multilingual_v2`". So: ~11 common fields
covering the 90 % case, normalised by each provider adapter, plus a `provider_options: dict`
passed through untouched for vendor specifics.

Keeps the tool schema flat — no `$defs` / `$ref`, which `usable_as_tool` requires.

### D4 — Capabilities declared in JSON, not code

`server/config/speech_defaults.json`, one block per provider per direction, overridable per model —
the same shape `llm_defaults.json` uses for `max_output_tokens`. Drives the provider dropdown, the
voice loader, and validation from one place. `test_plugin_shape.py` bans `if provider == "x"`
branches in shared code; this is how we avoid needing any.

### D5 — First multi-credential node in the repo

`credentials = (OpenAICredential, ElevenLabsCredential, …)` with `ctx.connection(params.provider)`.
Already supported: `_make_connection_factory` ([`base.py:870`](../server/services/plugin/base.py))
builds a dict over **all** declared credentials and raises only for undeclared ids. No node uses
this today.

Consequence: the node must use imperative `@Operation` bodies — the declarative `routing=` path
hardcodes `self.credentials[0]` ([`base.py:566`](../server/services/plugin/base.py)).

### D6 — Extract the generic registry rather than copy it

`ProviderSpec` + lazy `"module:Class"` `sdk_exception_refs` + idempotent registration is entirely
provider-agnostic. Copying it into `services/speech/` would fork the boot-time-import-avoidance
logic that exists specifically to keep ~7 s (warm) / ~45 s (cold) of SDK imports off startup.

`services/provider_registry.py` + `services/provider_clients.py`; `services/llm/registry.py`
becomes a shim. **Success criterion: `server/tests/llm/` passes untouched.**

### D7 — `tinytag` for duration and format

| Candidate | Native deps | Windows | License | Verdict |
|---|---|---|---|---|
| stdlib `wave` | none | ✅ | PSF | WAV only — insufficient alone |
| **`tinytag`** | **none** | ✅ | **MIT** | **chosen** |
| `mutagen` | none | ✅ | **GPL-2.0** | rejected — this repo is MIT; a licensing call, not a technical one |
| `pydub` | ffmpeg on PATH | ❌ | MIT | rejected |
| `ffprobe` | external binary | ❌ | LGPL/GPL | rejected — a pooch-managed binary for a 4-line metadata read |

Fallback chain: `tinytag` → stdlib `wave` → raw-PCM arithmetic → give up.

### D8 — Inspection never fails a workflow

Unknown format returns an all-`None` probe, logs at DEBUG, and still produces a valid `AudioRef`.
Duration-based **rejection only fires when duration is known**; unknown duration falls through to
the byte cap.

A metadata-parser miss on some codec variant must never turn a working workflow into a failing one.
Degrading a billing *estimate* is acceptable; hard-failing a valid file is not.

### D9 — The result-size guard ships WARN-only first

A generic size check in `BaseNode._serialize_result` is the highest-value safety net available: it
converts a 31-second silent triple-retry into an immediate, actionable error. But flipping straight
to hard-fail risks breaking a node that legitimately returns a large result today (a document
parser emitting 3 MB of extracted text).

Ship the warning, read one release of logs, then flip. Both thresholds behind `Settings`.

Implementation note: raise **`NodeUserError`** — it is already in `NON_RETRYABLE_ERROR_TYPES`, so
the failure is immediate with zero new registrations. Do **not** subclass it: Temporal matches
`non_retryable_error_types` on the exception **type-name string**, so a subclass silently starts
retrying again.

---

## 4. Architecture

```
services/provider_registry.py   generic ProviderSpec + registry      (extracted from llm/)
services/provider_clients.py    generic lease-counted client cache   (extracted from llm/)

services/media/                 audio transport — vendor-neutral, reusable for image/video
  refs.py       AudioRef
  workspace.py  write_audio / resolve_media / read_media_bytes / coerce_file_param
  inspect.py    tinytag -> wave -> PCM arithmetic; never raises
  limits.py     every size constant, one place

services/speech/                provider abstraction — mirrors services/llm/
  protocol.py   TtsProvider / SttProvider, requests, results, SpeechError
  registry.py   two registries over the generic one
  config.py     reads server/config/speech_defaults.json
  unifier.py    SpeechUnifier — dispatch + typed-error -> NodeUserError
  providers/    _http.py, _openai_compat.py (openai+groq STT), elevenlabs.py,
                sarvam.py, deepgram.py, gemini.py

nodes/speech/                   the two nodes
routers/workspace.py            GET file (Range-capable) + POST upload
```

**Layering rule.** `services/speech` takes credential **id strings**, never `Credential` classes.
The classes live under `nodes/`, and a `services → nodes` import inverts the layering and breaks
`test_plugin_self_containment.py`. A test locks the two sides agree.

---

## 5. Two bugs this work closes

1. **Path traversal in `sarvam_speech_to_text`.** `_read_audio` does `Path(workspace_dir) / raw`
   with no containment check, so `audio_file="../../credentials.db"` reads the encrypted credential
   store and uploads it to the provider. `coerce_file_param` closes it by construction.
2. **Workflow rename orphans every workspace file.** `rename_workflow` is a single-row `UPDATE`; the
   directory is never moved, and `_get_workspace_dir` keys on the mutable slug. Pre-existing and not
   audio-specific, but `AudioRef` makes it fixable — a best-effort `os.replace` in the rename path.

---

## 6. Explicitly out of scope

| Not doing | Why |
|---|---|
| Streaming / realtime speech | `@Operation` returns one Pydantic model; no `AsyncGenerator` support, no SSE anywhere in the repo. A genuinely new operation kind. |
| Keying workspaces on `workflow_id` | Changes the on-disk layout for every existing install, invalidates `--add-dir` paths baked into live Claude sessions, and reverses a documented decision. Best-effort directory rename instead. |
| Deduping the 3× `store_node_output` write | Touches `ParameterResolver`, the `socialReceive` four-handle case, and `store_agent_output` to save ~1.2 KB once refs replace blobs. |
| Removing the double `node_status` + `node_output` broadcast | Frontend-visible contract change. Elide the cached payload instead so the duplication is bounded. |
| Fixing `MachinaWorkflow.run`'s aggregate result | 20 nodes × 900 KB still exceeds 2 MiB even when every individual node passes. Structural Temporal change; the retry-policy fix makes it fail fast instead of slowly. |

---

## 7. Verified provider surface

Confirmed against live documentation on 2026-07-25. Anything not listed here was **not** verified
and must be checked before it is coded.

**OpenAI** — TTS `POST /v1/audio/speech`, models `gpt-4o-mini-tts` / `tts-1` / `tts-1-hd`, voices
`alloy ash ballad coral echo fable nova onyx sage shimmer verse marin cedar` (subset for tts-1),
`response_format` ∈ `mp3 opus aac flac wav pcm`, plus `instructions` and `speed`. Raw binary
response. STT `POST /v1/audio/transcriptions`, multipart.

**ElevenLabs** — TTS `POST /v1/text-to-speech/{voice_id}`, header `xi-api-key`, body `text`,
`model_id` (default `eleven_multilingual_v2`), `voice_settings{stability, similarity_boost, style,
use_speaker_boost, speed}`, `language_code`, `seed`, `apply_text_normalization` ∈ `auto|on|off`;
**query** `output_format` (default `mp3_44100_128`, 27 values). Binary response. Models via
`GET /v1/models`, voices via `GET /v1/voices`. STT `POST /v1/speech-to-text`, multipart,
`model_id` ∈ `scribe_v2|scribe_v1`, `language_code`, `tag_audio_events` (default true),
`num_speakers` (≤32), `timestamps_granularity` ∈ `none|word|character`, `diarize` (default false).
Response `{language_code, language_probability, text, words[]}`.

**Groq** — STT `POST https://api.groq.com/openai/v1/audio/transcriptions`, OpenAI-compatible,
`whisper-large-v3` / `whisper-large-v3-turbo`. This is why `_openai_compat.py` covers both vendors
off one factory.

**Deepgram** — TTS `POST /v1/speak`, header `Authorization: Token <key>`, model names of the form
`aura-2-<voice>-<lang>`. STT `POST /v1/listen`. Query-parameter driven; exact parameter set to be
confirmed at implementation time.

**Sarvam** — already implemented; see [`sarvam_service.md`](./sarvam_service.md). TTS returns
base64 inside JSON (`{audios: […]}`), the one provider that does.

**Gemini** — TTS via `generateContent` with an audio response modality. Shape **not yet verified**;
must be confirmed before coding.
