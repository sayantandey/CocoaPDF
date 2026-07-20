from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..ir.evidence import Evidence
from ..ir.regions import Rect, Region


def detect_regions(
	lines_by_page: Dict[int, List[Any]],
	segments: Sequence[Any],
	fills: Sequence[Any],
	images: Sequence[Any],
	page_sizes: Dict[int, Tuple[float, float]],
	block_events_by_page: Optional[Dict[int, List[Any]]] = None,
) -> List[Region]:
	regions: List[Region] = []
	furniture = _repeated_furniture(lines_by_page, page_sizes)
	order = 0
	for page in sorted(page_sizes):
		lines = [line for line in lines_by_page.get(page, []) if line_text(line)]
		width, height = page_sizes.get(page, (612.0, 792.0))
		header_ids, footer_ids = furniture.get(page, (set(), set()))
		header_lines = [line for line in lines if id(line) in header_ids]
		footer_lines = [line for line in lines if id(line) in footer_ids]
		claimed = {id(line) for line in header_lines + footer_lines}
		if header_lines:
			regions.append(_region(page, "header", header_lines, order, 0.94, "repeated_top_margin"))
			order += 1
		structured = _event_regions(
			page,
			(block_events_by_page or {}).get(page, []),
			order,
		)
		regions.extend(structured)
		order += len(structured)
		claimed.update(_region_child_ids(structured, lines))
		page_callouts = _callout_regions(
			page,
			[line for line in lines if id(line) not in claimed],
			fills,
			images,
			order,
		)
		regions.extend(page_callouts)
		order += len(page_callouts)
		claimed.update(_region_child_ids(page_callouts, lines))
		regions.extend(_figure_regions(page, images, order))
		order += sum(1 for image in images if getattr(image, "page", None) == page)
		footnote = _footnote_region(page, [line for line in lines if id(line) not in claimed], segments, page_sizes.get(page, (width, height)), order)
		if footnote:
			regions.append(footnote)
			order += 1
			claimed.update(_region_child_ids([footnote], lines))
		available = [line for line in lines if id(line) not in claimed]
		page_columns = _column_regions(page, available, segments, page_sizes.get(page, (width, height)), order)
		if page_columns:
			regions.extend(page_columns)
			order += len(page_columns)
		else:
			body_lines = available
			if body_lines:
				regions.append(_region(page, "body", body_lines, order, 0.86, "single_column_body"))
				order += 1
		if footer_lines:
			regions.append(_region(page, "footer", footer_lines, order, 0.94, "repeated_bottom_margin"))
			order += 1
	return sorted(_dedupe_regions(regions), key=lambda r: (r.page, r.reading_order_index, r.bbox.y0, r.bbox.x0))


def _region_child_ids(regions: Sequence[Region], lines: Sequence[Any]) -> set[int]:
	by_name = {line_id(line): id(line) for line in lines}
	return {
		by_name[child]
		for region in regions
		for child in region.children
		if isinstance(child, str) and child in by_name
	}


def _repeated_furniture(
	lines_by_page: Dict[int, List[Any]],
	page_sizes: Dict[int, Tuple[float, float]],
) -> Dict[int, Tuple[set[int], set[int]]]:
	"""Return repeated header/footer line identities, including odd/even sets."""
	pages = sorted(page_sizes)
	if len(pages) < 2:
		return {}
	candidates: Dict[Tuple[str, str], List[Tuple[int, Any]]] = {}
	for page in pages:
		height = page_sizes[page][1]
		for line in lines_by_page.get(page, []):
			text = line_text(line)
			if not text:
				continue
			zone = "header" if line.y1 <= height * 0.15 else "footer" if line.y0 >= height * 0.85 else ""
			if not zone:
				continue
			signature = _furniture_signature(text)
			if signature:
				candidates.setdefault((zone, signature), []).append((page, line))
	result: Dict[int, Tuple[set[int], set[int]]] = {}
	page_set = set(pages)
	for (zone, _signature), occurrences in candidates.items():
		seen_pages = {page for page, _line in occurrences}
		all_required = max(2, int(math.ceil(len(pages) * 0.50)))
		repeated = len(seen_pages) >= all_required
		if not repeated:
			for parity in (0, 1):
				parity_pages = {page for page in page_set if page % 2 == parity}
				if len(parity_pages) < 2:
					continue
				matches = seen_pages & parity_pages
				if len(matches) >= max(2, int(math.ceil(len(parity_pages) * 0.75))):
					repeated = True
					break
		if not repeated:
			continue
		for page, line in occurrences:
			headers, footers = result.setdefault(page, (set(), set()))
			(headers if zone == "header" else footers).add(id(line))
	return result


def _furniture_signature(text: str) -> str:
	value = text.casefold()
	value = re.sub(r"\b(?:page\s+)?[ivxlcdm]{1,8}\b(?=\s*$)", "#", value)
	value = re.sub(r"\d+", "#", value)
	value = re.sub(r"\s+", " ", value).strip(" -|\u2013\u2014\u2022\u00b7")
	return value


def _dedupe_regions(regions: Sequence[Region]) -> List[Region]:
	deduped: List[Region] = []
	seen = set()
	for region in regions:
		key = (
			region.page,
			region.kind,
			round(region.bbox.x0, 1),
			round(region.bbox.y0, 1),
			round(region.bbox.x1, 1),
			round(region.bbox.y1, 1),
			tuple(region.children),
		)
		if key in seen:
			continue
		seen.add(key)
		deduped.append(region)
	return deduped


def _column_regions(page: int, lines: List[Any], segments: Sequence[Any], page_size: Tuple[float, float], order: int) -> List[Region]:
	width, _height = page_size
	separators = []
	for seg in segments:
		if getattr(seg, "page", None) != page or not getattr(seg, "vertical", False):
			continue
		x = (seg.x0 + seg.x1) / 2
		y0, y1 = sorted((seg.y0, seg.y1))
		if width * 0.30 <= x <= width * 0.70 and (y1 - y0) >= 80:
			separators.append((x, y0, y1))
	evidence_kind = "column_gutter"
	evidence_data: Dict[str, Any] = {}
	if separators:
		sep_x, y0, y1 = max(separators, key=lambda item: item[2] - item[1])
		band = [line for line in lines if y0 - 8 <= line.y0 <= y1 + 8]
		left = [line for line in band if (line.x0 + line.x1) / 2 < sep_x]
		right = [line for line in band if (line.x0 + line.x1) / 2 >= sep_x]
		if len(left) < 2 or len(right) < 2:
			return []
	else:
		inferred = _outer_rule_column_band(page, lines, segments, width)
		if inferred is None:
			return []
		sep_x, y0, y1, outer_rule_x, left, right = inferred
		evidence_kind = "column_whitespace_gutter"
		evidence_data["outer_rule_x"] = outer_rule_x
	evidence_data["separator_x"] = sep_x
	return [
		_region(page, "column", left, order, 0.92, evidence_kind, dict(evidence_data, side="left")),
		_region(page, "column", right, order + 1, 0.92, evidence_kind, dict(evidence_data, side="right")),
	]


def _outer_rule_column_band(
	page: int,
	lines: List[Any],
	segments: Sequence[Any],
	page_width: float,
) -> Optional[Tuple[float, float, float, float, List[Any], List[Any]]]:
	"""Infer a local two-column band from an outer rule and aligned text starts.

	Some producers draw only the styled container's left border and leave the
	column gutter as whitespace. The rule bounds the candidate vertically; a
	large split in repeated line starts and overlapping text on both sides then
	provides the independent column evidence.
	"""
	best: Optional[Tuple[float, Tuple[float, float, float, float, List[Any], List[Any]]]] = None
	for seg in segments:
		if (
			getattr(seg, "page", None) != page
			or not getattr(seg, "vertical", False)
			or getattr(seg, "width", 0.0) < 1.5
		):
			continue
		rule_x = (seg.x0 + seg.x1) / 2
		sy0, sy1 = sorted((seg.y0, seg.y1))
		if rule_x > page_width * 0.30 or sy1 - sy0 < 40.0:
			continue
		band = [
			line
			for line in lines
			if sy0 - 8.0 <= (line.y0 + line.y1) / 2 <= sy1 + 8.0
			and line.x0 >= rule_x - 3.0
			and line_text(line)
		]
		if len(band) < 4:
			continue
		starts = sorted({round(line.x0, 1) for line in band})
		if len(starts) < 2:
			continue
		body = median([getattr(line, "size", 0.0) for line in band])
		start_left, start_right = max(
			zip(starts, starts[1:]),
			key=lambda pair: pair[1] - pair[0],
		)
		start_gap = start_right - start_left
		if start_gap < max(72.0, body * 7.0):
			continue
		start_separator = (start_left + start_right) / 2
		left = [line for line in band if line.x0 < start_separator]
		right = [line for line in band if line.x0 >= start_separator]
		if len(left) < 2 or len(right) < 2:
			continue
		left_y0 = min(line.y0 for line in left)
		left_y1 = max(line.y1 for line in left)
		right_y0 = min(line.y0 for line in right)
		right_y1 = max(line.y1 for line in right)
		overlap = min(left_y1, right_y1) - max(left_y0, right_y0)
		minimum_span = min(left_y1 - left_y0, right_y1 - right_y0)
		if minimum_span <= 0 or overlap < minimum_span * 0.40:
			continue
		left_edge = max(line.x1 for line in left)
		right_edge = min(line.x0 for line in right)
		sep_x = (left_edge + right_edge) / 2 if left_edge < right_edge else start_separator
		if not (rule_x + max(4.0, body * 0.4) < min(line.x0 for line in left)):
			continue
		y0 = min(left_y0, right_y0)
		y1 = max(left_y1, right_y1)
		score = (y1 - y0) + start_gap * 0.25
		value = (sep_x, y0, y1, rule_x, left, right)
		if best is None or score > best[0]:
			best = (score, value)
	return best[1] if best is not None else None


def _event_regions(page: int, events: Sequence[Any], order: int) -> List[Region]:
	"""Promote renderer-verified structural blocks into provenance regions."""
	kind_map = {"table": "table", "callout": "callout", "equation": "equation"}
	confidence = {"table": 0.94, "callout": 0.90, "equation": 0.86}
	out: List[Region] = []
	claimed: set[str] = set()
	for event in sorted(events, key=lambda item: float(getattr(item, "rank", 0.0))):
		event_kind = str(getattr(event, "kind", ""))
		region_kind = kind_map.get(event_kind)
		event_lines = [line for line in getattr(event, "lines", []) if line_text(line)]
		if region_kind is None or not event_lines:
			continue
		unique_lines = [line for line in event_lines if line_id(line) not in claimed]
		if not unique_lines:
			continue
		claimed.update(line_id(line) for line in unique_lines)
		out.append(
			_region(
				page,
				region_kind,
				unique_lines,
				order + len(out),
				confidence[event_kind],
				"renderer_%s_block" % event_kind,
			)
		)
	return out


def _callout_regions(
	page: int,
	lines: List[Any],
	fills: Sequence[Any],
	images: Sequence[Any],
	order: int,
) -> List[Region]:
	out: List[Region] = []
	idx = 0
	for fill in fills:
		if getattr(fill, "page", None) != page:
			continue
		w = fill.x1 - fill.x0
		h = fill.y1 - fill.y0
		if w < 80 or h < 18 or w > 650 or h > 260:
			continue
		if max(fill.color) - min(fill.color) > 0.18:
			continue
		if any(
			getattr(image, "page", None) == page
			and image.x0 - 3 <= (fill.x0 + fill.x1) / 2 <= image.x1 + 3
			and image.y0 - 3 <= (fill.y0 + fill.y1) / 2 <= image.y1 + 3
			for image in images
		):
			continue
		inside = [line for line in lines if fill.x0 - 3 <= line.x0 and line.x1 <= fill.x1 + 3 and fill.y0 - 3 <= line.y0 and line.y1 <= fill.y1 + 3]
		# Neutral backgrounds behind monospaced cohorts are code blocks, not
		# callouts. Actual callouts remain eligible even when their fill is gray.
		if inside and not all(float(getattr(line, "mono_ratio", 0.0)) >= 0.70 for line in inside):
			out.append(_region(page, "callout", inside, order + idx, 0.78, "background_fill_box"))
			idx += 1
	return out


def _figure_regions(page: int, images: Sequence[Any], order: int) -> List[Region]:
	out: List[Region] = []
	for idx, image in enumerate(img for img in images if getattr(img, "page", None) == page):
		out.append(
			Region(
				id="p%d-figure-%d" % (page, idx + 1),
				page=page,
				kind="figure",
				bbox=Rect(image.x0, image.y0, image.x1, image.y1),
				confidence=0.88,
				evidence=[Evidence("image_xobject", 0.88, getattr(image, "name", ""), page)],
				reading_order_index=order + idx,
			)
		)
	return out


def _footnote_region(page: int, lines: List[Any], segments: Sequence[Any], page_size: Tuple[float, float], order: int) -> Optional[Region]:
	width, height = page_size
	separators = [
		seg
		for seg in segments
		if getattr(seg, "page", None) == page
		and getattr(seg, "horizontal", False)
		and seg.y0 > height * 0.65
		and 40 <= abs(seg.x1 - seg.x0) <= width * 0.55
	]
	if not separators:
		return None
	sep = min(separators, key=lambda s: s.y0)
	foot_lines = [line for line in lines if line.y0 > sep.y0 and getattr(line, "size", 0) <= median([getattr(l, "size", 0) for l in lines]) * 0.95]
	if not foot_lines:
		return None
	return _region(page, "footnote", foot_lines, order, 0.74, "footnote_separator")


def _region(page: int, kind: str, lines: List[Any], order: int, confidence: float, evidence_kind: str, data: Optional[Dict[str, Any]] = None) -> Region:
	bbox = lines_bbox(lines)
	return Region(
		id="p%d-%s-%d" % (page, kind, order + 1),
		page=page,
		kind=kind,  # type: ignore[arg-type]
		bbox=bbox,
		children=[line_id(line) for line in lines],
		confidence=confidence,
		evidence=[Evidence(evidence_kind, confidence, page=page, data=data or {})],
		reading_order_index=order,
	)


def line_text(line: Any) -> str:
	return "".join(getattr(ch, "text", "") for ch in getattr(line, "chars", [])).strip()


def line_id(line: Any) -> str:
	return "line-%s-%s" % (getattr(line, "page", 0), getattr(line, "seq", 0))


def lines_bbox(lines: Iterable[Any]) -> Rect:
	items = list(lines)
	return Rect(min(l.x0 for l in items), min(l.y0 for l in items), max(l.x1 for l in items), max(l.y1 for l in items))


def median(values: List[float]) -> float:
	vals = sorted(v for v in values if v > 0)
	if not vals:
		return 0.0
	mid = len(vals) // 2
	if len(vals) % 2:
		return vals[mid]
	return (vals[mid - 1] + vals[mid]) / 2.0
