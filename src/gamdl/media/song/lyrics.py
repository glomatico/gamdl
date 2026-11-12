"""Structured TTML lyrics with line/stanza and syllable timing."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from xml.etree import ElementTree

from ..exceptions import GamdlMediaError


class LyricsFormat(str, Enum):
    """Supported sidecar and embedded lyrics representations."""

    LRC = "lrc"
    SRT = "srt"
    TTML = "ttml"
    TEXT = "text"
    ENHANCED_LRC = "enhanced-lrc"

    @property
    def extension(self) -> str:
        return {
            LyricsFormat.LRC: ".lrc",
            LyricsFormat.SRT: ".srt",
            LyricsFormat.TTML: ".ttml",
            LyricsFormat.TEXT: ".txt",
            LyricsFormat.ENHANCED_LRC: ".lrc",
        }[self]


class LyricsFormatUnavailableError(GamdlMediaError):
    """Requested lyrics representation cannot be produced from this source."""


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

    Conversion methods never contact the Apple Music API. Timing is in integer
    milliseconds. When ``syllable=True``, enhanced A2 LRC can retain
    per-syllable timing, while SRT exports only complete line-level cues.
    """

    stanzas: list[list[LyricsLine]] = field(default_factory=list)
    source_ttml: str | None = None
    language: str | None = None
    syllable: bool = False

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
    def from_ttml(cls, xml: str, *, syllable: bool = False) -> Lyrics:
        """Parse TTML paragraphs as lines and TTML divs as stanzas.

        Args:
            xml: Raw TTML document, preserved for lossless export.
            syllable: Enable enhanced LRC for direct ``to_lrc()`` calls.
                Set when the source is Apple's syllable-lyrics endpoint.
        """
        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError as exc:
            raise GamdlMediaError("Invalid lyrics TTML document") from exc
        if _local(root.tag) != "tt":
            raise GamdlMediaError("Expected a TTML document")

        def parse_line(p: ElementTree.Element) -> LyricsLine:
            text = "".join(p.itertext()).strip()
            parts: list[LyricsSegment] = []

            def collect_parts(element: ElementTree.Element) -> None:
                # Traverse text nodes once, not span.itertext() for each span.
                # Apple can nest timed syllables in an untimed role="x-bg"
                # wrapper; recursing on every span would duplicate its text.
                timed = _local(element.tag) == "span"
                begin = _milliseconds(element.get("begin")) if timed else None
                end = _milliseconds(element.get("end")) if timed else None
                if element.text:
                    parts.append(LyricsSegment(element.text, begin, end))
                for child in element:
                    collect_parts(child)
                    if child.tail:
                        # Tail whitespace/punctuation follows the preceding
                        # segment; keep it attached instead of inventing a
                        # second timestamp for the same syllable.
                        if parts:
                            parts[-1] = replace(
                                parts[-1], text=parts[-1].text + child.tail
                            )
                        else:
                            parts.append(LyricsSegment(child.tail))

            collect_parts(p)
            segments = (
                tuple(parts) if any(part.start is not None for part in parts) else ()
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
        return cls(stanzas=stanzas, source_ttml=xml, language=lang, syllable=syllable)

    def to_plain_text(self) -> str:
        """Assemble text from parsed lines with stanza breaks, without timing.

        Also supports syllable-timed TTML: each parsed ``LyricsLine.text``
        already contains the complete joined text, including nested spans.
        """
        return "\n\n".join(
            "\n".join(line.text for line in stanza) for stanza in self.stanzas
        )

    def to_lrc(self, *, enhanced: bool | None = None) -> str:
        """Export standard LRC or A2 enhanced LRC.

        A2 uses ``[mm:ss.xx]`` for each line and ``<mm:ss.xx>`` before each
        timed word/syllable. Text outside timed spans is retained verbatim.
        If omitted, ``enhanced`` retains the original ``syllable`` behavior.
        """
        if enhanced is None:
            enhanced = self.syllable
        values: list[str] = []
        for line in self.lines:
            prefix = f"[{_lrc_stamp(line.start)}]" if line.start is not None else ""
            if enhanced and line.segments:
                content = "".join(
                    (f"<{_lrc_stamp(s.start)}>" if s.start is not None else "") + s.text
                    for s in line.segments
                )
                values.append(prefix + content.strip())
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

    def render(self, format: LyricsFormat | str, *, embedded: bool = False) -> str:
        """Render a selected format; embedded tags may fall back to LRC/text.

        Files are strict except enhanced LRC, which naturally degrades to
        line-level LRC when syllable timing is missing.
        """
        requested = LyricsFormat(format)

        def convert(target: LyricsFormat) -> str:
            if target == LyricsFormat.TEXT:
                return self.to_plain_text()
            if target == LyricsFormat.TTML:
                if self.source_ttml is None:
                    raise LyricsFormatUnavailableError("Original TTML is unavailable")
                return self.to_ttml()
            if target in {LyricsFormat.LRC, LyricsFormat.ENHANCED_LRC}:
                if not self.is_synced:
                    raise LyricsFormatUnavailableError(
                        "LRC requires synchronized lyrics"
                    )
                return self.to_lrc(enhanced=target == LyricsFormat.ENHANCED_LRC)
            if not any(
                line.start is not None and line.end is not None for line in self.lines
            ):
                raise LyricsFormatUnavailableError(
                    "SRT requires lines with start and end timestamps"
                )
            return self.to_srt()

        try:
            return convert(requested)
        except LyricsFormatUnavailableError:
            if not embedded:
                raise
            if self.is_synced:
                return convert(LyricsFormat.LRC)
            return convert(LyricsFormat.TEXT)
