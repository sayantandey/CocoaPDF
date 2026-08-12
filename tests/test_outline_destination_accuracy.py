from __future__ import annotations

import unittest

from cocoapdf.ir.semantic import SemanticNode, SourceRef
from cocoapdf.semantics.navigation import (
    _best_heading_target,
    _normalized_destination_y,
)


def node(
    identifier: str,
    kind: str,
    text: str,
    y0: float,
    y1: float,
) -> SemanticNode:
    return SemanticNode(
        id=identifier,
        kind=kind,
        text=text,
        attrs={"level": 2} if kind == "heading" else {},
        sources=[SourceRef(page=4, bbox=(72.0, y0, 420.0, y1))],
    )


class OutlineDestinationAccuracyTests(unittest.TestCase):
    def test_pdf_destination_is_normalized_to_extracted_page_coordinates(self) -> None:
        class Document:
            def pages(self):
                return [{"MediaBox": [0.0, 0.0, 612.0, 792.0]}]

            def resolve(self, value):
                return value

            def resolve_array(self, value):
                return value if isinstance(value, list) else []

        self.assertEqual(
            _normalized_destination_y(
                Document(),
                1,
                ["XYZ", 0.0, 497.0, 1.0],
            ),
            295.0,
        )

    def test_explicit_destination_prefers_nearest_exact_paragraph(self) -> None:
        composite = node(
            "composite",
            "heading",
            "Safety Limitations",
            90.0,
            110.0,
        )
        exact = node("exact", "paragraph", "Safety", 295.0, 315.0)

        target = _best_heading_target(
            [exact, composite],
            "Safety",
            page=4,
            outline_level=1,
            destination=["XYZ", 0.0, 497.0, 1.0],
            destination_y=295.0,
        )

        self.assertEqual(target, ("exact", "safety"))
        self.assertEqual(exact.kind, "heading")
        self.assertNotIn("anchor", composite.attrs)

    def test_explicit_destination_preserves_composite_heading_target(self) -> None:
        composite = node(
            "composite",
            "heading",
            "record_search The main search function",
            90.0,
            110.0,
        )
        cross_reference = node(
            "cross-reference",
            "paragraph",
            "record_search",
            295.0,
            315.0,
        )

        target = _best_heading_target(
            [cross_reference, composite],
            "record_search",
            page=4,
            outline_level=1,
            destination=["FitH", 702.0],
            destination_y=90.0,
        )

        self.assertEqual(
            target,
            ("composite", "record-search-the-main-search-function"),
        )
        self.assertEqual(cross_reference.kind, "paragraph")
        self.assertNotIn("anchor", cross_reference.attrs)

    def test_page_only_destination_keeps_textual_composite_prior(self) -> None:
        composite = node(
            "composite",
            "heading",
            "record_search The main search function",
            90.0,
            110.0,
        )
        cross_reference = node(
            "cross-reference",
            "paragraph",
            "record_search",
            295.0,
            315.0,
        )

        target = _best_heading_target(
            [cross_reference, composite],
            "record_search",
            page=4,
            outline_level=1,
            destination=["XYZ", None, None, None],
        )

        self.assertEqual(target[0], "composite")
        self.assertEqual(cross_reference.kind, "paragraph")

    def test_missing_geometry_retains_existing_textual_behavior(self) -> None:
        composite = SemanticNode(
            id="composite",
            kind="heading",
            text="Safety Limitations",
            attrs={"level": 2},
            sources=[SourceRef(page=4)],
        )
        exact = SemanticNode(
            id="exact",
            kind="paragraph",
            text="Safety",
            sources=[SourceRef(page=4)],
        )

        target = _best_heading_target(
            [exact, composite],
            "Safety",
            page=4,
            destination=["XYZ", 0.0, 400.0, 1.0],
            destination_y=392.0,
        )

        self.assertEqual(target[0], "composite")
        self.assertEqual(exact.kind, "paragraph")

    def test_far_destination_does_not_override_textual_composite_prior(self) -> None:
        composite = node(
            "composite",
            "heading",
            "lookup_entry Detailed lookup operation",
            90.0,
            110.0,
        )
        exact = node("exact", "paragraph", "lookup_entry", 295.0, 315.0)

        target = _best_heading_target(
            [exact, composite],
            "lookup_entry",
            page=4,
            destination=["XYZ", 0.0, 192.0, 1.0],
            destination_y=600.0,
        )

        self.assertEqual(
            target,
            ("composite", "lookup-entry-detailed-lookup-operation"),
        )
        self.assertEqual(exact.kind, "paragraph")


if __name__ == "__main__":
    unittest.main(verbosity=2)
