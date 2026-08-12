from __future__ import annotations

import math
import re
from dataclasses import replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..ir.evidence import Evidence
from ..ir.semantic import (
    NodeFactory,
    SemanticDocument,
    SemanticNode,
    SourceRef,
    merge_sources,
)


_TOC_HEADING = re.compile(r"^(?:table\s+of\s+contents|contents|目次|sommaire|inhalt|índice)$", re.I)
_TOC_ENTRY = re.compile(r"^(.*?)\s*(?:\.{2,}|·{2,}|…+)\s*(\d+|[ivxlcdm]+)\s*$", re.I)
_TOC_ENTRY_SCAN = re.compile(
    r"(.*?)\s*(?:(?:\.\s*){2,}|(?:·\s*){2,}|…+)\s*"
    r"(\d+|[ivxlcdm]+)(?=\s|$)",
    re.I,
)


def extract_outline(
    document: Any,
    factory: NodeFactory,
    page_ref_to_num: Dict[Tuple[int, int], int],
    semantic_document: Optional[SemanticDocument] = None,
) -> Optional[SemanticNode]:
    catalog = document.catalog()
    root = _resolve(document, catalog.get("Outlines")) if isinstance(catalog, dict) else None
    if not isinstance(root, dict):
        return None
    named_destinations = _named_destinations(document, catalog if isinstance(catalog, dict) else {})
    first = root.get("First")
    children = _walk_siblings(
        document,
        factory,
        first,
        1,
        page_ref_to_num,
        named_destinations,
        semantic_document,
        set(),
    )
    if not children:
        return None
    return factory.make(
        "outline",
        children=children,
        attrs={"source": "pdf_outline"},
        confidence=0.99,
        evidence=[Evidence("pdf_outline_tree", 0.99, data={"entry_count": sum(1 for node in _walk(children) if node.kind == "outline_item")})],
        sources=[source for child in children for source in child.sources],
    )


def _entry_sources(node: SemanticNode, start: int, end: int) -> List[SourceRef]:
    selected: List[SourceRef] = []
    offset = 0
    for child in node.children:
        text = _node_text(child)
        child_start, child_end = offset, offset + len(text)
        if child_end > start and child_start < end:
            selected.extend(child.sources)
        offset = child_end
    merged = merge_sources(selected)
    if not merged:
        return list(node.sources)
    regions_by_page: Dict[int, Tuple[str, ...]] = {}
    for source in node.sources:
        regions_by_page[source.page] = tuple(
            sorted(
                set(regions_by_page.get(source.page, ()))
                | set(source.region_ids)
            )
        )
    return [
        replace(
            source,
            region_ids=tuple(
                sorted(
                    set(source.region_ids)
                    | set(regions_by_page.get(source.page, ()))
                )
            ),
        )
        for source in merged
    ]


def _toc_matches(
    node: SemanticNode,
) -> List[Tuple[str, str, List[SourceRef], float]]:
    text = _node_text(node)
    matches = list(_TOC_ENTRY_SCAN.finditer(text))
    if not matches:
        match = _TOC_ENTRY.fullmatch(text.strip())
        if match is None:
            return []
        sources = list(node.sources)
        bbox = sources[0].bbox if sources else None
        return [
            (
                match.group(1).strip(),
                match.group(2),
                sources,
                float(bbox[0]) if bbox else 0.0,
            )
        ]
    if _TOC_ENTRY_SCAN.sub("", text).strip():
        return []
    entries: List[Tuple[str, str, List[SourceRef], float]] = []
    for match in matches:
        title, page_label = match.group(1).strip(), match.group(2)
        if not title:
            return []
        sources = _entry_sources(node, match.start(), match.end())
        boxes = [source.bbox for source in sources if source.bbox is not None]
        indent = min(box[0] for box in boxes) if boxes else 0.0
        entries.append((title, page_label, sources, float(indent)))
    return entries


def reconstruct_visible_toc(document: SemanticDocument, factory: NodeFactory) -> None:
    nodes = document.children
    heading_index = next(
        (
            index
            for index, node in enumerate(nodes)
            if node.kind == "heading"
            and _TOC_HEADING.match(_node_text(node).strip())
        ),
        None,
    )
    if heading_index is None:
        return
    entries: List[SemanticNode] = []
    consumed: List[int] = []
    levels: List[float] = []
    layout_fragments: List[str] = []
    for index in range(heading_index + 1, min(len(nodes), heading_index + 80)):
        node = nodes[index]
        if node.kind == "heading":
            break
        if node.kind != "paragraph":
            if entries:
                break
            continue
        parsed = _toc_matches(node)
        if not parsed:
            if entries:
                break
            continue
        fragment = node.attrs.get("_layout_markdown")
        if isinstance(fragment, str) and fragment:
            layout_fragments.append(fragment)
        for title, page_label, sources, indent in parsed:
            if not levels or indent > levels[-1] + 6:
                levels.append(indent)
            while len(levels) > 1 and indent < levels[-1] - 4:
                levels.pop()
            level = len(levels)
            confidence = 0.96 if len(parsed) > 1 else 0.94
            entries.append(
                factory.make(
                    "toc_item",
                    text=title,
                    attrs=_toc_target_attrs(
                        level,
                        page_label,
                        _best_heading_target(
                            nodes,
                            title,
                            _page_label_number(page_label),
                            level,
                        ),
                    ),
                    confidence=confidence,
                    evidence=[
                        Evidence(
                            "visible_toc_dot_leader",
                            confidence,
                            page=sources[0].page if sources else None,
                            data={
                                "split_from_compound_paragraph": len(parsed) > 1
                            },
                        )
                    ],
                    sources=sources,
                )
            )
        consumed.append(index)
    if not entries:
        return
    attrs: Dict[str, Any] = {"source": "visible_toc"}
    if layout_fragments:
        attrs.update(
            {
                "_layout_markdown": "\n\n".join(layout_fragments),
                "_layout_kind": "paragraph",
            }
        )
    confidence = min(entry.confidence for entry in entries)
    toc = factory.make(
        "toc",
        children=entries,
        attrs=attrs,
        confidence=confidence,
        evidence=[Evidence("visible_toc", confidence)],
        sources=merge_sources(
            source for entry in entries for source in entry.sources
        ),
    )
    nodes[min(consumed) : max(consumed) + 1] = [toc]


def outline_to_toc(outline: SemanticNode, factory: NodeFactory) -> SemanticNode:
    def convert(item: SemanticNode, level: int) -> SemanticNode:
        children = [convert(child, level + 1) for child in item.children if child.kind == "outline_item"]
        return factory.make(
            "toc_item",
            text=item.text,
            children=children,
            attrs={"level": level, "target_id": item.attrs.get("target_id"), "target_anchor": item.attrs.get("target_anchor"), "page": item.attrs.get("page")},
            confidence=item.confidence,
            evidence=list(item.evidence),
            sources=list(item.sources),
        )
    return factory.make("toc", children=[convert(child, 1) for child in outline.children], attrs={"source": "pdf_outline"}, confidence=outline.confidence, evidence=list(outline.evidence), sources=list(outline.sources))


def _walk_siblings(
    document: Any,
    factory: NodeFactory,
    raw: Any,
    level: int,
    page_ref_to_num: Dict[Tuple[int, int], int],
    named_destinations: Dict[str, Any],
    semantic_document: Optional[SemanticDocument],
    active: set[str],
) -> List[SemanticNode]:
    out: List[SemanticNode] = []
    current = raw
    count = 0
    while current is not None and count < 10000:
        count += 1
        key = _ref_text(current)
        if key in active:
            break
        active.add(key)
        value = _resolve(document, current)
        if not isinstance(value, dict):
            active.remove(key)
            break
        title = _text(document, value.get("Title"))
        destination = value.get("Dest")
        action = _resolve(document, value.get("A"))
        if destination is None and isinstance(action, dict) and _name(document, action.get("S")) == "GoTo":
            destination = action.get("D")
        target = _destination(document, destination, page_ref_to_num, named_destinations)
        children = _walk_siblings(document, factory, value.get("First"), level + 1, page_ref_to_num, named_destinations, semantic_document, active) if value.get("First") is not None else []
        sources = [SourceRef(page=target[0], object_refs=(key,))] if target[0] else []
        target_node = (
            _best_heading_target(
                semantic_document.children,
                title,
                target[0],
                level,
                destination=target[1],
                destination_y=_normalized_destination_y(
                    document,
                    target[0],
                    target[1],
                ),
            )
            if semantic_document is not None
            else None
        )
        out.append(factory.make(
            "outline_item",
            text=title,
            children=children,
            attrs={
                "level": level,
                "page": target[0] or None,
                "destination": target[1],
                "target_id": target_node[0] if target_node else None,
                "target_anchor": target_node[1] if target_node else None,
                "open": int(_number(document, value.get("Count"), 0)) >= 0,
            },
            confidence=0.99 if title else 0.75,
            evidence=[Evidence("pdf_outline_item", 0.99 if title else 0.75, page=target[0] or None, data={"object_ref": key})],
            sources=sources,
        ))
        active.remove(key)
        current = value.get("Next")
    return out


def _destination(
    document: Any,
    raw: Any,
    page_ref_to_num: Dict[Tuple[int, int], int],
    named_destinations: Optional[Dict[str, Any]] = None,
    active_names: Optional[set[str]] = None,
) -> Tuple[int, Any]:
    value = _resolve(document, raw)
    if isinstance(value, dict) and value.get("D") is not None:
        return _destination(document, value.get("D"), page_ref_to_num, named_destinations, active_names)
    if isinstance(value, list) and value:
        page_raw = value[0]
        number = getattr(page_raw, "num", None)
        generation = int(getattr(page_raw, "gen", 0) or 0)
        page = page_ref_to_num.get((number, generation), page_ref_to_num.get((number, 0), 0)) if isinstance(number, int) else 0
        return page, [_plain(document, item) for item in value[1:]]
    if isinstance(value, (bytes, str)):
        name = _text(document, value) if isinstance(value, bytes) else str(value).lstrip("/")
        destinations = named_destinations or {}
        active = active_names or set()
        if name in destinations and name not in active:
            return _destination(document, destinations[name], page_ref_to_num, destinations, active | {name})
        return 0, name
    return 0, None


def _best_heading_target(
    nodes: Sequence[SemanticNode],
    title: str,
    page: Optional[int] = None,
    outline_level: int = 1,
    destination: Any = None,
    destination_y: Optional[float] = None,
) -> Optional[Tuple[str, str]]:
    normalized = _normalize(title)
    title_words = set(normalized.split())
    best: Tuple[float, Optional[SemanticNode]] = (0.0, None)
    candidates = list(_walk(nodes))
    exact_paragraphs: List[SemanticNode] = []
    prefix_headings: List[SemanticNode] = []
    for node in candidates:
        if page and node.source_pages() and page not in node.source_pages():
            continue
        candidate = _normalize(_node_text(node))
        if candidate == normalized and node.kind == "heading":
            return node.id, _ensure_anchor(candidates, node, title)
        if candidate == normalized and node.kind == "paragraph":
            exact_paragraphs.append(node)
            continue
        if node.kind != "heading":
            continue
        words = set(candidate.split())
        score = len(words & title_words) / max(len(words | title_words), 1)
        if normalized and candidate.startswith(normalized + " "):
            score = max(score, 0.95)
            prefix_headings.append(node)
        if score > best[0]:
            best = (score, node)
    destination_target = _destination_disambiguated_target(
        prefix_headings,
        exact_paragraphs,
        destination_y,
    )
    if destination_target is not None:
        if destination_target.kind == "heading":
            return destination_target.id, _ensure_anchor(
                candidates,
                destination_target,
                _node_text(destination_target),
            )
        return _promote_outline_paragraph(
            candidates,
            destination_target,
            title,
            page,
            outline_level,
        )
    if best[0] >= 0.72 and best[1] is not None:
        node = best[1]
        return node.id, _ensure_anchor(candidates, node, _node_text(node))
    if exact_paragraphs:
        return _promote_outline_paragraph(
            candidates,
            exact_paragraphs[0],
            title,
            page,
            outline_level,
        )
    # A page-only outline destination cannot identify a precise vertical
    # coordinate. When that page has exactly one recovered heading, the PDF
    # destination plus unique page structure is stronger than title wording.
    if not _page_only_destination(destination):
        return None
    page_headings = [
        node
        for node in candidates
        if page
        and node.kind == "heading"
        and node.source_pages()
        and page in node.source_pages()
    ]
    if len(page_headings) != 1:
        return None
    node = page_headings[0]
    node.evidence.append(
        Evidence(
            "pdf_outline_unique_page_heading",
            0.90,
            page=page,
            detail=title,
        )
    )
    node.confidence = min(node.confidence, 0.90)
    return node.id, _ensure_anchor(candidates, node, _node_text(node))


def _destination_disambiguated_target(
    prefix_headings: Sequence[SemanticNode],
    exact_paragraphs: Sequence[SemanticNode],
    destination_y: Optional[float],
) -> Optional[SemanticNode]:
    """Resolve a prefix-heading/exact-paragraph conflict using PDF geometry.

    An outline title such as ``search`` can legitimately target a composite
    heading such as ``search Main entry point``.  The same short text may also
    occur as a cross-reference paragraph elsewhere on the destination page.
    Textual prefix scoring alone cannot distinguish those cases.  When the PDF
    supplies a vertical destination, compare it with both source regions and
    choose the nearest target.  If either side lacks geometry, retain the
    established text-only behavior rather than pretending the coordinate is
    decisive.
    """
    if destination_y is None or not math.isfinite(destination_y):
        return None
    headings = [
        (distance, node)
        for node in prefix_headings
        if (distance := _node_vertical_distance(node, destination_y)) is not None
    ]
    paragraphs = [
        (distance, node)
        for node in exact_paragraphs
        if (distance := _node_vertical_distance(node, destination_y)) is not None
    ]
    if not headings or not paragraphs:
        return None
    heading_distance, heading = min(headings, key=lambda item: item[0])
    paragraph_distance, paragraph = min(paragraphs, key=lambda item: item[0])
    # An explicit ordinate is corroborating evidence only near an actual text
    # region.  If both candidates are far away, the destination may describe a
    # viewport rather than the target baseline; retain text-only behavior.
    if min(heading_distance, paragraph_distance) > 72.0:
        return None
    # A heading remains the structural prior only for a true geometric tie.
    # Otherwise the explicit destination decides which text occurrence owns
    # the outline target.
    if heading_distance <= paragraph_distance + 1.0:
        return heading
    return paragraph


def _node_vertical_distance(
    node: SemanticNode,
    destination_y: float,
) -> Optional[float]:
    boxes = [source.bbox for source in node.sources if source.bbox is not None]
    if not boxes:
        raw_bbox = node.attrs.get("bbox")
        if isinstance(raw_bbox, (list, tuple)) and len(raw_bbox) == 4:
            try:
                parsed_bbox = tuple(float(value) for value in raw_bbox)
            except (TypeError, ValueError, OverflowError):
                parsed_bbox = ()
            if len(parsed_bbox) == 4 and all(math.isfinite(value) for value in parsed_bbox):
                boxes = [parsed_bbox]
    if not boxes:
        return None
    distances = []
    for box in boxes:
        top, bottom = sorted((float(box[1]), float(box[3])))
        if top <= destination_y <= bottom:
            distances.append(0.0)
        else:
            distances.append(min(abs(destination_y - top), abs(destination_y - bottom)))
    return min(distances)


def _promote_outline_paragraph(
    candidates: Sequence[SemanticNode],
    node: SemanticNode,
    title: str,
    page: Optional[int],
    outline_level: int,
) -> Tuple[str, str]:
    node.kind = "heading"
    node.attrs.setdefault("level", min(6, max(1, outline_level)))
    node.confidence = min(node.confidence, 0.97)
    node.evidence.append(Evidence("pdf_outline_heading_match", 0.97, page=page))
    return node.id, _ensure_anchor(candidates, node, title)


def _normalized_destination_y(
    document: Any,
    page: int,
    destination: Any,
) -> Optional[float]:
    """Map an explicit PDF destination ordinate into CocoaPDF page geometry."""
    if not page or not isinstance(destination, list) or not destination:
        return None
    mode = str(destination[0] or "").lstrip("/")
    raw_x = 0.0
    raw_y: Optional[float] = None
    if mode == "XYZ":
        raw_x = _finite_number(destination[1] if len(destination) > 1 else None, 0.0)
        raw_y = _finite_number(destination[2] if len(destination) > 2 else None)
    elif mode in {"FitH", "FitBH"}:
        raw_y = _finite_number(destination[1] if len(destination) > 1 else None)
    elif mode == "FitR":
        raw_x = _finite_number(destination[1] if len(destination) > 1 else None, 0.0)
        raw_y = _finite_number(destination[4] if len(destination) > 4 else None)
    if raw_y is None:
        return None
    try:
        pages = document.pages()
        page_dict = pages[page - 1]
    except (AttributeError, IndexError, TypeError, ValueError):
        return None
    if not isinstance(page_dict, dict):
        return None
    try:
        media = document.resolve_array(page_dict.get("CropBox")) or document.resolve_array(
            page_dict.get("MediaBox")
        )
    except Exception:
        return None
    if not isinstance(media, (list, tuple)):
        return None
    values = [_finite_number(_resolve(document, value)) for value in media[:4]]
    if len(values) < 4 or any(value is None for value in values):
        return None
    x0, x1 = sorted((float(values[0]), float(values[2])))
    y0, y1 = sorted((float(values[1]), float(values[3])))
    width, height = x1 - x0, y1 - y0
    if width <= 1.0 or height <= 1.0:
        return None
    rotate_value = _finite_number(_resolve(document, page_dict.get("Rotate")), 0.0)
    rotate = int(rotate_value) % 360
    if rotate not in {0, 90, 180, 270}:
        rotate = 0
    user_unit = _finite_number(_resolve(document, page_dict.get("UserUnit")), 1.0)
    if user_unit is None or user_unit <= 0:
        user_unit = 1.0
    # Reuse the same transform as text extraction so crop offsets, rotation,
    # and UserUnit cannot make outline and glyph coordinates disagree.
    from ..core import apply_mat, page_normalization_transform

    matrix, _display_width, display_height = page_normalization_transform(
        x0,
        y0,
        width,
        height,
        rotate,
        user_unit,
    )
    _point_x, point_y = apply_mat(matrix, raw_x, raw_y)
    return max(0.0, min(display_height, display_height - point_y))


def _finite_number(value: Any, default: Optional[float] = None) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    parsed = float(value)
    return parsed if math.isfinite(parsed) else default


def _page_only_destination(destination: Any) -> bool:
    """Return true only when a PDF destination carries no vertical target."""
    if not isinstance(destination, list) or not destination:
        return False
    mode = str(destination[0] or "").lstrip("/")
    operands = destination[1:]
    if mode in {"Fit", "FitB", "FitV", "FitBV"}:
        return True
    if mode == "XYZ":
        top = operands[1] if len(operands) > 1 else None
        return top is None
    if mode in {"FitH", "FitBH"}:
        top = operands[0] if operands else None
        return top is None
    return False


def _ensure_anchor(nodes: Sequence[SemanticNode], node: SemanticNode, title: str) -> str:
    existing = str(node.attrs.get("anchor") or "")
    if existing:
        return existing
    base = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-") or node.id
    used = {str(item.attrs.get("anchor")) for item in nodes if item is not node and item.attrs.get("anchor")}
    anchor = base
    counter = 2
    while anchor in used:
        anchor = "%s-%d" % (base, counter)
        counter += 1
    node.attrs["anchor"] = anchor
    return anchor


def _toc_target_attrs(level: int, page_label: str, target: Optional[Tuple[str, str]]) -> Dict[str, Any]:
    return {
        "level": level,
        "page_label": page_label,
        "target_id": target[0] if target else None,
        "target_anchor": target[1] if target else None,
    }


def _named_destinations(document: Any, catalog: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    legacy = _resolve(document, catalog.get("Dests"))
    if isinstance(legacy, dict):
        for key, value in legacy.items():
            name = _text(document, key) if isinstance(key, bytes) else str(key).lstrip("/")
            if name:
                out[name] = value
    names = _resolve(document, catalog.get("Names"))
    dest_tree = _resolve(document, names.get("Dests")) if isinstance(names, dict) else None
    _read_name_tree(document, dest_tree, out, set())
    return out


def _read_name_tree(document: Any, raw: Any, out: Dict[str, Any], active: set[str]) -> None:
    node = _resolve(document, raw)
    if not isinstance(node, dict):
        return
    identity = _ref_text(raw)
    if identity in active:
        return
    active.add(identity)
    names = _resolve(document, node.get("Names"))
    if isinstance(names, list):
        for index in range(0, len(names) - 1, 2):
            key = _text(document, names[index])
            if key:
                out[key] = names[index + 1]
    kids = _resolve(document, node.get("Kids"))
    if isinstance(kids, list):
        for child in kids:
            _read_name_tree(document, child, out, active)
    active.remove(identity)


def _page_label_number(label: str) -> Optional[int]:
    if label.isdigit():
        return int(label)
    return None


def _node_text(node: SemanticNode) -> str:
    return node.text or "".join(_node_text(child) for child in node.children)


def _normalize(text: str) -> str:
    return re.sub(r"[^\w]+", " ", text.casefold()).strip()


def _walk(nodes: Sequence[SemanticNode]):
    for node in nodes:
        yield node
        yield from _walk(node.children)


def _resolve(document: Any, value: Any) -> Any:
    try:
        return document.resolve(value)
    except Exception:
        return None


def _text(document: Any, value: Any) -> str:
    value = _resolve(document, value)
    if isinstance(value, bytes):
        from ..core import decode_pdf_text
        return decode_pdf_text(value)
    return str(value) if isinstance(value, str) else ""


def _name(document: Any, value: Any) -> str:
    return str(_resolve(document, value) or "").lstrip("/")


def _number(document: Any, value: Any, default: float) -> float:
    value = _resolve(document, value)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _plain(document: Any, value: Any) -> Any:
    value = _resolve(document, value)
    if isinstance(value, bytes):
        return _text(document, value)
    if isinstance(value, list):
        return [_plain(document, item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(document, item) for key, item in value.items()}
    return value


def _ref_text(raw: Any) -> str:
    number = getattr(raw, "num", None)
    generation = int(getattr(raw, "gen", 0) or 0)
    return "%d %d R" % (number, generation) if isinstance(number, int) else "direct:%d" % id(raw)
