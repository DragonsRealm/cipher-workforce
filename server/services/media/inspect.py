"""Audio metadata probing — duration, sample rate, channels.

Three fallbacks, and a hard rule: **this never raises and never fails a
workflow.** An unknown container returns an empty probe, logs at DEBUG,
and the caller still gets a valid :class:`~services.media.AudioRef`.

The rule matters because the alternative is worse in exactly the wrong
direction. Degrading a *billing estimate* when a codec variant confuses
the parser is acceptable; hard-failing a file the provider would have
accepted is not.

``tinytag`` was chosen over the alternatives on two filters. License:
``mutagen`` is GPL-2.0 and this repository is MIT, which is a legal
decision rather than a technical one. Portability: ``pydub`` and
``ffprobe`` both require an ffmpeg binary on PATH, and this repository is
developed on Windows where nothing installs one.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.logging import get_logger

logger = get_logger(__name__)

# Bytes per sample frame for the raw, header-less encodings a TTS provider
# may hand back. Used only when the caller tells us the sample rate --
# there is nothing in the file itself to read.
_PCM_BYTES_PER_SAMPLE = {
    "pcm": 2,
    "linear16": 2,
    "pcm_s16le": 2,
    "pcm_l16": 2,
    "pcm_raw": 2,
    "mulaw": 1,
    "ulaw": 1,
    "alaw": 1,
}


@dataclass(frozen=True)
class AudioProbe:
    """What we managed to learn. Every field may legitimately be None."""

    duration_seconds: Optional[float] = None
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    format: Optional[str] = None


def inspect_audio(
    path: Path | str,
    *,
    declared_format: Optional[str] = None,
    pcm_sample_rate: Optional[int] = None,
    pcm_channels: int = 1,
) -> AudioProbe:
    """Best-effort metadata for an audio file. Never raises."""
    target = Path(path)

    probe = _probe_tinytag(target)
    if probe.duration_seconds is not None:
        return probe

    probe = _probe_wave(target)
    if probe.duration_seconds is not None:
        return probe

    probe = _probe_raw_pcm(
        target,
        declared_format=declared_format,
        sample_rate=pcm_sample_rate,
        channels=pcm_channels,
    )
    if probe.duration_seconds is not None:
        return probe

    logger.debug(
        "audio metadata unavailable",
        path=str(target),
        declared_format=declared_format,
    )
    return AudioProbe(format=declared_format or None)


def _probe_tinytag(target: Path) -> AudioProbe:
    """mp3 / m4a / flac / ogg / opus / wav / aiff / wma."""
    try:
        from tinytag import TinyTag

        tag = TinyTag.get(str(target))
    except Exception as exc:  # unsupported container, corrupt header, missing dep
        logger.debug("tinytag declined", path=str(target), error=str(exc))
        return AudioProbe()

    duration = getattr(tag, "duration", None)
    return AudioProbe(
        duration_seconds=float(duration) if duration else None,
        sample_rate=getattr(tag, "samplerate", None),
        channels=getattr(tag, "channels", None),
        format=target.suffix.lstrip(".").lower() or None,
    )


def _probe_wave(target: Path) -> AudioProbe:
    """stdlib fallback for WAV variants tinytag declines."""
    try:
        with wave.open(str(target), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
            channels = handle.getnchannels()
    except Exception as exc:
        logger.debug("wave declined", path=str(target), error=str(exc))
        return AudioProbe()

    if not rate:
        return AudioProbe()
    return AudioProbe(
        duration_seconds=frames / float(rate),
        sample_rate=rate,
        channels=channels,
        format="wav",
    )


def _probe_raw_pcm(
    target: Path,
    *,
    declared_format: Optional[str],
    sample_rate: Optional[int],
    channels: int,
) -> AudioProbe:
    """Header-less PCM: arithmetic, only when the caller knows the rate."""
    if not declared_format or not sample_rate:
        return AudioProbe()
    width = _PCM_BYTES_PER_SAMPLE.get(declared_format.lower())
    if not width:
        return AudioProbe()
    try:
        size = target.stat().st_size
    except OSError:
        return AudioProbe()
    divisor = sample_rate * width * max(1, channels)
    if divisor <= 0:
        return AudioProbe()
    return AudioProbe(
        duration_seconds=size / divisor,
        sample_rate=sample_rate,
        channels=channels,
        format=declared_format.lower(),
    )


__all__ = ["AudioProbe", "inspect_audio"]
