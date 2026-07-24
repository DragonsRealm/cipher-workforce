"""Google Cloud CLI (`gcloud`) downloader — pooch-driven, project-local.

Mirrors :mod:`server.nodes.github._install` (the repo's shared pattern
for fetching official CLI binaries): ``pooch.retrieve`` handles the
download, caching (re-runs are instant cache hits keyed by filename),
and archive extraction; we contribute only the pinned versioned URL and
the per-platform asset map.

The SDK lands under ``<DATA_DIR>/packages/gcloud/`` — the same
:func:`core.paths.package_dir` root that holds the gh / Stripe /
Temporal binaries. The system-global gcloud is deliberately never
consulted, so node behavior doesn't depend on whatever version is on
the operator's PATH. Auth state is fully isolated too: every
invocation pins ``CLOUDSDK_CONFIG`` under the data dir (see
``_service.gcloud_env``), so this install never reads or writes the
operator's own ``~/.config/gcloud`` / ``%APPDATA%\\gcloud``.

Asset matrix notes (verified against dl.google.com):

- The Windows bundled-python zip uses the legacy ``google-cloud-sdk-``
  filename prefix; every other versioned asset uses
  ``google-cloud-cli-``. Do NOT "normalize" the Windows name on a
  version bump — the ``-cli-`` spelling 404s.
- Windows has no ARM64 asset; ARM64 maps to the x86_64 bundled-python
  zip (runs under x64 emulation).
- linux-x86_64 and the Windows zip bundle their own Python; darwin and
  linux-arm archives do NOT — they need a system python3 (3.9-3.13),
  overridable via ``CLOUDSDK_PYTHON``.
- The archives are directly runnable after extraction: ``install.sh`` /
  ``install.bat`` only do PATH/completion setup and are deliberately
  not run. ``gcloud storage`` is native core — no ``components
  install`` step either.
"""

from __future__ import annotations

import asyncio
import platform
import stat
from pathlib import Path
from typing import Optional, Tuple

import pooch

from core.logging import get_logger

logger = get_logger(__name__)

_VERSION = "577.0.0"
_DL_BASE = "https://dl.google.com/dl/cloudsdk/channels/rapid/downloads"

_WINDOWS_ASSET = f"google-cloud-sdk-{_VERSION}-windows-x86_64-bundled-python.zip"

# (system, machine) -> (asset filename, binary name inside google-cloud-sdk/bin/).
_ASSETS: dict[Tuple[str, str], Tuple[str, str]] = {
    ("Windows", "AMD64"): (_WINDOWS_ASSET, "gcloud.cmd"),
    ("Windows", "ARM64"): (_WINDOWS_ASSET, "gcloud.cmd"),
    ("Linux", "x86_64"): (f"google-cloud-cli-{_VERSION}-linux-x86_64.tar.gz", "gcloud"),
    ("Linux", "aarch64"): (f"google-cloud-cli-{_VERSION}-linux-arm.tar.gz", "gcloud"),
    ("Linux", "arm64"): (f"google-cloud-cli-{_VERSION}-linux-arm.tar.gz", "gcloud"),
    ("Darwin", "x86_64"): (f"google-cloud-cli-{_VERSION}-darwin-x86_64.tar.gz", "gcloud"),
    ("Darwin", "arm64"): (f"google-cloud-cli-{_VERSION}-darwin-arm.tar.gz", "gcloud"),
}

_cached_path: Optional[Path] = None
_install_lock = asyncio.Lock()


def _package_root() -> Path:
    from core.paths import package_dir

    p = package_dir("gcloud")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _platform_asset() -> Tuple[str, str]:
    key = (platform.system(), platform.machine())
    asset = _ASSETS.get(key)
    if asset is None:
        raise RuntimeError(
            f"No prebuilt Google Cloud CLI for {key}. Supported: {sorted(_ASSETS)}. "
            "See https://cloud.google.com/sdk/docs/install"
        )
    return asset


def _extracted_binary_path() -> Path:
    """Deterministic post-extraction location: pooch's Unzip/Untar
    processors extract next to the archive under ``<fname>.unzip`` /
    ``<fname>.untar``. Unlike gh's version-suffixed inner dir, every
    Google Cloud CLI archive extracts to the constant inner root
    ``google-cloud-sdk/``."""
    asset_name, binary_name = _platform_asset()
    suffix = ".unzip" if asset_name.endswith(".zip") else ".untar"
    return _package_root() / f"{asset_name}{suffix}" / "google-cloud-sdk" / "bin" / binary_name


def gcloud_cli_path() -> Optional[Path]:
    """Sync getter for the project-local binary — the already-installed
    copy, without downloading. ``None`` when never installed."""
    global _cached_path
    if _cached_path and _cached_path.exists():
        return _cached_path
    target = _extracted_binary_path()
    if target.exists():
        _cached_path = target
        return target
    return None


async def ensure_gcloud_cli() -> Path:
    """Return absolute path to the project-local gcloud entry point,
    downloading the pinned release on miss. Idempotent + concurrent-safe.

    Cold path is heavy: 61-111 MB download plus a large extraction
    (~15k files). Runs in a worker thread under the install lock, so
    nothing blocks the event loop — callers on the WS path use the
    pending-response pattern to stay inside the frontend's budget.
    """
    global _cached_path
    existing = gcloud_cli_path()
    if existing:
        return existing

    async with _install_lock:
        existing = gcloud_cli_path()
        if existing:
            return existing
        binary = await asyncio.to_thread(_fetch_cli_sync)
        _cached_path = binary
        return binary


def _fetch_cli_sync() -> Path:
    """Download + extract the pinned Google Cloud CLI release via pooch.

    ``known_hash=None``: the URL is version-pinned and served over TLS
    from Google's CDN (temporal/gh precedent — transport integrity
    without hand-maintaining per-platform hashes).
    """
    asset_name, _ = _platform_asset()
    url = f"{_DL_BASE}/{asset_name}"
    logger.info("[GCloud] downloading Google Cloud CLI v%s from %s", _VERSION, url)

    processor = pooch.Unzip() if asset_name.endswith(".zip") else pooch.Untar()
    try:
        pooch.retrieve(
            url=url,
            known_hash=None,
            path=_package_root(),
            fname=asset_name,
            processor=processor,
            # requests' timeout is per-socket-read, not total download time
            # (temporal precedent: pooch's 30s default killed slow links).
            downloader=pooch.HTTPDownloader(timeout=300, progressbar=True),
        )
    except Exception as e:
        raise RuntimeError(
            f"Google Cloud CLI download failed: {e}. "
            "Note: darwin/linux-arm archives need a system python3 3.9-3.13 at "
            "runtime (override with CLOUDSDK_PYTHON)."
        ) from e

    # The archive is huge (~15k files) — resolve the entry point
    # directly instead of scanning pooch's extracted-files list.
    binary = _extracted_binary_path()
    if not binary.exists():
        raise RuntimeError(f"[GCloud] gcloud entry point not found at {binary} after extracting {asset_name}")
    if platform.system() != "Windows":
        # Untar preserves modes; belt-and-suspenders on the bin/ tree so
        # gcloud and its sibling launchers are always executable.
        for item in binary.parent.iterdir():
            if item.is_file():
                item.chmod(item.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    logger.info("[GCloud] Google Cloud CLI installed at %s", binary)
    return binary


__all__ = ["ensure_gcloud_cli", "gcloud_cli_path"]
