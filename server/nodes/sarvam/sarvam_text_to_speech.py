"""Sarvam Text to Speech — POST /text-to-speech (Bulbul v2/v3)."""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from pathlib import Path
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from services.plugin import ActionNode, NodeContext, NodeUserError, Operation, TaskQueue

from ._base import post_json, track_sarvam_usage
from ..model._credentials import SarvamCredential

_MAX_TEXT = {"bulbul:v3": 2500, "bulbul:v2": 1500}

# Sarvam returns audio as base64 inside the JSON body. A 2500-character v3
# request is roughly 12 MB of base64, and node outputs are persisted to the
# DB, broadcast over the status WebSocket, and — for a tool-exposed node —
# serialized into an LLM message. Writing to the workspace is therefore the
# default; inline base64 is opt-in and capped.
_MAX_INLINE_B64 = 1_000_000

_V3_SPEAKERS = (
    "shubh", "aditya", "ritu", "priya", "neha", "rahul", "pooja", "rohan",
    "simran", "kavya", "amit", "dev", "ishita", "shreya", "ratan", "varun",
    "manan", "sumit", "roopa", "kabir", "aayan", "ashutosh", "advait",
    "anand", "tanya", "tarun", "sunny", "mani", "gokul", "vijay", "shruti",
    "suhani", "mohit", "kavitha", "rehan", "soham", "rupali",
)
_V2_SPEAKERS = (
    "anushka", "manisha", "vidya", "arya", "abhilash", "karun", "hitesh",
)

_CODEC_EXT = {
    "wav": "wav", "mp3": "mp3", "flac": "flac", "aac": "aac",
    "opus": "opus", "linear16": "pcm", "mulaw": "ulaw", "alaw": "alaw",
}


class SarvamTextToSpeechParams(BaseModel):
    tool_name: str = Field(
        default="sarvam_text_to_speech",
        description="Override name shown to the LLM when used as a tool.",
    )
    tool_description: str = Field(
        default=(
            "Synthesize natural speech from text in 11 Indian languages using Sarvam's "
            "Bulbul voices. Writes an audio file and returns its path."
        ),
        description="Override description shown to the LLM when used as a tool.",
        json_schema_extra={"rows": 3},
    )
    text: str = Field(
        ...,
        min_length=1,
        description="Text to speak.",
        json_schema_extra={"rows": 4},
    )
    target_language_code: Literal[
        "bn-IN", "en-IN", "gu-IN", "hi-IN", "kn-IN", "ml-IN",
        "mr-IN", "od-IN", "pa-IN", "ta-IN", "te-IN",
    ] = Field(default="hi-IN", description="Language of the spoken output.")
    model: Literal["bulbul:v3", "bulbul:v2"] = Field(
        default="bulbul:v3",
        description="bulbul:v3 is the current model (37 voices, 2500 chars).",
    )
    speaker: str = Field(
        default="shubh",
        description="Voice name (lowercase). v3 and v2 have separate voice sets.",
    )
    pace: float = Field(
        default=1.0, ge=0.3, le=3.0, description="Speaking rate. v3 allows 0.5-2.0; v2 allows 0.3-3.0."
    )
    speech_sample_rate: Literal[8000, 16000, 22050, 24000, 32000, 44100, 48000] = Field(
        default=24000, description="Output sample rate in Hz."
    )
    output_audio_codec: Literal[
        "wav", "mp3", "linear16", "mulaw", "alaw", "opus", "flac", "aac"
    ] = Field(default="wav", description="Audio container/codec.")
    temperature: Optional[float] = Field(
        default=None,
        ge=0.01,
        le=2.0,
        description="Expressiveness. bulbul:v3 only.",
        json_schema_extra={"displayOptions": {"show": {"model": ["bulbul:v3"]}}},
    )
    pitch: Optional[float] = Field(
        default=None,
        ge=-0.75,
        le=0.75,
        description="Pitch shift. bulbul:v2 only.",
        json_schema_extra={"displayOptions": {"show": {"model": ["bulbul:v2"]}}},
    )
    loudness: Optional[float] = Field(
        default=None,
        ge=0.3,
        le=3.0,
        description="Output gain. bulbul:v2 only.",
        json_schema_extra={"displayOptions": {"show": {"model": ["bulbul:v2"]}}},
    )
    enable_preprocessing: Optional[bool] = Field(
        default=None,
        description="Normalise numbers and abbreviations. bulbul:v2 only (v3 always preprocesses).",
        json_schema_extra={"displayOptions": {"show": {"model": ["bulbul:v2"]}}},
    )
    return_audio: Literal["file", "base64"] = Field(
        default="file",
        description=(
            "'file' writes the audio into the workflow workspace and returns its path "
            "(recommended). 'base64' returns the bytes inline — only for short clips."
        ),
    )

    model_config = {"extra": "ignore"}


class SarvamTextToSpeechOutput(BaseModel):
    file_path: Optional[str] = None
    files: List[str] = Field(default_factory=list)
    chunk_count: int = 0
    audio_format: str = "wav"
    audio_base64: Optional[str] = None
    note: Optional[str] = None
    request_id: Optional[str] = None

    model_config = {"extra": "allow"}


class SarvamTextToSpeechNode(ActionNode):
    type = "sarvamTextToSpeech"
    display_name = "Sarvam Text to Speech"
    subtitle = "Synthesize Speech"
    group = ("language", "tool")
    description = "Generate natural speech in 11 Indian languages with Sarvam Bulbul voices"
    component_kind = "square"
    tool_name = "sarvam_text_to_speech"
    tool_description = (
        "Synthesize natural speech from text in 11 Indian languages using Sarvam's Bulbul "
        "voices. Writes an audio file into the workflow workspace and returns its path in "
        "`file_path` — pass that path to other nodes rather than expecting audio bytes. "
        "Keep `text` under 2500 characters."
    )
    handles = (
        {"name": "input-main", "kind": "input", "position": "left", "label": "Input", "role": "main"},
        {"name": "output-main", "kind": "output", "position": "right", "label": "Output", "role": "main"},
    )
    credentials = (SarvamCredential,)
    annotations = {"destructive": False, "readonly": False, "open_world": True}
    task_queue = TaskQueue.REST_API
    usable_as_tool = True

    Params = SarvamTextToSpeechParams
    Output = SarvamTextToSpeechOutput

    @Operation("synthesize", cost={"service": "sarvam", "action": "text_to_speech", "count": 1})
    async def synthesize(
        self, ctx: NodeContext, params: SarvamTextToSpeechParams
    ) -> SarvamTextToSpeechOutput:
        is_v3 = params.model == "bulbul:v3"
        cap = _MAX_TEXT[params.model]
        if len(params.text) > cap:
            raise NodeUserError(
                f"text is {len(params.text)} characters; {params.model} accepts at most "
                f"{cap}. Split the text across multiple calls."
            )

        speakers = _V3_SPEAKERS if is_v3 else _V2_SPEAKERS
        speaker = params.speaker.strip().lower()
        if speaker not in speakers:
            raise NodeUserError(
                f"'{params.speaker}' is not a {params.model} voice. Available: "
                + ", ".join(speakers)
            )
        if is_v3 and not (0.5 <= params.pace <= 2.0):
            raise NodeUserError(
                f"pace {params.pace} is out of range for bulbul:v3 (0.5-2.0)."
            )

        body = {
            "text": params.text,
            "target_language_code": params.target_language_code,
            "model": params.model,
            "speaker": speaker,
            "pace": params.pace,
            "speech_sample_rate": params.speech_sample_rate,
            "output_audio_codec": params.output_audio_codec,
        }
        if is_v3:
            body["temperature"] = params.temperature
        else:
            # v3 rejects these outright, so they are only sent for v2.
            body["pitch"] = params.pitch
            body["loudness"] = params.loudness
            body["enable_preprocessing"] = params.enable_preprocessing

        data = await post_json(ctx, "/text-to-speech", body)

        chunks = [c for c in (data.get("audios") or []) if c]
        if not chunks:
            raise NodeUserError("Sarvam returned no audio for this request.")

        out = SarvamTextToSpeechOutput(
            chunk_count=len(chunks),
            audio_format=params.output_audio_codec,
            request_id=data.get("request_id"),
        )

        if params.return_audio == "file":
            out.files = _write_chunks(
                chunks,
                workspace_dir=ctx.workspace_dir,
                node_id=ctx.node_id,
                codec=params.output_audio_codec,
                text=params.text,
            )
            out.file_path = out.files[0]
            if len(chunks) > 1:
                out.note = (
                    f"Sarvam split this request into {len(chunks)} audio chunks. "
                    "file_path is the first; see `files` for all of them. Each chunk "
                    "is a standalone file — concatenating them byte-wise is invalid."
                )
        else:
            first = chunks[0]
            if len(first) > _MAX_INLINE_B64:
                out.note = (
                    f"Inline audio suppressed: {len(first)} base64 characters exceeds the "
                    f"{_MAX_INLINE_B64} limit. Re-run with return_audio='file'."
                )
            else:
                out.audio_base64 = first
                if len(chunks) > 1:
                    out.note = (
                        f"audio_base64 is chunk 1 of {len(chunks)}. Use "
                        "return_audio='file' to capture every chunk."
                    )

        # bulbul:v2 is billed at half the v3 character rate.
        await track_sarvam_usage(
            ctx,
            "text_to_speech" if is_v3 else "text_to_speech_v2",
            len(params.text),
        )
        return out


def _write_chunks(
    chunks: List[str],
    *,
    workspace_dir: Optional[str],
    node_id: str,
    codec: str,
    text: str,
) -> List[str]:
    """Decode each base64 chunk to its own file under ``<workspace>/audio``.

    One file per chunk: every chunk carries its own container header, so
    concatenating them produces an unplayable file.
    """
    base = Path(workspace_dir) / "audio" if workspace_dir else Path("audio")
    base.mkdir(parents=True, exist_ok=True)

    slug = re.sub(r"[^a-z0-9]+", "-", text[:32].lower()).strip("-") or "speech"
    stem = f"{slug}-{node_id[:8]}-{uuid.uuid4().hex[:6]}"
    ext = _CODEC_EXT.get(codec, "wav")

    paths: List[str] = []
    for index, chunk in enumerate(chunks, start=1):
        try:
            blob = base64.b64decode(chunk, validate=True)
        except (binascii.Error, ValueError) as e:
            raise NodeUserError(
                f"Sarvam returned audio chunk {index} that is not valid base64: {e}"
            ) from e
        suffix = "" if len(chunks) == 1 else f"-{index}"
        target = base / f"{stem}{suffix}.{ext}"
        target.write_bytes(blob)
        paths.append(str(target))
    return paths
