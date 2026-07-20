from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Sequence, Tuple


@dataclass(frozen=True)
class FormulaFigure:
	page: int
	bbox: Tuple[float, float, float, float]
	name: str
	data: bytes
	alt: str
	seq: int


def detect_formula_figures(
	converter: Any,
	lines_by_page: Dict[int, List[Any]],
	excluded_boxes: Dict[int, List[Tuple[float, float, float, float]]],
) -> List[FormulaFigure]:
	"""Detect outline-only formulas and preserve them as external SVG assets.

	The detector intentionally requires either tagged-PDF formula evidence or a
	nearby math/equation label.  It does not attempt to reverse glyph outlines
	into LaTeX, because visually equivalent formula sources are not invertible.
	"""
	out: List[FormulaFigure] = []
	by_page: Dict[int, List[Any]] = {}
	for path in converter.painted_paths:
		if any(_center_inside(path.bbox, box) for box in excluded_boxes.get(path.page, [])):
			continue
		by_page.setdefault(path.page, []).append(path)

	for page, paths in by_page.items():
		page_width, page_height = converter.page_sizes.get(page, (612.0, 792.0))
		for component in _contiguous_components(paths):
			if not _looks_like_formula_geometry(component, page_width, page_height):
				continue
			raw_box = _union(path.bbox for path in component)
			tagged = any(
				str(tag).lower() == "formula"
				for path in component
				for tag in getattr(path, "tags", ())
			)
			actual = _actual_text(component)
			anchor = _formula_anchor(lines_by_page.get(page, []), raw_box)
			if not tagged and not _actual_text_looks_mathematical(actual) and anchor is None:
				continue
			padding = min(2.0, max(0.75, (raw_box[3] - raw_box[1]) * 0.04))
			box = (
				max(0.0, raw_box[0] - padding),
				max(0.0, raw_box[1] - padding),
				min(page_width, raw_box[2] + padding),
				min(page_height, raw_box[3] + padding),
			)
			alt = actual or "Formula preserved from vector outlines; source text unavailable"
			svg = _render_svg(component, box, alt)
			name = "formula-%s.svg" % hashlib.sha256(svg).hexdigest()[:16]
			out.append(
				FormulaFigure(
					page=page,
					bbox=box,
					name=name,
					data=svg,
					alt=alt,
					seq=min(path.seq for path in component),
				)
			)
	return sorted(out, key=lambda item: (item.page, item.bbox[1], item.bbox[0], item.seq))


def _contiguous_components(paths: Sequence[Any]) -> List[List[Any]]:
	ordered = sorted(paths, key=lambda path: path.seq)
	if not ordered:
		return []
	components: List[List[Any]] = []
	current = [ordered[0]]
	current_box = ordered[0].bbox
	previous_seq = ordered[0].seq
	for path in ordered[1:]:
		if path.seq == previous_seq + 1 and _boxes_near(current_box, path.bbox, 14.0):
			current.append(path)
			current_box = _union((current_box, path.bbox))
		else:
			components.append(current)
			current = [path]
			current_box = path.bbox
		previous_seq = path.seq
	components.append(current)
	return components


def _looks_like_formula_geometry(
	paths: Sequence[Any],
	page_width: float,
	page_height: float,
) -> bool:
	if len(paths) < 3:
		return False
	curve_count = sum(
		1
		for path in paths
		for command, _values in path.commands
		if command == "C"
	)
	if curve_count < 8:
		return False
	x0, y0, x1, y1 = _union(path.bbox for path in paths)
	width = x1 - x0
	height = y1 - y0
	if width < 12.0 or height < 6.0:
		return False
	if width > page_width * 0.85 or height > page_height * 0.30:
		return False
	return width * height <= page_width * page_height * 0.12


def _formula_anchor(
	lines: Sequence[Any],
	box: Tuple[float, float, float, float],
) -> Any:
	candidates = []
	for line in lines:
		text = "".join(
			char.text
			for char in sorted(line.chars, key=lambda char: (char.x0, char.seq))
		).strip()
		if not text or line.y1 > box[1] + 2.0:
			continue
		gap = box[1] - line.y1
		if gap > max(52.0, line.size * 4.5):
			continue
		if not re.search(r"\b(?:formula|equation|math(?:ematical)?)\b", text, re.I):
			continue
		candidates.append((gap, line))
	return min(candidates, key=lambda item: item[0])[1] if candidates else None


def _actual_text(paths: Sequence[Any]) -> str:
	values = []
	seen = set()
	for path in paths:
		for value in getattr(path, "actual_text", ()):
			text = str(value).strip()
			if text and text not in seen:
				seen.add(text)
				values.append(text)
	return " ".join(values)


def _actual_text_looks_mathematical(text: str) -> bool:
	value = text.strip()
	if not value:
		return False
	if value.startswith(("$", r"\(", r"\[", "<math")):
		return True
	strong = "∀∃∈∑∏∫√∞∂∇≤≥≠≈"
	if any(symbol in value for symbol in strong):
		return True
	return sum(value.count(symbol) for symbol in "=+−-×÷*/^") >= 2


def _render_svg(
	paths: Sequence[Any],
	box: Tuple[float, float, float, float],
	alt: str,
) -> bytes:
	x0, y0, x1, y1 = box
	width = max(1.0, x1 - x0)
	height = max(1.0, y1 - y0)
	parts = [
		'<svg xmlns="http://www.w3.org/2000/svg" '
		'width="%.3fpt" height="%.3fpt" viewBox="0 0 %.3f %.3f" '
		'role="img" aria-label="%s">'
		% (width, height, width, height, html.escape(alt, quote=True)),
		"<title>%s</title>" % html.escape(alt),
	]
	for path in sorted(paths, key=lambda item: item.seq):
		d = _path_data(path.commands, x0, y0)
		if not d:
			continue
		parts.append(
			'<path d="%s" fill="%s" fill-rule="%s" />'
			% (d, _color(path.color), path.fill_rule)
		)
	parts.append("</svg>")
	return "\n".join(parts).encode("utf-8")


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
		parts.append("%s %s" % (command, " ".join("%.3f" % value for value in shifted)))
	return " ".join(parts)


def _boxes_near(
	left: Tuple[float, float, float, float],
	right: Tuple[float, float, float, float],
	maximum_gap: float,
) -> bool:
	x_gap = max(0.0, max(left[0], right[0]) - min(left[2], right[2]))
	y_gap = max(0.0, max(left[1], right[1]) - min(left[3], right[3]))
	return x_gap <= maximum_gap and y_gap <= maximum_gap


def _center_inside(
	inner: Tuple[float, float, float, float],
	outer: Tuple[float, float, float, float],
) -> bool:
	cx = (inner[0] + inner[2]) / 2.0
	cy = (inner[1] + inner[3]) / 2.0
	return outer[0] - 2.0 <= cx <= outer[2] + 2.0 and outer[1] - 2.0 <= cy <= outer[3] + 2.0


def _union(
	boxes: Iterable[Tuple[float, float, float, float]],
) -> Tuple[float, float, float, float]:
	items = list(boxes)
	return (
		min(box[0] for box in items),
		min(box[1] for box in items),
		max(box[2] for box in items),
		max(box[3] for box in items),
	)


def _color(rgb: Tuple[float, float, float]) -> str:
	values = tuple(
		max(0, min(255, int(round(component * 255))))
		for component in rgb
	)
	return "#%02x%02x%02x" % values
