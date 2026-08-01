import unittest

from cocoapdf.html.semantic import render_semantic_html
from cocoapdf.ir.semantic import SemanticDocument, SemanticNode, SourceRef


class HtmlTaskAccessibilityTests(unittest.TestCase):
	def test_styled_inline_text_has_no_invented_spaces_and_nested_list_is_excluded(self):
		sources = [SourceRef(page=1)]

		def text(node_id: str, value: str) -> SemanticNode:
			return SemanticNode(node_id, "text", text=value, sources=sources)

		nested = SemanticNode(
			"nested-list",
			"list",
			children=[
				SemanticNode(
					"nested-item",
					"item",
					children=[text("nested-text", "Nested bullet")],
					sources=sources,
				)
			],
			attrs={"ordered": False, "marker_style": "disc"},
			sources=sources,
		)
		styled = SemanticNode(
			"strong",
			"strong",
			children=[text("strong-text", "mark")],
			sources=sources,
		)
		task = SemanticNode(
			"task",
			"item",
			children=[
				text("prefix", "re"),
				styled,
				text("suffix", "able task"),
				nested,
			],
			attrs={"task": True, "checked": True},
			sources=sources,
		)
		document = SemanticDocument(
			[
				SemanticNode(
					"tasks",
					"list",
					children=[task],
					attrs={"ordered": False, "marker_style": "disc"},
					sources=sources,
				)
			]
		)

		rendered = render_semantic_html(document)
		self.assertIn('aria-label="Checked task: remarkable task"', rendered)
		self.assertNotIn('aria-label="Checked task: re mark able task', rendered)
		self.assertNotIn('aria-label="Checked task: remarkable task Nested bullet', rendered)
		self.assertRegex(rendered, r"re<strong[^>]*>mark</strong>able task")
		self.assertIn("Nested bullet", rendered)


if __name__ == "__main__":
	unittest.main()
