from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..ir.evidence import Evidence
from ..ir.semantic import NodeFactory, SemanticDocument, SemanticNode, SourceRef, merge_sources
from .forms import extract_acroform
from .navigation import extract_outline, outline_to_toc, reconstruct_visible_toc
from .notes import enrich_notes_references_crossrefs
from .records import (
    canonical_list_records,
    canonical_quote_records,
    gfm_table_alignments,
)
from .source import inline_nodes_from_tokens, line_identifier, region_index, sources_from_lines
from .tables import build_table_node, merge_continued_tables


def _project_table_alignments(
    alignments: Sequence[str],
    table: SemanticNode,
) -> List[str]:
    """Project legacy alignments onto a possibly pruned semantic grid."""

    try:
        column_count = int(table.attrs.get("column_count", 0))
    except (TypeError, ValueError, OverflowError):
        return []
    if column_count <= 0:
        return []

    projected = list(alignments)
    if len(projected) == column_count:
        return projected

    records = table.attrs.get("pruned_vacuous_edge_columns")
    if not isinstance(records, list) or len(projected) != column_count + len(records):
        return []
    # Each record's index addresses the grid immediately before that recorded
    # removal. Applying them in order therefore handles left-plus-right pruning
    # without translating indices back into the original physical grid.
    for record in records:
        if not isinstance(record, dict):
            return []
        raw_column = record.get("column")
        if isinstance(raw_column, bool):
            return []
        try:
            column = int(raw_column)
        except (TypeError, ValueError, OverflowError):
            return []
        if column < 0 or column >= len(projected):
            return []
        projected.pop(column)
    return projected if len(projected) == column_count else []


def build_semantic_graph(converter: Any, renderer: Any, events_by_page: Dict[int, Sequence[Any]], regions: Sequence[Any]) -> SemanticDocument:
    factory = NodeFactory("node")
    regions_by_line = region_index(regions)
    region_by_id = {str(region.id): region for region in regions}
    document = SemanticDocument(metadata={
        "title": _metadata_title(converter.doc) or "CocoaPDF Document",
        "producer": _metadata_value(converter.doc, "Producer"),
        "creator": _metadata_value(converter.doc, "Creator"),
        "language": _catalog_language(converter.doc) or None,
        "source": "pdf_operators_and_structure",
        "output_policy": "independent_semantic_projections",
        "markdown_projection": "lossless_layout_reconciliation",
        "html_projection": "semantic_html_with_lossless_generated_fragments",
        "ocr_used": False,
        "page_selection_active": converter.options.pages is not None,
        "processed_pages": sorted(converter.processed_pages),
    }, version="2")
    for page in sorted(events_by_page):
        page_nodes: List[SemanticNode] = []
        page_events = sorted(events_by_page[page], key=lambda item: item.rank)
        event_index = 0
        while event_index < len(page_events):
            event = page_events[event_index]
            if event.kind == "list":
                grouped = [event]
                event_index += 1
                while (
                    event_index < len(page_events)
                    and page_events[event_index].kind == "list"
                ):
                    grouped.append(page_events[event_index])
                    event_index += 1
                event = _combine_list_events(grouped)
            else:
                event_index += 1
            node = _event_node(factory, converter, renderer, event, regions_by_line, region_by_id)
            if node is not None:
                page_nodes.append(node)
        if converter.options.page_breaks and document.children and page_nodes:
            document.children.append(factory.make("page_break", attrs={"page": page}, confidence=1.0))
        document.children.extend(page_nodes)
    document.children = _merge_cross_page_paragraphs(document.children, renderer, converter.options.page_breaks)
    document.children = merge_continued_tables(document.children)
    _assign_heading_anchors(document)
    outline = extract_outline(converter.doc, factory, converter._page_ref_to_num, document)
    reconstruct_visible_toc(document, factory)
    if outline is not None:
        document.metadata["outline"] = outline.to_dict()
        # A PDF outline describes the complete source document. Keep it as
        # diagnostic metadata, but do not inject entries outside an explicitly
        # selected page slice. A TOC physically present on a selected page has
        # already been reconstructed as an ordinary semantic node above.
        if (
            not document.metadata["page_selection_active"]
            and not any(node.kind == "toc" for node in document.children)
        ):
            document.children.insert(0, outline_to_toc(outline, factory))
    selected_pages = set(converter.processed_pages) if converter.options.pages is not None else None
    form = extract_acroform(
        converter.doc,
        factory,
        converter._page_ref_to_num,
        selected_pages=selected_pages,
    )
    if form is not None:
        document.children.append(form)
    enrich_notes_references_crossrefs(document, factory)
    return document


def _combine_list_events(events: Sequence[Any]) -> Any:
    first = events[0]
    attrs = dict(first.attrs)
    if any(event.attrs.get("visual_markers") for event in events):
        attrs["visual_markers"] = True
    combined_markdown = "\n".join(
        event.legacy_markdown.rstrip("\n") for event in events
    )
    list_records: List[Dict[str, Any]] = []
    for event in events:
        semantic = _event_semantic(event)
        records = semantic.get("list_records")
        if not isinstance(records, list):
            records = canonical_list_records(event.legacy_markdown)
        list_records.extend(
            dict(record) for record in records if isinstance(record, dict)
        )
    return SimpleNamespace(
        page=first.page,
        rank=first.rank,
        kind="list",
        lines=[line for event in events for line in event.lines],
        attrs=attrs,
        legacy_markdown=combined_markdown,
        semantic={"list_records": list_records},
    )


def _event_semantic(event: Any) -> Dict[str, Any]:
    semantic = getattr(event, "semantic", {})
    return semantic if isinstance(semantic, dict) else {}


def _event_node(factory: NodeFactory, converter: Any, renderer: Any, event: Any, regions_by_line: Dict[str, Tuple[str, ...]], region_by_id: Dict[str, Any]) -> Optional[SemanticNode]:
    if event.attrs.get("merged_into_table"):
        return None
    sources = sources_from_lines(event.lines, regions_by_line)
    region_ids = tuple(sorted({region_id for line in event.lines for region_id in regions_by_line.get(line_identifier(line), ())}))
    region_kinds = tuple(sorted({str(region_by_id[region_id].kind) for region_id in region_ids if region_id in region_by_id}))
    bbox = _bbox(event.lines)
    attrs = dict(event.attrs)
    attrs.update({"page": event.page, "bbox": bbox, "region_ids": region_ids, "region_kinds": region_kinds})
    attrs["_layout_markdown"] = event.legacy_markdown
    attrs["_layout_kind"] = event.kind
    semantic = _event_semantic(event)
    generated_html = semantic.get("generated_html")
    if not isinstance(generated_html, str):
        # Compatibility for callers constructing pre-v2 BlockEvent-like
        # objects directly. Production events always carry semantic records.
        generated_html = event.legacy_markdown
    lossless_html = _lossless_html_for_event(event.kind, generated_html)
    if lossless_html:
        attrs["_layout_html"] = lossless_html
    writing_modes = [getattr(line, "writing_mode", "horizontal") for line in event.lines]
    if writing_modes:
        attrs["writing_mode"] = max(set(writing_modes), key=writing_modes.count)
    plain_event_text = " ".join(_line_plain(line) for line in event.lines)
    if _contains_rtl(plain_event_text):
        attrs["direction"] = "rtl" if _rtl_dominant(plain_event_text) else "auto"
    page_height = converter.page_sizes.get(event.page, (612.0, 792.0))[1]
    attrs["bottom_zone"] = bool(bbox and bbox[1] >= page_height * 0.72)
    confidence = _event_confidence(event.kind, region_kinds)
    evidence = [Evidence("geometric_semantic_detector", confidence, detail=event.kind, page=event.page, data={"region_ids": region_ids})]
    artifact_text_recovered = bool(attrs.get("artifact_text_recovered"))
    if artifact_text_recovered:
        reasons = attrs.get("artifact_recovery_reasons")
        if not isinstance(reasons, (list, tuple)):
            reasons = []
        confidence = min(confidence, 0.86)
        evidence.append(Evidence(
            "artifact_text_recovery",
            confidence,
            detail="authored display heading retained despite defective source Artifact tag",
            page=event.page,
            data={
                "source_marked_artifact": True,
                "admission_reasons": [str(reason) for reason in reasons],
                "glyph_count": sum(
                    1
                    for line in event.lines
                    for character in getattr(line, "chars", ())
                    if getattr(character, "text", "")
                ),
            },
        ))
    kind = event.kind
    if kind == "anchor":
        return factory.make("anchor", attrs={"name": attrs.get("anchor"), "page": event.page, "y": attrs.get("y")}, confidence=1.0)
    if kind in {"paragraph", "heading"} and event.lines:
        layout = renderer._paragraph_layout(event.lines)
        if layout in {"center", "right"}:
            attrs["alignment"] = layout
        elif layout and layout.startswith("indent:"):
            attrs["text_indent_em"] = float(layout.split(":", 1)[1])
    if kind == "heading":
        node = factory.make("heading", children=_paragraph_inlines(factory, renderer, event.lines), attrs=attrs, confidence=confidence, evidence=evidence, sources=sources)
        if artifact_text_recovered:
            node.warnings.append("ARTIFACT_TEXT_RECOVERED")
        return node
    if kind == "paragraph":
        return factory.make("paragraph", children=_paragraph_inlines(factory, renderer, event.lines), attrs=attrs, confidence=confidence, evidence=evidence, sources=sources)
    if kind == "list":
        return _list_node(factory, renderer, event, attrs, sources, evidence, confidence)
    if kind == "quote":
        return _quote_node(
            factory,
            renderer,
            event,
            attrs,
            sources,
            evidence,
            confidence,
            regions_by_line,
        )
    if kind == "code_block":
        from ..core import line_text_tokens, plain_text
        text = str(event.attrs.get("code") or "\n".join(plain_text(line_text_tokens(line)) for line in event.lines))
        return factory.make("code_block", text=text, attrs=attrs, confidence=confidence, evidence=evidence, sources=sources)
    if kind == "thematic_break":
        return factory.make("thematic_break", attrs=attrs, confidence=confidence, evidence=evidence, sources=sources or [SourceRef(page=event.page)])
    if kind == "table":
        node = build_table_node(factory, converter, renderer, event, regions_by_line)
        node.attrs["_layout_markdown"] = event.legacy_markdown
        node.attrs["_layout_kind"] = event.kind
        alignments = semantic.get("table_alignments")
        if not isinstance(alignments, list):
            alignments = gfm_table_alignments(event.legacy_markdown)
        alignments = [
            str(value) if str(value) in {"", "left", "center", "right"} else ""
            for value in alignments
        ]
        alignments = _project_table_alignments(alignments, node)
        if alignments:
            node.attrs["column_alignments"] = alignments
            for row in node.children:
                if row.kind != "table_row":
                    continue
                for cell in row.children:
                    if cell.kind != "table_cell":
                        continue
                    try:
                        column = int(cell.attrs.get("col", 0))
                        colspan = max(1, int(cell.attrs.get("colspan", 1)))
                    except (TypeError, ValueError, OverflowError):
                        continue
                    covered = alignments[column : column + colspan]
                    if covered and len(set(covered)) == 1 and covered[0]:
                        cell.attrs["alignment"] = covered[0]
            node.evidence.append(
                Evidence(
                    "layout_table_column_alignment",
                    0.96,
                    page=event.page,
                    data={"alignments": alignments},
                )
            )
        if lossless_html and not node.attrs.get("pruned_vacuous_edge_columns"):
            node.attrs["_layout_html"] = lossless_html
        return node
    if kind == "figure":
        return _figure_node(factory, converter, event, attrs, sources, evidence, confidence, regions_by_line)
    if kind in {"callout", "equation"}:
        children = [factory.make("paragraph", children=_paragraph_inlines(factory, renderer, event.lines), confidence=confidence, sources=sources)] if event.lines else []
        return factory.make(kind, children=children, text="" if children else _strip_generated_html(generated_html), attrs=attrs, confidence=confidence, evidence=evidence, sources=sources or [SourceRef(page=event.page)])
    if kind == "columns":
        children = [factory.make("paragraph", children=_paragraph_inlines(factory, renderer, [line]), attrs={"writing_mode": line.writing_mode}, confidence=0.90, sources=sources_from_lines([line], regions_by_line)) for line in event.lines]
        return factory.make("section", children=children, attrs=dict(attrs, layout="columns"), confidence=0.90, evidence=evidence, sources=sources)
    if kind == "form_appearance":
        return factory.make("form", children=[factory.make("paragraph", children=_paragraph_inlines(factory, renderer, event.lines), confidence=0.78, sources=sources)], attrs=dict(attrs, source="printed_appearance", interactive=False), confidence=0.78, evidence=evidence, sources=sources)
    return factory.make("unknown", text=plain_event_text or _strip_generated_html(generated_html), attrs=attrs, confidence=min(confidence, 0.55), evidence=evidence, sources=sources or [SourceRef(page=event.page)])


def _paragraph_inlines(factory: NodeFactory, renderer: Any, lines: Sequence[Any]) -> List[SemanticNode]:
    from ..core import hyphen_join_mode, line_ends_soft_hyphen, line_text_tokens, plain_text
    tokens: List[Dict[str, Any]] = []
    for index, line in enumerate(lines):
        current = line_text_tokens(line)
        if index and tokens:
            previous_line = lines[index - 1]
            previous_plain = plain_text(line_text_tokens(previous_line)).rstrip()
            current_plain = plain_text(current).lstrip()
            mode = "delete" if line_ends_soft_hyphen(previous_line) and current_plain[:1].islower() else hyphen_join_mode(previous_plain, current_plain)
            if mode == "delete":
                _delete_trailing_hyphen(tokens)
            if not mode:
                separator = renderer._paragraph_separator(previous_line, line, list(lines))
                tokens.append({"text": separator, "style": (False,) * 8, "link": None, "synthetic_space": True, "hard_break": "\n" in separator, "page": line.page, "glyph_ids": (), "mcids": (), "bbox": None})
        tokens.extend(current)
    return inline_nodes_from_tokens(factory, tokens)


def _quote_node(
    factory: NodeFactory,
    renderer: Any,
    event: Any,
    attrs: Dict[str, Any],
    sources: List[SourceRef],
    evidence: List[Evidence],
    confidence: float,
    regions_by_line: Dict[str, Tuple[str, ...]],
) -> SemanticNode:
    """Materialize native quote children from the analyzer's quote record.

    Quote bars are graphics rather than text in several supported producer
    dialects. The layout analyzer has already reconciled those bars with list
    markers, nesting depth, and fenced code into format-neutral event records.
    Consume those closed records into typed nodes here; HTML is rendered
    directly from the semantic graph and never from the document Markdown.
    """
    records = _canonical_quote_records(event)
    children = _quote_children(
        factory,
        renderer,
        event,
        records,
        1,
        regions_by_line,
    )
    if not children:
        children = [
            factory.make(
                "paragraph",
                children=_paragraph_inlines(factory, renderer, [line]),
                confidence=0.94,
                sources=sources_from_lines([line], regions_by_line),
            )
            for line in event.lines
        ]
    return factory.make(
        "quote",
        children=children,
        attrs=attrs,
        confidence=confidence,
        evidence=evidence,
        sources=sources,
    )


def _canonical_quote_records(event: Any) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    source_index = 0
    semantic = _event_semantic(event)
    raw_records = semantic.get("quote_records")
    if not isinstance(raw_records, list):
        raw_records = canonical_quote_records(event.legacy_markdown)
    for raw_record in raw_records:
        if not isinstance(raw_record, dict):
            return []
        try:
            depth = int(raw_record["depth"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return []
        content = str(raw_record.get("content", ""))
        stripped = content.strip()
        is_fence = bool(
            raw_record.get("fence")
            or re.match(r"^(`{3,}|~{3,})", stripped)
        )
        source_line = None
        if stripped and not is_fence and source_index < len(event.lines):
            source_line = event.lines[source_index]
            source_index += 1
        records.append(
            {
                "depth": depth,
                "content": content,
                "source_line": source_line,
                "fence": is_fence,
            }
        )
    return records


def _quote_children(
    factory: NodeFactory,
    renderer: Any,
    event: Any,
    records: Sequence[Dict[str, Any]],
    depth: int,
    regions_by_line: Dict[str, Tuple[str, ...]],
) -> List[SemanticNode]:
    children: List[SemanticNode] = []
    index = 0
    marker_re = re.compile(
        r"^( *)(-|\d+\.|[A-Za-z]+\.)(?:\s+\[([ xX])\])?\s+(.+)$"
    )
    while index < len(records):
        record = records[index]
        record_depth = int(record["depth"])
        content = str(record["content"])
        stripped = content.strip()
        if record_depth < depth or not stripped:
            index += 1
            continue
        if record_depth > depth:
            end = index
            while end < len(records) and int(records[end]["depth"]) > depth:
                end += 1
            nested_records = records[index:end]
            nested_children = _quote_children(
                factory,
                renderer,
                event,
                nested_records,
                depth + 1,
                regions_by_line,
            )
            nested_lines = [
                item["source_line"]
                for item in nested_records
                if item.get("source_line") is not None
            ]
            children.append(
                factory.make(
                    "quote",
                    children=nested_children,
                    attrs={"level": depth + 1, "page": event.page},
                    confidence=0.96,
                    evidence=[
                        Evidence(
                            "canonical_quote_depth",
                            0.97,
                            page=event.page,
                            data={"depth": depth + 1},
                        )
                    ],
                    sources=sources_from_lines(
                        nested_lines,
                        regions_by_line,
                    )
                    or [SourceRef(page=event.page)],
                )
            )
            index = end
            continue
        if record.get("fence"):
            delimiter_match = re.match(r"^(`{3,}|~{3,})(.*)$", stripped)
            delimiter = delimiter_match.group(1) if delimiter_match else "```"
            info = delimiter_match.group(2).strip() if delimiter_match else ""
            end = index + 1
            code_records: List[Dict[str, Any]] = []
            while end < len(records):
                candidate = records[end]
                if (
                    int(candidate["depth"]) == depth
                    and str(candidate["content"]).strip().startswith(delimiter)
                ):
                    end += 1
                    break
                code_records.append(candidate)
                end += 1
            code_lines = [
                item["source_line"]
                for item in code_records
                if item.get("source_line") is not None
            ]
            children.append(
                factory.make(
                    "code_block",
                    text="\n".join(str(item["content"]) for item in code_records),
                    attrs={"info": info, "inside_quote": True, "page": event.page},
                    confidence=0.97,
                    evidence=[
                        Evidence(
                            "canonical_quote_fence",
                            0.98,
                            page=event.page,
                        )
                    ],
                    sources=sources_from_lines(code_lines, regions_by_line)
                    or [SourceRef(page=event.page)],
                )
            )
            index = end
            continue
        if marker_re.match(content.expandtabs(4)):
            end = index
            list_records: List[Dict[str, Any]] = []
            while (
                end < len(records)
                and int(records[end]["depth"]) == depth
                and marker_re.match(
                    str(records[end]["content"]).expandtabs(4)
                )
            ):
                list_records.append(records[end])
                end += 1
            list_lines = [
                item["source_line"]
                for item in list_records
                if item.get("source_line") is not None
            ]
            list_sources = sources_from_lines(list_lines, regions_by_line)
            list_event = SimpleNamespace(
                page=event.page,
                lines=list_lines,
                legacy_markdown="\n".join(
                    str(item["content"]) for item in list_records
                ),
                semantic={
                    "list_records": canonical_list_records(
                        "\n".join(
                            str(item["content"]) for item in list_records
                        )
                    )
                },
            )
            list_node = _canonical_list_node(
                factory,
                renderer,
                list_event,
                {"inside_quote": True, "page": event.page},
                list_sources or [SourceRef(page=event.page)],
                [
                    Evidence(
                        "canonical_quote_list",
                        0.97,
                        page=event.page,
                    )
                ],
                0.96,
            )
            if list_node is not None:
                children.append(list_node)
                index = end
                continue
        if re.fullmatch(r"(?:-{3,}|\*{3,}|_{3,})", stripped):
            source_line = record.get("source_line")
            children.append(
                factory.make(
                    "thematic_break",
                    attrs={"inside_quote": True, "page": event.page},
                    confidence=0.96,
                    evidence=[
                        Evidence(
                            "canonical_quote_thematic_break",
                            0.97,
                            page=event.page,
                        )
                    ],
                    sources=sources_from_lines(
                        [source_line] if source_line is not None else [],
                        regions_by_line,
                    )
                    or [SourceRef(page=event.page)],
                )
            )
            index += 1
            continue

        end = index
        paragraph_records: List[Dict[str, Any]] = []
        while end < len(records):
            candidate = records[end]
            candidate_content = str(candidate["content"])
            candidate_stripped = candidate_content.strip()
            if (
                int(candidate["depth"]) != depth
                or not candidate_stripped
                or candidate.get("fence")
                or marker_re.match(candidate_content.expandtabs(4))
                or re.fullmatch(
                    r"(?:-{3,}|\*{3,}|_{3,})",
                    candidate_stripped,
                )
            ):
                break
            paragraph_records.append(candidate)
            end += 1
        paragraph_lines = [
            item["source_line"]
            for item in paragraph_records
            if item.get("source_line") is not None
        ]
        paragraph_sources = sources_from_lines(
            paragraph_lines,
            regions_by_line,
        )
        if paragraph_lines:
            inline_children = _paragraph_inlines(
                factory,
                renderer,
                paragraph_lines,
            )
        else:
            fallback_text = " ".join(
                _canonical_list_text(str(item["content"]).strip())
                for item in paragraph_records
            ).strip()
            inline_children = (
                [
                    factory.make(
                        "text",
                        text=fallback_text,
                        confidence=0.75,
                        sources=[],
                    )
                ]
                if fallback_text
                else []
            )
        children.append(
            factory.make(
                "paragraph",
                children=inline_children,
                attrs={"inside_quote": True, "page": event.page},
                confidence=0.95,
                evidence=[
                    Evidence(
                        "canonical_quote_paragraph",
                        0.96,
                        page=event.page,
                    )
                ],
                sources=paragraph_sources or [SourceRef(page=event.page)],
            )
        )
        index = max(end, index + 1)
    return children


def _list_node(factory: NodeFactory, renderer: Any, event: Any, attrs: Dict[str, Any], sources: List[SourceRef], evidence: List[Evidence], confidence: float) -> SemanticNode:
    from ..core import line_text_tokens, list_marker, plain_text

    canonical = _canonical_list_node(
        factory,
        renderer,
        event,
        attrs,
        sources,
        evidence,
        confidence,
    )
    if canonical is not None:
        return canonical

    entries: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for line in event.lines:
        tokens = line_text_tokens(line)
        text = plain_text(tokens).strip()
        marker = list_marker(text)
        visual = None if marker else renderer._visual_list_marker(line)
        if marker or visual is not None:
            marker_kind = marker[0] if marker else "ul"
            marker_value = marker[2] if marker else getattr(visual, "kind", "bullet")
            marker_end = int(marker[1]) if marker else 0
            body_tokens = _strip_marker_tokens(tokens, marker_end)
            body_x = _first_token_x(body_tokens, line.x0)
            marker_x = renderer._drawn_list_marker_x(line) if visual is not None else line.x0
            current = {
                "line": line,
                "lines": [line],
                "tokens": list(body_tokens),
                "indent": float(marker_x if marker_x is not None else body_x),
                "body_x": body_x,
                "ordered": marker_kind == "ol",
                "value": marker_value,
                "checked": marker_value in {"task-checked", "☑", "☒", "✓", "✔", "✗", "✘"},
                "task": str(marker_value).startswith("task-") or marker_value in {"☐", "☑", "☒", "□", "✓", "✔", "✗", "✘"},
            }
            entries.append(current)
        elif current is not None:
            previous = current["lines"][-1]
            current["lines"].append(line)
            separator = renderer._paragraph_separator(previous, line, current["lines"])
            current["tokens"].append({
                "text": separator,
                "style": (False,) * 8,
                "link": None,
                "synthetic_space": True,
                "hard_break": "\n" in separator,
                "page": line.page,
                "glyph_ids": (),
                "mcids": (),
                "bbox": None,
            })
            current["tokens"].extend(tokens)
    if not entries:
        return factory.make("paragraph", children=_paragraph_inlines(factory, renderer, event.lines), attrs=attrs, confidence=min(confidence, 0.70), evidence=evidence, sources=sources)

    tolerance = max(4.0, min(getattr(entry["line"], "size", 10.0) for entry in entries) * 0.45)
    top_lists: List[SemanticNode] = []
    # stack entry: indent, active list, containing parent item (None at root), last item
    stack: List[Tuple[float, SemanticNode, Optional[SemanticNode], Optional[SemanticNode]]] = []

    def make_list(entry: Dict[str, Any], level: int, parent_item: Optional[SemanticNode]) -> SemanticNode:
        list_sources = sources_from_lines(entry["lines"])
        node = factory.make(
            "list",
            attrs={
                **(attrs if level == 0 else {}),
                "ordered": bool(entry["ordered"]),
                "start": _numeric_start(entry["value"]),
                "tight": True,
                "level": level,
            },
            confidence=confidence if level == 0 else min(confidence, 0.92),
            evidence=list(evidence) if level == 0 else [Evidence("list_hanging_indent", 0.92, page=entry["line"].page, data={"indent": entry["indent"]})],
            sources=sources if level == 0 else list_sources,
        )
        if parent_item is None:
            top_lists.append(node)
        else:
            parent_item.children.append(node)
        return node

    for entry in entries:
        indent = float(entry["indent"])
        if not stack:
            initial = make_list(entry, 0, None)
            stack.append((indent, initial, None, None))
        else:
            while len(stack) > 1 and indent < stack[-1][0] - tolerance:
                stack.pop()
            if indent > stack[-1][0] + tolerance and stack[-1][3] is not None:
                parent_item = stack[-1][3]
                nested = make_list(entry, len(stack), parent_item)
                stack.append((indent, nested, parent_item, None))
            else:
                # Snap small geometric drift to the active level. If the marker
                # family changes, start a sibling list rather than inserting a
                # list node where a list item is required.
                if abs(indent - stack[-1][0]) <= tolerance:
                    indent = stack[-1][0]
                if bool(entry["ordered"]) != bool(stack[-1][1].attrs.get("ordered")):
                    parent_item = stack[-1][2]
                    sibling = make_list(entry, len(stack) - 1, parent_item)
                    stack[-1] = (indent, sibling, parent_item, None)

        current_list = stack[-1][1]
        item_attrs = {"marker": entry["value"]}
        if entry["task"]:
            item_attrs.update({"task": True, "checked": bool(entry["checked"])})
        item = factory.make(
            "item",
            children=inline_nodes_from_tokens(factory, entry["tokens"]),
            attrs=item_attrs,
            confidence=0.95,
            evidence=[Evidence("list_marker_and_indent", 0.95, page=entry["line"].page, data={"indent": indent})],
            sources=sources_from_lines(entry["lines"]),
        )
        current_list.children.append(item)
        stack[-1] = (stack[-1][0], current_list, stack[-1][2], item)

    if len(top_lists) == 1:
        top_lists[0].sources = merge_sources(sources)
        return top_lists[0]
    return factory.make(
        "section",
        children=top_lists,
        attrs={**attrs, "semantic_group": "list_sequence"},
        confidence=min(node.confidence for node in top_lists),
        evidence=[Evidence("list_marker_family_sequence", 0.90, page=event.page)],
        sources=merge_sources(sources),
    )


def _canonical_list_node(
    factory: NodeFactory,
    renderer: Any,
    event: Any,
    attrs: Dict[str, Any],
    sources: List[SourceRef],
    evidence: List[Evidence],
    confidence: float,
) -> Optional[SemanticNode]:
    """Build a typed nested list from the analyzer's canonical list record.

    The layout analyzer has already reconciled explicit glyph markers, drawn
    bullets, hanging indents, and continuation lines when it creates a list
    event.  Consuming that closed record here prevents a second, weaker marker
    inference pass from flattening nested lists or losing task state.
    """
    entries: List[Dict[str, Any]] = []
    stack: List[Dict[str, Any]] = []
    line_cursor = 0
    semantic = _event_semantic(event)
    records = semantic.get("list_records")
    if not isinstance(records, list):
        records = canonical_list_records(event.legacy_markdown)
    for record in records:
        if not isinstance(record, dict):
            return None
        source_line = (
            event.lines[line_cursor]
            if line_cursor < len(event.lines)
            else None
        )
        line_cursor += int(source_line is not None)
        if record.get("kind") == "continuation":
            if not entries:
                return None
            entries[-1]["content_lines"].append(
                str(record.get("content", "")).strip()
            )
            if source_line is not None:
                entries[-1]["lines"].append(source_line)
            continue
        if record.get("kind") != "item":
            return None
        try:
            indent = max(0, int(record.get("indent", 0)))
        except (TypeError, ValueError, OverflowError):
            return None
        ordered = bool(record.get("ordered"))
        marker = record.get("marker", "-" if not ordered else 1)
        entry: Dict[str, Any] = {
            "indent": indent,
            "ordered": ordered,
            "marker": marker,
            "task": bool(record.get("task")),
            "checked": bool(record.get("checked")),
            "content_lines": [str(record.get("content", "")).strip()],
            "lines": [source_line] if source_line is not None else [],
            "children": [],
            "parented": False,
        }
        while stack and indent <= int(stack[-1]["indent"]):
            stack.pop()
        if stack:
            stack[-1]["children"].append(entry)
            entry["parented"] = True
        stack.append(entry)
        entries.append(entry)

    roots = [entry for entry in entries if not entry["parented"]]
    if not roots:
        return None

    def item_inlines(entry: Dict[str, Any]) -> List[SemanticNode]:
        from ..core import line_text_tokens, list_marker, plain_text

        tokens: List[Dict[str, Any]] = []
        for index, line in enumerate(entry["lines"]):
            current = line_text_tokens(line)
            if index == 0:
                physical = list_marker(plain_text(current).strip())
                if physical is not None:
                    current = _strip_marker_tokens(current, int(physical[1]))
            if index and tokens:
                separator = renderer._paragraph_separator(
                    entry["lines"][index - 1],
                    line,
                    entry["lines"],
                )
                tokens.append(
                    {
                        "text": separator,
                        "style": (False,) * 8,
                        "link": None,
                        "synthetic_space": True,
                        "hard_break": "\n" in separator,
                        "page": line.page,
                        "glyph_ids": (),
                        "mcids": (),
                        "bbox": None,
                    }
                )
            tokens.extend(current)
        if tokens:
            return inline_nodes_from_tokens(factory, tokens)
        text = " ".join(str(value) for value in entry["content_lines"]).strip()
        return [
            factory.make(
                "text",
                text=_canonical_list_text(text),
                confidence=0.80,
                sources=[],
            )
        ] if text else []

    def build_lists(
        siblings: Sequence[Dict[str, Any]],
        level: int,
        root: bool,
    ) -> List[SemanticNode]:
        lists: List[SemanticNode] = []
        marker_styles = _canonical_marker_styles(siblings)
        index = 0
        while index < len(siblings):
            signature = (
                bool(siblings[index]["ordered"]),
                marker_styles[index],
            )
            group: List[Dict[str, Any]] = []
            while index < len(siblings):
                candidate = siblings[index]
                candidate_signature = (
                    bool(candidate["ordered"]),
                    marker_styles[index],
                )
                if candidate_signature != signature:
                    break
                group.append(candidate)
                index += 1
            item_nodes: List[SemanticNode] = []
            for entry in group:
                entry_sources = sources_from_lines(entry["lines"])
                item_attrs = {"marker": entry["marker"]}
                if entry["task"]:
                    item_attrs.update(
                        {"task": True, "checked": bool(entry["checked"])}
                    )
                item = factory.make(
                    "item",
                    children=item_inlines(entry),
                    attrs=item_attrs,
                    confidence=0.96,
                    evidence=[
                        Evidence(
                            "canonical_list_item",
                            0.96,
                            page=event.page,
                            data={"indent": entry["indent"]},
                        )
                    ],
                    sources=entry_sources or [SourceRef(page=event.page)],
                )
                item.children.extend(
                    build_lists(entry["children"], level + 1, False)
                )
                item_nodes.append(item)
            list_sources = merge_sources(
                source
                for item in item_nodes
                for source in item.sources
            )
            list_attrs: Dict[str, Any] = {
                **(attrs if root and not lists else {}),
                "ordered": signature[0],
                "start": _numeric_start(group[0]["marker"]),
                "marker_style": signature[1],
                "tight": True,
                "level": level,
            }
            lists.append(
                factory.make(
                    "list",
                    children=item_nodes,
                    attrs=list_attrs,
                    confidence=confidence,
                    evidence=list(evidence)
                    + [
                        Evidence(
                            "canonical_layout_list_structure",
                            0.97,
                            page=event.page,
                        )
                    ],
                    sources=list_sources or list(sources),
                )
            )
        return lists

    top_lists = build_lists(roots, 0, True)
    if len(top_lists) == 1:
        return top_lists[0]
    return factory.make(
        "section",
        children=top_lists,
        attrs={**attrs, "semantic_group": "list_sequence"},
        confidence=min(node.confidence for node in top_lists),
        evidence=[
            Evidence("canonical_list_family_sequence", 0.96, page=event.page)
        ],
        sources=merge_sources(sources),
    )


def _canonical_marker_style(marker: Any) -> str:
    if isinstance(marker, int) and not isinstance(marker, bool):
        return "decimal"
    value = str(marker)
    if len(value) > 1 and re.fullmatch(r"[ivxlcdmIVXLCDM]+", value):
        return "upper-roman" if value.isupper() else "lower-roman"
    if re.fullmatch(r"[A-Za-z]", value):
        return "upper-alpha" if value.isupper() else "lower-alpha"
    return "disc"


def _canonical_marker_styles(
    siblings: Sequence[Dict[str, Any]],
) -> List[str]:
    styles = [
        _canonical_marker_style(entry["marker"])
        for entry in siblings
    ]
    for index, entry in enumerate(siblings):
        marker = str(entry["marker"])
        if (
            not entry["ordered"]
            or not re.fullmatch(r"[ivxlcdmIVXLCDM]", marker)
        ):
            continue
        roman_style = "upper-roman" if marker.isupper() else "lower-roman"
        previous_is_roman = index > 0 and styles[index - 1] == roman_style
        next_is_roman = (
            index + 1 < len(styles)
            and styles[index + 1] == roman_style
        )
        if previous_is_roman or next_is_roman:
            styles[index] = roman_style
    return styles


def _canonical_list_text(text: str) -> str:
    value = re.sub(r"\\([\\`*_[\]{}()#+.!|<>~-])", r"\1", text)
    value = re.sub(r"(`+)(.*?)\1", r"\2", value)
    value = re.sub(r"(\*\*\*|\*\*|\*|~~)(.*?)\1", r"\2", value)
    return re.sub(r"<[^>]+>", "", value)


def _strip_marker_tokens(tokens: Sequence[Dict[str, Any]], count: int) -> List[Dict[str, Any]]:
    remaining = max(0, count)
    out: List[Dict[str, Any]] = []
    for original in tokens:
        token = dict(original)
        text = str(token.get("text", ""))
        if remaining >= len(text):
            remaining -= len(text)
            continue
        if remaining:
            token["text"] = text[remaining:]
            remaining = 0
        out.append(token)
    while out and not str(out[0].get("text", "")).strip():
        if str(out[0].get("text", "")):
            out[0]["text"] = str(out[0]["text"]).lstrip()
            break
        out.pop(0)
    return out


def _first_token_x(tokens: Sequence[Dict[str, Any]], fallback: float) -> float:
    for token in tokens:
        bbox = token.get("bbox")
        if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
            return float(bbox[0])
    return float(fallback)


def _numeric_start(value: Any) -> int:
    return int(value) if isinstance(value, int) and value > 0 else 1


def _figure_node(factory: NodeFactory, converter: Any, event: Any, attrs: Dict[str, Any], sources: List[SourceRef], evidence: List[Evidence], confidence: float, regions_by_line: Dict[str, Tuple[str, ...]]) -> SemanticNode:
    image = event.attrs.get("image")
    if image is None:
        return factory.make("figure", attrs=attrs, confidence=confidence, evidence=evidence, sources=sources)
    page_width = converter.page_sizes.get(image.page, (612.0, 792.0))[0]
    from ..core import image_alignment, image_source as render_image_source
    object_refs = tuple(value for value in (getattr(image, "object_ref", None), getattr(image, "link_object_ref", None), image.name) if value)
    image_source = SourceRef(
        page=image.page,
        glyph_ids=tuple(getattr(image, "glyph_ids", ()) or ()),
        mcids=tuple(getattr(image, "mcids", ()) or ()),
        object_refs=object_refs,
        bbox=(image.x0, image.y0, image.x1, image.y1),
    )
    evidence_kind = {
        "vector": "pdf_vector_artwork",
        "formula": "pdf_vector_formula",
    }.get(image.kind, "pdf_image_xobject")
    image_confidence = 0.96 if image.kind in {"vector", "formula"} else 0.99
    image_node = factory.make(
        "image",
        attrs={
            "src": render_image_source(image, converter.options),
            "alt": image.alt or "",
            "kind": image.kind,
            "intrinsic_width": image.intrinsic_width,
            "intrinsic_height": image.intrinsic_height,
            "display_width_pt": image.placed_width or image.x1 - image.x0,
            "display_height_pt": image.placed_height or image.y1 - image.y0,
            "alignment": image_alignment(image, page_width),
            "quad": image.quad,
            "link": image.link,
            "text_extraction_attempted": False,
            "marked_content_tags": list(getattr(image, "tags", ()) or ()),
        },
        confidence=image_confidence,
        evidence=[
            Evidence(
                evidence_kind,
                image_confidence,
                page=image.page,
                data={"asset": image.name, "kind": image.kind},
            )
        ],
        sources=[image_source],
    )
    children = [image_node]
    caption_text = str(event.attrs.get("caption") or "")
    if caption_text:
        caption_sources = sources_from_lines(event.lines, regions_by_line)
        caption = factory.make("caption", children=_paragraph_inlines(factory, _IdentitySeparator(), event.lines), confidence=0.94, evidence=[Evidence("figure_caption_pair", 0.94, page=image.page)], sources=caption_sources)
        children.append(caption)
        match = re.match(r"^(?:Figure|Fig\.|Chart|Listing|Exhibit)\s+([A-Za-z0-9IVXLC.-]+)", caption_text, re.I)
        if match:
            attrs["label"] = match.group(1)
            attrs["caption"] = caption_text
    return factory.make("figure", children=children, attrs=attrs, confidence=confidence, evidence=evidence, sources=merge_sources(sources + [image_source]))


class _IdentitySeparator:
    @staticmethod
    def _paragraph_separator(_previous: Any, _current: Any, _lines: Sequence[Any]) -> str:
        return " "


def _merge_cross_page_paragraphs(nodes: Sequence[SemanticNode], renderer: Any, page_breaks: bool) -> List[SemanticNode]:
    if page_breaks:
        return list(nodes)
    out: List[SemanticNode] = []
    for node in nodes:
        if node.kind == "paragraph" and out and out[-1].kind == "paragraph":
            left, right = out[-1], node
            left_pages, right_pages = left.source_pages(), right.source_pages()
            if left_pages and right_pages and right_pages[0] == left_pages[-1] + 1 and renderer._page_boundary_continuation(left_pages[-1], right_pages[0]):
                join_sources = merge_sources((left.sources[-1:] or right.sources[:1]))
                left.children.append(SemanticNode(id=left.id + "-join", kind="text", text=" ", confidence=1.0, sources=join_sources))
                left.children.extend(right.children)
                left.sources = merge_sources(left.sources + right.sources)
                left.evidence.append(Evidence("cross_page_paragraph_continuation", 0.90, data={"pages": left.source_pages()}))
                left.confidence = min(left.confidence, right.confidence, 0.90)
                continue
        out.append(node)
    return out


def _assign_heading_anchors(document: SemanticDocument) -> None:
    seen: Dict[str, int] = {}
    for node in document.walk():
        if node.kind != "heading":
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", _node_text(node).casefold()).strip("-") or node.id
        count = seen.get(slug, 0) + 1
        seen[slug] = count
        node.attrs["anchor"] = slug if count == 1 else "%s-%d" % (slug, count)


def _event_confidence(kind: str, region_kinds: Sequence[str]) -> float:
    base = {"heading": 0.92, "paragraph": 0.96, "list": 0.90, "quote": 0.88, "code_block": 0.94, "thematic_break": 0.90, "figure": 0.95, "callout": 0.82, "equation": 0.78, "columns": 0.88, "form_appearance": 0.75}.get(kind, 0.80)
    if kind in region_kinds:
        base += 0.04
    return max(0.0, min(0.99, base))


def _bbox(lines: Sequence[Any]):
    if not lines:
        return None
    return (min(line.x0 for line in lines), min(line.y0 for line in lines), max(line.x1 for line in lines), max(line.y1 for line in lines))


def _delete_trailing_hyphen(tokens: List[Dict[str, Any]]) -> None:
    for token in reversed(tokens):
        text = str(token.get("text", ""))
        if not text:
            continue
        if text.endswith(("-", "\u00ad")):
            token["text"] = text[:-1]
        return


def _line_plain(line: Any) -> str:
    from ..core import line_text_tokens, plain_text
    return plain_text(line_text_tokens(line)).strip()


def _contains_rtl(text: str) -> bool:
    import unicodedata
    return any(unicodedata.bidirectional(character) in {"R", "AL", "RLE", "RLO", "RLI"} for character in text)


def _rtl_dominant(text: str) -> bool:
    import unicodedata
    rtl = sum(1 for character in text if unicodedata.bidirectional(character) in {"R", "AL"})
    ltr = sum(1 for character in text if unicodedata.bidirectional(character) == "L")
    return rtl > ltr


def _strip_generated_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def _lossless_html_for_event(kind: str, markup: str) -> str:
    """Retain only closed, internally generated HTML-specific constructs.

    Ordinary paragraphs, headings, lists, and figures are projected from typed
    semantic nodes.  These layout constructs currently carry
    browser semantics or geometry that cannot be represented losslessly in the
    generic node attributes alone, so their already-escaped generated fragment
    remains an explicitly verified fallback.
    """
    if kind not in {"columns", "form_appearance", "callout", "equation", "table"}:
        return ""
    from ..html.sanitize import is_safe_generated_html

    return markup if is_safe_generated_html(markup.strip()) else ""


def _node_text(node: SemanticNode) -> str:
    return node.text or "".join(_node_text(child) for child in node.children)


def _metadata_title(document: Any) -> str:
    return _metadata_value(document, "Title")


def _metadata_value(document: Any, key: str) -> str:
    trailer = getattr(document, "trailer", {})
    info = document.resolve(trailer.get("Info")) if isinstance(trailer, dict) else None
    value = document.resolve(info.get(key)) if isinstance(info, dict) else None
    if isinstance(value, bytes):
        from ..core import decode_pdf_text
        return decode_pdf_text(value)
    return str(value) if isinstance(value, str) else ""


def _catalog_language(document: Any) -> str:
    catalog = document.catalog()
    value = document.resolve(catalog.get("Lang")) if isinstance(catalog, dict) else None
    if isinstance(value, bytes):
        from ..core import decode_pdf_text

        return decode_pdf_text(value).strip()
    return value.strip() if isinstance(value, str) else ""
