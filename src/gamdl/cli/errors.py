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
from gamdl.download import DownloadFinalizationError
from gamdl.drm import DrmLicenseError
from gamdl.exceptions import GamdlError
from gamdl.media.exceptions import (
    MediaNotFoundError,
    MediaNotStreamableError,
    SelectionRequiredError,
    StreamNotAvailableError,
    UnsupportedMediaError,
)


@dataclass(frozen=True)
class FailureTraceback:
    """An exception traceback captured before its worker exits."""

    label: str
    traceback: str


def capture_failure_traceback(
    label: str, error: Exception, *, debug: bool = False
) -> FailureTraceback:
    """Capture stack locations without leaking exception text by default.

    Raw exception messages may contain request URLs, tokens, or remote payloads.
    Source-code lines are omitted for the same reason. --debug opts into the
    original, complete Python traceback when deeper diagnostics are needed.
    """
    if debug:
        return FailureTraceback(label, "".join(traceback.format_exception(error)))
    lines: list[str] = []
    seen: set[int] = set()

    def add_exception(current: BaseException) -> None:
        if id(current) in seen:
            return
        seen.add(id(current))
        cause = current.__cause__
        context = current.__context__ if not current.__suppress_context__ else None
        chained = cause or context
        if chained is not None:
            add_exception(chained)
            lines.append(
                "\nThe above exception was the direct cause of the following exception:\n\n"
                if cause is not None
                else "\nDuring handling of the above exception, another exception occurred:\n\n"
            )
        lines.append("Traceback (most recent call last):\n")
        for frame in traceback.extract_tb(current.__traceback__):
            lines.append(
                f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}\n'
            )
        lines.append(f"{type(current).__name__}: [details hidden; use --debug]\n")

    add_exception(error)
    return FailureTraceback(label, "".join(lines))


def print_failure_tracebacks(output, failures: list[FailureTraceback]) -> None:
    """Show diagnostics after all Rich progress displays have closed."""
    if not failures:
        return
    output.print(f"[bold red]Exception tracebacks ({len(failures)}):[/bold red]")
    for failure in failures:
        output.print(Text(f"  ✗ {failure.label}"))
        output.print(Text(failure.traceback))
    output.print(
        "[dim]Tracebacks contain file paths. --debug includes raw exception "
        "details, which may contain signed URLs or account information.[/dim]"
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
            reason != "unexpected error",
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
