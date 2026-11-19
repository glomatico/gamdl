"""Expand local URL lists passed to `gamdl rip` before authentication."""

from pathlib import Path

import click


def _is_media_input(value: str) -> bool:
    return value.lower().startswith(("https://", "http://", "isrc:"))


def expand_rip_inputs(inputs: tuple[str, ...]) -> tuple[str, ...]:
    """Expand top-level file paths into URL/ISRC inputs, without recursion."""
    expanded: list[str] = []
    for source in inputs:
        if _is_media_input(source):
            expanded.append(source)
            continue

        path = Path(source).expanduser()
        if not path.is_file():
            raise click.UsageError(
                f"Invalid URL or file containing URLs not found: {source}"
            )
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeError):
            raise click.UsageError(f"Could not read URL file: {source}") from None

        found = 0
        for line_number, raw_line in enumerate(lines, 1):
            value = raw_line.strip()
            if not value or value.startswith("#"):
                continue
            if not _is_media_input(value):
                raise click.UsageError(
                    f"Invalid URL or ISRC in {source} at line {line_number}"
                )
            expanded.append(value)
            found += 1

        if not found:
            raise click.UsageError(f"No URLs or ISRCs found in file: {source}")

    return tuple(expanded)
