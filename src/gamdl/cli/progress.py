"""Bounded Rich progress UI fed exclusively by typed Downloader events."""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from os import PathLike, fspath

from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)
from rich.table import Column, Table
from rich.text import Text

from gamdl.download import DownloadEvent, DownloadStage, EventState


def _single_line(value: str) -> str:
    """Keep metadata and paths from adding terminal rows or control sequences."""
    return "".join(
        " " if ord(char) < 32 or ord(char) == 127 else char for char in value
    )


class _ActiveSpinnerColumn(SpinnerColumn):
    """A failed/stopped task must not keep spinning in the completed rows."""

    def render(self, task):
        if task.stop_time is not None:
            return Text(" ")
        return super().render(task)


class _TerminalProgress(Progress):
    """Keep frames within the current terminal size, including the final refresh."""

    def __init__(self, *columns, max_visible: int, **kwargs):
        self.max_visible = max_visible
        self._terminal_console = kwargs["console"]
        self._used_live = False
        super().__init__(*columns, **kwargs)

    def start(self) -> None:
        if self.disable or self.live.is_started:
            return
        if self._used_live:
            # A stopped Live retains its previous cursor geometry and changes
            # overflow to 'visible'. Reusing it can erase permanent history.
            self.live = Live(
                console=self.console,
                get_renderable=self.get_renderable,
                transient=True,
                refresh_per_second=8,
                vertical_overflow="ellipsis",
            )
        self._used_live = True
        super().start()

    def fit_rows(self):
        with self._lock:
            tasks = self.tasks
            discovery = [t for t in tasks if t.fields.get("discovery")]
            active = [
                t
                for t in tasks
                if not t.fields.get("discovery") and t.stop_time is None
            ]
            finished = [
                t
                for t in tasks
                if t.stop_time is not None and t.fields.get("outcome") == "failed"
            ]
            budget = max(1, self._terminal_console.size.height - 2)
            limit = min(
                self.max_visible, max(1, self._terminal_console.size.height - 5)
            )
            # Keep the oldest active jobs visible instead of replacing them
            # with every newly queued item. Hidden jobs remain in the count.
            selected = (discovery + active + finished[-limit:])[:limit]
            hidden = len(active) - sum(t in selected for t in active)
            if hidden and len(selected) >= budget:
                selected = selected[: max(0, budget - 1)]
                hidden = len(active) - sum(t in selected for t in active)
            visible = {t.id for t in selected}
            for task in tasks:
                task.visible = task.id in visible
            return selected, hidden

    def get_renderables(self):
        # Rich refreshes on a separate thread. Derive layout from its locked
        # task snapshot, not from the runner's mutable occurrence dictionaries.
        with self._lock:
            tasks, hidden = self.fit_rows()
            columns = [
                column
                for column in self.columns
                if self._terminal_console.size.width >= 48
                or not isinstance(column, BarColumn)
            ]
            table = Table.grid(
                *(column.get_table_column().copy() for column in columns),
                padding=(0, 1),
                expand=True,
            )
            for task in tasks:
                table.add_row(*(column(task) for column in columns))
            yield table
            if hidden:
                yield Text(
                    f"+ {hidden} other active / queued tracks",
                    style="dim",
                    no_wrap=True,
                    overflow="ellipsis",
                )


class RipProgress:
    """A growing active-track display that doesn't retain every finished row."""

    def __init__(
        self, *, enabled: bool, max_visible: int = 5, console: Console | None = None
    ):
        self.console = console or Console()
        self.enabled = enabled
        self.max_visible = max_visible
        self.progress = _TerminalProgress(
            _ActiveSpinnerColumn(),
            TextColumn(
                "{task.description}",
                table_column=Column(ratio=1, no_wrap=True, overflow="ellipsis"),
            ),
            BarColumn(bar_width=20),
            TaskProgressColumn(),
            max_visible=max_visible,
            console=self.console,
            # A permanent Rich frame duplicates the final summary and leaves
            # old spinners in scrollback on both success and failure.
            transient=True,
            disable=not enabled,
            expand=False,
            refresh_per_second=8,
        )
        self._rows: dict[int, int] = {}
        self._finished: deque[int] = deque()
        self._transfer: dict[int, dict[str, float]] = {}
        self._last_percent: dict[int, float] = {}
        self._discovery: int | None = None
        self._batch_task: int | None = None
        self._batch_total = 0
        self._batch_finished = 0
        self._pending_messages: list[str] = []
        self._completed: set[int] = set()
        self._closed = False
        # A full-screen selector may open several prompts (source, codec,
        # video, audio). Do not restart Rich between those screens.
        self._prompt_hidden = False

    def __enter__(self):
        self._closed = False
        self._restore_overflow()
        self.progress.start()
        if self.enabled:
            self._discovery = self.progress.add_task(
                "Discovering tracks", total=None, discovery=True
            )
        return self

    def __exit__(self, *_exc):
        self._closed = True
        self.progress.stop()
        self._prompt_hidden = False
        self._flush_messages()

    def _restore_overflow(self) -> None:
        self.progress.live.vertical_overflow = "ellipsis"

    def _flush_messages(self) -> None:
        if self._prompt_hidden:
            return
        for line in self._pending_messages:
            self.console.print(line)
        self._pending_messages.clear()

    def hide_for_prompt(self) -> None:
        """Hide Rich without leaving a permanent frame in terminal scrollback."""
        if self._prompt_hidden:
            return
        self._prompt_hidden = True
        if not self.enabled:
            return
        # Always erase the current Live frame before PTK uses alt-screen.
        live = self.progress.live
        original_transient = live.transient
        live.transient = True
        try:
            self.progress.stop()
        finally:
            live.transient = original_transient

    def resume_after_prompt(self) -> None:
        """Resume only after the entire stream-selection stage has finished."""
        if not self._prompt_hidden:
            return
        self._prompt_hidden = False
        if self.enabled and not self._closed:
            self._restore_overflow()
            self.progress.start()
        self._flush_messages()

    @contextmanager
    def suspend(self):
        """Temporarily hand the terminal to another UI, including on errors."""
        self.hide_for_prompt()
        try:
            yield
        finally:
            self.resume_after_prompt()

    def discovered(self, count: int):
        if self._discovery is not None:
            self.progress.update(
                self._discovery,
                description=f"Discovered {count} track{'s' if count != 1 else ''}",
            )

    def discovery_finished(self, count: int = 0):
        if self._discovery is not None:
            self.progress.remove_task(self._discovery)
            self._discovery = None
        self._fit_rows()

    def start_batch(self, total: int) -> None:
        """Show an overall completed/total counter for a known download batch.

        Used by sync after playlist discovery; rip still supports streaming
        discovery where the eventual count is not known upfront.
        """
        self.discovery_finished()
        self._batch_total = total
        self._batch_finished = 0
        if self.enabled:
            self._batch_task = self.progress.add_task(
                f"Downloading 0/{total} tracks", total=total, discovery=True
            )
            self._fit_rows()

    def add(self, occurrence: int, title: str, *, tracks: tuple[str, ...] = ("audio",)):
        if self.enabled:
            self._transfer[occurrence] = dict.fromkeys(tracks, 0.0)
            self._rows[occurrence] = self.progress.add_task(
                f"[cyan]{escape(self._display_title(title))}[/cyan] · queued",
                total=None,
            )
            self._fit_rows()

    def _display_title(self, title: str) -> str:
        # Leave space for status, progress bar and percentage on narrow TTYs.
        title = _single_line(title)
        limit = max(12, min(72, self.console.size.width - 40))
        return title if len(title) <= limit else title[: limit - 1] + "…"

    def _fit_rows(self) -> None:
        if not self.enabled:
            return
        self.progress.fit_rows()

    def event(self, occurrence: int, title: str, event: DownloadEvent):
        if (
            not self.enabled
            or occurrence not in self._rows
            or occurrence in self._completed
        ):
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
            percent = max(self._last_percent.get(occurrence, 0), percent)
            self._last_percent[occurrence] = percent
            self.progress.update(task, total=100, completed=percent)
        elif event.state == EventState.STARTED:
            stage = event.stage.value.replace("_", " ")
            label = f"[cyan]{escape(self._display_title(title))}[/cyan] · {stage}"
            if event.track:
                label += f" ({event.track})"
            self.progress.update(task, description=label)

    def finish(
        self,
        occurrence: int,
        title: str,
        status: str,
        message: str | None = None,
        *,
        path: str | PathLike[str] | None = None,
    ):
        if occurrence in self._completed:
            return
        self._completed.add(occurrence)
        if self._batch_task is not None:
            self._batch_finished += 1
            self.progress.update(
                self._batch_task,
                description=f"Downloading {self._batch_finished}/{self._batch_total} tracks",
                advance=1,
            )
        if self.enabled and occurrence in self._rows:
            task = self._rows[occurrence]
            if status in {"failed", "partial"}:
                percent = self._last_percent.get(occurrence)
                color = "yellow" if status == "partial" else "red"
                mark = "!" if status == "partial" else "✗"
                self.progress.update(
                    task,
                    description=(
                        f"[{color}]{mark} {escape(self._display_title(title))} · "
                        f"{escape(_single_line(message or 'failed'))}[/{color}]"
                    ),
                    total=100 if status == "partial" or percent is not None else None,
                    completed=100 if status == "partial" else percent or 0,
                )
            else:
                mark, color = ("↷", "yellow") if status == "skipped" else ("✓", "green")
                self.progress.update(
                    task,
                    description=(
                        f"[{color}]{mark} {escape(self._display_title(title))} "
                        f"· {status}[/{color}]"
                    ),
                    # Skipped files weren't transferred: don't display 100%
                    # as if bytes had been downloaded.
                    total=None if status == "skipped" else 100,
                    completed=0 if status == "skipped" else 100,
                    visible=True,
                )
            self.progress.stop_task(task)
            self.progress.update(task, outcome=status)
            self._finished.append(occurrence)
            while len(self._finished) > self.max_visible:
                previous = self._finished.popleft()
                old_task = self._rows.pop(previous, None)
                if old_task is not None:
                    self.progress.remove_task(old_task)
                self._transfer.pop(previous, None)
                self._last_percent.pop(previous, None)
            self._fit_rows()
        # Live rows are deliberately transient. Record successful outcomes in
        # scrollback once, with full titles/paths, even when progress is on.
        # Failure details have one authoritative report in the runner.
        if status != "failed":
            symbol = "↷" if status == "skipped" else "✓"
            outcome = (
                "media saved (finalization incomplete)"
                if status == "partial"
                else status
            )
            line = f"{symbol} {escape(_single_line(title))}: {escape(outcome)}"
            if status == "skipped" and message:
                line += f" ({escape(_single_line(message))})"
            if path is not None:
                line += f" → {escape(_single_line(fspath(path)))}"
            if self._prompt_hidden:
                self._pending_messages.append(line)
            else:
                self.console.print(line)
