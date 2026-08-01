import io
import importlib
import json
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from html.parser import HTMLParser
from pathlib import Path

from cocoapdf import ConvertOptions, convert_file
from cocoapdf._textio import canonical_newlines, write_utf8_lf
from cocoapdf.cli import main as cli_main
from cocoapdf.core import convert
from cocoapdf.synthetic import image_xobject_rgb, line_op, make_pdf, text_op


class _RenderedHtmlAudit(HTMLParser):
	_VOID = {
		"area", "base", "br", "col", "embed", "hr", "img", "input",
		"link", "meta", "param", "source", "track", "wbr",
	}
	_TABLE_PARENTS = {
		"caption": {"table"},
		"thead": {"table"},
		"tbody": {"table"},
		"tfoot": {"table"},
		"tr": {"table", "thead", "tbody", "tfoot"},
		"th": {"tr"},
		"td": {"tr"},
	}
	_P_FORBIDDEN = {
		"article", "aside", "blockquote", "div", "figure", "form", "h1",
		"h2", "h3", "h4", "h5", "h6", "hr", "main", "nav", "ol",
		"p", "pre", "section", "table", "ul",
	}

	def __init__(self):
		super().__init__(convert_charrefs=True)
		self.stack = []
		self.errors = []
		self.ids = set()
		self.duplicate_ids = set()
		self.references = set()
		self.main_count = 0

	def handle_starttag(self, tag, attrs):
		tag = tag.lower()
		parent = self.stack[-1] if self.stack else ""
		attribute_map = {}
		for name, value in attrs:
			name = name.lower()
			if name in attribute_map:
				self.errors.append("duplicate attribute %s on <%s>" % (name, tag))
			attribute_map[name] = value or ""
		identifier = attribute_map.get("id")
		if identifier:
			if identifier in self.ids:
				self.duplicate_ids.add(identifier)
			self.ids.add(identifier)
		for name in ("aria-labelledby", "headers"):
			self.references.update(attribute_map.get(name, "").split())
		href = attribute_map.get("href", "")
		if href.startswith("#") and len(href) > 1:
			self.references.add(href[1:])
		parents = self._TABLE_PARENTS.get(tag)
		if parents is not None and parent not in parents:
			self.errors.append("<%s> cannot be inside <%s>" % (tag, parent))
		if tag == "li" and parent not in {"ol", "ul"}:
			self.errors.append("<li> cannot be inside <%s>" % parent)
		if tag == "a" and "a" in self.stack:
			self.errors.append("nested <a>")
		if "p" in self.stack and tag in self._P_FORBIDDEN:
			self.errors.append("<%s> nested in <p>" % tag)
		if tag == "main":
			self.main_count += 1
		if tag not in self._VOID:
			self.stack.append(tag)

	def handle_startendtag(self, tag, attrs):
		self.handle_starttag(tag, attrs)
		if tag.lower() not in self._VOID and self.stack:
			self.stack.pop()

	def handle_endtag(self, tag):
		tag = tag.lower()
		if tag in self._VOID:
			return
		if not self.stack or self.stack[-1] != tag:
			self.errors.append("misnested closing </%s>" % tag)
			return
		self.stack.pop()


class OutputDeterminismTests(unittest.TestCase):
	def test_utf8_output_bytes_use_lf_on_every_platform(self):
		self.assertEqual(
			canonical_newlines("alpha\r\nbravo\rcharlie\n"),
			"alpha\nbravo\ncharlie\n",
		)
		with tempfile.TemporaryDirectory() as directory:
			output = Path(directory) / "output.txt"
			write_utf8_lf(output, "alpha\r\nbravo\rcharlie\n")
			self.assertEqual(output.read_bytes(), b"alpha\nbravo\ncharlie\n")


class V14FixtureTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.source_path = Path("tests/strategic_corner_cases_v1_4.md")
		cls.pdf_path = Path("tests/strategic_corner_cases_v1_4.pdf")
		if not cls.source_path.exists() or not cls.pdf_path.exists():
			raise unittest.SkipTest("V1_4 strategic fixture files are missing")
		cls.source = cls.source_path.read_text(encoding="utf-8")
		cls.asset_tmp = tempfile.TemporaryDirectory()
		cls.result = convert_file(
			cls.pdf_path,
			ConvertOptions(
				assets_dir=cls.asset_tmp.name,
				asset_reference_dir="assets",
				image_markup="auto",
			),
		)
		cls.markdown = cls.result.markdown
		cls.html = cls.result.html

	@classmethod
	def tearDownClass(cls):
		asset_tmp = getattr(cls, "asset_tmp", None)
		if asset_tmp is not None:
			asset_tmp.cleanup()

	def test_all_source_sentinels_survive_current_text_layer(self):
		sentinels = sorted(set(re.findall(r"SENTINEL-[A-Z0-9-]+", self.source)))
		missing = [sentinel for sentinel in sentinels if sentinel not in self.markdown]
		self.assertEqual(missing, [])

	def test_all_source_sentinels_survive_independent_html_projection(self):
		sentinels = sorted(set(re.findall(r"SENTINEL-[A-Z0-9-]+", self.source)))
		missing = [sentinel for sentinel in sentinels if sentinel not in self.html]
		self.assertEqual(missing, [])
		self.assertTrue(
			self.result.report["semantic_valid"],
			self.result.report["semantic_errors"],
		)

	def test_html_document_is_passive_and_self_contained(self):
		self.assertIn(
			'http-equiv="Content-Security-Policy"',
			self.html,
		)
		self.assertNotRegex(
			self.html,
			r'img-src[^"]*(?:https?:)',
		)
		self.assertFalse(
			re.search(
				r"<\s*(?:script|iframe|object|embed|form)\b",
				self.html,
				re.I,
			)
		)
		self.assertFalse(
			re.search(
				r'(?:href|src)\s*=\s*["\']\s*'
				r'(?:javascript|vbscript|file|data:text)',
				self.html,
				re.I,
			)
		)
		for tag in re.findall(r"<input\b[^>]*>", self.html, re.I):
			self.assertRegex(tag, r"\bdisabled\b")
		for tag in re.findall(r"<img\b[^>]*>", self.html, re.I):
			self.assertRegex(tag, r'\balt="[^"]*"')
			self.assertNotRegex(tag, r'\bsrc="https?://')

	def test_html_document_is_well_nested_and_references_existing_ids(self):
		audit = _RenderedHtmlAudit()
		audit.feed(self.html)
		audit.close()
		self.assertEqual(audit.stack, [])
		self.assertEqual(audit.errors, [])
		self.assertEqual(audit.duplicate_ids, set())
		self.assertEqual(audit.main_count, 1)
		self.assertEqual(audit.references - audit.ids, set())

	def test_no_control_characters_are_emitted(self):
		controls = [
			ord(ch)
			for ch in self.markdown
			if (ord(ch) < 32 and ch not in "\n\r\t") or ord(ch) == 127
		]
		self.assertEqual(controls, [])

	def test_complex_script_actual_text_is_preserved(self):
		self.assertIn(
			"हिन्दी परीक्षण, বাংলা পরীক্ষা, தமிழ் சோதனை, తెలుగు పరీక్ష, ಕನ್ನಡ ಪರೀಕ್ಷೆ",
			self.markdown,
		)
		self.assertIn("👩‍💻 🧑🏽‍🔬 👨‍👩‍👧‍👦 🏳️‍🌈 🇮🇳 🇺🇸", self.markdown)

	def test_rtl_visual_order_is_repaired(self):
		source_line = next(line for line in self.source.splitlines() if "SENTINEL-UNICODE-012" in line)
		output_line = next(line for line in self.markdown.splitlines() if "SENTINEL-UNICODE-012" in line)
		self.assertEqual(output_line.strip(), source_line.strip())

	def test_named_zero_width_controls_keep_local_bracket_order(self):
		source_line = next(line for line in self.source.splitlines() if "SENTINEL-UNICODE-015" in line)
		output_line = next(line for line in self.markdown.splitlines() if "SENTINEL-UNICODE-015" in line)
		self.assertEqual(
			output_line.replace("\\[", "[").replace("\\]", "]"),
			source_line,
		)

	def test_security_warnings_and_region_provenance_are_reported(self):
		warning_codes = [w.code for w in self.result.warnings]
		self.assertGreaterEqual(warning_codes.count("SECURITY_UNSAFE_URI"), 2)
		self.assertTrue(self.result.report["nodes"])
		self.assertTrue(
			any(node.get("type") == "region" and node.get("evidence") for node in self.result.report["nodes"])
		)

	def test_no_separator_column_flow_keeps_left_column_first(self):
		self.assertLess(
			self.markdown.index("PDF physically lays it out in columns."),
			self.markdown.index("Column content charlie contains a link-like visible string"),
		)
		self.assertLess(
			self.markdown.index("Column content bravo contains Greek"),
			self.markdown.index("Column content delta ends the multi-column region."),
		)

	def test_outer_rule_columns_use_safe_html_fallback_and_region_evidence(self):
		start = self.markdown.index('<div class="cocoapdf-columns"')
		end = self.markdown.index("</div>", start)
		block = self.markdown[start:end]
		self.assertIn("columns: 2", block)
		self.assertIn("border-left:", block)
		for text in (
			"SENTINEL-COLUMNS-001 left/right flow:",
			"Column content bravo contains Greek",
			"Column content charlie contains a link-like visible string",
			"Column content delta ends the multi-column region.",
		):
			self.assertIn(text, block)
		self.assertNotIn("> **SENTINEL-COLUMNS-001", self.markdown)
		self.assertIn('<div class="cocoapdf-columns"', self.result.html)
		columns = [region for region in self.result.report["regions"] if region["kind"] == "column"]
		self.assertGreaterEqual(len(columns), 2)
		self.assertTrue(
			any(
				evidence["kind"] == "column_whitespace_gutter"
				for region in columns
				for evidence in region["evidence"]
			)
		)

	def test_printed_form_appearances_use_disabled_html_controls(self):
		self.assertIn(
			'<div class="cocoapdf-form-appearance" data-cocoapdf-kind="printed">',
			self.markdown,
		)
		self.assertIn('<label>Name: <input type="text" value="Cocoa Tester" disabled /></label>', self.markdown)
		self.assertIn('<input type="checkbox" checked disabled /> checked checkbox visible label', self.markdown)
		self.assertIn('<input type="checkbox" disabled /> unchecked checkbox visible label', self.markdown)
		self.assertIn(
			'<select disabled>\n  <option selected>bravo selected</option>\n</select>',
			self.markdown,
		)
		self.assertIn(
			'</label>\n\n<label><input type="checkbox" checked disabled />',
			self.markdown,
		)
		self.assertNotIn("- [x] checked checkbox visible label", self.markdown)
		self.assertIn('<div class="cocoapdf-form-appearance"', self.result.html)
		self.assertIn('<option selected>bravo selected</option>', self.result.html)
		self.assertIn("FORM_APPEARANCE_CONTROLS", {warning.code for warning in self.result.warnings})

	def test_outline_formula_uses_honest_svg_fallback_and_unicode_formula_is_separate(self):
		self.assertRegex(
			self.markdown,
			r"SENTINEL-MATH-002 display formula source:\n\n"
			r'<figure class="cocoapdf-figure cocoapdf-align-center">\n'
			r'<img src="[^"]*formula-[0-9a-f]+\.svg"',
		)
		self.assertIn(
			"SENTINEL-MATH-003 Unicode formula:\n\n"
			"∀x ∈ ℝ, ∃y ∈ ℝ such that y² = x when x ≥ 0.",
			self.markdown,
		)
		formula_assets = {
			name: data for name, data in self.result.assets.items() if name.startswith("formula-")
		}
		self.assertEqual(len(formula_assets), 1)
		self.assertTrue(next(iter(formula_assets.values())).startswith(b'<svg xmlns="http://www.w3.org/2000/svg"'))
		self.assertIn("FORMULA_VECTOR_FALLBACK", {warning.code for warning in self.result.warnings})
		self.assertNotIn(r"\int_0^1", self.markdown)

	def test_markerless_indented_runs_recover_as_unordered_lists(self):
		for item in (
			"- alpha unordered item",
			"- V3-A: object streams and xref streams",
			"- V3-H: decompression bomb guard fixture",
			"- V1/V2 Markdown-first output may preserve complex sections as safe HTML fallback.",
			"- Structural false positives are worse than plain-text fallback.",
		):
			self.assertIn(item, self.markdown)

	def test_heading_levels_rules_and_final_punctuation_are_preserved(self):
		self.assertIn("##### H5 ALL CAPS HEADING\n\n###### H6 Small Heading", self.markdown)
		self.assertIn(
			"SENTINEL-RULE-001 rule follows.\n\n---\n\nSENTINEL-RULE-002",
			self.markdown,
		)
		self.assertIn("SENTINEL-RULE-003:", self.markdown)
		self.assertIn("\n\n---\n\n## 11. Tables", self.markdown)
		final_line = next(line for line in self.markdown.splitlines() if "SENTINEL-FINAL-001" in line)
		self.assertIn("# + - . ! |.", final_line)
		escape_line = next(line for line in self.markdown.splitlines() if "SENTINEL-ESCAPE-001" in line)
		self.assertIn("minus - dot . exclamation ! pipe |", escape_line)

	def test_vector_figure_does_not_consume_adjacent_prose(self):
		self.assertIn("before-icon", self.markdown)
		self.assertNotIn("\n\n`w`\n\n", self.markdown)
		self.assertNotIn('<p align="right">`w`</p>', self.markdown)
		self.assertTrue(
			all(b"before-icon" not in data for name, data in self.result.assets.items() if name.endswith(".svg"))
		)
		self.assertRegex(self.markdown, r'src="[^"]*vector-[0-9a-f]+\.svg"')
		self.assertIn("width: 540.000pt; height: 180.000pt", self.markdown)
		self.assertIn("margin-left: auto; margin-right: auto;", self.markdown)
		self.assertNotIn("data:image/svg+xml", self.markdown)

	def test_raster_only_fixture_is_preserved_as_an_opaque_image(self):
		images = self.result.report["images_detail"]
		rasters = [image for image in images if image["kind"] == "raster"]
		vectors = [image for image in images if image["kind"] == "vector"]
		self.assertEqual(len(rasters), 1)
		self.assertGreaterEqual(len(vectors), 1)
		self.assertRegex(self.markdown, r'<img src=".+/img-[0-9a-f]+\.png"')
		self.assertIn("14. Raster Image Preservation Fixture", self.markdown)
		self.assertIn("SENTINEL-RASTER-001", self.markdown)
		self.assertIn("Raster SENTINEL: Raster text = 12345", self.source)
		self.assertIn("Raster SENTINEL: Raster text = 12345", self.markdown)
		for obsolete in ("OCR-ONLY", "SENTINEL-OCR", "Raster Hybrid Future"):
			self.assertNotIn(obsolete, self.source)
			self.assertNotIn(obsolete, self.markdown)
			self.assertNotIn(obsolete, self.result.html)
		self.assertFalse(self.result.report["image_text_extraction_attempted"])
		self.assertFalse(self.result.report["ocr_used"])
		raster_nodes = [node for node in self.result.semantic.walk() if node.kind == "image" and node.attrs.get("kind") == "raster"]
		self.assertEqual(len(raster_nodes), 1)
		self.assertEqual(raster_nodes[0].text, "")
		self.assertEqual(raster_nodes[0].children, [])
		self.assertTrue(raster_nodes[0].sources)
		self.assertTrue(all(not source.glyph_ids for source in raster_nodes[0].sources))
		self.assertFalse(raster_nodes[0].attrs.get("text_extraction_attempted"))
		raster_asset_names = [
			name for name in self.result.assets
			if name.startswith("img-") and name.endswith(".png")
		]
		self.assertEqual(len(raster_asset_names), 1)
		asset_name = raster_asset_names[0]
		asset_source = raster_nodes[0].attrs["src"]
		self.assertTrue(asset_source.replace("\\", "/").endswith("/" + asset_name))
		self.assertIn('src="%s"' % asset_source, self.result.html)
		self.assertEqual(self.result.html.count('src="%s"' % asset_source), 1)
		self.assertTrue(self.result.assets[asset_name].startswith(b"\x89PNG\r\n\x1a\n"))
		self.assertEqual(
			self.result.report["output_derivation"]["html"],
			"semantic_graph",
		)
		self.assertIn(
			"VECTOR_FIGURE_APPROXIMATE",
			{warning.code for warning in self.result.warnings},
		)

	def test_inline_styles_are_minimal_coalesced_and_code_safe(self):
		for sentinel in ("SENTINEL-INLINE-%03d" % number for number in range(1, 7)):
			source_line = next(line for line in self.source.splitlines() if sentinel in line)
			output_line = next(line for line in self.markdown.splitlines() if sentinel in line)
			self.assertEqual(output_line, source_line)
		self.assertNotIn("## <u>**", self.markdown)
		self.assertNotIn("<mark>highlight</mark> <mark>via</mark>", self.markdown)
		self.assertNotIn(r"`inline\_code()`", self.markdown)
		self.assertNotIn(r"`a|b\*c\_d\[0\]", self.markdown)
		self.assertIn(
			"SENTINEL-TEXT-004: Hard line breaks follow:  \n"
			"first hard line  \nsecond hard line  \nthird hard line",
			self.markdown,
		)

	def test_lists_and_quotes_preserve_markers_depth_and_block_grouping(self):
		for fragment in (
			"a. letter item source\nb. second letter item source\niv. roman item source\nv. second roman item source",
			"1. parent ordered alpha\n   - child unordered alpha\n   - child unordered bravo\n     1. grandchild ordered one",
			"- [x] checked task item\n- [ ] unchecked task item\n- [x] uppercase checked task item",
			"> - quoted bullet alpha\n> - quoted bullet bravo",
			"> > nested level two\n>\n> > > nested level three",
			"> ```\n> def quoted_code(x):\n>  return x + 1\n> ```",
		):
			self.assertIn(fragment, self.markdown)
		self.assertIn("• Singleton bullet-looking prose line.", self.markdown)
		self.assertNotIn("- This is dialogue with an em dash.", self.markdown)
		quote_html = self.result.html[
			self.result.html.index("SENTINEL-QUOTE-001"):
			self.result.html.index("9. Code Blocks")
		]
		self.assertRegex(
			quote_html,
			r"(?s)<ul\b.*quoted bullet alpha.*</ul>.*"
			r"<ol\b.*quoted ordered alpha.*</ol>",
		)
		self.assertGreaterEqual(quote_html.count("<blockquote"), 3)
		self.assertIn("<pre", quote_html)
		self.assertIn("<code>def quoted_code(x):", quote_html)
		marker_html = self.result.html[
			self.result.html.index("SENTINEL-LIST-004"):
			self.result.html.index("SENTINEL-LIST-005")
		]
		self.assertIn('<ol type="a"', marker_html)
		self.assertIn('<ol type="i" start="4"', marker_html)
		self.assertEqual(marker_html.count('<ol type="i"'), 1)
		self.assertIn("second roman item source</li>", marker_html)

	def test_complex_tables_and_formula_matrix_remain_structural(self):
		for fragment in (
			"<caption>Complex semantic table with spans, nested content, and code</caption>",
			'<th rowspan="2">Component</th><th colspan="2">Evidence</th><th rowspan="2">Required output</th>',
			'<td colspan="2"><ul><li>nested list inside cell</li><li>inline <code>code()</code> inside cell</li>',
			'<td rowspan="2">shared geometry evidence</td>',
			"| `A = [[1, 2], [3, 4]]` | matrix literal |",
			"| `det(A) = -2` | determinant |",
		):
			self.assertIn(fragment, self.markdown)
		self.assertNotIn("font size, ga</td>", self.markdown)
		self.assertNotIn("shared geo</td>", self.markdown)

	def test_loss_aware_regions_forms_assets_and_appendix_are_complete(self):
		self.assertIn('<div class="cocoapdf-columns" style="columns: 2;', self.markdown)
		self.assertIn("<p><strong>SENTINEL-COLUMNS-001 left/right flow:</strong>", self.markdown)
		self.assertIn('<div style="border: 1px solid #9bb7d3;', self.markdown)
		self.assertIn('<div class="cocoapdf-form-appearance"', self.markdown)
		self.assertIn("- V1/V2 Markdown-first output may preserve complex sections", self.markdown)
		self.assertIn("- Structural false positives are worse than plain-text fallback.", self.markdown)
		self.assertTrue(all("assets/" not in name for name in self.result.assets))
		self.assertFalse(re.search(r'\]\((?:javascript:|file:|data:text/html)', self.markdown, re.I))

	def test_region_report_does_not_relabel_code_tables_or_vectors_as_callouts(self):
		regions = self.result.report["regions"]
		callouts = [region for region in regions if region["kind"] == "callout"]
		tables = [region for region in regions if region["kind"] == "table"]
		self.assertEqual(len(callouts), 1)
		self.assertEqual(len(tables), 6)
		self.assertEqual(callouts[0]["evidence"][0]["kind"], "renderer_callout_block")
		self.assertTrue(all(region["evidence"][0]["kind"] == "renderer_table_block" for region in tables))

	def test_pdf_linked_footnotes_use_native_markdown_syntax(self):
		self.assertIn(
			"SENTINEL-FOOTNOTE-001: This sentence has a numeric footnote. [^1] "
			"This sentence has another note with Unicode. [^2]",
			self.markdown,
		)
		self.assertIn("SENTINEL-FOOTNOTE-002: Multiple references to one definition may occur. [^1]", self.markdown)
		self.assertIn(
			"[^1]: Footnote definition one. It includes a URL <https://example.com/footnote>, "
			"punctuation, and continuation text.",
			self.markdown,
		)
		self.assertIn("[^2]: Footnote with Greek αβγ, emoji ☕, and CJK 中文.", self.markdown)
		self.assertNotIn('id="ref-footnote-', self.markdown)
		self.assertNotIn('id="dfref-footnote-', self.markdown)
		self.assertEqual(
			[(node.kind, node.attrs.get("label")) for node in self.result.semantic.walk() if node.kind == "footnote_ref"],
			[("footnote_ref", "1"), ("footnote_ref", "2"), ("footnote_ref", "1")],
		)

	def test_reference_typing_does_not_flatten_inline_style_or_links(self):
		self.assertIn(
			'<a id="ref-2"></a>[2] Smith, John and Roe, Alex. *Tables Without Borders*. '
			'Example Press, 2025. <https://example.com/references/2>.',
			self.markdown,
		)
		self.assertNotIn("[<u>https://example.com/references/2</u>]", self.markdown)


# ---- asset extraction and CLI integration contracts ----


ROOT = Path(__file__).resolve().parents[1]


class ArchitectureAndImageTests(unittest.TestCase):
	def test_retired_package_and_runtime_patch_files_are_absent(self):
		retired = "pdf" + "2md"
		self.assertFalse((ROOT / ("run_" + retired + ".py")).exists())
		self.assertFalse((ROOT / "src" / retired).exists())
		self.assertFalse((ROOT / "src" / "cocoapdf" / "hardening.py").exists())

	def test_raster_image_auto_markup_preserves_pdf_size_and_alignment(self):
		pdf = make_pdf(
			[b"q 120 0 0 60 246 500 cm /Im1 Do Q"],
			xobjects={
				"Im1": image_xobject_rgb(
					2,
					1,
					b"\xff\x00\x00\x00\x00\xff",
				)
			},
		)
		result = convert(
			pdf,
			ConvertOptions(assets_dir="assets", image_markup="auto"),
		)
		self.assertIn(
			'class="cocoapdf-figure cocoapdf-align-center"',
			result.markdown,
		)
		self.assertIn("width: 120.000pt", result.markdown)
		self.assertIn("height: 60.000pt", result.markdown)
		self.assertIn("margin-left: auto; margin-right: auto;", result.markdown)
		self.assertIn(
			'class="cocoapdf-figure cocoapdf-align-center"',
			result.html,
		)
		self.assertRegex(result.html, r'<img src="assets/img-[0-9a-f]+\.png"')
		self.assertIn("width: 120.000pt", result.html)
		self.assertIn("height: 60.000pt", result.html)
		self.assertEqual(len(result.assets), 1)
		self.assertEqual(result.report["images_detail"][0]["placed_width"], 120.0)

	def test_cli_asset_references_are_relative_to_output_document(self):
		pdf = make_pdf(
			[b"q 120 0 0 60 246 500 cm /Im1 Do Q"],
			xobjects={"Im1": image_xobject_rgb(1, 1, b"\xff\x00\x00")},
		)
		with tempfile.TemporaryDirectory() as tmp:
			root = Path(tmp)
			pdf_path = root / "fixture.pdf"
			output_path = root / "fixture.md"
			assets_path = root / "assets"
			pdf_path.write_bytes(pdf)
			self.assertEqual(
				cli_main(
					[
						str(pdf_path),
						"-o",
						str(output_path),
						"--assets",
						str(assets_path),
					]
				),
				0,
			)
			markdown = output_path.read_text(encoding="utf-8")
			self.assertRegex(
				markdown,
				r"!\[[^\]]*\]\(assets/img-[0-9a-f]+\.png\)",
			)
			self.assertNotIn(str(assets_path).replace("\\", "/"), markdown)
			self.assertTrue(any(assets_path.glob("img-*.png")))

	def test_wrapped_caption_is_paired_without_dropping_continuation(self):
		stream = b"\n".join(
			[
				b"q 120 0 0 60 246 500 cm /Im1 Do Q",
				text_op(246, 475, "Figure 1 caption: complete or", size=10),
				text_op(246, 460, "continued caption.", size=10),
			]
		)
		result = convert(
			make_pdf(
				[stream],
				xobjects={"Im1": image_xobject_rgb(1, 1, b"\xff\x00\x00")},
			),
			ConvertOptions(image_markup="auto"),
		)
		caption = "Figure 1 caption: complete or continued caption."
		self.assertIn("<figcaption>%s</figcaption>" % caption, result.markdown)
		self.assertEqual(result.report["images_detail"][0]["alt"], caption)
		self.assertEqual(result.markdown.count("continued caption."), 2)

	def test_vector_canvas_is_asset_not_callout_or_rules(self):
		stream = b"\n".join(
			[
				b"q 156 400 300 120 re W n",
				b"1 .96 .87 rg 157 401 298 118 re f",
				b"0 0 1 RG 3 w",
				b"196 435 m 416 435 l S",
				b"196 465 m 396 465 l S",
				b"196 495 m 436 495 l S",
				text_op(181, 450, "Vector figure text", size=16),
				b"Q",
			]
		)
		result = convert(
			make_pdf([stream]),
			ConvertOptions(assets_dir="assets", image_markup="auto"),
		)
		self.assertTrue(any(name.endswith(".svg") for name in result.assets))
		self.assertIn("vector-", result.markdown)
		self.assertIn("width: 300.000pt; height: 120.000pt", result.markdown)
		self.assertIn("margin-left: auto; margin-right: auto;", result.markdown)
		self.assertNotIn('<div style="border: 1px solid', result.markdown)
		self.assertIn(
			"VECTOR_FIGURE_APPROXIMATE",
			{warning.code for warning in result.warnings},
		)
		self.assertIn(
			"vector",
			{image["kind"] for image in result.report["images_detail"]},
		)
		vector = next(data for name, data in result.assets.items() if name.startswith("vector-"))
		self.assertIn(b'width="300.000pt" height="120.000pt"', vector)

	def test_vector_capture_preserves_text_outside_artwork_on_shared_baseline(self):
		stream = b"\n".join(
			[
				b"1 .96 .87 rg 100 400 300 120 re f",
				b"0 0 1 RG 3 w",
				b"140 435 m 360 435 l S",
				b"140 465 m 340 465 l S",
				b"140 495 m 380 495 l S",
				b"0 0 0 rg",
				text_op(30, 450, "before-icon", size=10),
				text_op(140, 450, "Vector figure text", size=10),
			]
		)
		result = convert(make_pdf([stream]), ConvertOptions(image_markup="auto"))
		self.assertIn("before-icon", result.markdown)
		vector = next(data for name, data in result.assets.items() if name.endswith(".svg"))
		self.assertNotIn(b"before-icon", vector)
		self.assertIn(b"Vector figure text", vector)

	def test_connected_flowchart_is_one_complete_vector_figure(self):
		stream = b"\n".join(
			[
				text_op(80, 710, "System Architecture", "F2", 16),
				b"1 1 .87 rg 80 280 450 400 re f",
				b"1 1 .87 rg 190 170 230 80 re f",
				b".93 .93 1 rg 120 610 130 40 re f",
				b".93 .93 1 rg 360 610 130 40 re f",
				b".93 .93 1 rg 120 500 130 40 re f",
				b".93 .93 1 rg 360 500 130 40 re f",
				b".93 .93 1 rg 240 380 130 40 re f",
				b".93 .93 1 rg 230 190 150 40 re f",
				b"0 0 0 RG 1 w",
				line_op(185, 610, 185, 545, 1),
				line_op(425, 610, 425, 545, 1),
				line_op(185, 500, 280, 425, 1),
				line_op(425, 500, 330, 425, 1),
				line_op(305, 380, 305, 280, 1),
				line_op(305, 280, 305, 250, 1),
				b".2 .2 .2 rg 180 555 m 190 555 l 185 545 l 180 555 l f",
				b".2 .2 .2 rg 420 555 m 430 555 l 425 545 l 420 555 l f",
				b".2 .2 .2 rg 300 290 m 310 290 l 305 280 l 300 290 l f",
				text_op(145, 625, "Node Alpha", "F1", 10),
				text_op(385, 625, "Node Bravo", "F1", 10),
				text_op(145, 515, "Node Charlie", "F1", 10),
				text_op(385, 515, "Node Delta", "F1", 10),
				text_op(265, 395, "Validated merge", "F1", 10),
				text_op(250, 205, "Existing controller", "F1", 10),
			]
		)
		result = convert(
			make_pdf([stream]),
			ConvertOptions(assets_dir="assets", image_markup="auto"),
		)
		vectors = [
			(name, data)
			for name, data in result.assets.items()
			if name.startswith("vector-")
		]
		self.assertEqual(len(vectors), 1)
		self.assertEqual(result.markdown.count("<img "), 1)
		self.assertIn("System Architecture", result.markdown)
		self.assertNotRegex(result.markdown, r"(?m)^Node Alpha$")
		self.assertNotRegex(result.markdown, r"(?m)^Existing controller$")
		self.assertIn(b"Node Alpha", vectors[0][1])
		self.assertIn(b"Existing controller", vectors[0][1])
		self.assertGreaterEqual(vectors[0][1].count(b"<path "), 3)
		image = result.report["images_detail"][0]
		self.assertGreater(image["placed_height"], 500.0)
		self.assertIn("Node Alpha", image["alt"])
		self.assertIn("Existing controller", image["alt"])
		self.assertFalse(result.report["image_text_extraction_attempted"])
		self.assertFalse(result.report["ocr_used"])
		image_node = next(
			node
			for node in result.semantic.walk()
			if node.kind == "image" and node.attrs.get("kind") == "vector"
		)
		self.assertTrue(image_node.sources[0].glyph_ids)
		self.assertEqual(image_node.evidence[0].kind, "pdf_vector_artwork")

	def test_staggered_labeled_outline_panels_remain_one_vector_figure(self):
		def outline(x0, y0, x1, y1):
			return [
				line_op(x0, y0, x1, y0, 1),
				line_op(x1, y0, x1, y1, 1),
				line_op(x1, y1, x0, y1, 1),
				line_op(x0, y1, x0, y0, 1),
			]

		parts = [text_op(72, 510, "Diagram sequence", "F2", 11)]
		labels = ("Parse", "Decode", "Layout", "Reconcile", "Render")
		for index, (x0, label) in enumerate(
			zip((45.0, 155.0, 265.0, 375.0, 485.0), labels)
		):
			y0 = 455.0 - (index % 2) * 34.0
			parts.extend(outline(x0, y0, x0 + 82.0, y0 + 24.0))
			parts.append(text_op(x0 + 8.0, y0 + 8.0, label, "F1", 8))
		result = convert(
			make_pdf([b"\n".join(parts)]),
			ConvertOptions(assets_dir="assets", image_markup="auto"),
		)
		vectors = [
			(name, data)
			for name, data in result.assets.items()
			if name.startswith("vector-")
		]
		self.assertEqual(len(vectors), 1)
		self.assertEqual(result.markdown.count("<img "), 1)
		self.assertNotRegex(result.markdown, r"(?m)^Parse Layout Render$")
		self.assertNotRegex(result.markdown, r"(?m)^Decode Reconcile$")
		for label in labels:
			self.assertIn(label.encode("ascii"), vectors[0][1])
		self.assertGreaterEqual(vectors[0][1].count(b"<line "), 16)
		image = result.report["images_detail"][0]
		self.assertEqual(image["kind"], "vector")
		self.assertGreater(image["placed_width"], 450.0)
		self.assertGreater(image["placed_height"], 40.0)
		self.assertIn("Parse", image["alt"])
		image_node = next(
			node
			for node in result.semantic.walk()
			if node.kind == "image" and node.attrs.get("kind") == "vector"
		)
		self.assertGreaterEqual(
			len(image_node.sources[0].glyph_ids),
			sum(len(label) for label in labels),
		)
		self.assertNotIn(
			"FORM_APPEARANCE_CONTROLS",
			{warning.code for warning in result.warnings},
		)

	def test_aligned_labeled_outline_fields_are_not_vectorized(self):
		def outline(x0, y0, x1, y1):
			return [
				line_op(x0, y0, x1, y0, 1),
				line_op(x1, y0, x1, y1, 1),
				line_op(x1, y1, x0, y1, 1),
				line_op(x0, y1, x0, y0, 1),
			]

		parts = []
		for index in range(5):
			y0 = 680.0 - index * 48.0
			parts.extend(outline(220.0, y0, 410.0, y0 + 26.0))
			parts.append(text_op(72, y0 + 8.0, "Field %d:" % (index + 1), "F1", 9))
			parts.append(text_op(230, y0 + 8.0, "Value %d" % (index + 1), "F1", 9))
		result = convert(
			make_pdf([b"\n".join(parts)]),
			ConvertOptions(assets_dir="assets", image_markup="auto"),
		)
		self.assertFalse(
			any(name.startswith("vector-") for name in result.assets)
		)
		self.assertIn("Field 1:", result.markdown)
		self.assertIn("Value 5", result.markdown)

	def test_table_lattice_is_not_reclassified_as_outline_artwork(self):
		parts = []
		for y in (560.0, 610.0, 660.0, 710.0):
			parts.append(line_op(120.0, y, 480.0, y, 1))
		for x in (120.0, 240.0, 360.0, 480.0):
			parts.append(line_op(x, 560.0, x, 710.0, 1))
		for row, y in enumerate((675.0, 625.0, 575.0)):
			for column, x in enumerate((135.0, 255.0, 375.0)):
				parts.append(
					text_op(x, y, "R%dC%d" % (row + 1, column + 1), "F1", 9)
				)
		result = convert(
			make_pdf([b"\n".join(parts)]),
			ConvertOptions(assets_dir="assets", image_markup="auto"),
		)
		self.assertFalse(
			any(name.startswith("vector-") for name in result.assets)
		)
		self.assertIn("<table>", result.markdown)
		for row in range(1, 4):
			for column in range(1, 4):
				self.assertIn("R%dC%d" % (row, column), result.markdown)

	def test_outline_only_display_formula_is_preserved_as_external_svg(self):
		curve = (
			b"%d 500 m %d 520 %d 520 %d 510 c "
			b"%d 500 %d 500 %d 510 c "
			b"%d 516 %d 516 %d 500 c h f"
		)
		paths = []
		for x in (240, 254, 268):
			paths.append(
				curve
				% (
					x,
					x + 2,
					x + 8,
					x + 10,
					x + 8,
					x + 2,
					x,
					x + 2,
					x + 8,
					x + 10,
				)
			)
		stream = b"\n".join(
			[text_op(72, 560, "Display formula:", size=12), b"0 0 0 rg"]
			+ paths
		)
		result = convert(
			make_pdf([stream]),
			ConvertOptions(assets_dir="assets", image_markup="auto"),
		)
		formula = next(
			(name, data)
			for name, data in result.assets.items()
			if name.startswith("formula-")
		)
		self.assertRegex(result.markdown, r'src="assets/formula-[0-9a-f]+\.svg"')
		self.assertIn(b"<path ", formula[1])
		self.assertIn(
			"FORMULA_VECTOR_FALLBACK",
			{warning.code for warning in result.warnings},
		)


class HeadingAndTableBoundaryTests(unittest.TestCase):
	def test_large_bold_titles_before_tables_remain_headings(self):
		parts = [
			text_op(72, 760, "Body text establishes the document font size.", "F1", 10),
			text_op(280, 720, "Pilot software", "F2", 15),
		]
		for y in (700, 660, 620):
			parts.append(line_op(180, y, 480, y, 1))
		for x in (180, 330, 480):
			parts.append(line_op(x, 620, x, 700, 1))
		parts.extend(
			[
				text_op(195, 675, "Component", "F2", 10),
				text_op(345, 675, "Choice", "F2", 10),
				text_op(195, 635, "Runtime", "F1", 10),
				text_op(345, 635, "Local", "F1", 10),
				text_op(72, 590, "Intervening explanatory body text.", "F1", 10),
				text_op(235, 550, "Language-model comparison", "F2", 15),
			]
		)
		for y in (530, 490, 450):
			parts.append(line_op(180, y, 480, y, 1))
		for x in (180, 330, 480):
			parts.append(line_op(x, 450, x, 530, 1))
		parts.extend(
			[
				text_op(195, 505, "Candidate", "F2", 10),
				text_op(345, 505, "Role", "F2", 10),
				text_op(195, 465, "Compact", "F1", 10),
				text_op(345, 465, "Pilot", "F1", 10),
			]
		)
		result = convert(make_pdf([b"\n".join(parts)]), ConvertOptions())
		markdown = result.markdown
		self.assertIn("### Pilot software", markdown)
		self.assertIn("### Language-model comparison", markdown)
		self.assertNotIn('<p align="center">Pilot software</p>', markdown)
		self.assertNotIn('<p align="center">Language-model comparison</p>', markdown)
		self.assertEqual(markdown.count("| Component | Choice |"), 1)
		self.assertEqual(markdown.count("| Candidate | Role |"), 1)
		self.assertRegex(
			result.html,
			r'<h3 id="pilot-software"[^>]*>Pilot software</h3>',
		)
		self.assertRegex(
			result.html,
			r'<h3 id="language-model-comparison"[^>]*>'
			r'Language-model comparison</h3>',
		)

	def test_explicit_table_label_remains_a_caption_even_when_bold(self):
		parts = [text_op(278, 720, "Table 1. Results", "F2", 15)]
		for y in (700, 660, 620):
			parts.append(line_op(180, y, 480, y, 1))
		for x in (180, 330, 480):
			parts.append(line_op(x, 620, x, 700, 1))
		parts.extend(
			[
				text_op(195, 675, "Metric", "F2", 10),
				text_op(345, 675, "Value", "F2", 10),
				text_op(195, 635, "Accuracy", "F1", 10),
				text_op(345, 635, "High", "F1", 10),
			]
		)
		markdown = convert(make_pdf([b"\n".join(parts)]), ConvertOptions()).markdown
		self.assertIn('<p align="center">Table 1. Results</p>', markdown)
		self.assertNotIn("### Table 1. Results", markdown)


class CliSurfaceContractTests(unittest.TestCase):
	def test_help_version_and_confidence_validation(self):
		stdout = io.StringIO()
		with redirect_stdout(stdout):
			with self.assertRaises(SystemExit) as raised:
				cli_main(["--help"])
		self.assertEqual(raised.exception.code, 0)
		help_text = stdout.getvalue()
		for option in (
			"--version",
			"--output",
			"--assets",
			"--html-underline",
			"--no-html-underline",
			"--page-breaks",
			"--pages",
			"--image-mode",
			"--image-markup",
			"--report",
			"--format",
			"--explain",
			"--min-confidence",
			"--show-low-confidence",
		):
			self.assertIn(option, help_text)
		self.assertIn("Structured text-layer PDFs only. No OCR. No AI.", help_text)

		stdout = io.StringIO()
		with redirect_stdout(stdout):
			with self.assertRaises(SystemExit) as raised:
				cli_main(["--version"])
		self.assertEqual(raised.exception.code, 0)
		self.assertRegex(stdout.getvalue(), r"^cocoapdf \d")

		stderr = io.StringIO()
		with redirect_stderr(stderr):
			with self.assertRaises(SystemExit) as raised:
				cli_main(["missing.pdf", "--min-confidence", "1.01"])
		self.assertEqual(raised.exception.code, 2)
		self.assertIn("confidence must be between 0 and 1", stderr.getvalue())

		stderr = io.StringIO()
		with redirect_stderr(stderr):
			with self.assertRaises(SystemExit) as raised:
				cli_main(["missing.pdf", "--ocr"])
		self.assertEqual(raised.exception.code, 2)
		self.assertIn("unrecognized arguments: --ocr", stderr.getvalue())

	def test_output_formats_image_modes_and_markup_choices(self):
		pdf = make_pdf(
			[
				b"\n".join(
					[
						text_op(72, 720, "Image contract", "F2", 14),
						b"q 120 0 0 60 246 500 cm /Im1 Do Q",
					]
				)
			],
			xobjects={"Im1": image_xobject_rgb(1, 1, b"\xff\x00\x00")},
		)
		with tempfile.TemporaryDirectory() as tmp:
			root = Path(tmp)
			pdf_path = root / "fixture.pdf"
			pdf_path.write_bytes(pdf)

			stdout = io.StringIO()
			with redirect_stdout(stdout):
				self.assertEqual(
					cli_main(
						[
							str(pdf_path),
							"--image-mode",
							"embed",
						]
					),
					0,
				)
			self.assertIn("Image contract", stdout.getvalue())
			self.assertIn("data:image/png;base64,", stdout.getvalue())

			md_output = root / "markdown" / "document.md"
			md_assets = root / "markdown" / "assets"
			self.assertEqual(
				cli_main(
					[
						str(pdf_path),
						"--output",
						str(md_output),
						"--assets",
						str(md_assets),
						"--format",
						"md",
						"--image-mode",
						"reference",
						"--image-markup",
						"markdown",
					]
				),
				0,
			)
			self.assertRegex(
				md_output.read_text(encoding="utf-8"),
				r"!\[[^\]]*\]\(assets/img-[0-9a-f]+\.png\)",
			)
			self.assertTrue(any(md_assets.glob("img-*.png")))

			html_output = root / "html" / "document.html"
			html_assets = root / "html" / "assets"
			self.assertEqual(
				cli_main(
					[
						str(pdf_path),
						"-o",
						str(html_output),
						"--assets",
						str(html_assets),
						"--format",
						"html",
						"--image-markup",
						"html",
					]
				),
				0,
			)
			html_output_text = html_output.read_text(encoding="utf-8")
			self.assertIn("<!doctype html>", html_output_text)
			self.assertRegex(html_output_text, r'<img src="assets/img-[0-9a-f]+\.png"')

			json_output = root / "json" / "document.json"
			self.assertEqual(
				cli_main(
					[
						str(pdf_path),
						"-o",
						str(json_output),
						"--format",
						"json",
						"--assets",
						str(root / "json" / "assets"),
					]
				),
				0,
			)
			payload = json.loads(json_output.read_text(encoding="utf-8"))
			self.assertEqual(
				set(payload),
				{"semantic_document", "report", "markdown", "html"},
			)

			both_output = root / "both"
			self.assertEqual(
				cli_main(
					[
						str(pdf_path),
						"-o",
						str(both_output),
						"--assets",
						str(root / "both-assets"),
						"--format",
						"both",
					]
				),
				0,
			)
			for name in ("document.md", "document.html", "document.json", "report.json"):
				self.assertTrue((both_output / name).is_file(), name)
			# --image-markup defaults to native Markdown images, so the Markdown
			# projection of --format both stays plain Markdown.
			both_markdown = (both_output / "document.md").read_text(encoding="utf-8")
			self.assertRegex(both_markdown, r"!\[[^\]]*\]\([^)]*img-[0-9a-f]+\.png\)")
			self.assertNotIn('class="cocoapdf-figure', both_markdown)

			figure_output = root / "both-figure"
			self.assertEqual(
				cli_main(
					[
						str(pdf_path),
						"-o",
						str(figure_output),
						"--assets",
						str(root / "both-figure-assets"),
						"--format",
						"both",
						"--image-markup",
						"html",
					]
				),
				0,
			)
			# Requesting HTML image markup preserves the image's intrinsic size
			# and alignment, which Markdown cannot express.
			self.assertIn(
				'class="cocoapdf-figure',
				(figure_output / "document.md").read_text(encoding="utf-8"),
			)

			embed_output = root / "embed" / "document.md"
			embed_assets = root / "embed-assets"
			self.assertEqual(
				cli_main(
					[
						str(pdf_path),
						"-o",
						str(embed_output),
						"--assets",
						str(embed_assets),
						"--image-mode",
						"embed",
						"--image-markup",
						"html",
					]
				),
				0,
			)
			self.assertIn(
				"data:image/png;base64,",
				embed_output.read_text(encoding="utf-8"),
			)
			self.assertFalse(embed_assets.exists())

	def test_page_underline_report_and_explainability_options(self):
		pdf = make_pdf(
			[
				b"\n".join(
					[
						text_op(72, 720, "Underlined text", "F1", 10),
						line_op(72, 718, 145, 718, 0.8),
					]
				),
				text_op(72, 720, "Page two", "F1", 10),
			]
		)
		with tempfile.TemporaryDirectory() as tmp:
			root = Path(tmp)
			pdf_path = root / "fixture.pdf"
			output = root / "with-underline.md"
			report = root / "diagnostics" / "report.json"
			pdf_path.write_bytes(pdf)
			stderr = io.StringIO()
			with redirect_stderr(stderr):
				self.assertEqual(
					cli_main(
						[
							str(pdf_path),
							"-o",
							str(output),
							"--html-underline",
							"--page-breaks",
							"--pages",
							"1-2",
							"--report",
							str(report),
							"--explain",
							"--min-confidence",
							"1",
							"--show-low-confidence",
						]
					),
					0,
				)
			markdown = output.read_text(encoding="utf-8")
			self.assertIn("<u>Underlined text</u>", markdown)
			self.assertIn("<!-- page 2 -->", markdown)
			self.assertIn("Page two", markdown)
			self.assertTrue(report.is_file())
			self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["processed_pages"], [1, 2])
			self.assertIn("CocoaPDF", stderr.getvalue())
			self.assertIn('"low_confidence"', stderr.getvalue())

			plain_output = root / "without-underline.md"
			self.assertEqual(
				cli_main(
					[
						str(pdf_path),
						"-o",
						str(plain_output),
						"--no-html-underline",
						"--pages",
						"1",
					]
				),
				0,
			)
			plain_markdown = plain_output.read_text(encoding="utf-8")
			self.assertIn("Underlined text", plain_markdown)
			self.assertNotIn("<u>", plain_markdown)
			self.assertNotIn("Page two", plain_markdown)


# ---- public package architecture contracts ----


class PackageArchitectureTests(unittest.TestCase):
	def test_cocoapdf_is_canonical_public_api(self):
		import cocoapdf

		self.assertEqual(cocoapdf.__project__, "CocoaPDF")
		self.assertTrue(hasattr(cocoapdf, "convert"))
		self.assertTrue(hasattr(cocoapdf, "convert_file"))

	def test_canonical_deep_imports_resolve(self):
		canonical_regions = importlib.import_module("cocoapdf.layout.regions")
		canonical_html = importlib.import_module("cocoapdf.html.render")

		self.assertTrue(hasattr(canonical_regions, "detect_regions"))
		self.assertTrue(hasattr(canonical_html, "render_html"))

	def test_cocoapdf_module_entrypoint_uses_canonical_cli(self):
		canonical_cli = importlib.import_module("cocoapdf.cli")
		module_entrypoint = importlib.import_module("cocoapdf.__main__")
		self.assertIs(module_entrypoint.main, canonical_cli.main)

	def test_canonical_sources_use_only_canonical_namespace(self):
		import pathlib

		root = pathlib.Path("src/cocoapdf")
		offenders = []
		for path in root.rglob("*.py"):
			text = path.read_text(encoding="utf-8")
			if "from " + "pdf" + "2md" in text or "import " + "pdf" + "2md" in text:
				offenders.append(str(path))
		self.assertEqual(offenders, [])


if __name__ == "__main__":
	unittest.main()
