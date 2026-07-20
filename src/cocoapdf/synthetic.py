from __future__ import annotations

import zlib
from typing import Dict, List, Optional, Sequence, Tuple, Union


def pdf_string(text: str) -> bytes:
	data = text.encode("latin1", "replace")
	data = data.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
	return b"(" + data + b")"


def text_op(x: float, y: float, text: str, font: str = "F1", size: float = 12, render: Optional[int] = None) -> bytes:
	parts = [b"BT", b"/" + font.encode("ascii"), fmt(size), b"Tf", b"1 0 0 1", fmt(x), fmt(y), b"Tm"]
	if render is not None:
		parts += [str(render).encode("ascii"), b"Tr"]
	parts += [pdf_string(text), b"Tj", b"ET"]
	return b" ".join(parts)


def hex_text_op(x: float, y: float, data: bytes, font: str = "F1", size: float = 12) -> bytes:
	return b"BT /%s %s Tf 1 0 0 1 %s %s Tm <%s> Tj ET" % (
		font.encode("ascii"),
		fmt(size),
		fmt(x),
		fmt(y),
		data.hex().encode("ascii"),
	)


def tj_op(x: float, y: float, items: Sequence[object], font: str = "F1", size: float = 12) -> bytes:
	arr = []
	for item in items:
		if isinstance(item, str):
			arr.append(pdf_string(item))
		else:
			arr.append(str(item).encode("ascii"))
	return b"BT /%s %s Tf 1 0 0 1 %s %s Tm [%s] TJ ET" % (
		font.encode("ascii"),
		fmt(size),
		fmt(x),
		fmt(y),
		b" ".join(arr),
	)


def line_op(x0: float, y0: float, x1: float, y1: float, width: float = 1) -> bytes:
	return b"%s w n %s %s m %s %s l S" % (fmt(width), fmt(x0), fmt(y0), fmt(x1), fmt(y1))


def rect_fill_op(x: float, y: float, w: float, h: float, gray: float = 0.94) -> bytes:
	return b"%s g n %s %s %s %s re f 0 g" % (fmt(gray), fmt(x), fmt(y), fmt(w), fmt(h))


def make_pdf(
	page_streams: Sequence[bytes],
	page_size: Tuple[int, int] = (612, 792),
	extra_fonts: Optional[Dict[str, Union[bytes, Tuple[bytes, bytes]]]] = None,
	annots: Optional[Dict[int, List[bytes]]] = None,
	xobjects: Optional[Dict[str, bytes]] = None,
) -> bytes:
	b = Builder()
	font_objs: Dict[str, int] = {
		"F1": b.add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"),
		"F2": b.add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"),
		"F3": b.add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Oblique /Encoding /WinAnsiEncoding >>"),
		"F4": b.add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>"),
	}
	if extra_fonts:
		for name, body in extra_fonts.items():
			if isinstance(body, tuple):
				font_body, cmap_body = body
				cmap_obj = b.add(cmap_body)
				body = font_body.replace(b"TOUNICODE 0 R", b"%d 0 R" % cmap_obj)
			font_objs[name] = b.add(body)
	xobj_objs: Dict[str, int] = {}
	if xobjects:
		for name, body in xobjects.items():
			xobj_objs[name] = b.add(body)

	pages: List[int] = []
	annots = annots or {}
	for idx, stream in enumerate(page_streams, 1):
		content = b.add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
		font_res = b" ".join(b"/%s %d 0 R" % (k.encode("ascii"), v) for k, v in font_objs.items())
		res = b"<< /Font << " + font_res + b" >>"
		if xobj_objs:
			xres = b" ".join(b"/%s %d 0 R" % (k.encode("ascii"), v) for k, v in xobj_objs.items())
			res += b" /XObject << " + xres + b" >>"
		res += b" >>"
		annot_refs = []
		for annot in annots.get(idx, []):
			annot_refs.append(b.add(annot))
		annot_part = b""
		if annot_refs:
			annot_part = b" /Annots [ " + b" ".join(b"%d 0 R" % n for n in annot_refs) + b" ]"
		page = b.add(
			b"<< /Type /Page /Parent PAGES 0 R /MediaBox [0 0 %d %d] /Resources %s /Contents %d 0 R%s >>"
			% (page_size[0], page_size[1], res, content, annot_part)
		)
		pages.append(page)
	kids = b" ".join(b"%d 0 R" % p for p in pages)
	pages_obj = b.add(b"<< /Type /Pages /Kids [ " + kids + b" ] /Count %d >>" % len(pages))
	b.replace(b"PAGES 0 R", b"%d 0 R" % pages_obj)
	catalog = b.add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_obj)
	return b.render(catalog)


def link_annot(x0: float, y0: float, x1: float, y1: float, uri: str) -> bytes:
	return (
		b"<< /Type /Annot /Subtype /Link /Rect [%s %s %s %s] "
		b"/A << /S /URI /URI %s >> >>"
		% (fmt(x0), fmt(y0), fmt(x1), fmt(y1), pdf_string(uri))
	)


def image_xobject_rgb(width: int, height: int, rgb: bytes) -> bytes:
	data = zlib.compress(rgb)
	return (
		b"<< /Type /XObject /Subtype /Image /Width %d /Height %d "
		b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length %d >>\nstream\n"
		% (width, height, len(data))
	) + data + b"\nendstream"


def tounicode_font(mapping: Dict[int, str]) -> Dict[str, bytes]:
	cmap_lines = [
		b"/CIDInit /ProcSet findresource begin",
		b"12 dict begin",
		b"begincmap",
		b"1 begincodespacerange",
		b"<00> <ff>",
		b"endcodespacerange",
		("%d beginbfchar" % len(mapping)).encode("ascii"),
	]
	for code, text in mapping.items():
		cmap_lines.append(b"<%02X> <%s>" % (code, text.encode("utf-16-be").hex().encode("ascii")))
	cmap_lines += [b"endbfchar", b"endcmap", b"CMapName currentdict /CMap defineresource pop", b"end", b"end"]
	cmap = b"\n".join(cmap_lines)
	stream = b"<< /Length %d >>\nstream\n" % len(cmap) + cmap + b"\nendstream"
	return {"TU": b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding /ToUnicode TUCMAP 0 R >>", "TUCMAP": stream}


def fmt(v: float) -> bytes:
	if float(v).is_integer():
		return str(int(v)).encode("ascii")
	return ("%.3f" % v).rstrip("0").rstrip(".").encode("ascii")


class Builder:
	def __init__(self) -> None:
		self.objects: List[bytes] = []

	def add(self, body: bytes) -> int:
		self.objects.append(body)
		return len(self.objects)

	def replace(self, old: bytes, new: bytes) -> None:
		self.objects = [obj.replace(old, new) for obj in self.objects]

	def render(self, root: int) -> bytes:
		out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
		offsets = [0]
		for i, body in enumerate(self.objects, 1):
			offsets.append(len(out))
			out.extend(b"%d 0 obj\n" % i)
			out.extend(body)
			out.extend(b"\nendobj\n")
		xref = len(out)
		out.extend(b"xref\n0 %d\n" % (len(self.objects) + 1))
		out.extend(b"0000000000 65535 f \n")
		for off in offsets[1:]:
			out.extend(b"%010d 00000 n \n" % off)
		out.extend(b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(self.objects) + 1, root, xref))
		return bytes(out)
