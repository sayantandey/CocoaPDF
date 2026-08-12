from __future__ import annotations

import re
import unittest

from cocoapdf import ConvertOptions, convert
from cocoapdf.core import hyphen_join_mode
from cocoapdf.synthetic import make_pdf, text_op


class ReferenceManualSafetyTests(unittest.TestCase):
	def test_dynamic_furniture_keeps_middle_same_baseline_content(self):
		names = ("amber", "birch", "cedar", "dogwood")

		def page_stream(page: int) -> bytes:
			name = names[page - 1]
			if page % 2:
				margin = [
					text_op(72, 720, str(page)),
					text_op(460, 720, "running_%s" % name),
				]
			else:
				margin = [
					text_op(72, 720, "running_%s" % name),
					text_op(530, 720, str(page)),
				]
			# A separate authored component shares the physical baseline. This is
			# deliberately the closest near-miss for a producer that groups a body
			# heading with the changing running head and folio.
			margin.append(text_op(250, 720, "Kept%s" % name.title()))
			return b"\n".join(
				margin
				+ [text_op(72, 650, "Ordinary body %s." % name, size=12)]
			)

		result = convert(
			make_pdf([page_stream(page) for page in range(1, 5)]),
			ConvertOptions(page_breaks=True),
		)
		for page, name in enumerate(names, 1):
			self.assertIn("Kept%s" % name.title(), result.markdown)
			self.assertIn("Ordinary body %s." % name, result.markdown)
			self.assertNotIn("running_%s" % name, result.markdown)
			self.assertIsNone(re.search(r"(?m)^%d$" % page, result.markdown))

		warnings = [
			warning
			for warning in result.warnings
			if warning.code == "PHYSICAL_PAGE_FURNITURE_REMOVED"
		]
		self.assertEqual([warning.page for warning in warnings], [1, 2, 3, 4])
		for warning in warnings:
			self.assertIn("document_cohort", warning.detail)
			self.assertRegex(warning.detail, r"source_glyph_ids=\d")
			self.assertIn("removed_component_bboxes=[", warning.detail)
			self.assertIn("retained_components=1", warning.detail)

	def test_standalone_physical_folios_use_dynamic_outer_margin_cohort(self):
		pages = [
			b"\n".join(
				[
					text_op(72, 650, "Body page %d remains." % page, size=12),
					# This falls inside the patch's outer 12% cohort band but
					# outside the older 8% unconditional margin-marker band.
					text_op(300, 80, str(page)),
				]
			)
			for page in range(1, 4)
		]
		result = convert(make_pdf(pages), ConvertOptions(page_breaks=True))
		for page in range(1, 4):
			self.assertIn("Body page %d remains." % page, result.markdown)
			self.assertIsNone(re.search(r"(?m)^%d$" % page, result.markdown))

	def test_hard_prefix_hyphens_remain_conservative(self):
		# The reference-manual recovery stays narrow: it must not turn ordinary
		# lexical compounds into concatenated words while generic physical wraps
		# continue to join.
		self.assertEqual(hyphen_join_mode("pro-", "active"), "keep")
		self.assertEqual(hyphen_join_mode("pre-", "existing"), "keep")
		self.assertEqual(hyphen_join_mode("well-", "known"), "keep")
		self.assertEqual(hyphen_join_mode("determin-", "istic"), "delete")


if __name__ == "__main__":
	unittest.main(verbosity=2)
