from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import unicodedata
import zlib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from . import limits
from ._textio import write_utf8_lf
from ._version import __version__
from .html.sanitize import is_unsafe_href, safe_href

PdfObj = Any


class PdfName(str):
	pass


@dataclass(frozen=True)
class Ref:
	num: int
	gen: int = 0


@dataclass
class Stream:
	attrs: Dict[str, PdfObj]
	raw: bytes
	objnum: Optional[int] = None
	decoded_cache: Optional[bytes] = None


@dataclass(frozen=True)
class XrefEntry:
	obj_type: int
	field1: int
	field2: int


@dataclass
class Warning:
	code: str
	page: Optional[int] = None
	detail: str = ""


@dataclass
class ConvertOptions:
	assets_dir: str = "assets"
	asset_reference_dir: Optional[str] = None
	html_underline: bool = True
	report_path: Optional[str] = None
	keep_invisible: bool = False
	page_breaks: bool = False
	output_format: str = "md"
	pages: Optional[str] = None
	image_mode: str = "reference"
	image_markup: str = "auto"


@dataclass
class ConvertResult:
	markdown: str
	html: str = ""
	assets: Dict[str, bytes] = field(default_factory=dict)
	warnings: List[Warning] = field(default_factory=list)
	report: Dict[str, Any] = field(default_factory=dict)
	semantic: Any = None


@dataclass
class Font:
	name: str
	base_font: str = ""
	subtype: str = ""
	encoding: str = "WinAnsiEncoding"
	to_unicode: Dict[bytes, str] = field(default_factory=dict)
	widths: Dict[int, float] = field(default_factory=dict)
	first_char: int = 0
	default_width: float = 500.0
	descendant_widths: Dict[int, float] = field(default_factory=dict)
	composite: bool = False
	dw: Optional[float] = None
	differences: Dict[int, str] = field(default_factory=dict)
	vertical: bool = False
	dw2: Tuple[float, float] = (880.0, -1000.0)
	vertical_metrics: Dict[int, Tuple[float, float, float]] = field(default_factory=dict)

	def decode(self, data: bytes) -> List[Tuple[bytes, str, float]]:
		from .fonts.decoding import decode_font

		return decode_font(self, data)

	def width_for_code(self, code: int) -> float:
		if self.composite:
			return self.descendant_widths.get(code, self.dw if self.dw is not None else 1000.0)
		if code in self.widths:
			return self.widths[code]
		ch = winansi_char(code, self.base_font)
		return standard_width(self.base_font, ch, self.default_width)

	def vertical_metrics_for_code(self, code: int, width: float) -> Tuple[float, float, float]:
		if code in self.vertical_metrics:
			return self.vertical_metrics[code]
		v1y, w1y = self.dw2
		return (w1y, width / 2.0, v1y)

	@property
	def bold(self) -> bool:
		return bool(re.search(r"bold|black|heavy", strip_subset(self.base_font), re.I))

	@property
	def italic(self) -> bool:
		return bool(re.search(r"italic|oblique", strip_subset(self.base_font), re.I))

	@property
	def mono(self) -> bool:
		return bool(re.search(r"courier|mono|consolas|console|menlo|code", strip_subset(self.base_font), re.I))


@dataclass
class Char:
	text: str
	x0: float
	y0: float
	x1: float
	y1: float
	size: float
	font: Font
	page: int
	seq: int
	invisible: bool = False
	synthetic_bold: bool = False
	underline: bool = False
	strike: bool = False
	highlight: bool = False
	rise: float = 0.0
	link: Optional[str] = None
	link_object_ref: Optional[str] = None
	fill_color: Tuple[float, float, float] = (0.0, 0.0, 0.0)
	stroke_color: Tuple[float, float, float] = (0.0, 0.0, 0.0)
	render_mode: int = 0
	mc: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
	artifact: bool = False

	@property
	def bold(self) -> bool:
		return self.synthetic_bold or self.font.bold

	@property
	def italic(self) -> bool:
		return self.font.italic

	@property
	def mono(self) -> bool:
		return self.font.mono


@dataclass
class Segment:
	x0: float
	y0: float
	x1: float
	y1: float
	width: float
	page: int
	seq: int
	fill: bool = False
	color: Tuple[float, float, float] = (0.0, 0.0, 0.0)

	@property
	def horizontal(self) -> bool:
		return abs(self.y0 - self.y1) <= 1.5

	@property
	def vertical(self) -> bool:
		return abs(self.x0 - self.x1) <= 1.5

	@property
	def length(self) -> float:
		return math.hypot(self.x1 - self.x0, self.y1 - self.y0)


@dataclass
class Fill:
	x0: float
	y0: float
	x1: float
	y1: float
	color: Tuple[float, float, float]
	page: int
	seq: int
	clip_bbox: Optional[Tuple[float, float, float, float]] = None


@dataclass(frozen=True)
class PaintedPath:
	"""A filled PDF path retained for loss-aware vector fallbacks."""

	page: int
	seq: int
	commands: Tuple[Tuple[str, Tuple[float, ...]], ...]
	bbox: Tuple[float, float, float, float]
	color: Tuple[float, float, float]
	fill_rule: str = "nonzero"
	tags: Tuple[str, ...] = field(default_factory=tuple)
	actual_text: Tuple[str, ...] = field(default_factory=tuple)


@dataclass
class ImageItem:
	x0: float
	y0: float
	x1: float
	y1: float
	page: int
	seq: int
	name: str
	data: bytes
	alt: str = ""
	intrinsic_width: int = 0
	intrinsic_height: int = 0
	placed_width: float = 0.0
	placed_height: float = 0.0
	quad: Tuple[Tuple[float, float], ...] = field(default_factory=tuple)
	link: Optional[str] = None
	kind: str = "raster"
	mcids: Tuple[int, ...] = field(default_factory=tuple)
	tags: Tuple[str, ...] = field(default_factory=tuple)
	object_ref: Optional[str] = None
	link_object_ref: Optional[str] = None


@dataclass
class LinkItem:
	rect: Tuple[float, float, float, float]
	uri: Optional[str]
	dest: Optional[str]
	page: int
	object_ref: Optional[str] = None


@dataclass(frozen=True)
class AnchorItem:
	name: str
	page: int
	y: float


@dataclass(frozen=True)
class VisualListMarker:
	x: float
	kind: str


@dataclass
class Line:
	chars: List[Char]
	page: int
	seq: int
	source_order: bool = False
	writing_mode: str = "horizontal"
	_text_tokens_cache: Optional[Tuple[Dict[str, Any], ...]] = field(
		default=None,
		init=False,
		repr=False,
		compare=False,
	)
	_size_cache: Optional[float] = field(default=None, init=False, repr=False, compare=False)
	_bold_ratio_cache: Optional[float] = field(default=None, init=False, repr=False, compare=False)
	_mono_ratio_cache: Optional[float] = field(default=None, init=False, repr=False, compare=False)

	def invalidate_caches(self) -> None:
		"""Discard values derived from ``chars`` after a line is mutated."""
		self._text_tokens_cache = None
		self._size_cache = None
		self._bold_ratio_cache = None
		self._mono_ratio_cache = None

	@property
	def x0(self) -> float:
		return min((c.x0 for c in self.chars), default=0.0)

	@property
	def x1(self) -> float:
		return max((c.x1 for c in self.chars), default=0.0)

	@property
	def y0(self) -> float:
		return min((c.y0 for c in self.chars), default=0.0)

	@property
	def y1(self) -> float:
		return max((c.y1 for c in self.chars), default=0.0)

	@property
	def size(self) -> float:
		if self._size_cache is None:
			vals = [c.size for c in self.chars if c.text.strip()]
			self._size_cache = median(vals) if vals else 0.0
		return self._size_cache

	@property
	def bold_ratio(self) -> float:
		if self._bold_ratio_cache is None:
			vals = [c for c in self.chars if c.text.strip()]
			self._bold_ratio_cache = (
				sum(1 for c in vals if c.bold) / len(vals)
				if vals
				else 0.0
			)
		return self._bold_ratio_cache

	@property
	def mono_ratio(self) -> float:
		if self._mono_ratio_cache is None:
			vals = [c for c in self.chars if c.text.strip()]
			self._mono_ratio_cache = (
				sum(1 for c in vals if c.mono) / len(vals)
				if vals
				else 0.0
			)
		return self._mono_ratio_cache


@dataclass
class BlockEvent:
	page: int
	rank: float
	kind: str
	lines: List[Line] = field(default_factory=list)
	attrs: Dict[str, Any] = field(default_factory=dict)
	legacy_markdown: str = ""


class PdfSyntaxError(Exception):
	pass


class PdfLexer:
	WHITESPACE = b"\x00\t\n\f\r "
	DELIMS = b"()<>[]{}/%"

	def __init__(self, data: bytes, pos: int = 0):
		self.data = data
		self.pos = pos

	def skip_ws(self) -> None:
		n = len(self.data)
		while self.pos < n:
			b = self.data[self.pos]
			if b in self.WHITESPACE:
				self.pos += 1
				continue
			if b == ord("%"):
				while self.pos < n and self.data[self.pos] not in b"\r\n":
					self.pos += 1
				continue
			break

	def peek_token(self) -> Optional[Tuple[str, Any, int, int]]:
		old = self.pos
		tok = self.next_token()
		self.pos = old
		return tok

	def next_token(self) -> Optional[Tuple[str, Any, int, int]]:
		self.skip_ws()
		if self.pos >= len(self.data):
			return None
		start = self.pos
		b = self.data[self.pos]
		if self.data.startswith(b"<<", self.pos):
			self.pos += 2
			return ("DICT_OPEN", "<<", start, self.pos)
		if self.data.startswith(b">>", self.pos):
			self.pos += 2
			return ("DICT_CLOSE", ">>", start, self.pos)
		if b == ord("["):
			self.pos += 1
			return ("ARR_OPEN", "[", start, self.pos)
		if b == ord("]"):
			self.pos += 1
			return ("ARR_CLOSE", "]", start, self.pos)
		if b == ord("/"):
			self.pos += 1
			raw = bytearray()
			while self.pos < len(self.data):
				c = self.data[self.pos]
				if c in self.WHITESPACE or c in self.DELIMS:
					break
				raw.append(c)
				self.pos += 1
			return ("NAME", PdfName(decode_name(bytes(raw))), start, self.pos)
		if b == ord("("):
			return ("STRING", self._literal_string(), start, self.pos)
		if b == ord("<"):
			return ("HEXSTRING", self._hex_string(), start, self.pos)

		end = self.pos
		while end < len(self.data):
			c = self.data[end]
			if c in self.WHITESPACE or c in self.DELIMS:
				break
			end += 1
		raw = self.data[self.pos : end]
		self.pos = end
		if re.match(rb"^[+-]?(?:\d+\.\d*|\.\d+|\d+)$", raw):
			if b"." in raw:
				return ("REAL", float(raw), start, self.pos)
			return ("INT", int(raw), start, self.pos)
		return ("KEYWORD", raw.decode("latin1", "replace"), start, self.pos)

	def _literal_string(self) -> bytes:
		assert self.data[self.pos] == ord("(")
		self.pos += 1
		out = bytearray()
		depth = 1
		n = len(self.data)
		while self.pos < n and depth:
			b = self.data[self.pos]
			self.pos += 1
			if b == ord("\\"):
				if self.pos >= n:
					break
				c = self.data[self.pos]
				self.pos += 1
				if c in b"nrtbf":
					out.append({ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}[c])
				elif c in b"()\\":
					out.append(c)
				elif c in b"\r\n":
					if c == ord("\r") and self.pos < n and self.data[self.pos] == ord("\n"):
						self.pos += 1
				elif ord("0") <= c <= ord("7"):
					octal = bytes([c])
					for _ in range(2):
						if self.pos < n and ord("0") <= self.data[self.pos] <= ord("7"):
							octal += bytes([self.data[self.pos]])
							self.pos += 1
						else:
							break
					out.append(int(octal, 8) & 0xFF)
				else:
					out.append(c)
				continue
			if b == ord("("):
				depth += 1
				out.append(b)
				continue
			if b == ord(")"):
				depth -= 1
				if depth:
					out.append(b)
				continue
			if b == ord("\r"):
				if self.pos < n and self.data[self.pos] == ord("\n"):
					self.pos += 1
				out.append(10)
			else:
				out.append(b)
		return bytes(out)

	def _hex_string(self) -> bytes:
		assert self.data[self.pos] == ord("<")
		self.pos += 1
		raw = bytearray()
		while self.pos < len(self.data):
			b = self.data[self.pos]
			self.pos += 1
			if b == ord(">"):
				break
			if b in self.WHITESPACE:
				continue
			raw.append(b)
		if len(raw) % 2:
			raw.append(ord("0"))
		try:
			return bytes.fromhex(raw.decode("ascii"))
		except ValueError:
			return b""


class ObjectParser:
	def __init__(self, data: bytes):
		self.lexer = PdfLexer(data)

	@property
	def pos(self) -> int:
		return self.lexer.pos

	def parse_value(self) -> PdfObj:
		tok = self.lexer.next_token()
		if tok is None:
			raise PdfSyntaxError("unexpected EOF")
		kind, value, _start, _end = tok
		if kind == "DICT_OPEN":
			d: Dict[str, PdfObj] = {}
			while True:
				nxt = self.lexer.peek_token()
				if nxt is None:
					break
				if nxt[0] == "DICT_CLOSE":
					self.lexer.next_token()
					break
				key_tok = self.lexer.next_token()
				if key_tok is None or key_tok[0] != "NAME":
					raise PdfSyntaxError("expected dict key")
				d[str(key_tok[1])] = self.parse_value()
			return d
		if kind == "ARR_OPEN":
			arr: List[PdfObj] = []
			while True:
				nxt = self.lexer.peek_token()
				if nxt is None:
					break
				if nxt[0] == "ARR_CLOSE":
					self.lexer.next_token()
					break
				arr.append(self.parse_value())
			return arr
		if kind == "INT":
			old = self.lexer.pos
			t2 = self.lexer.next_token()
			t3 = self.lexer.next_token()
			if t2 and t3 and t2[0] == "INT" and t3[0] == "KEYWORD" and t3[1] == "R":
				return Ref(value, int(t2[1]))
			self.lexer.pos = old
			return value
		if kind == "REAL":
			return value
		if kind == "NAME":
			return value
		if kind in ("STRING", "HEXSTRING"):
			return value
		if kind == "KEYWORD":
			if value == "true":
				return True
			if value == "false":
				return False
			if value == "null":
				return None
			return value
		raise PdfSyntaxError("unexpected token %s" % (kind,))


class PdfDocument:
	def __init__(self, data: bytes):
		self.data = data
		self.objects: Dict[Tuple[int, int], PdfObj] = {}
		self.xref_entries: Dict[Tuple[int, int], XrefEntry] = {}
		self.trailer: Dict[str, PdfObj] = {}
		self.warnings: List[Warning] = []
		self.parse_mode = "unparsed"
		self.encrypted = False
		self.encryption: Dict[str, Any] = {}
		self.active_content: List[Dict[str, Any]] = []
		self.total_decoded = 0
		self._named_destinations_cache: Optional[Dict[str, PdfObj]] = None
		self._loading_objects: set[Tuple[int, int]] = set()
		if self._parse_objects_by_xref():
			self.parse_mode = "xref"
		else:
			self.objects.clear()
			self.xref_entries.clear()
			self.trailer = {}
			self._parse_objects_by_scan()
			self.parse_mode = "recovered"
		self._detect_encryption()
		if not self.encrypted:
			self._unpack_object_streams()
		self._scan_active_content()

	def warn(self, code: str, detail: str = "", page: Optional[int] = None) -> None:
		self.warnings.append(Warning(code, page, detail))

	def resolve(self, obj: PdfObj, depth: int = 0) -> PdfObj:
		if depth > 32:
			self.warn("REF_CYCLE", "reference depth exceeded")
			return None
		if isinstance(obj, Ref):
			key = (obj.num, obj.gen) if (obj.num, obj.gen) in self.objects else (obj.num, 0)
			if key not in self.objects:
				self._load_xref_object(obj.num, obj.gen)
				key = (obj.num, obj.gen) if (obj.num, obj.gen) in self.objects else (obj.num, 0)
			if key not in self.objects:
				return None
			return self.resolve(self.objects[key], depth + 1)
		return obj

	def _xref_key(self, object_number: int, generation: int = 0) -> Optional[Tuple[int, int]]:
		direct = (object_number, generation)
		if direct in self.xref_entries:
			return direct
		for key in self.xref_entries:
			if key[0] == object_number:
				return key
		return None

	def _load_xref_object(self, object_number: int, generation: int = 0) -> Optional[PdfObj]:
		key = self._xref_key(object_number, generation)
		if key is None:
			return None
		if key in self.objects:
			return self.objects[key]
		if key in self._loading_objects:
			self.warn("REF_CYCLE", "object %d %d" % key)
			return None
		entry = self.xref_entries.get(key)
		if entry is None or entry.obj_type != 1:
			return None
		self._loading_objects.add(key)
		try:
			num, gen, value = self._parse_indirect_at_offset(entry.field1)
			if (num, gen) != key:
				self.warn(
					"XREF_OBJECT_MISMATCH",
					"xref %d %d points to %d %d" % (key[0], key[1], num, gen),
				)
				return None
			self.objects[key] = value
			return value
		except Exception as exc:
			self.warn("XREF_OBJECT_PARSE_FAILED", "object %d: %s" % (object_number, exc))
			return None
		finally:
			self._loading_objects.discard(key)


	def resolve_number(self, obj: PdfObj, default: Optional[float] = None) -> Optional[float]:
		value = self.resolve(obj)
		if isinstance(value, bool) or not isinstance(value, (int, float)):
			return default
		value = float(value)
		return value if math.isfinite(value) else default

	def resolve_array(self, obj: PdfObj) -> List[PdfObj]:
		value = self.resolve(obj)
		return list(value) if isinstance(value, list) else []

	def _resolve_decode_parms(self, obj: PdfObj) -> PdfObj:
		from .cos.filters import resolve_decode_parms

		return resolve_decode_parms(self, obj)

	def _detect_encryption(self) -> None:
		encrypt = self.trailer.get("Encrypt") if isinstance(self.trailer, dict) else None
		if encrypt is None and self.parse_mode == "recovered":
			for match in reversed(list(re.finditer(rb"\btrailer\b", self.data))):
				try:
					trailer = ObjectParser(self.data[match.end() :]).parse_value()
				except Exception:
					continue
				if isinstance(trailer, dict) and trailer.get("Encrypt") is not None:
					encrypt = trailer.get("Encrypt")
					break
		if encrypt is None:
			return
		details = self.resolve(encrypt)
		if isinstance(details, dict):
			self.encryption = {
				key: self.resolve(details.get(key))
				for key in ("Filter", "SubFilter", "V", "R", "Length")
				if details.get(key) is not None
			}
		self.encrypted = True
		self.warn("ENCRYPTED_UNSUPPORTED", json.dumps(self.encryption, default=str, sort_keys=True))

	def _scan_active_content(self) -> None:
		catalog = self.catalog()
		if not isinstance(catalog, dict):
			return

		def finding(code: str, path: str) -> None:
			item = {"code": code, "path": path}
			self.active_content.append(item)
			self.warn(code, path)

		for key in ("OpenAction", "AA"):
			if catalog.get(key) is not None:
				finding("SECURITY_ACTIVE_CONTENT", "Catalog/%s" % key)
		if catalog.get("AcroForm") is not None:
			finding("FORMS_PRESENT_UNPROCESSED", "Catalog/AcroForm")
		names = self.resolve(catalog.get("Names"))
		if isinstance(names, dict):
			if names.get("JavaScript") is not None:
				finding("SECURITY_ACTIVE_CONTENT", "Catalog/Names/JavaScript")
			if names.get("EmbeddedFiles") is not None:
				finding("SECURITY_EMBEDDED_FILE", "Catalog/Names/EmbeddedFiles")


	def decoded_stream(self, stream: Stream) -> bytes:
		from .cos.filters import decode_stream

		return decode_stream(self, stream)

	def catalog(self) -> Optional[Dict[str, PdfObj]]:
		root = self.resolve(self.trailer.get("Root")) if self.trailer else None
		if isinstance(root, dict):
			return root
		for obj in self.objects.values():
			d = obj.attrs if isinstance(obj, Stream) else obj
			if isinstance(d, dict) and str(d.get("Type")) == "Catalog":
				return d
		return None

	def named_destinations(self) -> Dict[str, PdfObj]:
		if self._named_destinations_cache is not None:
			return dict(self._named_destinations_cache)

		out: Dict[str, PdfObj] = {}
		catalog = self.catalog()
		if not isinstance(catalog, dict):
			self._named_destinations_cache = out
			return {}

		legacy = self.resolve(catalog.get("Dests"))
		if isinstance(legacy, dict):
			for key, value in legacy.items():
				out[str(key)] = value

		names = self.resolve(catalog.get("Names"))
		root = self.resolve(names.get("Dests")) if isinstance(names, dict) else None
		seen_refs: set[Tuple[int, int]] = set()
		seen_direct: set[int] = set()

		def walk(raw: PdfObj, depth: int = 0) -> None:
			if depth > 64:
				self.warn("DEST_NAME_TREE_DEPTH", "named destination tree depth exceeded")
				return
			if isinstance(raw, Ref):
				key = (raw.num, raw.gen)
				if key in seen_refs:
					self.warn("DEST_NAME_TREE_CYCLE", "%d %d R" % key)
					return
				seen_refs.add(key)
			node = self.resolve(raw)
			if not isinstance(node, dict):
				return
			if not isinstance(raw, Ref):
				node_id = id(node)
				if node_id in seen_direct:
					self.warn("DEST_NAME_TREE_CYCLE", "direct destination name tree")
					return
				seen_direct.add(node_id)

			values = self.resolve(node.get("Names"))
			if isinstance(values, list):
				for index in range(0, len(values) - 1, 2):
					key_obj = self.resolve(values[index])
					if isinstance(key_obj, bytes):
						name = decode_pdf_text(key_obj)
					elif isinstance(key_obj, (PdfName, str)):
						name = str(key_obj)
					else:
						continue
					if name:
						out[name] = values[index + 1]

			kids = self.resolve(node.get("Kids"))
			if isinstance(kids, list):
				for kid in kids:
					walk(kid, depth + 1)

		if root is not None:
			walk(root)

		self._named_destinations_cache = dict(out)
		return out

	def pages(self) -> List[Dict[str, PdfObj]]:
		cat = self.catalog()
		if not cat:
			return []
		out: List[Dict[str, PdfObj]] = []
		stack: List[Tuple[PdfObj, Dict[str, PdfObj], int]] = [(cat.get("Pages"), {}, 0)]
		seen_refs: set[Tuple[int, int]] = set()
		seen_direct: set[int] = set()
		while stack:
			raw_node, inherited, depth = stack.pop()
			if depth > limits.MAX_PAGE_TREE_DEPTH:
				self.warn("PAGE_TREE_DEPTH", "depth %d" % depth)
				continue
			if isinstance(raw_node, Ref):
				ref_key = (raw_node.num, raw_node.gen)
				if ref_key in seen_refs:
					self.warn("PAGE_TREE_CYCLE", "%d %d R" % ref_key)
					continue
				seen_refs.add(ref_key)
			node = self.resolve(raw_node)
			if not isinstance(node, dict):
				continue
			direct_key = id(node)
			if not isinstance(raw_node, Ref):
				if direct_key in seen_direct:
					self.warn("PAGE_TREE_CYCLE", "direct page-tree dictionary")
					continue
				seen_direct.add(direct_key)
			cur = dict(inherited)
			for k in ("Resources", "MediaBox", "CropBox", "Rotate", "UserUnit"):
				if k in node:
					cur[k] = node[k]
			typ = str(self.resolve(node.get("Type")))
			if typ == "Page":
				page = dict(node)
				if isinstance(raw_node, Ref):
					page["__page_ref__"] = raw_node
				for k, v in cur.items():
					page.setdefault(k, v)
				out.append(page)
				if len(out) >= limits.MAX_PAGES:
					self.warn("PAGE_LIMIT", "maximum page count reached")
					break
			else:
				kids = self.resolve(node.get("Kids")) or []
				if isinstance(kids, list):
					for kid in reversed(kids):
						stack.append((kid, cur, depth + 1))
		return out

	def _parse_objects_by_xref(self) -> bool:
		start = self._find_startxref()
		if start is None:
			self.warn("XREF_MISSING", "startxref not found")
			return False
		try:
			entries, trailer = self._walk_xrefs(start)
		except Exception as exc:
			self.warn("XREF_PARSE_FAILED", str(exc))
			return False
		if not entries:
			self.warn("XREF_EMPTY", "no usable xref entries")
			return False
		self.xref_entries = entries
		self.trailer = trailer
		loaded = 0
		for (expected_num, expected_gen), entry in sorted(entries.items()):
			if entry.obj_type != 1 or expected_num == 0:
				continue
			if loaded >= limits.MAX_OBJECTS:
				self.warn("OBJECT_LIMIT", "maximum object count reached")
				break
			if self._load_xref_object(expected_num, expected_gen) is not None:
				loaded += 1
		if not loaded:
			self.warn("XREF_NO_OBJECTS", "xref did not load any objects")
			return False
		if not self.trailer.get("Root"):
			self.warn("XREF_NO_ROOT", "xref trailer lacks Root")
			return False
		return True

	def _find_startxref(self) -> Optional[int]:
		found = None
		for m in re.finditer(rb"startxref\s+(\d+)", self.data):
			found = int(m.group(1))
		if found is None or found < 0 or found >= len(self.data):
			return None
		return found

	def _walk_xrefs(self, start: int) -> Tuple[Dict[Tuple[int, int], XrefEntry], Dict[str, PdfObj]]:
		from .cos.xref import walk_xrefs

		return walk_xrefs(self, start)

	def _read_stream_raw(
		self,
		pos: int,
		length: Optional[int],
		num: int,
		data: bytes,
	) -> bytes:
		from .cos.streams import read_stream_raw

		return read_stream_raw(self, pos, length, num, data)

	def _parse_xref_at(self, offset: int) -> Tuple[Dict[Tuple[int, int], XrefEntry], Dict[str, PdfObj]]:
		pos = self._skip_ws(offset)
		if self.data.startswith(b"xref", pos):
			return self._parse_classic_xref(pos)
		return self._parse_xref_stream(pos)

	def _parse_classic_xref(self, pos: int) -> Tuple[Dict[Tuple[int, int], XrefEntry], Dict[str, PdfObj]]:
		entries: Dict[Tuple[int, int], XrefEntry] = {}
		pos += 4
		while True:
			pos = self._skip_ws(pos)
			if self.data.startswith(b"trailer", pos):
				pos += len(b"trailer")
				pos = self._skip_ws(pos)
				trailer = ObjectParser(self.data[pos:]).parse_value()
				return entries, trailer if isinstance(trailer, dict) else {}
			line, pos = self._read_line(pos)
			parts = line.strip().split()
			if len(parts) < 2:
				raise PdfSyntaxError("malformed xref subsection")
			start_obj = int(parts[0])
			count = int(parts[1])
			if count < 0 or len(entries) + count > limits.MAX_OBJECTS:
				self.warn("OBJECT_LIMIT", "classic xref entry limit")
				count = max(0, limits.MAX_OBJECTS - len(entries))
			for i in range(count):
				line, pos = self._read_line(pos)
				cols = line.strip().split()
				if len(cols) < 3:
					raise PdfSyntaxError("malformed xref entry")
				off = int(cols[0])
				gen = int(cols[1])
				status = cols[2][:1]
				obj_num = start_obj + i
				if status == b"n":
					entries[(obj_num, gen)] = XrefEntry(1, off, gen)
				else:
					entries[(obj_num, gen)] = XrefEntry(0, off, gen)

	def _parse_xref_stream(self, offset: int) -> Tuple[Dict[Tuple[int, int], XrefEntry], Dict[str, PdfObj]]:
		num, _gen, obj = self._parse_indirect_at_offset(offset)
		if not isinstance(obj, Stream) or str(obj.attrs.get("Type")) != "XRef":
			raise PdfSyntaxError("expected xref stream at offset %d" % offset)
		widths = self.resolve(obj.attrs.get("W"))
		if not isinstance(widths, list) or len(widths) < 3:
			raise PdfSyntaxError("xref stream missing W")
		w = [int(x) if isinstance(x, (int, float)) else 0 for x in widths[:3]]
		size = self.resolve(obj.attrs.get("Size"))
		index = self.resolve(obj.attrs.get("Index"))
		if not isinstance(index, list):
			index = [0, int(size) if isinstance(size, int) else num + 1]
		data = self.decoded_stream(obj)
		row_size = sum(w)
		if row_size <= 0:
			raise PdfSyntaxError("invalid xref stream row width")
		entries: Dict[Tuple[int, int], XrefEntry] = {}
		row = 0
		for i in range(0, len(index), 2):
			if i + 1 >= len(index):
				break
			start_obj = int(index[i])
			count = int(index[i + 1])
			if count < 0 or len(entries) + count > limits.MAX_OBJECTS:
				self.warn("OBJECT_LIMIT", "xref stream entry limit")
				count = max(0, limits.MAX_OBJECTS - len(entries))
			for j in range(count):
				start = row * row_size
				end = start + row_size
				if end > len(data):
					raise PdfSyntaxError("truncated xref stream")
				fields = self._read_xref_stream_fields(data[start:end], w)
				typ = fields[0] if w[0] else 1
				field1 = fields[1]
				field2 = fields[2]
				gen = field2 if typ in (0, 1) else 0
				entries[(start_obj + j, gen)] = XrefEntry(typ, field1, field2)
				row += 1
		return entries, obj.attrs

	def _read_xref_stream_fields(self, row: bytes, widths: Sequence[int]) -> Tuple[int, int, int]:
		values = []
		pos = 0
		for width in widths[:3]:
			if width <= 0:
				values.append(0)
				continue
			values.append(int.from_bytes(row[pos : pos + width], "big"))
			pos += width
		while len(values) < 3:
			values.append(0)
		return int(values[0]), int(values[1]), int(values[2])

	def _skip_ws(self, pos: int) -> int:
		lex = PdfLexer(self.data, pos)
		lex.skip_ws()
		return lex.pos

	def _read_line(self, pos: int) -> Tuple[bytes, int]:
		n = len(self.data)
		end = pos
		while end < n and self.data[end] not in b"\r\n":
			end += 1
		line = self.data[pos:end]
		if end < n and self.data[end] == ord("\r"):
			end += 1
			if end < n and self.data[end] == ord("\n"):
				end += 1
		elif end < n and self.data[end] == ord("\n"):
			end += 1
		return line, end

	def _parse_indirect_at_offset(self, offset: int) -> Tuple[int, int, PdfObj]:
		num, gen, value, _end = self._parse_indirect_with_extent(offset)
		return num, gen, value

	def _parse_indirect_with_extent(self, offset: int) -> Tuple[int, int, PdfObj, int]:
		lex = PdfLexer(self.data, offset)
		t1 = lex.next_token()
		t2 = lex.next_token()
		t3 = lex.next_token()
		if not (t1 and t2 and t3 and t1[0] == "INT" and t2[0] == "INT" and t3[0] == "KEYWORD" and t3[1] == "obj"):
			raise PdfSyntaxError("expected indirect object")
		num = int(t1[1])
		gen = int(t2[1])
		body_start = lex.pos
		parser = ObjectParser(self.data[body_start:])
		value = parser.parse_value()
		pos = self._skip_ws(body_start + parser.pos)
		if isinstance(value, dict) and self.data.startswith(b"stream", pos):
			pos += len(b"stream")
			if self.data[pos : pos + 2] == b"\r\n":
				pos += 2
			elif self.data[pos : pos + 1] in (b"\n", b"\r"):
				pos += 1
			length = self.resolve(value.get("Length"))
			raw = self._read_stream_raw(
				pos,
				length if isinstance(length, int) else None,
				num,
				self.data,
			)
			stream_end = pos + len(raw)
			probe = stream_end
			if self.data[probe : probe + 2] == b"\r\n":
				probe += 2
			elif self.data[probe : probe + 1] in (b"\n", b"\r"):
				probe += 1
			if self.data.startswith(b"endstream", probe):
				probe += len(b"endstream")
			endobj = self.data.find(b"endobj", probe)
			object_end = endobj + len(b"endobj") if endobj >= 0 else probe
			return num, gen, Stream(value, raw, objnum=num), object_end
		endobj = self.data.find(b"endobj", pos)
		if endobj < 0:
			self.warn("MISSING_ENDOBJ", "object %d" % num)
			return num, gen, value, pos
		return num, gen, value, endobj + len(b"endobj")

	def _parse_objects_by_scan(self) -> None:
		self.warn("RECOVERED", "xref unavailable; scanned indirect objects")
		pattern = re.compile(rb"(\d{1,10})\s+(\d{1,5})\s+obj\b")
		position = 0
		while position < len(self.data):
			m = pattern.search(self.data, position)
			if m is None:
				break
			if len(self.objects) >= limits.MAX_OBJECTS:
				self.warn("OBJECT_LIMIT", "maximum object count reached")
				break
			try:
				num, gen, obj, object_end = self._parse_indirect_with_extent(m.start())
				self.objects[(num, gen)] = obj
				position = max(m.end(), object_end)
			except Exception as exc:
				self.warn("OBJECT_PARSE_FAILED", "offset %d: %s" % (m.start(), exc))
				position = m.end()

	def _parse_indirect_body(self, body: bytes, num: int) -> PdfObj:
		parser = ObjectParser(body)
		value = parser.parse_value()
		pos = parser.pos
		lex = PdfLexer(body, pos)
		lex.skip_ws()
		pos = lex.pos
		if isinstance(value, dict) and body[pos : pos + 6] == b"stream":
			pos += 6
			if body[pos : pos + 2] == b"\r\n":
				pos += 2
			elif body[pos : pos + 1] == b"\n":
				pos += 1
			elif body[pos : pos + 1] == b"\r":
				pos += 1
			length = self.resolve(value.get("Length"))
			raw = self._read_stream_raw(
				pos,
				length if isinstance(length, int) else None,
				num,
				body,
			)
			return Stream(value, raw, objnum=num)
		return value

	def _unpack_object_streams(self) -> None:
		cache: Dict[int, List[Tuple[int, PdfObj]]] = {}

		def unpack(stream_num: int) -> List[Tuple[int, PdfObj]]:
			if stream_num in cache:
				return cache[stream_num]
			obj = self.objects.get((stream_num, 0))
			if not isinstance(obj, Stream) or str(self.resolve(obj.attrs.get("Type"))) != "ObjStm":
				raise PdfSyntaxError("xref type-2 entry points to non-ObjStm %d" % stream_num)
			n_value = self.resolve_number(obj.attrs.get("N"), 0)
			first_value = self.resolve_number(obj.attrs.get("First"), 0)
			n = int(n_value or 0)
			first = int(first_value or 0)
			if n < 0 or n > limits.MAX_OBJECT_STREAM_OBJECTS:
				raise PdfSyntaxError("invalid ObjStm /N %d" % n)
			data = self.decoded_stream(obj)
			if first < 0 or first > len(data):
				raise PdfSyntaxError("invalid ObjStm /First %d" % first)
			header = data[:first].strip().split()
			if len(header) < n * 2:
				raise PdfSyntaxError("short ObjStm header")
			pairs = [(int(header[i]), int(header[i + 1])) for i in range(0, n * 2, 2)]
			payload = data[first:]
			parsed: List[Tuple[int, PdfObj]] = []
			for index, (obj_num, offset) in enumerate(pairs):
				if offset < 0 or offset > len(payload):
					raise PdfSyntaxError("invalid ObjStm member offset")
				end = pairs[index + 1][1] if index + 1 < len(pairs) else len(payload)
				value = ObjectParser(payload[offset:end]).parse_value()
				if isinstance(value, Stream) and str(value.attrs.get("Type")) == "ObjStm":
					self.warn("OBJSTM_NESTED", "object %d" % obj_num)
				parsed.append((obj_num, value))
			cache[stream_num] = parsed
			return parsed

		type2 = [
			(key, entry)
			for key, entry in self.xref_entries.items()
			if entry.obj_type == 2
		]
		if type2:
			for (expected_num, _gen), entry in sorted(type2):
				try:
					members = unpack(entry.field1)
					if entry.field2 < 0 or entry.field2 >= len(members):
						raise PdfSyntaxError("ObjStm index out of range")
					actual_num, value = members[entry.field2]
					if actual_num != expected_num:
						self.warn("OBJSTM_OBJECT_MISMATCH", "xref %d, header %d" % (expected_num, actual_num))
					self.objects[(expected_num, 0)] = value
				except Exception as exc:
					self.warn("OBJSTM_PARSE_FAILED", "object %d: %s" % (expected_num, exc))
			return

		if self.parse_mode != "recovered":
			return
		for obj in list(self.objects.values()):
			if not isinstance(obj, Stream) or str(self.resolve(obj.attrs.get("Type"))) != "ObjStm":
				continue
			try:
				for num, value in unpack(int(obj.objnum or 0)):
					if (num, 0) in self.objects:
						continue
					self.objects[(num, 0)] = value
			except Exception as exc:
				self.warn("OBJSTM_PARSE_FAILED", str(exc))


class Converter:
	def __init__(self, data: bytes, options: Optional[ConvertOptions] = None):
		self.doc = PdfDocument(data)
		self.options = options or ConvertOptions()
		self.assets: Dict[str, bytes] = {}
		self.chars: List[Char] = []
		self.segments: List[Segment] = []
		self.fills: List[Fill] = []
		self.painted_paths: List[PaintedPath] = []
		self.images: List[ImageItem] = []
		self.links: List[LinkItem] = []
		self.seq = 0
		self.paint_seq = 0
		self.paint_path_counts: Dict[int, int] = {}
		self.page_sizes: Dict[int, Tuple[float, float]] = {}
		self.lines_by_page: Dict[int, List[Line]] = {}
		self.degraded_pages: set[int] = set()
		self.ink_pages: set[int] = set()		
		self.processed_pages: set[int] = set()
		self.anchors: List[AnchorItem] = []
		self._anchor_keys: set[Tuple[str, int]] = set()
		self._pages: List[Dict[str, PdfObj]] = []
		self._page_ref_to_num: Dict[Tuple[int, int], int] = {}
		self.page_transforms: Dict[int, Tuple[float, float, float, float, float, float]] = {}
		self._png_raster_cache: Dict[str, Optional[Tuple[int, int, int, int, bytes]]] = {}

	def convert(self) -> ConvertResult:
		if self.doc.encrypted:
			# Keep the semantic refusal record available to JSON/report callers,
			# while preserving the established empty Markdown refusal contract.
			from .html.semantic import render_semantic_html
			from .ir.semantic import SemanticDocument
			from .reporting.report import attach_semantic_document

			semantic_document = SemanticDocument(
				metadata={
					"title": "CocoaPDF Document",
					"source": "encrypted_pdf_refusal",
					"output_policy": "refused",
					"ocr_used": False,
				},
				warnings=["ENCRYPTED_PDF_REFUSED"],
				version="2",
			)
			report = self._report(self.doc.pages(), [])
			attach_semantic_document(report, semantic_document, require_provenance=True)
			report["semantic_output_used"] = True
			report["output_derivation"] = {
				"markdown": "semantic_graph",
				"html": "semantic_graph",
				"json": "semantic_graph",
			}
			report["warnings"] = [warning.__dict__ for warning in self.doc.warnings]
			result = ConvertResult(
				markdown="",
				html=render_semantic_html(semantic_document),
				assets={},
				warnings=self.doc.warnings,
				report=report,
				semantic=semantic_document,
			)
			if self.options.report_path:
				report_path = Path(self.options.report_path)
				report_path.parent.mkdir(parents=True, exist_ok=True)
				write_utf8_lf(report_path, json.dumps(report, indent=2))
			return result
		pages = self.doc.pages()
		self._pages = pages
		for page_num, page in enumerate(pages, 1):
			ref = page.get("__page_ref__")
			if isinstance(ref, Ref):
				self._page_ref_to_num[(ref.num, ref.gen)] = page_num
				self._page_ref_to_num.setdefault((ref.num, 0), page_num)
		if not pages:
			self.doc.warn("NO_PAGES", "no page tree found")
		selected_pages = parse_page_selection(self.options.pages, len(pages))
		for i, page in enumerate(pages, 1):
			if selected_pages is not None and i not in selected_pages:
				continue
			self._interpret_page(i, page)
		self._postprocess_chars()
		renderer = MarkdownRenderer(self)
		events_by_page = renderer.analyze()
		layout_markdown = renderer.render()
		self.lines_by_page = renderer.lines_by_page
		regions = []
		try:
			from .layout.regions import detect_regions

			regions = detect_regions(
				self.lines_by_page,
				self.segments,
				self.fills,
				self.images,
				self.page_sizes,
				events_by_page,
			)
		except Exception as exc:
			self.doc.warn("REGION_DETECTION_FAILED", str(exc))
		from .semantics.graph import build_semantic_graph

		semantic_document = build_semantic_graph(self, renderer, events_by_page, regions)
		catalog = self.doc.catalog()
		if isinstance(catalog, dict) and catalog.get("StructTreeRoot") is not None:
			try:
				from .semantics.reconcile import reconcile_semantic_graph
				from .semantics.tagged import parse_tagged_structure

				tagged_document = parse_tagged_structure(self.doc)
				reconcile_semantic_graph(semantic_document, tagged_document, self.chars)
			except Exception as exc:
				self.doc.warn("TAGGED_RECONCILE_FAILED", str(exc))
		report = self._report(pages, regions)
		report["semantic_output_used"] = True
		report["output_derivation"] = {
			"markdown": "semantic_graph",
			"html": "semantic_graph",
			"json": "semantic_graph",
		}
		report["output_projection"] = "semantic_graph_with_lossless_layout_reconciliation"
		try:
			from .semantics.output import render_reconciled_outputs

			markdown, html = render_reconciled_outputs(layout_markdown, semantic_document, report)
		except Exception as exc:
			self.doc.warn("SEMANTIC_OUTPUT_FAILED", str(exc))
			markdown = layout_markdown
			from .html.render import render_html

			html = render_html(markdown, report)
			report["semantic_output_used"] = False
			report["output_derivation"] = {
				"markdown": "layout_renderer_fallback",
				"html": "markdown_html_renderer_fallback",
				"json": "semantic_graph",
			}
		from .reporting.report import attach_semantic_document

		attach_semantic_document(report, semantic_document, require_provenance=True)
		semantic_warnings = [
			item for item in report.get("warnings", [])
			if isinstance(item, dict) and item.get("code") == "SEMANTIC_GRAPH_INVALID"
		]
		report["warnings"] = [warning.__dict__ for warning in self.doc.warnings] + semantic_warnings
		if self.options.report_path:
			report_path = Path(self.options.report_path)
			report_path.parent.mkdir(parents=True, exist_ok=True)
			write_utf8_lf(report_path, json.dumps(report, indent=2))
		return ConvertResult(markdown=markdown, html=html, assets=self.assets, warnings=self.doc.warnings, report=report, semantic=semantic_document)

	def _report(self, pages: List[Dict[str, PdfObj]], regions: Optional[List[Any]] = None) -> Dict[str, Any]:
		region_dicts = [r.to_dict() if hasattr(r, "to_dict") else r for r in (regions or [])]
		nodes = self._report_nodes(region_dicts)
		return {
			"tool": "CocoaPDF",
			"version": __version__,
			"description": "Deterministic PDF-to-Markdown/HTML conversion for structured text-layer PDFs. No OCR. No AI.",
			"pages": len(pages),
			"processed_pages": sorted(self.processed_pages),
			"chars": len(self.chars),
			"segments": len(self.segments),
			"fills": len(self.fills),
			"images": len(self.images),
			"ocr_used": False,
			"image_text_extraction_attempted": False,
			"warnings": [w.__dict__ for w in self.doc.warnings],
			"assets": sorted(self.assets),
			"images_detail": self._report_images(),
			"parser_mode": self.doc.parse_mode,
			"encrypted": self.doc.encrypted,
			"active_content": list(self.doc.active_content),
			"anchors": [
				{"id": anchor.name, "page": anchor.page, "y": round(anchor.y, 3)}
				for anchor in self.anchors
			],
			"mode_per_page": [
				(
					"refused"
					if self.doc.encrypted
					else "not_selected"
					if i not in self.processed_pages
					else "degraded"
					if i in self.degraded_pages
					else "geometric"
				)
				for i in range(1, len(pages) + 1)
			],
			"regions": region_dicts,
			"nodes": nodes,
		}

	def _report_nodes(self, regions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
		nodes: List[Dict[str, Any]] = []
		for idx, region in enumerate(regions, 1):
			if not isinstance(region, dict):
				continue
			region_id = str(region.get("id") or "region-%d" % idx)
			nodes.append(
				{
					"id": "node-%s" % region_id,
					"type": "region",
					"kind": region.get("kind", "unknown"),
					"page": region.get("page"),
					"bbox": region.get("bbox", {}),
					"region_id": region_id,
					"confidence": float(region.get("confidence", 0.0) or 0.0),
					"evidence": region.get("evidence", []),
					"reading_order_index": region.get("reading_order_index", idx - 1),
				}
			)
		return nodes

	def _report_images(self) -> List[Dict[str, Any]]:
		out: List[Dict[str, Any]] = []
		for img in self.images:
			page_width, _page_height = self.page_sizes.get(img.page, (612.0, 792.0))
			out.append(
				{
					"asset": img.name,
					"page": img.page,
					"bbox": {
						"x0": round(img.x0, 3),
						"y0": round(img.y0, 3),
						"x1": round(img.x1, 3),
						"y1": round(img.y1, 3),
					},
					"display_width": round(max(0.0, img.x1 - img.x0), 3),
					"display_height": round(max(0.0, img.y1 - img.y0), 3),
					"placed_width": round(
						img.placed_width or max(0.0, img.x1 - img.x0),
						3,
					),
					"placed_height": round(
						img.placed_height or max(0.0, img.y1 - img.y0),
						3,
					),
					"intrinsic_width": img.intrinsic_width,
					"intrinsic_height": img.intrinsic_height,
					"kind": img.kind,
					"alt": img.alt,
					"link": img.link,
					"quad": [
						{"x": round(point[0], 3), "y": round(point[1], 3)}
						for point in img.quad
					],
					"alignment": image_alignment(img, page_width),
				}
			)
		return out

	def _interpret_page(self, page_num: int, page: Dict[str, PdfObj]) -> None:
		self.processed_pages.add(page_num)
		media = self.doc.resolve_array(page.get("CropBox")) or self.doc.resolve_array(page.get("MediaBox"))
		nums = [self.doc.resolve_number(value) for value in media[:4]]
		if len(nums) < 4 or any(value is None for value in nums):
			nums = [0.0, 0.0, 612.0, 792.0]
			self.doc.warn("BAD_MEDIABOX", "missing or non-numeric box", page_num)
		x0, x1 = sorted((float(nums[0]), float(nums[2])))
		y0, y1 = sorted((float(nums[1]), float(nums[3])))
		width, height = x1 - x0, y1 - y0
		if width <= 1.0 or height <= 1.0:
			x0, y0, width, height = 0.0, 0.0, 612.0, 792.0
			self.doc.warn("BAD_MEDIABOX", "degenerate box", page_num)
		rotate = int(self.doc.resolve_number(page.get("Rotate"), 0) or 0) % 360
		if rotate not in (0, 90, 180, 270):
			self.doc.warn("BAD_ROTATE", str(rotate), page_num)
			rotate = 0
		user_unit = self.doc.resolve_number(page.get("UserUnit"), 1.0) or 1.0
		if user_unit <= 0:
			self.doc.warn("BAD_USERUNIT", str(user_unit), page_num)
			user_unit = 1.0
		page_ctm, display_width, display_height = page_normalization_transform(
			x0, y0, width, height, rotate, user_unit
		)
		self.page_transforms[page_num] = page_ctm
		self.page_sizes[page_num] = (display_width, display_height)
		if page.get("AA") is not None:
			path = "Page[%d]/AA" % page_num
			self.doc.active_content.append({"code": "SECURITY_ACTIVE_CONTENT", "path": path})
			self.doc.warn("SECURITY_ACTIVE_CONTENT", path, page_num)
		resources = self.doc.resolve(page.get("Resources")) or {}
		fonts = self._load_fonts(resources)
		xobjects = self._load_xobjects(resources)
		extgstates = self._load_named_resource_dict(resources, "ExtGState")
		properties = self._load_named_resource_dict(resources, "Properties")
		contents = self.doc.resolve(page.get("Contents"))
		streams: List[Stream] = []
		if isinstance(contents, Stream):
			streams = [contents]
		elif isinstance(contents, list):
			streams = [self.doc.resolve(x) for x in contents if isinstance(self.doc.resolve(x), Stream)]
		data = b"\n".join(self.doc.decoded_stream(s) for s in streams)
		if len(data) > limits.MAX_CONTENT_BYTES_PER_PAGE:
			data = data[: limits.MAX_CONTENT_BYTES_PER_PAGE]
			self.degraded_pages.add(page_num)
			self.doc.warn("PAGE_TRUNCATED", "content stream byte limit", page_num)
		interpreter = ContentInterpreter(
			self,
			page_num,
			display_height,
			fonts,
			xobjects,
			extgstates=extgstates,
			properties=properties,
		)
		interpreter.ctm = page_ctm
		interpreter.run(data)
		if any(item.page == page_num for item in self.segments + self.fills + self.images):
			self.ink_pages.add(page_num)
		if page_num in self.ink_pages and not any(char.page == page_num and not char.invisible for char in self.chars):
			self.degraded_pages.add(page_num)
			self.doc.warn("NO_TEXT_LAYER", "page has graphics but no visible text", page_num)
		self._load_links(page_num, page, display_height, page_ctm)


	def _load_fonts(self, resources: Dict[str, PdfObj]) -> Dict[str, Font]:
		out: Dict[str, Font] = {}
		font_dict = self.doc.resolve(resources.get("Font")) if isinstance(resources, dict) else None
		if not isinstance(font_dict, dict):
			return out
		for key, ref in font_dict.items():
			fd = self.doc.resolve(ref)
			if isinstance(fd, dict):
				out[str(key)] = self._font_from_dict(str(key), fd)
		return out

	def _font_from_dict(self, key: str, fd: Dict[str, PdfObj]) -> Font:
		from .fonts.decoding import parse_encoding_differences

		subtype = str(self.doc.resolve(fd.get("Subtype")) or "")
		base = str(self.doc.resolve(fd.get("BaseFont")) or key)
		encoding_value = self.doc.resolve(fd.get("Encoding"))
		vertical = False
		if isinstance(encoding_value, Stream):
			enc = str(self.doc.resolve(encoding_value.attrs.get("CMapName")) or "EmbeddedCMap")
			vertical = self.doc.resolve_number(encoding_value.attrs.get("WMode"), 0) == 1
		elif isinstance(encoding_value, dict):
			enc = str(
				self.doc.resolve(encoding_value.get("BaseEncoding"))
				or self.doc.resolve(encoding_value.get("CMapName"))
				or "WinAnsiEncoding"
			)
			vertical = self.doc.resolve_number(encoding_value.get("WMode"), 0) == 1
		else:
			enc = str(encoding_value or "WinAnsiEncoding")
		vertical = vertical or enc.lstrip("/").endswith("-V")
		font = Font(
			name=key,
			base_font=base,
			subtype=subtype,
			encoding=enc,
			composite=(subtype == "Type0"),
			vertical=vertical,
		)
		font.differences = parse_encoding_differences(encoding_value)
		tu = self.doc.resolve(fd.get("ToUnicode"))
		if isinstance(tu, Stream):
			font.to_unicode = parse_tounicode(self.doc.decoded_stream(tu))
		widths = self.doc.resolve(fd.get("Widths"))
		first = self.doc.resolve(fd.get("FirstChar"))
		if isinstance(widths, list) and isinstance(first, int):
			font.first_char = first
			for i, w in enumerate(widths):
				if isinstance(w, (int, float)):
					font.widths[first + i] = float(w)
		desc = self.doc.resolve(fd.get("DescendantFonts"))
		if isinstance(desc, list) and desc:
			cid = self.doc.resolve(desc[0])
			if isinstance(cid, dict):
				dw = self.doc.resolve(cid.get("DW"))
				if isinstance(dw, (int, float)):
					font.dw = float(dw)
				font.descendant_widths.update(parse_w_array(self.doc.resolve(cid.get("W"))))
				dw2 = self.doc.resolve(cid.get("DW2"))
				if isinstance(dw2, list) and len(dw2) >= 2:
					v1y = self.doc.resolve_number(dw2[0])
					w1y = self.doc.resolve_number(dw2[1])
					if v1y is not None and w1y is not None:
						font.dw2 = (float(v1y), float(w1y))
				font.vertical_metrics.update(parse_w2_array(self.doc.resolve(cid.get("W2"))))
		if font.composite and font.dw is None:
			font.dw = 1000.0
		return font

	def _load_xobjects(self, resources: Dict[str, PdfObj]) -> Dict[str, Stream]:
		out: Dict[str, Stream] = {}
		xobjs = self.doc.resolve(resources.get("XObject")) if isinstance(resources, dict) else None
		if isinstance(xobjs, dict):
			for k, v in xobjs.items():
				obj = self.doc.resolve(v)
				if isinstance(obj, Stream):
					out[str(k)] = obj
		return out

	def _load_named_resource_dict(self, resources: Dict[str, PdfObj], key: str) -> Dict[str, PdfObj]:
		out: Dict[str, PdfObj] = {}
		values = self.doc.resolve(resources.get(key)) if isinstance(resources, dict) else None
		if not isinstance(values, dict):
			return out
		for name, value in values.items():
			resolved = self.doc.resolve(value)
			if resolved is not None:
				out[str(name)] = resolved
		return out

	def _load_links(self, page_num: int, page: Dict[str, PdfObj], page_height: float, page_ctm: Tuple[float, float, float, float, float, float]) -> None:
		annots = self.doc.resolve(page.get("Annots")) or []
		if not isinstance(annots, list):
			return
		for a in annots:
			annotation_object_ref = indirect_ref_text(a)
			ad = self.doc.resolve(a)
			if not isinstance(ad, dict):
				continue
			subtype = str(ad.get("Subtype"))
			if subtype in ("RichMedia", "Movie", "Screen", "FileAttachment"):
				code = "SECURITY_EMBEDDED_FILE" if subtype == "FileAttachment" else "SECURITY_ACTIVE_CONTENT"
				path = "Page[%d]/Annot/%s" % (page_num, subtype)
				self.doc.active_content.append({"code": code, "path": path})
				self.doc.warn(code, path, page_num)
			
			rect = self.doc.resolve(ad.get("Rect"))
			if not isinstance(rect, list) or len(rect) < 4:
				continue
			values = [self.doc.resolve_number(value) for value in rect[:4]]
			if any(value is None for value in values):
				self.doc.warn("BAD_ANNOT_RECT", "non-numeric annotation rectangle", page_num)
				continue
			x0, y0, x1, y1 = [float(value) for value in values]
			points = [apply_mat(page_ctm, x, y) for x in (x0, x1) for y in (y0, y1)]
			xs = [point[0] for point in points]
			ys = [page_height - point[1] for point in points]
			norm = (min(xs), min(ys), max(xs), max(ys))
			action = self.doc.resolve(ad.get("A"))
			uri = None
			dest = self._dest_href(ad.get("Dest"))
			if subtype == "Link":
				if isinstance(action, dict):
					action_type = str(action.get("S") or "")
					if action_type == "URI":
						u = self.doc.resolve(action.get("URI"))
						raw_uri = decode_pdf_text(u) if isinstance(u, bytes) else (str(u) if u else None)
						if raw_uri:
							uri = safe_href(raw_uri)
							if uri is None and is_unsafe_href(raw_uri):
								self.doc.warn("SECURITY_UNSAFE_URI", raw_uri, page_num)
					elif action_type == "GoTo":
						dest = self._dest_href(action.get("D")) or dest
					elif action_type in ("GoToR", "Launch", "JavaScript", "SubmitForm", "ResetForm", "ImportData"):
						self.doc.warn("SECURITY_ACTIVE_ACTION", action_type, page_num)
					else:
						dest = self._dest_href(action.get("D")) or dest
				self.links.append(LinkItem(norm, uri, dest, page_num, annotation_object_ref))
			elif action:
				self.doc.warn("SECURITY_ACTIVE_ACTION", "annotation %s" % subtype, page_num)

	def _dest_href(self, obj: PdfObj) -> Optional[str]:
		dest = self.doc.resolve(obj)
		if dest is None:
			return None

		if isinstance(dest, bytes):
			name = decode_pdf_text(dest)
		elif isinstance(dest, (PdfName, str)):
			name = str(dest)
		else:
			name = ""

		if name:
			anchor = self._anchor_name(name)
			target = self.doc.named_destinations().get(name)
			if target is not None:
				self._register_destination(anchor, target)
			else:
				self.doc.warn("DEST_TARGET_UNRESOLVED", name)
			return safe_href("#" + anchor)

		if isinstance(dest, dict) and dest.get("D") is not None:
			dest = self.doc.resolve(dest.get("D"))

		if isinstance(dest, list):
			fingerprint = hashlib.sha256(repr(dest).encode("utf-8", "replace")).hexdigest()[:16]
			anchor = "pdf-dest-" + fingerprint
			self._register_destination(anchor, dest)
			return safe_href("#" + anchor)
		return None

	def _anchor_name(self, value: str) -> str:
		name = re.sub(r"[^A-Za-z0-9._~:-]+", "-", str(value).strip()).strip("-")
		if name:
			return name
		return "pdf-dest-" + hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()[:16]

	def _register_destination(self, anchor: str, raw_destination: PdfObj) -> None:
		destination = self.doc.resolve(raw_destination)
		if isinstance(destination, dict):
			destination = self.doc.resolve(destination.get("D"))
		if not isinstance(destination, list) or not destination:
			return

		page_obj = destination[0]
		page_num: Optional[int] = None
		if isinstance(page_obj, Ref):
			page_num = self._page_ref_to_num.get((page_obj.num, page_obj.gen))
			if page_num is None:
				page_num = self._page_ref_to_num.get((page_obj.num, 0))
		elif isinstance(page_obj, int) and 0 <= page_obj < len(self._pages):
			page_num = page_obj + 1
		if page_num is None or not (1 <= page_num <= len(self._pages)):
			self.doc.warn("DEST_TARGET_UNRESOLVED", anchor)
			return

		matrix, _display_width, display_height = self._page_geometry_for_destination(page_num)
		kind = str(self.doc.resolve(destination[1])) if len(destination) > 1 else "Fit"
		raw_x = 0.0
		raw_y: Optional[float] = None
		if kind == "XYZ":
			raw_x = self.doc.resolve_number(destination[2], 0.0) or 0.0 if len(destination) > 2 else 0.0
			raw_y = self.doc.resolve_number(destination[3]) if len(destination) > 3 else None
		elif kind in ("FitH", "FitBH"):
			raw_y = self.doc.resolve_number(destination[2]) if len(destination) > 2 else None
		elif kind == "FitR" and len(destination) > 5:
			raw_x = self.doc.resolve_number(destination[2], 0.0) or 0.0
			raw_y = self.doc.resolve_number(destination[5])

		if raw_y is None:
			normalized_y = 0.0
		else:
			_point_x, point_y = apply_mat(matrix, raw_x, raw_y)
			normalized_y = max(0.0, min(display_height, display_height - point_y))

		key = (anchor, page_num)
		if key in self._anchor_keys:
			return
		self._anchor_keys.add(key)
		self.anchors.append(AnchorItem(anchor, page_num, normalized_y))

	def _page_geometry_for_destination(
		self,
		page_num: int,
	) -> Tuple[Tuple[float, float, float, float, float, float], float, float]:
		if page_num in self.page_transforms and page_num in self.page_sizes:
			width, height = self.page_sizes[page_num]
			return self.page_transforms[page_num], width, height

		page = self._pages[page_num - 1]
		media = self.doc.resolve_array(page.get("CropBox")) or self.doc.resolve_array(page.get("MediaBox"))
		values = [self.doc.resolve_number(item) for item in media[:4]]
		if len(values) < 4 or any(value is None for value in values):
			values = [0.0, 0.0, 612.0, 792.0]
		x0, x1 = sorted((float(values[0]), float(values[2])))
		y0, y1 = sorted((float(values[1]), float(values[3])))
		width = max(1.0, x1 - x0)
		height = max(1.0, y1 - y0)
		rotate = int(self.doc.resolve_number(page.get("Rotate"), 0) or 0) % 360
		if rotate not in (0, 90, 180, 270):
			rotate = 0
		user_unit = self.doc.resolve_number(page.get("UserUnit"), 1.0) or 1.0
		if user_unit <= 0:
			user_unit = 1.0
		return page_normalization_transform(x0, y0, width, height, rotate, user_unit)

	def _postprocess_chars(self) -> None:
		# Deduplicate fake bold/double-drawn text.
		kept: List[Char] = []
		for ch in sorted(self.chars, key=lambda c: (c.page, c.seq)):
			dup = None
			for prev in reversed(kept[-20:]):
				if (
					prev.page == ch.page
					and prev.text == ch.text
					and abs(prev.x0 - ch.x0) <= 0.45
					and abs(prev.y0 - ch.y0) <= 0.45
				):
					dup = prev
					break
			if dup:
				dup.synthetic_bold = True
				continue
			kept.append(ch)
		self.chars = kept
		self._mark_low_contrast_text()

		# Attach link annotations by char center.
		for ch in self.chars:
			cx = (ch.x0 + ch.x1) / 2
			cy = (ch.y0 + ch.y1) / 2
			for link in self.links:
				if link.page != ch.page:
					continue
				x0, y0, x1, y1 = link.rect
				if x0 <= cx <= x1 and y0 <= cy <= y1:
					ch.link = link.uri or link.dest
					ch.link_object_ref = link.object_ref
					break

		# Image links are attached independently from text links.
		for image in self.images:
			cx = (image.x0 + image.x1) / 2
			cy = (image.y0 + image.y1) / 2
			for link in self.links:
				if link.page != image.page:
					continue
				x0, y0, x1, y1 = link.rect
				if x0 <= cx <= x1 and y0 <= cy <= y1:
					image.link = link.uri or link.dest
					image.link_object_ref = link.object_ref
					break

		# Underline/strike evidence.
		for seg in self.segments:
			if not seg.horizontal:
				continue
			y = (seg.y0 + seg.y1) / 2
			x0, x1 = sorted((seg.x0, seg.x1))
			for ch in self.chars:
				cx = (ch.x0 + ch.x1) / 2
				if ch.page != seg.page or not (x0 <= cx <= x1):
					continue
				rel = (y - ch.y0) / max(ch.size, 1.0)
				if 0.25 <= rel <= 0.65:
					ch.strike = True
				elif 0.75 <= rel <= 1.25:
					ch.underline = True

		# Highlight evidence from colored fills behind text. Large page/background
		# fills and neutral gray boxes are intentionally ignored.
		for fill in self.fills:
			if not is_highlight_fill(fill):
				continue
			for ch in self.chars:
				if ch.page != fill.page or not ch.text.strip():
					continue
				cx = (ch.x0 + ch.x1) / 2
				cy = (ch.y0 + ch.y1) / 2
				if fill.x0 <= cx <= fill.x1 and fill.y0 <= cy <= fill.y1:
					ch.highlight = True

	def _mark_low_contrast_text(self) -> None:
		counts: Dict[int, int] = {}
		for char in self.chars:
			if char.invisible or not char.text.strip():
				continue
			background = self._painted_background_color(char)
			if background is None:
				continue
			colors = []
			if char.render_mode in (0, 2, 4, 6):
				colors.append(char.fill_color)
			if char.render_mode in (1, 2, 5, 6):
				colors.append(char.stroke_color)
			if not colors:
				continue
			# A glyph is visible if any of its painted components has useful
			# contrast. The tight threshold only removes effectively concealed
			# text layers; ordinary low-contrast design text is preserved.
			if max(color_contrast(color, background) for color in colors) <= 1.20:
				char.invisible = True
				counts[char.page] = counts.get(char.page, 0) + 1
		for page, count in sorted(counts.items()):
			self.doc.warn("INVISIBLE_TEXT", "%d low-contrast glyphs" % count, page)

	def _painted_background_color(self, char: Char) -> Optional[Tuple[float, float, float]]:
		cx = (char.x0 + char.x1) / 2
		cy = (char.y0 + char.y1) / 2
		painted: List[Tuple[int, str, Any]] = []
		for fill in self.fills:
			if (
				fill.page == char.page
				and fill.seq < char.seq
				and fill.x0 <= cx <= fill.x1
				and fill.y0 <= cy <= fill.y1
			):
				painted.append((fill.seq, "fill", fill))
		for image in self.images:
			if (
				image.page == char.page
				and image.seq < char.seq
				and image.x0 <= cx <= image.x1
				and image.y0 <= cy <= image.y1
			):
				painted.append((image.seq, "image", image))
		if not painted:
			return (1.0, 1.0, 1.0)
		_seq, kind, item = max(painted, key=lambda entry: entry[0])
		if kind == "fill":
			return item.color
		return self._sample_image_color(item, cx, cy)

	def _sample_image_color(self, image: ImageItem, x: float, y: float) -> Optional[Tuple[float, float, float]]:
		cache_key = image.name or hashlib.sha256(image.data).hexdigest()
		if cache_key not in self._png_raster_cache:
			self._png_raster_cache[cache_key] = decode_png_pixels(image.data)
		raster = self._png_raster_cache.get(cache_key)
		if raster is None or image.x1 <= image.x0 or image.y1 <= image.y0:
			return None
		width, height, color_type, channels, pixels = raster
		px = max(0, min(width - 1, int(round((x - image.x0) / (image.x1 - image.x0) * (width - 1)))))
		py = max(0, min(height - 1, int(round((y - image.y0) / (image.y1 - image.y0) * (height - 1)))))
		offset = (py * width + px) * channels
		if color_type == 0:
			value = pixels[offset] / 255.0
			return (value, value, value)
		if color_type == 2:
			return tuple(value / 255.0 for value in pixels[offset : offset + 3])
		if color_type == 4:
			gray, alpha = pixels[offset : offset + 2]
			alpha_value = alpha / 255.0
			value = (gray / 255.0) * alpha_value + (1.0 - alpha_value)
			return (value, value, value)
		if color_type == 6:
			r, g, b, alpha = pixels[offset : offset + 4]
			alpha_value = alpha / 255.0
			return tuple((value / 255.0) * alpha_value + (1.0 - alpha_value) for value in (r, g, b))
		return None


class ContentInterpreter:
	def __init__(
		self,
		conv: Converter,
		page: int,
		page_height: float,
		fonts: Dict[str, Font],
		xobjects: Dict[str, Stream],
		extgstates: Optional[Dict[str, PdfObj]] = None,
		properties: Optional[Dict[str, PdfObj]] = None,
	):
		self.conv = conv
		self.page = page
		self.page_height = page_height
		self.fonts = fonts
		self.xobjects = xobjects
		self.extgstates = dict(extgstates or {})
		self.properties = dict(properties or {})
		self.ctm = identity()
		self.stack: List[Tuple[Any, ...]] = []
		self.tm = identity()
		self.tlm = identity()
		self.font = next(iter(fonts.values()), Font("F1", "Helvetica"))
		self.font_size = 12.0
		self.leading = 14.4
		self.hscale = 1.0
		self.char_space = 0.0
		self.word_space = 0.0
		self.rise = 0.0
		self.render_mode = 0
		self.line_width = 1.0
		self.fill_rgb = (0.0, 0.0, 0.0)
		self.stroke_rgb = (0.0, 0.0, 0.0)
		self.fill_cs = "DeviceGray"
		self.stroke_cs = "DeviceGray"
		self.fill_alpha = 1.0
		self.stroke_alpha = 1.0
		self.clip_bbox: Optional[Tuple[float, float, float, float]] = None
		self.pending_clip_bbox: Optional[Tuple[float, float, float, float]] = None
		self.path: List[Tuple[str, Tuple[float, ...]]] = []
		self.marked_content: List[Dict[str, Any]] = []
		self.page_char_count = sum(1 for char in conv.chars if char.page == page)
		self.page_segment_count = sum(1 for seg in conv.segments if seg.page == page)
		self.page_fill_count = sum(1 for fill in conv.fills if fill.page == page)
		self._limit_warnings: set[str] = set()
		self.invisible_count = 0
		self.depth = 0
		self.form_stack: Tuple[Any, ...] = ()
		

	def run(self, data: bytes) -> None:
		from .content.tokens import InlineImageToken

		operands: List[Any] = []
		for tok in content_tokens(data):
			if isinstance(tok, InlineImageToken):
				self._do_inline_image(tok.attrs, tok.data)
				operands = []
			elif isinstance(tok, Operator):
				self._op(tok.name, operands)
				operands = []
			else:
				operands.append(tok)
		if self.invisible_count:
			self.conv.doc.warn("INVISIBLE_TEXT", "%d glyphs" % self.invisible_count, self.page)
				

	def _op(self, op: str, a: List[Any]) -> None:
		from .content.runtime import handle_operator

		if handle_operator(self, op, a):
			return
		try:
			if op == "q":
				self.stack.append(
					(
						self.ctm,
						self.font,
						self.font_size,
						self.leading,
						self.hscale,
						self.char_space,
						self.word_space,
						self.rise,
						self.render_mode,
						self.line_width,
						self.fill_rgb,
						self.stroke_rgb,
						self.fill_cs,
						self.stroke_cs,
						self.fill_alpha,
						self.stroke_alpha,
						self.clip_bbox,
					)
				)
			elif op == "Q" and self.stack:
				(
					self.ctm,
					self.font,
					self.font_size,
					self.leading,
					self.hscale,
					self.char_space,
					self.word_space,
					self.rise,
					self.render_mode,
					self.line_width,
					self.fill_rgb,
					self.stroke_rgb,
					self.fill_cs,
					self.stroke_cs,
					self.fill_alpha,
					self.stroke_alpha,
					self.clip_bbox,
				) = self.stack.pop()
			elif op == "cm" and len(a) >= 6:
				self.ctm = mat_mul(self.ctm, tuple(float(x) for x in a[-6:]))
			elif op == "BT":
				self.tm = identity()
				self.tlm = identity()
			elif op == "ET":
				pass
			elif op == "Tf" and len(a) >= 2:
				name = str(a[-2])
				self.font = self.fonts.get(name, self.font)
				self.font_size = float(a[-1])
			elif op == "TL" and a:
				self.leading = float(a[-1])
			elif op == "Tc" and a:
				self.char_space = float(a[-1])
			elif op == "Tw" and a:
				self.word_space = float(a[-1])
			elif op == "Tz" and a:
				self.hscale = float(a[-1]) / 100.0
			elif op == "Ts" and a:
				self.rise = float(a[-1])
			elif op == "Tr" and a:
				self.render_mode = int(a[-1])
			elif op == "gs" and a:
				state = self.extgstates.get(str(a[-1]))
				if isinstance(state, dict):
					fill_alpha = self.conv.doc.resolve_number(state.get("ca"))
					stroke_alpha = self.conv.doc.resolve_number(state.get("CA"))
					if fill_alpha is not None:
						self.fill_alpha = max(0.0, min(1.0, fill_alpha))
					if stroke_alpha is not None:
						self.stroke_alpha = max(0.0, min(1.0, stroke_alpha))
			elif op == "Td" and len(a) >= 2:
				self.tlm = mat_mul(self.tlm, translate(float(a[-2]), float(a[-1])))
				self.tm = self.tlm
			elif op == "TD" and len(a) >= 2:
				self.leading = -float(a[-1])
				self.tlm = mat_mul(self.tlm, translate(float(a[-2]), float(a[-1])))
				self.tm = self.tlm
			elif op == "Tm" and len(a) >= 6:
				self.tm = tuple(float(x) for x in a[-6:])
				self.tlm = self.tm
			elif op == "T*":
				self.tlm = mat_mul(self.tlm, translate(0, -self.leading))
				self.tm = self.tlm
			elif op == "Tj" and a:
				self._show(a[-1])
			elif op == "TJ" and a and isinstance(a[-1], list):
				for item in a[-1]:
					if isinstance(item, (int, float)):
						adjustment = (-float(item) / 1000.0) * self.font_size
						if self.font.vertical:
							self.tm = mat_mul(self.tm, translate(0, adjustment))
						else:
							self.tm = mat_mul(self.tm, translate(adjustment * self.hscale, 0))
					else:
						self._show(item)
			elif op == "'":
				self._op("T*", [])
				if a:
					self._show(a[-1])
			elif op == '"' and len(a) >= 3:
				self.word_space = float(a[-3])
				self.char_space = float(a[-2])
				self._op("'", [a[-1]])
			elif op in ("rg", "RG") and len(a) >= 3:
				rgb = (float(a[-3]), float(a[-2]), float(a[-1]))
				if op == "rg":
					self.fill_rgb = rgb
				else:
					self.stroke_rgb = rgb
			elif op in ("g", "G") and a:
				v = float(a[-1])
				if op == "g":
					self.fill_rgb = (v, v, v)
				else:
					self.stroke_rgb = (v, v, v)
			elif op == "w" and a:
				self.line_width = float(a[-1])
			elif op == "m" and len(a) >= 2:
				self.path.append(("m", (float(a[-2]), float(a[-1]))))
			elif op == "l" and len(a) >= 2:
				self.path.append(("l", (float(a[-2]), float(a[-1]))))
			elif op == "c" and len(a) >= 6:
				self.path.append(("c", tuple(float(value) for value in a[-6:])))
			elif op in ("v", "y") and len(a) >= 4:
				self.path.append((op, tuple(float(value) for value in a[-4:])))
			elif op == "re" and len(a) >= 4:
				x, y, w, h = [float(v) for v in a[-4:]]
				self.path.append(("re", (x, y, w, h)))
			elif op in ("W", "W*"):
				self.pending_clip_bbox = self._path_bbox()
			elif op in ("S", "s"):
				self._stroke_path()
				self._commit_clip()
				self.path = []
			elif op in ("f", "F", "f*"):
				self._record_filled_path("evenodd" if op == "f*" else "nonzero")
				self._fill_path()
				self._commit_clip()
				self.path = []
			elif op == "n":
				self._commit_clip()
				self.path = []
			elif op == "Do" and a:
				self._do_xobject(str(a[-1]))
			elif op == "BMC":
				self.marked_content.append({"actual_text": None, "emitted": True})
			elif op == "BDC":
				props = a[-1] if a and isinstance(a[-1], dict) else {}
				self.marked_content.append({"actual_text": actual_text_from_props(props), "emitted": False})
			elif op == "EMC":
				if self.marked_content:
					self.marked_content.pop()
		except Exception as exc:
			self.conv.doc.warn("CONTENT_OP_FAILED", "%s: %s" % (op, exc), self.page)

	def _show(self, obj: Any) -> None:
		from .content.runtime import show_text

		show_text(self, obj)

	def _path_bbox(self) -> Optional[Tuple[float, float, float, float]]:
		points: List[Tuple[float, float]] = []
		for kind, values in self.path:
			if kind in ("m", "l"):
				points.append(apply_mat(self.ctm, values[0], values[1]))
			elif kind == "c":
				for index in (0, 2, 4):
					points.append(apply_mat(self.ctm, values[index], values[index + 1]))
			elif kind in ("v", "y"):
				for index in (0, 2):
					points.append(apply_mat(self.ctm, values[index], values[index + 1]))
			elif kind == "re":
				x, y, width, height = values
				for px, py in ((x, y), (x + width, y), (x + width, y + height), (x, y + height)):
					points.append(apply_mat(self.ctm, px, py))
		if not points:
			return None
		xs = [point[0] for point in points]
		ys = [self.page_height - point[1] for point in points]
		return (min(xs), min(ys), max(xs), max(ys))

	def _record_filled_path(self, fill_rule: str = "nonzero") -> None:
		"""Retain visible fill geometry for formula/vector loss fallbacks."""
		if not self.path or self._is_artifact() or self.fill_alpha <= 0.001:
			return
		count = self.conv.paint_path_counts.get(self.page, 0)
		if count >= limits.MAX_FILLS_PER_PAGE:
			if "painted_path" not in self._limit_warnings:
				self.conv.doc.warn("PAGE_TRUNCATED", "painted path limit", self.page)
				self._limit_warnings.add("painted_path")
			return

		def point(x: float, y: float) -> Tuple[float, float]:
			px, py = apply_mat(self.ctm, x, y)
			return (px, self.page_height - py)

		commands: List[Tuple[str, Tuple[float, ...]]] = []
		current: Optional[Tuple[float, float]] = None
		for kind, values in self.path:
			if kind in ("m", "l"):
				current = point(values[0], values[1])
				commands.append((kind.upper(), current))
			elif kind == "c":
				control1 = point(values[0], values[1])
				control2 = point(values[2], values[3])
				end = point(values[4], values[5])
				commands.append(("C", control1 + control2 + end))
				current = end
			elif kind == "v" and current is not None:
				control2 = point(values[0], values[1])
				end = point(values[2], values[3])
				commands.append(("C", current + control2 + end))
				current = end
			elif kind == "y" and current is not None:
				control1 = point(values[0], values[1])
				end = point(values[2], values[3])
				commands.append(("C", control1 + end + end))
				current = end
			elif kind == "re":
				x, y, width, height = values
				corners = [
					point(x, y),
					point(x + width, y),
					point(x + width, y + height),
					point(x, y + height),
				]
				commands.extend(
					[("M", corners[0])]
					+ [("L", corner) for corner in corners[1:]]
					+ [("Z", ())]
				)
				current = corners[0]
		if not commands:
			return
		coordinates = [
			(values[index], values[index + 1])
			for command, values in commands
			if command != "Z"
			for index in range(0, len(values), 2)
		]
		if not coordinates:
			return
		xs = [value[0] for value in coordinates]
		ys = [value[1] for value in coordinates]
		bbox = (min(xs), min(ys), max(xs), max(ys))
		visible = intersect_rects(bbox, self.clip_bbox)
		if visible is None or visible[2] <= visible[0] or visible[3] <= visible[1]:
			return
		tags = tuple(
			str(mark.get("tag") or "").lstrip("/")
			for mark in self.marked_content
			if mark.get("tag")
		)
		actual_text = tuple(
			dict.fromkeys(
				text
				for mark in self.marked_content
				if mark.get("actual_text") is not None
				for text in [sanitize_decoded_text(str(mark.get("actual_text"))).strip()]
				if text
			)
		)
		self.conv.paint_seq += 1
		self.conv.painted_paths.append(
			PaintedPath(
				page=self.page,
				seq=self.conv.paint_seq,
				commands=tuple(commands),
				bbox=visible,
				color=self.fill_rgb,
				fill_rule=fill_rule,
				tags=tags,
				actual_text=actual_text,
			)
		)
		self.conv.paint_path_counts[self.page] = count + 1

	def _commit_clip(self) -> None:
		if self.pending_clip_bbox is None:
			return
		self.clip_bbox = intersect_rects(self.clip_bbox, self.pending_clip_bbox)
		self.pending_clip_bbox = None

	def _is_artifact(self) -> bool:
		return any(str(mark.get("tag") or "").lstrip("/") == "Artifact" for mark in self.marked_content)

	def _stroke_path(self) -> None:
		if self._is_artifact() or self.stroke_alpha <= 0.001:
			return
		cur: Optional[Tuple[float, float]] = None
		for kind, vals in self.path:
			if kind == "m":
				cur = (vals[0], vals[1])
			elif kind == "l" and cur:
				if self.page_segment_count >= limits.MAX_SEGMENTS_PER_PAGE:
					if "segment" not in self._limit_warnings:
						self.conv.doc.warn("PAGE_TRUNCATED", "segment limit", self.page)
						self._limit_warnings.add("segment")
					return
				p0 = apply_mat(self.ctm, cur[0], cur[1])
				p1 = apply_mat(self.ctm, vals[0], vals[1])
				bbox = (
					min(p0[0], p1[0]),
					min(self.page_height - p0[1], self.page_height - p1[1]),
					max(p0[0], p1[0]),
					max(self.page_height - p0[1], self.page_height - p1[1]),
				)
				if self.clip_bbox is not None and not stroked_bbox_intersects_clip(
					bbox,
					self.line_width,
					self.clip_bbox,
				):
					cur = (vals[0], vals[1])
					continue
				self.conv.seq += 1
				self.conv.segments.append(
					Segment(
						p0[0],
						self.page_height - p0[1],
						p1[0],
						self.page_height - p1[1],
						self.line_width,
						self.page,
						self.conv.seq,
						color=self.stroke_rgb,
					)
				)
				self.page_segment_count += 1
				cur = (vals[0], vals[1])
			elif kind == "re":
				x, y, w, h = vals
				pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]
				for p, q in zip(pts, pts[1:]):
					if self.page_segment_count >= limits.MAX_SEGMENTS_PER_PAGE:
						if "segment" not in self._limit_warnings:
							self.conv.doc.warn("PAGE_TRUNCATED", "segment limit", self.page)
							self._limit_warnings.add("segment")
						return
					p0 = apply_mat(self.ctm, p[0], p[1])
					p1 = apply_mat(self.ctm, q[0], q[1])
					bbox = (
						min(p0[0], p1[0]),
						min(self.page_height - p0[1], self.page_height - p1[1]),
						max(p0[0], p1[0]),
						max(self.page_height - p0[1], self.page_height - p1[1]),
					)
					if self.clip_bbox is not None and not stroked_bbox_intersects_clip(
						bbox,
						self.line_width,
						self.clip_bbox,
					):
						continue
					self.conv.seq += 1
					self.conv.segments.append(
						Segment(
							p0[0],
							self.page_height - p0[1],
							p1[0],
							self.page_height - p1[1],
							self.line_width,
							self.page,
							self.conv.seq,
							color=self.stroke_rgb,
						)
					)
					self.page_segment_count += 1

	def _fill_path(self) -> None:
		if self._is_artifact() or self.fill_alpha <= 0.001:
			return
		for kind, vals in self.path:
			if kind != "re":
				continue
			if self.page_fill_count >= limits.MAX_FILLS_PER_PAGE:
				if "fill" not in self._limit_warnings:
					self.conv.doc.warn("PAGE_TRUNCATED", "fill limit", self.page)
					self._limit_warnings.add("fill")
				return
			x, y, w, h = vals
			p0 = apply_mat(self.ctm, x, y)
			p1 = apply_mat(self.ctm, x + w, y + h)
			x0, x1 = sorted((p0[0], p1[0]))
			y0, y1 = sorted((self.page_height - p0[1], self.page_height - p1[1]))
			visible = intersect_rects((x0, y0, x1, y1), self.clip_bbox)
			if visible is None or visible[2] <= visible[0] or visible[3] <= visible[1]:
				continue
			x0, y0, x1, y1 = visible
			self.conv.seq += 1
			self.conv.fills.append(
				Fill(
					x0,
					y0,
					x1,
					y1,
					self.fill_rgb,
					self.page,
					self.conv.seq,
					clip_bbox=self.clip_bbox,
				)
			)
			self.page_fill_count += 1
			if abs(y1 - y0) <= 3.5:
				cy = (y0 + y1) / 2
				self.conv.segments.append(
					Segment(
						x0,
						cy,
						x1,
						cy,
						max(abs(y1 - y0), 0.5),
						self.page,
						self.conv.seq,
						fill=True,
						color=self.fill_rgb,
					)
				)
				self.page_segment_count += 1
			elif abs(x1 - x0) <= 3.5:
				cx = (x0 + x1) / 2
				self.conv.segments.append(
					Segment(
						cx,
						y0,
						cx,
						y1,
						max(abs(x1 - x0), 0.5),
						self.page,
						self.conv.seq,
						fill=True,
						color=self.fill_rgb,
					)
				)
				self.page_segment_count += 1

	def _do_xobject(self, name: str) -> None:
		xo = self.xobjects.get(name)
		if not xo:
			return
		from .content.forms import execute_form_xobject

		if execute_form_xobject(self, name, xo):
			return
		subtype = str(self.conv.doc.resolve(xo.attrs.get("Subtype")))
		if subtype == "Image":
			self._register_image_stream(xo)

	def _do_inline_image(self, attrs: Dict[str, PdfObj], raw: bytes) -> None:
		aliases = {
			"W": "Width", "H": "Height", "BPC": "BitsPerComponent",
			"CS": "ColorSpace", "F": "Filter", "DP": "DecodeParms",
			"D": "Decode", "IM": "ImageMask", "I": "Interpolate",
		}
		color_aliases = {"G": "DeviceGray", "RGB": "DeviceRGB", "CMYK": "DeviceCMYK", "I": "Indexed"}
		filter_aliases = {
			"AHx": "ASCIIHexDecode", "A85": "ASCII85Decode", "LZW": "LZWDecode",
			"Fl": "FlateDecode", "RL": "RunLengthDecode", "CCF": "CCITTFaxDecode",
			"DCT": "DCTDecode",
		}
		normalized: Dict[str, PdfObj] = {}
		for key, value in attrs.items():
			name = aliases.get(str(key), str(key))
			if name == "ColorSpace" and isinstance(value, str):
				value = color_aliases.get(value.lstrip("/"), value)
			if name == "Filter":
				if isinstance(value, list):
					value = [filter_aliases.get(str(item).lstrip("/"), item) for item in value]
				elif isinstance(value, str):
					value = filter_aliases.get(value.lstrip("/"), value)
			normalized[name] = value
		self._register_image_stream(Stream(normalized, raw), inline=True)

	def _register_image_stream(self, image: Stream, inline: bool = False) -> None:
		if self._is_artifact():
			return
		if self.conv.doc.resolve(image.attrs.get("ImageMask")) is True:
			self.conv.doc.warn("IMAGE_UNSUPPORTED", "image mask", self.page)
			self.conv.degraded_pages.add(self.page)
			return
		data = self.conv.doc.decoded_stream(image)
		filters = self.conv.doc.resolve(image.attrs.get("Filter"))
		if isinstance(filters, list):
			filter_names = [str(self.conv.doc.resolve(value)).lstrip("/") for value in filters]
		elif filters is None:
			filter_names = []
		else:
			filter_names = [str(filters).lstrip("/")]
		final_filter = filter_names[-1] if filter_names else ""
		width = self.conv.doc.resolve_number(image.attrs.get("Width"))
		height = self.conv.doc.resolve_number(image.attrs.get("Height"))
		bpc = self.conv.doc.resolve_number(image.attrs.get("BitsPerComponent"), 8)
		colorspace = resolve_image_colorspace(self.conv.doc, image.attrs.get("ColorSpace"))
		if width is None or height is None or width <= 0 or height <= 0:
			self.conv.doc.warn("IMAGE_UNSUPPORTED", "invalid dimensions", self.page)
			self.conv.degraded_pages.add(self.page)
			return
		w, h = int(width), int(height)
		if w > limits.MAX_IMAGE_DIMENSION or h > limits.MAX_IMAGE_DIMENSION or w * h > limits.MAX_IMAGE_PIXELS:
			self.conv.doc.warn("IMAGE_UNSUPPORTED", "image dimensions exceed policy limit", self.page)
			self.conv.degraded_pages.add(self.page)
			return
		payload = data
		if final_filter in ("DCTDecode", "DCT"):
			if not payload.startswith(b"\xff\xd8"):
				self.conv.doc.warn("IMAGE_UNSUPPORTED", "invalid JPEG payload", self.page)
				self.conv.degraded_pages.add(self.page)
				return
			ext = ".jpg"
			if image.attrs.get("SMask") is not None or image.attrs.get("Mask") is not None:
				self.conv.doc.warn("IMAGE_MASK_IGNORED", "JPEG mask requires image transcoding", self.page)
		elif final_filter in ("JPXDecode", "JPX"):
			if not (payload.startswith(b"\x00\x00\x00\x0cjP  \r\n\x87\n") or payload.startswith(b"\xffO\xffQ")):
				self.conv.doc.warn("IMAGE_UNSUPPORTED", "invalid JPEG 2000 payload", self.page)
				self.conv.degraded_pages.add(self.page)
				return
			ext = ".jp2"
		elif int(bpc or 0) in (1, 2, 4, 8, 16) and colorspace in ("DeviceGray", "DeviceRGB", "DeviceCMYK"):
			channels = {"DeviceGray": 1, "DeviceRGB": 3, "DeviceCMYK": 4}[colorspace]
			raster = expand_image_samples(payload, w, h, channels, int(bpc or 0))
			if raster is None:
				self.conv.doc.warn("IMAGE_UNSUPPORTED", "short raster payload", self.page)
				self.conv.degraded_pages.add(self.page)
				return
			raster = apply_image_decode_8(self.conv.doc, raster, channels, image.attrs.get("Decode"))
			alpha = None if inline else image_soft_mask_alpha(self.conv.doc, image, w, h, self.page)
			if image.attrs.get("Mask") is not None and not isinstance(self.conv.doc.resolve(image.attrs.get("Mask")), Stream):
				self.conv.doc.warn("IMAGE_MASK_IGNORED", "color-key mask is not yet applied", self.page)
			payload = png_from_raw(raster, w, h, colorspace, alpha=alpha)
			ext = ".png"
		else:
			self.conv.doc.warn("IMAGE_UNSUPPORTED", "%s bpc=%s filters=%s" % (colorspace, bpc, filter_names), self.page)
			self.conv.degraded_pages.add(self.page)
			return
		pdf_points = [
			apply_mat(self.ctm, 0, 0), apply_mat(self.ctm, 1, 0),
			apply_mat(self.ctm, 1, 1), apply_mat(self.ctm, 0, 1),
		]
		points = [(point[0], self.page_height - point[1]) for point in pdf_points]
		x_values = [point[0] for point in points]
		y_values = [point[1] for point in points]
		bbox = (min(x_values), min(y_values), max(x_values), max(y_values))
		if self.clip_bbox is not None and not rects_intersect(bbox, self.clip_bbox):
			return
		asset_name = "img-%s%s" % (hashlib.sha256(payload).hexdigest()[:16], ext)
		self.conv.assets[asset_name] = payload
		placed_width = math.hypot(points[1][0] - points[0][0], points[1][1] - points[0][1])
		placed_height = math.hypot(points[3][0] - points[0][0], points[3][1] - points[0][1])
		self.conv.seq += 1
		marked_mcids = tuple(sorted({
			int(mark.get("mcid")) for mark in self.marked_content
			if isinstance(mark, dict) and isinstance(mark.get("mcid"), int) and not isinstance(mark.get("mcid"), bool)
		}))
		marked_tags = tuple(str(mark.get("tag") or "").lstrip("/") for mark in self.marked_content if isinstance(mark, dict) and mark.get("tag"))
		object_ref = "%d 0 R" % image.objnum if image.objnum is not None else None
		self.conv.images.append(ImageItem(
			x0=bbox[0], y0=bbox[1], x1=bbox[2], y1=bbox[3],
			page=self.page, seq=self.conv.seq, name=asset_name, data=payload,
			intrinsic_width=w, intrinsic_height=h,
			placed_width=placed_width, placed_height=placed_height,
			quad=tuple(points), mcids=marked_mcids, tags=marked_tags, object_ref=object_ref,
		))


class Operator:
	def __init__(self, name: str):
		self.name = name

	def __repr__(self) -> str:
		return "Operator(%r)" % self.name


def content_tokens(data: bytes) -> Iterable[Any]:
	from .content.tokens import tokenize_content

	yield from tokenize_content(data)


def parse_content_array(lex: PdfLexer) -> List[Any]:
	arr: List[Any] = []
	while True:
		tok = lex.next_token()
		if tok is None or tok[0] == "ARR_CLOSE":
			break
		if tok[0] == "ARR_OPEN":
			arr.append(parse_content_array(lex))
		elif tok[0] == "DICT_OPEN":
			arr.append(parse_content_dict(lex))
		elif tok[0] == "NAME":
			arr.append(str(tok[1]))
		else:
			arr.append(tok[1])
	return arr


def parse_content_dict(lex: PdfLexer) -> Dict[str, Any]:
	out: Dict[str, Any] = {}
	while True:
		tok = lex.next_token()
		if tok is None or tok[0] == "DICT_CLOSE":
			break
		if tok[0] != "NAME":
			continue
		key = str(tok[1])
		value = parse_content_value(lex)
		out[key] = value
	return out


def parse_content_value(lex: PdfLexer) -> Any:
	tok = lex.next_token()
	if tok is None:
		return None
	kind, value, _s, _e = tok
	if kind == "ARR_OPEN":
		return parse_content_array(lex)
	if kind == "DICT_OPEN":
		return parse_content_dict(lex)
	if kind == "NAME":
		return str(value)
	return value


class MarkdownRenderer:
	def __init__(self, conv: Converter):
		self.conv = conv
		self.lines_by_page: Dict[int, List[Line]] = {}
		self.block_events_by_page: Dict[int, List[BlockEvent]] = {}
		self._prepared = False
		self._analyzed = False
		self._body_size_cache: Dict[Tuple[int, ...], float] = {}
		self._text_frame_cache: Dict[int, Tuple[float, float]] = {}
		self._available_width_cache: Dict[int, float] = {}
		self._inferred_column_bands: Dict[int, List[Tuple[float, float, float]]] = {}
		self._table_cache: Dict[
			int,
			List[Tuple[float, str, List[Line], Tuple[float, float, float, float]]],
		] = {}
		self._vector_boxes: Dict[
			int,
			List[Tuple[float, float, float, float]],
		] = {}
		self._vector_line_ids: Dict[int, set[int]] = {}
		self._panel_label_cache: Dict[int, set[int]] = {}
		self._code_region_cache: Dict[int, Optional[Tuple[float, float, float, float]]] = {}
		self._code_border_boxes_cache: Dict[int, List[Tuple[float, float, float, float]]] = {}
		self._quote_bars_cache: Dict[int, Tuple[Segment, ...]] = {}
		self._visual_marker_cache: Dict[int, Optional[VisualListMarker]] = {}

	def analyze(self) -> Dict[int, List[BlockEvent]]:
		if self._analyzed:
			return self.block_events_by_page
		if not self._prepared:
			self._build_lines()
			self._stitch_page_boundary_rotated_text()
			self._remove_furniture()
			self._materialize_vector_figures()
			self._materialize_formula_figures()
			self._prepared = True
		self._body_size_cache.clear()
		self._text_frame_cache.clear()
		self._available_width_cache.clear()
		self.block_events_by_page = {}
		for page in sorted(self.conv.page_sizes):
			self._render_page(page)
		self._analyzed = True
		return self.block_events_by_page

	def _body_font_size(self, lines: Sequence[Line]) -> float:
		key = tuple(id(line) for line in lines)
		cached = self._body_size_cache.get(key)
		if cached is None:
			cached = body_font_size(list(lines))
			self._body_size_cache[key] = cached
		return cached

	def render(self) -> str:
		self.analyze()
		blocks: List[str] = []
		render_pages = set(self.conv.page_sizes)
		previous_page: Optional[int] = None
		for page in sorted(render_pages):
			page_events = self.block_events_by_page.get(page, [])
			page_blocks: List[str] = []
			previous_event: Optional[BlockEvent] = None
			for event in page_events:
				# The physical-line collector can emit adjacent list fragments when
				# nesting changes the marker family (for example ol -> ul -> ol).
				# They remain one Markdown list and therefore need a single newline,
				# not the blank line used between independent block events.
				previous_indent = previous_event.lines[-1].x0 if previous_event is not None and previous_event.lines else None
				current_indent = event.lines[0].x0 if event.lines else None
				indent_delta = (
					abs(current_indent - previous_indent)
					if current_indent is not None and previous_indent is not None
					else 0.0
				)
				indent_tolerance = max(event.lines[0].size * 0.75, 6.0) if event.lines else 6.0
				if (
					previous_event is not None
					and previous_event.kind == event.kind == "list"
					and page_blocks
					and (
						previous_event.attrs.get("ordered") == event.attrs.get("ordered")
						or indent_delta > indent_tolerance
					)
				):
					page_blocks[-1] += "\n" + event.legacy_markdown
				else:
					page_blocks.append(event.legacy_markdown)
				previous_event = event
			if previous_page is not None and blocks and page_events:
				previous_events = self.block_events_by_page.get(previous_page, [])
				if previous_events and self._is_cross_page_table_caption(previous_events[-1], page_events[0]):
					caption_event = previous_events[-1]
					caption = plain_text(line_text_tokens(caption_event.lines[0])).strip()
					blocks.pop()
					page_blocks[0] = page_blocks[0].replace(
						"<table>",
						"<table>\n<caption>%s</caption>" % escape_html(caption),
						1,
					)
					caption_event.attrs["merged_into_table"] = True
					page_events[0].attrs["cross_page_caption"] = caption
					page_events[0].legacy_markdown = page_blocks[0]
			if blocks and page_blocks and not self.conv.options.page_breaks:
				continued_table = merge_gfm_table_blocks(blocks[-1], page_blocks[0])
				if continued_table is not None:
					# A repeated, byte-identical header at a physical page
					# boundary is strong continuation evidence. Keep one valid GFM
					# table and consume the producer-repeated header.
					blocks[-1] = continued_table
					page_blocks = page_blocks[1:]
				elif previous_page is not None and self._page_boundary_continuation(previous_page, page):
					continued_paragraph = merge_page_boundary_paragraph_blocks(blocks[-1], page_blocks[0])
					if continued_paragraph is not None:
						blocks[-1] = continued_paragraph
						page_blocks = page_blocks[1:]
			if self.conv.options.page_breaks and blocks and page_blocks:
				blocks.append("---")
				blocks.append("<!-- page %d -->" % page)
			blocks.extend(page_blocks)
			previous_page = page
		if not blocks and self.conv.doc.pages():
			if any(img.page for img in self.conv.images):
				pass
			else:
				self.conv.doc.warn("NO_TEXT", "no visible text")
		md = "\n\n".join(b for b in blocks if b is not None and b != "")
		return md.rstrip() + ("\n" if md else "")

	def _is_cross_page_table_caption(self, previous: BlockEvent, current: BlockEvent) -> bool:
		if previous.kind != "paragraph" or current.kind != "table":
			return False
		if len(previous.lines) != 1 or not current.legacy_markdown.startswith("<table>\n"):
			return False
		if current.legacy_markdown.startswith("<table>\n<caption>"):
			return False
		line = previous.lines[0]
		if current.page != line.page + 1:
			return False
		_previous_width, previous_height = self.conv.page_sizes.get(line.page, (612.0, 792.0))
		_current_width, current_height = self.conv.page_sizes.get(current.page, (612.0, 792.0))
		bbox = current.attrs.get("bbox")
		if not isinstance(bbox, tuple) or len(bbox) != 4:
			return False
		text = plain_text(line_text_tokens(line)).strip()
		return bool(
			previous.legacy_markdown.startswith('<p align="center">')
			and text
			and not text.endswith(":")
			and line.y1 >= previous_height * 0.85
			and float(bbox[1]) <= current_height * 0.15
		)

	def _event(
		self,
		page: int,
		rank: float,
		kind: str,
		legacy_markdown: str,
		lines: Optional[Sequence[Line]] = None,
		attrs: Optional[Dict[str, Any]] = None,
	) -> Tuple[float, str]:
		event = BlockEvent(
			page=page,
			rank=float(rank),
			kind=kind,
			lines=list(lines or []),
			attrs=dict(attrs or {}),
			legacy_markdown=legacy_markdown,
		)
		self.block_events_by_page.setdefault(page, []).append(event)
		return (event.rank, legacy_markdown)

	def _page_boundary_continuation(self, previous_page: int, current_page: int) -> bool:
		previous_lines = [
			line
			for line in self.lines_by_page.get(previous_page, [])
			if plain_text(line_text_tokens(line)).strip()
		]
		current_lines = [
			line
			for line in self.lines_by_page.get(current_page, [])
			if plain_text(line_text_tokens(line)).strip()
		]
		if not previous_lines or not current_lines:
			return False
		left = previous_lines[-1]
		right = current_lines[0]
		_previous_width, previous_height = self.conv.page_sizes.get(previous_page, (612.0, 792.0))
		_current_width, current_height = self.conv.page_sizes.get(current_page, (612.0, 792.0))
		if left.writing_mode != "horizontal" or right.writing_mode != "horizontal":
			return False
		if left.y1 < previous_height * 0.72 or right.y0 > current_height * 0.28:
			return False
		if abs(left.x0 - right.x0) > max(8.0, left.size * 1.25):
			return False
		if abs(left.size - right.size) > max(1.0, left.size * 0.12):
			return False
		return True

	def _materialize_vector_figures(self) -> None:
		from .assets.vector import detect_vector_figures

		table_boxes = {
			page: [candidate[3] for candidate in self._table_candidates(page)]
			for page in self.conv.page_sizes
		}
		for figure in detect_vector_figures(
			self.conv,
			self.lines_by_page,
			table_boxes,
		):
			self.conv.assets[figure.name] = figure.data
			x0, y0, x1, y1 = figure.bbox
			image = ImageItem(
				x0=x0,
				y0=y0,
				x1=x1,
				y1=y1,
				page=figure.page,
				seq=figure.seq,
				name=figure.name,
				data=figure.data,
				intrinsic_width=max(1, int(round(x1 - x0))),
				intrinsic_height=max(1, int(round(y1 - y0))),
				placed_width=max(1.0, x1 - x0),
				placed_height=max(1.0, y1 - y0),
				quad=((x0, y0), (x1, y0), (x1, y1), (x0, y1)),
				kind="vector",
			)
			cx = (x0 + x1) / 2
			cy = (y0 + y1) / 2
			for link in self.conv.links:
				if link.page != figure.page:
					continue
				lx0, ly0, lx1, ly1 = link.rect
				if lx0 <= cx <= lx1 and ly0 <= cy <= ly1:
					image.link = link.uri or link.dest
					image.link_object_ref = link.object_ref
					break
			self.conv.images.append(image)
			self._vector_boxes.setdefault(figure.page, []).append(figure.bbox)
			self._vector_line_ids.setdefault(figure.page, set()).update(
				figure.line_ids
			)
			self.conv.doc.warn(
				"VECTOR_FIGURE_APPROXIMATE",
				"supported PDF fills, line segments, filled paths, and text replayed as one SVG; original authoring vector unavailable",
				figure.page,
			)

	def _materialize_formula_figures(self) -> None:
		from .assets.formula_svg import detect_formula_figures

		excluded_boxes = {
			page: list(self._vector_boxes.get(page, []))
			+ [candidate[3] for candidate in self._table_candidates(page)]
			for page in self.conv.page_sizes
		}
		for formula in detect_formula_figures(
			self.conv,
			self.lines_by_page,
			excluded_boxes,
		):
			self.conv.assets[formula.name] = formula.data
			x0, y0, x1, y1 = formula.bbox
			self.conv.images.append(
				ImageItem(
					x0=x0,
					y0=y0,
					x1=x1,
					y1=y1,
					page=formula.page,
					seq=formula.seq,
					name=formula.name,
					data=formula.data,
					alt=formula.alt,
					intrinsic_width=max(1, int(round(x1 - x0))),
					intrinsic_height=max(1, int(round(y1 - y0))),
					placed_width=max(1.0, x1 - x0),
					placed_height=max(1.0, y1 - y0),
					quad=((x0, y0), (x1, y0), (x1, y1), (x0, y1)),
					kind="formula",
				)
			)
			self.conv.doc.warn(
				"FORMULA_VECTOR_FALLBACK",
				"outline-only formula preserved as SVG; no source text was inferred",
				formula.page,
			)

	def _build_lines(self) -> None:
		chars = [
			c
			for c in self.conv.chars
			if (self.conv.options.keep_invisible or not c.invisible)
			and not c.artifact
			and c.text
		]
		by_page: Dict[int, List[Char]] = {}
		for c in chars:
			by_page.setdefault(c.page, []).append(c)
		for page, items in by_page.items():
			items.sort(key=lambda c: (c.y0, c.x0, c.seq))
			vertical_lines, items = self._extract_vertical_text_lines(page, items)
			rotated_lines, items = self._extract_rotated_text_lines(page, items)
			lines: List[Line] = list(vertical_lines) + list(rotated_lines)
			for ch in items:
				target = None
				for line in reversed(lines[-5:]):
					if abs(((line.y0 + line.y1) / 2) - ((ch.y0 + ch.y1) / 2)) <= max(2.5, ch.size * 0.45):
						target = line
						break
				if target is None:
					lines.append(Line([ch], page, ch.seq))
				else:
					target.chars.append(ch)
					target.invalidate_caches()
			for line in lines:
				line.chars.sort(key=lambda c: (c.x0, c.seq))
			lines = self._merge_inline_shifted_fragments(lines)
			lines.sort(
				key=lambda line: (
					line.y0,
					line.seq if line.writing_mode == "vertical" else line.x0,
					line.seq,
				)
			)
			lines = self._split_lines_on_column_gaps(page, lines)
			lines = self._order_column_bands(page, lines)
			lines = self._order_directional_regions(lines)
			self.lines_by_page[page] = lines

	def _extract_vertical_text_lines(self, page: int, items: List[Char]) -> Tuple[List[Line], List[Char]]:
		eligible = [
			char
			for char in items
			if char.text.strip() and is_east_asian_vertical_text(char.text)
		]
		x_clusters: List[List[Char]] = []
		for char in sorted(eligible, key=lambda item: ((item.x0 + item.x1) / 2, item.y0, item.seq)):
			cx = (char.x0 + char.x1) / 2
			best: Optional[List[Char]] = None
			best_distance = float("inf")
			for cluster in x_clusters:
				cluster_x = median([(candidate.x0 + candidate.x1) / 2 for candidate in cluster])
				distance = abs(cx - cluster_x)
				if distance <= max(2.5, char.size * 0.35) and distance < best_distance:
					best = cluster
					best_distance = distance
			if best is None:
				x_clusters.append([char])
			else:
				best.append(char)

		cohorts: List[List[Char]] = []
		for cluster in x_clusters:
			current: List[Char] = []
			for char in sorted(cluster, key=lambda item: ((item.y0 + item.y1) / 2, item.seq)):
				if current:
					previous = current[-1]
					gap = ((char.y0 + char.y1) - (previous.y0 + previous.y1)) / 2
					if gap > max(previous.size * 1.8, 18.0):
						if self._is_vertical_text_cohort(current):
							cohorts.append(current)
						current = []
				current.append(char)
			if self._is_vertical_text_cohort(current):
				cohorts.append(current)
		if not cohorts:
			return [], items

		# Keep physical columns separate. Traditional vertical layout orders columns
		# from right to left; merging nearby x-cohorts destroys that order and cell
		# provenance in vertically set tables.
		regions = [list(cohort) for cohort in cohorts]
		used = {id(char) for region in regions for char in region}
		vertical_lines = [
			Line(
				sorted(region, key=lambda char: (((char.y0 + char.y1) / 2), char.seq)),
				page,
				min(char.seq for char in region),
				source_order=True,
				writing_mode="vertical",
			)
			for region in sorted(regions, key=lambda group: (-median([(char.x0 + char.x1) / 2 for char in group]), min(char.y0 for char in group)))
		]
		return vertical_lines, [char for char in items if id(char) not in used]

	def _is_vertical_text_cohort(self, chars: List[Char]) -> bool:
		minimum = 2 if chars and all(char.font.vertical for char in chars) else 5
		if len(chars) < minimum:
			return False
		ordered = sorted(chars, key=lambda char: ((char.y0 + char.y1) / 2, char.seq))
		size = median([char.size for char in ordered]) or 1.0
		y_centers = [(char.y0 + char.y1) / 2 for char in ordered]
		x_centers = [(char.x0 + char.x1) / 2 for char in ordered]
		gaps = [right - left for left, right in zip(y_centers, y_centers[1:])]
		seq_steps = [abs(right.seq - left.seq) for left, right in zip(ordered, ordered[1:])]
		vertical_font = all(char.font.vertical for char in chars)
		minimum_span = size * (0.55 if vertical_font else 3.0)
		return bool(
			max(y_centers) - min(y_centers) >= minimum_span
			and max(x_centers) - min(x_centers) <= max(2.5, size * 0.35)
			and size * 0.40 <= median(gaps) <= size * 1.85
			and sum(1 for step in seq_steps if step <= 2) >= math.ceil(len(seq_steps) * 0.80)
		)

	def _extract_rotated_text_lines(self, page: int, items: List[Char]) -> Tuple[List[Line], List[Char]]:
		runs: List[List[Char]] = []
		current: List[Char] = []
		for char in sorted([item for item in items if item.text], key=lambda item: item.seq):
			if current:
				previous = current[-1]
				pcx = (previous.x0 + previous.x1) / 2
				pcy = (previous.y0 + previous.y1) / 2
				cx = (char.x0 + char.x1) / 2
				cy = (char.y0 + char.y1) / 2
				continuous = (
					char.seq - previous.seq <= 2
					and abs(char.size - previous.size) <= max(1.0, previous.size * 0.15)
					and 0 <= cx - pcx <= max(previous.size * 2.0, 20.0)
					and math.hypot(cx - pcx, cy - pcy) <= max(previous.size * 2.0, 20.0)
				)
				if not continuous:
					if self._is_rotated_text_run(current):
						runs.append(current)
					current = []
			current.append(char)
		if self._is_rotated_text_run(current):
			runs.append(current)
		if not runs:
			return [], items
		used = {id(char) for run in runs for char in run}
		return (
			[
				Line(
					sorted(run, key=lambda char: char.seq),
					page,
					min(char.seq for char in run),
					source_order=True,
					writing_mode="rotated",
				)
				for run in runs
			],
			[char for char in items if id(char) not in used],
		)

	def _is_rotated_text_run(self, chars: List[Char]) -> bool:
		if len(chars) < 5:
			return False
		ordered = sorted(chars, key=lambda char: char.seq)
		xs = [(char.x0 + char.x1) / 2 for char in ordered]
		ys = [(char.y0 + char.y1) / 2 for char in ordered]
		size = median([char.size for char in ordered]) or 1.0
		x_span = xs[-1] - xs[0]
		if x_span < size * 3.0 or max(ys) - min(ys) < size * 0.50:
			return False
		slope = (ys[-1] - ys[0]) / x_span
		if not (0.04 <= abs(slope) <= 1.0):
			return False
		intercept = ys[0] - slope * xs[0]
		residuals = [abs(y - (slope * x + intercept)) for x, y in zip(xs, ys)]
		return max(residuals) <= max(2.5, size * 0.35)

	def _order_directional_regions(self, lines: List[Line]) -> List[Line]:
		out = list(lines)
		for line in [candidate for candidate in lines if candidate.writing_mode == "vertical"]:
			line_index = out.index(line)
			move_before: List[int] = []
			for other in out:
				if other is line or other.writing_mode != "rotated" or other.seq <= line.seq or other.x0 < line.x1:
					continue
				pad = max(line.size, other.size) * 0.35
				if min(line.y1, other.y1) + pad >= max(line.y0, other.y0):
					move_before.append(out.index(other))
			if move_before and min(move_before) < line_index:
				out.pop(line_index)
				out.insert(min(move_before), line)
		return out

	def _stitch_page_boundary_rotated_text(self) -> None:
		pages = sorted(self.lines_by_page)
		for previous_page, current_page in zip(pages, pages[1:]):
			if current_page != previous_page + 1:
				continue
			_previous_width, previous_height = self.conv.page_sizes.get(previous_page, (612.0, 792.0))
			_current_width, current_height = self.conv.page_sizes.get(current_page, (612.0, 792.0))
			bottom = [
				line
				for line in self.lines_by_page.get(previous_page, [])
				if line.writing_mode == "rotated" and line.y1 >= previous_height * 0.90
			]
			top = [
				line
				for line in self.lines_by_page.get(current_page, [])
				if line.writing_mode == "rotated" and line.y0 <= current_height * 0.10
			]
			used_top: set[int] = set()
			for lower in bottom:
				matches = [
					candidate
					for candidate in top
					if id(candidate) not in used_top and self._rotated_boundary_match(lower, candidate)
				]
				if not matches:
					continue
				upper = max(matches, key=lambda candidate: self._rotated_boundary_overlap_count(lower, candidate))
				merged = self._merge_rotated_boundary_lines(current_page, lower, upper)
				self.lines_by_page[previous_page] = [
					line for line in self.lines_by_page.get(previous_page, []) if line is not lower
				]
				current_lines = list(self.lines_by_page.get(current_page, []))
				current_lines[current_lines.index(upper)] = merged
				self.lines_by_page[current_page] = current_lines
				self.lines_by_page[current_page] = self._order_directional_regions(self.lines_by_page[current_page])
				used_top.add(id(upper))
				self.conv.doc.warn(
					"ROTATED_TEXT_PAGE_STITCHED",
					"clipped rotated run reconciled with adjacent-page glyph overlap",
					current_page,
				)

	def _rotated_boundary_match(self, lower: Line, upper: Line) -> bool:
		if abs(lower.size - upper.size) > max(1.0, upper.size * 0.15):
			return False
		overlap = min(lower.x1, upper.x1) - max(lower.x0, upper.x0)
		if overlap < min(lower.x1 - lower.x0, upper.x1 - upper.x0) * 0.35:
			return False
		lower_slope = line_baseline_slope(lower)
		upper_slope = line_baseline_slope(upper)
		if lower_slope is None or upper_slope is None or abs(lower_slope - upper_slope) > 0.08:
			return False
		return self._rotated_boundary_overlap_count(lower, upper) >= 2

	def _rotated_boundary_overlap_count(self, lower: Line, upper: Line) -> int:
		tolerance = max(1.0, min(lower.size, upper.size) * 0.14)
		return sum(
			1
			for left in lower.chars
			if any(
				left.text == right.text
				and abs(((left.x0 + left.x1) - (right.x0 + right.x1)) / 2) <= tolerance
				for right in upper.chars
			)
		)

	def _merge_rotated_boundary_lines(self, page: int, lower: Line, upper: Line) -> Line:
		tolerance = max(1.0, min(lower.size, upper.size) * 0.14)
		selected: List[Char] = []
		for char in sorted(upper.chars, key=lambda item: ((item.x0 + item.x1) / 2, item.seq)):
			selected.append(char)
		for char in sorted(lower.chars, key=lambda item: ((item.x0 + item.x1) / 2, item.seq)):
			cx = (char.x0 + char.x1) / 2
			if any(abs(cx - (existing.x0 + existing.x1) / 2) <= tolerance for existing in selected):
				continue
			selected.append(char)
		slope = line_baseline_slope(upper) or 0.0
		upper_centers = [((char.x0 + char.x1) / 2, (char.y0 + char.y1) / 2) for char in upper.chars]
		intercept = median([y - slope * x for x, y in upper_centers]) if upper_centers else upper.y0
		projected: List[Char] = []
		for char in sorted(selected, key=lambda item: ((item.x0 + item.x1) / 2, item.seq)):
			cx = (char.x0 + char.x1) / 2
			cy = slope * cx + intercept
			height = max(0.1, char.y1 - char.y0)
			projected.append(
				replace(
					char,
					y0=cy - height / 2,
					y1=cy + height / 2,
					page=page,
				)
			)
		return Line(projected, page, upper.seq, source_order=False, writing_mode="rotated")

	def _merge_inline_shifted_fragments(self, lines: List[Line]) -> List[Line]:
		if len(lines) < 2:
			return lines
		body = self._body_font_size(lines)
		merged = [False] * len(lines)
		for idx, frag in enumerate(lines):
			text = plain_text(line_text_tokens(frag)).strip()
			if not text or len(text) > 4 or frag.size <= 0 or frag.size > body * 0.92:
				continue
			best: Optional[Tuple[float, int]] = None
			fc = (frag.y0 + frag.y1) / 2
			for j, base in enumerate(lines):
				if j == idx or merged[j] or base.size <= frag.size * 1.05:
					continue
				if not plain_text(line_text_tokens(base)).strip():
					continue
				bc = (base.y0 + base.y1) / 2
				if abs(fc - bc) > max(base.size * 0.95, 10.0):
					continue
				if frag.x1 < base.x0 - base.size or frag.x0 > base.x1 + base.size * 1.4:
					continue
				x_gap = 0.0
				if frag.x1 < base.x0:
					x_gap = base.x0 - frag.x1
				elif frag.x0 > base.x1:
					x_gap = frag.x0 - base.x1
				score = abs(fc - bc) + x_gap * 0.25
				if best is None or score < best[0]:
					best = (score, j)
			if best is not None:
				lines[best[1]].chars.extend(frag.chars)
				lines[best[1]].invalidate_caches()
				merged[idx] = True
		out = [line for idx, line in enumerate(lines) if not merged[idx]]
		for line in out:
			line.chars.sort(key=lambda c: (c.x0, c.seq))
		return out

	def _split_lines_on_column_gaps(self, page: int, lines: List[Line]) -> List[Line]:
		sep_infos = self._column_separator_infos(page)
		if not sep_infos:
			sep_infos = self._inferred_column_separator_infos(page, lines)
			if sep_infos:
				self._inferred_column_bands[page] = sep_infos
		if not sep_infos:
			return lines
		out: List[Line] = []
		for line in lines:
			cy = (line.y0 + line.y1) / 2
			seps = [x for x, y0, y1 in sep_infos if y0 - 3.0 <= cy <= y1 + 3.0]
			if not seps:
				out.append(line)
				continue
			# When two column runs physically overprint, x-gap splitting is
			# impossible. A large source-sequence discontinuity within one
			# visual baseline, together with an active separator and runs on
			# both sides, provides independent evidence for a safe split.
			source_chars = sorted(line.chars, key=lambda c: c.seq)
			source_runs: List[List[Char]] = []
			current_run: List[Char] = []
			previous_seq: Optional[int] = None
			for ch in source_chars:
				if previous_seq is not None and ch.seq - previous_seq > 4 and current_run:
					source_runs.append(current_run)
					current_run = []
				current_run.append(ch)
				previous_seq = ch.seq
			if current_run:
				source_runs.append(current_run)
			sep_center = median(seps)
			left_runs = [run for run in source_runs if median([(c.x0 + c.x1) / 2 for c in run]) < sep_center]
			right_runs = [run for run in source_runs if median([(c.x0 + c.x1) / 2 for c in run]) >= sep_center]
			if len(source_runs) >= 2 and left_runs and right_runs:
				for runs in (left_runs, right_runs):
					group = [ch for run in runs for ch in run]
					out.append(Line(group, page, min(c.seq for c in group)))
				continue
			chars = sorted(line.chars, key=lambda c: (c.x0, c.seq))
			groups: List[List[Char]] = []
			cur: List[Char] = []
			prev: Optional[Char] = None
			for ch in chars:
				if prev is not None:
					gap = ch.x0 - prev.x1
					crosses_column_separator = any(prev.x1 < sep < ch.x0 for sep in seps)
					min_sep_gap = max(8.0, min(line.size * 1.1, 18.0))
					if crosses_column_separator and gap > min_sep_gap:
						if cur:
							groups.append(cur)
						cur = []
				cur.append(ch)
				prev = ch
			if cur:
				groups.append(cur)
			if len(groups) <= 1:
				out.append(line)
			else:
				for group in groups:
					out.append(Line(group, page, min(c.seq for c in group)))
		out.sort(key=lambda l: (l.y0, l.x0, l.seq))
		return out

	def _order_column_bands(self, page: int, lines: List[Line]) -> List[Line]:
		sep_infos = self._column_separator_infos(page)
		if not sep_infos:
			sep_infos = self._inferred_column_bands.get(page) or self._inferred_column_separator_infos(page, lines)
		if not sep_infos:
			return lines
		bands = self._coalesce_column_separator_bands(sep_infos)
		physical = sorted(lines, key=lambda line: (line.y0, line.x0, line.seq))
		emitted: set[int] = set()
		out: List[Line] = []
		for line in physical:
			if id(line) in emitted:
				continue
			cy = (line.y0 + line.y1) / 2
			matching = [band for band in bands if band[1] - 2.0 <= cy <= band[2] + 2.0]
			if not matching:
				out.append(line)
				emitted.add(id(line))
				continue
			sep_x, y0, y1 = min(matching, key=lambda band: band[2] - band[1])
			band_lines = [
				candidate
				for candidate in physical
				if id(candidate) not in emitted
				and y0 - 2.0 <= (candidate.y0 + candidate.y1) / 2 <= y1 + 2.0
			]
			out.extend(self._order_one_column_band(page, band_lines, sep_x))
			emitted.update(id(candidate) for candidate in band_lines)
		return out

	def _coalesce_column_separator_bands(
		self,
		infos: List[Tuple[float, float, float]],
	) -> List[Tuple[float, float, float]]:
		groups: List[Dict[str, Any]] = []
		for x, y0, y1 in sorted(infos, key=lambda info: (info[1], info[2], info[0])):
			best: Optional[Dict[str, Any]] = None
			best_overlap = 0.0
			for group in groups:
				overlap = min(y1, group["y1"]) - max(y0, group["y0"])
				minimum_height = min(y1 - y0, group["y1"] - group["y0"])
				if overlap >= minimum_height * 0.65 and overlap > best_overlap:
					best = group
					best_overlap = overlap
			if best is None:
				groups.append({"xs": [x], "y0": y0, "y1": y1})
			else:
				best["xs"].append(x)
				best["y0"] = min(best["y0"], y0)
				best["y1"] = max(best["y1"], y1)
		return sorted(
			[(median(group["xs"]), group["y0"], group["y1"]) for group in groups],
			key=lambda band: (band[1], band[0]),
		)

	def _order_one_column_band(self, page: int, lines: List[Line], sep_x: float) -> List[Line]:
		physical = sorted(lines, key=lambda line: (line.y0, line.x0, line.seq))
		page_width, _page_height = self.conv.page_sizes.get(page, (612.0, 792.0))
		out: List[Line] = []
		pending: List[Line] = []

		def flush() -> None:
			if not pending:
				return
			left = [line for line in pending if (line.x0 + line.x1) / 2 < sep_x]
			right = [line for line in pending if (line.x0 + line.x1) / 2 >= sep_x]
			if left and right:
				out.extend(sorted(left, key=lambda line: (line.y0, line.x0, line.seq)))
				out.extend(sorted(right, key=lambda line: (line.y0, line.x0, line.seq)))
			else:
				out.extend(sorted(pending, key=lambda line: (line.y0, line.x0, line.seq)))
			pending.clear()

		for line in physical:
			if (
				line.x0 < sep_x - 12.0
				and line.x1 > sep_x + 12.0
				and line.x1 - line.x0 >= page_width * 0.65
			):
				flush()
				out.append(line)
			else:
				pending.append(line)
		flush()
		return out

	def _column_containers(self, page: int) -> List[Tuple[float, str, List[Line]]]:
		"""Render strongly evidenced CSS-column regions without flattening them.

		An inferred whitespace gutter establishes reading order. Requiring a
		coextensive rule on the outer left edge distinguishes a styled column
		container from two unrelated text clusters and from an ordinary quote.
		"""
		bands = self._coalesce_column_separator_bands(self._inferred_column_bands.get(page, []))
		# The report detector also handles producers that never place both
		# columns in one PDF text-showing operation. In that dialect there is
		# no oversized intra-line gap for the earlier splitter to observe, so
		# include the same outer-rule plus aligned-start evidence here even if
		# another, narrower intra-line candidate was already recorded.
		from .layout.regions import _outer_rule_column_band

		page_width, _page_height = self.conv.page_sizes.get(page, (612.0, 792.0))
		inferred = _outer_rule_column_band(page, self.lines_by_page.get(page, []), self.conv.segments, page_width)
		if inferred is not None:
			sep_x, y0, y1, _rule_x, _left, _right = inferred
			# Keep this bounded candidate intact. Coalescing it with a broader,
			# nearby intra-line gap can pull the band above the actual outer rule
			# and erase the very coextensiveness evidence that validates it.
			if not any(abs(sep_x - x) <= 12.0 and abs(y0 - by0) <= 8.0 and abs(y1 - by1) <= 8.0 for x, by0, by1 in bands):
				bands.append((sep_x, y0, y1))
		if not bands:
			return []
		lines = self.lines_by_page.get(page, [])
		out: List[Tuple[float, str, List[Line]]] = []
		from .html.render import render_inline_fragment

		for sep_x, y0, y1 in bands:
			band_lines = [
				line
				for line in lines
				if y0 - 2.0 <= (line.y0 + line.y1) / 2 <= y1 + 2.0
				and plain_text(line_text_tokens(line)).strip()
			]
			left = [line for line in band_lines if (line.x0 + line.x1) / 2 < sep_x]
			right = [line for line in band_lines if (line.x0 + line.x1) / 2 >= sep_x]
			if len(left) < 2 or len(right) < 2:
				continue
			rules = []
			for segment in self.conv.segments:
				if segment.page != page or not segment.vertical:
					continue
				x = (segment.x0 + segment.x1) / 2
				sy0, sy1 = sorted((segment.y0, segment.y1))
				if (
					x < sep_x - max(60.0, median([line.size for line in band_lines]) * 6.0)
					and sy0 <= y0 + 10.0
					and sy1 >= y1 - 10.0
					and segment.width >= 1.5
				):
					rules.append(segment)
			if not rules:
				continue
			rule = min(rules, key=lambda segment: abs(((segment.x0 + segment.x1) / 2) - min(line.x0 for line in left)))

			def paragraphs(column_lines: List[Line]) -> List[List[Line]]:
				ordered = sorted(column_lines, key=lambda line: (line.y0, line.x0, line.seq))
				groups: List[List[Line]] = []
				for line in ordered:
					if not groups:
						groups.append([line])
						continue
					previous = groups[-1][-1]
					gap = line_flow_gap(previous, line)
					if (
						0 < gap <= max(previous.size * 1.9, 18.0)
						and abs(line.x0 - previous.x0) <= max(5.0, line.size * 0.55)
					):
						groups[-1].append(line)
					else:
						groups.append([line])
				return groups

			body: List[str] = []
			for group in paragraphs(left) + paragraphs(right):
				fragment = self._render_paragraph(group, preserve_layout=False)
				body.append("<p>%s</p>" % render_inline_fragment(fragment))
			border_px = max(1, int(round(rule.width * 4.0 / 3.0)))
			rule_x = (rule.x0 + rule.x1) / 2
			padding_rem = max(0.5, min(3.0, (min(line.x0 for line in left) - rule_x) / 12.0))
			html = (
				'<div class="cocoapdf-columns" style="columns: 2; column-gap: 2rem; '
				'border-left: %dpx solid %s; padding-left: %.1frem;">\n%s\n</div>'
				% (border_px, color_to_hex(rule.color), padding_rem, "\n".join(body))
			)
			out.append((min(line.y0 for line in band_lines), html, band_lines))
			self.conv.doc.warn(
				"COLUMN_CONTAINER_HTML",
				"two-column region reconstructed from whitespace gutter and outer container rule",
				page,
			)
		return out

	def _column_separators(self, page: int) -> List[float]:
		return [x for x, _y0, _y1 in self._column_separator_infos(page)]

	def _column_separator_infos(self, page: int) -> List[Tuple[float, float, float]]:
		width, _height = self.conv.page_sizes.get(page, (612, 792))
		infos = []
		for seg in self.conv.segments:
			if seg.page != page or not seg.vertical:
				continue
			x = (seg.x0 + seg.x1) / 2
			y0, y1 = sorted((seg.y0, seg.y1))
			if width * 0.35 <= x <= width * 0.65 and (y1 - y0) >= 80:
				infos.append((x, y0, y1))
		return infos

	def _inferred_column_separator_infos(self, page: int, lines: List[Line]) -> List[Tuple[float, float, float]]:
		width, _height = self.conv.page_sizes.get(page, (612, 792))
		candidates: List[Tuple[float, float, float, Line]] = []
		for line in lines:
			chars = sorted([c for c in line.chars if c.text], key=lambda c: (c.x0, c.seq))
			if len(chars) < 12:
				continue
			prev: Optional[Char] = None
			for idx, ch in enumerate(chars):
				if prev is None:
					prev = ch
					continue
				gap = ch.x0 - prev.x1
				if gap < max(72.0, line.size * 7.0):
					prev = ch
					continue
				left_len = sum(len(c.text.strip()) for c in chars[:idx])
				right_len = sum(len(c.text.strip()) for c in chars[idx:])
				sep_x = (prev.x1 + ch.x0) / 2
				if left_len >= 8 and right_len >= 8 and width * 0.25 <= sep_x <= width * 0.75:
					candidates.append((sep_x, (line.y0 + line.y1) / 2, gap, line))
				prev = ch
		out: List[Tuple[float, float, float]] = []
		for sep_x, cy, gap, candidate_line in candidates:
			aligned_candidates = [
				item
				for item in candidates
				if abs(item[0] - sep_x) <= 12
				and abs(item[1] - cy) <= 180
			]
			band = [
				line
				for line in lines
				if abs(((line.y0 + line.y1) / 2) - cy) <= 80
				and plain_text(line_text_tokens(line)).strip()
			]
			single_side = [line for line in band if not (line.x0 < sep_x - 12 and line.x1 > sep_x + 12)]
			left = [line for line in single_side if (line.x0 + line.x1) / 2 < sep_x and line.x0 < sep_x - 12]
			right = [line for line in single_side if (line.x0 + line.x1) / 2 >= sep_x and line.x1 > sep_x + 12]
			if (not left or not right) and len(aligned_candidates) < 2:
				continue
			evidence_lines = left + right + [item[3] for item in aligned_candidates]
			evidence_lines.append(candidate_line)
			evidence_lines = list({id(line): line for line in evidence_lines}.values())
			y0 = min(line.y0 for line in evidence_lines)
			y1 = max(line.y1 for line in evidence_lines)
			if y1 - y0 < max(30.0, median([line.size for line in evidence_lines]) * 3.0):
				continue
			if not self._has_left_column_container_rule(page, sep_x, y0, y1):
				continue
			out.append((sep_x, y0, y1))
		deduped: List[Tuple[float, float, float]] = []
		for sep_x, y0, y1 in sorted(out, key=lambda item: (item[1], item[0])):
			if any(abs(sep_x - x) <= 12 and not (y1 < oy0 or y0 > oy1) for x, oy0, oy1 in deduped):
				continue
			deduped.append((sep_x, y0, y1))
		return deduped

	def _has_left_column_container_rule(self, page: int, sep_x: float, y0: float, y1: float) -> bool:
		min_len = max(30.0, (y1 - y0) * 0.45)
		for seg in self.conv.segments:
			if seg.page != page or not seg.vertical or seg.length < min_len:
				continue
			x = (seg.x0 + seg.x1) / 2
			sy0, sy1 = sorted((seg.y0, seg.y1))
			if x <= sep_x - 60 and sy1 >= y0 - 10 and sy0 <= y1 + 10:
				return True
		return False

	def _remove_furniture(self) -> None:
		if len(self.lines_by_page) < 3:
			return
		sigs: Dict[str, set[int]] = {}
		literal_sigs: Dict[str, set[int]] = {}
		margin_geometry_sigs: Dict[Tuple[int, int], set[int]] = {}
		line_sig: Dict[int, str] = {}
		line_literal_sig: Dict[int, str] = {}
		line_margin_geometry_sig: Dict[int, Tuple[int, int]] = {}
		for page, lines in self.lines_by_page.items():
			_w, h = self.conv.page_sizes.get(page, (612, 792))
			for idx, line in enumerate(lines):
				cy = (line.y0 + line.y1) / 2
				# Geometry alone is only safe evidence at the trailing margin.
				# Authored headings commonly repeat the same top position and
				# size across section-opening pages, while generated footers often
				# vary their text or link target at one fixed baseline.
				if cy >= h * 0.93:
					geometry_sig = (round((cy / max(h, 1.0)) * 200), round(line.size * 2))
					margin_geometry_sigs.setdefault(geometry_sig, set()).add(page)
					line_margin_geometry_sig[(page << 16) + idx] = geometry_sig
				if cy > h * 0.15 and cy < h * 0.85:
					continue
				raw_text = plain_text(line_text_tokens(line)).strip().lower()
				text = re.sub(r"\d+", "#", raw_text)
				if not text:
					continue
				sig = "%s|%d|%d" % (text, round(cy / 3), round(line.size))
				literal_sig = "%s|%d|%d" % (raw_text, round(cy / 3), round(line.size))
				sigs.setdefault(sig, set()).add(page)
				literal_sigs.setdefault(literal_sig, set()).add(page)
				line_sig[(page << 16) + idx] = sig
				line_literal_sig[(page << 16) + idx] = literal_sig
		page_count = len(self.lines_by_page)
		repeated_margin_geometry = {
			sig
			for sig, pages in margin_geometry_sigs.items()
			if len(pages) >= max(2, math.ceil(page_count * 0.6))
		}
		odd_pages = {page for page in self.lines_by_page if page % 2 == 1}
		even_pages = set(self.lines_by_page) - odd_pages
		remove = {
			sig
			for sig, pages in sigs.items()
			if len(pages) >= max(2, math.ceil(page_count * 0.6))
			or (odd_pages and len(pages & odd_pages) >= max(2, math.ceil(len(odd_pages) * 0.6)))
			or (even_pages and len(pages & even_pages) >= max(2, math.ceil(len(even_pages) * 0.6)))
		}
		for page, lines in list(self.lines_by_page.items()):
			kept = []
			_w, h = self.conv.page_sizes.get(page, (612, 792))
			for idx, line in enumerate(lines):
				text = plain_text(line_text_tokens(line)).strip()
				sig = line_sig.get((page << 16) + idx)
				literal_sig = line_literal_sig.get((page << 16) + idx)
				cy = (line.y0 + line.y1) / 2
				margin_marker = re.fullmatch(r"(?:page\s*)?\d+|[ivxlcdm]+", text, re.I) and (cy <= h * 0.08 or cy >= h * 0.92)
				literal_pages = literal_sigs.get(literal_sig or "", set())
				repeated_literal = len(literal_pages) >= max(2, math.ceil(page_count * 0.6))
				margin_geometry_sig = line_margin_geometry_sig.get((page << 16) + idx)
				if (
					(sig in remove and repeated_literal)
					or margin_marker
					or margin_geometry_sig in repeated_margin_geometry
				):
					continue
				kept.append(line)
			self.lines_by_page[page] = kept

	def _render_page(self, page: int) -> List[str]:
		lines = self.lines_by_page.get(page, [])
		body_size = self._body_font_size(
			[line for page_lines in self.lines_by_page.values() for line in page_lines]
		)
		consumed: set = set(self._vector_line_ids.get(page, set()))
		blocks: List[Tuple[float, str]] = []
		for anchor in self.conv.anchors:
			if anchor.page == page:
				blocks.append(
					self._event(
						page,
						self._rank_for_y(lines, anchor.y) - 0.35,
						"anchor",
						'<a id="%s"></a>' % escape_attr(anchor.name),
						attrs={"anchor": anchor.name, "y": anchor.y},
					)
				)
		for y, column_html, column_lines in self._column_containers(page):
			blocks.append(self._event(page, self._rank_for_y(lines, y) - 0.25, "columns", column_html, column_lines))
			consumed.update(id(line) for line in column_lines)
		for y, form_html, form_lines in self._form_appearances(page):
			blocks.append(self._event(page, self._rank_for_y(lines, y) - 0.225, "form_appearance", form_html, form_lines))
			consumed.update(id(line) for line in form_lines)
		for y, callout_md, callout_lines in self._callouts(page):
			kind = "equation" if "<math" in callout_md else "callout"
			blocks.append(self._event(page, self._rank_for_y(lines, y) - 0.2, kind, callout_md, callout_lines))
			consumed.update(id(l) for l in callout_lines)
		for y, table_md, table_lines, table_box in self._table_candidates(page):
			if self._box_is_inside_vector_artwork(page, table_box):
				continue
			blocks.append(self._event(page, self._rank_for_y(lines, y) - 0.1, "table", table_md, table_lines, {"bbox": table_box}))
			consumed.update(id(l) for l in table_lines)
		for img in self.conv.images:
			if img.page == page:
				caption_lines = self._image_caption_lines(img, consumed)
				caption = ""
				if caption_lines:
					caption = cleanup_spaces(
						" ".join(
							plain_text(line_text_tokens(line)).strip()
							for line in caption_lines
						)
					)
					consumed.update(id(line) for line in caption_lines)
					if not img.alt:
						img.alt = caption
				blocks.append(
					self._event(
						page,
						self._rank_for_y(lines, img.y0),
						"figure",
						self._render_image(img, caption),
						caption_lines,
						{"image": img, "caption": caption},
					)
				)
		for y, rule in self._rules(page):
			blocks.append(self._event(page, self._rank_for_y(lines, y), "thematic_break", rule, attrs={"y": y}))

		i = 0
		while i < len(lines):
			line = lines[i]
			if id(line) in consumed:
				i += 1
				continue
			text = plain_text(line_text_tokens(line)).strip()
			if not text:
				i += 1
				continue
			if self._is_heading(line, body_size, previous_line(lines, i), next_line(lines, i)):
				rank = i
				heading_lines = [line]
				j = i + 1
				while (
					j < len(lines)
					and id(lines[j]) not in consumed
					and self._is_heading(lines[j], body_size, heading_lines[-1], next_line(lines, j))
					and self._is_heading_continuation(heading_lines[-1], lines[j], next_line(lines, j))
				):
					heading_lines.append(lines[j])
					j += 1
				level = self._heading_level(line, body_size, previous_line(lines, i), next_line(lines, i))
				heading_text = " ".join(strip_wrapping_styles(render_inline(line_text_tokens(h), self.conv.options)) for h in heading_lines)
				blocks.append(self._event(page, rank, "heading", "%s %s" % ("#" * level, strip_wrapping_styles(heading_text)), heading_lines, {"level": level}))
				i = j
				continue
			if self._is_quote_line(line, page):
				rank = i
				quote_items: List[Line] = []
				while i < len(lines):
					if self._is_quote_line(lines[i], page) or self._quote_context_code_line(lines, i, page):
						quote_items.append(lines[i])
						i += 1
						continue
					break
				blocks.append(self._event(page, rank, "quote", self._render_quote_block(quote_items, page), quote_items))
				continue
			if self._is_code_line(line, body_size):
				rank = i
				code_items: List[Optional[Line]] = []
				while i < len(lines) and self._is_code_line(lines[i], body_size) and id(lines[i]) not in consumed:
					if code_items and (lines[i].y0 - lines[i - 1].y0) > max(lines[i - 1].size * 2.3, 22):
						if (
							(
								self._same_code_fill(lines[i - 1], lines[i])
								and (lines[i].y0 - lines[i - 1].y0) <= max(lines[i - 1].size * 4.0, 40)
							)
							or self._same_code_layout_cohort(lines[i - 1], lines[i])
						):
							leading = max(lines[i - 1].size * 1.6, 12.0)
							blanks = max(1, min(3, int(round((lines[i].y0 - lines[i - 1].y0) / leading)) - 1))
							code_items.extend([None] * blanks)
						else:
							break
					code_items.append(lines[i])
					i += 1
				block_left = min(item.x0 for item in code_items if item is not None)
				code_lines = ["" if item is None else code_line_text(item, block_left) for item in code_items]
				fence = "```"
				body = "\n".join(code_lines)
				while fence in body:
					fence += "`"
				blocks.append(self._event(page, rank, "code_block", fence + "\n" + body.rstrip("\n") + "\n" + fence, [item for item in code_items if item is not None], {"code": body.rstrip("\n")}))
				continue
			marker = list_marker(text)
			if marker:
				rank = i
				collected: List[Tuple[Line, Tuple[str, int, Union[int, str]], str]] = []
				first_type = marker[0]
				prev_list_line = line
				j = i
				while j < len(lines):
					t = plain_text(line_text_tokens(lines[j])).strip()
					m = list_marker(t)
					if not m:
						break
					if m[0] != first_type and (lines[j].y0 - prev_list_line.y0) > max(line.size * 1.5, 16):
						break
					collected.append((lines[j], m, t))
					prev_list_line = lines[j]
					j += 1
				if not self._explicit_list_is_confirmed(collected):
					if len(collected) == 1:
						prose_lines = [collected[0][0]]
						while j < len(lines):
							continuation = lines[j]
							continuation_text = plain_text(line_text_tokens(continuation)).strip()
							gap = continuation.y0 - prose_lines[-1].y0
							if (
								not continuation_text
								or id(continuation) in consumed
								or list_marker(continuation_text)
								or gap <= 0
								or gap > max(prose_lines[-1].size * 1.9, 18.0)
								or abs(continuation.x0 - prose_lines[-1].x0) > max(8.0, continuation.size * 0.8)
								or self._is_heading(continuation, body_size, prose_lines[-1], next_line(lines, j))
								or self._is_code_line(continuation, body_size)
							):
								break
							prose_lines.append(continuation)
							j += 1
						blocks.append(self._event(page, rank, "paragraph", self._render_paragraph(prose_lines), prose_lines))
						i = j
						continue
					for offset, (candidate_line, _candidate_marker, _candidate_text) in enumerate(collected):
						prose = escape_block_start(
							render_inline(line_text_tokens(candidate_line), self.conv.options).strip()
						)
						blocks.append(self._event(page, rank + offset * 0.001, "paragraph", prose, [candidate_line]))
					i = j
					continue
				list_lines = []
				continuation_indent = "  "
				for list_line, m, t in collected:
					indent = self._explicit_list_indent(list_line, m)
					content = self._render_list_content(list_line, m[1])
					prefix = self._list_prefix(m)
					list_lines.append(indent + prefix + content)
					continuation_indent = indent + " " * len(prefix)
				continuation_start = j
				while j < len(lines):
					cont = lines[j]
					cont_text = plain_text(line_text_tokens(cont)).strip()
					if not cont_text or id(cont) in consumed or list_marker(cont_text) or self._is_code_line(cont, body_size) or self._is_quote_line(cont, page):
						break
					if self._is_heading(cont, body_size, prev_list_line, next_line(lines, j)):
						break
					if self._missing_bullet_start(lines, j):
						break
					if cont.x0 - prev_list_line.x0 < max(cont.size * 0.8, 8) or cont.y0 - prev_list_line.y0 > max(cont.size * 2.8, 30):
						break
					indented_run_end = self._plain_indented_run_end(lines, j, list_line.x0)
					if indented_run_end is not None:
						while j < indented_run_end:
							cont = lines[j]
							list_lines.append(
								continuation_indent
								+ "- "
								+ render_inline(line_text_tokens(cont), self.conv.options).strip()
							)
							prev_list_line = cont
							j += 1
						continue
					list_lines[-1] += "\n" + continuation_indent + render_inline(line_text_tokens(cont), self.conv.options).strip()
					prev_list_line = cont
					j += 1
				blocks.append(self._event(page, rank, "list", "\n".join(list_lines), [item[0] for item in collected] + lines[continuation_start:j], {"ordered": first_type == "ol"}))
				i = j
				continue
			indented_bullets = self._indented_bullet_groups(lines, i, body_size, consumed)
			if indented_bullets is not None:
				groups, new_i = indented_bullets
				indent = self._missing_bullet_indent(lines, i)
				items = [
					self._indent_list_item_continuations(
						indent
						+ self._visual_marker_prefix(self._visual_list_marker(group[0]))
						+ self._render_paragraph(
							group,
							preserve_layout=False,
							preserve_hard_breaks=False,
						),
						indent,
					)
					for group in groups
				]
				flat_group_lines = [candidate for group in groups for candidate in group]
				blocks.append(self._event(page, i, "list", "\n".join(items), flat_group_lines, {"visual_markers": True}))
				i = new_i
				continue
			if self._missing_bullet_start(lines, i):
				rank = i
				list_lines, new_i = self._collect_missing_bullets(lines, i)
				blocks.append(self._event(page, rank, "list", "\n".join(list_lines), lines[i:new_i], {"visual_markers": True}))
				i = new_i
				continue
			rank = i
			para_lines = [line]
			i += 1
			while i < len(lines):
				nxt = lines[i]
				if id(nxt) in consumed:
					break
				gap = line_flow_gap(para_lines[-1], nxt)
				nxt_text = plain_text(line_text_tokens(nxt)).strip()
				if gap < -max(para_lines[-1].size, 8):
					break
				if not nxt_text or (
					gap > max(para_lines[-1].size * 1.9, 18)
					and not self._styled_wrap_continuation(para_lines[-1], nxt, gap)
				):
					break
				punctuation_continuation = self._punctuation_list_continuation(para_lines[-1], nxt)
				if (
					self._is_heading(nxt, body_size, para_lines[-1], next_line(lines, i))
					or (list_marker(nxt_text) and not punctuation_continuation)
					or self._is_code_line(nxt, body_size)
				):
					break
				if self._paragraph_style_boundary(para_lines[-1], nxt):
					break
				para_lines.append(nxt)
				i += 1
			blocks.append(self._event(page, rank, "paragraph", self._render_paragraph(para_lines), para_lines))

		blocks.sort(key=lambda x: x[0])
		self.block_events_by_page.setdefault(page, []).sort(key=lambda event: event.rank)
		rendered = [block for _rank, block in blocks]
		if page in self.conv.ink_pages and not lines:
			return ["<!-- page %d: no extractable text layer -->" % page] + rendered
		return rendered

	def _box_is_inside_vector_artwork(
		self,
		page: int,
		box: Tuple[float, float, float, float],
	) -> bool:
		x0, y0, x1, y1 = box
		return any(
			vx0 - 3.0 <= x0
			and vy0 - 3.0 <= y0
			and x1 <= vx1 + 3.0
			and y1 <= vy1 + 3.0
			for vx0, vy0, vx1, vy1 in self._vector_boxes.get(page, [])
		)

	def _paragraph_style_boundary(self, previous: Line, current: Line) -> bool:
		"""Detect a strong block-level font transition between adjacent lines."""
		if previous.writing_mode != current.writing_mode and (
			previous.writing_mode != "horizontal" or current.writing_mode != "horizontal"
		):
			return True
		mono_transition = (
			(previous.mono_ratio >= 0.85 and current.mono_ratio <= 0.35)
			or (current.mono_ratio >= 0.85 and previous.mono_ratio <= 0.35)
		)
		if mono_transition and not self._is_inline_code_tail(previous, current):
			return True
		previous_text = plain_text(line_text_tokens(previous)).strip()
		current_text = plain_text(line_text_tokens(current)).strip()
		if previous_text.endswith(":") and is_formula_like_text(current_text):
			return True
		current_tokens = line_text_tokens(current)
		if (
			previous.x0 - current.x0 > max(current.size * 2.0, 18.0)
			and current_tokens
			and current_tokens[0]["style"][0]
		):
			return True
		return bool(
			previous_text
			and current_text
			and previous_text.isupper()
			and current_text.isupper()
			and previous.mono_ratio >= 0.85
			and current.mono_ratio >= 0.85
			and current.y0 - previous.y0 >= max(previous.size * 1.6, 10.0)
		)

	def _styled_wrap_continuation(self, previous: Line, current: Line, gap: float) -> bool:
		"""Keep a styled run together across a producer-expanded wrap gap."""
		if gap <= 0 or gap > max(previous.size * 2.6, 28.0):
			return False
		if abs(previous.x0 - current.x0) > max(5.0, previous.size * 0.55):
			return False
		previous_tokens = [token for token in line_text_tokens(previous) if token["text"].strip()]
		current_tokens = [token for token in line_text_tokens(current) if token["text"].strip()]
		if not previous_tokens or not current_tokens:
			return False
		style = previous_tokens[-1]["style"]
		if not any(style) or current_tokens[0]["style"] != style:
			return False
		previous_text = plain_text(previous_tokens).rstrip()
		if previous_text[-1:] in ".!?:;":
			return False
		return (previous.x1 - previous.x0) / max(self._available_width(previous), 1.0) >= 0.60

	def _punctuation_list_continuation(self, previous: Line, current: Line) -> bool:
		text = plain_text(line_text_tokens(current)).strip()
		marker = list_marker(text)
		if marker is None or marker[0] != "ul":
			return False
		remainder = text[marker[1] :].strip()
		if not remainder or any(char.isalnum() for char in remainder):
			return False
		if abs(previous.x0 - current.x0) > max(5.0, current.size * 0.55):
			return False
		available = self._available_width(previous)
		return (previous.x1 - previous.x0) / max(available, 1.0) >= 0.75

	def _is_inline_code_tail(self, previous: Line, current: Line) -> bool:
		if previous.mono_ratio > 0.35 or current.mono_ratio < 0.85:
			return False
		previous_text = plain_text(line_text_tokens(previous)).strip()
		current_text = plain_text(line_text_tokens(current)).strip()
		return bool(
			previous_text.endswith((":", ";"))
			and 0 < len(current_text) <= 48
			and current.x1 - current.x0 <= max(current.size * 14.0, 120.0)
			and abs(current.x0 - previous.x0) <= max(current.size * 0.8, 8.0)
			and 0 < current.y0 - previous.y0 <= max(previous.size * 1.8, 20.0)
		)


	def _image_caption_lines(
		self,
		image: ImageItem,
		consumed: set,
	) -> List[Line]:
		candidates: List[Tuple[float, Line]] = []
		image_width = max(1.0, image.x1 - image.x0)
		for line in self.lines_by_page.get(image.page, []):
			if id(line) in consumed:
				continue
			text = plain_text(line_text_tokens(line)).strip()
			if not text:
				continue
			overlap = max(
				0.0,
				min(image.x1, line.x1) - max(image.x0, line.x0),
			)
			if overlap < min(image_width, max(1.0, line.x1 - line.x0)) * 0.45:
				continue
			if line.y0 >= image.y1:
				gap = line.y0 - image.y1
			elif line.y1 <= image.y0:
				gap = image.y0 - line.y1
			else:
				continue
			if gap > max(30.0, line.size * 2.8):
				continue
			styled = [char for char in line.chars if char.text.strip()]
			italic_ratio = (
				sum(1 for char in styled if char.italic) / len(styled)
				if styled
				else 0.0
			)
			labelled = bool(
				re.match(
					r"^(?:Figure|Fig\.|Chart|Listing|Exhibit)\s+\w+",
					text,
					re.I,
				)
			)
			if not labelled and italic_ratio < 0.55:
				continue
			candidates.append((gap, line))
		if not candidates:
			return []
		first = min(candidates, key=lambda item: item[0])[1]
		page_lines = self.lines_by_page.get(image.page, [])
		try:
			index = page_lines.index(first)
		except ValueError:
			return [first]

		caption_lines = [first]
		for candidate in page_lines[index + 1 : index + 4]:
			if id(candidate) in consumed:
				break
			previous = caption_lines[-1]
			gap = candidate.y0 - previous.y0
			if gap <= 0 or gap > max(first.size * 1.8, 22.0):
				break
			if abs(candidate.size - first.size) > max(1.0, first.size * 0.12):
				break
			if abs(candidate.x0 - first.x0) > max(12.0, first.size * 1.5):
				break
			text = plain_text(line_text_tokens(candidate)).strip()
			if not text or list_marker(text) or self._is_code_line(
				candidate,
				body_font_size(page_lines),
			):
				break
			visible = [char for char in candidate.chars if char.text.strip()]
			italic_ratio = (
				sum(1 for char in visible if char.italic) / len(visible)
				if visible
				else 0.0
			)
			previous_text = plain_text(line_text_tokens(previous)).strip()
			lowercase_wrap = bool(
				text[:1].islower()
				and not previous_text.endswith((".", "!", "?", ":", ";"))
			)
			if (italic_ratio < 0.55 and not lowercase_wrap) or (
				previous_text.endswith((".", "!", "?", ":"))
				and text[:1].isupper()
			):
				break
			caption_lines.append(candidate)
			if text.endswith((".", "!", "?")):
				break
		return caption_lines

	def _render_image(self, img: ImageItem, caption: str = "") -> str:
		src = image_source(img, self.conv.options)
		alt = img.alt or caption or ""
		href = safe_href(img.link) if img.link else None
		if self.conv.options.image_markup == "markdown":
			markup = "![%s](%s)" % (escape_md(alt), src)
			if href:
				markup = "[%s](%s)" % (markup, href)
			if caption:
				markup += "\n\n*%s*" % escape_inline(caption)
			return markup

		page_width, _page_height = self.conv.page_sizes.get(
			img.page,
			(612.0, 792.0),
		)
		align = image_alignment(img, page_width)
		width = max(1.0, img.placed_width or img.x1 - img.x0)
		height = max(1.0, img.placed_height or img.y1 - img.y0)
		style = (
			"width: %.3fpt; height: %.3fpt; "
			"max-width: 100%%; object-fit: contain;"
		) % (width, height)
		alignment_style = image_style_for_alignment(align)
		if alignment_style:
			style += " " + alignment_style
		image_html = (
			'<img src="%s" alt="%s" style="%s" />'
			% (
				escape_attr(src),
				escape_attr(alt),
				style,
			)
		)
		if href:
			image_html = (
				'<a href="%s" rel="noopener noreferrer">%s</a>'
				% (escape_attr(href), image_html)
			)
		parts = [
			'<figure class="cocoapdf-figure cocoapdf-align-%s">' % align,
			image_html,
		]
		if caption:
			parts.append("<figcaption>%s</figcaption>" % escape_html(caption))
		parts.append("</figure>")
		return "\n".join(parts)

	def _rank_for_y(self, lines: List[Line], y: float) -> float:
		for idx, line in enumerate(lines):
			if y < line.y0:
				return idx - 0.25
			if line.y0 <= y <= line.y1:
				return idx
		return float(len(lines))

	def _tagged_heading_level(self, line: Optional[Line]) -> Optional[int]:
		if line is None:
			return None
		levels: set[int] = set()
		for char in line.chars:
			for mark in char.mc:
				tag = str(mark.get("tag") or "").lstrip("/")
				match = re.fullmatch(r"H([1-6])", tag)
				if match:
					levels.add(int(match.group(1)))
		return next(iter(levels)) if len(levels) == 1 else None

	def _is_heading(self, line: Line, body_size: float, prev: Optional[Line], nxt: Optional[Line]) -> bool:
		if self._tagged_heading_level(line) is not None:
			return True
		text = plain_text(line_text_tokens(line)).strip()
		if not text or len(text) > 140:
			return False
		if re.fullmatch(r"[A-Z0-9]+(?:[-_][A-Z0-9]+)+", text):
			# Standalone machine identifiers are safer as emphasized prose than
			# invented document-outline entries when tags provide no heading role.
			return False
		if line.size < body_size * 0.88:
			return False
		if prev is None and nxt is None and line.size < body_size * 1.18:
			return False
		if line.size >= body_size * 1.18 and line.bold_ratio >= 0.25:
			return True
		gap_above = (line.y0 - prev.y0) if prev else 999
		gap_below = (nxt.y0 - line.y0) if nxt else 999
		near_body_size = line.size < body_size * 1.12
		if near_body_size and line.bold_ratio >= 0.75:
			if len(text) > 70:
				return False
			if text[-1:] in ".!?" and len(text.split()) >= 5 and not text.isupper():
				return False
			if text.isupper() and gap_above > body_size * 1.35:
				return True
			if prev is not None and self._same_wrapped_style(prev, line) and gap_above <= body_size * 1.75:
				return False
			if nxt is not None and self._same_wrapped_style(line, nxt) and gap_below <= body_size * 1.75:
				return False
			if gap_above <= body_size * 1.65 or gap_below <= body_size * 1.65:
				return False
		if line.bold_ratio >= 0.75 and gap_above > body_size * 1.4 and gap_below > body_size * 1.0:
			return True
		if text.isupper() and line.size >= body_size * 1.05 and gap_above > body_size * 1.2:
			return True
		return False

	def _same_wrapped_style(self, a: Line, b: Line) -> bool:
		return (
			abs(a.x0 - b.x0) <= max(3.0, a.size * 0.35)
			and abs(a.size - b.size) <= 1.0
			and abs(a.bold_ratio - b.bold_ratio) <= 0.25
			and a.bold_ratio >= 0.65
			and b.bold_ratio >= 0.65
		)

	def _is_heading_continuation(self, prev: Line, cur: Line, nxt: Optional[Line]) -> bool:
		prev_tagged = self._tagged_heading_level(prev)
		cur_tagged = self._tagged_heading_level(cur)
		if prev_tagged is not None or cur_tagged is not None:
			return prev_tagged is not None and prev_tagged == cur_tagged and prev.page == cur.page
		if prev.page != cur.page:
			return False
		if abs(prev.x0 - cur.x0) > max(prev.size * 0.4, 5.0):
			return False
		if abs(prev.size - cur.size) > 1.0 or abs(prev.bold_ratio - cur.bold_ratio) > 0.25:
			return False
		gap = cur.y0 - prev.y0
		if gap <= 0 or gap > max(prev.size * 1.45, 24):
			return False
		if nxt is None:
			return True
		gap_after = nxt.y0 - cur.y0
		return gap_after > max(gap * 1.3, cur.size * 1.65)

	def _heading_level(
		self,
		line: Line,
		body_size: float,
		prev: Optional[Line] = None,
		nxt: Optional[Line] = None,
	) -> int:
		tagged_level = self._tagged_heading_level(line)
		if tagged_level is not None:
			return tagged_level
		text = plain_text(line_text_tokens(line)).strip()
		ratio = line.size / max(body_size, 1.0)
		if ratio >= 1.9:
			return 1
		if ratio >= 1.55:
			return 2
		if ratio >= 1.24:
			return 3
		if ratio >= 1.07:
			return 4
		numbered = re.match(r"^(\d+(?:\.\d+){1,5})\b", text)
		if numbered:
			return min(6, max(2, numbered.group(1).count(".") + 2))
		if ratio >= 0.98:
			return 5
		next_text = plain_text(line_text_tokens(nxt)).strip() if nxt is not None else ""
		if (
			line.bold_ratio >= 0.75
			and nxt is not None
			and nxt.bold_ratio >= 0.75
			and 0 < nxt.y0 - line.y0 <= max(line.size * 4.0, 38.0)
			and (
				nxt.size <= line.size - max(0.5, line.size * 0.08)
				or (
					abs(nxt.size - line.size) <= 0.25
					and text.isupper()
					and not next_text.isupper()
				)
			)
		):
			# Browser default H5/H6 sizes are both below body size. A directly
			# following, distinctly smaller bold heading supplies the missing
			# hierarchy evidence for the larger one.
			return 5
		if ratio < 0.98 and line.bold_ratio >= 0.75 and text.isupper():
			return 6
		styled_chars = [c for c in line.chars if c.text.strip()]
		italic_ratio = (sum(1 for c in styled_chars if c.italic) / len(styled_chars)) if styled_chars else 0.0
		if line.bold_ratio >= 0.75 and italic_ratio >= 0.35:
			return 6
		if (
			line.bold_ratio >= 0.75
			and prev is not None
			and abs(prev.size - line.size) <= 1.0
			and prev.bold_ratio >= 0.75
			and 0 < line.y0 - prev.y0 <= max(line.size * 3.2, 32)
		):
			return 6
		if line.bold_ratio >= 0.75:
			return 5
		return 6

	def _is_code_line(self, line: Line, body_size: float) -> bool:
		if line.mono_ratio < 0.85:
			return False
		if self._code_region_key(line) is not None:
			return True
		all_lines = [l for ls in self.lines_by_page.values() for l in ls]
		if len(all_lines) > 0 and sum(1 for l in all_lines if l.mono_ratio > 0.85) / len(all_lines) > 0.8:
			return False
		page_lines = self.lines_by_page.get(line.page, [])
		body_lefts = [
			candidate.x0
			for candidate in page_lines
			if candidate.mono_ratio < 0.50
			and body_size * 0.82 <= candidate.size <= body_size * 1.18
			and plain_text(line_text_tokens(candidate)).strip()
		]
		if body_lefts:
			# The densest small x-coordinate band is a more stable body margin
			# than either the minimum (which can be page furniture) or median
			# (which moves in multi-column documents).
			bands: Dict[int, List[float]] = {}
			for x in body_lefts:
				bands.setdefault(int(round(x / 4.0)), []).append(x)
			body_left = median(max(bands.values(), key=lambda values: (len(values), -median(values))))
		else:
			page_width, _page_height = self.conv.page_sizes.get(line.page, (612.0, 792.0))
			body_left = page_width * 0.12
		if line.x0 <= body_left + max(body_size * 0.65, 6.0):
			return False

		# Without a containing fill, indentation alone is ambiguous: a
		# single monospace line is commonly a label or ordinary prose. Treat
		# it as block code only when another compact monospace line supplies
		# independent layout evidence for a code cohort.
		for candidate in page_lines:
			if candidate is line or candidate.mono_ratio < 0.85:
				continue
			if abs(candidate.size - line.size) > max(1.0, line.size * 0.15):
				continue
			vertical_gap = abs(candidate.y0 - line.y0)
			if vertical_gap > max(line.size * 3.2, 30.0):
				continue
			if abs(candidate.x0 - line.x0) <= max(line.size * 5.0, 40.0):
				return True
		return False

	def _is_quote_line(self, line: Line, page: int) -> bool:
		return self._quote_depth(line, page) > 0

	def _quote_depth(self, line: Line, page: int) -> int:
		return len(self._quote_bars_for_line(line, page))

	def _quote_bars_for_line(self, line: Line, page: int) -> List[Segment]:
		cached = self._quote_bars_cache.get(id(line))
		if cached is not None:
			return list(cached)
		width, _height = self.conv.page_sizes.get(page, (612, 792))
		code_fill = self._code_region_key(line)
		bars: List[Segment] = []
		for seg in self.conv.segments:
			if seg.page == page and seg.vertical and seg.width >= 1.5 and seg.length > max(10, line.size * 0.8):
				x = (seg.x0 + seg.x1) / 2
				if code_fill is not None and (
					abs(x - code_fill[0]) <= 3.0 or abs(x - code_fill[2]) <= 3.0
				):
					continue
				if width * 0.35 <= x <= width * 0.65 and seg.length >= 80:
					continue
				for sep_x, band_y0, band_y1 in self._inferred_column_bands.get(page, []):
					if (
						band_y0 <= line.y0 <= band_y1
						and x < sep_x - max(30.0, line.size * 3.0)
						and seg.length >= (band_y1 - band_y0) * 0.70
					):
						break
				else:
					if line.x0 - line.size * 5.0 <= x <= line.x0 - 1 and not (
						line.y1 < min(seg.y0, seg.y1) or line.y0 > max(seg.y0, seg.y1)
					):
						bars.append(seg)
		bars.sort(key=lambda s: (s.x0 + s.x1) / 2)
		self._quote_bars_cache[id(line)] = tuple(bars)
		return bars

	def _render_quote_block(self, lines: List[Line], page: int) -> str:
		if not lines:
			return ""
		body_size = self._body_font_size(lines)
		code_lines = [line for line in lines if self._is_code_line(line, body_size)]
		code_left = min((line.x0 for line in code_lines), default=0.0)
		depths = [max(1, self._quote_depth(line, page)) for line in lines]
		base_x_by_depth: Dict[int, float] = {}
		for line, depth in zip(lines, depths):
			base_x_by_depth[depth] = min(base_x_by_depth.get(depth, line.x0), line.x0)
		rendered: List[str] = []
		for idx, line in enumerate(lines):
			if idx > 0:
				gap = line.y0 - lines[idx - 1].y0
				if gap > max(lines[idx - 1].size * 2.15, 22):
					rendered.append(">")
			depth = depths[idx]
			prefix = "> " * depth
			if self._is_code_line(line, body_size):
				prev_code = idx > 0 and self._is_code_line(lines[idx - 1], body_size)
				next_code = idx + 1 < len(lines) and self._is_code_line(lines[idx + 1], body_size)
				if not prev_code:
					rendered.append(prefix + "```")
				rendered.append(prefix + code_line_text(line, code_left))
				if not next_code:
					rendered.append(prefix + "```")
				continue
			text = render_inline(line_text_tokens(line), self.conv.options)
			marker = list_marker(plain_text(line_text_tokens(line)).strip())
			if marker:
				content = self._render_list_content(line, marker[1])
				rendered.append(prefix + self._list_prefix(marker) + content)
				continue
			if self._quote_missing_bullet(lines, depths, idx, base_x_by_depth):
				visual_marker = self._visual_list_marker(line)
				rendered.append(prefix + self._visual_marker_prefix(visual_marker) + text)
			else:
				rendered.append(prefix + text)
		return "\n".join(rendered)

	def _quote_missing_bullet(self, lines: List[Line], depths: List[int], idx: int, base_x_by_depth: Dict[int, float]) -> bool:
		line = lines[idx]
		depth = depths[idx]
		if depth != 1:
			return False
		marker_x = self._drawn_list_marker_x(line)
		if line.x0 - base_x_by_depth.get(depth, line.x0) < max(line.size * 1.8, 16):
			return False
		same_indent_neighbors = 0
		for j in (idx - 1, idx + 1):
			if not (0 <= j < len(lines)) or depths[j] != depth:
				continue
			other_marker_x = self._drawn_list_marker_x(lines[j])
			if abs(lines[j].x0 - line.x0) > 4.0:
				continue
			if marker_x is not None and other_marker_x is not None and abs(other_marker_x - marker_x) <= max(4.0, line.size * 0.35):
				same_indent_neighbors += 1
				continue
			if marker_x is None and other_marker_x is None:
				same_indent_neighbors += 1
		return same_indent_neighbors > 0

	def _quote_context_code_line(self, lines: List[Line], i: int, page: int) -> bool:
		line = lines[i]
		body_size = self._body_font_size(lines)
		if not self._is_code_line(line, body_size):
			return False
		prev_quote = None
		next_quote = None
		for j in range(i - 1, max(-1, i - 5), -1):
			if self._is_quote_line(lines[j], page):
				prev_quote = lines[j]
				break
			if plain_text(line_text_tokens(lines[j])).strip() and not self._is_code_line(lines[j], body_size):
				break
		for j in range(i + 1, min(len(lines), i + 5)):
			if self._is_quote_line(lines[j], page):
				next_quote = lines[j]
				break
			if plain_text(line_text_tokens(lines[j])).strip() and not self._is_code_line(lines[j], body_size):
				break
		if prev_quote is None or next_quote is None:
			return False
		quote_left = min(prev_quote.x0, next_quote.x0)
		return line.x0 >= quote_left - max(line.size * 2.0, 18.0)

	def _line_inside_code_fill(self, line: Line) -> bool:
		return self._code_region_key(line) is not None

	def _same_code_fill(self, prev: Line, cur: Line) -> bool:
		prev_key = self._code_region_key(prev)
		return prev_key is not None and prev_key == self._code_region_key(cur)

	def _same_code_layout_cohort(self, previous: Line, current: Line) -> bool:
		return (
			previous.page == current.page
			and previous.mono_ratio >= 0.85
			and current.mono_ratio >= 0.85
			and abs(previous.size - current.size) <= max(1.0, previous.size * 0.15)
			and 0 < current.y0 - previous.y0 <= max(previous.size * 3.2, 30.0)
			and abs(previous.x0 - current.x0) <= max(previous.size * 5.0, 40.0)
		)

	def _code_fill_key(self, line: Line) -> Optional[Tuple[float, float, float, float]]:
		matches: List[Tuple[float, float, float, float]] = []
		page_width, page_height = self.conv.page_sizes.get(line.page, (612.0, 792.0))
		page_area = max(page_width * page_height, 1.0)
		for fill in self.conv.fills:
			fill_width = fill.x1 - fill.x0
			fill_height = fill.y1 - fill.y0
			if fill_width * fill_height > page_area * 0.35:
				continue
			if (
				fill.page == line.page
				and fill.x0 <= line.x0 + 2
				and fill.x1 >= line.x1 - 2
				and fill.y0 <= line.y0 + 2
				and fill.y1 >= line.y1 - 2
				and fill.x1 - fill.x0 > 100
			):
				matches.append((round(fill.x0, 1), round(fill.y0, 1), round(fill.x1, 1), round(fill.y1, 1)))
		if not matches:
			return None
		return max(matches, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))

	def _code_region_key(self, line: Line) -> Optional[Tuple[float, float, float, float]]:
		key = id(line)
		if key in self._code_region_cache:
			return self._code_region_cache[key]
		fill = self._code_fill_key(line)
		region = fill if fill is not None else self._code_border_key(line)
		self._code_region_cache[key] = region
		return region

	def _code_border_key(self, line: Line) -> Optional[Tuple[float, float, float, float]]:
		matches = [
			box
			for box in self._code_border_boxes(line.page)
			if box[0] - 2.0 <= line.x0
			and line.x1 <= box[2] + 2.0
			and box[1] - 2.0 <= line.y0
			and line.y1 <= box[3] + 2.0
		]
		if not matches:
			return None
		return min(matches, key=lambda box: (box[2] - box[0]) * (box[3] - box[1]))

	def _code_border_boxes(self, page: int) -> List[Tuple[float, float, float, float]]:
		cached = self._code_border_boxes_cache.get(page)
		if cached is not None:
			return cached
		page_width, page_height = self.conv.page_sizes.get(page, (612.0, 792.0))
		segments = [segment for segment in self.conv.segments if segment.page == page and segment.length > 3.0]
		matches: List[Tuple[float, float, float, float]] = []
		horizontal_edges = [
			segment
			for segment in segments
			if abs(segment.y1 - segment.y0) <= 3.0 and abs(segment.x1 - segment.x0) >= 100.0
		]
		vertical_edges = [
			segment
			for segment in segments
			if abs(segment.x1 - segment.x0) <= 3.0 and abs(segment.y1 - segment.y0) >= 12.0
		]
		for index, first in enumerate(horizontal_edges):
			for second in horizontal_edges[index + 1 :]:
				first_x0, first_x1 = sorted((first.x0, first.x1))
				second_x0, second_x1 = sorted((second.x0, second.x1))
				first_y = (first.y0 + first.y1) / 2
				second_y = (second.y0 + second.y1) / 2
				x0 = min(first_x0, second_x0)
				x1 = max(first_x1, second_x1)
				y0, y1 = sorted((first_y, second_y))
				if not (18.0 <= y1 - y0 <= 200.0):
					continue
				if abs(first_x0 - second_x0) > 5.0 or abs(first_x1 - second_x1) > 5.0:
					continue
				left_edge = any(
					abs((segment.x0 + segment.x1) / 2 - x0) <= 5.0
					and min(segment.y0, segment.y1) <= y0 + 4.0
					and max(segment.y0, segment.y1) >= y1 - 4.0
					for segment in vertical_edges
				)
				right_edge = any(
					abs((segment.x0 + segment.x1) / 2 - x1) <= 5.0
					and min(segment.y0, segment.y1) <= y0 + 4.0
					and max(segment.y0, segment.y1) >= y1 - 4.0
					for segment in vertical_edges
				)
				if (
					left_edge
					and right_edge
					and (x1 - x0) * (y1 - y0) <= page_width * page_height * 0.35
				):
					matches.append((round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)))
		for component in segment_components(segments):
			if len(component) < 4:
				continue
			horizontal = sum(
				1
				for segment in component
				if abs(segment.y1 - segment.y0) <= 3.0 and abs(segment.x1 - segment.x0) >= 20.0
			)
			vertical = sum(
				1
				for segment in component
				if abs(segment.x1 - segment.x0) <= 3.0 and abs(segment.y1 - segment.y0) >= 12.0
			)
			if horizontal < 2 or vertical < 2:
				continue
			x0 = min(min(segment.x0, segment.x1) for segment in component)
			x1 = max(max(segment.x0, segment.x1) for segment in component)
			y0 = min(min(segment.y0, segment.y1) for segment in component)
			y1 = max(max(segment.y0, segment.y1) for segment in component)
			width = x1 - x0
			height = y1 - y0
			if (
				width < 100.0
				or height < 18.0
				or width * height > page_width * page_height * 0.35
			):
				continue
			matches.append((round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)))
		boxes = sorted(set(matches), key=lambda box: ((box[2] - box[0]) * (box[3] - box[1]), box))
		self._code_border_boxes_cache[page] = boxes
		return boxes

	def _explicit_list_is_confirmed(
		self,
		collected: List[Tuple[Line, Tuple[str, int, Union[int, str]], str]],
	) -> bool:
		if not collected:
			return False

		first_line, first_marker, _first_text = collected[0]
		if len(collected) >= 2:
			tolerance = max(4.0, first_line.size * 0.40)
			matching = [
				item
				for item in collected
				if item[1][0] == first_marker[0]
				and abs(item[0].x0 - first_line.x0) <= tolerance
				and self._has_hanging_marker_gap(item[0], minimum_em=0.25)
			]
			if len(matching) >= 2:
				return True

		# Nested siblings are often separated by their own descendants. Search a
		# bounded physical-line neighborhood rather than requiring adjacency.
		page_lines = self.lines_by_page.get(first_line.page, [])
		try:
			first_index = page_lines.index(first_line)
		except ValueError:
			first_index = -1
		if first_index >= 0:
			sibling_count = 0
			tolerance = max(4.0, first_line.size * 0.40)
			for other in page_lines[max(0, first_index - 20) : first_index + 21]:
				other_text = plain_text(line_text_tokens(other)).strip()
				other_marker = list_marker(other_text)
				if (
					other_marker is not None
					and other_marker[0] == first_marker[0]
					and abs(other.x0 - first_line.x0) <= tolerance
					and self._has_hanging_marker_gap(other, minimum_em=0.25)
				):
					sibling_count += 1
			if sibling_count >= 2:
				return True

		marker_value = str(first_marker[2])
		if marker_value in UNCHECKED_TASK_MARKERS or marker_value in CHECKED_TASK_MARKERS:
			return True

		# A singleton numeric marker is accepted only when a real tab/hanging
		# gap separates the marker from its body, or when repeated indented
		# descendants make the list structure visible. This rejects ordinary
		# prose such as "1. Newton described ...".
		if first_marker[0] == "ol" and isinstance(first_marker[2], int):
			if self._has_hanging_marker_gap(first_line, minimum_em=0.55):
				return True
			page_lines = self.lines_by_page.get(first_line.page, [])
			try:
				first_index = page_lines.index(first_line)
			except ValueError:
				first_index = -1
			if first_index >= 0:
				if self._plain_indented_run_end(page_lines, first_index + 1, first_line.x0) is not None:
					return True
				if first_index > 0:
					prev = page_lines[first_index - 1]
					prev_text = plain_text(line_text_tokens(prev)).strip()
					if list_marker(prev_text) and 0 < first_line.y0 - prev.y0 <= max(first_line.size * 4.0, 45.0):
						return True
			return False
		return False

	def _plain_indented_run_end(self, lines: List[Line], i: int, base_x: float) -> Optional[int]:
		if i >= len(lines):
			return None
		first = lines[i]
		indent = first.x0 - base_x
		if indent < max(first.size * 2.0, 16.0):
			return None
		j = i
		last: Optional[Line] = None
		while j < len(lines):
			line = lines[j]
			text = plain_text(line_text_tokens(line)).strip()
			if not text or list_marker(text) or self._is_code_line(line, self._body_font_size(lines)):
				break
			if abs(line.x0 - first.x0) > max(4.0, first.size * 0.40):
				break
			if last is not None and not (0 < line.y0 - last.y0 <= max(first.size * 2.6, 30.0)):
				break
			last = line
			j += 1
		return j if j - i >= 2 else None

	def _has_hanging_marker_gap(self, line: Line, minimum_em: float) -> bool:
		boxes = word_boxes(line)
		if len(boxes) < 2:
			return False
		marker_right = boxes[0][3]
		body_left = boxes[1][1]
		return body_left - marker_right >= max(2.5, line.size * minimum_em)

	def _list_prefix(self, marker: Tuple[str, int, Union[int, str]]) -> str:
		if marker[0] == "ol":
			return "%s. " % marker[2]
		value = str(marker[2])
		if value in UNCHECKED_TASK_MARKERS:
			return "- [ ] "
		if value in CHECKED_TASK_MARKERS:
			return "- [x] "
		return "- "

	def _visual_list_marker(self, line: Line) -> Optional[VisualListMarker]:
		cache_key = id(line)
		if cache_key in self._visual_marker_cache:
			return self._visual_marker_cache[cache_key]
		size = max(line.size, 1.0)
		marker_left = line.x0 - max(30.0, size * 3.0)
		marker_right = line.x0 - max(1.5, size * 0.12)
		line_center = (line.y0 + line.y1) / 2
		candidates: List[Tuple[float, VisualListMarker]] = []

		for fill in self.conv.fills:
			if fill.page != line.page:
				continue
			width = fill.x1 - fill.x0
			height = fill.y1 - fill.y0
			center_x = (fill.x0 + fill.x1) / 2
			center_y = (fill.y0 + fill.y1) / 2
			if not (marker_left <= center_x <= marker_right):
				continue
			if abs(center_y - line_center) > max(4.0, size * 0.60):
				continue
			if 0.7 <= width <= max(12.0, size * 1.35) and 0.7 <= height <= max(12.0, size * 1.35):
				ratio = width / max(height, 0.1)
				if 0.65 <= ratio <= 1.55 and min(width, height) >= size * 0.68:
					kind = "task_checked"
				elif 0.15 <= ratio <= 6.0 and max(width, height) <= size * 0.75:
					kind = "bullet"
				else:
					continue
				candidates.append((line.x0 - center_x, VisualListMarker(center_x, kind)))

		# Some producers outline or fill bullet glyphs as compact paths instead
		# of emitting a text character. Retain that native vector evidence; it is
		# stronger than inferring a list from indentation alone.
		for path in self.conv.painted_paths:
			if path.page != line.page:
				continue
			x0, y0, x1, y1 = path.bbox
			width = x1 - x0
			height = y1 - y0
			center_x = (x0 + x1) / 2
			center_y = (y0 + y1) / 2
			if not (marker_left <= center_x <= marker_right):
				continue
			if abs(center_y - line_center) > max(4.0, size * 0.60):
				continue
			if not (0.7 <= width <= max(12.0, size * 1.35) and 0.7 <= height <= max(12.0, size * 1.35)):
				continue
			ratio = width / max(height, 0.1)
			if 0.35 <= ratio <= 2.85:
				candidates.append((line.x0 - center_x, VisualListMarker(center_x, "bullet")))

		for image in self.conv.images:
			if image.page != line.page:
				continue
			width = image.x1 - image.x0
			height = image.y1 - image.y0
			center_x = (image.x0 + image.x1) / 2
			center_y = (image.y0 + image.y1) / 2
			if (
				marker_left <= center_x <= marker_right
				and abs(center_y - line_center) <= max(4.0, size * 0.60)
				and 0.7 <= width <= max(14.0, size * 1.50)
				and 0.7 <= height <= max(14.0, size * 1.50)
			):
				candidates.append(
					(line.x0 - center_x, VisualListMarker(center_x, "bullet"))
				)

		small_segments = []
		for seg in self.conv.segments:
			if seg.page != line.page or seg.length > max(16.0, size * 1.60):
				continue
			sx0, sy0, sx1, sy1 = seg_bbox(seg)
			center_x = (sx0 + sx1) / 2
			center_y = (sy0 + sy1) / 2
			if (
				marker_left <= center_x <= marker_right
				and abs(center_y - line_center) <= max(4.0, size * 0.60)
				and sx1 - sx0 <= max(14.0, size * 1.50)
				and sy1 - sy0 <= max(14.0, size * 1.50)
			):
				small_segments.append(seg)

		# A single nearby border segment is not a bullet. Requiring a compact
		# component prevents input/select borders from becoming list markers.
		if len(small_segments) >= 2:
			sx0 = min(min(seg.x0, seg.x1) for seg in small_segments)
			sx1 = max(max(seg.x0, seg.x1) for seg in small_segments)
			sy0 = min(min(seg.y0, seg.y1) for seg in small_segments)
			sy1 = max(max(seg.y0, seg.y1) for seg in small_segments)
			width = sx1 - sx0
			height = sy1 - sy0
			center_x = (sx0 + sx1) / 2
			if (
				width <= max(14.0, size * 1.50)
				and height <= max(14.0, size * 1.50)
				and marker_left <= center_x <= marker_right
			):
				diagonal = any(not seg.horizontal and not seg.vertical for seg in small_segments)
				if (
					len(small_segments) >= 4
					and min(width, height) >= size * 0.55
					and 0.60 <= width / max(height, 0.1) <= 1.65
				):
					kind = "task_unchecked"
				elif len(small_segments) >= 2 and diagonal:
					kind = "task_checked"
				else:
					kind = "bullet"
				candidates.append(
					(line.x0 - center_x, VisualListMarker(center_x, kind))
				)

		if not candidates:
			self._visual_marker_cache[cache_key] = None
			return None
		marker = min(candidates, key=lambda item: item[0])[1]
		self._visual_marker_cache[cache_key] = marker
		return marker

	def _drawn_list_marker_x(self, line: Line) -> Optional[float]:
		marker = self._visual_list_marker(line)
		return marker.x if marker is not None else None

	def _visual_marker_prefix(self, marker: Optional[VisualListMarker]) -> str:
		if marker is not None and marker.kind == "task_checked":
			return "- [x] "
		if marker is not None and marker.kind == "task_unchecked":
			return "- [ ] "
		return "- "

	def _indented_bullet_groups(
		self,
		lines: List[Line],
		i: int,
		body_size: float,
		consumed: set,
	) -> Optional[Tuple[List[List[Line]], int]]:
		"""Recover compact unordered lists whose producer omitted bullet glyphs."""
		if i <= 0 or i >= len(lines):
			return None
		line = lines[i]
		text = plain_text(line_text_tokens(line)).strip()
		if not text or id(line) in consumed or list_marker(text) or self._is_code_line(line, body_size):
			return None
		previous = lines[i - 1]
		previous_text = plain_text(line_text_tokens(previous)).strip()
		previous_is_heading = self._is_heading(
			previous,
			body_size,
			previous_line(lines, i - 1),
			line,
		)
		if not previous_is_heading and not previous_text.endswith(":"):
			return None
		frame_left, _frame_right = self._text_frame(line.page)
		# A page dominated by several list runs can make the modal text frame
		# coincide with the item text. The local lead-in/heading offset remains
		# direct indentation evidence in that case.
		indent = max(line.x0 - frame_left, line.x0 - previous.x0)
		if not (max(line.size * 1.6, 16.0) <= indent <= max(line.size * 6.0, 60.0)):
			return None

		groups: List[List[Line]] = [[line]]
		j = i + 1
		last = line
		while j < len(lines):
			candidate = lines[j]
			candidate_text = plain_text(line_text_tokens(candidate)).strip()
			if (
				id(candidate) in consumed
				or not candidate_text
				or list_marker(candidate_text)
				or self._is_code_line(candidate, body_size)
				or self._is_quote_line(candidate, candidate.page)
				or self._is_heading(candidate, body_size, last, next_line(lines, j))
				or abs(candidate.x0 - line.x0) > max(4.0, line.size * 0.45)
			):
				break
			gap = line_flow_gap(last, candidate)
			if gap <= 0 or gap > max(line.size * 3.0, 32.0):
				break
			if gap >= max(line.size * 1.8, 17.5):
				groups.append([candidate])
			else:
				groups[-1].append(candidate)
			last = candidate
			j += 1
		return (groups, j) if len(groups) >= 3 else None

	def _missing_bullet_start(self, lines: List[Line], i: int) -> bool:
		line = lines[i]
		text = plain_text(line_text_tokens(line)).strip()
		body_size = self._body_font_size(lines)
		if not text or list_marker(text) or self._is_code_line(line, body_size):
			return False
		if re.match(r"^(?:Figure|Table|Equation)\s+\d+[:.]\s+\S", text, re.I):
			return False
		if re.match(r"^\d+(?:\.\d+)+\s+\S", text):
			return False
		if self._is_quote_line(line, line.page):
			return False
		if line.size > body_size * 1.20:
			return False
		marker_x = self._drawn_list_marker_x(line)
		if marker_x is None:
			if (
				i > 0
				and list_marker(plain_text(line_text_tokens(lines[i - 1])).strip())
				and -max(4.0, line.size * 0.40)
				<= line.x0 - lines[i - 1].x0
				<= max(line.size * 1.6, 18.0)
			):
				return False
			frame_left, _frame_right = self._text_frame(line.page)
			indent = line.x0 - frame_left
			if not (
				max(line.size * 1.6, 16.0)
				<= indent
				<= max(line.size * 6.0, 60.0)
			):
				return False
			if text[:1].islower():
				return False
			return self._has_same_indent_plain_sibling(lines, i)

		for j in range(max(0, i - 8), min(len(lines), i + 9)):
			if j == i:
				continue
			other = lines[j]
			if abs(other.x0 - line.x0) > max(4.0, line.size * 0.40):
				continue
			other_marker_x = self._drawn_list_marker_x(other)
			if (
				other_marker_x is not None
				and abs(other_marker_x - marker_x) <= max(4.0, line.size * 0.35)
			):
				return True
		return False

	def _has_same_indent_plain_sibling(self, lines: List[Line], i: int) -> bool:
		line = lines[i]
		line_text = plain_text(line_text_tokens(line)).strip()
		line_stem = self._missing_bullet_stem(line_text)
		if not line_stem:
			return False
		for j in range(max(0, i - 6), min(len(lines), i + 7)):
			if j == i:
				continue
			if j < i and i - j == 1:
				continue
			other = lines[j]
			if abs(other.x0 - line.x0) > max(4.0, line.size * 0.40):
				continue
			text = plain_text(line_text_tokens(other)).strip()
			if (
				not text
				or text[:1].islower()
				or list_marker(text)
				or self._is_code_line(other, self._body_font_size(lines))
			):
				continue
			if (
				self._missing_bullet_stem(text) == line_stem
				and abs(other.y0 - line.y0) <= max(line.size * 12.0, 120.0)
			):
				return True
		return False

	def _collect_missing_bullets(self, lines: List[Line], i: int) -> Tuple[List[str], int]:
		base_line = lines[i]
		base_x = base_line.x0
		base_size = max(base_line.size, 1.0)
		base_marker = self._visual_list_marker(base_line)
		base_marker_x = base_marker.x if base_marker is not None else None
		indent = self._missing_bullet_indent(lines, i)
		groups: List[List[Line]] = []
		j = i
		last_line: Optional[Line] = None

		while j < len(lines):
			line = lines[j]
			text = plain_text(line_text_tokens(line)).strip()
			body_size = self._body_font_size(lines)
			if not text or list_marker(text) or self._is_code_line(line, body_size):
				break
			if self._is_heading(line, body_size, previous_line(lines, j), next_line(lines, j)):
				break

			visual_marker = self._visual_list_marker(line)
			marker_x = visual_marker.x if visual_marker is not None else None
			is_new_item = (
				marker_x is not None
				and base_marker_x is not None
				and abs(marker_x - base_marker_x) <= max(4.0, base_size * 0.35)
				and abs(line.x0 - base_x) <= max(4.0, base_size * 0.40)
			)
			if base_marker_x is None:
				same_indent = abs(line.x0 - base_x) <= max(4.0, base_size * 0.40)
				base_text = plain_text(line_text_tokens(base_line)).strip()
				is_new_item = same_indent and (
					not groups
					or (
						not text[:1].islower()
						and self._missing_bullet_stem(text)
						== self._missing_bullet_stem(base_text)
					)
				)
			elif marker_x is None:
				same_indent = abs(line.x0 - base_x) <= max(4.0, base_size * 0.40)
				is_new_item = same_indent and len(groups) >= 2 and not text[:1].islower()
			if is_new_item:
				groups.append([line])
			elif (
				groups
				and last_line is not None
				and abs(line.x0 - base_x) <= max(4.0, base_size * 0.40)
				and 0 < line.y0 - last_line.y0 <= max(base_size * 2.0, 22.0)
			):
				groups[-1].append(line)
			else:
				break

			last_line = line
			j += 1

		rendered = []
		for group in groups:
			marker = self._visual_list_marker(group[0])
			rendered.append(
				indent
				+ self._visual_marker_prefix(marker)
				+ self._render_paragraph(
					group,
					preserve_layout=False,
					preserve_hard_breaks=False,
				)
			)
		return [self._indent_list_item_continuations(item, indent) for item in rendered], j

	def _missing_bullet_stem(self, text: str) -> str:
		words = re.findall(r"[A-Za-z]+", text.lower())
		return " ".join(words[:2])

	def _indent_list_item_continuations(self, item: str, indent: str) -> str:
		lines = item.split("\n")
		if len(lines) <= 1:
			return item
		return "\n".join([lines[0]] + [indent + "  " + line for line in lines[1:]])

	def _render_paragraph(
		self,
		lines: List[Line],
		preserve_layout: bool = True,
		preserve_hard_breaks: bool = True,
	) -> str:
		force_bold = bool(lines and id(lines[0]) in self._panel_label_lines(lines[0].page))
		suppress_mono = self._is_monospace_prose(lines)

		def transform(line: Line, tokens: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
			out: List[Dict[str, Any]] = []
			for token in tokens:
				updated = dict(token)
				style = list(updated["style"])
				if force_bold and line is lines[0]:
					style[0] = True
				if suppress_mono:
					style[2] = False
				updated["style"] = tuple(style)
				out.append(updated)
			return out

		text = escape_block_start(
			render_paragraph(
				lines,
				self.conv.options,
				self._paragraph_separator if preserve_hard_breaks else self._list_item_separator,
				transform,
			)
		)
		if not preserve_layout:
			return text
		plain = " ".join(plain_text(line_text_tokens(line)).strip() for line in lines)
		if is_rtl_dominant_text(plain):
			from .html.render import render_inline_fragment

			return '<p dir="rtl">%s</p>' % render_inline_fragment(text)
		if any(line.writing_mode != "horizontal" for line in lines):
			from .html.render import render_inline_fragment

			return (
				'<p style="writing-mode: vertical-rl; text-orientation: mixed;">%s</p>'
				% render_inline_fragment(text)
			)
		layout = self._paragraph_layout(lines)
		if layout == "center":
			return '<p align="center">%s</p>' % text
		if layout == "right":
			return '<p align="right">%s</p>' % text
		if layout and layout.startswith("indent:"):
			return '<p style="text-indent: %sem;">%s</p>' % (layout.split(":", 1)[1], text)
		return text

	def _is_monospace_prose(self, lines: List[Line]) -> bool:
		if not lines or any(line.mono_ratio < 0.85 for line in lines):
			return False
		text = " ".join(plain_text(line_text_tokens(line)).strip() for line in lines)
		words = re.findall(r"[A-Za-z]{2,}", text)
		if len(words) < 8 or text[-1:] not in ".!?":
			return False
		return not re.search(r"(?:^|\s)(?:def|class|return|import|SELECT|INSERT)\b|[{};=]", text)

	def _panel_label_lines(self, page: int) -> set[int]:
		cached = self._panel_label_cache.get(page)
		if cached is not None:
			return cached
		page_width, _page_height = self.conv.page_sizes.get(page, (612.0, 792.0))
		lines = self.lines_by_page.get(page, [])
		segments = [
			segment
			for segment in self.conv.segments
			if segment.page == page and (segment.horizontal or segment.vertical) and segment.length > 5.0
		]
		labels: set[int] = set()
		for component in segment_components(segments):
			horizontal = [segment for segment in component if segment.horizontal]
			vertical = [segment for segment in component if segment.vertical]
			if len(horizontal) < 2 or len(vertical) < 2:
				continue
			xs = cluster_values([(segment.x0 + segment.x1) / 2 for segment in vertical], 2.0)
			ys = cluster_values([(segment.y0 + segment.y1) / 2 for segment in horizontal], 2.0)
			if len(xs) != 2 or len(ys) != 2:
				continue
			x0, x1 = min(xs), max(xs)
			y0, y1 = min(ys), max(ys)
			if not (90.0 <= x1 - x0 <= page_width * 0.65 and 40.0 <= y1 - y0 <= 150.0):
				continue
			inside = [
				line
				for line in lines
				if x0 <= (line.x0 + line.x1) / 2 <= x1
				and y0 <= (line.y0 + line.y1) / 2 <= y1
				and plain_text(line_text_tokens(line)).strip()
			]
			inside.sort(key=lambda line: (line.y0, line.x0, line.seq))
			if len(inside) < 2:
				continue
			label = inside[0]
			label_text = plain_text(line_text_tokens(label)).strip()
			if (
				0 < len(label_text) <= 32
				and label.x1 - label.x0 <= (x1 - x0) * 0.55
				and label.y0 - y0 <= max(label.size * 2.0, 24.0)
				and inside[1].y0 - label.y0 <= max(label.size * 2.2, 26.0)
			):
				labels.add(id(label))
		self._panel_label_cache[page] = labels
		return labels

	def _paragraph_layout(self, lines: List[Line]) -> Optional[str]:
		if not lines:
			return None
		if any(line.writing_mode != "horizontal" for line in lines):
			return None
		if any(line.page != lines[0].page for line in lines):
			return None
		frame_left, frame_right = self._text_frame(lines[0].page)
		frame_width = max(frame_right - frame_left, 1.0)
		if len(lines) == 1:
			line = lines[0]
			text = plain_text(line_text_tokens(line)).strip()
			if not text or line.size <= 0:
				return None
			line_width = line.x1 - line.x0
			if line_width >= frame_width * 0.82:
				return None
			center_delta = abs(((line.x0 + line.x1) / 2) - ((frame_left + frame_right) / 2))
			left_delta = line.x0 - frame_left
			right_delta = frame_right - line.x1
			tol = max(5.0, line.size * 0.75)
			side_margin = max(16.0, line.size * 1.4)
			if center_delta <= tol and left_delta > side_margin and right_delta > side_margin:
				return "center"
			if abs(right_delta) <= tol and left_delta > max(24.0, line.size * 2.0):
				return "right"
			return None
		rest_left = min(line.x0 for line in lines[1:])
		indent = lines[0].x0 - rest_left
		if indent > max(lines[0].size * 1.2, 10.0):
			em = max(0.1, indent / max(lines[0].size, 1.0))
			return "indent:%.1f" % em
		return None

	def _text_frame(self, page: int) -> Tuple[float, float]:
		cached = self._text_frame_cache.get(page)
		if cached is not None:
			return cached
		page_width, page_height = self.conv.page_sizes.get(page, (612, 792))
		all_lines = [line for line in self.lines_by_page.get(page, []) if plain_text(line_text_tokens(line)).strip() and line.size > 0]
		if not all_lines:
			frame = (72.0, page_width - 72.0)
			self._text_frame_cache[page] = frame
			return frame
		body_size = self._body_font_size(all_lines)
		candidates = [line for line in all_lines if line.mono_ratio < 0.85 and line.size <= body_size * 1.35]
		if not candidates:
			candidates = all_lines
		interior = [
			line
			for line in candidates
			if page_height * 0.07 <= (line.y0 + line.y1) / 2 <= page_height * 0.93
		]
		if len(interior) >= 2:
			# Running headers and footers often span farther than the authored
			# text frame. Let interior content define alignment and indentation
			# whenever the page supplies enough evidence.
			candidates = interior
		lefts = [line.x0 for line in candidates if line.x0 < page_width * 0.55]
		rights = [line.x1 for line in candidates if line.x1 > page_width * 0.25]
		if lefts:
			left_bands: Dict[int, List[float]] = {}
			for x in lefts:
				left_bands.setdefault(int(round(x / 4.0)), []).append(x)
			left = median(max(left_bands.values(), key=lambda values: (len(values), -median(values))))
		else:
			left = min(line.x0 for line in all_lines)
		right = max(rights) if rights else max(line.x1 for line in all_lines)
		if right <= left + 40:
			right = page_width - left
		frame = (left, right)
		self._text_frame_cache[page] = frame
		return frame

	def _paragraph_separator(self, prev: Line, cur: Line, lines: List[Line]) -> str:
		if self._hard_line_break(prev, cur, lines):
			# A bare newline is only a CommonMark soft break. Preserve a verified
			# document line break with Markdown's two-space hard-break syntax.
			return "  \n"
		return self._soft_paragraph_separator(prev, cur, lines)

	def _soft_paragraph_separator(self, prev: Line, cur: Line, _lines: List[Line]) -> str:
		if id(prev) not in self._panel_label_lines(prev.page):
			prev_text = plain_text(line_text_tokens(prev)).rstrip()
			cur_text = plain_text(line_text_tokens(cur)).lstrip()
			if joins_without_word_space(prev_text, cur_text):
				return ""
		return " "

	def _list_item_separator(self, _prev: Line, _cur: Line, _lines: List[Line]) -> str:
		# A continuation line stays inside the same Markdown list item. Keep its
		# source line boundary without promoting it to a rendered hard break.
		return "\n"

	def _hard_line_break(self, prev: Line, cur: Line, lines: List[Line]) -> bool:
		if prev.page != cur.page:
			return True
		if abs(prev.x0 - cur.x0) > max(prev.size * 0.8, 8):
			return False
		prev_text = plain_text(line_text_tokens(prev)).strip()
		cur_text = plain_text(line_text_tokens(cur)).strip()
		if not prev_text or not cur_text:
			return False
		if hyphen_join_mode(prev_text, cur_text):
			return False
		if self._in_hard_break_block(prev, cur, lines):
			return True
		available = self._available_width(prev)
		width_ratio = (prev.x1 - prev.x0) / max(available, 1.0)
		if width_ratio >= 0.82:
			return False
		if prev_text.endswith(",") and cur_text[:1].isupper() and width_ratio < 0.75:
			return True
		if prev_text[-1:] in ".!?:;" and cur_text[:1].isupper():
			return True
		visible = [char for char in sorted(prev.chars, key=lambda char: (char.x0, char.seq)) if char.text.strip()]
		link_ends_short_line = bool(visible and visible[-1].link) and width_ratio < 0.75
		if link_ends_short_line:
			return True
		return False

	def _in_hard_break_block(self, prev: Line, cur: Line, lines: List[Line]) -> bool:
		try:
			idx = lines.index(prev)
		except ValueError:
			return False
		if idx + 1 >= len(lines) or lines[idx + 1] is not cur:
			return False

		def short(line: Line) -> bool:
			available = self._available_width(line)
			return (line.x1 - line.x0) / max(available, 1.0) < 0.68

		def compatible(left: Line, right: Line) -> bool:
			if left.page != right.page or not short(left) or not short(right):
				return False
			if abs(left.x0 - right.x0) > max(5.0, left.size * 0.45):
				return False
			if abs(left.size - right.size) > 1.0:
				return False
			if abs(left.bold_ratio - right.bold_ratio) > 0.30:
				return False
			gap = right.y0 - left.y0
			return 0 < gap <= max(left.size * 1.80, 22.0)

		start = idx
		while start > 0 and compatible(lines[start - 1], lines[start]):
			start -= 1
		end = idx + 1
		while end + 1 < len(lines) and compatible(lines[end], lines[end + 1]):
			end += 1

		# Requiring at least three compatible short lines avoids treating the
		# ordinary short final line of a wrapped paragraph as a hard-break block.
		group = lines[start : end + 1]
		if len(group) < 3:
			return False
		texts = [plain_text(line_text_tokens(line)).strip() for line in group]
		terminal_breaks = sum(1 for text in texts[:-1] if text[-1:] in ".!?:;")
		italic_lines = sum(
			1
			for line in group
			if line.chars
			and sum(1 for char in line.chars if char.text.strip() and char.italic)
			>= max(1, math.ceil(sum(1 for char in line.chars if char.text.strip()) * 0.80))
		)
		return bool(
			texts[0].endswith(":")
			or terminal_breaks >= max(2, math.ceil((len(texts) - 1) * 0.60))
			or italic_lines == len(group)
		)

	def _available_width(self, line: Line) -> float:
		cached = self._available_width_cache.get(id(line))
		if cached is not None:
			return cached
		page_width, _height = self.conv.page_sizes.get(line.page, (612, 792))
		left_margin = min((l.x0 for l in self.lines_by_page.get(line.page, []) if plain_text(line_text_tokens(l)).strip()), default=72)
		right_edge = page_width - left_margin
		for sep_x, y0, y1 in self._column_separator_infos(line.page):
			if y0 - 8 <= line.y0 <= y1 + 8:
				if line.x0 < sep_x:
					right_edge = min(right_edge, sep_x - 24)
				else:
					right_edge = page_width - left_margin
		available = max(20.0, right_edge - line.x0)
		self._available_width_cache[id(line)] = available
		return available

	def _missing_bullet_indent(self, lines: List[Line], i: int) -> str:
		if i <= 0:
			return ""
		prev = lines[i - 1]
		prev_text = plain_text(line_text_tokens(prev)).strip().lower()
		prev_marker = list_marker(prev_text)
		if prev_marker and lines[i].x0 - prev.x0 >= max(lines[i].size * 2.0, 18):
			prefix = "%s. " % prev_marker[2] if prev_marker[0] == "ol" else "- "
			return self._explicit_list_indent(prev, prev_marker) + " " * len(prefix)
		frame_left, _frame_right = self._text_frame(lines[i].page)
		step = max(lines[i].size * 2.2, 20.0)
		anchor_x = lines[i].x0
		visual_marker = self._visual_list_marker(lines[i])
		if visual_marker is not None:
			anchor_x = visual_marker.x
			if visual_marker.kind.startswith("task_"):
				# The visible checkbox follows the list bullet. Project back by the
				# normal marker-to-control gap before estimating nesting depth.
				anchor_x -= lines[i].size * 1.8
		level = max(0, int(round((anchor_x - frame_left) / step)) - 1)
		if level:
			return "  " * level
		return ""

	def _explicit_list_indent(self, line: Line, marker: Tuple[str, int, Union[int, str]]) -> str:
		level = self._visual_list_level(line)
		if marker[0] == "ol" and level >= 2:
			# Spaces are deterministic across Markdown renderers; tabs change width
			# with the consumer and made deeply nested lists appear flattened.
			return "   " + "  " * (level - 1)
		if marker[0] == "ol" and level == 1:
			return "   "
		return "  " * level

	def _render_list_content(self, line: Line, marker_end: int) -> str:
		remaining = marker_end
		out_tokens: List[Dict[str, Any]] = []
		for tok in line_text_tokens(line):
			text = tok["text"]
			if remaining >= len(text):
				remaining -= len(text)
				continue
			next_tok = dict(tok)
			if remaining:
				next_tok["text"] = text[remaining:]
				remaining = 0
			out_tokens.append(next_tok)
		return render_inline(out_tokens, self.conv.options).strip()

	def _visual_list_level(self, line: Line) -> int:
		frame_left, _frame_right = self._text_frame(line.page)
		delta = max(0.0, line.x0 - frame_left)
		step = max(line.size * 2.2, 24.0)
		return max(0, int(delta // step))

	def _form_appearances(self, page: int) -> List[Tuple[float, str, List[Line]]]:
		"""Recover a mixed run of printed input, checkbox, and select controls.

		These are appearance graphics, not AcroForm widgets. Generated controls
		are therefore disabled and explicitly marked as printed appearances.
		"""
		lines = self.lines_by_page.get(page, [])
		path_groups = self._control_path_groups(page)
		large_boxes: List[Tuple[Tuple[float, float, float, float], List[Segment]]] = []
		for group in path_groups:
			boxes = [seg_bbox(segment) for segment in group]
			x0 = min(box[0] for box in boxes)
			y0 = min(box[1] for box in boxes)
			x1 = max(box[2] for box in boxes)
			y1 = max(box[3] for box in boxes)
			width = x1 - x0
			height = y1 - y0
			horizontal = sum(1 for box in boxes if box[2] - box[0] >= max(4.0, (box[3] - box[1]) * 3.0))
			vertical = sum(1 for box in boxes if box[3] - box[1] >= max(4.0, (box[2] - box[0]) * 3.0))
			if 40.0 <= width <= 300.0 and 10.0 <= height <= 45.0 and horizontal >= 2 and vertical >= 1:
				large_boxes.append(((x0, y0, x1, y1), group))
		if not large_boxes:
			return []
		if len(large_boxes) >= 4 and not self._has_form_column_pattern(large_boxes):
			# Architecture diagrams and flowcharts commonly contain many short
			# labeled rectangles distributed across several columns. Printed
			# forms instead repeat controls on one or two stable alignment axes.
			# Veto the ambiguous candidate before any lines are consumed.
			return []

		controls: List[Tuple[float, str, Line]] = []
		used: set[int] = set()
		for (x0, y0, x1, y1), group in large_boxes:
			box_width = x1 - x0
			matches = [
				line
				for line in lines
				if y0 - 3.0 <= (line.y0 + line.y1) / 2 <= y1 + 3.0
				and line.x0 <= x1 + 3.0
				and line.x1 >= x0 - 180.0
			]
			if not matches:
				continue
			line = max(
				matches,
				key=lambda candidate: sum(
					1 for char in candidate.chars if x0 <= (char.x0 + char.x1) / 2 <= x1
				),
			)
			inside_chars = [char for char in line.chars if x0 <= (char.x0 + char.x1) / 2 <= x1]
			before_chars = [char for char in line.chars if (char.x0 + char.x1) / 2 < x0 - 0.5]
			if not inside_chars:
				continue
			inside = plain_text(line_text_tokens(Line(inside_chars, page, line.seq))).strip()
			before = plain_text(line_text_tokens(Line(before_chars, page, line.seq))).strip() if before_chars else ""
			chevron = sum(
				1
				for segment in group
				if not segment.horizontal
				and not segment.vertical
				and 3.0 <= segment.length <= 12.0
				and (segment.x0 + segment.x1) / 2 >= x1 - box_width * 0.25
			) >= 2
			if chevron:
				markup = (
					"<select disabled>\n"
					"  <option selected>%s</option>\n"
					"</select>"
				) % escape_html(inside)
			elif before:
				markup = '<label>%s <input type="text" value="%s" disabled /></label>' % (
					escape_html(before),
					escape_attr(inside),
				)
			else:
				continue
			controls.append((line.y0, markup, line))
			used.add(id(line))

		if not controls:
			return []
		region_y0 = min(box[0][1] for box in large_boxes) - 12.0
		region_y1 = max(box[0][3] for box in large_boxes) + 12.0
		for line in lines:
			if id(line) in used or not (region_y0 <= (line.y0 + line.y1) / 2 <= region_y1):
				continue
			marker = self._visual_list_marker(line)
			if marker is None or marker.kind not in ("task_checked", "task_unchecked"):
				continue
			checked = " checked" if marker.kind == "task_checked" else ""
			label = plain_text(line_text_tokens(line)).strip()
			controls.append(
				(
					line.y0,
					'<label><input type="checkbox"%s disabled /> %s</label>'
					% (checked, escape_html(label)),
					line,
				)
			)
			used.add(id(line))

		if len(controls) < 3 or not any("checkbox" in markup for _y, markup, _line in controls):
			return []
		controls.sort(key=lambda item: (item[0], item[2].x0))
		html = [
			'<div class="cocoapdf-form-appearance" data-cocoapdf-kind="printed">',
			"\n\n".join(markup for _y, markup, _line in controls),
			"</div>",
		]
		self.conv.doc.warn(
			"FORM_APPEARANCE_CONTROLS",
			"disabled HTML controls reconstructed from printed appearance geometry; no AcroForm semantics claimed",
			page,
		)
		return [(controls[0][0], "\n".join(html), [line for _y, _markup, line in controls])]

	@staticmethod
	def _has_form_column_pattern(
		large_boxes: Sequence[Tuple[Tuple[float, float, float, float], List[Segment]]],
	) -> bool:
		left_edges = sorted(box[0][0] for box in large_boxes)
		widths = sorted(max(1.0, box[0][2] - box[0][0]) for box in large_boxes)
		median_width = widths[len(widths) // 2]
		tolerance = max(12.0, min(30.0, median_width * 0.18))
		clusters: List[List[float]] = []
		for edge in left_edges:
			for cluster in clusters:
				center = sum(cluster) / len(cluster)
				if abs(edge - center) <= tolerance:
					cluster.append(edge)
					break
			else:
				clusters.append([edge])
		sizes = sorted((len(cluster) for cluster in clusters), reverse=True)
		count = max(len(left_edges), 1)
		if sizes and sizes[0] / count >= 0.60:
			return True
		# Preserve legitimate two-column forms while rejecting diagrams spread
		# across three or more weak axes.
		return (
			len(sizes) >= 2
			and sizes[1] >= max(2, math.ceil(count * 0.25))
			and sum(sizes[:2]) / count >= 0.85
		)

	def _control_path_groups(self, page: int) -> List[List[Segment]]:
		segments = sorted(
			[
				segment
				for segment in self.conv.segments
				if segment.page == page and segment.length <= 320.0
			],
			key=lambda segment: segment.seq,
		)
		groups: List[List[Segment]] = []
		for segment in segments:
			if groups and segment.seq - groups[-1][-1].seq <= 1:
				groups[-1].append(segment)
			else:
				groups.append([segment])
		return [group for group in groups if len(group) >= 2]

	def _callouts(self, page: int) -> List[Tuple[float, str, List[Line]]]:
		lines = self.lines_by_page.get(page, [])
		out: List[Tuple[float, str, List[Line]]] = []
		seen: set = set()
		page_width, page_height = self.conv.page_sizes.get(page, (612.0, 792.0))
		page_area = max(page_width * page_height, 1.0)
		table_boxes = [candidate[3] for candidate in self._table_candidates(page)]
		for fill in self.conv.fills:
			if fill.page != page:
				continue
			fill_width = fill.x1 - fill.x0
			fill_height = fill.y1 - fill.y0
			if fill_width < 120 or fill_height < 24 or fill_width * fill_height > page_area * 0.35:
				continue
			fill_box = (fill.x0, fill.y0, fill.x1, fill.y1)
			if any(
				rect_contains(fill_box, vector_box, pad=2.0)
				for vector_box in self._vector_boxes.get(page, [])
			):
				continue
			if any(rect_contains(fill_box, table_box, pad=2.0) for table_box in table_boxes):
				continue
			r, g, b = fill.color
			callout_lines = [
				line
				for line in lines
				if id(line) not in seen
				and fill.x0 - 2 <= (line.x0 + line.x1) / 2 <= fill.x1 + 2
				and fill.y0 - 2 <= (line.y0 + line.y1) / 2 <= fill.y1 + 2
				and plain_text(line_text_tokens(line)).strip()
			]
			if not callout_lines:
				continue
			math_html = self._display_math_html(fill, callout_lines)
			if math_html is not None:
				out.append((min(line.y0 for line in callout_lines), math_html, callout_lines))
				seen.update(id(line) for line in callout_lines)
				self.conv.doc.warn(
					"DISPLAY_MATH_GEOMETRY",
					"MathML reconstructed from operator, script, and fraction geometry",
					page,
				)
				continue
			neutral_code_gray = max(fill.color) - min(fill.color) < 0.03 and 0.90 <= r <= 0.98
			border_edges = sum(
				1
				for seg in self.conv.segments
				if seg.page == page and rect_contains(seg_bbox(seg), (fill.x0, fill.y0, fill.x1, fill.y1), pad=3.0)
			)
			if neutral_code_gray or (max(fill.color) - min(fill.color) < 0.04 and border_edges < 3):
				continue
			callout_lines.sort(key=lambda line: (line.y0, line.x0, line.seq))
			plain_body = " ".join(
				plain_text(line_text_tokens(line)).strip() for line in callout_lines
			)
			label = re.match(r"^([^:\n]{1,80}:)(?:\s+|$)(.*)$", plain_body)
			if label:
				body = "<strong>%s</strong>" % escape_html(label.group(1))
				if label.group(2):
					body += " " + escape_html(label.group(2))
			else:
				body = escape_html(plain_body)
			bg = color_to_hex(fill.color)
			html = '<div style="border: 1px solid #9bb7d3; background: %s; padding: 12px;">%s</div>' % (bg, body)
			out.append((min(line.y0 for line in callout_lines), html, callout_lines))
			seen.update(id(line) for line in callout_lines)
		out.sort(key=lambda item: item[0])
		return out

	def _display_math_html(self, fill: Fill, lines: List[Line]) -> Optional[str]:
		chars = sorted(
			{
				id(char): char
				for line in lines
				for char in line.chars
				if char.text.strip()
			}.values(),
			key=lambda char: char.seq,
		)
		if not chars or len(chars) > 64:
			return None
		math_symbols = "=+\u2212-\u00d7\u00f7*/^\u2211\u220f\u222b\u221a\u221e\u2264\u2265\u2260\u2248\u2202\u03c0\u03b8\u03b1\u03b2\u03b3\u0394\u03bb\u03bc\u03c3"
		if not any(any(symbol in char.text for symbol in math_symbols) for char in chars):
			return None
		plain = "".join(char.text for char in chars)
		if re.search(r"[A-Za-z]{4,}", plain):
			return None
		base_size = max(char.size for char in chars)
		base_bottoms = [char.y1 for char in chars if char.size >= base_size * 0.88]
		base_bottom = median(base_bottoms) if base_bottoms else median([char.y1 for char in chars])

		def script_kind(char: Char) -> Optional[str]:
			if char.size > base_size * 0.82:
				return None
			if char.y1 <= base_bottom - base_size * 0.22:
				return "sup"
			if char.y0 >= base_bottom + base_size * 0.10:
				return "sub"
			return None

		fraction: Optional[Tuple[List[Char], List[Char]]] = None
		for segment in self.conv.segments:
			if segment.page != fill.page or not segment.horizontal:
				continue
			sx0, sx1 = sorted((segment.x0, segment.x1))
			y = (segment.y0 + segment.y1) / 2
			if not (fill.x0 <= sx0 < sx1 <= fill.x1) or not (6.0 <= sx1 - sx0 <= (fill.x1 - fill.x0) * 0.35):
				continue
			above = [
				char for char in chars
				if sx0 - 3.0 <= (char.x0 + char.x1) / 2 <= sx1 + 3.0
				and 0 <= y - char.y1 <= base_size * 1.5
			]
			below = [
				char for char in chars
				if sx0 - 3.0 <= (char.x0 + char.x1) / 2 <= sx1 + 3.0
				and 0 <= char.y0 - y <= base_size * 1.5
			]
			if above and below:
				fraction = (sorted(above, key=lambda char: char.seq), sorted(below, key=lambda char: char.seq))
				break

		def atom(char: Char) -> str:
			text = escape_html(char.text)
			if char.text.isdigit():
				return "<mn>%s</mn>" % text
			if any(unicodedata.category(value).startswith("L") for value in char.text):
				return "<mi>%s</mi>" % text
			return "<mo>%s</mo>" % text

		def row(sequence: List[Char]) -> str:
			if sequence and sequence[0].text == "\u221a":
				return "<msqrt><mrow>%s</mrow></msqrt>" % "".join(atom(char) for char in sequence[1:])
			return "<mrow>%s</mrow>" % "".join(atom(char) for char in sequence)

		fraction_ids: set[int] = set()
		fraction_start: Optional[int] = None
		fraction_html = ""
		if fraction is not None:
			numerator, denominator = fraction
			fraction_ids = {id(char) for char in numerator + denominator}
			fraction_start = min(char.seq for char in numerator)
			fraction_html = "<mfrac>%s%s</mfrac>" % (row(numerator), row(denominator))

		def script_row(sequence: List[Char]) -> str:
			parts: List[str] = []
			i = 0
			while i < len(sequence):
				current = sequence[i]
				if (
					i + 1 < len(sequence)
					and sequence[i + 1].size <= current.size * 0.80
					and sequence[i + 1].y1 <= current.y1 - current.size * 0.15
				):
					parts.append("<msup>%s%s</msup>" % (atom(current), atom(sequence[i + 1])))
					i += 2
					continue
				parts.append(atom(current))
				i += 1
			return "<mrow>%s</mrow>" % "".join(parts)

		parts: List[str] = []
		i = 0
		while i < len(chars):
			char = chars[i]
			if id(char) in fraction_ids:
				if fraction_start == char.seq:
					parts.append(fraction_html)
				i += 1
				continue
			if char.text in ("\u2211", "\u220f", "\u222b"):
				j = i + 1
				subs: List[Char] = []
				sups: List[Char] = []
				while j < len(chars) and id(chars[j]) not in fraction_ids and script_kind(chars[j]) in ("sub", "sup"):
					(sups if script_kind(chars[j]) == "sup" else subs).append(chars[j])
					j += 1
				if subs and sups:
					parts.append("<msubsup>%s%s%s</msubsup>" % (atom(char), row(subs), row(sups)))
				elif subs:
					parts.append("<msub>%s%s</msub>" % (atom(char), row(subs)))
				elif sups:
					parts.append("<msup>%s%s</msup>" % (atom(char), row(sups)))
				else:
					parts.append(atom(char))
				i = j
				continue
			if script_kind(char) is None:
				j = i + 1
				scripts: List[Char] = []
				kind: Optional[str] = None
				while j < len(chars) and id(chars[j]) not in fraction_ids and script_kind(chars[j]) is not None:
					current_kind = script_kind(chars[j])
					if kind is not None and current_kind != kind:
						break
					kind = current_kind
					scripts.append(chars[j])
					j += 1
				if scripts and kind == "sup":
					parts.append("<msup>%s%s</msup>" % (atom(char), script_row(scripts)))
					i = j
					continue
				if scripts and kind == "sub":
					parts.append("<msub>%s%s</msub>" % (atom(char), script_row(scripts)))
					i = j
					continue
			parts.append(atom(char))
			i += 1
		return '<math display="block"><mrow>%s</mrow></math>' % "".join(parts)

	def _rules(self, page: int) -> List[Tuple[float, str]]:
		out = []
		table_boxes = [cand[3] for cand in self._table_candidates(page)]
		width, height = self.conv.page_sizes.get(page, (612, 792))
		lines = self.lines_by_page.get(page, [])
		body_size = body_font_size([l for ls in self.lines_by_page.values() for l in ls])
		for seg in self.conv.segments:
			if seg.page == page and seg.horizontal and seg.length >= width * 0.30 and seg.width <= 3:
				sx0, sx1 = sorted((seg.x0, seg.x1))
				sy0, sy1 = sorted((seg.y0, seg.y1))
				if self._is_repeated_margin_rule(seg):
					continue
				if any(
					rect_contains(
						(sx0, sy0, sx1, sy1),
						vector_box,
						pad=2.0,
					)
					for vector_box in self._vector_boxes.get(page, [])
				):
					continue
				if any(rect_contains((sx0, sy0, sx1, sy1), box, pad=2.0) for box in table_boxes):
					continue
				if self._rule_looks_like_footnote_separator(seg, width, height, lines):
					continue
				if self._segment_on_fill_border(seg):
					continue
				if self._is_repeated_parallel_rule(seg):
					continue
				if self._rule_near_heading(seg, lines, body_size):
					continue
				# Avoid a rule that physically crosses glyph bounds. A font-size
				# distance check also swallowed legitimate rules immediately before
				# large headings even though their rectangles never intersected.
				vertical_pad = max(1.0, seg.width)
				if any(
					l.page == page
					and not (l.x1 < sx0 or l.x0 > sx1)
					and l.y0 <= sy1 + vertical_pad
					and l.y1 >= sy0 - vertical_pad
					for l in lines
				):
					continue
				rule_y = min(seg.y0, seg.y1)
				if any(abs(existing_y - rule_y) <= 2.0 for existing_y, _rule in out):
					continue
				out.append((rule_y, "---"))
		return out

	def _is_repeated_margin_rule(self, segment: Segment) -> bool:
		page_width, page_height = self.conv.page_sizes.get(segment.page, (612.0, 792.0))
		y_ratio = ((segment.y0 + segment.y1) / 2) / max(page_height, 1.0)
		if 0.07 < y_ratio < 0.93:
			return False
		left_ratio = min(segment.x0, segment.x1) / max(page_width, 1.0)
		right_ratio = max(segment.x0, segment.x1) / max(page_width, 1.0)
		matching_pages = set()
		for other in self.conv.segments:
			if not other.horizontal or other.width > 3:
				continue
			other_width, other_height = self.conv.page_sizes.get(other.page, (612.0, 792.0))
			if other.length < other_width * 0.30:
				continue
			other_y_ratio = ((other.y0 + other.y1) / 2) / max(other_height, 1.0)
			other_left_ratio = min(other.x0, other.x1) / max(other_width, 1.0)
			other_right_ratio = max(other.x0, other.x1) / max(other_width, 1.0)
			if (
				abs(other_y_ratio - y_ratio) <= 0.004
				and abs(other_left_ratio - left_ratio) <= 0.01
				and abs(other_right_ratio - right_ratio) <= 0.01
			):
				matching_pages.add(other.page)
		page_count = max(1, len(self.conv.page_sizes))
		return len(matching_pages) >= max(2, math.ceil(page_count * 0.6))

	def _is_repeated_parallel_rule(self, segment: Segment) -> bool:
		"""Reject row dividers and repeated container edges as thematic rules."""
		sx0, sx1 = sorted((segment.x0, segment.x1))
		baselines: List[float] = []
		for other in self.conv.segments:
			if other.page != segment.page or not other.horizontal or other.width > 3:
				continue
			if abs(other.width - segment.width) > max(0.2, segment.width * 0.20):
				continue
			ox0, ox1 = sorted((other.x0, other.x1))
			if abs(ox0 - sx0) <= 3.0 and abs(ox1 - sx1) <= 3.0:
				baseline = (other.y0 + other.y1) / 2
				if not any(abs(baseline - existing) <= 2.5 for existing in baselines):
					baselines.append(baseline)
		if len(baselines) < 2:
			return False
		baselines.sort()
		vertical_edges = [
			other
			for other in self.conv.segments
			if other.page == segment.page
			and other.vertical
			and (
				abs((other.x0 + other.x1) / 2 - sx0) <= 3.0
				or abs((other.x0 + other.x1) / 2 - sx1) <= 3.0
			)
		]
		for y0, y1 in zip(baselines, baselines[1:]):
			left = any(
				abs((edge.x0 + edge.x1) / 2 - sx0) <= 3.0
				and min(edge.y0, edge.y1) <= y0 + 3.0
				and max(edge.y0, edge.y1) >= y1 - 3.0
				for edge in vertical_edges
			)
			right = any(
				abs((edge.x0 + edge.x1) / 2 - sx1) <= 3.0
				and min(edge.y0, edge.y1) <= y0 + 3.0
				and max(edge.y0, edge.y1) >= y1 - 3.0
				for edge in vertical_edges
			)
			if left and right:
				return True
		if len(baselines) < 3:
			return False
		page_lines = self.lines_by_page.get(segment.page, [])
		body = body_font_size(page_lines)
		occupied_intervals = 0
		for y0, y1 in zip(baselines, baselines[1:]):
			# Horizontal-only table rows form a compact repeated rhythm. Widely
			# separated standalone thematic rules can also have text between them
			# and must not be rejected merely because their endpoints repeat.
			if y1 - y0 > max(body * 4.5, 50.0):
				continue
			if any(
				line.page == segment.page
				and y0 <= (line.y0 + line.y1) / 2 <= y1
				and line.x1 >= sx0 - 3.0
				and line.x0 <= sx1 + 3.0
				and plain_text(line_text_tokens(line)).strip()
				for line in page_lines
			):
				occupied_intervals += 1
		return occupied_intervals >= 2

	def _rule_looks_like_footnote_separator(self, seg: Segment, page_width: float, page_height: float, lines: List[Line]) -> bool:
		y = (seg.y0 + seg.y1) / 2
		if y <= page_height * 0.35:
			return False
		body = self._body_font_size(lines)
		below = [line for line in lines if 0 < line.y0 - y <= 90 and line.size <= max(body * 1.05, 8.5)]
		if y > page_height * 0.65 and 40 <= seg.length <= page_width * 0.55 and below:
			return True
		for line in below:
			text = plain_text(line_text_tokens(line)).strip()
			if re.match(r"^\d{1,3}\.\s+\S", text):
				return True
		return False

	def _rule_near_heading(self, seg: Segment, lines: List[Line], body_size: float) -> bool:
		y = (seg.y0 + seg.y1) / 2
		for line in lines:
			if not self._heading_like(line, body_size):
				continue
			# Heading borders sit below the glyph box. Measuring from the text
			# top swallowed nearby independent rules for large headings, while a
			# rule above a heading must retain its negative bottom-gap sign.
			if 0 <= y - line.y1 <= max(line.size * 1.25, 18.0):
				return True
		return False

	def _heading_like(self, line: Line, body_size: float) -> bool:
		text = plain_text(line_text_tokens(line)).strip()
		return (
			bool(text)
			and len(text) < 160
			and line.size >= body_size * 0.88
			and (line.size >= body_size * 1.18 or line.bold_ratio >= 0.75)
		)

	def _segment_on_fill_border(self, seg: Segment) -> bool:
		y = (seg.y0 + seg.y1) / 2
		sx0, sx1 = sorted((seg.x0, seg.x1))
		for fill in self.conv.fills:
			if fill.page != seg.page:
				continue
			fill_width = fill.x1 - fill.x0
			fill_height = fill.y1 - fill.y0
			if fill_height <= 3.5 and fill_width >= max(seg.length * 0.9, 40.0):
				continue
			if abs(y - fill.y0) <= 2.5 or abs(y - fill.y1) <= 2.5:
				if sx0 >= fill.x0 - 3 and sx1 <= fill.x1 + 3:
					return True
		return False

	def _tables(self, page: int) -> List[Tuple[float, str, List[Line]]]:
		return [(y, md, lines) for y, md, lines, _box in self._table_candidates(page)]

	def _table_candidates(self, page: int) -> List[Tuple[float, str, List[Line], Tuple[float, float, float, float]]]:
		cached = self._table_cache.get(page)
		if cached is not None:
			return cached
		segs = [s for s in self.conv.segments if s.page == page and (s.horizontal or s.vertical) and s.length > 5]
		out: List[Tuple[float, str, List[Line], Tuple[float, float, float, float]]] = []
		for comp in segment_components(segs):
			hs = [s for s in comp if s.horizontal]
			vs = [s for s in comp if s.vertical]
			if len(hs) < 2 or len(vs) < 2:
				continue
			xs = cluster_values([(s.x0 + s.x1) / 2 for s in vs], 2.0)
			ys = cluster_values([(s.y0 + s.y1) / 2 for s in hs], 2.0)
			if len(xs) < 2 or len(ys) < 2:
				continue
			x0, x1 = min(xs), max(xs)
			y0, y1 = min(ys), max(ys)
			rows = len(ys) - 1
			cols = len(xs) - 1
			if rows < 1 or cols < 1:
				continue
			# A simple box or SVG frame is not a table; one-row tables still need at least two columns.
			if rows == 1 and cols == 1:
				continue
			if (x1 - x0) < 35 or (y1 - y0) < 12:
				continue
			coverage = grid_coverage(xs, ys, hs, vs)
			if coverage < 0.55:
				continue
			lines = [
				l
				for l in self.lines_by_page.get(page, [])
				if x0 - 2 <= (l.x0 + l.x1) / 2 <= x1 + 2 and y0 - 2 <= (l.y0 + l.y1) / 2 <= y1 + 2
			]
			if not lines:
				continue
			caption_line = self._table_caption_before(page, y0, x0, x1)
			note_line = self._table_note_after(page, y1, x0, x1)
			caption_text = plain_text(line_text_tokens(caption_line)).strip() if caption_line else ""
			if cols == 1 and caption_line is not None and self._borderless_rows_from_lines(lines):
				continue
			if any(line.writing_mode != "horizontal" for line in lines):
				table_lines = list(lines) + ([caption_line] if caption_line is not None else []) + ([note_line] if note_line is not None else [])
				out.append(
					(
						caption_line.y0 if caption_line else y0,
						self._render_spanned_table_html(page, xs, ys, lines, caption_text),
						table_lines,
						(x0, y0, x1, y1),
					)
				)
				continue

			if not lattice_has_all_cell_edges(xs, ys, hs, vs):
				if rows >= 2 and cols >= 2:
					fallback_lines = sorted(lines, key=lambda line: (line.y0, line.x0, line.seq))
					table_lines = list(fallback_lines) + ([caption_line] if caption_line is not None else []) + ([note_line] if note_line is not None else [])
					self.conv.doc.warn(
						"TABLE_SPAN_UNSUPPORTED",
						"partial lattice emitted as HTML fallback; span inference approximate",
						page,
					)
					out.append(
						(
							caption_line.y0 if caption_line else y0,
							self._render_spanned_table_html(page, xs, ys, fallback_lines, caption_text),
							table_lines,
							(x0, y0, x1, y1),
						)
					)
					continue
				fallback_lines = sorted(lines, key=lambda line: (line.y0, line.x0, line.seq))
				if caption_line is not None:
					fallback_lines = [caption_line] + fallback_lines
				if note_line is not None:
					fallback_lines = fallback_lines + [note_line]
				fallback = "\n\n".join(
					escape_block_start(
						render_inline(line_text_tokens(line), self.conv.options).strip()
					)
					for line in fallback_lines
					if plain_text(line_text_tokens(line)).strip()
				)
				self.conv.doc.warn(
					"TABLE_SPAN_UNSUPPORTED",
					"partial lattice preserved as text; span inference unavailable",
					page,
				)
				out.append((caption_line.y0 if caption_line else y0, fallback, fallback_lines, (x0, y0, x1, y1)))
				continue

			cells = [["" for _ in range(cols)] for _ in range(rows)]
			styled_cells: List[List[List[Tuple[str, Tuple[bool, bool, bool, bool, bool, bool, bool, bool]]]]] = [
				[[] for _ in range(cols)] for _ in range(rows)
			]
			cell_extents: List[List[List[Tuple[float, float]]]] = [
				[[] for _ in range(cols)] for _ in range(rows)
			]
			for line in lines:
				cy = (line.y0 + line.y1) / 2
				row = max(0, min(rows - 1, find_interval(ys, cy)))
				for txt, wx0, _wy0, wx1, _wy1, style in styled_word_boxes(line):
					cx = (wx0 + wx1) / 2
					col = max(0, min(cols - 1, find_interval(xs, cx)))
					cells[row][col] = (cells[row][col] + " " + txt).strip()
					styled_cells[row][col].append((txt, style))
					cell_extents[row][col].append((wx0, wx1))
			markdown_cells = [
				[self._styled_table_cell_markdown(words) for words in row]
				for row in styled_cells
			]
			top_lines = [
				line
				for line in lines
				if ys[0] <= (line.y0 + line.y1) / 2 <= ys[1]
			]
			top_chars = [char for line in top_lines for char in line.chars if char.text.strip()]
			bold_header = bool(top_chars) and sum(1 for char in top_chars if char.bold) / len(top_chars) >= 0.70
			fill_header = any(
				fill.page == page
				and fill.x0 <= x0 + 2
				and fill.x1 >= x1 - 2
				and fill.y0 <= ys[0] + 2
				and fill.y1 >= ys[1] - 2
				for fill in self.conv.fills
			)
			if not (bold_header or fill_header):
				html = ["<table>"]
				if caption_text:
					html.append("<caption>%s</caption>" % escape_html(caption_text))
				html.append("<tbody>")
				for row in cells:
					html.append("<tr>" + "".join("<td>%s</td>" % escape_html(cell) for cell in row) + "</tr>")
				html.extend(["</tbody>", "</table>"])
				table_lines = list(lines) + ([caption_line] if caption_line is not None else []) + ([note_line] if note_line is not None else [])
				out.append((caption_line.y0 if caption_line else y0, "\n".join(html), table_lines, (x0, y0, x1, y1)))
				continue
			header = cells[0]
			body = cells[1:]
			if sum(1 for c in header if c.strip()) == 0:
				continue
			md = []
			md.append("| " + " | ".join(escape_table(c) for c in header) + " |")
			numeric_columns = {
				column
				for column in range(cols)
				if sum(1 for row in body if is_numeric_table_cell(row[column]))
				>= max(2, math.ceil(len(body) * 0.66))
			}
			centered_columns: set[int] = set()
			for column in range(cols):
				if column in numeric_columns:
					continue
				center_hits = 0
				nonempty = 0
				for row in range(rows):
					extents = cell_extents[row][column]
					if not extents:
						continue
					nonempty += 1
					content_x0 = min(extent[0] for extent in extents)
					content_x1 = max(extent[1] for extent in extents)
					left_margin = content_x0 - xs[column]
					right_margin = xs[column + 1] - content_x1
					if abs(left_margin - right_margin) <= max(5.0, (xs[column + 1] - xs[column]) * 0.08):
						center_hits += 1
				if nonempty >= 3 and center_hits >= math.ceil(nonempty * 0.75):
					centered_columns.add(column)
			md.append(
				"| "
				+ " | ".join(
					"---:" if column in numeric_columns else ":---:" if column in centered_columns else "---"
					for column in range(cols)
				)
				+ " |"
			)
			for row_index, row in enumerate(body, 1):
				md.append(
					"| "
					+ " | ".join(
						escape_rendered_table_cell(markdown_cells[row_index][column])
						for column, _cell in enumerate(row)
					)
					+ " |"
				)
			table_lines = list(lines) + ([caption_line] if caption_line is not None else []) + ([note_line] if note_line is not None else [])
			table_markdown = "\n".join(md)
			if caption_line is not None:
				table_markdown = "%s\n\n%s" % (
					self._table_caption_markdown(caption_line, caption_text),
					table_markdown,
				)
			out.append((caption_line.y0 if caption_line else y0, table_markdown, table_lines, (x0, y0, x1, y1)))
		out.extend(self._form_grid_candidates(page))
		out.extend(self._financial_statement_candidates(page))
		out.extend(self._borderless_key_value_candidates(page))
		out.extend(self._borderless_numeric_candidates(page))
		out.sort(key=lambda t: (t[0], t[3][0]))
		deduped: List[Tuple[float, str, List[Line], Tuple[float, float, float, float]]] = []
		used_lines: set = set()
		for cand in out:
			line_ids = {id(line) for line in cand[2]}
			if line_ids & used_lines:
				continue
			deduped.append(cand)
			used_lines.update(line_ids)
		self._table_cache[page] = deduped
		return deduped

	def _form_grid_candidates(self, page: int) -> List[Tuple[float, str, List[Line], Tuple[float, float, float, float]]]:
		lines = self.lines_by_page.get(page, [])
		page_width, _page_height = self.conv.page_sizes.get(page, (612.0, 792.0))
		segments = [
			segment
			for segment in self.conv.segments
			if segment.page == page and (segment.horizontal or segment.vertical) and segment.length > 5.0
		]
		control_boxes: List[Tuple[float, float, float, float]] = []
		for component in segment_components(segments):
			horizontal = [segment for segment in component if segment.horizontal]
			vertical = [segment for segment in component if segment.vertical]
			if len(horizontal) < 2 or len(vertical) < 2:
				continue
			xs = cluster_values([(segment.x0 + segment.x1) / 2 for segment in vertical], 2.0)
			ys = cluster_values([(segment.y0 + segment.y1) / 2 for segment in horizontal], 2.0)
			if len(xs) != 2 or len(ys) != 2:
				continue
			x0, x1 = min(xs), max(xs)
			y0, y1 = min(ys), max(ys)
			width = x1 - x0
			height = y1 - y0
			if width < max(100.0, page_width * 0.25) or not (18.0 <= height <= 90.0):
				continue
			if grid_coverage(xs, ys, horizontal, vertical) < 0.90:
				continue
			control_boxes.append((x0, y0, x1, y1))

		aligned_groups: List[List[Tuple[float, float, float, float]]] = []
		for box in sorted(control_boxes, key=lambda value: (value[1], value[0])):
			for group in aligned_groups:
				gx0 = median([value[0] for value in group])
				gx1 = median([value[2] for value in group])
				if abs(box[0] - gx0) <= 3.0 and abs(box[2] - gx1) <= 3.0:
					group.append(box)
					break
			else:
				aligned_groups.append([box])

		def region_text(
			x0: float,
			y0: float,
			x1: float,
			y1: float,
		) -> Tuple[str, List[Line]]:
			parts: List[Tuple[float, int, str, Line]] = []
			for line in lines:
				selected = [
					char
					for char in line.chars
					if char.text.strip()
					and x0 <= (char.x0 + char.x1) / 2 <= x1
					and y0 <= (char.y0 + char.y1) / 2 <= y1
				]
				if not selected:
					continue
				partial = Line(
					selected,
					line.page,
					line.seq,
					source_order=line.source_order,
					writing_mode=line.writing_mode,
				)
				text = plain_text(line_text_tokens(partial)).strip()
				if text:
					parts.append((partial.y0, partial.seq, text, line))
			parts.sort(key=lambda value: (value[0], value[1]))
			return cleanup_spaces(" ".join(value[2] for value in parts)), [value[3] for value in parts]

		out: List[Tuple[float, str, List[Line], Tuple[float, float, float, float]]] = []
		for group in aligned_groups:
			if len(group) < 3:
				continue
			group.sort(key=lambda value: value[1])
			control_x0 = median([box[0] for box in group])
			control_x1 = median([box[2] for box in group])
			label_x0 = max(0.0, control_x0 - min(180.0, page_width * 0.40))
			rows: List[Tuple[float, str, str, List[Line]]] = []
			valid = True
			for box in group:
				x0, y0, x1, y1 = box
				label, label_lines = region_text(label_x0, y0 - 4.0, x0 - 3.0, y1 + 4.0)
				value, value_lines = region_text(x0 + 1.0, y0 - 2.0, x1 - 1.0, y1 + 2.0)
				if not label or not value or len(label) > 48 or len(value) > 240:
					valid = False
					break
				rows.append((y0, label, value, label_lines + value_lines))
			if not valid:
				continue

			for previous, following in zip(group, group[1:]):
				band_y0 = previous[3] + 1.0
				band_y1 = following[1] - 1.0
				if band_y1 - band_y0 < 8.0:
					continue
				label, label_lines = region_text(label_x0, band_y0, control_x0 - 3.0, band_y1)
				value, value_lines = region_text(control_x0 + 1.0, band_y0, control_x1, band_y1)
				if label and value and len(label) <= 48 and len(value) <= 120:
					rows.append((band_y0, label, value, label_lines + value_lines))

			rows.sort(key=lambda value: value[0])
			if len(rows) < 3 or len({cleanup_spaces(row[1]).casefold() for row in rows}) != len(rows):
				continue
			html = ["<table>", "<tbody>"]
			for _y, label, value, _row_lines in rows:
				html.append(
					'<tr><th scope="row">%s</th><td>%s</td></tr>'
					% (escape_html(label), escape_html(value))
				)
			html.extend(["</tbody>", "</table>"])
			used_lines: List[Line] = []
			seen_lines: set[int] = set()
			for _y, _label, _value, row_lines in rows:
				for line in row_lines:
					if id(line) not in seen_lines:
						seen_lines.add(id(line))
						used_lines.append(line)
			if len(used_lines) < 3:
				continue
			self.conv.doc.warn(
				"FORM_APPEARANCE_GRID",
				"printed field/value grid reconstructed from aligned control appearances",
				page,
			)
			out.append(
				(
					min(row[0] for row in rows),
					"\n".join(html),
					used_lines,
					(
						min(min(line.x0 for line in used_lines), label_x0),
						min(row[0] for row in rows),
						control_x1,
						max(box[3] for box in group),
					),
				)
			)
		return out

	def _table_caption_before(self, page: int, y0: float, x0: float, x1: float) -> Optional[Line]:
		candidates: List[Tuple[int, float, Line]] = []
		table_center = (x0 + x1) / 2
		table_width = max(1.0, x1 - x0)
		for line in self.lines_by_page.get(page, []):
			text = plain_text(line_text_tokens(line)).strip()
			explicit = bool(re.match(r"^(?:Table|Tab\.|Exhibit)\s+[A-Za-z0-9IVXLC]+(?:[.-][A-Za-z0-9IVXLC]+)*\s*[:.]?\s+", text, re.I))
			if line.y0 >= y0:
				continue
			gap = y0 - line.y1
			center = (line.x0 + line.x1) / 2
			visual_caption = (
				bool(text)
				and len(text) <= 180
				and not text.endswith(":")
				and list_marker(text) is None
				and not self._line_is_heading_candidate(line)
				and gap <= max(line.size * 2.2, 26.0)
				and abs(center - table_center) <= max(8.0, table_width * 0.06)
				and line.x0 >= x0 - 8.0
				and line.x1 <= x1 + 8.0
			)
			if (explicit and gap <= max(line.size * 3.5, 42.0) and x0 - 50 <= center <= x1 + 50) or visual_caption:
				candidates.append((0 if explicit else 1, gap, line))
		if not candidates:
			return None
		return min(candidates, key=lambda item: (item[0], item[1]))[2]

	def _line_is_heading_candidate(self, line: Line) -> bool:
		lines = self.lines_by_page.get(line.page, [])
		index = next(
			(
				position
				for position, candidate in enumerate(lines)
				if candidate is line
			),
			-1,
		)
		if index < 0:
			return False
		body_size = self._body_font_size(
			[
				candidate
				for page_lines in self.lines_by_page.values()
				for candidate in page_lines
			]
		)
		return self._is_heading(
			line,
			body_size,
			previous_line(lines, index),
			next_line(lines, index),
		)

	def _table_note_after(self, page: int, y1: float, x0: float, x1: float) -> Optional[Line]:
		candidates: List[Tuple[float, Line]] = []
		for line in self.lines_by_page.get(page, []):
			text = plain_text(line_text_tokens(line)).strip()
			if not re.match(r"^(?:Note|Notes|Source|Sources)\s*[:.]", text, re.I):
				continue
			if line.y0 <= y1:
				continue
			gap = line.y0 - y1
			center = (line.x0 + line.x1) / 2
			if gap <= max(line.size * 4.0, 52.0) and x0 - 50 <= center <= x1 + 50:
				candidates.append((gap, line))
		if not candidates:
			return None
		return min(candidates, key=lambda item: item[0])[1]

	def _table_needs_html_fallback(self, cells: List[List[str]], caption: str) -> bool:
		return False

	def _render_complex_table_html(self, cells: List[List[str]], caption: str) -> str:
		lines = ["<table>"]
		if caption:
			lines.append("<caption>%s</caption>" % escape_html(caption))
		for row in cells:
			tag = "th" if row is cells[0] else "td"
			lines.append("<tr>" + "".join("<%s>%s</%s>" % (tag, escape_html(cell), tag) for cell in row) + "</tr>")
		lines.append("</table>")
		return "\n".join(lines)

	def _borderless_key_value_candidates(self, page: int) -> List[Tuple[float, str, List[Line], Tuple[float, float, float, float]]]:
		lines = self.lines_by_page.get(page, [])
		out: List[Tuple[float, str, List[Line], Tuple[float, float, float, float]]] = []
		i = 0
		while i < len(lines):
			first = self._borderless_key_value_row(lines[i])
			if first is None:
				i += 1
				continue
			group: List[Tuple[Line, Tuple[str, str]]] = [(lines[i], first)]
			j = i + 1
			while j < len(lines):
				row = self._borderless_key_value_row(lines[j])
				if row is None:
					break
				if abs(lines[j].x0 - lines[i].x0) > max(4.0, lines[i].size * 0.40):
					break
				if lines[j].y0 - group[-1][0].y0 > max(lines[i].size * 3.0, 34.0):
					break
				group.append((lines[j], row))
				j += 1
			if len(group) < 3:
				i += 1
				continue
			row_lines = [line for line, _row in group]
			x0 = min(line.x0 for line in row_lines)
			x1 = max(line.x1 for line in row_lines)
			y0 = min(line.y0 for line in row_lines)
			y1 = max(line.y1 for line in row_lines)
			caption_line = self._table_caption_before(page, y0, x0, x1)
			if caption_line is None:
				i += 1
				continue
			caption = plain_text(line_text_tokens(caption_line)).strip()
			html = ["<table>"]
			html.append("<caption>%s</caption>" % escape_html(caption))
			html.append("<tbody>")
			for _line, (key, value) in group:
				html.append(
					'<tr><th scope="row">%s</th><td>%s</td></tr>'
					% (escape_html(key), escape_html(value))
				)
			html.extend(["</tbody>", "</table>"])
			out.append(
				(
					caption_line.y0,
					"\n".join(html),
					row_lines + [caption_line],
					(x0, caption_line.y0, x1, y1),
				)
			)
			i = j
		return out

	def _financial_statement_candidates(self, page: int) -> List[Tuple[float, str, List[Line], Tuple[float, float, float, float]]]:
		"""Recover year-column statements whose rows use dot leaders, not rules.

		Admission deliberately requires a compact all-year bold header, complete
		numeric occupancy below every year, and repeated leader evidence. This
		keeps the detector useful for annual-report statements without treating
		ordinary article columns or tables of contents as financial tables.
		"""
		lines = self.lines_by_page.get(page, [])
		out: List[Tuple[float, str, List[Line], Tuple[float, float, float, float]]] = []
		i = 0
		while i < len(lines):
			header_line = lines[i]
			header_boxes = word_boxes(header_line)
			years = sorted(
				[
					(text, (x0 + x1) / 2)
					for text, x0, _y0, x1, _y1 in header_boxes
					if re.fullmatch(r"(?:19|20)\d{2}", cleanup_spaces(text))
				],
				key=lambda item: item[1],
			)
			if (
				not (2 <= len(years) <= 5)
				or len(years) != len(header_boxes)
				or len({year for year, _center in years}) != len(years)
				or header_line.bold_ratio < 0.55
			):
				i += 1
				continue
			centers = [center for _year, center in years]
			separations = [right - left for left, right in zip(centers, centers[1:])]
			if not separations or min(separations) < max(24.0, header_line.size * 2.2):
				i += 1
				continue
			column_pitch = median(separations)
			numeric_start = centers[0] - column_pitch * 0.55
			rows: List[Tuple[Line, str, List[str], bool]] = []
			numeric_rows = 0
			leader_rows = 0
			previous_line = header_line
			j = i + 1
			while j < len(lines):
				line = lines[j]
				vertical_gap = line.y0 - previous_line.y0
				if vertical_gap > max(header_line.size * 3.2, 34.0):
					break
				if vertical_gap < -max(header_line.size, 8.0):
					# Reading order has moved to another region or column.
					break
				parsed = self._financial_statement_row(
					line,
					centers,
					numeric_start,
					column_pitch,
				)
				if parsed is None:
					break
				label, values, has_leader, section_row = parsed
				if section_row:
					rows.append((line, label, values, True))
				else:
					if (
						len(values) != len(years)
						or not all(is_financial_table_cell(value) for value in values)
					):
						break
					rows.append((line, label, values, False))
					numeric_rows += 1
					leader_rows += int(has_leader)
				previous_line = line
				j += 1
			if (
				numeric_rows < 3
				or leader_rows < max(3, math.ceil(numeric_rows * 0.60))
			):
				i += 1
				continue
			html = ["<table>", "<thead>"]
			html.append(
				'<tr><th scope="col"></th>'
				+ "".join(
					'<th scope="col">%s</th>' % escape_html(year)
					for year, _center in years
				)
				+ "</tr>"
			)
			html.extend(["</thead>", "<tbody>"])
			for _line, label, values, section_row in rows:
				if section_row:
					html.append(
						'<tr><th scope="rowgroup" colspan="%d">%s</th></tr>'
						% (len(years) + 1, escape_html(label))
					)
					continue
				html.append(
					'<tr><th scope="row">%s</th>%s</tr>'
					% (
						escape_html(label),
						"".join(
							"<td>%s</td>" % escape_html(value)
							for value in values
						),
					)
				)
			html.extend(["</tbody>", "</table>"])
			table_lines = [header_line] + [
				line for line, _label, _values, _section in rows
			]
			out.append(
				(
					header_line.y0,
					"\n".join(html),
					table_lines,
					(
						min(line.x0 for line in table_lines),
						min(line.y0 for line in table_lines),
						max(line.x1 for line in table_lines),
						max(line.y1 for line in table_lines),
					),
				)
			)
			i = j
		return out

	def _financial_statement_row(
		self,
		line: Line,
		centers: Sequence[float],
		numeric_start: float,
		column_pitch: float,
	) -> Optional[Tuple[str, List[str], bool, bool]]:
		boxes = word_boxes(line)
		if not boxes:
			return None
		label_parts: List[str] = []
		value_parts: List[List[str]] = [[] for _center in centers]
		has_leader = False
		for raw_text, x0, _y0, x1, _y1 in boxes:
			text = cleanup_spaces(raw_text).strip()
			if not text:
				continue
			if re.fullmatch(r"\.{8,}", text):
				has_leader = True
				continue
			trimmed = re.sub(r"\.{8,}$", "", text).rstrip()
			if trimmed != text:
				has_leader = True
				text = trimmed
				if not text:
					continue
			center = (x0 + x1) / 2
			if center < numeric_start:
				label_parts.append(text)
				continue
			column = min(
				range(len(centers)),
				key=lambda index: abs(center - centers[index]),
			)
			if abs(center - centers[column]) > column_pitch * 0.66:
				return None
			value_parts[column].append(text)
		label = cleanup_spaces(" ".join(label_parts)).strip()
		values = [cleanup_spaces(" ".join(parts)).strip() for parts in value_parts]
		if label and label.endswith(":") and not any(values):
			return label, values, has_leader, True
		if not label or any(not value for value in values):
			return None
		return label, values, has_leader, False

	def _borderless_numeric_candidates(self, page: int) -> List[Tuple[float, str, List[Line], Tuple[float, float, float, float]]]:
		lines = self.lines_by_page.get(page, [])
		out: List[Tuple[float, str, List[Line], Tuple[float, float, float, float]]] = []
		i = 0
		while i < len(lines):
			header_line = lines[i]
			header = self._borderless_column_cells(header_line)
			if header_line.bold_ratio < 0.70 or not (3 <= len(header) <= 8):
				i += 1
				continue
			rows: List[Tuple[Line, List[str]]] = []
			j = i + 1
			while j < len(lines):
				line = lines[j]
				if line.y0 - (rows[-1][0].y0 if rows else header_line.y0) > max(header_line.size * 3.2, 34.0):
					break
				cells = self._borderless_column_cells(line)
				if len(cells) != len(header):
					break
				rows.append((line, cells))
				j += 1
			if len(rows) < 3:
				i += 1
				continue
			numeric_columns = []
			for column in range(1, len(header)):
				hits = sum(1 for _line, cells in rows if is_numeric_table_cell(cells[column]))
				if hits >= max(2, math.ceil(len(rows) * 0.66)):
					numeric_columns.append(column)
			if len(numeric_columns) < 2:
				i += 1
				continue
			markdown = ["| " + " | ".join(escape_table(cell) for cell in header) + " |"]
			markdown.append(
				"| "
				+ " | ".join("---:" if column in numeric_columns else "---" for column in range(len(header)))
				+ " |"
			)
			for _line, cells in rows:
				markdown.append("| " + " | ".join(escape_table(cell) for cell in cells) + " |")
			table_lines = [header_line] + [line for line, _cells in rows]
			out.append(
				(
					header_line.y0,
					"\n".join(markdown),
					table_lines,
					(
						min(line.x0 for line in table_lines),
						min(line.y0 for line in table_lines),
						max(line.x1 for line in table_lines),
						max(line.y1 for line in table_lines),
					),
				)
			)
			i = j
		return out

	def _borderless_column_cells(self, line: Line) -> List[str]:
		boxes = word_boxes(line)
		if not boxes:
			return []
		groups: List[List[str]] = [[boxes[0][0]]]
		previous_right = boxes[0][3]
		for text, x0, _y0, x1, _y1 in boxes[1:]:
			if x0 - previous_right >= max(line.size * 2.0, 18.0):
				groups.append([text])
			else:
				groups[-1].append(text)
			previous_right = x1
		return [cleanup_spaces(" ".join(group)) for group in groups]

	def _borderless_rows_from_lines(self, lines: List[Line]) -> List[Tuple[Line, Tuple[str, str]]]:
		rows = []
		for line in sorted(lines, key=lambda item: (item.y0, item.x0, item.seq)):
			if not plain_text(line_text_tokens(line)).strip():
				continue
			row = self._borderless_key_value_row(line)
			if row is None:
				return []
			rows.append((line, row))
		return rows if len(rows) >= 3 else []

	def _borderless_key_value_row(self, line: Line) -> Optional[Tuple[str, str]]:
		tokens = line_text_tokens(line)
		key_parts: List[str] = []
		value_tokens: List[Dict[str, Any]] = []
		seen_value = False
		for tok in tokens:
			text = tok["text"]
			if not seen_value and tok["style"][0] and not tok["style"][2]:
				key_parts.append(text)
				continue
			if key_parts:
				seen_value = True
				value_tokens.append(tok)
		key = cleanup_spaces("".join(key_parts))
		value = render_inline(value_tokens, self.conv.options).strip()
		if not key or not value:
			return None
		if len(key) > 40 or len(value) > 120:
			return None
		return key, value

	def _table_caption_markdown(self, line: Line, text: str) -> str:
		page_width, _page_height = self.conv.page_sizes.get(line.page, (612.0, 792.0))
		center = (line.x0 + line.x1) / 2
		if abs(center - page_width / 2) <= page_width * 0.08:
			return '<p align="center">%s</p>' % escape_html(text)
		return escape_block_start(render_inline(line_text_tokens(line), self.conv.options).strip())

	def _render_spanned_table_html(self, page: int, xs: List[float], ys: List[float], lines: List[Line], caption: str) -> str:
		rows = len(ys) - 1
		cols = len(xs) - 1
		occupied = [[False for _ in range(cols)] for _ in range(rows)]
		cells_by_row: List[List[Dict[str, Any]]] = [[] for _ in range(rows)]
		for r in range(rows):
			for c in range(cols):
				if occupied[r][c]:
					continue
				colspan = 1
				while c + colspan < cols and not self._table_has_vertical_edge(page, xs[c + colspan], ys[r], ys[r + 1]):
					colspan += 1
				rowspan = 1
				while r + rowspan < rows and not self._table_has_horizontal_edge(page, ys[r + rowspan], xs[c], xs[c + colspan]):
					rowspan += 1
				for rr in range(r, min(rows, r + rowspan)):
					for cc in range(c, min(cols, c + colspan)):
						occupied[rr][cc] = True
				cells_by_row[r].append(
					{
						"row": r,
						"col": c,
						"rowspan": rowspan,
						"colspan": colspan,
						"html": self._table_cell_html(lines, xs[c], ys[r], xs[c + colspan], ys[r + rowspan], colspan),
					}
				)

		header_rows = self._table_header_rows(lines, ys)
		out = ["<table>"]
		if caption:
			out.append("<caption>%s</caption>" % escape_html(caption))
		if header_rows:
			out.append("<thead>")
			for r in range(header_rows):
				out.append(self._render_html_table_row(cells_by_row[r], "th"))
			out.append("</thead>")
			out.append("<tbody>")
			for r in range(header_rows, rows):
				out.append(self._render_html_table_row(cells_by_row[r], "td"))
			out.append("</tbody>")
		else:
			for row in cells_by_row:
				out.append(self._render_html_table_row(row, "td"))
		out.append("</table>")
		return "\n".join(out)

	def _render_html_table_row(self, cells: List[Dict[str, Any]], tag: str) -> str:
		parts = []
		for cell in cells:
			attrs = []
			if cell["rowspan"] > 1:
				attrs.append('rowspan="%d"' % cell["rowspan"])
			if cell["colspan"] > 1:
				attrs.append('colspan="%d"' % cell["colspan"])
			attr_text = " " + " ".join(attrs) if attrs else ""
			parts.append("<%s%s>%s</%s>" % (tag, attr_text, cell["html"], tag))
		return "<tr>" + "".join(parts) + "</tr>"

	def _table_header_rows(self, lines: List[Line], ys: List[float]) -> int:
		count = 0
		for r in range(len(ys) - 1):
			row_chars = [
				char
				for line in lines
				if ys[r] - 2 <= (line.y0 + line.y1) / 2 <= ys[r + 1] + 2
				for char in line.chars
				if char.text.strip()
			]
			if not row_chars:
				break
			if sum(1 for char in row_chars if char.bold) / len(row_chars) < 0.70:
				break
			count += 1
		return count

	def _table_cell_html(self, lines: List[Line], x0: float, y0: float, x1: float, y1: float, colspan: int) -> str:
		line_parts: List[Tuple[str, str, Optional[VisualListMarker]]] = []
		for line in sorted(lines, key=lambda item: (item.y0, item.x0, item.seq)):
			words: List[Tuple[str, str]] = []
			for text, wx0, wy0, wx1, wy1, style in styled_word_boxes(line):
				cx = (wx0 + wx1) / 2
				cy = (wy0 + wy1) / 2
				if x0 - 2 <= cx <= x1 + 2 and y0 - 2 <= cy <= y1 + 2:
					words.append((text, styled_word_html(text, style)))
			if words:
				plain = cleanup_spaces(" ".join(text for text, _html in words))
				rendered = " ".join(html for _text, html in words)
				if line.writing_mode != "horizontal":
					rendered = '<span style="writing-mode: vertical-rl; text-orientation: mixed;">%s</span>' % rendered
				line_parts.append((plain, rendered, self._visual_list_marker(line)))
		if len(line_parts) >= 2:
			markers = [list_marker(plain) for plain, _html, _visual in line_parts]
			if all(marker is not None or visual is not None for marker, (_plain, _html, visual) in zip(markers, line_parts)):
				items = []
				for (plain, rendered, visual), marker in zip(line_parts, markers):
					if marker is not None:
						body = plain[marker[1] :].strip()
						items.append("<li>%s</li>" % escape_html(body))
					else:
						assert visual is not None
						items.append("<li>%s</li>" % rendered)
				return "<ul>" + "".join(items) + "</ul>"
		return "<br />".join(rendered for _plain, rendered, _visual in line_parts)

	def _styled_table_cell_markdown(
		self,
		words: List[Tuple[str, Tuple[bool, bool, bool, bool, bool, bool, bool, bool]]],
	) -> str:
		if not words:
			return ""
		groups: List[Tuple[Tuple[bool, bool, bool, bool, bool, bool, bool, bool], List[str]]] = []
		for text, style in words:
			if groups and groups[-1][0] == style:
				groups[-1][1].append(text)
			else:
				groups.append((style, [text]))
		parts = [
			render_inline([{"text": " ".join(texts), "style": style, "link": None}], self.conv.options)
			for style, texts in groups
		]
		return " ".join(part for part in parts if part)

	def _table_has_vertical_edge(self, page: int, x: float, y0: float, y1: float) -> bool:
		intervals: List[Tuple[float, float]] = []
		for seg in self.conv.segments:
			if seg.page != page or not seg.vertical:
				continue
			sx = (seg.x0 + seg.x1) / 2
			sy0, sy1 = sorted((seg.y0, seg.y1))
			if abs(sx - x) <= 2.0 and sy1 >= y0 - 2.0 and sy0 <= y1 + 2.0:
				intervals.append((max(sy0, y0), min(sy1, y1)))
		return interval_covered(intervals, y0, y1, tolerance=3.0)

	def _table_has_horizontal_edge(self, page: int, y: float, x0: float, x1: float) -> bool:
		intervals: List[Tuple[float, float]] = []
		for seg in self.conv.segments:
			if seg.page != page or not seg.horizontal:
				continue
			sy = (seg.y0 + seg.y1) / 2
			sx0, sx1 = sorted((seg.x0, seg.x1))
			if abs(sy - y) <= 2.0 and sx1 >= x0 - 2.0 and sx0 <= x1 + 2.0:
				intervals.append((max(sx0, x0), min(sx1, x1)))
		return interval_covered(intervals, x0, x1, tolerance=3.0)


def line_text_tokens(line: Line) -> List[Dict[str, Any]]:
	if line._text_tokens_cache is not None:
		return [dict(token) for token in line._text_tokens_cache]
	rtl_list_marker = source_rtl_list_marker(line.chars)
	chars = (
		sorted(line.chars, key=lambda char: char.seq)
		if rtl_list_marker is not None
		else ordered_line_chars(line)
	)
	tokens: List[Dict[str, Any]] = []
	prev: Optional[Char] = None
	base_size = line.size or 1.0
	base_y0_vals = [c.y0 for c in chars if c.text.strip() and c.size >= base_size * 0.92]
	base_y0 = median(base_y0_vals) if base_y0_vals else line.y0
	for index, ch in enumerate(chars):
		display_text = ch.text
		if display_text == "\u00ad":
			has_visible_after = any(other.text.strip() for other in chars[index + 1 :])
			display_text = "" if has_visible_after else "-"
		if not display_text:
			prev = ch
			continue
		if prev is not None:
			gap = ch.x0 - prev.x1
			threshold = max(1.2, min(prev.size * 0.45, standard_width(prev.font.base_font, " ", 278) / 1000.0 * prev.size * 0.8))
			format_control_boundary = any(
				len(candidate.text) == 1 and unicodedata.category(candidate.text) == "Cf"
				for candidate in (prev, ch)
			)
			# A few producers place intended gaps just below the nominal font
			# space width because of PDF metric rounding. Admit only a one-percent
			# tolerance and retain every script/control suppression guard.
			if (
				gap >= threshold * 0.99
				and not format_control_boundary
				and not suppress_geometric_space(prev, ch, gap)
			):
				tokens.append({"text": " ", "style": neutral_style(), "link": None, "synthetic_space": True, "page": line.page, "glyph_ids": (), "mcids": (), "object_refs": (), "bbox": None})
		style = style_key(ch, base_size, base_y0)
		mcids = tuple(sorted({
			int(mark.get("mcid"))
			for mark in ch.mc
			if isinstance(mark, dict) and isinstance(mark.get("mcid"), int) and not isinstance(mark.get("mcid"), bool)
		}))
		metadata = {
			"page": ch.page,
			"glyph_ids": (ch.seq,),
			"mcids": mcids,
			"object_refs": (ch.link_object_ref,) if ch.link_object_ref else (),
			"bbox": (ch.x0, ch.y0, ch.x1, ch.y1),
		}
		if display_text.isspace():
			if not tokens or tokens[-1]["text"] != " ":
				tokens.append({"text": " ", "style": style, "link": ch.link, "synthetic_space": False, **metadata})
		elif display_text:
			tokens.append(
				{
					"text": display_text,
					"style": style,
					"link": ch.link,
					"atomic_bidi": len(display_text) > 1,
					**metadata,
				}
			)
		prev = ch
	# Convert geometry-ordered visual runs to logical text once. This is shared
	# by the mature Markdown renderer and the semantic graph, so downstream
	# emitters must not apply a second bidi reversal.
	repaired = repair_bidi_tokens(tokens)
	if rtl_list_marker is not None:
		repaired = restore_rtl_list_marker(repaired, rtl_list_marker)
	merged = merge_tokens(repaired)
	line._text_tokens_cache = tuple(dict(token) for token in merged)
	return [dict(token) for token in line._text_tokens_cache]


_RTL_SOURCE_LIST_MARKERS = frozenset(
	"\u2022\u25e6\u25aa\u25ab\u2023\u2219\u25cf\u25cb\u25a0\uf0a7\uf0b7"
)


def source_rtl_list_marker(
	chars: Sequence[Char],
) -> Optional[Tuple[str, int, Dict[str, Any]]]:
	first_visible = min(
		(
			char for char in chars
			if char.text and not char.text.isspace()
		),
		key=lambda char: char.seq,
		default=None,
	)
	if (
		first_visible is None
		or first_visible.text not in _RTL_SOURCE_LIST_MARKERS
	):
		return None
	visible = sorted(
		(
			char for char in chars
			if char.text and not char.text.isspace()
		),
		key=lambda char: char.seq,
	)
	body = "".join(char.text for char in visible[1:])
	if not is_rtl_dominant_text(body):
		return None
	marker = visible[0]
	style = style_key(marker, marker.size or 1.0, marker.y0)
	mcids = tuple(sorted({
		int(mark.get("mcid"))
		for mark in marker.mc
		if (
			isinstance(mark, dict)
			and isinstance(mark.get("mcid"), int)
			and not isinstance(mark.get("mcid"), bool)
		)
	}))
	token = {
		"text": marker.text,
		"style": style,
		"link": marker.link,
		"page": marker.page,
		"glyph_ids": (marker.seq,),
		"mcids": mcids,
		"object_refs": (marker.link_object_ref,) if marker.link_object_ref else (),
		"bbox": (marker.x0, marker.y0, marker.x1, marker.y1),
	}
	return marker.text, marker.seq, token


def restore_rtl_list_marker(
	tokens: List[Dict[str, Any]],
	marker: Tuple[str, int, Dict[str, Any]],
) -> List[Dict[str, Any]]:
	marker_text, marker_seq, marker_token = marker
	target_index = next(
		(
			index for index, token in enumerate(tokens)
			if marker_seq in tuple(token.get("glyph_ids", ()))
		),
		None,
	)
	if target_index is None:
		target_index = next(
			(
				index for index, token in enumerate(tokens)
				if marker_text in str(token.get("text", ""))
			),
			None,
		)
	if target_index is None:
		return tokens
	target_text = str(tokens[target_index].get("text", ""))
	text_index = target_text.find(marker_text)
	if text_index < 0:
		return tokens
	body: List[Dict[str, Any]] = []
	for index, token in enumerate(tokens):
		if index != target_index:
			body.append(dict(token))
			continue
		for part in (
			target_text[:text_index],
			target_text[text_index + len(marker_text) :],
		):
			if not part:
				continue
			fragment = dict(token)
			fragment["text"] = part
			fragment["glyph_ids"] = tuple(
				glyph_id
				for glyph_id in fragment.get("glyph_ids", ())
				if glyph_id != marker_seq
			)
			body.append(fragment)
	while body and str(body[0].get("text", "")).isspace():
		body.pop(0)
	while body and str(body[-1].get("text", "")).isspace():
		body.pop()
	space = {
		"text": " ",
		"style": neutral_style(),
		"link": None,
		"synthetic_space": True,
		"page": marker_token.get("page"),
		"glyph_ids": (),
		"mcids": (),
		"object_refs": (),
		"bbox": None,
	}
	return [dict(marker_token), space] + body


def ordered_line_chars(line: Line) -> List[Char]:
	if line.source_order:
		return sorted(line.chars, key=lambda char: char.seq)
	ordered = sorted(line.chars, key=lambda char: (char.x0, char.seq))
	# Zero-width directional/word controls have no reliable x coordinate. When
	# their immediate source-sequence neighbors are matching brackets, retain
	# that local logical order while geometry continues to order the rest.
	source = sorted(line.chars, key=lambda char: char.seq)
	for index, char in enumerate(source[1:-1], 1):
		if len(char.text) != 1 or unicodedata.category(char.text) != "Cf":
			continue
		opening = source[index - 1]
		closing = source[index + 1]
		if opening.text != "[" or closing.text != "]":
			continue
		ordered = [candidate for candidate in ordered if id(candidate) != id(char)]
		opening_index = next(
			(position for position, candidate in enumerate(ordered) if id(candidate) == id(opening)),
			None,
		)
		closing_index = next(
			(position for position, candidate in enumerate(ordered) if id(candidate) == id(closing)),
			None,
		)
		if opening_index is None or closing_index is None:
			continue
		insert_at = opening_index + 1 if opening_index < closing_index else closing_index
		ordered.insert(insert_at, char)
	return ordered


def word_boxes(line: Line) -> List[Tuple[str, float, float, float, float]]:
	chars = [char for char in ordered_line_chars(line) if char.text]
	words: List[Tuple[str, float, float, float, float]] = []
	cur: List[Char] = []
	prev: Optional[Char] = None
	for ch in chars:
		gap = (ch.x0 - prev.x1) if prev is not None else 0.0
		threshold = max(1.2, min(ch.size * 0.45, standard_width(ch.font.base_font, " ", 278) / 1000.0 * ch.size * 0.8))
		if ch.text.isspace():
			if cur:
				words.append(chars_to_word(cur))
				cur = []
			prev = ch
			continue
		if prev is not None and gap > threshold and not suppress_geometric_space(prev, ch, gap):
			if cur:
				words.append(chars_to_word(cur))
				cur = []
		cur.append(ch)
		prev = ch
	if cur:
		words.append(chars_to_word(cur))
	return words


def styled_word_boxes(line: Line) -> List[Tuple[str, float, float, float, float, Tuple[bool, bool, bool, bool, bool, bool, bool, bool]]]:
	chars = [char for char in ordered_line_chars(line) if char.text]
	words: List[Tuple[str, float, float, float, float, Tuple[bool, bool, bool, bool, bool, bool, bool, bool]]] = []
	cur: List[Char] = []
	prev: Optional[Char] = None
	base_size = line.size or 1.0
	base_y0_vals = [c.y0 for c in chars if c.text.strip() and c.size >= base_size * 0.92]
	base_y0 = median(base_y0_vals) if base_y0_vals else line.y0

	def push_current() -> None:
		if not cur:
			return
		text, x0, y0, x1, y1 = chars_to_word(cur)
		styles = [style_key(char, base_size, base_y0) for char in cur if char.text.strip()]
		style = max(set(styles), key=styles.count) if styles else neutral_style()
		words.append((text, x0, y0, x1, y1, style))

	for ch in chars:
		gap = (ch.x0 - prev.x1) if prev is not None else 0.0
		threshold = max(1.2, min(ch.size * 0.45, standard_width(ch.font.base_font, " ", 278) / 1000.0 * ch.size * 0.8))
		if ch.text.isspace():
			push_current()
			cur = []
			prev = ch
			continue
		if prev is not None and gap > threshold and not suppress_geometric_space(prev, ch, gap):
			push_current()
			cur = []
		cur.append(ch)
		prev = ch
	push_current()
	return words


def styled_word_html(text: str, style: Tuple[bool, bool, bool, bool, bool, bool, bool, bool]) -> str:
	out = escape_html(text)
	if style[2]:
		out = "<code>%s</code>" % out
	if style[5]:
		out = "<sup>%s</sup>" % out
	elif style[6]:
		out = "<sub>%s</sub>" % out
	if style[7]:
		out = "<mark>%s</mark>" % out
	return out


def interval_covered(intervals: List[Tuple[float, float]], start: float, end: float, tolerance: float = 1.0) -> bool:
	if start > end:
		start, end = end, start
	merged_end = start
	for left, right in sorted((min(a, b), max(a, b)) for a, b in intervals):
		if right < merged_end - tolerance:
			continue
		if left > merged_end + tolerance:
			return False
		merged_end = max(merged_end, right)
		if merged_end >= end - tolerance:
			return True
	return merged_end >= end - tolerance


def chars_to_word(chars: List[Char]) -> Tuple[str, float, float, float, float]:
	parts: List[str] = []
	for index, char in enumerate(chars):
		if char.text == "\u00ad":
			has_visible_after = any(other.text.strip() for other in chars[index + 1 :])
			if not has_visible_after:
				parts.append("-")
			continue
		parts.append(char.text)
	return (
		"".join(parts),
		min(c.x0 for c in chars),
		min(c.y0 for c in chars),
		max(c.x1 for c in chars),
		max(c.y1 for c in chars),
	)


def style_key(ch: Char, base_size: Optional[float] = None, base_y0: Optional[float] = None) -> Tuple[bool, bool, bool, bool, bool, bool, bool, bool]:
	sup = ch.rise > ch.size * 0.15
	sub = ch.rise < -ch.size * 0.15
	if not sup and not sub and base_size is not None and base_y0 is not None and ch.text.strip() and ch.size <= base_size * 0.9:
		dy = ch.y0 - base_y0
		if dy <= -base_size * 0.18:
			sup = True
		elif dy >= base_size * 0.18:
			sub = True
	return (ch.bold, ch.italic, ch.mono, ch.strike, ch.underline, sup, sub, ch.highlight)


def neutral_style() -> Tuple[bool, bool, bool, bool, bool, bool, bool, bool]:
	return (False, False, False, False, False, False, False, False)


def merge_tokens(tokens: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
	out: List[Dict[str, Any]] = []
	for tok in tokens:
		if (
			out
			and out[-1]["style"] == tok["style"]
			and out[-1].get("link") == tok.get("link")
			and tuple(out[-1].get("mcids", ())) == tuple(tok.get("mcids", ()))
			and tuple(out[-1].get("object_refs", ())) == tuple(tok.get("object_refs", ()))
			and not out[-1].get("synthetic_space")
			and not tok.get("synthetic_space")
		):
			out[-1]["text"] += tok["text"]
			out[-1]["glyph_ids"] = tuple(sorted(set(out[-1].get("glyph_ids", ())) | set(tok.get("glyph_ids", ()))))
			out[-1]["mcids"] = tuple(sorted(set(out[-1].get("mcids", ())) | set(tok.get("mcids", ()))))
			out[-1]["object_refs"] = tuple(sorted(set(out[-1].get("object_refs", ())) | set(tok.get("object_refs", ()))))
			left = out[-1].get("bbox")
			right = tok.get("bbox")
			if left is None:
				out[-1]["bbox"] = right
			elif right is not None:
				out[-1]["bbox"] = (
					min(left[0], right[0]),
					min(left[1], right[1]),
					max(left[2], right[2]),
					max(left[3], right[3]),
				)
		else:
			out.append(dict(tok))
	return out


def render_paragraph(
	lines: List[Line],
	options: ConvertOptions,
	line_separator: Optional[Callable[[Line, Line, List[Line]], str]] = None,
	token_transform: Optional[Callable[[Line, List[Dict[str, Any]]], List[Dict[str, Any]]]] = None,
) -> str:
	parts = []
	for i, line in enumerate(lines):
		tokens = line_text_tokens(line)
		if token_transform is not None:
			tokens = token_transform(line, tokens)
		text = render_inline(tokens, options).strip()
		if i and parts:
			prev_plain = plain_text(line_text_tokens(lines[i - 1])).rstrip()
			cur_plain = plain_text(line_text_tokens(line)).lstrip()
			if is_rtl_text(prev_plain) and not is_rtl_text(cur_plain):
				leading_terminal = re.match(r"^([.!?;,])((?:https?://|www\.)\S+)$", text)
				if leading_terminal:
					text = leading_terminal.group(2) + leading_terminal.group(1)
					cur_plain = cur_plain[1:] + cur_plain[:1]
			hyphen_mode = (
				"delete"
				if line_ends_soft_hyphen(lines[i - 1]) and cur_plain[:1].islower()
				else hyphen_join_mode(prev_plain, cur_plain)
			)
			if hyphen_mode:
				parts[-1] = parts[-1].rstrip()
				if hyphen_mode == "delete" and parts[-1].endswith("-"):
					parts[-1] = parts[-1][:-1]
				parts.append(text)
			else:
				sep = line_separator(lines[i - 1], line, lines) if line_separator else " "
				parts.append(sep + text)
		else:
			parts.append(text)
	return merge_adjacent_markdown_links(cleanup_inline("".join(parts), preserve_punctuation_spaces=True).strip())


def is_formula_like_text(text: str) -> bool:
	"""Return true for a visibly mathematical expression, not ordinary prose."""
	value = text.strip()
	if not value or len(value) > 512:
		return False
	strong_symbols = "∀∃∄∈∉∋∑∏∫√∞∂∇⊂⊃⊆⊇∧∨⇒⇔"
	if any(symbol in value for symbol in strong_symbols):
		return True
	operators = "=+−-×÷*/^≤≥≠≈±∝"
	operator_count = sum(value.count(symbol) for symbol in operators)
	math_atoms = sum(
		1
		for char in value
		if char.isdigit()
		or unicodedata.category(char) in ("Sm", "No")
		or char in "()[]{}"
	)
	return operator_count >= 2 and math_atoms >= 4


def joins_without_word_space(previous: str, current: str) -> bool:
	if not previous or not current:
		return False
	return is_no_space_script_char(previous[-1]) and is_no_space_script_char(current[0])


def line_ends_soft_hyphen(line: Line) -> bool:
	chars = sorted(line.chars, key=lambda char: (char.x0, char.seq))
	for char in reversed(chars):
		if not char.text or char.text.isspace():
			continue
		return char.text.endswith("\u00ad")
	return False


HARD_HYPHEN_PREFIXES = {
	"all",
	"anti",
	"cross",
	"eighty",
	"end",
	"ex",
	"fifty",
	"forty",
	"ill",
	"mother",
	"ninety",
	"non",
	"post",
	"pre",
	"pro",
	"quasi",
	"self",
	"seventy",
	"sixty",
	"state",
	"thirty",
	"twenty",
	"well",
}


def hyphen_join_mode(prev_text: str, cur_text: str) -> Optional[str]:
	if not prev_text.endswith("-") or not cur_text[:1].islower():
		return None
	token = prev_text.rsplit(None, 1)[-1]
	if not token.endswith("-"):
		return None
	stem = token[:-1]
	if not stem:
		return None
	if stem.isupper() and len(stem) >= 2:
		return "keep"
	last_segment = stem.rsplit("-", 1)[-1].lower()
	if "-" in stem or last_segment in HARD_HYPHEN_PREFIXES:
		return "keep"
	return "delete"


def render_inline(tokens: List[Dict[str, Any]], options: ConvertOptions) -> str:
	out = []
	for index, tok in enumerate(tokens):
		style = tok["style"]
		raw_text = tok["text"]
		link = tok.get("link")
		if tok.get("synthetic_space") and raw_text.isspace():
			next_text = str(tokens[index + 1].get("text", "")) if index + 1 < len(tokens) else ""
			if re.match(r"^[,.;:!?]", next_text):
				continue
		text = escape_inline(raw_text)
		if not text:
			continue
		if style[6]:
			text = "<sub>%s</sub>" % text
		if style[5]:
			text = "<sup>%s</sup>" % text
		if style[2] and text.strip():
			text = render_code_span(raw_text)
		if style[0] and style[1]:
			text = wrap_inline_style(text, "***")
		elif style[0]:
			text = wrap_inline_style(text, "**")
		elif style[1]:
			text = wrap_inline_style(text, "*")
		if style[3]:
			text = wrap_inline_style(text, "~~")
		# Link styling is already represented by Markdown link syntax. A painted
		# underline on linked text is normally producer presentation, not a second
		# semantic emphasis instruction.
		if style[4] and options.html_underline and not link:
			text = "<u>%s</u>" % text
		if style[7]:
			text = "<mark>%s</mark>" % text
		if link and text.strip():
			href = safe_href(link)
			if href is None:
				out.append(text)
				continue
			plain = re.sub(r"[*_`~<>]", "", text)
			if plain == href and re.match(r"(?:https?://|mailto:)", href):
				text = "<%s>" % href
			else:
				text = "[%s](%s)" % (text, href)
		out.append(text)
	return cleanup_inline("".join(out), preserve_punctuation_spaces=True)


def render_code_span(text: str) -> str:
	body = str(text).replace("\r", " ").replace("\n", " ")
	longest = max((len(run) for run in re.findall(r"`+", body)), default=0)
	fence = "`" * (longest + 1)
	if (
		body.startswith("`")
		or body.endswith("`")
		or (body.startswith(" ") and body.endswith(" ") and body.strip())
	):
		body = " " + body + " "
	return fence + body + fence


def wrap_inline_style(text: str, marker: str) -> str:
	if not text.strip():
		return text
	leading = re.match(r"^\s*", text).group(0)
	trailing = re.search(r"\s*$", text).group(0)
	core = text[len(leading) : len(text) - len(trailing) if trailing else len(text)]
	if not core:
		return text
	return "%s%s%s%s%s" % (leading, marker, core, marker, trailing)


def plain_text(tokens: List[Dict[str, Any]]) -> str:
	return cleanup_spaces("".join(tok["text"] for tok in tokens))


def merge_adjacent_markdown_links(text: str) -> str:
	pattern = re.compile(r"\[([^\]\n]+)\]\(([^()\s]+)\)(?:[ \t]*\n[ \t]*| +)\[([^\]\n]+)\]\(\2\)")
	previous = None
	while text != previous:
		previous = text
		text = pattern.sub(
			lambda match: "[%s %s](%s)" % (match.group(1), match.group(3), match.group(2)),
			text,
		)
	return text


def code_line_text(line: Line, block_left: Optional[float] = None) -> str:
	chars = sorted([c for c in line.chars if c.text], key=lambda c: (c.x0, c.seq))
	if not chars:
		return ""
	advs = [max(1.0, c.x1 - c.x0) for c in chars if c.text.strip()]
	cell = median(advs) if advs else max(1.0, line.size * 0.6)
	left = min(c.x0 for c in chars) if block_left is None else block_left
	out = []
	pos = 0
	for ch in chars:
		col = int(round((ch.x0 - left) / cell))
		if col > pos:
			out.append(" " * (col - pos))
			pos = col
		out.append(ch.text)
		pos += max(1, len(ch.text))
	text = "".join(out).rstrip()
	return re.sub(r"^(?: {4})+", lambda match: "\t" * (len(match.group(0)) // 4), text)


UNCHECKED_TASK_MARKERS = {"task-unchecked", "\u2610", "\u25a1"}
CHECKED_TASK_MARKERS = {
	"task-checked",
	"\u2611",
	"\u2612",
	"\u2713",
	"\u2714",
	"\u2717",
	"\u2718",
}


def list_marker(text: str) -> Optional[Tuple[str, int, Union[int, str]]]:
	m = re.match(r"^[\-*+]\s+\[([ xX])\]\s+", text)
	if m:
		state = "task-checked" if m.group(1).lower() == "x" else "task-unchecked"
		return ("ul", m.end(), state)
	m = re.match(r"^([\u2610\u2611\u2612\u2713\u2714\u2717\u2718\u25a1])\s+", text)
	if m:
		return ("ul", m.end(), m.group(1))
	m = re.match(
		r"^([\-*+]|[\u2022\u25e6\u25aa\u25ab\u2023\u2219\u25cf\u25cb\u25a0\uf0a7\uf0b7])\s+",
		text,
	)
	if m:
		return ("ul", m.end(), m.group(1))
	m = re.match(r"^(\(?([0-9]{1,3})[.)\]]|([A-Za-z])[.)\]]|([ivxlcdmIVXLCDM]+)[.)\]])\s+", text)
	if m:
		try:
			marker: Union[int, str] = int(m.group(2)) if m.group(2) else (m.group(3) or m.group(4) or "1")
		except ValueError:
			marker = 1
		return ("ol", m.end(), marker)
	return None


def body_font_size(lines: List[Line]) -> float:
	vals = [round(l.size * 2) / 2 for l in lines if plain_text(line_text_tokens(l)).strip()]
	if not vals:
		return 10.0
	counts: Dict[float, int] = {}
	for v in vals:
		counts[v] = counts.get(v, 0) + 1
	max_count = max(counts.values())
	return min(size for size, count in counts.items() if count == max_count)


def line_flow_position(line: Line) -> float:
	"""Return a baseline-like top position immune to superscript outliers."""
	visible = [char for char in line.chars if char.text.strip()]
	if not visible:
		return line.y0
	base = max(line.size, 0.1)
	base_chars = [char for char in visible if char.size >= base * 0.85 and abs(char.rise) <= base * 0.45]
	return median([char.y0 for char in (base_chars or visible)])


def line_flow_gap(previous: Line, current: Line) -> float:
	return line_flow_position(current) - line_flow_position(previous)


def previous_line(lines: List[Line], i: int) -> Optional[Line]:
	return lines[i - 1] if i > 0 else None


def next_line(lines: List[Line], i: int) -> Optional[Line]:
	return lines[i + 1] if i + 1 < len(lines) else None


def convert(data: bytes, options: Optional[ConvertOptions] = None) -> ConvertResult:
	return Converter(data, options).convert()


def convert_file(path: Union[str, os.PathLike[str]], options: Optional[ConvertOptions] = None) -> ConvertResult:
	effective_options = options or ConvertOptions()
	result = convert(Path(path).read_bytes(), effective_options)
	if effective_options.image_mode != "embed":
		asset_dir = Path(effective_options.assets_dir)
		if result.assets:
			asset_dir.mkdir(parents=True, exist_ok=True)
			for name, data in result.assets.items():
				(asset_dir / name).write_bytes(data)
	return result


def parse_page_selection(spec: Optional[str], total_pages: int) -> Optional[set]:
	if not spec:
		return None
	selected: set = set()
	for part in str(spec).split(","):
		item = part.strip()
		if not item:
			continue
		if "-" in item:
			left, right = item.split("-", 1)
			start = int(left) if left.strip() else 1
			end = int(right) if right.strip() else total_pages
			if start > end:
				start, end = end, start
			selected.update(page for page in range(start, end + 1) if 1 <= page <= total_pages)
		else:
			page = int(item)
			if 1 <= page <= total_pages:
				selected.add(page)
	return selected


def image_source(img: ImageItem, options: ConvertOptions) -> str:
	if options.image_mode == "embed":
		return "data:%s;base64,%s" % (image_mime_type(img.name), base64.b64encode(img.data).decode("ascii"))
	reference_dir = options.asset_reference_dir
	if reference_dir is None:
		reference_dir = options.assets_dir
	prefix = str(reference_dir).replace("\\", "/").rstrip("/")
	return "%s/%s" % (prefix, img.name) if prefix and prefix != "." else img.name


def image_mime_type(name: str) -> str:
	lower = name.lower()
	if lower.endswith(".jpg") or lower.endswith(".jpeg"):
		return "image/jpeg"
	if lower.endswith(".png"):
		return "image/png"
	if lower.endswith(".jp2"):
		return "image/jp2"
	if lower.endswith(".svg"):
		return "image/svg+xml"
	return "application/octet-stream"


def image_alignment(img: ImageItem, page_width: float) -> str:
	left = img.x0
	right = max(0.0, page_width - img.x1)
	if abs(left - right) <= max(4.0, page_width * 0.03):
		return "center"
	if right < left * 0.45:
		return "right"
	return "left"


def image_style_for_alignment(align: str) -> str:
	if align == "center":
		return "display: block; margin-left: auto; margin-right: auto;"
	if align == "right":
		return "display: block; margin-left: auto;"
	return ""


def escape_attr(value: str) -> str:
	return (
		str(value)
		.replace("&", "&amp;")
		.replace('"', "&quot;")
		.replace("<", "&lt;")
		.replace(">", "&gt;")
	)


def decode_name(raw: bytes) -> str:
	def repl(m: re.Match[bytes]) -> bytes:
		return bytes([int(m.group(1), 16)])
	return re.sub(rb"#([0-9A-Fa-f]{2})", repl, raw).decode("latin1", "replace")


PDFDOC_OVERRIDES: Dict[int, str] = {
	0x16: "\u0017",
	0x18: "\u02d8", 0x19: "\u02c7", 0x1A: "\u02c6", 0x1B: "\u02d9",
	0x1C: "\u02dd", 0x1D: "\u02db", 0x1E: "\u02da", 0x1F: "\u02dc",
	0x80: "\u2022", 0x81: "\u2020", 0x82: "\u2021", 0x83: "\u2026",
	0x84: "\u2014", 0x85: "\u2013", 0x86: "\u0192", 0x87: "\u2044",
	0x88: "\u2039", 0x89: "\u203a", 0x8A: "\u2212", 0x8B: "\u2030",
	0x8C: "\u201e", 0x8D: "\u201c", 0x8E: "\u201d", 0x8F: "\u2018",
	0x90: "\u2019", 0x91: "\u201a", 0x92: "\u2122", 0x93: "\ufb01",
	0x94: "\ufb02", 0x95: "\u0141", 0x96: "\u0152", 0x97: "\u0160",
	0x98: "\u0178", 0x99: "\u017d", 0x9A: "\u0131", 0x9B: "\u0142",
	0x9C: "\u0153", 0x9D: "\u0161", 0x9E: "\u017e", 0xA0: "\u20ac",
	0x7F: "\ufffd", 0x9F: "\ufffd", 0xAD: "\ufffd",
}


def decode_pdfdocencoding(raw: bytes) -> str:
	chars: List[str] = []
	for value in raw:
		if value in PDFDOC_OVERRIDES:
			chars.append(PDFDOC_OVERRIDES[value])
		else:
			chars.append(chr(value))
	return "".join(chars)


def indirect_ref_text(value: Any) -> Optional[str]:
	"""Return a stable PDF indirect-object identifier without resolving it."""
	number = getattr(value, "num", None)
	generation = getattr(value, "gen", 0)
	if isinstance(number, int) and not isinstance(number, bool):
		return "%d %d R" % (number, int(generation or 0))
	return None


def decode_pdf_text(obj: Any) -> str:
	if isinstance(obj, bytes):
		if obj.startswith(b"\xfe\xff"):
			return obj[2:].decode("utf-16-be", "replace")
		if obj.startswith(b"\xef\xbb\xbf"):
			return obj[3:].decode("utf-8", "replace")
		return decode_pdfdocencoding(obj)
	return str(obj)


WINANSI_EXTRA = {
	0x7F: "\u2022",
	0x80: "\u20ac",
	0x82: "\u201a",
	0x83: "\u0192",
	0x84: "\u201e",
	0x85: "\u2026",
	0x86: "\u2020",
	0x87: "\u2021",
	0x88: "\u02c6",
	0x89: "\u2030",
	0x8A: "\u0160",
	0x8B: "\u2039",
	0x8C: "\u0152",
	0x91: "\u2018",
	0x92: "\u2019",
	0x93: "\u201c",
	0x94: "\u201d",
	0x95: "\u2022",
	0x96: "\u2013",
	0x97: "\u2014",
	0x99: "\u2122",
	0x9A: "\u0161",
	0x9B: "\u203a",
	0x9C: "\u0153",
	0x9F: "\u0178",
}


def winansi_char(code: int, base_font: str = "") -> str:
	if "ZapfDingbats" in base_font and code in (0x6C, 0x6D, 0x6E, 0x75):
		return "\u2022"
	if code == 0x09:
		return "\t"
	if 0x20 <= code <= 0x7E:
		return chr(code)
	if code in WINANSI_EXTRA:
		return WINANSI_EXTRA[code]
	if 0xA0 <= code <= 0xFF:
		return bytes([code]).decode("latin1")
	if 0x80 <= code <= 0x9F:
		# Undefined WinAnsi codes must remain visibly unknown. Mapping them to a
		# bullet fabricates list semantics and violates deterministic decoding.
		return "\ufffd"
	return ""


def normalize_ligatures(text: str) -> str:
	return (
		text.replace("\ufb00", "ff")
		.replace("\ufb01", "fi")
		.replace("\ufb02", "fl")
		.replace("\ufb03", "ffi")
		.replace("\ufb04", "ffl")
		.replace("\u00a0", " ")
	)


def sanitize_decoded_text(text: str) -> str:
	return "".join(ch for ch in text if ord(ch) >= 32 or ch in "\t\n\r")


def repair_bidi_tokens(tokens: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
	if not any(is_rtl_text(tok.get("text", "")) for tok in tokens):
		return tokens
	if not any(
		unicodedata.category(char).startswith("L")
		and unicodedata.bidirectional(char) in {"R", "AL"}
		for tok in tokens
		for char in str(tok.get("text", ""))
	):
		# Directional formatting/control characters such as RLM are content,
		# but are not evidence that the surrounding LTR line is visually
		# reversed. Reordering such a line displaces its brackets and controls.
		return tokens
	atoms: List[Dict[str, Any]] = []
	for tok in tokens:
		text = str(tok.get("text", ""))
		units = [text] if tok.get("atomic_bidi") and is_rtl_text(text) else list(text)
		for unit in units:
			atom = dict(tok)
			atom["text"] = unit
			atoms.append(atom)
	rtl_count = sum(len(atom["text"]) for atom in atoms if is_rtl_char(atom["text"]))
	ltr_count = sum(len(atom["text"]) for atom in atoms if is_strong_ltr_char(atom["text"]))
	if rtl_count >= max(2, ltr_count * 2):
		return merge_tokens(repair_rtl_span(atoms))
	repaired: List[Dict[str, Any]] = []
	span: List[Dict[str, Any]] = []
	for atom in atoms:
		if is_strong_ltr_char(atom["text"]):
			if span:
				repaired.extend(repair_rtl_span(span, preserve_leading_delimiter=bool(repaired)))
				span = []
			repaired.append(atom)
		else:
			span.append(atom)
	if span:
		repaired.extend(repair_rtl_span(span, preserve_leading_delimiter=bool(repaired)))
	return merge_tokens(repaired)


def repair_rtl_span(
	atoms: List[Dict[str, Any]],
	preserve_leading_delimiter: bool = False,
) -> List[Dict[str, Any]]:
	if not any(is_rtl_char(atom["text"]) for atom in atoms):
		return atoms
	start = 0
	end = len(atoms)
	if preserve_leading_delimiter:
		first_rtl = next(
			(index for index, atom in enumerate(atoms) if is_rtl_char(atom["text"])),
			0,
		)
		prefix = atoms[:first_rtl]
		if (
			prefix
			and prefix[-1]["text"].isspace()
			and any(is_neutral_bidi_punctuation(atom["text"]) for atom in prefix)
		):
			# In mixed LTR/RTL prose, a delimiter followed by whitespace belongs
			# to the preceding LTR label ("Label: <RTL>"). A leading period in
			# an RTL-dominant visual run has no such LTR context and still moves
			# to the logical end of that run.
			start = first_rtl
	while start < end and atoms[start]["text"].isspace():
		start += 1
	while end > start and atoms[end - 1]["text"].isspace():
		end -= 1
	leading = atoms[:start]
	trailing = atoms[end:]
	body = atoms[start:end]
	units: List[Dict[str, List[Dict[str, Any]]]] = []
	i = 0
	while i < len(body):
		text = body[i]["text"]
		if text.isspace():
			i += 1
			continue
		if is_rtl_char(text) or is_combining_text(text):
			run: List[Dict[str, Any]] = []
			while i < len(body) and (
				is_rtl_char(body[i]["text"])
				or is_combining_text(body[i]["text"])
				or body[i]["text"].isspace()
			):
				run.append(body[i])
				i += 1
			run = trim_space_atoms(run)
			if run:
				units.append({"atoms": list(reversed(run)), "suffix": []})
			continue
		run = []
		while i < len(body) and not body[i]["text"].isspace() and not is_rtl_char(body[i]["text"]):
			run.append(body[i])
			i += 1
		if any(is_strong_ltr_char(atom["text"]) for atom in run):
			while run and run[0]["text"] in ".,;:!?" and len(run) > 1:
				run.append(run.pop(0))
		if units and run and all(is_neutral_bidi_punctuation(atom["text"]) for atom in run):
			units[-1]["suffix"].extend(run)
		else:
			units.append({"atoms": run, "suffix": []})
	if not units:
		return atoms
	out = list(leading)
	for unit in reversed(units):
		segment = unit["atoms"] + unit["suffix"]
		if not segment:
			continue
		if needs_bidi_inserted_space(out, segment):
			out.append({"text": " ", "style": neutral_style(), "link": None, "synthetic_space": True})
		out.extend(segment)
	out.extend(trailing)
	return repair_rtl_identifier_atoms(out)


def repair_rtl_identifier_atoms(atoms: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
	out = list(atoms)
	i = 1
	while i < len(out) - 2:
		if not out[i]["text"].isspace() or not is_rtl_char(out[i - 1]["text"]):
			i += 1
			continue
		j = i + 1
		while j < len(out) and out[j]["text"].isdigit():
			j += 1
		if j == i + 1 or j >= len(out) or out[j]["text"] not in ("-", "\u2010", "\u2011", "\u2013"):
			i += 1
			continue
		digits = out[i + 1 : j]
		hyphen = out[j]
		out[i : j + 1] = [hyphen] + digits
		i += len(digits) + 1
	return out


def trim_space_atoms(atoms: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
	start = 0
	end = len(atoms)
	while start < end and atoms[start]["text"].isspace():
		start += 1
	while end > start and atoms[end - 1]["text"].isspace():
		end -= 1
	return atoms[start:end]


def needs_bidi_inserted_space(left: List[Dict[str, Any]], right: List[Dict[str, Any]]) -> bool:
	if not left or not right:
		return False
	a = left[-1]["text"]
	b = right[0]["text"]
	if a.isspace() or b.isspace():
		return False
	if is_neutral_bidi_punctuation(b) and b not in "([{":
		return False
	return True


def suppress_geometric_space(prev: Char, cur: Char, gap: float) -> bool:
	if (
		prev.text
		and cur.text
		and is_no_space_script_char(prev.text[-1])
		and is_no_space_script_char(cur.text[0])
	):
		return True
	if gap > max(prev.size, cur.size) * 0.85:
		return False
	return is_complex_script_text(prev.text) and is_complex_script_text(cur.text)


def is_no_space_script_char(char: str) -> bool:
	if not char:
		return False
	code = ord(char)
	return bool(
		0x0E00 <= code <= 0x0E7F
		or 0x1780 <= code <= 0x17FF
		or 0x3040 <= code <= 0x30FF
		or 0x31F0 <= code <= 0x31FF
		or 0x3400 <= code <= 0x4DBF
		or 0x4E00 <= code <= 0x9FFF
		or 0xF900 <= code <= 0xFAFF
		or 0x20000 <= code <= 0x3134F
		or 0x3000 <= code <= 0x303F
	)


def is_rtl_text(text: str) -> bool:
	return any(is_rtl_char(ch) for ch in text)


def is_rtl_dominant_text(text: str) -> bool:
	rtl = sum(1 for char in text if is_rtl_char(char))
	ltr = sum(1 for char in text if is_strong_ltr_char(char))
	return rtl >= max(2, ltr * 2)


def is_rtl_char(ch: str) -> bool:
	return bool(ch) and all(
		unicodedata.bidirectional(value) in {"R", "AL"}
		or unicodedata.category(value).startswith("M")
		for value in ch
	)


def is_strong_ltr_char(ch: str) -> bool:
	return bool(ch) and all(unicodedata.bidirectional(value) == "L" for value in ch)


def is_combining_text(text: str) -> bool:
	return bool(text) and all(unicodedata.category(value).startswith("M") for value in text)


def is_neutral_bidi_punctuation(ch: str) -> bool:
	return bool(ch) and not ch.isalnum() and not ch.isspace() and not is_rtl_char(ch) and not is_strong_ltr_char(ch)


def is_complex_script_text(text: str) -> bool:
	for ch in text:
		code = ord(ch)
		if unicodedata.category(ch).startswith("M"):
			return True
		if (
			0x0900 <= code <= 0x0D7F
			or 0x0D80 <= code <= 0x0DFF
			or 0x1780 <= code <= 0x17FF
			or 0xA800 <= code <= 0xA82F
			or 0xA880 <= code <= 0xA8DF
		):
			return True
	return False


def actual_text_from_props(props: Any) -> Optional[str]:
	if not isinstance(props, dict):
		return None
	if "ActualText" not in props:
		return None
	return decode_pdf_text_value(props.get("ActualText"))


def active_actual_text_mark(marked_content: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
	for mark in reversed(marked_content):
		if mark.get("actual_text") is not None:
			return mark
	return None


def decode_pdf_text_value(value: Any) -> Optional[str]:
	if isinstance(value, bytes):
		return decode_pdf_text(value)
	if isinstance(value, str):
		return value
	return None


def strip_subset(name: str) -> str:
	return re.sub(r"^[A-Z]{6}\+", "", name)


WIDTHS = {
	" ": 278,
	"i": 222,
	"l": 222,
	"I": 278,
	"m": 833,
	"w": 722,
	"M": 833,
	"W": 944,
	".": 278,
	",": 278,
	":": 278,
	";": 278,
	"-": 333,
	"t": 278,
	"f": 278,
	"r": 333,
}


def standard_width(base_font: str, ch: str, default: float = 500.0) -> float:
	if not ch:
		return default
	if "Courier" in base_font:
		return 600.0
	if ch in WIDTHS:
		return float(WIDTHS[ch])
	if ch.isupper():
		return 667.0
	if ch.isdigit():
		return 556.0
	return 500.0


def parse_tounicode(data: bytes) -> Dict[bytes, str]:
	from .fonts.decoding import parse_tounicode as parse

	return parse(data)


def utf16_hex(hexstr: str) -> str:
	data = bytes.fromhex(hexstr)
	try:
		return normalize_ligatures(data.decode("utf-16-be"))
	except UnicodeDecodeError:
		return data.decode("latin1", "replace")


def parse_w_array(w: Any) -> Dict[int, float]:
	out: Dict[int, float] = {}
	if not isinstance(w, list):
		return out
	i = 0
	while i < len(w):
		if not isinstance(w[i], int):
			i += 1
			continue
		start = w[i]
		if i + 1 < len(w) and isinstance(w[i + 1], list):
			for off, val in enumerate(w[i + 1]):
				if isinstance(val, (int, float)):
					out[start + off] = float(val)
			i += 2
		elif i + 2 < len(w) and isinstance(w[i + 1], int) and isinstance(w[i + 2], (int, float)):
			for code in range(start, w[i + 1] + 1):
				out[code] = float(w[i + 2])
			i += 3
		else:
			i += 1
	return out


def parse_w2_array(w: Any) -> Dict[int, Tuple[float, float, float]]:
	out: Dict[int, Tuple[float, float, float]] = {}
	if not isinstance(w, list):
		return out
	i = 0
	while i < len(w):
		if not isinstance(w[i], int):
			i += 1
			continue
		start = w[i]
		if i + 1 < len(w) and isinstance(w[i + 1], list):
			values = w[i + 1]
			for offset in range(0, len(values) - 2, 3):
				triple = values[offset : offset + 3]
				if all(isinstance(value, (int, float)) for value in triple):
					out[start + offset // 3] = tuple(float(value) for value in triple)  # type: ignore[assignment]
			i += 2
		elif (
			i + 4 < len(w)
			and isinstance(w[i + 1], int)
			and all(isinstance(value, (int, float)) for value in w[i + 2 : i + 5])
		):
			metrics = tuple(float(value) for value in w[i + 2 : i + 5])
			for code in range(start, w[i + 1] + 1):
				out[code] = metrics  # type: ignore[assignment]
			i += 5
		else:
			i += 1
	return out


def identity() -> Tuple[float, float, float, float, float, float]:
	return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def page_normalization_transform(
	x0: float,
	y0: float,
	width: float,
	height: float,
	rotate: int,
	user_unit: float,
) -> Tuple[Tuple[float, float, float, float, float, float], float, float]:
	shift = (1.0, 0.0, 0.0, 1.0, -x0, -y0)
	if rotate == 90:
		rotation = (0.0, -1.0, 1.0, 0.0, 0.0, width)
		display_width, display_height = height, width
	elif rotate == 180:
		rotation = (-1.0, 0.0, 0.0, -1.0, width, height)
		display_width, display_height = width, height
	elif rotate == 270:
		rotation = (0.0, 1.0, -1.0, 0.0, height, 0.0)
		display_width, display_height = height, width
	else:
		rotation = identity()
		display_width, display_height = width, height

	scale = (user_unit, 0.0, 0.0, user_unit, 0.0, 0.0)
	return (
		mat_mul(scale, mat_mul(rotation, shift)),
		display_width * user_unit,
		display_height * user_unit,
	)


def translate(x: float, y: float) -> Tuple[float, float, float, float, float, float]:
	return (1.0, 0.0, 0.0, 1.0, x, y)


def mat_mul(m1: Tuple[float, float, float, float, float, float], m2: Tuple[float, float, float, float, float, float]) -> Tuple[float, float, float, float, float, float]:
	a1, b1, c1, d1, e1, f1 = m1
	a2, b2, c2, d2, e2, f2 = m2
	return (
		a1 * a2 + c1 * b2,
		b1 * a2 + d1 * b2,
		a1 * c2 + c1 * d2,
		b1 * c2 + d1 * d2,
		a1 * e2 + c1 * f2 + e1,
		b1 * e2 + d1 * f2 + f1,
	)


def apply_mat(m: Tuple[float, float, float, float, float, float], x: float, y: float) -> Tuple[float, float]:
	a, b, c, d, e, f = m
	return (a * x + c * y + e, b * x + d * y + f)


def apply_vec(m: Tuple[float, float, float, float, float, float], x: float, y: float) -> Tuple[float, float]:
	a, b, c, d, _e, _f = m
	return (a * x + c * y, b * x + d * y)


def ascii_hex_decode(data: bytes) -> bytes:
	from .cos.filters import ascii_hex_decode as decode

	return decode(data)


def run_length_decode(data: bytes) -> bytes:
	from .cos.filters import run_length_decode as decode

	return decode(data)


def apply_predictor(data: bytes, parms: Optional[Dict[str, Any]]) -> bytes:
	from .cos.filters import apply_predictor as decode_predictor

	return decode_predictor(data, parms)


def paeth(a: int, b: int, c: int) -> int:
	from .cos.filters import paeth as paeth_predictor

	return paeth_predictor(a, b, c)


def resolve_image_colorspace(doc: PdfDocument, value: PdfObj) -> str:
	colorspace = doc.resolve(value)
	if isinstance(colorspace, (PdfName, str)):
		return str(colorspace).lstrip("/")
	if isinstance(colorspace, list) and colorspace:
		kind = str(doc.resolve(colorspace[0])).lstrip("/")
		if kind == "ICCBased" and len(colorspace) > 1:
			profile = doc.resolve(colorspace[1])
			if isinstance(profile, Stream):
				components = doc.resolve_number(profile.attrs.get("N"))
				if components == 1:
					return "DeviceGray"
				if components == 3:
					return "DeviceRGB"
				if components == 4:
					return "DeviceCMYK"
		if kind in ("DeviceGray", "DeviceRGB", "DeviceCMYK"):
			return kind
		return kind
	return "DeviceRGB"


def apply_image_decode_8(
	doc: PdfDocument,
	data: bytes,
	channels: int,
	decode_obj: PdfObj,
) -> bytes:
	decode = doc.resolve(decode_obj)
	if not isinstance(decode, list) or len(decode) < channels * 2:
		return data
	pairs = []
	for channel in range(channels):
		low = doc.resolve_number(decode[channel * 2])
		high = doc.resolve_number(decode[channel * 2 + 1])
		if low is None or high is None:
			return data
		pairs.append((low, high))
	out = bytearray(len(data))
	for index, sample in enumerate(data):
		low, high = pairs[index % channels]
		value = low + (sample / 255.0) * (high - low)
		out[index] = max(0, min(255, int(round(value * 255.0))))
	return bytes(out)



def expand_image_samples(
	data: bytes,
	width: int,
	height: int,
	channels: int,
	bits_per_component: int,
) -> Optional[bytes]:
	"""Expand row-padded PDF image samples to deterministic 8-bit channels."""
	if width <= 0 or height <= 0 or channels <= 0 or bits_per_component not in (1, 2, 4, 8, 16):
		return None
	row_bits = width * channels * bits_per_component
	row_bytes = (row_bits + 7) // 8
	if row_bytes <= 0 or len(data) < row_bytes * height:
		return None
	if bits_per_component == 8:
		out = bytearray(width * height * channels)
		for row in range(height):
			start = row * row_bytes
			destination = row * width * channels
			out[destination : destination + width * channels] = data[start : start + width * channels]
		return bytes(out)
	maximum = (1 << bits_per_component) - 1
	mask = maximum
	out = bytearray(width * height * channels)
	destination = 0
	for row_index in range(height):
		row = data[row_index * row_bytes : (row_index + 1) * row_bytes]
		accumulator = 0
		available = 0
		position = 0
		for _sample in range(width * channels):
			while available < bits_per_component:
				if position >= len(row):
					return None
				accumulator = (accumulator << 8) | row[position]
				position += 1
				available += 8
			available -= bits_per_component
			value = (accumulator >> available) & mask
			if available:
				accumulator &= (1 << available) - 1
			else:
				accumulator = 0
			out[destination] = int(round(value * 255.0 / maximum))
			destination += 1
	return bytes(out)

def image_soft_mask_alpha(
	doc: PdfDocument,
	image: Stream,
	width: int,
	height: int,
	page: int,
) -> Optional[bytes]:
	mask = doc.resolve(image.attrs.get("SMask"))
	if not isinstance(mask, Stream):
		return None
	mask_width = doc.resolve_number(mask.attrs.get("Width"))
	mask_height = doc.resolve_number(mask.attrs.get("Height"))
	mask_bpc = doc.resolve_number(mask.attrs.get("BitsPerComponent"), 8)
	if mask_width != width or mask_height != height or int(mask_bpc or 0) != 8:
		doc.warn("IMAGE_MASK_IGNORED", "unsupported soft-mask dimensions or bit depth", page)
		return None
	data = doc.decoded_stream(mask)
	expected = width * height
	if len(data) < expected:
		doc.warn("IMAGE_MASK_IGNORED", "short soft-mask raster", page)
		return None
	return apply_image_decode_8(doc, data[:expected], 1, mask.attrs.get("Decode"))


def color_contrast(
	foreground: Tuple[float, float, float],
	background: Tuple[float, float, float],
) -> float:
	def luminance(color: Tuple[float, float, float]) -> float:
		linear = []
		for component in color:
			value = max(0.0, min(1.0, float(component)))
			linear.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
		return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

	left = luminance(foreground)
	right = luminance(background)
	return (max(left, right) + 0.05) / (min(left, right) + 0.05)


def decode_png_pixels(data: bytes) -> Optional[Tuple[int, int, int, int, bytes]]:
	"""Decode the non-interlaced 8-bit PNG shapes CocoaPDF can emit."""
	if not data.startswith(b"\x89PNG\r\n\x1a\n"):
		return None
	import struct

	position = 8
	width = height = bit_depth = color_type = interlace = 0
	idat: List[bytes] = []
	while position + 12 <= len(data):
		length = struct.unpack(">I", data[position : position + 4])[0]
		kind = data[position + 4 : position + 8]
		end = position + 8 + length
		if end + 4 > len(data):
			return None
		payload = data[position + 8 : end]
		position = end + 4
		if kind == b"IHDR" and len(payload) == 13:
			width, height, bit_depth, color_type, _compression, _filter, interlace = struct.unpack(">IIBBBBB", payload)
		elif kind == b"IDAT":
			idat.append(payload)
		elif kind == b"IEND":
			break
	channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color_type)
	if not width or not height or bit_depth != 8 or interlace != 0 or channels is None or not idat:
		return None
	try:
		encoded = zlib.decompress(b"".join(idat))
	except zlib.error:
		return None
	row_size = width * channels
	expected = height * (row_size + 1)
	if len(encoded) < expected:
		return None
	previous = bytearray(row_size)
	decoded = bytearray(width * height * channels)
	source = 0
	destination = 0
	for _row_index in range(height):
		filter_type = encoded[source]
		source += 1
		row = bytearray(encoded[source : source + row_size])
		source += row_size
		if filter_type not in (0, 1, 2, 3, 4):
			return None
		for index in range(row_size):
			left = row[index - channels] if index >= channels else 0
			up = previous[index]
			up_left = previous[index - channels] if index >= channels else 0
			if filter_type == 1:
				row[index] = (row[index] + left) & 0xFF
			elif filter_type == 2:
				row[index] = (row[index] + up) & 0xFF
			elif filter_type == 3:
				row[index] = (row[index] + ((left + up) // 2)) & 0xFF
			elif filter_type == 4:
				row[index] = (row[index] + paeth(left, up, up_left)) & 0xFF
		decoded[destination : destination + row_size] = row
		destination += row_size
		previous = row
	return width, height, color_type, channels, bytes(decoded)


def png_from_raw(
	data: bytes,
	width: int,
	height: int,
	colorspace: str,
	alpha: Optional[bytes] = None,
) -> bytes:
	if colorspace == "DeviceCMYK":
		rgb = bytearray(width * height * 3)
		for pixel in range(width * height):
			c, m, y, k = (data[pixel * 4 + offset] / 255.0 for offset in range(4))
			r, g, b = (
				1.0 - min(1.0, c + k),
				1.0 - min(1.0, m + k),
				1.0 - min(1.0, y + k),
			)
			rgb[pixel * 3 : pixel * 3 + 3] = bytes(
				max(0, min(255, int(round(component * 255.0))))
				for component in (r, g, b)
			)
		data = bytes(rgb)
		colorspace = "DeviceRGB"

	base_channels = 3 if colorspace == "DeviceRGB" else 1
	if alpha is not None:
		color_type = 6 if base_channels == 3 else 4
		channels = base_channels + 1
		interleaved = bytearray(width * height * channels)
		for pixel in range(width * height):
			src = pixel * base_channels
			dst = pixel * channels
			interleaved[dst : dst + base_channels] = data[src : src + base_channels]
			interleaved[dst + base_channels] = alpha[pixel]
		data = bytes(interleaved)
	else:
		color_type = 2 if base_channels == 3 else 0
		channels = base_channels
	stride = width * channels
	rows = []
	for y in range(height):
		row = data[y * stride : (y + 1) * stride]
		if len(row) < stride:
			row = row + b"\x00" * (stride - len(row))
		rows.append(b"\x00" + row)
	raw = b"".join(rows)
	def chunk(kind: bytes, payload: bytes) -> bytes:
		import struct
		import binascii
		return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
	import struct
	return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def cluster_values(vals: List[float], tol: float) -> List[float]:
	if not vals:
		return []
	vals = sorted(vals)
	clusters = [[vals[0]]]
	for v in vals[1:]:
		if abs(v - median(clusters[-1])) <= tol:
			clusters[-1].append(v)
		else:
			clusters.append([v])
	return [median(c) for c in clusters]


def find_interval(bounds: List[float], value: float) -> int:
	bounds = sorted(bounds)
	if len(bounds) < 2 or value <= bounds[0]:
		return 0
	if value >= bounds[-1]:
		return max(0, len(bounds) - 2)	
	for i in range(len(bounds) - 1):
		if bounds[i] <= value <= bounds[i + 1]:
			return i
	return 0


def segment_components(segs: List[Segment]) -> List[List[Segment]]:
	comps: List[List[Segment]] = []
	seen: set = set()
	for i, seg in enumerate(segs):
		if i in seen:
			continue
		stack = [i]
		seen.add(i)
		comp: List[Segment] = []
		while stack:
			idx = stack.pop()
			cur = segs[idx]
			comp.append(cur)
			for j, other in enumerate(segs):
				if j in seen:
					continue
				if segments_touch(cur, other, tol=2.5):
					seen.add(j)
					stack.append(j)
		comps.append(comp)
	return comps


def segments_touch(a: Segment, b: Segment, tol: float = 2.0) -> bool:
	ax0, ay0, ax1, ay1 = seg_bbox(a, tol)
	bx0, by0, bx1, by1 = seg_bbox(b, tol)
	if ax1 < bx0 or bx1 < ax0 or ay1 < by0 or by1 < ay0:
		return False
	# For table components we mainly want real intersections/end-point joins.
	if a.horizontal and b.vertical:
		return min(a.x0, a.x1) - tol <= b.x0 <= max(a.x0, a.x1) + tol and min(b.y0, b.y1) - tol <= a.y0 <= max(b.y0, b.y1) + tol
	if a.vertical and b.horizontal:
		return segments_touch(b, a, tol)
	if a.horizontal and b.horizontal:
		return abs(a.y0 - b.y0) <= tol and not (max(a.x0, a.x1) < min(b.x0, b.x1) - tol or max(b.x0, b.x1) < min(a.x0, a.x1) - tol)
	if a.vertical and b.vertical:
		return abs(a.x0 - b.x0) <= tol and not (max(a.y0, a.y1) < min(b.y0, b.y1) - tol or max(b.y0, b.y1) < min(a.y0, a.y1) - tol)
	return False


def seg_bbox(s: Segment, pad: float = 0.0) -> Tuple[float, float, float, float]:
	return (
		min(s.x0, s.x1) - pad,
		min(s.y0, s.y1) - pad,
		max(s.x0, s.x1) + pad,
		max(s.y0, s.y1) + pad,
	)


def grid_coverage(xs: List[float], ys: List[float], hs: List[Segment], vs: List[Segment]) -> float:
	expected_h = len(ys)
	expected_v = len(xs)
	if expected_h + expected_v == 0:
		return 0.0
	x_min, x_max = min(xs), max(xs)
	y_min, y_max = min(ys), max(ys)
	h_hits = 0
	for y in ys:
		intervals = [(min(s.x0, s.x1), max(s.x0, s.x1)) for s in hs if abs(s.y0 - y) <= 2.5]
		if interval_coverage(intervals, x_min, x_max) >= 0.75:
			h_hits += 1
	v_hits = 0
	for x in xs:
		intervals = [(min(s.y0, s.y1), max(s.y0, s.y1)) for s in vs if abs(s.x0 - x) <= 2.5]
		if interval_coverage(intervals, y_min, y_max) >= 0.75:
			v_hits += 1
	return (h_hits + v_hits) / float(expected_h + expected_v)


def lattice_has_all_cell_edges(
	xs: List[float],
	ys: List[float],
	hs: List[Segment],
	vs: List[Segment],
	min_coverage: float = 0.60,
) -> bool:
	"""Require every individual cell edge before emitting a span-free table.

	A whole-grid coverage score can still be 1.0 when an internal border is
	missing over only one row. Checking every edge interval prevents that
	partial lattice from being silently rendered as independent cells.
	"""
	for y in ys:
		intervals = [
			(min(segment.x0, segment.x1), max(segment.x0, segment.x1))
			for segment in hs
			if abs(segment.y0 - y) <= 2.5
		]
		for left, right in zip(xs, xs[1:]):
			if interval_coverage(intervals, left, right) < min_coverage:
				return False
	for x in xs:
		intervals = [
			(min(segment.y0, segment.y1), max(segment.y0, segment.y1))
			for segment in vs
			if abs(segment.x0 - x) <= 2.5
		]
		for top, bottom in zip(ys, ys[1:]):
			if interval_coverage(intervals, top, bottom) < min_coverage:
				return False
	return True


def interval_coverage(intervals: List[Tuple[float, float]], start: float, end: float) -> float:
	if end <= start or not intervals:
		return 0.0
	clipped = []
	for a, b in intervals:
		a = max(start, a)
		b = min(end, b)
		if b > a:
			clipped.append((a, b))
	if not clipped:
		return 0.0
	clipped.sort()
	total = 0.0
	cur_a, cur_b = clipped[0]
	for a, b in clipped[1:]:
		if a <= cur_b + 2.5:
			cur_b = max(cur_b, b)
		else:
			total += cur_b - cur_a
			cur_a, cur_b = a, b
	total += cur_b - cur_a
	return total / (end - start)


def rect_contains(inner: Tuple[float, float, float, float], outer: Tuple[float, float, float, float], pad: float = 0.0) -> bool:
	ix0, iy0, ix1, iy1 = inner
	ox0, oy0, ox1, oy1 = outer
	return ox0 - pad <= ix0 <= ox1 + pad and ox0 - pad <= ix1 <= ox1 + pad and oy0 - pad <= iy0 <= oy1 + pad and oy0 - pad <= iy1 <= oy1 + pad


def rects_intersect(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> bool:
	return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def stroked_bbox_intersects_clip(
	bbox: Tuple[float, float, float, float],
	line_width: float,
	clip: Tuple[float, float, float, float],
) -> bool:
	"""Test the painted stroke area, including degenerate line bboxes."""
	padding = max(abs(line_width) / 2.0, 1e-6)
	painted = (
		bbox[0] - padding,
		bbox[1] - padding,
		bbox[2] + padding,
		bbox[3] + padding,
	)
	return rects_intersect(painted, clip)


def intersect_rects(
	a: Optional[Tuple[float, float, float, float]],
	b: Optional[Tuple[float, float, float, float]],
) -> Optional[Tuple[float, float, float, float]]:
	if a is None:
		return b
	if b is None:
		return a
	x0, y0 = max(a[0], b[0]), max(a[1], b[1])
	x1, y1 = min(a[2], b[2]), min(a[3], b[3])
	if x1 <= x0 or y1 <= y0:
		# None means that no clipping path is active. Preserve an explicit
		# zero-area rectangle for an empty clip intersection.
		return (x0, y0, x0, y0)
	return (x0, y0, x1, y1)


def median(vals: Sequence[float]) -> float:
	vals = sorted(vals)
	if not vals:
		return 0.0
	mid = len(vals) // 2
	if len(vals) % 2:
		return float(vals[mid])
	return (float(vals[mid - 1]) + float(vals[mid])) / 2.0


def is_highlight_fill(fill: Fill) -> bool:
	r, g, b = fill.color
	width = fill.x1 - fill.x0
	height = fill.y1 - fill.y0
	if width <= 2 or height <= 2 or width > 240 or height > 40:
		return False
	if max(fill.color) - min(fill.color) < 0.25:
		return False
	return r >= 0.75 and g >= 0.65 and b <= 0.55


def cleanup_spaces(s: str) -> str:
	return re.sub(r"[ \t]+", " ", s).strip()


def is_east_asian_vertical_text(text: str) -> bool:
	visible = [char for char in text if not char.isspace()]
	return bool(visible) and all(
		unicodedata.east_asian_width(char) in ("W", "F")
		and unicodedata.category(char)[0] in ("L", "N", "P", "S")
		for char in visible
	)


def line_baseline_slope(line: Line) -> Optional[float]:
	chars = sorted([char for char in line.chars if char.text], key=lambda char: ((char.x0 + char.x1) / 2, char.seq))
	if len(chars) < 2:
		return None
	xs = [(char.x0 + char.x1) / 2 for char in chars]
	ys = [(char.y0 + char.y1) / 2 for char in chars]
	x_mean = sum(xs) / len(xs)
	y_mean = sum(ys) / len(ys)
	denominator = sum((x - x_mean) ** 2 for x in xs)
	if denominator <= 1e-9:
		return None
	return sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denominator


def is_financial_table_cell(text: str) -> bool:
	value = cleanup_spaces(text).strip()
	if value in {"-", "--", "---", "\u2013", "\u2014", "\u2212"}:
		return True
	without_currency = re.sub(r"[$\u20ac\u00a3\u00a5\u20b9]", "", value).strip()
	without_currency = re.sub(r"\(\s+", "(", without_currency)
	without_currency = re.sub(r"\s+\)", ")", without_currency)
	return is_numeric_table_cell(without_currency)


def is_numeric_table_cell(text: str) -> bool:
	value = cleanup_spaces(text)
	return bool(
		re.fullmatch(
			r"[+\-\u2212\u2013(]?(?:[$\u20ac\u00a3\u00a5\u20b9]\s*)?\d[\d, ]*(?:\.\d+)?%?\)?(?:\s+pp)?",
			value,
		)
	)


def cleanup_inline(s: str, preserve_punctuation_spaces: bool = False) -> str:
	# Preserve exactly two spaces before a newline: the paragraph renderer uses
	# that sequence for a verified Markdown hard break. Longer trailing runs are
	# normalized down to two by matching only while another space follows.
	s = re.sub(r" {2,}(?!\n)", " ", s)
	s = s.replace("** **", " ").replace("* *", " ")
	s = s.replace("</mark> <mark>", " ")
	s = re.sub(r" {2,}(?!\n)", " ", s)
	# Geometry-derived word gaps before punctuation are normally spurious, but
	# preserve deliberately spaced punctuation sequences such as ``- . !``.
	# In those sequences the preceding non-space character is itself punctuation.
	if not preserve_punctuation_spaces:
		s = re.sub(r"(?<![-+.!?]) +([,.;:!?])", r"\1", s)
	return s


def escape_inline(s: str) -> str:
	s = str(s).replace("\\", "\\\\")
	# Existing source entities must remain literal text rather than becoming
	# active entities inside CocoaPDF-generated raw HTML wrappers.
	s = re.sub(
		r"&(?=(?:#\d+|#x[0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]+);)",
		"&amp;",
		s,
	)
	# Backslash does not neutralize a tag when Markdown text is placed inside a
	# generated HTML block. Use an entity for tag-like source text.
	s = re.sub(r"<(?=[A-Za-z/!?])", "&lt;", s)
	for char in ("`", "*", "~", "[", "]"):
		s = s.replace(char, "\\" + char)
	# An underscore surrounded by word characters cannot open or close
	# Markdown emphasis. Preserve identifiers such as ``snake_case`` while
	# still escaping delimiter-capable underscores at word boundaries.
	s = re.sub(r"(?<![\w])_|_(?![\w])", r"\\_", s, flags=re.UNICODE)
	return s


def escape_block_start(text: str) -> str:
	ordered = re.match(r"^(\s{0,3})(\d{1,9})([.)])(?=\s|$)", text)
	if ordered:
		end = ordered.end()
		return (
			ordered.group(1)
			+ ordered.group(2)
			+ "\\"
			+ ordered.group(3)
			+ text[end:]
		)

	block = re.match(
		r"^(\s{0,3})(?:"
		r">"
		r"|#{1,6}(?=\s|$)"
		r"|[+*-](?=\s|$)"
		r"|`{3,}(?=\s|$)"
		r"|~{3,}(?=\s|$)"
		r"|-{3,}(?=\s|$)"
		r"|=+(?=\s|$)"
		r")",
		text,
	)
	if not block:
		return text
	indent = block.group(1)
	return indent + "\\" + text[len(indent):]

def escape_plain(s: str) -> str:
	return escape_block_start(escape_inline(s))


def escape_md(s: str) -> str:
	return escape_inline(s).replace("(", "\\(").replace(")", "\\)")


def escape_table(s: str) -> str:
	return escape_inline(s).replace("|", "\\|").replace("\n", "<br>").strip()


def escape_rendered_table_cell(s: str) -> str:
	"""Escape GFM cell separators without neutralizing generated inline markup."""
	return str(s).replace("|", "\\|").replace("\n", "<br>").strip()


def merge_gfm_table_blocks(previous: str, current: str) -> Optional[str]:
	"""Merge a repeated-header GFM table across one page boundary."""
	prefix = ""
	left_block = previous.rstrip()
	if not left_block.lstrip().startswith("|"):
		boundary = left_block.rfind("\n\n|")
		if boundary < 0:
			return None
		prefix = left_block[:boundary]
		left_block = left_block[boundary + 2 :]
	left = left_block.splitlines()
	right = current.rstrip().splitlines()
	separator = re.compile(r"^\|\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|$")
	if (
		len(left) < 3
		or len(right) < 3
		or left[0].strip() != right[0].strip()
		or left[1].strip() != right[1].strip()
		or not separator.fullmatch(left[1].strip())
		or any(not line.strip().startswith("|") or not line.strip().endswith("|") for line in left + right)
	):
		return None
	merged = "\n".join(left + right[2:])
	return "%s\n\n%s" % (prefix, merged) if prefix else merged


def merge_page_boundary_paragraph_blocks(previous: str, current: str) -> Optional[str]:
	"""Join a conservative lowercase prose continuation across a physical page."""
	left = previous.rstrip()
	right = current.lstrip()
	if not left or not right:
		return None
	blocked_prefixes = ("#", "|", ">", "```", "<", "- ", "* ")
	if left.startswith(blocked_prefixes) or right.startswith(blocked_prefixes):
		return None
	if re.match(r"^\d+\.\s", right) or "\n\n" in left or "\n\n" in right:
		return None
	left_plain = re.sub(r"[*_`~]+$", "", left).rstrip()
	right_plain = re.sub(r"^[*_`~]+", "", right).lstrip()
	if not left_plain or not right_plain:
		return None
	if left_plain[-1:] in ".!?:;" or not right_plain[:1].islower():
		return None
	return left + " " + right

def escape_html(s: str) -> str:
	return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def color_to_hex(color: Tuple[float, float, float]) -> str:
	vals = [max(0, min(255, int(round(c * 255)))) for c in color]
	return "#%02x%02x%02x" % tuple(vals)


def strip_wrapping_styles(s: str) -> str:
	s = s.strip()
	previous = None
	while s != previous:
		previous = s
		s = re.sub(r"^<u>(.*)</u>$", r"\1", s, flags=re.DOTALL).strip()
		s = re.sub(r"^([*_`~]+)(.*)\1$", r"\2", s, flags=re.DOTALL).strip()
	return s
