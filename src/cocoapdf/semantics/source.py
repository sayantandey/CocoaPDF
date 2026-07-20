from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..ir.semantic import NodeFactory, SemanticNode, SourceRef, merge_sources


def line_identifier(line: Any) -> str:
	return "line-%s-%s" % (getattr(line, "page", 0), getattr(line, "seq", 0))


def region_index(regions: Sequence[Any]) -> Dict[str, Tuple[str, ...]]:
	mapping: Dict[str, List[str]] = {}
	for region in regions:
		region_id = str(getattr(region, "id", "") or "")
		for child in getattr(region, "children", ()) or ():
			if isinstance(child, str) and region_id:
				mapping.setdefault(child, []).append(region_id)
	return {key: tuple(sorted(set(values))) for key, values in mapping.items()}


def sources_from_chars(
	characters: Iterable[Any],
	regions_by_line: Optional[Dict[str, Tuple[str, ...]]] = None,
) -> List[SourceRef]:
	characters = list(characters)
	by_page: Dict[int, Dict[str, Any]] = {}
	for character in characters:
		page = int(getattr(character, "page", 0) or 0)
		if page <= 0:
			continue
		item = by_page.setdefault(
			page,
			{"glyphs": set(), "regions": set(), "mcids": set(), "boxes": []},
		)
		seq = int(getattr(character, "seq", 0) or 0)
		if seq:
			item["glyphs"].add(seq)
		for mark in getattr(character, "mc", ()) or ():
			if isinstance(mark, dict):
				mcid = mark.get("mcid")
				if isinstance(mcid, int) and not isinstance(mcid, bool):
					item["mcids"].add(mcid)
		item["boxes"].append(
			(
				float(getattr(character, "x0", 0.0)),
				float(getattr(character, "y0", 0.0)),
				float(getattr(character, "x1", 0.0)),
				float(getattr(character, "y1", 0.0)),
			)
		)
	if regions_by_line:
		for character in characters:
			page = int(getattr(character, "page", 0) or 0)
			if page <= 0:
				continue
			# Line membership is established by callers when possible; glyph-only
			# sources remain valid even when a character has not yet been grouped.
			line_key = getattr(character, "line_id", None)
			if line_key:
				by_page[page]["regions"].update(regions_by_line.get(str(line_key), ()))
	out: List[SourceRef] = []
	for page, item in sorted(by_page.items()):
		boxes = item["boxes"]
		bbox = None
		if boxes:
			bbox = (
				min(box[0] for box in boxes),
				min(box[1] for box in boxes),
				max(box[2] for box in boxes),
				max(box[3] for box in boxes),
			)
		out.append(
			SourceRef(
				page=page,
				glyph_ids=tuple(sorted(item["glyphs"])),
				region_ids=tuple(sorted(item["regions"])),
				mcids=tuple(sorted(item["mcids"])),
				bbox=bbox,
			)
		)
	return out


def sources_from_lines(
	lines: Sequence[Any],
	regions_by_line: Optional[Dict[str, Tuple[str, ...]]] = None,
) -> List[SourceRef]:
	by_page: Dict[int, Dict[str, Any]] = {}
	for line in lines:
		page = int(getattr(line, "page", 0) or 0)
		if page <= 0:
			continue
		item = by_page.setdefault(
			page,
			{"glyphs": set(), "regions": set(), "mcids": set(), "boxes": []},
		)
		if regions_by_line:
			item["regions"].update(regions_by_line.get(line_identifier(line), ()))
		for char in getattr(line, "chars", ()) or ():
			seq = int(getattr(char, "seq", 0) or 0)
			if seq:
				item["glyphs"].add(seq)
			for mark in getattr(char, "mc", ()) or ():
				if isinstance(mark, dict):
					mcid = mark.get("mcid")
					if isinstance(mcid, int) and not isinstance(mcid, bool):
						item["mcids"].add(mcid)
			item["boxes"].append((float(char.x0), float(char.y0), float(char.x1), float(char.y1)))
	out: List[SourceRef] = []
	for page, item in sorted(by_page.items()):
		boxes = item["boxes"]
		bbox = None
		if boxes:
			bbox = (
				min(box[0] for box in boxes),
				min(box[1] for box in boxes),
				max(box[2] for box in boxes),
				max(box[3] for box in boxes),
			)
		out.append(
			SourceRef(
				page=page,
				glyph_ids=tuple(sorted(item["glyphs"])),
				region_ids=tuple(sorted(item["regions"])),
				mcids=tuple(sorted(item["mcids"])),
				bbox=bbox,
			)
		)
	return out


def source_from_token(
	token: Dict[str, Any],
	regions: Tuple[str, ...] = (),
	include_object_refs: bool = False,
) -> List[SourceRef]:
	page = int(token.get("page", 0) or 0)
	if page <= 0:
		return []
	bbox = token.get("bbox")
	if bbox is not None:
		bbox = tuple(float(value) for value in bbox)
	return [
		SourceRef(
			page=page,
			glyph_ids=tuple(int(value) for value in token.get("glyph_ids", ()) if int(value) > 0),
			region_ids=tuple(sorted(set(regions))),
			mcids=tuple(int(value) for value in token.get("mcids", ()) if isinstance(value, int)),
			object_refs=tuple(str(value) for value in token.get("object_refs", ()) if include_object_refs and value),
			bbox=bbox,  # type: ignore[arg-type]
		)
	]


def inline_nodes_from_tokens(
	factory: NodeFactory,
	tokens: Sequence[Dict[str, Any]],
	regions: Tuple[str, ...] = (),
) -> List[SemanticNode]:
	out: List[SemanticNode] = []
	for token in tokens:
		text = str(token.get("text", ""))
		if not text:
			continue
		sources = source_from_token(token, regions, include_object_refs=False)
		base = factory.make(
			"text",
			text=text,
			attrs={"hard_break": bool(token.get("hard_break"))},
			confidence=0.99 if token.get("glyph_ids") else 0.92,
			sources=sources,
		)
		style = tuple(token.get("style", (False,) * 8))
		node = base
		for enabled, kind in (
			(bool(style[2]), "code"),
			(bool(style[0] and style[1]), "strong_emphasis"),
			(bool(style[0] and not style[1]), "strong"),
			(bool(style[1] and not style[0]), "emphasis"),
			(bool(style[3]), "strikethrough"),
			(bool(style[4]), "underline"),
			(bool(style[5]), "superscript"),
			(bool(style[6]), "subscript"),
			(bool(style[7]), "mark"),
		):
			if not enabled:
				continue
			if kind == "strong_emphasis":
				emphasis = factory.make("emphasis", children=[node], confidence=node.confidence, sources=list(node.sources))
				node = factory.make("strong", children=[emphasis], confidence=node.confidence, sources=list(node.sources))
			else:
				node = factory.make(kind, children=[node], confidence=node.confidence, sources=list(node.sources))
		link = token.get("link")
		if link and text.strip():
			link_sources = source_from_token(token, regions, include_object_refs=True) or list(node.sources)
			node = factory.make(
				"link",
				children=[node],
				attrs={"href": str(link)},
				confidence=0.99,
				sources=link_sources,
			)
		out.append(node)
	return coalesce_inline_nodes(out)


def coalesce_inline_nodes(nodes: Sequence[SemanticNode]) -> List[SemanticNode]:
	out: List[SemanticNode] = []
	for node in nodes:
		if (
			out
			and node.kind == "text"
			and out[-1].kind == "text"
			and node.attrs == out[-1].attrs
		):
			out[-1].text += node.text
			out[-1].sources = merge_sources(out[-1].sources + node.sources)
			continue
		out.append(node)
	return out
