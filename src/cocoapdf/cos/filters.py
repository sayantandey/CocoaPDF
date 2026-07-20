from __future__ import annotations

import base64
import re
import zlib
from typing import Any, Dict, List, Optional

from .. import limits


def resolve_decode_parms(document: Any, value: Any) -> Any:
	value = document.resolve(value)
	if isinstance(value, dict):
		return {
			str(key): resolve_decode_parms(document, item)
			for key, item in value.items()
		}
	if isinstance(value, list):
		return [resolve_decode_parms(document, item) for item in value]
	return value


def decode_stream(document: Any, stream: Any) -> bytes:
	if stream.decoded_cache is not None:
		return stream.decoded_cache

	data = stream.raw
	filters = document.resolve(stream.attrs.get("Filter"))
	parms = document.resolve(stream.attrs.get("DecodeParms"))
	if filters is None:
		filter_names: List[str] = []
	elif isinstance(filters, list):
		filter_names = [
			str(document.resolve(item))
			for item in filters
			if item is not None
		]
	else:
		filter_names = [str(filters)]

	if isinstance(parms, list):
		parm_list = [resolve_decode_parms(document, item) for item in parms]
	elif parms is None:
		parm_list = [None] * len(filter_names)
	elif len(filter_names) == 1:
		parm_list = [resolve_decode_parms(document, parms)]
	else:
		parm_list = [None] * len(filter_names)
		document.warn(
			"DECODE_PARMS_MISMATCH",
			"single dictionary with multiple filters",
		)
	while len(parm_list) < len(filter_names):
		parm_list.append(None)

	for filter_name, params in zip(filter_names, parm_list):
		filter_name = filter_name.lstrip("/")
		try:
			if filter_name in ("FlateDecode", "Fl"):
				data = bounded_flate_decode(document, data)
				data = apply_predictor(
					data,
					params if isinstance(params, dict) else None,
				)
			elif filter_name in ("ASCII85Decode", "A85"):
				encoded = data.strip()
				if encoded.startswith(b"<~"):
					encoded = encoded[2:]
				if encoded.endswith(b"~>"):
					encoded = encoded[:-2]
				data = base64.a85decode(encoded, adobe=False)
			elif filter_name in ("ASCIIHexDecode", "AHx"):
				data = ascii_hex_decode(data)
			elif filter_name in ("RunLengthDecode", "RL"):
				data = run_length_decode(data)
			elif filter_name in ("LZWDecode", "LZW"):
				data = lzw_decode(
					data,
					params if isinstance(params, dict) else None,
				)
				data = apply_predictor(data, params if isinstance(params, dict) else None)
			elif filter_name in (
				"DCTDecode",
				"DCT",
				"JPXDecode",
				"JBIG2Decode",
				"CCITTFaxDecode",
			):
				document.warn("FILTER_PASSTHROUGH", filter_name)
			else:
				document.warn("FILTER_UNSUPPORTED", filter_name)
			if len(data) > limits.MAX_DECODED_STREAM:
				raise ValueError("decoded stream limit exceeded")
		except Exception as exc:
			document.warn(
				"FILTER_DECODE_FAILED",
				"%s: %s" % (filter_name, exc),
			)
			break

	if len(data) > limits.MAX_DECODED_STREAM:
		document.warn(
			"DECODED_STREAM_LIMIT",
			"decoded stream byte limit exceeded",
		)
		data = b""
	document.total_decoded += len(data)
	if document.total_decoded > limits.MAX_TOTAL_DECODED:
		document.warn(
			"DECODED_TOTAL_LIMIT",
			"total decoded byte limit exceeded",
		)
		data = b""
	stream.decoded_cache = data
	return data


def _warn_once(document: Any, code: str, detail: str) -> None:
	key = (code, detail)
	seen = getattr(document, "_filter_warnings", set())
	if key in seen:
		return
	seen.add(key)
	document._filter_warnings = seen
	document.warn(code, detail)


def bounded_flate_decode(document: Any, data: bytes) -> bytes:
	decoder = zlib.decompressobj()
	out = decoder.decompress(data, limits.MAX_DECODED_STREAM + 1)
	if len(out) > limits.MAX_DECODED_STREAM or decoder.unconsumed_tail:
		raise ValueError("Flate output exceeds limit")
	remaining = limits.MAX_DECODED_STREAM - len(out)
	tail = decoder.flush(remaining + 1)
	if len(tail) > remaining:
		raise ValueError("Flate output exceeds limit")
	out += tail
	if not decoder.eof:
		_warn_once(document, "FILTER_TRUNCATED", "Flate stream ended before EOD")
	return out


def ascii_hex_decode(data: bytes) -> bytes:
	encoded = re.sub(rb"\s+", b"", data.split(b">", 1)[0])
	encoded = bytes(
		value
		for value in encoded
		if 48 <= value <= 57 or 65 <= value <= 70 or 97 <= value <= 102
	)
	if len(encoded) % 2:
		encoded += b"0"
	return bytes.fromhex(encoded.decode("ascii", "ignore"))


def run_length_decode(data: bytes) -> bytes:
	out = bytearray()
	index = 0
	while index < len(data):
		length = data[index]
		index += 1
		if length == 128:
			break
		if length < 128:
			chunk = data[index : index + length + 1]
			index += length + 1
		else:
			if index >= len(data):
				break
			chunk = bytes([data[index]]) * (257 - length)
			index += 1
		if len(out) + len(chunk) > limits.MAX_DECODED_STREAM:
			raise ValueError("RunLength output exceeds limit")
		out.extend(chunk)
	return bytes(out)


class _MsbBitReader:
	def __init__(self, data: bytes) -> None:
		self.data = data
		self.bit = 0

	def read(self, width: int) -> Optional[int]:
		if self.bit + width > len(self.data) * 8:
			return None
		value = 0
		for _ in range(width):
			byte_index, bit_index = divmod(self.bit, 8)
			value = (value << 1) | ((self.data[byte_index] >> (7 - bit_index)) & 1)
			self.bit += 1
		return value


def lzw_decode(data: bytes, parms: Optional[Dict[str, Any]] = None) -> bytes:
	"""Decode PDF LZW (MSB-first, 9..12 bits, clear=256, EOD=257)."""
	early_change = int((parms or {}).get("EarlyChange", 1) or 0)
	if early_change not in (0, 1):
		raise ValueError("LZW EarlyChange must be 0 or 1")
	reader = _MsbBitReader(data)
	out = bytearray()

	def fresh_table() -> Dict[int, bytes]:
		return {value: bytes([value]) for value in range(256)}

	table = fresh_table()
	code_width = 9
	next_code = 258
	previous: Optional[bytes] = None
	while True:
		code = reader.read(code_width)
		if code is None:
			break
		if code == 256:
			table = fresh_table()
			code_width = 9
			next_code = 258
			previous = None
			continue
		if code == 257:
			break
		if code in table:
			entry = table[code]
		elif code == next_code and previous:
			entry = previous + previous[:1]
		else:
			raise ValueError("invalid LZW code %d" % code)
		if len(out) + len(entry) > limits.MAX_DECODED_STREAM:
			raise ValueError("LZW output exceeds limit")
		out.extend(entry)
		if previous is not None and next_code < 4096:
			table[next_code] = previous + entry[:1]
			next_code += 1
			if (
				code_width < 12
				and next_code == (1 << code_width) - early_change
			):
				code_width += 1
		previous = entry
	return bytes(out)


def apply_predictor(data: bytes, parms: Optional[Dict[str, Any]]) -> bytes:
	if not parms:
		return data
	predictor = int(parms.get("Predictor", 1) or 1)
	if predictor <= 1:
		return data
	columns = max(1, int(parms.get("Columns", 1) or 1))
	colors = max(1, int(parms.get("Colors", 1) or 1))
	bpc = max(1, int(parms.get("BitsPerComponent", 8) or 8))
	row_bytes = max(1, (columns * colors * bpc + 7) // 8)
	bytes_per_pixel = max(1, (colors * bpc + 7) // 8)
	if row_bytes > limits.MAX_DECODED_STREAM:
		raise ValueError("predictor row exceeds decoded-stream limit")

	if predictor == 2:
		if bpc != 8:
			raise ValueError("TIFF predictor currently requires 8-bit components")
		if len(data) % row_bytes:
			raise ValueError("truncated TIFF predictor row")
		out = bytearray()
		for offset in range(0, len(data), row_bytes):
			row = bytearray(data[offset : offset + row_bytes])
			for index in range(bytes_per_pixel, len(row)):
				row[index] = (row[index] + row[index - bytes_per_pixel]) & 0xFF
			out.extend(row)
		return bytes(out)

	if 10 <= predictor <= 15:
		out = bytearray()
		previous = bytearray(row_bytes)
		offset = 0
		while offset < len(data):
			filter_type = data[offset]
			if filter_type not in (0, 1, 2, 3, 4):
				raise ValueError("unknown PNG predictor filter %d" % filter_type)
			offset += 1
			if offset + row_bytes > len(data):
				raise ValueError("truncated PNG predictor row")
			row = bytearray(data[offset : offset + row_bytes])
			offset += row_bytes
			for index in range(len(row)):
				left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
				up = previous[index] if index < len(previous) else 0
				up_left = (
					previous[index - bytes_per_pixel]
					if index >= bytes_per_pixel and index - bytes_per_pixel < len(previous)
					else 0
				)
				if filter_type == 1:
					row[index] = (row[index] + left) & 0xFF
				elif filter_type == 2:
					row[index] = (row[index] + up) & 0xFF
				elif filter_type == 3:
					row[index] = (row[index] + ((left + up) // 2)) & 0xFF
				elif filter_type == 4:
					row[index] = (row[index] + paeth(left, up, up_left)) & 0xFF
			out.extend(row)
			previous = row
		return bytes(out)
	return data


def paeth(left: int, up: int, up_left: int) -> int:
	estimate = left + up - up_left
	distance_left = abs(estimate - left)
	distance_up = abs(estimate - up)
	distance_up_left = abs(estimate - up_left)
	if distance_left <= distance_up and distance_left <= distance_up_left:
		return left
	if distance_up <= distance_up_left:
		return up
	return up_left
