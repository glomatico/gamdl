"""Full-screen single-choice selector for audio/video codecs and sources."""

from __future__ import annotations

import click
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings

from gamdl.media.music_video.models import AudioSelectionRequest, VideoSelectionRequest
from gamdl.media.song.models import StreamSelectionRequest, StreamSourceSelectionRequest
from gamdl.media.uploaded_video.stream import UploadedVideoSelectionRequest

from .app import make_app
from .components import MenuChoice, footer, heading


def _value(value: object) -> str:
    return str(getattr(value, "value", value))


def stream_choices(request) -> tuple[str, list[MenuChoice]]:
    """Represent stream candidates without exposing signed stream URLs."""
    if isinstance(request, StreamSourceSelectionRequest):
        return "Song source", [
            MenuChoice(_value(item.origin), item.index) for item in request.options
        ]
    if isinstance(request, StreamSelectionRequest):
        choices = []
        for item in request.options:
            details = []
            if item.bitrate:
                details.append(f"{item.bitrate // 1000} kbps")
            if item.hls_type:
                details.append(_value(item.hls_type))
            choices.append(
                MenuChoice(_value(item.codec), item.index, " · ".join(details))
            )
        return "Song codec", choices
    if isinstance(request, VideoSelectionRequest):
        choices = []
        for item in request.options:
            details = []
            if item.width and item.height:
                details.append(f"{item.width}×{item.height}")
            if item.frame_rate:
                details.append(f"{item.frame_rate:g} fps")
            if item.bitrate:
                details.append(f"{item.bitrate // 1000} kbps")
            choices.append(
                MenuChoice(_value(item.codec), item.index, " · ".join(details))
            )
        return "Music-video codec", choices
    if isinstance(request, AudioSelectionRequest):
        choices = []
        for item in request.options:
            details = []
            if item.name:
                details.append(item.name)
            if item.language:
                details.append(item.language)
            if item.bitrate:
                details.append(f"{item.bitrate // 1000} kbps")
            choices.append(
                MenuChoice(_value(item.codec), item.index, " · ".join(details))
            )
        return "Music-video audio", choices
    if isinstance(request, UploadedVideoSelectionRequest):
        return "Uploaded-video quality", [
            MenuChoice(_value(item.quality), item.index) for item in request.options
        ]
    raise TypeError("Unsupported stream selection request")


class SingleChoiceState:
    def __init__(self, choices: list[MenuChoice]):
        self.choices = choices
        self.cursor = 0

    def move(self, step: int):
        self.cursor = max(0, min(len(self.choices) - 1, self.cursor + step))

    @property
    def selected(self) -> int:
        return self.choices[self.cursor].value


async def select_one(
    title: str,
    choices: list[MenuChoice],
    *,
    subtitle: str | None = None,
    input=None,
    output=None,
) -> int:
    """Use the alternate screen to preserve Rich progress and shell history."""
    if not choices:
        raise click.ClickException("No selectable streams are available")
    state = SingleChoiceState(choices)
    keys = KeyBindings()

    def body() -> FormattedText:
        fragments = heading(
            "GAMDL 4.0", title if not subtitle else f"{title} · {subtitle}"
        )
        for i, choice in enumerate(choices):
            label = choice.title
            if choice.detail:
                label += f"   ·   {choice.detail}"
            fragments.append(
                (
                    "class:focused" if i == state.cursor else "",
                    f" {'❯' if i == state.cursor else ' '} {label}\n",
                )
            )
        fragments.append(("class:muted", "\n"))
        current = choices[state.cursor]
        fragments.append(("class:detail", f" Selected: {current.title}"))
        return FormattedText(fragments)

    app = make_app(
        body=body,
        footer=lambda: footer(
            f"{len(choices)} available options",
            "↑/↓ Navigate  ·  Enter Select  ·  Esc/Ctrl+C Cancel",
        ),
        cursor_row=lambda: state.cursor + 2,
        keys=keys,
        input=input,
        output=output,
    )

    @keys.add("up")
    @keys.add("k")
    def up(event):
        state.move(-1)

    @keys.add("down")
    @keys.add("j")
    def down(event):
        state.move(1)

    @keys.add("home")
    def home(event):
        state.move(-len(choices))

    @keys.add("end")
    def end(event):
        state.move(len(choices))

    @keys.add("pageup")
    def pageup(event):
        state.move(-10)

    @keys.add("pagedown")
    def pagedown(event):
        state.move(10)

    @keys.add("enter")
    def confirm(event):
        app.exit(result=state.selected)

    @keys.add("escape")
    @keys.add("c-c")
    def cancel(event):
        app.exit(result=None)

    answer = await app.run_async()
    if answer is None:
        raise click.Abort()
    return answer


async def select_stream(
    request, *, title: str | None = None, input=None, output=None
) -> int:
    """Select a typed stream candidate using the shared full-screen selector."""
    name, choices = stream_choices(request)
    return await select_one(name, choices, subtitle=title, input=input, output=output)
