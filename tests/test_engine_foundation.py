from __future__ import annotations

import inspect
from pathlib import Path
import tempfile
import unittest
import zlib
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from typing import Iterable

import cocoapdf
from cocoapdf import ConvertOptions, convert
from cocoapdf.core import (
	Char,
	Converter,
	Fill,
	Font,
	Line,
	MarkdownRenderer,
	PdfDocument,
	Ref,
	Segment,
	Stream,
	XrefEntry,
	apply_mat,
	ascii_hex_decode,
	cleanup_inline,
	decode_pdf_text,
	escape_block_start,
	escape_inline,
	find_interval,
	joins_without_word_space,
	line_text_tokens,
	merge_gfm_table_blocks,
	page_normalization_transform,
	parse_tounicode,
	parse_w2_array,
	plain_text,
	repair_bidi_tokens,
	render_code_span,
	render_inline,
	neutral_style,
	winansi_char,
)
from cocoapdf.cos.filters import apply_predictor, lzw_decode
from cocoapdf.cos.xref import walk_xrefs
from cocoapdf.fonts.decoding import decode_font, glyph_name_to_unicode
from cocoapdf.html.render import render_html
from cocoapdf.synthetic import hex_text_op, image_xobject_rgb, line_op, link_annot, make_pdf, rect_fill_op, text_op
from cocoapdf.tools import bench_v1, character_accuracy, region_overlay_svg, trace_pdf


class FoundationDiagnosticTests(unittest.TestCase):
	def test_inline_cleanup_preserves_deliberately_spaced_punctuation_sequence(self):
		self.assertEqual(cleanup_inline("normal word , next"), "normal word, next")
		self.assertEqual(cleanup_inline("characters # + - . ! |."), "characters # + - . ! |.")
		style = neutral_style()
		explicit = [
			{"text": "word", "style": style, "link": None},
			{"text": " ", "style": style, "link": None, "synthetic_space": False},
			{"text": ".", "style": style, "link": None},
		]
		synthetic = [dict(token) for token in explicit]
		synthetic[1]["synthetic_space"] = True
		self.assertEqual(render_inline(explicit, ConvertOptions()), "word .")
		self.assertEqual(render_inline(synthetic, ConvertOptions()), "word.")

	def test_isolated_directional_control_does_not_reorder_ltr_brackets(self):
		style = neutral_style()
		tokens = [{"text": "right-to-left mark [\u200f].", "style": style, "link": None}]
		self.assertEqual(repair_bidi_tokens(tokens), tokens)

	def test_zero_width_control_uses_local_source_order_inside_brackets(self):
		font = Font(name="F1", base_font="Helvetica")
		line = Line(
			[
				Char("[", 0, 0, 4, 10, 10, font, 1, 1),
				Char("\u2060", 10, 0, 10, 10, 10, font, 1, 2),
				Char("]", 5, 0, 9, 10, 10, font, 1, 3),
			],
			1,
			1,
		)
		self.assertEqual(plain_text(line_text_tokens(line)), "[\u2060]")

	def test_rtl_repair_preserves_decimal_and_embedded_ltr_units(self):
		style = (False, False, False, False, False, False, False, False)

		def repair(text):
			return "".join(
				token["text"]
				for token in repair_bidi_tokens([{"text": text, "style": style, "link": None}])
			)

		self.assertEqual(repair("طبارلاو 123.45 مقرلا"), "الرقم 123.45 والرابط")
		self.assertEqual(
			repair(".ABC-42 ההזמהו 678.90 רפסמה"),
			"המספר 678.90 והמזהה ABC-42.",
		)
		self.assertEqual(repair("Mixed Account 42-باسح owes"), "Mixed Account حساب-42 owes")

		hebrew = "\u05e2\u05d1\u05e8\u05d9\u05ea \u05e9\u05dc\u05d5\u05dd \u05e2\u05d5\u05dc\u05dd"
		arabic = "\u0627\u0644\u0639\u0631\u0628\u064a\u0629 \u0645\u0631\u062d\u0628\u0627 \u0628\u0627\u0644\u0639\u0627\u0644\u0645"
		prefix = "SENTINEL-LONG-LTR-PREFIX Prefix: "
		suffix = ". Mixed LTR suffix ABC XYZ"
		visual = prefix + arabic[::-1] + " ." + hebrew[::-1] + suffix
		self.assertEqual(repair(visual), prefix + hebrew + ". " + arabic + suffix)

	def test_rtl_repair_keeps_multicodepoint_glyph_cluster_atomic(self):
		font = Font(name="F1", base_font="Arabic")
		chars = [
			Char(text, index * 6.0, 0, index * 6.0 + 5.0, 10, 10, font, 1, index)
			for index, text in enumerate(["ه", "ا", "ج", "ت", "لا"])
		]
		self.assertEqual(plain_text(line_text_tokens(Line(chars, 1, 1))), "لاتجاه")

	def test_rtl_source_order_list_marker_stays_at_logical_prefix(self):
		font = Font(name="F1", base_font="Arabic")
		logical = "مرحبا بالعالم"
		visual = logical[::-1]
		chars = [Char("●", 120.0, 0, 125.0, 10, 10, font, 1, 1)]
		chars.extend(
			Char(
				text,
				index * 6.0,
				0,
				index * 6.0 + 5.0,
				10,
				10,
				font,
				1,
				index + 2,
			)
			for index, text in enumerate(visual)
		)
		tokens = line_text_tokens(Line(chars, 1, 1))
		self.assertEqual(plain_text(tokens), "● مرحبا بالعالم")
		self.assertEqual(tokens[0]["glyph_ids"], (1,))

	def test_rtl_list_source_path_keeps_combining_clusters_atomic(self):
		font = Font(name="F1", base_font="Arabic")
		logical_clusters = ["مَ", "رْ", "حَ", "بً", "ا"]
		visual_clusters = list(reversed(logical_clusters))
		chars = [Char("•", 80.0, 0, 85.0, 10, 10, font, 1, 1)]
		chars.extend(
			Char(
				text,
				index * 8.0,
				0,
				index * 8.0 + 7.0,
				10,
				10,
				font,
				1,
				index + 2,
			)
			for index, text in enumerate(visual_clusters)
		)
		self.assertEqual(
			plain_text(line_text_tokens(Line(chars, 1, 1))),
			"• مَرْحَبًا",
		)

	def test_no_space_scripts_join_without_affecting_korean_word_spaces(self):
		self.assertTrue(joins_without_word_space("标", "点"))
		self.assertTrue(joins_without_word_space("และ", "ลำดับ"))
		self.assertFalse(joins_without_word_space("순서를", "보존해야"))

	def test_thai_cluster_geometry_does_not_invent_a_space(self):
		font = Font(name="F1", base_font="Thai")
		line = Line(
			[
				Char("ลำ", 0, 0, 10, 10, 10, font, 1, 1),
				Char("ดั", 22, 0, 32, 10, 10, font, 1, 2),
				Char("บ", 32, 0, 38, 10, 10, font, 1, 3),
			],
			1,
			1,
		)
		self.assertEqual(plain_text(line_text_tokens(line)), "ลำดับ")

	def test_word_gap_tolerance_is_bounded_to_pdf_metric_rounding(self):
		font = Font(name="F1", base_font="NimbusRomNo9L-Regu")
		near_threshold = Line(
			[
				Char("A", 0.0, 0.0, 5.0, 10.0, 10.0, font, 1, 1),
				Char("B", 7.21, 0.0, 12.21, 10.0, 10.0, font, 1, 2),
			],
			1,
			1,
		)
		below_tolerance = Line(
			[
				Char("A", 0.0, 0.0, 5.0, 10.0, 10.0, font, 1, 3),
				Char("B", 7.19, 0.0, 12.19, 10.0, 10.0, font, 1, 4),
			],
			1,
			2,
		)
		self.assertEqual(plain_text(line_text_tokens(near_threshold)), "A B")
		self.assertEqual(plain_text(line_text_tokens(below_tolerance)), "AB")

	def test_printed_form_boxes_need_one_or_two_stable_columns(self):
		aligned = [
			((100.0, float(index * 30), 240.0, float(index * 30 + 20)), [])
			for index in range(5)
		]
		two_column = [
			(
				(100.0 if index % 2 == 0 else 360.0, float(index * 30), 240.0 if index % 2 == 0 else 500.0, float(index * 30 + 20)),
				[],
			)
			for index in range(6)
		]
		diagram = [
			(
				(float(40 + index * 110), 100.0, float(120 + index * 110), 122.0),
				[],
			)
			for index in range(5)
		]
		self.assertTrue(MarkdownRenderer._has_form_column_pattern(aligned))
		self.assertTrue(MarkdownRenderer._has_form_column_pattern(two_column))
		self.assertFalse(MarkdownRenderer._has_form_column_pattern(diagram))

	def test_generated_rtl_paragraph_passes_through_html_safely(self):
		html = render_html('<p dir="rtl"><strong>العربية</strong> 123.45</p>', {})
		self.assertIn('<p dir="rtl"><strong>العربية</strong> 123.45</p>', html)
		self.assertNotIn("&lt;p dir=", html)

	def test_html_renderer_preserves_list_continuations_and_hard_breaks(self):
		html = render_html(
			"1. Endnote one.\n"
			"2. Endnote two continues across\n"
			"   a visual line.\n\n"
			"First authored line.\nSecond authored line.",
			{},
		)
		self.assertIn("<ol>", html)
		self.assertIn("<li>Endnote two continues across a visual line.</li>", html)
		self.assertIn("First authored line.<br />\nSecond authored line.", html)
		self.assertNotIn("&lt;br /&gt;", html)

	def test_display_math_uses_script_fraction_and_radical_geometry(self):
		font = Font(name="F1", base_font="Helvetica")

		def glyph(text, x0, y0, y1, size, seq):
			return Char(text, x0, y0, x0 + 5.0, y1, size, font, 1, seq)

		chars = [
			glyph("∫", 80, 100, 113, 13, 1),
			glyph("0", 83, 115, 123, 8, 2),
			glyph("∞", 84, 91, 99, 8, 3),
			glyph("e", 95, 100, 113, 13, 4),
			glyph("−", 102, 91, 100, 9, 5),
			glyph("x", 108, 91, 100, 9, 6),
			glyph("2", 113, 83, 90, 6, 7),
			glyph("d", 122, 100, 113, 13, 8),
			glyph("x", 129, 100, 113, 13, 9),
			glyph("=", 136, 100, 113, 13, 10),
			glyph("√", 146, 94, 105, 11, 11),
			glyph("π", 153, 94, 105, 11, 12),
			glyph("2", 153, 112, 122, 10, 13),
		]
		converter = Converter(make_pdf([b""]))
		converter.page_sizes = {1: (612.0, 792.0)}
		converter.segments = [Segment(143, 108, 165, 108, 1, 1, 20)]
		renderer = MarkdownRenderer(converter)
		math = renderer._display_math_html(
			Fill(70, 75, 180, 130, (0.98, 0.98, 0.99), 1, 1),
			[Line(chars, 1, 1)],
		)
		self.assertIsNotNone(math)
		self.assertIn("<msubsup>", math)
		self.assertIn("<msup>", math)
		self.assertIn("<mfrac>", math)
		self.assertIn("<msqrt>", math)
		self.assertIn(math, render_html(math or "", {}))

	def test_repeated_gfm_header_at_page_boundary_merges_continuation(self):
		first = "\n".join(
			[
				"| ID | Description | Score |",
				"| --- | --- | ---: |",
				"| R01 | First row | 0.99 |",
			]
		)
		second = "\n".join(
			[
				"| ID | Description | Score |",
				"| --- | --- | ---: |",
				"| R02 | Second row | 0.98 |",
			]
		)
		merged = merge_gfm_table_blocks(first, second)
		self.assertIsNotNone(merged)
		self.assertEqual(merged.count("| ID | Description | Score |"), 1)
		self.assertIn("| R01 | First row | 0.99 |", merged)
		self.assertIn("| R02 | Second row | 0.98 |", merged)

	def test_different_gfm_headers_do_not_merge_at_page_boundary(self):
		first = "| ID | Score |\n| --- | ---: |\n| R01 | 0.99 |"
		second = "| Name | Score |\n| --- | ---: |\n| Alpha | 0.98 |"
		self.assertIsNone(merge_gfm_table_blocks(first, second))

	def test_captioned_gfm_table_merges_repeated_header_at_page_boundary(self):
		first = "**Table 4: Continuation**\n\n| ID | Score |\n| --- | ---: |\n| R01 | 0.99 |"
		second = "| ID | Score |\n| --- | ---: |\n| R02 | 0.98 |"
		merged = merge_gfm_table_blocks(first, second)
		self.assertIsNotNone(merged)
		self.assertEqual((merged or "").count("| ID | Score |"), 1)
		self.assertIn("**Table 4: Continuation**", merged or "")

	def test_clipped_rotated_fragments_stitch_across_adjacent_pages(self):
		font = Font(name="F1", base_font="Helvetica")

		def glyph(char, index, page, baseline, seq):
			x0 = 100.0 + index * 6.0
			cy = baseline - x0 * 0.20
			return Char(char, x0, cy - 5.0, x0 + 5.0, cy + 5.0, 10.0, font, page, seq)

		upper_chars = [glyph(char, index, 2, 60.0, 200 + index) for index, char in enumerate("Rotated label ")]
		upper_chars.append(glyph("1", 15, 2, 60.0, 215))
		lower_chars = [glyph(char, 8 + offset, 1, 805.0, 100 + offset) for offset, char in enumerate("label −12°")]
		lower = Line(lower_chars, 1, 100, source_order=True, writing_mode="rotated")
		upper = Line(upper_chars, 2, 200, source_order=True, writing_mode="rotated")
		converter = Converter(make_pdf([b"", b""]))
		converter.page_sizes = {1: (612.0, 792.0), 2: (612.0, 792.0)}
		renderer = MarkdownRenderer(converter)
		renderer.lines_by_page = {1: [lower], 2: [upper]}
		renderer._stitch_page_boundary_rotated_text()
		self.assertEqual(renderer.lines_by_page[1], [])
		self.assertEqual(len(renderer.lines_by_page[2]), 1)
		self.assertEqual(plain_text(line_text_tokens(renderer.lines_by_page[2][0])), "Rotated label −12°")

	def test_cocoapdf_public_api(self):
		self.assertEqual(cocoapdf.__project__, "CocoaPDF")
		self.assertIs(cocoapdf.convert, convert)

	def test_convert_result_contains_html_and_region_report(self):
		stream = b"\n".join(
			[
				text_op(72, 720, "Left column line one", "F1", 10),
				text_op(72, 704, "Left column line two", "F1", 10),
				text_op(306, 720, "Right column line one", "F1", 10),
				text_op(306, 704, "Right column line two", "F1", 10),
				line_op(288, 640, 288, 750, 1),
			]
		)
		result = convert(make_pdf([stream]), ConvertOptions())
		self.assertIn("<!doctype html>", result.html)
		kinds = {region["kind"] for region in result.report["regions"]}
		self.assertIn("column", kinds)
		self.assertEqual(result.report["tool"], "CocoaPDF")
		self.assertTrue(result.report["nodes"])
		self.assertTrue(
			any(node.get("type") == "region" and node.get("kind") == "column" for node in result.report["nodes"])
		)

	def test_report_path_creates_parent_directory(self):
		stream = text_op(72, 720, "Report path fixture", "F1", 10)
		with tempfile.TemporaryDirectory() as tmp:
			report_path = Path(tmp) / "nested" / "report.json"
			result = convert(make_pdf([stream]), ConvertOptions(report_path=str(report_path)))
			self.assertEqual(result.report["tool"], "CocoaPDF")
			self.assertTrue(report_path.exists())

	def test_control_glyphs_are_dropped_not_emitted(self):
		cmap = (
			b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
			b"1 begincodespacerange\n<00> <ff>\nendcodespacerange\n"
			b"2 beginbfchar\n<01> <0000>\n<02> <0041>\nendbfchar\n"
			b"endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend"
		)
		cmap_stream = b"<< /Length %d >>\nstream\n" % len(cmap) + cmap + b"\nendstream"
		font = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding /ToUnicode TOUNICODE 0 R >>"
		result = convert(
			make_pdf([hex_text_op(72, 720, b"\x01\x02", "F5")], extra_fonts={"F5": (font, cmap_stream)}),
			ConvertOptions(),
		)
		self.assertEqual(result.markdown, "A\n")
		self.assertNotIn("\x00", result.markdown)
		self.assertTrue(any(w.code == "TEXT_CONTROL_GLYPH_DROPPED" for w in result.warnings))

	def test_actual_text_marked_content_replaces_shaped_glyph_payload(self):
		actual = "हिन्दी परीक्षण"
		actual_hex = ("FEFF" + actual.encode("utf-16-be").hex()).encode("ascii")
		stream = (
			b"/Span << /ActualText <%s> >> BDC\n"
			b"BT /F1 10 Tf 1 0 0 1 72 720 Tm (bad glyphs) Tj ET\n"
			b"EMC"
		) % actual_hex
		result = convert(make_pdf([stream]), ConvertOptions())
		self.assertEqual(result.markdown, actual + "\n")

	def test_actual_text_literal_string_uses_single_byte_pdf_text_encoding(self):
		stream = (
			b"/Span << /ActualText (fi) >> BDC\n"
			b"BT /F1 10 Tf 1 0 0 1 72 720 Tm (wrong) Tj ET\n"
			b"EMC"
		)
		result = convert(make_pdf([stream]), ConvertOptions())
		self.assertEqual(result.markdown, "fi\n")

	def test_indented_letter_spaced_monospace_label_is_not_fenced_code(self):
		stream = b"\n".join(
			[
				text_op(72, 690, "Ordinary body text", "F1", 10),
				b"BT /F4 7.5 Tf 0.375 Tc 1 0 0 1 83 720 Tm (CASE TXT-001) Tj ET",
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertIn("`CASE TXT-001`", markdown)
		self.assertNotIn("```", markdown)
		self.assertNotIn("CASE T XT", markdown)

	def test_monospace_label_is_a_separate_block_from_tightly_following_prose(self):
		stream = b"\n".join(
			[
				b"BT /F4 7.5 Tf 0.375 Tc 1 0 0 1 83 720 Tm (CASE TXT-001) Tj ET",
				text_op(83, 706, "Ordinary proportional prose follows.", "F1", 10),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertIn("`CASE TXT-001`\n\nOrdinary proportional prose", markdown)
		self.assertNotIn("```", markdown)

	def test_nearby_monospace_lines_with_different_sizes_are_not_one_code_block(self):
		stream = b"\n".join(
			[
				text_op(83, 720, "ordinary monospace prose", "F4", 10),
				text_op(83, 704, "SMALL MONOSPACE LABEL", "F4", 7),
				text_op(72, 680, "Proportional body", "F1", 10),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertNotIn("```", markdown)

	def test_wide_footer_does_not_shift_text_frame_or_invent_bullets(self):
		stream = b"\n".join(
			[
				text_op(83, 720, "First ordinary paragraph.", "F1", 10),
				text_op(83, 690, "Second ordinary paragraph.", "F1", 10),
				text_op(38, 20, "A much wider running footer across the page", "F1", 9),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertNotIn("- First ordinary", markdown)
		self.assertNotIn("- Second ordinary", markdown)

	def test_repeated_margin_text_and_rules_are_page_furniture(self):
		pages = []
		for index, suffix in enumerate(("A", "B", "C"), 1):
			pages.append(
				b"\n".join(
					[
						text_op(72, 720, "Body page %d" % index, "F1", 10),
						line_op(30, 45, 500, 45, 1),
						text_op(38, 20, "Draft footer " + suffix, "F1", 9),
					]
				)
			)
		markdown = convert(make_pdf(pages), ConvertOptions()).markdown
		self.assertNotIn("Draft footer", markdown)
		self.assertNotIn("---", markdown)
		self.assertIn("Body page 1", markdown)
		self.assertIn("Body page 3", markdown)

	def test_repeated_top_heading_geometry_is_not_treated_as_furniture(self):
		pages = [
			b"\n".join(
				[
					text_op(72, 760, "Section %s" % suffix, "F2", 22),
					text_op(72, 700, "Distinct body %s" % suffix, "F1", 10),
				]
			)
			for suffix in ("Alpha", "Beta", "Gamma")
		]
		markdown = convert(make_pdf(pages), ConvertOptions()).markdown
		self.assertIn("Section Alpha", markdown)
		self.assertIn("Section Beta", markdown)
		self.assertIn("Section Gamma", markdown)

	def test_first_line_indent_and_nearby_centered_prose_are_not_missing_bullets(self):
		stream = b"\n".join(
			[
				text_op(104, 720, "This paragraph has a first-line indent.", "F1", 10),
				text_op(83, 706, "It remains ordinary prose.", "F1", 10),
				text_op(106, 670, "Centered prose remains ordinary prose.", "F1", 10),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertNotIn("- This paragraph", markdown)
		self.assertNotIn("- Centered prose", markdown)

	def test_small_bold_label_and_sentence_are_not_headings(self):
		stream = b"\n".join(
			[
				text_op(72, 740, "SMALL CASE LABEL", "F2", 7.5),
				text_op(72, 700, "Bold body sentence is not a heading.", "F2", 10.5),
				text_op(72, 670, "Ordinary body establishes size.", "F1", 10.5),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertNotIn("# SMALL CASE LABEL", markdown)
		self.assertNotIn("# Bold body sentence", markdown)
		self.assertIn("**SMALL CASE LABEL**", markdown)
		self.assertIn("**Bold body sentence is not a heading.**", markdown)

	def test_typographic_heading_size_ladder_maps_h3_through_h6(self):
		stream = b"\n".join(
			[
				text_op(72, 740, "Third Level", "F2", 13.5),
				text_op(72, 700, "Fourth Level", "F2", 11.5),
				text_op(72, 665, "Fifth Level", "F2", 10.5),
				text_op(72, 635, "SIXTH LEVEL", "F2", 9.5),
				text_op(72, 600, "Ordinary body establishes size.", "F1", 10.5),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertIn("### Third Level", markdown)
		self.assertIn("#### Fourth Level", markdown)
		self.assertIn("##### Fifth Level", markdown)
		self.assertIn("###### SIXTH LEVEL", markdown)

	def test_thin_filled_rectangle_can_emit_horizontal_rule(self):
		stream = b"\n".join(
			[
				text_op(72, 720, "Before", "F1", 10),
				rect_fill_op(72, 680, 300, 1, 0.0),
				text_op(72, 640, "After", "F1", 10),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertIn("---", markdown)

	def test_more_than_three_real_rules_can_emit_on_one_page(self):
		stream = b"\n".join(
			[
				text_op(72, 760, "Rule fixture body text", "F1", 10),
				line_op(72, 720, 360, 720, 1),
				line_op(72, 680, 360, 680, 1),
				line_op(72, 640, 360, 640, 1),
				line_op(72, 600, 360, 600, 1),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertEqual(markdown.count("---"), 4)

	def test_footnote_separator_is_not_thematic_rule(self):
		stream = b"\n".join(
			[
				text_op(72, 720, "Body with footnote marker 1", "F1", 10),
				line_op(72, 120, 250, 120, 1),
				text_op(72, 96, "1. Footnote text", "F1", 8),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertNotIn("---", markdown)
		self.assertIn("Footnote text", markdown)

	def test_full_width_footnote_separator_is_not_thematic_rule(self):
		stream = b"\n".join(
			[
				text_op(96, 760, "End marker before footnotes.", "F1", 10),
				line_op(96, 725, 696, 725, 1),
				text_op(96, 690, "1. Structural footnote definition.", "F1", 8),
			]
		)
		markdown = convert(make_pdf([stream], page_size=(792, 1224)), ConvertOptions()).markdown
		self.assertNotIn("---", markdown)
		self.assertIn("Structural footnote definition", markdown)

	def test_visual_hyphen_wrap_distinguishes_compounds(self):
		stream = b"\n".join(
			[
				text_op(72, 720, "Soft wraps: micro-", "F1", 10),
				text_op(72, 706, "service, co-", "F1", 10),
				text_op(72, 692, "operate, re-", "F1", 10),
				text_op(72, 678, "entry. Hard compounds: non-", "F1", 10),
				text_op(72, 664, "breaking and italic-looking-", "F1", 10),
				text_op(72, 650, "words stay hyphenated.", "F1", 10),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertIn("microservice, cooperate, reentry", markdown)
		self.assertIn("non-breaking", markdown)
		self.assertIn("italic-looking-words", markdown)

	def test_uppercase_acronym_hyphen_is_preserved_across_visual_wrap(self):
		stream = b"\n".join(
			[
				text_op(72, 720, "Future OCR-", "F1", 10),
				text_op(72, 706, "derived content remains attributable.", "F1", 10),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertIn("OCR-derived", markdown)

	def test_short_wrapped_prose_is_soft_but_sentence_lines_are_hard_breaks(self):
		stream = b"\n".join(
			[
				text_op(72, 720, "This narrow paragraph wraps across", "F1", 10),
				text_op(72, 705, "several visual lines but remains one", "F1", 10),
				text_op(72, 690, "paragraph with ordinary spacing,", "F1", 10),
				text_op(72, 675, "indentation and continuous flow.", "F1", 10),
				text_op(72, 630, "Line one ends here.", "F1", 10),
				text_op(72, 615, "Line two also ends here.", "F1", 10),
				text_op(72, 600, "Line three closes.", "F1", 10),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertIn(
			"This narrow paragraph wraps across several visual lines but remains one paragraph with ordinary spacing, indentation and continuous flow.",
			markdown,
		)
		self.assertIn("Line one ends here.  \nLine two also ends here.  \nLine three closes.", markdown)

	def test_machine_identifier_is_emphasis_not_an_invented_heading(self):
		stream = b"\n".join(
			[
				text_op(72, 720, "Context paragraph above.", "F1", 10),
				text_op(72, 680, "OVERPRINT-DEDUP-55AA", "F2", 10),
				text_op(72, 650, "The identifier remains body content.", "F1", 10),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertIn("**OVERPRINT-DEDUP-55AA**", markdown)
		self.assertNotIn("# OVERPRINT-DEDUP-55AA", markdown)

	def test_inline_code_tail_rejoins_preceding_prose_line(self):
		stream = b"\n".join(
			[
				text_op(72, 720, "A second sample contains two:", "F1", 10),
				text_op(74, 705, "gamma``delta", "F4", 10),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertIn("contains two: ```gamma``delta```", markdown)
		self.assertNotIn("contains two:\n\n", markdown)

	def test_whole_sentence_monospace_prose_is_not_inline_code(self):
		stream = b"\n".join(
			[
				text_op(72, 720, "This whole sentence is monospace prose and must remain ordinary", "F4", 10),
				text_op(72, 705, "paragraph text rather than any kind of code.", "F4", 10),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertIn("This whole sentence is monospace prose", markdown)
		self.assertNotIn("`This whole sentence", markdown)

	def test_singleton_list_lookalike_keeps_its_wrapped_prose_continuation(self):
		stream = b"\n".join(
			[
				text_op(72, 720, "- A singleton marker-looking paragraph continues", "F1", 10),
				text_op(72, 705, "on its next visual line.", "F1", 10),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertIn("\\- A singleton marker-looking paragraph continues on its next visual line.", markdown)
		self.assertNotIn("continues\n\non its", markdown)

	def test_code_indentation_is_measured_from_the_whole_block(self):
		stream = b"\n".join(
			[
				rect_fill_op(68, 680, 220, 60),
				text_op(72, 720, "if value:", "F4", 10),
				text_op(96, 706, "return value", "F4", 10),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertIn("\n\treturn value\n", markdown)

	def test_unfilled_code_cohort_keeps_a_geometry_encoded_blank_line(self):
		stream = b"\n".join(
			[
				text_op(96, 720, "first_code_line()", "F4", 10),
				text_op(96, 694, "second_code_line()", "F4", 10),
				text_op(72, 660, "Ordinary body", "F1", 10),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertIn("first_code_line()\n\nsecond_code_line()", markdown)

	def test_consecutive_small_cap_monospace_labels_remain_separate_blocks(self):
		stream = b"\n".join(
			[
				b"BT /F4 7.5 Tf 0.375 Tc 1 0 0 1 83 720 Tm (FIRST LABEL) Tj ET",
				b"BT /F4 7.5 Tf 0.375 Tc 1 0 0 1 83 705 Tm (SECOND LABEL) Tj ET",
				text_op(83, 675, "Ordinary body one", "F1", 10),
				text_op(83, 650, "Ordinary body two", "F1", 10),
				text_op(83, 625, "Ordinary body three", "F1", 10),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertIn("`FIRST LABEL`\n\n`SECOND LABEL`", markdown)

	def test_wrapped_annotation_with_one_target_emits_one_logical_link(self):
		stream = b"\n".join(
			[
				text_op(72, 720, "linked words", "F1", 10),
				text_op(72, 706, "continue here", "F1", 10),
			]
		)
		pdf = make_pdf(
			[stream],
			annots={1: [link_annot(70, 695, 250, 735, "https://example.com/wrapped")]},
		)
		markdown = convert(pdf, ConvertOptions()).markdown
		self.assertIn("[linked words continue here](https://example.com/wrapped)", markdown)
		self.assertEqual(markdown.count("https://example.com/wrapped"), 1)

	def test_rule_above_heading_survives_while_heading_underline_is_suppressed(self):
		stream = b"\n".join(
			[
				text_op(72, 760, "Ordinary body establishes size", "F1", 10),
				text_op(72, 720, "SMALL CASE LABEL", "F2", 7.5),
				line_op(72, 700, 360, 700, 1),
				text_op(72, 680, "Section Heading", "F2", 14),
				line_op(72, 660, 360, 660, 1),
				text_op(72, 630, "Following body", "F1", 10),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertEqual(markdown.count("---"), 1)

	def test_repeated_parallel_container_edges_are_not_thematic_rules(self):
		stream = b"\n".join(
			[
				line_op(72, 720, 360, 720, 1),
				line_op(72, 680, 360, 680, 1),
				line_op(72, 640, 360, 640, 1),
				text_op(90, 700, "First framed row", "F1", 10),
				text_op(90, 660, "Second framed row", "F1", 10),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertNotIn("---", markdown)

	def test_repeated_indented_missing_bullets_under_ordered_parent(self):
		stream = b"\n".join(
			[
				text_op(72, 720, "SENTINEL nested list:", "F1", 10),
				text_op(72, 700, "1. parent ordered item", "F1", 10),
				text_op(112, 682, "child unordered alpha", "F1", 10),
				text_op(112, 660, "child unordered bravo", "F1", 10),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertIn("1. parent ordered item", markdown)
		self.assertIn("   - child unordered alpha", markdown)
		self.assertIn("   - child unordered bravo", markdown)

	def test_missing_bullet_sibling_survives_intervening_nested_descendants(self):
		stream = b"\n".join(
			[
				text_op(72, 740, "List context:", "F1", 10),
				text_op(96, 720, "Unordered item one.", "F1", 10),
				text_op(96, 700, "Unordered item two.", "F1", 10),
				text_op(120, 680, "Nested item alpha.", "F1", 10),
				text_op(120, 660, "Nested item bravo.", "F1", 10),
				text_op(144, 640, "1. Nested ordered one.", "F1", 10),
				text_op(144, 620, "2. Nested ordered two.", "F1", 10),
				text_op(96, 600, "Unordered item three.", "F1", 10),
				text_op(96, 582, "Continuation for item three.", "F1", 10),
				text_op(72, 540, "Ordinary context alpha.", "F1", 10),
				text_op(72, 520, "Ordinary context bravo.", "F1", 10),
				text_op(72, 500, "Ordinary context charlie.", "F1", 10),
				text_op(72, 480, "Ordinary context delta.", "F1", 10),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertIn("- Unordered item three.\n  Continuation for item three.", markdown)

	def test_no_separator_column_band_reads_left_column_first(self):
		stream = b"\n".join(
			[
				line_op(60, 670, 60, 735, 3),
				text_op(300, 720, "Right one starts.", "F1", 10),
				text_op(72, 714, "Left one starts.", "F1", 10),
				text_op(72, 698, "Left two continues.", "F1", 10),
				text_op(300, 698, "Right two continues.", "F1", 10),
				text_op(72, 682, "Left three ends.", "F1", 10),
				text_op(300, 682, "Right three ends.", "F1", 10),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertLess(markdown.index("Left three ends."), markdown.index("Right one starts."))
		self.assertNotIn("continues.Right", markdown)

	def test_column_content_finishes_before_a_label_just_below_the_rule(self):
		stream = b"\n".join(
			[
				line_op(288, 520, 288, 720, 1),
				text_op(310, 700, "Right column first.", "F1", 10),
				text_op(72, 700, "Left column first.", "F1", 10),
				text_op(72, 670, "Left column second.", "F1", 10),
				text_op(310, 670, "Right column second.", "F1", 10),
				text_op(72, 510, "CASE AFTER COLUMNS", "F4", 7.5),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertLess(markdown.index("Left column second."), markdown.index("Right column first."))
		self.assertLess(markdown.index("Right column second."), markdown.index("CASE AFTER COLUMNS"))

	def test_multiple_disconnected_column_bands_are_ordered_independently(self):
		stream = b"\n".join(
			[
				line_op(288, 620, 288, 720, 1),
				line_op(288, 400, 288, 500, 1),
				text_op(72, 700, "Band A left first.", "F1", 10),
				text_op(310, 700, "Band A right first.", "F1", 10),
				text_op(72, 675, "Band A left second.", "F1", 10),
				text_op(310, 675, "Band A right second.", "F1", 10),
				text_op(72, 580, "Between column bands.", "F1", 10),
				text_op(72, 480, "Band B left first.", "F1", 10),
				text_op(310, 480, "Band B right first.", "F1", 10),
				text_op(72, 455, "Band B left second.", "F1", 10),
				text_op(310, 455, "Band B right second.", "F1", 10),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertLess(markdown.index("Band A left second."), markdown.index("Band A right first."))
		self.assertLess(markdown.index("Band A right second."), markdown.index("Between column bands."))
		self.assertLess(markdown.index("Band B left second."), markdown.index("Band B right first."))

	def test_vertical_east_asian_columns_follow_source_sequence_not_x_interleaving(self):
		def vertical_glyph(x, y, value):
			actual = ("FEFF" + value.encode("utf-16-be").hex()).encode("ascii")
			return (
				b"/Span << /ActualText <%s> >> BDC\n" % actual
				+ text_op(x, y, "x", "F1", 10)
				+ b"\nEMC"
			)

		first = "縦書き日本語"
		second = "確認文字列。"
		parts = [vertical_glyph(120, 720 - index * 11, char) for index, char in enumerate(first)]
		parts.extend(vertical_glyph(100, 720 - index * 11, char) for index, char in enumerate(second))
		markdown = convert(make_pdf([b"\n".join(parts)]), ConvertOptions()).markdown
		self.assertIn(first + second, markdown)
		self.assertNotIn("確縦", markdown)

	def test_rotated_text_run_preserves_source_character_order(self):
		stream = b"BT /F1 10 Tf 0.978 0.208 -0.208 0.978 120 680 Tm (Rotated label -12 degrees) Tj ET"
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertIn("Rotated label -12 degrees", markdown)
		self.assertNotIn("degrees label Rotated", markdown)
		self.assertNotIn('<p align="right">Rotated label', markdown)

	def test_closed_panel_promotes_its_short_leading_label(self):
		stream = b"\n".join(
			[
				line_op(72, 600, 300, 600, 1),
				line_op(300, 600, 300, 700, 1),
				line_op(300, 700, 72, 700, 1),
				line_op(72, 700, 72, 600, 1),
				text_op(84, 680, "Language label", "F1", 10),
				text_op(84, 665, "Panel body wraps across", "F1", 10),
				text_op(84, 650, "a second visual line.", "F1", 10),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertIn("**Language label** Panel body wraps across a second visual line.", markdown)

	def test_closed_code_border_supports_single_line_fenced_code(self):
		stream = b"\n".join(
			[
				text_op(72, 740, "Proportional context.", "F1", 10),
				line_op(100, 650, 430, 650, 1),
				line_op(430, 650, 430, 690, 1),
				line_op(430, 690, 100, 690, 1),
				line_op(100, 690, 100, 650, 1),
				text_op(115, 665, 'quoted_call("value")', "F4", 9),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertIn('```\nquoted_call("value")\n```', markdown)

	def test_decoration_endpoint_does_not_capture_adjacent_punctuation(self):
		font = Font(name="F1", base_font="Helvetica")
		chars = [
			Char(char, index * 5.0, 0, index * 5.0 + 5.0, 10, 10, font, 1, index)
			for index, char in enumerate("strike,")
		]
		converter = Converter(make_pdf([b""]))
		converter.page_sizes = {1: (612.0, 792.0)}
		converter.chars = chars
		converter.segments = [Segment(0, 5, 30, 5, 0.75, 1, 100)]
		converter._postprocess_chars()
		self.assertTrue(all(char.strike for char in converter.chars[:-1]))
		self.assertFalse(converter.chars[-1].strike)

	def test_dotted_section_continuations_are_not_missing_bullets(self):
		stream = b"\n".join(
			[
				text_op(72, 720, "- 7.2 Borderless Table", "F1", 10),
				text_op(112, 702, "7.1 GFM Lattice Table", "F1", 10),
				text_op(112, 680, "7.3 Complex HTML Table", "F1", 10),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertIn("7.1 GFM Lattice Table", markdown)
		self.assertIn("7.3 Complex HTML Table", markdown)
		self.assertNotIn("- 7.1 GFM Lattice Table", markdown)
		self.assertNotIn("- 7.3 Complex HTML Table", markdown)

	def test_callout_region_report(self):
		stream = b"\n".join(
			[
				rect_fill_op(68, 690, 220, 40, 0.94),
				text_op(80, 715, "Callout text", "F1", 10),
			]
		)
		result = convert(make_pdf([stream]), ConvertOptions())
		self.assertIn("callout", {region["kind"] for region in result.report["regions"]})

	def test_neutral_panel_with_diagonal_path_evidence_is_a_vector_asset(self):
		stream = b"\n".join(
			[
				b".98 g 72 560 320 120 re f 0 g",
				b"0 .2 .8 RG 3 w 90 590 m 150 620 l 220 610 l 300 650 l 370 630 l S",
				text_op(90, 655, "Panel trend", "F1", 10),
			]
		)
		result = convert(make_pdf([stream]), ConvertOptions(assets_dir="assets"))
		self.assertTrue(any(name.endswith(".svg") for name in result.assets))
		self.assertNotIn('<div style="border: 1px solid', result.markdown)

	def test_colored_bars_and_axes_inside_white_canvas_are_a_vector_asset(self):
		stream = b"\n".join(
			[
				b"1 g 72 500 360 170 re f 0 g",
				b".2 .4 .8 rg 120 520 50 70 re f",
				b".2 .7 .4 rg 220 520 50 110 re f",
				b".9 .5 0 rg 320 520 50 90 re f 0 g",
				line_op(100, 520, 400, 520, 2),
				line_op(100, 520, 100, 650, 2),
				text_op(125, 505, "Alpha", "F1", 10),
				text_op(225, 505, "Beta", "F1", 10),
				text_op(325, 505, "Gamma", "F1", 10),
			]
		)
		result = convert(make_pdf([stream]), ConvertOptions(assets_dir="assets"))
		self.assertTrue(any(name.endswith(".svg") for name in result.assets))
		self.assertIn("vector-", result.markdown)

	def test_adjacent_vector_panels_with_one_caption_group_as_one_figure(self):
		stream = b"\n".join(
			[
				b".98 g 72 540 200 100 re f 0 g",
				b"0 .2 .8 RG 3 w 90 560 m 125 585 l 165 575 l 210 615 l 250 600 l S",
				text_op(88, 620, "Panel A", "F2", 10),
				b".98 g 292 540 200 100 re f 0 g",
				b".8 .2 0 RG 3 w 310 610 m 350 585 l 390 595 l 435 565 l 475 555 l S",
				text_op(308, 620, "Panel B", "F2", 10),
				text_op(205, 515, "Figure 2: Shared panels", "F3", 10),
			]
		)
		result = convert(make_pdf([stream]), ConvertOptions(assets_dir="assets"))
		self.assertEqual(sum(name.endswith(".svg") for name in result.assets), 1)
		self.assertEqual(result.markdown.count("<figcaption>Figure 2: Shared panels</figcaption>"), 1)
		self.assertEqual(result.markdown.count("<img "), 1)

	def test_html_renderer_escapes_raw_html_and_drops_unsafe_links(self):
		html = render_html("<script>alert(1)</script>\n\n[bad](javascript:alert(1))")
		self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
		self.assertNotIn("<script>", html)
		self.assertNotIn("javascript:alert", html)
		self.assertIn("<p>bad</p>", html)

	def test_html_renderer_allows_safe_image_assets_only(self):
		html = render_html(
			"![Alt](assets/img.png)\n\n"
			"![Embedded](data:image/png;base64,AAAA)\n\n"
			"![Bad](../secret.png)"
		)
		self.assertIn('<img src="assets/img.png" alt="Alt" />', html)
		self.assertIn('<img src="data:image/png;base64,AAAA" alt="Embedded" />', html)
		self.assertNotIn('src="../secret.png"', html)
		self.assertIn("![Bad](../secret.png)", html)

	def test_html_renderer_preserves_generated_figures_lists_quotes_and_page_comments(self):
		markdown = (
			"3. Third item.\n"
			"4. Fourth item.\n"
			"   continuation.\n\n"
			"    - Nested A.\n"
			"    - Nested B.\n\n"
			"        a. Alpha child.\n"
			"        b. Beta child.\n\n"
			"- [x] Checked task.\n"
			"- [ ] Open task.\n\n"
			"> Quote.\n>\n> ```\n> call(\"x\")\n> ```\n\n"
			'<figure class="cocoapdf-figure cocoapdf-align-center">\n'
			'<img src="C:/Temp/generated-assets/img.png" alt="Alt" '
			'style="width: 10.000pt; height: 10.000pt; max-width: 100%; object-fit: contain;" />\n'
			"</figure>\n\n"
			"<!-- page 2 -->"
		)
		html = render_html(markdown)
		self.assertIn('<li>Fourth item. continuation.\n<ul>', html)
		self.assertIn('<ol type="a">', html)
		self.assertIn('<input type="checkbox" disabled checked />', html)
		self.assertIn('<pre><code>call(&quot;x&quot;)</code></pre>', html)
		self.assertIn('<figure class="cocoapdf-figure', html)
		self.assertNotIn("&lt;figure", html)
		self.assertIn("<!-- page 2 -->", html)
		self.assertNotIn("&lt;!-- page 2", html)

	def test_html_renderer_keeps_single_alpha_prose_and_gfm_table_alignment(self):
		html = render_html(
			"I. Newton wrote this sentence.\n\n"
			"| Item | Status | Amount |\n"
			"| --- | :---: | ---: |\n"
			"| Alpha | Ready | 12.50 |"
		)
		self.assertIn("<p>I. Newton wrote this sentence.</p>", html)
		self.assertNotIn('<ol type="A"', html)
		self.assertIn('<th style="text-align: center;">Status</th>', html)
		self.assertIn('<th style="text-align: right;">Amount</th>', html)
		self.assertIn('<td style="text-align: right;">12.50</td>', html)

	def test_bare_mailto_link_annotation_renders_as_autolink(self):
		stream = text_op(72, 720, "mailto:bare@example.org", "F1", 10)
		pdf = make_pdf([stream], annots={1: [link_annot(70, 716, 230, 735, "mailto:bare@example.org")]})
		markdown = convert(pdf, ConvertOptions()).markdown
		self.assertEqual(markdown, "<mailto:bare@example.org>\n")

	def test_html_image_markup_preserves_pdf_size_and_alignment(self):
		stream = b"q 300 0 0 100 156 500 cm /Im1 Do Q"
		pdf = make_pdf([stream], xobjects={"Im1": image_xobject_rgb(1, 1, b"\xff\x00\x00")})
		result = convert(pdf, ConvertOptions(image_markup="html"))
		self.assertIn("width: 300.000pt", result.markdown)
		self.assertIn("height: 100.000pt", result.markdown)
		self.assertIn("cocoapdf-align-center", result.markdown)
		self.assertEqual(result.report["images_detail"][0]["alignment"], "center")

	def test_embedded_image_mode_uses_data_uri(self):
		stream = b"q 20 0 0 20 72 700 cm /Im1 Do Q"
		pdf = make_pdf([stream], xobjects={"Im1": image_xobject_rgb(1, 1, b"\xff\x00\x00")})
		markdown = convert(pdf, ConvertOptions(image_mode="embed")).markdown
		self.assertIn('src="data:image/png;base64,', markdown)
		self.assertNotIn("assets/img-", markdown)

	def test_raw_image_decode_array_is_applied_without_rejecting_image(self):
		raw = zlib.compress(b"\x00\x80\xff")
		xobject = (
			b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
			b"/ColorSpace /DeviceRGB /BitsPerComponent 8 "
			b"/Decode [1 0 0 1 0 1] /Filter /FlateDecode /Length %d >>\nstream\n"
			% len(raw)
		) + raw + b"\nendstream"
		pdf = make_pdf([b"q 20 0 0 20 72 700 cm /Im1 Do Q"], xobjects={"Im1": xobject})
		result = convert(pdf, ConvertOptions())
		self.assertEqual(result.report["images"], 1)
		self.assertTrue(next(iter(result.assets.values())).startswith(b"\x89PNG"))
		self.assertNotIn("IMAGE_UNSUPPORTED", [warning.code for warning in result.warnings])

	def test_near_white_text_over_a_pale_raster_is_treated_as_concealed_overlay(self):
		stream = b"\n".join(
			[
				b"q 200 0 0 100 72 600 cm /Im1 Do Q",
				b"1 g",
				text_op(90, 650, "concealed raster overlay", "F1", 10),
			]
		)
		result = convert(
			make_pdf([stream], xobjects={"Im1": image_xobject_rgb(1, 1, bytes((244, 239, 225)))}),
			ConvertOptions(),
		)
		self.assertNotIn("concealed raster overlay", result.markdown)
		self.assertTrue(any(w.code == "INVISIBLE_TEXT" for w in result.warnings))

	def test_high_contrast_white_text_over_a_dark_raster_is_preserved(self):
		stream = b"\n".join(
			[
				b"q 200 0 0 100 72 600 cm /Im1 Do Q",
				b"1 g",
				text_op(90, 650, "visible raster caption", "F1", 10),
			]
		)
		result = convert(
			make_pdf([stream], xobjects={"Im1": image_xobject_rgb(1, 1, b"\x00\x00\x00")}),
			ConvertOptions(),
		)
		self.assertIn("visible raster caption", result.markdown)

	def test_internal_destination_link_emits_anchor(self):
		streams = [
			b"BT /F1 10 Tf 1 0 0 1 72 720 Tm (Go second) Tj ET",
			b"BT /F1 10 Tf 1 0 0 1 72 720 Tm (Target) Tj ET",
		]
		pdf = make_pdf(
			streams,
			annots={
				1: [
					b"<< /Type /Annot /Subtype /Link /Rect [70 716 160 735] "
					b"/A << /S /GoTo /D [9 0 R /XYZ 0 720 0] >> >>"
				]
			},
		)
		result = convert(pdf, ConvertOptions())
		self.assertIn("[Go second](#pdf-dest-", result.markdown)
		self.assertIn('<a id="pdf-dest-', result.markdown)
		self.assertTrue(result.report["anchors"])

	def test_captioned_styled_key_value_rows_emit_borderless_table(self):
		stream = b"\n".join(
			[
				text_op(190, 720, "Table 4: Styled profile", "F1", 10),
				text_op(72, 690, "Project", "F2", 10),
				text_op(160, 690, "CocoaPDF", "F1", 10),
				text_op(72, 668, "Runtime policy", "F2", 10),
				text_op(160, 668, "stdlib only", "F1", 10),
				text_op(72, 646, "Primary output", "F2", 10),
				text_op(160, 646, "Markdown", "F1", 10),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertIn("<caption>Table 4: Styled profile</caption>", markdown)
		self.assertIn('<th scope="row">Project</th><td>CocoaPDF</td>', markdown)
		self.assertNotIn("<td>Project CocoaPDF</td>", markdown)

	def test_aligned_printed_controls_emit_field_value_grid(self):
		def outline(x0, y0, x1, y1):
			return [
				line_op(x0, y0, x1, y0, 1),
				line_op(x1, y0, x1, y1, 1),
				line_op(x1, y1, x0, y1, 1),
				line_op(x0, y1, x0, y0, 1),
			]

		parts = []
		parts.extend(outline(160, 690, 430, 720))
		parts.extend([text_op(72, 700, "Name", "F1", 10), text_op(170, 700, "Ada Example", "F1", 10)])
		parts.extend(outline(160, 640, 430, 670))
		parts.extend([text_op(72, 650, "Mode", "F1", 10), text_op(170, 650, "Verified", "F1", 10)])
		parts.extend([text_op(72, 620, "Approved", "F1", 10), text_op(170, 620, "Yes", "F1", 10)])
		parts.extend(outline(160, 540, 430, 600))
		parts.extend(
			[
				text_op(72, 555, "Notes", "F1", 10),
				text_op(170, 580, "Printed appearance; a browser-generated PDF", "F1", 10),
				text_op(170, 560, "may not create a form widget.", "F1", 10),
			]
		)
		result = convert(make_pdf([b"\n".join(parts)]), ConvertOptions())
		self.assertIn('<th scope="row">Name</th><td>Ada Example</td>', result.markdown)
		self.assertIn('<th scope="row">Approved</th><td>Yes</td>', result.markdown)
		self.assertIn(
			'<th scope="row">Notes</th><td>Printed appearance; a browser-generated PDF may not create a form widget.</td>',
			result.markdown,
		)
		self.assertTrue(any(warning.code == "FORM_APPEARANCE_GRID" for warning in result.warnings))

	def test_bold_header_and_repeated_numeric_columns_emit_borderless_table(self):
		parts = [
			text_op(72, 720, "Metric", "F2", 10),
			text_op(180, 720, "2025", "F2", 10),
			text_op(280, 720, "2026", "F2", 10),
			text_op(380, 720, "Change", "F2", 10),
		]
		for y, name, first, second, change in (
			(695, "Revenue", "12,400.00", "13,888.25", "+12.00%"),
			(670, "Cost", "7,010.80", "7,122.10", "+1.59%"),
			(645, "Margin", "43.46%", "48.72%", "+5.26 pp"),
		):
			parts.extend(
				[
					text_op(72, y, name, "F1", 10),
					text_op(220, y, first, "F1", 10),
					text_op(320, y, second, "F1", 10),
					text_op(420, y, change, "F1", 10),
				]
			)
		markdown = convert(make_pdf([b"\n".join(parts)]), ConvertOptions()).markdown
		self.assertIn("| Metric | 2025 | 2026 | Change |", markdown)
		self.assertIn("| Revenue | 12,400.00 | 13,888.25 | +12.00% |", markdown)
		self.assertIn("| --- | ---: | ---: | ---: |", markdown)

	def test_ruled_table_infers_centered_and_numeric_column_alignment(self):
		parts = []
		for y in (720, 680, 640, 600):
			parts.append(line_op(72, y, 410, y, 1))
		for x in (72, 180, 300, 410):
			parts.append(line_op(x, 600, x, 720, 1))
		parts.extend(
			[
				text_op(84, 695, "Item", "F2", 10),
				text_op(226, 695, "Status", "F2", 10),
				text_op(350, 695, "Amount", "F2", 10),
				text_op(84, 655, "Alpha", "F1", 10),
				text_op(228, 655, "Ready", "F1", 10),
				text_op(365, 655, "12.50", "F1", 10),
				text_op(84, 615, "Beta", "F1", 10),
				text_op(224, 615, "Review", "F1", 10),
				text_op(365, 615, "42.75", "F1", 10),
			]
		)
		result = convert(make_pdf([b"\n".join(parts)]), ConvertOptions())
		self.assertIn("| --- | :---: | ---: |", result.markdown)
		self.assertIn('style="text-align: center;"', result.html)
		self.assertIn('style="text-align: right;"', result.html)

	def test_aligned_prose_without_numeric_column_evidence_stays_prose(self):
		stream = b"\n".join(
			[
				text_op(72, 720, "Observation", "F2", 10),
				text_op(200, 720, "Editorial prose statement", "F1", 10),
				text_op(72, 695, "Constraint", "F2", 10),
				text_op(200, 695, "Geometry alone is insufficient", "F1", 10),
				text_op(72, 670, "Decision", "F2", 10),
				text_op(200, 670, "Keep these as paragraphs", "F1", 10),
			]
		)
		markdown = convert(make_pdf([stream]), ConvertOptions()).markdown
		self.assertNotIn("| Observation |", markdown)

	def test_partial_lattice_table_uses_html_colspan_fallback(self):
		stream = b"\n".join(
			[
				line_op(72, 700, 360, 700, 1),
				line_op(72, 660, 360, 660, 1),
				line_op(72, 620, 360, 620, 1),
				line_op(72, 620, 72, 700, 1),
				line_op(160, 620, 160, 660, 1),
				line_op(260, 620, 260, 700, 1),
				line_op(360, 620, 360, 700, 1),
				text_op(84, 676, "Grouped", "F1", 10),
				text_op(272, 676, "Solo", "F1", 10),
				text_op(84, 636, "Left", "F1", 10),
				text_op(172, 636, "Mid", "F1", 10),
				text_op(272, 636, "Right", "F1", 10),
			]
		)
		result = convert(make_pdf([stream]), ConvertOptions())
		self.assertIn("<table>", result.markdown)
		self.assertIn('<td colspan="2">Grouped</td>', result.markdown)
		self.assertTrue(any(w.code == "TABLE_SPAN_UNSUPPORTED" for w in result.warnings))

	def test_page_range_limits_processed_pages(self):
		pdf = make_pdf(
			[
				text_op(72, 720, "Page one", "F1", 10),
				text_op(72, 720, "Page two", "F1", 10),
				text_op(72, 720, "Page three", "F1", 10),
			]
		)
		result = convert(pdf, ConvertOptions(pages="2-3"))
		self.assertNotIn("Page one", result.markdown)
		self.assertIn("Page two", result.markdown)
		self.assertIn("Page three", result.markdown)
		self.assertEqual(result.report["processed_pages"], [2, 3])

	def test_trace_and_overlay_tools(self):
		pdf = make_pdf([text_op(72, 720, "Trace me", "F1", 10)])
		with tempfile.TemporaryDirectory() as tmp:
			path = Path(tmp) / "sample.pdf"
			path.write_bytes(pdf)
			trace = trace_pdf(path, 1)
			self.assertIn("Trace me", trace)
			svg = region_overlay_svg(path, 1)
			self.assertIn("<svg", svg)

	def test_bench_v1_runs(self):
		buf = StringIO()
		with redirect_stdout(buf):
			code = bench_v1()
		self.assertIn(code, {0, 1})
		self.assertIn('"suite": "v1"', buf.getvalue())


# ---- parser, content-stream, page, and font adversarial cases ----


def render_pdf(objects: Iterable[bytes], root: int, *, xref: bool = True, trailer_extra: bytes = b"") -> bytes:
	objects = list(objects)
	data = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
	offsets = [0]
	for number, body in enumerate(objects, 1):
		offsets.append(len(data))
		data.extend(("%d 0 obj\n" % number).encode("ascii"))
		data.extend(body)
		data.extend(b"\nendobj\n")
	if xref:
		xref_offset = len(data)
		data.extend(("xref\n0 %d\n" % (len(objects) + 1)).encode("ascii"))
		data.extend(b"0000000000 65535 f \n")
		for offset in offsets[1:]:
			data.extend(("%010d 00000 n \n" % offset).encode("ascii"))
		data.extend(
			(
				"trailer\n<< /Size %d /Root %d 0 R "
				% (len(objects) + 1, root)
			).encode("ascii")
		)
		data.extend(trailer_extra)
		data.extend((" >>\nstartxref\n%d\n%%%%EOF\n" % xref_offset).encode("ascii"))
	else:
		data.extend(("trailer\n<< /Size %d /Root %d 0 R " % (len(objects) + 1, root)).encode("ascii"))
		data.extend(trailer_extra)
		data.extend(b" >>\n%%EOF\n")
	return bytes(data)


def one_page_pdf(
	content: bytes,
	*,
	extra_resources: bytes = b"",
	extra_objects: Iterable[bytes] = (),
	page_extra: bytes = b"",
	catalog_extra: bytes = b"",
	xref: bool = True,
) -> bytes:
	objects = [
		b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
		b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
		(
			b"<< /Type /Page /Parent 4 0 R /MediaBox [0 0 612 792] "
			b"/Resources << /Font << /F1 1 0 R >> " + extra_resources + b" >> "
			b"/Contents 2 0 R " + page_extra + b" >>"
		),
		b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
		b"<< /Type /Catalog /Pages 4 0 R " + catalog_extra + b" >>",
	]
	objects.extend(extra_objects)
	return render_pdf(objects, 5, xref=xref)


class ParserRegressionTests(unittest.TestCase):
	def test_forward_indirect_stream_length_is_authoritative(self) -> None:
		content = b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (before\nendstream\nendobj\nafter) Tj ET"
		objects = [
			b"<< /Length 6 0 R >>\nstream\n" + content + b"\nendstream",
			b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
			b"<< /Type /Page /Parent 4 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 2 0 R >> >> /Contents 1 0 R >>",
			b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
			b"<< /Type /Catalog /Pages 4 0 R >>",
			str(len(content)).encode("ascii"),
		]
		result = convert(render_pdf(objects, 5))
		self.assertIn("before endstream endobj after", result.markdown)
		self.assertNotIn("WARN_BAD_LENGTH", {warning.code for warning in result.warnings})

	def test_recovery_does_not_invent_object_from_stream_payload(self) -> None:
		content = b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (visible 99 0 obj \\(FAKE\\) endobj text) Tj ET"
		data = one_page_pdf(content, xref=False)
		document = PdfDocument(data)
		self.assertNotIn((99, 0), document.objects)
		self.assertIn("RECOVERED", {warning.code for warning in document.warnings})


class ContentRegressionTests(unittest.TestCase):
	def test_rectangular_clip_excludes_outside_text(self) -> None:
		content = b"q 0 0 10 10 re W n BT /F1 12 Tf 1 0 0 1 72 720 Tm (CLIPPED) Tj ET Q"
		self.assertEqual(convert(one_page_pdf(content)).markdown, "")

	def test_disjoint_clip_intersection_remains_empty(self) -> None:
		content = (
			b"q 0 0 10 10 re W n 100 100 10 10 re W n "
			b"BT /F1 12 Tf 1 0 0 1 2 2 Tm (HIDDEN) Tj ET Q"
		)
		self.assertEqual(convert(one_page_pdf(content)).markdown, "")

	def test_zero_alpha_text_is_not_emitted(self) -> None:
		content = b"/GS0 gs BT /F1 12 Tf 1 0 0 1 72 720 Tm (INVISIBLE) Tj ET"
		data = one_page_pdf(
			content,
			extra_resources=b"/ExtGState << /GS0 6 0 R >>",
			extra_objects=[b"<< /Type /ExtGState /ca 0 /CA 0 >>"],
		)
		self.assertEqual(convert(data).markdown, "")

	def test_named_property_actual_text_is_used(self) -> None:
		content = b"/Span /P1 BDC BT /F1 12 Tf 1 0 0 1 72 720 Tm (Wrong) Tj ET EMC"
		properties = b"<< /ActualText <FEFF0043006F00720072006500630074> /MCID 0 >>"
		data = one_page_pdf(
			content,
			extra_resources=b"/Properties << /P1 6 0 R >>",
			extra_objects=[properties],
		)
		result = convert(data)
		self.assertIn("Correct", result.markdown)
		self.assertNotIn("Wrong", result.markdown)

	def test_artifact_marked_content_is_not_emitted(self) -> None:
		content = (
			b"/Artifact BMC BT /F1 12 Tf 1 0 0 1 72 740 Tm (HEADER) Tj ET EMC "
			b"BT /F1 12 Tf 1 0 0 1 72 700 Tm (Body) Tj ET"
		)
		result = convert(one_page_pdf(content))
		self.assertNotIn("HEADER", result.markdown)
		self.assertIn("Body", result.markdown)

	def test_basic_inline_rgb_image_is_extracted(self) -> None:
		content = (
			b"q 20 0 0 20 72 650 cm BI /W 1 /H 1 /CS /RGB /BPC 8 ID "
			+ bytes([255, 0, 0])
			+ b" EI Q BT /F1 12 Tf 1 0 0 1 72 720 Tm (Text) Tj ET"
		)
		result = convert(one_page_pdf(content))
		self.assertEqual(len(result.assets), 1)
		self.assertIn("img-", result.markdown)
		self.assertNotIn("INLINE_IMAGE_SKIPPED", {warning.code for warning in result.warnings})

	def test_packed_one_bit_inline_image_is_extracted(self) -> None:
		content = b"q 80 0 0 10 72 650 cm BI /W 8 /H 1 /CS /G /BPC 1 ID \xff EI Q"
		result = convert(one_page_pdf(content))
		self.assertEqual(len(result.assets), 1)
		self.assertFalse(any(warning.code == "IMAGE_UNSUPPORTED" for warning in result.warnings))

	def test_packed_rows_are_byte_aligned_independently(self) -> None:
		content = b"q 10 0 0 20 72 650 cm BI /W 1 /H 2 /CS /G /BPC 1 ID \x80\x00 EI Q"
		result = convert(one_page_pdf(content))
		self.assertEqual(len(result.assets), 1)
		self.assertFalse(any(warning.code == "IMAGE_UNSUPPORTED" for warning in result.warnings))

	def test_inline_image_crlf_and_filter_abbreviation(self) -> None:
		raw = bytes([255, 0, 0])
		encoded = zlib.compress(raw)
		content = b"q 10 0 0 10 72 650 cm BI /W 1 /H 1 /CS /RGB /BPC 8 /F /Fl ID\r\n" + encoded + b" EI Q"
		result = convert(one_page_pdf(content))
		self.assertEqual(len(result.assets), 1)
		self.assertFalse(any(warning.code in {"FILTER_UNSUPPORTED", "IMAGE_UNSUPPORTED"} for warning in result.warnings))


class PageAndTaggedRegressionTests(unittest.TestCase):
	@staticmethod
	def three_page_pdf() -> bytes:
		objects = [b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
		for word in (b"one", b"two", b"three"):
			content = b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (" + word + b") Tj ET"
			objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
		objects.extend([
			b"<< /Type /Page /Parent 8 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 1 0 R >> >> /Contents 2 0 R >>",
			b"<< /Type /Page /Parent 8 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 1 0 R >> >> /Contents 3 0 R >>",
			b"<< /Type /Page /Parent 8 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 1 0 R >> >> /Contents 4 0 R >>",
			b"<< /Type /Pages /Kids [5 0 R 6 0 R 7 0 R] /Count 3 >>",
			b"<< /Type /Catalog /Pages 8 0 R >>",
		])
		return render_pdf(objects, 9)

	def test_page_boundary_does_not_merge_unrelated_short_blocks(self) -> None:
		markdown = convert(self.three_page_pdf()).markdown
		self.assertEqual(markdown, "one\n\ntwo\n\nthree\n")

	def test_page_breaks_are_preserved_before_paragraph_joining(self) -> None:
		markdown = convert(self.three_page_pdf(), ConvertOptions(page_breaks=True)).markdown
		self.assertIn("<!-- page 2 -->", markdown)
		self.assertIn("<!-- page 3 -->", markdown)

	def test_unselected_pages_are_reported_explicitly(self) -> None:
		result = convert(self.three_page_pdf(), ConvertOptions(pages="2"))
		self.assertEqual(result.report["mode_per_page"], ["not_selected", "geometric", "not_selected"])
		self.assertEqual(result.report["processed_pages"], [2])

	def test_tagged_mcid_has_page_and_glyph_provenance(self) -> None:
		content = b"/P <</MCID 0>> BDC BT /F1 12 Tf 1 0 0 1 72 720 Tm (Tagged) Tj ET EMC"
		objects = [
			b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
			b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
			b"<< /Type /Page /Parent 4 0 R /StructParents 0 /MediaBox [0 0 612 792] /Resources << /Font << /F1 1 0 R >> >> /Contents 2 0 R >>",
			b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
			b"<< /Type /StructElem /S /P /P 7 0 R /Pg 3 0 R /K 0 >>",
			b"<< /Nums [0 [5 0 R]] >>",
			b"<< /Type /StructTreeRoot /K [5 0 R] /ParentTree 6 0 R >>",
			b"<< /Type /Catalog /Pages 4 0 R /StructTreeRoot 7 0 R /MarkInfo << /Marked true >> >>",
		]
		result = convert(render_pdf(objects, 8))
		self.assertTrue(result.report.get("semantic_valid"), result.report.get("semantic_errors"))
		text_nodes = [node for node in result.report.get("nodes", []) if node.get("kind") == "text"]
		self.assertEqual(len(text_nodes), 1)
		self.assertEqual(text_nodes[0]["source_pages"], [1])
		self.assertEqual(text_nodes[0]["sources"][0]["mcids"], [0])
		self.assertTrue(text_nodes[0]["sources"][0]["glyph_ids"])

	def test_tagged_heading_role_drives_markdown_level(self) -> None:
		content = b"/H1 <</MCID 0>> BDC BT /F1 12 Tf 1 0 0 1 72 720 Tm (Tagged heading) Tj ET EMC"
		objects = [
			b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
			b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
			b"<< /Type /Page /Parent 4 0 R /StructParents 0 /MediaBox [0 0 612 792] /Resources << /Font << /F1 1 0 R >> >> /Contents 2 0 R >>",
			b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
			b"<< /Type /StructElem /S /H1 /P 7 0 R /Pg 3 0 R /K 0 >>",
			b"<< /Nums [0 [5 0 R]] >>",
			b"<< /Type /StructTreeRoot /K [5 0 R] /ParentTree 6 0 R >>",
			b"<< /Type /Catalog /Pages 4 0 R /StructTreeRoot 7 0 R /MarkInfo << /Marked true >> >>",
		]
		self.assertEqual(convert(render_pdf(objects, 8)).markdown, "# Tagged heading\n")


class EncodingRegressionTests(unittest.TestCase):
	def test_pdfdocencoding_and_utf8_bom(self) -> None:
		self.assertEqual(decode_pdf_text(bytes([0x80])), "\u2022")
		self.assertEqual(decode_pdf_text(bytes([0xAD])), "\ufffd")
		self.assertEqual(decode_pdf_text(b"\xef\xbb\xbfhello"), "hello")

	def test_undefined_winansi_code_is_not_fabricated_as_bullet(self) -> None:
		self.assertEqual(winansi_char(0x81), "\ufffd")

	def test_unknown_differences_glyph_is_not_fabricated_from_base_encoding(self) -> None:
		font = Font("F3", "CustomType3", subtype="Type3", differences={65: "heart"})
		self.assertEqual(font.decode(b"A")[0][1], "\ufffd")

	def test_unicode_predefined_cmap_recovers_text_without_tounicode(self) -> None:
		font = Font("F0", "HeiseiMin-W3", subtype="Type0", encoding="UniJIS-UTF16-H", composite=True)
		self.assertEqual("".join(item[1] for item in font.decode("A\U0001f600".encode("utf-16-be"))), "A\U0001f600")

	def test_vertical_writing_uses_dw2_and_w2_metrics(self) -> None:
		cmap = b"""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
1 begincodespacerange
<0000> <FFFF>
endcodespacerange
2 beginbfchar
<0001> <65E5>
<0002> <672C>
endbfchar
endcmap
CMapName currentdict /CMap defineresource pop
end
end"""
		content = b"BT /F1 12 Tf 1 0 0 1 72 720 Tm <00010002> Tj ET"
		objects = [
			b"<< /Type /Font /Subtype /Type0 /BaseFont /Vertical /Encoding /Identity-V /DescendantFonts [7 0 R] /ToUnicode 6 0 R >>",
			b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
			b"<< /Type /Page /Parent 4 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 1 0 R >> >> /Contents 2 0 R >>",
			b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
			b"<< /Type /Catalog /Pages 4 0 R >>",
			b"<< /Length %d >>\nstream\n" % len(cmap) + cmap + b"\nendstream",
			b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Vertical /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> /DW 1000 /DW2 [880 -1000] /W2 [1 [ -900 600 850 -1100 500 800 ]] >>",
		]
		converter = Converter(render_pdf(objects, 5), ConvertOptions())
		result = converter.convert()
		self.assertIn("writing-mode: vertical-rl", result.markdown)
		self.assertIn("日本", result.markdown)
		self.assertIn("writing-mode: vertical-rl", result.html)
		self.assertEqual(len(converter.chars), 2)
		self.assertAlmostEqual(converter.chars[0].x0, 73.2, places=3)
		self.assertAlmostEqual(converter.chars[1].x0, 72.0, places=3)
		self.assertGreater(converter.chars[1].y0, converter.chars[0].y0 + 10.0)
		self.assertEqual(parse_w2_array([1, [-900, 600, 850], 2, 3, -1100, 500, 800])[3], (-1100.0, 500.0, 800.0))


# ---- integrity and security regressions ----


class IntegrityRegressionTests(unittest.TestCase):
	def test_find_interval_clamps_both_sides(self):
		self.assertEqual(find_interval([10, 20, 30], 5), 0)
		self.assertEqual(find_interval([10, 20, 30], 35), 1)

	def test_composite_default_width_is_1000(self):
		self.assertEqual(Font("F", composite=True).width_for_code(42), 1000.0)

	def test_type2_xref_overrides_stale_direct_object(self):
		doc = PdfDocument.__new__(PdfDocument)
		doc.data = b""
		doc.objects = {
			(5, 0): b"old",
			(10, 0): Stream({"Type": "ObjStm", "N": 1, "First": 4}, b"5 0 (new)", objnum=10),
		}
		doc.xref_entries = {(5, 0): XrefEntry(2, 10, 0)}
		doc.trailer = {}
		doc.warnings = []
		doc.parse_mode = "xref"
		doc.encrypted = False
		doc.encryption = {}
		doc.active_content = []
		doc.total_decoded = 0
		doc._unpack_object_streams()
		self.assertEqual(doc.objects[(5, 0)], b"new")

	def test_in_range_lie_does_not_trust_stream_length(self):
		doc = PdfDocument.__new__(PdfDocument)
		doc.warnings = []
		raw = doc._read_stream_raw(0, 2, 7, b"abc\nendstream\nendobj")
		self.assertEqual(raw, b"abc")
		self.assertIn("WARN_BAD_LENGTH", [warning.code for warning in doc.warnings])

	def test_page_rotation_and_user_unit_transform(self):
		matrix, width, height = page_normalization_transform(0, 0, 612, 792, 90, 1.0)
		self.assertEqual((width, height), (792, 612))
		x, y = apply_mat(matrix, 72, 708)
		self.assertAlmostEqual(x, 708)
		self.assertAlmostEqual(height - y, 72)

	def test_literal_markdown_is_escaped(self):
		self.assertEqual(escape_inline("*x* _y_ ~z~"), r"\*x\* \_y\_ \~z\~")
		self.assertEqual(escape_block_start("# prose"), r"\# prose")
		self.assertEqual(escape_block_start("1. prose"), r"1\. prose")

	def test_code_span_uses_longer_fence(self):
		rendered = render_code_span("a ` b")
		self.assertTrue(rendered.startswith("``"))
		self.assertTrue(rendered.endswith("``"))
		self.assertIn("<code>a ` b</code>", render_html(rendered))

	def test_encrypted_trailer_is_refused(self):
		pdf = make_pdf([text_op(72, 700, "secret")])
		pdf = pdf.replace(b" /Root ", b" /Encrypt 1 0 R /Root ", 1)
		result = convert(pdf, ConvertOptions())
		self.assertEqual(result.markdown, "")
		self.assertTrue(result.report["encrypted"])
		self.assertIn("ENCRYPTED_UNSUPPORTED", [warning.code for warning in result.warnings])

	def test_cyclic_page_tree_does_not_recurse(self):
		pdf = (
			b"%PDF-1.4\n"
			b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
			b"2 0 obj << /Type /Pages /Kids [2 0 R] /Count 1 >> endobj\n"
			b"trailer << /Root 1 0 R >>\n%%EOF\n"
		)
		doc = PdfDocument(pdf)
		self.assertEqual(doc.pages(), [])
		self.assertIn("PAGE_TREE_CYCLE", [warning.code for warning in doc.warnings])

	def test_fixture_literals_are_absent_from_renderer(self):
		source = inspect.getsource(MarkdownRenderer)
		for literal in (
			"hard line breaks",
			"nested list inside cell",
			'"checked"',
			'"unchecked"',
			"key[- ]value",
		):
			self.assertNotIn(literal, source)


# ---- xref, filter, font, HTML, and metric correctness ----


def pack_nine_bit_codes(*codes: int) -> bytes:
	bits = "".join(format(code, "09b") for code in codes)
	bits += "0" * ((8 - len(bits) % 8) % 8)
	return int(bits, 2).to_bytes(len(bits) // 8, "big")


class XrefShadowTests(unittest.TestCase):
	def test_new_free_generation_suppresses_old_live_generation(self) -> None:
		class Document:
			def __init__(self) -> None:
				self.warnings = []

			def warn(self, code, detail):
				self.warnings.append((code, detail))

			def _parse_xref_at(self, offset):
				if offset == 200:
					return {(5, 1): "new-free"}, {"Prev": 100}
				return {(5, 0): "old-live"}, {}

		entries, _trailer = walk_xrefs(Document(), 200)
		self.assertEqual(entries, {(5, 1): "new-free"})

	def test_hybrid_stream_wins_by_object_number(self) -> None:
		class Document:
			def warn(self, code, detail):
				pass

			def _parse_xref_at(self, offset):
				if offset == 200:
					return {(8, 0): "classic"}, {"XRefStm": 220}
				return {(8, 2): "compressed"}, {}

		entries, _trailer = walk_xrefs(Document(), 200)
		self.assertEqual(entries, {(8, 2): "compressed"})


class FilterTests(unittest.TestCase):
	def test_lzw_clear_and_eod(self) -> None:
		encoded = pack_nine_bit_codes(256, ord("A"), ord("B"), ord("C"), 257)
		self.assertEqual(lzw_decode(encoded), b"ABC")

	def test_png_sub_uses_bytes_per_pixel(self) -> None:
		encoded = b"\x01" + bytes((10, 20, 30, 1, 2, 3))
		decoded = apply_predictor(encoded, {"Predictor": 15, "Columns": 2, "Colors": 3, "BitsPerComponent": 8})
		self.assertEqual(decoded, bytes((10, 20, 30, 11, 22, 33)))

	def test_truncated_predictor_row_is_rejected(self) -> None:
		with self.assertRaises(ValueError):
			apply_predictor(b"\x00\x01", {"Predictor": 15, "Columns": 3})


class FontTests(unittest.TestCase):
	def test_bfrange_increment_carries_across_bytes(self) -> None:
		cmap = b"1 beginbfrange\n<01> <02> <00FF>\nendbfrange"
		mapping = parse_tounicode(cmap)
		self.assertEqual(mapping[b"\x01"], "\u00ff")
		self.assertEqual(mapping[b"\x02"], "\u0100")

	def test_variable_width_composite_mapping(self) -> None:
		font = SimpleNamespace(
			composite=True,
			to_unicode={b"\x01": "A", b"\x81\x40": "\u4e00"},
			encoding="Custom-H",
			width_for_code=lambda _code: 1000.0,
		)
		self.assertEqual([item[1] for item in decode_font(font, b"\x01\x81\x40")], ["A", "\u4e00"])

	def test_unmapped_cid_does_not_invent_unicode(self) -> None:
		font = SimpleNamespace(
			composite=True,
			to_unicode={},
			encoding="Identity-H",
			width_for_code=lambda _code: 1000.0,
		)
		self.assertEqual(decode_font(font, b"\x00A")[0][1], "")

	def test_algorithmic_glyph_names(self) -> None:
		self.assertEqual(glyph_name_to_unicode("uni00410042.alt"), "AB")
		self.assertEqual(glyph_name_to_unicode("A_B"), "AB")


class HtmlTests(unittest.TestCase):
	def test_duplicate_heading_ids_are_unique(self) -> None:
		html = render_html("# Same\n\n# Same")
		self.assertIn('id="same"', html)
		self.assertIn('id="same-2"', html)

	def test_escaped_table_pipe_stays_in_cell(self) -> None:
		html = render_html("| A | B |\n| --- | --- |\n| x\\|y | z |")
		self.assertIn("<td>x|y</td>", html)

	def test_ordered_list_start_is_preserved(self) -> None:
		self.assertIn('<ol start="3">', render_html("3. three\n4. four"))


class MetricTests(unittest.TestCase):
	def test_character_accuracy(self) -> None:
		self.assertEqual(character_accuracy("abc", "abc"), 1.0)
		self.assertLess(character_accuracy("abc", "axc"), 1.0)


# ---- parser and renderer hardening cases ----


class V2HardeningPatchTests(unittest.TestCase):
	def test_resolve_preserves_falsey_indirect_values(self):
		doc = PdfDocument(make_pdf([b""]))
		doc.objects[(99, 0)] = 0
		self.assertEqual(doc.resolve(Ref(99, 0)), 0)

	def test_text_matrix_scaling_contributes_to_effective_size(self):
		pdf = make_pdf([b"BT /F1 1 Tf 12 0 0 12 72 720 Tm (Scaled) Tj ET"])
		result = convert(pdf, ConvertOptions())
		self.assertIn("Scaled", result.markdown)
		self.assertGreater(result.report["chars"], 0)

	def test_ascii_hex_decoder_keeps_valid_partial_payload(self):
		self.assertEqual(ascii_hex_decode(b"48 65 6c 6c 6f >"), b"Hello")
		self.assertEqual(ascii_hex_decode(b"48zz65>"), b"He")

	def test_tounicode_bfrange_array_destinations(self):
		cmap = b"""
		beginbfrange
		<01> <03> [<0041> <0042> <0043>]
		endbfrange
		"""
		self.assertEqual(parse_tounicode(cmap)[b"\x01"], "A")
		self.assertEqual(parse_tounicode(cmap)[b"\x03"], "C")

	def test_html_renderer_preserves_generated_table_fallback_and_images(self):
		html = render_html("<table>\n<tr><td>A</td></tr>\n</table>\n\n![Alt](assets/img.png)\n")
		self.assertIn("<table>", html)
		self.assertIn('<img src="assets/img.png" alt="Alt" />', html)


if __name__ == "__main__":
	unittest.main()
