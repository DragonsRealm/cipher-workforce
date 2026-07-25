"""Speech provider plugins — importing this package registers them all.

Adding a provider:

1. Add a ``providers.<name>`` block to ``server/config/speech_defaults.json``
   with at least ``base_url`` and a ``tts`` and/or ``stt`` sub-block. Which
   sub-blocks exist declares which directions it serves.
2. Write ``<name>.py`` here implementing the matching protocol(s) from
   :mod:`nodes.speech._protocol`, subclassing
   :class:`nodes.speech._providers._http.HttpSpeechProvider` if it is a
   plain HTTP API.
3. Call ``register_tts_provider`` / ``register_stt_provider`` at the bottom
   of that module.
4. Add the side-effect import below.
5. Add the credential id to ``server/config/credential_providers.json`` and a
   ``Credential`` subclass under ``nodes/speech/_credentials.py``.

If the provider speaks OpenAI's ``/audio/*`` wire format, steps 2-4 collapse
into adding its name to ``_COMPAT_STT_PROVIDERS`` in ``_openai_compat.py``.

**Never import a provider SDK at module level.** ``sdk_exception_refs`` are
lazy ``"module:ClassName"`` strings precisely so registration stays free;
``tests/speech/test_lazy_sdk_imports.py`` enforces it from a clean
interpreter.
"""

from __future__ import annotations

# Side-effect imports — each module registers into one or both registries at
# the bottom of the file. Order is documentation only; Python's module cache
# makes duplicate registration impossible.
from . import _openai_compat  # noqa: F401
from . import elevenlabs  # noqa: F401
from . import deepgram  # noqa: F401
from . import sarvam  # noqa: F401
