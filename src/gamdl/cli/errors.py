"""Human-friendly expected errors and end-of-run exception diagnostics."""

from __future__ import annotations

import traceback
from dataclasses import dataclass

import click
from rich.text import Text

from gamdl.api import (
    GamdlApiResponseError,
    GamdlAuthenticationError,
    GamdlRateLimitError,
    GamdlTransportError,
)
from gamdl.download import DownloadFinalizationError, TagResolutionError
from gamdl.drm import (
    DrmConfigurationError,
    DrmLicenseError,
    DrmMetadataError,
)
from gamdl.exceptions import GamdlError
from gamdl.media.exceptions import (
    MediaNotFoundError,
    MediaNotStreamableError,
    SelectionRequiredError,
    StreamManifestError,
    StreamNotAvailableError,
    UnsupportedMediaError,
)


@dataclass(frozen=True)
class FailureTraceback:
    """An exception traceback captured before its worker exits."""

    label: str
    traceback: str


def capture_failure_traceback(label: str, error: Exception) -> FailureTraceback:
    """Capture the full Python traceback while the exception is available."""
    return FailureTraceback(label, "".join(traceback.format_exception(error)))


def print_failure_tracebacks(output, failures: list[FailureTraceback]) -> None:
    """Show diagnostics after all Rich progress displays have closed."""
    if not failures:
        return
    output.print(f"[bold red]Exception tracebacks ({len(failures)}):[/bold red]")
    for failure in failures:
        output.print(Text(f"  ✗ {failure.label}"))
        output.print(Text(failure.traceback))
    output.print(
        "[yellow]Tracebacks may contain signed URLs, account details, or "
        "credentials. Review before sharing.[/yellow]"
    )


def short_error(error: Exception) -> tuple[str, bool]:
    if isinstance(error, SelectionRequiredError):
        return (
            "Interactive selection required (use --auto-select or run in a terminal)",
            True,
        )
    if isinstance(error, MediaNotStreamableError):
        return "Media is not streamable", True
    if isinstance(error, MediaNotFoundError):
        return "Media is unavailable in this storefront", True
    if isinstance(error, StreamNotAvailableError):
        # These resolver messages are static and safe to expose. Keep codec
        # failures generic rather than leaking stream URLs or manifest data.
        for prefix in (
            "No music-video HLS stream available",
            "No music-video HLS rendition matches",
            "Video rendition has no separate audio HLS track",
            "Library song has no playable catalog equivalent",
        ):
            if str(error).startswith(prefix):
                return str(error), True
        return "Requested codec or stream unavailable", True
    if isinstance(error, UnsupportedMediaError):
        return "Unsupported or unavailable media", True
    if isinstance(error, GamdlAuthenticationError):
        return "Authentication failed (check cookies or wrapper login)", True
    if isinstance(error, GamdlRateLimitError):
        return "Apple Music rate limit reached", True
    if isinstance(error, DrmLicenseError):
        return "Unable to acquire or process decryption key", False
    if isinstance(error, DrmConfigurationError):
        known_configuration_errors = (
            "Supply either a CDM or a device path",
            "PlayReady requires a device path or CDM",
            "Required DRM backend or default device is not available",
            "PlayReady backend not installed",
            "Widevine backend not installed",
        )
        known = str(error).startswith(known_configuration_errors)
        return str(error) if known else "DRM backend configuration failed", known
    if isinstance(error, DrmMetadataError):
        known = str(error) == "No usable DRM initialization data for selected backend"
        return str(error) if known else "DRM initialization metadata invalid", known
    if isinstance(error, GamdlApiResponseError):
        # HTTP errors have a clear cause; malformed success payloads and
        # unexpected response shapes are diagnostics, not expected skips.
        status = error.status_code
        if status is not None and status >= 400:
            return f"Apple Music or wrapper request failed (HTTP {status})", True
        return "Unexpected Apple Music or wrapper API response", False
    if isinstance(error, GamdlTransportError):
        return "Network request failed", True
    if isinstance(error, StreamManifestError):
        supported_limits = (
            "Only completed HLS media playlists are supported",
            "Selected HLS playlist contains no media segments",
            "AES-128 HLS transport decryption is not supported",
            "HLS discontinuities are not supported",
            "HLS gap segments are not supported",
        )
        known = str(error) in supported_limits
        return str(error) if known else "Invalid HLS manifest", known
    if isinstance(error, TagResolutionError):
        known = error.__cause__ is None
        return str(error) if known else "Media metadata could not be resolved", known
    if isinstance(error, DownloadFinalizationError):
        cause = error.__cause__
        if (
            isinstance(cause, GamdlApiResponseError)
            and cause.status_code is not None
            and cause.status_code >= 400
        ):
            reason = f"Apple Music request failed (HTTP {cause.status_code})"
        elif isinstance(cause, GamdlAuthenticationError):
            reason = "authentication failed"
        elif isinstance(cause, GamdlTransportError):
            reason = "network request failed"
        elif isinstance(cause, PermissionError):
            reason = "permission denied"
        elif isinstance(cause, FileNotFoundError):
            reason = "file or directory not found"
        elif isinstance(cause, OSError):
            reason = "file I/O error"
        else:
            reason = "unexpected error"
        actions = {
            "lyrics": "save lyrics",
            "cover": "save artwork",
            "playlist": "update playlist",
            "database": "update database",
        }
        return (
            f"Media saved, but couldn't {actions.get(error.stage, 'finish media')}: {reason}",
            reason != "unexpected error",
        )
    if isinstance(error, FileExistsError):
        return "Output already exists", True
    if isinstance(error, GamdlError):
        return type(error).__name__.removeprefix("Gamdl").replace(
            "Error", " failed"
        ), False
    if isinstance(error, click.ClickException):
        return error.format_message(), True
    return "Unexpected download error", False


class CliCommand(click.Group):
    """Use a consistent Rich error renderer for Click parsing/usage errors."""

    def main(self, *args, **kwargs):
        standalone = kwargs.pop("standalone_mode", True)
        from .output import banner

        banner()
        try:
            return super().main(*args, standalone_mode=False, **kwargs)
        except click.ClickException as exc:
            if not standalone:
                raise
            render_click_error(exc)
            raise SystemExit(exc.exit_code) from None
        except click.Abort:
            if not standalone:
                raise
            from .output import error

            error("Aborted")
            raise SystemExit(1) from None


def render_click_error(exc: click.ClickException) -> None:
    from .output import error

    error(exc.format_message())
    if isinstance(exc, click.UsageError):
        error("Run 'gamdl rip --help' to see available options.")
