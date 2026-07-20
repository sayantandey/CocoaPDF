from __future__ import annotations

from typing import Any

from .. import limits


def execute_form_xobject(interpreter: Any, name: str, xobject: Any) -> bool:
	from .. import core as C

	subtype = str(interpreter.conv.doc.resolve(xobject.attrs.get("Subtype")))
	if subtype != "Form":
		return False
	depth = interpreter.depth
	form_key = xobject.objnum if xobject.objnum is not None else id(xobject)
	if form_key in interpreter.form_stack:
		interpreter.conv.doc.warn("FORM_XOBJECT_CYCLE", name, interpreter.page)
		return True
	if depth >= limits.MAX_FORM_DEPTH:
		interpreter.conv.doc.warn("FORM_XOBJECT_DEPTH", name, interpreter.page)
		return True
	data = interpreter.conv.doc.decoded_stream(xobject)
	resources = interpreter.conv.doc.resolve(xobject.attrs.get("Resources")) or {}
	fonts = interpreter.fonts.copy()
	xobjects = interpreter.xobjects.copy()
	extgstates = interpreter.extgstates.copy()
	properties = interpreter.properties.copy()
	if isinstance(resources, dict):
		fonts.update(interpreter.conv._load_fonts(resources))
		xobjects.update(interpreter.conv._load_xobjects(resources))
		extgstates.update(interpreter.conv._load_named_resource_dict(resources, "ExtGState"))
		properties.update(interpreter.conv._load_named_resource_dict(resources, "Properties"))
	child = C.ContentInterpreter(
		interpreter.conv,
		interpreter.page,
		interpreter.page_height,
		fonts,
		xobjects,
		extgstates=extgstates,
		properties=properties,
	)
	child.depth = depth + 1
	child.form_stack = interpreter.form_stack + (form_key,)
	child.ctm = interpreter.ctm
	matrix = interpreter.conv.doc.resolve(xobject.attrs.get("Matrix"))
	if isinstance(matrix, list) and len(matrix) >= 6:
		child.ctm = C.mat_mul(
			child.ctm,
			tuple(float(value) for value in matrix[:6]),
		)
	child.fill_rgb = interpreter.fill_rgb
	child.stroke_rgb = interpreter.stroke_rgb
	child.fill_cs = interpreter.fill_cs
	child.stroke_cs = interpreter.stroke_cs
	child.fill_alpha = interpreter.fill_alpha
	child.stroke_alpha = interpreter.stroke_alpha
	child.clip_bbox = interpreter.clip_bbox
	child.marked_content = [dict(mark) for mark in interpreter.marked_content]
	child.line_width = interpreter.line_width
	bbox = interpreter.conv.doc.resolve(xobject.attrs.get("BBox"))
	if isinstance(bbox, list) and len(bbox) >= 4:
		values = [interpreter.conv.doc.resolve_number(value) for value in bbox[:4]]
		if all(value is not None for value in values):
			x0, y0, x1, y1 = [float(value) for value in values]
			points = [
				C.apply_mat(child.ctm, x, y)
				for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
			]
			xs = [point[0] for point in points]
			ys = [child.page_height - point[1] for point in points]
			child.clip_bbox = C.intersect_rects(
				child.clip_bbox,
				(min(xs), min(ys), max(xs), max(ys)),
			)
	child.run(data)
	return True
