from __future__ import annotations

import unittest

from cocoapdf.html.semantic import render_semantic_html
from cocoapdf.ir.semantic import SemanticDocument, SemanticNode, SourceRef
from cocoapdf.markdown.semantic import render_semantic_markdown
from cocoapdf.semantics.output import render_reconciled_outputs
from cocoapdf.semantics.tables import _requires_html


SOURCE = [SourceRef(page=1, glyph_ids=(1, 2, 3))]


def _text(node_id: str, value: str, *, code: bool = False) -> SemanticNode:
    leaf = SemanticNode(
        id=node_id + "-text",
        kind="text",
        text=value,
        sources=SOURCE,
    )
    if not code:
        return leaf
    return SemanticNode(
        id=node_id,
        kind="code",
        children=[leaf],
        sources=SOURCE,
    )


def _cell(
    node_id: str,
    value: str,
    *,
    row: int,
    col: int,
    row_header: bool = False,
    code: bool = False,
) -> SemanticNode:
    content = _text(node_id + "-content", value, code=code)
    paragraph = SemanticNode(
        id=node_id + "-paragraph",
        kind="paragraph",
        children=[content],
        sources=SOURCE,
    )
    attrs = {
        "row": row,
        "col": col,
        "rowspan": 1,
        "colspan": 1,
        "role": "th" if row_header else "td",
    }
    if row_header:
        attrs["scope"] = "row"
    return SemanticNode(
        id=node_id,
        kind="table_cell",
        children=[paragraph],
        attrs=attrs,
        sources=SOURCE,
    )


def _row_header_table(*, layout_markdown: str = "") -> SemanticNode:
    rows = [
        SemanticNode(
            id="row-alpha",
            kind="table_row",
            children=[
                _cell(
                    "header-alpha",
                    "alpha_key",
                    row=0,
                    col=0,
                    row_header=True,
                    code=True,
                ),
                _cell("value-alpha", "First value", row=0, col=1),
            ],
            sources=SOURCE,
        ),
        SemanticNode(
            id="row-beta",
            kind="table_row",
            children=[
                _cell(
                    "header-beta",
                    "beta_key",
                    row=1,
                    col=0,
                    row_header=True,
                    code=True,
                ),
                _cell("value-beta", "Second value", row=1, col=1),
            ],
            sources=SOURCE,
        ),
    ]
    attrs = {
        "header_rows": 0,
        "row_count": 2,
        "column_count": 2,
        # Deliberately stale hints exercise semantic precedence in both
        # reconciled Markdown and independent HTML.
        "_layout_html": (
            "<table><tr><td>stale key</td><td>stale value</td></tr></table>"
        ),
    }
    if layout_markdown:
        attrs["_layout_markdown"] = layout_markdown
    return SemanticNode(
        id="row-header-table",
        kind="table",
        children=rows,
        attrs=attrs,
        sources=SOURCE,
    )


class RowHeaderProjectionTests(unittest.TestCase):
    def test_graph_markdown_uses_html_for_scoped_row_headers(self) -> None:
        table = _row_header_table()
        markdown = render_semantic_markdown(SemanticDocument([table]))

        self.assertIn(
            '<th scope="row"><code>alpha_key</code></th>',
            markdown,
        )
        self.assertIn("<td>First value</td>", markdown)
        self.assertNotIn("| alpha_key |", markdown)
        self.assertEqual(markdown.count('<th scope="row">'), 2)

    def test_graph_html_overrides_stale_layout_fragment(self) -> None:
        table = _row_header_table()
        rendered = render_semantic_html(SemanticDocument([table]))

        self.assertIn('<th scope="row"', rendered)
        self.assertIn("<code", rendered)
        self.assertIn("alpha_key", rendered)
        self.assertNotIn("stale key", rendered)
        self.assertEqual(table.attrs["_layout_html"].count("<td>"), 2)

    def test_graph_html_keeps_complete_lossless_row_header_fragment(self) -> None:
        table = _row_header_table()
        table.attrs["_layout_html"] = (
            '<table><tr><th scope="row"><code>alpha_key</code></th>'
            '<td>First value</td></tr><tr><th scope="row">'
            '<code>beta_key</code></th><td>Second value</td></tr></table>'
        )

        rendered = render_semantic_html(SemanticDocument([table]))

        self.assertEqual(rendered.count('<th scope="row">'), 2)
        self.assertNotIn("<tbody>", rendered)

    def test_reconciliation_overlays_gfm_that_cannot_carry_row_scope(self) -> None:
        layout = (
            "| Key | Value |\n"
            "| --- | --- |\n"
            "| alpha_key | First value |"
        )
        table = _row_header_table(layout_markdown=layout)
        report = {}

        markdown, rendered = render_reconciled_outputs(
            layout + "\n",
            SemanticDocument([table]),
            report,
        )

        self.assertIn('<th scope="row"><code>alpha_key</code></th>', markdown)
        self.assertNotIn("| alpha_key | First value |", markdown)
        self.assertIn('<th scope="row"', rendered)
        self.assertNotIn("stale key", rendered)
        self.assertEqual(
            report["markdown_projection"],
            "lossless_layout_reconciliation",
        )

    def test_simple_column_headers_remain_gfm(self) -> None:
        rows = [
            SemanticNode(
                id="heading-row",
                kind="table_row",
                children=[
                    _cell("heading-term", "Term", row=0, col=0),
                    _cell("heading-value", "Value", row=0, col=1),
                ],
                sources=SOURCE,
            ),
            SemanticNode(
                id="data-row",
                kind="table_row",
                children=[
                    _cell("data-term", "Alpha", row=1, col=0),
                    _cell("data-value", "One", row=1, col=1),
                ],
                sources=SOURCE,
            ),
        ]
        for cell in rows[0].children:
            cell.attrs["role"] = "th"
        table = SemanticNode(
            id="simple-table",
            kind="table",
            children=rows,
            attrs={"header_rows": 1},
            sources=SOURCE,
        )

        markdown = render_semantic_markdown(SemanticDocument([table]))
        self.assertIn("| Term | Value |", markdown)
        self.assertNotIn("<table>", markdown)
        self.assertFalse(_requires_html(rows, 1))

    def test_table_model_marks_body_header_cells_as_html_only(self) -> None:
        table = _row_header_table()
        rows = [child for child in table.children if child.kind == "table_row"]
        self.assertTrue(_requires_html(rows, 0))

    def test_explicit_row_scope_is_not_widened_by_rowspan(self) -> None:
        table = _row_header_table()
        first_header = table.children[0].children[0]
        first_header.attrs["rowspan"] = 2

        markdown = render_semantic_markdown(SemanticDocument([table]))

        self.assertIn('<th rowspan="2" scope="row">', markdown)
        self.assertNotIn('scope="rowgroup"', markdown)


if __name__ == "__main__":
    unittest.main(verbosity=2)
