from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple


_CMAP_RANGE_EXPANSION_LIMIT = 65536


class CMapMapping(dict):
	def __init__(self) -> None:
		super().__init__()
		self.code_space_ranges: List[Tuple[bytes, bytes]] = []

	def add_codespace(self, low: bytes, high: bytes) -> None:
		if low and len(low) == len(high) and low <= high:
			self.code_space_ranges.append((low, high))


GLYPH_NAME_TO_UNICODE = {
	"space": " ",
	"hyphen": "-",
	"minus": "-",
	"period": ".",
	"comma": ",",
	"colon": ":",
	"semicolon": ";",
	"parenleft": "(",
	"parenright": ")",
	"bracketleft": "[",
	"bracketright": "]",
	"braceleft": "{",
	"braceright": "}",
	"bullet": "\u2022",
	"emdash": "\u2014",
	"endash": "\u2013",
	"ellipsis": "\u2026",
	"quotedblleft": "\u201c",
	"quotedblright": "\u201d",
	"quoteleft": "\u2018",
	"quoteright": "\u2019",
	"fi": "fi",
	"fl": "fl",
	"ffi": "ffi",
	"ffl": "ffl",
	"ff": "ff",
}


def decode_font(font: Any, data: bytes) -> List[Tuple[bytes, str, float]]:
	from ..core import normalize_ligatures, winansi_char

	out: List[Tuple[bytes, str, float]] = []
	if font.composite:
		encoding = str(getattr(font, "encoding", "")).lstrip("/")
		identity = encoding in ("Identity-H", "Identity-V")
		unicode_predefined = bool(re.fullmatch(r"Uni(?:JIS|KS|GB|CNS)-(?:UTF16|UCS2)-[HV]", encoding))
		index = 0
		while index < len(data):
			if unicode_predefined and not font.to_unicode:
				code, text = _next_predefined_unicode_code(data, index, encoding)
			else:
				code = _next_composite_code(data, index, font.to_unicode, identity)
				text = font.to_unicode.get(code) if code else None
			if not code:
				break
			index += len(code)
			cid = int.from_bytes(code, "big") if code else 0
			if text is None:
				text = ""
			out.append(
				(code, normalize_ligatures(text), font.width_for_code(cid))
			)
		return out

	for value in data:
		code = bytes([value])
		text = font.to_unicode.get(code)
		if text is None:
			glyph_name = font.differences.get(value)
			if glyph_name is not None:
				# A Differences entry overrides the base encoding.  Falling back to
				# WinAnsi when that glyph name is unknown fabricates Unicode (especially
				# for Type 3 and symbolic fonts), so preserve an explicit unknown.
				text = glyph_name_to_unicode(glyph_name) or "\ufffd"
		if text is None:
			encoding = str(font.encoding).lstrip("/")
			if encoding == "MacRomanEncoding":
				text = bytes([value]).decode("mac_roman", "replace")
			else:
				text = winansi_char(value, font.base_font)
		out.append(
			(code, normalize_ligatures(text), font.width_for_code(value))
		)
	return out


def _next_predefined_unicode_code(data: bytes, index: int, encoding: str) -> Tuple[bytes, str]:
	if index + 2 > len(data):
		return data[index:], "\ufffd"
	width = 2
	first = int.from_bytes(data[index : index + 2], "big")
	if "UTF16" in encoding and 0xD800 <= first <= 0xDBFF and index + 4 <= len(data):
		second = int.from_bytes(data[index + 2 : index + 4], "big")
		if 0xDC00 <= second <= 0xDFFF:
			width = 4
	code = data[index : index + width]
	try:
		return code, code.decode("utf-16-be")
	except UnicodeDecodeError:
		return code, "\ufffd"


def _next_composite_code(data: bytes, index: int, mapping: Dict[bytes, str], identity: bool) -> bytes:
	ranges = list(getattr(mapping, "code_space_ranges", []))
	if ranges:
		for width in sorted({len(low) for low, _high in ranges}):
			candidate = data[index : index + width]
			if len(candidate) != width:
				continue
			if any(len(low) == width and low <= candidate <= high for low, high in ranges):
				return candidate
	mapped_lengths = sorted({len(code) for code in mapping if code}, reverse=True)
	for width in mapped_lengths:
		candidate = data[index : index + width]
		if len(candidate) == width and candidate in mapping:
			return candidate
	width = 2 if identity and index + 1 < len(data) else 1
	return data[index : index + width]


def _hex_pairs(body: str) -> Iterable[Tuple[str, str]]:
	return re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", body)


def parse_encoding_differences(encoding: Any) -> Dict[int, str]:
	out: Dict[int, str] = {}
	if not isinstance(encoding, dict):
		return out
	differences = encoding.get("Differences")
	if not isinstance(differences, list):
		return out
	current: Optional[int] = None
	for item in differences:
		if isinstance(item, int):
			current = item
		elif current is not None and isinstance(item, str):
			out[current] = str(item)
			current += 1
	return out


def glyph_name_to_unicode(name: Optional[str]) -> Optional[str]:
	if not name:
		return None
	name = str(name).lstrip("/").split(".", 1)[0]
	if name in GLYPH_NAME_TO_UNICODE:
		return GLYPH_NAME_TO_UNICODE[name]
	if "_" in name:
		parts = [glyph_name_to_unicode(part) for part in name.split("_")]
		return "".join(part for part in parts if part is not None) if all(part is not None for part in parts) else None
	if len(name) == 1:
		return name
	if name in {"zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"}:
		return str(("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine").index(name))
	match = re.fullmatch(r"uni((?:[0-9A-Fa-f]{4})+)", name)
	if match:
		groups = re.findall(r"[0-9A-Fa-f]{4}", match.group(1))
		return "".join(chr(int(group, 16)) for group in groups)
	match = re.fullmatch(r"u([0-9A-Fa-f]{4,6})", name)
	if match:
		value = int(match.group(1), 16)
		return chr(value) if value <= 0x10FFFF and not 0xD800 <= value <= 0xDFFF else None
	return None


def parse_tounicode(data: bytes) -> CMapMapping:
	text = data.decode("latin1", "ignore")
	out = CMapMapping()
	for block in re.finditer(r"begincodespacerange(.*?)endcodespacerange", text, re.S):
		for low, high in _hex_pairs(block.group(1)):
			try:
				out.add_codespace(bytes.fromhex(low), bytes.fromhex(high))
			except ValueError:
				continue
	for block in re.finditer(r"beginbfchar(.*?)endbfchar", text, re.S):
		for source, destination in re.findall(
			r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>",
			block.group(1),
		):
			out[bytes.fromhex(source)] = _utf16_hex(destination)
	for block in re.finditer(r"beginbfrange(.*?)endbfrange", text, re.S):
		for kind, low, high, destinations in _bfrange_entries(block.group(1)):
			bounds = _bfrange_bounds(low, high)
			if bounds is None:
				continue
			low_bytes, start, end = bounds
			if kind == "scalar":
				try:
					destination_bytes = bytes.fromhex(destinations[0])
				except (IndexError, ValueError):
					continue
				if not destination_bytes:
					continue
				destination_start = int.from_bytes(destination_bytes, "big")
				for offset, code in enumerate(range(start, end + 1)):
					value = destination_start + offset
					try:
						encoded = value.to_bytes(len(destination_bytes), "big")
					except OverflowError:
						break
					out[code.to_bytes(len(low_bytes), "big")] = _utf16_hex(encoded.hex())
				continue
			for offset, destination in enumerate(destinations[: end - start + 1]):
				try:
					decoded = _utf16_hex(destination)
				except ValueError:
					continue
				out[(start + offset).to_bytes(len(low_bytes), "big")] = decoded
	return out


def _bfrange_entries(
	body: str,
) -> Iterable[Tuple[str, str, str, List[str]]]:
	"""Parse mutually exclusive scalar/array bfrange grammar entries.

	A token cursor is intentional here.  Regex searches can restart inside an
	array (including a malformed, unterminated one) and reinterpret destination
	hex strings as a new scalar range.  Once a valid source-pair prefix opens an
	array, the cursor consumes the whole bracketed value or the remainder of the
	block, so destination tokens are never promoted to source codes.
	"""
	clean = re.sub(r"%[^\r\n]*", " ", body)
	tokens: List[Tuple[str, str]] = []
	for match in re.finditer(r"<([0-9A-Fa-f]+)>|(\[)|(\])|(\S+)", clean):
		if match.group(1) is not None:
			tokens.append(("hex", match.group(1)))
		elif match.group(2) is not None:
			tokens.append(("open", "["))
		elif match.group(3) is not None:
			tokens.append(("close", "]"))
		else:
			tokens.append(("other", match.group(4)))

	index = 0
	while index < len(tokens):
		if (
			index + 2 >= len(tokens)
			or tokens[index][0] != "hex"
			or tokens[index + 1][0] != "hex"
		):
			index += 1
			continue
		low = tokens[index][1]
		high = tokens[index + 1][1]
		third_kind, third_value = tokens[index + 2]
		if third_kind == "hex":
			yield ("scalar", low, high, [third_value])
			index += 3
			continue
		if third_kind != "open":
			index += 3
			continue

		cursor = index + 3
		values: List[str] = []
		malformed = False
		while cursor < len(tokens) and tokens[cursor][0] != "close":
			kind, value = tokens[cursor]
			if kind == "hex":
				values.append(value)
			else:
				malformed = True
			cursor += 1
		if cursor >= len(tokens):
			break
		if not malformed:
			yield ("array", low, high, values)
		index = cursor + 1


def _bfrange_bounds(
	low: str,
	high: str,
) -> Optional[Tuple[bytes, int, int]]:
	try:
		low_bytes = bytes.fromhex(low)
		high_bytes = bytes.fromhex(high)
	except ValueError:
		return None
	if (
		not low_bytes
		or len(low_bytes) > 4
		or len(low_bytes) != len(high_bytes)
	):
		return None
	start = int.from_bytes(low_bytes, "big")
	end = int.from_bytes(high_bytes, "big")
	count = end - start + 1
	if count <= 0 or count > _CMAP_RANGE_EXPANSION_LIMIT:
		return None
	return low_bytes, start, end


def _utf16_hex(value: str) -> str:
	data = bytes.fromhex(value)
	try:
		text = data.decode("utf-16-be")
	except UnicodeDecodeError:
		text = data.decode("latin1", "replace")
	return (
		text.replace("\ufb00", "ff")
		.replace("\ufb01", "fi")
		.replace("\ufb02", "fl")
		.replace("\ufb03", "ffi")
		.replace("\ufb04", "ffl")
	)
