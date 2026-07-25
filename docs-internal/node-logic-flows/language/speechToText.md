# Speech to Text (`speechToText`)

| Field | Value |
|------|-------|
| **Category** | language |
| **Backend handler** | [`server/nodes/speech/speech_to_text.py`](../../../server/nodes/speech/speech_to_text.py) |
| **Shared helpers** | [`server/nodes/speech/_base.py`](../../../server/nodes/speech/_base.py), [`_unifier.py`](../../../server/nodes/speech/_unifier.py), [`_providers/`](../../../server/nodes/speech/_providers/) |
| **Capabilities config** | [`server/config/speech_defaults.json`](../../../server/config/speech_defaults.json) |
| **Tests** | [`server/tests/nodes/test_speech.py`](../../../server/tests/nodes/test_speech.py) |
| **Skill (if any)** | [`server/skills/language_agent/speech-skill/SKILL.md`](../../../server/skills/language_agent/speech-skill/SKILL.md) |
| **Dual-purpose tool** | yes — tool name `speech_to_text` |

## Purpose

Transcribe audio through any configured provider. Replaces the vendor-locked
`sarvamSpeechToText`, and fixes two of its defects along the way (see
**Edge cases**).

## Inputs (handles)

| Handle | Connection type | Required | Purpose |
|--------|-----------------|----------|---------|
| `input-main` | main | no | Upstream data injected into params |

## Parameters

| Name | Type | Default | Required | Description |
|------|------|---------|----------|-------------|
| `provider` | enum | `deepgram` | no | Registry-driven: the enum is `stt_providers()` |
| `audio_file` | string \| AudioRef \| upload | `""` | yes | Widget `file`. Three shapes accepted — see below |
| `stt_model` | string | `""` | no | Blank uses the provider default. **Not** named `model` |
| `language` | string | `""` | no | Blank auto-detects |
| `translate` | boolean | `false` | no | Translate to English rather than transcribe |
| `diarize` | boolean | `false` | no | Speaker labels, where supported |
| `timestamps` | boolean | `false` | no | Per-word timing, where supported |
| `provider_options` | object | `{}` | no | Vendor-specific keys, passed through untouched |

`audio_file` accepts an `AudioRef` (from an upstream node or the upload
route), a bare workspace path, or the legacy `{type: "upload", data: base64}`
envelope the file widget used to emit. All three route through
`services.media`, which resolves under containment.

## Outputs

| Field | Type | Description |
|-------|------|-------------|
| `transcript` | string | The text |
| `language` / `language_confidence` | string / float | Detected or echoed |
| `duration_seconds` | float | Provider-reported, else probed from the file |
| `words` | object[] | `word` / `start` / `end` / `speaker` / `confidence` |
| `segments` | object[] | Utterances or segments, provider-shaped |
| `provider` / `stt_model` / `request_id` | string | What actually ran |

## Side effects

- Records an `APIUsageMetric` keyed on the provider id, in that provider's
  own billing unit — seconds for OpenAI / Groq / Sarvam, **minutes** for
  Deepgram (converted by the provider module, not by shared code)

## Edge cases

- **Path traversal — fixed.** The predecessor joined a user-supplied path
  onto the workspace root unchecked, so `audio_file="../../credentials.db"`
  read the encrypted credential store and uploaded it to the provider.
  Every input shape now resolves through `services.media`, which rejects
  `..` / `~` / drive prefixes and re-checks containment after resolution.
- **Billing — fixed.** The predecessor charged every transcription as a flat
  30 seconds because it never measured the clip. Duration is now taken from
  the provider when reported, otherwise probed with `inspect_audio`; when
  neither is possible no metric is written, because an under-count is honest
  and a fabricated one is not.
- **Capability downgrades**: `verbose_json` is a 400 on `gpt-4o-transcribe`,
  so a timestamp request on a gated model is downgraded with a WARN rather
  than failing.
- **Groq**: bills a 10-second floor per request; the provider applies it so
  cost reporting matches the invoice. `whisper-large-v3-turbo` cannot
  translate and falls back with a WARN.
- **Sarvam**: timestamps and diarization are batch-API-only and come back
  empty on the synchronous endpoint.

## Related

- **Peer nodes**: [`textToSpeech`](./textToSpeech.md) — the other half of a voice loop.
- **RFC**: [`speech_provider_rfc.md`](../../speech_provider_rfc.md)
