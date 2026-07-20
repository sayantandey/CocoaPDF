from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..ir.evidence import Evidence
from ..ir.semantic import NodeFactory, SemanticNode, SourceRef, merge_sources
from .source import inline_nodes_from_tokens, sources_from_lines


_CAPTION_RE = re.compile(r"^(?:table|tab\.|exhibit)\s+[A-Za-z0-9IVXLC]+(?:[.-][A-Za-z0-9IVXLC]+)*\b", re.I)
_NOTE_RE = re.compile(r"^(?:note|notes|source|sources)\s*[:.]\s*", re.I)


def build_table_node(
    factory: NodeFactory,
    converter: Any,
    renderer: Any,
    event: Any,
    regions_by_line: Optional[Dict[str, Tuple[str, ...]]] = None,
) -> SemanticNode:
    lines = sorted(event.lines, key=lambda line: (line.y0, line.x0, line.seq))
    bbox = tuple(float(value) for value in event.attrs.get("bbox", _bbox(lines)))
    table_lines, caption_lines, note_lines = _partition_supporting_lines(lines, bbox)
    model = _lattice_model(factory, converter, renderer, event.page, table_lines, bbox, regions_by_line)
    if model is None:
        model = _borderless_model(factory, renderer, table_lines, bbox, regions_by_line)
    if model is None:
        return _rejected_table_candidate(factory, renderer, lines, event.page, bbox, regions_by_line)
    rows, header_rows, confidence, evidence = model
    sources = sources_from_lines(lines, regions_by_line)
    attrs: Dict[str, Any] = {
        "bbox": bbox,
        "header_rows": header_rows,
        "output_mode": "html" if _requires_html(rows, header_rows) else "gfm",
        "column_count": max((sum(max(1, int(cell.attrs.get("colspan", 1))) for cell in row.children) for row in rows), default=0),
        "row_count": len(rows),
        "page_size": list(converter.page_sizes.get(event.page, (0.0, 0.0))),
        "source_pages": [event.page],
        "grid_signature": _grid_signature(rows, bbox),
    }
    table = factory.make(
        "table",
        children=rows,
        attrs=attrs,
        confidence=confidence,
        evidence=evidence,
        sources=sources,
    )
    if caption_lines:
        caption = factory.make(
            "caption",
            children=_inline_lines(factory, renderer, caption_lines, regions_by_line),
            attrs={"placement": "before" if max(line.y1 for line in caption_lines) <= bbox[1] + 4 else "after"},
            confidence=0.96,
            evidence=[Evidence("table_caption_geometry", 0.96, page=event.page)],
            sources=sources_from_lines(caption_lines, regions_by_line),
        )
        table.children.insert(0, caption)
        label = _plain_lines(renderer, caption_lines)
        match = re.match(r"^(?:Table|Tab\.|Exhibit)\s+([A-Za-z0-9IVXLC]+(?:[.-][A-Za-z0-9IVXLC]+)*)", label, re.I)
        if match:
            table.attrs["label"] = match.group(1)
            table.attrs["caption"] = label
    for note_line in note_lines:
        table.children.append(
            factory.make(
                "table_note",
                children=_inline_lines(factory, renderer, [note_line], regions_by_line),
                confidence=0.94,
                evidence=[Evidence("table_note_label", 0.94, page=event.page)],
                sources=sources_from_lines([note_line], regions_by_line),
            )
        )
    return table


def merge_continued_tables(nodes: Sequence[SemanticNode]) -> List[SemanticNode]:
    out: List[SemanticNode] = []
    for node in nodes:
        if node.kind != "table" or not out or out[-1].kind != "table":
            out.append(node)
            continue
        previous = out[-1]
        if not _continuation_match(previous, node):
            out.append(node)
            continue
        previous_rows = [child for child in previous.children if child.kind == "table_row"]
        current_rows = [child for child in node.children if child.kind == "table_row"]
        previous_header = int(previous.attrs.get("header_rows", 0))
        current_header = int(node.attrs.get("header_rows", 0))
        if previous_header and current_header and _row_signature(previous_rows[0]) == _row_signature(current_rows[0]):
            current_rows = current_rows[current_header:]
        insertion = len(previous.children)
        for index, child in enumerate(previous.children):
            if child.kind == "table_note":
                insertion = index
                break
        previous.children[insertion:insertion] = current_rows
        previous.children.extend(child for child in node.children if child.kind == "table_note")
        previous.sources = merge_sources(previous.sources + node.sources)
        previous.attrs["row_count"] = len([child for child in previous.children if child.kind == "table_row"])
        previous.attrs["source_pages"] = previous.source_pages()
        previous.evidence.append(Evidence("multi_page_table_continuation", 0.95, data={"pages": previous.source_pages()}))
        previous.confidence = min(previous.confidence, node.confidence, 0.95)
    return out


def _partition_supporting_lines(lines: Sequence[Any], bbox: Tuple[float, float, float, float]) -> Tuple[List[Any], List[Any], List[Any]]:
    table: List[Any] = []
    captions: List[Any] = []
    notes: List[Any] = []
    for line in lines:
        text = _line_plain(line)
        center = ((line.x0 + line.x1) / 2, (line.y0 + line.y1) / 2)
        inside = bbox[0] - 3 <= center[0] <= bbox[2] + 3 and bbox[1] - 3 <= center[1] <= bbox[3] + 3
        if _CAPTION_RE.match(text):
            captions.append(line)
        elif _NOTE_RE.match(text) and line.y0 >= bbox[3] - 3:
            notes.append(line)
        elif inside:
            table.append(line)
        elif text:
            # Event-level table detectors may include an unlabelled italic caption.
            captions.append(line)
    return table, captions, notes


def _lattice_model(
    factory: NodeFactory,
    converter: Any,
    renderer: Any,
    page: int,
    lines: Sequence[Any],
    bbox: Tuple[float, float, float, float],
    regions_by_line: Optional[Dict[str, Tuple[str, ...]]],
) -> Optional[Tuple[List[SemanticNode], int, float, List[Evidence]]]:
    vertical = [segment for segment in converter.segments if segment.page == page and segment.vertical and _segment_intersects(segment, bbox)]
    horizontal = [segment for segment in converter.segments if segment.page == page and segment.horizontal and _segment_intersects(segment, bbox)]
    xs = _cluster([(segment.x0 + segment.x1) / 2 for segment in vertical], 2.0)
    ys = _cluster([(segment.y0 + segment.y1) / 2 for segment in horizontal], 2.0)
    if len(xs) < 2 or len(ys) < 2:
        return None
    rows_count, cols_count = len(ys) - 1, len(xs) - 1
    occupied = [[False] * cols_count for _ in range(rows_count)]
    rows: List[SemanticNode] = []
    header_rows = int(renderer._table_header_rows(list(lines), ys))
    inferred_spans = 0
    for row_index in range(rows_count):
        cells: List[SemanticNode] = []
        for column_index in range(cols_count):
            if occupied[row_index][column_index]:
                continue
            colspan = 1
            while column_index + colspan < cols_count:
                boundary_x = xs[column_index + colspan]
                if renderer._table_has_vertical_edge(page, boundary_x, ys[row_index], ys[row_index + 1]):
                    break
                if not _supported_missing_vertical_boundary(renderer, page, boundary_x, row_index, ys):
                    break
                colspan += 1
            rowspan = 1
            while row_index + rowspan < rows_count:
                boundary_y = ys[row_index + rowspan]
                if renderer._table_has_horizontal_edge(page, boundary_y, xs[column_index], xs[column_index + colspan]):
                    break
                if not _supported_missing_horizontal_boundary(renderer, page, boundary_y, column_index, colspan, xs):
                    break
                rowspan += 1
            if rowspan > 1 or colspan > 1:
                inferred_spans += 1
            for rr in range(row_index, min(rows_count, row_index + rowspan)):
                for cc in range(column_index, min(cols_count, column_index + colspan)):
                    occupied[rr][cc] = True
            cell_box = (xs[column_index], ys[row_index], xs[column_index + colspan], ys[row_index + rowspan])
            cell_lines = _lines_in_box(lines, cell_box)
            role = "th" if row_index < header_rows else "td"
            cell = factory.make(
                "table_cell",
                children=_cell_blocks(factory, renderer, cell_lines, regions_by_line),
                attrs={
                    "row": row_index,
                    "col": column_index,
                    "rowspan": rowspan,
                    "colspan": colspan,
                    "role": role,
                    "bbox": cell_box,
                    "rotation": _dominant_rotation(cell_lines),
                },
                confidence=0.91 if rowspan > 1 or colspan > 1 else 0.98,
                evidence=[
                    Evidence("lattice_cell", 0.98, page=page, data={"bbox": cell_box}),
                    *([Evidence("missing_border_span", 0.91, page=page, data={"bbox": cell_box})] if rowspan > 1 or colspan > 1 else []),
                ],
                sources=sources_from_lines(cell_lines, regions_by_line) or [SourceRef(page=page, bbox=cell_box)],
            )
            cells.append(cell)
        rows.append(
            factory.make(
                "table_row",
                children=cells,
                attrs={"row": row_index, "role": "header" if row_index < header_rows else "body"},
                confidence=min((cell.confidence for cell in cells), default=0.9),
                evidence=[Evidence("lattice_row", 0.97, page=page)],
                sources=merge_sources(source for cell in cells for source in cell.sources),
            )
        )
    confidence = 0.96 if not inferred_spans else 0.90
    evidence = [Evidence("lattice_table", confidence, page=page, data={"bbox": bbox, "inferred_spans": inferred_spans})]
    return rows, header_rows, confidence, evidence


def _borderless_model(
    factory: NodeFactory,
    renderer: Any,
    lines: Sequence[Any],
    bbox: Tuple[float, float, float, float],
    regions_by_line: Optional[Dict[str, Tuple[str, ...]]],
) -> Optional[Tuple[List[SemanticNode], int, float, List[Evidence]]]:
    key_value = renderer._borderless_rows_from_lines(list(lines))
    rows_data: List[Tuple[Any, List[str]]] = []
    if key_value:
        rows_data = [(line, [key, value]) for line, (key, value) in key_value]
        header_rows = 0
        evidence_name = "borderless_key_value"
    else:
        rows_data = [(line, renderer._borderless_column_cells(line)) for line in lines]
        rows_data = [(line, cells) for line, cells in rows_data if cells]
        width = max((len(cells) for _line, cells in rows_data), default=0)
        rows_data = [(line, cells) for line, cells in rows_data if len(cells) == width]
        header_rows = 1 if rows_data and rows_data[0][0].bold_ratio >= 0.65 else 0
        evidence_name = "borderless_alignment"
    rejection = _borderless_rejection_reason(lines, rows_data, bbox)
    if rejection is not None:
        return None
    rows: List[SemanticNode] = []
    for row_index, (line, cells_text) in enumerate(rows_data):
        cells: List[SemanticNode] = []
        word_groups = _word_groups(line, len(cells_text))
        for column_index, text in enumerate(cells_text):
            chars = word_groups[column_index] if column_index < len(word_groups) else []
            cell_sources = sources_from_lines([_partial_line(line, chars)], regions_by_line) if chars else sources_from_lines([line], regions_by_line)
            tokens = _tokens_for_chars(line, chars) if chars else [{"text": text, "page": line.page, "glyph_ids": (), "mcids": (), "bbox": (line.x0, line.y0, line.x1, line.y1), "style": (False,) * 8}]
            cells.append(
                factory.make(
                    "table_cell",
                    children=inline_nodes_from_tokens(factory, tokens),
                    attrs={"row": row_index, "col": column_index, "rowspan": 1, "colspan": 1, "role": "th" if row_index < header_rows else "td"},
                    confidence=0.91,
                    evidence=[Evidence(evidence_name + "_cell", 0.91, page=line.page)],
                    sources=cell_sources,
                )
            )
        rows.append(
            factory.make(
                "table_row",
                children=cells,
                attrs={"row": row_index, "role": "header" if row_index < header_rows else "body"},
                confidence=0.91,
                evidence=[Evidence(evidence_name + "_row", 0.91, page=line.page)],
                sources=sources_from_lines([line], regions_by_line),
            )
        )
    confidence = _borderless_confidence(lines, rows_data)
    return rows, header_rows, confidence, [Evidence(evidence_name, confidence, page=lines[0].page if lines else None, data={"bbox": bbox})]


def _cell_blocks(factory: NodeFactory, renderer: Any, lines: Sequence[Any], regions_by_line: Optional[Dict[str, Tuple[str, ...]]]) -> List[SemanticNode]:
    if not lines:
        return []
    from ..core import Line, line_text_tokens, list_marker, plain_text
    rotated = bool(_dominant_rotation(lines))
    if rotated:
        seen: set[int] = set()
        chars = []
        for line in lines:
            for char in line.chars:
                if id(char) not in seen:
                    seen.add(id(char))
                    chars.append(char)
        chars.sort(key=lambda char: char.seq)
        if chars:
            lines = [Line(chars, chars[0].page, chars[0].seq, source_order=True, writing_mode="rotated")]
    blocks: List[SemanticNode] = []
    if all(list_marker(plain_text(line_text_tokens(line)).strip()) for line in lines):
        items = []
        ordered = False
        start = 1
        for index, line in enumerate(lines):
            marker = list_marker(plain_text(line_text_tokens(line)).strip())
            assert marker is not None
            ordered = ordered or marker[0] == "ol"
            if index == 0 and isinstance(marker[2], int):
                start = marker[2]
            tokens = line_text_tokens(line)
            remaining = marker[1]
            body_tokens = []
            for token in tokens:
                text = token["text"]
                if remaining >= len(text):
                    remaining -= len(text)
                    continue
                token = dict(token)
                if remaining:
                    token["text"] = text[remaining:]
                    remaining = 0
                body_tokens.append(token)
            items.append(factory.make("item", children=inline_nodes_from_tokens(factory, body_tokens), confidence=0.95, sources=sources_from_lines([line], regions_by_line)))
        return [factory.make("list", children=items, attrs={"ordered": ordered, "start": start}, confidence=0.94, sources=sources_from_lines(lines, regions_by_line))]
    if all(getattr(line, "mono_ratio", 0.0) >= 0.80 for line in lines):
        return [factory.make("code_block", text="\n".join(plain_text(line_text_tokens(line)) for line in lines), confidence=0.93, sources=sources_from_lines(lines, regions_by_line))]
    for line in lines:
        tokens = line_text_tokens(line)
        if rotated:
            # Table ruling crossing a rotated header is border evidence, not an
            # underline. Preserve all other style and provenance fields.
            for token in tokens:
                style = tuple(token.get("style", ()))
                if len(style) >= 5 and style[4]:
                    token["style"] = style[:4] + (False,) + style[5:]
        blocks.append(factory.make("paragraph", children=inline_nodes_from_tokens(factory, tokens), confidence=0.97, sources=sources_from_lines([line], regions_by_line)))
    return blocks


def _inline_lines(factory: NodeFactory, renderer: Any, lines: Sequence[Any], regions_by_line: Optional[Dict[str, Tuple[str, ...]]]) -> List[SemanticNode]:
    from ..core import line_text_tokens
    out: List[SemanticNode] = []
    for index, line in enumerate(lines):
        if index:
            out.extend(inline_nodes_from_tokens(factory, [{"text": " ", "page": line.page, "style": (False,) * 8, "glyph_ids": (), "mcids": (), "bbox": None}]))
        out.extend(inline_nodes_from_tokens(factory, line_text_tokens(line)))
    return out


def _requires_html(rows: Sequence[SemanticNode], header_rows: int) -> bool:
    if header_rows not in {0, 1}:
        return True
    for row in rows:
        for cell in row.children:
            if int(cell.attrs.get("rowspan", 1)) != 1 or int(cell.attrs.get("colspan", 1)) != 1:
                return True
            if any(child.kind not in {"text", "strong", "emphasis", "code", "link", "paragraph"} for child in cell.children):
                return True
            if int(cell.attrs.get("rotation", 0)) % 360:
                return True
    return False


def _continuation_match(left: SemanticNode, right: SemanticNode) -> bool:
    left_pages, right_pages = left.source_pages(), right.source_pages()
    if not left_pages or not right_pages or right_pages[0] != left_pages[-1] + 1:
        return False
    if int(left.attrs.get("column_count", 0)) != int(right.attrs.get("column_count", 0)):
        return False
    left_signature = tuple(float(value) for value in left.attrs.get("grid_signature", ()) or ())
    right_signature = tuple(float(value) for value in right.attrs.get("grid_signature", ()) or ())
    if left_signature and right_signature:
        if len(left_signature) != len(right_signature) or any(abs(a - b) > 0.018 for a, b in zip(left_signature, right_signature)):
            return False
    left_rows = [child for child in left.children if child.kind == "table_row"]
    right_rows = [child for child in right.children if child.kind == "table_row"]
    if not left_rows or not right_rows:
        return False
    left_box = left.attrs.get("bbox")
    right_box = right.attrs.get("bbox")
    if left_box and right_box:
        left_width = float(left_box[2]) - float(left_box[0])
        right_width = float(right_box[2]) - float(right_box[0])
        if abs(left_width - right_width) > max(5.0, left_width * 0.04):
            return False
        left_size = left.attrs.get("page_size") or (0.0, 0.0)
        right_size = right.attrs.get("page_size") or (0.0, 0.0)
        left_height = float(left_size[1]) if len(left_size) > 1 else 0.0
        right_height = float(right_size[1]) if len(right_size) > 1 else 0.0
        if left_height and float(left_box[3]) < left_height * 0.55:
            return False
        if right_height and float(right_box[1]) > right_height * 0.45:
            return False
    left_header = int(left.attrs.get("header_rows", 0))
    right_header = int(right.attrs.get("header_rows", 0))
    if left_header and right_header:
        return tuple(_row_signature(row) for row in left_rows[:left_header]) == tuple(_row_signature(row) for row in right_rows[:right_header])
    return left.attrs.get("label") == right.attrs.get("label") and bool(left.attrs.get("label"))


def _row_signature(row: SemanticNode) -> Tuple[str, ...]:
    return tuple(_node_text(cell).strip().casefold() for cell in row.children if cell.kind == "table_cell")


def _node_text(node: SemanticNode) -> str:
    return node.text or "".join(_node_text(child) for child in node.children)


def _borderless_confidence(lines: Sequence[Any], rows: Sequence[Tuple[Any, List[str]]]) -> float:
    if len(rows) < 3:
        return 0.55
    punctuation = sum(1 for line in lines if _line_plain(line).endswith((".", "?", "!")))
    page_span = max((line.y1 for line in lines), default=0.0) - min((line.y0 for line in lines), default=0.0)
    stability = len({len(cells) for _line, cells in rows}) == 1
    score = 0.88 + (0.04 if stability else -0.10) - min(0.18, punctuation / max(len(lines), 1) * 0.25)
    if page_span > 500:
        score -= 0.18
    return max(0.50, min(0.96, score))


def _borderless_rejection_reason(
    lines: Sequence[Any],
    rows: Sequence[Tuple[Any, List[str]]],
    bbox: Tuple[float, float, float, float],
) -> Optional[str]:
    if len(rows) < 3:
        return "too_few_rows"
    widths = [len(cells) for _line, cells in rows]
    if not widths or min(widths) < 2 or len(set(widths)) != 1:
        return "unstable_column_count"
    texts = [_line_plain(line) for line in lines if _line_plain(line)]
    if not texts:
        return "empty"
    sentence_like = sum(text.endswith((".", "?", "!")) for text in texts) / len(texts)
    word_counts = sorted(len(text.split()) for text in texts)
    median_words = word_counts[len(word_counts) // 2]
    bibliography_like = sum(
        bool(re.match(r"^(?:\[?\d+\]?|[A-Z][\w'’-]+,?\s+[A-Z])", text))
        and bool(re.search(r"\b(?:19|20)\d{2}[a-z]?\b|\bdoi\b|https?://", text, re.I))
        for text in texts
    ) / len(texts)
    if bibliography_like >= 0.45:
        return "bibliography_pattern"
    if sentence_like >= 0.60 and median_words >= 8:
        return "article_prose"
    page_span = bbox[3] - bbox[1]
    if page_span > 500 and sentence_like >= 0.35:
        return "page_spanning_columns"
    # Real tables tend to repeat short records. Long wrapping text in the first
    # inferred column is more likely a two-column article or definition prose.
    first_column_lengths = [len(cells[0].split()) for _line, cells in rows if cells]
    if first_column_lengths and sum(length > 8 for length in first_column_lengths) / len(first_column_lengths) >= 0.50:
        return "wrapped_prose_column"
    return None


def _rejected_table_candidate(
    factory: NodeFactory,
    renderer: Any,
    lines: Sequence[Any],
    page: int,
    bbox: Tuple[float, float, float, float],
    regions_by_line: Optional[Dict[str, Tuple[str, ...]]],
) -> SemanticNode:
    paragraphs = [
        factory.make(
            "paragraph",
            children=_inline_lines(factory, renderer, [line], regions_by_line),
            confidence=0.96,
            evidence=[Evidence("table_candidate_rejected", 0.96, page=page)],
            sources=sources_from_lines([line], regions_by_line),
        )
        for line in lines
        if _line_plain(line)
    ]
    return factory.make(
        "section",
        children=paragraphs,
        attrs={"semantic_group": "rejected_table_candidate", "bbox": bbox},
        confidence=0.96,
        evidence=[Evidence("borderless_table_false_positive_guard", 0.96, page=page)],
        sources=sources_from_lines(lines, regions_by_line),
        warnings=["TABLE_CANDIDATE_RETAINED_AS_TEXT"],
    )


def _supported_missing_vertical_boundary(renderer: Any, page: int, x: float, row: int, ys: Sequence[float]) -> bool:
    adjacent = False
    if row > 0:
        adjacent = adjacent or renderer._table_has_vertical_edge(page, x, ys[row - 1], ys[row])
    if row + 2 < len(ys):
        adjacent = adjacent or renderer._table_has_vertical_edge(page, x, ys[row + 1], ys[row + 2])
    return adjacent


def _supported_missing_horizontal_boundary(
    renderer: Any,
    page: int,
    y: float,
    column: int,
    colspan: int,
    xs: Sequence[float],
) -> bool:
    adjacent = False
    if column > 0:
        adjacent = adjacent or renderer._table_has_horizontal_edge(page, y, xs[column - 1], xs[column])
    if column + colspan + 1 < len(xs):
        adjacent = adjacent or renderer._table_has_horizontal_edge(page, y, xs[column + colspan], xs[column + colspan + 1])
    return adjacent


def _grid_signature(rows: Sequence[SemanticNode], bbox: Tuple[float, float, float, float]) -> List[float]:
    width = max(float(bbox[2]) - float(bbox[0]), 1.0)
    boundaries = {0.0, 1.0}
    for row in rows:
        for cell in row.children:
            cell_box = cell.attrs.get("bbox")
            if isinstance(cell_box, (list, tuple)) and len(cell_box) >= 4:
                boundaries.add(round((float(cell_box[0]) - float(bbox[0])) / width, 4))
                boundaries.add(round((float(cell_box[2]) - float(bbox[0])) / width, 4))
    return sorted(boundaries)


def _line_plain(line: Any) -> str:
    from ..core import line_text_tokens, plain_text
    return plain_text(line_text_tokens(line)).strip()


def _plain_lines(renderer: Any, lines: Sequence[Any]) -> str:
    return " ".join(_line_plain(line) for line in lines).strip()


def _cluster(values: Iterable[float], tolerance: float) -> List[float]:
    values = sorted(float(value) for value in values)
    groups: List[List[float]] = []
    for value in values:
        if groups and value - groups[-1][-1] <= tolerance:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [sum(group) / len(group) for group in groups]


def _segment_intersects(segment: Any, bbox: Tuple[float, float, float, float]) -> bool:
    x0, x1 = sorted((segment.x0, segment.x1))
    y0, y1 = sorted((segment.y0, segment.y1))
    return x1 >= bbox[0] - 3 and x0 <= bbox[2] + 3 and y1 >= bbox[1] - 3 and y0 <= bbox[3] + 3


def _lines_in_box(lines: Sequence[Any], bbox: Tuple[float, float, float, float]) -> List[Any]:
    """Return cell-local line fragments instead of assigning a whole line.

    PDF producers frequently place text from several cells in one text object,
    and rotated headers can geometrically intersect a horizontal row.  Cell
    ownership therefore belongs to glyph centers, not the aggregate line bbox.
    """
    out: List[Any] = []
    for line in lines:
        chars = [
            char
            for char in line.chars
            if bbox[0] - 2 <= (char.x0 + char.x1) / 2 <= bbox[2] + 2
            and bbox[1] - 2 <= (char.y0 + char.y1) / 2 <= bbox[3] + 2
        ]
        if chars:
            out.append(_partial_line(line, chars))
    return out


def _bbox(lines: Sequence[Any]) -> Tuple[float, float, float, float]:
    if not lines:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(line.x0 for line in lines), min(line.y0 for line in lines), max(line.x1 for line in lines), max(line.y1 for line in lines))


def _dominant_rotation(lines: Sequence[Any]) -> int:
    if not lines:
        return 0
    vertical = sum(1 for line in lines if getattr(line, "writing_mode", "horizontal") != "horizontal")
    chars = [char for line in lines for char in getattr(line, "chars", ()) if getattr(char, "text", "")]
    if chars:
        x_span = max(char.x1 for char in chars) - min(char.x0 for char in chars)
        y_span = max(char.y1 for char in chars) - min(char.y0 for char in chars)
        if len(chars) >= 3 and y_span > max(x_span * 1.35, max(char.size for char in chars) * 1.8):
            return 90
    return 90 if vertical > len(lines) / 2 else 0


def _word_groups(line: Any, expected: int) -> List[List[Any]]:
    chars = sorted([char for char in line.chars if char.text], key=lambda char: (char.x0, char.seq))
    if expected <= 1 or not chars:
        return [chars]
    groups: List[List[Any]] = [[]]
    previous = None
    for char in chars:
        if previous is not None and char.x0 - previous.x1 >= max(line.size * 1.8, 16.0) and len(groups) < expected:
            groups.append([])
        groups[-1].append(char)
        previous = char
    while len(groups) < expected:
        groups.append([])
    return groups


def _partial_line(line: Any, chars: Sequence[Any]) -> Any:
    if not chars:
        return line
    from ..core import Line
    x_span = max(char.x1 for char in chars) - min(char.x0 for char in chars)
    y_span = max(char.y1 for char in chars) - min(char.y0 for char in chars)
    writing_mode = line.writing_mode
    if len(chars) >= 2 and y_span > max(x_span * 1.35, max(char.size for char in chars) * 1.8):
        writing_mode = "rotated"
    return Line(list(chars), line.page, line.seq, source_order=line.source_order, writing_mode=writing_mode)


def _tokens_for_chars(line: Any, chars: Sequence[Any]) -> List[Dict[str, Any]]:
    from ..core import line_text_tokens
    return line_text_tokens(_partial_line(line, chars))
