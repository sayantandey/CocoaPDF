from __future__ import annotations

import re
import unittest
from typing import Iterable

from cocoapdf import ConvertOptions, convert
from cocoapdf.core import Char, Font, Line, line_text_tokens, plain_text
from cocoapdf.ir.semantic import SemanticNode, SourceRef
from cocoapdf.semantics import navigation
from cocoapdf.synthetic import line_op, make_pdf, text_op


MONO_FONT = (
	b"<< /Type /Font /Subtype /Type1 /BaseFont /Inconsolatazi4-Regular "
	b"/Encoding /WinAnsiEncoding >>"
)
ITALIC_FONT = (
	b"<< /Type /Font /Subtype /Type1 /BaseFont /NimbusRomNo9L-ReguItal "
	b"/Encoding /WinAnsiEncoding >>"
)
EXTRA_FONTS = {"FM": MONO_FONT, "FI": ITALIC_FONT}


def semantic_walk(nodes: Iterable[SemanticNode]):
	for node in nodes:
		yield node
		yield from semantic_walk(node.children)


class ReferenceManualAccuracyTests(unittest.TestCase):
	def test_abbreviated_tex_font_families_are_classified(self):
		self.assertTrue(Font("FM", "ABCDEF+Inconsolatazi4-Regular").mono)
		self.assertTrue(Font("FI", "ABCDEF+NimbusRomNo9L-ReguItal").italic)
		self.assertTrue(Font("FI", "ABCDEF+NimbusRomNo9L-MediItal").italic)
		self.assertFalse(Font("FR", "ABCDEF+NimbusRomNo9L-Regu").italic)
		self.assertTrue(Font("FP", "ABCDEF+URWPalladioL-Ital").italic)

	def test_materially_contracted_repeated_word_spaces_are_recovered(self):
		font = Font("F1", "NimbusRomNo9L-Regu")

		def chars_for(text: str, word_gap: float, letter_gap: float = 0.0):
			chars = []
			x = 0.0
			sequence = 1
			for character in text:
				if character == " ":
					x += word_gap
					continue
				chars.append(
					Char(
						character,
						x,
						0.0,
						x + 5.0,
						10.0,
						10.0,
						font,
						1,
						sequence,
					)
				)
				x += 5.0 + letter_gap
				sequence += 1
			return chars

		contracted = Line(chars_for("long words need clear gaps", 1.90), 1, 1)
		tracked = Line(chars_for("TRACKEDTEXT", 0.0, letter_gap=1.90), 1, 2)
		formula = Line(chars_for("2p M Q h M Q 2h", 1.90), 1, 3)

		self.assertEqual(
			plain_text(line_text_tokens(contracted)),
			"long words need clear gaps",
		)
		self.assertEqual(plain_text(line_text_tokens(tracked)), "TRACKEDTEXT")
		self.assertNotIn(" ", plain_text(line_text_tokens(formula)).replace("2p", ""))

	def test_dynamic_folios_require_a_document_cohort(self):
		def page_stream(page: int) -> bytes:
			header = (
				[text_op(72, 720, str(page)), text_op(450, 720, f"chapter_{page}")]
				if page % 2
				else [text_op(72, 720, f"chapter_{page}"), text_op(530, 720, str(page))]
			)
			return b"\n".join(
				header
				+ [
					text_op(72, 650, f"Body content for page {page}.", size=12),
					text_op(300, 60, str(page)),
				]
			)

		four_page = convert(
			make_pdf([page_stream(page) for page in range(1, 5)]),
			ConvertOptions(page_breaks=True),
		).markdown
		for page in range(1, 5):
			self.assertIn(f"Body content for page {page}.", four_page)
			self.assertNotIn(f"chapter_{page}", four_page)
		self.assertIsNone(re.search(r"(?m)^[1-4]$", four_page))

		two_page = convert(
			make_pdf([page_stream(page) for page in range(1, 3)]),
			ConvertOptions(page_breaks=True),
		).markdown
		self.assertIn("chapter_1", two_page)
		self.assertIn("chapter_2", two_page)

	def test_reference_manual_semantics_from_independent_layout_evidence(self):
		operations = []

		def add_text(x, y, text, font="F1", size=10):
			operations.append(text_op(x, y, text, font, size))

		def add_rule(y):
			operations.append(line_op(72, y, 540, y, 0.4))

		add_text(72, 750, "Contents", "F2", 16)
		for index, (name, page) in enumerate(
			(("alpha_fn", 1), ("beta_fn", 1), ("gamma_fn", 1))
		):
			add_text(
				100,
				725 - index * 13,
				f"{name} . . . . . . . . . . . . . . . . . . . . . . {page}",
			)

		add_rule(665)
		add_text(84, 645, "alpha_fn", "FM")
		add_text(220, 645, "Alpha function does a thing", "FI")
		add_rule(628)
		add_text(72, 600, "Description", "F2")
		add_text(90, 582, "Does useful work in a deterministic way.")
		add_text(72, 555, "Usage", "F2")
		add_text(90, 537, "alpha_fn(x = 1)", "FM", 9)
		add_text(72, 510, "Arguments", "F2")
		add_text(90, 491, "x", "FM")
		add_text(180, 491, "Input value pro-")
		add_text(180, 479, "vided by the caller.")
		add_text(90, 458, "mode", "FM")
		add_text(180, 458, "Execution mode.")
		add_text(72, 430, "Value", "F2")
		add_text(90, 412, "A scalar value.")
		add_text(72, 385, "Source", "F2")
		add_text(90, 367, "https://example.test/api", "FM")
		add_text(72, 335, "The result has the following columns.")
		for index, name in enumerate(
			("id", "title", "summary", "authors", "category"),
			1,
		):
			y = 318 - (index - 1) * 14
			add_text(130, y, f"[,{index}]")
			add_text(210, y, name, "FM")
			add_text(315, y, f"{name} field")
		add_text(72, 225, "Title", "F2")
		add_text(120, 225, "Synthetic package")
		add_text(72, 207, "Version", "F2")
		add_text(126, 207, "1.0")
		add_text(72, 189, "NeedsCompilation", "F2")
		add_text(180, 189, "no")
		add_text(72, 171, "Author", "F2")
		add_text(120, 171, "A Person <a@example.test>")

		result = convert(
			make_pdf([b"\n".join(operations)], extra_fonts=EXTRA_FONTS),
			ConvertOptions(output_format="both"),
		)
		markdown = result.markdown
		nodes = list(semantic_walk(result.semantic.children))

		self.assertIn("## `alpha_fn` *Alpha function does a thing*", markdown)
		for label in ("Description", "Usage", "Arguments", "Value", "Source"):
			self.assertIn(f"### {label}", markdown)
		self.assertIn("```\nalpha_fn(x = 1)\n```", markdown)
		self.assertNotIn("```\nhttps://example.test/api", markdown)
		self.assertIn(
			'<tr><th scope="row"><code>x</code></th>'
			'<td>Input value provided by the caller.</td></tr>',
			markdown,
		)
		self.assertIn(
			'<tr><td>[,5]</td><th scope="row"><code>category</code></th>'
			'<td>category field</td></tr>',
			markdown,
		)
		self.assertIn("**Title** Synthetic package  \n**Version** 1.0", markdown)
		self.assertNotIn("##### **NeedsCompilation**", markdown)

		toc = next(node for node in nodes if node.kind == "toc")
		self.assertEqual([child.text for child in toc.children], ["alpha_fn", "beta_fn", "gamma_fn"])
		# Two entries are excerpts without matching headings in this synthetic
		# page, so Markdown keeps the source dot-leader block. The independent
		# semantic graph still exposes all three TOC items.
		self.assertNotIn(" — 1", markdown)
		self.assertEqual(sum(node.kind == "table" for node in nodes), 2)
		row_headers = [
			node for node in nodes
			if node.kind == "table_cell" and node.attrs.get("scope") == "row"
		]
		self.assertEqual(len(row_headers), 7)
		self.assertTrue(all(node.sources for node in row_headers))
		inline_code_headers = 0
		for cell in row_headers:
			descendants = list(semantic_walk(cell.children))
			self.assertTrue(cell.children and cell.children[0].kind == "paragraph")
			self.assertFalse(any(node.kind == "code_block" for node in descendants))
			inline_code_headers += int(any(node.kind == "code" for node in descendants))
		self.assertGreaterEqual(inline_code_headers, 2)

	def test_single_explicit_argument_row_is_a_definition_table(self):
		stream = b"\n".join(
			[
				text_op(72, 700, "Arguments", "F2", 10),
				text_op(90, 680, "max_time", "FM", 10),
				text_op(180, 680, "Maximum wait time in seconds", "F1", 10),
				text_op(72, 650, "Value", "F2", 10),
				text_op(90, 630, "TRUE on success.", "F1", 10),
			]
		)
		markdown = convert(
			make_pdf([stream], extra_fonts={"FM": MONO_FONT}),
			ConvertOptions(),
		).markdown
		self.assertIn(
			'<th scope="row"><code>max_time</code></th>'
			'<td>Maximum wait time in seconds</td>',
			markdown,
		)

	def test_table_detectors_keep_negative_admission_gates(self):
		operations = [
			text_op(72, 700, "Arguments", "F2", 10),
			text_op(90, 680, "This is ordinary prose, not a keyed argument row."),
			text_op(72, 650, "Value", "F2", 10),
			text_op(72, 620, "The result has the following columns."),
		]
		for index, name in enumerate(("id", "title", "summary", "authors"), 1):
			y = 600 - (index - 1) * 14
			operations.extend(
				[
					text_op(130, y, f"[,{index}]"),
					text_op(210, y, name, "FM", 10),
					text_op(315, y, f"{name} field"),
				]
			)
		markdown = convert(
			make_pdf([b"\n".join(operations)], extra_fonts={"FM": MONO_FONT}),
			ConvertOptions(),
		).markdown
		self.assertNotIn("<table>", markdown)

	def test_outline_prefers_composite_heading_over_exact_cross_reference(self):
		heading = SemanticNode(
			id="heading",
			kind="heading",
			text="catalog_lookup The primary lookup function",
			attrs={"level": 2},
			sources=[SourceRef(page=4, bbox=(0, 0, 10, 10))],
		)
		cross_reference = SemanticNode(
			id="paragraph",
			kind="paragraph",
			text="catalog_lookup()",
			sources=[SourceRef(page=4, bbox=(0, 20, 10, 30))],
		)
		target = navigation._best_heading_target(
			[cross_reference, heading],
			"catalog_lookup",
			page=4,
			outline_level=1,
		)
		self.assertEqual(target[0], "heading")
		self.assertEqual(cross_reference.kind, "paragraph")
		self.assertEqual(heading.attrs["level"], 2)

	def test_pro_prefix_exception_is_narrow(self):
		from cocoapdf.core import hyphen_join_mode

		self.assertEqual(hyphen_join_mode("pro-", "vided"), "delete")
		self.assertEqual(hyphen_join_mode("pro-", "viding"), "delete")
		self.assertEqual(hyphen_join_mode("pro-", "active"), "keep")
		self.assertEqual(hyphen_join_mode("well-", "known"), "keep")

	def test_reference_signature_preserves_front_matter_and_bold_heading(self):
		operations = [
			text_op(72, 750, "Opening prose establishes the ordinary size.", "F1", 10),
			text_op(72, 724, "Title", "F2", 10),
			text_op(120, 724, "Reference package", "F1", 10),
			text_op(72, 706, "Version", "F2", 10),
			text_op(130, 706, "2.0", "F1", 10),
			text_op(72, 688, "Author", "F2", 10),
			text_op(124, 688, "Example Maintainer", "F1", 10),
			# Separate same-baseline bold operations create neutral synthetic
			# spaces. They are typography boundaries only if the following visible
			# token actually changes style.
			text_op(72, 640, "Ablation", "F2", 10),
			text_op(140, 640, "Studies", "F2", 10),
			text_op(72, 610, "This section explains the measured ablation results.", "F1", 10),
			line_op(72, 565, 540, 565, 0.4),
			text_op(84, 545, "catalog_lookup", "FM", 10),
			text_op(220, 545, "The primary lookup function", "FI", 10),
			line_op(72, 528, 540, 528, 0.4),
			text_op(72, 500, "Description", "F2", 10),
			text_op(90, 482, "Returns a deterministic record.", "F1", 10),
		]
		result = convert(
			make_pdf([b"\n".join(operations)], extra_fonts=EXTRA_FONTS),
			ConvertOptions(output_format="both"),
		)
		self.assertIn(
			"**Title** Reference package  \n**Version** 2.0  \n"
			"**Author** Example Maintainer",
			result.markdown,
		)
		self.assertRegex(result.markdown, r"(?m)^#+ Ablation Studies$")
		headings = [node for node in result.semantic.walk() if node.kind == "heading"]
		self.assertTrue(
			any(
				"".join(
					child.text
					for child in node.walk()
					if child.kind == "text"
				) == "Ablation Studies"
				for node in headings
			)
		)
		self.assertIn('id="ablation-studies"', result.html)


if __name__ == "__main__":
	unittest.main(verbosity=2)
