from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional


@dataclass(frozen=True)
class InlineImageToken:
	attrs: Dict[str, Any]
	data: bytes


OPERATOR_NAMES = {
	"q", "Q", "cm", "BT", "ET", "Tf", "TL", "Tc", "Tw", "Tz", "Ts",
	"Tr", "Td", "TD", "Tm", "T*", "Tj", "TJ", "'", '"', "rg", "RG",
	"g", "G", "k", "K", "w", "m", "l", "c", "v", "y", "re", "S", "s", "f", "F",
	"f*", "B", "B*", "b", "b*", "n", "Do", "BMC", "BDC", "EMC",
	"cs", "CS", "sc", "scn", "SC", "SCN", "W", "W*", "h", "J",
	"j", "M", "d", "gs",
}


def tokenize_content(data: bytes) -> Iterable[Any]:
	from ..core import Operator, PdfLexer, parse_content_array, parse_content_dict

	lexer = PdfLexer(data)
	while True:
		token = lexer.next_token()
		if token is None:
			break
		kind, value, _start, _end = token
		if kind == "KEYWORD" and value == "BI":
			yield _read_inline_image(lexer, data)
			continue
		if kind == "KEYWORD" and value in OPERATOR_NAMES:
			yield Operator(value)
		elif kind == "ARR_OPEN":
			yield parse_content_array(lexer)
		elif kind == "DICT_OPEN":
			yield parse_content_dict(lexer)
		elif kind == "NAME":
			yield str(value)
		elif kind in ("STRING", "HEXSTRING", "INT", "REAL"):
			yield value
		elif kind == "KEYWORD":
			yield Operator(value)


def _read_inline_image(lexer: Any, content: bytes) -> InlineImageToken:
	from ..core import parse_content_array, parse_content_dict

	attrs: Dict[str, Any] = {}
	key: Optional[str] = None
	while True:
		token = lexer.next_token()
		if token is None:
			return InlineImageToken(attrs, b"")
		kind, value, _start, _end = token
		if kind == "KEYWORD" and value == "ID":
			if lexer.data[lexer.pos : lexer.pos + 2] == b"\r\n":
				lexer.pos += 2
			elif lexer.pos < len(lexer.data) and lexer.data[lexer.pos] in lexer.WHITESPACE:
				lexer.pos += 1
			break
		if kind == "NAME":
			if key is None:
				key = str(value).lstrip("/")
			else:
				attrs[key] = str(value).lstrip("/")
				key = None
		elif key is not None and kind in ("INT", "REAL", "KEYWORD"):
			attrs[key] = value
			key = None
		elif key is not None and kind == "ARR_OPEN":
			attrs[key] = parse_content_array(lexer)
			key = None
		elif key is not None and kind == "DICT_OPEN":
			attrs[key] = parse_content_dict(lexer)
			key = None
	data = content
	start = lexer.pos
	position = start
	exact_end = _inline_image_exact_end(data, start, attrs)
	if exact_end is not None:
		marker = _inline_ei_after(data, exact_end, lexer)
		if marker is not None:
			lexer.pos = marker
			return InlineImageToken(attrs, data[start:exact_end])
	while True:
		found = data.find(b"EI", position)
		if found < 0:
			lexer.pos = len(data)
			return InlineImageToken(attrs, data[start:])
		before = data[found - 1] if found > 0 else 0x20
		after = data[found + 2] if found + 2 < len(data) else 0x20
		if before in lexer.WHITESPACE and (
			after in lexer.WHITESPACE or after in lexer.DELIMS
		) and _plausible_content_tail(data, found + 2, lexer):
			lexer.pos = found + 2
			end = found - 1 if found > start and data[found - 1] in lexer.WHITESPACE else found
			return InlineImageToken(attrs, data[start:end])
		position = found + 2


def _inline_image_exact_end(data: bytes, start: int, attrs: Dict[str, Any]) -> Optional[int]:
	length = attrs.get("L", attrs.get("Length"))
	if isinstance(length, int) and 0 <= length <= len(data) - start:
		return start + length
	filter_name = str(attrs.get("F", attrs.get("Filter", ""))).lstrip("/")
	if filter_name in ("ASCIIHexDecode", "AHx"):
		end = data.find(b">", start)
		return end + 1 if end >= 0 else None
	if filter_name in ("ASCII85Decode", "A85"):
		end = data.find(b"~>", start)
		return end + 2 if end >= 0 else None
	if filter_name in ("DCTDecode", "DCT"):
		end = data.find(b"\xff\xd9", start)
		return end + 2 if end >= 0 else None
	if filter_name in ("RunLengthDecode", "RL"):
		end = data.find(b"\x80", start)
		return end + 1 if end >= 0 else None
	if filter_name:
		return None
	width = attrs.get("W", attrs.get("Width"))
	height = attrs.get("H", attrs.get("Height"))
	bpc = attrs.get("BPC", attrs.get("BitsPerComponent", 8))
	colorspace = str(attrs.get("CS", attrs.get("ColorSpace", "DeviceGray"))).lstrip("/")
	components = {"G": 1, "DeviceGray": 1, "RGB": 3, "DeviceRGB": 3, "CMYK": 4, "DeviceCMYK": 4}.get(colorspace)
	if all(isinstance(value, int) and value > 0 for value in (width, height, bpc)) and components:
		row_bits = int(width) * int(bpc) * components
		length = ((row_bits + 7) // 8) * int(height)
		return start + length if length <= len(data) - start else None
	return None


def _inline_ei_after(data: bytes, end: int, lexer: Any) -> Optional[int]:
	position = end
	while position < len(data) and data[position] in lexer.WHITESPACE:
		position += 1
	if data.startswith(b"EI", position):
		return position + 2
	return None


def _plausible_content_tail(data: bytes, start: int, lexer: Any) -> bool:
	position = start
	while position < len(data) and data[position] in lexer.WHITESPACE:
		position += 1
	if position >= len(data):
		return True
	# Binary false positives tend to be followed by non-PDF bytes.  Require a
	# plausible lexical starter; the content lexer will perform full parsing.
	value = data[position]
	return value in b"/+-.0123456789([<" or 65 <= value <= 90 or 97 <= value <= 122
