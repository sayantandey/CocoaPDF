from __future__ import annotations

from typing import Any, List, Optional

from .. import limits


PDF_WHITESPACE = b"\x00\t\n\f\r "
PDF_DELIMITERS = b"()<>[]{}/%"


def read_stream_raw(
	document: Any,
	position: int,
	length: Optional[int],
	object_number: int,
	data: bytes,
) -> bytes:
	if length is not None and length >= 0 and position + length <= len(data):
		declared_end = position + length
		check = declared_end
		if data[check : check + 2] == b"\r\n":
			check += 2
		elif data[check : check + 1] in (b"\r", b"\n"):
			check += 1
		if data.startswith(b"endstream", check):
			return data[position:declared_end]
		document.warn(
			"WARN_BAD_LENGTH",
			"object %d: declared slice is not followed by endstream" % object_number,
		)

	scan_end = min(len(data), position + limits.MAX_ENDSTREAM_SCAN)
	fallback: List[int] = []
	structural: Optional[int] = None
	search = position
	while search < scan_end:
		found = data.find(b"endstream", search, scan_end)
		if found < 0:
			break
		before_ok = found == position or data[found - 1] in b"\r\n"
		after = found + len(b"endstream")
		after_ok = after >= len(data) or data[after] in PDF_WHITESPACE + PDF_DELIMITERS
		if before_ok and after_ok:
			fallback.append(found)
			probe = after
			while probe < len(data) and data[probe] in PDF_WHITESPACE:
				probe += 1
			if data.startswith(b"endobj", probe):
				structural = found
				break
		search = found + len(b"endstream")
	endstream = structural if structural is not None else (fallback[0] if fallback else -1)
	if endstream < 0:
		document.warn("MISSING_ENDSTREAM", "object %d" % object_number)
		return data[position:]
	end = endstream
	if data[end - 2 : end] == b"\r\n":
		end -= 2
	elif data[end - 1 : end] in (b"\r", b"\n"):
		end -= 1
	document.warn("WARN_BAD_LENGTH", "object %d" % object_number)
	return data[position:end]
