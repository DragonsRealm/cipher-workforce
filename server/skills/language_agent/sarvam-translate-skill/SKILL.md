---
name: sarvam-translate-skill
description: Translate and transliterate text across 22 Indian languages, and detect which language a piece of text is written in, using Sarvam AI.
allowed-tools: sarvam_translate sarvam_transliterate sarvam_detect_language
metadata:
  author: opencompany
  version: "1.0"
  category: language

---

# Sarvam Text Skill

Sarvam AI's text APIs cover the Indian language space: translation across 22
languages, script conversion that preserves pronunciation, and language
identification.

## How It Works

This skill provides instructions and context. To execute, connect the
**Sarvam Translate**, **Sarvam Transliterate**, and/or **Sarvam Detect
Language** nodes to the agent's `input-tools` handle. All three share one
Sarvam API key.

## Choosing the right tool

These three are routinely confused. The distinction matters:

| You want to... | Use |
|---|---|
| Change the **language** — "Hello" to "नमस्ते" | `sarvam_translate` |
| Change the **script**, keeping the words — "namaste" to "नमस्ते" | `sarvam_transliterate` |
| Find out **what language** some text is in | `sarvam_detect_language` |

Transliteration is not translation. `sarvam_transliterate` on "namaste"
returns the same word in Devanagari; it does not turn it into "hello".

## sarvam_translate

| Field | Type | Required | Description |
|---|---|---|---|
| input | string | Yes | Text to translate |
| source_language_code | enum | No | Defaults to `auto` |
| target_language_code | enum | No | Defaults to `hi-IN` |
| model | enum | No | `sarvam-translate:v1` (22 langs, 2000 chars) or `mayura:v1` (10 langs, 1000 chars) |
| mode | enum | No | `formal` (default), `modern-colloquial`, `classic-colloquial`, `code-mixed` |
| speaker_gender | enum | No | `Male` / `Female`, for languages that inflect on it |
| output_script | enum | No | `roman`, `fully-native`, `spoken-form-in-native` |
| numerals_format | enum | No | `international` (default) or `native` |

Languages: `as-IN bn-IN brx-IN doi-IN en-IN gu-IN hi-IN kn-IN kok-IN ks-IN
mai-IN ml-IN mni-IN mr-IN ne-IN od-IN pa-IN sa-IN sat-IN sd-IN ta-IN te-IN
ur-IN`.

```json
{
  "translated_text": "नमस्ते दुनिया",
  "source_language_code": "en-IN"
}
```

Pick `mode` deliberately: `formal` for documents and notices,
`modern-colloquial` for chat and app copy, `code-mixed` when the audience
naturally mixes English with the target language.

## sarvam_transliterate

Languages: `bn-IN en-IN gu-IN hi-IN kn-IN ml-IN mr-IN od-IN pa-IN ta-IN
te-IN` (plus `auto` as a source).

Set `spoken_form: true` to expand numbers, dates and abbreviations into how
they are said aloud — useful when the output feeds text-to-speech.

```json
{
  "transliterated_text": "नमस्ते",
  "source_language_code": "en-IN"
}
```

## sarvam_detect_language

Takes `input` (max 1000 characters) and returns a BCP-47 language code plus
an ISO 15924 script code.

```json
{
  "language_code": "hi-IN",
  "script_code": "Deva"
}
```

**Both fields can be `null`** when the text is too short or ambiguous.
Always check before feeding the result into another call — do not assume a
language was detected.

## When to Use

- Localising content into Indian languages
- Normalising user input typed in Roman script into native script
- Routing a message to the right handler based on its language
- Preparing text for `text_to_speech`, which needs a target language

## When NOT to Use

- Non-Indian languages — Sarvam covers Indian languages plus English only
- Long documents in one call: cap is 2000 characters (`sarvam-translate:v1`)
  or 1000 (`mayura:v1`). Split on sentence boundaries and translate in
  batches; splitting mid-sentence degrades quality badly.
- Changing script only — reach for `sarvam_transliterate`, not translate

## API Details

- **Translate**: POST `https://api.sarvam.ai/translate` — 20 INR / 10K characters
- **Transliterate**: POST `https://api.sarvam.ai/transliterate`
- **Language ID**: POST `https://api.sarvam.ai/text-lid` — 3.5 INR / 10K characters
- **Auth**: `api-subscription-key` header

## Setup Requirements

A single Sarvam API key from [dashboard.sarvam.ai](https://dashboard.sarvam.ai),
entered once in the Credentials modal under **Sarvam AI**. The same key
covers translation, speech and the Sarvam chat models.
