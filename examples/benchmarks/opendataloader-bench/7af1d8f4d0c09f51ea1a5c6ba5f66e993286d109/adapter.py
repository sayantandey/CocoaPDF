"""Canonical CocoaPDF adapter for opendataloader-project/opendataloader-bench.

Copy to the benchmark checkout as ``src/pdf_parser_cocoapdf.py``.

The adapter applies output-schema projection only. CocoaPDF's detectors and
semantic graph are untouched; the two policies below exist because the
benchmark's annotation schema differs from CocoaPDF's default Markdown
projection, not because they improve the underlying analysis:

* ``heading_level_mode="flat"`` — the benchmark ground truth annotates every
  heading as Markdown ``#``. Its heading metric discards the level entirely, so
  this only stops CocoaPDF's inferred depth from adding characters that the
  reading-order metric would score as edits.
* Table markup is reduced to plain ``table``/``tr``/``td`` with only ``rowspan``
  and ``colspan``. Cell text keeps its content but drops nested inline markup,
  which the table metric would otherwise tokenise as cell content.

A conversion exception writes an empty ``<document-id>.md`` and records the
exception in ``failures.json`` so every corpus item stays visible to the scorer
instead of aborting the run.
"""

from __future__ import annotations

import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from cocoapdf import ConvertOptions, convert_file


class _ODLTableParser(HTMLParser):
    """Project CocoaPDF table HTML onto the benchmark's plain-cell dialect."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.caption_parts: List[str] = []
        self.rows: List[List[Tuple[str, int, int]]] = []
        self._row: Optional[List[Tuple[str, int, int]]] = None
        self._cell_parts: Optional[List[str]] = None
        self._cell_rowspan = 1
        self._cell_colspan = 1
        self._in_caption = False

    def handle_starttag(self, tag, attrs) -> None:
        tag = tag.lower()
        if tag == "caption":
            self._in_caption = True
        elif tag == "tr":
            self._row = []
        elif tag in {"td", "th"}:
            values = {name.lower(): value for name, value in attrs}
            self._cell_parts = []
            self._cell_rowspan = _positive_span(values.get("rowspan"))
            self._cell_colspan = _positive_span(values.get("colspan"))
        elif tag == "br":
            self._append_text(" ")

    def handle_startendtag(self, tag, attrs) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag) -> None:
        tag = tag.lower()
        if tag == "caption":
            self._in_caption = False
        elif tag in {"td", "th"} and self._cell_parts is not None:
            if self._row is None:
                self._row = []
            self._row.append(
                (
                    _normalized_text("".join(self._cell_parts)),
                    self._cell_rowspan,
                    self._cell_colspan,
                )
            )
            self._cell_parts = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        self._append_text(data)

    def _append_text(self, text: str) -> None:
        if self._cell_parts is not None:
            self._cell_parts.append(text)
        elif self._in_caption:
            self.caption_parts.append(text)

    def render(self) -> str:
        if not self.rows:
            return ""
        output = ["<table>"]
        for row in self.rows:
            cells = []
            for text, rowspan, colspan in row:
                attributes = []
                if rowspan > 1:
                    attributes.append('rowspan="%d"' % rowspan)
                if colspan > 1:
                    attributes.append('colspan="%d"' % colspan)
                suffix = " " + " ".join(attributes) if attributes else ""
                cells.append("<td%s>%s</td>" % (suffix, html.escape(text)))
            output.append("<tr>" + "".join(cells) + "</tr>")
        output.append("</table>")
        return "\n".join(output)


def _positive_span(value: Optional[str]) -> int:
    try:
        return max(1, int(value or "1"))
    except (TypeError, ValueError):
        return 1


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


_TABLE_PATTERN = re.compile(r"<table(?:\s[^>]*)?>.*?</table>", re.IGNORECASE | re.DOTALL)

# The benchmark annotates plain text. These patterns remove markup that CocoaPDF
# adds and the ground truth never carries, so the reading-order and heading
# metrics compare prose against prose. None of them rewrite extracted characters:
# quotation glyphs, dashes, and cell text are left exactly as the page produced
# them, because changing those would be tuning the text toward the reference
# rather than projecting an output schema.
_IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\([^)\n]*\)")
_LINK_PATTERN = re.compile(r"\[([^\]\n]*)\]\((?:#|https?://|mailto:)[^)\n]*\)")
_ESCAPE_PATTERN = re.compile(r"\\([#*_`~\[\]().\-+!>|])")
_PARAGRAPH_TAG_PATTERN = re.compile(r"</?p[^>]*>")
_UNDERLINE_TAG_PATTERN = re.compile(r"</?u>")
_STRIKETHROUGH_PATTERN = re.compile(r"~~(.*?)~~", re.DOTALL)
_BLANK_RUN_PATTERN = re.compile(r"\n{3,}")


def _project_prose(segment: str) -> str:
    """Strip non-table markup from a run of text outside any table block.

    GFM rows are left untouched: their cell text escapes ``|`` and the benchmark
    converts those rows to HTML itself, so unescaping here would split cells.
    """
    lines = []
    for line in segment.split("\n"):
        if line.lstrip().startswith("|"):
            lines.append(line)
            continue
        line = _IMAGE_PATTERN.sub("", line)
        line = _LINK_PATTERN.sub(r"\1", line)
        line = _ESCAPE_PATTERN.sub(r"\1", line)
        line = _PARAGRAPH_TAG_PATTERN.sub("", line)
        line = _UNDERLINE_TAG_PATTERN.sub("", line)
        lines.append(line)
    text = _STRIKETHROUGH_PATTERN.sub(r"\1", "\n".join(lines))
    return _BLANK_RUN_PATTERN.sub("\n\n", text)


def project_for_odl(markdown: str) -> str:
    """Match the benchmark's plain HTML-table annotation schema."""

    def replace(match) -> str:
        parser = _ODLTableParser()
        try:
            parser.feed(match.group(0))
            table = parser.render()
        except Exception:  # noqa: BLE001 - never lose a document to markup repair
            return match.group(0)
        if not table:
            return match.group(0)
        caption = _normalized_text("".join(parser.caption_parts))
        return (caption + "\n\n" if caption else "") + table

    converted = _TABLE_PATTERN.sub(replace, markdown)

    projected: List[str] = []
    position = 0
    for match in _TABLE_PATTERN.finditer(converted):
        projected.append(_project_prose(converted[position:match.start()]))
        projected.append(match.group(0))
        position = match.end()
    projected.append(_project_prose(converted[position:]))
    return "".join(projected)


def to_markdown(doc_paths: Iterable[Path], input_path: Path, output_dir: Path) -> None:
    del input_path  # Required by the benchmark adapter interface.

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    failures: List[dict] = []

    for doc_path in doc_paths:
        doc_path = Path(doc_path)
        output_file = output_dir / ("%s.md" % doc_path.stem)
        try:
            result = convert_file(doc_path, ConvertOptions(heading_level_mode="flat"))
            output_file.write_text(
                project_for_odl(result.markdown), encoding="utf-8", newline="\n"
            )
        except Exception as exc:  # noqa: BLE001 - keep the corpus complete
            output_file.write_text("", encoding="utf-8", newline="\n")
            failures.append(
                {
                    "document": doc_path.name,
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                }
            )

    failures_file = output_dir.parent / "failures.json"
    failures_file.write_text(
        json.dumps(failures, indent=2, ensure_ascii=False),
        encoding="utf-8",
        newline="\n",
    )
