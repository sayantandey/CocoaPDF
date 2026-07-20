import importlib
import re
import tempfile
import unittest
from pathlib import Path

from cocoapdf import ConvertOptions, convert_file
from cocoapdf.cli import main as cli_main
from cocoapdf.core import convert
from cocoapdf.synthetic import image_xobject_rgb, make_pdf, text_op


class V14FixtureTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.source_path = Path("tests/strategic_corner_cases_v1_4.md")
		cls.pdf_path = Path("tests/strategic_corner_cases_v1_4.pdf")
		if not cls.source_path.exists() or not cls.pdf_path.exists():
			raise unittest.SkipTest("V1_4 strategic fixture files are missing")
		cls.source = cls.source_path.read_text(encoding="utf-8")
		cls.asset_tmp = tempfile.TemporaryDirectory()
		cls.result = convert_file(cls.pdf_path, ConvertOptions(assets_dir=cls.asset_tmp.name))
		cls.markdown = cls.result.markdown

	@classmethod
	def tearDownClass(cls):
		asset_tmp = getattr(cls, "asset_tmp", None)
		if asset_tmp is not None:
			asset_tmp.cleanup()

	def test_all_source_sentinels_survive_current_text_layer(self):
		sentinels = sorted(set(re.findall(r"SENTINEL-[A-Z0-9-]+", self.source)))
		missing = [sentinel for sentinel in sentinels if sentinel not in self.markdown]
		self.assertEqual(missing, [])

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

	def test_raster_ocr_fixture_is_preserved_as_image_by_default(self):
		images = self.result.report["images_detail"]
		rasters = [image for image in images if image["kind"] == "raster"]
		vectors = [image for image in images if image["kind"] == "vector"]
		self.assertEqual(len(rasters), 1)
		self.assertGreaterEqual(len(vectors), 1)
		self.assertRegex(self.markdown, r'<img src=".+/img-[0-9a-f]+\.png"')
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
			self.assertRegex(markdown, r'<img src="assets/img-[0-9a-f]+\.png"')
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
