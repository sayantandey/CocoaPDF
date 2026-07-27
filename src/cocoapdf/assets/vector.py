from __future__ import annotations

import hashlib
import html
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple


@dataclass(frozen=True)
class VectorFigure:
	page: int
	bbox: Tuple[float, float, float, float]
	name: str
	data: bytes
	line_ids: Tuple[int, ...]
	seq: int


def detect_vector_figures(
	converter: Any,
	lines_by_page: Dict[int, List[Any]],
	table_boxes: Dict[int, List[Tuple[float, float, float, float]]],
) -> List[VectorFigure]:
	out: List[VectorFigure] = []
	claimed: Dict[int, List[Tuple[float, float, float, float]]] = {}
	for page, box, seq in _composite_diagram_boxes(converter):
		if any(
			image.page == page
			and box[0] <= (image.x0 + image.x1) / 2 <= box[2]
			and box[1] <= (image.y0 + image.y1) / 2 <= box[3]
			for image in converter.images
		):
			continue
		lines, line_ids = _capture_lines(lines_by_page.get(page, []), box)
		if not lines:
			continue
		segments = [
			segment
			for segment in converter.segments
			if segment.page == page and _segment_inside(segment, box, 3.0)
		]
		svg = _render_svg(converter, box, lines, segments)
		out.append(
			VectorFigure(
				page=page,
				bbox=box,
				name="vector-%s.svg" % hashlib.sha256(svg).hexdigest()[:16],
				data=svg,
				line_ids=tuple(line_ids),
				seq=seq,
			)
		)
		claimed.setdefault(page, []).append(box)
	for fill in sorted(
		converter.fills,
		key=lambda item: (item.page, -item.x1 + item.x0, item.seq),
	):
		page_width, page_height = converter.page_sizes.get(
			fill.page,
			(612.0, 792.0),
		)
		box = _source_viewport_box(fill)
		width = box[2] - box[0]
		height = box[3] - box[1]
		area = width * height
		if width < 72.0 or height < 28.0:
			continue
		if area > page_width * page_height * 0.45:
			continue
		if width / max(height, 1.0) < 1.45:
			continue
		spread = max(fill.color) - min(fill.color)
		neutral_background = spread < 0.03 and 0.90 <= min(fill.color) <= 1.0
		if any(
			_contains(box, table_box, 2.0)
			or _contains(table_box, box, 2.0)
			for table_box in table_boxes.get(fill.page, [])
		):
			continue
		if any(
			_overlap_ratio(box, previous) >= 0.80
			for previous in claimed.get(fill.page, [])
		):
			continue
		if any(
			image.page == fill.page
			and box[0] <= (image.x0 + image.x1) / 2 <= box[2]
			and box[1] <= (image.y0 + image.y1) / 2 <= box[3]
			for image in converter.images
		):
			continue
		segments = [
			segment
			for segment in converter.segments
			if segment.page == fill.page and _segment_inside(segment, box, 3.0)
		]
		internal = [
			segment
			for segment in segments
			if not _segment_on_box_edge(segment, box, 4.0)
			and segment.length >= max(10.0, min(width, height) * 0.12)
		]
		contained_colored_fills = [
			candidate
			for candidate in converter.fills
			if candidate is not fill
			and candidate.page == fill.page
			and box[0] - 2.0 <= candidate.x0
			and candidate.x1 <= box[2] + 2.0
			and box[1] - 2.0 <= candidate.y0
			and candidate.y1 <= box[3] + 2.0
			and max(candidate.color) - min(candidate.color) >= 0.08
		]
		diagonal_count = sum(1 for segment in internal if not segment.horizontal and not segment.vertical)
		strong_vector_evidence = diagonal_count >= 2 or (
			len(internal) >= 2 and len(contained_colored_fills) >= 2
		)
		if len(internal) < 3 and not strong_vector_evidence:
			continue
		if neutral_background and not strong_vector_evidence:
			continue
		lines, line_ids = _capture_lines(lines_by_page.get(fill.page, []), box)
		if not lines:
			continue
		svg = _render_svg(converter, box, lines, segments)
		name = "vector-%s.svg" % hashlib.sha256(svg).hexdigest()[:16]
		out.append(
			VectorFigure(
				page=fill.page,
				bbox=box,
				name=name,
				data=svg,
				line_ids=tuple(line_ids),
				seq=fill.seq,
			)
		)
		claimed.setdefault(fill.page, []).append(box)
	return _group_adjacent_panels(converter, out, lines_by_page)


def _capture_lines(
	source_lines: Sequence[Any],
	box: Tuple[float, float, float, float],
) -> Tuple[List[Any], List[int]]:
	candidates = [
		line
		for line in source_lines
		if any(
			box[0] <= (char.x0 + char.x1) / 2 <= box[2]
			and box[1] <= (char.y0 + char.y1) / 2 <= box[3]
			for char in line.chars
		)
	]
	lines: List[Any] = []
	line_ids: List[int] = []
	for line in candidates:
		inside = []
		outside = []
		for run in _line_char_runs(line):
			visible = [char for char in run if char.text.strip()]
			inside_count = sum(
				1
				for char in visible
				if box[0] <= (char.x0 + char.x1) / 2 <= box[2]
				and box[1] <= (char.y0 + char.y1) / 2 <= box[3]
			)
			# Classify a contiguous painted text run as one unit. This keeps a
			# separated outside label in document flow while retaining the final
			# glyphs of an artwork label that slightly crosses its clipping box.
			if visible and inside_count * 2 >= len(visible):
				inside.extend(run)
			else:
				outside.extend(run)
		if outside and not any(char.text.strip() for char in outside):
			inside = list(line.chars)
			outside = []
		if not inside:
			continue
		if outside:
			# A PDF producer may put prose beside a vector in the same text
			# baseline. Replay only glyphs geometrically inside the artwork and
			# leave the outside glyphs in the document flow.
			lines.append(
				type(line)(
					inside,
					line.page,
					min(char.seq for char in inside),
					source_order=getattr(line, "source_order", False),
					writing_mode=getattr(line, "writing_mode", "horizontal"),
				)
			)
			line.chars[:] = outside
			invalidate = getattr(line, "invalidate_caches", None)
			if callable(invalidate):
				invalidate()
		else:
			lines.append(line)
			line_ids.append(id(line))
	return lines, line_ids


def _composite_diagram_boxes(
	converter: Any,
) -> List[Tuple[int, Tuple[float, float, float, float], int]]:
	"""Find one native vector diagram even when it extends across several panels.

	A large colored rectangle alone is not enough evidence: tables, code blocks,
	and callouts frequently use the same geometry.  Require nested panels,
	non-border connectors, and multiple compact filled paths adjacent to those
	connectors (normally arrowheads) before suppressing the diagram's text from
	document flow.
	"""
	candidates: List[Tuple[int, Tuple[float, float, float, float], int]] = []
	fills_by_page: Dict[int, List[Any]] = {}
	segments_by_page: Dict[int, List[Any]] = {}
	paths_by_page: Dict[int, List[Any]] = {}
	for fill in converter.fills:
		fills_by_page.setdefault(fill.page, []).append(fill)
	for segment in converter.segments:
		segments_by_page.setdefault(segment.page, []).append(segment)
	for path in converter.painted_paths:
		paths_by_page.setdefault(path.page, []).append(path)

	for page, page_fills in fills_by_page.items():
		page_width, page_height = converter.page_sizes.get(page, (612.0, 792.0))
		page_area = page_width * page_height
		page_segments = segments_by_page.get(page, [])
		page_paths = paths_by_page.get(page, [])
		fill_edge_segment_ids = {
			id(segment)
			for segment in page_segments
			if _segment_on_any_fill_edge(segment, page_fills, 3.0)
		}
		for background in page_fills:
			base = _source_viewport_box(background)
			width = base[2] - base[0]
			height = base[3] - base[1]
			area = width * height
			if width < 144.0 or height < 72.0:
				continue
			if not (page_area * 0.015 <= area <= page_area * 0.50):
				continue
			nested = [
				fill
				for fill in page_fills
				if fill is not background
				and _contains((fill.x0, fill.y0, fill.x1, fill.y1), base, 2.0)
				and 24.0 <= fill.x1 - fill.x0 <= width * 0.78
				and 12.0 <= fill.y1 - fill.y0 <= height * 0.38
				and _color_distance(fill.color, background.color) >= 0.04
			]
			if len(nested) < 4:
				continue
			connectors = [
				segment
				for segment in page_segments
				if segment.length >= 6.0
				and _segment_inside(segment, base, 3.0)
				and id(segment) not in fill_edge_segment_ids
			]
			if len(connectors) < 4:
				continue
			arrowheads = [
				path
				for path in page_paths
				if _looks_like_arrowhead(path)
				and _contains(path.bbox, base, 3.0)
				and any(_path_near_segment(path, segment, 10.0) for segment in connectors)
			]
			if len(arrowheads) < 2:
				continue
			box = _expand_connected_diagram(
				page,
				base,
				background,
				page_fills,
				page_segments,
				page_paths,
				fill_edge_segment_ids,
			)
			expanded_area = (box[2] - box[0]) * (box[3] - box[1])
			if expanded_area > page_area * 0.65:
				continue
			candidates.append((page, box, background.seq))

	# Prefer the complete containing diagram when several eligible backgrounds
	# describe the same graphic.
	ordered = sorted(
		candidates,
		key=lambda item: (
			item[0],
			-(item[1][2] - item[1][0]) * (item[1][3] - item[1][1]),
			item[2],
		),
	)
	out: List[Tuple[int, Tuple[float, float, float, float], int]] = []
	for candidate in ordered:
		if any(
			candidate[0] == previous[0]
			and _overlap_ratio(candidate[1], previous[1]) >= 0.75
			for previous in out
		):
			continue
		out.append(candidate)
	return sorted(out, key=lambda item: (item[0], item[1][1], item[1][0], item[2]))


def _expand_connected_diagram(
	page: int,
	base: Tuple[float, float, float, float],
	background: Any,
	page_fills: Sequence[Any],
	page_segments: Sequence[Any],
	page_paths: Sequence[Any],
	fill_edge_segment_ids: set[int],
) -> Tuple[float, float, float, float]:
	base_width = base[2] - base[0]
	base_height = base[3] - base[1]
	x_padding = max(12.0, base_width * 0.12)
	x_min = base[0] - x_padding
	x_max = base[2] + x_padding
	eligible_fills = [
		fill
		for fill in page_fills
		if fill.page == page
		and fill.x0 >= x_min
		and fill.x1 <= x_max
		and fill.x1 - fill.x0 <= base_width * 0.88
		and fill.y1 - fill.y0 <= max(120.0, base_height * 0.46)
	]
	connector_segments = [
		segment
		for segment in page_segments
		if segment.page == page
		and segment.length >= 6.0
		and min(segment.x0, segment.x1) >= x_min
		and max(segment.x0, segment.x1) <= x_max
		and segment.length <= max(base_width * 0.72, base_height * 0.55)
		and id(segment) not in fill_edge_segment_ids
	]

	selected_fills: List[Any] = [background]
	selected_fill_ids = {id(background)}
	for fill in eligible_fills:
		if _contains((fill.x0, fill.y0, fill.x1, fill.y1), base, 2.0):
			selected_fills.append(fill)
			selected_fill_ids.add(id(fill))
	selected_segments = [
		segment
		for segment in connector_segments
		if _segment_inside(segment, base, 3.0)
	]
	selected_segment_ids = {id(segment) for segment in selected_segments}

	for _iteration in range(24):
		changed = False
		selected_boxes = [base] + [
			(fill.x0, fill.y0, fill.x1, fill.y1)
			for fill in selected_fills
		]
		for segment in connector_segments:
			if id(segment) in selected_segment_ids:
				continue
			if any(
				_segment_touches_box(segment, box, 1.5)
				for box in selected_boxes
			) or any(
				_segments_connected(segment, selected, 2.5)
				for selected in selected_segments
			):
				selected_segments.append(segment)
				selected_segment_ids.add(id(segment))
				changed = True
		for fill in eligible_fills:
			if id(fill) in selected_fill_ids:
				continue
			fill_box = (fill.x0, fill.y0, fill.x1, fill.y1)
			if any(
				_contains(fill_box, selected_box, 2.0)
				for selected_box in selected_boxes
			) or any(
				_segment_touches_box(segment, fill_box, 7.0)
				for segment in selected_segments
			):
				selected_fills.append(fill)
				selected_fill_ids.add(id(fill))
				changed = True
		if not changed:
			break

	selected_paths = [
		path
		for path in page_paths
		if _looks_like_arrowhead(path)
		and x_min <= path.bbox[0]
		and path.bbox[2] <= x_max
		and any(_path_near_segment(path, segment, 10.0) for segment in selected_segments)
	]
	boxes = [base]
	boxes.extend((fill.x0, fill.y0, fill.x1, fill.y1) for fill in selected_fills)
	boxes.extend(
		(
			min(segment.x0, segment.x1),
			min(segment.y0, segment.y1),
			max(segment.x0, segment.x1),
			max(segment.y0, segment.y1),
		)
		for segment in selected_segments
	)
	boxes.extend(path.bbox for path in selected_paths)
	box = _union_boxes(boxes)
	max_height = base_height * 2.20
	if box[3] - box[1] > max_height:
		return base
	return box


def _segment_on_any_fill_edge(
	segment: Any,
	fills: Sequence[Any],
	tolerance: float,
) -> bool:
	for fill in fills:
		if segment.horizontal:
			y = (segment.y0 + segment.y1) / 2.0
			sx0, sx1 = sorted((segment.x0, segment.x1))
			if (
				(abs(y - fill.y0) <= tolerance or abs(y - fill.y1) <= tolerance)
				and fill.x0 - tolerance <= sx0
				and sx1 <= fill.x1 + tolerance
			):
				return True
		elif segment.vertical:
			x = (segment.x0 + segment.x1) / 2.0
			sy0, sy1 = sorted((segment.y0, segment.y1))
			if (
				(abs(x - fill.x0) <= tolerance or abs(x - fill.x1) <= tolerance)
				and fill.y0 - tolerance <= sy0
				and sy1 <= fill.y1 + tolerance
			):
				return True
	return False


def _looks_like_arrowhead(path: Any) -> bool:
	x0, y0, x1, y1 = path.bbox
	width = x1 - x0
	height = y1 - y0
	commands = tuple(path.commands)
	if not (3.0 <= width <= 24.0 and 3.0 <= height <= 24.0):
		return False
	if not (4 <= len(commands) <= 12):
		return False
	kinds = [command for command, _values in commands]
	return kinds.count("M") >= 1 and kinds.count("L") >= 2


def _path_near_segment(path: Any, segment: Any, distance: float) -> bool:
	return _boxes_distance(
		path.bbox,
		(
			min(segment.x0, segment.x1),
			min(segment.y0, segment.y1),
			max(segment.x0, segment.x1),
			max(segment.y0, segment.y1),
		),
	) <= distance


def _segment_touches_box(
	segment: Any,
	box: Tuple[float, float, float, float],
	distance: float,
) -> bool:
	segment_box = (
		min(segment.x0, segment.x1),
		min(segment.y0, segment.y1),
		max(segment.x0, segment.x1),
		max(segment.y0, segment.y1),
	)
	return _boxes_distance(segment_box, box) <= distance


def _segments_connected(left: Any, right: Any, distance: float) -> bool:
	left_points = ((left.x0, left.y0), (left.x1, left.y1))
	right_points = ((right.x0, right.y0), (right.x1, right.y1))
	return any(
		math.hypot(lx - rx, ly - ry) <= distance
		for lx, ly in left_points
		for rx, ry in right_points
	)


def _boxes_distance(
	left: Tuple[float, float, float, float],
	right: Tuple[float, float, float, float],
) -> float:
	x_gap = max(0.0, max(left[0], right[0]) - min(left[2], right[2]))
	y_gap = max(0.0, max(left[1], right[1]) - min(left[3], right[3]))
	return math.hypot(x_gap, y_gap)


def _union_boxes(
	boxes: Sequence[Tuple[float, float, float, float]],
) -> Tuple[float, float, float, float]:
	return (
		min(box[0] for box in boxes),
		min(box[1] for box in boxes),
		max(box[2] for box in boxes),
		max(box[3] for box in boxes),
	)


def _color_distance(
	left: Tuple[float, float, float],
	right: Tuple[float, float, float],
) -> float:
	return max(abs(a - b) for a, b in zip(left, right))


def _line_char_runs(line: Any) -> List[List[Any]]:
	ordered = sorted(line.chars, key=lambda char: (char.x0, char.seq))
	if not ordered:
		return []
	threshold = max(8.0, getattr(line, "size", 0.0) * 1.25)
	runs: List[List[Any]] = [[ordered[0]]]
	for char in ordered[1:]:
		previous = runs[-1][-1]
		if char.x0 - previous.x1 > threshold:
			runs.append([char])
		else:
			runs[-1].append(char)
	return runs


def _source_viewport_box(fill: Any) -> Tuple[float, float, float, float]:
	"""Recover a tightly clipped vector viewport around an inset background."""
	paint = (fill.x0, fill.y0, fill.x1, fill.y1)
	clip = getattr(fill, "clip_bbox", None)
	if not isinstance(clip, tuple) or len(clip) != 4:
		return paint
	if clip[2] <= clip[0] or clip[3] <= clip[1]:
		return paint
	margins = (
		fill.x0 - clip[0],
		fill.y0 - clip[1],
		clip[2] - fill.x1,
		clip[3] - fill.y1,
	)
	if any(margin < -0.05 for margin in margins):
		return paint
	paint_width = max(1.0, fill.x1 - fill.x0)
	paint_height = max(1.0, fill.y1 - fill.y0)
	max_inset = max(2.0, min(paint_width, paint_height) * 0.03)
	if max(margins) > max_inset:
		return paint
	return clip


def _group_adjacent_panels(
	converter: Any,
	figures: List[VectorFigure],
	lines_by_page: Dict[int, List[Any]],
) -> List[VectorFigure]:
	ordered = sorted(figures, key=lambda figure: (figure.page, figure.bbox[1], figure.bbox[0]))
	out: List[VectorFigure] = []
	i = 0
	while i < len(ordered):
		left = ordered[i]
		if i + 1 >= len(ordered):
			out.append(left)
			break
		right = ordered[i + 1]
		if not _shared_panel_caption(converter, left, right, lines_by_page):
			out.append(left)
			i += 1
			continue
		box = (
			min(left.bbox[0], right.bbox[0]),
			min(left.bbox[1], right.bbox[1]),
			max(left.bbox[2], right.bbox[2]),
			max(left.bbox[3], right.bbox[3]),
		)
		lines = [
			line
			for line in lines_by_page.get(left.page, [])
			if box[0] <= (line.x0 + line.x1) / 2 <= box[2]
			and box[1] <= (line.y0 + line.y1) / 2 <= box[3]
		]
		segments = [
			segment
			for segment in converter.segments
			if segment.page == left.page and _segment_inside(segment, box, 3.0)
		]
		if not lines:
			out.append(left)
			i += 1
			continue
		svg = _render_svg(converter, box, lines, segments)
		out.append(
			VectorFigure(
				page=left.page,
				bbox=box,
				name="vector-%s.svg" % hashlib.sha256(svg).hexdigest()[:16],
				data=svg,
				line_ids=tuple(sorted(set(left.line_ids + right.line_ids))),
				seq=min(left.seq, right.seq),
			)
		)
		i += 2
	return out


def _shared_panel_caption(
	converter: Any,
	left: VectorFigure,
	right: VectorFigure,
	lines_by_page: Dict[int, List[Any]],
) -> bool:
	if left.page != right.page or right.bbox[0] < left.bbox[2]:
		return False
	left_height = left.bbox[3] - left.bbox[1]
	right_height = right.bbox[3] - right.bbox[1]
	overlap = min(left.bbox[3], right.bbox[3]) - max(left.bbox[1], right.bbox[1])
	if overlap < min(left_height, right_height) * 0.75:
		return False
	page_width, _page_height = converter.page_sizes.get(left.page, (612.0, 792.0))
	if right.bbox[0] - left.bbox[2] > page_width * 0.08:
		return False
	x0 = left.bbox[0]
	x1 = right.bbox[2]
	y1 = max(left.bbox[3], right.bbox[3])
	center = (x0 + x1) / 2
	for line in lines_by_page.get(left.page, []):
		text = "".join(char.text for char in sorted(line.chars, key=lambda char: (char.x0, char.seq))).strip()
		if not re.match(r"^Figure\s+\d+[:.]\s+\S", text, re.I):
			continue
		if not (0 <= line.y0 - y1 <= max(60.0, max(left_height, right_height) * 0.55)):
			continue
		if abs((line.x0 + line.x1) / 2 - center) <= max(24.0, (x1 - x0) * 0.18):
			return True
	return False


def _render_svg(
	converter: Any,
	box: Tuple[float, float, float, float],
	lines: Sequence[Any],
	segments: Sequence[Any],
) -> bytes:
	x0, y0, x1, y1 = box
	width = max(1.0, x1 - x0)
	height = max(1.0, y1 - y0)
	parts = [
		'<svg xmlns="http://www.w3.org/2000/svg" '
		'width="%.3fpt" height="%.3fpt" '
		'viewBox="0 0 %.3f %.3f" role="img" '
		'aria-label="Vector artwork preserved from PDF graphics and text">'
		% (width, height, width, height),
		"<title>Vector artwork preserved from PDF graphics and text</title>",
	]
	fills = [
		item
		for item in converter.fills
		if item.page == lines[0].page
		and _contains((item.x0, item.y0, item.x1, item.y1), box, 2.0)
	]
	for fill in sorted(fills, key=lambda item: item.seq):
		parts.append(
			'<rect x="%.3f" y="%.3f" width="%.3f" height="%.3f" '
			'fill="%s" />'
			% (
				fill.x0 - x0,
				fill.y0 - y0,
				fill.x1 - fill.x0,
				fill.y1 - fill.y0,
				_color(fill.color),
			)
		)
	for segment in sorted(segments, key=lambda item: item.seq):
		parts.append(
			'<line x1="%.3f" y1="%.3f" x2="%.3f" y2="%.3f" '
			'stroke="%s" stroke-width="%.3f" stroke-linecap="round" />'
			% (
				segment.x0 - x0,
				segment.y0 - y0,
				segment.x1 - x0,
				segment.y1 - y0,
				_color(getattr(segment, "color", (0.0, 0.0, 0.0))),
				max(0.25, segment.width),
			)
		)
	for path in _svg_paths(converter, lines[0].page, box, fills):
		data = _path_data(path.commands, x0, y0)
		if not data:
			continue
		parts.append(
			'<path d="%s" fill="%s" fill-rule="%s" />'
			% (data, _color(path.color), path.fill_rule)
		)
	for line in sorted(lines, key=lambda item: (item.y0, item.x0, item.seq)):
		# Physical-line assembly intentionally joins same-baseline text for prose.
		# A diagram may put independent labels in distant sibling boxes on that
		# same baseline, so preserve each native x-positioned run in the SVG.
		for run in _line_char_runs(line):
			run_chars = sorted(run, key=lambda item: (item.x0, item.seq))
			visible_chars = [char for char in run_chars if char.text.strip()]
			text = "".join(char.text for char in run_chars).strip()
			if not text or not visible_chars:
				continue
			run_line = type(line)(
				list(run_chars),
				line.page,
				min(char.seq for char in run_chars),
				source_order=getattr(line, "source_order", False),
				writing_mode=getattr(line, "writing_mode", "horizontal"),
			)
			family = "monospace" if run_line.mono_ratio >= 0.70 else "sans-serif"
			weight = "700" if run_line.bold_ratio >= 0.60 else "400"
			italic_ratio = (
				sum(1 for char in visible_chars if char.italic)
				/ len(visible_chars)
			)
			font_style = "italic" if italic_ratio >= 0.60 else "normal"
			text_color = visible_chars[0].fill_color
			baseline = run_line.y1 - y0 - run_line.size * 0.20
			parts.append(
				'<text x="%.3f" y="%.3f" font-size="%.3f" '
				'font-family="%s" font-weight="%s" font-style="%s" '
				'fill="%s">%s</text>'
				% (
					visible_chars[0].x0 - x0,
					baseline,
					max(1.0, run_line.size),
					family,
					weight,
					font_style,
					_color(text_color),
					html.escape(text),
				)
			)
	parts.append("</svg>")
	return "\n".join(parts).encode("utf-8")


def _svg_paths(
	converter: Any,
	page: int,
	box: Tuple[float, float, float, float],
	fills: Sequence[Any],
) -> List[Any]:
	paths = [
		path
		for path in converter.painted_paths
		if path.page == page and _contains(path.bbox, box, 2.0)
	]
	simple_paths = [path for path in paths if len(path.commands) <= 12]
	out = []
	for path in sorted(paths, key=lambda item: item.seq):
		if _path_matches_rect_fill(path, fills):
			continue
		# Some producers emit a compact arrowhead and then repeat its painted
		# extent as a hundreds-of-segments compatibility outline. Replaying both
		# darkens or corrupts the SVG; the compact native path is authoritative.
		if (
			len(path.commands) >= 32
			and any(
				simple is not path
				and len(path.commands) >= len(simple.commands) * 4
				and _same_painted_extent(simple, path)
				for simple in simple_paths
			)
		):
			continue
		out.append(path)
	return out


def _same_painted_extent(left: Any, right: Any) -> bool:
	left_center = (
		(left.bbox[0] + left.bbox[2]) / 2.0,
		(left.bbox[1] + left.bbox[3]) / 2.0,
	)
	right_center = (
		(right.bbox[0] + right.bbox[2]) / 2.0,
		(right.bbox[1] + right.bbox[3]) / 2.0,
	)
	return (
		_color_distance(left.color, right.color) <= 0.01
		and math.hypot(
			left_center[0] - right_center[0],
			left_center[1] - right_center[1],
		) <= 2.0
		and _overlap_ratio(left.bbox, right.bbox) >= 0.75
	)


def _path_matches_rect_fill(path: Any, fills: Sequence[Any]) -> bool:
	kinds = [command for command, _values in path.commands]
	if kinds != ["M", "L", "L", "L", "Z"]:
		return False
	return any(
		max(abs(a - b) for a, b in zip(path.bbox, (fill.x0, fill.y0, fill.x1, fill.y1)))
		<= 0.2
		and _color_distance(path.color, fill.color) <= 0.01
		for fill in fills
	)


def _path_data(
	commands: Sequence[Tuple[str, Tuple[float, ...]]],
	x0: float,
	y0: float,
) -> str:
	parts: List[str] = []
	for command, values in commands:
		if command == "Z":
			parts.append("Z")
			continue
		shifted = [
			value - (x0 if index % 2 == 0 else y0)
			for index, value in enumerate(values)
		]
		parts.append(
			"%s %s"
			% (command, " ".join("%.3f" % value for value in shifted))
		)
	return " ".join(parts)


def _segment_inside(
	segment: Any,
	box: Tuple[float, float, float, float],
	padding: float,
) -> bool:
	x0, y0, x1, y1 = box
	return (
		x0 - padding <= min(segment.x0, segment.x1)
		and max(segment.x0, segment.x1) <= x1 + padding
		and y0 - padding <= min(segment.y0, segment.y1)
		and max(segment.y0, segment.y1) <= y1 + padding
	)


def _segment_on_box_edge(
	segment: Any,
	box: Tuple[float, float, float, float],
	tolerance: float,
) -> bool:
	x0, y0, x1, y1 = box
	if segment.horizontal:
		y = (segment.y0 + segment.y1) / 2
		return abs(y - y0) <= tolerance or abs(y - y1) <= tolerance
	if segment.vertical:
		x = (segment.x0 + segment.x1) / 2
		return abs(x - x0) <= tolerance or abs(x - x1) <= tolerance
	return False


def _contains(
	inner: Tuple[float, float, float, float],
	outer: Tuple[float, float, float, float],
	padding: float,
) -> bool:
	ix0, iy0, ix1, iy1 = inner
	ox0, oy0, ox1, oy1 = outer
	return (
		ox0 - padding <= ix0
		and iy0 >= oy0 - padding
		and ix1 <= ox1 + padding
		and iy1 <= oy1 + padding
	)


def _overlap_ratio(
	left: Tuple[float, float, float, float],
	right: Tuple[float, float, float, float],
) -> float:
	x0 = max(left[0], right[0])
	y0 = max(left[1], right[1])
	x1 = min(left[2], right[2])
	y1 = min(left[3], right[3])
	if x1 <= x0 or y1 <= y0:
		return 0.0
	intersection = (x1 - x0) * (y1 - y0)
	left_area = max(1.0, (left[2] - left[0]) * (left[3] - left[1]))
	right_area = max(1.0, (right[2] - right[0]) * (right[3] - right[1]))
	return intersection / min(left_area, right_area)


def _color(rgb: Tuple[float, float, float]) -> str:
	values = [
		max(0, min(255, int(round(component * 255))))
		for component in rgb
	]
	return "#%02x%02x%02x" % tuple(values)
