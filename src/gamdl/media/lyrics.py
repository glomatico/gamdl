"""Structured TTML lyrics with line/stanza and syllable timing."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from xml.etree import ElementTree

from .exceptions import GamdlMediaError


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _milliseconds(value: str | None) -> int | None:
    if not value:
        return None
    try:
        if value.endswith("ms"):
            seconds = Decimal(value[:-2]) / 1000
        elif value.endswith("s"):
            seconds = Decimal(value[:-1])
        else:
            pieces = value.split(":")
            if len(pieces) > 3:
                raise ValueError
            seconds = sum(
                Decimal(piece) * Decimal(60**index)
                for index, piece in enumerate(reversed(pieces))
            )
        if seconds < 0:
            raise ValueError
        return int((seconds * 1000).to_integral_value(rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError) as exc:
        raise GamdlMediaError("Invalid TTML timestamp") from exc


def _lrc_stamp(milliseconds: int) -> str:
    total_centiseconds = (milliseconds + 5) // 10
    minutes, remainder = divmod(total_centiseconds, 6000)
    seconds, centiseconds = divmod(remainder, 100)
    return f"{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def _srt_stamp(milliseconds: int) -> str:
    hours, remaining = divmod(milliseconds, 3_600_000)
    minutes, remaining = divmod(remaining, 60_000)
    seconds, ms = divmod(remaining, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"


@dataclass(frozen=True)
class LyricsSegment:
    """One word/syllable within a lyric line, timed in milliseconds."""

    text: str
    start: int | None = None
    end: int | None = None


@dataclass(frozen=True)
class LyricsLine:
    """One lyric line, with optional line and per-segment timing."""

    text: str
    start: int | None = None
    end: int | None = None
    segments: tuple[LyricsSegment, ...] = ()


@dataclass
class Lyrics:
    """Lyrics grouped into stanzas, retaining the source TTML losslessly.

    Conversion methods never contact the Apple Music API. Timing is in
    integer milliseconds; enhanced LRC uses timed word/syllable segments.
    """

    stanzas: list[list[LyricsLine]] = field(default_factory=list)
    source_ttml: str | None = None
    language: str | None = None

    @property
    def lines(self) -> list[LyricsLine]:
        """Flatten all stanzas without changing their stored grouping."""
        return [line for stanza in self.stanzas for line in stanza]

    @property
    def is_synced(self) -> bool:
        return any(line.start is not None for line in self.lines)

    @property
    def is_syllable_synced(self) -> bool:
        return any(
            segment.start is not None
            for line in self.lines
            for segment in line.segments
        )

    @classmethod
    def from_ttml(cls, xml: str) -> Lyrics:
        """Parse TTML paragraphs as lines and TTML divs as stanzas."""
        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError as exc:
            raise GamdlMediaError("Invalid lyrics TTML document") from exc
        if _local(root.tag) != "tt":
            raise GamdlMediaError("Expected a TTML document")

        def parse_line(p: ElementTree.Element) -> LyricsLine:
            text = "".join(p.itertext()).strip()
            segments = tuple(
                LyricsSegment(
                    text="".join(span.itertext()) + (span.tail or ""),
                    start=_milliseconds(span.get("begin")),
                    end=_milliseconds(span.get("end")),
                )
                for span in p.iter()
                if _local(span.tag) == "span"
            )
            start = _milliseconds(p.get("begin"))
            end = _milliseconds(p.get("end"))
            if start is None:
                start = next((s.start for s in segments if s.start is not None), None)
            if end is None:
                end = next(
                    (s.end for s in reversed(segments) if s.end is not None), None
                )
            return LyricsLine(text=text, start=start, end=end, segments=segments)

        stanzas = [
            [parse_line(p) for p in div if _local(p.tag) == "p"]
            for div in root.iter()
            if _local(div.tag) == "div"
        ]
        stanzas = [stanza for stanza in stanzas if stanza]
        if not stanzas:
            lines = [parse_line(p) for p in root.iter() if _local(p.tag) == "p"]
            if lines:
                stanzas = [lines]
        lang = root.get("{http://www.w3.org/XML/1998/namespace}lang")
        return cls(stanzas=stanzas, source_ttml=xml, language=lang)

    def to_plain_text(self) -> str:
        """Preserve stanza breaks while discarding all timing."""
        return "\n\n".join(
            "\n".join(line.text for line in stanza) for stanza in self.stanzas
        )

    def to_lrc(self) -> str:
        """Return traditional line-timed LRC (or untimed text where necessary)."""
        return "\n".join(
            (f"[{_lrc_stamp(line.start)}]" if line.start is not None else "")
            + line.text
            for line in self.lines
        )

    def to_enhanced_lrc(self) -> str:
        """Preserve segment timing when available, else use line timing."""
        values: list[str] = []
        for line in self.lines:
            prefix = f"[{_lrc_stamp(line.start)}]" if line.start is not None else ""
            if (
                self.is_syllable_synced
                and line.segments
                and "".join(s.text for s in line.segments).strip() == line.text
            ):
                # Some TTML mixes spans and additional text outside spans.
                # Never drop that text just to retain partial segment timing.
                content = "".join(
                    (f"<{_lrc_stamp(s.start)}>" if s.start is not None else "") + s.text
                    for s in line.segments
                )
                values.append(prefix + content)
            else:
                values.append(prefix + line.text)
        return "\n".join(values)

    def to_srt(self) -> str:
        """Convert fully timed lines to SRT without inventing missing end times."""
        timed = [
            line
            for line in self.lines
            if line.start is not None and line.end is not None
        ]
        return "\n\n".join(
            f"{index}\n{_srt_stamp(line.start)} --> {_srt_stamp(line.end)}\n{line.text}"
            for index, line in enumerate(timed, start=1)
        )

    def to_ttml(self) -> str:
        """Return the original TTML, preserving unsupported markup and timing."""
        if self.source_ttml is None:
            raise GamdlMediaError("No original TTML is available for lossless export")
        return self.source_ttml
