"""Questionary-driven terminal prompts for streams, credentials and device files."""

from __future__ import annotations

import sys
from pathlib import Path

import click
import questionary

from gamdl.media.music_video.models import AudioSelectionRequest, VideoSelectionRequest
from gamdl.media.song.models import StreamSelectionRequest, StreamSourceSelectionRequest
from gamdl.media.uploaded_video.stream import UploadedVideoSelectionRequest


PROMPT_STYLE = questionary.Style(
    [
        ("qmark", "fg:#69b5ff bold"),
        ("question", "bold"),
        ("pointer", "fg:#63d89c bold"),
        ("highlighted", "fg:#63d89c bold"),
        ("selected", "fg:#63d89c"),
    ]
)


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def require_terminal(description: str) -> None:
    if not interactive():
        raise click.ClickException(f"{description} requires an interactive terminal")


def _value(value: object) -> str:
    return str(getattr(value, "value", value))


def stream_choices(request) -> tuple[str, list[questionary.Choice]]:
    """Render all supported selection request types without exposing stream URLs."""
    if isinstance(request, StreamSourceSelectionRequest):
        return "Select song stream source:", [
            questionary.Choice(_value(item.origin), value=item.index)
            for item in request.options
        ]
    if isinstance(request, StreamSelectionRequest):
        choices = []
        for item in request.options:
            parts = [_value(item.codec)]
            if item.bitrate:
                parts.append(f"{item.bitrate // 1000} kbps")
            if item.hls_type:
                parts.append(_value(item.hls_type))
            choices.append(questionary.Choice(" · ".join(parts), value=item.index))
        return "Select song codec:", choices
    if isinstance(request, VideoSelectionRequest):
        choices = []
        for item in request.options:
            parts = [_value(item.codec)]
            if item.width and item.height:
                parts.append(f"{item.width}×{item.height}")
            if item.frame_rate:
                parts.append(f"{item.frame_rate:g} fps")
            if item.bitrate:
                parts.append(f"{item.bitrate // 1000} kbps")
            choices.append(questionary.Choice(" · ".join(parts), value=item.index))
        return "Select music-video codec:", choices
    if isinstance(request, AudioSelectionRequest):
        choices = []
        for item in request.options:
            parts = [_value(item.codec)]
            if item.name:
                parts.append(item.name)
            if item.language:
                parts.append(item.language)
            if item.bitrate:
                parts.append(f"{item.bitrate // 1000} kbps")
            choices.append(questionary.Choice(" · ".join(parts), value=item.index))
        return "Select music-video audio:", choices
    if isinstance(request, UploadedVideoSelectionRequest):
        return "Select uploaded-video quality:", [
            questionary.Choice(_value(item.quality), value=item.index)
            for item in request.options
        ]
    raise TypeError("Unsupported stream selection request")


async def select_stream(request) -> int:
    require_terminal("Stream selection")
    question, choices = stream_choices(request)
    if not choices:
        raise click.ClickException("No selectable streams are available")
    answer = await questionary.select(
        question,
        choices=choices,
        use_arrow_keys=True,
        instruction="(↑/↓ to navigate, Enter to select)",
        style=PROMPT_STYLE,
    ).ask_async()
    if answer is None:
        raise click.Abort()
    return answer


async def ask_text(message: str, *, password: bool = False) -> str:
    require_terminal(message)
    prompt = (
        questionary.password(message, style=PROMPT_STYLE)
        if password
        else questionary.text(message, style=PROMPT_STYLE)
    )
    answer = await prompt.ask_async()
    if answer is None:
        raise click.Abort()
    return answer


async def ask_file(
    path: str | None, *, label: str, extension: str | None = None
) -> Path:
    """Prompt until a readable file exists; fail immediately in pipelines/CI."""
    candidate = Path(path).expanduser() if path else None
    while (
        candidate is None
        or not candidate.is_file()
        or (extension and candidate.suffix.lower() != extension)
    ):
        if not interactive():
            raise click.ClickException(
                f"{label} file missing or invalid; supply a valid path via "
                "--cookies-path/--device-path or YAML configuration"
            )
        suggestion = str(candidate) if candidate else ""
        answer = await questionary.text(
            f"{label} file missing. Enter a path or drag the file here:",
            default=suggestion,
            style=PROMPT_STYLE,
        ).ask_async()
        if answer is None:
            raise click.Abort()
        # Shells often quote dragged-in paths.
        value = answer.strip().strip("\"'")
        candidate = Path(value).expanduser() if value else None
    return candidate
