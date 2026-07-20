from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..ir.evidence import Evidence
from ..ir.semantic import NodeFactory, SemanticDocument, SemanticNode, merge_sources


_REFERENCE_HEADING = re.compile(r"^(?:references|bibliography|works\s+cited|sources|literatur|références)$", re.I)
_ENDNOTE_HEADING = re.compile(r"^(?:endnotes?|notes?)$", re.I)
_REFERENCE_ENTRY = re.compile(r"^(?:\[(\d+)\]|(\d+)[.)])\s*(.+)$", re.S)
_NOTE_ENTRY = re.compile(r"^(?:\[(\d+)\]|(\d+)[.)]|([*†‡§¶]))\s*(.+)$", re.S)
_CROSSREF = re.compile(r"\b(Figure|Fig\.|Table|Section|Sec\.|Equation|Eq\.|Appendix)\s+([A-Za-z0-9IVXLC.-]+)\b", re.I)
_CITATION = re.compile(r"(?<!\w)\[(\d+(?:\s*[,;]\s*\d+)*)\](?!\w)")


def enrich_notes_references_crossrefs(document: SemanticDocument, factory: NodeFactory) -> None:
    _extract_reference_sections(document, factory)
    _extract_endnotes(document, factory)
    _extract_footnotes(document, factory)
    _link_cross_references(document, factory)


def _extract_reference_sections(document: SemanticDocument, factory: NodeFactory) -> None:
    nodes = document.children
    index = 0
    while index < len(nodes):
        heading = nodes[index]
        heading_text = _node_text(heading).strip()
        lexical_heading = (
            heading.kind == "paragraph"
            and len(heading_text) <= 64
            and _REFERENCE_HEADING.fullmatch(heading_text) is not None
            and index + 1 < len(nodes)
        )
        if heading.kind != "heading" and not lexical_heading:
            index += 1
            continue
        if _REFERENCE_HEADING.fullmatch(heading_text) is None:
            index += 1
            continue
        if lexical_heading:
            heading.kind = "heading"
            heading.attrs.setdefault("level", 2)
            heading.confidence = min(heading.confidence, 0.90)
            heading.evidence.append(Evidence("reference_heading_lexical", 0.90, page=heading.source_pages()[0] if heading.source_pages() else None))
        level = int(heading.attrs.get("level", 6))
        end = index + 1
        entries: List[SemanticNode] = []
        while end < len(nodes):
            candidate = nodes[end]
            if candidate.kind == "heading" and int(candidate.attrs.get("level", 6)) <= level:
                break
            if candidate.kind in {"paragraph", "list", "item"}:
                text = _node_text(candidate).strip()
                match = _REFERENCE_ENTRY.match(text)
                label = match.group(1) or match.group(2) if match else None
                body = match.group(3).strip() if match else text
                if body:
                    entries.append(factory.make(
                        "reference",
                        children=(
                            list(candidate.children)
                            if not match
                            else _children_after_prefix(candidate, match.start(3), factory)
                            or [factory.make("text", text=body, confidence=candidate.confidence, sources=list(candidate.sources))]
                        ),
                        attrs={
                            "label": label,
                            "anchor": "ref-%s" % label if label else None,
                            "style": "numeric" if label else "author_year" if re.search(r"\b(?:19|20)\d{2}[a-z]?\b", body) else "unknown",
                            **_layout_attrs(candidate),
                        },
                        confidence=0.95 if label else 0.86,
                        evidence=[Evidence("reference_entry", 0.95 if label else 0.86, page=candidate.source_pages()[0] if candidate.source_pages() else None)],
                        sources=list(candidate.sources),
                    ))
            end += 1
        if entries:
            section = factory.make(
                "reference_section",
                children=entries,
                attrs={"heading_id": heading.id},
                confidence=min(entry.confidence for entry in entries),
                evidence=[Evidence("reference_section_heading", 0.98)],
                sources=merge_sources(source for entry in entries for source in entry.sources),
            )
            nodes[index + 1 : end] = [section]
            index += 2
        else:
            index += 1


def _extract_endnotes(document: SemanticDocument, factory: NodeFactory) -> None:
    nodes = document.children
    index = 0
    while index < len(nodes):
        heading = nodes[index]
        text = _node_text(heading).strip()
        if heading.kind not in {"heading", "paragraph"} or _ENDNOTE_HEADING.fullmatch(text) is None:
            index += 1
            continue
        end = index + 1
        definitions: List[SemanticNode] = []
        while end < len(nodes):
            candidate = nodes[end]
            if candidate.kind == "heading":
                break
            if candidate.kind != "paragraph":
                if definitions:
                    break
                end += 1
                continue
            match = _NOTE_ENTRY.match(_node_text(candidate).strip())
            if not match:
                if definitions:
                    break
                end += 1
                continue
            label = next((group for group in match.groups()[:3] if group), "note")
            body = match.group(4).strip()
            definitions.append(factory.make(
                "footnote",
                children=[factory.make("text", text=body, confidence=0.96, sources=list(candidate.sources))],
                attrs={"label": label, "note_type": "endnote", **_layout_attrs(candidate)},
                confidence=0.96,
                evidence=[Evidence("endnote_section_entry", 0.96, page=candidate.source_pages()[0] if candidate.source_pages() else None)],
                sources=list(candidate.sources),
            ))
            end += 1
        if definitions:
            nodes[index + 1 : end] = definitions
            if heading.kind == "paragraph":
                heading.kind = "heading"
                heading.attrs.setdefault("level", 2)
            index += len(definitions) + 1
        else:
            index += 1


def _extract_footnotes(document: SemanticDocument, factory: NodeFactory) -> None:
    linked_destinations = _linked_note_destinations(document)
    anchors = {
        str(node.attrs.get("name")): node
        for node in document.children
        if node.kind == "anchor" and node.attrs.get("name")
    }
    definitions: Dict[str, SemanticNode] = {
        str(node.attrs.get("label")): node
        for node in document.children
        if node.kind == "footnote" and node.attrs.get("label") is not None
    }
    existing_ids = {node.id for node in definitions.values()}
    retained: List[SemanticNode] = []
    for node in document.children:
        if node.kind != "paragraph":
            retained.append(node)
            continue
        region_kinds = set(node.attrs.get("region_kinds", ()))
        text = _node_text(node).strip()
        match = _NOTE_ENTRY.match(text)
        if not match:
            retained.append(node)
            continue
        label = next((group for group in match.groups()[:3] if group), "note")
        body = match.group(4).strip()
        bottom_zone = bool(node.attrs.get("bottom_zone")) or "footnote" in region_kinds
        linked_definition = _is_linked_note_definition(
            node,
            label,
            linked_destinations,
            anchors,
        )
        bottom_zone = bottom_zone or linked_definition
        if not bottom_zone:
            retained.append(node)
            continue
        note = factory.make(
            "footnote",
            children=_note_body_children(node, match, factory) or [
                factory.make("text", text=body, confidence=0.95, sources=list(node.sources))
            ],
            attrs={
                "label": label,
                "source_destinations": tuple(sorted(linked_destinations.get(label, set()))),
                "source_backlinks": tuple(sorted(_link_destinations(node, "#ref-"))),
                **_layout_attrs(node),
            },
            confidence=0.98 if linked_definition else 0.95,
            evidence=[Evidence(
                "linked_footnote_destination" if linked_definition else "footnote_definition_zone",
                0.98 if linked_definition else 0.95,
                page=node.source_pages()[0] if node.source_pages() else None,
            )],
            sources=list(node.sources),
        )
        definitions[label] = note
    document.children = retained
    if not definitions:
        return
    for node in document.walk():
        if node.kind in {"footnote", "reference", "code", "code_block"}:
            continue
        node.children = _replace_note_refs(
            node.children,
            definitions,
            factory,
            linked_destinations=linked_destinations,
        )
    document.children.extend(
        definitions[label]
        for label in sorted(definitions, key=_label_sort)
        if definitions[label].id not in existing_ids
    )


def _replace_note_refs(
    children: Sequence[SemanticNode],
    definitions: Dict[str, SemanticNode],
    factory: NodeFactory,
    in_superscript: bool = False,
    linked_destinations: Optional[Dict[str, set[str]]] = None,
) -> List[SemanticNode]:
    out: List[SemanticNode] = []
    pattern = re.compile(r"(?<!\w)(?:\[(\d+)\]|([*†‡§¶])|(%s))(?!\w)" % (r"\d+" if in_superscript else r"(?!)"))
    for child in children:
        if child.kind == "link":
            label = _node_text(child).strip()
            destination = str(child.attrs.get("href", "")).lstrip("#")
            if (
                label in definitions
                and destination
                and destination in (linked_destinations or {}).get(label, set())
            ):
                out.append(factory.make(
                    "footnote_ref",
                    text=label,
                    attrs={"label": label, "target_id": definitions[label].id},
                    confidence=0.99,
                    evidence=[Evidence("linked_footnote_reference", 0.99)],
                    sources=list(child.sources),
                ))
                continue
        child.children = _replace_note_refs(
            child.children,
            definitions,
            factory,
            in_superscript or child.kind == "superscript",
            linked_destinations,
        )
        if child.kind not in {"text", "superscript"} or child.children:
            out.append(child)
            continue
        text = child.text
        cursor = 0
        found = False
        for match in pattern.finditer(text):
            label = match.group(1) or match.group(2) or match.group(3)
            if label not in definitions or label in (linked_destinations or {}):
                continue
            found = True
            if match.start() > cursor:
                out.append(factory.make("text", text=text[cursor:match.start()], confidence=child.confidence, sources=list(child.sources)))
            out.append(factory.make(
                "footnote_ref",
                text=label,
                attrs={"label": label, "target_id": definitions[label].id},
                confidence=0.98,
                evidence=[Evidence("footnote_marker_match", 0.98)],
                sources=list(child.sources),
            ))
            cursor = match.end()
        if found:
            if cursor < len(text):
                out.append(factory.make("text", text=text[cursor:], confidence=child.confidence, sources=list(child.sources)))
        else:
            out.append(child)
    return out


def _linked_note_destinations(document: SemanticDocument) -> Dict[str, set[str]]:
    """Return superscript-label destinations explicitly encoded by the PDF."""
    destinations: Dict[str, set[str]] = {}
    for node in document.walk():
        if node.kind != "link" or not _contains_kind(node, "superscript"):
            continue
        label = _node_text(node).strip()
        href = str(node.attrs.get("href", ""))
        if not re.fullmatch(r"\d+|[*†‡§¶]", label) or not href.startswith("#"):
            continue
        destinations.setdefault(label, set()).add(href[1:])
    return destinations


def _is_linked_note_definition(
    node: SemanticNode,
    label: str,
    destinations: Dict[str, set[str]],
    anchors: Dict[str, SemanticNode],
) -> bool:
    bbox = node.attrs.get("bbox")
    definition_y = float(bbox[1]) if isinstance(bbox, (tuple, list)) and len(bbox) == 4 else None
    pages = set(node.source_pages())
    for name in destinations.get(label, set()):
        anchor = anchors.get(name)
        if anchor is None:
            continue
        anchor_page = anchor.attrs.get("page")
        anchor_y = anchor.attrs.get("y")
        if pages and anchor_page not in pages:
            continue
        if definition_y is None or not isinstance(anchor_y, (int, float)):
            return True
        if -2.0 <= definition_y - float(anchor_y) <= 180.0:
            return True
    return False


def _note_body_children(
    node: SemanticNode,
    match: re.Match[str],
    factory: NodeFactory,
) -> List[SemanticNode]:
    """Preserve inline links/styles while removing the definition marker/backlinks."""
    remaining_prefix = match.start(4)
    out: List[SemanticNode] = []
    for child in node.children:
        child_text = _node_text(child)
        if remaining_prefix >= len(child_text):
            remaining_prefix -= len(child_text)
            continue
        if child.kind == "link" and str(child.attrs.get("href", "")).startswith("#ref-"):
            continue
        if child.kind == "link" and _node_text(child).strip() == str(child.attrs.get("href", "")).strip():
            child.children = [factory.make(
                "text",
                text=_node_text(child).strip(),
                confidence=child.confidence,
                sources=list(child.sources),
            )]
        if remaining_prefix:
            text = child_text[remaining_prefix:]
            remaining_prefix = 0
            if text:
                out.append(factory.make("text", text=text, confidence=child.confidence, sources=list(child.sources)))
            continue
        out.append(child)
    while out and out[-1].kind == "text" and not out[-1].text.strip():
        out.pop()
    if out and out[-1].kind == "text":
        out[-1].text = out[-1].text.rstrip()
    return out


def _children_after_prefix(
    node: SemanticNode,
    prefix_chars: int,
    factory: NodeFactory,
) -> List[SemanticNode]:
    """Retain inline semantics after a flattened leading label/marker."""
    remaining = prefix_chars
    out: List[SemanticNode] = []
    for child in node.children:
        child_text = _node_text(child)
        if remaining >= len(child_text):
            remaining -= len(child_text)
            continue
        if remaining:
            text = child_text[remaining:]
            remaining = 0
            if text:
                out.append(factory.make("text", text=text, confidence=child.confidence, sources=list(child.sources)))
            continue
        out.append(child)
    return out


def _contains_kind(node: SemanticNode, kind: str) -> bool:
    return any(child.kind == kind or _contains_kind(child, kind) for child in node.children)


def _link_destinations(node: SemanticNode, prefix: str) -> set[str]:
    out: set[str] = set()
    for child in node.children:
        if child.kind == "link":
            href = str(child.attrs.get("href", ""))
            if href.startswith(prefix):
                out.add(href[1:])
        out.update(_link_destinations(child, prefix))
    return out


def _link_cross_references(document: SemanticDocument, factory: NodeFactory) -> None:
    targets: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for node in document.walk():
        if node.kind == "table" and node.attrs.get("label"):
            anchor = str(node.attrs.setdefault("anchor", "table-%s" % node.attrs["label"]))
            targets[("table", str(node.attrs["label"]).casefold())] = (node.id, anchor)
        elif node.kind == "figure" and node.attrs.get("label"):
            anchor = str(node.attrs.setdefault("anchor", "figure-%s" % node.attrs["label"]))
            targets[("figure", str(node.attrs["label"]).casefold())] = (node.id, anchor)
        elif node.kind == "heading":
            text = _node_text(node).strip()
            numbered = re.match(r"^((?:\d+\.)*\d+|[A-Z])\s+", text)
            if numbered:
                targets[("section", numbered.group(1).casefold())] = (node.id, str(node.attrs.get("anchor") or node.id))
        elif node.kind == "reference" and node.attrs.get("label"):
            anchor = str(node.attrs.get("anchor") or node.id)
            targets[("reference", str(node.attrs["label"]).casefold())] = (node.id, anchor)
    for node in document.walk():
        if node.kind in {"code", "code_block", "reference", "cross_reference", "figure", "caption", "image", "table", "table_cell"}:
            continue
        node.children = _replace_crossrefs(node.children, targets, factory)


def _replace_crossrefs(children: Sequence[SemanticNode], targets: Dict[Tuple[str, str], Tuple[str, str]], factory: NodeFactory) -> List[SemanticNode]:
    out: List[SemanticNode] = []
    for child in children:
        child.children = _replace_crossrefs(child.children, targets, factory)
        if child.kind != "text" or child.children:
            out.append(child)
            continue
        cursor = 0
        matched = False
        matches: List[Tuple[int, int, str, str, Tuple[str, str]]] = []
        for match in _CROSSREF.finditer(child.text):
            category = match.group(1).lower().rstrip(".")
            category = "figure" if category in {"fig", "figure"} else "section" if category in {"sec", "section"} else "equation" if category in {"eq", "equation"} else category
            target = targets.get((category, match.group(2).casefold()))
            if not target:
                continue
            matches.append((match.start(), match.end(), match.group(0), category, target))
        for match in _CITATION.finditer(child.text):
            labels = [part.strip() for part in re.split(r"[,;]", match.group(1))]
            if len(labels) != 1:
                continue
            target = targets.get(("reference", labels[0].casefold()))
            if target:
                matches.append((match.start(), match.end(), match.group(0), "reference", target))
        for start, end, text, category, target in sorted(matches, key=lambda item: (item[0], item[1])):
            if start < cursor:
                continue
            matched = True
            if start > cursor:
                out.append(factory.make("text", text=child.text[cursor:start], confidence=child.confidence, sources=list(child.sources)))
            text_node = factory.make("text", text=text, confidence=0.96, sources=list(child.sources))
            out.append(factory.make(
                "cross_reference",
                children=[text_node],
                attrs={"reference_kind": category, "label": text, "target_id": target[0], "target_anchor": target[1]},
                confidence=0.96,
                evidence=[Evidence("cross_reference_target_match", 0.96)],
                sources=list(child.sources),
            ))
            cursor = end
        if matched:
            if cursor < len(child.text):
                out.append(factory.make("text", text=child.text[cursor:], confidence=child.confidence, sources=list(child.sources)))
        else:
            out.append(child)
    return out


def _node_text(node: SemanticNode) -> str:
    return node.text or "".join(_node_text(child) for child in node.children)


def _layout_attrs(node: SemanticNode) -> Dict[str, object]:
    return {
        key: node.attrs[key]
        for key in ("_layout_markdown", "_layout_kind")
        if key in node.attrs
    }


def _label_sort(label: str):
    return (0, int(label)) if label.isdigit() else (1, label)
