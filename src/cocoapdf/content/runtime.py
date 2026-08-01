from __future__ import annotations

from typing import Any, List, Optional, Tuple

from .. import limits


def _glyph_geometry(
	interpreter: Any,
	code: bytes,
	width: float,
	text_matrix: Any = None,
) -> tuple[tuple[float, float, float, float], float, float, float]:
	from .. import core as C

	matrix = text_matrix if text_matrix is not None else C.mat_mul(interpreter.ctm, interpreter.tm)
	word_space = interpreter.word_space if code == b" " else 0.0
	if interpreter.font.vertical:
		cid = int.from_bytes(code, "big") if code else 0
		w1y, v1x, v1y = interpreter.font.vertical_metrics_for_code(cid, width)
		advance_x = 0.0
		advance_y = (w1y / 1000.0) * interpreter.font_size + interpreter.char_space + word_space
		position_x = (v1x / 1000.0) * interpreter.font_size * interpreter.hscale
		position_y = (v1y / 1000.0) * interpreter.font_size + interpreter.rise
		glyph_width = max(0.1, (width / 1000.0) * interpreter.font_size * interpreter.hscale)
		glyph_height = max(0.1, interpreter.font_size)
		local = (
			(position_x - glyph_width / 2.0, position_y - glyph_height * 0.88),
			(position_x + glyph_width / 2.0, position_y - glyph_height * 0.88),
			(position_x + glyph_width / 2.0, position_y + glyph_height * 0.12),
			(position_x - glyph_width / 2.0, position_y + glyph_height * 0.12),
		)
	else:
		advance_x = (
			(width / 1000.0) * interpreter.font_size
			+ interpreter.char_space
			+ word_space
		) * interpreter.hscale
		advance_y = 0.0
		local = (
			(0.0, interpreter.rise - interpreter.font_size * 0.25),
			(advance_x, interpreter.rise - interpreter.font_size * 0.25),
			(advance_x, interpreter.rise + interpreter.font_size * 0.85),
			(0.0, interpreter.rise + interpreter.font_size * 0.85),
		)
	points = [C.apply_mat(matrix, x, y) for x, y in local]
	xs = [point[0] for point in points]
	ys = [interpreter.page_height - point[1] for point in points]
	size = interpreter.font_size * max(
		0.1,
		(matrix[2] ** 2 + matrix[3] ** 2) ** 0.5,
	)
	return (min(xs), min(ys), max(xs), max(ys)), size, advance_x, advance_y


def _advance_text(interpreter: Any, x: float, y: float) -> None:
	from .. import core as C

	interpreter.tm = C.mat_mul(interpreter.tm, C.translate(x, y))


def show_text(interpreter: Any, value: Any) -> None:
	from .. import core as C

	if not isinstance(value, (bytes, bytearray)):
		return
	actual_mark = C.active_actual_text_mark(interpreter.marked_content)
	if actual_mark is not None:
		_show_actual_text(interpreter, bytes(value), actual_mark)
		return

	for code, text, width in interpreter.font.decode(bytes(value)):
		bbox, size, advance_x, advance_y = _glyph_geometry(interpreter, code, width)
		clean_text = C.sanitize_decoded_text(text)
		if clean_text != text:
			interpreter.conv.doc.warn(
				"TEXT_CONTROL_GLYPH_DROPPED",
				"control glyph in decoded text",
				interpreter.page,
			)
			text = clean_text
		if not text:
			_advance_text(interpreter, advance_x, advance_y)
			continue
		if interpreter.page_char_count >= limits.MAX_CHARS_PER_PAGE:
			_warn_limit(interpreter, "char", "character limit")
			_advance_text(interpreter, advance_x, advance_y)
			continue
		invisible = _text_is_invisible(interpreter)
		if interpreter.clip_bbox is not None and not C.rects_intersect(bbox, interpreter.clip_bbox):
			_advance_text(interpreter, advance_x, advance_y)
			continue
		interpreter.conv.seq += 1
		character = C.Char(
			text=text,
			x0=bbox[0],
			y0=bbox[1],
			x1=bbox[2],
			y1=bbox[3],
			size=size,
			font=interpreter.font,
			page=interpreter.page,
			seq=interpreter.conv.seq,
			invisible=invisible,
			rise=interpreter.rise,
			fill_color=interpreter.fill_rgb,
			stroke_color=interpreter.stroke_rgb,
			render_mode=interpreter.render_mode,
			paint_order=interpreter.content_order,
		)
		_attach_marked_content(character, interpreter.marked_content)
		interpreter.conv.chars.append(character)
		interpreter.page_char_count += 1
		if invisible:
			interpreter.invisible_count += 1
		_advance_text(interpreter, advance_x, advance_y)


def _show_actual_text(
	interpreter: Any,
	value: bytes,
	mark: dict[str, Any],
) -> None:
	from .. import core as C

	decoded = interpreter.font.decode(value)
	simulated_tm = interpreter.tm
	bboxes: List[tuple[float, float, float, float]] = []
	sizes: List[float] = []
	total_x = 0.0
	total_y = 0.0
	for code, _decoded_text, width in decoded:
		matrix = C.mat_mul(interpreter.ctm, simulated_tm)
		bbox, size, advance_x, advance_y = _glyph_geometry(interpreter, code, width, matrix)
		bboxes.append(bbox)
		sizes.append(size)
		total_x += advance_x
		total_y += advance_y
		simulated_tm = C.mat_mul(simulated_tm, C.translate(advance_x, advance_y))
	if not mark.get("emitted"):
		text = C.sanitize_decoded_text(str(mark.get("actual_text") or ""))
		if text and interpreter.page_char_count < limits.MAX_CHARS_PER_PAGE:
			if bboxes:
				bbox = (
					min(item[0] for item in bboxes),
					min(item[1] for item in bboxes),
					max(item[2] for item in bboxes),
					max(item[3] for item in bboxes),
				)
				size = max(sizes)
			else:
				matrix = C.mat_mul(interpreter.ctm, interpreter.tm)
				origin = C.apply_mat(matrix, 0, interpreter.rise)
				size = interpreter.font_size
				bbox = (origin[0], interpreter.page_height - origin[1] - size, origin[0] + size, interpreter.page_height - origin[1])
			invisible = _text_is_invisible(interpreter)
			if interpreter.clip_bbox is not None and not C.rects_intersect(bbox, interpreter.clip_bbox):
				mark["emitted"] = True
				_advance_text(interpreter, total_x, total_y)
				return
			interpreter.conv.seq += 1
			character = C.Char(
				text=text,
				x0=bbox[0],
				y0=bbox[1],
				x1=bbox[2],
				y1=bbox[3],
				size=size,
				font=interpreter.font,
				page=interpreter.page,
				seq=interpreter.conv.seq,
				invisible=invisible,
				rise=interpreter.rise,
				fill_color=interpreter.fill_rgb,
				stroke_color=interpreter.stroke_rgb,
				render_mode=interpreter.render_mode,
				paint_order=interpreter.content_order,
			)
			_attach_marked_content(character, interpreter.marked_content)
			interpreter.conv.chars.append(character)
			interpreter.page_char_count += 1
			if invisible:
				interpreter.invisible_count += 1
		elif text:
			_warn_limit(interpreter, "char", "character limit")
		mark["emitted"] = True
	_advance_text(interpreter, total_x, total_y)


def _text_is_invisible(interpreter: Any) -> bool:
	mode = int(interpreter.render_mode)
	if mode in (3, 7):
		return True
	fill_visible = mode in (0, 2, 4, 6) and interpreter.fill_alpha > 0.001
	stroke_visible = mode in (1, 2, 5, 6) and interpreter.stroke_alpha > 0.001
	return not (fill_visible or stroke_visible)


def _attach_marked_content(character: Any, marked_content: List[dict]) -> None:
	refs = []
	artifact = False
	for mark in marked_content:
		tag = str(mark.get("tag") or "").lstrip("/")
		mcid = mark.get("mcid")
		entry = {"tag": tag}
		if isinstance(mcid, int) and not isinstance(mcid, bool):
			entry["mcid"] = mcid
		if mark.get("actual_text") is not None:
			entry["actual_text"] = str(mark.get("actual_text"))
		refs.append(entry)
		artifact = artifact or tag == "Artifact"
	try:
		setattr(character, "mc", tuple(refs))
		setattr(character, "artifact", artifact)
	except (AttributeError, TypeError):
		# Slot-based future IRs must declare these fields explicitly.
		pass


def handle_operator(
	interpreter: Any,
	operator: str,
	operands: List[Any],
) -> bool:
	from .. import core as C

	try:
		if operator == "INLINE_IMAGE_SKIPPED":
			interpreter.conv.doc.warn(
				"INLINE_IMAGE_SKIPPED",
				"inline image not extracted",
				interpreter.page,
			)
			return True
		if operator == "h":
			_close_current_path(interpreter)
			return True
		if operator == "s":
			_close_current_path(interpreter)
			interpreter._stroke_path()
			interpreter._commit_clip()
			interpreter.path = []
			return True
		if operator == "BMC":
			interpreter.marked_content.append(
				{
					"tag": operands[-1] if operands else "",
					"mcid": None,
					"actual_text": None,
					"emitted": True,
				}
			)
			return True
		if operator == "BDC":
			tag = operands[-2] if len(operands) >= 2 else ""
			properties: Any = operands[-1] if operands else {}
			if isinstance(properties, str):
				properties = interpreter.properties.get(properties, {})
			properties = interpreter.conv.doc.resolve(properties)
			if not isinstance(properties, dict):
				properties = {}
			interpreter.marked_content.append(
				{
					"tag": tag,
					"mcid": properties.get("MCID") if isinstance(properties.get("MCID"), int) else None,
					"actual_text": C.actual_text_from_props(properties),
					"emitted": False,
				}
			)
			return True
		if operator == "EMC":
			if interpreter.marked_content:
				interpreter.marked_content.pop()
			return True
		if operator in ("k", "K") and len(operands) >= 4:
			rgb = cmyk_to_rgb(
				float(operands[-4]),
				float(operands[-3]),
				float(operands[-2]),
				float(operands[-1]),
			)
			if operator == "k":
				interpreter.fill_rgb = rgb
			else:
				interpreter.stroke_rgb = rgb
			return True
		if operator in ("cs", "CS") and operands:
			if operator == "cs":
				interpreter.fill_cs = str(operands[-1])
			else:
				interpreter.stroke_cs = str(operands[-1])
			return True
		if operator in ("sc", "scn", "SC", "SCN"):
			if any(isinstance(value, str) for value in operands):
				interpreter.conv.doc.warn(
					"COLORSPACE_UNSUPPORTED",
					"pattern/separation operator %s" % operator,
					interpreter.page,
				)
				return True
			values = [
				float(value)
				for value in operands
				if isinstance(value, (int, float))
			]
			if len(values) == 1:
				rgb = (values[0], values[0], values[0])
			elif len(values) == 3:
				rgb = tuple(values[:3])
			elif len(values) == 4:
				rgb = cmyk_to_rgb(*values)
			else:
				interpreter.conv.doc.warn(
					"COLORSPACE_UNSUPPORTED",
					"operator %s" % operator,
					interpreter.page,
				)
				return True
			rgb = tuple(max(0.0, min(1.0, component)) for component in rgb)
			if operator[0].islower():
				interpreter.fill_rgb = rgb
			else:
				interpreter.stroke_rgb = rgb
			return True
		if operator in ("B", "B*", "b", "b*"):
			if operator in ("b", "b*"):
				_close_current_path(interpreter)
			interpreter._record_filled_path(
				"evenodd" if operator.endswith("*") else "nonzero"
			)
			interpreter._fill_path()
			interpreter._stroke_path()
			interpreter._commit_clip()
			interpreter.path = []
			return True
	except Exception as exc:
		interpreter.conv.doc.warn(
			"CONTENT_OP_FAILED",
			"%s: %s" % (operator, exc),
			interpreter.page,
		)
		return True
	return False


def _warn_limit(interpreter: Any, key: str, detail: str) -> None:
	if key in interpreter._limit_warnings:
		return
	interpreter._limit_warnings.add(key)
	interpreter.conv.doc.warn("PAGE_TRUNCATED", detail, interpreter.page)


def _close_current_path(interpreter: Any) -> None:
	start: Optional[Tuple[float, float]] = None
	for kind, values in reversed(interpreter.path):
		if kind == "m":
			start = (values[0], values[1])
			break
	if start is not None:
		interpreter.path.append(("l", start))


def cmyk_to_rgb(
	cyan: float,
	magenta: float,
	yellow: float,
	black: float,
) -> Tuple[float, float, float]:
	cyan = max(0.0, min(1.0, cyan))
	magenta = max(0.0, min(1.0, magenta))
	yellow = max(0.0, min(1.0, yellow))
	black = max(0.0, min(1.0, black))
	return (
		1.0 - min(1.0, cyan + black),
		1.0 - min(1.0, magenta + black),
		1.0 - min(1.0, yellow + black),
	)
