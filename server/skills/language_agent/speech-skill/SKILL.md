---
name: speech-skill
description: Convert text to spoken audio and transcribe audio to text, across multiple speech providers (OpenAI, ElevenLabs, Deepgram, Groq, Sarvam AI).
allowed-tools: text_to_speech speech_to_text
metadata:
  author: opencompany
  version: "2.0"
  category: language

---

# Speech Skill

Two tools, several providers behind each. Pick the provider that fits the
job rather than defaulting to one.

## How It Works

Connect the **Text to Speech** and/or **Speech to Text** nodes to the
agent's `input-tools` handle. Each node has a `provider` parameter, and the
credential for the chosen provider must be configured in the Credentials
modal.

Audio never travels as data. Synthesis writes a file into the workflow
workspace and returns a **reference** to it; transcription accepts a path or
a reference produced upstream. So Text to Speech can be wired straight into
Speech to Text, and neither tool ever puts audio bytes into the
conversation.

## text_to_speech

| Field | Type | Required | Description |
|---|---|---|---|
| text | string | Yes | The text to speak |
| provider | enum | No | `openai` (default), `elevenlabs`, `sarvam` |
| tts_model | string | No | Provider model id; blank uses the provider default |
| voice | string | No | Voice id; blank uses the provider default |
| language | string | No | Locale code. Required for Sarvam (e.g. `hi-IN`); auto-detected elsewhere |
| speed | number | No | Playback rate. Clamped to whatever the provider allows |
| output_format | string | No | Audio format; blank uses the provider default |
| provider_options | object | No | Vendor-specific extras, passed through untouched |

Returns `audio` — a reference carrying `path`, `url`, `duration_seconds` and
`mime_type` — plus `files` and `chunk_count`.

**When several clips come back**, they are separate playable files, not
parts of one stream. Each carries its own container header, so concatenating
them produces audio that plays only the first chunk.

### Choosing a provider

- **openai** — solid general-purpose quality, 13 voices, inexpensive. Put
  `instructions` in `provider_options` to steer tone, but note it works only
  on `gpt-4o-mini-tts` and is ignored on `tts-1`.
- **elevenlabs** — the most natural and controllable. Requires an explicit
  `voice`; there is no account-wide default. `stability`,
  `similarity_boost`, `style` and `use_speaker_boost` go in
  `provider_options`.
- **sarvam** — Indian languages: 11 locales, 37 voices on `bulbul:v3`.
  `language` is required. `pitch` / `loudness` work on `bulbul:v2` only and
  `temperature` on `bulbul:v3` only; the wrong one for the model is dropped
  rather than sent.

## speech_to_text

| Field | Type | Required | Description |
|---|---|---|---|
| audio_file | string / reference | Yes | Workspace path, or a reference from an upstream node |
| provider | enum | No | `openai` (default), `deepgram`, `groq`, `sarvam` |
| stt_model | string | No | Provider model id; blank uses the provider default |
| language | string | No | Language hint; blank auto-detects |
| translate | boolean | No | Translate to English instead of transcribing in-language |
| diarize | boolean | No | Label speakers, where supported |
| timestamps | boolean | No | Per-word timing, where supported |
| provider_options | object | No | Vendor-specific extras, passed through untouched |

Returns `transcript`, `language`, `duration_seconds`, and — when asked for
and supported — `words` and `segments`.

### Choosing a provider

- **openai** — reliable default. Word timestamps require `whisper-1`; the
  `gpt-4o-transcribe` models return plain JSON only, so a timestamp request
  is quietly downgraded rather than failing.
- **deepgram** — best for long recordings, diarization and keyword boosting.
  Billed per minute.
- **groq** — fastest and cheapest in bulk, but it bills a 10-second minimum
  per request, so many tiny clips cost more than their duration suggests.
  `whisper-large-v3-turbo` cannot translate; use `whisper-large-v3` when
  `translate` is set.
- **sarvam** — Indian languages. Timestamps and diarization are unavailable
  on its synchronous endpoint and come back empty.

## When to Use

- Narration, voiceovers, or "read this out"
- Working out what was said in a recording
- A voice loop: transcribe, reason over the text, synthesize a reply

## When NOT to Use

- Real-time or streaming speech — these tools are batch only
- Translating text you already have — use a translation tool
- Very long recordings on a synchronous provider; split them first

## Setup Requirements

One API key per provider you intend to use, added in the Credentials modal.
Nothing is shared between them: an ElevenLabs key does not enable Deepgram.
The `openai`, `groq` and `sarvam` keys are the same ones their chat models
use, so if those are already configured, speech works with no extra setup.
