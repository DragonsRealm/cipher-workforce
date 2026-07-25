"""Sarvam Speech to Text — POST /speech-to-text (multipart transcription)."""

from __future__ import annotations

import base64
import binascii
import mimetypes
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field

from services.plugin import ActionNode, NodeContext, NodeUserError, Operation, TaskQueue

from ._base import post_multipart, track_sarvam_usage
from ..model._credentials import SarvamCredential

# The synchronous REST endpoint is documented for clips under 30 seconds;
# anything longer needs Sarvam's Batch API, which is a different job-based
# surface and out of scope for this node.
_MAX_SECONDS = 30
# Guard against a caller pasting a multi-hundred-MB blob into the param.
_MAX_BYTES = 24 * 1024 * 1024


class SarvamSpeechToTextParams(BaseModel):
    tool_name: str = Field(
        default="sarvam_speech_to_text",
        description="Override name shown to the LLM when used as a tool.",
    )
    tool_description: str = Field(
        default=(
            "Transcribe a short audio clip (under 30 seconds) in any of 23 Indian "
            "languages. Can also translate speech directly into English."
        ),
        description="Override description shown to the LLM when used as a tool.",
        json_schema_extra={"rows": 3},
    )
    # Accepts either a workspace/absolute path OR the upload envelope the
    # file widget emits ({type, data, filename, mimeType}) — the frontend
    # sends whichever the user chose, so the field must tolerate both.
    audio_file: Union[str, Dict[str, Any]] = Field(
        default="",
        description="Path to an audio file, or an uploaded file.",
        json_schema_extra={
            "widget": "file",
            "accept": "audio/*,.wav,.mp3,.m4a,.ogg,.opus,.flac,.aac,.webm,.amr",
        },
    )
    model: Literal["saaras:v3", "saarika:v2.5"] = Field(
        default="saaras:v3",
        description="saaras:v3 covers 23 languages; saarika:v2.5 is the legacy 11-language model.",
    )
    language_code: str = Field(
        default="unknown",
        description="BCP-47 code of the spoken language, or 'unknown' to auto-detect.",
    )
    mode: Literal["transcribe", "translate", "verbatim", "translit", "codemix"] = Field(
        default="transcribe",
        description=(
            "transcribe = native script; translate = English output; verbatim keeps "
            "disfluencies; translit = Roman script; codemix preserves mixed-language input. "
            "Everything except transcribe/translate requires saaras:v3."
        ),
    )
    input_audio_codec: Optional[Literal["pcm_s16le", "pcm_l16", "pcm_raw"]] = Field(
        default=None,
        description="Only needed for headerless PCM; every other format is auto-detected.",
    )

    model_config = {"extra": "ignore"}


class SarvamSpeechToTextOutput(BaseModel):
    transcript: str = ""
    language_code: Optional[str] = None
    language_probability: Optional[float] = None
    # Present only when Sarvam returns them; the sync endpoint omits
    # diarization entirely (Batch API only).
    timestamps: Optional[Dict[str, Any]] = None
    diarized_transcript: Optional[Dict[str, Any]] = None
    request_id: Optional[str] = None

    model_config = {"extra": "allow"}


class SarvamSpeechToTextNode(ActionNode):
    type = "sarvamSpeechToText"
    display_name = "Sarvam Speech to Text"
    subtitle = "Transcribe Audio"
    group = ("language", "tool")
    description = "Transcribe or translate short audio clips across 23 Indian languages"
    component_kind = "square"
    tool_name = "sarvam_speech_to_text"
    tool_description = (
        "Transcribe a short audio clip (under 30 seconds) in any of 23 Indian languages. "
        "Pass a file path in `audio_file`. Set mode='translate' to get English text from "
        "non-English speech. Leave language_code='unknown' to auto-detect. Returns the "
        "transcript and the detected language."
    )
    handles = (
        {"name": "input-main", "kind": "input", "position": "left", "label": "Input", "role": "main"},
        {"name": "output-main", "kind": "output", "position": "right", "label": "Output", "role": "main"},
    )
    credentials = (SarvamCredential,)
    annotations = {"destructive": False, "readonly": True, "open_world": True}
    task_queue = TaskQueue.REST_API
    usable_as_tool = True

    Params = SarvamSpeechToTextParams
    Output = SarvamSpeechToTextOutput

    @Operation("transcribe", cost={"service": "sarvam", "action": "speech_to_text", "count": 1})
    async def transcribe(
        self, ctx: NodeContext, params: SarvamSpeechToTextParams
    ) -> SarvamSpeechToTextOutput:
        if params.mode not in ("transcribe", "translate") and params.model != "saaras:v3":
            raise NodeUserError(
                f"mode='{params.mode}' requires model='saaras:v3'; "
                f"'{params.model}' supports only transcribe and translate."
            )

        filename, blob = _read_audio(params.audio_file, ctx.workspace_dir)
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"

        data = await post_multipart(
            ctx,
            "/speech-to-text",
            files={"file": (filename, blob, mime)},
            data={
                "model": params.model,
                "language_code": params.language_code,
                "mode": params.mode,
                "input_audio_codec": params.input_audio_codec,
            },
        )

        # Billed per second of audio; we do not decode the clip to measure
        # it, so charge the documented sync-endpoint ceiling.
        await track_sarvam_usage(ctx, "speech_to_text", _MAX_SECONDS)
        return SarvamSpeechToTextOutput(
            transcript=data.get("transcript", ""),
            language_code=data.get("language_code"),
            language_probability=data.get("language_probability"),
            timestamps=data.get("timestamps"),
            diarized_transcript=data.get("diarized_transcript"),
            request_id=data.get("request_id"),
        )


def _read_audio(
    audio_file: Union[str, Dict[str, Any]], workspace_dir: Optional[str]
) -> tuple[str, bytes]:
    """Resolve the dual-shaped ``audio_file`` param to ``(filename, bytes)``.

    The file widget emits ``{"type": "upload", "data": "<base64>", ...}``
    when the user picks a local file, and a bare path string when they type
    one or drag an upstream value in. Relative paths resolve against the
    per-workflow workspace so they line up with ``fileDownloader`` output.
    """
    if isinstance(audio_file, dict):
        if audio_file.get("type") != "upload" or not audio_file.get("data"):
            raise NodeUserError(
                "audio_file is an object but not a file upload. Provide a file "
                "path or re-select the file."
            )
        try:
            blob = base64.b64decode(audio_file["data"], validate=True)
        except (binascii.Error, ValueError) as e:
            raise NodeUserError(f"Uploaded audio is not valid base64: {e}") from e
        name = str(audio_file.get("filename") or "audio.wav")
    else:
        raw = (audio_file or "").strip()
        if not raw:
            raise NodeUserError(
                "audio_file is required — provide a path to an audio file or upload one."
            )
        path = Path(raw)
        if not path.is_absolute() and workspace_dir:
            path = Path(workspace_dir) / path
        if not path.is_file():
            raise NodeUserError(f"Audio file not found: {path}")
        blob = path.read_bytes()
        name = path.name

    if not blob:
        raise NodeUserError("Audio file is empty.")
    if len(blob) > _MAX_BYTES:
        raise NodeUserError(
            f"Audio is {len(blob) // (1024 * 1024)} MB. The synchronous endpoint takes "
            f"clips under {_MAX_SECONDS} seconds — trim it, or use Sarvam's Batch API "
            "for long recordings."
        )
    return name, blob


__all__: List[str] = [
    "SarvamSpeechToTextNode",
    "SarvamSpeechToTextOutput",
    "SarvamSpeechToTextParams",
]
