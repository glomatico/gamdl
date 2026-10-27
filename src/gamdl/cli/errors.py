"""Human-friendly errors without printing tokens, signed URLs or tracebacks."""

import click

from gamdl.api import (
    GamdlAuthenticationError,
    GamdlApiResponseError,
    GamdlRateLimitError,
    GamdlTransportError,
)
from gamdl.download import DownloadFinalizationError
from gamdl.drm import DrmLicenseError
from gamdl.exceptions import GamdlError
from gamdl.media.exceptions import StreamNotAvailableError, UnsupportedMediaError


def short_error(error: Exception) -> tuple[str, bool]:
    if isinstance(error, StreamNotAvailableError):
        return "Requested codec or stream unavailable", True
    if isinstance(error, UnsupportedMediaError):
        return "Unsupported or unavailable media", True
    if isinstance(error, GamdlAuthenticationError):
        return "Authentication failed (check cookies or wrapper login)", True
    if isinstance(error, GamdlRateLimitError):
        return "Apple Music rate limit reached", True
    if isinstance(error, DrmLicenseError):
        return "Unable to acquire decryption key", True
    if isinstance(error, GamdlTransportError):
        return "Network request failed", True
    if isinstance(error, DownloadFinalizationError):
        cause = error.__cause__
        if isinstance(cause, GamdlApiResponseError) and cause.status_code is not None:
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
            True,
        )
    if isinstance(error, FileExistsError):
        return "Output already exists", True
    if isinstance(error, GamdlError):
        return type(error).__name__.removeprefix("Gamdl").replace(
            "Error", " failed"
        ), True
    if isinstance(error, click.ClickException):
        return error.format_message(), True
    return "Unexpected download error", False


class CliCommand(click.Group):
    """Use a consistent Rich error renderer for Click parsing/usage errors."""

    def main(self, *args, **kwargs):
        standalone = kwargs.pop("standalone_mode", True)
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
