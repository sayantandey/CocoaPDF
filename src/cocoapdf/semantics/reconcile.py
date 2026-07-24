from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence, Tuple

from ..ir.evidence import Evidence
from ..ir.semantic import SemanticDocument, SemanticNode, SourceRef


def reconcile_tagged_content(
	document: SemanticDocument,
	characters: Iterable[Any],
) -> SemanticDocument:
	"""Bind tagged MCID leaves to emitted characters without guessing Unicode."""
	by_key: Dict[Tuple[int, int], List[Any]] = {}
	for character in characters:
		page = int(getattr(character, "page", 0) or 0)
		if page <= 0 or getattr(character, "artifact", False):
			continue
		for mark in getattr(character, "mc", ()) or ():
			if not isinstance(mark, dict):
				continue
			mcid = mark.get("mcid")
			if isinstance(mcid, int) and not isinstance(mcid, bool):
				by_key.setdefault((page, mcid), []).append(character)

	for node in document.walk():
		mcid = node.attrs.get("mcid")
		if not isinstance(mcid, int) or isinstance(mcid, bool):
			continue
		pages = node.source_pages()
		if not pages:
			node.warnings.append("TAGGED_MCID_PAGE_UNKNOWN")
			continue
		matches: List[Any] = []
		for page in pages:
			matches.extend(by_key.get((page, mcid), []))
		if not matches:
			node.warnings.append("TAGGED_MCID_UNBOUND")
			node.confidence = min(node.confidence, 0.45)
			continue
		matches = _dedupe_characters(matches)
		matches.sort(key=lambda char: (int(getattr(char, "page", 0)), int(getattr(char, "seq", 0)), float(getattr(char, "y0", 0.0)), float(getattr(char, "x0", 0.0))))
		node.text = "".join(str(getattr(char, "text", "")) for char in matches)
		node.sources = _sources(matches, mcid)
		node.evidence.append(Evidence("tag_geometry_bound", 0.99, data={"mcid": mcid, "glyph_count": len(matches)}))
		node.confidence = max(node.confidence, 0.99)
	return document


def _dedupe_characters(characters: Sequence[Any]) -> List[Any]:
	out: List[Any] = []
	seen = set()
	for character in characters:
		key = (
			int(getattr(character, "page", 0)),
			int(getattr(character, "seq", 0)),
			str(getattr(character, "text", "")),
			round(float(getattr(character, "x0", 0.0)), 3),
			round(float(getattr(character, "y0", 0.0)), 3),
		)
		if key in seen:
			continue
		seen.add(key)
		out.append(character)
	return out


def _sources(characters: Sequence[Any], mcid: int) -> List[SourceRef]:
	by_page: Dict[int, List[Any]] = {}
	for character in characters:
		by_page.setdefault(int(getattr(character, "page", 0)), []).append(character)
	out = []
	for page, items in sorted(by_page.items()):
		x0 = min(float(getattr(char, "x0", 0.0)) for char in items)
		y0 = min(float(getattr(char, "y0", 0.0)) for char in items)
		x1 = max(float(getattr(char, "x1", x0)) for char in items)
		y1 = max(float(getattr(char, "y1", y0)) for char in items)
		out.append(SourceRef(
			page=page,
			glyph_ids=tuple(sorted({int(getattr(char, "seq", 0)) for char in items})),
			mcids=(mcid,),
			bbox=(x0, y0, x1, y1),
		))
	return out


def reconcile_semantic_graph(
    geometric: SemanticDocument,
    tagged: SemanticDocument,
    characters: Iterable[Any],
) -> SemanticDocument:
    """Reconcile a geometric semantic graph with the PDF structure tree.

    Tagged roles are accepted only when they bind to the same source MCIDs or
    object references as a geometric node.  Geometry remains authoritative for
    physical inclusion and table cell bounds; tags are authoritative for role,
    hierarchy, ActualText, Alt, language, list numbering, and table attributes
    when the binding is unambiguous.
    """
    reconcile_tagged_content(tagged, characters)
    geometric_nodes = list(geometric.walk())
    by_mcid: Dict[Tuple[int, int], List[SemanticNode]] = {}
    by_object: Dict[str, List[SemanticNode]] = {}
    for node in geometric_nodes:
        for source in node.sources:
            for mcid in source.mcids:
                by_mcid.setdefault((source.page, mcid), []).append(node)
            for object_ref in source.object_refs:
                by_object.setdefault(object_ref, []).append(node)
    bindings: Dict[str, SemanticNode] = {}
    conflicts: List[str] = []
    artifacts: set[str] = set()
    for tagged_node in tagged.walk():
        keys = _tagged_keys(tagged_node)
        candidates: Dict[str, Tuple[SemanticNode, int]] = {}
        for page, mcid in keys:
            for candidate in by_mcid.get((page, mcid), []):
                existing = candidates.get(candidate.id)
                candidates[candidate.id] = (candidate, (existing[1] if existing else 0) + 1)
        for source in tagged_node.sources:
            for object_ref in source.object_refs:
                for candidate in by_object.get(object_ref, []):
                    existing = candidates.get(candidate.id)
                    candidates[candidate.id] = (candidate, (existing[1] if existing else 0) + 2)
        if not candidates:
            continue
        ranked = sorted(candidates.values(), key=lambda item: (_binding_score(tagged_node, item[0], item[1]), -len(item[0].sources)), reverse=True)
        best, count = ranked[0]
        score = _binding_score(tagged_node, best, count)
        if score < 0.55:
            conflicts.append("TAGGED_GEOMETRY_LOW_SCORE:%s" % tagged_node.id)
            continue
        if len(ranked) > 1 and abs(score - _binding_score(tagged_node, ranked[1][0], ranked[1][1])) < 0.04:
            conflicts.append("TAGGED_GEOMETRY_AMBIGUOUS:%s" % tagged_node.id)
            continue
        bindings[tagged_node.id] = best
        if tagged_node.kind == "artifact":
            artifacts.add(best.id)
            continue
        _apply_tagged_prior(best, tagged_node, score)
    _materialize_tagged_structures(geometric, tagged, bindings, by_mcid, conflicts)
    if artifacts:
        geometric.children = _remove_nodes(geometric.children, artifacts)
    _apply_tagged_order(geometric, tagged, bindings)
    _apply_tagged_child_order(tagged, bindings)
    geometric.metadata["tagged_pdf"] = {
        "present": bool(tagged.children),
        "role_map": tagged.metadata.get("role_map", {}),
        "parent_tree_keys": tagged.metadata.get("parent_tree_keys", []),
        "class_map_keys": tagged.metadata.get("class_map_keys", []),
        "namespace_role_maps": tagged.metadata.get("namespace_role_maps", {}),
        "language": tagged.metadata.get("language"),
        "bindings": len(bindings),
        "conflicts": conflicts,
    }
    geometric.warnings.extend(tagged.warnings)
    geometric.warnings.extend(conflicts)
    return geometric


def _tagged_keys(node: SemanticNode) -> set[Tuple[int, int]]:
    keys: set[Tuple[int, int]] = set()
    for candidate in node.walk():
        mcid = candidate.attrs.get("mcid")
        if not isinstance(mcid, int) or isinstance(mcid, bool):
            continue
        for page in candidate.source_pages():
            keys.add((page, mcid))
    return keys


def _binding_score(tagged: SemanticNode, geometric: SemanticNode, matched: int) -> float:
    role_compatibility = {
        "heading": {"heading", "paragraph"},
        "paragraph": {"paragraph", "heading"},
        "list": {"list", "paragraph", "section"},
        "item": {"item", "paragraph", "text"},
        "table": {"table", "section"},
        "table_row": {"table_row", "paragraph"},
        "table_cell": {"table_cell", "paragraph"},
        "figure": {"figure", "image"},
        "caption": {"caption", "paragraph"},
        "link": {"link", "text", "paragraph"},
        "footnote": {"footnote", "paragraph"},
        "footnote_ref": {"footnote_ref", "superscript", "text"},
        "equation": {"equation", "paragraph", "callout"},
        "section": {"section", "paragraph"},
        "text": {"text", "paragraph", "heading", "table_cell"},
    }
    compatible = geometric.kind in role_compatibility.get(tagged.kind, {tagged.kind, "unknown"})
    exact = geometric.kind == tagged.kind
    tagged_keys = _tagged_keys(tagged)
    coverage = matched / max(len(tagged_keys), 1)
    role_bonus = 0.14 if exact else 0.08 if compatible else -0.18
    return min(1.0, 0.43 + min(0.43, coverage * 0.43) + role_bonus)


def _apply_tagged_prior(geometric: SemanticNode, tagged: SemanticNode, score: float) -> None:
    old_kind = geometric.kind
    if tagged.kind != "unknown" and _role_change_allowed(old_kind, tagged.kind):
        geometric.kind = tagged.kind
    if tagged.attrs.get("tag_role"):
        geometric.attrs["tag_role"] = tagged.attrs.get("tag_role")
    if tagged.attrs.get("raw_tag_role"):
        geometric.attrs["raw_tag_role"] = tagged.attrs.get("raw_tag_role")
    geometric.attrs.setdefault("tagged_node_id", tagged.id)
    for key in ("level", "lang", "alt", "namespace", "structure_id", "expanded_text"):
        if tagged.attrs.get(key) not in (None, ""):
            geometric.attrs[key] = tagged.attrs[key]
    structure = tagged.attrs.get("structure_attributes")
    if isinstance(structure, dict):
        geometric.attrs["structure_attributes"] = structure
        _apply_table_attributes(geometric, structure)
        numbering = structure.get("ListNumbering")
        if numbering and geometric.kind == "list":
            geometric.attrs["list_numbering"] = str(numbering)
            geometric.attrs["ordered"] = str(numbering).lower() not in {"none", "disc", "circle", "square"}
    if tagged.text and tagged.attrs.get("actual_text"):
        replacement = SemanticNode(
            id=geometric.id + "-actual-text",
            kind="text",
            text=tagged.text,
            attrs={"actual_text": True},
            confidence=1.0,
            evidence=list(tagged.evidence),
            sources=list(geometric.sources),
        )
        geometric.text = ""
        geometric.children = [replacement]
        geometric.attrs["actual_text"] = True
    if tagged.attrs.get("alt") and geometric.kind == "figure":
        for child in geometric.children:
            if child.kind == "image":
                child.attrs["alt"] = tagged.attrs["alt"]
    geometric.evidence.append(Evidence("tag_geometry_reconciled", score, detail="%s->%s" % (old_kind, geometric.kind), data={"tagged_node": tagged.id}))
    geometric.confidence = max(geometric.confidence, score)


def _role_change_allowed(old: str, new: str) -> bool:
    if old == new:
        return True
    compatible = {
        "paragraph": {"heading", "item", "caption", "footnote", "equation", "section", "text"},
        "heading": {"paragraph"},
        "section": {"list", "table", "toc", "reference_section"},
        "text": {"link", "footnote_ref", "code", "caption", "strong", "emphasis", "subscript"},
        "callout": {"equation", "sidebar"},
        "image": {"figure"},
    }
    return new in compatible.get(old, set())


def _apply_table_attributes(node: SemanticNode, attributes: Dict[str, Any]) -> None:
    aliases = {
        "RowSpan": "rowspan", "ColSpan": "colspan", "Headers": "headers",
        "Scope": "scope", "Summary": "summary", "Axis": "axis",
    }
    for source, target in aliases.items():
        if source in attributes:
            value = attributes[source]
            if target in {"rowspan", "colspan"}:
                try:
                    value = max(1, int(value))
                except (TypeError, ValueError):
                    continue
            node.attrs[target] = value


def _remove_nodes(nodes: Sequence[SemanticNode], ids: set[str]) -> List[SemanticNode]:
    out: List[SemanticNode] = []
    for node in nodes:
        if node.id in ids:
            continue
        node.children = _remove_nodes(node.children, ids)
        out.append(node)
    return out


def _apply_tagged_order(geometric: SemanticDocument, tagged: SemanticDocument, bindings: Dict[str, SemanticNode]) -> None:
    order: Dict[str, int] = {}
    index = 0
    for tagged_node in tagged.walk():
        bound = bindings.get(tagged_node.id)
        if bound is not None and bound.id not in order:
            order[bound.id] = index
            index += 1
    if not order:
        return
    original = {node.id: position for position, node in enumerate(geometric.children)}
    geometric.children.sort(key=lambda node: (order.get(node.id, 10**9), original.get(node.id, 10**9)))


def _materialize_tagged_structures(
    geometric: SemanticDocument,
    tagged: SemanticDocument,
    bindings: Dict[str, SemanticNode],
    by_mcid: Dict[Tuple[int, int], List[SemanticNode]],
    conflicts: List[str],
) -> None:
    # Geometry remains the source of physical bounds. When a verified structure
    # subtree describes semantics that geometry cannot infer (notably an
    # unmarked list or a borderless tagged table), materialize that subtree only
    # when every referenced MCID is bound and the covered geometric blocks can
    # be replaced without swallowing unrelated content.
    for tagged_node in list(tagged.walk()):
        if tagged_node.kind not in {"list", "table", "toc"}:
            continue
        bound = bindings.get(tagged_node.id)
        if bound is not None and bound.kind == tagged_node.kind:
            continue
        keys = _tagged_keys(tagged_node)
        if not keys or any(key not in by_mcid for key in keys):
            conflicts.append("TAGGED_STRUCTURE_INCOMPLETE_BINDING:%s" % tagged_node.id)
            continue
        replacement = (
            _tagged_list_node(tagged_node, by_mcid) if tagged_node.kind == "list"
            else _tagged_table_node(tagged_node) if tagged_node.kind == "table"
            else _tagged_toc_node(tagged_node)
        )
        if replacement is None:
            continue
        covered = _covered_top_level_indices(geometric.children, keys)
        if not covered:
            conflicts.append("TAGGED_STRUCTURE_NO_REPLACEABLE_BLOCK:%s" % tagged_node.id)
            continue
        first, last = min(covered), max(covered)
        if covered != list(range(first, last + 1)):
            conflicts.append("TAGGED_STRUCTURE_NONCONTIGUOUS:%s" % tagged_node.id)
            continue
        geometric.children[first : last + 1] = [replacement]
        bindings[tagged_node.id] = replacement


def _tagged_list_node(
    tagged: SemanticNode,
    by_mcid: Dict[Tuple[int, int], List[SemanticNode]] | None = None,
) -> SemanticNode | None:
    tagged_items = _direct_descendants(tagged, "item", stop_kinds={"list"})
    if not tagged_items:
        return None
    items: List[SemanticNode] = []
    labels: List[str] = []
    for index, tagged_item in enumerate(tagged_items):
        nested_lists = _direct_descendants(
            tagged_item,
            "list",
            stop_kinds={"list"},
        )
        non_list_nodes = list(_walk_excluding(tagged_item.children, {"list"}))
        label_nodes = [
            child for child in non_list_nodes
            if child.attrs.get("tag_role") == "Lbl"
        ]
        label = "".join(_node_text(node) for node in label_nodes).strip()
        labels.append(label)
        body_roots = [
            child for child in non_list_nodes
            if child.attrs.get("tag_role") == "LBody"
        ]
        if not body_roots:
            body_roots = [
                child for child in tagged_item.children
                if child.kind != "list" and child.attrs.get("tag_role") != "Lbl"
            ]
        allowed_keys = (
            _tagged_keys_from_nodes(body_roots, stop_kinds={"list"})
            | _tagged_keys_from_nodes(label_nodes, stop_kinds={"list"})
        )
        body = _tagged_body_text(body_roots, by_mcid, allowed_keys).strip()
        body = _strip_tagged_label_prefix(body, label)
        sources = _recursive_sources(tagged_item)
        children: List[SemanticNode] = []
        if body:
            children.append(SemanticNode(
                id="tagged-%s-text" % tagged_item.id, kind="text", text=body, confidence=0.995,
                evidence=[Evidence("tagged_list_body", 0.995, data={"tagged_node": tagged_item.id})], sources=sources,
            ))
        for nested in nested_lists:
            materialized = _tagged_list_node(nested, by_mcid)
            if materialized is not None:
                children.append(materialized)
        items.append(SemanticNode(
            id="tagged-%s-item" % tagged_item.id, kind="item", children=children,
            attrs={"label": label or None, "tagged_node_id": tagged_item.id}, confidence=0.995,
            evidence=[Evidence("tagged_list_item", 0.995)], sources=sources,
        ))
    attributes = tagged.attrs.get("structure_attributes") if isinstance(tagged.attrs.get("structure_attributes"), dict) else {}
    numbering = str(attributes.get("ListNumbering", "")).lstrip("/")
    ordered = numbering.lower() not in {"", "none", "disc", "circle", "square"}
    if not numbering:
        nonempty_labels = [label for label in labels if label]
        ordered = bool(nonempty_labels and len(nonempty_labels) == len(labels) and all(_ordered_label(label) for label in nonempty_labels))
    start = _label_start(labels[0]) if ordered and labels else 1
    sources = _recursive_sources(tagged)
    return SemanticNode(
        id="tagged-%s" % tagged.id, kind="list", children=items,
        attrs={"ordered": ordered, "start": start, "list_numbering": numbering or None, "tagged_node_id": tagged.id},
        confidence=0.995, evidence=[Evidence("tagged_list_structure", 0.995)], sources=sources,
    )


def _tagged_body_text(
    body_nodes: Sequence[SemanticNode],
    by_mcid: Dict[Tuple[int, int], List[SemanticNode]] | None,
    allowed_keys: set[Tuple[int, int]],
) -> str:
    fallback = _node_text_from_excluding(
        body_nodes,
        stop_kinds={"list"},
        stop_roles={"Lbl"},
    )
    # ActualText is an explicit PDF replacement and remains authoritative.
    if any(
        candidate.attrs.get("actual_text")
        for candidate in _walk_excluding(body_nodes, {"list"})
    ):
        return fallback
    target_keys = _tagged_keys_from_nodes(body_nodes, stop_kinds={"list"})
    if not by_mcid or not target_keys:
        return fallback

    candidates: Dict[str, Tuple[SemanticNode, set[Tuple[int, int]]]] = {}
    inspected: set[str] = set()
    for key in target_keys:
        for candidate in by_mcid.get(key, []):
            if candidate.id in inspected:
                continue
            inspected.add(candidate.id)
            if candidate.kind not in {
                "paragraph",
                "heading",
                "item",
                "table_cell",
                "text",
            }:
                continue
            candidate_keys = _semantic_mcid_keys(candidate)
            if (
                not candidate_keys
                or not candidate_keys <= allowed_keys
                or not (candidate_keys & target_keys)
                or not _node_text(candidate).strip()
            ):
                continue
            candidates[candidate.id] = (candidate, candidate_keys)
    if not candidates:
        return fallback

    selected: List[SemanticNode] = []
    covered: set[Tuple[int, int]] = set()
    kind_rank = {
        "paragraph": 5,
        "heading": 4,
        "item": 3,
        "table_cell": 2,
        "text": 1,
    }
    while covered != target_keys:
        remaining = target_keys - covered
        eligible = [
            (candidate, keys)
            for candidate, keys in candidates.values()
            if (keys & target_keys) <= remaining
        ]
        if not eligible:
            return fallback
        candidate, keys = max(
            eligible,
            key=lambda item: (
                len(item[1] & remaining),
                -len(item[1] - target_keys),
                kind_rank.get(item[0].kind, 0),
                len(_node_text(item[0]).strip()),
                item[0].id,
            ),
        )
        selected.append(candidate)
        covered.update(keys & target_keys)

    def position(node: SemanticNode) -> Tuple[int, float, float, str]:
        sources = [source for source in node.sources if source.page > 0]
        page = min((source.page for source in sources), default=10**9)
        boxes = [source.bbox for source in sources if source.bbox is not None]
        y = min((box[1] for box in boxes), default=10**9)
        x = min((box[0] for box in boxes), default=10**9)
        return page, y, x, node.id

    repaired = " ".join(
        text
        for text in (
            _node_text(node).strip()
            for node in sorted(selected, key=position)
        )
        if text
    )
    return repaired or fallback


def _strip_tagged_label_prefix(body: str, label: str) -> str:
    if not label or not body.startswith(label):
        return body
    remainder = body[len(label):]
    if not remainder:
        return ""
    # A bare alphanumeric label must be token-delimited. Otherwise a label
    # such as "A" would corrupt a legitimate body beginning with "Apple".
    if (
        label[-1].isalnum()
        and remainder[0].isalnum()
    ):
        return body
    return remainder.lstrip()


def _walk_excluding(
    nodes: Sequence[SemanticNode],
    stop_kinds: set[str],
) -> Iterable[SemanticNode]:
    for node in nodes:
        if node.kind in stop_kinds:
            continue
        yield node
        yield from _walk_excluding(node.children, stop_kinds)


def _tagged_keys_from_nodes(
    nodes: Sequence[SemanticNode],
    stop_kinds: set[str],
) -> set[Tuple[int, int]]:
    keys: set[Tuple[int, int]] = set()
    for candidate in _walk_excluding(nodes, stop_kinds):
        mcid = candidate.attrs.get("mcid")
        if not isinstance(mcid, int) or isinstance(mcid, bool):
            continue
        for page in candidate.source_pages():
            keys.add((page, mcid))
    return keys


def _node_text_from_excluding(
    nodes: Sequence[SemanticNode],
    stop_kinds: set[str],
    stop_roles: set[str],
) -> str:
    def node_text(node: SemanticNode) -> str:
        if node.kind in stop_kinds or node.attrs.get("tag_role") in stop_roles:
            return ""
        if node.text:
            return node.text
        return "".join(node_text(child) for child in node.children)

    return " ".join(
        text
        for text in (node_text(node).strip() for node in nodes)
        if text
    )


def _tagged_table_node(tagged: SemanticNode) -> SemanticNode | None:
    tagged_rows = _direct_descendants(tagged, "table_row", stop_kinds={"table"})
    if not tagged_rows:
        return None
    rows: List[SemanticNode] = []
    header_rows = 0
    for row_index, tagged_row in enumerate(tagged_rows):
        tagged_cells = _direct_descendants(tagged_row, "table_cell", stop_kinds={"table", "table_row"})
        if not tagged_cells:
            continue
        cells: List[SemanticNode] = []
        all_header = True
        column = 0
        for tagged_cell in tagged_cells:
            attributes = tagged_cell.attrs.get("structure_attributes") if isinstance(tagged_cell.attrs.get("structure_attributes"), dict) else {}
            role = "th" if tagged_cell.attrs.get("cell_role") == "th" or tagged_cell.attrs.get("tag_role") == "TH" else "td"
            all_header = all_header and role == "th"
            rowspan = _positive_int(attributes.get("RowSpan"), 1)
            colspan = _positive_int(attributes.get("ColSpan"), 1)
            text = _node_text(tagged_cell).strip()
            sources = _recursive_sources(tagged_cell)
            bbox = _sources_bbox(sources)
            text_node = SemanticNode(
                id="tagged-%s-text" % tagged_cell.id, kind="text", text=text, confidence=0.995,
                evidence=[Evidence("tagged_table_cell_text", 0.995)], sources=sources,
            )
            cells.append(SemanticNode(
                id="tagged-%s-cell" % tagged_cell.id, kind="table_cell", children=[text_node] if text else [],
                attrs={"row": row_index, "col": column, "rowspan": rowspan, "colspan": colspan, "role": role, "bbox": bbox, "headers": attributes.get("Headers"), "scope": attributes.get("Scope")},
                confidence=0.995, evidence=[Evidence("tagged_table_cell", 0.995)], sources=sources,
            ))
            column += colspan
        if row_index == header_rows and all_header:
            header_rows += 1
        rows.append(SemanticNode(
            id="tagged-%s-row" % tagged_row.id, kind="table_row", children=cells,
            attrs={"row": row_index, "role": "header" if all_header else "body"}, confidence=0.995,
            evidence=[Evidence("tagged_table_row", 0.995)], sources=_recursive_sources(tagged_row),
        ))
    if not rows:
        return None
    caption_tag = next((node for node in tagged.children if node.kind == "caption"), None)
    children: List[SemanticNode] = list(rows)
    if caption_tag is not None:
        children.insert(0, SemanticNode(
            id="tagged-%s-caption" % caption_tag.id, kind="caption", text=_node_text(caption_tag).strip(),
            confidence=0.995, evidence=[Evidence("tagged_table_caption", 0.995)], sources=_recursive_sources(caption_tag),
        ))
    complex_table = any(int(cell.attrs.get("rowspan", 1)) > 1 or int(cell.attrs.get("colspan", 1)) > 1 for row in rows for cell in row.children)
    return SemanticNode(
        id="tagged-%s" % tagged.id, kind="table", children=children,
        attrs={"header_rows": header_rows, "row_count": len(rows), "column_count": max((sum(int(cell.attrs.get("colspan", 1)) for cell in row.children) for row in rows), default=0), "output_mode": "html" if complex_table else "gfm", "tagged_node_id": tagged.id},
        confidence=0.995, evidence=[Evidence("tagged_table_structure", 0.995)], sources=_recursive_sources(tagged),
    )


def _tagged_toc_node(tagged: SemanticNode) -> SemanticNode | None:
    tagged_items = _direct_descendants(tagged, "toc_item", stop_kinds={"toc"})
    if not tagged_items:
        return None
    items = [SemanticNode(
        id="tagged-%s-toc-item" % item.id, kind="toc_item", text=_node_text(item).strip(),
        attrs={"level": max(1, _tag_depth(tagged, item)), "tagged_node_id": item.id}, confidence=0.995,
        evidence=[Evidence("tagged_toc_item", 0.995)], sources=_recursive_sources(item),
    ) for item in tagged_items]
    return SemanticNode(
        id="tagged-%s" % tagged.id, kind="toc", children=items, attrs={"source": "tagged_pdf", "tagged_node_id": tagged.id},
        confidence=0.995, evidence=[Evidence("tagged_toc_structure", 0.995)], sources=_recursive_sources(tagged),
    )


def _covered_top_level_indices(nodes: Sequence[SemanticNode], keys: set[Tuple[int, int]]) -> List[int]:
    covered: List[int] = []
    for index, node in enumerate(nodes):
        node_keys = _semantic_mcid_keys(node)
        if node_keys and node_keys <= keys:
            covered.append(index)
    return covered


def _semantic_mcid_keys(node: SemanticNode) -> set[Tuple[int, int]]:
    return {(source.page, mcid) for candidate in node.walk() for source in candidate.sources for mcid in source.mcids}


def _recursive_sources(node: SemanticNode) -> List[SourceRef]:
    from ..ir.semantic import merge_sources
    return merge_sources(source for candidate in node.walk() for source in candidate.sources)


def _sources_bbox(sources: Sequence[SourceRef]):
    boxes = [source.bbox for source in sources if source.bbox is not None]
    if not boxes:
        return None
    return (min(box[0] for box in boxes), min(box[1] for box in boxes), max(box[2] for box in boxes), max(box[3] for box in boxes))


def _node_text(node: SemanticNode) -> str:
    return node.text or "".join(_node_text(child) for child in node.children)


def _node_text_from(nodes: Sequence[SemanticNode]) -> str:
    return " ".join(filter(None, (_node_text(node).strip() for node in nodes)))


def _direct_descendants(node: SemanticNode, kind: str, stop_kinds: set[str]) -> List[SemanticNode]:
    out: List[SemanticNode] = []
    for child in node.children:
        if child.kind == kind:
            out.append(child)
            continue
        if child.kind in stop_kinds:
            continue
        out.extend(_direct_descendants(child, kind, stop_kinds))
    return out


def _ordered_label(label: str) -> bool:
    import re
    return bool(re.fullmatch(r"(?:\(?\d+[.)\]]?|[A-Za-z][.)]|[ivxlcdmIVXLCDM]+[.)])", label.strip()))


def _label_start(label: str) -> int:
    import re
    match = re.search(r"\d+", label or "")
    return max(1, int(match.group(0))) if match else 1


def _positive_int(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _tag_depth(root: SemanticNode, target: SemanticNode, depth: int = 1) -> int:
    if root is target:
        return depth
    for child in root.children:
        result = _tag_depth(child, target, depth + (1 if child.kind == "toc_item" else 0))
        if result:
            return result
    return 0


def _apply_tagged_child_order(tagged: SemanticDocument, bindings: Dict[str, SemanticNode]) -> None:
    for tagged_parent in tagged.walk():
        parent = bindings.get(tagged_parent.id)
        if parent is None or not parent.children:
            continue
        desired: List[str] = []
        for tagged_child in tagged_parent.children:
            bound = bindings.get(tagged_child.id)
            if bound is not None and bound is not parent and bound.id not in desired:
                desired.append(bound.id)
        if not desired:
            continue
        positions = {node_id: index for index, node_id in enumerate(desired)}
        original = {child.id: index for index, child in enumerate(parent.children)}
        parent.children.sort(key=lambda child: (_child_order_position(child, positions), original.get(child.id, 10**9)))


def _child_order_position(node: SemanticNode, positions: Dict[str, int]) -> int:
    matches = [positions[candidate.id] for candidate in node.walk() if candidate.id in positions]
    return min(matches) if matches else 10**9
