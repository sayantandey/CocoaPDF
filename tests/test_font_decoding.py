from __future__ import annotations

import unittest

from cocoapdf.fonts.decoding import parse_tounicode


class ToUnicodeRangeParsingTests(unittest.TestCase):
	def test_array_destinations_are_not_reinterpreted_as_scalar_range(self) -> None:
		cmap = b"""
		1 begincodespacerange
		<0000> <FFFF>
		endcodespacerange
		2 beginbfchar
		<002C> <0048>
		<002F> <0049>
		endbfchar
		1 beginbfrange
		<0355> <0358> [<002C> <003B> <003A> <002E>]
		endbfrange
		"""
		mapping = parse_tounicode(cmap)

		self.assertEqual(mapping[b"\x00,"], "H")
		self.assertEqual(mapping[b"\x00/"], "I")
		self.assertNotIn(b"\x00-", mapping)
		self.assertEqual(
			[mapping[code.to_bytes(2, "big")] for code in range(0x0355, 0x0359)],
			[",", ";", ":", "."],
		)

	def test_scalar_and_array_forms_coexist_in_one_range_block(self) -> None:
		cmap = b"""
		2 beginbfrange
		<01> <03> <0041>
		<10> <12> [<0061> <0062> <0063>]
		endbfrange
		"""
		mapping = parse_tounicode(cmap)

		self.assertEqual([mapping[bytes([code])] for code in range(1, 4)], ["A", "B", "C"])
		self.assertEqual([mapping[bytes([code])] for code in range(0x10, 0x13)], ["a", "b", "c"])

	def test_short_array_does_not_fabricate_unlisted_destinations(self) -> None:
		mapping = parse_tounicode(
			b"1 beginbfrange\n<10> <13> [<0041> <0042>]\nendbfrange"
		)

		self.assertEqual(mapping[b"\x10"], "A")
		self.assertEqual(mapping[b"\x11"], "B")
		self.assertNotIn(b"\x12", mapping)
		self.assertNotIn(b"\x13", mapping)

	def test_array_preserves_multicodepoint_and_surrogate_destinations(self) -> None:
		mapping = parse_tounicode(
			b"1 beginbfrange\n<20> <21> [<00660069> <D83DDE00>]\nendbfrange"
		)

		self.assertEqual(mapping[b"\x20"], "fi")
		self.assertEqual(mapping[b"\x21"], "\U0001f600")

	def test_unterminated_array_cannot_restart_as_a_scalar_range(self) -> None:
		mapping = parse_tounicode(
			b"1 beginbfchar\n<2C> <0048>\nendbfchar\n"
			b"1 beginbfrange\n<10> <13> [<2C> <3B> <3A>\nendbfrange"
		)

		self.assertEqual(mapping[b","], "H")
		for code in range(0x10, 0x14):
			self.assertNotIn(bytes([code]), mapping)
		self.assertNotIn(b"-", mapping)

	def test_mismatched_width_and_descending_source_ranges_are_ignored(self) -> None:
		mapping = parse_tounicode(
			b"2 beginbfrange\n"
			b"<01> <0003> <0041>\n"
			b"<10> <0F> [<0061> <0062>]\n"
			b"endbfrange"
		)

		self.assertEqual(mapping, {})

	def test_range_expansion_is_bounded(self) -> None:
		mapping = parse_tounicode(
			b"1 beginbfrange\n<00000000> <00010000> <0041>\nendbfrange"
		)

		self.assertEqual(mapping, {})


if __name__ == "__main__":
	unittest.main()
