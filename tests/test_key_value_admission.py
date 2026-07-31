from __future__ import annotations

import unittest

from cocoapdf import ConvertOptions, convert
from cocoapdf.synthetic import make_pdf, text_op


def _styled_row(y: float, key: str, value: str, value_x: float = 260.0) -> list[bytes]:
	return [
		text_op(72, y, key, "F2", 10),
		text_op(value_x, y, value, "F1", 10),
	]


def _convert_parts(parts: list[bytes]) -> str:
	return convert(make_pdf([b"\n".join(parts)]), ConvertOptions()).markdown


class BorderlessKeyValueAdmissionTests(unittest.TestCase):
	def test_stable_short_field_rows_remain_a_table(self) -> None:
		parts = [text_op(180, 720, "Table 2: Runtime profile", "F1", 10)]
		parts += _styled_row(690, "Mode", "verified")
		parts += _styled_row(668, "Format", "structured")
		parts += _styled_row(646, "Policy", "deterministic")

		markdown = _convert_parts(parts)

		self.assertIn("<caption>Table 2: Runtime profile</caption>", markdown)
		self.assertIn('<th scope="row">Mode</th><td>verified</td>', markdown)

	def test_long_delimited_field_labels_remain_a_table(self) -> None:
		parts = [text_op(180, 720, "Table 3: Configuration", "F1", 10)]
		parts += _styled_row(690, "Primary runtime execution policy:", "strict", 300)
		parts += _styled_row(668, "Default structured output mode:", "semantic", 300)
		parts += _styled_row(646, "Source provenance storage rule:", "complete", 300)

		markdown = _convert_parts(parts)

		self.assertIn("<caption>Table 3: Configuration</caption>", markdown)
		self.assertIn("Primary runtime execution policy:", markdown)

	def test_long_undelimited_bold_sentence_openings_are_not_a_table(self) -> None:
		parts = [text_op(180, 720, "Table 5: Narrative samples", "F1", 10)]
		parts += _styled_row(690, "Primary runtime execution policy", "remains strict.", 300)
		parts += _styled_row(668, "Default structured output mode", "remains semantic.", 300)
		parts += _styled_row(646, "Source provenance storage rule", "remains complete.", 300)

		markdown = _convert_parts(parts)

		self.assertNotIn("<table>", markdown)
		self.assertIn("Primary runtime execution policy", markdown)
		self.assertIn("remains complete.", markdown)

	def test_ragged_prose_continuations_are_not_a_key_value_table(self) -> None:
		parts = [text_op(170, 720, "Table 6: Regional notes", "F1", 10)]
		parts += _styled_row(690, "North", "has a long explanatory sentence.", 125)
		parts += _styled_row(668, "Southeastern district", "has another observation.", 205)
		parts += _styled_row(646, "West", "has the final note.", 145)

		markdown = _convert_parts(parts)

		self.assertNotIn("<table>", markdown)
		self.assertIn("long explanatory sentence", markdown)

	def test_contents_page_numbers_remain_navigation_without_dots_or_links(self) -> None:
		parts = [
			text_op(72, 720, "Contents", "F2", 16),
			text_op(250, 680, "Index", "F1", 10),
		]
		parts += _styled_row(650, "Opening", "2", 480)
		parts += _styled_row(628, "Methods", "4", 480)
		parts += _styled_row(606, "Results", "7", 480)

		markdown = _convert_parts(parts)

		self.assertNotIn("<table>", markdown)
		self.assertIn("Contents", markdown)
		self.assertIn("Results", markdown)

	def test_data_row_cannot_be_reused_as_a_visual_caption(self) -> None:
		parts: list[bytes] = []
		parts += _styled_row(690, "Overview", "introductory value", 240)
		parts += _styled_row(654, "Mode", "verified", 240)
		parts += _styled_row(632, "Format", "structured", 240)
		parts += _styled_row(610, "Policy", "deterministic", 240)

		markdown = _convert_parts(parts)

		self.assertNotIn("<table>", markdown)
		self.assertIn("introductory value", markdown)


if __name__ == "__main__":
	unittest.main()
