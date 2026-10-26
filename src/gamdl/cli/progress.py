"""Bounded Rich progress UI fed exclusively by typed Downloader events."""

from __future__ import annotations

from collections import deque

from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)

from gamdl.download import DownloadEvent, DownloadStage, EventState


class RipProgress:
    """A growing active-track display that doesn't retain every finished row."""

    def __init__(
        self, *, enabled: bool, max_visible: int = 5, console: Console | None = None
    ):
        self.console = console or Console()
        self.enabled = enabled
        self.max_visible = max_visible
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=self.console,
            transient=False,
            disable=not enabled,
            expand=False,
        )
        self._rows: dict[int, int] = {}
        self._finished: deque[int] = deque()
        self._transfer: dict[int, dict[str, float]] = {}
        self._last_percent: dict[int, float] = {}
        self._discovery: int | None = None

    def __enter__(self):
        self.progress.start()
        if self.enabled:
            self._discovery = self.progress.add_task("Discovering tracks", total=None)
        return self

    def __exit__(self, *_exc):
        self.progress.stop()

    def discovered(self, count: int):
        if self._discovery is not None:
            self.progress.update(
                self._discovery, description=f"Discovered {count} tracks"
            )

    def discovery_finished(self):
        if self._discovery is not None:
            self.progress.remove_task(self._discovery)
            self._discovery = None

    def add(self, occurrence: int, title: str):
        if self.enabled:
            self._rows[occurrence] = self.progress.add_task(
                f"[cyan]{escape(title)}[/cyan] · queued", total=None
            )

    def event(self, occurrence: int, title: str, event: DownloadEvent):
        if not self.enabled or occurrence not in self._rows:
            return
        task = self._rows[occurrence]
        if event.stage == DownloadStage.COMPLETED:
            return
        if event.state == EventState.PROGRESS and event.progress is not None:
            self._transfer.setdefault(occurrence, {})[event.track or "audio"] = (
                event.progress.percentage
            )
            # Video/audio are separate transfers; show a representative
            # average of the observed tracks (never invent a byte total).
            percent = sum(self._transfer[occurrence].values()) / len(
                self._transfer[occurrence]
            )
            self._last_percent[occurrence] = percent
            self.progress.update(task, total=100, completed=percent)
        elif event.state == EventState.STARTED:
            stage = event.stage.value.replace("_", " ")
            label = f"[cyan]{escape(title)}[/cyan] · {stage}"
            if event.track:
                label += f" ({event.track})"
            self.progress.update(task, description=label)

    def finish(
        self, occurrence: int, title: str, status: str, message: str | None = None
    ):
        if self.enabled and occurrence in self._rows:
            task = self._rows[occurrence]
            if status == "failed":
                percent = self._last_percent.get(occurrence)
                self.progress.update(
                    task,
                    description=f"[red]✗ {escape(title)} · {escape(message or 'failed')}[/red]",
                    total=100 if percent is not None else None,
                    completed=percent or 0,
                )
            else:
                mark, color = ("↷", "yellow") if status == "skipped" else ("✓", "green")
                self.progress.update(
                    task,
                    description=f"[{color}]{mark} {escape(title)} · {status}[/{color}]",
                    total=100,
                    completed=100,
                    visible=True,
                )
            self._finished.append(occurrence)
            while len(self._finished) > self.max_visible:
                previous = self._finished.popleft()
                old_task = self._rows.pop(previous, None)
                if old_task is not None:
                    self.progress.remove_task(old_task)
                self._transfer.pop(previous, None)
                self._last_percent.pop(previous, None)
        if not self.enabled:
            symbol = "✗" if status == "failed" else "↷" if status == "skipped" else "✓"
            self.console.print(f"{symbol} {escape(title)}: {escape(message or status)}")
        elif status == "failed":
            # Persistent log line survives removal of old failed progress rows.
            self.console.print(
                f"[red]✗ {escape(title)}: {escape(message or 'failed')}[/red]"
            )
