from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..ir.evidence import Evidence
from ..ir.semantic import NodeFactory, SemanticDocument, SemanticNode, SourceRef


_TOC_HEADING = re.compile(r"^(?:table\s+of\s+contents|contents|目次|sommaire|inhalt|índice)$", re.I)
_TOC_ENTRY = re.compile(r"^(.*?)\s*(?:\.{2,}|·{2,}|…+)\s*(\d+|[ivxlcdm]+)\s*$", re.I)


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


def reconstruct_visible_toc(document: SemanticDocument, factory: NodeFactory) -> None:
    nodes = document.children
    heading_index = next((index for index, node in enumerate(nodes) if node.kind == "heading" and _TOC_HEADING.match(_node_text(node).strip())), None)
    if heading_index is None:
        return
    entries: List[SemanticNode] = []
    consumed: List[int] = []
    previous_indent = 0.0
    levels: List[float] = []
    for index in range(heading_index + 1, min(len(nodes), heading_index + 80)):
        node = nodes[index]
        if node.kind == "heading":
            break
        if node.kind != "paragraph":
            if entries:
                break
            continue
        text = _node_text(node).strip()
        match = _TOC_ENTRY.match(text)
        if not match:
            if entries:
                break
            continue
        title, page_label = match.group(1).strip(), match.group(2)
        bbox = node.attrs.get("bbox") or (0, 0, 0, 0)
        indent = float(bbox[0]) if isinstance(bbox, (list, tuple)) and bbox else 0.0
        if not levels or indent > levels[-1] + 6:
            levels.append(indent)
        while len(levels) > 1 and indent < levels[-1] - 4:
            levels.pop()
        level = len(levels)
        entries.append(factory.make(
            "toc_item",
            text=title,
            attrs=_toc_target_attrs(level, page_label, _best_heading_target(nodes, title, _page_label_number(page_label), level)) ,
            confidence=0.94,
            evidence=[Evidence("visible_toc_dot_leader", 0.94, page=node.source_pages()[0] if node.source_pages() else None)],
            sources=list(node.sources),
        ))
        previous_indent = indent
        consumed.append(index)
    if not entries:
        return
    toc = factory.make(
        "toc",
        children=entries,
        attrs={"source": "visible_toc"},
        confidence=min(entry.confidence for entry in entries),
        evidence=[Evidence("visible_toc", 0.94)],
        sources=[source for entry in entries for source in entry.sources],
    )
    first = min(consumed)
    last = max(consumed)
    nodes[first : last + 1] = [toc]


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
) -> Optional[Tuple[str, str]]:
    normalized = _normalize(title)
    title_words = set(normalized.split())
    best: Tuple[float, Optional[SemanticNode]] = (0.0, None)
    candidates = list(_walk(nodes))
    for node in candidates:
        if page and node.source_pages() and page not in node.source_pages():
            continue
        candidate = _normalize(_node_text(node))
        if candidate == normalized and node.kind in {"heading", "paragraph"}:
            if node.kind == "paragraph":
                node.kind = "heading"
                node.attrs.setdefault("level", min(6, max(1, outline_level)))
                node.confidence = min(node.confidence, 0.97)
                node.evidence.append(Evidence("pdf_outline_heading_match", 0.97, page=page))
            anchor = _ensure_anchor(candidates, node, title)
            return node.id, anchor
        if node.kind != "heading":
            continue
        words = set(candidate.split())
        score = len(words & title_words) / max(len(words | title_words), 1)
        if score > best[0]:
            best = (score, node)
    if best[0] < 0.72 or best[1] is None:
        # A page-only outline destination cannot identify a precise vertical
        # coordinate.  When that page has exactly one recovered heading, the
        # PDF destination plus unique page structure is stronger evidence than
        # title wording alone (for example "First Page" targeting "Page Scope
        # Review").  Do not guess when multiple headings compete.
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
    node = best[1]
    return node.id, _ensure_anchor(candidates, node, _node_text(node))


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
