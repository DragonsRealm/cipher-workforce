---
name: translation-skill
description: Translate text between languages, convert between writing systems, and identify what language a piece of text is in — across DeepL, Sarvam AI and LLM-backed providers.
allowed-tools: translate_text transliterate_text detect_language
metadata:
  author: opencompany
  version: "2.0"
  category: language

---

# Translation Skill

Three tools, several providers behind each. Pick the provider that fits the
language pair rather than defaulting to one.

## How It Works

Connect **Translate**, **Transliterate** and/or **Detect Language** to the
agent's `input-tools` handle. Each has a `provider` parameter, and the
credential for the chosen provider must be configured in the Credentials
modal.

## The distinction that matters most

**Translation changes the words. Transliteration changes the script.**

| Input | Tool | Output |
|---|---|---|
| `hello` | `translate_text` → hi | `नमस्ते` |
| `namaste` | `transliterate_text` → hi | `नमस्ते` |

Both produce Devanagari, but only one changed the meaning. Getting this
backwards is the most common misuse: if the user wants to *read* foreign text,
they want translation; if they want to *type* their own language in its native
script, or make a foreign name pronounceable, they want transliteration.

## translate_text

| Field | Type | Required | Description |
|---|---|---|---|
| text | string | Yes | Text to translate |
| provider | enum | No | `deepl`, `sarvam`, `openai` |
| target_language | string | No | Blank uses the provider default |
| source_language | string | No | Blank auto-detects |
| translate_model | string | No | Blank uses the provider default |
| formality | string | No | Register, where the provider supports it |
| context | string | No | Disambiguating context — not itself translated |
| preserve_formatting | boolean | No | Keep line breaks and punctuation as-is |
| provider_options | object | No | Vendor-specific extras |

Returns `translated_text` and `detected_source_language`.

### Choosing a provider

- **deepl** — best quality for European languages, and the only one that
  reports exactly how many characters it billed. Language codes are uppercase
  (`DE`, `EN-US`, `PT-BR`). Prefer `prefer_more` / `prefer_less` for formality
  over `more` / `less`: the strict forms 400 on target languages that do not
  support the distinction. Does **not** transliterate or detect.
- **sarvam** — Indian languages: 22 on `sarvam-translate:v1`, 11 on
  `mayura:v1`. Codes are always `-IN` suffixed (`hi-IN`, `ta-IN`). `formality`
  maps to its register modes: `formal`, `modern-colloquial`,
  `classic-colloquial`, `code-mixed`.
- **openai** — LLM-backed. Covers pairs the others do not, needs no extra
  credential if a chat key is already configured, and takes free-form context
  well. Slower, and billed as tokens rather than characters.

## transliterate_text

| Field | Type | Required | Description |
|---|---|---|---|
| text | string | Yes | Text to convert |
| provider | enum | No | `sarvam`, `openai` |
| target_language | string | No | Language whose script to write in |
| source_language | string | No | Blank auto-detects |
| target_script | string | No | Script style, where the provider offers a choice |
| provider_options | object | No | Vendor-specific extras |

Returns `transliterated_text`. Sarvam's `target_script` values are `roman`,
`fully-native` and `spoken-form-in-native` — the last renders numbers and
abbreviations the way they would be *spoken*, which is what you want when the
output feeds `text_to_speech`.

## detect_language

| Field | Type | Required | Description |
|---|---|---|---|
| text | string | Yes | Text to identify |
| provider | enum | No | `sarvam`, `openai` |

Returns `language`, `script` and — from providers that report it —
`confidence`. Use it before translating when the source is unknown, or to
route text by language.

## When to Use

- The user asks for a translation, or pastes text in a language they cannot read
- Preparing text for `text_to_speech`, which needs a target language
- Routing or filtering content by language

## When NOT to Use

- Translating an entire document — chunk it first; every provider caps input
- Detecting language from a single word; give it a sentence
- Transliteration when the user actually meant translation (see above)

## Setup Requirements

One API key per provider you intend to use. The `sarvam` and `openai` keys are
the same ones their chat models use, so if those are configured these tools
work with no extra setup. DeepL needs its own key — free keys end in `:fx` and
are routed to the free host automatically.
