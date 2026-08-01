#!/usr/bin/env python3
"""Deterministically update raster-preservation wording in the strategic fixture.

The fixture is a locked Typora-produced PDF whose producer dialect is itself
valuable regression evidence.  Re-exporting it with a different application
would replace that evidence.  This tool therefore keeps the original PDF as an
immutable prefix and appends a standards-compliant incremental update for only:

* the one raster-image XObject;
* the two page content streams containing the fixture copy; and
* the corresponding outline title.

The source Markdown's embedded PNG is rebuilt from the same pixels.  Strict
legacy hashes and exact text-operator matches make the operation fail closed.
Running the tool repeatedly produces identical bytes.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import re
import struct
import sys
import zlib
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
	sys.path.insert(0, str(SOURCE_ROOT))

from cocoapdf._textio import write_utf8_lf  # noqa: E402
from cocoapdf.core import Converter, PdfDocument, Stream  # noqa: E402


PDF_PATH = ROOT / "tests" / "strategic_corner_cases_v1_4.pdf"
SOURCE_PATH = ROOT / "tests" / "strategic_corner_cases_v1_4.md"
LEGACY_OUTPUT_PATH = ROOT / "tests" / "strategic_corner_cases_v1_4_temp.md"

LEGACY_PDF_SIZE = 340_350
LEGACY_PDF_SHA256 = "f8e34e48c551d98591ab41af8c54251373067d2301beb93e9aa106aab2914256"
LEGACY_IMAGE_RGB_SHA256 = "d20d953708dc57fb4f4415eb190c0418739d853f8a2a708520a962bdb9456882"
LEGACY_STARTXREF = 339_338
PDF_SIZE = 311

HEADING = "14. Raster Image Preservation Fixture"
PARAGRAPH_LINES = (
	"SENTINEL-RASTER-001: The following image contains pixel-only text and must remain an extracted raster asset.",
	"CocoaPDF preserves the image without parsing or inventing body text from its pixels.",
	"The semantic image node keeps page, PDF-object, dimensions, asset hash, and confidence provenance.",
)
CAPTION_PREFIX = "Pixel-only phrase inside the extracted image: "
RASTER_SENTINEL = "Raster SENTINEL: Raster text = 12345"
APPENDIX_LINE = "Raster-only content must remain an extracted image with page, object, and glyph-free provenance."

# Exact legacy glyph codes are retained instead of obsolete user-facing copy.
# They are scoped to the immutable, hash-verified producer PDF above.
LEGACY_HEADING_HEX = b"00140017001100030031002600340003001200030034004300550056004700540003002B005B00440054004B0046000300290057005600570054004700030029004B005A0056005700540047"
LEGACY_PARAGRAPH_HEX = (
	b"0035002800300036018D00300028002E00100031002600340010001300130014001D00030036004A0047000300480051004E004E00510059004B005000490003004B004F0043004900470003004B00550003004B00500056004700500056004B005100500043004E004E005B0003005400430055005600470054000300560047005A005600110003003800140012003800150012003800160003004500510054004700030059004B0056004A005100570056000300310026003400030055004A00510057004E00460003005200540047005500470054005800470003004B00560003004300550003004300500003004B004F00430049004700030051005400030043004E0056",
	b"00560047005A0056000F00030050005100560003004A0043004E004E00570045004B00500043005600470003004400510046005B000300560047005A005600110003003800170003005100520056004B005100500043004E00030031002600340003004F0043005B0003005400470045005100580047005400030056004A00470003005400430055005600470054000300560047005A0056000300510050004E005B00030059004A0047005000030047005A0052004E004B0045004B0056004E005B00030047005000430044004E0047004600030043005000460003004F00570055005600030054004700520051005400560003003100260034",
	b"00520054005100580047005000430050004500470011",
)
LEGACY_CAPTION_HEX = b"0028005A005200470045005600470046000300310026003400100058004B0055004B0044004E004700030052004A00540043005500470003004B004800030031002600340003004B005500030047005000430044004E00470046001D0003"
LEGACY_MONOSPACE_HEX = b"003200260035001000320031002F003C00030036002800310037002C00310028002F001D0003003500440056005700480055000300570048005B0057000300140015001600170018"
LEGACY_APPENDIX_HEX = b"0038001700030031002600340003004B00550003005100520056004B005100500043004E00030043005000460003004F00570055005600030044004700030046004B005500430044004E0047004600030044005B000300460047004800430057004E0056001E00030059004A0047005000030047005000430044004E00470046000F0003003100260034000300560047005A00560003004F00570055005600030045004300540054005B00030052005400510058004700500043005000450047000300430050004600030045005100500048004B004600470050004500470011"


def _sha256(data: bytes) -> str:
	return hashlib.sha256(data).hexdigest()


def _legacy_pdf(data: bytes) -> bytes:
	if len(data) < LEGACY_PDF_SIZE:
		raise RuntimeError("strategic PDF is shorter than its immutable legacy prefix")
	legacy = data[:LEGACY_PDF_SIZE]
	if _sha256(legacy) != LEGACY_PDF_SHA256:
		raise RuntimeError("strategic PDF legacy prefix hash mismatch")
	return legacy


def _font_inverse(converter: Converter, page_index: int, name: str) -> Dict[str, bytes]:
	page = converter.doc.pages()[page_index]
	resources = converter.doc.resolve(page.get("Resources"))
	fonts = converter._load_fonts(resources)  # Fixture tool: reuse the production CMap parser.
	font = fonts[name]
	inverse: Dict[str, bytes] = {}
	for code, text in font.to_unicode.items():
		if len(text) == 1:
			inverse.setdefault(text, code)
	return inverse


def _encoded_hex(text: str, inverse: Mapping[str, bytes]) -> bytes:
	missing = sorted({character for character in text if character not in inverse})
	if missing:
		raise RuntimeError("embedded font cannot encode: %r" % "".join(missing))
	return b"".join(inverse[character] for character in text).hex().upper().encode("ascii")


def _replace_once(data: bytes, old: bytes, new: bytes, label: str) -> bytes:
	count = data.count(old)
	if count != 1:
		raise RuntimeError("expected one %s operator, found %d" % (label, count))
	return data.replace(old, new, 1)


def _replace_encoded_text_operator(
	data: bytes,
	legacy_hex: bytes,
	new_text: str,
	inverse: Mapping[str, bytes],
	label: str,
) -> bytes:
	old = b"<" + legacy_hex + b"> Tj"
	new = b"<" + _encoded_hex(new_text, inverse) + b"> Tj"
	return _replace_once(data, old, new, label)


def _monospace_run(text: str, inverse: Mapping[str, bytes]) -> bytes:
	encoded = [_encoded_hex(character, inverse) for character in text]
	parts = [b"<" + encoded[0] + b"> Tj\n"]
	parts.extend(b"7.0496979 0 Td <" + code + b"> Tj\n" for code in encoded[1:])
	return b"".join(parts)


def _legacy_monospace_run(encoded_hex: bytes) -> bytes:
	if len(encoded_hex) % 4:
		raise RuntimeError("legacy monospace glyph sequence has an invalid width")
	encoded = [encoded_hex[index : index + 4] for index in range(0, len(encoded_hex), 4)]
	parts = [b"<" + encoded[0] + b"> Tj\n"]
	parts.extend(b"7.0496979 0 Td <" + code + b"> Tj\n" for code in encoded[1:])
	return b"".join(parts)


def _updated_content_streams(converter: Converter) -> Tuple[bytes, bytes]:
	regular = _font_inverse(converter, 5, "F5")
	bold = _font_inverse(converter, 5, "F4")
	mono = _font_inverse(converter, 5, "F7")

	page_six = converter.doc.decoded_stream(converter.doc.objects[(102, 0)])
	page_six = _replace_encoded_text_operator(page_six, LEGACY_HEADING_HEX, HEADING, bold, "raster heading")
	for index, (legacy_hex, new) in enumerate(zip(LEGACY_PARAGRAPH_HEX, PARAGRAPH_LINES), start=1):
		page_six = _replace_encoded_text_operator(
			page_six,
			legacy_hex,
			new,
			regular,
			"raster paragraph line %d" % index,
		)
	page_six = _replace_encoded_text_operator(
		page_six,
		LEGACY_CAPTION_HEX,
		CAPTION_PREFIX,
		regular,
		"raster caption",
	)
	page_six = _replace_once(
		page_six,
		_legacy_monospace_run(LEGACY_MONOSPACE_HEX),
		_monospace_run(RASTER_SENTINEL, mono),
		"raster inline-code sentinel",
	)

	page_eight = converter.doc.decoded_stream(converter.doc.objects[(118, 0)])
	page_eight_regular = _font_inverse(converter, 7, "F5")
	page_eight = _replace_encoded_text_operator(
		page_eight,
		LEGACY_APPENDIX_HEX,
		APPENDIX_LINE,
		page_eight_regular,
		"raster appendix line",
	)
	return page_six, page_eight


def _copy_rgb_rect(
	source: bytes,
	destination: bytearray,
	*,
	width: int,
	source_box: Tuple[int, int, int, int],
	destination_xy: Tuple[int, int],
) -> None:
	x0, y0, x1, y1 = source_box
	destination_x, destination_y = destination_xy
	for row in range(y1 - y0):
		source_start = ((y0 + row) * width + x0) * 3
		source_end = ((y0 + row) * width + x1) * 3
		destination_start = ((destination_y + row) * width + destination_x) * 3
		destination[destination_start : destination_start + (source_end - source_start)] = source[
			source_start:source_end
		]


def _updated_image_rgb(converter: Converter) -> bytes:
	image = converter.doc.objects[(85, 0)]
	if not isinstance(image, Stream):
		raise RuntimeError("strategic raster XObject is not a stream")
	rgb = zlib.decompress(image.raw)
	if len(rgb) != 900 * 220 * 3 or _sha256(rgb) != LEGACY_IMAGE_RGB_SHA256:
		raise RuntimeError("strategic raster pixels do not match the immutable source")

	updated = bytearray(rgb)
	# Clear only the original first line, then compose the replacement from the
	# producer's own antialiased glyph pixels.  The remaining two lines are
	# byte-for-byte unchanged.
	for y in range(24, 70):
		start = (y * 900 + 20) * 3
		end = (y * 900 + 880) * 3
		updated[start:end] = b"\xff" * (end - start)

	x = 29
	first_line_y = 29
	segments = (
		((403, 29, 509, 66), 15),  # Raster
		((214, 29, 385, 66), 15),  # SENTINEL:
		((403, 29, 509, 66), 15),  # Raster
		((520, 29, 587, 66), 12),  # text
		((680, 85, 697, 122), 12),  # = (copied from line two)
		((601, 29, 703, 66), 0),  # 12345
	)
	for box, spacing in segments:
		_copy_rgb_rect(rgb, updated, width=900, source_box=box, destination_xy=(x, first_line_y))
		x += box[2] - box[0] + spacing
	return bytes(updated)


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
	return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)


def _png_from_rgb(rgb: bytes, width: int = 900, height: int = 220) -> bytes:
	if len(rgb) != width * height * 3:
		raise RuntimeError("unexpected RGB image size")
	rows = b"".join(b"\x00" + rgb[y * width * 3 : (y + 1) * width * 3] for y in range(height))
	return b"".join(
		(
			b"\x89PNG\r\n\x1a\n",
			_png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)),
			_png_chunk(b"IDAT", zlib.compress(rows, 9)),
			_png_chunk(b"IEND", b""),
		)
	)


def _stream_object(number: int, dictionary: bytes, decoded: bytes) -> bytes:
	compressed = zlib.compress(decoded, 9)
	return b"".join(
		(
			("%d 0 obj\n" % number).encode("ascii"),
			dictionary.replace(b"{length}", str(len(compressed)).encode("ascii")),
			b"\nstream\n",
			compressed,
			b"\nendstream\nendobj\n",
		)
	)


def _image_object(rgb: bytes) -> bytes:
	dictionary = (
		b"<< /Type /XObject /Subtype /Image /Width 900 /Height 220 "
		b"/ColorSpace [/ICCBased 86 0 R] /BitsPerComponent 8 "
		b"/Filter /FlateDecode /Length {length} >>"
	)
	return _stream_object(85, dictionary, rgb)


def _outline_object() -> bytes:
	title = b"\xfe\xff" + HEADING.encode("utf-16-be")
	return b"".join(
		(
			b"291 0 obj\n<< /Parent 273 0 R /Dest [84 0 R /XYZ null 1196 null] ",
			b"/Title <" + title.hex().upper().encode("ascii") + b"> ",
			b"/Next 292 0 R /Prev 290 0 R /Count 0 >>\nendobj\n",
		)
	)


def build_pdf(legacy: bytes) -> Tuple[bytes, bytes]:
	converter = Converter(legacy)
	page_six, page_eight = _updated_content_streams(converter)
	rgb = _updated_image_rgb(converter)
	objects = {
		85: _image_object(rgb),
		102: _stream_object(102, b"<< /Filter /FlateDecode /Length {length} >>", page_six),
		118: _stream_object(118, b"<< /Filter /FlateDecode /Length {length} >>", page_eight),
		291: _outline_object(),
	}

	output = bytearray(legacy)
	if not output.endswith(b"\n"):
		output.extend(b"\n")
	output.extend(b"\n")
	offsets: Dict[int, int] = {}
	for number in sorted(objects):
		offsets[number] = len(output)
		output.extend(objects[number])
	xref_offset = len(output)
	output.extend(b"xref\n")
	for number in sorted(offsets):
		output.extend(("%d 1\n%010d 00000 n \n" % (number, offsets[number])).encode("ascii"))
	output.extend(
		(
			"trailer\n<< /Size %d /Root 266 0 R /Info 305 0 R /Prev %d >>\n"
			"startxref\n%d\n%%%%EOF\n" % (PDF_SIZE, LEGACY_STARTXREF, xref_offset)
		).encode("ascii")
	)
	return bytes(output), _png_from_rgb(rgb)


def _replace_source_text(text: str, png: bytes) -> str:
	new_paragraph = " ".join(PARAGRAPH_LINES)
	for required in (
		HEADING,
		new_paragraph,
		CAPTION_PREFIX + "`" + RASTER_SENTINEL + "`.",
		"- " + APPENDIX_LINE,
	):
		if required not in text:
			raise RuntimeError("source fixture is missing current raster text: %s" % required[:80])

	image_markup = "![Raster image preservation sentinel RASTER-001](data:image/png;base64,%s)" % base64.b64encode(png).decode("ascii")
	pattern = re.compile(
		r"!\[Raster image preservation sentinel RASTER-001\]"
		r"\(data:image/png;base64,[A-Za-z0-9+/=]+\)"
	)
	text, count = pattern.subn(image_markup, text, count=1)
	if count != 1:
		raise RuntimeError("source fixture is missing its embedded raster image")
	return text


def _replace_legacy_output_text(text: str, asset_name: str) -> str:
	new_paragraph = " ".join(PARAGRAPH_LINES)
	for required in (
		HEADING,
		new_paragraph,
		CAPTION_PREFIX + "`" + RASTER_SENTINEL + "`.",
		"- " + APPENDIX_LINE,
	):
		if required not in text:
			raise RuntimeError("legacy output fixture is missing current raster text: %s" % required[:80])
	text = re.sub(r"assets/img-[0-9a-f]{16}\.png", "assets/" + asset_name, text, count=1)
	return text


def _asset_name(pdf: bytes) -> str:
	result = Converter(pdf).convert()
	names = sorted(name for name in result.assets if name.startswith("img-") and name.endswith(".png"))
	if len(names) != 1:
		raise RuntimeError("updated fixture did not produce exactly one raster asset")
	return names[0]


def update(*, check: bool) -> bool:
	legacy = _legacy_pdf(PDF_PATH.read_bytes())
	pdf, png = build_pdf(legacy)
	source = _replace_source_text(SOURCE_PATH.read_text(encoding="utf-8"), png)
	asset_name = _asset_name(pdf)
	legacy_output = _replace_legacy_output_text(
		LEGACY_OUTPUT_PATH.read_text(encoding="utf-8"),
		asset_name,
	)

	changed = (
		PDF_PATH.read_bytes() != pdf
		or SOURCE_PATH.read_text(encoding="utf-8") != source
		or LEGACY_OUTPUT_PATH.read_text(encoding="utf-8") != legacy_output
	)
	if check:
		if changed:
			print("strategic raster fixture is stale", file=sys.stderr)
			return False
		return True

	PDF_PATH.write_bytes(pdf)
	write_utf8_lf(SOURCE_PATH, source)
	write_utf8_lf(LEGACY_OUTPUT_PATH, legacy_output)
	print("updated %s (%s)" % (PDF_PATH.relative_to(ROOT), _sha256(pdf)))
	print("embedded raster PNG: %s" % _sha256(png))
	print("extracted asset: %s" % asset_name)
	return True


def main(argv: Optional[Iterable[str]] = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--check", action="store_true", help="verify that committed fixture bytes are current")
	args = parser.parse_args(list(argv) if argv is not None else None)
	return 0 if update(check=args.check) else 1


if __name__ == "__main__":
	raise SystemExit(main())
