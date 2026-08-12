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
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

from . import limits
from ._textio import write_utf8_lf
from ._version import __version__
from .html.sanitize import is_unsafe_href, safe_href

PdfObj = Any


_FONT_BOLD_NAME = re.compile(r"bold|black|heavy", re.I)
_FONT_CMBX_NAME = re.compile(r"CMBX(?:SL|TI)?\d*", re.I)
_FONT_NIMBUS_BOLD_NAME = re.compile(
	r"Nimbus(?:RomNo9L|Roman)[-_]Medi(?:Ital)?",
	re.I,
)
_FONT_DEMI_SEMIBOLD_NAME = re.compile(
	r"(?:^|[-_])(?:demi(?:bold)?|semi[-_]?bold)(?:$|[-_])",
	re.I,
)
_FONT_ITALIC_NAME = re.compile(r"italic|oblique", re.I)
_FONT_MONO_NAME = re.compile(r"courier|mono|consolas|console|menlo|code", re.I)
_TAGGED_HEADING_NAME = re.compile(r"H([1-6])")


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
	image_markup: str = "markdown"
	heading_level_mode: str = "semantic"


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
	_trait_cache: Optional[Tuple[str, Tuple[bool, bool, bool]]] = field(
		default=None,
		init=False,
		repr=False,
		compare=False,
	)

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

	def _traits(self) -> Tuple[bool, bool, bool]:
		"""Return immutable name traits while preserving mutable-Font behavior."""
		cached = self._trait_cache
		if cached is not None and cached[0] == self.base_font:
			return cached[1]
		name = strip_subset(self.base_font)
		bold = bool(
			_FONT_BOLD_NAME.search(name)
			or _FONT_CMBX_NAME.fullmatch(name)
			or _FONT_NIMBUS_BOLD_NAME.fullmatch(name)
			or _FONT_DEMI_SEMIBOLD_NAME.search(name)
		)
		italic = _FONT_ITALIC_NAME.search(name) is not None
		mono = _FONT_MONO_NAME.search(name) is not None
		traits = (bold, italic, mono)
		self._trait_cache = (self.base_font, traits)
		return traits

	@property
	def bold(self) -> bool:
		return self._traits()[0]

	@property
	def italic(self) -> bool:
		return self._traits()[1]

	@property
	def mono(self) -> bool:
		return self._traits()[2]


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
	paint_order: int = 0

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
	paint_order: int = 0


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
	paint_order: int = 0


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
	glyph_ids: Tuple[int, ...] = field(default_factory=tuple, repr=False)
	object_ref: Optional[str] = None
	link_object_ref: Optional[str] = None
	paint_order: int = 0


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
	_bbox_cache: Optional[Tuple[float, float, float, float]] = field(
		default=None,
		init=False,
		repr=False,
		compare=False,
	)

	def invalidate_caches(self) -> None:
		"""Discard values derived from ``chars`` after a line is mutated."""
		self._text_tokens_cache = None
		self._size_cache = None
		self._bold_ratio_cache = None
		self._mono_ratio_cache = None
		self._bbox_cache = None

	def _bbox(self) -> Tuple[float, float, float, float]:
		if self._bbox_cache is None:
			if not self.chars:
				self._bbox_cache = (0.0, 0.0, 0.0, 0.0)
			else:
				first = self.chars[0]
				x0, y0, x1, y1 = first.x0, first.y0, first.x1, first.y1
				for char in self.chars[1:]:
					x0 = min(x0, char.x0)
					y0 = min(y0, char.y0)
					x1 = max(x1, char.x1)
					y1 = max(y1, char.y1)
				self._bbox_cache = (x0, y0, x1, y1)
		return self._bbox_cache

	@property
	def x0(self) -> float:
		return self._bbox()[0]

	@property
	def x1(self) -> float:
		return self._bbox()[2]

	@property
	def y0(self) -> float:
		return self._bbox()[1]

	@property
	def y1(self) -> float:
		return self._bbox()[3]

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
	semantic: Dict[str, Any] = field(default_factory=dict)


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
		if self.options.heading_level_mode not in {"semantic", "flat"}:
			raise ValueError("heading_level_mode must be 'semantic' or 'flat'")
		self.assets: Dict[str, bytes] = {}
		self.chars: List[Char] = []
		self.segments: List[Segment] = []
		self.fills: List[Fill] = []
		# Page-sized artifact paint is layout furniture, but it is still real
		# visual evidence for deciding whether later text is concealed.  Keep it
		# isolated from ``fills`` so it cannot become a table rule, highlight,
		# callout, region, or semantic node.
		self._artifact_page_backgrounds: Dict[int, Fill] = {}
		# Local artifact fills can still be the real visual background behind
		# authored text (for example, a coloured tagged-table header).  Preserve
		# them only for paint-order-aware contrast checks: unlike ordinary fills,
		# they must never become regions, table evidence, or output nodes.
		self._artifact_local_backgrounds: List[Fill] = []
		# Some office producers mark visible table borders as /Artifact even
		# though the enclosed text is authored content.  Retain only thin,
		# visible rule rectangles in an isolated channel.  They are geometry
		# evidence only: never expose them as page graphics, provenance glyphs,
		# regions, or ordinary table segments.
		self._artifact_rule_segments: List[Segment] = []
		self.painted_paths: List[PaintedPath] = []
		self.images: List[ImageItem] = []
		self.links: List[LinkItem] = []
		self.seq = 0
		self.paint_seq = 0
		# A content-operator clock is independent from glyph/source IDs.  It lets
		# visibility reconciliation compare complex paths, rectangles, images, and
		# text without perturbing provenance identifiers used by existing outputs.
		self.content_order = 0
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
			report["markdown_projection"] = "refused"
			report["html_projection"] = "direct_semantic_html"
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
		catalog = self.doc.catalog()
		tagged_document = None
		if isinstance(catalog, dict) and catalog.get("StructTreeRoot") is not None:
			try:
				from .semantics.tagged import parse_tagged_structure

				tagged_document = parse_tagged_structure(self.doc)
			except Exception as exc:
				self.doc.warn("TAGGED_RECONCILE_FAILED", str(exc))
			if tagged_document is not None:
				try:
					from .semantics.reconcile import refine_tagged_paragraph_events

					refine_tagged_paragraph_events(
						renderer,
						events_by_page,
						tagged_document,
					)
				except Exception as exc:
					self.doc.warn("TAGGED_BLOCK_REFINE_FAILED", str(exc))
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
		if tagged_document is not None:
			try:
				from .semantics.reconcile import reconcile_semantic_graph

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
		report["output_projection"] = "independent_semantic_projections_with_lossless_layout_reconciliation"
		try:
			from .semantics.output import render_reconciled_outputs

			markdown, html = render_reconciled_outputs(
				layout_markdown,
				semantic_document,
				report,
				self.options.image_markup,
			)
		except Exception as exc:
			self.doc.warn("SEMANTIC_OUTPUT_FAILED", str(exc))
			markdown = layout_markdown
			from .html.semantic import render_minimal_semantic_html
			from .semantics.output import _strip_layout_hints

			html = render_minimal_semantic_html(semantic_document)
			_strip_layout_hints(semantic_document)
			report["semantic_output_used"] = False
			report["output_derivation"] = {
				"markdown": "layout_renderer_fallback",
				"html": "minimal_semantic_html_fallback",
				"json": "semantic_graph",
			}
			report["markdown_projection"] = "layout_renderer_fallback"
			report["html_projection"] = "minimal_semantic_html_fallback"
			report["output_projection"] = (
				"layout_markdown_and_minimal_semantic_html_fallback"
			)
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
		page_background = self._artifact_page_backgrounds.get(char.page)
		fallback = (1.0, 1.0, 1.0)
		if (
			page_background is not None
			and page_background.x0 <= cx <= page_background.x1
			and page_background.y0 <= cy <= page_background.y1
		):
			fallback = page_background.color
		painted: List[Tuple[Tuple[int, int], str, Any]] = []

		def painted_before_text(item: Any) -> bool:
			item_order = int(getattr(item, "paint_order", 0) or 0)
			if char.paint_order > 0 and item_order > 0:
				return item_order < char.paint_order
			return item.seq < char.seq

		def ordering(item: Any) -> Tuple[int, int]:
			item_order = int(getattr(item, "paint_order", 0) or 0)
			if char.paint_order > 0 and item_order > 0:
				return (item_order, int(item.seq))
			return (int(item.seq), 0)

		for fill in self._artifact_local_backgrounds:
			if (
				fill.page == char.page
				and painted_before_text(fill)
				and fill.x0 <= cx <= fill.x1
				and fill.y0 <= cy <= fill.y1
			):
				painted.append((ordering(fill), "fill", fill))
		for fill in self.fills:
			if (
				fill.page == char.page
				and painted_before_text(fill)
				and fill.x0 <= cx <= fill.x1
				and fill.y0 <= cy <= fill.y1
			):
				painted.append((ordering(fill), "fill", fill))
		for path in self.painted_paths:
			if (
				path.page == char.page
				and painted_before_text(path)
				and path.bbox[0] <= cx <= path.bbox[2]
				and path.bbox[1] <= cy <= path.bbox[3]
				and painted_path_contains_point(path, cx, cy)
			):
				painted.append((ordering(path), "path", path))
		for image in self.images:
			if (
				image.page == char.page
				and painted_before_text(image)
				and image.x0 <= cx <= image.x1
				and image.y0 <= cy <= image.y1
			):
				painted.append((ordering(image), "image", image))
		if not painted:
			return fallback
		_seq, kind, item = max(painted, key=lambda entry: entry[0])
		if kind in ("fill", "path"):
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
		self.content_order = conv.content_order
		

	def run(self, data: bytes) -> None:
		from .content.tokens import InlineImageToken

		operands: List[Any] = []
		for tok in content_tokens(data):
			if isinstance(tok, InlineImageToken):
				self.conv.content_order += 1
				self.content_order = self.conv.content_order
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

		self.conv.content_order += 1
		self.content_order = self.conv.content_order
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
				fill_rule = "evenodd" if op == "f*" else "nonzero"
				self._record_filled_path(fill_rule)
				self._fill_path(fill_rule)
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
				paint_order=self.content_order,
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

	def _fill_path(self, fill_rule: str = "nonzero") -> None:
		if self.fill_alpha <= 0.001:
			return
		artifact = self._is_artifact()
		local_backgrounds_are_solid = self._artifact_rect_backgrounds_are_solid(
			fill_rule
		) if artifact else False
		if artifact:
			# One sequence value per paint operation preserves ordering against
			# text and other backgrounds without pretending that the rectangles
			# inside one compound path were painted independently.
			self.conv.seq += 1
			for bbox in self._artifact_path_rule_bboxes():
				self._retain_artifact_rule_segment(bbox)
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
			if artifact:
				if local_backgrounds_are_solid:
					self._retain_artifact_page_background((x0, y0, x1, y1))
					self._retain_artifact_local_background((x0, y0, x1, y1))
				self._retain_artifact_rule_segment((x0, y0, x1, y1))
				continue
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
					paint_order=self.content_order,
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

	def _artifact_rect_backgrounds_are_solid(self, fill_rule: str) -> bool:
		"""Whether artifact rectangles can be represented as independent fills.

		A compound path can use overlapping rectangles to cut a hole.  Treating
		each ``re`` as a solid background then invents paint inside that hole and
		can incorrectly suppress visible text.  Disjoint rectangles are safe under
		either PDF fill rule.  Overlapping nonzero rectangles are safe only when
		they have the same winding direction.
		"""
		if not self.path or any(kind != "re" for kind, _values in self.path):
			return False
		rectangles = []
		for _kind, values in self.path:
			x, y, width, height = values
			if abs(width) <= 1e-9 or abs(height) <= 1e-9:
				continue
			rectangles.append(
				(
					min(x, x + width),
					min(y, y + height),
					max(x, x + width),
					max(y, y + height),
					1 if width * height > 0 else -1,
				)
			)
		if not rectangles:
			return False
		for index, left in enumerate(rectangles):
			for right in rectangles[index + 1:]:
				overlap_width = min(left[2], right[2]) - max(left[0], right[0])
				overlap_height = min(left[3], right[3]) - max(left[1], right[1])
				if overlap_width <= 1e-9 or overlap_height <= 1e-9:
					continue
				if fill_rule == "evenodd" or left[4] != right[4]:
					return False
		return True

	def _artifact_path_rule_bboxes(
		self,
	) -> List[Tuple[float, float, float, float]]:
		"""Normalize exact filled rule outlines without retaining artifact art.

		Some tagged-PDF producers paint table borders as closed ``m/l/h``
		outlines instead of ``re`` rectangles.  Accept only one closed subpath
		whose transformed geometry is either a thin axis-aligned rule outline or
		an exact rectangular frame ring.  Curves, open paths, rotated shapes,
		multiple subpaths, and ordinary filled polygons remain artifacts and are
		discarded.
		"""
		if (
			not self.path
			or self.path[0][0] != "m"
			or sum(kind == "m" for kind, _values in self.path) != 1
			or any(kind not in {"m", "l"} for kind, _values in self.path)
		):
			return []

		def transformed(values: Tuple[float, ...]) -> Tuple[float, float]:
			x, y = apply_mat(self.ctm, values[0], values[1])
			return (x, self.page_height - y)

		points = [transformed(values) for _kind, values in self.path]
		close_tolerance = 0.20
		if len(points) == 5 and point_distance(points[0], points[-1]) <= close_tolerance:
			corners = points[:-1]
			x0 = min(point[0] for point in corners)
			y0 = min(point[1] for point in corners)
			x1 = max(point[0] for point in corners)
			y1 = max(point[1] for point in corners)
			width = x1 - x0
			height = y1 - y0
			thin = min(width, height)
			long = max(width, height)
			if (
				0 < thin <= 2.0
				and long >= 4.0
				and long / thin >= 4.0
				and self._thin_outline_is_axis_aligned(corners, width >= height)
			):
				visible = intersect_rects((x0, y0, x1, y1), self.clip_bbox)
				return [visible] if visible is not None else []
			return []

		# A rectangular frame can be encoded as two closed, oppositely wound
		# contours joined inside one path.  The exact repeat signature prevents
		# arbitrary compound artwork from entering the rule channel.
		if (
			len(points) != 11
			or point_distance(points[0], points[4]) > close_tolerance
			or point_distance(points[5], points[9]) > close_tolerance
			or point_distance(points[0], points[10]) > close_tolerance
		):
			return []
		outer = axis_aligned_rectangle_bbox(points[:4], close_tolerance)
		inner = axis_aligned_rectangle_bbox(points[5:9], close_tolerance)
		if outer is None or inner is None:
			return []
		ox0, oy0, ox1, oy1 = outer
		ix0, iy0, ix1, iy1 = inner
		insets = (ix0 - ox0, iy0 - oy0, ox1 - ix1, oy1 - iy1)
		if (
			any(value <= 0 or value > 2.0 for value in insets)
			or max(insets) - min(insets) > 0.25
			or ix1 <= ix0
			or iy1 <= iy0
		):
			return []
		boxes = [
			(ox0, oy0, ox1, iy0),
			(ox0, iy1, ox1, oy1),
			(ox0, iy0, ix0, iy1),
			(ix1, iy0, ox1, iy1),
		]
		visible_boxes = [intersect_rects(box, self.clip_bbox) for box in boxes]
		return [box for box in visible_boxes if box is not None]

	def _thin_outline_is_axis_aligned(
		self,
		corners: Sequence[Tuple[float, float]],
		horizontal: bool,
	) -> bool:
		edges = [
			(
				abs(right[0] - left[0]),
				abs(right[1] - left[1]),
			)
			for left, right in zip(corners, (*corners[1:], corners[0]))
		]
		if horizontal:
			long_edges = [edge for edge in edges if edge[0] >= 4.0]
			short_edges = [edge for edge in edges if edge[0] < 4.0]
			return (
				len(long_edges) == 2
				and len(short_edges) == 2
				and all(dy <= 0.20 for _dx, dy in long_edges)
				and all(max(dx, dy) <= 3.0 for dx, dy in short_edges)
			)
		long_edges = [edge for edge in edges if edge[1] >= 4.0]
		short_edges = [edge for edge in edges if edge[1] < 4.0]
		return (
			len(long_edges) == 2
			and len(short_edges) == 2
			and all(dx <= 0.20 for dx, _dy in long_edges)
			and all(max(dx, dy) <= 3.0 for dx, dy in short_edges)
		)

	def _retain_artifact_page_background(
		self,
		bbox: Tuple[float, float, float, float],
	) -> None:
		"""Retain explicit page paint solely as text-visibility evidence."""
		page_width, page_height = self.conv.page_sizes.get(
			self.page,
			(612.0, 792.0),
		)
		if page_width <= 0 or page_height <= 0:
			return
		x0, y0, x1, y1 = bbox
		if (
			x0 > page_width * 0.05
			or y0 > page_height * 0.05
			or x1 < page_width * 0.95
			or y1 < page_height * 0.95
		):
			return
		# A page-sized artifact painted after authored content is an overlay,
		# not that content's background.  Stay conservative instead of using it
		# to revive text that was genuinely concealed when painted.
		if any(
			char.page == self.page and not char.artifact
			for char in self.conv.chars
		):
			return
		previous = self.conv._artifact_page_backgrounds.get(self.page)
		under = previous.color if previous is not None else (1.0, 1.0, 1.0)
		alpha = max(0.0, min(1.0, self.fill_alpha))
		color = tuple(
			alpha * foreground + (1.0 - alpha) * background
			for foreground, background in zip(self.fill_rgb, under)
		)
		self.conv._artifact_page_backgrounds[self.page] = Fill(
			x0=x0,
			y0=y0,
			x1=x1,
			y1=y1,
			color=color,
			page=self.page,
			seq=self.conv.seq,
			clip_bbox=self.clip_bbox,
			paint_order=self.content_order,
		)

	def _retain_artifact_local_background(
		self,
		bbox: Tuple[float, float, float, float],
	) -> None:
		"""Retain a non-rule artifact fill solely as local contrast evidence."""
		x0, y0, x1, y1 = bbox
		width = x1 - x0
		height = y1 - y0
		page_width, page_height = self.conv.page_sizes.get(
			self.page,
			(612.0, 792.0),
		)
		if (
			width <= 2.0
			or height <= 2.0
			or (
				width >= page_width * 0.90
				and height >= page_height * 0.90
			)
		):
			return
		cx = (x0 + x1) / 2.0
		cy = (y0 + y1) / 2.0
		under = (1.0, 1.0, 1.0)
		page_background = self.conv._artifact_page_backgrounds.get(self.page)
		if (
			page_background is not None
			and page_background.x0 <= cx <= page_background.x1
			and page_background.y0 <= cy <= page_background.y1
		):
			under = page_background.color
		previous = [
			fill
			for fill in self.conv._artifact_local_backgrounds
			if fill.page == self.page
			and fill.x0 <= cx <= fill.x1
			and fill.y0 <= cy <= fill.y1
		]
		if previous:
			under = max(previous, key=lambda fill: fill.seq).color
		alpha = max(0.0, min(1.0, self.fill_alpha))
		color = tuple(
			alpha * foreground + (1.0 - alpha) * background
			for foreground, background in zip(self.fill_rgb, under)
		)
		self.conv._artifact_local_backgrounds.append(
			Fill(
				x0=x0,
				y0=y0,
				x1=x1,
				y1=y1,
				color=color,
				page=self.page,
				seq=self.conv.seq,
				clip_bbox=self.clip_bbox,
				paint_order=self.content_order,
			)
		)

	def _retain_artifact_rule_segment(
		self,
		bbox: Tuple[float, float, float, float],
	) -> None:
		"""Retain a visible thin artifact rectangle as geometry-only evidence."""
		x0, y0, x1, y1 = bbox
		width = x1 - x0
		height = y1 - y0
		thin = min(width, height)
		long = max(width, height)
		# Reject page backgrounds, panels, cell fills, square decorations, and
		# hairline specks.  Office-generated grid rules are long, highly
		# elongated rectangles with visible contrast against a white page.
		if (
			thin <= 0
			or thin > 2.0
			or long < 4.0
			or long / thin < 4.0
			or color_contrast(self.fill_rgb, (1.0, 1.0, 1.0)) < 1.12
		):
			return
		if width >= height:
			segment = Segment(
				x0,
				(y0 + y1) / 2.0,
				x1,
				(y0 + y1) / 2.0,
				height,
				self.page,
				self.conv.seq,
				fill=True,
				color=self.fill_rgb,
			)
		else:
			segment = Segment(
				(x0 + x1) / 2.0,
				y0,
				(x0 + x1) / 2.0,
				y1,
				width,
				self.page,
				self.conv.seq,
				fill=True,
				color=self.fill_rgb,
			)
		self.conv._artifact_rule_segments.append(segment)

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
			paint_order=self.content_order,
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
		self._compact_column_bands: Dict[int, List[Tuple[float, float, float]]] = {}
		self._filled_sidebar_bands: Dict[int, List[Dict[str, Any]]] = {}
		self._inferred_panel_bands: Dict[int, List[Dict[str, Any]]] = {}
		self._table_cache: Dict[
			int,
			List[Tuple[float, str, List[Line], Tuple[float, float, float, float]]],
		] = {}
		self._partial_table_models: Dict[
			Tuple[int, Tuple[float, float, float, float]],
			Dict[str, Any],
		] = {}
		self._vector_boxes: Dict[
			int,
			List[Tuple[float, float, float, float]],
		] = {}
		self._vector_line_ids: Dict[int, set[int]] = {}
		self._panel_label_cache: Dict[int, set[int]] = {}
		self._code_region_cache: Dict[int, Optional[Tuple[float, float, float, float]]] = {}
		self._tiled_code_panels_cache: Dict[int, Tuple[Dict[str, Any], ...]] = {}
		self._tiled_code_panel_by_line: Dict[int, Optional[Dict[str, Any]]] = {}
		self._tiled_noncode_line_ids: set[int] = set()
		self._code_border_boxes_cache: Dict[int, List[Tuple[float, float, float, float]]] = {}
		self._quote_bars_cache: Dict[int, Tuple[Segment, ...]] = {}
		self._visual_marker_cache: Dict[int, Optional[VisualListMarker]] = {}
		self._line_index_by_id: Dict[int, Tuple[int, int]] = {}
		self._tagged_heading_level_cache: Dict[int, Optional[int]] = {}
		self._display_wrap_run_cache: Dict[
			int,
			Tuple[Tuple[Line, ...], int, int],
		] = {}
		self._line_analysis_frozen = False

	def analyze(self) -> Dict[int, List[BlockEvent]]:
		if self._analyzed:
			return self.block_events_by_page
		if not self._prepared:
			self._build_lines()
			self._stitch_page_boundary_rotated_text()
			self._remove_furniture()
			self._materialize_vector_figures()
			self._materialize_artifact_boundary_continuation_tables()
			self._materialize_formula_figures()
			self._prepared = True
		if not self._line_analysis_frozen:
			self._freeze_line_analysis_caches()
		self._body_size_cache.clear()
		self._text_frame_cache.clear()
		self._available_width_cache.clear()
		self.block_events_by_page = {}
		for page in sorted(self.conv.page_sizes):
			self._render_page(page)
		self._analyzed = True
		return self.block_events_by_page

	def _clear_line_analysis_caches(self) -> None:
		self._line_index_by_id.clear()
		self._tagged_heading_level_cache.clear()
		self._display_wrap_run_cache.clear()
		self._line_analysis_frozen = False

	def _freeze_line_analysis_caches(self) -> None:
		"""Index immutable prepared lines for renderer-local evidence caches."""
		self._clear_line_analysis_caches()
		for page, lines in self.lines_by_page.items():
			for index, line in enumerate(lines):
				self._line_index_by_id[id(line)] = (page, index)
		self._line_analysis_frozen = True

	def _frozen_line_position(self, line: Line) -> Optional[Tuple[int, int]]:
		if not self._line_analysis_frozen:
			return None
		position = self._line_index_by_id.get(id(line))
		if position is None:
			return None
		page, index = position
		lines = self.lines_by_page.get(page, ())
		if page != line.page or index < 0 or index >= len(lines) or lines[index] is not line:
			return None
		return position

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
					from .semantics.records import semantic_block_record

					page_events[0].semantic = semantic_block_record(
						"table",
						page_blocks[0],
					)
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
		from .semantics.records import semantic_block_record

		event_attrs = dict(attrs or {})
		panel_contexts = [self._panel_line_context(line) for line in lines or ()]
		if (
			panel_contexts
			and all(context is not None for context in panel_contexts)
			and len({
				(str(context["group"]), int(context["index"]))
				for context in panel_contexts
				if context is not None
			}) == 1
		):
			context = panel_contexts[0]
			assert context is not None
			roles = {
				str(item["role"])
				for item in panel_contexts
				if item is not None
			}
			event_attrs.update(
				{
					"panel_local": True,
					"panel_mode": context["mode"],
					"panel_group": context["group"],
					"panel_index": context["index"],
					"panel_count": 3,
					"panel_role": next(iter(roles)) if len(roles) == 1 else "mixed",
					"panel_bbox": context["bbox"],
					"panel_confidence": context["confidence"],
					"panel_evidence": context["evidence"],
				}
			)
		event = BlockEvent(
			page=page,
			rank=float(rank),
			kind=kind,
			lines=list(lines or []),
			attrs=event_attrs,
			legacy_markdown=legacy_markdown,
			semantic=semantic_block_record(kind, legacy_markdown),
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
				alt=figure.alt,
				intrinsic_width=max(1, int(round(x1 - x0))),
				intrinsic_height=max(1, int(round(y1 - y0))),
				placed_width=max(1.0, x1 - x0),
				placed_height=max(1.0, y1 - y0),
				quad=((x0, y0), (x1, y0), (x1, y1), (x0, y1)),
				kind="vector",
				glyph_ids=figure.glyph_ids,
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

	def _materialize_artifact_boundary_continuation_tables(self) -> None:
		"""Promote a complete short lattice only when a strong peer proves it.

		Some producers repeat a table header after a page break and leave only one
		body row on that physical page.  The ordinary artifact-lattice detector
		intentionally requires more body support, so such a fragment otherwise
		falls through as a heading and prose.  This second pass runs only after all
		normal table candidates have been cached.  It snapshots those candidates
		before scanning, which prevents weak fragments from bootstrapping one
		another and makes admission independent of page traversal order.
		"""
		strong_candidates = {
			page: tuple(self._table_cache.get(page, ()))
			for page in self.conv.page_sizes
		}
		additions: List[
			Tuple[
				int,
				Tuple[float, str, List[Line], Tuple[float, float, float, float]],
				Dict[str, Any],
			]
		] = []
		for page in sorted(self.conv.page_sizes):
			for component in segment_components([
				segment
				for segment in self.conv._artifact_rule_segments
				if segment.page == page
			]):
				horizontal = [
					segment for segment in component
					if segment.horizontal and segment.length >= 4.0
				]
				vertical = [
					segment for segment in component
					if segment.vertical and segment.length >= 4.0
				]
				xs = cluster_values(
					[(segment.x0 + segment.x1) / 2.0 for segment in vertical],
					2.0,
				)
				ys = cluster_values(
					[(segment.y0 + segment.y1) / 2.0 for segment in horizontal],
					2.0,
				)
				columns = len(xs) - 1
				if len(ys) != 3 or not 2 <= columns <= 12:
					continue
				page_width, page_height = self.conv.page_sizes.get(
					page,
					(612.0, 792.0),
				)
				if (
					any(right - left < 12.0 for left, right in zip(xs, xs[1:]))
					or any(bottom - top < 8.0 for top, bottom in zip(ys, ys[1:]))
					or xs[-1] - xs[0] < max(120.0, page_width * 0.25)
					or xs[-1] - xs[0] > page_width * 0.96
					or ys[-1] - ys[0] < max(36.0, page_height * 0.045)
					or grid_coverage(xs, ys, horizontal, vertical) < 0.96
					or not lattice_has_all_cell_edges(
						xs,
						ys,
						horizontal,
						vertical,
					)
				):
					continue
				top_boundary = ys[0] <= page_height * 0.20
				bottom_boundary = ys[-1] >= page_height * 0.80
				if top_boundary == bottom_boundary:
					continue
				boundary_side = "top" if top_boundary else "bottom"
				peer_page = page - 1 if top_boundary else page + 1
				if peer_page not in self.conv.page_sizes:
					continue
				box = (
					float(xs[0]),
					float(ys[0]),
					float(xs[-1]),
					float(ys[-1]),
				)
				if any(
					self._boxes_intersect(box, candidate[3])
					for candidate in strong_candidates.get(page, ())
				):
					continue
				if any(
					self._boxes_intersect(box, artwork)
					for artwork in self._vector_boxes.get(page, ())
				):
					continue
				if any(
					image.page == page
					and self._boxes_intersect(
						box,
						(image.x0, image.y0, image.x1, image.y1),
					)
					for image in self.conv.images
				):
					continue
				if any(
					self._boxes_intersect(
						box,
						(
							min(seg_bbox(segment)[0] for segment in group),
							min(seg_bbox(segment)[1] for segment in group),
							max(seg_bbox(segment)[2] for segment in group),
							max(seg_bbox(segment)[3] for segment in group),
						),
					)
					for group in self._control_path_groups(page)
				):
					continue
				table_lines = [
					line
					for line in self.lines_by_page.get(page, ())
					if any(
						char.text.strip()
						and xs[0] - 2.0 <= (char.x0 + char.x1) / 2.0 <= xs[-1] + 2.0
						and ys[0] - 2.0 <= (char.y0 + char.y1) / 2.0 <= ys[-1] + 2.0
						for char in line.chars
					)
				]
				visible_chars = [
					char
					for line in table_lines
					for char in line.chars
					if char.text.strip()
					and xs[0] - 2.0 <= (char.x0 + char.x1) / 2.0 <= xs[-1] + 2.0
					and ys[0] - 2.0 <= (char.y0 + char.y1) / 2.0 <= ys[-1] + 2.0
				]
				if not visible_chars:
					continue
				authored_ratio = (
					sum(not char.artifact for char in visible_chars)
					/ len(visible_chars)
				)
				if authored_ratio < 0.90:
					continue
				occupancy = self._partial_grid_occupancy(table_lines, xs, ys)
				if occupancy != [set(range(columns)), set(range(columns))]:
					continue
				if self._artifact_lattice_header_rows(table_lines, xs, ys) != 1:
					continue
				header_chars = [
					char
					for line in table_lines
					for char in line.chars
					if char.text.strip()
					and xs[0] - 2.0 <= (char.x0 + char.x1) / 2.0 <= xs[-1] + 2.0
					and ys[0] - 2.0 <= (char.y0 + char.y1) / 2.0 <= ys[1] + 2.0
				]
				if not header_chars or sum(
					char.bold
					or self._marked_cell_identity(char, {"TH"}) is not None
					for char in header_chars
				) < math.ceil(len(header_chars) * 0.70):
					continue
				if self._artifact_lattice_words_cross_boundaries(
					table_lines,
					xs,
					ys,
				):
					continue
				if self._artifact_fragment_has_intervening_text(
					page,
					ys,
					boundary_side,
					table_lines,
				):
					continue
				header_signature = self._artifact_lattice_header_signature(
					table_lines,
					xs,
					ys,
				)
				if (
					len(header_signature) != columns
					or any(
						not label or not any(character.isalpha() for character in label)
						for label in header_signature
					)
				):
					continue
				peer = self._artifact_boundary_strong_peer(
					peer_page,
					boundary_side,
					page_width,
					xs,
					header_signature,
					strong_candidates,
				)
				if peer is None:
					continue
				peer_model, peer_rows, boundary_drift = peer
				html = self._render_partial_grid_html(
					page,
					xs,
					ys,
					table_lines,
					"",
					header_rows_override=1,
				)
				model = {
					"model_kind": "artifact_boundary_continuation_lattice",
					"xs": list(xs),
					"ys": list(ys),
					"header_rows": 1,
					"evidence": {
						"artifact_geometry_only": True,
						"artifact_rule_rectangles": len(component),
						"complete_edge_coverage": True,
						"boundary_side": boundary_side,
						"adjacent_peer_page": peer_page,
						"peer_model_kind": peer_model,
						"strong_peer_rows": peer_rows,
						"repeated_header_match": True,
						"normalized_boundary_match": True,
						"maximum_normalized_boundary_drift": boundary_drift,
						"authored_glyph_ratio": authored_ratio,
						"no_crossing_words": True,
					},
				}
				additions.append(
					(page, (float(ys[0]), html, table_lines, box), model)
				)

		for page, candidate, model in additions:
			self._table_cache.setdefault(page, []).append(candidate)
			self._table_cache[page].sort(key=lambda item: (item[0], item[3][0]))
			self._partial_table_models[(page, candidate[3])] = model

	def _artifact_boundary_strong_peer(
		self,
		peer_page: int,
		fragment_side: str,
		fragment_page_width: float,
		xs: Sequence[float],
		header_signature: Tuple[str, ...],
		strong_candidates: Dict[
			int,
			Sequence[
				Tuple[float, str, List[Line], Tuple[float, float, float, float]]
			],
		],
	) -> Optional[Tuple[str, int, float]]:
		peer_width, peer_height = self.conv.page_sizes.get(
			peer_page,
			(612.0, 792.0),
		)
		if abs(fragment_page_width - peer_width) > max(
			2.0,
			fragment_page_width * 0.02,
		):
			return None
		matches: List[Tuple[str, int, float]] = []
		for candidate in strong_candidates.get(peer_page, ()):
			model = self._partial_table_models.get((peer_page, candidate[3]))
			if not isinstance(model, dict):
				continue
			model_kind = str(model.get("model_kind") or "")
			if model_kind not in {
				"artifact_filled_lattice",
				"artifact_fragmented_lattice",
			}:
				continue
			try:
				peer_xs = [float(value) for value in model.get("xs", ())]
				peer_ys = [float(value) for value in model.get("ys", ())]
			except (TypeError, ValueError, OverflowError):
				continue
			peer_rows = len(peer_ys) - 1
			if (
				len(peer_xs) != len(xs)
				or peer_rows < 3
				or len(peer_ys) < 4
				or int(model.get("header_rows", 0)) != 1
			):
				continue
			if fragment_side == "top":
				if peer_ys[-1] < peer_height * 0.65:
					continue
			elif peer_ys[0] > peer_height * 0.35:
				continue
			drift = max(
				abs(left / fragment_page_width - right / peer_width)
				for left, right in zip(xs, peer_xs)
			)
			if drift > 0.008:
				continue
			peer_lines = candidate[2]
			peer_occupancy = self._partial_grid_occupancy(
				peer_lines,
				peer_xs,
				peer_ys,
			)
			columns = len(xs) - 1
			if (
				not peer_occupancy
				or peer_occupancy[0] != set(range(columns))
				or sum(
					len(row) >= max(2, math.ceil(columns * 0.50))
					for row in peer_occupancy[1:]
				) < 2
			):
				continue
			if self._artifact_lattice_header_signature(
				peer_lines,
				peer_xs,
				peer_ys,
			) != header_signature:
				continue
			matches.append((model_kind, peer_rows, drift))
		return matches[0] if len(matches) == 1 else None

	@staticmethod
	def _artifact_lattice_header_signature(
		lines: Sequence[Line],
		xs: Sequence[float],
		ys: Sequence[float],
	) -> Tuple[str, ...]:
		labels: List[str] = []
		for left, right in zip(xs, xs[1:]):
			words: List[str] = []
			for line in sorted(lines, key=lambda item: (item.y0, item.x0, item.seq)):
				for text, x0, y0, x1, y1 in word_boxes(line):
					cx = (x0 + x1) / 2.0
					cy = (y0 + y1) / 2.0
					if left - 2.0 <= cx <= right + 2.0 and ys[0] - 2.0 <= cy <= ys[1] + 2.0:
						words.append(text)
			labels.append(cleanup_spaces(" ".join(words)).casefold())
		return tuple(labels)

	@staticmethod
	def _artifact_lattice_words_cross_boundaries(
		lines: Sequence[Line],
		xs: Sequence[float],
		ys: Sequence[float],
	) -> bool:
		return any(
			x0 < boundary - 1.0 and x1 > boundary + 1.0
			for line in lines
			for _text, x0, y0, x1, y1 in word_boxes(line)
			if ys[0] - 2.0 <= (y0 + y1) / 2.0 <= ys[-1] + 2.0
			for boundary in xs[1:-1]
		)

	def _artifact_fragment_has_intervening_text(
		self,
		page: int,
		ys: Sequence[float],
		boundary_side: str,
		table_lines: Sequence[Line],
	) -> bool:
		owned = {id(line) for line in table_lines}
		for line in self.lines_by_page.get(page, ()):
			if id(line) in owned or not plain_text(line_text_tokens(line)).strip():
				continue
			center = (line.y0 + line.y1) / 2.0
			if boundary_side == "top" and center < ys[0] - 2.0:
				return True
			if boundary_side == "bottom" and center > ys[-1] + 2.0:
				return True
		return False

	@staticmethod
	def _boxes_intersect(
		left: Tuple[float, float, float, float],
		right: Tuple[float, float, float, float],
	) -> bool:
		return (
			min(left[2], right[2]) - max(left[0], right[0]) > 1.0
			and min(left[3], right[3]) - max(left[1], right[1]) > 1.0
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
		# Column inference is scoped to the line geometry produced by this pass.
		# Clear both positive and negative entries if a renderer is rebuilt.
		self._clear_line_analysis_caches()
		self._inferred_column_bands.clear()
		self._compact_column_bands.clear()
		self._filled_sidebar_bands.clear()
		self._inferred_panel_bands.clear()
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
		artifact_candidates = self._artifact_text_line_candidates()
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
			lines = self._repair_drop_cap_lines(lines)
			lines.extend(
				self._recovered_artifact_display_lines(
					page,
					lines,
					artifact_candidates,
				)
			)
			lines.sort(
				key=lambda line: (
					line.y0,
					line.seq if line.writing_mode == "vertical" else line.x0,
					line.seq,
				)
			)
			lines = self._split_lines_on_column_gaps(page, lines)
			# A display/prose separator can expose the lowercase first-line target
			# that was previously fused with the title rail.  Re-run the idempotent
			# drop-cap repair on those newly independent physical lines.
			lines = self._repair_drop_cap_lines(lines)
			lines = self._order_column_bands(page, lines)
			lines = self._order_directional_regions(lines)
			self.lines_by_page[page] = lines

	def _artifact_text_line_candidates(self) -> Dict[int, List[Line]]:
		"""Cluster decodable artifact text without admitting it as authored text.

		Most ``/Artifact`` text is correctly tagged furniture and must stay out of
		the semantic stream.  A small producer failure class incorrectly tags a
		real display title, however.  Keep candidate clustering isolated here so
		the later admission gate can require independent document and layout
		evidence; candidates that fail it remain completely invisible to output.
		"""
		by_page: Dict[int, List[Char]] = {}
		for char in self.conv.chars:
			if (
				char.artifact
				and char.text
				and (self.conv.options.keep_invisible or not char.invisible)
				and char.render_mode != 3
			):
				by_page.setdefault(char.page, []).append(char)
		out: Dict[int, List[Line]] = {}
		for page, chars in by_page.items():
			baselines: List[Line] = []
			for char in sorted(chars, key=lambda item: (item.y0, item.x0, item.seq)):
				center = (char.y0 + char.y1) / 2
				best: Optional[Line] = None
				best_distance = float("inf")
				for line in reversed(baselines[-8:]):
					line_center = median([(item.y0 + item.y1) / 2 for item in line.chars])
					distance = abs(center - line_center)
					if distance <= max(2.5, char.size * 0.35) and distance < best_distance:
						best = line
						best_distance = distance
				if best is None:
					baselines.append(Line([char], page, char.seq))
				else:
					best.chars.append(char)
					best.invalidate_caches()

			page_lines: List[Line] = []
			for baseline in baselines:
				ordered = sorted(baseline.chars, key=lambda item: (item.x0, item.seq))
				visible = [char for char in ordered if char.text.strip()]
				if not visible:
					continue
				cohort: List[Char] = []
				cohort_size = median([char.size for char in visible])
				for char in ordered:
					if cohort:
						previous = cohort[-1]
						gap = char.x0 - previous.x1
						if gap > max(cohort_size * 1.75, 28.0):
							if any(item.text.strip() for item in cohort):
								page_lines.append(Line(cohort, page, min(item.seq for item in cohort)))
							cohort = []
					cohort.append(char)
				if cohort and any(item.text.strip() for item in cohort):
					page_lines.append(Line(cohort, page, min(item.seq for item in cohort)))
			out[page] = page_lines
		return out

	def _recovered_artifact_display_lines(
		self,
		page: int,
		authored_lines: Sequence[Line],
		candidates_by_page: Dict[int, List[Line]],
	) -> List[Line]:
		"""Recover only strongly corroborated section titles mis-tagged Artifact."""
		if not authored_lines:
			return []
		page_width, page_height = self.conv.page_sizes.get(page, (612.0, 792.0))
		body_size = body_font_size(list(authored_lines))
		if body_size <= 0:
			return []
		recovered: List[Line] = []
		for candidate in candidates_by_page.get(page, []):
			text = cleanup_spaces(plain_text(line_text_tokens(candidate))).strip()
			words = text.split()
			if not re.match(
				r"^(?:\d+(?:\.\d+){0,5}\.|[IVXLCDM]+\.)\s+[A-Z0-9]\S*",
				text,
				re.I,
			):
				continue
			if not 3 <= len(words) <= 18 or len(text) > 120:
				continue
			if sum(character.isalpha() for character in text) < 10:
				continue
			if candidate.size < max(body_size * 1.70, body_size + 5.0):
				continue
			if candidate.y0 < page_height * 0.05 or candidate.y1 > page_height * 0.90:
				continue
			if candidate.x1 - candidate.x0 > page_width * 0.88:
				continue
			if not self._artifact_display_title_has_rule_brackets(candidate, page_width):
				continue

			visible = [char for char in ordered_line_chars(candidate) if char.text.strip()]
			if len(visible) < 8:
				continue
			if any(
				right.x0 - left.x1 > max(candidate.size * 1.25, 24.0)
				for left, right in zip(visible, visible[1:])
			):
				continue

			normalized = text.casefold()
			repeated_pages = {
				other_page
				for other_page, other_candidates in candidates_by_page.items()
				if any(
					cleanup_spaces(plain_text(line_text_tokens(other))).strip().casefold()
					== normalized
					for other in other_candidates
				)
			}
			if len(repeated_pages) > 1:
				continue

			nearby_body = [
				line
				for line in authored_lines
				if line.y0 > candidate.y1
				and line.y0 - candidate.y1 <= max(body_size * 18.0, 180.0)
				and line.size <= candidate.size * 0.72
				and plain_text(line_text_tokens(line)).strip()
			]
			if len(nearby_body) < 2:
				continue

			image_overlap = False
			margin = max(3.0, candidate.size * 0.20)
			for image in self.conv.images:
				if image.page != page:
					continue
				if rects_intersect(
					(candidate.x0, candidate.y0, candidate.x1, candidate.y1),
					(image.x0 - margin, image.y0 - margin, image.x1 + margin, image.y1 + margin),
				):
					image_overlap = True
					break
			if image_overlap:
				continue

			recovered.append(candidate)
			self.conv.doc.warn(
				"ARTIFACT_TEXT_RECOVERED",
				"authored display heading recovered from defective /Artifact tagging; "
				"admission=numbered_title+display_scale+paired_rules+adjacent_body+unique_position",
				page,
			)
		return recovered

	def _artifact_display_title_has_rule_brackets(
		self,
		candidate: Line,
		page_width: float,
	) -> bool:
		"""Require the independent title-band geometry of the defective dialect."""
		above = False
		below = False
		for segment in list(self.conv.segments) + list(self.conv._artifact_rule_segments):
			if (
				segment.page != candidate.page
				or not segment.horizontal
				or segment.width > 3.0
				or segment.length < max(page_width * 0.60, candidate.x1 - candidate.x0)
			):
				continue
			x0, x1 = sorted((segment.x0, segment.x1))
			if x0 > candidate.x0 + candidate.size or x1 < candidate.x1 - candidate.size:
				continue
			y = (segment.y0 + segment.y1) / 2
			if 0 <= candidate.y0 - y <= max(candidate.size * 0.55, 10.0):
				above = True
			if 0 <= y - candidate.y1 <= max(candidate.size * 0.55, 10.0):
				below = True
		return above and below

	def _repair_drop_cap_lines(self, lines: List[Line]) -> List[Line]:
		"""Attach a multi-line drop cap to the first line of its paragraph.

		PDF producers often place a large initial beside two or three body lines.
		Baseline clustering naturally attaches that glyph to a lower line whose
		vertical centre is closest, which reverses the first two text lines.
		Repair only a single alphabetic, strongly oversized left-edge glyph
		followed by a lowercase continuation line immediately to its right.
		"""
		for source in list(lines):
			if source.writing_mode != "horizontal":
				continue
			visible = [char for char in source.chars if char.text.strip()]
			if len(visible) < 4:
				continue
			leftmost = min(visible, key=lambda char: (char.x0, char.seq))
			if len(leftmost.text) > 2 or not leftmost.text.isalpha():
				continue
			remainder = [char for char in visible if char is not leftmost]
			body_size = median([char.size for char in remainder])
			if body_size <= 0 or leftmost.size < max(body_size * 2.2, body_size + 12.0):
				continue
			if sum(1 for char in remainder if 0.75 * body_size <= char.size <= 1.35 * body_size) < len(remainder) * 0.85:
				continue
			remainder_y = median([(char.y0 + char.y1) / 2 for char in remainder])
			candidates: List[Tuple[float, Line]] = []
			for target in lines:
				if target is source or target.page != source.page or target.writing_mode != "horizontal":
					continue
				target_visible = [char for char in target.chars if char.text.strip()]
				if not target_visible:
					continue
				first_text = min(target_visible, key=lambda char: (char.x0, char.seq)).text
				if not first_text[:1].islower():
					continue
				if not 0.75 * body_size <= target.size <= 1.35 * body_size:
					continue
				target_center = median([(char.y0 + char.y1) / 2 for char in target_visible])
				if not leftmost.y0 < target_center < remainder_y - body_size * 0.25:
					continue
				horizontal_gap = target.x0 - leftmost.x1
				if horizontal_gap < -body_size * 0.20 or horizontal_gap > body_size * 1.25:
					continue
				if abs(target.x0 - min(char.x0 for char in remainder)) > max(body_size * 1.5, 18.0):
					continue
				candidates.append((remainder_y - target_center, target))
			if not candidates:
				continue
			target = min(candidates, key=lambda item: item[0])[1]
			source.chars.remove(leftmost)
			target.chars.append(leftmost)
			source.chars.sort(key=lambda char: (char.x0, char.seq))
			target.chars.sort(key=lambda char: (char.x0, char.seq))
			source.seq = min(char.seq for char in source.chars)
			target.seq = min(char.seq for char in target.chars)
			source.invalidate_caches()
			target.invalidate_caches()
		return lines

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
		panel_bands = self._cached_three_panel_bands(page, lines)
		sep_infos = self._column_separator_infos(page)
		sep_infos.extend(self._cached_filled_sidebar_separator_infos(page, lines))
		if not sep_infos and not panel_bands:
			sep_infos = self._cached_inferred_column_separator_infos(page, lines)
		if not sep_infos and not panel_bands:
			return lines
		out: List[Line] = []
		for line in lines:
			cy = (line.y0 + line.y1) / 2
			panel_band = next(
				(
					band
					for band in panel_bands
					if band["y0"] - 3.0 <= cy <= band["y1"] + 3.0
				),
				None,
			)
			if panel_band is not None:
				out.extend(
					self._split_line_on_panel_separators(
						line,
						panel_band["separators"],
					)
				)
				continue
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
					compact_separator = any(
						abs(active_sep - compact_x) <= 14.0
						and compact_y0 - 3.0 <= cy <= compact_y1 + 3.0
						for active_sep in seps
						for compact_x, compact_y0, compact_y1 in self._compact_column_bands.get(page, [])
					)
					min_sep_gap = (
						max(7.0, min(line.size * 0.75, 12.0))
						if compact_separator
						else max(8.0, min(line.size * 1.1, 18.0))
					)
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
		panel_bands = self._inferred_panel_bands.get(page, [])
		if panel_bands:
			return self._order_three_panel_bands(lines, panel_bands)
		sep_infos = self._column_separator_infos(page)
		sep_infos.extend(self._cached_filled_sidebar_separator_infos(page, lines))
		if not sep_infos:
			sep_infos = self._cached_inferred_column_separator_infos(page, lines)
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

	def _cached_three_panel_bands(
		self,
		page: int,
		lines: List[Line],
	) -> List[Dict[str, Any]]:
		if page not in self._inferred_panel_bands:
			tri_fold = self._tri_fold_panel_bands(page, lines)
			self._inferred_panel_bands[page] = (
				tri_fold if tri_fold else self._three_panel_bands(page, lines)
			)
		return [dict(band) for band in self._inferred_panel_bands[page]]

	def _tri_fold_panel_bands(
		self,
		page: int,
		lines: List[Line],
	) -> List[Dict[str, Any]]:
		"""Recognize a full-page landscape tri-fold brochure composition.

		This is intentionally narrower than generic column inference. Each page
		third must own real text and a display label; multiple physical baselines
		must mix otherwise well-separated panels; and independent design evidence
		must pair a complex painted background in the first panel with raster
		anchors across the other two. Grid and numeric-dashboard geometry vetoes
		the model before it can affect reading order.
		"""
		page_width, page_height = self.conv.page_sizes.get(page, (612.0, 792.0))
		if not (page_height > 0 and 1.20 <= page_width / page_height <= 1.75):
			return []
		body = self._body_font_size(lines)
		if body <= 0:
			return []
		boundaries = (page_width / 3.0, page_width * 2.0 / 3.0)
		panel_runs: List[List[Tuple[Line, List[Char]]]] = [[], [], []]
		mixed_baselines = 0
		boundary_gaps: List[List[float]] = [[], []]
		crossing_glyphs = [0, 0]
		visible_chars: List[Char] = []

		def alpha_count(chars: Sequence[Char]) -> int:
			return sum(character.isalpha() for char in chars for character in char.text)

		for line in lines:
			visible = [
				char
				for char in line.chars
				if char.text.strip() and not char.invisible and not char.artifact
			]
			if not visible:
				continue
			visible_chars.extend(visible)
			groups: List[List[Char]] = [[], [], []]
			for char in visible:
				center = (char.x0 + char.x1) / 2.0
				groups[sum(center >= boundary for boundary in boundaries)].append(char)
			for index, group in enumerate(groups):
				if alpha_count(group) >= 3:
					panel_runs[index].append((line, group))
			if sum(alpha_count(group) >= 3 for group in groups) >= 2:
				mixed_baselines += 1
			for index, boundary in enumerate(boundaries):
				left = [char for char in visible if (char.x0 + char.x1) / 2.0 < boundary]
				right = [char for char in visible if (char.x0 + char.x1) / 2.0 >= boundary]
				if alpha_count(left) >= 3 and alpha_count(right) >= 3:
					boundary_gaps[index].append(
						min(char.x0 for char in right) - max(char.x1 for char in left)
					)
				crossing_glyphs[index] += sum(
					char.x0 + 0.5 < boundary < char.x1 - 0.5
					for char in visible
				)

		if mixed_baselines < 4 or any(len(runs) < 4 for runs in panel_runs):
			return []
		if any(
			alpha_count([char for _line, run in runs for char in run]) < 40
			for runs in panel_runs
		):
			return []
		if any(crossing_glyphs) or any(
			len(gaps) < 2 or median(gaps) < max(20.0, page_width * 0.035)
			for gaps in boundary_gaps
		):
			return []

		for runs in panel_runs:
			has_display = False
			for _line, chars in runs:
				local_size = median([char.size for char in chars if char.size > 0])
				bold_ratio = sum(char.bold for char in chars) / len(chars)
				text = cleanup_spaces("".join(char.text for char in sorted(chars, key=lambda item: (item.x0, item.seq))))
				if (
					4 <= sum(character.isalpha() for character in text)
					and len(text) <= 90
					and (bold_ratio >= 0.50 or local_size >= body * 1.45)
				):
					has_display = True
					break
			if not has_display:
				return []

		page_area = max(page_width * page_height, 1.0)
		left_backgrounds = []
		for path in self.conv.painted_paths:
			if path.page != page or len(path.commands) < 16:
				continue
			x0, y0, x1, y1 = path.bbox
			if (
				x0 > boundaries[0] * 0.18
				or x1 > boundaries[0] + page_width * 0.025
				or x1 - x0 < page_width * 0.16
				or y1 - y0 < page_height * 0.18
				or (x1 - x0) * (y1 - y0) < page_area * 0.035
			):
				continue
			covered_alpha = sum(
				character.isalpha()
				for char in visible_chars
				if char.paint_order > path.paint_order > 0
				and x0 <= (char.x0 + char.x1) / 2.0 <= x1
				and y0 <= (char.y0 + char.y1) / 2.0 <= y1
				and painted_path_contains_point(
					path,
					(char.x0 + char.x1) / 2.0,
					(char.y0 + char.y1) / 2.0,
				)
				for character in char.text
			)
			if covered_alpha >= 24:
				left_backgrounds.append(path)
		if not left_backgrounds:
			return []

		images = [
			image
			for image in self.conv.images
			if image.page == page
			and max(0.0, min(image.x1, page_width) - max(image.x0, 0.0))
				* max(0.0, min(image.y1, page_height) - max(image.y0, 0.0))
				>= page_area * 0.012
		]
		middle_images = [
			image
			for image in images
			if boundaries[0] <= (image.x0 + image.x1) / 2.0 <= boundaries[1]
		]
		right_images = [
			image
			for image in images
			if min(image.x1, page_width) - max(image.x0, boundaries[1])
				>= page_width * 0.10
			and min(image.y1, page_height) - max(image.y0, 0.0)
				>= page_height * 0.20
		]
		if not middle_images or not right_images or len({id(image) for image in middle_images + right_images}) < 2:
			return []

		long_vertical = [
			segment
			for segment in self.conv.segments
			if segment.page == page and segment.vertical and segment.length >= page_height * 0.22
		]
		long_horizontal = [
			segment
			for segment in self.conv.segments
			if segment.page == page and segment.horizontal and segment.length >= page_width * 0.28
		]
		if len(long_vertical) >= 2 or len(long_horizontal) >= 3:
			return []
		plain_lines = []
		for line in lines:
			line_text = plain_text(line_text_tokens(line)).strip()
			if line_text:
				plain_lines.append(line_text)
		plain = cleanup_spaces(" ".join(plain_lines))
		if re.search(r"(?:^|\s)(?:table|tab\.|exhibit)\s+[A-Z0-9IVXLCDM]+\b", plain, re.I):
			return []
		alphanumeric = [character for character in plain if character.isalnum()]
		if alphanumeric and sum(character.isdigit() for character in alphanumeric) / len(alphanumeric) > 0.16:
			return []

		return [
			{
				"kind": "tri_fold",
				"separators": list(boundaries),
				"x0": 0.0,
				"x1": page_width,
				"y0": 0.0,
				"y1": page_height,
				"panel_bounds": (
					(0.0, boundaries[0]),
					(boundaries[0], boundaries[1]),
					(boundaries[1], page_width),
				),
				"evidence": {
					"mixed_baselines": mixed_baselines,
					"complex_left_backgrounds": len(left_backgrounds),
					"middle_images": len(middle_images),
					"right_images": len(right_images),
				},
			}
		]

	def _cached_filled_sidebar_separator_infos(
		self,
		page: int,
		lines: List[Line],
	) -> List[Tuple[float, float, float]]:
		"""Return conservative separator evidence from a painted edge sidebar.

		A few presentation-oriented PDF producers paint one tall, narrow band at
		the page edge and place an independent sequence of headings, prose, rules,
		and images inside it.  Text at the same baseline as the main column is then
		collected into one physical line unless the paint boundary is used as a
		separator.  Fill geometry alone is much too common to establish columns,
		so admission also requires authored text on both sides and repeated
		structural anchors inside the band.
		"""
		if page not in self._filled_sidebar_bands:
			self._filled_sidebar_bands[page] = self._filled_sidebar_separator_bands(
				page,
				lines,
			)
		return [
			(band["separator"], band["y0"], band["y1"])
			for band in self._filled_sidebar_bands[page]
		]

	def _filled_sidebar_separator_bands(
		self,
		page: int,
		lines: List[Line],
	) -> List[Dict[str, Any]]:
		page_width, page_height = self.conv.page_sizes.get(page, (612.0, 792.0))
		body = self._body_font_size(lines)
		if page_width <= 0 or page_height <= 0 or body <= 0:
			return []

		def authored(chars: Sequence[Char]) -> List[Char]:
			return [
				char
				for char in chars
				if char.text.strip()
				and not char.artifact
				and not char.invisible
			]

		def alpha_count(chars: Sequence[Char]) -> int:
			return sum(character.isalpha() for char in chars for character in char.text)

		candidates: List[Dict[str, Any]] = []
		for fill in self.conv.fills:
			if fill.page != page:
				continue
			fill_width = fill.x1 - fill.x0
			fill_height = fill.y1 - fill.y0
			if not (
				max(70.0, page_width * 0.10) <= fill_width <= page_width * 0.30
				and fill_height >= page_height * 0.42
			):
				continue
			right_side = fill.x1 >= page_width * 0.88 and fill.x0 >= page_width * 0.58
			left_side = fill.x0 <= page_width * 0.12 and fill.x1 <= page_width * 0.42
			if right_side == left_side:
				continue
			if (
				max(fill.color) - min(fill.color) < 0.18
				or color_contrast(fill.color, (1.0, 1.0, 1.0)) < 1.20
			):
				continue

			inside_runs: List[Tuple[Line, List[Char]]] = []
			main_runs: List[Tuple[Line, List[Char]]] = []
			all_main_runs: List[Tuple[Line, List[Char]]] = []
			for line in lines:
				visible = authored(line.chars)
				if not visible:
					continue
				inside = [
					char
					for char in visible
					if fill.x0 - 1.5 <= (char.x0 + char.x1) / 2.0 <= fill.x1 + 1.5
					and fill.y0 - 2.0 <= (char.y0 + char.y1) / 2.0 <= fill.y1 + 2.0
				]
				if alpha_count(inside) >= 3:
					inside_runs.append((line, inside))
				outside = [
					char
					for char in visible
					if (
						(char.x0 + char.x1) / 2.0 < fill.x0 - 2.0
						if right_side
						else (char.x0 + char.x1) / 2.0 > fill.x1 + 2.0
					)
				]
				if alpha_count(outside) < 3:
					continue
				all_main_runs.append((line, outside))
				if fill.y0 - 2.0 <= (line.y0 + line.y1) / 2.0 <= fill.y1 + 2.0:
					main_runs.append((line, outside))

			if (
				len(inside_runs) < 6
				or alpha_count([char for _line, run in inside_runs for char in run]) < 45
				or len(main_runs) < 4
				or alpha_count([char for _line, run in main_runs for char in run]) < 50
			):
				continue

			inside_y0 = min(line.y0 for line, _run in inside_runs)
			inside_y1 = max(line.y1 for line, _run in inside_runs)
			main_y0 = min(line.y0 for line, _run in main_runs)
			main_y1 = max(line.y1 for line, _run in main_runs)
			overlap = min(inside_y1, main_y1) - max(inside_y0, main_y0)
			if overlap < min(inside_y1 - inside_y0, main_y1 - main_y0) * 0.35:
				continue

			inside_edges = [
				min(char.x0 for char in run) if right_side else max(char.x1 for char in run)
				for _line, run in inside_runs
			]
			main_edges = [
				max(char.x1 for char in run) if right_side else min(char.x0 for char in run)
				for _line, run in main_runs
			]
			gutter = (
				median(inside_edges) - median(main_edges)
				if right_side
				else median(main_edges) - median(inside_edges)
			)
			if gutter < max(8.0, body * 0.60):
				continue

			contained_images = [
				image
				for image in self.conv.images
				if image.page == page
				and fill.x0 - 3.0 <= (image.x0 + image.x1) / 2.0 <= fill.x1 + 3.0
				and fill.y0 - 3.0 <= (image.y0 + image.y1) / 2.0 <= fill.y1 + 3.0
				and image.x0 >= fill.x0 - 6.0
				and image.x1 <= fill.x1 + 6.0
			]
			horizontal_rules = [
				segment
				for segment in self.conv.segments
				if segment.page == page
				and segment.horizontal
				and fill_width * 0.65 <= segment.length <= fill_width * 1.10
				and min(segment.x0, segment.x1) >= fill.x0 - 5.0
				and max(segment.x0, segment.x1) <= fill.x1 + 5.0
				and fill.y0 - 3.0 <= (segment.y0 + segment.y1) / 2.0 <= fill.y1 + 3.0
			]
			if len(contained_images) < 2 and len(horizontal_rules) < 2:
				continue
			vertical_grid_rules = [
				segment
				for segment in self.conv.segments
				if segment.page == page
				and segment.vertical
				and segment.length >= fill_height * 0.25
				and fill.x0 - 3.0 <= (segment.x0 + segment.x1) / 2.0 <= fill.x1 + 3.0
				and min(segment.y0, segment.y1) <= fill.y1
				and max(segment.y0, segment.y1) >= fill.y0
			]
			if vertical_grid_rules:
				continue

			regular_sizes = [
				char.size
				for _line, run in inside_runs
				for char in run
				if char.size > 0 and not char.bold
			]
			sidebar_body = median(regular_sizes) if regular_sizes else body

			# A main-column paragraph may continue a few baselines below the paint.
			# Keep that short tail before the sidebar, while refusing bottom-zone
			# furniture and any new block separated by a material vertical gap.
			extended_y1 = fill.y1
			last_end = max(
				[fill.y1]
				+ [
					line.y1
					for line, _run in main_runs
					if line.y1 >= fill.y1 - body * 1.25
				]
			)
			limit = fill.y1 + max(48.0, body * 4.8)
			for line, _run in sorted(all_main_runs, key=lambda item: (item[0].y0, item[0].seq)):
				center = (line.y0 + line.y1) / 2.0
				if line.y1 < fill.y1 - body * 1.25 or line.y0 > limit:
					continue
				if center > page_height * 0.92:
					break
				if line.y0 > last_end + max(20.0, body * 1.6):
					break
				last_end = max(last_end, line.y1)
				extended_y1 = max(extended_y1, line.y1)

			candidates.append(
				{
					"x0": fill.x0,
					"x1": fill.x1,
					"y0": fill.y0,
					"y1": extended_y1,
					"fill_y1": fill.y1,
					"separator": fill.x0 if right_side else fill.x1,
					"side": "right" if right_side else "left",
					"sidebar_body_size": sidebar_body,
					"inside_baselines": len(inside_runs),
					"main_baselines": len(main_runs),
					"image_anchors": len(contained_images),
					"rule_anchors": len(horizontal_rules),
				}
			)

		deduped: List[Dict[str, Any]] = []
		for candidate in sorted(
			candidates,
			key=lambda band: (-(band["y1"] - band["y0"]), band["x0"]),
		):
			if any(
				candidate["side"] == other["side"]
				and min(candidate["y1"], other["y1"])
					- max(candidate["y0"], other["y0"])
					>= min(
						candidate["y1"] - candidate["y0"],
						other["y1"] - other["y0"],
					) * 0.60
				for other in deduped
			):
				continue
			deduped.append(candidate)
		return sorted(deduped, key=lambda band: (band["y0"], band["x0"]))

	def _three_panel_bands(
		self,
		page: int,
		lines: List[Line],
	) -> List[Dict[str, Any]]:
		"""Infer bounded three-card reading streams on landscape pages.

		The evidence deliberately models slide/card composition, not generic
		newspaper columns: two aligned display-label baselines must introduce
		the same three starts, followed by either four prose baselines or a
		compact description row with independent raster content in every panel.
		"""
		page_width, page_height = self.conv.page_sizes.get(page, (612.0, 792.0))
		if page_width < page_height * 1.35:
			return []
		body = self._body_font_size(lines)
		triples: List[Dict[str, Any]] = []
		for line in lines:
			groups = self._three_panel_groups(line)
			if len(groups) != 3:
				continue
			starts = [float(group["x0"]) for group in groups]
			if not (
				starts[0] <= page_width * 0.28
				and page_width * 0.25 <= starts[1] <= page_width * 0.62
				and starts[2] >= page_width * 0.55
			):
				continue
			if sum(self._panel_text_has_alpha(group["text"]) for group in groups) < 2:
				continue
			triples.append(
				{
					"line": line,
					"groups": groups,
					"starts": starts,
					"separators": [
						(groups[0]["x1"] + groups[1]["x0"]) / 2.0,
						(groups[1]["x1"] + groups[2]["x0"]) / 2.0,
					],
				}
			)
		models = self._labelled_list_triptych_models(
			page,
			lines,
			triples,
			body,
		)
		if len(triples) < 3:
			return models

		clusters: List[List[Dict[str, Any]]] = []
		for entry in sorted(triples, key=lambda item: item["starts"]):
			for cluster in clusters:
				reference = [
					median([item["starts"][index] for item in cluster])
					for index in range(3)
				]
				if max(
					abs(entry["starts"][index] - reference[index])
					for index in range(3)
				) <= max(8.0, page_width * 0.012):
					cluster.append(entry)
					break
			else:
				clusters.append([entry])

		for cluster in clusters:
			ordered = sorted(cluster, key=lambda item: item["line"].y0)
			cohorts: List[List[Dict[str, Any]]] = []
			for entry in ordered:
				if (
					cohorts
					and entry["line"].y0 - cohorts[-1][-1]["line"].y1
						<= max(48.0, body * 5.2)
				):
					cohorts[-1].append(entry)
				else:
					cohorts.append([entry])
			for cohort in cohorts:
				if len(cohort) < 3:
					continue
				display = [
					entry
					for entry in cohort
					if entry["line"].size >= body * 1.22
					and all(
						len(group["text"]) <= 95
						and len(group["text"].split()) <= 10
						for group in entry["groups"]
					)
				]
				prose_triples = [
					entry
					for entry in cohort
					if self._is_panel_prose(
						[group["text"] for group in entry["groups"]],
						entry["line"].size,
						body,
					)
				]
				if len(display) < 2 or not prose_triples:
					continue
				starts = [
					median([entry["starts"][index] for entry in cohort])
					for index in range(3)
				]
				separators = [
					median([entry["separators"][index] for entry in cohort])
					for index in range(2)
				]
				if not (starts[0] < separators[0] < starts[1] < separators[1] < starts[2]):
					continue
				x0 = min(group["x0"] for entry in cohort for group in entry["groups"]) - body * 3.0
				x1 = max(group["x1"] for entry in cohort for group in entry["groups"]) + body * 3.0
				included = {id(entry["line"]): entry["line"] for entry in cohort}
				last_y1 = max(entry["line"].y1 for entry in cohort)
				for line in sorted(lines, key=lambda item: (item.y0, item.x0, item.seq)):
					if line.y0 <= last_y1 or id(line) in included:
						continue
					if line.y0 - last_y1 > max(55.0, body * 5.8):
						break
					if line.x1 < x0 or line.x0 > x1:
						break
					if self._panel_partition_texts(line, separators) is None:
						break
					included[id(line)] = line
					last_y1 = max(last_y1, line.y1)
				band_lines = sorted(
					included.values(),
					key=lambda line: (line.y0, line.x0, line.seq),
				)
				y0 = min(line.y0 for line in band_lines)
				y1 = max(line.y1 for line in band_lines)
				if any(
					re.match(
						r"^(?:table|tab\.|exhibit)\b",
						plain_text(line_text_tokens(line)).strip(),
						re.I,
					)
					for line in lines
					if y0 - 40.0 <= line.y0 <= y1 + 10.0
				):
					continue
				if self._three_panel_grid_evidence(page, x0, x1, y0, y1, separators):
					continue
				prose_count = sum(
					self._is_panel_prose(
						self._panel_partition_texts(line, separators) or [],
						line.size,
						body,
					)
					for line in band_lines
				)
				graphic_columns = self._three_panel_graphic_columns(
					page,
					separators,
					y0,
					y1,
					x0,
					x1,
				)
				if prose_count < 4 and not (
					prose_triples
					and graphic_columns == {0, 1, 2}
				):
					continue
				models.append(
					{
						"starts": starts,
						"separators": separators,
						"x0": x0,
						"x1": x1,
						"y0": y0,
						"y1": y1,
						"evidence": {
							"display_baselines": len(display),
							"prose_baselines": prose_count,
							"graphic_columns": len(graphic_columns),
						},
					}
				)

		deduped: List[Dict[str, Any]] = []
		for model in sorted(
			models,
			key=lambda item: (
				item["y0"],
				-(item["y1"] - item["y0"]),
			),
		):
			if any(
				max(0.0, min(model["y1"], other["y1"]) - max(model["y0"], other["y0"]))
				>= min(model["y1"] - model["y0"], other["y1"] - other["y0"]) * 0.60
				for other in deduped
			):
				continue
			deduped.append(model)
		return deduped

	def _labelled_list_triptych_models(
		self,
		page: int,
		lines: Sequence[Line],
		triples: Sequence[Dict[str, Any]],
		body: float,
	) -> List[Dict[str, Any]]:
		"""Recover a compact label/payload/list triptych as panel-local flow.

		A presentation slide can use one short coloured label row, one larger
		payload row, and a list in only one panel.  That composition has too few
		coextensive baselines for the general card detector.  Admission here is
		therefore deliberately conjunctive: typography, three aligned starts, two
		wide persistent gutters, and an explicit list confined to exactly one panel
		must all agree.  Captions, grids, images, crossing words, and lists spread
		across panels veto the inference.
		"""
		page_width, page_height = self.conv.page_sizes.get(page, (612.0, 792.0))
		if body <= 0 or len(triples) < 2:
			return []

		def short_label(text: str) -> bool:
			words = re.findall(r"[A-Za-z]+", text)
			return bool(
				1 <= len(words) <= 4
				and len(text) <= 32
				and text[-1:] not in ".!?;:"
				and list_marker(text) is None
				and not self._is_explicit_caption_label(text)
				and sum(word[:1].isupper() for word in words)
					>= max(1, math.ceil(len(words) * 0.60))
			)

		def compact_payload(text: str) -> bool:
			words = text.split()
			return bool(
				2 <= len(words) <= 14
				and len(text) <= 100
				and sum(char.isalpha() for char in text) >= 5
				and list_marker(text) is None
				and not self._is_explicit_caption_label(text)
			)

		models: List[Dict[str, Any]] = []
		ordered = sorted(triples, key=lambda entry: (entry["line"].y0, entry["line"].seq))
		for label, payload in zip(ordered, ordered[1:]):
			label_texts = [str(group["text"]).strip() for group in label["groups"]]
			payload_texts = [str(group["text"]).strip() for group in payload["groups"]]
			if not all(short_label(text) for text in label_texts):
				continue
			if not all(compact_payload(text) for text in payload_texts):
				continue
			if not (
				0 < payload["line"].y0 - label["line"].y1
				<= max(52.0, body * 4.8)
			):
				continue
			start_tolerance = max(6.0, page_width * 0.008)
			if max(
				abs(float(label["starts"][index]) - float(payload["starts"][index]))
				for index in range(3)
			) > start_tolerance:
				continue

			label_styles = [
				self._panel_group_style(label["line"], group)
				for group in label["groups"]
			]
			payload_styles = [
				self._panel_group_style(payload["line"], group)
				for group in payload["groups"]
			]
			if not self._panel_style_cohort(label_styles):
				continue
			if not self._panel_style_cohort(payload_styles):
				continue
			label_style = label_styles[0]
			payload_style = payload_styles[0]
			label_size = float(label_style.get("size", 0.0))
			payload_size = float(payload_style.get("size", 0.0))
			if not (
				body * 0.88 <= label_size <= body * 1.35
				and payload_size >= max(label_size * 1.15, body * 1.35)
				and payload_size <= body * 2.25
			):
				continue
			label_color = tuple(label_style.get("color", (0.0, 0.0, 0.0)))
			payload_color = tuple(payload_style.get("color", (0.0, 0.0, 0.0)))
			style_contrast = color_contrast(label_color, payload_color)
			if style_contrast < 1.35:
				continue

			starts = [
				median([float(label["starts"][index]), float(payload["starts"][index])])
				for index in range(3)
			]
			gutter_intervals: List[Tuple[float, float]] = []
			for index in range(2):
				left = max(
					float(label["groups"][index]["x1"]),
					float(payload["groups"][index]["x1"]),
				)
				right = min(
					float(label["groups"][index + 1]["x0"]),
					float(payload["groups"][index + 1]["x0"]),
				)
				if right - left < max(36.0, page_width * 0.05):
					break
				gutter_intervals.append((left, right))
			if len(gutter_intervals) != 2:
				continue
			separators = [(left + right) / 2.0 for left, right in gutter_intervals]
			if not (
				starts[0] < separators[0] < starts[1]
				< separators[1] < starts[2]
			):
				continue

			markers: List[Tuple[Line, int]] = []
			for line in lines:
				if line.y0 <= payload["line"].y1:
					continue
				text = plain_text(line_text_tokens(line)).strip()
				if list_marker(text) is None or sum(char.isalpha() for char in text) < 3:
					continue
				parts = self._panel_partition_texts(line, separators)
				if parts is None or sum(bool(part) for part in parts) != 1:
					continue
				panel_index = next(index for index, part in enumerate(parts) if part)
				markers.append((line, panel_index))
			if len(markers) < 3 or len({column for _line, column in markers}) != 1:
				continue
			markers.sort(key=lambda item: (item[0].y0, item[0].seq))
			list_panel = markers[0][1]
			if any(
				abs(line.x0 - starts[list_panel]) > max(26.0, body * 2.5)
				or not body * 0.72 <= line.size <= body * 1.28
				for line, _column in markers
			):
				continue
			marker_pitches = [
				right[0].y0 - left[0].y0
				for left, right in zip(markers, markers[1:])
			]
			if any(
				pitch <= 0 or pitch > max(44.0, body * 4.0)
				for pitch in marker_pitches
			):
				continue

			payload_y1 = payload["line"].y1
			first_marker_y0 = markers[0][0].y0
			for line in lines:
				if not payload["line"].y0 <= line.y0 < first_marker_y0:
					continue
				if self._panel_styles_match(
					self._panel_line_style(line),
					payload_style,
				):
					parts = self._panel_partition_texts(line, separators)
					if parts is not None and any(parts):
						payload_y1 = max(payload_y1, line.y1)
			if first_marker_y0 - payload_y1 > max(110.0, body * 10.0):
				continue

			last_y1 = max(line.y1 for line, _column in markers)
			last_line = markers[-1][0]
			for line in sorted(lines, key=lambda item: (item.y0, item.x0, item.seq)):
				if line.y0 <= last_line.y0:
					continue
				if line.y0 - last_y1 > max(32.0, body * 3.0):
					break
				parts = self._panel_partition_texts(line, separators)
				if (
					parts is None
					or sum(bool(part) for part in parts) != 1
					or not parts[list_panel]
					or not body * 0.72 <= line.size <= body * 1.28
				):
					break
				last_y1 = max(last_y1, line.y1)
				last_line = line

			x0 = max(0.0, starts[0] - body * 2.0)
			relevant_lines = [
				line
				for line in lines
				if label["line"].y0 - 2.0 <= line.y0 <= last_y1 + 2.0
				and self._panel_partition_texts(line, separators) is not None
			]
			x1 = min(
				page_width,
				max(
					[group["x1"] for entry in (label, payload) for group in entry["groups"]]
					+ [line.x1 for line in relevant_lines]
				) + body * 2.0,
			)
			y0 = label["line"].y0
			y1 = last_y1
			if any(
				re.match(
					r"^(?:table|tab\.|exhibit)\b",
					plain_text(line_text_tokens(line)).strip(),
					re.I,
				)
				for line in lines
				if y0 - 40.0 <= line.y0 <= y1 + 10.0
			):
				continue
			if self._three_panel_grid_evidence(page, x0, x1, y0, y1, separators):
				continue
			if any(
				image.page == page
				and rects_intersect(
					(x0, y0, x1, y1),
					(image.x0, image.y0, image.x1, image.y1),
				)
				for image in self.conv.images
			):
				continue
			if any(
				self._panel_partition_texts(line, separators) is None
				for line in lines
				if y0 - 2.0 <= (line.y0 + line.y1) / 2.0 <= y1 + 2.0
				and x0 <= (line.x0 + line.x1) / 2.0 <= x1
			):
				continue

			models.append(
				{
					"mode": "labelled_list_triptych",
					"starts": starts,
					"separators": separators,
					"x0": x0,
					"x1": x1,
					"y0": y0,
					"y1": y1,
					"label_y0": label["line"].y0,
					"label_y1": label["line"].y1,
					"payload_y0": payload["line"].y0,
					"payload_y1": payload_y1,
					"list_y0": first_marker_y0,
					"list_panel": list_panel,
					"label_style": label_style,
					"payload_style": payload_style,
					"confidence": 0.94,
					"evidence": {
						"aligned_starts": 3,
						"label_baselines": 1,
						"payload_baselines": 1 + sum(
							line is not payload["line"]
							and payload["line"].y0 <= line.y0 <= payload_y1
							and self._panel_styles_match(
								self._panel_line_style(line),
								payload_style,
							)
							for line in lines
						),
						"explicit_list_items": len(markers),
						"list_panel": list_panel,
						"gutter_widths": [
							round(right - left, 3)
							for left, right in gutter_intervals
						],
						"style_contrast": round(style_contrast, 3),
						"admission_reasons": [
							"aligned_label_and_payload_starts",
							"coherent_label_and_payload_styles",
							"contrasting_display_roles",
							"two_persistent_empty_gutters",
							"explicit_list_confined_to_one_panel",
							"no_grid_caption_image_or_crossing_text",
						],
					},
				}
			)
		return models

	def _panel_group_style(
		self,
		line: Line,
		group: Dict[str, Any],
	) -> Dict[str, Any]:
		x0 = float(group["x0"])
		x1 = float(group["x1"])
		chars = [
			char
			for char in line.chars
			if char.text.strip()
			and x0 - 1.5 <= (char.x0 + char.x1) / 2.0 <= x1 + 1.5
		]
		return self._panel_char_style(chars)

	def _panel_line_style(self, line: Line) -> Dict[str, Any]:
		return self._panel_char_style([char for char in line.chars if char.text.strip()])

	def _panel_char_style(self, chars: Sequence[Char]) -> Dict[str, Any]:
		if not chars:
			return {}
		return {
			"size": median([char.size for char in chars]),
			"fonts": tuple(sorted({strip_subset(str(char.font.base_font)) for char in chars})),
			"color": tuple(
				median([char.fill_color[index] for char in chars])
				for index in range(3)
			),
			"bold_ratio": sum(char.bold for char in chars) / len(chars),
			"italic_ratio": sum(char.italic for char in chars) / len(chars),
		}

	def _panel_style_cohort(self, styles: Sequence[Dict[str, Any]]) -> bool:
		return bool(
			styles
			and all(style for style in styles)
			and all(self._panel_styles_match(style, styles[0]) for style in styles[1:])
		)

	def _panel_styles_match(
		self,
		left: Dict[str, Any],
		right: Dict[str, Any],
	) -> bool:
		if not left or not right or left.get("fonts") != right.get("fonts"):
			return False
		left_size = float(left.get("size", 0.0))
		right_size = float(right.get("size", 0.0))
		left_color = tuple(left.get("color", (0.0, 0.0, 0.0)))
		right_color = tuple(right.get("color", (0.0, 0.0, 0.0)))
		return bool(
			abs(left_size - right_size) <= max(0.65, right_size * 0.06)
			and max(abs(left_color[index] - right_color[index]) for index in range(3)) <= 0.06
			and abs(float(left.get("bold_ratio", 0.0)) - float(right.get("bold_ratio", 0.0))) <= 0.20
			and abs(float(left.get("italic_ratio", 0.0)) - float(right.get("italic_ratio", 0.0))) <= 0.20
		)

	def _panel_line_context(self, line: Line) -> Optional[Dict[str, Any]]:
		visible_centers = [
			(char.x0 + char.x1) / 2.0
			for char in line.chars
			if char.text.strip()
		]
		center_x = median(visible_centers) if visible_centers else (line.x0 + line.x1) / 2.0
		center_y = (line.y0 + line.y1) / 2.0
		for band_index, band in enumerate(self._inferred_panel_bands.get(line.page, [])):
			if band.get("mode") != "labelled_list_triptych":
				continue
			if not (
				band["x0"] <= center_x <= band["x1"]
				and band["y0"] - 3.0 <= center_y <= band["y1"] + 3.0
			):
				continue
			panel_index = sum(center_x >= separator for separator in band["separators"])
			style = self._panel_line_style(line)
			role = "content"
			if (
				band["label_y0"] - 3.0 <= center_y <= band["label_y1"] + 3.0
				and self._panel_styles_match(style, band["label_style"])
			):
				role = "label"
			elif (
				band["payload_y0"] - 3.0 <= center_y <= band["payload_y1"] + 3.0
				and self._panel_styles_match(style, band["payload_style"])
			):
				role = "payload"
			elif center_y >= band["list_y0"] - 3.0 and panel_index == band["list_panel"]:
				role = "list"
			bounds = [band["x0"], *band["separators"], band["x1"]]
			return {
				"mode": band["mode"],
				"group": "p%d-triptych-%d" % (line.page, band_index + 1),
				"index": panel_index,
				"role": role,
				"start": band["starts"][panel_index],
				"right": bounds[panel_index + 1],
				"bbox": (
					bounds[panel_index],
					band["y0"],
					bounds[panel_index + 1],
					band["y1"],
				),
				"confidence": float(band.get("confidence", 0.90)),
				"evidence": dict(band.get("evidence", {})),
			}
		return None

	def _three_panel_groups(self, line: Line) -> List[Dict[str, Any]]:
		boxes = word_boxes(line)
		if not boxes:
			return []
		threshold = max(28.0, line.size * 2.5)
		groups: List[List[Tuple[str, float, float, float, float]]] = [[boxes[0]]]
		previous_right = boxes[0][3]
		for box in boxes[1:]:
			if box[1] - previous_right >= threshold:
				groups.append([box])
			else:
				groups[-1].append(box)
			previous_right = box[3]
		return [
			{
				"x0": min(box[1] for box in group),
				"x1": max(box[3] for box in group),
				"text": cleanup_spaces(" ".join(box[0] for box in group)).strip(),
			}
			for group in groups
		]

	def _panel_text_has_alpha(self, text: str) -> bool:
		letters = sum(char.isalpha() for char in text)
		return letters >= 3

	def _panel_partition_texts(
		self,
		line: Line,
		separators: Sequence[float],
	) -> Optional[List[str]]:
		parts: List[List[str]] = [[] for _column in range(3)]
		for text, x0, _y0, x1, _y1 in word_boxes(line):
			if any(x0 + 1.5 < separator < x1 - 1.5 for separator in separators):
				return None
			center = (x0 + x1) / 2.0
			column = sum(center >= separator for separator in separators)
			parts[column].append(text)
		return [
			cleanup_spaces(" ".join(part)).strip()
			for part in parts
		]

	def _is_panel_prose(
		self,
		texts: Sequence[str],
		size: float,
		body: float,
	) -> bool:
		if not (body * 0.85 <= size <= body * 1.18):
			return False
		occupied = [text for text in texts if text]
		if len(occupied) < 2:
			return False
		if sum(len(text.split()) for text in occupied) < 6:
			return False
		return all(
			len(text.split()) >= 2
			and sum(char.isalpha() for char in text) >= 4
			for text in occupied
		)

	def _three_panel_graphic_columns(
		self,
		page: int,
		separators: Sequence[float],
		y0: float,
		y1: float,
		x0: float,
		x1: float,
	) -> set[int]:
		columns: set[int] = set()
		for image in self.conv.images:
			if (
				image.page != page
				or image.y1 < y0
				or image.y0 > y1
			):
				continue
			center = (image.x0 + image.x1) / 2.0
			if x0 <= center <= x1:
				columns.add(sum(center >= separator for separator in separators))
		return columns

	def _three_panel_grid_evidence(
		self,
		page: int,
		x0: float,
		x1: float,
		y0: float,
		y1: float,
		separators: Sequence[float],
	) -> bool:
		width = max(1.0, x1 - x0)
		height = max(1.0, y1 - y0)
		horizontal = [
			segment
			for segment in self.conv.segments
			if segment.page == page
			and segment.horizontal
			and segment.length >= width * 0.70
			and y0 - 3.0 <= (segment.y0 + segment.y1) / 2.0 <= y1 + 3.0
		]
		vertical = [
			segment
			for segment in self.conv.segments
			if segment.page == page
			and segment.vertical
			and segment.length >= height * 0.55
			and any(
				abs((segment.x0 + segment.x1) / 2.0 - separator) <= 5.0
				for separator in separators
			)
		]
		return len(horizontal) >= 3 and len(vertical) >= 2

	def _split_line_on_panel_separators(
		self,
		line: Line,
		separators: Sequence[float],
	) -> List[Line]:
		groups: List[List[Char]] = [[] for _column in range(3)]
		for char in line.chars:
			center = (char.x0 + char.x1) / 2.0
			column = sum(center >= separator for separator in separators)
			groups[column].append(char)
		occupied = [
			group
			for group in groups
			if any(char.text.strip() for char in group)
		]
		if len(occupied) <= 1:
			return [line]
		return [
			Line(
				group,
				line.page,
				min(char.seq for char in group),
				source_order=line.source_order,
				writing_mode=line.writing_mode,
			)
			for group in occupied
		]

	def _order_three_panel_bands(
		self,
		lines: List[Line],
		bands: Sequence[Dict[str, Any]],
	) -> List[Line]:
		physical = sorted(lines, key=lambda line: (line.y0, line.x0, line.seq))
		emitted: set[int] = set()
		emitted_bands: set[int] = set()
		out: List[Line] = []
		for line in physical:
			if id(line) in emitted:
				continue
			center_y = (line.y0 + line.y1) / 2.0
			band_index = next(
				(
					index
					for index, band in enumerate(bands)
					if band["y0"] - 3.0 <= center_y <= band["y1"] + 3.0
					and band["x0"] <= (line.x0 + line.x1) / 2.0 <= band["x1"]
				),
				None,
			)
			if band_index is None:
				out.append(line)
				emitted.add(id(line))
				continue
			if band_index in emitted_bands:
				continue
			band = bands[band_index]
			band_lines = [
				candidate
				for candidate in physical
				if band["y0"] - 3.0 <= (candidate.y0 + candidate.y1) / 2.0 <= band["y1"] + 3.0
				and band["x0"] <= (candidate.x0 + candidate.x1) / 2.0 <= band["x1"]
			]
			columns: List[List[Line]] = [[], [], []]
			for candidate in band_lines:
				visible_centers = [
					(char.x0 + char.x1) / 2.0
					for char in candidate.chars
					if char.text.strip()
				]
				center_x = (
					median(visible_centers)
					if visible_centers
					else (candidate.x0 + candidate.x1) / 2.0
				)
				column = sum(
					center_x >= separator
					for separator in band["separators"]
				)
				columns[column].append(candidate)
			for column in columns:
				out.extend(
					sorted(
						column,
						key=lambda candidate: (
							candidate.y0,
							candidate.x0,
							candidate.seq,
						),
					)
				)
			emitted.update(id(candidate) for candidate in band_lines)
			emitted_bands.add(band_index)
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
		panel_bands = self._inferred_panel_bands.get(page, [])
		if panel_bands:
			bands = [
				band
				for band in bands
				if not any(
					max(0.0, min(band[2], panel["y1"]) - max(band[1], panel["y0"]))
					>= min(band[2] - band[1], panel["y1"] - panel["y0"]) * 0.35
					for panel in panel_bands
				)
			]
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

	def _cached_inferred_column_separator_infos(
		self,
		page: int,
		lines: List[Line],
	) -> List[Tuple[float, float, float]]:
		"""Return one immutable-by-contract inference result per page build."""
		if page not in self._inferred_column_bands:
			self._inferred_column_bands[page] = self._inferred_column_separator_infos(
				page,
				lines,
			)
		# Do not expose the cached list to accidental caller mutation.
		return list(self._inferred_column_bands[page])

	def _inferred_column_separator_infos(self, page: int, lines: List[Line]) -> List[Tuple[float, float, float]]:
		width, _height = self.conv.page_sizes.get(page, (612, 792))
		candidates: List[Tuple[float, float, float, Line]] = []
		for line in lines:
			chars = sorted([c for c in line.chars if c.text], key=lambda c: (c.x0, c.seq))
			if len(chars) < 12:
				continue
			text_lengths = [len(char.text.strip()) for char in chars]
			text_prefix_lengths = [0]
			for text_length in text_lengths:
				text_prefix_lengths.append(text_prefix_lengths[-1] + text_length)
			total_text_length = text_prefix_lengths[-1]
			prev: Optional[Char] = None
			for idx, ch in enumerate(chars):
				if prev is None:
					prev = ch
					continue
				gap = ch.x0 - prev.x1
				if gap < max(72.0, line.size * 7.0):
					prev = ch
					continue
				left_len = text_prefix_lengths[idx]
				right_len = total_text_length - left_len
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
		out.extend(self._unruled_prose_column_infos(page, lines))
		out.extend(self._side_display_prose_column_infos(page, lines))
		deduped: List[Tuple[float, float, float]] = []
		for sep_x, y0, y1 in sorted(out, key=lambda item: (item[1], item[0])):
			if any(abs(sep_x - x) <= 12 and not (y1 < oy0 or y0 > oy1) for x, oy0, oy1 in deduped):
				continue
			deduped.append((sep_x, y0, y1))
		return deduped

	def _side_display_prose_column_infos(
		self,
		page: int,
		lines: Sequence[Line],
	) -> List[Tuple[float, float, float]]:
		"""Recover a display-title rail beside one sustained prose stream.

		Editorial reports sometimes reserve a narrow left rail for a large,
		multi-line section title while the article starts beside it and continues
		down the page.  Baseline clustering can then splice title fragments into
		the prose.  This is not a general two-column layout: admission requires a
		sparse, bold display cohort, an independently stable body column, and no
		ordinary prose stream in the title rail.
		"""
		width, height = self.conv.page_sizes.get(page, (612.0, 792.0))
		if height < width * 1.18 or len(lines) < 14:
			return []
		body_size = self._body_font_size(lines)
		if body_size <= 0:
			return []

		display_fragments: List[Tuple[Line, List[Char]]] = []
		body_fragments: List[Tuple[Line, List[Char]]] = []
		for line in lines:
			if line.writing_mode != "horizontal":
				continue
			visible = [char for char in ordered_line_chars(line) if char.text.strip()]
			display = [
				char
				for char in visible
				if char.bold
				and char.size >= max(body_size * 2.10, body_size + 10.0)
			]
			if display:
				display_fragments.append((line, display))
			body = [
				char
				for char in visible
				if not char.bold
				and abs(char.size - body_size) <= max(0.65, body_size * 0.07)
			]
			if body:
				body_fragments.append((line, body))

		if not 2 <= len(display_fragments) <= 4 or len(body_fragments) < 12:
			return []
		display_chars = [char for _line, chars in display_fragments for char in chars]
		if sum(character.isalpha() for char in display_chars for character in char.text) < 12:
			return []
		display_x0 = min(char.x0 for char in display_chars)
		display_x1 = max(char.x1 for char in display_chars)
		display_y0 = min(char.y0 for char in display_chars)
		display_y1 = max(char.y1 for char in display_chars)
		if (
			display_x0 > width * 0.30
			or display_x1 > width * 0.47
			or display_y0 > height * 0.22
			or display_y1 > height * 0.33
		):
			return []

		body_starts = [min(char.x0 for char in chars) for _line, chars in body_fragments]
		body_start = median(body_starts)
		stable_body = [
			(line, chars)
			for line, chars in body_fragments
			if abs(min(char.x0 for char in chars) - body_start) <= max(12.0, body_size * 1.25)
		]
		if len(stable_body) < 10 or body_start < width * 0.48:
			return []
		if display_x1 + max(body_size * 2.0, width * 0.04) >= body_start:
			return []
		body_y0 = min(min(char.y0 for char in chars) for _line, chars in stable_body)
		body_y1 = max(max(char.y1 for char in chars) for _line, chars in stable_body)
		if body_y1 - body_y0 < height * 0.48:
			return []
		if max(body_y0, display_y0) > min(body_y1, display_y1):
			return []

		separator = (display_x1 + body_start) / 2.0
		left_body_rows = [
			(line, chars)
			for line, chars in body_fragments
			if sum(
				character.isalpha()
				for char in chars
				if (char.x0 + char.x1) / 2 < separator
				for character in char.text
			) >= 16
		]
		if len(left_body_rows) >= 3:
			return []
		prose_rows = 0
		for _line, chars in stable_body:
			text = cleanup_spaces("".join(char.text for char in chars))
			if (
				sum(character.isalpha() for character in text) >= 18
				and any(character.islower() for character in text)
			):
				prose_rows += 1
		if prose_rows < math.ceil(len(stable_body) * 0.65):
			return []
		if any(
			self._is_explicit_caption_label(
				cleanup_spaces("".join(char.text for char in chars))
			)
			for _line, chars in stable_body
		):
			return []

		title_box = (display_x0, display_y0, display_x1, display_y1)
		if any(
			image.page == page
			and rects_intersect(
				title_box,
				(image.x0 - body_size, image.y0 - body_size, image.x1 + body_size, image.y1 + body_size),
			)
			for image in self.conv.images
		):
			return []
		coextensive_vertical_rules = [
			segment
			for segment in self.conv.segments
			if segment.page == page
			and segment.vertical
			and segment.length >= (body_y1 - body_y0) * 0.45
			and display_x0 - body_size <= (segment.x0 + segment.x1) / 2 <= body_start + body_size
		]
		if len(coextensive_vertical_rules) >= 2:
			return []
		return [(separator, min(body_y0, display_y0), body_y1)]

	def _unruled_prose_column_infos(
		self,
		page: int,
		lines: List[Line],
	) -> List[Tuple[float, float, float]]:
		"""Infer a long two-column prose band from one stable whitespace gutter.

		Scientific-paper producers commonly place both columns on the same
		baselines without drawing a separator.  The physical line builder then
		interleaves the two reading streams.  Admit this rule-free case only
		when many prose-heavy baselines independently expose the same central
		gutter over a substantial fraction of the page.  Compact tables, cards,
		and label/value grids deliberately fail the span and prose requirements.
		"""
		width, height = self.conv.page_sizes.get(page, (612.0, 792.0))
		evidence: List[Tuple[float, float, float, int, int, Line]] = []
		for line in lines:
			if line.writing_mode != "horizontal" or line.size <= 0:
				continue
			chars = sorted(
				[char for char in line.chars if char.text and not char.text.isspace()],
				key=lambda char: (char.x0, char.seq),
			)
			if len(chars) < 20:
				continue
			text_lengths = [len(char.text.strip()) for char in chars]
			text_prefix_lengths = [0]
			for text_length in text_lengths:
				text_prefix_lengths.append(text_prefix_lengths[-1] + text_length)
			total_text_length = text_prefix_lengths[-1]
			best: Optional[Tuple[float, float, int, int]] = None
			for index, (left_char, right_char) in enumerate(
				zip(chars, chars[1:]),
				1,
			):
				gap = right_char.x0 - left_char.x1
				separator = (left_char.x1 + right_char.x0) / 2.0
				if (
					gap < max(12.0, line.size * 1.20)
					or not width * 0.38 <= separator <= width * 0.62
				):
					continue
				left_count = text_prefix_lengths[index]
				right_count = total_text_length - left_count
				if left_count < 20 or right_count < 20:
					continue
				candidate = (gap, separator, left_count, right_count)
				if best is None or candidate[0] > best[0]:
					best = candidate
			if best is None:
				continue
			gap, separator, left_count, right_count = best
			evidence.append(
				(
					separator,
					(line.y0 + line.y1) / 2.0,
					gap,
					left_count,
					right_count,
					line,
				)
			)
		x_groups: List[List[Tuple[float, float, float, int, int, Line]]] = []
		for item in sorted(evidence, key=lambda value: value[0]):
			for group in x_groups:
				if abs(item[0] - median([value[0] for value in group])) <= 6.0:
					group.append(item)
					break
			else:
				x_groups.append([item])
		qualified = [
			group
			for group in x_groups
			if len(group) >= 8
		]
		compact_mode = False
		strict_span = max(
			(
				max(value[5].y1 for value in group)
				- min(value[5].y0 for value in group)
				for group in qualified
			),
			default=0.0,
		)
		if not qualified or strict_span < height * 0.30:
			compact_groups = self._compact_unruled_prose_groups(
				page,
				lines,
				width,
				height,
			)
			strict_medium = bool(
				qualified
				and self._is_anchored_medium_prose_column_band(
					page,
					max(
						qualified,
						key=lambda group: (
							len(group),
							max(value[1] for value in group)
							- min(value[1] for value in group),
						),
					),
					lines,
					width,
					height,
				)
			)
			if compact_groups and (
				not qualified
				or strict_span < height * 0.20
				or not strict_medium
			):
				qualified = compact_groups
				compact_mode = True
		if not qualified:
			return []
		qualified.sort(
			key=lambda group: (
				len(group),
				max(value[1] for value in group) - min(value[1] for value in group),
			),
			reverse=True,
		)
		def bounds(
			group: List[Tuple[float, float, float, int, int, Line]],
		) -> Tuple[float, float]:
			return (
				min(value[5].y0 for value in group),
				max(value[5].y1 for value in group),
			)

		dominant = qualified[0]
		if compact_mode:
			# Compact evidence is intentionally validated one bounded vertical
			# cohort at a time.  A figure or full-width transition can separate two
			# legitimate column sections that share the same gutter; collapsing both
			# cohorts into one page-spanning group makes the compact detector reject
			# them as an overlong card/table.  Keep every disjoint, independently
			# qualified cohort, while retaining the existing overlap veto against
			# competing grid-like gutters.
			selected = sorted(qualified, key=lambda group: bounds(group)[0])
			for index, group in enumerate(selected):
				y0, y1 = bounds(group)
				for other in selected[index + 1 :]:
					other_y0, other_y1 = bounds(other)
					overlap = max(0.0, min(y1, other_y1) - max(y0, other_y0))
					minimum_span = min(y1 - y0, other_y1 - other_y0)
					if minimum_span > 0 and overlap >= minimum_span * 0.35:
						return []
			separators = [median([value[0] for value in group]) for group in selected]
			if max(separators) - min(separators) > max(18.0, width * 0.04):
				return []
			competing: List[List[Tuple[float, float, float, int, int, Line]]] = []
			minimum_page_span = height * 0.09
			minimum_line_span = 6.0
			medium_dominant = False
		else:
			competing = [
				group
				for group in qualified[1:]
				if len(group) >= math.ceil(len(dominant) * 0.70)
			]

		if not compact_mode and competing:
			strong_groups = [dominant] + competing
			# Two similarly persistent gutters over the same vertical extent are
			# table/grid evidence.  Distinct document sections may legitimately
			# shift an otherwise stable two-column gutter, though, so do not let
			# a later disjoint section veto the earlier one.
			for index, group in enumerate(strong_groups):
				y0, y1 = bounds(group)
				for other in strong_groups[index + 1 :]:
					other_y0, other_y1 = bounds(other)
					overlap = max(0.0, min(y1, other_y1) - max(y0, other_y0))
					minimum_span = min(y1 - y0, other_y1 - other_y0)
					if minimum_span > 0 and overlap >= minimum_span * 0.35:
						return []
			separators = [median([value[0] for value in group]) for group in strong_groups]
			if max(separators) - min(separators) > max(18.0, width * 0.04):
				return []
			spans = [bounds(group)[1] - bounds(group)[0] for group in strong_groups]
			outer_y0 = min(bounds(group)[0] for group in strong_groups)
			outer_y1 = max(bounds(group)[1] for group in strong_groups)
			if (
				sum(len(group) for group in strong_groups) < 16
				or sum(spans) < height * 0.42
				or outer_y1 - outer_y0 < height * 0.48
			):
				return []
			selected = sorted(strong_groups, key=lambda group: bounds(group)[0])
			minimum_page_span = height * 0.20
			minimum_line_span = 10.0
			medium_dominant = False
		elif not compact_mode:
			selected = [dominant]
			minimum_page_span = height * 0.30
			minimum_line_span = 14.0
			medium_dominant = False
			medium_dominant = self._is_anchored_medium_prose_column_band(
				page,
				dominant,
				lines,
				width,
				height,
			)
			if medium_dominant:
				minimum_page_span = height * 0.20

		out: List[Tuple[float, float, float]] = []
		for group in selected:
			y0, y1 = bounds(group)
			if y1 - y0 < max(
				minimum_page_span,
				median([value[5].size for value in group]) * minimum_line_span,
			):
				return []
			separator = median([value[0] for value in group])
			separator_tolerance = 14.0 if compact_mode else 6.0
			if max(abs(value[0] - separator) for value in group) > separator_tolerance:
				return []
			if median([value[3] + value[4] for value in group]) < 55:
				return []
			y0 = self._extend_proven_academic_column_start(
				page,
				lines,
				group,
				separator,
				y0,
				width,
				height,
				out[-1][2] if out else None,
			)
			if medium_dominant and group is dominant:
				y1 = self._extend_medium_prose_column_end(
					lines,
					separator,
					y1,
					median([value[5].size for value in group]),
					width,
					height,
				)
			if not competing:
				anchored_start = self._side_visual_column_start(
					page,
					separator,
					y0,
					group,
					lines,
					width,
					height,
				)
				if anchored_start is not None:
					y0 = min(y0, anchored_start)
			y1 = self._extend_qualified_one_sided_prose_tail(
				page,
				lines,
				group,
				separator,
				y1,
				width,
				height,
			)
			out.append((separator, y0, y1))
		if compact_mode:
			self._compact_column_bands[page] = list(out)
		return out

	def _extend_proven_academic_column_start(
		self,
		page: int,
		lines: Sequence[Line],
		group: Sequence[Tuple[float, float, float, int, int, Line]],
		separator: float,
		y0: float,
		width: float,
		height: float,
		previous_y1: Optional[float],
	) -> float:
		"""Include ragged headings immediately above a proven prose gutter.

		Academic producers can begin the two physical columns on different
		baselines.  A short bold heading in one column may consequently share a
		physical line with ordinary prose from the other, just above the first
		baseline that is strong enough to prove the gutter.  Keeping the original
		band start merges those independent streams and hides the heading.

		This pass cannot infer a gutter.  It only looks a few body lines backward
		from a cohort that already passed the long/compact column admission gates.
		Every accepted two-sided line must expose real whitespace around that exact
		separator, and a short bold fragment must be paired with ordinary prose on
		the other side.  Continuous full-width text, graphics, rules, captions,
		lists, panels, and a preceding column band stop the extension.
		"""
		body = median([value[5].size for value in group])
		if body <= 0:
			return y0
		lookback = max(body * 5.0, height * 0.065)
		floor = y0 - lookback
		if previous_y1 is not None:
			floor = max(floor, previous_y1 + body * 0.35)
		if floor >= y0:
			return y0

		def fragment(chars: Sequence[Char]) -> Tuple[str, int, float]:
			ordered = sorted(chars, key=lambda char: (char.x0, char.seq))
			text = (
				plain_text(
					line_text_tokens(
						Line(
							list(ordered),
							page,
							min((char.seq for char in ordered), default=0),
						)
					)
				).strip()
				if ordered
				else ""
			)
			weight = sum(max(1, len(char.text.strip())) for char in ordered)
			bold_weight = sum(
				max(1, len(char.text.strip()))
				for char in ordered
				if char.bold
			)
			return text, sum(char.isalpha() for char in text), bold_weight / max(weight, 1)

		def heading_like(value: Tuple[str, int, float]) -> bool:
			text, alpha, bold_ratio = value
			words = [word for word in text.split() if any(char.isalpha() for char in word)]
			return (
				4 <= alpha
				and 1 <= len(words) <= 8
				and len(text) <= 90
				and bold_ratio >= 0.70
				and text[-1:] not in ".!?:;"
			)

		def prose_like(value: Tuple[str, int, float]) -> bool:
			text, alpha, bold_ratio = value
			words = [word for word in text.split() if any(char.isalpha() for char in word)]
			return alpha >= 15 and len(words) >= 4 and bold_ratio < 0.35

		def graphic_barrier(upper: float, lower: float) -> bool:
			# Coordinates are normalized to the page's top-left origin here.
			for image in self.conv.images:
				if image.page != page or image.y1 < upper - body or image.y0 > lower + body:
					continue
				if image.x1 >= separator - body and image.x0 <= separator + body:
					return True
				if image.x1 - image.x0 >= width * 0.22:
					return True
			for fill in self.conv.fills:
				if fill.page != page or fill.y1 < upper - body or fill.y0 > lower + body:
					continue
				if (
					fill.x1 - fill.x0 >= width * 0.18
					or fill.x0 <= separator <= fill.x1
				):
					return True
			for segment in self.conv.segments:
				if segment.page != page:
					continue
				segment_y0, segment_y1 = sorted((segment.y0, segment.y1))
				if segment_y1 < upper - body or segment_y0 > lower + body:
					continue
				if segment.horizontal and segment.length >= width * 0.25:
					return True
				if (
					segment.vertical
					and abs(((segment.x0 + segment.x1) / 2.0) - separator) <= body
					and segment.length >= body * 2.0
				):
					return True
			for panel in self._inferred_panel_bands.get(page, []):
				if panel["y1"] >= upper - body and panel["y0"] <= lower + body:
					return True
			return False

		candidates = sorted(
			(
				line
				for line in lines
				if line.writing_mode == "horizontal"
				and floor <= (line.y0 + line.y1) / 2.0 < y0
				and plain_text(line_text_tokens(line)).strip()
			),
			key=lambda line: (line.y0, line.x0, line.seq),
			reverse=True,
		)
		accepted: List[Line] = []
		alpha_by_side = [0, 0]
		has_heading_prose_pair = False
		front = y0
		for line in candidates:
			if front - line.y1 > max(body * 2.4, 28.0):
				break
			if not body * 0.78 <= line.size <= body * 1.35:
				break
			text = plain_text(line_text_tokens(line)).strip()
			if (
				self._is_explicit_caption_label(text)
				or self._is_toc_navigation_row(line, text)
				or list_marker(text) is not None
			):
				break
			visible = sorted(
				[char for char in line.chars if char.text and not char.text.isspace()],
				key=lambda char: (char.x0, char.seq),
			)
			if not visible:
				continue
			if sum(1 for char in visible if char.link) / len(visible) >= 0.50:
				break
			left_visible = [
				char for char in visible if (char.x0 + char.x1) / 2.0 < separator
			]
			right_visible = [
				char for char in visible if (char.x0 + char.x1) / 2.0 >= separator
			]
			if left_visible and right_visible:
				left_edge = max(char.x1 for char in left_visible)
				right_edge = min(char.x0 for char in right_visible)
				if not (
					left_edge < separator < right_edge
					and right_edge - left_edge >= max(8.0, line.size * 0.72)
				):
					# Ordinary full-width prose crossing the gutter is a hard block.
					break
			elif left_visible:
				if max(char.x1 for char in left_visible) > separator - body * 0.25:
					break
			elif right_visible:
				if min(char.x0 for char in right_visible) < separator + body * 0.25:
					break
			else:
				continue
			if graphic_barrier(line.y0, front):
				break

			# Retain authored spaces for lexical admission. They are excluded only
			# from the geometric gap measurement above.
			left = [
				char
				for char in line.chars
				if char.text and (char.x0 + char.x1) / 2.0 < separator
			]
			right = [
				char
				for char in line.chars
				if char.text and (char.x0 + char.x1) / 2.0 >= separator
			]
			left_value = fragment(left)
			right_value = fragment(right)
			alpha_by_side[0] += left_value[1]
			alpha_by_side[1] += right_value[1]
			has_heading_prose_pair = has_heading_prose_pair or (
				(heading_like(left_value) and prose_like(right_value))
				or (heading_like(right_value) and prose_like(left_value))
			)
			accepted.append(line)
			front = min(front, line.y0)

		if (
			accepted
			and has_heading_prose_pair
			and alpha_by_side[0] >= 5
			and alpha_by_side[1] >= 5
		):
			return min(y0, min(line.y0 for line in accepted))
		return y0

	def _compact_unruled_prose_groups(
		self,
		page: int,
		lines: Sequence[Line],
		width: float,
		height: float,
	) -> List[List[Tuple[float, float, float, int, int, Line]]]:
		"""Admit a short but text-dense two-column prose section.

		Some book and report pages contain only six or seven paired baselines
		below a figure, heading, or footnote transition. The long-page detector
		intentionally rejects those bands, so their two columns merge into one
		reading stream. This fallback requires balanced prose on both sides of
		one stable central gutter and rejects ruled/grid-like geometry, so
		compact cards and small tables remain in physical order.
		"""
		# Keep the complete whitespace interval, rather than only its midpoint.
		# Ragged prose columns can end at substantially different x positions
		# while still sharing one real gutter: the midpoint then drifts even though
		# every interval contains the same separator.  The downstream admission
		# gates remain unchanged; interval intersection is strictly the geometric
		# reconciliation used to form candidate cohorts.
		interval_evidence: List[
			Tuple[float, float, float, float, int, int, Line]
		] = []
		for line in lines:
			if line.writing_mode != "horizontal" or line.size <= 0:
				continue
			chars = sorted(
				[char for char in line.chars if char.text and not char.text.isspace()],
				key=lambda char: (char.x0, char.seq),
			)
			if len(chars) < 20:
				continue
			text_lengths = [len(char.text.strip()) for char in chars]
			prefix = [0]
			for text_length in text_lengths:
				prefix.append(prefix[-1] + text_length)
			total = prefix[-1]
			best: Optional[Tuple[float, float, int, int, float, float]] = None
			for index, (left_char, right_char) in enumerate(zip(chars, chars[1:]), 1):
				gap = right_char.x0 - left_char.x1
				separator = (left_char.x1 + right_char.x0) / 2.0
				if (
					gap < max(8.0, line.size * 0.72)
					or not width * 0.37 <= separator <= width * 0.63
				):
					continue
				left_count = prefix[index]
				right_count = total - left_count
				if left_count < 20 or right_count < 20:
					continue
				candidate = (
					gap,
					separator,
					left_count,
					right_count,
					left_char.x1,
					right_char.x0,
				)
				if best is None or candidate[0] > best[0]:
					best = candidate
			if best is not None:
				interval_evidence.append(
					(
						max(best[4], width * 0.37),
						min(best[5], width * 0.63),
						(line.y0 + line.y1) / 2.0,
						best[0],
						best[2],
						best[3],
						line,
					)
				)

		interval_groups: List[
			List[Tuple[float, float, float, float, int, int, Line]]
		] = []
		for item in sorted(
			interval_evidence,
			key=lambda value: (
				(value[0] + value[1]) / 2.0,
				value[2],
				value[6].seq,
			),
		):
			compatible: List[
				Tuple[
					float,
					float,
					int,
					List[Tuple[float, float, float, float, int, int, Line]],
				]
			] = []
			for index, group in enumerate(interval_groups):
				left = max(item[0], max(value[0] for value in group))
				right = min(item[1], min(value[1] for value in group))
				if left <= right:
					group_center = (
						max(value[0] for value in group)
						+ min(value[1] for value in group)
					) / 2.0
					item_center = (item[0] + item[1]) / 2.0
					compatible.append(
						(abs(item_center - group_center), -(right - left), index, group)
					)
			if compatible:
				min(compatible, key=lambda value: value[:3])[3].append(item)
			else:
				interval_groups.append([item])

		groups: List[List[Tuple[float, float, float, int, int, Line]]] = []
		for interval_group in interval_groups:
			common_left = max(value[0] for value in interval_group)
			common_right = min(value[1] for value in interval_group)
			if common_left > common_right:
				continue
			separator = (common_left + common_right) / 2.0
			groups.append(
				[
					(
						separator,
						value[2],
						value[3],
						value[4],
						value[5],
						value[6],
					)
					for value in interval_group
				]
			)
		cohorts: List[List[Tuple[float, float, float, int, int, Line]]] = []
		for group in groups:
			ordered = sorted(group, key=lambda value: value[1])
			if not ordered:
				continue
			# Preserve the original compact detector whenever the complete gutter
			# cohort already forms one valid prose band.  Splitting a valid group at
			# a paragraph/figure gap can discard the only evidence that protects the
			# surrounding reading order.
			if self._is_compact_unruled_prose_group(
				page,
				ordered,
				width,
				height,
			):
				cohorts.append(ordered)
				continue
			body = median([value[5].size for value in ordered]) or 1.0
			# A large visual or full-width block can divide two independently valid
			# compact sections that reuse the same gutter.  Split only after the
			# whole cohort fails, and only at a gap far beyond paragraph rhythm.
			maximum_flow_gap = max(48.0, body * 4.5)
			current = [ordered[0]]
			for item in ordered[1:]:
				if item[1] - current[-1][1] > maximum_flow_gap:
					cohorts.append(current)
					current = []
				current.append(item)
			if current:
				cohorts.append(current)
		return [
			group
			for group in cohorts
			if len(group) >= 6
			and self._is_compact_unruled_prose_group(
				page,
				group,
				width,
				height,
			)
		]

	def _is_compact_unruled_prose_group(
		self,
		page: int,
		group: Sequence[Tuple[float, float, float, int, int, Line]],
		width: float,
		height: float,
	) -> bool:
		if len(group) < 6:
			return False
		body = median([value[5].size for value in group])
		if body <= 0:
			return False
		y0 = min(value[5].y0 for value in group)
		y1 = max(value[5].y1 for value in group)
		span = y1 - y0
		if span < max(height * 0.09, body * 6.0) or span > height * 0.34:
			return False
		separator = median([value[0] for value in group])
		if not width * 0.37 <= separator <= width * 0.63:
			return False
		if max(abs(value[0] - separator) for value in group) > 14.0:
			return False
		gaps = [value[2] for value in group]
		if not max(8.0, body * 0.72) <= median(gaps) <= width * 0.13:
			return False
		if median([value[3] + value[4] for value in group]) < 62:
			return False
		if sum(abs(value[5].size - body) <= body * 0.24 for value in group) < math.ceil(len(group) * 0.70):
			return False

		centers = sorted(value[1] for value in group)
		pitches = [
			current - previous
			for previous, current in zip(centers, centers[1:])
			if current > previous + 0.5
		]
		if not pitches:
			return False
		pitch = median(pitches)
		if not body * 0.75 <= pitch <= body * 3.2:
			return False
		if pitch * (len(centers) - 1) < (centers[-1] - centers[0]) * 0.52:
			return False

		prose_rows = 0
		side_texts: List[List[str]] = [[], []]
		has_explicit_caption_fragment = False
		for value in group:
			chars = sorted(value[5].chars, key=lambda char: (char.x0, char.seq))
			left_text = cleanup_spaces(
				"".join(
					char.text
					for char in chars
					if (char.x0 + char.x1) / 2.0 < separator
				)
			).strip()
			right_text = cleanup_spaces(
				"".join(
					char.text
					for char in chars
					if (char.x0 + char.x1) / 2.0 >= separator
				)
			).strip()
			left_words = [word for word in left_text.split() if any(char.isalpha() for char in word)]
			right_words = [word for word in right_text.split() if any(char.isalpha() for char in word)]
			side_texts[0].append(left_text)
			side_texts[1].append(right_text)
			has_explicit_caption_fragment = (
				has_explicit_caption_fragment
				or self._is_explicit_caption_label(left_text)
				or self._is_explicit_caption_label(right_text)
			)
			if (
				len(left_words) >= 4
				and len(right_words) >= 4
				and sum(char.isalpha() for char in left_text) >= 18
				and sum(char.isalpha() for char in right_text) >= 18
			):
				prose_rows += 1
		# A short caption sharing physical baselines with an adjacent prose
		# column does not establish one coherent two-column reading band.  The
		# compact detector cannot safely move surrounding body text across that
		# semantic boundary; longer/anchored column models still handle layouts
		# whose independent flow is corroborated beyond the caption.
		if has_explicit_caption_fragment:
			return False
		if prose_rows < math.ceil(len(group) * 0.68):
			return False

		# Repeated row templates are paired cards or record grids, not two
		# independent prose streams.  Their geometry can otherwise be identical
		# to a compact column band: every physical row exposes the same central
		# gutter and substantial text on both sides.  Require independent lexical
		# flow by vetoing the narrower case where both sides repeat the same
		# three-word opening on nearly every row.  Real wrapped prose varies at
		# line starts; card labels and row templates do not.
		def dominant_prefix_ratio(values: Sequence[str]) -> float:
			counts: Dict[Tuple[str, ...], int] = {}
			for value in values:
				words = [
					match.group(0).casefold()
					for match in re.finditer(
						r"[^\W\d_]+(?:[-'’][^\W\d_]+)*",
						value,
					)
				]
				if len(words) < 3:
					continue
				prefix = tuple(words[:3])
				counts[prefix] = counts.get(prefix, 0) + 1
			if not counts or not values:
				return 0.0
			return max(counts.values()) / len(values)

		if all(dominant_prefix_ratio(values) >= 0.75 for values in side_texts):
			return False

		segments = getattr(self.conv, "segments", ())
		horizontal_rules = [
			segment
			for segment in segments
			if segment.page == page
			and segment.horizontal
			and segment.length >= width * 0.40
			and y0 - body <= (segment.y0 + segment.y1) / 2.0 <= y1 + body
		]
		if len(horizontal_rules) >= 3:
			return False
		vertical_rules = [
			segment
			for segment in segments
			if segment.page == page
			and segment.vertical
			and segment.length >= span * 0.45
			and y0 - body <= min(segment.y0, segment.y1)
			and max(segment.y0, segment.y1) <= y1 + body
		]
		return len(vertical_rules) < 2

	def _is_anchored_medium_prose_column_band(
		self,
		page: int,
		group: Sequence[Tuple[float, float, float, int, int, Line]],
		lines: Sequence[Line],
		width: float,
		height: float,
	) -> bool:
		"""Admit a shorter prose gutter only below a strong visual/caption anchor.

		A large figure often leaves only the bottom quarter of a page available
		for two-column body text.  Span alone cannot distinguish that pattern
		from compact cards or alpha-heavy grids, so the reduced-span path also
		requires dense balanced prose, one exceptionally stable gutter, and a
		large centered visual followed by a small-type caption.  The existing
		long/disjoint-column paths remain unchanged.
		"""
		if len(group) < 8:
			return False
		y0 = min(value[5].y0 for value in group)
		y1 = max(value[5].y1 for value in group)
		span = y1 - y0
		if not height * 0.20 <= span < height * 0.30:
			return False
		body = median([value[5].size for value in group])
		if body <= 0:
			return False
		if sum(abs(value[5].size - body) <= body * 0.12 for value in group) < math.ceil(len(group) * 0.85):
			return False
		if median([value[3] for value in group]) < 32 or median([value[4] for value in group]) < 32:
			return False
		if median([value[2] for value in group]) > width * 0.10:
			return False

		centers = sorted(value[1] for value in group)
		pitches = [
			current - previous
			for previous, current in zip(centers, centers[1:])
			if current > previous + 0.5
		]
		if not pitches:
			return False
		pitch = median(pitches)
		if not body * 0.95 <= pitch <= body * 2.20:
			return False
		covered = pitch * (len(centers) - 1)
		if covered < (centers[-1] - centers[0]) * 0.62:
			return False

		separator = median([value[0] for value in group])
		prose_rows = 0
		for value in group:
			left_text = cleanup_spaces(
				"".join(
					char.text
					for char in sorted(value[5].chars, key=lambda char: (char.x0, char.seq))
					if (char.x0 + char.x1) / 2.0 < separator
				)
			).strip()
			right_text = cleanup_spaces(
				"".join(
					char.text
					for char in sorted(value[5].chars, key=lambda char: (char.x0, char.seq))
					if (char.x0 + char.x1) / 2.0 >= separator
				)
			).strip()
			left_words = [word for word in left_text.split() if any(char.isalpha() for char in word)]
			right_words = [word for word in right_text.split() if any(char.isalpha() for char in word)]
			if (
				len(left_words) >= 4
				and len(right_words) >= 4
				and sum(char.isalpha() for char in left_text) >= 18
				and sum(char.isalpha() for char in right_text) >= 18
			):
				prose_rows += 1
		if prose_rows < math.ceil(len(group) * 0.75):
			return False

		horizontal_rules = [
			segment
			for segment in self.conv.segments
			if segment.page == page
			and segment.horizontal
			and segment.length >= width * 0.45
			and y0 - body <= (segment.y0 + segment.y1) / 2.0 <= y1 + body
		]
		if len(horizontal_rules) >= 3:
			return False

		for image in self.conv.images:
			if image.page != page:
				continue
			image_width = image.x1 - image.x0
			image_height = image.y1 - image.y0
			if (
				image_width < width * 0.55
				or image_height < height * 0.16
				or image_width * image_height < width * height * 0.10
				or image.x0 > separator - width * 0.15
				or image.x1 < separator + width * 0.15
				or image.y1 > y0 + body
				or y0 - image.y1 > height * 0.16
			):
				continue
			caption_lines = [
				line
				for line in lines
				if image.y1 - 2.0 <= (line.y0 + line.y1) / 2.0 < y0 - 1.0
				and line.x1 >= image.x0 - body
				and line.x0 <= image.x1 + body
				and plain_text(line_text_tokens(line)).strip()
			]
			if not 1 <= len(caption_lines) <= 7:
				continue
			if median([line.size for line in caption_lines]) > body * 1.05:
				continue
			if sum(
				char.isalpha()
				for line in caption_lines
				for char in plain_text(line_text_tokens(line))
			) < 12:
				continue
			return True
		return False

	def _extend_medium_prose_column_end(
		self,
		lines: Sequence[Line],
		separator: float,
		y1: float,
		body: float,
		width: float,
		height: float,
	) -> float:
		"""Include adjacent column tails without swallowing a later section."""
		end = y1
		limit = min(height * 0.93, y1 + max(body * 4.0, height * 0.06))
		for line in sorted(lines, key=lambda candidate: (candidate.y0, candidate.x0, candidate.seq)):
			if line.y1 <= y1 + 0.5:
				continue
			if line.y0 > limit or line.y0 > end + body * 2.4:
				break
			if not plain_text(line_text_tokens(line)).strip():
				continue
			if line.size > body * 1.18:
				break
			if (
				line.x0 < separator - 12.0
				and line.x1 > separator + 12.0
				and line.x1 - line.x0 >= width * 0.65
			):
				chars = sorted(
					[char for char in line.chars if char.text and not char.text.isspace()],
					key=lambda char: (char.x0, char.seq),
				)
				if not any(
					left.x1 < separator < right.x0
					and right.x0 - left.x1 >= max(12.0, line.size * 1.20)
					for left, right in zip(chars, chars[1:])
				):
					break
			end = max(end, line.y1)
		return end

	def _extend_qualified_one_sided_prose_tail(
		self,
		page: int,
		lines: Sequence[Line],
		group: Sequence[Tuple[float, float, float, int, int, Line]],
		separator: float,
		y1: float,
		width: float,
		height: float,
	) -> float:
		"""Keep an asymmetric final column inside an already-proven prose band.

		The paired baselines that prove a two-column gutter end when the shorter
		column ends.  A longer column may legitimately continue alone.  Extend
		only when that tail begins at the next same-font baseline, remains inside
		one established column frame, contains substantial prose, and encounters
		no opposite-column text, full-width content, or native graphic boundary.
		"""
		if not group:
			return y1
		body = median([value[5].size for value in group])
		if body <= 0:
			return y1

		font_counts: Dict[Tuple[str, str], int] = {}
		side_chars: List[List[Char]] = [[], []]
		last_side_center = [-math.inf, -math.inf]
		group_line_ids = {id(value[5]) for value in group}
		max_group_center = max(value[1] for value in group)
		for value in group:
			for side in (0, 1):
				chars = [
					char
					for char in value[5].chars
					if char.text.strip()
					and (
						((char.x0 + char.x1) / 2.0 < separator)
						if side == 0
						else ((char.x0 + char.x1) / 2.0 >= separator)
					)
				]
				if not chars:
					continue
				side_chars[side].extend(chars)
				text = cleanup_spaces(
					"".join(
						char.text
						for char in sorted(chars, key=lambda char: (char.x0, char.seq))
					)
				).strip()
				if text and value[1] >= last_side_center[side]:
					last_side_center[side] = value[1]
				for char in chars:
					key = (char.font.name, char.font.base_font)
					font_counts[key] = font_counts.get(key, 0) + max(1, len(char.text.strip()))
		if not all(side_chars):
			return y1
		font_total = sum(font_counts.values())
		allowed_fonts = {
			key
			for key, count in font_counts.items()
			if count >= max(3, math.ceil(font_total * 0.015))
		}
		frames = [
			(
				min(char.x0 for char in chars),
				max(char.x1 for char in chars),
			)
			for chars in side_chars
		]

		def has_graphic_boundary(lower: float, upper: float, side: int) -> bool:
			frame_x0, frame_x1 = frames[side]
			for image in self.conv.images:
				if image.page != page:
					continue
				if image.y1 < lower - body or image.y0 > upper + body:
					continue
				if image.x1 >= frame_x0 - body and image.x0 <= frame_x1 + body:
					return True
			for fill in self.conv.fills:
				if fill.page != page:
					continue
				if fill.y1 < lower - body or fill.y0 > upper + body:
					continue
				if (
					fill.x1 >= frame_x0 - body
					and fill.x0 <= frame_x1 + body
					and fill.x1 - fill.x0 >= (frame_x1 - frame_x0) * 0.55
				):
					return True
			for segment in self.conv.segments:
				if segment.page != page:
					continue
				segment_y0, segment_y1 = sorted((segment.y0, segment.y1))
				if segment_y1 < lower - body or segment_y0 > upper + body:
					continue
				if segment.horizontal:
					segment_x0, segment_x1 = sorted((segment.x0, segment.x1))
					if (
						segment_x1 >= frame_x0 - body
						and segment_x0 <= frame_x1 + body
						and segment.length >= (frame_x1 - frame_x0) * 0.65
					):
						return True
				elif (
					segment.vertical
					and frame_x0 - body <= (segment.x0 + segment.x1) / 2.0 <= frame_x1 + body
					and segment.length >= body * 3.0
				):
					return True
			return False

		accepted: List[Line] = []
		tail_side: Optional[int] = None
		end = y1
		alpha_total = 0
		prose_lines = 0
		for line in sorted(lines, key=lambda candidate: (candidate.y0, candidate.x0, candidate.seq)):
			if id(line) in group_line_ids or line.writing_mode != "horizontal":
				continue
			center_y = (line.y0 + line.y1) / 2.0
			if center_y <= max_group_center + body * 0.35:
				continue
			if line.y0 > end + body * 1.35:
				break
			if not body * 0.88 <= line.size <= body * 1.12:
				break
			visible = [char for char in line.chars if char.text.strip()]
			if not visible:
				continue
			left = [char for char in visible if (char.x0 + char.x1) / 2.0 < separator]
			right = [char for char in visible if (char.x0 + char.x1) / 2.0 >= separator]
			left_alpha = sum(char.isalpha() for item in left for char in item.text)
			right_alpha = sum(char.isalpha() for item in right for char in item.text)
			occupied = [
				side
				for side, alpha in enumerate((left_alpha, right_alpha))
				if alpha >= 4
			]
			if len(occupied) != 1:
				break
			side = occupied[0]
			if tail_side is None:
				if center_y - last_side_center[side] > body * 1.65:
					return y1
				tail_side = side
			elif side != tail_side:
				break

			frame_x0, frame_x1 = frames[side]
			if (
				line.x0 < frame_x0 - body * 1.5
				or line.x1 > frame_x1 + body * 1.5
				or (side == 0 and line.x1 > separator - body * 0.35)
				or (side == 1 and line.x0 < separator + body * 0.35)
			):
				break
			visible_weight = sum(max(1, len(char.text.strip())) for char in visible)
			matching_weight = sum(
				max(1, len(char.text.strip()))
				for char in visible
				if (char.font.name, char.font.base_font) in allowed_fonts
			)
			if matching_weight < visible_weight * 0.80:
				break
			if has_graphic_boundary(end, line.y1, side):
				break

			text = plain_text(line_text_tokens(line)).strip()
			alpha = sum(char.isalpha() for char in text)
			words = [word for word in text.split() if any(char.isalpha() for char in word)]
			alpha_total += alpha
			prose_lines += int(len(words) >= 4 and alpha >= 18)
			accepted.append(line)
			end = max(end, line.y1)

		if len(accepted) < 5:
			return y1
		if end - accepted[0].y0 < max(body * 4.0, height * 0.06):
			return y1
		if alpha_total < 100 or prose_lines < math.ceil(len(accepted) * 0.55):
			return y1
		return end

	def _side_visual_column_start(
		self,
		page: int,
		separator: float,
		y0: float,
		group: Sequence[Tuple[float, float, float, int, int, Line]],
		lines: Sequence[Line],
		width: float,
		height: float,
	) -> Optional[float]:
		"""Extend a proven column band beside one large column-width visual."""
		body = median([value[5].size for value in group])
		for image in self.conv.images:
			if image.page != page:
				continue
			image_width = image.x1 - image.x0
			image_height = image.y1 - image.y0
			if (
				image_width < width * 0.25
				or image_width > width * 0.48
				or image_height < height * 0.20
				or image_width * image_height < width * height * 0.08
				or image.y0 >= y0
				or image.y1 > y0 + body * 2.0
				or y0 - image.y1 > body * 3.0
			):
				continue
			if image.x1 <= separator + width * 0.025 and image.x0 <= separator - width * 0.20:
				image_side = 0
			elif image.x0 >= separator - width * 0.025 and image.x1 >= separator + width * 0.20:
				image_side = 1
			else:
				continue

			opposite_lines: List[Line] = []
			for line in lines:
				center_y = (line.y0 + line.y1) / 2.0
				if not image.y0 - body <= center_y < y0:
					continue
				if not body * 0.80 <= line.size <= body * 1.20:
					continue
				left_text = cleanup_spaces(
					"".join(
						char.text
						for char in line.chars
						if (char.x0 + char.x1) / 2.0 < separator
					)
				).strip()
				right_text = cleanup_spaces(
					"".join(
						char.text
						for char in line.chars
						if (char.x0 + char.x1) / 2.0 >= separator
					)
				).strip()
				same_text, opposite_text = (
					(left_text, right_text)
					if image_side == 0
					else (right_text, left_text)
				)
				if (
					sum(char.isalpha() for char in opposite_text) >= 20
					and sum(char.isalpha() for char in same_text) <= 3
					and len(
						[
							word
							for word in opposite_text.split()
							if any(char.isalpha() for char in word)
						]
					) >= 4
				):
					opposite_lines.append(line)
			if len(opposite_lines) < 8:
				continue
			if (
				max(line.y1 for line in opposite_lines)
				- min(line.y0 for line in opposite_lines)
				< height * 0.20
			):
				continue

			caption_side_text = 0
			for line in lines:
				center_y = (line.y0 + line.y1) / 2.0
				if not image.y1 - body <= center_y <= y0 + body * 4.0:
					continue
				caption_side_text += sum(
					text_char.isalpha()
					for char in line.chars
					for text_char in char.text
					if (
						((char.x0 + char.x1) / 2.0 < separator)
						if image_side == 0
						else ((char.x0 + char.x1) / 2.0 >= separator)
					)
				)
			if caption_side_text < 12:
				continue
			return min(
				image.y0,
				min(line.y0 for line in opposite_lines),
			)
		return None

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
		for y, callout_md, callout_lines, callout_attrs in self._callouts(page):
			kind = "equation" if "<math" in callout_md else "callout"
			blocks.append(self._event(
				page,
				self._rank_for_y(lines, y) - 0.2,
				kind,
				callout_md,
				callout_lines,
				callout_attrs,
			))
			consumed.update(id(l) for l in callout_lines)
		for y, table_md, table_lines, table_box in self._table_candidates(page):
			if self._box_is_inside_vector_artwork(page, table_box):
				continue
			table_attrs: Dict[str, Any] = {"bbox": table_box}
			partial_grid = self._partial_table_models.get((page, table_box))
			if partial_grid is not None:
				table_attrs["partial_grid"] = partial_grid
			blocks.append(self._event(page, self._rank_for_y(lines, y) - 0.1, "table", table_md, table_lines, table_attrs))
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
					if not img.alt or img.kind == "vector":
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
				heading_attrs: Dict[str, Any] = {"level": level}
				if any(
					line_item.chars
					and all(char.artifact for char in line_item.chars if char.text)
					for line_item in heading_lines
				):
					heading_attrs.update({
						"artifact_text_recovered": True,
						"source_marked_artifact": True,
						"artifact_recovery_reasons": [
							"numbered_display_title",
							"display_scale_above_body",
							"paired_title_rules",
							"adjacent_authored_body",
							"document_position_unique",
							"outside_raster_figure",
						],
					})
				blocks.append(self._event(page, rank, "heading", "%s %s" % ("#" * level, strip_wrapping_styles(heading_text)), heading_lines, heading_attrs))
				i = j
				continue
			if (
				self._is_quote_line(line, page)
				and not self._is_decorative_accent_eyebrow(
					lines,
					i,
					page,
					body_size,
				)
			):
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
					if code_items:
						previous_panel = self._tiled_code_panel_for_line(lines[i - 1])
						current_panel = self._tiled_code_panel_for_line(lines[i])
						if (
							previous_panel is not None
							and current_panel is not None
							and previous_panel["bbox"] != current_panel["bbox"]
						):
							break
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
				code_source_lines = [item for item in code_items if item is not None]
				panel = next(
					(
						self._tiled_code_panel_for_line(item)
						for item in code_source_lines
						if self._tiled_code_panel_for_line(item) is not None
					),
					None,
				)
				code_attrs = {"code": body.rstrip("\n")}
				code_attrs.update(self._tiled_code_event_attrs(panel))
				if panel is not None and panel["style_outlier_count"]:
					self.conv.doc.warn(
						"CODE_PANEL_STYLE_OUTLIER_RECOVERED",
						"one style outlier retained inside a paint-order-verified code panel",
						page,
					)
				blocks.append(self._event(
					page,
					rank,
					"code_block",
					fence + "\n" + body.rstrip("\n") + "\n" + fence,
					code_source_lines,
					code_attrs,
				))
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
			# A visible caption already supplies the image's equivalent text in
			# Markdown.  Repeating the same string as alt text makes screen
			# readers announce it twice and duplicates the source projection.
			# The independent semantic HTML projection still retains the
			# original image alt and its separate <figcaption>.
			markdown_alt = alt
			if (
				caption
				and cleanup_spaces(unicodedata.normalize("NFKC", alt)).casefold()
				== cleanup_spaces(unicodedata.normalize("NFKC", caption)).casefold()
			):
				markdown_alt = ""
			markup = "![%s](%s)" % (escape_md(markdown_alt), src)
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
		cacheable = self._frozen_line_position(line) is not None
		line_id = id(line)
		if cacheable and line_id in self._tagged_heading_level_cache:
			return self._tagged_heading_level_cache[line_id]
		levels: set[int] = set()
		for char in line.chars:
			for mark in char.mc:
				tag = str(mark.get("tag") or "").lstrip("/")
				match = _TAGGED_HEADING_NAME.fullmatch(tag)
				if match:
					levels.add(int(match.group(1)))
		level = next(iter(levels)) if len(levels) == 1 else None
		if cacheable:
			self._tagged_heading_level_cache[line_id] = level
		return level

	def _tri_fold_band_and_panel(
		self,
		line: Optional[Line],
	) -> Optional[Tuple[Dict[str, Any], int]]:
		if line is None:
			return None
		visible = [char for char in line.chars if char.text.strip()]
		if not visible:
			return None
		center_x = median([(char.x0 + char.x1) / 2.0 for char in visible])
		center_y = median([(char.y0 + char.y1) / 2.0 for char in visible])
		for band in self._inferred_panel_bands.get(line.page, []):
			if band.get("kind") != "tri_fold":
				continue
			if not (
				band["x0"] - 2.0 <= center_x <= band["x1"] + 2.0
				and band["y0"] - 2.0 <= center_y <= band["y1"] + 2.0
			):
				continue
			return band, sum(center_x >= separator for separator in band["separators"])
		return None

	def _is_tri_fold_attribution(self, line: Line, body_size: float, text: str) -> bool:
		if self._tri_fold_band_and_panel(line) is None:
			return False
		if line.size > body_size * 1.15 or len(text) > 70 or len(text.split()) > 9:
			return False
		if re.search(r"\b(?:18|19|20|21)\d{2}\b", text) is None:
			return False
		return re.search(
			r"(?:\u00a9|copyright|copyleft|creative\s+commons|public\s+domain|"
			r"\bcc\s*(?:0|by(?:[-\s](?:sa|nc|nd))*)\b)",
			text,
			re.I,
		) is not None

	def _is_tri_fold_colon_heading(
		self,
		line: Line,
		body_size: float,
		text: str,
	) -> bool:
		return (
			self._tri_fold_band_and_panel(line) is not None
			and line.bold_ratio >= 0.75
			and body_size * 0.84 <= line.size <= body_size * 1.40
			and text.endswith(":")
			and 2 <= len(text.split()) <= 10
			and len(text) <= 80
			and any(character.isalpha() for character in text)
			and next((character for character in text if character.isalpha()), "").isupper()
			and list_marker(text) is None
			and not self._is_explicit_caption_label(text)
			and not self._is_toc_navigation_row(line, text)
		)

	def _is_tri_fold_ampersand_bridge(
		self,
		line: Line,
		body_size: float,
		prev: Optional[Line],
		nxt: Optional[Line],
		text: str,
	) -> bool:
		owner = self._tri_fold_band_and_panel(line)
		if owner is None or text.strip() != "&" or prev is None or nxt is None:
			return False
		for neighbor in (prev, nxt):
			neighbor_owner = self._tri_fold_band_and_panel(neighbor)
			neighbor_text = plain_text(line_text_tokens(neighbor)).strip()
			if (
				neighbor_owner is None
				or neighbor_owner[0] is not owner[0]
				or neighbor_owner[1] != owner[1]
				or neighbor.size < body_size * 1.35
				or abs(neighbor.size - line.size) > max(1.0, line.size * 0.12)
				or not 2 <= len(neighbor_text.split()) <= 9
				or neighbor_text.upper() != neighbor_text
			):
				return False
		upper_gap = line_flow_gap(prev, line)
		lower_gap = line_flow_gap(line, nxt)
		return (
			0 < upper_gap <= max(18.0, line.size * 1.55)
			and 0 < lower_gap <= max(18.0, line.size * 1.55)
		)

	def _is_tri_fold_heading_continuation(self, upper: Line, lower: Line) -> bool:
		upper_owner = self._tri_fold_band_and_panel(upper)
		lower_owner = self._tri_fold_band_and_panel(lower)
		if (
			upper_owner is None
			or lower_owner is None
			or upper_owner[0] is not lower_owner[0]
			or upper_owner[1] != lower_owner[1]
		):
			return False
		upper_text = plain_text(line_text_tokens(upper)).strip()
		lower_text = plain_text(line_text_tokens(lower)).strip()
		if not upper_text or not lower_text:
			return False
		if upper_text == "&" or lower_text == "&":
			other = lower if upper_text == "&" else upper
			other_text = lower_text if upper_text == "&" else upper_text
			return (
				other_text.upper() == other_text
				and other.size >= self._body_font_size(self.lines_by_page.get(other.page, [])) * 1.35
				and abs(upper.size - lower.size) <= max(1.0, other.size * 0.12)
				and 0 < line_flow_gap(upper, lower) <= max(18.0, other.size * 1.55)
			)
		if (
			upper_text.upper() != upper_text
			or lower_text.upper() != lower_text
			or sum(character.isalpha() for character in upper_text) < 3
			or sum(character.isalpha() for character in lower_text) < 3
			or len((upper_text + " " + lower_text).split()) > 12
		):
			return False
		upper_center = (upper.x0 + upper.x1) / 2.0
		lower_center = (lower.x0 + lower.x1) / 2.0
		if abs(upper_center - lower_center) > max(12.0, min(upper.size, lower.size) * 0.75):
			return False
		ratio = min(upper.size, lower.size) / max(upper.size, lower.size)
		if ratio < 0.65:
			return False
		gap = line_flow_gap(upper, lower)
		return 0 < gap <= min(upper.size, lower.size) * 1.42

	def _is_filled_sidebar_heading(
		self,
		line: Line,
		body_size: float,
		prev: Optional[Line],
		nxt: Optional[Line],
		text: str,
	) -> bool:
		"""Admit a wrapped, bold label inside a verified painted sidebar."""
		if (
			line.writing_mode != "horizontal"
			or line.bold_ratio < 0.75
			or len(text) > 90
			or not 1 <= len(text.split()) <= 10
			or sum(character.isalpha() for character in text) < 4
			or text[-1:] in ".!?;:"
			or list_marker(text) is not None
			or self._is_explicit_caption_label(text)
			or self._is_toc_navigation_row(line, text)
			or re.fullmatch(r"[A-Z0-9]+(?:[-_][A-Z0-9]+)+", text) is not None
			or self._has_adjacent_ordered_peer(line, text)
		):
			return False

		visible = [char for char in line.chars if char.text.strip()]
		if not visible:
			return False
		x0 = min(char.x0 for char in visible)
		x1 = max(char.x1 for char in visible)
		center_y = median([(char.y0 + char.y1) / 2.0 for char in visible])

		def inside(candidate: Line, band: Dict[str, Any]) -> bool:
			candidate_visible = [char for char in candidate.chars if char.text.strip()]
			if not candidate_visible:
				return False
			candidate_x0 = min(char.x0 for char in candidate_visible)
			candidate_x1 = max(char.x1 for char in candidate_visible)
			candidate_y = median(
				[(char.y0 + char.y1) / 2.0 for char in candidate_visible]
			)
			return (
				candidate.page == line.page
				and candidate_x0 >= band["x0"] - 2.0
				and candidate_x1 <= band["x1"] + 2.0
				and band["y0"] - 2.0 <= candidate_y <= band["fill_y1"] + 2.0
			)

		band = next(
			(
				candidate
				for candidate in self._filled_sidebar_bands.get(line.page, [])
				if x0 >= candidate["x0"] - 2.0
				and x1 <= candidate["x1"] + 2.0
				and candidate["y0"] - 2.0 <= center_y <= candidate["fill_y1"] + 2.0
			),
			None,
		)
		if band is None:
			return False
		sidebar_body = float(band.get("sidebar_body_size") or body_size)
		if not (
			line.size >= body_size * 0.65
			and sidebar_body * 0.80 <= line.size <= sidebar_body * 1.30
		):
			return False

		for peer in (prev, nxt):
			if peer is None or not inside(peer, band) or peer.bold_ratio < 0.75:
				continue
			peer_text = plain_text(line_text_tokens(peer)).strip()
			if (
				not peer_text
				or len(peer_text) > 90
				or not 1 <= len(peer_text.split()) <= 10
				or peer_text[-1:] in ".!?;:"
				or list_marker(peer_text) is not None
				or self._is_explicit_caption_label(peer_text)
				or self._is_toc_navigation_row(peer, peer_text)
				or abs(peer.size - line.size) > max(0.65, line.size * 0.08)
				or abs(peer.x0 - line.x0) > max(6.0, line.size * 0.65)
			):
				continue
			upper, lower = (
				(peer, line)
				if (peer.y0, peer.seq) < (line.y0, line.seq)
				else (line, peer)
			)
			gap = line_flow_gap(upper, lower)
			if 0 < gap <= max(line.size * 1.75, 20.0):
				ordered_text = (
					(peer_text, text)
					if (peer.y0, peer.seq) < (line.y0, line.seq)
					else (text, peer_text)
				)
				combined = cleanup_spaces("%s %s" % ordered_text)
				first_alpha = next(
					(character for character in combined if character.isalpha()),
					"",
				)
				if first_alpha.isupper() and len(combined.split()) <= 14:
					return True
		return False

	def _is_heading(self, line: Line, body_size: float, prev: Optional[Line], nxt: Optional[Line]) -> bool:
		if self._tagged_heading_level(line) is not None:
			return True
		panel_context = self._panel_line_context(line)
		if panel_context is not None:
			if panel_context["role"] == "label":
				return True
			if panel_context["role"] == "payload":
				return False
		text = plain_text(line_text_tokens(line)).strip()
		if not text or len(text) > 140:
			return False
		if self._is_explicit_caption_label(text):
			return False
		if self._is_toc_navigation_row(line, text):
			return False
		if (
			re.fullmatch(r"(?:table\s+of\s+)?contents", text, re.I)
			and (line.bold_ratio >= 0.50 or line.size >= body_size * 1.15)
		):
			# Contents titles frequently share the row typography used by the
			# generated entries below them.  Their explicit semantic label and
			# independent first-row position are stronger evidence than the
			# tight/blank baseline that otherwise suppresses a body-sized title.
			return True
		if re.fullmatch(r"[A-Z0-9]+(?:[-_][A-Z0-9]+)+", text):
			# Standalone machine identifiers are safer as emphasized prose than
			# invented document-outline entries when tags provide no heading role.
			return False
		if self._is_tri_fold_attribution(line, body_size, text):
			return False
		if self._is_tri_fold_colon_heading(line, body_size, text):
			return True
		if self._is_tri_fold_ampersand_bridge(line, body_size, prev, nxt, text):
			return True
		if self._is_filled_sidebar_heading(line, body_size, prev, nxt, text):
			return True
		if line.size < body_size * 0.88:
			return False
		if self._has_adjacent_ordered_peer(line, text):
			# Consecutive, same-style ordered labels are list items (or an
			# unlinked contents sequence), not independent outline headings.
			# Apply this before the large/bold fast path as producers often
			# render such navigation rows in display typography.
			return False
		if prev is None and nxt is None and line.size < body_size * 1.18:
			return False
		if line.size >= body_size * 1.18 and line.bold_ratio >= 0.25:
			return True
		if self._is_regular_display_heading(line, body_size):
			return True
		if self._is_page_leading_regular_heading(line, body_size, text):
			return True
		if self._is_isolated_regular_heading(line, body_size, prev, nxt, text):
			return True
		if self._is_numbered_section_heading_run(line, body_size):
			return True
		if self._is_compact_section_heading(line, body_size, text):
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

	def _is_explicit_caption_label(self, text: str) -> bool:
		"""Return true for an explicit numbered Figure/Fig./Table caption.

		The designator is intentionally required to be numeric (optionally
		prefixed by a supplement letter) or a standalone Roman numeral.  This
		keeps ordinary headings such as ``Table of Contents`` and ``Figure
		Skating`` outside the caption class while covering common scholarly and
		office-producer caption forms.
		"""
		if re.fullmatch(r"table\s+of\s+contents", text.strip(), re.I):
			return False
		return re.match(
			r"^(?:fig(?:ure)?\.?|table)\s+"
			r"(?:[A-Z]?\d+(?:[.\-]\d+)*|[IVXLCDM]+)\b",
			text,
			re.I,
		) is not None

	def _is_toc_navigation_row(self, line: Line, text: str) -> bool:
		"""Reject generated contents rows without rejecting their section title."""
		if re.fullmatch(r"(?:table\s+of\s+)?contents", text.strip(), re.I):
			return False
		page_suffix = re.search(r"(?:^|\s)(?:\d{1,4}|[ivxlcdm]{1,8})\s*$", text, re.I)
		dot_leader = re.search(r"(?:\.{3,}|…{2,}|(?:\.\s*){4,})", text) is not None
		visible = [char for char in ordered_line_chars(line) if char.text.strip()]
		linked_ratio = (
			sum(1 for char in visible if char.link) / len(visible)
			if visible
			else 0.0
		)
		if dot_leader and (page_suffix is not None or linked_ratio >= 0.50):
			return True
		if linked_ratio >= 0.50 and page_suffix is not None:
			return True
		if page_suffix is None or len(visible) < 2:
			return False
		horizontal_gaps = [
			right.x0 - left.x1
			for left, right in zip(visible, visible[1:])
			if right.x0 >= left.x0
		]
		return bool(horizontal_gaps and max(horizontal_gaps) > max(line.size * 2.0, 12.0))

	def _display_wrap_peer(self, upper: Line, lower: Line) -> bool:
		"""Return whether two physical lines form one display-text wrap.

		Left alignment covers ordinary title wraps; equal centers cover
		producers that center every physical line independently.  Typography,
		flow, and semantic guards must all agree before geometry can join them.
		"""
		if upper.page != lower.page or upper.writing_mode != lower.writing_mode:
			return False
		if upper.writing_mode != "horizontal":
			return False
		if self._tagged_heading_level(upper) is not None or self._tagged_heading_level(lower) is not None:
			return False
		upper_text = plain_text(line_text_tokens(upper)).strip()
		lower_text = plain_text(line_text_tokens(lower)).strip()
		if not upper_text or not lower_text:
			return False
		if any(
			self._is_explicit_caption_label(text)
			or self._is_toc_navigation_row(candidate, text)
			or list_marker(text) is not None
			or re.fullmatch(r"[A-Z0-9]+(?:[-_][A-Z0-9]+)+", text) is not None
			for candidate, text in ((upper, upper_text), (lower, lower_text))
		):
			return False
		if abs(upper.size - lower.size) > max(0.75, max(upper.size, lower.size) * 0.05):
			return False
		if abs(upper.bold_ratio - lower.bold_ratio) > 0.25:
			return False
		gap = line_flow_gap(upper, lower)
		if gap <= 0 or gap > max(upper.size * 1.55, 24.0):
			return False
		left_aligned = abs(upper.x0 - lower.x0) <= max(8.0, upper.size * 0.55)
		upper_center = (upper.x0 + upper.x1) / 2.0
		lower_center = (lower.x0 + lower.x1) / 2.0
		center_aligned = abs(upper_center - lower_center) <= max(7.0, upper.size * 0.40)
		return left_aligned or center_aligned

	def _display_wrap_run(self, line: Line) -> Tuple[List[Line], int, int]:
		page_lines = self.lines_by_page.get(line.page, [])
		position = self._frozen_line_position(line)
		if position is not None:
			cached = self._display_wrap_run_cache.get(id(line))
			if cached is not None:
				run, start, end = cached
				return list(run), start, end
			index = position[1]
		else:
			index = next(
				(i for i, candidate in enumerate(page_lines) if candidate is line),
				-1,
			)
		if index < 0:
			return [line], -1, -1
		start = index
		while start > 0 and self._display_wrap_peer(page_lines[start - 1], page_lines[start]):
			start -= 1
		end = index + 1
		while end < len(page_lines) and self._display_wrap_peer(page_lines[end - 1], page_lines[end]):
			end += 1
		run = tuple(page_lines[start:end])
		if position is not None:
			entry = (run, start, end)
			for member in run:
				if self._frozen_line_position(member) is not None:
					self._display_wrap_run_cache[id(member)] = entry
		return list(run), start, end

	def _is_regular_display_heading(self, line: Line, body_size: float) -> bool:
		"""Recognize an isolated regular-weight display title or section label.

		Size contrast alone is intentionally insufficient.  A candidate must
		also form a coherent physical-line run, have whitespace around the
		whole run, and—for medium display sizes—either be centered in the text
		frame or introduce a smaller same-margin content block.  This recovers
		centered book headings and slide/panel titles without promoting enlarged
		prose, captions, navigation rows, or table/list sequences.
		"""
		run, start, end = self._display_wrap_run(line)
		if start < 0:
			return False
		texts = [plain_text(line_text_tokens(candidate)).strip() for candidate in run]
		if not all(texts):
			return False
		combined = cleanup_spaces(" ".join(texts))
		if not combined or len(combined) > 200:
			return False
		if any(
			self._is_explicit_caption_label(text)
			or self._is_toc_navigation_row(candidate, text)
			or list_marker(text) is not None
			for candidate, text in zip(run, texts)
		):
			return False
		visible = [char for candidate in run for char in candidate.chars if char.text.strip()]
		if visible and sum(1 for char in visible if char.link) / len(visible) >= 0.50:
			return False

		page_lines = self.lines_by_page.get(line.page, [])
		previous = next(
			(
				candidate
				for candidate in reversed(page_lines[:start])
				if plain_text(line_text_tokens(candidate)).strip()
			),
			None,
		)
		following = next(
			(
				candidate
				for candidate in page_lines[end:]
				if plain_text(line_text_tokens(candidate)).strip()
			),
			None,
		)
		display_size = median([candidate.size for candidate in run])
		word_count = len(combined.split())

		# A sparse title page can make the title itself the modal "body" size.
		# Admit only a multi-line, centered, bold display run that dominates all
		# remaining page text; short logos and taglines intentionally fail.
		outside = [
			candidate
			for position, candidate in enumerate(page_lines)
			if not (start <= position < end)
			and plain_text(line_text_tokens(candidate)).strip()
		]
		page_width, _page_height = self.conv.page_sizes.get(line.page, (612.0, 792.0))
		dominant_title_run = (
			len(run) >= 2
			and display_size >= 18.0
			and 4 <= word_count <= 24
			and all(candidate.bold_ratio >= 0.75 for candidate in run)
			and all(
				abs(((candidate.x0 + candidate.x1) / 2.0) - page_width / 2.0)
				<= page_width * 0.06
				for candidate in run
			)
			and bool(outside)
			and max(candidate.size for candidate in outside) <= display_size * 0.72
		)
		if dominant_title_run:
			gap_after = line_flow_gap(run[-1], following) if following is not None else float("inf")
			return gap_after > display_size * 1.50

		if any(candidate.bold_ratio >= 0.25 for candidate in run):
			return False
		ratio = display_size / max(body_size, 1.0)
		if ratio < 1.18:
			return False
		if ratio < 1.45 and word_count > 14:
			return False
		if ratio >= 1.45 and word_count > 28:
			return False
		if ratio < 1.45 and following is None:
			# A medium-size section label must govern a following content block.
			# This prevents the final ordinary line on a sparse page from being
			# promoted merely because a smaller label skewed the body-size mode.
			return False
		if combined[-1:] in ".!?" and word_count >= 12:
			return False

		raw_gap_above = (
			line_flow_gap(previous, run[0])
			if previous is not None
			else float("inf")
		)
		raw_gap_below = (
			line_flow_gap(run[-1], following)
			if following is not None
			else float("inf")
		)
		# A negative flow gap marks the beginning of the next ordered column or
		# panel.  It is a real block boundary, not negative whitespace.
		gap_above = float("inf") if raw_gap_above <= 0 else raw_gap_above
		gap_below = float("inf") if raw_gap_below <= 0 else raw_gap_below

		previous_text = (
			plain_text(line_text_tokens(previous)).strip()
			if previous is not None
			else ""
		)
		if (
			previous is not None
			and 0 < raw_gap_above <= max(display_size * 2.0, body_size * 3.0)
			and previous.size <= display_size * 0.75
			and len(previous_text) <= 80
			and len(previous_text.split()) <= 8
			and not self._is_explicit_caption_label(previous_text)
			and not self._is_toc_navigation_row(previous, previous_text)
		):
			# A short smaller eyebrow belongs to the display group but is not an
			# independent outline node, and must not suppress the main title.
			gap_above = float("inf")

		if (
			following is not None
			and following.size >= display_size * 1.25
			and 0 < raw_gap_below <= max(body_size * 3.0, following.size * 2.0)
		):
			# Conversely, this run is the eyebrow for a larger title.
			return False

		frame_left, frame_right = self._text_frame(line.page)
		frame_center = (frame_left + frame_right) / 2.0
		center_tolerance = max(display_size * 0.65, (frame_right - frame_left) * 0.04)
		centered = all(
			abs(((candidate.x0 + candidate.x1) / 2.0) - frame_center) <= center_tolerance
			for candidate in run
		)

		following_is_body = (
			following is not None
			and following.size <= display_size * 0.90
			and abs(following.x0 - run[0].x0) <= max(9.0, display_size * 0.75)
		)
		previous_is_larger_label = (
			previous is not None
			and previous.size >= display_size * 1.25
			and abs(previous.x0 - run[0].x0) <= max(9.0, display_size * 0.75)
		)
		strong_block_start = (
			previous is None
			or raw_gap_above <= 0
			or raw_gap_above >= max(body_size * 1.75, display_size * 1.35)
			or previous_is_larger_label
		)
		strict_gap = max(body_size * 1.35, display_size * 1.40)
		strictly_isolated = gap_above > strict_gap and gap_below > strict_gap
		if ratio >= 1.45:
			return strictly_isolated
		if centered:
			return strictly_isolated
		local_gap = max(body_size * 1.35, display_size * 1.10)
		return (
			following_is_body
			and strong_block_start
			and gap_above > local_gap
			and gap_below > local_gap
		)

	def _is_page_leading_regular_heading(
		self,
		line: Line,
		body_size: float,
		text: str,
	) -> bool:
		"""Recognize a body-sized page lead that governs wrapped prose.

		Some editorial layouts use whitespace alone for a short section title.
		That evidence is normally too weak, so this path is limited to the first
		meaningful line near the page top and requires two same-margin,
		regular-weight lines that form a substantially wider prose paragraph.
		Chart labels, captions, linked navigation rows, and sparse logos do not
		supply that continuous prose-block evidence.
		"""
		if not (body_size * 0.95 <= line.size <= body_size * 1.06):
			return False
		if line.bold_ratio >= 0.25 or line.writing_mode != "horizontal":
			return False
		if len(text) > 64 or not (2 <= len(text.split()) <= 8):
			return False
		if text[-1:] in ".!?:;" or not any(char.isalpha() for char in text):
			return False
		visible = [char for char in line.chars if char.text.strip()]
		if visible and sum(1 for char in visible if char.link) / len(visible) >= 0.50:
			return False

		page_lines = self.lines_by_page.get(line.page, [])
		index = next(
			(i for i, candidate in enumerate(page_lines) if candidate is line),
			-1,
		)
		if index < 0 or any(
			plain_text(line_text_tokens(candidate)).strip()
			for candidate in page_lines[:index]
		):
			return False
		page_width, page_height = self.conv.page_sizes.get(line.page, (612.0, 792.0))
		if line.y0 > page_height * 0.20:
			return False

		following = [
			candidate
			for candidate in page_lines[index + 1:]
			if plain_text(line_text_tokens(candidate)).strip()
		]
		if len(following) < 2:
			return False
		first, second = following[:2]
		first_text = plain_text(line_text_tokens(first)).strip()
		second_text = plain_text(line_text_tokens(second)).strip()
		if any(
			candidate.writing_mode != "horizontal"
			or candidate.bold_ratio >= 0.25
			or abs(candidate.size - body_size) > max(0.65, body_size * 0.06)
			or abs(candidate.x0 - line.x0) > max(6.0, body_size * 0.55)
			for candidate in (first, second)
		):
			return False
		lead_gap = line_flow_gap(line, first)
		prose_gap = line_flow_gap(first, second)
		if not (
			body_size * 2.10 < lead_gap <= body_size * 4.0
			and body_size * 0.90 <= prose_gap <= body_size * 1.75
		):
			return False
		if len(first_text.split()) < 10 or len(second_text.split()) < 6:
			return False
		if not any(char.islower() for char in first_text + second_text):
			return False
		if first.x1 - first.x0 < page_width * 0.60:
			return False
		if first.x1 - first.x0 < (line.x1 - line.x0) * 2.5:
			return False
		frame_left, frame_right = self._text_frame(line.page)
		return (
			abs(line.x0 - frame_left) <= max(8.0, body_size * 0.70)
			and first.x1 >= frame_right - max(10.0, body_size)
		)

	def _is_isolated_regular_heading(
		self,
		line: Line,
		body_size: float,
		prev: Optional[Line],
		nxt: Optional[Line],
		text: str,
	) -> bool:
		"""Admit a strongly isolated display-size heading without bold weight.

		Many PDF producers distinguish titles only through size and whitespace.
		The deliberately high size ratio and bilateral isolation keep enlarged
		prose, figure/table captions, and linked table-of-contents entries from
		becoming invented outline nodes.
		"""
		if line.bold_ratio >= 0.25 or line.size < body_size * 1.45:
			return False
		if self._is_explicit_caption_label(text):
			return False
		visible = [char for char in line.chars if char.text.strip()]
		if visible and sum(1 for char in visible if char.link) / len(visible) >= 0.50:
			return False
		if text[-1:] in ".!?" and len(text.split()) >= 12:
			return False

		meaningful_prev = (
			prev
			if prev is not None and plain_text(line_text_tokens(prev)).strip()
			else None
		)
		meaningful_next = (
			nxt
			if nxt is not None and plain_text(line_text_tokens(nxt)).strip()
			else None
		)
		gap_above = line_flow_gap(meaningful_prev, line) if meaningful_prev is not None else float("inf")
		gap_below = line_flow_gap(line, meaningful_next) if meaningful_next is not None else float("inf")
		minimum_gap = max(body_size * 1.75, line.size * 1.45)
		if gap_above <= minimum_gap or gap_below <= minimum_gap:
			return False
		if (
			meaningful_next is not None
			and meaningful_next.size >= line.size * 1.25
			and gap_below <= max(body_size * 3.0, meaningful_next.size * 2.0)
		):
			# A smaller display line immediately introducing a larger title is
			# a kicker/eyebrow, not an independent heading.
			return False
		return True

	def _is_numbered_section_heading_run(
		self,
		line: Line,
		body_size: float,
	) -> bool:
		"""Recognize an isolated bold numbered section, including one wrap.

		Book and report producers often keep a section heading at body size and
		distinguish it only with bold weight. A physical wrap can make the first
		line look too close to its continuation, while a column transition can
		make the preceding flow gap meaningless. Require the complete display run
		to start with a section number, govern ordinary same-margin prose, and be
		either vertically isolated or at an unambiguous reading-band transition.
		"""
		run, start, end = self._display_wrap_run(line)
		if start < 0 or not 1 <= len(run) <= 2:
			return False
		texts = [plain_text(line_text_tokens(candidate)).strip() for candidate in run]
		if not all(texts):
			return False
		combined = cleanup_spaces(" ".join(texts))
		if not (
			re.match(r"^\d{1,3}(?:\.\d+){0,5}\.?\s+[A-Z]", combined)
			and 4 <= len(combined.split()) <= 18
			and len(combined) <= 120
			and all(candidate.bold_ratio >= 0.75 for candidate in run)
			and all(abs(candidate.size - body_size) <= max(1.0, body_size * 0.14) for candidate in run)
		):
			return False

		page_lines = self.lines_by_page.get(line.page, [])
		previous = next(
			(
				candidate
				for candidate in reversed(page_lines[:start])
				if plain_text(line_text_tokens(candidate)).strip()
			),
			None,
		)
		following = next(
			(
				candidate
				for candidate in page_lines[end:]
				if plain_text(line_text_tokens(candidate)).strip()
			),
			None,
		)
		if following is None:
			return False
		following_text = plain_text(line_text_tokens(following)).strip()
		if (
			list_marker(following_text) is not None
			or following.bold_ratio >= 0.65
			or abs(following.size - body_size) > max(0.75, body_size * 0.08)
			or abs(following.x0 - run[0].x0) > max(8.0, body_size * 0.75)
			or len(following_text.split()) < 5
			or not any(char.islower() for char in following_text)
		):
			return False
		gap_below = line_flow_gap(run[-1], following)
		if gap_below <= body_size * 1.35:
			return False
		if previous is None:
			return True
		previous_text = plain_text(line_text_tokens(previous)).strip()
		if (
			list_marker(previous_text) is not None
			and abs(previous.x0 - run[0].x0) <= max(8.0, body_size * 0.75)
			and abs(previous.size - run[0].size) <= 1.0
		):
			return False
		gap_above = line_flow_gap(previous, run[0])
		if gap_above > body_size * 1.35:
			return True
		page_width, _page_height = self.conv.page_sizes.get(line.page, (612.0, 792.0))
		return (
			abs(previous.x0 - run[0].x0) >= max(80.0, page_width * 0.20)
			and abs(following.x0 - run[0].x0) <= max(8.0, body_size * 0.75)
		)

	def _is_compact_section_heading(
		self,
		line: Line,
		body_size: float,
		text: str,
	) -> bool:
		"""Recognize compact section labels that use weight/case, not size.

		Space-only PDF text runs sometimes occupy the blank baselines on both
		sides of a heading. Search through those non-content lines for the
		nearest meaningful neighbors, then require independent typography and
		section-label evidence. Adjacent ordered-list peers, captions, linked
		TOC entries, wrapped titles, and orphan glyph fragments are excluded.
		"""
		if len(text) > 90 or line.size < body_size * 0.88:
			return False
		if self._is_explicit_caption_label(text):
			return False
		visible = [char for char in line.chars if char.text.strip()]
		if visible and sum(1 for char in visible if char.link) / len(visible) >= 0.50:
			return False

		page_lines = self.lines_by_page.get(line.page, [])
		index = next(
			(i for i, candidate in enumerate(page_lines) if candidate is line),
			-1,
		)
		if index < 0:
			return False
		previous = next(
			(
				candidate
				for candidate in reversed(page_lines[:index])
				if plain_text(line_text_tokens(candidate)).strip()
			),
			None,
		)
		following = next(
			(
				candidate
				for candidate in page_lines[index + 1:]
				if plain_text(line_text_tokens(candidate)).strip()
			),
			None,
		)
		# A heading must govern content. This also prevents one isolated bold
		# logo fragment on an otherwise image-only page becoming an outline.
		if following is None:
			return False

		previous_text = (
			plain_text(line_text_tokens(previous)).strip()
			if previous is not None
			else ""
		)
		following_text = plain_text(line_text_tokens(following)).strip()
		gap_above = (
			line_flow_gap(previous, line)
			if previous is not None
			else float("inf")
		)
		gap_below = line_flow_gap(line, following)

		list_peers = [
			candidate
			for candidate, candidate_text in (
				(previous, previous_text),
				(following, following_text),
			)
			if candidate is not None
			and list_marker(candidate_text) is not None
			and abs(candidate.x0 - line.x0) <= max(6.0, line.size * 0.60)
			and abs(candidate.size - line.size) <= 1.0
		]
		nested_number = re.match(
			r"^(?:\d+|[A-Z])(?:\.\d+){1,5}\.?\s+\S",
			text,
		) is not None
		numbered_upper = (
			re.match(r"^\d{1,3}\.\s+\S", text) is not None
			and text.isupper()
		)
		if (
			nested_number
			and line.bold_ratio >= 0.75
			and not list_peers
			and self._is_new_reading_band_numbered_heading(
				line,
				body_size,
				page_lines,
				index,
				previous,
				following,
			)
		):
			return True
		if (
			nested_number
			and line.bold_ratio >= 0.75
			and not list_peers
			and gap_above > body_size * 1.45
			and gap_below > body_size * 1.45
		):
			return True
		if (
			numbered_upper
			and not list_peers
			and gap_above > body_size * 1.80
			and gap_below > body_size * 1.05
		):
			return True

		compact_bold = (
			line.bold_ratio >= 0.75
			and len(text) <= 45
			and len(text.split()) <= 4
			and text[-1:] not in ".!?;:"
		)
		if not (
			compact_bold
			and gap_above > body_size * 2.0
			and gap_below > body_size * 0.95
			and abs(following.x0 - line.x0) <= max(8.0, line.size * 0.75)
		):
			return False
		if previous is not None and self._same_wrapped_style(previous, line):
			return False
		if self._same_wrapped_style(line, following) or following.bold_ratio >= 0.65:
			return False
		return True

	def _is_new_reading_band_numbered_heading(
		self,
		line: Line,
		body_size: float,
		page_lines: List[Line],
		index: int,
		previous: Optional[Line],
		following: Line,
	) -> bool:
		"""Verify a bold nested section label at a column transition.

		Reading-order reconciliation places the bottom of one column directly
		before the top/middle of the next, producing a negative flow gap.  Treat
		that as block-start evidence only when the horizontal transition is
		unambiguous and two ordinary same-margin prose lines establish the new
		column.  Numeric chart labels and ordered peers intentionally fail the
		prose and typography checks.
		"""
		if previous is None or index + 2 >= len(page_lines):
			return False
		if line_flow_gap(previous, line) > -body_size * 3.0:
			return False
		page_width, _page_height = self.conv.page_sizes.get(line.page, (612.0, 792.0))
		if line.x0 - previous.x0 < max(80.0, page_width * 0.20):
			return False
		second = next(
			(
				candidate
				for candidate in page_lines[index + 2:]
				if plain_text(line_text_tokens(candidate)).strip()
			),
			None,
		)
		if second is None:
			return False
		following_text = plain_text(line_text_tokens(following)).strip()
		second_text = plain_text(line_text_tokens(second)).strip()
		if list_marker(following_text) is not None or list_marker(second_text) is not None:
			return False
		if any(
			candidate.writing_mode != "horizontal"
			or candidate.bold_ratio >= 0.35
			or abs(candidate.size - body_size) > max(0.65, body_size * 0.06)
			or abs(candidate.x0 - line.x0) > max(8.0, body_size * 0.75)
			for candidate in (following, second)
		):
			return False
		first_gap = line_flow_gap(line, following)
		second_gap = line_flow_gap(following, second)
		if not (
			body_size * 1.15 <= first_gap <= body_size * 1.90
			and body_size * 0.85 <= second_gap <= body_size * 1.70
		):
			return False
		if len(following_text.split()) < 7 or len(second_text.split()) < 6:
			return False
		if not any(char.islower() for char in following_text + second_text):
			return False
		return following.x1 - following.x0 >= (line.x1 - line.x0) * 1.35

	def _has_adjacent_ordered_peer(self, line: Line, text: str) -> bool:
		if list_marker(text) is None:
			return False
		page_lines = self.lines_by_page.get(line.page, [])
		index = next(
			(i for i, candidate in enumerate(page_lines) if candidate is line),
			-1,
		)
		if index < 0:
			return False
		neighbors: List[Line] = []
		for candidates in (
			reversed(page_lines[:index]),
			iter(page_lines[index + 1:]),
		):
			neighbor = next(
				(
					candidate
					for candidate in candidates
					if plain_text(line_text_tokens(candidate)).strip()
				),
				None,
			)
			if neighbor is not None:
				neighbors.append(neighbor)
		for neighbor in neighbors:
			neighbor_text = plain_text(line_text_tokens(neighbor)).strip()
			if list_marker(neighbor_text) is None:
				continue
			if abs(neighbor.x0 - line.x0) > max(6.0, line.size * 0.60):
				continue
			if abs(neighbor.size - line.size) > 1.0:
				continue
			if abs(neighbor.bold_ratio - line.bold_ratio) > 0.25:
				continue
			if abs(neighbor.y0 - line.y0) > max(28.0, line.size * 2.20):
				continue
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

	def _is_numbered_hanging_display_continuation(
		self,
		upper: Line,
		lower: Line,
		following: Optional[Line],
	) -> bool:
		"""Recognize one hanging-indent wrap of a numbered display heading.

		Some report producers place the section marker in its own fixed-width
		field, start the title after a tab-like gap, and align every continuation
		with the first title word rather than with the marker.  That is distinct
		from an ordered-list wrap: both title lines use the same display style,
		the complete run is followed by smaller same-margin prose, and no nearby
		list, caption, navigation, or machine-label evidence is allowed.
		"""
		if (
			following is None
			or upper.page != lower.page
			or upper.page != following.page
			or any(
				candidate.writing_mode != "horizontal"
				for candidate in (upper, lower, following)
			)
		):
			return False
		upper_text = plain_text(line_text_tokens(upper)).strip()
		lower_text = plain_text(line_text_tokens(lower)).strip()
		following_text = plain_text(line_text_tokens(following)).strip()
		if not upper_text or not lower_text or not following_text:
			return False
		if any(
			self._is_explicit_caption_label(text)
			or self._is_toc_navigation_row(candidate, text)
			or re.fullmatch(r"[A-Z0-9]+(?:[-_][A-Z0-9]+)+", text) is not None
			for candidate, text in (
				(upper, upper_text),
				(lower, lower_text),
				(following, following_text),
			)
		):
			return False
		if list_marker(lower_text) is not None or list_marker(following_text) is not None:
			return False

		boxes = word_boxes(upper)
		if len(boxes) < 2:
			return False
		marker, marker_x0, _marker_y0, marker_x1, _marker_y1 = boxes[0]
		first_word, first_word_x0, _word_y0, _word_x1, _word_y1 = boxes[1]
		if re.fullmatch(r"\d{1,3}(?:\.\d+){0,5}\.?", marker) is None:
			return False
		if not first_word[:1].isupper():
			return False
		if first_word_x0 - marker_x1 < max(4.0, upper.size * 0.30):
			return False
		if abs(lower.x0 - first_word_x0) > max(3.0, upper.size * 0.28):
			return False
		if lower.x0 <= marker_x0 or lower.x1 > upper.x1 + max(6.0, upper.size * 0.45):
			return False

		combined = cleanup_spaces("%s %s" % (upper_text, lower_text))
		if (
			not 4 <= len(combined.split()) <= 18
			or len(combined) > 120
			or lower_text[-1:] in ".!?;:"
		):
			return False
		if (
			upper.bold_ratio < 0.75
			or lower.bold_ratio < 0.75
			or abs(upper.size - lower.size) > max(0.75, upper.size * 0.05)
		):
			return False

		all_lines = [
			line
			for page_lines in self.lines_by_page.values()
			for line in page_lines
		]
		body_size = self._body_font_size(all_lines)
		if body_size <= 0 or upper.size < body_size * 1.10:
			return False
		if (
			abs(following.size - body_size) > max(0.75, body_size * 0.08)
			or following.size > lower.size * 0.94
			or following.bold_ratio >= 0.35
			or abs(following.x0 - upper.x0) > max(8.0, body_size * 0.75)
			or len(following_text.split()) < 5
			or not any(character.islower() for character in following_text)
		):
			return False
		if self._has_adjacent_ordered_peer(upper, upper_text):
			return False

		wrap_gap = line_flow_gap(upper, lower)
		body_gap = line_flow_gap(lower, following)
		if wrap_gap <= 0 or wrap_gap > max(upper.size * 1.55, 24.0):
			return False
		return body_gap > max(wrap_gap * 1.12, lower.size * 1.35)

	def _is_standalone_display_marker_continuation(
		self,
		upper: Line,
		lower: Line,
		following: Optional[Line],
	) -> bool:
		"""Join a standalone chapter marker to its governed display title."""
		if following is None or upper.page != lower.page or lower.page != following.page:
			return False
		upper_text = plain_text(line_text_tokens(upper)).strip()
		lower_text = plain_text(line_text_tokens(lower)).strip()
		if re.fullmatch(r"(?:\d{1,3}|[IVXLCDM]+)[.)]?", upper_text, re.I) is None:
			return False
		if (
			not lower_text
			or not lower_text[:1].isupper()
			or not 1 <= len(lower_text.split()) <= 10
			or lower_text[-1:] in ".!?;:"
			or self._is_explicit_caption_label(lower_text)
			or self._is_toc_navigation_row(lower, lower_text)
			or upper.bold_ratio < 0.75
			or lower.bold_ratio < 0.75
		):
			return False
		page_lines = self.lines_by_page.get(upper.page, [])
		body_size = self._body_font_size(page_lines)
		if (
			body_size <= 0
			or lower.size < body_size * 1.45
			or upper.size < lower.size * 1.03
			or upper.size > lower.size * 1.60
			or abs(upper.x0 - lower.x0) > max(8.0, lower.size * 0.55)
			or self._has_adjacent_ordered_peer(upper, upper_text)
		):
			return False
		gap = line_flow_gap(upper, lower)
		if gap <= 0 or gap > max(upper.size * 1.65, 58.0):
			return False
		if self._display_wrap_peer(lower, following):
			return True
		following_text = plain_text(line_text_tokens(following)).strip()
		return bool(
			following_text
			and following.bold_ratio < 0.35
			and following.size <= lower.size * 0.82
			and abs(following.x0 - lower.x0) <= max(9.0, body_size * 0.80)
			and len(following_text.split()) >= 5
			and line_flow_gap(lower, following) > lower.size * 1.25
		)

	def _is_numbered_outdented_display_continuation(
		self,
		upper: Line,
		lower: Line,
		following: Optional[Line],
	) -> bool:
		"""Join a numbered display line to one full-margin title continuation."""
		if following is None or upper.page != lower.page or lower.page != following.page:
			return False
		upper_text = plain_text(line_text_tokens(upper)).strip()
		lower_text = plain_text(line_text_tokens(lower)).strip()
		following_text = plain_text(line_text_tokens(following)).strip()
		if re.match(r"^\d{1,3}(?:\.\d+){0,5}\.\s+[A-Z]", upper_text) is None:
			return False
		if any(
			self._is_explicit_caption_label(text)
			or self._is_toc_navigation_row(candidate, text)
			for candidate, text in ((upper, upper_text), (lower, lower_text))
		):
			return False
		if (
			not 4 <= len(upper_text.split()) <= 12
			or not 5 <= len(lower_text.split()) <= 12
			or len(cleanup_spaces("%s %s" % (upper_text, lower_text))) > 140
			or lower_text[-1:] in ".!?;:"
			or upper.bold_ratio < 0.75
			or lower.bold_ratio < 0.75
			or abs(upper.size - lower.size) > max(0.65, upper.size * 0.06)
		):
			return False
		body_size = self._body_font_size(self.lines_by_page.get(upper.page, []))
		outdent = upper.x0 - lower.x0
		if (
			body_size <= 0
			or not body_size * 0.85 <= outdent <= body_size * 2.25
			or abs(following.x0 - lower.x0) > max(7.0, body_size * 0.65)
			or abs(following.size - body_size) > max(0.65, body_size * 0.07)
			or following.bold_ratio >= 0.35
			or len(following_text.split()) < 5
			or self._has_adjacent_ordered_peer(upper, upper_text)
		):
			return False
		wrap_gap = line_flow_gap(upper, lower)
		body_gap = line_flow_gap(lower, following)
		return bool(
			wrap_gap > 0
			and body_gap > 0
			and 0.72 <= body_gap / wrap_gap <= 1.45
			and wrap_gap <= max(upper.size * 2.10, 30.0)
		)

	def _is_heading_continuation(self, prev: Line, cur: Line, nxt: Optional[Line]) -> bool:
		prev_tagged = self._tagged_heading_level(prev)
		cur_tagged = self._tagged_heading_level(cur)
		if prev_tagged is not None or cur_tagged is not None:
			return prev_tagged is not None and prev_tagged == cur_tagged and prev.page == cur.page
		if self._is_standalone_display_marker_continuation(prev, cur, nxt):
			return True
		if self._is_numbered_outdented_display_continuation(prev, cur, nxt):
			return True
		if self._is_tri_fold_heading_continuation(prev, cur):
			return True
		if self._is_numbered_hanging_display_continuation(prev, cur, nxt):
			return True
		if not self._display_wrap_peer(prev, cur):
			return False
		gap = line_flow_gap(prev, cur)
		run, _start, _end = self._display_wrap_run(prev)
		if (
			len(run) >= 2
			and prev.bold_ratio < 0.25
			and cur.bold_ratio < 0.25
		):
			# The regular-weight classifier has already verified isolation and
			# block evidence for this complete run.  Requiring an additional
			# post-line gap here would split the same verified title again.
			return True
		page_lines = self.lines_by_page.get(cur.page, [])
		index = next(
			(i for i, candidate in enumerate(page_lines) if candidate is cur),
			-1,
		)
		following = (
			next(
				(
					candidate
					for candidate in page_lines[index + 1:]
					if plain_text(line_text_tokens(candidate)).strip()
				),
				None,
			)
			if index >= 0
			else nxt
		)
		if following is None:
			return True
		if self._display_wrap_peer(cur, following):
			return True
		gap_after = line_flow_gap(cur, following)
		if gap_after <= 0:
			return True
		return gap_after > max(gap * 1.20, cur.size * 1.40)

	def _heading_level(
		self,
		line: Line,
		body_size: float,
		prev: Optional[Line] = None,
		nxt: Optional[Line] = None,
	) -> int:
		if self.conv.options.heading_level_mode == "flat":
			# Some consumers model headings as a flat set rather than a nested
			# outline. Detection is unchanged; only the projected level of an
			# already-accepted heading collapses to one.
			return 1
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
		# Some producers paint one visual code panel as a sequence of adjacent
		# per-line background strips.  A validated cohort is stronger evidence
		# than one misreported font face (or a whitespace-only source line), so
		# consult it before the ordinary monospace admission gate.
		if self._tiled_code_panel_for_line(line) is not None:
			return True
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

	def _is_decorative_accent_eyebrow(
		self,
		lines: List[Line],
		index: int,
		page: int,
		body_size: float,
	) -> bool:
		"""Distinguish a short display accent from a blockquote bar.

		Slide producers often draw one vertical accent beside a small eyebrow
		and a wrapped regular-weight title.  The same primitive can resemble a
		quote bar.  Reclassify only the eyebrow: the following title remains
		available to the normal heading path.  A genuine quote with ordinary
		text, an enlarged opening line, or a bar extending into later content
		retains quote ownership.
		"""
		if index + 1 >= len(lines):
			return False
		eyebrow = lines[index]
		title = lines[index + 1]
		eyebrow_text = plain_text(line_text_tokens(eyebrow)).strip()
		if not eyebrow_text or len(eyebrow_text) > 80 or len(eyebrow_text.split()) > 10:
			return False
		if eyebrow_text[-1:] in ".!?":
			return False
		if eyebrow.bold_ratio >= 0.25 or title.bold_ratio >= 0.25:
			return False
		if title.size < body_size * 1.45 or eyebrow.size > title.size * 0.75:
			return False
		if abs(eyebrow.x0 - title.x0) > max(8.0, title.size * 0.45):
			return False

		run, start, _end = self._display_wrap_run(title)
		if start != index + 1 or len(run) < 2:
			return False
		if not self._is_regular_display_heading(title, body_size):
			return False

		eyebrow_bars = self._quote_bars_for_line(eyebrow, page)
		common_bars = [
			bar
			for bar in eyebrow_bars
			if all(
				any(candidate is bar for candidate in self._quote_bars_for_line(run_line, page))
				for run_line in run
			)
		]
		if not common_bars:
			return False
		run_bottom = max(run_line.y1 for run_line in run)
		run_top = min(run_line.y0 for run_line in run)
		for bar in common_bars:
			bar_top = min(bar.y0, bar.y1)
			bar_bottom = max(bar.y0, bar.y1)
			if (
				bar_top >= eyebrow.y0 - max(8.0, eyebrow.size * 0.75)
				and bar_top <= eyebrow.y1
				and bar_bottom >= run_top
				and bar_bottom <= run_bottom + max(12.0, title.size * 0.75)
			):
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

	def _tiled_code_panels(self, page: int) -> Tuple[Dict[str, Any], ...]:
		"""Return code panels encoded as contiguous per-line background fills.

		The fills remain geometry-only evidence.  Admission requires a stable,
		paint-order-backed one-fill/one-line cohort whose authored glyphs are
		overwhelmingly monospace.  This is deliberately stricter than ordinary
		code-fill handling so striped prose, key/value cards, and table rows do
		not become code merely because they share a background colour.
		"""
		cached = self._tiled_code_panels_cache.get(page)
		if cached is not None:
			return cached

		page_width, page_height = self.conv.page_sizes.get(page, (612.0, 792.0))
		page_area = max(page_width * page_height, 1.0)
		eligible: List[Fill] = []
		for fill in self.conv.fills:
			if fill.page != page:
				continue
			width = fill.x1 - fill.x0
			height = fill.y1 - fill.y0
			if (
				width <= 100.0
				or width >= page_width * 0.95
				or height < 2.0
				or height > max(40.0, page_height * 0.08)
				or width * height >= page_area * 0.35
			):
				continue
			eligible.append(fill)

		# Exact row strips from a producer normally share coordinates.  Rounding
		# only absorbs harmless PDF-number noise; the later geometric checks still
		# use the unrounded boxes.
		cohorts: Dict[
			Tuple[float, float, Tuple[float, float, float]],
			List[Fill],
		] = {}
		for fill in eligible:
			cohort_key = (
				round(fill.x0, 1),
				round(fill.x1, 1),
				tuple(round(channel, 2) for channel in fill.color),
			)
			cohorts.setdefault(cohort_key, []).append(fill)

		page_lines = self.lines_by_page.get(page, [])
		panels: List[Dict[str, Any]] = []
		for cohort_fills in cohorts.values():
			ordered = sorted(cohort_fills, key=lambda fill: (fill.y0, fill.y1, fill.seq))
			connected: List[List[Fill]] = []
			for fill in ordered:
				if not connected:
					connected.append([fill])
					continue
				previous = connected[-1][-1]
				previous_height = previous.y1 - previous.y0
				current_height = fill.y1 - fill.y0
				gap = fill.y0 - previous.y1
				allowed_gap = max(2.0, min(previous_height, current_height) * 0.25)
				if -2.0 <= gap <= allowed_gap:
					connected[-1].append(fill)
				else:
					connected.append([fill])

			for strips in connected:
				if len(strips) < 3:
					continue
				heights = [fill.y1 - fill.y0 for fill in strips]
				strip_height = median(heights)
				if any(
					abs(height - strip_height) > max(2.0, strip_height * 0.25)
					for height in heights
				):
					continue

				matched_lines: List[Line] = []
				valid = True
				for fill in strips:
					matches = [
						line
						for line in page_lines
						if fill.x0 <= line.x0 + 2.0
						and fill.x1 >= line.x1 - 2.0
						and fill.y0 <= line.y0 + 2.0
						and fill.y1 >= line.y1 - 2.0
					]
					if len(matches) != 1 or id(matches[0]) in {id(line) for line in matched_lines}:
						valid = False
						break
					line = matches[0]
					painted_chars = [
						char
						for char in line.chars
						if char.text and not char.invisible
					]
					if (
						not painted_chars
						or fill.paint_order <= 0
						or any(char.paint_order <= 0 for char in painted_chars)
						or fill.paint_order >= min(char.paint_order for char in painted_chars)
					):
						valid = False
						break
					matched_lines.append(line)
				if not valid:
					continue

				visible_by_line = [
					[
						char
						for char in line.chars
						if char.text.strip() and not char.invisible and not char.artifact
					]
					for line in matched_lines
				]
				nonempty_indexes = [
					index
					for index, chars in enumerate(visible_by_line)
					if chars
				]
				if len(nonempty_indexes) < 3:
					continue
				visible_chars = [
					char
					for chars in visible_by_line
					for char in chars
				]
				mono_char_ratio = sum(char.mono for char in visible_chars) / len(visible_chars)
				if mono_char_ratio < 0.85:
					continue
				if sum(char.bold for char in visible_chars) / len(visible_chars) >= 0.50:
					continue

				sizes = [line.size for line in matched_lines if line.size > 0]
				if not sizes:
					continue
				panel_size = median(sizes)
				if any(
					abs(size - panel_size) > max(1.0, panel_size * 0.15)
					for size in sizes
				):
					continue

				line_mono = {
					index: sum(char.mono for char in visible_by_line[index])
					/ len(visible_by_line[index])
					for index in nonempty_indexes
				}
				style_outliers = [
					index
					for index in nonempty_indexes
					if line_mono[index] < 0.85
				]
				if len(style_outliers) > 1:
					continue
				if style_outliers:
					outlier = style_outliers[0]
					previous_nonempty = max(
						(index for index in nonempty_indexes if index < outlier),
						default=-1,
					)
					next_nonempty = min(
						(index for index in nonempty_indexes if index > outlier),
						default=-1,
					)
					mono_lines = [
						matched_lines[index]
						for index in nonempty_indexes
						if index != outlier and line_mono[index] >= 0.85
					]
					if (
						previous_nonempty < 0
						or next_nonempty < 0
						or line_mono.get(previous_nonempty, 0.0) < 0.85
						or line_mono.get(next_nonempty, 0.0) < 0.85
						or not mono_lines
						or matched_lines[outlier].x0
							< min(line.x0 for line in mono_lines) - panel_size
						or matched_lines[outlier].x0
							> max(line.x0 for line in mono_lines) + panel_size * 2.0
					):
						continue

				bbox = (
					min(fill.x0 for fill in strips),
					min(fill.y0 for fill in strips),
					max(fill.x1 for fill in strips),
					max(fill.y1 for fill in strips),
				)
				unruled_record_grid = self._tiled_panel_has_unruled_record_grid(
						matched_lines,
						bbox,
						panel_size,
					)
				if unruled_record_grid:
					self._tiled_noncode_line_ids.update(
						id(line) for line in matched_lines
					)
				if self._tiled_panel_has_ruled_grid(page, bbox) or unruled_record_grid:
					continue
				panels.append({
					"bbox": bbox,
					"fill_seqs": tuple(fill.seq for fill in strips),
					"strip_count": len(strips),
					"mono_char_ratio": mono_char_ratio,
					"style_outlier_count": len(style_outliers),
					"paint_order_backed": True,
					"_line_ids": frozenset(id(line) for line in matched_lines),
				})

		panels.sort(key=lambda panel: (panel["bbox"][1], panel["bbox"][0]))
		result = tuple(panels)
		self._tiled_code_panels_cache[page] = result
		for panel in result:
			for line_id in panel["_line_ids"]:
				self._tiled_code_panel_by_line[line_id] = panel
		return result

	def _tiled_panel_has_ruled_grid(
		self,
		page: int,
		bbox: Tuple[float, float, float, float],
	) -> bool:
		"""Veto row-strip cohorts that are independently owned by a lattice.

		A bordered table can paint one background rectangle per row and use a
		monospace face in every cell, which otherwise resembles a tiled code
		panel.  Two persistent vertical boundaries plus three repeated horizontal
		boundaries are unambiguous grid evidence.  Requiring both orientations
		keeps ordinary bordered or underlined code panels eligible.
		"""
		x0, y0, x1, y1 = bbox
		width = max(x1 - x0, 1.0)
		height = max(y1 - y0, 1.0)
		verticals: List[float] = []
		horizontals: List[float] = []
		for segment in self.conv.segments:
			if segment.page != page:
				continue
			if segment.vertical:
				x = (segment.x0 + segment.x1) / 2.0
				segment_y0 = min(segment.y0, segment.y1)
				segment_y1 = max(segment.y0, segment.y1)
				overlap = max(0.0, min(segment_y1, y1) - max(segment_y0, y0))
				if x0 - 2.0 <= x <= x1 + 2.0 and overlap >= height * 0.75:
					verticals.append(x)
			elif segment.horizontal:
				y = (segment.y0 + segment.y1) / 2.0
				segment_x0 = min(segment.x0, segment.x1)
				segment_x1 = max(segment.x0, segment.x1)
				overlap = max(0.0, min(segment_x1, x1) - max(segment_x0, x0))
				if y0 - 2.0 <= y <= y1 + 2.0 and overlap >= width * 0.75:
					horizontals.append(y)
		if len(verticals) < 2 or len(horizontals) < 3:
			return False
		vertical_cohorts = {round(value, 1) for value in verticals}
		horizontal_cohorts = {round(value, 1) for value in horizontals}
		return len(vertical_cohorts) >= 2 and len(horizontal_cohorts) >= 3

	@staticmethod
	def _tiled_panel_has_unruled_record_grid(
		lines: Sequence[Line],
		bbox: Tuple[float, float, float, float],
		panel_size: float,
	) -> bool:
		"""Veto stable two-field record rows that merely share strip fills.

		Some producer templates paint each unruled table/card row separately and
		use a monospace face for both fields.  A repeated large internal gutter,
		stable left/right anchors, and an absence of code syntax are independent
		layout evidence for records rather than source code.  Ordinary aligned
		code remains eligible because assignments, delimiters, comments, or an
		unstable continuation gutter break this deliberately narrow cohort.
		"""
		x0, _y0, x1, _y1 = bbox
		minimum_gap = max(24.0, panel_size * 3.5)
		records: List[Tuple[float, float, float]] = []
		nonempty = 0
		code_syntax = re.compile(r"[=()\[\]{};:+*/<>#`\\\"']")
		for line in lines:
			chars = sorted(
				[
					char
					for char in line.chars
					if char.text.strip() and not char.invisible and not char.artifact
				],
				key=lambda char: (char.x0, char.seq),
			)
			if not chars:
				continue
			nonempty += 1
			gaps = [
				(
					next_char.x0 - previous.x1,
					(previous.x1 + next_char.x0) / 2.0,
					index,
				)
				for index, (previous, next_char) in enumerate(zip(chars, chars[1:]))
				if next_char.x0 - previous.x1 >= minimum_gap
			]
			if not gaps:
				continue
			_gap, midpoint, index = max(gaps)
			left = "".join(char.text for char in chars[: index + 1]).strip()
			right = "".join(char.text for char in chars[index + 1 :]).strip()
			if (
				not left
				or not right
				or code_syntax.search(left)
				or code_syntax.search(right)
				or len(left.split()) > 6
				or len(right.split()) > 6
			):
				continue
			records.append((midpoint, chars[0].x0, chars[index + 1].x0))
		minimum_records = max(3, math.ceil(nonempty * 0.80))
		if len(records) < minimum_records:
			return False
		midpoints = [record[0] for record in records]
		left_anchors = [record[1] for record in records]
		right_anchors = [record[2] for record in records]
		anchor_tolerance = max(4.0, panel_size * 0.65)
		return (
			max(midpoints) - min(midpoints) <= anchor_tolerance
			and max(left_anchors) - min(left_anchors) <= anchor_tolerance
			and max(right_anchors) - min(right_anchors) <= anchor_tolerance
			and x0 + panel_size < median(midpoints) < x1 - panel_size
		)

	def _tiled_code_panel_for_line(self, line: Line) -> Optional[Dict[str, Any]]:
		line_id = id(line)
		if line_id in self._tiled_code_panel_by_line:
			return self._tiled_code_panel_by_line[line_id]
		self._tiled_code_panels(line.page)
		panel = self._tiled_code_panel_by_line.get(line_id)
		self._tiled_code_panel_by_line[line_id] = panel
		return panel

	def _tiled_code_event_attrs(self, panel: Optional[Dict[str, Any]]) -> Dict[str, Any]:
		if panel is None:
			return {}
		return {
			"tiled_fill_code_panel": True,
			"tiled_fill_bbox": panel["bbox"],
			"tiled_fill_sequences": panel["fill_seqs"],
			"tiled_fill_strip_count": panel["strip_count"],
			"tiled_fill_mono_char_ratio": panel["mono_char_ratio"],
			"tiled_fill_style_outlier_count": panel["style_outlier_count"],
			"tiled_fill_paint_order_backed": panel["paint_order_backed"],
		}

	def _code_fill_key(self, line: Line) -> Optional[Tuple[float, float, float, float]]:
		if id(line) in self._tiled_noncode_line_ids:
			return None
		panel = self._tiled_code_panel_for_line(line)
		# Discovery can classify an unruled record cohort on this first call.
		# Honor that newly populated veto before inspecting the row's own fill.
		if id(line) in self._tiled_noncode_line_ids:
			return None
		if panel is not None:
			return panel["bbox"]
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
			panel_context = self._panel_line_context(line)
			frame_left = (
				float(panel_context["start"])
				if panel_context is not None
				else self._text_frame(line.page)[0]
			)
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
		panel_context = self._panel_line_context(line)
		if panel_context is not None:
			available = max(20.0, float(panel_context["right"]) - line.x0)
			self._available_width_cache[id(line)] = available
			return available
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
		panel_context = self._panel_line_context(lines[i])
		frame_left = (
			float(panel_context["start"])
			if panel_context is not None
			else self._text_frame(lines[i].page)[0]
		)
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
		panel_context = self._panel_line_context(line)
		frame_left = (
			float(panel_context["start"])
			if panel_context is not None
			else self._text_frame(line.page)[0]
		)
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

	def _callouts(
		self,
		page: int,
	) -> List[Tuple[float, str, List[Line], Dict[str, Any]]]:
		lines = self.lines_by_page.get(page, [])
		out: List[Tuple[float, str, List[Line], Dict[str, Any]]] = []
		seen: set = set()
		page_width, page_height = self.conv.page_sizes.get(page, (612.0, 792.0))
		page_area = max(page_width * page_height, 1.0)
		table_boxes = [candidate[3] for candidate in self._table_candidates(page)]
		fill_candidates = [
			(fill, False)
			for fill in self.conv.fills
		] + [
			(fill, True)
			for fill in self.conv._artifact_local_backgrounds
		]
		# Repeated same-size paint is commonly a card grid rather than a series of
		# callouts.  Build the cohort cardinalities once: scanning the complete
		# artifact-fill list for every candidate makes adversarial content
		# quadratic.  Colour participates in the key so differently purposed
		# warning/info/success boxes are not conflated merely because their
		# dimensions happen to match.
		artifact_fill_cohorts: Dict[
			Tuple[int, float, float, Tuple[float, float, float]],
			int,
		] = {}
		for candidate in self.conv._artifact_local_backgrounds:
			if max(candidate.color) - min(candidate.color) < 0.08:
				continue
			key = (
				candidate.page,
				round(candidate.x1 - candidate.x0, 1),
				round(candidate.y1 - candidate.y0, 1),
				tuple(round(channel, 2) for channel in candidate.color),
			)
			artifact_fill_cohorts[key] = artifact_fill_cohorts.get(key, 0) + 1
		for fill, artifact_geometry in fill_candidates:
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
			artifact_reasons: List[str] = []
			if artifact_geometry:
				if max(fill.color) - min(fill.color) < 0.08:
					continue
				if len(callout_lines) < 2:
					continue
				authored_chars = [
					char
					for line in callout_lines
					for char in line.chars
					if char.text.strip() and not char.artifact
				]
				all_visible_chars = [
					char
					for line in callout_lines
					for char in line.chars
					if char.text.strip()
				]
				if (
					len(authored_chars) < 8
					or len(authored_chars) < len(all_visible_chars) * 0.90
					or sum(line.mono_ratio >= 0.70 for line in callout_lines)
						>= math.ceil(len(callout_lines) * 0.50)
				):
					continue
				# A background must be painted before the authored text it supports.
				# Post-text overlays/highlights can share identical geometry but must
				# never be promoted to a semantic container.
				text_paint_orders = [
					char.paint_order
					for char in authored_chars
					if char.paint_order > 0
				]
				if (
					fill.paint_order <= 0
					or len(text_paint_orders) != len(authored_chars)
					or fill.paint_order >= min(text_paint_orders)
				):
					continue
				if (
					min(line.x0 for line in callout_lines) < fill.x0 + 2.0
					or max(line.x1 for line in callout_lines) > fill.x1 - 2.0
					or min(line.y0 for line in callout_lines) < fill.y0 + 2.0
					or max(line.y1 for line in callout_lines) > fill.y1 - 2.0
				):
					continue
				if any(
					image.page == page
					and not (
						image.x1 <= fill.x0
						or image.x0 >= fill.x1
						or image.y1 <= fill.y0
						or image.y0 >= fill.y1
					)
					for image in self.conv.images
				):
					continue
				internal_rules = []
				for segment in [
					*self.conv.segments,
					*self.conv._artifact_rule_segments,
				]:
					if segment.page != page:
						continue
					segment_x0, segment_x1 = sorted((segment.x0, segment.x1))
					segment_y0, segment_y1 = sorted((segment.y0, segment.y1))
					x_overlap = max(
						0.0,
						min(segment_x1, fill.x1) - max(segment_x0, fill.x0),
					)
					y_overlap = max(
						0.0,
						min(segment_y1, fill.y1) - max(segment_y0, fill.y0),
					)
					if (
						segment.horizontal
						and segment.length >= fill_width * 0.35
						and x_overlap >= fill_width * 0.35
						and fill.y0 + 4.0
						< (segment.y0 + segment.y1) / 2.0
						< fill.y1 - 4.0
					) or (
						segment.vertical
						and segment.length >= fill_height * 0.40
						and y_overlap >= fill_height * 0.40
						and fill.x0 + 4.0
						< (segment.x0 + segment.x1) / 2.0
						< fill.x1 - 4.0
					):
						internal_rules.append(segment)
				if internal_rules:
					continue
				cohort_key = (
					fill.page,
					round(fill_width, 1),
					round(fill_height, 1),
					tuple(round(channel, 2) for channel in fill.color),
				)
				if artifact_fill_cohorts.get(cohort_key, 0) >= 3:
					continue
				if self._artifact_callout_is_page_title_banner(
					fill,
					callout_lines,
					lines,
					page_width,
					page_height,
				):
					continue
				artifact_reasons = [
					"chromatic_local_artifact_fill",
					"multiple_authored_inset_lines",
					"background_painted_before_authored_text",
					"no_table_grid_image_or_vector_overlap",
				]
			math_html = None if artifact_geometry else self._display_math_html(fill, callout_lines)
			if math_html is not None:
				out.append((min(line.y0 for line in callout_lines), math_html, callout_lines, {}))
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
			attrs: Dict[str, Any] = {}
			if artifact_geometry:
				attrs = {
					"artifact_background_geometry": True,
					"artifact_background_bbox": fill_box,
					"artifact_background_color": fill.color,
					"artifact_background_paint_order": fill.paint_order,
					"artifact_background_sequence": fill.seq,
					"artifact_callout_reasons": artifact_reasons,
				}
				self.conv.doc.warn(
					"ARTIFACT_CALLOUT_BACKGROUND_RECOVERED",
					"local Artifact paint used only as callout geometry",
					page,
				)
			out.append((min(line.y0 for line in callout_lines), html, callout_lines, attrs))
			seen.update(id(line) for line in callout_lines)
		out.sort(key=lambda item: item[0])
		return out

	def _artifact_callout_is_page_title_banner(
		self,
		fill: Fill,
		callout_lines: Sequence[Line],
		page_lines: Sequence[Line],
		page_width: float,
		page_height: float,
	) -> bool:
		"""Keep a unique chromatic title strip in the document heading stream."""
		ordered = sorted(callout_lines, key=lambda line: (line.y0, line.x0, line.seq))
		if len(ordered) < 2:
			return False
		first_text = plain_text(line_text_tokens(ordered[0])).strip()
		if re.match(r"^[^:\n]{1,80}:(?:\s+|$)", first_text):
			return False
		fill_width = fill.x1 - fill.x0
		fill_height = fill.y1 - fill.y0
		if (
			fill.y0 > page_height * 0.20
			or fill_width < page_width * 0.45
			or fill_height > max(110.0, page_height * 0.18)
		):
			return False
		body_size = self._body_font_size(list(page_lines))
		companion_sizes = [line.size for line in ordered[1:] if line.size > 0]
		if not companion_sizes:
			return False
		return (
			ordered[0].size >= body_size * 1.35
			and ordered[0].size >= median(companion_sizes) * 1.20
		)

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
		sx0, sx1 = sorted((seg.x0, seg.x1))
		for line in lines:
			if not self._heading_like(line, body_size):
				continue
			if (
				line.chars
				and all(char.artifact for char in line.chars if char.text)
				and sx0 <= line.x0 + line.size
				and sx1 >= line.x1 - line.size
				and 0 <= line.y0 - y <= max(line.size * 0.55, 10.0)
			):
				# A paired rule above a recovered Artifact title is part of the
				# producer's decorative title band, not an authored thematic break.
				return True
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

	def _artifact_filled_lattice_candidates(
		self,
		page: int,
	) -> List[
		Tuple[
			float,
			str,
			List[Line],
			Tuple[float, float, float, float],
		]
	]:
		"""Recover closed tables whose visible thin rules are tagged Artifact.

		Tagged office PDFs sometimes classify border paint as pagination
		furniture even though the enclosed glyphs are ordinary authored table
		content.  Artifact status remains authoritative for semantics and
		provenance; the isolated rules are used only as geometry evidence.  A
		candidate must be a complete, connected lattice with a fully populated
		first band and repeated multi-cell body occupancy.  A deliberately narrow
		2-by-2 exception admits a large comparison table only when both bold or
		marked header cells and both dense, multiline body cells are populated.
		Open boxes, sparse forms, cards, backgrounds, and decorative frames
		therefore do not qualify.
		"""
		segments = [
			segment
			for segment in self.conv._artifact_rule_segments
			if segment.page == page
		]
		if not segments:
			return []
		components = segment_components(segments)
		# A one-column table is indistinguishable from a framed callout on
		# geometry alone.  It is eligible only when the page contains a single
		# complete one-column artifact lattice; repeated framed cards therefore
		# remain prose even before the independent fill/occupancy gates below.
		closed_one_column_components = 0
		for component in components:
			horizontal = [
				segment
				for segment in component
				if segment.horizontal and segment.length >= 4.0
			]
			vertical = [
				segment
				for segment in component
				if segment.vertical and segment.length >= 4.0
			]
			xs = cluster_values(
				[(segment.x0 + segment.x1) / 2.0 for segment in vertical],
				2.0,
			)
			ys = cluster_values(
				[(segment.y0 + segment.y1) / 2.0 for segment in horizontal],
				2.0,
			)
			if (
				len(xs) == 2
				and 5 <= len(ys) <= 81
				and grid_coverage(xs, ys, horizontal, vertical) >= 0.96
				and lattice_has_all_cell_edges(
					xs,
					ys,
					horizontal,
					vertical,
				)
			):
				closed_one_column_components += 1
		page_width, page_height = self.conv.page_sizes.get(
			page,
			(612.0, 792.0),
		)
		page_lines = self.lines_by_page.get(page, [])
		out: List[
			Tuple[
				float,
				str,
				List[Line],
				Tuple[float, float, float, float],
			]
		] = []
		for component in components:
			horizontal = [
				segment
				for segment in component
				if segment.horizontal and segment.length >= 4.0
			]
			vertical = [
				segment
				for segment in component
				if segment.vertical and segment.length >= 4.0
			]
			xs = cluster_values(
				[(segment.x0 + segment.x1) / 2.0 for segment in vertical],
				2.0,
			)
			ys = cluster_values(
				[(segment.y0 + segment.y1) / 2.0 for segment in horizontal],
				2.0,
			)
			columns = len(xs) - 1
			rows = len(ys) - 1
			standard_dimensions = (
				3 <= columns <= 12 and 3 <= rows <= 80
			)
			compact_dimensions = columns == 2 and rows == 2
			filled_one_column_dimensions = (
				columns == 1
				and 4 <= rows <= 80
				and closed_one_column_components == 1
			)
			if not (
				standard_dimensions
				or compact_dimensions
				or filled_one_column_dimensions
			):
				continue
			if (
				any(right - left < 12.0 for left, right in zip(xs, xs[1:]))
				or any(bottom - top < 8.0 for top, bottom in zip(ys, ys[1:]))
			):
				continue
			width = xs[-1] - xs[0]
			height = ys[-1] - ys[0]
			if (
				width < max(120.0, page_width * 0.25)
				or width > page_width * 0.96
				or height < max(48.0, page_height * 0.06)
				or grid_coverage(xs, ys, horizontal, vertical) < 0.96
				or not lattice_has_all_cell_edges(
					xs,
					ys,
					horizontal,
					vertical,
				)
			):
				continue
			table_lines = [
				line
				for line in page_lines
				if any(
					char.text.strip()
					and xs[0] - 2.0
						<= (char.x0 + char.x1) / 2.0
						<= xs[-1] + 2.0
					and ys[0] - 2.0
						<= (char.y0 + char.y1) / 2.0
						<= ys[-1] + 2.0
					for char in line.chars
				)
			]
			if len(table_lines) < rows:
				continue
			visible_chars = [
				char
				for line in table_lines
				for char in line.chars
				if (
					char.text.strip()
					and xs[0] - 2.0
						<= (char.x0 + char.x1) / 2.0
						<= xs[-1] + 2.0
					and ys[0] - 2.0
						<= (char.y0 + char.y1) / 2.0
						<= ys[-1] + 2.0
				)
			]
			if (
				not visible_chars
				or sum(not char.artifact for char in visible_chars)
					< len(visible_chars) * 0.90
			):
				continue
			occupancy = self._partial_grid_occupancy(
				table_lines,
				xs,
				ys,
			)
			if len(occupancy) != rows or any(not row for row in occupancy):
				continue
			fill_backed_rows = 0
			if filled_one_column_dimensions:
				for row_index, (top, bottom) in enumerate(zip(ys, ys[1:])):
					row_height = bottom - top
					row_fills = [
						fill
						for fill in self.conv.fills
						if fill.page == page
						and min(fill.y1, bottom) - max(fill.y0, top)
							>= max(2.0, row_height * 0.25)
						and fill.x1 > xs[0]
						and fill.x0 < xs[-1]
					]
					coverage = self._partial_interval_coverage(
						[
							(max(xs[0], fill.x0), min(xs[-1], fill.x1))
							for fill in row_fills
						],
						xs[0],
						xs[-1],
					)
					if occupancy[row_index] and coverage >= 0.75:
						fill_backed_rows += 1
				if fill_backed_rows < math.ceil(rows * 0.75):
					continue
			if compact_dimensions:
				if not self._dense_two_by_two_artifact_table(
					table_lines,
					xs,
					ys,
					page_width,
					page_height,
				):
					continue
			elif not filled_one_column_dimensions and (
				occupancy[0] != set(range(columns))
				or sum(
					len(row) >= max(2, math.ceil(columns * 0.50))
					for row in occupancy[1:]
				) < 2
			):
				continue
			header_rows = self._artifact_lattice_header_rows(
				table_lines,
				xs,
				ys,
			)
			box = (
				float(xs[0]),
				float(ys[0]),
				float(xs[-1]),
				float(ys[-1]),
			)
			html = self._render_partial_grid_html(
				page,
				xs,
					ys,
					table_lines,
					"",
					header_rows_override=header_rows,
				)
			self._partial_table_models[(page, box)] = {
				"model_kind": "artifact_filled_lattice",
				"xs": list(xs),
				"ys": list(ys),
				"header_rows": header_rows,
				"evidence": {
					"artifact_geometry_only": True,
					"artifact_rule_rectangles": len(component),
					"complete_edge_coverage": True,
					"dense_complete_two_by_two": compact_dimensions,
					"unique_filled_one_column": filled_one_column_dimensions,
					"fill_backed_rows": fill_backed_rows,
					"authored_glyph_ratio": (
						sum(not char.artifact for char in visible_chars)
						/ len(visible_chars)
					),
				},
			}
			out.append(
				(
					float(ys[0]),
					html,
					table_lines,
					box,
				)
			)
		return out

	def _artifact_partial_fill_grid_candidates(
		self,
		page: int,
	) -> List[
		Tuple[
			float,
			str,
			List[Line],
			Tuple[float, float, float, float],
		]
	]:
		"""Recover a fill-backed grid whose thin rules are artifact geometry.

		Some authored tables paint only a segmented top boundary and one
		fragmented internal divider, while alternating row fills preserve the
		remaining column bands.  Admission combines four independent signals:
		an explicit caption after the block, exactly three adjacent top-rule
		spans, a repeated three-band fill pattern, and two stable numeric value
		columns.  This intentionally excludes ordinary shaded lists, cards, and
		forms even though any one of those signals may occur there.
		"""
		lines = self.lines_by_page.get(page, [])
		captions = [
			line
			for line in lines
			if self._is_explicit_table_caption(
				plain_text(line_text_tokens(line)).strip()
			)
		]
		if not captions:
			return []
		artifact_segments = [
			segment
			for segment in self.conv._artifact_rule_segments
			if segment.page == page
		]
		horizontal = [
			segment
			for segment in artifact_segments
			if segment.horizontal and segment.length >= 20.0
		]
		vertical = [
			segment
			for segment in artifact_segments
			if segment.vertical and segment.length >= 5.0
		]
		if len(horizontal) < 3 or len(vertical) < 4:
			return []
		horizontal_rows: List[List[Segment]] = []
		for segment in sorted(
			horizontal,
			key=lambda item: (
				(item.y0 + item.y1) / 2.0,
				min(item.x0, item.x1),
			),
		):
			y = (segment.y0 + segment.y1) / 2.0
			if horizontal_rows:
				peer_y = median(
					[(item.y0 + item.y1) / 2.0 for item in horizontal_rows[-1]]
				)
			else:
				peer_y = -math.inf
			if horizontal_rows and abs(y - peer_y) <= 2.0:
				horizontal_rows[-1].append(segment)
			else:
				horizontal_rows.append([segment])
		vertical_groups = [
			group
			for group in self._partial_vertical_groups(vertical)
			if len(group["segments"]) >= 4 and group["coverage"] >= 0.88
		]
		out: List[
			Tuple[
				float,
				str,
				List[Line],
				Tuple[float, float, float, float],
			]
		] = []
		for rule_row in horizontal_rows:
			intervals = sorted(
				[
					tuple(sorted((segment.x0, segment.x1)))
					for segment in rule_row
				],
			)
			if len(intervals) != 3 or any(
				following[0] - previous[1] > 3.0
				for previous, following in zip(intervals, intervals[1:])
			):
				continue
			top_y = median(
				[(segment.y0 + segment.y1) / 2.0 for segment in rule_row]
			)
			xs = [
				float(intervals[0][0]),
				*[float((left[1] + right[0]) / 2.0) for left, right in zip(intervals, intervals[1:])],
				float(intervals[-1][1]),
			]
			if any(right - left < 30.0 for left, right in zip(xs, xs[1:])):
				continue
			matching_vertical = [
				group
				for group in vertical_groups
				if abs(group["y0"] - top_y) <= 3.0
				and group["span"] >= 72.0
				and any(abs(group["x"] - boundary) <= 3.0 for boundary in xs[1:-1])
			]
			if not matching_vertical:
				continue
			bottom_y = max(group["y1"] for group in matching_vertical)
			caption_options = [
				caption
				for caption in captions
				if 2.0 < caption.y0 - bottom_y <= max(60.0, caption.size * 6.0)
				and caption.x1 >= xs[0] - 8.0
				and caption.x0 <= xs[-1] + 8.0
			]
			if not caption_options:
				continue
			caption_line = min(
				caption_options,
				key=lambda line: (line.y0 - bottom_y, line.seq),
			)
			if len(matching_vertical) == 1:
				endpoint_ys = [
					value
					for segment in matching_vertical[0]["segments"]
					for value in (
						min(segment.y0, segment.y1),
						max(segment.y0, segment.y1),
					)
				]
			else:
				endpoint_ys = self._partial_shared_vertical_endpoints(
					matching_vertical
				)
			ys = cluster_values(
				[top_y, bottom_y, *endpoint_ys],
				2.0,
			)
			ys = [value for value in ys if top_y - 2.0 <= value <= bottom_y + 2.0]
			if len(ys) < 7 or any(
				right - left < 8.0 for left, right in zip(ys, ys[1:])
			):
				continue
			region_lines = [
				line
				for line in lines
				if top_y - 2.0 <= (line.y0 + line.y1) / 2.0 <= bottom_y + 2.0
				and line.x1 >= xs[0] - 2.0
				and line.x0 <= xs[-1] + 2.0
			]
			occupancy = self._partial_grid_occupancy(region_lines, xs, ys)
			if (
				len(occupancy) != len(ys) - 1
				or len(occupancy) < 6
				or sum(row == set(range(3)) for row in occupancy[1:]) < 4
			):
				continue
			fill_backed_rows = 0
			for row_index, (row_top, row_bottom) in enumerate(zip(ys, ys[1:])):
				row_height = row_bottom - row_top
				row_fills = [
					fill
					for fill in self.conv.fills
					if fill.page == page
					and min(fill.y1, row_bottom) - max(fill.y0, row_top)
						>= max(2.0, row_height * 0.30)
				]
				if all(
					self._partial_interval_coverage(
						[
							(max(left, fill.x0), min(right, fill.x1))
							for fill in row_fills
							if fill.x1 > left and fill.x0 < right
						],
						left,
						right,
					) >= 0.65
					for left, right in zip(xs, xs[1:])
				):
					fill_backed_rows += 1
			if fill_backed_rows < 3:
				continue
			numeric_rows = 0
			for line in region_lines:
				numeric_columns = {
					max(0, min(2, find_interval(xs, (word[1] + word[3]) / 2.0)))
					for word in word_boxes(line)
					if self._booktabs_numeric_token(word[0])
				}
				if {1, 2}.issubset(numeric_columns):
					numeric_rows += 1
			if numeric_rows < 4:
				continue
			box = (
				float(xs[0]),
				float(ys[0]),
				float(xs[-1]),
				float(ys[-1]),
			)
			caption_text = plain_text(line_text_tokens(caption_line)).strip()
			html = self._render_partial_grid_html(
				page,
				xs,
				ys,
				region_lines,
				caption_text,
				header_rows_override=1,
				caption_placement="after",
			)
			self._partial_table_models[(page, box)] = {
				"model_kind": "artifact_partial_fill_grid",
				"xs": list(xs),
				"ys": list(ys),
				"header_rows": 1,
				"evidence": {
					"caption": caption_text,
					"caption_placement": "after",
					"artifact_top_rule_spans": 3,
					"fragmented_vertical_boundaries": len(matching_vertical),
					"fill_backed_rows": fill_backed_rows,
					"numeric_body_rows": numeric_rows,
				},
			}
			out.append((float(ys[0]), html, [*region_lines, caption_line], box))
		return out

	def _artifact_fragmented_lattice_candidates(
		self,
		page: int,
	) -> List[
		Tuple[
			float,
			str,
			List[Line],
			Tuple[float, float, float, float],
		]
	]:
		"""Recover authored tables from fragmented artifact-tagged borders.

		Office and curriculum producers frequently emit each visible table border
		as a stack of short artifact-tagged rectangles rather than one continuous
		path.  Merged cells intentionally omit only the boundary segment inside the
		span, and a table clipped at the page edge may omit the final vertical edge
		entirely.  The complete-lattice detector must reject those shapes because it
		cannot distinguish a missing edge from arbitrary artwork.

		This path admits the narrower, independently evidenced case: a connected
		component with persistent vertical boundary cohorts, repeated horizontal
		row bands, authored (non-artifact) glyphs in every physical row, and a
		populated multi-cell first row.  Missing internal edges become spans only
		when the source cell contains text and every covered neighbour is empty.
		Consequently blank worksheet cells remain separate cells, while explicit
		rowspan/colspan geometry is retained.  Artifact objects remain geometry-only
		evidence and are never promoted to semantic text.
		"""
		segments = [
			segment
			for segment in self.conv._artifact_rule_segments
			if segment.page == page
		]
		if not segments:
			return []
		page_width, page_height = self.conv.page_sizes.get(
			page,
			(612.0, 792.0),
		)
		page_lines = self.lines_by_page.get(page, [])
		out: List[
			Tuple[
				float,
				str,
				List[Line],
				Tuple[float, float, float, float],
			]
		] = []
		for component in segment_components(segments):
			horizontal = [
				segment
				for segment in component
				if segment.horizontal and segment.length >= 4.0
			]
			vertical = [
				segment
				for segment in component
				if segment.vertical and segment.length >= 4.0
			]
			if len(horizontal) < 4 or len(vertical) < 4:
				continue
			vertical_groups = self._partial_vertical_groups(vertical)
			persistent = [
				group
				for group in vertical_groups
				if len(group["segments"]) >= 2
				and group["coverage"] >= 0.68
				and group["span"] >= max(42.0, page_height * 0.055)
			]
			if len(persistent) < 2:
				continue
			maximum_vertical_span = max(group["span"] for group in persistent)
			persistent = [
				group
				for group in persistent
				if group["span"] >= maximum_vertical_span * 0.55
			]
			if len(persistent) < 2:
				continue
			anchors = [
				group
				for group in persistent
				if group["span"] >= maximum_vertical_span * 0.85
			]
			if len(anchors) < 2:
				continue
			y0 = median([group["y0"] for group in anchors])
			y1 = median([group["y1"] for group in anchors])
			if y1 - y0 < max(48.0, page_height * 0.06):
				continue

			horizontal_groups = self._artifact_horizontal_groups(horizontal)
			long_horizontal = sorted(
				[
					group
					for group in horizontal_groups
					if y0 - 3.0 <= group["y"] <= y1 + 3.0
				],
				key=lambda group: group["x1"] - group["x0"],
				reverse=True,
			)
			if len(long_horizontal) < 3:
				continue
			maximum_horizontal_span = (
				long_horizontal[0]["x1"] - long_horizontal[0]["x0"]
			)
			outer_evidence = [
				group
				for group in long_horizontal
				if group["x1"] - group["x0"] >= maximum_horizontal_span * 0.85
				and self._partial_interval_coverage(
					group["intervals"],
					group["x0"],
					group["x1"],
				) >= 0.88
			]
			if len(outer_evidence) < 2:
				continue
			outer_x0 = median([group["x0"] for group in outer_evidence])
			outer_x1 = median([group["x1"] for group in outer_evidence])
			if outer_x1 - outer_x0 < max(120.0, page_width * 0.25):
				continue

			xs = sorted(float(group["x"]) for group in persistent)
			if abs(xs[0] - outer_x0) <= 5.0:
				xs[0] = float((xs[0] + outer_x0) / 2.0)
			elif outer_x0 < xs[0] - 12.0:
				xs.insert(0, float(outer_x0))
			if abs(xs[-1] - outer_x1) <= 5.0:
				xs[-1] = float((xs[-1] + outer_x1) / 2.0)
			elif outer_x1 > xs[-1] + 12.0:
				known_widths = [
					right - left
					for left, right in zip(xs, xs[1:])
					if right - left >= 12.0
				]
				new_width = outer_x1 - xs[-1]
				if (
					known_widths
					and median(known_widths) * 0.55
						<= new_width
						<= median(known_widths) * 1.75
					and (
						outer_x1 >= page_width * 0.96
						or sum(
							abs(group["x1"] - outer_x1) <= 4.0
							for group in outer_evidence
						) >= 2
					)
				):
					xs.append(float(outer_x1))
			columns = len(xs) - 1
			if not (2 <= columns <= 12):
				continue
			if any(right - left < 12.0 for left, right in zip(xs, xs[1:])):
				continue
			if xs[-1] - xs[0] > page_width * 0.98:
				continue

			eligible_horizontal = []
			for group in horizontal_groups:
				if not (y0 - 3.0 <= group["y"] <= y1 + 3.0):
					continue
				overall_coverage = self._partial_interval_coverage(
					group["intervals"],
					xs[0],
					xs[-1],
				)
				cell_coverage = max(
					self._partial_interval_coverage(
						group["intervals"],
						left,
						right,
					)
					for left, right in zip(xs, xs[1:])
				)
				if overall_coverage >= 0.45 and cell_coverage >= 0.72:
					eligible_horizontal.append(group)
			ys = cluster_values(
				[group["y"] for group in eligible_horizontal],
				2.0,
			)
			if len(ys) < 4 or len(ys) > 81:
				continue
			if abs(ys[0] - y0) > 4.0 or abs(ys[-1] - y1) > 4.0:
				continue
			rows = len(ys) - 1
			if rows < 3 or any(
				bottom - top < 8.0
				for top, bottom in zip(ys, ys[1:])
			):
				continue
			if any(
				self._artifact_horizontal_edge_coverage(
					eligible_horizontal,
					y,
					xs[0],
					xs[-1],
				) < 0.82
				for y in (ys[0], ys[-1])
			):
				continue

			table_lines = [
				line
				for line in page_lines
				if any(
					char.text.strip()
					and xs[0] - 2.0
						<= (char.x0 + char.x1) / 2.0
						<= xs[-1] + 2.0
					and ys[0] - 2.0
						<= (char.y0 + char.y1) / 2.0
						<= ys[-1] + 2.0
					for char in line.chars
				)
			]
			visible_chars = [
				char
				for line in table_lines
				for char in line.chars
				if char.text.strip()
				and xs[0] - 2.0
					<= (char.x0 + char.x1) / 2.0
					<= xs[-1] + 2.0
				and ys[0] - 2.0
					<= (char.y0 + char.y1) / 2.0
					<= ys[-1] + 2.0
			]
			if (
				not visible_chars
				or sum(not char.artifact for char in visible_chars)
					< len(visible_chars) * 0.90
			):
				continue
			occupancy = self._partial_grid_occupancy(table_lines, xs, ys)
			if len(occupancy) != rows or any(not row for row in occupancy):
				continue
			covered_columns = {
				column
				for row in occupancy
				for column in row
			}
			if len(covered_columns) < 2 or len(occupancy[0]) < 2:
				continue
			if (
				sum(len(row) >= 2 for row in occupancy) < 2
				and rows < 5
			):
				continue

			cell_occupancy = [
				[column in row for column in range(columns)]
				for row in occupancy
			]
			header_rows = self._artifact_lattice_header_rows(
				table_lines,
				xs,
				ys,
			)
			continuous_header_colspans = (
				self._artifact_continuous_header_colspans(
					table_lines,
					xs,
					ys,
					vertical,
					eligible_horizontal,
					header_rows,
				)
			)
			spans: Dict[Tuple[int, int], Tuple[int, int]] = {
				(row, 0): (1, columns)
				for row in continuous_header_colspans
			}
			covered_cells: set[Tuple[int, int]] = {
				(row, column)
				for row in continuous_header_colspans
				for column in range(1, columns)
			}
			for row in range(rows):
				for column in range(columns):
					if (
						(row, column) in covered_cells
						or (row, column) in spans
						or not cell_occupancy[row][column]
					):
						continue
					colspan = 1
					while column + colspan < columns:
						boundary = xs[column + colspan]
						if self._artifact_vertical_edge_coverage(
							vertical,
							boundary,
							ys[row],
							ys[row + 1],
						) >= 0.45:
							break
						if any(
							cell_occupancy[row][peer]
							for peer in range(column + 1, column + colspan + 1)
						):
							break
						colspan += 1
					rowspan = 1
					while row + rowspan < rows:
						boundary = ys[row + rowspan]
						if self._artifact_horizontal_edge_coverage(
							eligible_horizontal,
							boundary,
							xs[column],
							xs[column + colspan],
						) >= 0.45:
							break
						if any(
							cell_occupancy[row + rowspan][peer]
							for peer in range(column, column + colspan)
						) and self._artifact_fragmented_band_has_distinct_text(
							table_lines,
							xs[column],
							xs[column + colspan],
							boundary,
							ys[row + rowspan + 1],
						):
							break
						rowspan += 1
					if rowspan > 1 or colspan > 1:
						spans[(row, column)] = (rowspan, colspan)
						for covered_row in range(row, row + rowspan):
							for covered_column in range(column, column + colspan):
								if covered_row != row or covered_column != column:
									covered_cells.add((covered_row, covered_column))

			box = (
				float(xs[0]),
				float(ys[0]),
				float(xs[-1]),
				float(ys[-1]),
			)
			html = self._render_partial_grid_html(
				page,
				xs,
				ys,
				table_lines,
				"",
				header_rows_override=header_rows,
				explicit_spans=spans,
			)
			self._partial_table_models[(page, box)] = {
				"model_kind": "artifact_fragmented_lattice",
				"xs": list(xs),
				"ys": list(ys),
				"header_rows": header_rows,
				"spans": [
					{
						"row": row,
						"col": column,
						"rowspan": rowspan,
						"colspan": colspan,
						**(
							{
								"evidence_kind": "artifact_continuous_text_colspan",
								"confidence": 0.97,
							}
							if row in continuous_header_colspans and column == 0
							else {}
						),
					}
					for (row, column), (rowspan, colspan) in sorted(spans.items())
				],
				"evidence": {
					"artifact_geometry_only": True,
					"artifact_rule_rectangles": len(component),
					"persistent_vertical_boundaries": len(persistent),
					"horizontal_row_boundaries": len(ys),
					"inferred_outer_boundaries": (
						len(xs) - len(persistent)
					),
					"physical_spans": len(spans),
					"continuous_text_colspans": [
						continuous_header_colspans[row]
						for row in sorted(continuous_header_colspans)
					],
					"authored_glyph_ratio": (
						sum(not char.artifact for char in visible_chars)
						/ len(visible_chars)
					),
				},
			}
			out.append((float(ys[0]), html, table_lines, box))
		return out

	def _artifact_horizontal_groups(
		self,
		segments: Sequence[Segment],
	) -> List[Dict[str, Any]]:
		groups: List[List[Segment]] = []
		for segment in sorted(
			segments,
			key=lambda item: (
				(item.y0 + item.y1) / 2.0,
				min(item.x0, item.x1),
			),
		):
			y = (segment.y0 + segment.y1) / 2.0
			if groups:
				previous_y = median(
					[(item.y0 + item.y1) / 2.0 for item in groups[-1]]
				)
			else:
				previous_y = -math.inf
			if groups and abs(y - previous_y) <= 2.0:
				groups[-1].append(segment)
			else:
				groups.append([segment])
		out: List[Dict[str, Any]] = []
		for group in groups:
			intervals = [
				tuple(sorted((segment.x0, segment.x1)))
				for segment in group
			]
			out.append(
				{
					"y": median(
						[(segment.y0 + segment.y1) / 2.0 for segment in group]
					),
					"x0": min(interval[0] for interval in intervals),
					"x1": max(interval[1] for interval in intervals),
					"intervals": intervals,
					"segments": group,
				}
			)
		return out

	def _artifact_vertical_edge_coverage(
		self,
		segments: Sequence[Segment],
		x: float,
		y0: float,
		y1: float,
	) -> float:
		return self._partial_interval_coverage(
			[
				(max(y0, min(segment.y0, segment.y1)), min(y1, max(segment.y0, segment.y1)))
				for segment in segments
				if abs((segment.x0 + segment.x1) / 2.0 - x) <= 2.5
				and max(segment.y0, segment.y1) > y0
				and min(segment.y0, segment.y1) < y1
			],
			y0,
			y1,
		)

	def _artifact_horizontal_edge_coverage(
		self,
		groups: Sequence[Dict[str, Any]],
		y: float,
		x0: float,
		x1: float,
	) -> float:
		matching = [group for group in groups if abs(group["y"] - y) <= 2.5]
		if not matching:
			return 0.0
		return max(
			self._partial_interval_coverage(group["intervals"], x0, x1)
			for group in matching
		)

	def _artifact_fragmented_band_has_distinct_text(
		self,
		lines: Sequence[Line],
		x0: float,
		x1: float,
		boundary: float,
		band_end: float,
	) -> bool:
		"""Distinguish a populated next cell from boundary-straddling wrap text.

		The occupancy grid deliberately uses a two-point boundary tolerance so a
		baseline cannot disappear through rounding. That makes a wrapped line
		whose centre sits just below a boundary appear in both adjacent rows.
		Only glyphs strictly beyond that shared tolerance are independent evidence
		for a populated downstream cell and therefore veto a rowspan.
		"""
		return any(
			char.text.strip()
			and x0 <= (char.x0 + char.x1) / 2.0 <= x1
			and boundary + 2.0 < (char.y0 + char.y1) / 2.0 <= band_end + 2.0
			for line in lines
			for char in line.chars
		)

	def _artifact_lattice_header_rows(
		self,
		lines: Sequence[Line],
		xs: Sequence[float],
		ys: Sequence[float],
	) -> int:
		first_band = [
			char
			for line in lines
			for char in line.chars
			if char.text.strip()
			and xs[0] - 2.0 <= (char.x0 + char.x1) / 2.0 <= xs[-1] + 2.0
			and ys[0] - 2.0 <= (char.y0 + char.y1) / 2.0 <= ys[1] + 2.0
		]
		if first_band:
			th_count = sum(
				self._marked_cell_identity(char, {"TH"}) is not None
				for char in first_band
			)
			td_count = sum(
				self._marked_cell_identity(char, {"TD"}) is not None
				for char in first_band
			)
			threshold = math.ceil(len(first_band) * 0.80)
			if th_count >= threshold:
				return 1
			# Explicit data-cell ownership is stronger than a boldness-only
			# fallback.  This prevents the first bold data row of a headerless
			# worksheet from being promoted to a semantic header.
			if td_count >= threshold:
				return 0
		detected = self._table_header_rows(list(lines), list(ys))
		if detected:
			return detected
		columns = len(xs) - 1
		rows = len(ys) - 1
		if columns >= 3 and rows >= 3:
			def cell_is_header(row: int, column: int) -> bool:
				chars = [
					char
					for line in lines
					for char in line.chars
					if char.text.strip()
					and xs[column] - 2.0
						<= (char.x0 + char.x1) / 2.0
						<= xs[column + 1] + 2.0
					and ys[row] - 2.0
						<= (char.y0 + char.y1) / 2.0
						<= ys[row + 1] + 2.0
				]
				return bool(chars) and sum(
					self._marked_cell_identity(char, {"TH"}) is not None
					for char in chars
				) >= math.ceil(len(chars) * 0.60)

			first_row_headers = sum(
				cell_is_header(0, column) for column in range(columns)
			)
			body_row_headers = sum(
				cell_is_header(row, 0) for row in range(1, rows)
			)
			body_data_headers = sum(
				cell_is_header(row, column)
				for row in range(1, rows)
				for column in range(1, columns)
			)
			if (
				first_row_headers >= columns - 1
				and body_row_headers >= rows - 1
				and body_data_headers == 0
			):
				return 1
		return 0

	@staticmethod
	def _artifact_header_has_stable_column_labels(
		row_lines: Sequence[Line],
		xs: Sequence[float],
		local_size: float,
		minimum_lines: int,
	) -> bool:
		"""Recognize repeated per-column anchors inside a false header grid.

		A producer can put three independent labels in one marked-content span.
		Small gaps alone must not launder those labels into one colspan.  Stable
		left anchors in every inherited column distinguish that pattern from a
		wrapped paragraph whose word positions drift between baselines.
		"""
		anchors: List[List[float]] = [[] for _ in range(len(xs) - 1)]
		for line in row_lines:
			words = word_boxes(line)
			for column, (left, right) in enumerate(zip(xs, xs[1:])):
				owned = [
					word for word in words
					if left <= (word[1] + word[3]) / 2.0 <= right
				]
				if owned:
					anchors[column].append(min(word[1] for word in owned))
		tolerance = max(2.0, local_size * 0.25)
		return all(
			len(column_anchors) >= minimum_lines
			and max(column_anchors) - min(column_anchors) <= tolerance
			for column_anchors in anchors
		)

	def _artifact_continuous_header_colspans(
		self,
		lines: Sequence[Line],
		xs: Sequence[float],
		ys: Sequence[float],
		vertical: Sequence[Segment],
		horizontal_groups: Sequence[Dict[str, Any]],
		header_rows: int,
	) -> Dict[int, Dict[str, Any]]:
		"""Find a populated full-width header that a false grid partitions.

		A missing divider is ordinarily interpreted as a span only when covered
		neighbours are empty.  One valid exception is a wrapped header paragraph
		that continuously crosses every absent internal divider while one verified
		marked-content identity owns the complete row.  Stable dividers in the
		remaining rows establish the inherited grid; the marked identity and small
		cross-boundary word gaps distinguish continuous prose from separate labels.
		"""
		columns = len(xs) - 1
		rows = len(ys) - 1
		if header_rows <= 0 or columns < 2 or rows < 3:
			return {}
		out: Dict[int, Dict[str, Any]] = {}
		for row in range(min(header_rows, rows)):
			top, bottom = ys[row], ys[row + 1]
			boundary_coverages = {
				boundary: self._artifact_vertical_edge_coverage(
					vertical,
					boundary,
					top,
					bottom,
				)
				for boundary in xs[1:-1]
			}
			if not boundary_coverages or any(
				coverage >= 0.15 for coverage in boundary_coverages.values()
			):
				continue

			peer_support: Dict[float, int] = {}
			peer_required = max(2, math.ceil((rows - 1) * 0.75))
			for boundary in xs[1:-1]:
				peer_coverages = [
					self._artifact_vertical_edge_coverage(
						vertical,
						boundary,
						ys[peer],
						ys[peer + 1],
					)
					for peer in range(rows)
					if peer != row
				]
				peer_support[boundary] = sum(
					coverage >= 0.80 for coverage in peer_coverages
				)
				if peer_support[boundary] < peer_required:
					break
			else:
				if (
					self._artifact_horizontal_edge_coverage(
						horizontal_groups,
						top,
						xs[0],
						xs[-1],
					) < 0.82
					or self._artifact_horizontal_edge_coverage(
						horizontal_groups,
						bottom,
						xs[0],
						xs[-1],
					) < 0.82
				):
					continue

				row_lines = []
				row_chars: List[Char] = []
				for line in lines:
					chars = [
						char
						for char in line.chars
						if char.text.strip()
						and xs[0] - 2.0
							<= (char.x0 + char.x1) / 2.0
							<= xs[-1] + 2.0
						and top - 2.0
							<= (char.y0 + char.y1) / 2.0
							<= bottom + 2.0
					]
					if chars:
						row_lines.append(line)
						row_chars.extend(chars)
				if len(row_lines) < 2 or not row_chars or any(
					char.artifact for char in row_chars
				):
					continue
				identities = {
					self._marked_cell_identity(char)
					for char in row_chars
				}
				if None in identities or len(identities) != 1:
					continue
				identity = next(iter(identities))
				assert identity is not None
				if any(
					char.text.strip()
					and not char.artifact
					and self._marked_cell_identity(char) == identity
					and not (
						top - 2.0
						<= (char.y0 + char.y1) / 2.0
						<= bottom + 2.0
					)
					for line in lines
					for char in line.chars
				):
					continue

				local_size = median([
					char.size for char in row_chars if char.size > 0
				])
				maximum_word_gap = max(4.0, local_size * 0.65)
				required_crossings = max(2, math.ceil(len(row_lines) * 0.60))
				if self._artifact_header_has_stable_column_labels(
					row_lines,
					xs,
					local_size,
					required_crossings,
				):
					continue
				crossing_counts: Dict[float, int] = {}
				crossing_gaps: Dict[float, Tuple[float, ...]] = {}
				for boundary in xs[1:-1]:
					accepted_gaps: List[float] = []
					for line in row_lines:
						words = [
							word
							for word in word_boxes(line)
							if word[3] >= xs[0] - 2.0
							and word[1] <= xs[-1] + 2.0
						]
						if not words:
							continue
						if any(word[1] < boundary < word[3] for word in words):
							gap = 0.0
						else:
							left_edges = [
								word[3] for word in words if word[3] <= boundary
							]
							right_edges = [
								word[1] for word in words if word[1] >= boundary
							]
							if not left_edges or not right_edges:
								continue
							gap = min(right_edges) - max(left_edges)
						if 0.0 <= gap <= maximum_word_gap:
							accepted_gaps.append(gap)
					crossing_counts[boundary] = len(accepted_gaps)
					crossing_gaps[boundary] = tuple(accepted_gaps)
					if len(accepted_gaps) < required_crossings:
						break
				else:
					out[row] = {
						"row": row,
						"evidence_kind": "artifact_continuous_text_colspan",
						"confidence": 0.97,
						"boundary_coverages": {
							str(round(boundary, 3)): coverage
							for boundary, coverage in boundary_coverages.items()
						},
						"stable_peer_rows": {
							str(round(boundary, 3)): count
							for boundary, count in peer_support.items()
						},
						"shared_marked_identity": {
							"tag": identity[0],
							"mcid": identity[1],
						},
						"wrapped_line_count": len(row_lines),
						"required_crossing_lines": required_crossings,
						"crossing_line_counts": {
							str(round(boundary, 3)): count
							for boundary, count in crossing_counts.items()
						},
						"crossing_word_gaps": {
							str(round(boundary, 3)): gaps
							for boundary, gaps in crossing_gaps.items()
						},
					}
		return out

	def _dense_fragmented_grid_candidates(
		self,
		page: int,
	) -> List[
		Tuple[
			float,
			str,
			List[Line],
			Tuple[float, float, float, float],
		]
	]:
		"""Recover a dense page-top grid drawn as repeated vertical fragments.

		The candidate has no caption to act as a semantic prior, so geometry is
		intentionally much stronger than for the captioned partial-grid path: at
		least five independently fragmented internal boundaries, two matching
		full-width horizontal bookends, six dense multi-column rows, and repeated
		numeric body anchors.  Header spans are derived only inside the header
		bands delimited by the first full-width rule.  Missing horizontal body
		rules never imply rowspans.
		"""
		lines = self.lines_by_page.get(page, [])
		page_width, page_height = self.conv.page_sizes.get(page, (612.0, 792.0))
		segments = [
			segment
			for segment in self.conv.segments
			if segment.page == page and (segment.horizontal or segment.vertical)
		]
		vertical = [
			segment
			for segment in segments
			if segment.vertical and segment.length > 5.0
		]
		if len(vertical) < 30:
			return []
		vertical_groups = [
			group
			for group in self._partial_vertical_groups(vertical)
			if len(group["segments"]) >= 6
			and group["coverage"] >= 0.92
			and group["span"] >= max(90.0, page_height * 0.20)
		]
		if len(vertical_groups) < 5:
			return []
		out: List[
			Tuple[
				float,
				str,
				List[Line],
				Tuple[float, float, float, float],
			]
		] = []
		used_group_sets: set[Tuple[int, ...]] = set()
		for anchor in sorted(
			vertical_groups,
			key=lambda group: (-group["span"], group["x"]),
		):
			selected = [
				group
				for group in vertical_groups
				if self._partial_span_overlap(group, anchor) >= 0.84
			]
			selected.sort(key=lambda group: group["x"])
			if not (5 <= len(selected) <= 11):
				continue
			group_key = tuple(
				id(group) for group in selected
			)
			if group_key in used_group_sets:
				continue
			used_group_sets.add(group_key)
			internal_xs = [float(group["x"]) for group in selected]
			if any(
				right - left < 24.0
				for left, right in zip(internal_xs, internal_xs[1:])
			):
				continue
			y0 = min(group["y0"] for group in selected)
			y1 = max(group["y1"] for group in selected)
			if y0 > page_height * 0.32:
				continue
			preceding_lines = [
				line
				for line in lines
				if line.y1 < y0 - 2.0
				and plain_text(line_text_tokens(line)).strip()
			]
			if preceding_lines:
				continue
			horizontal_groups = self._partial_horizontal_groups(
				page,
				y0,
				y1,
				0.0,
				page_width,
			)
			wide_groups = [
				group
				for group in horizontal_groups
				if group["x0"] <= internal_xs[0] - 12.0
				and group["x1"] >= internal_xs[-1] + 12.0
				and self._partial_interval_coverage(
					group["intervals"],
					group["x0"],
					group["x1"],
				) >= 0.94
			]
			if len(wide_groups) < 2:
				continue
			x0 = median([float(group["x0"]) for group in wide_groups])
			x1 = median([float(group["x1"]) for group in wide_groups])
			if (
				x1 - x0 < max(240.0, page_width * 0.55)
				or max(group["x0"] for group in wide_groups)
					- min(group["x0"] for group in wide_groups) > 3.0
				or max(group["x1"] for group in wide_groups)
					- min(group["x1"] for group in wide_groups) > 3.0
			):
				continue
			xs = [float(x0), *internal_xs, float(x1)]
			if any(right - left < 20.0 for left, right in zip(xs, xs[1:])):
				continue
			endpoint_ys = self._partial_shared_vertical_endpoints(selected)
			ys = cluster_values([y0, y1, *endpoint_ys], 2.0)
			ys = [value for value in ys if y0 - 2.0 <= value <= y1 + 2.0]
			if not (8 <= len(ys) - 1 <= 40) or any(
				right - left < 8.0 for left, right in zip(ys, ys[1:])
			):
				continue
			first_full_rule = min(wide_groups, key=lambda group: group["y"])
			header_boundary = min(
				range(1, len(ys) - 1),
				key=lambda index: abs(ys[index] - first_full_rule["y"]),
			)
			if (
				abs(ys[header_boundary] - first_full_rule["y"]) > 3.0
				or not (1 <= header_boundary <= 3)
			):
				continue
			nearby_captions = [
				line
				for line in lines
				if self._is_explicit_table_caption(
					plain_text(line_text_tokens(line)).strip()
				)
				and y0 - 72.0 <= line.y0 <= y1 + 72.0
			]
			if nearby_captions:
				continue
			region_lines = [
				line
				for line in lines
				if y0 - 2.0 <= (line.y0 + line.y1) / 2.0 <= y1 + 2.0
				and line.x1 >= x0 - 2.0
				and line.x0 <= x1 + 2.0
			]
			occupancy = self._partial_grid_occupancy(region_lines, xs, ys)
			columns = len(xs) - 1
			if (
				len(occupancy) != len(ys) - 1
				or sum(
					len(row) >= math.ceil(columns * 0.80)
					for row in occupancy[header_boundary:]
				) < 6
				or set().union(*occupancy) != set(range(columns))
			):
				continue
			numeric_rows = 0
			for row_index in range(header_boundary, len(ys) - 1):
				numeric_columns: set[int] = set()
				for line in region_lines:
					for word in word_boxes(line):
						cx = (word[1] + word[3]) / 2.0
						cy = (word[2] + word[4]) / 2.0
						if (
							ys[row_index] - 2.0 <= cy <= ys[row_index + 1] + 2.0
							and self._booktabs_numeric_token(word[0])
						):
							numeric_columns.add(
								max(0, min(columns - 1, find_interval(xs, cx)))
							)
				if len(numeric_columns) >= 3:
					numeric_rows += 1
			if numeric_rows < 4:
				continue
			# A complete physical lattice is a stronger source of truth and is
			# already handled by the primary detector.
			complete_overlap = False
			for component in segment_components(segments):
				hs = [segment for segment in component if segment.horizontal]
				vs = [segment for segment in component if segment.vertical]
				component_xs = cluster_values(
					[(segment.x0 + segment.x1) / 2.0 for segment in vs],
					2.0,
				)
				component_ys = cluster_values(
					[(segment.y0 + segment.y1) / 2.0 for segment in hs],
					2.0,
				)
				if (
					len(component_xs) >= 2
					and len(component_ys) >= 2
					and component_xs[0] <= x1
					and component_xs[-1] >= x0
					and component_ys[0] <= y1
					and component_ys[-1] >= y0
					and lattice_has_all_cell_edges(
						component_xs,
						component_ys,
						hs,
						vs,
					)
				):
					complete_overlap = True
					break
			if complete_overlap:
				continue
			spans = self._fragmented_header_spans(
				page,
				xs,
				ys,
				header_boundary,
			)
			box = (
				float(xs[0]),
				float(ys[0]),
				float(xs[-1]),
				float(ys[-1]),
			)
			html = self._render_partial_grid_html(
				page,
				xs,
				ys,
				region_lines,
				"",
				header_rows_override=header_boundary,
				explicit_spans=spans,
			)
			self._partial_table_models[(page, box)] = {
				"model_kind": "dense_fragmented_grid",
				"xs": list(xs),
				"ys": list(ys),
				"header_rows": header_boundary,
				"spans": [
					{
						"row": row,
						"col": column,
						"rowspan": rowspan,
						"colspan": colspan,
					}
					for (row, column), (rowspan, colspan) in sorted(spans.items())
				],
				"evidence": {
					"page_top": True,
					"fragmented_vertical_boundaries": len(selected),
					"full_width_horizontal_boundaries": len(wide_groups),
					"dense_rows": sum(
						len(row) >= math.ceil(columns * 0.80)
						for row in occupancy[header_boundary:]
					),
					"numeric_body_rows": numeric_rows,
					"physical_header_spans": len(spans),
				},
			}
			out.append((float(ys[0]), html, region_lines, box))
		return out

	def _fragmented_header_spans(
		self,
		page: int,
		xs: Sequence[float],
		ys: Sequence[float],
		header_rows: int,
	) -> Dict[Tuple[int, int], Tuple[int, int]]:
		"""Infer physical spans only inside a delimited header region."""
		columns = len(xs) - 1
		occupied: set[Tuple[int, int]] = set()
		spans: Dict[Tuple[int, int], Tuple[int, int]] = {}
		for row in range(header_rows):
			for column in range(columns):
				if (row, column) in occupied:
					continue
				colspan = 1
				while (
					column + colspan < columns
					and not self._table_has_vertical_edge(
						page,
						xs[column + colspan],
						ys[row],
						ys[row + 1],
					)
				):
					colspan += 1
				rowspan = 1
				while (
					row + rowspan < header_rows
					and not self._table_has_horizontal_edge(
						page,
						ys[row + rowspan],
						xs[column],
						xs[column + colspan],
					)
				):
					rowspan += 1
				for covered_row in range(row, row + rowspan):
					for covered_column in range(column, column + colspan):
						occupied.add((covered_row, covered_column))
				if rowspan > 1 or colspan > 1:
					spans[(row, column)] = (rowspan, colspan)
		return spans

	def _overlaid_image_table_candidates(
		self,
		page: int,
	) -> List[
		Tuple[
			float,
			str,
			List[Line],
			Tuple[float, float, float, float],
		]
	]:
		"""Recover a complete text overlay arranged over a table image.

		The raster is used only as an independent region boundary; every cell is
		built from PDF text-layer glyphs.  Admission requires a four-run header,
		a repeated bold function-name band, and dense occupancy in the remaining
		three columns.  No pixel content or OCR output participates.
		"""
		lines = self.lines_by_page.get(page, [])
		page_width, page_height = self.conv.page_sizes.get(page, (612.0, 792.0))
		region_images = [
			image
			for image in self.conv.images
			if image.page == page
			and 0.65 <= (image.x1 - image.x0) / max(1.0, page_width) <= 0.97
			and 0.35 <= (image.y1 - image.y0) / max(1.0, page_height) <= 0.85
			and image.x0 > 2.0
			and image.y0 > 2.0
		]
		out: List[
			Tuple[
				float,
				str,
				List[Line],
				Tuple[float, float, float, float],
			]
		] = []
		for image in sorted(
			region_images,
			key=lambda item: -((item.x1 - item.x0) * (item.y1 - item.y0)),
		):
			overlay_lines = [
				line
				for line in lines
				if any(
					char.text.strip()
					and image.x0 <= (char.x0 + char.x1) / 2.0 <= image.x1
					and image.y0 <= (char.y0 + char.y1) / 2.0 <= image.y1
					for char in line.chars
				)
			]
			if len(overlay_lines) < 16:
				continue
			header_options = [
				(line, self._booktabs_cell_runs(line, image.x0, image.x1))
				for line in overlay_lines
				if line.y0 <= image.y0 + (image.y1 - image.y0) * 0.12
			]
			header_options = [
				(line, runs)
				for line, runs in header_options
				if len(runs) == 4
				and all(run[2] - run[1] >= line.size * 1.5 for run in runs)
			]
			if not header_options:
				continue
			header_line, header_runs = min(
				header_options,
				key=lambda item: (item[0].y0, item[0].seq),
			)
			margin = max(3.0, header_line.size * 0.70)
			xs = [
				float(image.x0),
				*[float(run[1] - margin) for run in header_runs[1:]],
				float(image.x1),
			]
			if (
				len(xs) != 5
				or any(right - left < 60.0 for left, right in zip(xs, xs[1:]))
			):
				continue

			function_fragments: List[Line] = []
			for line in overlay_lines:
				if line.y0 <= header_line.y1 + 3.0:
					continue
				selected = [
					char
					for char in line.chars
					if char.text.strip()
					and xs[1] <= (char.x0 + char.x1) / 2.0 <= xs[2]
				]
				if not selected:
					continue
				if sum(char.bold for char in selected) < math.ceil(len(selected) * 0.65):
					continue
				function_fragments.append(
					Line(
						selected,
						line.page,
						line.seq,
						source_order=line.source_order,
						writing_mode=line.writing_mode,
					)
				)
			function_fragments.sort(key=lambda line: (line.y0, line.x0, line.seq))
			if len(function_fragments) < 8:
				continue
			fragment_size = median([line.size for line in function_fragments])
			join_gap = max(13.0, fragment_size * 2.20)
			function_groups: List[List[Line]] = []
			for fragment in function_fragments:
				if (
					function_groups
					and fragment.y0 - function_groups[-1][-1].y0 <= join_gap
				):
					function_groups[-1].append(fragment)
				else:
					function_groups.append([fragment])
			if not (6 <= len(function_groups) <= 16):
				continue
			if any(
				not cleanup_spaces(
					" ".join(
						plain_text(line_text_tokens(fragment)).strip()
						for fragment in group
					)
				)
				for group in function_groups
			):
				continue

			row_anchors = [
				(header_line.y0 + header_line.y1) / 2.0,
				*[ (group[0].y0 + group[0].y1) / 2.0 for group in function_groups ],
			]
			word_centers = cluster_values(
				[
					(word[2] + word[4]) / 2.0
					for line in overlay_lines
					for word in word_boxes(line)
					if image.x0 <= (word[1] + word[3]) / 2.0 <= image.x1
					and header_line.y0 - 2.0 <= (word[2] + word[4]) / 2.0 <= image.y1
				],
				1.5,
			)
			boundaries: List[float] = []
			valid_boundaries = True
			for left_anchor, right_anchor in zip(row_anchors, row_anchors[1:]):
				levels = [
					value
					for value in word_centers
					if left_anchor - 3.0 <= value <= right_anchor + 4.0
				]
				gaps = [
					(following - previous, previous, following)
					for previous, following in zip(levels, levels[1:])
				]
				if not gaps:
					valid_boundaries = False
					break
				gap, previous, following = max(gaps)
				if gap < max(3.0, fragment_size * 0.65):
					valid_boundaries = False
					break
				boundaries.append((previous + following) / 2.0)
			if not valid_boundaries:
				continue
			visible_chars = [
				char
				for line in overlay_lines
				for char in line.chars
				if char.text.strip()
				and image.x0 <= (char.x0 + char.x1) / 2.0 <= image.x1
				and header_line.y0 - 2.0 <= (char.y0 + char.y1) / 2.0 <= image.y1
			]
			if (
				not visible_chars
				or sum(not char.artifact for char in visible_chars)
					< len(visible_chars) * 0.98
				or not all(char.seq > image.seq for char in visible_chars)
			):
				continue
			bottom_y = min(
				float(image.y1),
				max(char.y1 for char in visible_chars) + max(2.0, fragment_size * 0.50),
			)
			ys = [
				max(float(image.y0), header_line.y0 - max(3.0, header_line.size * 0.60)),
				*boundaries,
				bottom_y,
			]
			if len(ys) != len(function_groups) + 2 or any(
				right - left < 7.0 for left, right in zip(ys, ys[1:])
			):
				continue
			region_lines = [
				line
				for line in overlay_lines
				if ys[0] - 2.0 <= (line.y0 + line.y1) / 2.0 <= ys[-1] + 2.0
				and line.x1 >= xs[0]
				and line.x0 <= xs[-1]
			]
			occupancy = self._partial_grid_occupancy(region_lines, xs, ys)
			if (
				len(occupancy) != len(ys) - 1
				or occupancy[0] != set(range(4))
				or sum(len(row) >= 3 for row in occupancy[1:]) < len(occupancy) - 2
				or any(not {1, 2, 3}.issubset(row) for row in occupancy[1:])
			):
				continue
			spans: Dict[Tuple[int, int], Tuple[int, int]] = {}
			first_column_occupied = [0 in row for row in occupancy]
			row = 1
			while row < len(occupancy):
				if first_column_occupied[row]:
					row += 1
					continue
				start = row
				while row < len(occupancy) and not first_column_occupied[row]:
					row += 1
				if (
					row < len(occupancy)
					and row - start >= 2
					and row - start <= 4
				):
					spans[(start, 0)] = (row - start + 1, 1)
					row += 1
			box = (float(xs[0]), float(ys[0]), float(xs[-1]), float(ys[-1]))
			html = self._render_partial_grid_html(
				page,
				xs,
				ys,
				region_lines,
				"",
				header_rows_override=1,
				explicit_spans=spans,
			)
			self._partial_table_models[(page, box)] = {
				"model_kind": "overlaid_image_table",
				"xs": list(xs),
				"ys": list(ys),
				"header_rows": 1,
				"source_object_refs": (
					[image.object_ref] if image.object_ref else []
				),
				"source_bbox": [image.x0, image.y0, image.x1, image.y1],
				"spans": [
					{
						"row": span_row,
						"col": column,
						"rowspan": rowspan,
						"colspan": colspan,
						"evidence_kind": "geometry_inferred_table_span",
						"confidence": 0.88,
					}
					for (span_row, column), (rowspan, colspan) in sorted(spans.items())
				],
				"evidence": {
					"image_region_geometry_only": True,
					"raster_text_used": False,
					"image_painted_before_text": True,
					"image_object_ref": image.object_ref,
					"image_bbox": [image.x0, image.y0, image.x1, image.y1],
					"image_asset": image.name,
					"text_overlay_glyphs": len(visible_chars),
					"stable_columns": 4,
					"function_rows": len(function_groups),
					"dense_rows": sum(len(row) >= 3 for row in occupancy[1:]),
					"geometry_inferred_rowspans": len(spans),
					"rowspan_inference": "bounded_first_column_glyph_occupancy",
				},
			}
			out.append((float(ys[0]), html, region_lines, box))
		return out

	def _dense_two_by_two_artifact_table(
		self,
		lines: Sequence[Line],
		xs: Sequence[float],
		ys: Sequence[float],
		page_width: float,
		page_height: float,
	) -> bool:
		if (
			xs[-1] - xs[0] < max(180.0, page_width * 0.35)
			or ys[-1] - ys[0] < max(72.0, page_height * 0.09)
		):
			return False
		cells: Dict[Tuple[int, int], List[Char]] = {
			(row, column): []
			for row in range(2)
			for column in range(2)
		}
		line_hits: Dict[Tuple[int, int], set[int]] = {
			key: set() for key in cells
		}
		for line in lines:
			for char in line.chars:
				if not char.text.strip():
					continue
				cx = (char.x0 + char.x1) / 2.0
				cy = (char.y0 + char.y1) / 2.0
				if not (xs[0] - 2 <= cx <= xs[-1] + 2 and ys[0] - 2 <= cy <= ys[-1] + 2):
					continue
				row = max(0, min(1, find_interval(list(ys), cy)))
				column = max(0, min(1, find_interval(list(xs), cx)))
				cells[(row, column)].append(char)
				line_hits[(row, column)].add(id(line))
		header_cells = [cells[(0, column)] for column in range(2)]
		if any(not cell for cell in header_cells):
			return False
		bold_header = all(
			sum(char.bold for char in cell) >= math.ceil(len(cell) * 0.65)
			for cell in header_cells
		)
		marked_header = all(
			sum(
				self._marked_cell_identity(char, {"TH"}) is not None
				for char in cell
			) >= math.ceil(len(cell) * 0.80)
			for cell in header_cells
		)
		body_cells = [cells[(1, column)] for column in range(2)]
		body_size = median(
			[char.size for cell in body_cells for char in cell]
		)
		return (
			(bold_header or marked_header)
			and all(len(cell) >= 60 for cell in body_cells)
			and all(len(line_hits[(1, column)]) >= 3 for column in range(2))
			and ys[2] - ys[1] >= max(48.0, body_size * 5.0)
		)

	def _marked_cell_identity(
		self,
		char: Char,
		roles: Optional[set[str]] = None,
	) -> Optional[Tuple[str, int]]:
		for mark in reversed(char.mc):
			tag = str(mark.get("tag") or "").lstrip("/")
			mcid = mark.get("mcid")
			if (
				isinstance(mcid, int)
				and not isinstance(mcid, bool)
				and (roles is None or tag in roles)
			):
				return (tag, mcid)
		return None

	def _captioned_marked_bookend_candidates(
		self,
		page: int,
	) -> List[
		Tuple[
			float,
			str,
			List[Line],
			Tuple[float, float, float, float],
		]
	]:
		"""Recover a two-column tagged table bounded only by horizontal rules.

		This path intentionally requires independent PDF-native signals: an
		explicit caption after the block, two matching artifact bookend rules, a
		dark artifact header background, one marked TH run spanning the table,
		and at least four regularly aligned body rows made from adjacent marked
		cell runs.  No text literal participates in admission.
		"""
		page_lines = self.lines_by_page.get(page, [])
		captions = [
			(line, plain_text(line_text_tokens(line)).strip())
			for line in page_lines
			if self._is_explicit_table_caption(
				plain_text(line_text_tokens(line)).strip()
			)
		]
		if not captions:
			return []
		rules = sorted(
			[
				segment
				for segment in self.conv._artifact_rule_segments
				if segment.page == page
				and segment.horizontal
				and segment.length >= 100.0
			],
			key=lambda segment: (
				(segment.y0 + segment.y1) / 2.0,
				min(segment.x0, segment.x1),
			),
		)
		out: List[
			Tuple[
				float,
				str,
				List[Line],
				Tuple[float, float, float, float],
			]
		] = []
		for top, bottom in zip(rules, rules[1:]):
			top_y = (top.y0 + top.y1) / 2.0
			bottom_y = (bottom.y0 + bottom.y1) / 2.0
			x0 = min(top.x0, top.x1)
			x1 = max(top.x0, top.x1)
			bottom_x0 = min(bottom.x0, bottom.x1)
			bottom_x1 = max(bottom.x0, bottom.x1)
			if (
				bottom_y - top_y < 48.0
				or bottom_y - top_y > 240.0
				or x1 - x0 < 120.0
				or abs(bottom_x0 - x0) > 3.0
				or abs(bottom_x1 - x1) > 3.0
			):
				continue
			matching_rules = [
				segment
				for segment in self.conv._artifact_rule_segments
				if segment.page == page
				and segment.horizontal
				and top_y - 2.0
					<= (segment.y0 + segment.y1) / 2.0
					<= bottom_y + 2.0
				and abs(min(segment.x0, segment.x1) - x0) <= 3.0
				and abs(max(segment.x0, segment.x1) - x1) <= 3.0
			]
			if len(matching_rules) != 2 or any(
				segment.page == page
				and segment.vertical
				and top_y - 2.0 <= min(segment.y0, segment.y1)
				and max(segment.y0, segment.y1) <= bottom_y + 2.0
				and x0 - 2.0
					<= (segment.x0 + segment.x1) / 2.0
					<= x1 + 2.0
				for segment in self.conv._artifact_rule_segments
			):
				continue
			caption_options = [
				(line, text)
				for line, text in captions
				if 2.0 < line.y0 - bottom_y <= max(60.0, line.size * 8.0)
				and line.x1 >= x0 - 8.0
				and line.x0 <= x1 + 8.0
			]
			if not caption_options:
				continue
			caption_line, caption_text = min(
				caption_options,
				key=lambda item: (item[0].y0 - bottom_y, item[0].seq),
			)
			table_lines = [
				line
				for line in page_lines
				if any(
					char.text.strip()
					and x0 - 2.0
						<= (char.x0 + char.x1) / 2.0
						<= x1 + 2.0
					and top_y - 2.0
						<= (char.y0 + char.y1) / 2.0
						<= bottom_y + 2.0
					for char in line.chars
				)
			]
			if len(table_lines) < 5:
				continue
			marked_rows: List[
				Tuple[Line, List[Dict[str, Any]]]
			] = []
			unmarked = False
			for line in sorted(table_lines, key=lambda item: (item.y0, item.x0, item.seq)):
				groups: Dict[Tuple[str, int], List[Char]] = {}
				for char in line.chars:
					if not char.text.strip():
						continue
					cx = (char.x0 + char.x1) / 2.0
					cy = (char.y0 + char.y1) / 2.0
					if not (x0 - 2.0 <= cx <= x1 + 2.0 and top_y - 2.0 <= cy <= bottom_y + 2.0):
						continue
					identity = self._marked_cell_identity(char)
					if identity is None or char.artifact:
						unmarked = True
						break
					groups.setdefault(identity, []).append(char)
				if unmarked:
					break
				if not groups:
					continue
				runs = [
					{
						"tag": identity[0],
						"mcid": identity[1],
						"chars": chars,
						"x0": min(char.x0 for char in chars),
						"x1": max(char.x1 for char in chars),
						"y0": min(char.y0 for char in chars),
						"y1": max(char.y1 for char in chars),
					}
					for identity, chars in groups.items()
				]
				runs.sort(key=lambda run: (run["x0"], run["mcid"]))
				marked_rows.append((line, runs))
			if unmarked or len(marked_rows) != len(table_lines):
				continue
			header_line, header_runs = marked_rows[0]
			body_rows = marked_rows[1:]
			if (
				len(header_runs) != 1
				or header_runs[0]["tag"] != "TH"
				or len(body_rows) < 4
				or any(len(runs) != 2 for _line, runs in body_rows)
			):
				continue
			if any(
				runs[0]["tag"] not in {"TD", "TH"}
				or runs[1]["tag"] not in {"TD", "Span"}
				or runs[1]["mcid"] != runs[0]["mcid"] + 1
				or runs[0]["x1"] >= runs[1]["x0"]
				for _line, runs in body_rows
			):
				continue
			mcids = [header_runs[0]["mcid"]] + [
				run["mcid"] for _line, runs in body_rows for run in runs
			]
			if (
				len(set(mcids)) != len(mcids)
				or mcids != list(range(mcids[0], mcids[0] + len(mcids)))
			):
				continue
			left_runs = [runs[0] for _line, runs in body_rows]
			right_runs = [runs[1] for _line, runs in body_rows]
			body_size = median(
				[
					char.size
					for run in (*left_runs, *right_runs)
					for char in run["chars"]
				]
			)
			if (
				max(run["x0"] for run in left_runs)
					- min(run["x0"] for run in left_runs)
					> max(2.0, body_size * 0.35)
				or max(run["x0"] for run in right_runs)
					- min(run["x0"] for run in right_runs)
					> max(2.0, body_size * 0.35)
			):
				continue
			left_end = max(run["x1"] for run in left_runs)
			right_start = min(run["x0"] for run in right_runs)
			if right_start - left_end < max(3.0, body_size * 0.50):
				continue
			centers = [
				(header_line.y0 + header_line.y1) / 2.0,
				*[
					(line.y0 + line.y1) / 2.0
					for line, _runs in body_rows
				],
			]
			body_gaps = [
				right - left for left, right in zip(centers[1:], centers[2:])
			]
			row_pitch = median(body_gaps)
			if (
				row_pitch < body_size * 1.20
				or any(
					abs(gap - row_pitch) > max(1.0, row_pitch * 0.12)
					for gap in body_gaps
				)
				or not row_pitch * 0.75
					<= centers[1] - centers[0]
					<= row_pitch * 1.25
			):
				continue
			header_chars = header_runs[0]["chars"]
			backgrounds = [
				fill
				for fill in self.conv._artifact_local_backgrounds
				if fill.page == page
				and fill.seq < min(char.seq for char in header_chars)
				and color_contrast(fill.color, (1.0, 1.0, 1.0)) >= 1.50
				and sum(
					fill.x0 <= (char.x0 + char.x1) / 2.0 <= fill.x1
					and fill.y0 <= (char.y0 + char.y1) / 2.0 <= fill.y1
					for char in header_chars
				) >= math.ceil(len(header_chars) * 0.80)
			]
			if not backgrounds:
				continue
			separator = (left_end + right_start) / 2.0
			xs = [float(x0), float(separator), float(x1)]
			ys = [
				float(top_y),
				*[
					(left + right) / 2.0
					for left, right in zip(centers, centers[1:])
				],
				float(bottom_y),
			]
			if self._partial_grid_occupancy(table_lines, xs, ys) != [
				set(range(2)),
				*[set(range(2)) for _row in body_rows],
			]:
				continue
			box = (float(x0), float(top_y), float(x1), float(bottom_y))
			spans = {(0, 0): (1, 2)}
			html = self._render_partial_grid_html(
				page,
				xs,
				ys,
				table_lines,
				caption_text,
				header_rows_override=1,
				explicit_spans=spans,
			)
			self._partial_table_models[(page, box)] = {
				"model_kind": "captioned_marked_bookend",
				"xs": list(xs),
				"ys": list(ys),
				"header_rows": 1,
				"spans": [
					{
						"row": 0,
						"col": 0,
						"rowspan": 1,
						"colspan": 2,
					}
				],
				"evidence": {
					"caption": caption_text,
					"caption_placement": "after",
					"artifact_bookend_rules": 2,
					"marked_header_mcid": header_runs[0]["mcid"],
					"marked_body_rows": len(body_rows),
					"stable_two_column_alignment": True,
					"artifact_header_background": True,
				},
			}
			out.append(
				(
					float(top_y),
					html,
					[*table_lines, caption_line],
					box,
				)
			)
		return out

	def _open_internal_grid_candidates(
		self,
		page: int,
	) -> List[
		Tuple[
			float,
			str,
			List[Line],
			Tuple[float, float, float, float],
		]
	]:
		"""Recover an open matrix defined only by internal PDF rules.

		Some presentation producers omit all four outer borders while drawing
		each row separator across the full matrix and each internal column
		divider from the header separator to the final body row.  Admission is
		intentionally narrow: exactly three aligned full-width separators,
		exactly three crossing internal dividers, an empty corner header followed
		by three populated header cells, and complete four-cell body occupancy.
		The inferred outer edges and open top/bottom are bounded by the physical
		rules and nearby source glyphs; no expected text participates.
		"""
		lines = self.lines_by_page.get(page, [])
		page_width, page_height = self.conv.page_sizes.get(page, (612.0, 792.0))
		horizontal = [
			segment
			for segment in self.conv.segments
			if segment.page == page
			and segment.horizontal
			and segment.length >= page_width * 0.70
		]
		vertical = [
			segment
			for segment in self.conv.segments
			if segment.page == page and segment.vertical
		]
		if len(horizontal) < 3 or len(vertical) < 3:
			return []
		out: List[
			Tuple[
				float,
				str,
				List[Line],
				Tuple[float, float, float, float],
			]
		] = []
		seen_rule_groups: set[Tuple[int, ...]] = set()
		for anchor in horizontal:
			x0 = min(anchor.x0, anchor.x1)
			x1 = max(anchor.x0, anchor.x1)
			matching = [
				segment
				for segment in horizontal
				if abs(min(segment.x0, segment.x1) - x0) <= 3.0
				and abs(max(segment.x0, segment.x1) - x1) <= 3.0
			]
			matching.sort(key=lambda segment: (segment.y0 + segment.y1) / 2.0)
			if len(matching) != 3:
				continue
			identity = tuple(sorted(id(segment) for segment in matching))
			if identity in seen_rule_groups:
				continue
			seen_rule_groups.add(identity)
			x0 = median([min(segment.x0, segment.x1) for segment in matching])
			x1 = median([max(segment.x0, segment.x1) for segment in matching])
			rule_ys = [
				(segment.y0 + segment.y1) / 2.0 for segment in matching
			]
			row_gaps = [right - left for left, right in zip(rule_ys, rule_ys[1:])]
			if (
				x1 - x0 < page_width * 0.75
				or min(row_gaps) < 45.0
				or max(row_gaps) > page_height * 0.30
			):
				continue
			internal = [
				segment
				for segment in vertical
				if x0 + 20.0 < (segment.x0 + segment.x1) / 2.0 < x1 - 20.0
				and abs(min(segment.y0, segment.y1) - rule_ys[0]) <= 4.0
				and max(segment.y0, segment.y1)
					>= rule_ys[-1] + max(30.0, min(row_gaps) * 0.45)
			]
			internal_xs = cluster_values(
				[(segment.x0 + segment.x1) / 2.0 for segment in internal],
				2.0,
			)
			if len(internal_xs) != 3:
				continue
			if any(
				not any(
					abs((segment.x0 + segment.x1) / 2.0 - column_x) <= 2.0
					and abs(min(segment.y0, segment.y1) - rule_ys[0]) <= 4.0
					and max(segment.y0, segment.y1) >= rule_ys[-1] + 30.0
					for segment in internal
				)
				for column_x in internal_xs
			):
				continue
			xs = [float(x0), *map(float, internal_xs), float(x1)]
			if any(right - left < 65.0 for left, right in zip(xs, xs[1:])):
				continue

			header_chars = [
				char
				for line in lines
				for char in line.chars
				if char.text.strip()
				and x0 <= (char.x0 + char.x1) / 2.0 <= x1
				and rule_ys[0] - 42.0
					<= (char.y0 + char.y1) / 2.0
					< rule_ys[0] - 2.0
			]
			if not header_chars:
				continue
			header_size = median([char.size for char in header_chars])
			top_y = max(
				0.0,
				min(char.y0 for char in header_chars) - max(3.0, header_size * 0.45),
			)
			last_rule_y = rule_ys[-1]
			body_tail_chars = [
				char
				for line in lines
				for char in line.chars
				if char.text.strip()
				and x0 + 8.0 <= (char.x0 + char.x1) / 2.0 <= x1 - 8.0
				and last_rule_y + 2.0 < (char.y0 + char.y1) / 2.0
				and (char.y0 + char.y1) / 2.0
					<= min(page_height, max(max(segment.y0, segment.y1) for segment in internal) + 2.0)
			]
			if not body_tail_chars:
				continue
			body_size = median([char.size for char in body_tail_chars])
			bottom_y = min(
				page_height,
				max(char.y1 for char in body_tail_chars) + max(3.0, body_size * 0.60),
			)
			if bottom_y - last_rule_y < max(35.0, min(row_gaps) * 0.50):
				continue
			ys = [float(top_y), *map(float, rule_ys), float(bottom_y)]
			if any(right - left < 18.0 for left, right in zip(ys, ys[1:])):
				continue
			region_lines = [
				line
				for line in lines
				if any(
					char.text.strip()
					and xs[0] <= (char.x0 + char.x1) / 2.0 <= xs[-1]
					and ys[0] <= (char.y0 + char.y1) / 2.0 <= ys[-1]
					for char in line.chars
				)
			]
			occupancy = self._partial_grid_occupancy(region_lines, xs, ys)
			if occupancy != [
				{1, 2, 3},
				{0, 1, 2, 3},
				{0, 1, 2, 3},
				{0, 1, 2, 3},
			]:
				continue
			box = (float(xs[0]), float(ys[0]), float(xs[-1]), float(ys[-1]))
			html = self._render_partial_grid_html(
				page,
				xs,
				ys,
				region_lines,
				"",
				header_rows_override=1,
			)
			self._partial_table_models[(page, box)] = {
				"model_kind": "open_internal_grid",
				"xs": list(xs),
				"ys": list(ys),
				"header_rows": 1,
				"spans": [],
				"evidence": {
					"physical_horizontal_separators": 3,
					"physical_internal_dividers": 3,
					"open_outer_edges": True,
					"empty_corner_header": True,
					"complete_body_occupancy": True,
				},
			}
			out.append((float(ys[0]), html, region_lines, box))
		return out

	def _partial_grid_candidates(self, page: int) -> List[Tuple[float, str, List[Line], Tuple[float, float, float, float]]]:
		"""Recover captioned tables whose rules are emitted one row at a time.

		Several PDF producers draw an otherwise regular table as disconnected
		vertical fragments, often with alternating filled rows and only a header
		separator.  ``segment_components`` intentionally does not connect those
		fragments, so the ordinary lattice detector cannot see the complete grid.
		This detector stays conservative: an explicit table caption, at least two
		stable fragmented vertical boundaries, repeated row endpoints, and an
		independent fill/rule signal are all required.
		"""
		lines = self.lines_by_page.get(page, [])
		captions = [
			line
			for line in lines
			if self._is_explicit_table_caption(
				plain_text(line_text_tokens(line)).strip()
			)
		]
		if not captions:
			return []
		vertical = [
			segment
			for segment in self.conv.segments
			if segment.page == page and segment.vertical and segment.length > 5.0
		]
		if len(vertical) < 6:
			return []
		vertical_groups = self._partial_vertical_groups(vertical)
		page_width, _page_height = self.conv.page_sizes.get(page, (612.0, 792.0))
		out: List[Tuple[float, str, List[Line], Tuple[float, float, float, float]]] = []
		for caption_line in captions:
			caption_bottom = caption_line.y1
			nearby = [
				group
				for group in vertical_groups
				if group["y0"] >= caption_bottom - 4.0
				and group["y0"] - caption_bottom <= max(120.0, caption_line.size * 12.0)
				and group["span"] >= max(36.0, caption_line.size * 3.5)
				and group["coverage"] >= 0.72
				and len(group["segments"]) >= 3
			]
			if len(nearby) < 2:
				continue
			anchor = min(
				nearby,
				key=lambda group: (
					group["y0"] - caption_bottom,
					-group["span"],
				),
			)
			selected = [
				group
				for group in nearby
				if self._partial_span_overlap(group, anchor) >= 0.72
			]
			if len(selected) < 2:
				continue
			selected.sort(key=lambda group: group["x"])
			y0 = min(group["y0"] for group in selected)
			y1 = max(group["y1"] for group in selected)
			if y1 - y0 < max(42.0, caption_line.size * 4.0):
				continue
			internal_xs = [float(group["x"]) for group in selected]
			x_gaps = [
				right - left
				for left, right in zip(internal_xs, internal_xs[1:])
				if right > left
			]
			frame_extension = max(70.0, (max(x_gaps) if x_gaps else 70.0) * 1.30)
			frame_x0 = max(0.0, internal_xs[0] - frame_extension)
			frame_x1 = min(page_width, internal_xs[-1] + frame_extension)
			horizontal_groups = self._partial_horizontal_groups(
				page,
				y0,
				y1,
				frame_x0,
				frame_x1,
			)
			supporting_horizontal = [
				group
				for group in horizontal_groups
				if group["x0"] <= internal_xs[0] + 3.0
				and group["x1"] >= internal_xs[-1] - 3.0
			]
			region_lines = [
				line
				for line in lines
				if y0 - 2.0 <= (line.y0 + line.y1) / 2 <= y1 + 2.0
				and line.x1 >= frame_x0
				and line.x0 <= frame_x1
			]
			if len(region_lines) < 3:
				continue
			region_fills = [
				fill
				for fill in self.conv.fills
				if fill.page == page
				and fill.y1 >= y0 - 2.0
				and fill.y0 <= y1 + 2.0
				and fill.x1 >= frame_x0
				and fill.x0 <= frame_x1
				and 3.0 <= fill.y1 - fill.y0 <= max(60.0, caption_line.size * 5.0)
			]
			x_extents: List[Tuple[float, float]] = [
				(float(group["x0"]), float(group["x1"]))
				for group in supporting_horizontal
			]
			x_extents.extend(
				(max(frame_x0, fill.x0), min(frame_x1, fill.x1))
				for fill in region_fills
			)
			x_extents.extend(
				(max(frame_x0, line.x0), min(frame_x1, line.x1))
				for line in region_lines
			)
			if not x_extents:
				continue
			x0 = min([internal_xs[0], *(extent[0] for extent in x_extents)])
			x1 = max([internal_xs[-1], *(extent[1] for extent in x_extents)])
			if x1 - x0 < 80.0:
				continue
			xs = cluster_values([x0, *internal_xs, x1], 2.0)
			if len(xs) < 4 or any(right - left < 12.0 for left, right in zip(xs, xs[1:])):
				continue

			endpoint_ys = self._partial_shared_vertical_endpoints(selected)
			horizontal_ys = [
				float(group["y"])
				for group in horizontal_groups
				if self._partial_interval_coverage(
					group["intervals"],
					x0,
					x1,
				) >= 0.55
			]
			fill_bands = self._partial_fill_bands(region_fills, x0, x1)
			# A repeated fill pattern or a wide physical rule independently
			# corroborates that the fragmented vertical strokes are tabular.
			if len(fill_bands) < 2 and not horizontal_ys:
				continue
			ys = cluster_values(
				[
					y0,
					y1,
					*endpoint_ys,
					*horizontal_ys,
					*(value for band in fill_bands for value in band),
				],
				2.0,
			)
			ys = [value for value in ys if y0 - 2.0 <= value <= y1 + 2.0]
			if len(ys) < 4:
				continue
			occupancy = self._partial_grid_occupancy(region_lines, xs, ys)
			if len(occupancy) != len(ys) - 1 or any(not row for row in occupancy):
				continue
			if sum(len(row) >= 2 for row in occupancy) < 3:
				continue
			covered_columns = {
				column
				for row in occupancy
				for column in row
			}
			if len(covered_columns) < 2:
				continue

			box = (float(xs[0]), float(ys[0]), float(xs[-1]), float(ys[-1]))
			caption_text = plain_text(line_text_tokens(caption_line)).strip()
			note_line = self._table_note_after(page, box[3], box[0], box[2])
			table_lines = list(region_lines) + [caption_line]
			if note_line is not None:
				table_lines.append(note_line)
			html = self._render_partial_grid_html(
				page,
				xs,
				ys,
				region_lines,
				caption_text,
			)
			self._partial_table_models[(page, box)] = {
				"xs": list(xs),
				"ys": list(ys),
				"evidence": {
					"caption": caption_text,
					"fragmented_vertical_boundaries": len(selected),
					"shared_row_boundaries": len(endpoint_ys),
					"fill_bands": len(fill_bands),
					"wide_horizontal_boundaries": len(horizontal_ys),
				},
			}
			out.append((caption_line.y0, html, table_lines, box))
		return out

	def _partial_vertical_groups(self, segments: Sequence[Segment]) -> List[Dict[str, Any]]:
		groups: List[List[Segment]] = []
		for segment in sorted(
			segments,
			key=lambda item: ((item.x0 + item.x1) / 2, min(item.y0, item.y1)),
		):
			x = (segment.x0 + segment.x1) / 2
			if groups:
				previous_x = sum(
					(item.x0 + item.x1) / 2 for item in groups[-1]
				) / len(groups[-1])
			else:
				previous_x = 0.0
			if groups and abs(x - previous_x) <= 2.0:
				groups[-1].append(segment)
			else:
				groups.append([segment])
		out: List[Dict[str, Any]] = []
		for group in groups:
			intervals = [
				tuple(sorted((segment.y0, segment.y1)))
				for segment in group
			]
			y0 = min(interval[0] for interval in intervals)
			y1 = max(interval[1] for interval in intervals)
			out.append(
				{
					"x": sum((segment.x0 + segment.x1) / 2 for segment in group) / len(group),
					"y0": y0,
					"y1": y1,
					"span": y1 - y0,
					"coverage": self._partial_interval_coverage(intervals, y0, y1),
					"segments": group,
				}
			)
		return out

	def _partial_horizontal_groups(
		self,
		page: int,
		y0: float,
		y1: float,
		x0: float,
		x1: float,
	) -> List[Dict[str, Any]]:
		segments = [
			segment
			for segment in self.conv.segments
			if segment.page == page
			and segment.horizontal
			and segment.length > 5.0
			and y0 - 2.0 <= (segment.y0 + segment.y1) / 2 <= y1 + 2.0
			and max(segment.x0, segment.x1) >= x0
			and min(segment.x0, segment.x1) <= x1
		]
		groups: List[List[Segment]] = []
		for segment in sorted(
			segments,
			key=lambda item: ((item.y0 + item.y1) / 2, min(item.x0, item.x1)),
		):
			y = (segment.y0 + segment.y1) / 2
			if groups:
				previous_y = sum(
					(item.y0 + item.y1) / 2 for item in groups[-1]
				) / len(groups[-1])
			else:
				previous_y = 0.0
			if groups and abs(y - previous_y) <= 2.0:
				groups[-1].append(segment)
			else:
				groups.append([segment])
		out: List[Dict[str, Any]] = []
		for group in groups:
			intervals = [
				tuple(sorted((segment.x0, segment.x1)))
				for segment in group
			]
			out.append(
				{
					"y": sum((segment.y0 + segment.y1) / 2 for segment in group) / len(group),
					"x0": min(interval[0] for interval in intervals),
					"x1": max(interval[1] for interval in intervals),
					"intervals": intervals,
				}
			)
		return out

	def _partial_shared_vertical_endpoints(
		self,
		groups: Sequence[Dict[str, Any]],
	) -> List[float]:
		endpoints: List[Tuple[float, int]] = []
		for group_index, group in enumerate(groups):
			for segment in group["segments"]:
				endpoints.append((min(segment.y0, segment.y1), group_index))
				endpoints.append((max(segment.y0, segment.y1), group_index))
		clusters: List[List[Tuple[float, int]]] = []
		for endpoint in sorted(endpoints):
			if clusters and endpoint[0] - clusters[-1][-1][0] <= 2.0:
				clusters[-1].append(endpoint)
			else:
				clusters.append([endpoint])
		required = max(2, math.ceil(len(groups) * 0.60))
		return [
			sum(value for value, _group in cluster) / len(cluster)
			for cluster in clusters
			if len({group for _value, group in cluster}) >= required
		]

	def _partial_fill_bands(
		self,
		fills: Sequence[Fill],
		x0: float,
		x1: float,
	) -> List[Tuple[float, float]]:
		groups: List[List[Fill]] = []
		for fill in sorted(fills, key=lambda item: (item.y0, item.y1, item.x0)):
			for group in groups:
				reference = group[0]
				if abs(fill.y0 - reference.y0) <= 2.0 and abs(fill.y1 - reference.y1) <= 2.0:
					group.append(fill)
					break
			else:
				groups.append([fill])
		out: List[Tuple[float, float]] = []
		for group in groups:
			intervals = [
				(max(x0, fill.x0), min(x1, fill.x1))
				for fill in group
				if fill.x1 > x0 and fill.x0 < x1
			]
			if self._partial_interval_coverage(intervals, x0, x1) >= 0.55:
				out.append(
					(
						sum(fill.y0 for fill in group) / len(group),
						sum(fill.y1 for fill in group) / len(group),
					)
				)
		return out

	def _partial_grid_occupancy(
		self,
		lines: Sequence[Line],
		xs: Sequence[float],
		ys: Sequence[float],
	) -> List[set[int]]:
		rows: List[set[int]] = []
		for row_index in range(len(ys) - 1):
			occupied: set[int] = set()
			for line in lines:
				for char in line.chars:
					if not char.text.strip():
						continue
					cx = (char.x0 + char.x1) / 2
					cy = (char.y0 + char.y1) / 2
					if not (ys[row_index] - 2.0 <= cy <= ys[row_index + 1] + 2.0):
						continue
					if xs[0] - 2.0 <= cx <= xs[-1] + 2.0:
						occupied.add(max(0, min(len(xs) - 2, find_interval(list(xs), cx))))
			rows.append(occupied)
		return rows

	def _render_partial_grid_html(
		self,
		page: int,
		xs: Sequence[float],
		ys: Sequence[float],
		lines: Sequence[Line],
		caption: str,
		header_rows_override: Optional[int] = None,
		explicit_spans: Optional[
			Dict[Tuple[int, int], Tuple[int, int]]
		] = None,
		caption_placement: str = "before",
	) -> str:
		header_rows = (
			self._table_header_rows(list(lines), list(ys))
			if header_rows_override is None
			else max(0, min(len(ys) - 1, int(header_rows_override)))
		)
		span_map = explicit_spans or {}
		occupied: set[Tuple[int, int]] = set()
		out = ["<table>"]
		if caption:
			caption_class = (
				' class="cocoapdf-caption-bottom"'
				if caption_placement == "after"
				else ""
			)
			out.append(
				"<caption%s>%s</caption>"
				% (caption_class, escape_html(caption))
			)
		if header_rows:
			out.append("<thead>")
		for row_index in range(len(ys) - 1):
			if header_rows and row_index == header_rows:
				out.extend(["</thead>", "<tbody>"])
			tag = "th" if row_index < header_rows else "td"
			cells = []
			for column in range(len(xs) - 1):
				if (row_index, column) in occupied:
					continue
				rowspan, colspan = span_map.get((row_index, column), (1, 1))
				if (
					rowspan < 1
					or colspan < 1
					or row_index + rowspan >= len(ys)
					or column + colspan >= len(xs)
				):
					rowspan, colspan = (1, 1)
				for covered_row in range(row_index, row_index + rowspan):
					for covered_column in range(column, column + colspan):
						if covered_row != row_index or covered_column != column:
							occupied.add((covered_row, covered_column))
				attrs = ""
				if rowspan > 1:
					attrs += ' rowspan="%d"' % rowspan
				if colspan > 1:
					attrs += ' colspan="%d"' % colspan
				cells.append(
					"<%s%s>%s</%s>"
					% (
						tag,
						attrs,
						self._table_cell_html(
							list(lines),
							xs[column],
							ys[row_index],
							xs[column + colspan],
							ys[row_index + rowspan],
							1,
						),
						tag,
					)
				)
			out.append("<tr>" + "".join(cells) + "</tr>")
		if header_rows:
			out.append("</tbody>" if len(ys) - 1 > header_rows else "</thead>")
		out.append("</table>")
		return "\n".join(out)

	def _partial_span_overlap(
		self,
		left: Dict[str, Any],
		right: Dict[str, Any],
	) -> float:
		overlap = max(0.0, min(left["y1"], right["y1"]) - max(left["y0"], right["y0"]))
		return overlap / max(1.0, min(left["span"], right["span"]))

	def _partial_interval_coverage(
		self,
		intervals: Sequence[Tuple[float, float]],
		start: float,
		end: float,
	) -> float:
		if end <= start:
			return 0.0
		clipped = sorted(
			(max(start, min(left, right)), min(end, max(left, right)))
			for left, right in intervals
			if max(left, right) > start and min(left, right) < end
		)
		covered = 0.0
		current_start: Optional[float] = None
		current_end: Optional[float] = None
		for left, right in clipped:
			if right <= left:
				continue
			if current_start is None:
				current_start, current_end = left, right
			elif left <= float(current_end) + 2.0:
				current_end = max(float(current_end), right)
			else:
				covered += float(current_end) - current_start
				current_start, current_end = left, right
		if current_start is not None and current_end is not None:
			covered += current_end - current_start
		return min(1.0, covered / (end - start))

	def _is_explicit_table_caption(self, text: str) -> bool:
		if re.match(r"^(?:table|tab\.)\s+of\s+contents\b", text, re.I):
			return False
		return bool(
			re.match(
				r"^(?:Table|Tab\.|Exhibit)"
				r"(?:\s+[A-Za-z0-9IVXLC]+(?:[.-][A-Za-z0-9IVXLC]+)*)?"
				r"\s*[:.]?\s+\S",
				text,
				re.I,
			)
		)

	def _table_candidates(self, page: int) -> List[Tuple[float, str, List[Line], Tuple[float, float, float, float]]]:
		cached = self._table_cache.get(page)
		if cached is not None:
			return cached
		segs = [s for s in self.conv.segments if s.page == page and (s.horizontal or s.vertical) and s.length > 5]
		out: List[Tuple[float, str, List[Line], Tuple[float, float, float, float]]] = []
		candidate_quality: Dict[int, int] = {}
		candidate_warnings: Dict[int, Tuple[str, str]] = {}
		candidate_models: Dict[int, Dict[str, Any]] = {}

		def add_candidate(
			candidate: Tuple[
				float,
				str,
				List[Line],
				Tuple[float, float, float, float],
			],
			quality: int,
		) -> None:
			out.append(candidate)
			candidate_quality[id(candidate)] = quality
			model = self._partial_table_models.get((page, candidate[3]))
			if model is not None:
				candidate_models[id(candidate)] = model

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
			complete_lattice = lattice_has_all_cell_edges(xs, ys, hs, vs)
			if any(line.writing_mode != "horizontal" for line in lines):
				table_lines = list(lines) + ([caption_line] if caption_line is not None else []) + ([note_line] if note_line is not None else [])
				candidate = (
					caption_line.y0 if caption_line else y0,
					self._render_spanned_table_html(page, xs, ys, lines, caption_text),
					table_lines,
					(x0, y0, x1, y1),
				)
				add_candidate(candidate, 3 if complete_lattice else 1)
				continue

			if not complete_lattice:
				if rows >= 2 and cols >= 2:
					fallback_lines = sorted(lines, key=lambda line: (line.y0, line.x0, line.seq))
					table_lines = list(fallback_lines) + ([caption_line] if caption_line is not None else []) + ([note_line] if note_line is not None else [])
					candidate = (
						caption_line.y0 if caption_line else y0,
						self._render_spanned_table_html(page, xs, ys, fallback_lines, caption_text),
						table_lines,
						(x0, y0, x1, y1),
					)
					add_candidate(candidate, 1)
					candidate_warnings[id(candidate)] = (
						"TABLE_SPAN_UNSUPPORTED",
						"partial lattice emitted as HTML fallback; span inference approximate",
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
				candidate = (
					caption_line.y0 if caption_line else y0,
					fallback,
					fallback_lines,
					(x0, y0, x1, y1),
				)
				add_candidate(candidate, 1)
				candidate_warnings[id(candidate)] = (
					"TABLE_SPAN_UNSUPPORTED",
					"partial lattice preserved as text; span inference unavailable",
				)
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
				add_candidate(
					(caption_line.y0 if caption_line else y0, "\n".join(html), table_lines, (x0, y0, x1, y1)),
					3,
				)
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
			add_candidate(
				(caption_line.y0 if caption_line else y0, table_markdown, table_lines, (x0, y0, x1, y1)),
				3,
			)

		open_internal_candidates = self._open_internal_grid_candidates(page)
		for candidate in open_internal_candidates:
			# A fully occupied open matrix can strictly contain a smaller closed
			# lattice made from its internal separators.  The three additional
			# physical dividers plus the populated outer cells prove that the
			# enclosing model preserves more source information than that nested
			# sub-grid, so it wins only under this narrowly admitted pattern.
			add_candidate(candidate, 4)

		recovered_detectors = (
			self._form_grid_candidates,
			self._artifact_filled_lattice_candidates,
			self._artifact_fragmented_lattice_candidates,
			self._artifact_partial_fill_grid_candidates,
			self._dense_fragmented_grid_candidates,
			self._overlaid_image_table_candidates,
			self._captioned_marked_bookend_candidates,
			self._partial_grid_candidates,
			self._multilevel_booktabs_candidates,
			self._booktabs_candidates,
			self._financial_statement_candidates,
			self._borderless_key_value_candidates,
			self._captioned_measurement_grid_candidates,
			self._tiered_numeric_grid_candidates,
			self._spreadsheet_grid_candidates,
			self._captioned_sparse_two_column_candidates,
			self._borderless_numeric_candidates,
			self._aligned_column_table_candidates,
		)
		for detector in recovered_detectors:
			for candidate in detector(page):
				# Capture the model before another detector can reuse the same
				# page/box key.  These models are stronger than an incomplete
				# connected-component fallback; except for the separately ranked
				# open enclosure, a complete lattice remains authoritative.
				add_candidate(candidate, 2)

		deduped: List[Tuple[float, str, List[Line], Tuple[float, float, float, float]]] = []
		used_lines: set = set()
		accepted_ids: set[int] = set()
		# Resolve overlap by information quality, not detector call order:
		# complete lattice > recovered grid > lossy/incomplete lattice.  Stable
		# source order is retained within a tier so independent detector
		# precedence does not otherwise change.
		ranked_candidates = sorted(
			enumerate(out),
			key=lambda item: (
				-candidate_quality.get(id(item[1]), 1),
				item[0],
			),
		)
		for _source_order, cand in ranked_candidates:
			line_ids = {id(line) for line in cand[2]}
			if line_ids & used_lines:
				continue
			deduped.append(cand)
			used_lines.update(line_ids)
			accepted_ids.add(id(cand))
		for cand in out:
			self._partial_table_models.pop((page, cand[3]), None)
		for cand in deduped:
			model = candidate_models.get(id(cand))
			if model is not None:
				self._partial_table_models[(page, cand[3])] = model
		deduped.sort(key=lambda t: (t[0], t[3][0]))
		for cand in deduped:
			warning = candidate_warnings.get(id(cand))
			if warning is not None:
				self.conv.doc.warn(warning[0], warning[1], page)
		self._table_cache[page] = deduped
		return deduped

	def _multilevel_booktabs_candidates(
		self,
		page: int,
	) -> List[
		Tuple[
			float,
			str,
			List[Line],
			Tuple[float, float, float, float],
		]
	]:
		"""Recover a three-tier booktabs header from its partial underlines.

		A grouped header can place one stub label vertically across all header
		rows while progressively underlining two or more column groups.  The
		group underlines are PDF-native span evidence: their endpoints must align
		with stable body gutters, the final header rule must cover every column,
		and the three text tiers must agree with that hierarchy.  This narrow
		model avoids interpreting arbitrary centered headings as table spans.
		"""
		lines = self.lines_by_page.get(page, [])
		page_width, page_height = self.conv.page_sizes.get(page, (612.0, 792.0))
		captions = [
			line
			for line in lines
			if self._is_explicit_table_caption(
				plain_text(line_text_tokens(line)).strip()
			)
		]
		if not captions:
			return []
		groups = [
			group
			for group in self._partial_horizontal_groups(
				page,
				0.0,
				page_height,
				0.0,
				page_width,
			)
			if group["x1"] - group["x0"] >= max(120.0, page_width * 0.25)
		]
		out: List[
			Tuple[
				float,
				str,
				List[Line],
				Tuple[float, float, float, float],
			]
		] = []
		for caption_line in captions:
			bottom_options = [
				group
				for group in groups
				if 2.0 < caption_line.y0 - group["y"] <= max(42.0, caption_line.size * 4.5)
			]
			if not bottom_options:
				continue
			bottom = max(bottom_options, key=lambda group: group["y"])
			x0 = float(bottom["x0"])
			x1 = float(bottom["x1"])
			if self._partial_interval_coverage(bottom["intervals"], x0, x1) < 0.94:
				continue
			top_options = [
				group
				for group in groups
				if group["y"] < bottom["y"] - 24.0
				and bottom["y"] - group["y"] <= max(180.0, caption_line.size * 16.0)
				and abs(float(group["x0"]) - x0) <= 5.0
				and abs(float(group["x1"]) - x1) <= 5.0
				and self._partial_interval_coverage(group["intervals"], x0, x1) >= 0.94
			]
			if not top_options:
				continue
			def internal_rules_after(group: Dict[str, Any]) -> List[Dict[str, Any]]:
				return sorted(
					[
						candidate
						for candidate in groups
						if group["y"] + 3.0 < candidate["y"] < bottom["y"] - 3.0
						and candidate["x1"] >= x0
						and candidate["x0"] <= x1
						and self._partial_interval_coverage(
							candidate["intervals"],
							x0,
							x1,
						) >= 0.65
					],
					key=lambda candidate: candidate["y"],
				)

			viable_top = [
				(group, internal_rules_after(group))
				for group in top_options
			]
			viable_top = [
				(group, internal)
				for group, internal in viable_top
				if len(internal) == 2
			]
			if not viable_top:
				continue
			top, internal = max(viable_top, key=lambda item: item[0]["y"])
			hierarchy_rule, header_rule = internal
			table_lines = [
				line
				for line in lines
				if top["y"] + 1.0 <= (line.y0 + line.y1) / 2.0 <= bottom["y"] - 1.0
				and line.x1 >= x0 - 2.0
				and line.x0 <= x1 + 2.0
			]
			header_lines = sorted(
				[
					line
					for line in table_lines
					if (line.y0 + line.y1) / 2.0 < header_rule["y"]
				],
				key=lambda line: ((line.y0 + line.y1) / 2.0, line.x0, line.seq),
			)
			body_lines = sorted(
				[
					line
					for line in table_lines
					if (line.y0 + line.y1) / 2.0 > header_rule["y"]
				],
				key=lambda line: ((line.y0 + line.y1) / 2.0, line.x0, line.seq),
			)
			if len(header_lines) != 3 or len(body_lines) < 2:
				continue
			if any(
				sum(self._booktabs_numeric_token(word[0]) for word in word_boxes(line)) < 2
				for line in body_lines
			):
				continue
			boundaries = self._booktabs_column_boundaries(body_lines, x0, x1)
			if not (5 <= len(boundaries) <= 11):
				continue
			xs = [x0, *boundaries, x1]
			columns = len(xs) - 1
			if any(right - left < 8.0 for left, right in zip(xs, xs[1:])):
				continue

			def interval_ranges(group: Dict[str, Any]) -> Optional[List[Tuple[int, int]]]:
				ranges: List[Tuple[int, int]] = []
				tolerance = max(8.0, median([line.size for line in table_lines]) * 1.25)
				for left, right in sorted(group["intervals"]):
					start = min(range(len(xs)), key=lambda index: abs(xs[index] - left))
					end = min(range(len(xs)), key=lambda index: abs(xs[index] - right))
					if (
						start >= end
						or abs(xs[start] - left) > tolerance
						or abs(xs[end] - right) > tolerance
						or (right - left) / max(1.0, xs[end] - xs[start]) < 0.88
					):
						return None
					ranges.append((start, end))
				if any(left[1] > right[0] for left, right in zip(ranges, ranges[1:])):
					return None
				return ranges

			hierarchy_ranges = interval_ranges(hierarchy_rule)
			header_ranges = interval_ranges(header_rule)
			if hierarchy_ranges is None or header_ranges is None:
				continue
			if (
				len(hierarchy_ranges) < 2
				or hierarchy_ranges[0][0] >= hierarchy_ranges[-1][1]
				or any(left[1] != right[0] for left, right in zip(hierarchy_ranges, hierarchy_ranges[1:]))
			):
				continue
			covered_start = hierarchy_ranges[0][0]
			covered_end = hierarchy_ranges[-1][1]
			missing_columns = [
				column
				for column in range(columns)
				if not covered_start <= column < covered_end
			]
			if len(missing_columns) != 1:
				continue
			stub_column = missing_columns[0]
			if stub_column not in {0, columns - 1}:
				continue
			if (
				header_ranges[0][0] != 0
				or header_ranges[-1][1] != columns
				or any(left[1] != right[0] for left, right in zip(header_ranges, header_ranges[1:]))
			):
				continue
			runs = [self._booktabs_cell_runs(line, x0, x1) for line in header_lines]
			if (
				len(runs[0]) != 1
				or len(runs[1]) != len(hierarchy_ranges) + 1
				or len(runs[2]) != covered_end - covered_start
			):
				continue
			stub_center = (xs[stub_column] + xs[stub_column + 1]) / 2.0
			middle_centers = [(run[1] + run[2]) / 2.0 for run in runs[1]]
			stub_run_index = min(
				range(len(middle_centers)),
				key=lambda index: abs(middle_centers[index] - stub_center),
			)
			if abs(middle_centers[stub_run_index] - stub_center) > max(12.0, (xs[stub_column + 1] - xs[stub_column]) * 0.25):
				continue
			group_runs = [
				run
				for index, run in enumerate(runs[1])
				if index != stub_run_index
			]
			if len(group_runs) != len(hierarchy_ranges) or any(
				abs(
					(run[1] + run[2]) / 2.0
					- (xs[start] + xs[end]) / 2.0
				) > max(14.0, (xs[end] - xs[start]) * 0.12)
				for run, (start, end) in zip(group_runs, hierarchy_ranges)
			):
				continue
			bottom_run_centers = [(run[1] + run[2]) / 2.0 for run in runs[2]]
			covered_centers = [
				(xs[column] + xs[column + 1]) / 2.0
				for column in range(covered_start, covered_end)
			]
			if any(
				abs(run_center - column_center)
					> max(12.0, (xs[column + 1] - xs[column]) * 0.32)
				for run_center, column_center, column in zip(
					bottom_run_centers,
					covered_centers,
					range(covered_start, covered_end),
				)
			):
				continue
			top_center = (runs[0][0][1] + runs[0][0][2]) / 2.0
			if abs(top_center - (xs[covered_start] + xs[covered_end]) / 2.0) > max(14.0, (xs[covered_end] - xs[covered_start]) * 0.08):
				continue
			ordered_lines = [*header_lines, *body_lines]
			centers = [(line.y0 + line.y1) / 2.0 for line in ordered_lines]
			ys = [
				float(top["y"]),
				*((left + right) / 2.0 for left, right in zip(centers, centers[1:])),
				float(bottom["y"]),
			]
			if len(ys) != len(ordered_lines) + 1:
				continue
			spans: Dict[Tuple[int, int], Tuple[int, int]] = {
				(0, stub_column): (3, 1),
				(0, covered_start): (1, covered_end - covered_start),
			}
			for start, end in hierarchy_ranges:
				if end - start > 1:
					spans[(1, start)] = (1, end - start)
			box = (float(xs[0]), float(ys[0]), float(xs[-1]), float(ys[-1]))
			caption_text = plain_text(line_text_tokens(caption_line)).strip()
			html = self._render_partial_grid_html(
				page,
				xs,
				ys,
				ordered_lines,
				caption_text,
				header_rows_override=3,
				explicit_spans=spans,
				caption_placement="after",
			)
			self._partial_table_models[(page, box)] = {
				"model_kind": "multilevel_booktabs",
				"xs": list(xs),
				"ys": list(ys),
				"header_rows": 3,
				"spans": [
					{
						"row": row,
						"col": column,
						"rowspan": rowspan,
						"colspan": colspan,
					}
					for (row, column), (rowspan, colspan) in sorted(spans.items())
				],
				"evidence": {
					"caption": caption_text,
					"caption_placement": "after",
					"group_underlines": len(hierarchy_ranges),
					"stable_gap_boundaries": len(boundaries),
					"body_rows": len(body_lines),
					"physical_header_spans": len(spans),
				},
			}
			out.append((float(ys[0]), html, [*ordered_lines, caption_line], box))
		return out

	def _booktabs_candidates(self, page: int) -> List[Tuple[float, str, List[Line], Tuple[float, float, float, float]]]:
		"""Recover simple numeric tables delimited by horizontal rules only.

		The admission policy models the common ``booktabs`` convention without
		treating arbitrary ruled prose as a table.  It requires an explicit
		caption immediately below the table, aligned top/header/bottom rules,
		exactly one physical header row, and repeated numeric body-column
		anchors.  Multi-level headers and inferred spans remain outside this
		conservative slice.
		"""
		lines = self.lines_by_page.get(page, [])
		page_width, page_height = self.conv.page_sizes.get(page, (612.0, 792.0))
		captions = [
			line
			for line in lines
			if self._is_explicit_table_caption(
				plain_text(line_text_tokens(line)).strip()
			)
		]
		if not captions:
			return []
		horizontal_groups = [
			group
			for group in self._partial_horizontal_groups(
				page,
				0.0,
				page_height,
				0.0,
				page_width,
			)
			if group["x1"] - group["x0"] >= max(120.0, page_width * 0.28)
			and self._partial_interval_coverage(
				group["intervals"],
				group["x0"],
				group["x1"],
			) >= 0.90
		]
		if len(horizontal_groups) < 3:
			return []
		out: List[Tuple[float, str, List[Line], Tuple[float, float, float, float]]] = []
		for caption_line in captions:
			bottom_candidates = [
				group
				for group in horizontal_groups
				if group["y"] < caption_line.y0
				and caption_line.y0 - group["y"] <= max(42.0, caption_line.size * 4.5)
			]
			if not bottom_candidates:
				continue
			bottom = max(bottom_candidates, key=lambda group: group["y"])
			x0 = float(bottom["x0"])
			x1 = float(bottom["x1"])
			peer_rules = [
				group
				for group in horizontal_groups
				if bottom["y"] - max(200.0, caption_line.size * 18.0) <= group["y"] <= bottom["y"]
				and abs(float(group["x0"]) - x0) <= 5.0
				and abs(float(group["x1"]) - x1) <= 5.0
				and self._partial_interval_coverage(
					group["intervals"],
					x0,
					x1,
				) >= 0.84
			]
			if len(peer_rules) < 3:
				continue
			internal_rules = [
				group
				for group in peer_rules
				if group["y"] < bottom["y"] - 3.0
			]
			if not internal_rules:
				continue
			header_rule = max(internal_rules, key=lambda group: group["y"])
			top_candidates = [
				group
				for group in peer_rules
				if group["y"] < header_rule["y"] - 3.0
			]
			if not top_candidates:
				continue
			# Use the nearest contiguous rule trio.  Reusing the oldest matching
			# rule can accidentally absorb an earlier table with identical width.
			top = max(top_candidates, key=lambda group: group["y"])
			table_lines = [
				line
				for line in lines
				if top["y"] + 1.0 <= (line.y0 + line.y1) / 2 <= bottom["y"] - 1.0
				and line.x1 >= x0 - 2.0
				and line.x0 <= x1 + 2.0
			]
			header_lines = [
				line
				for line in table_lines
				if (line.y0 + line.y1) / 2 < header_rule["y"]
			]
			body_lines = [
				line
				for line in table_lines
				if (line.y0 + line.y1) / 2 > header_rule["y"]
			]
			if len(header_lines) != 1 or not body_lines:
				continue
			if len(body_lines) == 1:
				if sum(
					self._booktabs_numeric_token(box[0])
					for box in word_boxes(body_lines[0])
				) < 2:
					continue
				boundaries = self._booktabs_single_row_boundaries(
					header_lines[0],
					body_lines[0],
					x0,
					x1,
				)
			else:
				if any(
					sum(
						self._booktabs_numeric_token(box[0])
						for box in word_boxes(line)
					) < 2
					for line in body_lines
				):
					continue
				boundaries = self._booktabs_column_boundaries(body_lines, x0, x1)
			if not (2 <= len(boundaries) <= 13):
				continue
			xs = [
				x0,
				*boundaries,
				x1,
			]
			if any(right - left < 8.0 for left, right in zip(xs, xs[1:])):
				continue
			ordered_lines = sorted(
				table_lines,
				key=lambda line: ((line.y0 + line.y1) / 2, line.x0, line.seq),
			)
			centers = [
				(line.y0 + line.y1) / 2
				for line in ordered_lines
			]
			ys = [
				float(top["y"]),
				*((left + right) / 2 for left, right in zip(centers, centers[1:])),
				float(bottom["y"]),
			]
			occupancy = self._partial_grid_occupancy(ordered_lines, xs, ys)
			if len(occupancy) != len(ordered_lines) or any(len(row) < 2 for row in occupancy):
				continue
			box = (float(xs[0]), float(ys[0]), float(xs[-1]), float(ys[-1]))
			caption_text = plain_text(line_text_tokens(caption_line)).strip()
			html = self._render_partial_grid_html(
				page,
				xs,
				ys,
				ordered_lines,
				caption_text,
				header_rows_override=1,
				caption_placement="after",
			)
			self._partial_table_models[(page, box)] = {
				"model_kind": "booktabs",
				"xs": list(xs),
				"ys": list(ys),
				"header_rows": 1,
				"evidence": {
					"caption": caption_text,
					"horizontal_boundaries": len(peer_rules),
					"body_rows": len(body_lines),
					"column_anchors": len(xs) - 1,
					"stable_gap_boundaries": len(boundaries),
				},
			}
			out.append((float(top["y"]), html, [*ordered_lines, caption_line], box))
		return out

	def _booktabs_single_row_boundaries(
		self,
		header_line: Line,
		body_line: Line,
		x0: float,
		x1: float,
	) -> List[float]:
		"""Validate gutters using both rows when a table has one body row."""
		header_runs = self._booktabs_cell_runs(header_line, x0, x1)
		body_runs = self._booktabs_cell_runs(body_line, x0, x1)
		if not (3 <= len(header_runs) == len(body_runs) <= 14):
			return []
		header_gutters = [
			(left[2] + right[1]) / 2.0
			for left, right in zip(header_runs, header_runs[1:])
		]
		body_gutters = [
			(left[2] + right[1]) / 2.0
			for left, right in zip(body_runs, body_runs[1:])
		]
		tolerance = max(8.0, median([header_line.size, body_line.size]) * 1.10)
		if any(
			abs(header - body) > tolerance
			for header, body in zip(header_gutters, body_gutters)
		):
			return []
		return [
			(header + body) / 2.0
			for header, body in zip(header_gutters, body_gutters)
		]

	def _booktabs_column_boundaries(
		self,
		lines: Sequence[Line],
		x0: float,
		x1: float,
	) -> List[float]:
		median_size = median([line.size for line in lines])
		tolerance = max(3.0, median_size * 0.55)
		entries = sorted(
			(
				float((left[2] + right[1]) / 2.0),
				row_index,
			)
			for row_index, line in enumerate(lines)
			for left, right in zip(
				self._booktabs_cell_runs(line, x0, x1),
				self._booktabs_cell_runs(line, x0, x1)[1:],
			)
		)
		clusters: List[List[Tuple[float, int]]] = []
		for entry in entries:
			if clusters and entry[0] - clusters[-1][-1][0] <= tolerance:
				clusters[-1].append(entry)
			else:
				clusters.append([entry])
		required_rows = max(2, math.ceil(len(lines) * 0.60))
		candidates = [
			(
				sum(value for value, _row in cluster) / len(cluster),
				len({row for _value, row in cluster}),
			)
			for cluster in clusters
			if len({row for _value, row in cluster}) >= required_rows
		]
		merged: List[Tuple[float, int]] = []
		merge_distance = max(5.0, median_size * 0.75)
		for candidate in candidates:
			if merged and candidate[0] - merged[-1][0] <= merge_distance:
				merged[-1] = (
					(merged[-1][0] + candidate[0]) / 2.0,
					max(merged[-1][1], candidate[1]),
				)
			else:
				merged.append(candidate)
		return [
			value
			for value, _support in merged
			if x0 + 8.0 <= value <= x1 - 8.0
		]

	def _booktabs_cell_runs(
		self,
		line: Line,
		x0: float,
		x1: float,
	) -> List[Tuple[str, float, float]]:
		"""Coalesce words separated by ordinary in-cell gaps.

		Column anchors must describe semantic cells, not every word start.  This
		keeps labels such as ``Cand. 1``, ``H6 (Avg.)``, and weighted merge
		descriptions together while the substantially larger inter-column gutters
		remain stable across body rows.
		"""
		words = [
			word
			for word in sorted(word_boxes(line), key=lambda item: (item[1], item[2]))
			if word[3] >= x0 - 2.0 and word[1] <= x1 + 2.0
		]
		if not words:
			return []
		gap_limit = max(3.0, line.size * 0.65)
		runs: List[List[Tuple[str, float, float, float, float]]] = []
		for word in words:
			if runs and word[1] - runs[-1][-1][3] <= gap_limit:
				runs[-1].append(word)
			else:
				runs.append([word])
		return [
			(
				cleanup_spaces(" ".join(word[0] for word in run)),
				min(word[1] for word in run),
				max(word[3] for word in run),
			)
			for run in runs
		]

	def _booktabs_numeric_token(self, text: str) -> bool:
		value = cleanup_spaces(text).strip()
		return is_numeric_table_cell(value) or bool(
			re.fullmatch(r"[OX✓✗✔✘]", value, re.I)
			or re.fullmatch(
				r"[+-]?(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d+)?(?:[KMB]|%)",
				value,
				re.I,
			)
		)

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
			explicit = self._is_explicit_table_caption(text)
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
				# A preceding data row can have caption-like centring and width.
				# It is still table content, not a visual caption.  Explicit
				# ``Table ...`` labels remain eligible through the separate path.
				and self._borderless_key_value_row(line) is None
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
			if not self._borderless_key_value_group_is_admissible(page, group):
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

	def _spreadsheet_grid_candidates(
		self,
		page: int,
	) -> List[
		Tuple[
			float,
			str,
			List[Line],
			Tuple[float, float, float, float],
		]
	]:
		"""Recover borderless spreadsheet excerpts from native row/column labels.

		Spreadsheet and office producers sometimes omit every visible cell rule
		while retaining the sheet's consecutive column letters and row numbers.
		Those labels are stronger structural evidence than typography: require a
		consecutive letter band, a logical row ``1`` header, at least six
		consecutive numeric rows, stable row-number geometry, and numeric
		occupancy in multiple sheet columns.  Ordinary numbered prose, chart
		axes, and short matrices fail several independent requirements.
		"""
		lines = self.lines_by_page.get(page, [])
		page_width, _page_height = self.conv.page_sizes.get(page, (612.0, 792.0))
		out: List[
			Tuple[
				float,
				str,
				List[Line],
				Tuple[float, float, float, float],
			]
		] = []
		for letter_index, letter_line in enumerate(lines):
			letter_boxes = word_boxes(letter_line)
			if not (3 <= len(letter_boxes) <= 12):
				continue
			labels = [
				cleanup_spaces(box[0]).strip()
				for box in letter_boxes
			]
			label_numbers = [
				self._spreadsheet_column_number(label)
				for label in labels
			]
			if (
				any(value is None for value in label_numbers)
				or any(
					right != left + 1
					for left, right in zip(label_numbers, label_numbers[1:])
				)
			):
				continue
			label_centers = [
				(box[1] + box[3]) / 2.0
				for box in letter_boxes
			]
			label_gaps = [
				right - left
				for left, right in zip(label_centers, label_centers[1:])
			]
			if (
				not label_gaps
				or min(label_gaps) < max(18.0, letter_line.size * 2.0)
				or max(label_gaps) > min(label_gaps) * 3.5
			):
				continue

			header_seed_index: Optional[int] = None
			for index in range(letter_index + 1, len(lines)):
				line = lines[index]
				if line.y0 - letter_line.y1 > max(54.0, letter_line.size * 6.0):
					break
				boxes = word_boxes(line)
				if (
					boxes
					and cleanup_spaces(boxes[0][0]).strip() == "1"
					and (boxes[0][1] + boxes[0][3]) / 2.0
						< label_centers[0] - max(5.0, letter_line.size * 0.55)
				):
					header_seed_index = index
					break
			if header_seed_index is None:
				continue

			data_rows: List[Tuple[int, Line, List[Tuple[str, float, float, float, float]]]] = []
			expected_row = 2
			for index in range(header_seed_index + 1, len(lines)):
				line = lines[index]
				boxes = word_boxes(line)
				if not boxes:
					continue
				first = cleanup_spaces(boxes[0][0]).strip()
				if not re.fullmatch(r"\d{1,4}", first):
					if data_rows and line.y0 - data_rows[-1][1].y1 > max(12.0, letter_line.size * 1.35):
						break
					continue
				row_number = int(first)
				if row_number != expected_row:
					if data_rows:
						break
					continue
				if len(boxes) < 3 or any(
					not is_numeric_table_cell(cleanup_spaces(box[0]).strip())
					for box in boxes[1:]
				):
					data_rows = []
					break
				if data_rows:
					gap = line.y0 - data_rows[-1][1].y0
					if gap > max(36.0, letter_line.size * 3.5) or gap <= 0:
						break
				elif line.y0 - lines[header_seed_index].y0 > max(54.0, letter_line.size * 6.0):
					break
				data_rows.append((index, line, boxes))
				expected_row += 1
			if len(data_rows) < 6:
				continue

			data_centers_y = [
				(line.y0 + line.y1) / 2.0
				for _index, line, _boxes in data_rows
			]
			row_pitches = [
				right - left
				for left, right in zip(data_centers_y, data_centers_y[1:])
			]
			row_pitch = median(row_pitches)
			if row_pitch <= 0 or any(
				abs(pitch - row_pitch) > max(4.0, row_pitch * 0.30)
				for pitch in row_pitches
			):
				continue

			header_seed_boxes = word_boxes(lines[header_seed_index])
			row_number_centers = [
				(header_seed_boxes[0][1] + header_seed_boxes[0][3]) / 2.0,
				*[
					(boxes[0][1] + boxes[0][3]) / 2.0
					for _index, _line, boxes in data_rows
				],
			]
			row_number_center = median(row_number_centers)
			if (
				max(abs(value - row_number_center) for value in row_number_centers)
					> max(3.0, letter_line.size * 0.45)
				or label_centers[0] - row_number_center
					< max(10.0, letter_line.size * 1.10)
			):
				continue
			column_centers = [row_number_center, *label_centers]
			xs = [
				max(
					0.0,
					column_centers[0]
					- (column_centers[1] - column_centers[0]) / 2.0,
				),
				*[
					(left + right) / 2.0
					for left, right in zip(column_centers, column_centers[1:])
				],
				min(
					page_width,
					column_centers[-1]
					+ (column_centers[-1] - column_centers[-2]) / 2.0,
				),
			]
			if any(right - left < 8.0 for left, right in zip(xs, xs[1:])):
				continue

			column_support: Dict[int, int] = {}
			valid_rows = True
			for _index, _line, boxes in data_rows:
				occupied: set[int] = set()
				for box_index, box in enumerate(boxes):
					center = (box[1] + box[3]) / 2.0
					column = max(
						0,
						min(len(xs) - 2, find_interval(xs, center)),
					)
					if box_index == 0:
						if column != 0:
							valid_rows = False
							break
						continue
					if column == 0 or column in occupied:
						valid_rows = False
						break
					occupied.add(column)
					column_support[column] = column_support.get(column, 0) + 1
				if not valid_rows or len(occupied) < 2:
					valid_rows = False
					break
			if (
				not valid_rows
				or sum(
					support >= 3
					for support in column_support.values()
				) < 2
			):
				continue

			first_data_index = data_rows[0][0]
			header_lines = [
				line
				for line in lines[letter_index + 1:first_data_index]
				if plain_text(line_text_tokens(line)).strip()
			]
			if (
				lines[header_seed_index] not in header_lines
				or len(header_lines) > 4
				or max(line.y1 for line in header_lines)
					- min(line.y0 for line in header_lines)
					> max(36.0, letter_line.size * 4.0)
			):
				continue
			# Spreadsheet column letters are centered in their cells, whereas
			# numeric content is commonly left- or right-aligned.  Midpoints
			# between letter centers can therefore sit within the renderer's
			# small glyph-boundary tolerance.  Refine each internal boundary
			# into the whitespace between the closest observed anchors while
			# preserving the provisional column assignment.
			center_samples: List[List[float]] = [
				[]
				for _column in range(len(xs) - 1)
			]
			for structural_line in [
				letter_line,
				*header_lines,
				*[line for _index, line, _boxes in data_rows],
			]:
				for _text, box_x0, _box_y0, box_x1, _box_y1 in word_boxes(
					structural_line
				):
					center = (box_x0 + box_x1) / 2.0
					column = max(
						0,
						min(len(xs) - 2, find_interval(xs, center)),
					)
					center_samples[column].append(center)
			refined_xs = list(xs)
			for boundary in range(1, len(xs) - 1):
				left_samples = center_samples[boundary - 1]
				right_samples = center_samples[boundary]
				if not left_samples or not right_samples:
					continue
				left_anchor = max(left_samples)
				right_anchor = min(right_samples)
				if right_anchor - left_anchor >= 5.0:
					refined_xs[boundary] = (
						left_anchor + right_anchor
					) / 2.0
			xs = refined_xs
			header_occupancy = self._partial_grid_occupancy(
				header_lines,
				xs,
				[
					min(line.y0 for line in header_lines) - 2.0,
					max(line.y1 for line in header_lines) + 2.0,
				],
			)
			if (
				not header_occupancy
				or header_occupancy[0] != set(range(len(xs) - 1))
			):
				continue

			letter_header_boundary = (
				letter_line.y1 + min(line.y0 for line in header_lines)
			) / 2.0
			header_data_boundary = (
				max(line.y1 for line in header_lines) + data_rows[0][1].y0
			) / 2.0
			ys = [
				max(0.0, letter_line.y0 - max(2.0, letter_line.size * 0.45)),
				letter_header_boundary,
				header_data_boundary,
				*[
					(left + right) / 2.0
					for left, right in zip(data_centers_y, data_centers_y[1:])
				],
				data_centers_y[-1] + row_pitch / 2.0,
			]
			if any(bottom <= top for top, bottom in zip(ys, ys[1:])):
				continue
			table_lines = [
				letter_line,
				*header_lines,
				*[line for _index, line, _boxes in data_rows],
			]
			box = (float(xs[0]), float(ys[0]), float(xs[-1]), float(ys[-1]))
			html = self._render_partial_grid_html(
				page,
				xs,
				ys,
				table_lines,
				"",
			).replace("<br />", " ")
			self._partial_table_models[(page, box)] = {
				"model_kind": "spreadsheet_grid",
				"xs": list(xs),
				"ys": list(ys),
				"evidence": {
					"consecutive_column_labels": len(labels),
					"consecutive_row_labels": len(data_rows) + 1,
					"numeric_columns": sum(
						support >= 3
						for support in column_support.values()
					),
				},
			}
			out.append((letter_line.y0, html, table_lines, box))
		return out

	def _spreadsheet_column_number(self, label: str) -> Optional[int]:
		value = cleanup_spaces(label).strip()
		if not re.fullmatch(r"[A-Z]{1,2}", value):
			return None
		number = 0
		for char in value:
			number = number * 26 + ord(char) - ord("A") + 1
		return number

	def _captioned_sparse_two_column_candidates(
		self,
		page: int,
	) -> List[
		Tuple[
			float,
			str,
			List[Line],
			Tuple[float, float, float, float],
		]
	]:
		"""Recover compact captioned two-column tables without painted rules.

		A common workbook layout has one bold two-cell header followed by a short,
		regularly pitched run.  The right column may contain measurements, or it may
		be intentionally blank for handwritten observations.  Requiring an explicit
		table caption, a uniquely dominant header gutter, compact non-sentence rows,
		and repeated anchors keeps ordinary key/value prose and card layouts out of
		this deliberately narrow detector.
		"""
		lines = [
			line
			for line in self.lines_by_page.get(page, [])
			if line.writing_mode == "horizontal"
			and line.size > 0
			and plain_text(line_text_tokens(line)).strip()
		]
		lines.sort(key=lambda line: (line.y0, line.x0, line.seq))
		page_width, _page_height = self.conv.page_sizes.get(
			page,
			(612.0, 792.0),
		)
		out: List[
			Tuple[
				float,
				str,
				List[Line],
				Tuple[float, float, float, float],
			]
		] = []
		for caption_index, caption_line in enumerate(lines):
			caption_text = plain_text(line_text_tokens(caption_line)).strip()
			if not self._is_explicit_table_caption(caption_text):
				continue
			header_options = [
				line
				for line in lines[caption_index + 1 :]
				if 4.0
					<= line.y0 - caption_line.y1
					<= max(64.0, caption_line.size * 4.8)
				and line.bold_ratio >= 0.70
			]
			if not header_options:
				continue
			header_line = min(
				header_options,
				key=lambda line: (line.y0 - caption_line.y1, line.seq),
			)
			header_runs = self._booktabs_cell_runs(
				header_line,
				0.0,
				page_width,
			)
			if len(header_runs) != 2:
				continue
			left_header, right_header = header_runs
			gutter = right_header[1] - left_header[2]
			header_words = word_boxes(header_line)
			word_gaps = [
				right[1] - left[3]
				for left, right in zip(header_words, header_words[1:])
				if right[1] > left[3]
			]
			other_gaps = [gap for gap in word_gaps if abs(gap - gutter) > 0.25]
			ordinary_gap = median(other_gaps) if other_gaps else 0.0
			if (
				gutter < max(4.0, header_line.size * 0.50)
				or (
					other_gaps
					and gutter < max(ordinary_gap * 1.75, 5.0)
				)
			):
				continue
			separator = (left_header[2] + right_header[1]) / 2.0
			header_position = next(
				(
					position
					for position, candidate in enumerate(lines)
					if candidate is header_line
				),
				-1,
			)
			if header_position < 0:
				continue
			body_lines: List[Line] = []
			previous = header_line
			for line in lines[header_position + 1 :]:
				gap = line_flow_gap(previous, line)
				if gap > max(26.0, header_line.size * 3.0):
					break
				text = plain_text(line_text_tokens(line)).strip()
				words = word_boxes(line)
				if (
					not text
					or line.size < header_line.size * 0.72
					or line.size > header_line.size * 1.28
					or len(words) > 9
					or line.x0 < left_header[1] - 8.0
					or line.x1 > max(right_header[2] + 24.0, page_width * 0.70)
					or any(
						x0 + max(3.0, line.size * 0.50)
							< separator
							< x1 - max(3.0, line.size * 0.50)
						for _text, x0, _y0, x1, _y1 in words
					)
				):
					break
				body_lines.append(line)
				previous = line
			if len(body_lines) < 4:
				continue
			all_rows = [header_line, *body_lines]
			centers = [(line.y0 + line.y1) / 2.0 for line in all_rows]
			pitches = [right - left for left, right in zip(centers, centers[1:])]
			pitch = median(pitches)
			if (
				pitch <= 0
				or max(pitches) > max(pitch * 1.45, header_line.size * 2.5)
				or min(pitches) < max(pitch * 0.55, header_line.size * 0.85)
			):
				continue
			occupancy: List[set[int]] = []
			for line in all_rows:
				occupied = {
					0 if (x0 + x1) / 2.0 < separator else 1
					for _text, x0, _y0, x1, _y1 in word_boxes(line)
				}
				occupancy.append(occupied)
			if occupancy[0] != {0, 1} or any(not row for row in occupancy[1:]):
				continue
			left_support = sum(0 in row for row in occupancy[1:])
			right_support = sum(1 in row for row in occupancy[1:])
			if left_support < 4:
				continue
			if right_support < 2:
				compact_left_rows = all(
					len(word_boxes(line)) <= 2
					and len(plain_text(line_text_tokens(line)).strip()) <= 28
					for line in body_lines
				)
				if not (
					right_support == 0
					and len(body_lines) >= 5
					and compact_left_rows
					and len(right_header[0].split()) >= 3
				):
					continue
			x0 = min(line.x0 for line in all_rows) - 2.0
			x1 = max(line.x1 for line in all_rows) + 2.0
			xs = [float(x0), float(separator), float(x1)]
			if (
				separator - x0 < 24.0
				or x1 - separator < 24.0
				or x1 - x0 < page_width * 0.20
			):
				continue
			ys = [
				max(0.0, all_rows[0].y0 - max(2.0, header_line.size * 0.35)),
				*[
					(left + right) / 2.0
					for left, right in zip(centers, centers[1:])
				],
				all_rows[-1].y1 + max(2.0, header_line.size * 0.35),
			]
			box = (
				float(xs[0]),
				float(ys[0]),
				float(xs[-1]),
				float(ys[-1]),
			)
			html = self._render_partial_grid_html(
				page,
				xs,
				ys,
				all_rows,
				caption_text,
				header_rows_override=1,
			)
			self._partial_table_models[(page, box)] = {
				"model_kind": "captioned_sparse_two_column",
				"xs": list(xs),
				"ys": list(ys),
				"header_rows": 1,
				"evidence": {
					"caption": caption_text,
					"dominant_header_gutter": gutter,
					"body_rows": len(body_lines),
					"left_column_rows": left_support,
					"right_column_rows": right_support,
					"blank_response_column": right_support == 0,
				},
			}
			out.append(
				(
					caption_line.y0,
					html,
					[caption_line, *all_rows],
					box,
				)
			)
		return out

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

	def _aligned_column_cells(self, line: Line) -> List[Tuple[str, float, float]]:
		"""Split a line into cells at gutters that are wide for its type size.

		``_borderless_column_cells`` answers the same question for a bold header
		run, but it discards geometry and demands a very wide gutter.  Column
		reconstruction needs the horizontal extent of every cell so that repeated
		alignment can be measured across rows.
		"""
		boxes = word_boxes(line)
		if not boxes:
			return []
		gutter = max(line.size * 1.15, 7.0)
		cells: List[Tuple[List[str], float, float]] = [
			([boxes[0][0]], boxes[0][1], boxes[0][3])
		]
		for text, x0, _y0, x1, _y1 in boxes[1:]:
			if x0 - cells[-1][2] >= gutter:
				cells.append(([text], x0, x1))
			else:
				cells[-1][0].append(text)
				cells[-1] = (cells[-1][0], cells[-1][1], x1)
		return [
			(cleanup_spaces(" ".join(parts)), start, end)
			for parts, start, end in cells
			if cleanup_spaces(" ".join(parts))
		]

	def _aligned_column_table_candidates(
		self, page: int
	) -> List[Tuple[float, str, List[Line], Tuple[float, float, float, float]]]:
		"""Recover an unruled table from repeated inter-column alignment.

		``_borderless_numeric_candidates`` takes the column count from a single
		bold header line, so a table whose header is tiered, unemphasised, or
		wrapped is never admitted even when every body row is plainly aligned.
		This detector derives the grid from the body instead: a run of adjacent
		lines that split at the same gutters is the evidence, and the header is
		whatever sits above it inside the same alignment.

		Admission is deliberately narrow.  Prose wraps, list runs, and contents
		listings all produce incidental whitespace, so a candidate must show a
		stable modal cell count, gutters that no row crosses, a regular vertical
		pitch, and body text that does not read as sentences.
		"""
		lines = [
			line
			for line in self.lines_by_page.get(page, [])
			if line.writing_mode == "horizontal"
			and line.size > 0
			and plain_text(line_text_tokens(line)).strip()
		]
		if len(lines) < 3:
			return []
		lines.sort(key=lambda line: (line.y0, line.x0, line.seq))

		rows = [(line, self._aligned_column_cells(line)) for line in lines]
		out: List[Tuple[float, str, List[Line], Tuple[float, float, float, float]]] = []
		start = 0
		while start < len(rows):
			if len(rows[start][1]) < 3:
				start += 1
				continue
			end = start + 1
			while end < len(rows):
				previous, current = rows[end - 1][0], rows[end][0]
				if len(rows[end][1]) < 2:
					break
				if line_flow_gap(previous, current) > max(previous.size * 2.6, 26.0):
					break
				end += 1
			run = rows[start:end]
			if len(run) < 3:
				start += 1
				continue
			candidate = self._aligned_column_table(page, run)
			if candidate is None:
				start += 1
				continue
			out.append(candidate)
			start = end
		return out

	def _aligned_column_table(
		self,
		page: int,
		run: Sequence[Tuple[Line, List[Tuple[str, float, float]]]],
	) -> Optional[Tuple[float, str, List[Line], Tuple[float, float, float, float]]]:
		counts = [len(cells) for _line, cells in run]
		modal = max(set(counts), key=counts.count)
		if modal < 3:
			return None
		aligned = [item for item in run if len(item[1]) == modal]
		if len(aligned) < 3 or len(aligned) < math.ceil(len(run) * 0.70):
			return None

		body_size = median([line.size for line, _cells in run])
		if body_size <= 0:
			return None

		# Column bands come from the aligned rows only; a wrapped or spanning
		# row must not be allowed to move a boundary it does not respect.
		starts: List[List[float]] = [[] for _ in range(modal)]
		ends: List[List[float]] = [[] for _ in range(modal)]
		for _line, cells in aligned:
			for index, (_text, x0, x1) in enumerate(cells):
				starts[index].append(x0)
				ends[index].append(x1)
		lefts = [median(values) for values in starts]
		rights = [median(values) for values in ends]
		if any(
			max(values) - min(values) > max(body_size * 1.6, 14.0)
			for values in starts
		):
			return None
		separators = [
			(rights[index] + lefts[index + 1]) / 2.0 for index in range(modal - 1)
		]
		if any(
			separators[index] >= separators[index + 1]
			for index in range(len(separators) - 1)
		):
			return None

		# Every gutter must stay empty. A word straddling a separator means the
		# whitespace was incidental, which is what prose and wrapped lists look
		# like.
		for line, _cells in run:
			for text, x0, _y0, x1, _y1 in word_boxes(line):
				if not text.strip():
					continue
				if any(x0 + 0.6 < sep < x1 - 0.6 for sep in separators):
					return None

		pitches = [
			line_flow_gap(run[index - 1][0], run[index][0])
			for index in range(1, len(run))
		]
		pitch = median(pitches) if pitches else 0.0
		if pitch <= 0 or any(value > max(pitch * 2.4, body_size * 2.6) for value in pitches):
			return None

		texts = [plain_text(line_text_tokens(line)).strip() for line, _cells in run]
		if any(re.search(r"\.{4,}|…", text) for text in texts):
			# Dot leaders are a contents listing, not a table.
			return None
		sentences = sum(
			1
			for text in texts
			if len(text.split()) >= 14 and re.search(r"[.!?][\"')\]]?$", text)
		)
		if sentences >= math.ceil(len(run) * 0.34):
			return None
		if sum(1 for _line, cells in run if list_marker(cells[0][0]) is not None) >= math.ceil(
			len(run) * 0.60
		):
			return None

		width = rights[-1] - lefts[0]
		page_width, _page_height = self.conv.page_sizes.get(page, (612.0, 792.0))
		if width < page_width * 0.30:
			return None

		# The body defines the columns, but the authored header usually sits
		# above it and may be tiered or wrapped, so it never matches the modal
		# cell count. Absorb the lines directly above that stay inside the
		# column span; without them the caption is separated from the block by
		# the header and the table would be emitted headerless.
		page_lines = [
			candidate
			for candidate in self.lines_by_page.get(page, [])
			if candidate.writing_mode == "horizontal"
			and plain_text(line_text_tokens(candidate)).strip()
		]
		page_lines.sort(key=lambda candidate: (candidate.y0, candidate.x0, candidate.seq))
		margin = max(body_size * 1.5, 12.0)
		header_lines: List[Line] = []
		top = run[0][0]
		cursor = next(
			(
				position
				for position, candidate in enumerate(page_lines)
				if candidate is top
			),
			-1,
		) - 1
		while cursor >= 0 and len(header_lines) < 3:
			candidate = page_lines[cursor]
			text = plain_text(line_text_tokens(candidate)).strip()
			if self._is_explicit_table_caption(text):
				break
			if line_flow_gap(candidate, top) > max(top.size * 2.6, 26.0):
				break
			if candidate.x0 < lefts[0] - margin or candidate.x1 > rights[-1] + margin:
				break
			header_lines.insert(0, candidate)
			top = candidate
			cursor -= 1

		# Aligned text alone is not a table. Prompt lists, assignment forms, and
		# dashboard cards all place short values on a shared set of tab stops.
		# Require the two independent signals this engine already treats as
		# authoritative elsewhere: an explicit table caption naming the object,
		# and repeated numeric columns underneath it.
		table_lines = header_lines + [line for line, _cells in run]
		caption_line = self._table_caption_before(
			page,
			min(line.y0 for line in table_lines),
			lefts[0],
			rights[-1],
		)
		caption_text = (
			plain_text(line_text_tokens(caption_line)).strip() if caption_line else ""
		)
		if caption_line is None or not self._is_explicit_table_caption(caption_text):
			return None

		grid: List[List[str]] = []
		for line, cells in [
			(candidate, self._aligned_column_cells(candidate))
			for candidate in header_lines
		] + list(run):
			if len(cells) == modal:
				grid.append([text for text, _x0, _x1 in cells])
				continue
			row = [""] * modal
			for text, x0, x1 in cells:
				center = (x0 + x1) / 2.0
				index = 0
				for position, sep in enumerate(separators, start=1):
					if center >= sep:
						index = position
				row[index] = cleanup_spaces((row[index] + " " + text).strip())
			grid.append(row)
		if sum(1 for row in grid if any(cell for cell in row)) < 3:
			return None
		filled = sum(1 for row in grid for cell in row if cell)
		if filled < modal * 2:
			return None

		body_grid = grid[len(header_lines):]
		numeric_columns = [
			column
			for column in range(1, modal)
			if sum(1 for row in body_grid if is_numeric_table_cell(row[column]))
			>= max(2, math.ceil(len(body_grid) * 0.60))
		]
		if len(numeric_columns) < 2:
			return None

		markdown = ["| " + " | ".join(escape_table(cell) for cell in grid[0]) + " |"]
		markdown.append("| " + " | ".join("---" for _ in range(modal)) + " |")
		for row in grid[1:]:
			markdown.append("| " + " | ".join(escape_table(cell) for cell in row) + " |")
		body = "\n".join(markdown)
		if caption_line is not None:
			body = self._table_caption_markdown(caption_line, caption_text) + "\n\n" + body
			table_lines = [caption_line] + table_lines
		return (
			(caption_line.y0 if caption_line is not None else table_lines[0].y0),
			body,
			table_lines,
			(
				min(line.x0 for line in table_lines),
				min(line.y0 for line in table_lines),
				max(line.x1 for line in table_lines),
				max(line.y1 for line in table_lines),
			),
		)

	def _captioned_measurement_grid_candidates(
		self,
		page: int,
	) -> List[
		Tuple[
			float,
			str,
			List[Line],
			Tuple[float, float, float, float],
		]
	]:
		"""Recover simple captioned tables whose values carry unit suffixes.

		A numeric table may be visually borderless and express each value as a
		number followed by a short unit token (for example, ``8 ml``).  The
		general borderless detector deliberately does not classify those strings
		as bare numbers.  This narrower model requires several independent table
		signals instead: an explicit nearby caption, an all-bold header, at least
		three complete body rows, stable repeated body anchors, whitespace that
		separates every column on every row, and numeric-or-measurement content in
		at least two data columns.

		The reconstructed boundaries are retained as a partial-grid model so the
		semantic graph, HTML projection, evidence, and glyph provenance all use
		the same cells as the layout projection.
		"""
		lines = self.lines_by_page.get(page, [])
		out: List[
			Tuple[
				float,
				str,
				List[Line],
				Tuple[float, float, float, float],
			]
		] = []
		for header_index, header_line in enumerate(lines):
			if (
				header_line.writing_mode != "horizontal"
				or header_line.bold_ratio < 0.75
			):
				continue
			header_groups = self._captioned_measurement_groups(header_line)
			column_count = len(header_groups)
			if not (3 <= column_count <= 8):
				continue
			if any(
				not re.search(r"[A-Za-z]", group[0])
				or len(group[0]) > 80
				for group in header_groups
			):
				continue

			body_rows: List[
				Tuple[
					Line,
					List[Tuple[str, float, float]],
				]
			] = []
			previous = header_line
			for line in lines[header_index + 1:]:
				if (
					line.writing_mode != "horizontal"
					or line.y0 <= previous.y0
					or line.y0 - previous.y0
						> max(34.0, header_line.size * 3.2)
				):
					break
				groups = self._captioned_measurement_groups(line)
				if len(groups) != column_count:
					break
				body_rows.append((line, groups))
				previous = line
			if len(body_rows) < 3:
				continue

			measurement_columns = {
				column
				for column in range(1, column_count)
				if all(
					self._is_captioned_measurement_cell(groups[column][0])
					for _line, groups in body_rows
				)
			}
			if len(measurement_columns) < 2:
				continue

			# Repeated physical anchors are producer-native evidence that these
			# are rows, not several prose fragments that happened to share a
			# baseline.  Header labels may be centered, so only body anchors are
			# compared here.
			stable_anchors = True
			for column in range(column_count):
				anchors = [
					groups[column][1]
					for _line, groups in body_rows
				]
				anchor = median(anchors)
				if max(abs(value - anchor) for value in anchors) > max(
					4.0,
					header_line.size * 0.65,
				):
					stable_anchors = False
					break
			if not stable_anchors:
				continue

			all_groups = [
				header_groups,
				*[groups for _line, groups in body_rows],
			]
			boundaries: List[float] = []
			for column in range(column_count - 1):
				left_edge = max(groups[column][2] for groups in all_groups)
				right_edge = min(
					groups[column + 1][1]
					for groups in all_groups
				)
				if right_edge - left_edge < max(
					4.0,
					header_line.size * 0.28,
				):
					break
				boundaries.append((left_edge + right_edge) / 2.0)
			if len(boundaries) != column_count - 1:
				continue

			x_margin = max(2.0, header_line.size * 0.25)
			xs = [
				min(groups[0][1] for groups in all_groups) - x_margin,
				*boundaries,
				max(groups[-1][2] for groups in all_groups) + x_margin,
			]
			row_lines = [
				header_line,
				*[line for line, _groups in body_rows],
			]
			y_margin = max(2.0, header_line.size * 0.22)
			ys = [
				row_lines[0].y0 - y_margin,
				*[
					(left.y1 + right.y0) / 2.0
					for left, right in zip(row_lines, row_lines[1:])
				],
				row_lines[-1].y1 + y_margin,
			]
			if (
				any(right <= left for left, right in zip(xs, xs[1:]))
				or any(bottom <= top for top, bottom in zip(ys, ys[1:]))
				or self._partial_grid_occupancy(row_lines, xs, ys)
					!= [set(range(column_count)) for _line in row_lines]
			):
				continue

			caption_line = self._table_caption_before(
				page,
				ys[0],
				xs[0],
				xs[-1],
			)
			if caption_line is None:
				continue
			caption_lead = plain_text(
				line_text_tokens(caption_line)
			).strip()
			if not self._is_explicit_table_caption(caption_lead):
				continue
			try:
				caption_index = next(
					index
					for index, line in enumerate(lines)
					if line is caption_line
				)
			except StopIteration:
				continue
			if not (0 <= caption_index < header_index):
				continue
			caption_lines = [
				line
				for line in lines[caption_index:header_index]
				if (
					line.writing_mode == "horizontal"
					and plain_text(line_text_tokens(line)).strip()
					and line.y0 >= caption_line.y0 - 1.0
					and line.y1 <= header_line.y0 + 1.0
				)
			]
			if (
				not caption_lines
				or len(caption_lines) > 3
				or any(
					right.y0 - left.y1
						> max(10.0, header_line.size * 1.15)
					for left, right in zip(
						caption_lines,
						caption_lines[1:] + [header_line],
					)
				)
			):
				continue
			caption_text = cleanup_spaces(
				" ".join(
					plain_text(line_text_tokens(line)).strip()
					for line in caption_lines
				)
			)
			if len(caption_text) > 320:
				continue

			box = (
				float(xs[0]),
				float(ys[0]),
				float(xs[-1]),
				float(ys[-1]),
			)
			html = self._render_partial_grid_html(
				page,
				xs,
				ys,
				row_lines,
				caption_text,
			)
			self._partial_table_models[(page, box)] = {
				"model_kind": "captioned_measurement_grid",
				"xs": list(xs),
				"ys": list(ys),
				"evidence": {
					"caption": caption_text,
					"body_rows": len(body_rows),
					"stable_column_anchors": column_count,
					"measurement_columns": len(measurement_columns),
				},
			}
			out.append(
				(
					caption_line.y0,
					html,
					[*caption_lines, *row_lines],
					box,
				)
			)
		return out

	def _captioned_measurement_groups(
		self,
		line: Line,
	) -> List[Tuple[str, float, float]]:
		boxes = word_boxes(line)
		if not boxes:
			return []
		groups: List[
			List[Tuple[str, float, float]]
		] = [[(boxes[0][0], boxes[0][1], boxes[0][3])]]
		previous_right = boxes[0][3]
		for text, x0, _y0, x1, _y1 in boxes[1:]:
			if x0 - previous_right >= max(line.size * 2.0, 18.0):
				groups.append([])
			groups[-1].append((text, x0, x1))
			previous_right = x1
		return [
			(
				cleanup_spaces(" ".join(word[0] for word in group)),
				min(word[1] for word in group),
				max(word[2] for word in group),
			)
			for group in groups
		]

	def _is_captioned_measurement_cell(self, text: str) -> bool:
		value = cleanup_spaces(text).strip()
		value = re.sub(r"^[*†‡§¶#]+\s*", "", value)
		if is_numeric_table_cell(value):
			return True
		return bool(
			re.fullmatch(
				r"[<>=~≈]?\s*"
				r"[\(\[]?[+\-\u2212]?"
				r"(?:[$€£¥₹]\s*)?"
				r"(?:\d[\d,]*(?:\.\d+)?|\.\d+)"
				r"(?:\s*(?:±|\+/-)\s*(?:\d[\d,]*(?:\.\d+)?|\.\d+))?"
				r"\s+"
				r"(?:[A-Za-zµμ°][A-Za-z0-9µμ°/%²³^.\-]*"
				r"(?:\s+[A-Za-zµμ°][A-Za-z0-9µμ°/%²³^.\-]*){0,2})"
				r"[\)\]]?",
				value,
			)
		)

	def _tiered_numeric_grid_candidates(
		self,
		page: int,
	) -> List[
		Tuple[
			float,
			str,
			List[Line],
			Tuple[float, float, float, float],
		]
	]:
		"""Recover numeric grids whose headers span one or more baselines.

		The columns are inferred exclusively from at least three complete,
		repeated numeric body rows.  Header text is admitted only after those
		anchors establish non-overlapping columns, and only with either an
		explicit table caption or conservative page-top continuation evidence.
		This keeps the broader model independent from the simpler
		``captioned_measurement_grid`` detector and its precedence.
		"""
		lines = self.lines_by_page.get(page, [])
		_page_width, page_height = self.conv.page_sizes.get(
			page,
			(612.0, 792.0),
		)
		out: List[
			Tuple[
				float,
				str,
				List[Line],
				Tuple[float, float, float, float],
			]
		] = []
		claimed_body_lines: set[int] = set()
		for body_index, first_line in enumerate(lines):
			if id(first_line) in claimed_body_lines:
				continue
			first_groups = self._captioned_measurement_groups(first_line)
			column_count = len(first_groups)
			if (
				first_line.writing_mode != "horizontal"
				or not (3 <= column_count <= 8)
				or not all(
					self._is_tiered_numeric_grid_cell(group[0])
					for group in first_groups
				)
			):
				continue
			if body_index:
				previous_line = lines[body_index - 1]
				previous_groups = self._captioned_measurement_groups(
					previous_line
				)
				if (
					len(previous_groups) == column_count
					and all(
						self._is_tiered_numeric_grid_cell(group[0])
						for group in previous_groups
					)
					and first_line.y0 - previous_line.y0
						<= max(34.0, first_line.size * 3.4)
				):
					# Only the first complete row in a numeric run may seed a
					# model.  Otherwise a sparse trailing row could be mistaken
					# for an extra header of a shorter sub-run.
					continue

			body_rows: List[
				Tuple[
					Line,
					List[Tuple[str, float, float]],
				]
			] = []
			previous: Optional[Line] = None
			for line in lines[body_index:]:
				if line.writing_mode != "horizontal":
					break
				groups = self._captioned_measurement_groups(line)
				if (
					len(groups) != column_count
					or not all(
						self._is_tiered_numeric_grid_cell(group[0])
						for group in groups
					)
				):
					break
				if (
					previous is not None
					and (
						line.y0 <= previous.y0
						or line.y0 - previous.y0
							> max(34.0, first_line.size * 3.4)
					)
				):
					break
				body_rows.append((line, groups))
				previous = line
			if len(body_rows) < 3:
				continue

			body_size = median(
				[
					line.size
					for line, _groups in body_rows
					if line.size > 0
				]
			) if any(line.size > 0 for line, _groups in body_rows) else 10.0
			anchor_tolerance = max(4.0, body_size * 0.70)
			anchors: List[float] = []
			stable = True
			for column in range(column_count):
				values = [
					groups[column][1]
					for _line, groups in body_rows
				]
				anchor = median(values)
				if max(abs(value - anchor) for value in values) > anchor_tolerance:
					stable = False
					break
				anchors.append(anchor)
			if (
				not stable
				or any(
					right - left < max(22.0, body_size * 2.1)
					for left, right in zip(anchors, anchors[1:])
				)
			):
				continue

			body_gaps: List[Tuple[float, float]] = []
			for column in range(column_count - 1):
				left_edge = max(
					groups[column][2]
					for _line, groups in body_rows
				)
				right_edge = min(
					groups[column + 1][1]
					for _line, groups in body_rows
				)
				if right_edge - left_edge < max(5.0, body_size * 0.45):
					break
				body_gaps.append((left_edge, right_edge))
			if len(body_gaps) != column_count - 1:
				continue

			context = self._tiered_numeric_grid_context(
				page,
				lines,
				body_index,
				body_rows,
				page_height,
			)
			if context is None:
				continue
			caption_lines, header_lines, page_top_continuation = context
			# A single-line captioned header is owned by the established,
			# narrower measurement detector.  The broader model is needed for a
			# wrapped/tiered header or for a genuine page-top continuation.
			if (
				not page_top_continuation
				and len(header_lines) == 1
			):
				continue
			if (
				page_top_continuation
				and len(header_lines) == 1
				and header_lines[0].bold_ratio >= 0.70
				and len(
					self._borderless_column_cells(header_lines[0])
				) == column_count
			):
				# Preserve the established single-baseline borderless-numeric
				# model when its own bold-header grouping is already complete.
				continue
			header_tiers = self._tiered_numeric_header_tiers(header_lines)
			if not (1 <= len(header_tiers) <= 3):
				continue

			xs = self._tiered_numeric_grid_xs(
				header_lines,
				body_rows,
				body_gaps,
				body_size,
			)
			if xs is None:
				continue
			y_margin = max(2.0, body_size * 0.24)
			ys = [
				min(line.y0 for line in header_tiers[0]) - y_margin,
				*[
					(
						max(line.y1 for line in left)
						+ min(line.y0 for line in right)
					) / 2.0
					for left, right in zip(header_tiers, header_tiers[1:])
				],
				(
					max(line.y1 for line in header_tiers[-1])
					+ body_rows[0][0].y0
				) / 2.0,
				*[
					(left.y1 + right.y0) / 2.0
					for left, right in zip(
						[line for line, _groups in body_rows],
						[line for line, _groups in body_rows][1:],
					)
				],
				body_rows[-1][0].y1 + y_margin,
			]
			if (
				any(right <= left for left, right in zip(xs, xs[1:]))
				or any(bottom <= top for top, bottom in zip(ys, ys[1:]))
			):
				continue

			table_lines = [
				*header_lines,
				*[line for line, _groups in body_rows],
			]
			occupancy = self._partial_grid_occupancy(
				table_lines,
				xs,
				ys,
			)
			if (
				len(occupancy)
					!= len(header_tiers) + len(body_rows)
				or any(
					row != set(range(column_count))
					for row in occupancy[len(header_tiers):]
				)
				or any(
					len(row) < max(2, math.ceil(column_count * 0.50))
					for row in occupancy[:len(header_tiers)]
				)
			):
				continue
			alpha_columns = {
				max(
					0,
					min(
						column_count - 1,
						find_interval(
							list(xs),
							(char.x0 + char.x1) / 2.0,
						),
					),
				)
				for line in header_lines
				for char in line.chars
				if re.search(r"[A-Za-z]", char.text)
			}
			if len(alpha_columns) < max(2, math.ceil(column_count * 0.60)):
				continue

			caption_text = cleanup_spaces(
				" ".join(
					plain_text(line_text_tokens(line)).strip()
					for line in caption_lines
				)
			)
			box = (
				float(xs[0]),
				float(ys[0]),
				float(xs[-1]),
				float(ys[-1]),
			)
			html = self._render_partial_grid_html(
				page,
				xs,
				ys,
				table_lines,
				"",
			)
			markdown = (
				caption_text + "\n\n" + html
				if caption_text
				else html
			)
			admission = (
				"page_top_continuation"
				if page_top_continuation
				else "explicit_caption"
			)
			self._partial_table_models[(page, box)] = {
				"model_kind": "tiered_numeric_grid",
				"xs": list(xs),
				"ys": list(ys),
				"header_rows": len(header_tiers),
				"evidence": {
					"admission": admission,
					"caption": caption_text,
					"body_rows": len(body_rows),
					"header_rows": len(header_tiers),
					"header_physical_lines": len(header_lines),
					"stable_column_anchors": column_count,
					"numeric_columns": column_count,
					"header_whitespace_gutters": column_count - 1,
				},
			}
			out.append(
				(
					caption_lines[0].y0
						if caption_lines
						else min(line.y0 for line in header_lines),
					markdown,
					[*caption_lines, *table_lines],
					box,
				)
			)
			claimed_body_lines.update(
				id(line)
				for line, _groups in body_rows
			)
		return out

	def _tiered_numeric_grid_context(
		self,
		page: int,
		lines: Sequence[Line],
		body_index: int,
		body_rows: Sequence[
			Tuple[
				Line,
				List[Tuple[str, float, float]],
			]
		],
		page_height: float,
	) -> Optional[Tuple[List[Line], List[Line], bool]]:
		body_line = body_rows[0][0]
		body_size = max(1.0, body_line.size)
		caption_index: Optional[int] = None
		caption_window = max(160.0, body_size * 18.0)
		for index in range(body_index - 1, -1, -1):
			line = lines[index]
			if body_line.y0 - line.y0 > caption_window:
				break
			text = plain_text(line_text_tokens(line)).strip()
			if self._is_explicit_table_caption(text):
				caption_index = index
				break
		if caption_index is not None:
			caption_lines = [lines[caption_index]]
			cursor = caption_index + 1
			while cursor < body_index and len(caption_lines) < 3:
				line = lines[cursor]
				text = plain_text(line_text_tokens(line)).strip()
				previous = caption_lines[-1]
				if (
					line.writing_mode != "horizontal"
					or not text
					or self._is_explicit_table_caption(text)
					or line.y0 - previous.y1
						> max(10.0, previous.size * 0.75)
					or abs(line.size - caption_lines[0].size)
						> max(1.5, caption_lines[0].size * 0.20)
					or abs(line.bold_ratio - caption_lines[0].bold_ratio) > 0.35
				):
					break
				caption_lines.append(line)
				cursor += 1
			header_lines = [
				line
				for line in lines[cursor:body_index]
				if (
					line.writing_mode == "horizontal"
					and plain_text(line_text_tokens(line)).strip()
				)
			]
			if (
				not (1 <= len(header_lines) <= 8)
				or body_line.y0 - min(line.y0 for line in header_lines)
					> max(90.0, body_size * 11.0)
				or body_line.y0 - max(line.y1 for line in header_lines)
					> max(16.0, body_size * 2.0)
				or min(line.y0 for line in header_lines)
					- caption_lines[-1].y1
					> max(72.0, body_size * 8.0)
			):
				return None
			return caption_lines, header_lines, False

		# A continuation at the page top may have no repeated caption.  Collect
		# only the compact header block immediately above the body.  At most one
		# short preceding fragment is allowed (for example, the final word of a
		# paragraph continued from the previous page).
		header_lines_reversed: List[Line] = []
		following = body_line
		cursor = body_index - 1
		while cursor >= 0 and len(header_lines_reversed) < 8:
			line = lines[cursor]
			text = plain_text(line_text_tokens(line)).strip()
			if not text:
				cursor -= 1
				continue
			if (
				line.writing_mode != "horizontal"
				or following.y0 - line.y1
					> max(12.0, body_size * 1.75)
			):
				break
			header_lines_reversed.append(line)
			following = line
			cursor -= 1
		header_lines = list(reversed(header_lines_reversed))
		if not header_lines:
			return None
		header_top = min(line.y0 for line in header_lines)
		if (
			header_top > page_height * 0.20
			or body_line.y0 > page_height * 0.34
		):
			return None
		prior_lines = [
			line
			for line in lines[:cursor + 1]
			if plain_text(line_text_tokens(line)).strip()
		]
		if len(prior_lines) > 1:
			return None
		if prior_lines:
			prior = prior_lines[-1]
			prior_text = plain_text(line_text_tokens(prior)).strip()
			if (
				len(prior_text) > 48
				or header_top - prior.y1 < max(18.0, body_size * 2.0)
			):
				return None
		return [], header_lines, True

	def _tiered_numeric_header_tiers(
		self,
		header_lines: Sequence[Line],
	) -> List[List[Line]]:
		tiers: List[List[Line]] = []
		for line in sorted(
			header_lines,
			key=lambda item: (item.y0, item.x0, item.seq),
		):
			if (
				tiers
				and line.y0
					<= max(item.y1 for item in tiers[-1])
						+ max(0.75, line.size * 0.08)
			):
				tiers[-1].append(line)
			else:
				tiers.append([line])
		return tiers

	def _tiered_numeric_grid_xs(
		self,
		header_lines: Sequence[Line],
		body_rows: Sequence[
			Tuple[
				Line,
				List[Tuple[str, float, float]],
			]
		],
		body_gaps: Sequence[Tuple[float, float]],
		body_size: float,
	) -> Optional[List[float]]:
		intervals = sorted(
			(box_x0, box_x1)
			for line in header_lines
			for _text, box_x0, _box_y0, box_x1, _box_y1 in word_boxes(line)
		)
		if not intervals:
			return None
		merged: List[List[float]] = []
		for left, right in intervals:
			if merged and left <= merged[-1][1] + 0.25:
				merged[-1][1] = max(merged[-1][1], right)
			else:
				merged.append([left, right])
		minimum_gutter = max(1.5, body_size * 0.15)
		boundaries: List[float] = []
		for body_left, body_right in body_gaps:
			gutters = [
				(left[1], right[0])
				for left, right in zip(merged, merged[1:])
				if right[0] - left[1] >= minimum_gutter
				and body_left
					<= (left[1] + right[0]) / 2.0
					<= body_right
			]
			if not gutters:
				return None
			max_width = max(right - left for left, right in gutters)
			near_widest = [
				(left, right)
				for left, right in gutters
				if right - left
					>= max_width - max(0.35, body_size * 0.04)
			]
			left, right = max(
				near_widest,
				key=lambda gutter: (gutter[0] + gutter[1]) / 2.0,
			)
			boundaries.append((left + right) / 2.0)
		if len(boundaries) != len(body_gaps):
			return None
		x_margin = max(2.0, body_size * 0.25)
		all_lefts = [
			group[1]
			for _line, groups in body_rows
			for group in groups
		] + [left for left, _right in intervals]
		all_rights = [
			group[2]
			for _line, groups in body_rows
			for group in groups
		] + [right for _left, right in intervals]
		return [
			min(all_lefts) - x_margin,
			*boundaries,
			max(all_rights) + x_margin,
		]

	def _is_tiered_numeric_grid_cell(self, text: str) -> bool:
		value = cleanup_spaces(text).strip()
		value = re.sub(r"^[*†‡§¶#]+\s*", "", value)
		if self._is_captioned_measurement_cell(value):
			return True
		number = r"(?:\d[\d,]*(?:\.\d+)?|\.\d+)"
		return bool(
			re.fullmatch(
				r"(?:[<>=≤≥~≈]\s*)?"
				r"[\(\[]?[+\-\u2212]?"
				+ number
				+ r"(?:\s*(?:[-\u2013\u2014]|\bto\b)\s*"
					r"[+\-\u2212]?" + number + r")?"
				r"\s*\+?\s*%?[\)\]]?",
				value,
				re.I,
			)
		)

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

	def _borderless_key_value_group_is_admissible(
		self,
		page: int,
		group: Sequence[Tuple[Line, Tuple[str, str]]],
	) -> bool:
		"""Require a shared value column and reject navigation-like row runs.

		Bold sentence openings are common in prose, while genuine field/value
		rows place their values on a repeated column boundary.  Long labels are
		accepted only when the source carries an explicit delimiter, and a run of
		ascending page numbers beneath a Contents heading remains navigation even
		when the PDF omits dot leaders or link annotations.
		"""
		details = [self._borderless_key_value_row_details(line) for line, _row in group]
		if any(detail is None for detail in details):
			return False
		parsed = [detail for detail in details if detail is not None]
		value_starts = [detail[3] for detail in parsed]
		if any(value_start is None for value_start in value_starts):
			return False
		starts = [float(value_start) for value_start in value_starts if value_start is not None]
		body_size = median([line.size for line, _row in group])
		if max(starts) - min(starts) > max(1.5, body_size * 0.75):
			return False
		key_word_counts = [detail[4] for detail in parsed]
		if median(key_word_counts) > 3 and not all(detail[5] for detail in parsed):
			return False
		if self._borderless_key_value_group_is_navigation(page, group, parsed):
			return False
		return True

	def _borderless_key_value_group_is_navigation(
		self,
		page: int,
		group: Sequence[Tuple[Line, Tuple[str, str]]],
		details: Sequence[Tuple[str, str, str, Optional[float], int, bool]],
	) -> bool:
		if len(group) < 3:
			return False
		first_y = min(line.y0 for line, _row in group)
		has_contents_heading = any(
			line.y0 < first_y
			and re.fullmatch(
				r"(?:table\s+of\s+)?contents\s*[:.]?",
				cleanup_spaces(plain_text(line_text_tokens(line))).strip(),
				re.I,
			)
			for line in self.lines_by_page.get(page, [])
		)
		if not has_contents_heading:
			return False
		page_numbers: List[int] = []
		for detail in details:
			match = re.fullmatch(r"(?:p(?:age)?\.?\s*)?(\d{1,4})", detail[2], re.I)
			if match is None:
				return False
			page_numbers.append(int(match.group(1)))
		if not all(right > left for left, right in zip(page_numbers, page_numbers[1:])):
			return False
		right_edges = [line.x1 for line, _row in group]
		body_size = median([line.size for line, _row in group])
		return max(right_edges) - min(right_edges) <= max(1.5, body_size * 0.75)

	def _borderless_key_value_row(self, line: Line) -> Optional[Tuple[str, str]]:
		detail = self._borderless_key_value_row_details(line)
		return (detail[0], detail[1]) if detail is not None else None

	def _borderless_key_value_row_details(
		self,
		line: Line,
	) -> Optional[Tuple[str, str, str, Optional[float], int, bool]]:
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
		value_plain = cleanup_spaces(plain_text(value_tokens)).strip()
		if not key or not value:
			return None
		if len(key) > 40 or len(value) > 120:
			return None
		value_lefts = [
			float(token["bbox"][0])
			for token in value_tokens
			if token.get("text", "").strip()
			and isinstance(token.get("bbox"), tuple)
			and len(token["bbox"]) == 4
		]
		value_x0 = min(value_lefts) if value_lefts else None
		key_word_count = len(re.findall(r"\w+(?:[\u2019'-]\w+)*", key, re.UNICODE))
		explicit_delimiter = bool(
			re.search(r"[:=]\s*$", key)
			or re.match(r"^\s*[:=]", value_plain)
		)
		return key, value, value_plain, value_x0, key_word_count, explicit_delimiter

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


def adaptive_subnominal_word_gap_pairs(
	chars: Sequence[Char],
	base_size: float,
) -> Set[Tuple[int, int]]:
	"""Find repeatedly evidenced word gaps just below the nominal threshold.

	TeX justification may shrink every inter-word adjustment on a line by a few
	percent.  A fixed font-space threshold then joins all of those words even
	though the PDF repeats one distinct positive gap throughout the line.  Admit
	that narrower cohort only when it occurs at least three times, remains sparse
	among the character boundaries, and is accompanied by substantially more
	near-zero intra-word joins.  Those conditions keep isolated metric rounding
	and deliberately tracked text on the conservative path.
	"""
	visible = [char for char in chars if char.text and not char.text.isspace()]
	if len(visible) < 8:
		return set()
	# Repeated operator padding is mathematical layout, not prose word
	# separation.  Keep the veto limited to formula-dominated runs: explanatory
	# prose often contains units or an inline equation and still needs the same
	# TeX-justification repair as ordinary sentences.
	if is_formula_dominated_text("".join(char.text for char in visible)):
		return set()
	candidates: List[Tuple[float, Char, Char]] = []
	compact_joins = 0
	for previous, current in zip(visible, visible[1:]):
		gap = current.x0 - previous.x1
		if gap <= max(0.5, base_size * 0.05):
			compact_joins += 1
		threshold = max(
			1.2,
			min(
				previous.size * 0.45,
				standard_width(previous.font.base_font, " ", 278)
				/ 1000.0
				* previous.size
				* 0.8,
			),
		)
		if (
			max(1.2, threshold * 0.94) <= gap < threshold * 0.99
			and not suppress_geometric_space(previous, current, gap)
		):
			candidates.append((gap, previous, current))
	if len(candidates) < 3:
		return set()

	tolerance = max(0.08, base_size * 0.015)
	cohorts = [
		[
			candidate
			for candidate in candidates
			if abs(candidate[0] - anchor[0]) <= tolerance
		]
		for anchor in candidates
	]
	cohort = max(cohorts, key=len)
	boundary_count = len(visible) - 1
	if (
		len(cohort) < 3
		or len(cohort) > max(3, int(boundary_count * 0.35))
		or compact_joins < len(cohort) * 2
	):
		return set()
	return {(previous.seq, current.seq) for _gap, previous, current in cohort}


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
	adaptive_word_gaps = adaptive_subnominal_word_gap_pairs(chars, base_size)
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
			# space width because of PDF metric rounding. A lone boundary gets
			# only a one-percent tolerance; repeated justified gaps may use the
			# separately bounded line-level evidence above.
			if (
				(
					gap >= threshold * 0.99
					or (prev.seq, ch.seq) in adaptive_word_gaps
				)
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


def is_formula_dominated_text(text: str) -> bool:
	"""Return true when mathematical notation dominates rather than decorates prose."""
	value = text.strip()
	if not is_formula_like_text(value):
		return False
	prose_runs = re.findall(r"[^\W\d_]{3,}", value, flags=re.UNICODE)
	# Two substantial word runs are enough to establish explanatory prose even
	# when source kerning removed its spaces (for example, ``inwhich`` and
	# ``istheviscosity`` around a displayed unit).  Short function/variable names
	# such as ``sin``, ``det``, ``cp``, or ``hp`` do not by themselves defeat the
	# formula veto.
	return not (
		len(prose_runs) >= 2
		and sum(len(run) for run in prose_runs) >= 12
	)


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


def painted_path_contains_point(path: PaintedPath, x: float, y: float) -> bool:
	"""Evaluate PDF nonzero/even-odd fill membership at one page-space point.

	Curves are flattened with a fixed geometric tolerance and recursion limit,
	which keeps the result deterministic across platforms while preserving
	concave edges and compound-path holes. Open subpaths are implicitly closed,
	as required by PDF fill painting operators.
	"""
	if not (path.bbox[0] <= x <= path.bbox[2] and path.bbox[1] <= y <= path.bbox[3]):
		return False

	def point_line_distance(
		point: Tuple[float, float],
		start: Tuple[float, float],
		end: Tuple[float, float],
	) -> float:
		dx = end[0] - start[0]
		dy = end[1] - start[1]
		if abs(dx) + abs(dy) <= 1e-12:
			return math.hypot(point[0] - start[0], point[1] - start[1])
		return abs(
			dy * point[0]
			- dx * point[1]
			+ end[0] * start[1]
			- end[1] * start[0]
		) / math.hypot(dx, dy)

	def flatten_cubic(
		start: Tuple[float, float],
		control1: Tuple[float, float],
		control2: Tuple[float, float],
		end: Tuple[float, float],
		depth: int = 0,
	) -> List[Tuple[float, float]]:
		flatness = max(
			point_line_distance(control1, start, end),
			point_line_distance(control2, start, end),
		)
		if flatness <= 0.25 or depth >= 12:
			return [end]
		start_control = (
			(start[0] + control1[0]) / 2.0,
			(start[1] + control1[1]) / 2.0,
		)
		control_mid = (
			(control1[0] + control2[0]) / 2.0,
			(control1[1] + control2[1]) / 2.0,
		)
		control_end = (
			(control2[0] + end[0]) / 2.0,
			(control2[1] + end[1]) / 2.0,
		)
		left_control = (
			(start_control[0] + control_mid[0]) / 2.0,
			(start_control[1] + control_mid[1]) / 2.0,
		)
		right_control = (
			(control_mid[0] + control_end[0]) / 2.0,
			(control_mid[1] + control_end[1]) / 2.0,
		)
		midpoint = (
			(left_control[0] + right_control[0]) / 2.0,
			(left_control[1] + right_control[1]) / 2.0,
		)
		return flatten_cubic(
			start,
			start_control,
			left_control,
			midpoint,
			depth + 1,
		) + flatten_cubic(
			midpoint,
			right_control,
			control_end,
			end,
			depth + 1,
		)

	subpaths: List[List[Tuple[float, float]]] = []
	current_path: List[Tuple[float, float]] = []
	current: Optional[Tuple[float, float]] = None
	start: Optional[Tuple[float, float]] = None

	def finish() -> None:
		nonlocal current_path
		if len(current_path) >= 3:
			if current_path[-1] != current_path[0]:
				current_path.append(current_path[0])
			subpaths.append(current_path)
		current_path = []

	for command, values in path.commands:
		if command == "M" and len(values) >= 2:
			finish()
			current = (values[0], values[1])
			start = current
			current_path = [current]
		elif command == "L" and len(values) >= 2:
			point = (values[0], values[1])
			if current is None:
				start = point
				current_path = [point]
			else:
				current_path.append(point)
			current = point
		elif command == "C" and len(values) >= 6 and current is not None:
			control1 = (values[0], values[1])
			control2 = (values[2], values[3])
			end = (values[4], values[5])
			current_path.extend(flatten_cubic(current, control1, control2, end))
			current = end
		elif command == "Z":
			finish()
			current = start
			current_path = [start] if start is not None else []
	finish()
	if not subpaths:
		return False

	def on_segment(
		point: Tuple[float, float],
		start_point: Tuple[float, float],
		end_point: Tuple[float, float],
	) -> bool:
		if point_line_distance(point, start_point, end_point) > 1e-7:
			return False
		return (
			min(start_point[0], end_point[0]) - 1e-7
			<= point[0]
			<= max(start_point[0], end_point[0]) + 1e-7
			and min(start_point[1], end_point[1]) - 1e-7
			<= point[1]
			<= max(start_point[1], end_point[1]) + 1e-7
		)

	point = (x, y)
	crossings = 0
	winding = 0
	for subpath in subpaths:
		for start_point, end_point in zip(subpath, subpath[1:]):
			if on_segment(point, start_point, end_point):
				return True
			y0, y1 = start_point[1], end_point[1]
			if not ((y0 <= y < y1) or (y1 <= y < y0)):
				continue
			intersection_x = start_point[0] + (
				(y - y0) * (end_point[0] - start_point[0]) / (y1 - y0)
			)
			if intersection_x <= x:
				continue
			crossings += 1
			winding += 1 if y0 < y1 else -1
	return crossings % 2 == 1 if path.fill_rule == "evenodd" else winding != 0


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


def point_distance(
	left: Tuple[float, float],
	right: Tuple[float, float],
) -> float:
	return math.hypot(right[0] - left[0], right[1] - left[1])


def axis_aligned_rectangle_bbox(
	points: Sequence[Tuple[float, float]],
	tolerance: float,
) -> Optional[Tuple[float, float, float, float]]:
	"""Return a bbox only when four ordered points are an exact rectangle."""
	if len(points) != 4:
		return None
	xs = cluster_values([point[0] for point in points], tolerance)
	ys = cluster_values([point[1] for point in points], tolerance)
	if len(xs) != 2 or len(ys) != 2:
		return None
	for left, right in zip(points, (*points[1:], points[0])):
		dx = abs(right[0] - left[0])
		dy = abs(right[1] - left[1])
		if (dx <= tolerance) == (dy <= tolerance):
			return None
	corner_hits = {
		(
			min(range(2), key=lambda index: abs(point[0] - xs[index])),
			min(range(2), key=lambda index: abs(point[1] - ys[index])),
		)
		for point in points
	}
	if len(corner_hits) != 4:
		return None
	return (xs[0], ys[0], xs[1], ys[1])


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
