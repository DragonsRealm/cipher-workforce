---
name: sarvam-speech-skill
description: Transcribe speech to text and synthesize natural speech in Indian languages using Sarvam AI's Saaras and Bulbul models.
allowed-tools: sarvam_speech_to_text sarvam_text_to_speech
metadata:
  author: opencompany
  version: "1.0"
  category: language

---

# Sarvam Speech Skill

Speech-to-text across 23 Indian languages (Saaras) and text-to-speech with
37 voices (Bulbul).

## How It Works

This skill provides instructions and context. To execute, connect the
**Sarvam Speech to Text** and/or **Sarvam Text to Speech** nodes to the
agent's `input-tools` handle.

## sarvam_speech_to_text

| Field | Type | Required | Description |
|---|---|---|---|
| audio_file | string | Yes | Path to an audio file |
| model | enum | No | `saaras:v3` (default, 23 languages) or `saarika:v2.5` (legacy) |
| language_code | string | No | BCP-47 code, or `unknown` (default) to auto-detect |
| mode | enum | No | `transcribe` (default), `translate`, `verbatim`, `translit`, `codemix` |

Modes:
- `transcribe` — native script, cleaned up
- `translate` — English output regardless of the spoken language
- `verbatim` — keeps disfluencies ("um", repetitions); use for compliance
  or verbatim-record work
- `translit` — Roman script
- `codemix` — preserves mixed-language speech as spoken

Everything except `transcribe` and `translate` requires `saaras:v3`.

```json
{
  "transcript": "नमस्ते, आप कैसे हैं?",
  "language_code": "hi-IN",
  "language_probability": 0.97
}
```

**Hard limit: clips under 30 seconds.** This node calls the synchronous
endpoint. Longer recordings need Sarvam's Batch API, which is not wired
here — split the audio first, or tell the user it is out of scope rather
than retrying.

Formats: WAV, MP3, AAC, AIFF, OGG, OPUS, FLAC, MP4/M4A, AMR, WMA, WebM,
PCM. Accuracy is best at a 16 kHz sample rate.

## sarvam_text_to_speech

| Field | Type | Required | Description |
|---|---|---|---|
| text | string | Yes | Text to speak (max 2500 chars on v3, 1500 on v2) |
| target_language_code | enum | No | Defaults to `hi-IN` |
| model | enum | No | `bulbul:v3` (default) or `bulbul:v2` |
| speaker | string | No | Voice name, lowercase. Defaults to `shubh` |
| pace | number | No | v3: 0.5–2.0, v2: 0.3–3.0. Default 1.0 |
| speech_sample_rate | enum | No | 8000–48000 Hz, default 24000 |
| output_audio_codec | enum | No | `wav` (default), `mp3`, `flac`, `aac`, `opus`, `linear16`, `mulaw`, `alaw` |
| return_audio | enum | No | `file` (default) or `base64` |

Voices are **per model** and do not overlap:
- `bulbul:v3` — shubh, aditya, ritu, priya, neha, rahul, pooja, rohan,
  simran, kavya, amit, dev, ishita, shreya, ratan, varun, manan, sumit,
  roopa, kabir, aayan, ashutosh, advait, anand, tanya, tarun, sunny, mani,
  gokul, vijay, shruti, suhani, mohit, kavitha, rehan, soham, rupali
- `bulbul:v2` — anushka, manisha, vidya, arya, abhilash, karun, hitesh

Asking for a v2 voice on v3 is an error, not a fallback.

```json
{
  "file_path": "/…/workspaces/My_Flow_1/audio/namaste-a1b2c3d4-9f8e7d.wav",
  "files": ["/…/namaste-a1b2c3d4-9f8e7d.wav"],
  "chunk_count": 1,
  "audio_format": "wav"
}
```

**The audio is written to a file, not returned inline.** Pass `file_path`
along to whatever consumes it. Only set `return_audio: "base64"` for very
short clips you must inline — a full-length request is roughly 12 MB of
base64, which bloats the workflow output and your own context.

For long text, Sarvam may return several chunks. Each is a standalone audio
file with its own header, so `files` lists them all and `file_path` points
at the first. Concatenating them byte-wise produces an unplayable file.

## When to Use

- Transcribing voice notes, IVR recordings or short call clips
- Generating voice prompts and announcements in Indian languages
- Building a voice loop: transcribe → reason → synthesize a reply

## When NOT to Use

- Recordings longer than 30 seconds (Batch API territory)
- Speaker diarization — only the Batch API returns it
- Real-time streaming — this is the request/response endpoint
- Languages outside the Indian set plus English

## API Details

- **Speech to Text**: POST `https://api.sarvam.ai/speech-to-text` (multipart) — 30 INR/hour
- **Text to Speech**: POST `https://api.sarvam.ai/text-to-speech` — 30 INR / 10K chars (v3)
- **Auth**: `api-subscription-key` header

## Setup Requirements

A single Sarvam API key from [dashboard.sarvam.ai](https://dashboard.sarvam.ai),
entered once in the Credentials modal under **Sarvam AI**. The same key
covers speech, translation and the Sarvam chat models.
