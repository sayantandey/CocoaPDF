from __future__ import annotations

import math
import re
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

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


def refine_tagged_paragraph_events(
    renderer: Any,
    events_by_page: Dict[int, List[Any]],
    tagged: SemanticDocument,
) -> None:
    """Recover paragraph boundaries corroborated by tags and page geometry.

    A few producers expose valid sibling ``/P`` ownership while emitting one
    oversized geometric paragraph, or split one tagged paragraph at a local
    style transition.  Tags alone are not enough: this pass accepts only
    ParentTree-validated, disjoint sibling owners whose physical line order and
    spacing independently agree.  It operates on source lines before graph
    construction so Markdown, HTML, JSON, and provenance share one decision.
    """
    owner_meta, key_to_owner = _validated_tagged_paragraph_owners(tagged)
    if not owner_meta or not key_to_owner:
        return

    # Stage every page before publishing any mutation.  A renderer failure or
    # an invariant violation on a later page must not leave an earlier page
    # partially tag-refined while the caller falls back to geometric output.
    staged_pages: Dict[int, List[Any]] = {}
    changed_pages: set[int] = set()
    for page, page_events in list(events_by_page.items()):
        expanded: List[Any] = []
        for event in page_events:
            if not _tagged_paragraph_event_candidate(event):
                expanded.append(event)
                continue
            line_owners = [
                _validated_line_owner(line, key_to_owner)
                for line in event.lines
            ]
            owner_ids = [item[0] if item is not None else None for item in line_owners]
            distinct = []
            for owner_id in owner_ids:
                if owner_id is not None and owner_id not in distinct:
                    distinct.append(owner_id)
            if len(distinct) < 2:
                expanded.append(event)
                continue
            split = _split_tagged_paragraph_event(
                renderer,
                event,
                owner_ids,
                owner_meta,
                key_to_owner,
            )
            if split is None:
                expanded.append(event)
                continue
            expanded.extend(split)
            changed_pages.add(page)

        merged: List[Any] = []
        for event in expanded:
            if not merged:
                merged.append(event)
                continue
            previous = merged[-1]
            combined = _merge_same_owner_paragraph_events(
                renderer,
                previous,
                event,
                owner_meta,
                key_to_owner,
            )
            if combined is None:
                merged.append(event)
                continue
            merged[-1] = combined
            changed_pages.add(page)
        staged = sorted(merged, key=lambda event: event.rank)
        _validate_tagged_paragraph_transaction(page, page_events, staged)
        staged_pages[page] = staged

    for page, staged in staged_pages.items():
        events_by_page[page] = staged

    for page in sorted(changed_pages):
        renderer.conv.doc.warn(
            "TAGGED_BLOCK_BOUNDARY_RECOVERED",
            "validated paragraph ownership and page geometry refined block boundaries",
            page,
        )


def _validate_tagged_paragraph_transaction(
    page: int,
    original: Sequence[Any],
    refined: Sequence[Any],
) -> None:
    """Prove that refinement changed boundaries without changing ownership."""
    original_lines = sorted(
        id(line)
        for event in original
        for line in getattr(event, "lines", ())
    )
    refined_lines = sorted(
        id(line)
        for event in refined
        for line in getattr(event, "lines", ())
    )
    if original_lines != refined_lines:
        raise ValueError(
            "tagged paragraph refinement changed the page-%d source-line multiset"
            % page
        )
    if any(int(getattr(event, "page", page)) != page for event in refined):
        raise ValueError(
            "tagged paragraph refinement moved an event across page %d" % page
        )


def _validated_tagged_paragraph_owners(
    tagged: SemanticDocument,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[Tuple[int, int], str]]:
    owners: Dict[str, Dict[str, Any]] = {}
    key_claims: Dict[Tuple[int, int], List[str]] = {}

    def visit(
        node: SemanticNode,
        parent_id: str,
        sibling_index: int,
        active_owner: Optional[str],
    ) -> None:
        role = str(node.attrs.get("tag_role", ""))
        owner_id = active_owner
        if node.kind == "paragraph" and role == "P":
            owner_id = node.id
            owners[node.id] = {
                "id": node.id,
                "parent_id": parent_id,
                "sibling_index": sibling_index,
                "keys": set(),
            }
        mcid = node.attrs.get("mcid")
        if (
            owner_id is not None
            and isinstance(mcid, int)
            and not isinstance(mcid, bool)
            and node.attrs.get("parent_tree_validated") is True
        ):
            pages = node.source_pages()
            if len(pages) == 1 and pages[0] > 0:
                key = (pages[0], mcid)
                owners[owner_id]["keys"].add(key)
                key_claims.setdefault(key, []).append(owner_id)
        for index, child in enumerate(node.children):
            visit(child, node.id, index, owner_id)

    for root_index, root in enumerate(tagged.children):
        visit(root, "document", root_index, None)

    owners = {
        owner_id: meta
        for owner_id, meta in owners.items()
        if meta["keys"]
    }
    key_to_owner = {
        key: claimants[0]
        for key, claimants in key_claims.items()
        if len(set(claimants)) == 1 and claimants[0] in owners
    }
    for owner_id, meta in list(owners.items()):
        unique_keys = {
            key for key in meta["keys"] if key_to_owner.get(key) == owner_id
        }
        if not unique_keys:
            owners.pop(owner_id)
            continue
        meta["keys"] = unique_keys
    return owners, key_to_owner


def _validated_line_owner(
    line: Any,
    key_to_owner: Dict[Tuple[int, int], str],
) -> Optional[Tuple[str, float]]:
    visible = [
        character
        for character in getattr(line, "chars", ())
        if str(getattr(character, "text", "")).strip()
        and not getattr(character, "artifact", False)
        and not getattr(character, "invisible", False)
    ]
    if not visible:
        return None
    counts: Dict[str, int] = {}
    ambiguous = 0
    for character in visible:
        owners = {
            key_to_owner[(int(getattr(character, "page", 0)), mcid)]
            for mark in getattr(character, "mc", ()) or ()
            if isinstance(mark, dict)
            for mcid in [mark.get("mcid")]
            if isinstance(mcid, int)
            and not isinstance(mcid, bool)
            and (int(getattr(character, "page", 0)), mcid) in key_to_owner
        }
        if len(owners) == 1:
            owner_id = next(iter(owners))
            counts[owner_id] = counts.get(owner_id, 0) + 1
        elif len(owners) > 1:
            ambiguous += 1
    if not counts or ambiguous:
        return None
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    owner_id, count = ranked[0]
    coverage = count / len(visible)
    if coverage < 0.90 or (len(ranked) > 1 and ranked[1][1] / len(visible) > 0.05):
        return None
    return owner_id, coverage


def _validated_owner_keys_for_lines(
    lines: Sequence[Any],
    key_to_owner: Dict[Tuple[int, int], str],
    owner_id: str,
) -> Tuple[Tuple[int, int], ...]:
    """Return only validated owner keys physically present in ``lines``."""
    keys: set[Tuple[int, int]] = set()
    for line in lines:
        for character in getattr(line, "chars", ()):
            if (
                not str(getattr(character, "text", ""))
                or getattr(character, "artifact", False)
                or getattr(character, "invisible", False)
            ):
                continue
            page = int(getattr(character, "page", 0) or 0)
            for mark in getattr(character, "mc", ()) or ():
                if not isinstance(mark, dict):
                    continue
                mcid = mark.get("mcid")
                if not isinstance(mcid, int) or isinstance(mcid, bool):
                    continue
                key = (page, mcid)
                if key_to_owner.get(key) == owner_id:
                    keys.add(key)
    return tuple(sorted(keys))


def _tagged_paragraph_event_candidate(event: Any) -> bool:
    return bool(
        event.kind == "paragraph"
        and len(event.lines) >= 1
        and not event.attrs.get("panel_local")
        and all(getattr(line, "writing_mode", "horizontal") == "horizontal" for line in event.lines)
    )


def _split_tagged_paragraph_event(
    renderer: Any,
    event: Any,
    owner_ids: Sequence[Optional[str]],
    owner_meta: Dict[str, Dict[str, Any]],
    key_to_owner: Dict[Tuple[int, int], str],
) -> Optional[List[Any]]:
    if any(owner_id is None for owner_id in owner_ids):
        return None
    concrete_ids = [str(owner_id) for owner_id in owner_ids]
    groups: List[Tuple[str, List[Any], int]] = []
    for line_index, (line, owner_id) in enumerate(zip(event.lines, concrete_ids)):
        if groups and groups[-1][0] == owner_id:
            groups[-1][1].append(line)
        else:
            groups.append((owner_id, [line], line_index))
    ordered_owners = [owner_id for owner_id, _lines, _index in groups]
    if len(set(ordered_owners)) != len(ordered_owners):
        return None
    parents = {owner_meta[owner_id]["parent_id"] for owner_id in ordered_owners}
    if len(parents) != 1:
        return None
    sibling_indexes = [owner_meta[owner_id]["sibling_index"] for owner_id in ordered_owners]
    if sibling_indexes != sorted(sibling_indexes) or len(set(sibling_indexes)) != len(sibling_indexes):
        return None
    if any(
        current.y0 <= previous.y0
        for previous, current in zip(event.lines, event.lines[1:])
    ):
        return None

    within_owner_gaps = [
        current.y0 - previous.y0
        for previous, current, previous_owner, current_owner in zip(
            event.lines,
            event.lines[1:],
            concrete_ids,
            concrete_ids[1:],
        )
        if previous_owner == current_owner and current.y0 > previous.y0
    ]
    if len(within_owner_gaps) < 2:
        return None
    normal_pitch = median(within_owner_gaps)
    boundary_gaps = [
        event.lines[start].y0 - event.lines[start - 1].y0
        for _owner_id, _lines, start in groups[1:]
    ]
    minimum_boundary_gap = normal_pitch + max(1.0, normal_pitch * 0.12)
    if any(gap < minimum_boundary_gap for gap in boundary_gaps):
        return None
    first_line_lefts = [lines[0].x0 for _owner_id, lines, _start in groups]
    typical_size = median([
        line.size for line in event.lines if getattr(line, "size", 0.0) > 0
    ])
    if max(first_line_lefts) - min(first_line_lefts) > max(12.0, typical_size * 1.5):
        return None

    local_keys = [
        _validated_owner_keys_for_lines(lines, key_to_owner, owner_id)
        for owner_id, lines, _start in groups
    ]
    if any(not keys for keys in local_keys):
        return None

    recovered: List[Any] = []
    for group_index, (owner_id, lines, start) in enumerate(groups):
        meta = owner_meta[owner_id]
        attrs = dict(event.attrs)
        attrs.update({
            "tagged_block_boundary_recovered": True,
            "tagged_block_boundary_action": "split",
            "tagged_block_boundary_actions": ("split",),
            "tagged_block_owner_id": owner_id,
            "tagged_block_parent_id": meta["parent_id"],
            "tagged_block_mcids": local_keys[group_index],
            "tagged_block_original_line_count": len(event.lines),
            "tagged_block_group_count": len(groups),
            "tagged_block_group_index": group_index,
            "tagged_block_geometry": {
                "normal_line_pitch": normal_pitch,
                "boundary_gaps": tuple(boundary_gaps),
                "first_line_lefts": tuple(first_line_lefts),
            },
        })
        recovered.append(_rebuilt_paragraph_event(
            renderer,
            event,
            lines,
            attrs,
            float(event.rank) + start * 0.0001,
        ))
    return recovered


def _merge_same_owner_paragraph_events(
    renderer: Any,
    previous: Any,
    current: Any,
    owner_meta: Dict[str, Dict[str, Any]],
    key_to_owner: Dict[Tuple[int, int], str],
) -> Optional[Any]:
    if not (
        _tagged_paragraph_event_candidate(previous)
        and _tagged_paragraph_event_candidate(current)
        and previous.page == current.page
    ):
        return None
    previous_owners = [
        _validated_line_owner(line, key_to_owner) for line in previous.lines
    ]
    current_owners = [
        _validated_line_owner(line, key_to_owner) for line in current.lines
    ]
    if any(item is None for item in previous_owners + current_owners):
        return None
    owner_ids = {
        item[0]
        for item in previous_owners + current_owners
        if item is not None
    }
    if len(owner_ids) != 1:
        return None
    owner_id = next(iter(owner_ids))
    if owner_id not in owner_meta:
        return None
    last = previous.lines[-1]
    first = current.lines[0]
    if not _tagged_colon_formula_continuation(last, first):
        return None
    gap = first.y0 - last.y0
    if gap <= 0 or gap > max(last.size * 1.9, 18.0):
        return None
    horizontal_overlap = max(0.0, min(last.x1, first.x1) - max(last.x0, first.x0))
    narrower = max(1.0, min(last.x1 - last.x0, first.x1 - first.x0))
    if (
        horizontal_overlap < narrower * 0.25
        and abs(last.x0 - first.x0) > max(20.0, first.size * 2.0)
    ):
        return None

    meta = owner_meta[owner_id]
    combined_lines = list(previous.lines) + list(current.lines)
    local_keys = _validated_owner_keys_for_lines(
        combined_lines,
        key_to_owner,
        owner_id,
    )
    if not local_keys:
        return None
    prior_actions: List[str] = []
    prior_geometries: List[Dict[str, Any]] = []
    lineage: List[Dict[str, Any]] = []
    for side, source_event in (("left", previous), ("right", current)):
        actions = source_event.attrs.get("tagged_block_boundary_actions")
        if not isinstance(actions, (list, tuple)):
            action = source_event.attrs.get("tagged_block_boundary_action")
            actions = (action,) if isinstance(action, str) and action else ()
        for action in actions:
            value = str(action)
            if value and value not in prior_actions:
                prior_actions.append(value)
        geometry = source_event.attrs.get("tagged_block_geometry")
        if isinstance(geometry, dict):
            prior_geometries.append({
                "side": side,
                "action": source_event.attrs.get("tagged_block_boundary_action"),
                "geometry": dict(geometry),
            })
        lineage.append({
            "side": side,
            "line_count": len(source_event.lines),
            "prior_original_line_count": source_event.attrs.get(
                "tagged_block_original_line_count",
                len(source_event.lines),
            ),
            "prior_action": source_event.attrs.get(
                "tagged_block_boundary_action"
            ),
            "prior_group_count": source_event.attrs.get(
                "tagged_block_group_count"
            ),
            "prior_group_index": source_event.attrs.get(
                "tagged_block_group_index"
            ),
        })
    actions = [*prior_actions]
    if "merge" not in actions:
        actions.append("merge")
    combined_action = (
        "split_and_merge"
        if any("split" in action for action in actions)
        else "merge"
    )
    attrs = dict(previous.attrs)
    attrs.update({
        "tagged_block_boundary_recovered": True,
        "tagged_block_boundary_action": combined_action,
        "tagged_block_boundary_actions": tuple(actions),
        "tagged_block_owner_id": owner_id,
        "tagged_block_parent_id": meta["parent_id"],
        "tagged_block_mcids": local_keys,
        "tagged_block_original_line_count": len(combined_lines),
        "tagged_block_lineage": tuple(lineage),
        "tagged_block_geometry": {
            "merge_gap": gap,
            "horizontal_overlap_ratio": horizontal_overlap / narrower,
            "prior": tuple(prior_geometries),
        },
    })
    return _rebuilt_paragraph_event(
        renderer,
        previous,
        combined_lines,
        attrs,
        float(previous.rank),
    )


def _tagged_colon_formula_continuation(previous: Any, current: Any) -> bool:
    """Admit only the observed colon-to-formula/style false split."""
    from ..core import line_text_tokens, plain_text

    previous_text = plain_text(line_text_tokens(previous)).strip()
    current_text = plain_text(line_text_tokens(current)).strip()
    if not previous_text or not current_text:
        return False
    if not previous_text.endswith((":", "：")):
        return False
    if previous_text.casefold().endswith(("http:", "https:")):
        return current_text.startswith("//")
    if re.search(r"(?:=|≠|≤|≥|≈|≔|→|←|↔|∑|√)", current_text):
        return True
    compact_expression = bool(
        len(current_text) <= 120
        and len(current_text.split()) <= 12
        and re.search(r"\s[+*/^−-]\s", current_text)
        and not re.search(r"[.!?]\s*$", current_text)
    )
    return compact_expression or (
        float(getattr(current, "mono_ratio", 0.0)) >= 0.75
        and re.search(r"[+*/^=<>−-]", current_text) is not None
    )


def _rebuilt_paragraph_event(
    renderer: Any,
    original: Any,
    lines: Sequence[Any],
    attrs: Dict[str, Any],
    rank: float,
) -> Any:
    from .records import semantic_block_record

    legacy_markdown = renderer._render_paragraph(list(lines))
    return original.__class__(
        page=original.page,
        rank=rank,
        kind="paragraph",
        lines=list(lines),
        attrs=attrs,
        legacy_markdown=legacy_markdown,
        semantic=semantic_block_record("paragraph", legacy_markdown),
    )


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
    geometric_keys = _semantic_mcid_keys(geometric)
    if tagged_keys:
        overlap = tagged_keys & geometric_keys
        coverage = len(overlap) / len(tagged_keys)
        purity = len(overlap) / max(len(geometric_keys), 1)
    else:
        coverage = matched
        purity = 1.0
    role_bonus = 0.14 if exact else 0.08 if compatible else -0.18
    score = min(1.0, 0.43 + min(0.43, coverage * 0.43) + role_bonus)
    atomic_roles = {
        "paragraph",
        "heading",
        "item",
        "table_row",
        "table_cell",
        "caption",
        "link",
        "footnote",
        "footnote_ref",
        "text",
    }
    if tagged.kind in atomic_roles and tagged_keys and geometric_keys and purity <= 0.50:
        # Recall alone lets a tiny tagged paragraph or inline link claim an
        # oversized geometry node containing at least as much unrelated MCID
        # ownership. Atomic roles require a strict majority; containers
        # continue to use recall.
        return min(score, 0.49)
    return score


def _exact_tagged_content_ownership(
    tagged: SemanticNode,
    geometric: SemanticNode,
) -> Tuple[bool, Dict[str, Any]]:
    """Prove that a destructive text replacement owns the whole candidate."""
    tagged_keys = _tagged_keys(tagged)
    geometric_keys = _semantic_mcid_keys(geometric)
    if tagged_keys or geometric_keys:
        exact = bool(tagged_keys) and tagged_keys == geometric_keys
        return exact, {
            "basis": "mcid",
            "tagged_keys": tuple(sorted(tagged_keys)),
            "geometric_keys": tuple(sorted(geometric_keys)),
        }

    tagged_objects = {
        object_ref
        for candidate in tagged.walk()
        for source in candidate.sources
        for object_ref in source.object_refs
    }
    geometric_objects = {
        object_ref
        for candidate in geometric.walk()
        for source in candidate.sources
        for object_ref in source.object_refs
    }
    exact = bool(tagged_objects) and tagged_objects == geometric_objects
    # Direct-object identities are process-local parser handles. They may be
    # compared within this conversion, but must not leak into serialized
    # evidence and make otherwise deterministic output depend on ``id()``.
    stable_tagged_objects = tuple(sorted(
        value for value in tagged_objects if not value.startswith("direct:")
    ))
    stable_geometric_objects = tuple(sorted(
        value for value in geometric_objects if not value.startswith("direct:")
    ))
    return exact, {
        "basis": "object_ref",
        "tagged_object_refs": stable_tagged_objects,
        "geometric_object_refs": stable_geometric_objects,
        "tagged_direct_object_count": len(tagged_objects) - len(stable_tagged_objects),
        "geometric_direct_object_count": (
            len(geometric_objects) - len(stable_geometric_objects)
        ),
    }


def _apply_tagged_prior(geometric: SemanticNode, tagged: SemanticNode, score: float) -> None:
    old_kind = geometric.kind
    level = geometric.attrs.get("level")
    geometric_bbox = _sources_bbox(_recursive_sources(geometric))
    preserve_cover_heading = (
        old_kind == "heading"
        and tagged.kind == "paragraph"
        and isinstance(level, int)
        and not isinstance(level, bool)
        and level == 1
        and sorted({source.page for source in _recursive_sources(geometric)}) == [1]
        and geometric_bbox is not None
        and geometric.confidence >= 0.92
        and score >= 0.92
        and geometric.attrs.get("upper_page_zone") is True
        and geometric.attrs.get("bottom_zone") is False
        and any(
            evidence.kind == "geometric_semantic_detector"
            and evidence.detail == "heading"
            for evidence in geometric.evidence
        )
    )
    if preserve_cover_heading:
        if "TAGGED_ROLE_CONFLICT_GEOMETRY_PRESERVED" not in geometric.warnings:
            geometric.warnings.append("TAGGED_ROLE_CONFLICT_GEOMETRY_PRESERVED")
        geometric.evidence.append(Evidence(
            "tagged_role_geometry_override",
            min(geometric.confidence, score),
            detail="paragraph tag rejected for first-page level-one heading",
            data={
                "tagged_node": tagged.id,
                "tagged_kind": tagged.kind,
                "binding_score": score,
                "bbox": geometric_bbox,
                "upper_page_zone": True,
            },
        ))
    elif tagged.kind != "unknown" and _role_change_allowed(old_kind, tagged.kind):
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
        _apply_layout_attributes(geometric, structure)
        numbering = structure.get("ListNumbering")
        if numbering and geometric.kind == "list":
            normalized = str(numbering).lstrip("/")
            geometric.attrs["list_numbering"] = normalized
            geometric.attrs["marker_style"] = _list_marker_style(normalized)
            geometric.attrs["ordered"] = normalized.lower() not in {
                "none",
                "disc",
                "circle",
                "square",
            }
    if tagged.text and tagged.attrs.get("actual_text"):
        exact_ownership, ownership = _exact_tagged_content_ownership(
            tagged,
            geometric,
        )
        if exact_ownership:
            replacement = SemanticNode(
                id=geometric.id + "-actual-text",
                kind="text",
                text=tagged.text,
                attrs={"actual_text": True},
                confidence=1.0,
                evidence=list(tagged.evidence),
                sources=_recursive_sources(geometric),
            )
            geometric.text = ""
            geometric.children = [replacement]
            geometric.attrs["actual_text"] = True
        else:
            if "TAGGED_ACTUALTEXT_OWNERSHIP_CONFLICT" not in geometric.warnings:
                geometric.warnings.append("TAGGED_ACTUALTEXT_OWNERSHIP_CONFLICT")
            geometric.evidence.append(Evidence(
                "tagged_actual_text_rejected",
                0.99,
                detail="ActualText rejected because tagged ownership did not cover exactly one geometric node",
                data={
                    "tagged_node": tagged.id,
                    "binding_score": score,
                    **ownership,
                },
            ))
    if tagged.attrs.get("alt") and geometric.kind == "figure":
        for child in geometric.children:
            if child.kind == "image":
                child.attrs["alt"] = tagged.attrs["alt"]
    geometric.evidence.append(Evidence("tag_geometry_reconciled", score, detail="%s->%s" % (old_kind, geometric.kind), data={"tagged_node": tagged.id}))
    if not preserve_cover_heading:
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


def _apply_layout_attributes(
    node: SemanticNode,
    attributes: Dict[str, Any],
) -> None:
    alignment = str(attributes.get("TextAlign") or "").lstrip("/").lower()
    alignment_map = {
        "start": "start",
        "center": "center",
        "end": "end",
        "justify": "justify",
        "left": "left",
        "right": "right",
    }
    if alignment in alignment_map:
        node.attrs["alignment"] = alignment_map[alignment]

    writing_mode = str(
        attributes.get("WritingMode") or ""
    ).lstrip("/").lower()
    if writing_mode == "rltb":
        node.attrs["direction"] = "rtl"
        node.attrs["writing_mode"] = "horizontal"
    elif writing_mode == "lrtb":
        node.attrs["direction"] = "ltr"
        node.attrs["writing_mode"] = "horizontal"
    elif writing_mode == "tbrl":
        node.attrs["writing_mode"] = "vertical-rl"
    elif writing_mode == "tblr":
        node.attrs["writing_mode"] = "vertical-lr"

    for source, target in (
        ("TextIndent", "text_indent_pt"),
        ("StartIndent", "start_indent_pt"),
        ("EndIndent", "end_indent_pt"),
        ("SpaceBefore", "space_before_pt"),
        ("SpaceAfter", "space_after_pt"),
    ):
        value = _finite_number(attributes.get(source))
        if value is not None:
            node.attrs[target] = value


def _finite_number(raw: Any) -> float | None:
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _list_marker_style(numbering: Any) -> str:
    value = str(numbering or "").lstrip("/").lower()
    return {
        "decimal": "decimal",
        "upperroman": "upper-roman",
        "lowerroman": "lower-roman",
        "upperalpha": "upper-alpha",
        "loweralpha": "lower-alpha",
        "disc": "disc",
        "circle": "circle",
        "square": "square",
        "none": "none",
    }.get(value, "")


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
    # A structure tree is a prior over the nodes it actually binds.  It has no
    # authority to move unbound geometric content (including page-break
    # sentinels) to the end of the document, nor to move a bound node across a
    # physical page.  Reorder only contiguous, fully-bound cohorts from one
    # page; unbound and cross-page nodes remain hard anchors in geometric order.
    start = 0
    while start < len(geometric.children):
        node = geometric.children[start]
        pages = node.source_pages()
        if node.id not in order or len(pages) != 1:
            start += 1
            continue
        page = pages[0]
        end = start + 1
        while end < len(geometric.children):
            candidate = geometric.children[end]
            if candidate.id not in order or candidate.source_pages() != [page]:
                break
            end += 1
        cohort = geometric.children[start:end]
        reordered = sorted(cohort, key=lambda candidate: order[candidate.id])
        if [candidate.id for candidate in cohort] != [candidate.id for candidate in reordered]:
            status, boxes = _tagged_reorder_geometry_status(cohort, reordered, page)
            if status != "independent_tracks":
                if "TAGGED_ORDER_GEOMETRY_CONFLICT" not in geometric.warnings:
                    geometric.warnings.append("TAGGED_ORDER_GEOMETRY_CONFLICT")
                for candidate in cohort:
                    if "TAGGED_ORDER_GEOMETRY_CONFLICT" not in candidate.warnings:
                        candidate.warnings.append("TAGGED_ORDER_GEOMETRY_CONFLICT")
                    candidate.evidence.append(Evidence(
                        "tagged_root_order_rejected",
                        0.98 if status == "physical_flow" else 0.55,
                        detail=(
                            "verified physical flow retained over tag order"
                            if status == "physical_flow"
                            else "tag order rejected because sibling geometry is incomplete or ambiguous"
                        ),
                        page=page,
                        data={
                            "status": status,
                            "original_ids": [item.id for item in cohort],
                            "proposed_ids": [item.id for item in reordered],
                            "bboxes": boxes,
                        },
                    ))
            else:
                geometric.children[start:end] = reordered
                for candidate in reordered:
                    candidate.evidence.append(Evidence(
                        "tagged_root_order_applied",
                        0.94,
                        detail="tag order applied across verified independent visual tracks",
                        page=page,
                        data={
                            "original_ids": [item.id for item in cohort],
                            "proposed_ids": [item.id for item in reordered],
                            "bboxes": boxes,
                        },
                    ))
        start = end


def _tagged_reorder_geometry_status(
    nodes: Sequence[SemanticNode],
    reordered: Sequence[SemanticNode],
    page: int,
) -> Tuple[
    str,
    Dict[str, Optional[Tuple[float, float, float, float]]],
]:
    """Classify only the sibling inversions a proposed tag order would make."""
    boxes: Dict[str, Optional[Tuple[float, float, float, float]]] = {}
    for node in nodes:
        page_boxes = [
            source.bbox
            for source in _recursive_sources(node)
            if source.page == page and source.bbox is not None
        ]
        if not page_boxes:
            boxes[node.id] = None
            continue
        boxes[node.id] = (
            min(box[0] for box in page_boxes),
            min(box[1] for box in page_boxes),
            max(box[2] for box in page_boxes),
            max(box[3] for box in page_boxes),
        )
    proposed_positions = {node.id: index for index, node in enumerate(reordered)}
    inversions = [
        (left, right)
        for index, left in enumerate(nodes)
        for right in nodes[index + 1 :]
        if proposed_positions[left.id] > proposed_positions[right.id]
    ]
    if not inversions:
        return "unchanged", boxes
    saw_independent = False
    saw_physical_flow = False
    for upper_node, lower_node in inversions:
        upper = boxes.get(upper_node.id)
        lower = boxes.get(lower_node.id)
        if upper is None or lower is None:
            return "unknown", boxes
        upper_width = upper[2] - upper[0]
        lower_width = lower[2] - lower[0]
        upper_height = upper[3] - upper[1]
        lower_height = lower[3] - lower[1]
        if min(upper_width, lower_width, upper_height, lower_height) <= 0.0:
            return "unknown", boxes
        horizontal_overlap = max(
            0.0,
            min(upper[2], lower[2]) - max(upper[0], lower[0]),
        )
        vertical_overlap = max(
            0.0,
            min(upper[3], lower[3]) - max(upper[1], lower[1]),
        )
        horizontal_gap = max(lower[0] - upper[2], upper[0] - lower[2], 0.0)
        if (
            horizontal_gap >= max(4.0, min(upper_height, lower_height) * 0.35)
            and vertical_overlap >= min(upper_height, lower_height) * 0.25
        ):
            saw_independent = True
            continue
        vertical_tolerance = min(2.0, max(upper_height, lower_height) * 0.10)
        if (
            lower[1] >= upper[3] - vertical_tolerance
            and horizontal_overlap >= min(upper_width, lower_width) * 0.25
        ):
            saw_physical_flow = True
            continue
        return "unknown", boxes
    if saw_physical_flow:
        return "physical_flow", boxes
    return ("independent_tracks" if saw_independent else "unknown"), boxes


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
        claims_valid, claim_error, claim_evidence = (
            _tagged_structure_claim_validation(tagged_node)
        )
        if keys and not claims_valid:
            conflicts.append("%s:%s" % (claim_error, tagged_node.id))
            continue
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
        replacement.evidence.append(Evidence(
            "tagged_structure_parent_tree_validated",
            0.995,
            detail="all materialized MCID claims match one ParentTree-authorized structure subtree",
            data={
                "tagged_node": tagged_node.id,
                **claim_evidence,
            },
        ))
        geometric.children[first : last + 1] = [replacement]
        bindings[tagged_node.id] = replacement
        _bind_materialized_tagged_descendants(tagged_node, replacement, bindings)


def _tagged_structure_claim_validation(
    tagged: SemanticNode,
) -> Tuple[bool, str, Dict[str, Any]]:
    """Validate unique, stream-safe ParentTree claims for materialization.

	The geometric graph currently exposes page-local MCIDs but not a content-
	stream namespace.  Form-XObject MCRs therefore cannot be destructively
	matched without risking a collision with the page stream.  Keep those tags
	as non-destructive priors until that namespace is carried end-to-end.
    """
    claims: List[Tuple[int, int, Optional[int], str, str]] = []
    for candidate in tagged.walk():
        mcid = candidate.attrs.get("mcid")
        if not isinstance(mcid, int) or isinstance(mcid, bool):
            continue
        if candidate.attrs.get("parent_tree_validated") is not True:
            return False, "TAGGED_STRUCTURE_UNVALIDATED_PARENTTREE", {}
        pages = candidate.source_pages()
        if len(pages) != 1 or pages[0] <= 0:
            return False, "TAGGED_STRUCTURE_INCOMPLETE_BINDING", {}
        struct_parents = candidate.attrs.get("struct_parents")
        if isinstance(struct_parents, bool) or not isinstance(struct_parents, int):
            struct_parents = None
        stream_ref = str(candidate.attrs.get("stream_ref") or "")
        if stream_ref:
            return False, "TAGGED_STRUCTURE_STREAM_SCOPE_UNRESOLVED", {
                "claim_count": len(claims) + 1,
            }
        claims.append((pages[0], mcid, struct_parents, stream_ref, candidate.id))
    if not claims:
        return False, "TAGGED_STRUCTURE_INCOMPLETE_BINDING", {}
    identities = [claim[:4] for claim in claims]
    if len(set(identities)) != len(identities):
        return False, "TAGGED_STRUCTURE_DUPLICATE_MCID_CLAIM", {
            "claim_count": len(claims),
            "unique_claim_count": len(set(identities)),
        }
    authorized = tuple(
        {
            "page": page,
            "mcid": mcid,
            "struct_parents": struct_parents,
        }
        for page, mcid, struct_parents, _stream_ref, _node_id in sorted(
            claims,
            key=lambda claim: (
                claim[0],
                claim[1],
                -1 if claim[2] is None else claim[2],
                claim[4],
            ),
        )
    )
    return True, "", {
        "claim_count": len(claims),
        "authorized_mcid_claims": authorized,
    }


def _bind_materialized_tagged_descendants(
    tagged: SemanticNode,
    replacement: SemanticNode,
    bindings: Dict[str, SemanticNode],
) -> None:
    materialized = {
        str(candidate.attrs.get("tagged_node_id")): candidate
        for candidate in replacement.walk()
        if candidate.attrs.get("tagged_node_id")
    }
    for descendant in tagged.walk():
        if descendant.kind not in {"list", "table", "toc"}:
            continue
        candidate = materialized.get(descendant.id)
        if candidate is not None and candidate.kind == descendant.kind:
            bindings[descendant.id] = candidate


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
            attrs={
                "label": label or None,
                "marker": _normalized_list_label(label) if label else None,
                "tagged_node_id": tagged_item.id,
            },
            confidence=0.995,
            evidence=[Evidence("tagged_list_item", 0.995)], sources=sources,
        ))
    attributes = tagged.attrs.get("structure_attributes") if isinstance(tagged.attrs.get("structure_attributes"), dict) else {}
    numbering = str(attributes.get("ListNumbering", "")).lstrip("/")
    ordered = numbering.lower() not in {"", "none", "disc", "circle", "square"}
    if not numbering:
        nonempty_labels = [label for label in labels if label]
        ordered = bool(nonempty_labels and len(nonempty_labels) == len(labels) and all(_ordered_label(label) for label in nonempty_labels))
    start = _label_start(labels[0], numbering) if ordered and labels else 1
    sources = _recursive_sources(tagged)
    marker_style = _list_marker_style(numbering)
    return SemanticNode(
        id="tagged-%s" % tagged.id, kind="list", children=items,
        attrs={
            "ordered": ordered,
            "start": start,
            "list_numbering": numbering or None,
            "marker_style": marker_style,
            "tagged_node_id": tagged.id,
        },
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
    all_tagged_rows = _direct_descendants(
        tagged,
        "table_row",
        stop_kinds={"table"},
    )
    if not all_tagged_rows:
        return None
    # A cell-less TR has no semantic payload or physical location.  Remove it
    # before geometry verification so a producer's phantom row cannot prevent
    # otherwise unambiguous header/body order repair.
    tagged_rows = [
        row
        for row in all_tagged_rows
        if _direct_descendants(
            row,
            "table_cell",
            stop_kinds={"table", "table_row"},
        )
    ]
    if not tagged_rows:
        return None
    structural_row_ids = [row.id for row in all_tagged_rows]
    original_tagged_row_ids = [row.id for row in tagged_rows]
    retained_row_objects = {id(row) for row in tagged_rows}
    ignored_empty_row_ids = [
        row.id for row in all_tagged_rows if id(row) not in retained_row_objects
    ]
    tagged_rows, row_order_repaired = _geometry_ordered_tagged_rows(tagged_rows)
    rows: List[SemanticNode] = []
    header_rows = 0
    for tagged_row in tagged_rows:
        tagged_cells = _direct_descendants(tagged_row, "table_cell", stop_kinds={"table", "table_row"})
        if not tagged_cells:
            continue
        row_index = len(rows)
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
            cell = SemanticNode(
                id="tagged-%s-cell" % tagged_cell.id, kind="table_cell", children=[text_node] if text else [],
                attrs={
                    "row": row_index,
                    "col": column,
                    "rowspan": rowspan,
                    "colspan": colspan,
                    "role": role,
                    "bbox": bbox,
                    "headers": attributes.get("Headers"),
                    "scope": attributes.get("Scope"),
                    **(
                        {"header_id": tagged_cell.attrs["structure_id"]}
                        if tagged_cell.attrs.get("structure_id")
                        else {}
                    ),
                },
                confidence=0.995, evidence=[Evidence("tagged_table_cell", 0.995)], sources=sources,
            )
            _apply_layout_attributes(cell, attributes)
            cells.append(cell)
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
    column_count = _normalize_table_grid_coordinates(rows)
    complex_table = any(int(cell.attrs.get("rowspan", 1)) > 1 or int(cell.attrs.get("colspan", 1)) > 1 for row in rows for cell in row.children)
    table = SemanticNode(
        id="tagged-%s" % tagged.id, kind="table", children=children,
        attrs={"header_rows": header_rows, "row_count": len(rows), "column_count": column_count, "output_mode": "html" if complex_table else "gfm", "tagged_node_id": tagged.id},
        confidence=0.995, evidence=[Evidence("tagged_table_structure", 0.995)], sources=_recursive_sources(tagged),
    )
    table_attributes = (
        tagged.attrs.get("structure_attributes")
        if isinstance(tagged.attrs.get("structure_attributes"), dict)
        else {}
    )
    _apply_table_attributes(table, table_attributes)
    _apply_layout_attributes(table, table_attributes)
    if ignored_empty_row_ids:
        table.evidence.append(Evidence(
            "tagged_table_empty_rows_ignored",
            0.99,
            detail="cell-less tagged rows omitted before physical row ordering",
            data={
                "structural_row_ids": structural_row_ids,
                "ignored_row_ids": ignored_empty_row_ids,
                "retained_row_ids": original_tagged_row_ids,
            },
        ))
    if row_order_repaired:
        table.warnings.append("TAGGED_TABLE_ORDER_GEOMETRY_REPAIRED")
        source_rows = [
            {
                "id": row.id,
                "page": sorted({source.page for source in _recursive_sources(row)})[0],
                "bbox": _sources_bbox(_recursive_sources(row)),
            }
            for row in tagged_rows
        ]
        table.evidence.append(Evidence(
            "tagged_table_geometry_order",
            0.97,
            detail="defective structure-tree row order replaced by verified page geometry",
            data={
                "row_count": len(rows),
                "original_row_ids": original_tagged_row_ids,
                "physical_row_ids": [row.id for row in tagged_rows],
                "ignored_empty_row_ids": ignored_empty_row_ids,
                "physical_rows": source_rows,
            },
        ))
    return table


def _geometry_ordered_tagged_rows(
    rows: Sequence[SemanticNode],
) -> Tuple[List[SemanticNode], bool]:
    """Return physical row order only when every row has unambiguous geometry."""
    positioned: List[Tuple[SemanticNode, int, Tuple[float, float, float, float]]] = []
    for row in rows:
        sources = _recursive_sources(row)
        pages = sorted({source.page for source in sources if source.page > 0})
        bbox = _sources_bbox(sources)
        if len(pages) != 1 or bbox is None or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            return list(rows), False
        positioned.append((row, pages[0], bbox))
    if len({page for _row, page, _bbox in positioned}) != 1:
        return list(rows), False
    physical = sorted(positioned, key=lambda item: ((item[2][1] + item[2][3]) / 2.0, item[2][0], item[0].id))
    for (_left_row, _left_page, left), (_right_row, _right_page, right) in zip(physical, physical[1:]):
        horizontal_overlap = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
        narrower_width = min(left[2] - left[0], right[2] - right[0])
        left_height = left[3] - left[1]
        right_height = right[3] - right[1]
        vertical_tolerance = min(1.0, min(left_height, right_height) * 0.08)
        if (
            right[1] < left[3] - vertical_tolerance
            or horizontal_overlap < narrower_width * 0.50
        ):
            return list(rows), False
    ordered = [row for row, _page, _bbox in physical]
    return ordered, [row.id for row in ordered] != [row.id for row in rows]


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
    marker = _normalized_list_label(label)
    return bool(
        re.fullmatch(
            r"(?:[+-]?\d+|[A-Za-z]|[ivxlcdmIVXLCDM]+)",
            marker,
        )
    )


def _normalized_list_label(label: str) -> str:
    value = str(label or "").strip()
    value = re.sub(r"^[\[(]\s*", "", value)
    value = re.sub(r"\s*[\]).]+$", "", value)
    return value.strip()


def _label_start(label: str, numbering: str = "") -> int:
    marker = _normalized_list_label(label)
    if re.fullmatch(r"[+-]?\d+", marker):
        return int(marker)
    style = _list_marker_style(numbering)
    if style in {"upper-roman", "lower-roman"} or (
        not style
        and len(marker) > 1
        and re.fullmatch(r"[ivxlcdmIVXLCDM]+", marker)
    ):
        return _roman_label_value(marker) or 1
    if style in {"upper-alpha", "lower-alpha"} or re.fullmatch(
        r"[A-Za-z]",
        marker,
    ):
        return _alpha_label_value(marker) or 1
    return 1


def _alpha_label_value(marker: str) -> int:
    if not re.fullmatch(r"[A-Za-z]+", marker):
        return 0
    value = 0
    for character in marker.lower():
        value = value * 26 + ord(character) - ord("a") + 1
    return value


def _roman_label_value(marker: str) -> int:
    if not re.fullmatch(r"[ivxlcdmIVXLCDM]+", marker):
        return 0
    values = {
        "i": 1,
        "v": 5,
        "x": 10,
        "l": 50,
        "c": 100,
        "d": 500,
        "m": 1000,
    }
    total = 0
    previous = 0
    for character in reversed(marker.lower()):
        value = values[character]
        total += -value if value < previous else value
        previous = max(previous, value)
    return total


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
        # Unmatched children are geometry-owned anchors. Reorder only a
        # contiguous cohort for which the structure tree binds every child;
        # this prevents one partial binding from pushing a header/caption to
        # the end of its parent.
        start = 0
        while start < len(parent.children):
            if _child_order_position(parent.children[start], positions) >= 10**9:
                start += 1
                continue
            end = start + 1
            while (
                end < len(parent.children)
                and _child_order_position(parent.children[end], positions) < 10**9
            ):
                end += 1
            cohort = parent.children[start:end]
            original_ids = [child.id for child in cohort]
            proposed = sorted(
                cohort,
                key=lambda child: _child_order_position(child, positions),
            )
            proposed_ids = [child.id for child in proposed]
            if len(cohort) >= 2 and original_ids != proposed_ids:
                axis = (
                    "vertical"
                    if parent.kind == "table"
                    and all(child.kind == "table_row" for child in cohort)
                    else "horizontal"
                    if parent.kind == "table_row"
                    and all(child.kind == "table_cell" for child in cohort)
                    else ""
                )
                original_verified, original_geometry = (
                    _verified_child_axis_order(cohort, axis)
                    if axis
                    else (False, {})
                )
                proposed_verified, proposed_geometry = (
                    _verified_child_axis_order(proposed, axis)
                    if axis
                    else (False, {})
                )
                apply_order = not axis or (
                    proposed_verified and not original_verified
                )
                if axis and original_verified:
                    if "TAGGED_ORDER_GEOMETRY_CONFLICT" not in parent.warnings:
                        parent.warnings.append("TAGGED_ORDER_GEOMETRY_CONFLICT")
                    parent.evidence.append(Evidence(
                        "tagged_child_order_rejected",
                        0.97,
                        detail="verified table geometry retained over conflicting structure-tree order",
                        data={
                            "tagged_parent": tagged_parent.id,
                            "axis": axis,
                            "original_ids": original_ids,
                            "proposed_ids": proposed_ids,
                            "geometry": original_geometry,
                        },
                    ))
                elif axis and not proposed_verified:
                    if "TAGGED_ORDER_GEOMETRY_AMBIGUOUS" not in parent.warnings:
                        parent.warnings.append("TAGGED_ORDER_GEOMETRY_AMBIGUOUS")
                    parent.evidence.append(Evidence(
                        "tagged_child_order_rejected",
                        0.55,
                        detail="tagged table order rejected because neither ordering has complete axis geometry",
                        data={
                            "tagged_parent": tagged_parent.id,
                            "axis": axis,
                            "original_ids": original_ids,
                            "proposed_ids": proposed_ids,
                        },
                    ))
                elif apply_order:
                    parent.children[start:end] = proposed
                    normalized = _normalize_reordered_table_metadata(parent)
                    parent.evidence.append(Evidence(
                        "tagged_child_order_applied",
                        0.97 if axis else 0.94,
                        detail=(
                            "tagged child order applied after proposed axis order matched page geometry"
                            if axis
                            else "tagged child order applied within a fully bound cohort"
                        ),
                        data={
                            "tagged_parent": tagged_parent.id,
                            "axis": axis or None,
                            "original_ids": original_ids,
                            "proposed_ids": proposed_ids,
                            "geometry": proposed_geometry if axis else {},
                            "normalized_metadata": normalized,
                        },
                    ))
            start = end

    # A later horizontal cell reorder can change coordinates after the table's
    # own vertical cohort was normalized.  Recompute every bound table once at
    # the end so rowspans reserve their columns consistently across rows.
    normalized_tables: set[int] = set()
    for candidate in bindings.values():
        if candidate.kind != "table" or id(candidate) in normalized_tables:
            continue
        normalized_tables.add(id(candidate))
        _normalize_reordered_table_metadata(candidate)


def _normalize_reordered_table_metadata(parent: SemanticNode) -> Dict[str, Any]:
    """Keep row/column coordinates consistent with an accepted child order."""
    if parent.kind == "table_row":
        try:
            row_index = max(0, int(parent.attrs.get("row", 0)))
        except (TypeError, ValueError):
            row_index = 0
        column_count = _normalize_table_row_cells(parent, row_index)
        return {"row": row_index, "column_count": column_count}
    if parent.kind != "table":
        return {}

    rows = [child for child in parent.children if child.kind == "table_row"]
    header_flags = []
    for row in rows:
        cells = [child for child in row.children if child.kind == "table_cell"]
        header_flags.append(
            row.attrs.get("role") == "header"
            or bool(cells) and all(cell.attrs.get("role") == "th" for cell in cells)
        )
    column_count = 0
    for row_index, (row, is_header) in enumerate(zip(rows, header_flags)):
        row.attrs["row"] = row_index
        row.attrs["role"] = "header" if is_header else "body"
    column_count = _normalize_table_grid_coordinates(rows)
    header_rows = 0
    for is_header in header_flags:
        if not is_header:
            break
        header_rows += 1
    parent.attrs["header_rows"] = header_rows
    parent.attrs["row_count"] = len(rows)
    parent.attrs["column_count"] = column_count
    return {
        "header_rows": header_rows,
        "row_count": len(rows),
        "column_count": column_count,
    }


def _normalize_table_row_cells(row: SemanticNode, row_index: int) -> int:
    column = 0
    for cell in row.children:
        if cell.kind != "table_cell":
            continue
        cell.attrs["row"] = row_index
        cell.attrs["col"] = column
        column += _positive_int(cell.attrs.get("colspan"), 1)
    return column


def _normalize_table_grid_coordinates(rows: Sequence[SemanticNode]) -> int:
    """Assign coordinates while honoring cells spanning into later rows."""
    occupied: set[Tuple[int, int]] = set()
    column_count = 0
    for row_index, row in enumerate(rows):
        row.attrs["row"] = row_index
        column = 0
        for cell in row.children:
            if cell.kind != "table_cell":
                continue
            while (row_index, column) in occupied:
                column += 1
            rowspan = _positive_int(cell.attrs.get("rowspan"), 1)
            colspan = _positive_int(cell.attrs.get("colspan"), 1)
            cell.attrs["row"] = row_index
            cell.attrs["col"] = column
            cell.attrs["rowspan"] = rowspan
            cell.attrs["colspan"] = colspan
            for covered_row in range(row_index, row_index + rowspan):
                for covered_column in range(column, column + colspan):
                    occupied.add((covered_row, covered_column))
            column_count = max(column_count, column + colspan)
            column += colspan
    return column_count


def _verified_child_axis_order(
    nodes: Sequence[SemanticNode],
    axis: str,
) -> Tuple[bool, Dict[str, Any]]:
    positioned: List[
        Tuple[SemanticNode, int, Tuple[float, float, float, float]]
    ] = []
    for node in nodes:
        sources = _recursive_sources(node)
        pages = sorted({source.page for source in sources if source.page > 0})
        bbox = _sources_bbox(sources)
        if len(pages) != 1 or bbox is None:
            return False, {}
        if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            return False, {}
        positioned.append((node, pages[0], bbox))
    if len({page for _node, page, _bbox in positioned}) != 1:
        return False, {}
    for (_left_node, _left_page, left), (_right_node, _right_page, right) in zip(
        positioned,
        positioned[1:],
    ):
        if axis == "vertical":
            tolerance = min(1.0, min(left[3] - left[1], right[3] - right[1]) * 0.08)
            orthogonal_overlap = max(
                0.0,
                min(left[2], right[2]) - max(left[0], right[0]),
            )
            orthogonal_size = min(left[2] - left[0], right[2] - right[0])
            if right[1] < left[3] - tolerance or orthogonal_overlap < orthogonal_size * 0.50:
                return False, {}
        elif axis == "horizontal":
            tolerance = min(1.0, min(left[2] - left[0], right[2] - right[0]) * 0.02)
            orthogonal_overlap = max(
                0.0,
                min(left[3], right[3]) - max(left[1], right[1]),
            )
            orthogonal_size = min(left[3] - left[1], right[3] - right[1])
            if right[0] < left[2] - tolerance or orthogonal_overlap < orthogonal_size * 0.50:
                return False, {}
        else:
            return False, {}
    return True, {
        "page": positioned[0][1],
        "bboxes": {node.id: bbox for node, _page, bbox in positioned},
    }


def _child_order_position(node: SemanticNode, positions: Dict[str, int]) -> int:
    matches = [positions[candidate.id] for candidate in node.walk() if candidate.id in positions]
    return min(matches) if matches else 10**9
