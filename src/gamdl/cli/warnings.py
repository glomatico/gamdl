"""Non-fatal account and codec configuration warnings."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from gamdl.drm import Drm, DrmBackend
from gamdl.media import SongCodec

if TYPE_CHECKING:
    from rich.console import Console

    from gamdl.api import AppleMusicApi, WrapperApi


_ALAC_CODECS = frozenset(
    {SongCodec.ALAC, SongCodec.ALAC_HIRES, SongCodec.ALAC_LOSSLESS}
)


def warn_content_restrictions(api: AppleMusicApi, output: Console) -> None:
    """Report restrictions returned by the account endpoint, not missing data."""
    restrictions = getattr(api, "account_restrictions", None)
    if isinstance(restrictions, dict) and restrictions:
        output.print(
            "[yellow]![/yellow] Your Apple Music account has content restrictions "
            "enabled; some content may not be downloadable"
        )


def warn_alac_without_wrapper(
    wrapper: WrapperApi | None,
    output: Console,
    *,
    codec: str | None,
    codec_priority: Sequence[str],
    ask_song_codec: bool = False,
) -> None:
    """Warn about lossless licensing only when ALAC is actually configured."""
    if wrapper is not None or ask_song_codec:
        return
    selected = (codec,) if codec else codec_priority
    if not any(item in _ALAC_CODECS for item in selected):
        return
    output.print(
        "[yellow]![/yellow] ALAC selected without Wrapper. Apple Music may "
        "refuse Widevine/PlayReady licenses for ALAC; use Wrapper for more "
        "reliable lossless downloads"
    )


async def warn_high_resolution_security(
    drm: Drm,
    output: Console,
    *,
    max_height: int,
) -> None:
    """Warn about a verified insufficient device security level above 1080p."""
    if max_height <= 1080 or not isinstance(drm, Drm):
        return
    level = await drm.get_device_security_level()
    if level is None:
        return
    if drm.backend == DrmBackend.WIDEVINE:
        if level == 1:
            return
        description = f"Widevine L{level}"
    elif drm.backend == DrmBackend.PLAYREADY:
        if level >= 3000:
            return
        description = f"PlayReady SL{level}"
    else:
        return
    output.print(
        f"[yellow]![/yellow] Requested video height up to {max_height}p, "
        f"but the DRM device reports {description}. Video above 1080p "
        "requires Widevine L1 or PlayReady SL3000; higher-resolution "
        "downloads may fail"
    )
