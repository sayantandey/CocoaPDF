from __future__ import annotations

import hashlib
import html
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
		source_lines = [
			line
			for line in lines_by_page.get(fill.page, [])
			if any(
				box[0] <= (char.x0 + char.x1) / 2 <= box[2]
				and box[1] <= (char.y0 + char.y1) / 2 <= box[3]
				for char in line.chars
			)
		]
		lines = []
		line_ids: List[int] = []
		for line in source_lines:
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
			else:
				lines.append(line)
				line_ids.append(id(line))
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
		'viewBox="0 0 %.3f %.3f">'
		% (width, height, width, height)
	]
	for fill in sorted(
		(
			item
			for item in converter.fills
			if item.page == lines[0].page
			and _contains((item.x0, item.y0, item.x1, item.y1), box, 2.0)
		),
		key=lambda item: item.seq,
	):
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
	for line in sorted(lines, key=lambda item: (item.y0, item.x0, item.seq)):
		text = "".join(
			char.text
			for char in sorted(line.chars, key=lambda item: (item.x0, item.seq))
		).strip()
		if not text:
			continue
		family = "monospace" if line.mono_ratio >= 0.70 else "sans-serif"
		weight = "700" if line.bold_ratio >= 0.60 else "400"
		baseline = line.y1 - y0 - line.size * 0.20
		parts.append(
			'<text x="%.3f" y="%.3f" font-size="%.3f" '
			'font-family="%s" font-weight="%s" fill="#000000">%s</text>'
			% (
				line.x0 - x0,
				baseline,
				max(1.0, line.size),
				family,
				weight,
				html.escape(text),
			)
		)
	parts.append("</svg>")
	return "\n".join(parts).encode("utf-8")


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
