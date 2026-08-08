from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Iterable, Optional
from unittest.mock import patch

from cocoapdf import convert
from cocoapdf.cli import _format_payload
from cocoapdf.content.runtime import _attach_marked_content
from cocoapdf.core import (
    Char,
    ConvertOptions,
    Converter,
    Font,
    Line,
    MarkdownRenderer,
    painted_path_contains_point,
)
from cocoapdf.fonts.decoding import CMapMapping, decode_font, parse_tounicode
from cocoapdf.html.sanitize import is_safe_generated_html
from cocoapdf.html.semantic import render_semantic_html
from cocoapdf.ir.evidence import Evidence
from cocoapdf.ir.semantic import NodeFactory, SemanticDocument, SemanticNode, SourceRef
from cocoapdf.markdown.semantic import render_semantic_markdown
from cocoapdf.reporting.report import attach_semantic_document
from cocoapdf.semantics.navigation import _best_heading_target
from cocoapdf.semantics.graph import _project_table_alignments
from cocoapdf.semantics.notes import enrich_notes_references_crossrefs
from cocoapdf.semantics.output import (
    _overlay_changed_blocks,
    render_reconciled_outputs,
)
from cocoapdf.semantics.reconcile import (
    _apply_tagged_child_order,
    _apply_tagged_order,
    _apply_tagged_prior,
    _binding_score,
    _materialize_tagged_structures,
    _merge_same_owner_paragraph_events,
    _tagged_list_node,
    _tagged_table_node,
    reconcile_tagged_content,
)
from cocoapdf.semantics.source import inline_nodes_from_tokens
from cocoapdf.semantics.tables import _cell_blocks, _prune_vacuous_narrow_edge_columns
from cocoapdf.semantics.tagged import parse_tagged_structure
from cocoapdf.synthetic import image_xobject_rgb, line_op, make_pdf, rect_fill_op, text_op
from cocoapdf.text.bidi import reorder_text, reorder_tokens



def render_pdf(
    objects: Iterable[bytes],
    root: int,
    *,
    xref: bool = True,
    trailer_extra: bytes = b"",
) -> bytes:
    """Build a small deterministic PDF for semantic integration tests."""
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
            ("trailer\n<< /Size %d /Root %d 0 R " % (len(objects) + 1, root)).encode("ascii")
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


def convert(data: bytes, options: ConvertOptions | None = None):
    return Converter(data, options or ConvertOptions()).convert()


def semantic_nodes(result, kind: str):
    return [node for node in result.semantic.walk() if node.kind == kind]


class AuthoritativeGraphTests(unittest.TestCase):
    def test_all_outputs_derive_from_semantic_graph(self) -> None:
        content = b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (Graph source) Tj ET"
        result = convert(one_page_pdf(content))
        self.assertIsNotNone(result.semantic)
        self.assertTrue(result.report["semantic_output_used"])
        self.assertEqual(result.report["output_derivation"], {"markdown": "semantic_graph", "html": "semantic_graph", "json": "semantic_graph"})
        self.assertEqual(result.report["html_projection"], "direct_semantic_html")
        self.assertEqual(result.semantic.metadata["output_policy"], "independent_semantic_projections")
        self.assertTrue(result.report["semantic_valid"], result.report["semantic_errors"])
        self.assertIn("Graph source", result.markdown)
        self.assertIn("Graph source", result.html)
        self.assertRegex(
            result.html,
            r'<p data-cocoapdf-node="[^"]+" data-confidence="[0-9.]+" '
            r'data-source-pages="1">Graph source</p>',
        )
        payload = json.loads(_format_payload(result, "json"))
        self.assertEqual(payload["semantic_document"]["schema"], "cocoapdf.semantic-document")
        paragraphs = semantic_nodes(result, "paragraph")
        self.assertEqual(len(paragraphs), 1)
        self.assertTrue(paragraphs[0].sources[0].glyph_ids)

    def test_untagged_catalog_language_reaches_html_root(self) -> None:
        content = b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (Bonjour) Tj ET"
        result = convert(
            one_page_pdf(content, catalog_extra=b"/Lang (fr-FR)")
        )
        self.assertEqual(result.semantic.metadata["language"], "fr-FR")
        self.assertIn('<html lang="fr-FR">', result.html)

    def test_untagged_html_never_reparses_markdown(self) -> None:
        content = b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (Independent HTML) Tj ET"
        with patch(
            "cocoapdf.html.render.render_html",
            side_effect=AssertionError("legacy Markdown HTML renderer was called"),
        ):
            result = convert(one_page_pdf(content))
        self.assertEqual(result.markdown, "Independent HTML\n")
        self.assertIn("<main", result.html)
        self.assertIn("Independent HTML", result.html)
        self.assertEqual(result.report["html_projection"], "direct_semantic_html")

    def test_emergency_html_fallback_is_still_graph_derived(self) -> None:
        content = b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (Fallback evidence) Tj ET"
        with patch(
            "cocoapdf.semantics.output.render_reconciled_outputs",
            side_effect=RuntimeError("forced projection failure"),
        ), patch(
            "cocoapdf.html.render.render_html",
            side_effect=AssertionError("Markdown HTML fallback was called"),
        ) as legacy_renderer:
            result = convert(one_page_pdf(content))
        legacy_renderer.assert_not_called()
        self.assertEqual(result.markdown, "Fallback evidence\n")
        self.assertIn("cocoapdf-minimal-fallback", result.html)
        self.assertIn("Fallback evidence", result.html)
        self.assertEqual(
            result.report["html_projection"],
            "minimal_semantic_html_fallback",
        )
        self.assertFalse(result.report["semantic_output_used"])
        self.assertTrue(
            any(
                warning.code == "SEMANTIC_OUTPUT_FAILED"
                for warning in result.warnings
            )
        )

    def test_html_projection_cannot_change_markdown_bytes(self) -> None:
        content = b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (Markdown invariant) Tj ET"
        sentinel = "<!doctype html><html><body>semantic sentinel</body></html>\n"
        with patch(
            "cocoapdf.semantics.output.render_semantic_html",
            return_value=sentinel,
        ):
            result = convert(one_page_pdf(content))
        self.assertEqual(result.markdown, "Markdown invariant\n")
        self.assertEqual(result.html, sentinel)

    def test_html_list_structure_uses_event_records_not_markdown_overlay(self) -> None:
        stream = b"\n".join(
            [
                text_op(72, 720, "1. Alpha independent", "F1", 10),
                text_op(72, 700, "2. Bravo independent", "F1", 10),
            ]
        )
        original_analyze = MarkdownRenderer.analyze

        def poison_markdown_overlay(renderer):
            events_by_page = original_analyze(renderer)
            for events in events_by_page.values():
                for event in events:
                    if event.kind == "list":
                        event.legacy_markdown = "99. poisoned Markdown overlay"
            return events_by_page

        with patch.object(
            MarkdownRenderer,
            "analyze",
            poison_markdown_overlay,
        ):
            result = convert(make_pdf([stream]))

        self.assertEqual(result.markdown, "99. poisoned Markdown overlay\n")
        self.assertIn("Alpha independent", result.html)
        self.assertIn("Bravo independent", result.html)
        self.assertNotIn("poisoned Markdown overlay", result.html)
        self.assertRegex(
            result.html,
            r'(?s)<ol\b[^>]*>.*<li\b[^>]*value="1"[^>]*>'
            r".*Alpha independent.*<li\b[^>]*value=\"2\"[^>]*>"
            r".*Bravo independent.*</ol>",
        )

    def test_visible_toc_is_not_suppressed_as_an_outline_duplicate(self) -> None:
        source = [SourceRef(page=1)]
        item = SemanticNode(
            "toc-item",
            "toc_item",
            text="Introduction",
            attrs={"target_anchor": "introduction"},
            sources=source,
        )
        toc = SemanticNode(
            "visible-toc",
            "toc",
            children=[item],
            attrs={"source": "visible_toc"},
            sources=source,
        )
        document = SemanticDocument(
            [toc],
            metadata={"outline": {"kind": "outline"}},
        )
        markdown, rendered = render_reconciled_outputs(
            "## Contents\n\nIntroduction .... 1\n",
            document,
            {},
        )
        self.assertEqual(markdown, "## Contents\n\nIntroduction .... 1\n")
        self.assertIn('class="cocoapdf-toc"', rendered)
        self.assertIn('href="#introduction"', rendered)

    def test_encrypted_refusal_still_uses_authoritative_graph_contract(self) -> None:
        content = b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (Ciphertext) Tj ET"
        objects = [
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Page /Parent 4 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 1 0 R >> >> /Contents 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Catalog /Pages 4 0 R >>",
            b"<< /Filter /Standard /V 2 /R 3 /Length 128 /O <00> /U <00> /P -4 >>",
        ]
        result = convert(render_pdf(objects, 5, trailer_extra=b"/Encrypt 6 0 R"))
        self.assertIsNotNone(result.semantic)
        self.assertEqual(result.semantic.metadata["output_policy"], "refused")
        self.assertEqual(result.markdown, "")
        self.assertTrue(result.report["semantic_output_used"])
        self.assertFalse(result.report["ocr_used"])
        self.assertEqual(result.report["output_derivation"]["html"], "semantic_graph")

    def test_image_is_preserved_with_pdf_placement_and_no_ocr(self) -> None:
        content = b"q 100 0 0 40 256 650 cm BI /W 1 /H 1 /CS /RGB /BPC 8 ID \xff\x00\x00 EI Q"
        result = convert(one_page_pdf(content))
        images = semantic_nodes(result, "image")
        self.assertEqual(len(images), 1)
        image = images[0]
        self.assertAlmostEqual(image.attrs["display_width_pt"], 100.0)
        self.assertAlmostEqual(image.attrs["display_height_pt"], 40.0)
        self.assertEqual(image.attrs["alignment"], "center")
        self.assertFalse(image.attrs["text_extraction_attempted"])
        self.assertIn("![](", result.markdown)
        self.assertNotIn("<figure", result.markdown)
        self.assertIn("width: 100.000pt", result.html)
        self.assertIn("height: 40.000pt", result.html)


class AcroFormAndOutlineTests(unittest.TestCase):
    def test_acroform_field_tree_is_semantic_and_actions_are_not_executed(self) -> None:
        content = b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (Form) Tj ET"
        data = one_page_pdf(
            content,
            page_extra=b"/Annots [7 0 R]",
            catalog_extra=b"/AcroForm 6 0 R",
            extra_objects=[
                b"<< /Fields [7 0 R] /NeedAppearances false >>",
                b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (Name) /V (Alice) /Rect [72 650 240 675] /P 3 0 R /AA << /K << /S /JavaScript /JS (evil) >> >> >>",
            ],
        )
        result = convert(data)
        fields = semantic_nodes(result, "form_field")
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0].attrs["name"], "Name")
        self.assertEqual(fields[0].attrs["value"], "Alice")
        self.assertTrue(fields[0].attrs["actions_ignored"])
        self.assertIn("PDF_ACTIONS_NOT_EXECUTED", fields[0].warnings)
        self.assertIn("**Name:** Alice", result.markdown)
        self.assertIn('name="Name"', result.html)
        self.assertIn(
            '<span class="cocoapdf-form-field-name">Name:</span> '
            '<span class="cocoapdf-form-field-value">Alice</span>',
            result.html,
        )
        self.assertNotIn(
            'class="cocoapdf-form-field-value cocoapdf-form-field-value-evidenced"',
            result.html,
        )

    def test_acroform_widget_appearance_uses_only_pdf_native_evidence(self) -> None:
        content = b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (Styled field) Tj ET"
        appearance = (
            b"q 0.91 0.93 0.98 rg 0 0 160 25 re f "
            b"0.65 0.70 0.78 RG 1 w 0.5 0.5 159 24 re S "
            b"0.18 0.25 0.42 rg "
            b"BT /F1 18 Tf 1 0 0 1 4 5 Tm (Beta) Tj ET Q"
        )
        data = one_page_pdf(
            content,
            page_extra=b"/Annots [7 0 R]",
            catalog_extra=b"/AcroForm 6 0 R",
            extra_objects=[
                b"<< /Fields [7 0 R] /NeedAppearances false >>",
                (
                    b"<< /Type /Annot /Subtype /Widget /FT /Tx "
                    b"/T (SecondField) /V (Beta) /Rect [200 535 360 560] "
                    b"/P 3 0 R /DA (/F1 9 Tf 1 0 0 rg) /Q 0 "
                    b"/MK << /BG [1 0 0] /BC [0 1 0] >> "
                    b"/BS << /W 1 /S /S >> /AP << /N 8 0 R >> >>"
                ),
                (
                    b"<< /Type /XObject /Subtype /Form /FormType 1 "
                    b"/BBox [0 0 160 25] "
                    b"/Resources << /Font << /F1 1 0 R >> >> "
                    b"/Length %d >>\nstream\n" % len(appearance)
                    + appearance
                    + b"\nendstream"
                ),
            ],
        )
        result = convert(data)
        field = semantic_nodes(result, "form_field")[0]
        evidence = field.attrs["appearance"]
        self.assertEqual(evidence["font_size_pt"], 18.0)
        self.assertEqual(evidence["text_color_rgb"], [0.18, 0.25, 0.42])
        self.assertEqual(evidence["background_color_rgb"], [0.91, 0.93, 0.98])
        self.assertEqual(evidence["width_pt"], 160.0)
        self.assertEqual(evidence["height_pt"], 25.0)
        self.assertEqual(
            evidence["declared_default_appearance"]["font_size_pt"],
            9.0,
        )
        self.assertEqual(
            evidence["declared_appearance_characteristics"][
                "background_color_rgb"
            ],
            [1.0, 0.0, 0.0],
        )
        self.assertIn("normal_appearance_stream", evidence["sources"])
        self.assertIn("default_appearance", evidence["sources"])
        self.assertIn("appearance_characteristics", evidence["sources"])
        self.assertIn("cocoapdf-form-field-value-evidenced", result.html)
        self.assertIn("background-color: rgb(232, 237, 250)", result.html)
        self.assertIn("color: rgb(46, 64, 107)", result.html)
        self.assertIn("font-size: 18.000pt", result.html)
        self.assertNotIn("<input", result.html)

    def test_acroform_default_appearance_is_inherited(self) -> None:
        content = b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (Inherited style) Tj ET"
        data = one_page_pdf(
            content,
            page_extra=b"/Annots [7 0 R]",
            catalog_extra=b"/AcroForm 6 0 R",
            extra_objects=[
                (
                    b"<< /Fields [7 0 R] /NeedAppearances true "
                    b"/DA (/F1 13 Tf 0.1 g) "
                    b"/DR << /Font << /F1 1 0 R >> >> >>"
                ),
                (
                    b"<< /Type /Annot /Subtype /Widget /FT /Tx "
                    b"/T (InheritedField) /V (Value) "
                    b"/Rect [72 650 240 675] /P 3 0 R >>"
                ),
            ],
        )
        result = convert(data)
        appearance = semantic_nodes(result, "form_field")[0].attrs["appearance"]
        self.assertEqual(appearance["font_size_pt"], 13.0)
        self.assertEqual(appearance["text_color_rgb"], [0.1, 0.1, 0.1])
        self.assertEqual(
            appearance["sources"],
            ["default_appearance", "widget_rect"],
        )
        self.assertIn("font-size: 13.000pt", result.html)

    def test_outline_is_parsed_and_used_as_toc_when_visible_toc_is_absent(self) -> None:
        content = b"BT /F1 18 Tf 1 0 0 1 72 720 Tm (Introduction) Tj ET"
        data = one_page_pdf(
            content,
            catalog_extra=b"/Outlines 6 0 R",
            extra_objects=[
                b"<< /Type /Outlines /First 7 0 R /Last 7 0 R /Count 1 >>",
                b"<< /Title (Introduction) /Parent 6 0 R /Dest [3 0 R /XYZ null null null] >>",
            ],
        )
        result = convert(data)
        self.assertIn("outline", result.semantic.metadata)
        self.assertEqual(len(semantic_nodes(result, "toc")), 1)
        self.assertIn("Introduction", result.markdown)
        self.assertIn('class="cocoapdf-toc"', result.html)

    def test_page_only_outline_targets_the_unique_heading_on_that_page(self) -> None:
        factory = NodeFactory()
        source = [SourceRef(page=1)]
        heading = factory.make(
            "heading",
            attrs={"level": 2},
            sources=source,
        ).add(
            factory.make("text", text="Page Scope Review", sources=source)
        )
        body = factory.make("paragraph", sources=source).add(
            factory.make("text", text="First page body", sources=source)
        )

        target = _best_heading_target(
            [heading, body],
            "First Page",
            page=1,
            outline_level=1,
            destination=["XYZ", None, None, None],
        )

        self.assertEqual(target, (heading.id, "page-scope-review"))
        self.assertEqual(heading.attrs["anchor"], "page-scope-review")
        self.assertIn(
            "pdf_outline_unique_page_heading",
            {evidence.kind for evidence in heading.evidence},
        )

        competing = factory.make(
            "heading",
            attrs={"level": 2},
            sources=source,
        ).add(
            factory.make("text", text="Another Heading", sources=source)
        )
        self.assertIsNone(
            _best_heading_target(
                [heading, competing, body],
                "Unrelated Destination",
                page=1,
                outline_level=1,
                destination=["XYZ", None, None, None],
            )
        )

        self.assertIsNone(
            _best_heading_target(
                [heading, body],
                "Unrelated Destination",
                page=1,
                outline_level=1,
                destination=["XYZ", None, 400.0, None],
            )
        )

    def test_unresolved_outline_item_never_emits_a_none_anchor(self) -> None:
        content = b"\n".join(
            [
                b"BT /F1 18 Tf 1 0 0 1 72 720 Tm (Alpha Heading) Tj ET",
                b"BT /F1 18 Tf 1 0 0 1 72 650 Tm (Bravo Heading) Tj ET",
            ]
        )
        data = one_page_pdf(
            content,
            catalog_extra=b"/Outlines 6 0 R",
            extra_objects=[
                b"<< /Type /Outlines /First 7 0 R /Last 7 0 R /Count 1 >>",
                b"<< /Title (Unrelated Destination) /Parent 6 0 R /Dest [3 0 R /XYZ null null null] >>",
            ],
        )
        result = convert(data)
        item = semantic_nodes(result, "toc_item")[0]
        self.assertIsNone(item.attrs["target_anchor"])
        self.assertIn("Unrelated Destination", result.markdown)
        self.assertNotIn("#None", result.markdown)
        self.assertNotIn('href="#None"', result.html)

    def test_page_selection_keeps_outline_metadata_without_importing_full_toc(self) -> None:
        first = b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (First body) Tj ET"
        second = b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (Selected body) Tj ET"
        objects = [
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(first) + first + b"\nendstream",
            b"<< /Length %d >>\nstream\n" % len(second) + second + b"\nendstream",
            b"<< /Type /Page /Parent 6 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 1 0 R >> >> /Contents 2 0 R >>",
            b"<< /Type /Page /Parent 6 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 1 0 R >> >> /Contents 3 0 R >>",
            b"<< /Type /Pages /Kids [4 0 R 5 0 R] /Count 2 >>",
            b"<< /Type /Outlines /First 8 0 R /Last 9 0 R /Count 2 >>",
            b"<< /Title (Outline One) /Parent 7 0 R /Next 9 0 R /Dest [4 0 R /XYZ null null null] >>",
            b"<< /Title (Outline Two) /Parent 7 0 R /Prev 8 0 R /Dest [5 0 R /XYZ null null null] >>",
            b"<< /Type /Catalog /Pages 6 0 R /Outlines 7 0 R >>",
        ]
        result = convert(render_pdf(objects, 10), ConvertOptions(pages="2"))
        self.assertIn("outline", result.semantic.metadata)
        self.assertTrue(result.semantic.metadata["page_selection_active"])
        self.assertEqual(result.semantic.metadata["processed_pages"], [2])
        self.assertEqual(semantic_nodes(result, "toc"), [])
        self.assertEqual(result.markdown, "Selected body\n")
        self.assertNotIn("Outline One", result.html)
        self.assertNotIn("Outline Two", result.html)

    def test_page_selection_filters_document_global_acroform_fields(self) -> None:
        first = b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (First page) Tj ET"
        second = b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (Second page) Tj ET"
        objects = [
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(first) + first + b"\nendstream",
            b"<< /Length %d >>\nstream\n" % len(second) + second + b"\nendstream",
            b"<< /Type /Page /Parent 6 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 1 0 R >> >> /Contents 2 0 R /Annots [8 0 R] >>",
            b"<< /Type /Page /Parent 6 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 1 0 R >> >> /Contents 3 0 R /Annots [9 0 R] >>",
            b"<< /Type /Pages /Kids [4 0 R 5 0 R] /Count 2 >>",
            b"<< /Fields [8 0 R 9 0 R] >>",
            b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (FirstField) /V (Alpha) /Rect [72 650 240 675] /P 4 0 R >>",
            b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (SecondField) /V (Beta) /Rect [72 650 240 675] /P 5 0 R >>",
            b"<< /Type /Catalog /Pages 6 0 R /AcroForm 7 0 R >>",
        ]
        result = convert(render_pdf(objects, 10), ConvertOptions(pages="2"))
        fields = semantic_nodes(result, "form_field")
        self.assertEqual([field.attrs["name"] for field in fields], ["SecondField"])
        self.assertIn("**SecondField:** Beta", result.markdown)
        self.assertNotIn("FirstField", result.markdown)
        self.assertNotIn("Alpha", result.markdown)

    def test_page_selection_prunes_other_widgets_of_a_shared_field(self) -> None:
        first = b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (First page) Tj ET"
        second = b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (Second page) Tj ET"
        objects = [
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(first) + first + b"\nendstream",
            b"<< /Length %d >>\nstream\n" % len(second) + second + b"\nendstream",
            b"<< /Type /Page /Parent 6 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 1 0 R >> >> /Contents 2 0 R /Annots [8 0 R] >>",
            b"<< /Type /Page /Parent 6 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 1 0 R >> >> /Contents 3 0 R /Annots [9 0 R] >>",
            b"<< /Type /Pages /Kids [4 0 R 5 0 R] /Count 2 >>",
            b"<< /Fields [10 0 R] >>",
            b"<< /Type /Annot /Subtype /Widget /Parent 10 0 R /Rect [72 650 240 675] /P 4 0 R /DA (/F1 9 Tf 0 g) >>",
            b"<< /Type /Annot /Subtype /Widget /Parent 10 0 R /Rect [72 650 240 675] /P 5 0 R /DA (/F1 18 Tf 0 0 1 rg) >>",
            b"<< /FT /Tx /T (SharedField) /V (Gamma) /Kids [8 0 R 9 0 R] >>",
            b"<< /Type /Catalog /Pages 6 0 R /AcroForm 7 0 R >>",
        ]
        data = render_pdf(objects, 11)
        full_field = semantic_nodes(convert(data), "form_field")[0]
        self.assertIsNone(full_field.attrs["appearance"])
        result = convert(data, ConvertOptions(pages="2"))
        field = semantic_nodes(result, "form_field")[0]
        self.assertEqual(field.source_pages(), [2])
        self.assertEqual([widget["page"] for widget in field.attrs["widgets"]], [2])
        self.assertEqual(field.sources[0].object_refs, ("9 0 R",))
        self.assertEqual(field.attrs["appearance"]["font_size_pt"], 18.0)
        self.assertEqual(field.attrs["appearance"]["text_color_rgb"], [0.0, 0.0, 1.0])
        self.assertIn(
            'class="cocoapdf-form-field-value cocoapdf-form-field-value-evidenced"',
            result.html,
        )
        self.assertIn("**SharedField:** Gamma", result.markdown)


class NotesAndReferencesTests(unittest.TestCase):
    def test_footnote_definition_and_reference_are_paired(self) -> None:
        content = (
            b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (Body text [1]) Tj ET "
            b"0.5 w 72 110 m 300 110 l S "
            b"BT /F1 8 Tf 1 0 0 1 72 90 Tm (1. Footnote definition.) Tj ET"
        )
        result = convert(one_page_pdf(content))
        notes = semantic_nodes(result, "footnote")
        refs = semantic_nodes(result, "footnote_ref")
        self.assertEqual(len(notes), 1)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].attrs["target_id"], notes[0].id)
        self.assertIn("[^1]", result.markdown)
        self.assertIn("[^1]: Footnote definition.", result.markdown)
        self.assertIn('role="doc-footnote"', result.html)

    def test_reference_section_is_typed(self) -> None:
        content = (
            b"BT /F1 18 Tf 1 0 0 1 72 720 Tm (References) Tj ET "
            b"BT /F1 10 Tf 1 0 0 1 72 680 Tm ([1] Author. Title. 2024.) Tj ET"
        )
        result = convert(one_page_pdf(content))
        self.assertEqual(len(semantic_nodes(result, "reference_section")), 1)
        references = semantic_nodes(result, "reference")
        self.assertEqual(len(references), 1)
        self.assertEqual(references[0].attrs["label"], "1")
        self.assertIn("[1] Author. Title. 2024.", result.markdown)


class BidiConformanceTests(unittest.TestCase):
    def test_weak_neutral_numeric_and_bracket_resolution(self) -> None:
        self.assertEqual(reorder_text("abc (אבג) 123"), "abc (גבא) 123")
        self.assertEqual(reorder_text("אבג 123", "rtl"), "123 גבא")

    def test_explicit_embeddings_and_isolates_do_not_leak_controls(self) -> None:
        self.assertEqual(reorder_text("abc \u202bDEF\u202c ghi"), "abc DEF ghi")
        isolated = reorder_text("A\u2067אבג 123\u2069 Z")
        self.assertNotIn("\u2067", isolated)
        self.assertNotIn("\u2069", isolated)
        self.assertEqual(isolated, "A123 גבא Z")

    def test_token_provenance_survives_bidi_reordering(self) -> None:
        tokens = [
            {"text": "אבג", "glyph_ids": (1, 2, 3), "page": 1},
            {"text": " 123", "glyph_ids": (4, 5, 6, 7), "page": 1},
        ]
        reordered = reorder_tokens(tokens, "rtl")
        self.assertEqual("".join(token["text"] for token in reordered), "123 גבא")
        self.assertTrue(all(token.get("glyph_ids") for token in reordered if token["text"].strip()))


class TableGraphTests(unittest.TestCase):
    @staticmethod
    def _edge_column_rows(
        *,
        edge_text_by_row: tuple[str, ...] = ("", ""),
        edge_mcids: tuple[int, ...] = (),
        edge_colspan: int = 1,
        edge_attrs: Optional[dict] = None,
    ) -> list[SemanticNode]:
        rows: list[SemanticNode] = []
        for row_index, edge_text in enumerate(edge_text_by_row):
            y0 = 100.0 + row_index * 24.0
            y1 = y0 + 24.0
            edge_box = (100.0, y0, 110.0, y1)
            edge_source = SourceRef(page=1, bbox=edge_box, mcids=edge_mcids)
            edge_children = (
                [
                    SemanticNode(
                        "edge-text-%d" % row_index,
                        "text",
                        text=edge_text,
                        sources=[edge_source],
                    )
                ]
                if edge_text
                else []
            )
            edge_cell_attrs = {
                "row": row_index,
                "col": 0,
                "rowspan": 1,
                "colspan": edge_colspan,
                "role": "th" if row_index == 0 else "td",
                "bbox": edge_box,
                "rotation": 0,
            }
            edge_cell_attrs.update(edge_attrs or {})
            cells = [
                SemanticNode(
                    "edge-%d" % row_index,
                    "table_cell",
                    children=edge_children,
                    attrs=edge_cell_attrs,
                    evidence=[
                        Evidence(
                            "lattice_cell",
                            0.98,
                            page=1,
                            data={
                                "bbox": edge_box,
                                **(
                                    {"geometry_only_empty": True}
                                    if not edge_children
                                    else {}
                                ),
                            },
                        )
                    ],
                    sources=[edge_source],
                ),
                SemanticNode(
                    "middle-%d" % row_index,
                    "table_cell",
                    children=[
                        SemanticNode(
                            "middle-text-%d" % row_index,
                            "text",
                            text="Formula" if row_index == 0 else "x + y",
                            sources=[SourceRef(page=1, bbox=(110.0, y0, 300.0, y1))],
                        )
                    ],
                    attrs={
                        "row": row_index,
                        "col": edge_colspan,
                        "rowspan": 1,
                        "colspan": 1,
                        "role": "th" if row_index == 0 else "td",
                        "bbox": (110.0, y0, 300.0, y1),
                        "rotation": 0,
                    },
                    sources=[SourceRef(page=1, bbox=(110.0, y0, 300.0, y1))],
                ),
                SemanticNode(
                    "right-%d" % row_index,
                    "table_cell",
                    children=[
                        SemanticNode(
                            "right-text-%d" % row_index,
                            "text",
                            text="Meaning" if row_index == 0 else "sum",
                            sources=[SourceRef(page=1, bbox=(300.0, y0, 500.0, y1))],
                        )
                    ],
                    attrs={
                        "row": row_index,
                        "col": edge_colspan + 1,
                        "rowspan": 1,
                        "colspan": 1,
                        "role": "th" if row_index == 0 else "td",
                        "bbox": (300.0, y0, 500.0, y1),
                        "rotation": 0,
                    },
                    sources=[SourceRef(page=1, bbox=(300.0, y0, 500.0, y1))],
                ),
            ]
            rows.append(
                SemanticNode(
                    "row-%d" % row_index,
                    "table_row",
                    children=cells,
                    attrs={"row": row_index},
                    sources=[SourceRef(page=1, bbox=(110.0, y0, 500.0, y1))],
                )
            )
        return rows

    def test_geometry_only_narrow_edge_column_is_pruned_from_semantics_and_html(self) -> None:
        rows = self._edge_column_rows()
        retained_ids = [cell.id for row in rows for cell in row.children[1:]]
        bbox, records, removed_sources = _prune_vacuous_narrow_edge_columns(
            rows,
            (100.0, 100.0, 500.0, 148.0),
        )
        self.assertEqual(bbox, (110.0, 100.0, 500.0, 148.0))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["side"], "left")
        self.assertEqual(len(removed_sources), 2)
        self.assertEqual(
            [[cell.attrs["col"] for cell in row.children] for row in rows],
            [[0, 1], [0, 1]],
        )
        self.assertEqual(
            [cell.id for row in rows for cell in row.children],
            retained_ids,
        )
        self.assertTrue(all(row.sources[0].bbox[0] == 100.0 for row in rows))

        table = SemanticNode(
            "pruned-table",
            "table",
            children=rows,
            attrs={
                "bbox": bbox,
                "header_rows": 1,
                "column_count": 2,
                "pruned_vacuous_edge_columns": records,
            },
            sources=removed_sources,
        )
        rendered = render_semantic_html(SemanticDocument([table]))
        self.assertEqual(rendered.count("<th "), 2)
        self.assertEqual(rendered.count("<td "), 2)
        self.assertNotIn("<th></th>", rendered)
        self.assertNotIn("<td></td>", rendered)

    def test_narrow_edge_column_with_a_legitimate_empty_corner_is_preserved(self) -> None:
        rows = self._edge_column_rows(edge_text_by_row=("", "row label"))
        bbox, records, removed = _prune_vacuous_narrow_edge_columns(
            rows,
            (100.0, 100.0, 500.0, 148.0),
        )
        self.assertEqual(bbox, (100.0, 100.0, 500.0, 148.0))
        self.assertEqual(records, [])
        self.assertEqual(removed, [])
        self.assertTrue(all(len(row.children) == 3 for row in rows))

    def test_tagged_empty_edge_column_is_preserved(self) -> None:
        rows = self._edge_column_rows(
            edge_mcids=(7,),
            edge_attrs={"tagged_node_id": "struct-7"},
        )
        _bbox, records, removed = _prune_vacuous_narrow_edge_columns(
            rows,
            (100.0, 100.0, 500.0, 148.0),
        )
        self.assertEqual(records, [])
        self.assertEqual(removed, [])
        self.assertTrue(all(row.children[0].sources[0].mcids == (7,) for row in rows))

    def test_empty_edge_with_warning_or_extra_evidence_is_preserved(self) -> None:
        warned = self._edge_column_rows()
        warned[0].children[0].warnings.append("semantic warning")
        _bbox, records, _removed = _prune_vacuous_narrow_edge_columns(
            warned, (100.0, 100.0, 500.0, 148.0)
        )
        self.assertEqual(records, [])

        evidenced = self._edge_column_rows()
        evidenced[0].children[0].evidence.append(
            Evidence("unknown_semantic_signal", 0.8, page=1)
        )
        _bbox, records, _removed = _prune_vacuous_narrow_edge_columns(
            evidenced, (100.0, 100.0, 500.0, 148.0)
        )
        self.assertEqual(records, [])

    def test_empty_edge_with_glyph_region_or_object_provenance_is_preserved(self) -> None:
        for field, value in (
            ("glyph_ids", (7,)),
            ("region_ids", ("region-7",)),
            ("object_refs", ("7 0 R",)),
        ):
            rows = self._edge_column_rows()
            for row in rows:
                source = row.children[0].sources[0]
                row.children[0].sources = [
                    SourceRef(
                        page=source.page,
                        bbox=source.bbox,
                        **{field: value},
                    )
                ]
            _bbox, records, _removed = _prune_vacuous_narrow_edge_columns(
                rows, (100.0, 100.0, 500.0, 148.0)
            )
            self.assertEqual(records, [], field)

    def test_legacy_alignments_project_exactly_across_left_and_right_prunes(self) -> None:
        left = SemanticNode(
            "left-pruned",
            "table",
            attrs={
                "column_count": 2,
                "pruned_vacuous_edge_columns": [{"column": 0, "side": "left"}],
            },
        )
        self.assertEqual(
            _project_table_alignments(["right", "left", "center"], left),
            ["left", "center"],
        )
        right = SemanticNode(
            "right-pruned",
            "table",
            attrs={
                "column_count": 2,
                "pruned_vacuous_edge_columns": [{"column": 2, "side": "right"}],
            },
        )
        self.assertEqual(
            _project_table_alignments(["right", "left", "center"], right),
            ["right", "left"],
        )
        self.assertEqual(_project_table_alignments(["right"], left), [])

    def test_geometry_only_narrow_right_edge_column_is_pruned(self) -> None:
        rows = self._edge_column_rows()
        for row_index, row in enumerate(rows):
            y0 = 100.0 + row_index * 24.0
            y1 = y0 + 24.0
            edge, middle, right = row.children
            middle.attrs.update({"col": 0, "bbox": (100.0, y0, 300.0, y1)})
            right.attrs.update({"col": 1, "bbox": (300.0, y0, 490.0, y1)})
            edge_box = (490.0, y0, 500.0, y1)
            edge.attrs.update({"col": 2, "bbox": edge_box})
            edge.sources = [SourceRef(page=1, bbox=edge_box)]
            edge.evidence = [
                Evidence(
                    "lattice_cell",
                    0.98,
                    page=1,
                    data={"bbox": edge_box, "geometry_only_empty": True},
                )
            ]
            row.children = [middle, right, edge]
        bbox, records, _removed = _prune_vacuous_narrow_edge_columns(
            rows, (100.0, 100.0, 500.0, 148.0)
        )
        self.assertEqual(bbox, (100.0, 100.0, 490.0, 148.0))
        self.assertEqual([record["side"] for record in records], ["right"])

    def test_edge_pruning_respects_absolute_and_relative_width_thresholds(self) -> None:
        absolute = self._edge_column_rows()
        for row_index, row in enumerate(absolute):
            y0 = 100.0 + row_index * 24.0
            y1 = y0 + 24.0
            edge_box = (100.0, y0, 113.0, y1)
            edge = row.children[0]
            edge.attrs["bbox"] = edge_box
            edge.sources = [SourceRef(page=1, bbox=edge_box)]
            edge.evidence = [
                Evidence("lattice_cell", 0.98, page=1, data={
                    "bbox": edge_box, "geometry_only_empty": True
                })
            ]
            row.children[1].attrs["bbox"] = (113.0, y0, 300.0, y1)
        _bbox, records, _removed = _prune_vacuous_narrow_edge_columns(
            absolute, (100.0, 100.0, 500.0, 148.0)
        )
        self.assertEqual(records, [])

        relative = self._edge_column_rows()
        for row_index, row in enumerate(relative):
            y0 = 100.0 + row_index * 24.0
            y1 = y0 + 24.0
            row.children[1].attrs["bbox"] = (110.0, y0, 250.0, y1)
            row.children[2].attrs["bbox"] = (250.0, y0, 400.0, y1)
        _bbox, records, _removed = _prune_vacuous_narrow_edge_columns(
            relative, (100.0, 100.0, 400.0, 148.0)
        )
        self.assertEqual(records, [])

    def test_spanning_empty_edge_cells_are_preserved(self) -> None:
        rows = self._edge_column_rows(edge_colspan=2)
        _bbox, records, removed = _prune_vacuous_narrow_edge_columns(
            rows,
            (100.0, 100.0, 500.0, 148.0),
        )
        self.assertEqual(records, [])
        self.assertEqual(removed, [])
        self.assertTrue(all(row.children[0].attrs["colspan"] == 2 for row in rows))

    def test_ruled_table_has_cell_provenance(self) -> None:
        content = (
            b"0.5 w 72 500 m 300 500 l 72 540 m 300 540 l 72 580 m 300 580 l "
            b"72 500 m 72 580 l 186 500 m 186 580 l 300 500 m 300 580 l S "
            b"BT /F1 12 Tf 1 0 0 1 90 555 Tm (Name) Tj 1 0 0 1 205 555 Tm (Value) Tj "
            b"1 0 0 1 90 515 Tm (Alpha) Tj 1 0 0 1 205 515 Tm (10) Tj ET"
        )
        result = convert(one_page_pdf(content))
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.markdown)
        cells = semantic_nodes(result, "table_cell")
        self.assertEqual(len(cells), 4)
        self.assertTrue(all(cell.sources for cell in cells))
        self.assertIn("Name", result.markdown)
        self.assertIn("<table", result.html)


class TaggedReconciliationIntegrationTests(unittest.TestCase):
    @staticmethod
    def _tagged_paragraph_pdf(
        groups,
        *,
        validated: bool = True,
        within_gap: float = 12.0,
        boundary_gap: float = 16.0,
    ) -> bytes:
        operations = []
        owners = []
        y = 720.0
        mcid = 0
        for group_index, group in enumerate(groups):
            group_mcids = []
            for line_index, text in enumerate(group):
                operations.append(
                    b"/P <</MCID %d>> BDC " % mcid
                    + text_op(72, y, text, "F1", 10)
                    + b" EMC"
                )
                group_mcids.append(mcid)
                mcid += 1
                if line_index + 1 < len(group):
                    y -= within_gap
            owners.append(group_mcids)
            if group_index + 1 < len(groups):
                y -= boundary_gap

        content = b"\n".join(operations)
        owner_numbers = [5 + index for index in range(len(groups))]
        parent_tree_number = 5 + len(groups)
        root_number = parent_tree_number + 1
        catalog_number = root_number + 1
        objects = [
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Page /Parent 4 0 R /StructParents 0 /MediaBox [0 0 612 792] /Resources << /Font << /F1 1 0 R >> >> /Contents 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        ]
        for owner_number, group_mcids in zip(owner_numbers, owners):
            kids = b" ".join(str(value).encode("ascii") for value in group_mcids)
            objects.append(
                b"<< /Type /StructElem /S /P /P %d 0 R /Pg 3 0 R /K [%s] >>"
                % (root_number, kids)
            )
        parent_entries = []
        for owner_number, group_mcids in zip(owner_numbers, owners):
            parent_entries.extend([owner_number] * len(group_mcids))
        parent_refs = b" ".join(
            b"%d 0 R" % value for value in parent_entries
        )
        objects.append(
            b"<< /Nums [0 [%s]] >>" % parent_refs if validated else b"<< >>"
        )
        root = b"<< /Type /StructTreeRoot /K [%s]" % b" ".join(
            b"%d 0 R" % value for value in owner_numbers
        )
        if validated:
            root += b" /ParentTree %d 0 R" % parent_tree_number
        root += b" >>"
        objects.append(root)
        objects.append(
            b"<< /Type /Catalog /Pages 4 0 R /StructTreeRoot %d 0 R /MarkInfo << /Marked true >> >>"
            % root_number
        )
        return render_pdf(objects, catalog_number)

    def test_validated_sibling_paragraph_tags_recover_block_boundaries(self) -> None:
        groups = [
            ("Alpha paragraph begins", "and continues here."),
            ("Bravo paragraph begins", "and continues here."),
            ("Charlie paragraph begins", "and continues here."),
            ("Delta paragraph begins", "and continues here."),
        ]
        result = convert(self._tagged_paragraph_pdf(groups))
        paragraphs = semantic_nodes(result, "paragraph")
        self.assertEqual(len(paragraphs), 4, result.semantic.to_dict())
        self.assertEqual(
            [
                " ".join(
                    " ".join(
                        node.text or ""
                        for node in paragraph.walk()
                        if node.kind == "text"
                    ).split()
                )
                for paragraph in paragraphs
            ],
            [" ".join(group) for group in groups],
        )
        self.assertTrue(
            all("TAGGED_BLOCK_BOUNDARY_RECOVERED" in node.warnings for node in paragraphs)
        )
        self.assertTrue(
            all(
                any(item.kind == "tagged_block_boundary_geometry" for item in node.evidence)
                for node in paragraphs
            )
        )
        self.assertTrue(all(node.sources and node.sources[0].mcids for node in paragraphs))
        self.assertEqual(result.markdown.count("\n\n"), 3)
        self.assertEqual(result.html.count("<p "), 4)
        payload = json.loads(_format_payload(result, "json"))
        recovered = [
            node
            for node in payload["semantic_document"]["children"]
            if node["kind"] == "paragraph"
        ]
        self.assertEqual(len(recovered), 4)

    def test_unvalidated_sibling_tags_cannot_split_geometry(self) -> None:
        groups = [
            ("Alpha line one", "Alpha line two"),
            ("Bravo line one", "Bravo line two"),
            ("Charlie line one", "Charlie line two"),
            ("Delta line one", "Delta line two"),
        ]
        result = convert(self._tagged_paragraph_pdf(groups, validated=False))
        self.assertEqual(len(semantic_nodes(result, "paragraph")), 1, result.semantic.to_dict())
        self.assertFalse(
            any(
                "TAGGED_BLOCK_BOUNDARY_RECOVERED" in node.warnings
                for node in result.semantic.walk()
            )
        )

    def test_uniform_per_line_paragraph_tags_do_not_manufacture_boundaries(self) -> None:
        groups = [("Row %d remains one geometric paragraph" % index,) for index in range(1, 7)]
        result = convert(
            self._tagged_paragraph_pdf(
                groups,
                within_gap=12.0,
                boundary_gap=12.0,
            )
        )
        self.assertEqual(len(semantic_nodes(result, "paragraph")), 1, result.semantic.to_dict())
        self.assertFalse(
            any(
                "TAGGED_BLOCK_BOUNDARY_RECOVERED" in node.warnings
                for node in result.semantic.walk()
            )
        )

    def test_one_validated_paragraph_repairs_style_only_split(self) -> None:
        result = convert(
            self._tagged_paragraph_pdf(
                (("Parameters:", "x = 2 + 3"),),
                boundary_gap=12.0,
            )
        )
        paragraphs = semantic_nodes(result, "paragraph")
        self.assertEqual(len(paragraphs), 1, result.semantic.to_dict())
        self.assertIn("Parameters:", result.markdown)
        self.assertIn("x = 2 + 3", result.markdown)
        evidence = next(
            item
            for item in paragraphs[0].evidence
            if item.kind == "tagged_block_boundary_geometry"
        )
        self.assertEqual(evidence.data["action"], "merge")
        self.assertIn("TAGGED_BLOCK_BOUNDARY_RECOVERED", paragraphs[0].warnings)

    def test_coarse_single_paragraph_tag_does_not_merge_real_paragraphs(self) -> None:
        font = Font(name="F1", base_font="Helvetica")

        def line(text: str, y: float, mcid: int) -> Line:
            return Line(
                chars=[Char(
                    text=text,
                    x0=72.0,
                    y0=y,
                    x1=300.0,
                    y1=y + 10.0,
                    size=10.0,
                    font=font,
                    page=1,
                    seq=mcid + 1,
                    mc=({"tag": "P", "mcid": mcid},),
                )],
                page=1,
                seq=mcid + 1,
            )

        previous = SimpleNamespace(
            page=1,
            rank=1.0,
            kind="paragraph",
            lines=[line("The first paragraph ends with a complete sentence.", 72.0, 0)],
            attrs={},
            legacy_markdown="",
            semantic={},
        )
        current = SimpleNamespace(
            page=1,
            rank=2.0,
            kind="paragraph",
            lines=[line("The second paragraph begins a distinct subject.", 89.0, 1)],
            attrs={},
            legacy_markdown="",
            semantic={},
        )
        merged = _merge_same_owner_paragraph_events(
            SimpleNamespace(_render_paragraph=lambda lines: ""),
            previous,
            current,
            {"owner": {"parent_id": "root"}},
            {(1, 0): "owner", (1, 1): "owner"},
        )
        self.assertIsNone(merged)

    def test_rolemap_and_actualtext_drive_authoritative_heading(self) -> None:
        content = b"/Span <</MCID 0>> BDC BT /F1 12 Tf 1 0 0 1 72 720 Tm (Wrong) Tj ET EMC"
        actual = b"<FEFF0043006F00720072006500630074>"
        objects = [
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Page /Parent 4 0 R /StructParents 0 /MediaBox [0 0 612 792] /Resources << /Font << /F1 1 0 R >> >> /Contents 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /StructElem /S /CustomHead /P 7 0 R /Pg 3 0 R /K 0 /ActualText " + actual + b" >>",
            b"<< /Nums [0 [5 0 R]] >>",
            b"<< /Type /StructTreeRoot /K [5 0 R] /ParentTree 6 0 R /RoleMap << /CustomHead /H2 >> >>",
            b"<< /Type /Catalog /Pages 4 0 R /StructTreeRoot 7 0 R /MarkInfo << /Marked true >> >>",
        ]
        result = convert(render_pdf(objects, 8))
        self.assertEqual(result.markdown, "## Correct\n")
        self.assertEqual(result.semantic.metadata["tagged_pdf"]["role_map"], {"CustomHead": "H2"})
        heading = semantic_nodes(result, "heading")[0]
        self.assertEqual(heading.attrs["tag_role"], "H2")
        self.assertTrue(any(source.mcids == (0,) for source in heading.sources))

    def test_tagged_list_uses_item_pure_geometry_and_excludes_label_text(self) -> None:
        label = SemanticNode(
            id="label",
            kind="text",
            text="●",
            attrs={"tag_role": "Lbl", "mcid": 0},
            sources=[SourceRef(page=1, mcids=(0,))],
        )
        body = SemanticNode(
            id="body",
            kind="text",
            text="ملاعلاب ابحرم",
            attrs={"tag_role": "LBody", "mcid": 1},
            sources=[SourceRef(page=1, mcids=(1,))],
        )
        item = SemanticNode(
            id="item",
            kind="item",
            children=[label, body],
            sources=[SourceRef(page=1, mcids=(0, 1))],
        )
        tagged = SemanticNode(
            id="list",
            kind="list",
            children=[item],
            sources=[SourceRef(page=1, mcids=(0, 1))],
        )
        geometric = SemanticNode(
            id="paragraph",
            kind="paragraph",
            text="● مرحبا بالعالم",
            sources=[SourceRef(page=1, mcids=(1,), bbox=(72, 100, 300, 120))],
        )
        materialized = _tagged_list_node(tagged, {(1, 1): [geometric]})
        self.assertIsNotNone(materialized)
        assert materialized is not None
        self.assertEqual(
            materialized.children[0].children[0].text,
            "مرحبا بالعالم",
        )

    def test_tagged_list_rejects_geometry_shared_across_sibling_items(self) -> None:
        first_label = SemanticNode(
            id="first-label",
            kind="text",
            text="■",
            attrs={"tag_role": "Lbl", "mcid": 0},
            sources=[SourceRef(page=1, mcids=(0,))],
        )
        first_body = SemanticNode(
            id="first-body",
            kind="text",
            text="First",
            attrs={"tag_role": "LBody", "mcid": 1},
            sources=[SourceRef(page=1, mcids=(1,))],
        )
        second_label = SemanticNode(
            id="second-label",
            kind="text",
            text="■",
            attrs={"tag_role": "Lbl", "mcid": 2},
            sources=[SourceRef(page=1, mcids=(2,))],
        )
        second_body = SemanticNode(
            id="second-body",
            kind="text",
            text="Second",
            attrs={"tag_role": "LBody", "mcid": 3},
            sources=[SourceRef(page=1, mcids=(3,))],
        )
        tagged = SemanticNode(
            id="list",
            kind="list",
            children=[
                SemanticNode(id="first", kind="item", children=[first_label, first_body]),
                SemanticNode(id="second", kind="item", children=[second_label, second_body]),
            ],
        )
        shared = SemanticNode(
            id="shared-paragraph",
            kind="paragraph",
            text="■ First ■ Second",
            sources=[
                SourceRef(
                    page=1,
                    mcids=(0, 1, 2, 3),
                    bbox=(72, 100, 300, 150),
                )
            ],
        )
        materialized = _tagged_list_node(
            tagged,
            {(1, 1): [shared], (1, 3): [shared]},
        )
        self.assertIsNotNone(materialized)
        assert materialized is not None
        bodies = [item.children[0].text for item in materialized.children]
        self.assertEqual(bodies, ["First", "Second"])

    def test_tagged_bare_letter_label_does_not_strip_body_prefix(self) -> None:
        label = SemanticNode(
            id="label",
            kind="text",
            text="A",
            attrs={"tag_role": "Lbl", "mcid": 0},
            sources=[SourceRef(page=1, mcids=(0,))],
        )
        body = SemanticNode(
            id="body",
            kind="text",
            text="Apple",
            attrs={"tag_role": "LBody", "mcid": 1},
            sources=[SourceRef(page=1, mcids=(1,))],
        )
        tagged = SemanticNode(
            id="list",
            kind="list",
            children=[SemanticNode(id="item", kind="item", children=[label, body])],
        )
        geometric = SemanticNode(
            id="paragraph",
            kind="paragraph",
            text="Apple",
            sources=[SourceRef(page=1, mcids=(0, 1), bbox=(72, 100, 150, 120))],
        )
        materialized = _tagged_list_node(
            tagged,
            {(1, 1): [geometric]},
        )
        self.assertIsNotNone(materialized)
        assert materialized is not None
        self.assertEqual(materialized.children[0].children[0].text, "Apple")

    def test_tagged_list_actualtext_remains_authoritative(self) -> None:
        body = SemanticNode(
            id="body",
            kind="text",
            text="Authoritative replacement",
            attrs={"tag_role": "LBody", "mcid": 1, "actual_text": True},
            sources=[SourceRef(page=1, mcids=(1,))],
        )
        tagged = SemanticNode(
            id="list",
            kind="list",
            children=[SemanticNode(id="item", kind="item", children=[body])],
        )
        geometric = SemanticNode(
            id="paragraph",
            kind="paragraph",
            text="Geometry text",
            sources=[SourceRef(page=1, mcids=(1,), bbox=(72, 100, 150, 120))],
        )
        materialized = _tagged_list_node(
            tagged,
            {(1, 1): [geometric]},
        )
        self.assertIsNotNone(materialized)
        assert materialized is not None
        self.assertEqual(
            materialized.children[0].children[0].text,
            "Authoritative replacement",
        )

    def test_parenthesized_tagged_roman_label_keeps_exact_ordinal(self) -> None:
        source = [SourceRef(page=1, mcids=(0,))]
        label = SemanticNode(
            "roman-label",
            "text",
            text="(iv)",
            attrs={"tag_role": "Lbl"},
            sources=source,
        )
        body = SemanticNode(
            "roman-body",
            "text",
            text="Fourth",
            attrs={"tag_role": "LBody"},
            sources=source,
        )
        tagged = SemanticNode(
            "roman-list",
            "list",
            children=[
                SemanticNode(
                    "roman-item",
                    "item",
                    children=[label, body],
                    sources=source,
                )
            ],
            attrs={
                "structure_attributes": {
                    "ListNumbering": "LowerRoman",
                }
            },
            sources=source,
        )
        materialized = _tagged_list_node(tagged)
        self.assertIsNotNone(materialized)
        assert materialized is not None
        self.assertEqual(materialized.attrs["start"], 4)
        self.assertEqual(materialized.children[0].attrs["marker"], "iv")
        rendered = render_semantic_html(SemanticDocument([materialized]))
        self.assertIn('<ol type="i" start="4"', rendered)
        self.assertIn('<li value="4"', rendered)

    def test_tagged_list_materializes_without_visual_bullets(self) -> None:
        content = (
            b"/Span <</MCID 0>> BDC BT /F1 12 Tf 1 0 0 1 90 720 Tm (First) Tj ET EMC "
            b"/Span <</MCID 1>> BDC BT /F1 12 Tf 1 0 0 1 90 690 Tm (Second) Tj ET EMC"
        )
        objects = [
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Page /Parent 4 0 R /StructParents 0 /MediaBox [0 0 612 792] /Resources << /Font << /F1 1 0 R >> >> /Contents 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /StructElem /S /L /P 11 0 R /K [6 0 R 7 0 R] >>",
            b"<< /Type /StructElem /S /LI /P 5 0 R /K [8 0 R] >>",
            b"<< /Type /StructElem /S /LI /P 5 0 R /K [9 0 R] >>",
            b"<< /Type /StructElem /S /LBody /P 6 0 R /Pg 3 0 R /K 0 >>",
            b"<< /Type /StructElem /S /LBody /P 7 0 R /Pg 3 0 R /K 1 >>",
            b"<< /Nums [0 [8 0 R 9 0 R]] >>",
            b"<< /Type /StructTreeRoot /K [5 0 R] /ParentTree 10 0 R >>",
            b"<< /Type /Catalog /Pages 4 0 R /StructTreeRoot 11 0 R /MarkInfo << /Marked true >> >>",
        ]
        result = convert(render_pdf(objects, 12))
        self.assertEqual(result.markdown, "- First\n- Second\n")
        lists = semantic_nodes(result, "list")
        self.assertEqual(len(lists), 1)
        self.assertEqual(lists[0].attrs["tagged_node_id"], "tag-1")
        self.assertTrue(result.report["semantic_valid"], result.report["semantic_errors"])

    def test_tagged_list_numbering_and_layout_attributes_reach_html(self) -> None:
        content = (
            b"/Span <</MCID 0>> BDC BT /F1 12 Tf "
            b"1 0 0 1 90 720 Tm (First) Tj ET EMC "
            b"/Span <</MCID 1>> BDC BT /F1 12 Tf "
            b"1 0 0 1 90 690 Tm (Second) Tj ET EMC "
            b"/Span <</MCID 2>> BDC BT /F1 12 Tf "
            b"1 0 0 1 72 640 Tm (WWW) Tj ET EMC"
        )
        objects = [
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Page /Parent 4 0 R /StructParents 0 /MediaBox [0 0 612 792] /Resources << /Font << /F1 1 0 R >> >> /Contents 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /StructElem /S /L /P 13 0 R /A << /O /List /ListNumbering /UpperRoman >> /K [6 0 R 7 0 R] >>",
            b"<< /Type /StructElem /S /LI /P 5 0 R /K [8 0 R] >>",
            b"<< /Type /StructElem /S /LI /P 5 0 R /K [9 0 R] >>",
            b"<< /Type /StructElem /S /LBody /P 6 0 R /Pg 3 0 R /K 0 >>",
            b"<< /Type /StructElem /S /LBody /P 7 0 R /Pg 3 0 R /K 1 >>",
            b"<< /Type /StructElem /S /P /P 13 0 R /Pg 3 0 R /K 2 /E (World Wide Web) /A << /O /Layout /TextAlign /Center /WritingMode /RlTb /TextIndent 12 >> >>",
            b"<< /Nums [0 [8 0 R 9 0 R 10 0 R]] >>",
            b"<< >>",
            b"<< /Type /StructTreeRoot /K [5 0 R 10 0 R] /ParentTree 11 0 R >>",
            b"<< /Type /Catalog /Pages 4 0 R /StructTreeRoot 13 0 R /MarkInfo << /Marked true >> >>",
        ]
        result = convert(render_pdf(objects, 14))
        lists = semantic_nodes(result, "list")
        self.assertEqual(len(lists), 1)
        self.assertEqual(lists[0].attrs["marker_style"], "upper-roman")
        self.assertIn('<ol type="I"', result.html)
        self.assertIn(
            'style="text-align: center; text-indent: 12.000pt"',
            result.html,
        )
        self.assertIn('dir="rtl"', result.html)
        self.assertIn(
            '<abbr title="World Wide Web">WWW</abbr>',
            result.html,
        )
        self.assertTrue(
            result.report["semantic_valid"],
            result.report["semantic_errors"],
        )

    def test_tagged_table_preserves_colspan_and_cell_provenance(self) -> None:
        content = (
            b"/Span <</MCID 0>> BDC BT /F1 12 Tf 1 0 0 1 72 720 Tm (Header) Tj ET EMC "
            b"/Span <</MCID 1>> BDC BT /F1 12 Tf 1 0 0 1 72 680 Tm (Left) Tj ET EMC "
            b"/Span <</MCID 2>> BDC BT /F1 12 Tf 1 0 0 1 240 680 Tm (Right) Tj ET EMC"
        )
        objects = [
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Page /Parent 4 0 R /StructParents 0 /MediaBox [0 0 612 792] /Resources << /Font << /F1 1 0 R >> >> /Contents 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /StructElem /S /Table /P 13 0 R /K [6 0 R 7 0 R] >>",
            b"<< /Type /StructElem /S /TR /P 5 0 R /K [8 0 R] >>",
            b"<< /Type /StructElem /S /TR /P 5 0 R /K [9 0 R 10 0 R] >>",
            b"<< /Type /StructElem /S /TH /P 6 0 R /Pg 3 0 R /K 0 /ID (period) /A << /O /Table /ColSpan 2 /Scope /Column >> >>",
            b"<< /Type /StructElem /S /TD /P 7 0 R /Pg 3 0 R /K 1 /A << /O /Table /Headers [(period)] >> >>",
            b"<< /Type /StructElem /S /TD /P 7 0 R /Pg 3 0 R /K 2 >>",
            b"<< /Nums [0 [8 0 R 9 0 R 10 0 R]] >>",
            b"<< >>",
            b"<< /Type /StructTreeRoot /K [5 0 R] /ParentTree 11 0 R >>",
            b"<< /Type /Catalog /Pages 4 0 R /StructTreeRoot 13 0 R /MarkInfo << /Marked true >> >>",
        ]
        result = convert(render_pdf(objects, 14))
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.markdown)
        cells = semantic_nodes(result, "table_cell")
        self.assertEqual(len(cells), 3)
        self.assertEqual(cells[0].attrs["colspan"], 2)
        self.assertEqual(cells[0].attrs["scope"], "Column")
        self.assertEqual(cells[0].attrs["header_id"], "period")
        self.assertEqual(cells[1].attrs["headers"], ["period"])
        self.assertTrue(all(cell.sources and cell.sources[0].mcids for cell in cells))
        self.assertIn('colspan="2"', result.markdown)
        self.assertIn('colspan="2"', result.html)
        self.assertIn('scope="colgroup"', result.html)
        self.assertIn('id="period"', result.html)
        self.assertIn('headers="period"', result.html)

    def test_tagged_table_with_reversed_structure_rows_uses_physical_order(self) -> None:
        content = (
            b"/Span <</MCID 0>> BDC BT /F1 12 Tf 1 0 0 1 72 720 Tm (Header) Tj ET EMC "
            b"/Span <</MCID 1>> BDC BT /F1 12 Tf 1 0 0 1 72 680 Tm (Left) Tj ET EMC "
            b"/Span <</MCID 2>> BDC BT /F1 12 Tf 1 0 0 1 240 680 Tm (Right) Tj ET EMC"
        )
        objects = [
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Page /Parent 4 0 R /StructParents 0 /MediaBox [0 0 612 792] /Resources << /Font << /F1 1 0 R >> >> /Contents 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /StructElem /S /Table /P 13 0 R /K [7 0 R 6 0 R] >>",
            b"<< /Type /StructElem /S /TR /P 5 0 R /K [8 0 R] >>",
            b"<< /Type /StructElem /S /TR /P 5 0 R /K [9 0 R 10 0 R] >>",
            b"<< /Type /StructElem /S /TH /P 6 0 R /Pg 3 0 R /K 0 >>",
            b"<< /Type /StructElem /S /TD /P 7 0 R /Pg 3 0 R /K 1 >>",
            b"<< /Type /StructElem /S /TD /P 7 0 R /Pg 3 0 R /K 2 >>",
            b"<< /Nums [0 [8 0 R 9 0 R 10 0 R]] >>",
            b"<< >>",
            b"<< /Type /StructTreeRoot /K [5 0 R] /ParentTree 11 0 R >>",
            b"<< /Type /Catalog /Pages 4 0 R /StructTreeRoot 13 0 R /MarkInfo << /Marked true >> >>",
        ]
        result = convert(render_pdf(objects, 14))
        table = semantic_nodes(result, "table")[0]
        rows = [child for child in table.children if child.kind == "table_row"]
        self.assertEqual([node_text(row) for row in rows], ["Header", "LeftRight"])
        self.assertEqual([row.attrs["role"] for row in rows], ["header", "body"])
        self.assertIn("TAGGED_TABLE_ORDER_GEOMETRY_REPAIRED", table.warnings)
        order_evidence = next(
            evidence
            for evidence in table.evidence
            if evidence.kind == "tagged_table_geometry_order"
        )
        self.assertEqual(len(order_evidence.data["physical_row_ids"]), 2)
        self.assertNotEqual(
            order_evidence.data["original_row_ids"],
            order_evidence.data["physical_row_ids"],
        )
        self.assertTrue(all(cell.sources[0].mcids for cell in semantic_nodes(result, "table_cell")))
        self.assertLess(result.markdown.index("Header"), result.markdown.index("Left"))
        self.assertLess(result.html.index("Header"), result.html.index("Left"))
        self.assertTrue(result.report["semantic_valid"], result.report["semantic_errors"])

    def test_classmap_attribute_arrays_apply_table_spans_and_scope(self) -> None:
        content = (
            b"/Span <</MCID 0>> BDC BT /F1 12 Tf 1 0 0 1 72 720 Tm (Header) Tj ET EMC "
            b"/Span <</MCID 1>> BDC BT /F1 12 Tf 1 0 0 1 72 680 Tm (Left) Tj ET EMC "
            b"/Span <</MCID 2>> BDC BT /F1 12 Tf 1 0 0 1 240 680 Tm (Right) Tj ET EMC"
        )
        objects = [
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Page /Parent 4 0 R /StructParents 0 /MediaBox [0 0 612 792] /Resources << /Font << /F1 1 0 R >> >> /Contents 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /StructElem /S /Table /P 13 0 R /K [6 0 R 7 0 R] >>",
            b"<< /Type /StructElem /S /TR /P 5 0 R /K [8 0 R] >>",
            b"<< /Type /StructElem /S /TR /P 5 0 R /K [9 0 R 10 0 R] >>",
            b"<< /Type /StructElem /S /TH /P 6 0 R /Pg 3 0 R /K 0 /C [/Wide 2] >>",
            b"<< /Type /StructElem /S /TD /P 7 0 R /Pg 3 0 R /K 1 >>",
            b"<< /Type /StructElem /S /TD /P 7 0 R /Pg 3 0 R /K 2 >>",
            b"<< /Nums [0 [8 0 R 9 0 R 10 0 R]] >>",
            b"<< >>",
            b"<< /Type /StructTreeRoot /K [5 0 R] /ParentTree 11 0 R /ClassMap << /Wide [<< /O /Table /ColSpan 2 /Scope /Column >> 1] >> >>",
            b"<< /Type /Catalog /Pages 4 0 R /StructTreeRoot 13 0 R /MarkInfo << /Marked true >> >>",
        ]
        result = convert(render_pdf(objects, 14))
        cells = semantic_nodes(result, "table_cell")
        self.assertEqual(len(cells), 3)
        self.assertEqual(cells[0].attrs["colspan"], 2)
        self.assertEqual(cells[0].attrs["scope"], "Column")
        self.assertEqual(result.semantic.metadata["tagged_pdf"]["class_map_keys"], ["Wide"])

    def test_namespace_rolemap_resolves_custom_heading(self) -> None:
        content = b"/Span <</MCID 0>> BDC BT /F1 12 Tf 1 0 0 1 72 720 Tm (Namespaced) Tj ET EMC"
        objects = [
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Page /Parent 4 0 R /StructParents 0 /MediaBox [0 0 612 792] /Resources << /Font << /F1 1 0 R >> >> /Contents 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /StructElem /S /CustomHeading /NS 8 0 R /P 7 0 R /Pg 3 0 R /K 0 >>",
            b"<< /Nums [0 [5 0 R]] >>",
            b"<< /Type /StructTreeRoot /K [5 0 R] /ParentTree 6 0 R /Namespaces [8 0 R] >>",
            b"<< /Type /Namespace /NS (urn:cocoapdf:test) /RoleMap << /CustomHeading /H3 >> >>",
            b"<< /Type /Catalog /Pages 4 0 R /StructTreeRoot 7 0 R /MarkInfo << /Marked true >> >>",
        ]
        result = convert(render_pdf(objects, 9))
        self.assertEqual(result.markdown, "### Namespaced\n")
        headings = semantic_nodes(result, "heading")
        self.assertEqual(headings[0].attrs["tag_role"], "H3")
        self.assertEqual(headings[0].attrs["namespace"], "urn:cocoapdf:test")

    def test_tagged_figure_alt_is_applied_to_embedded_image(self) -> None:
        content = b"/Figure <</MCID 0>> BDC q 40 0 0 20 72 650 cm BI /W 1 /H 1 /CS /RGB /BPC 8 ID \xff\x00\x00 EI Q EMC"
        objects = [
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Page /Parent 4 0 R /StructParents 0 /MediaBox [0 0 612 792] /Resources << /Font << /F1 1 0 R >> >> /Contents 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /StructElem /S /Figure /P 7 0 R /Pg 3 0 R /K 0 /Alt (Red square) >>",
            b"<< /Nums [0 [5 0 R]] >>",
            b"<< /Type /StructTreeRoot /K [5 0 R] /ParentTree 6 0 R >>",
            b"<< /Type /Catalog /Pages 4 0 R /StructTreeRoot 7 0 R /MarkInfo << /Marked true >> >>",
        ]
        result = convert(render_pdf(objects, 8))
        images = semantic_nodes(result, "image")
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].attrs["alt"], "Red square")
        self.assertFalse(images[0].attrs["text_extraction_attempted"])
        self.assertIn('alt="Red square"', result.markdown)
        self.assertIn('alt="Red square"', result.html)

class AdvancedAcroFormTests(unittest.TestCase):
    def test_password_choice_and_signature_fields_preserve_semantics_safely(self) -> None:
        content = b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (Fields) Tj ET"
        data = one_page_pdf(
            content,
            page_extra=b"/Annots [6 0 R 7 0 R 8 0 R]",
            catalog_extra=b"/AcroForm 9 0 R",
            extra_objects=[
                b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (Password) /Ff 8192 /V (secret) /Rect [72 620 240 645] /P 3 0 R >>",
                b"<< /Type /Annot /Subtype /Widget /FT /Ch /T (Choice) /Ff 131072 /Opt [(a) [(b) (Bee)] (c)] /I [1] /V /b /Rect [72 580 240 605] /P 3 0 R >>",
                b"<< /Type /Annot /Subtype /Widget /FT /Sig /T (Approval) /V 10 0 R /Rect [72 540 240 565] /P 3 0 R >>",
                b"<< /Fields [6 0 R 7 0 R 8 0 R] /SigFlags 3 >>",
                b"<< /Type /Sig /Filter /Adobe.PPKLite /SubFilter /adbe.pkcs7.detached /Name (Signer) /M (D:20260101120000Z) /Reason (Approved) /ByteRange [0 100 200 50] /Contents <0102> >>",
            ],
        )
        result = convert(data)
        fields = {field.attrs["name"]: field for field in semantic_nodes(result, "form_field")}
        self.assertEqual(fields["Password"].attrs["value"], "[redacted]")
        self.assertTrue(fields["Password"].attrs["value_redacted"])
        self.assertNotIn("secret", result.markdown)
        self.assertEqual(fields["Choice"].attrs["value"], "Bee")
        self.assertEqual(fields["Choice"].attrs["selected_indices"], [1])
        signature = fields["Approval"].attrs["signature"]
        self.assertEqual(signature["signer_name"], "Signer")
        self.assertEqual(signature["byte_range"], [0, 100, 200, 50])
        self.assertFalse(signature["verified"])
        self.assertIn("Signature present", result.markdown)
        self.assertNotIn("<input", result.html)

    def test_inherited_parent_field_name_and_value(self) -> None:
        content = b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (Inherited) Tj ET"
        data = one_page_pdf(
            content,
            page_extra=b"/Annots [7 0 R]",
            catalog_extra=b"/AcroForm 6 0 R",
            extra_objects=[
                b"<< /Fields [8 0 R] >>",
                b"<< /Type /Annot /Subtype /Widget /Parent 8 0 R /Rect [72 620 240 645] /P 3 0 R >>",
                b"<< /FT /Tx /T (Person) /V (Alice) /Kids [7 0 R] >>",
            ],
        )
        field = semantic_nodes(convert(data), "form_field")[0]
        self.assertEqual(field.attrs["name"], "Person")
        self.assertEqual(field.attrs["value"], "Alice")
        self.assertEqual(field.sources[0].page, 1)


class AdvancedNavigationAndNotesTests(unittest.TestCase):
    def test_outline_named_destination_resolves_to_heading_anchor(self) -> None:
        content = b"BT /F1 18 Tf 1 0 0 1 72 720 Tm (Introduction) Tj ET"
        data = one_page_pdf(
            content,
            catalog_extra=b"/Outlines 6 0 R /Names << /Dests 8 0 R >>",
            extra_objects=[
                b"<< /Type /Outlines /First 7 0 R /Last 7 0 R /Count 1 >>",
                b"<< /Title (Introduction) /Parent 6 0 R /Dest /intro >>",
                b"<< /Names [(intro) [3 0 R /XYZ null null null]] >>",
            ],
        )
        result = convert(data)
        items = semantic_nodes(result, "toc_item")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].attrs["page"], 1)
        self.assertTrue(items[0].attrs["target_id"])
        self.assertIn("](#introduction)", result.markdown)

    def test_endnote_superscript_reference_and_definition_are_paired(self) -> None:
        content = (
            b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (Claim) Tj /F1 8 Tf 4 Ts (1) Tj 0 Ts ET "
            b"BT /F1 18 Tf 1 0 0 1 72 640 Tm (Endnotes) Tj ET "
            b"BT /F1 10 Tf 1 0 0 1 72 600 Tm (1. Supporting endnote.) Tj ET"
        )
        result = convert(one_page_pdf(content))
        notes = semantic_nodes(result, "footnote")
        refs = semantic_nodes(result, "footnote_ref")
        self.assertEqual(len(notes), 1, result.semantic.to_dict())
        self.assertEqual(notes[0].attrs["note_type"], "endnote")
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].attrs["target_id"], notes[0].id)
        self.assertIn("[^1]", result.markdown)

    def test_numeric_citation_links_to_typed_reference(self) -> None:
        content = (
            b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (Prior work [1] is relevant.) Tj ET "
            b"BT /F1 18 Tf 1 0 0 1 72 640 Tm (References) Tj ET "
            b"BT /F1 10 Tf 1 0 0 1 72 600 Tm ([1] Author. Title. 2024.) Tj ET"
        )
        result = convert(one_page_pdf(content))
        crossrefs = semantic_nodes(result, "cross_reference")
        references = semantic_nodes(result, "reference")
        self.assertEqual(len(references), 1)
        self.assertTrue(any(node.attrs.get("target_id") == references[0].id for node in crossrefs))
        self.assertIn("#ref-1", result.markdown)

    def test_titled_reference_suffix_requires_consecutive_numbered_entries(self) -> None:
        content = b" ".join([
            text_op(72, 700, "Appendix Z - Audited source references", "F1", 18),
            text_op(72, 650, "[4] First source. 2021.", "F1", 10),
            text_op(72, 625, "[5] Second source. 2022.", "F1", 10),
            text_op(72, 600, "[8] Separate numbered note.", "F1", 10),
        ])
        result = convert(one_page_pdf(content))
        sections = semantic_nodes(result, "reference_section")
        references = semantic_nodes(result, "reference")
        self.assertEqual(len(sections), 1, result.semantic.to_dict())
        self.assertEqual([node.attrs["label"] for node in references], ["4", "5"])
        self.assertIn("Separate numbered note", result.markdown)
        self.assertTrue(all(node.sources for node in references))
        self.assertTrue(
            all(any(item.kind == "reference_entry" for item in node.evidence) for node in references)
        )

    def test_cross_references_heading_does_not_capture_ordinary_prose(self) -> None:
        content = b" ".join([
            text_op(72, 700, "Cross references", "F1", 18),
            text_op(72, 650, "See the following sections for context.", "F1", 10),
            text_op(72, 625, "No bibliography begins here.", "F1", 10),
        ])
        result = convert(one_page_pdf(content))
        self.assertEqual(semantic_nodes(result, "reference_section"), [])
        self.assertEqual(semantic_nodes(result, "reference"), [])
        self.assertIn("See the following sections", result.markdown)

    def test_canonical_references_keep_numeric_and_author_year_entries(self) -> None:
        factory = NodeFactory()
        document = SemanticDocument([
            factory.make("heading", text="References", attrs={"level": 2}),
            factory.make("paragraph", text="[1] Rivera, A. Reliable systems. 2024."),
            factory.make("paragraph", text="Chen, B. (2023). Deterministic parsing."),
            factory.make("paragraph", text="Patel, C. (2022). Semantic fidelity."),
        ])
        enrich_notes_references_crossrefs(document, factory)
        references = [node for node in document.walk() if node.kind == "reference"]
        self.assertEqual(len(references), 3)
        self.assertEqual([node.attrs["label"] for node in references], ["1", None, None])
        self.assertEqual(references[1].attrs["style"], "author_year")
        self.assertIn("Deterministic parsing", node_text(references[1]))
        rendered = render_semantic_html(document)
        self.assertNotIn('id="None"', rendered)
        self.assertEqual(rendered.count('role="doc-biblioentry"'), 3)

    def test_navigation_reference_headings_reject_numbered_citation_like_rows(self) -> None:
        for title in ("Cross references", "Figure references", "Section references"):
            with self.subTest(title=title):
                content = b" ".join([
                    text_op(72, 700, title, "F1", 18),
                    text_op(72, 650, "[1] Rivera, A. Linked topic. 2024.", "F1", 10),
                    text_op(72, 625, "[2] Chen, B. Related topic. 2023.", "F1", 10),
                ])
                result = convert(one_page_pdf(content))
                self.assertEqual(semantic_nodes(result, "reference_section"), [])
                self.assertEqual(semantic_nodes(result, "reference"), [])

    def test_noncanonical_source_words_need_bibliographic_entry_evidence(self) -> None:
        for title in ("Configuring data sources", "Resources"):
            with self.subTest(title=title):
                content = b" ".join([
                    text_op(72, 700, title, "F1", 18),
                    text_op(72, 650, "[1] Open the configuration panel.", "F1", 10),
                    text_op(72, 625, "[2] Select the available input.", "F1", 10),
                ])
                result = convert(one_page_pdf(content))
                self.assertEqual(semantic_nodes(result, "reference_section"), [])
                self.assertEqual(semantic_nodes(result, "reference"), [])

    def test_reference_runs_preserve_anchor_and_page_break_sentinels(self) -> None:
        factory = NodeFactory()
        first_source = [SourceRef(page=1, glyph_ids=(1,), bbox=(72, 100, 300, 112))]
        second_source = [SourceRef(page=2, glyph_ids=(2,), bbox=(72, 80, 300, 92))]
        heading = factory.make("heading", text="References", attrs={"level": 2})
        first = factory.make(
            "paragraph",
            text="[1] Rivera, A. First source. 2024.",
            sources=first_source,
        )
        anchor = factory.make("anchor", attrs={"name": "page-two"})
        page_break = factory.make("page_break", attrs={"page": 2})
        second = factory.make(
            "paragraph",
            text="[2] Chen, B. Second source. 2023.",
            sources=second_source,
        )
        document = SemanticDocument([heading, first, anchor, page_break, second])
        enrich_notes_references_crossrefs(document, factory)
        self.assertEqual(
            [node.kind for node in document.children],
            ["heading", "reference_section", "anchor", "page_break", "reference_section"],
        )
        sections = [node for node in document.children if node.kind == "reference_section"]
        self.assertEqual([node.attrs.get("continuation_index", 0) for node in sections], [0, 1])
        self.assertEqual(sections[0].children[0].sources[0].glyph_ids, (1,))
        self.assertEqual(sections[1].children[0].sources[0].glyph_ids, (2,))

    def test_structural_references_retain_boundary_recovery_provenance(self) -> None:
        factory = NodeFactory()
        heading = factory.make(
            "heading",
            text="Appendix Z - Audited source references",
            attrs={"level": 2},
        )
        candidates = []
        for index, label in enumerate((4, 5, 8)):
            source = SourceRef(
                page=1,
                glyph_ids=(100 + index,),
                mcids=(index,),
                bbox=(72.0, 100.0 + index * 20.0, 300.0, 112.0 + index * 20.0),
            )
            candidates.append(factory.make(
                "paragraph",
                text="[%d] Authored source entry %d." % (label, label),
                evidence=[Evidence(
                    "tagged_block_boundary_geometry",
                    0.97,
                    page=1,
                    data={"action": "split"},
                )],
                sources=[source],
                warnings=["TAGGED_BLOCK_BOUNDARY_RECOVERED"],
            ))
        document = SemanticDocument([heading, *candidates])
        enrich_notes_references_crossrefs(document, factory)
        references = [node for node in document.walk() if node.kind == "reference"]
        self.assertEqual([node.attrs["label"] for node in references], ["4", "5"])
        self.assertTrue(
            all("TAGGED_BLOCK_BOUNDARY_RECOVERED" in node.warnings for node in references)
        )
        self.assertTrue(
            all(
                {"reference_entry", "tagged_block_boundary_geometry"}
                <= {item.kind for item in node.evidence}
                for node in references
            )
        )
        self.assertEqual(
            [node.sources[0].glyph_ids for node in references],
            [(100,), (101,)],
        )
        self.assertEqual(document.children[-1].kind, "paragraph")
        self.assertIn("[8]", document.children[-1].text or "")


class AdvancedTableTests(unittest.TestCase):
    def test_borderless_spreadsheet_grid_uses_native_row_and_column_labels(self) -> None:
        content = b" ".join([
            b"BT /F1 9 Tf",
            b"1 0 0 1 130 720 Tm (A) Tj 1 0 0 1 230 720 Tm (B) Tj 1 0 0 1 330 720 Tm (C) Tj",
            b"1 0 0 1 60 695 Tm (1) Tj 1 0 0 1 110 695 Tm (time) Tj "
            b"1 0 0 1 210 695 Tm (observed) Tj 1 0 0 1 310 695 Tm (forecast) Tj",
            b"1 0 0 1 60 670 Tm (2) Tj 1 0 0 1 110 670 Tm (0) Tj 1 0 0 1 210 670 Tm (13) Tj",
            b"1 0 0 1 60 650 Tm (3) Tj 1 0 0 1 110 650 Tm (1) Tj 1 0 0 1 210 650 Tm (12) Tj",
            b"1 0 0 1 60 630 Tm (4) Tj 1 0 0 1 110 630 Tm (2) Tj 1 0 0 1 210 630 Tm (13.5) Tj",
            b"1 0 0 1 60 610 Tm (5) Tj 1 0 0 1 110 610 Tm (3) Tj 1 0 0 1 310 610 Tm (15.2) Tj",
            b"1 0 0 1 60 590 Tm (6) Tj 1 0 0 1 110 590 Tm (4) Tj 1 0 0 1 310 590 Tm (16.4) Tj",
            b"1 0 0 1 60 570 Tm (7) Tj 1 0 0 1 110 570 Tm (5) Tj 1 0 0 1 310 570 Tm (18.0) Tj ET",
        ])
        result = convert(one_page_pdf(content))
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.semantic.to_dict())
        self.assertEqual(tables[0].attrs["row_count"], 8)
        self.assertEqual(tables[0].attrs["column_count"], 4)
        self.assertEqual(tables[0].attrs["header_rows"], 0)
        self.assertTrue(any(item.kind == "spreadsheet_grid" for item in tables[0].evidence))
        self.assertIn("<tr><td></td><td>A</td><td>B</td><td>C</td></tr>", result.markdown)
        self.assertIn("<tr><td>5</td><td>3</td><td></td><td>15.2</td></tr>", result.markdown)

    def test_spreadsheet_like_labels_with_numbered_prose_are_not_a_table(self) -> None:
        content = b" ".join([
            b"BT /F1 9 Tf",
            b"1 0 0 1 130 720 Tm (A) Tj 1 0 0 1 230 720 Tm (B) Tj 1 0 0 1 330 720 Tm (C) Tj",
            b"1 0 0 1 60 695 Tm (1) Tj 1 0 0 1 110 695 Tm (Topic) Tj "
            b"1 0 0 1 210 695 Tm (Summary) Tj 1 0 0 1 310 695 Tm (Note) Tj",
            b"1 0 0 1 60 670 Tm (2) Tj 1 0 0 1 110 670 Tm (First) Tj 1 0 0 1 210 670 Tm (10) Tj",
            b"1 0 0 1 60 650 Tm (3) Tj 1 0 0 1 110 650 Tm (Second) Tj 1 0 0 1 210 650 Tm (20) Tj",
            b"1 0 0 1 60 630 Tm (4) Tj 1 0 0 1 110 630 Tm (Third) Tj 1 0 0 1 210 630 Tm (30) Tj",
            b"1 0 0 1 60 610 Tm (5) Tj 1 0 0 1 110 610 Tm (Fourth) Tj 1 0 0 1 210 610 Tm (40) Tj",
            b"1 0 0 1 60 590 Tm (6) Tj 1 0 0 1 110 590 Tm (Fifth) Tj 1 0 0 1 210 590 Tm (50) Tj",
            b"1 0 0 1 60 570 Tm (7) Tj 1 0 0 1 110 570 Tm (Sixth) Tj 1 0 0 1 210 570 Tm (60) Tj ET",
        ])
        result = convert(one_page_pdf(content))
        self.assertEqual(semantic_nodes(result, "table"), [])
        self.assertNotIn("<table>", result.markdown)
        self.assertIn("First", result.markdown)

    def test_captioned_borderless_measurements_use_stable_body_anchors(self) -> None:
        content = b" ".join([
            b"BT /F1 10 Tf",
            b"1 0 0 1 72 720 Tm (Table 4. Mixture volumes used for a controlled) Tj",
            b"1 0 0 1 72 704 Tm (comparison.) Tj",
            b"/F2 10 Tf 1 0 0 1 90 686 Tm (Sample) Tj "
            b"1 0 0 1 200 686 Tm (Water) Tj "
            b"1 0 0 1 310 686 Tm (Sugar Solution) Tj "
            b"1 0 0 1 430 686 Tm (Culture) Tj",
            b"/F1 10 Tf 1 0 0 1 72 666 Tm (A) Tj "
            b"1 0 0 1 195 666 Tm (*8 ml) Tj "
            b"1 0 0 1 310 666 Tm (*6 ml) Tj "
            b"1 0 0 1 430 666 Tm (0 ml) Tj",
            b"1 0 0 1 72 646 Tm (B) Tj "
            b"1 0 0 1 195 646 Tm (*12 ml) Tj "
            b"1 0 0 1 310 646 Tm (0 ml) Tj "
            b"1 0 0 1 430 646 Tm (*2 ml) Tj",
            b"1 0 0 1 72 626 Tm (C) Tj "
            b"1 0 0 1 195 626 Tm (*6 ml) Tj "
            b"1 0 0 1 310 626 Tm (*6 ml) Tj "
            b"1 0 0 1 430 626 Tm (*2 ml) Tj ET",
        ])
        resources = b"/Resources << /Font << /F1 1 0 R /F2 2 0 R >> >>"
        objects = [
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Page /Parent 5 0 R /MediaBox [0 0 612 792] " + resources + b" /Contents 3 0 R >>",
            b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
            b"<< /Type /Catalog /Pages 5 0 R >>",
        ]
        result = convert(render_pdf(objects, 6))
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.semantic.to_dict())
        self.assertEqual(tables[0].attrs["row_count"], 4)
        self.assertEqual(tables[0].attrs["column_count"], 4)
        self.assertEqual(tables[0].attrs["header_rows"], 1)
        self.assertTrue(
            any(
                item.kind == "captioned_measurement_grid"
                for item in tables[0].evidence
            )
        )
        self.assertIn("Table 4. Mixture volumes used for a controlled comparison.", result.markdown)
        self.assertIn("<td>*8 ml</td>", result.markdown)
        self.assertIn('<th scope="col">Sugar Solution</th>', result.html)
        cells = semantic_nodes(result, "table_cell")
        self.assertTrue(all(cell.sources for cell in cells))
        self.assertTrue(
            any(
                source.glyph_ids
                for cell in cells
                for source in cell.sources
            )
        )

    def test_captioned_bold_prose_columns_are_not_measurement_table(self) -> None:
        content = b" ".join([
            b"BT /F1 10 Tf 1 0 0 1 72 720 Tm (Table 5. Discussion prompts for review) Tj",
            b"/F2 10 Tf 1 0 0 1 90 690 Tm (Topic) Tj "
            b"1 0 0 1 220 690 Tm (Observation) Tj "
            b"1 0 0 1 390 690 Tm (Response) Tj",
            b"/F1 10 Tf 1 0 0 1 72 670 Tm (First) Tj "
            b"1 0 0 1 220 670 Tm (steady growth) Tj "
            b"1 0 0 1 390 670 Tm (review later) Tj",
            b"1 0 0 1 72 650 Tm (Second) Tj "
            b"1 0 0 1 220 650 Tm (mixed evidence) Tj "
            b"1 0 0 1 390 650 Tm (needs context) Tj",
            b"1 0 0 1 72 630 Tm (Third) Tj "
            b"1 0 0 1 220 630 Tm (open question) Tj "
            b"1 0 0 1 390 630 Tm (discuss next) Tj ET",
        ])
        resources = b"/Resources << /Font << /F1 1 0 R /F2 2 0 R >> >>"
        objects = [
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Page /Parent 5 0 R /MediaBox [0 0 612 792] " + resources + b" /Contents 3 0 R >>",
            b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
            b"<< /Type /Catalog /Pages 5 0 R >>",
        ]
        result = convert(render_pdf(objects, 6))
        self.assertEqual(semantic_nodes(result, "table"), [])
        self.assertNotIn("<table>", result.markdown)
        self.assertIn("mixed evidence", result.markdown)

    def test_captioned_tiered_numeric_grid_uses_body_anchors_and_header_tiers(self) -> None:
        content = b" ".join([
            b"BT /F1 10 Tf",
            b"1 0 0 1 72 740 Tm (Table 21. Observed and expected returns for a) Tj",
            b"1 0 0 1 72 724 Tm (controlled period.) Tj",
            b"1 0 0 1 72 680 Tm (Year) Tj "
            b"1 0 0 1 220 680 Tm (Observed returns) Tj "
            b"1 0 0 1 380 680 Tm (Forecast) Tj",
            b"1 0 0 1 220 674 Tm (over time) Tj",
            b"1 0 0 1 72 650 Tm (Period) Tj "
            b"1 0 0 1 220 650 Tm (Actual Value) Tj "
            b"1 0 0 1 380 650 Tm (Expected Value) Tj",
            b"1 0 0 1 72 620 Tm (2022) Tj "
            b"1 0 0 1 220 620 Tm (10%) Tj "
            b"1 0 0 1 380 620 Tm (8%) Tj",
            b"1 0 0 1 72 600 Tm (2023) Tj "
            b"1 0 0 1 220 600 Tm (7%) Tj "
            b"1 0 0 1 380 600 Tm (9%) Tj",
            b"1 0 0 1 72 580 Tm (2024) Tj "
            b"1 0 0 1 220 580 Tm (6%) Tj "
            b"1 0 0 1 380 580 Tm (5%) Tj ET",
        ])
        result = convert(one_page_pdf(content))
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.semantic.to_dict())
        table = tables[0]
        self.assertEqual(table.attrs["row_count"], 5)
        self.assertEqual(table.attrs["column_count"], 3)
        self.assertEqual(table.attrs["header_rows"], 2)
        evidence = next(
            item
            for item in table.evidence
            if item.kind == "tiered_numeric_grid"
        )
        self.assertEqual(evidence.data["admission"], "explicit_caption")
        self.assertEqual(evidence.data["body_rows"], 3)
        self.assertEqual(evidence.data["header_rows"], 2)
        self.assertEqual(evidence.data["stable_column_anchors"], 3)
        self.assertIn(
            "Table 21. Observed and expected returns for a controlled period.",
            result.markdown,
        )
        self.assertIn("<td>Observed returns<br />over time</td>", result.markdown)
        self.assertIn("<caption ", result.html)
        cells = semantic_nodes(result, "table_cell")
        observed = next(
            cell
            for cell in cells
            if cell.attrs["row"] == 0 and cell.attrs["col"] == 1
        )
        observed_text = " ".join(
            node.text or ""
            for node in observed.walk()
            if node.kind == "text"
        )
        self.assertIn("Observed returns", observed_text)
        self.assertIn("over time", observed_text)
        self.assertTrue(observed.sources)
        self.assertTrue(any(source.glyph_ids for source in observed.sources))

    def test_page_top_numeric_continuation_requires_complete_stable_rows(self) -> None:
        content = b" ".join([
            b"BT /F1 10 Tf",
            b"1 0 0 1 72 740 Tm (Slope Gradient) Tj "
            b"1 0 0 1 220 740 Tm (Maximum Length) Tj "
            b"1 0 0 1 380 740 Tm (P Value) Tj",
            b"1 0 0 1 72 715 Tm (1 - 2) Tj "
            b"1 0 0 1 220 715 Tm (400) Tj "
            b"1 0 0 1 380 715 Tm (0.6) Tj",
            b"1 0 0 1 72 695 Tm (3 - 5) Tj "
            b"1 0 0 1 220 695 Tm (300) Tj "
            b"1 0 0 1 380 695 Tm (0.5) Tj",
            b"1 0 0 1 72 675 Tm (6 - 8) Tj "
            b"1 0 0 1 220 675 Tm (200) Tj "
            b"1 0 0 1 380 675 Tm (0.5) Tj ET",
        ])
        result = convert(one_page_pdf(content))
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.semantic.to_dict())
        table = tables[0]
        self.assertEqual(table.attrs["row_count"], 4)
        self.assertEqual(table.attrs["column_count"], 3)
        self.assertEqual(table.attrs["header_rows"], 1)
        evidence = next(
            item
            for item in table.evidence
            if item.kind == "tiered_numeric_grid"
        )
        self.assertEqual(evidence.data["admission"], "page_top_continuation")
        self.assertIn("<td>1 - 2</td>", result.markdown)

    def test_midpage_numeric_cards_without_caption_are_not_a_table(self) -> None:
        content = b" ".join([
            b"BT /F1 10 Tf",
            b"1 0 0 1 72 500 Tm (Card) Tj "
            b"1 0 0 1 220 500 Tm (Current) Tj "
            b"1 0 0 1 380 500 Tm (Target) Tj",
            b"1 0 0 1 72 470 Tm (1) Tj "
            b"1 0 0 1 220 470 Tm (10%) Tj "
            b"1 0 0 1 380 470 Tm (12%) Tj",
            b"1 0 0 1 72 450 Tm (2) Tj "
            b"1 0 0 1 220 450 Tm (11%) Tj "
            b"1 0 0 1 380 450 Tm (13%) Tj",
            b"1 0 0 1 72 430 Tm (3) Tj "
            b"1 0 0 1 220 430 Tm (12%) Tj "
            b"1 0 0 1 380 430 Tm (14%) Tj ET",
        ])
        result = convert(one_page_pdf(content))
        self.assertEqual(semantic_nodes(result, "table"), [])
        self.assertNotIn("<table>", result.markdown)
        self.assertIn("Current", result.markdown)

    def test_captioned_aligned_form_values_are_not_a_numeric_grid(self) -> None:
        content = b" ".join([
            b"BT /F1 10 Tf",
            b"1 0 0 1 72 740 Tm (Table 22. Review assignments for the period.) Tj",
            b"1 0 0 1 72 690 Tm (Item) Tj "
            b"1 0 0 1 220 690 Tm (Owner) Tj "
            b"1 0 0 1 380 690 Tm (Status) Tj",
            b"1 0 0 1 220 686 Tm (and reviewer) Tj",
            b"1 0 0 1 72 660 Tm (1) Tj "
            b"1 0 0 1 220 660 Tm (Alice) Tj "
            b"1 0 0 1 380 660 Tm (Open) Tj",
            b"1 0 0 1 72 640 Tm (2) Tj "
            b"1 0 0 1 220 640 Tm (Bob) Tj "
            b"1 0 0 1 380 640 Tm (Review) Tj",
            b"1 0 0 1 72 620 Tm (3) Tj "
            b"1 0 0 1 220 620 Tm (Carol) Tj "
            b"1 0 0 1 380 620 Tm (Closed) Tj ET",
        ])
        result = convert(one_page_pdf(content))
        self.assertEqual(semantic_nodes(result, "table"), [])
        self.assertNotIn("<table>", result.markdown)
        self.assertIn("Alice", result.markdown)

    def test_artifact_filled_lattice_recovers_wrapped_cells_with_provenance(self) -> None:
        content = b" ".join([
            b"/Artifact BMC 0 g",
            b"72 292 0.5 400 re f 170 292 0.5 400 re f "
            b"240 292 0.5 400 re f 400 292 0.5 400 re f "
            b"520 292 0.5 400 re f",
            b"72 692 448 0.5 re f 72 632 448 0.5 re f "
            b"72 532 448 0.5 re f 72 432 448 0.5 re f "
            b"72 292 448 0.5 re f EMC",
            b"BT /F2 10 Tf 1 0 0 1 82 670 Tm (Source) Tj "
            b"1 0 0 1 180 670 Tm (Year) Tj "
            b"1 0 0 1 250 670 Tm (Description) Tj "
            b"1 0 0 1 410 670 Tm (Outcome) Tj",
            b"/F1 10 Tf 1 0 0 1 82 610 Tm (Alpha) Tj "
            b"1 0 0 1 180 610 Tm (2022) Tj "
            b"1 0 0 1 250 610 Tm (First wrapped sentence) Tj "
            b"1 0 0 1 410 610 Tm (Useful) Tj",
            b"1 0 0 1 250 590 Tm (continues inside the cell.) Tj "
            b"1 0 0 1 410 590 Tm (impact.) Tj",
            b"1 0 0 1 82 510 Tm (Bravo) Tj "
            b"1 0 0 1 180 510 Tm (2023) Tj "
            b"1 0 0 1 250 510 Tm (Another long description) Tj "
            b"1 0 0 1 410 510 Tm (Measured) Tj",
            b"1 0 0 1 250 490 Tm (wraps on its own baseline.) Tj "
            b"1 0 0 1 410 490 Tm (result.) Tj",
            b"1 0 0 1 82 410 Tm (Charlie) Tj "
            b"1 0 0 1 180 410 Tm (2024) Tj "
            b"1 0 0 1 250 410 Tm (Final multi-line account) Tj "
            b"1 0 0 1 410 410 Tm (Lasting) Tj",
            b"1 0 0 1 250 390 Tm (remains horizontal text.) Tj "
            b"1 0 0 1 410 390 Tm (benefit.) Tj ET",
        ])
        resources = b"/Resources << /Font << /F1 1 0 R /F2 2 0 R >> >>"
        objects = [
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Page /Parent 5 0 R /MediaBox [0 0 612 792] " + resources + b" /Contents 3 0 R >>",
            b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
            b"<< /Type /Catalog /Pages 5 0 R >>",
        ]
        result = convert(render_pdf(objects, 6))
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.semantic.to_dict())
        table = tables[0]
        self.assertEqual(table.attrs["row_count"], 4)
        self.assertEqual(table.attrs["column_count"], 4)
        evidence = next(
            item
            for item in table.evidence
            if item.kind == "artifact_filled_lattice"
        )
        self.assertTrue(evidence.data["artifact_geometry_only"])
        self.assertTrue(evidence.data["complete_edge_coverage"])
        self.assertEqual(evidence.data["artifact_rule_rectangles"], 10)
        cells = semantic_nodes(result, "table_cell")
        wrapped = next(
            cell
            for cell in cells
            if cell.attrs["row"] == 1 and cell.attrs["col"] == 2
        )
        wrapped_text = " ".join(
            node.text or ""
            for node in wrapped.walk()
            if node.kind == "text"
        )
        self.assertIn("First wrapped sentence", wrapped_text)
        self.assertIn("continues inside the cell.", wrapped_text)
        self.assertTrue(wrapped.sources)
        self.assertTrue(any(source.glyph_ids for source in wrapped.sources))
        self.assertNotIn("writing-mode: vertical-rl", result.html)
        self.assertNotIn("artifact", wrapped_text.casefold())

    @staticmethod
    def _continuous_header_lattice(mode: str = "positive") -> bytes:
        operations = [b"/Artifact BMC"]
        row_bands = [(630.5, 69.0), (580.5, 49.0), (530.5, 49.0), (480.5, 49.0)]
        for x in (72.0, 540.0):
            for bottom, height in row_bands:
                operations.append(rect_fill_op(x, bottom, 0.5, height, 0.0))
        internal_bands = (
            [row_bands[0], row_bands[1], row_bands[3]]
            if mode == "body"
            else row_bands[1:]
        )
        for x in (230.0, 390.0):
            for bottom, height in internal_bands:
                operations.append(rect_fill_op(x, bottom, 0.5, height, 0.0))
        for y in (700.0, 630.0, 580.0, 530.0, 480.0):
            operations.append(rect_fill_op(72.0, y, 468.0, 0.5, 0.0))
        operations.append(b"EMC")

        if mode in {"positive", "single", "body"}:
            operations.append(b"/Span <</MCID 7>> BDC")
            if mode == "body":
                operations.extend([
                    text_op(82, 682, "Field", font="F2", size=10),
                    text_op(240, 682, "Policy", font="F2", size=10),
                    text_op(400, 682, "Status", font="F2", size=10),
                ])
            elif mode == "single":
                operations.extend([
                    text_op(82, 682, "Unified policy governs every supported acquisition validation review and publication stage", font="F2", size=10),
                    text_op(82, 664, "Left", font="F2", size=10),
                    text_op(250, 664, "Middle", font="F2", size=10),
                    text_op(410, 664, "Right", font="F2", size=10),
                    text_op(82, 646, "Short", font="F2", size=10),
                ])
            else:
                operations.extend([
                    text_op(82, 682, "Unified policy governs every supported acquisition validation review and publication stage", font="F2", size=10),
                    text_op(82, 664, "Continuous guidance applies across acquisition calibration analysis reporting and review", font="F2", size=10),
                    text_op(82, 646, "One semantic heading remains continuous across the complete inherited table grid", font="F2", size=10),
                ])
            operations.append(b"EMC")
        elif mode == "separated":
            operations.append(b"/Span <</MCID 7>> BDC")
            for y, suffix in ((682, "A"), (652, "B")):
                operations.extend([
                    text_op(82, y, "Left " + suffix, font="F2", size=10),
                    text_op(250, y, "Middle " + suffix, font="F2", size=10),
                    text_op(410, y, "Right " + suffix, font="F2", size=10),
                ])
            operations.append(b"EMC")
        elif mode == "distinct_mcids":
            for y in (682, 664, 646):
                for mcid, x, text in (
                    (7, 82, "Continuous first segment"),
                    (8, 225, "continues through center"),
                    (9, 375, "and reaches the far edge"),
                ):
                    operations.extend([
                        ("/Span <</MCID %d>> BDC" % mcid).encode("ascii"),
                        text_op(x, y, text, font="F2", size=10),
                        b"EMC",
                    ])

        body_rows = [
            (605, "Alpha", "Measured", "Ready"),
            (555, "Bravo", "Validated", "Review"),
            (505, "Charlie", "Published", "Done"),
        ]
        if mode == "body":
            operations.append(b"/Span <</MCID 11>> BDC")
            operations.extend([
                text_op(82, 568, "Continuous body prose crosses every false partition throughout the inherited grid", size=10),
                text_op(82, 552, "but a populated body row cannot manufacture a semantic colspan from prose", size=10),
                text_op(82, 536, "without verified header ownership and repeated structural evidence", size=10),
            ])
            operations.append(b"EMC")
            body_rows = [body_rows[0], body_rows[2]]
        for y, left, middle, right in body_rows:
            operations.extend([
                text_op(82, y, left, size=10),
                text_op(240, y, middle, size=10),
                text_op(400, y, right, size=10),
            ])
        return b"\n".join(operations)

    def test_fragmented_lattice_explicit_cell_roles_override_boldness(self) -> None:
        renderer = object.__new__(MarkdownRenderer)

        def marked_line(role: str, *, bold: bool) -> Line:
            font = Font(
                name="F1",
                base_font="Helvetica-Bold" if bold else "Helvetica",
            )
            char = Char(
                text="Cell",
                x0=10.0,
                y0=5.0,
                x1=34.0,
                y1=15.0,
                size=10.0,
                font=font,
                page=1,
                seq=1,
                mc=({"tag": role, "mcid": 4},),
            )
            return Line(chars=[char], page=1, seq=1)

        self.assertEqual(
            renderer._artifact_lattice_header_rows(
                [marked_line("TD", bold=True)],
                [0.0, 50.0],
                [0.0, 20.0, 40.0],
            ),
            0,
        )
        self.assertEqual(
            renderer._artifact_lattice_header_rows(
                [marked_line("TH", bold=False)],
                [0.0, 50.0],
                [0.0, 20.0, 40.0],
            ),
            1,
        )

    def test_fragmented_colspan_veto_recognizes_repeated_column_anchors(self) -> None:
        font = Font(name="F1", base_font="Helvetica-Bold")

        def anchored_line(y: float, drift: float = 0.0) -> Line:
            chars = [
                Char("Left", 12.0, y, 36.0, y + 10.0, 10.0, font, 1, int(y) + 1),
                Char("Middle", 104.0 + drift, y, 146.0 + drift, y + 10.0, 10.0, font, 1, int(y) + 2),
                Char("Right", 204.0 - drift, y, 234.0 - drift, y + 10.0, 10.0, font, 1, int(y) + 3),
            ]
            return Line(chars=chars, page=1, seq=int(y) + 1)

        stable = [anchored_line(y) for y in (5.0, 17.0, 29.0)]
        self.assertTrue(MarkdownRenderer._artifact_header_has_stable_column_labels(
            stable,
            [0.0, 100.0, 200.0, 300.0],
            10.0,
            2,
        ))
        drifting = [
            anchored_line(5.0, 0.0),
            anchored_line(17.0, 8.0),
            anchored_line(29.0, -8.0),
        ]
        self.assertFalse(MarkdownRenderer._artifact_header_has_stable_column_labels(
            drifting,
            [0.0, 100.0, 200.0, 300.0],
            10.0,
            2,
        ))

    def test_fragmented_lattice_recovers_continuous_marked_header_colspan(self) -> None:
        result = convert(make_pdf([self._continuous_header_lattice()]))
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.semantic.to_dict())
        table = tables[0]
        self.assertEqual(table.attrs["column_count"], 3)
        self.assertEqual(table.attrs["header_rows"], 1)
        header_cells = [
            cell
            for cell in semantic_nodes(result, "table_cell")
            if cell.attrs["row"] == 0
        ]
        self.assertEqual(len(header_cells), 1)
        self.assertEqual(header_cells[0].attrs["colspan"], 3)
        self.assertTrue(any(source.glyph_ids for source in header_cells[0].sources))
        self.assertEqual(
            {mcid for source in header_cells[0].sources for mcid in source.mcids},
            {7},
        )
        span_evidence = next(
            item
            for item in header_cells[0].evidence
            if item.kind == "artifact_continuous_text_colspan"
        )
        self.assertEqual(span_evidence.data["colspan"], 3)
        table_evidence = next(
            item for item in table.evidence
            if item.kind == "artifact_fragmented_lattice"
        )
        recovered = table_evidence.data["continuous_text_colspans"]
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["shared_marked_identity"]["mcid"], 7)
        self.assertTrue(all(count >= 2 for count in recovered[0]["crossing_line_counts"].values()))
        self.assertIn('<th colspan="3">', result.markdown)
        self.assertIn('<th colspan="3"', result.html)
        self.assertEqual(result.markdown.count("Unified policy governs"), 1)
        self.assertNotIn("Artifact", result.markdown)

    def test_fragmented_lattice_keeps_gutter_separated_header_cells(self) -> None:
        result = convert(make_pdf([self._continuous_header_lattice("separated")]))
        table = semantic_nodes(result, "table")[0]
        header_cells = [
            cell for cell in semantic_nodes(result, "table_cell")
            if cell.attrs["row"] == 0
        ]
        self.assertEqual(len(header_cells), 3)
        self.assertTrue(all(cell.attrs["colspan"] == 1 for cell in header_cells))
        evidence = next(
            item for item in table.evidence
            if item.kind == "artifact_fragmented_lattice"
        )
        self.assertEqual(evidence.data["continuous_text_colspans"], [])

    def test_fragmented_lattice_requires_one_marked_header_identity(self) -> None:
        result = convert(make_pdf([self._continuous_header_lattice("distinct_mcids")]))
        header_cells = [
            cell for cell in semantic_nodes(result, "table_cell")
            if cell.attrs["row"] == 0
        ]
        self.assertEqual(len(header_cells), 3)
        self.assertTrue(all(cell.attrs["colspan"] == 1 for cell in header_cells))

    def test_fragmented_lattice_requires_repeated_boundary_crossings(self) -> None:
        result = convert(make_pdf([self._continuous_header_lattice("single")]))
        header_cells = [
            cell for cell in semantic_nodes(result, "table_cell")
            if cell.attrs["row"] == 0
        ]
        self.assertEqual(len(header_cells), 3)
        self.assertTrue(all(cell.attrs["colspan"] == 1 for cell in header_cells))

    def test_fragmented_lattice_does_not_span_populated_body_prose(self) -> None:
        result = convert(make_pdf([self._continuous_header_lattice("body")]))
        cells = semantic_nodes(result, "table_cell")
        self.assertFalse(any(cell.attrs["colspan"] > 1 for cell in cells))

    def test_fragmented_artifact_lattice_spans_only_empty_neighbours(self) -> None:
        content = b" ".join([
            b"/Artifact BMC 0 g",
            # Per-band vertical rectangles reproduce the fragmented border
            # dialect emitted by office-document producers.
            b"72 450.5 0.5 49 re f 72 500.5 0.5 49 re f "
            b"72 550.5 0.5 49 re f 72 600.5 0.5 49 re f "
            b"72 650.5 0.5 49 re f",
            b"260 450.5 0.5 49 re f 260 500.5 0.5 49 re f "
            b"260 550.5 0.5 49 re f 260 600.5 0.5 49 re f "
            b"260 650.5 0.5 49 re f",
            b"540 450.5 0.5 49 re f 540 500.5 0.5 49 re f "
            b"540 550.5 0.5 49 re f 540 600.5 0.5 49 re f "
            b"540 650.5 0.5 49 re f",
            b"72 700 468 0.5 re f 72 650 468 0.5 re f ",
            b"260 600 280 0.5 re f 72 550 468 0.5 re f ",
            b"260 500 280 0.5 re f 72 450 468 0.5 re f EMC",
            b"BT /F2 10 Tf 1 0 0 1 82 675 Tm (Area) Tj "
            b"1 0 0 1 270 675 Tm (Competence) Tj",
            b"/F1 10 Tf 1 0 0 1 82 625 Tm (Group A) Tj "
            b"1 0 0 1 270 625 Tm (A1) Tj",
            b"1 0 0 1 82 575 Tm (Separate A) Tj "
            b"1 0 0 1 270 575 Tm (A2) Tj",
            b"1 0 0 1 82 525 Tm (Group B) Tj "
            b"1 0 0 1 270 525 Tm (B1) Tj",
            # This wrapped baseline straddles the detector's two-point row
            # tolerance and therefore belongs to the originating cell.
            b"1 0 0 1 82 496 Tm (continues here) Tj",
            b"1 0 0 1 270 475 Tm (B2) Tj ET",
        ])
        resources = b"/Resources << /Font << /F1 1 0 R /F2 2 0 R >> >>"
        objects = [
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Page /Parent 5 0 R /MediaBox [0 0 612 792] " + resources + b" /Contents 3 0 R >>",
            b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
            b"<< /Type /Catalog /Pages 5 0 R >>",
        ]
        result = convert(render_pdf(objects, 6))
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.markdown)
        self.assertEqual(tables[0].attrs["row_count"], 5)
        self.assertEqual(tables[0].attrs["column_count"], 2)
        self.assertEqual(tables[0].attrs["header_rows"], 1)
        evidence = next(
            item
            for item in tables[0].evidence
            if item.kind == "artifact_fragmented_lattice"
        )
        self.assertEqual(evidence.data["physical_spans"], 1)
        cells = {
            (cell.attrs["row"], cell.attrs["col"]): cell
            for cell in semantic_nodes(result, "table_cell")
        }
        self.assertEqual(cells[(1, 0)].attrs["rowspan"], 1)
        self.assertEqual(cells[(2, 0)].attrs["rowspan"], 1)
        group_a = "".join(
            node.text or ""
            for node in cells[(1, 0)].walk()
            if node.kind == "text"
        )
        separate_a = "".join(
            node.text or ""
            for node in cells[(2, 0)].walk()
            if node.kind == "text"
        )
        self.assertEqual(group_a, "Group A")
        self.assertEqual(separate_a, "Separate A")
        self.assertTrue(any(source.glyph_ids for source in cells[(1, 0)].sources))
        self.assertTrue(any(source.glyph_ids for source in cells[(2, 0)].sources))
        self.assertNotIn('rowspan="2">Group A', result.markdown)
        self.assertIn("<td>Separate A</td>", result.markdown)
        self.assertIn(
            '<td rowspan="2">Group B<br />continues here</td>',
            result.markdown,
        )
        self.assertNotIn("Artifact", result.markdown)

    def test_fragmented_artifact_lattice_infers_clipped_page_edge(self) -> None:
        content = b" ".join([
            b"/Artifact BMC 0 g",
            b"72 450.5 0.5 49 re f 72 500.5 0.5 49 re f "
            b"72 550.5 0.5 49 re f 72 600.5 0.5 49 re f "
            b"72 650.5 0.5 49 re f",
            b"240 450.5 0.5 49 re f 240 500.5 0.5 49 re f "
            b"240 550.5 0.5 49 re f 240 600.5 0.5 49 re f "
            b"240 650.5 0.5 49 re f",
            b"410 450.5 0.5 49 re f 410 500.5 0.5 49 re f "
            b"410 550.5 0.5 49 re f 410 600.5 0.5 49 re f",
            b"72 700 540 0.5 re f 72 650 540 0.5 re f "
            b"72 600 540 0.5 re f 72 550 540 0.5 re f "
            b"72 500 540 0.5 re f 72 450 540 0.5 re f EMC",
            b"BT /F1 10 Tf 1 0 0 1 250 675 Tm (Current) Tj "
            b"1 0 0 1 420 675 Tm (Target) Tj",
            b"1 0 0 1 82 625 Tm (Alpha) Tj",
            b"1 0 0 1 82 575 Tm (Bravo) Tj",
            b"1 0 0 1 82 525 Tm (Charlie) Tj",
            b"1 0 0 1 82 475 Tm (Delta) Tj ET",
        ])
        result = convert(one_page_pdf(content))
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.markdown)
        self.assertEqual(tables[0].attrs["row_count"], 5)
        self.assertEqual(tables[0].attrs["column_count"], 3)
        evidence = next(
            item
            for item in tables[0].evidence
            if item.kind == "artifact_fragmented_lattice"
        )
        self.assertEqual(evidence.data["inferred_outer_boundaries"], 1)
        self.assertIn("<tr><td>Alpha</td><td></td><td></td></tr>", result.markdown)

    def test_captioned_sparse_two_column_table_preserves_blank_response_cells(self) -> None:
        parts = [
            text_op(60, 720, "Table 9. Observation record.", font="F2", size=14),
            text_op(60, 670, "Field", font="F2", size=8),
            text_op(140, 670, "Recorded observation value", font="F2", size=8),
        ]
        for text, y in zip(("Aster", "Birch", "Cedar", "Dogwood", "Elm"), (650, 630, 610, 590, 570)):
            parts.append(text_op(60, y, text, size=8))
        result = convert(make_pdf([b" ".join(parts)]))
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.markdown)
        self.assertEqual(tables[0].attrs["row_count"], 6)
        self.assertEqual(tables[0].attrs["column_count"], 2)
        self.assertEqual(tables[0].attrs["header_rows"], 1)
        evidence = next(
            item
            for item in tables[0].evidence
            if item.kind == "captioned_sparse_two_column"
        )
        self.assertTrue(evidence.data["blank_response_column"])
        self.assertEqual(evidence.data["right_column_rows"], 0)
        headers = [
            cell for cell in semantic_nodes(result, "table_cell")
            if cell.attrs["row"] == 0
        ]
        self.assertEqual([cell.attrs["role"] for cell in headers], ["th", "th"])
        self.assertTrue(all(any(source.glyph_ids for source in cell.sources) for cell in headers))
        self.assertIn("<thead>", result.markdown)
        self.assertIn("<thead>", result.html)
        self.assertIn('<th scope="col"', result.html)
        self.assertIn("<tr><td>Aster</td><td></td></tr>", result.markdown)
        self.assertIn("<caption>Table 9. Observation record.</caption>", result.markdown)

    def test_sparse_two_column_run_without_caption_remains_prose(self) -> None:
        parts = [
            text_op(60, 670, "Field", font="F2", size=8),
            text_op(140, 670, "Recorded observation value", font="F2", size=8),
        ]
        for text, y in zip(("Aster", "Birch", "Cedar", "Dogwood", "Elm"), (650, 630, 610, 590, 570)):
            parts.append(text_op(60, y, text, size=8))
        result = convert(make_pdf([b" ".join(parts)]))
        self.assertFalse(any(
            item.kind == "captioned_sparse_two_column"
            for table in semantic_nodes(result, "table")
            for item in table.evidence
        ))

    def test_dense_complete_two_by_two_artifact_table_is_recovered(self) -> None:
        content = b" ".join([
            b"/Artifact BMC 0 g "
            b"72 300 0.5 200 re f 300 300 0.5 200 re f 540 300 0.5 200 re f "
            b"72 500 468 0.5 re f 72 460 468 0.5 re f 72 300 468 0.5 re f EMC",
            b"BT /F2 10 Tf 1 0 0 1 82 475 Tm (Materials) Tj "
            b"1 0 0 1 310 475 Tm (Equipment) Tj",
            b"/F1 10 Tf 1 0 0 1 82 430 Tm (Prepared sample and buffered solution for each station) Tj "
            b"1 0 0 1 310 430 Tm (Calibrated reader and labelled storage rack) Tj",
            b"1 0 0 1 82 400 Tm (Reference standard and control solution for comparison) Tj "
            b"1 0 0 1 310 400 Tm (Sterile pipettes and protective laboratory trays) Tj",
            b"1 0 0 1 82 370 Tm (Additional reagent and distilled water for all groups) Tj "
            b"1 0 0 1 310 370 Tm (Permanent markers and temperature controlled bath) Tj ET",
        ])
        resources = b"/Resources << /Font << /F1 1 0 R /F2 2 0 R >> >>"
        objects = [
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Page /Parent 5 0 R /MediaBox [0 0 612 792] " + resources + b" /Contents 3 0 R >>",
            b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
            b"<< /Type /Catalog /Pages 5 0 R >>",
        ]
        result = convert(render_pdf(objects, 6))
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.semantic.to_dict())
        self.assertEqual(tables[0].attrs["row_count"], 2)
        self.assertEqual(tables[0].attrs["column_count"], 2)
        evidence = next(
            item for item in tables[0].evidence
            if item.kind == "artifact_filled_lattice"
        )
        self.assertTrue(evidence.data["dense_complete_two_by_two"])
        self.assertIn("<th scope=\"col\">Materials</th>", result.html)
        self.assertTrue(any(source.glyph_ids for source in tables[0].sources))

    def test_two_by_two_artifact_cards_and_sparse_forms_are_not_tables(self) -> None:
        grid = (
            b"/Artifact BMC 0 g "
            b"72 300 0.5 200 re f 300 300 0.5 200 re f 540 300 0.5 200 re f "
            b"72 500 468 0.5 re f 72 460 468 0.5 re f 72 300 468 0.5 re f EMC "
        )
        cases = {
            "sparse_form": (
                b"BT /F1 10 Tf 1 0 0 1 82 475 Tm (Name) Tj "
                b"1 0 0 1 310 475 Tm (Value) Tj "
                b"1 0 0 1 82 420 Tm (Yes) Tj "
                b"1 0 0 1 310 420 Tm (No) Tj ET"
            ),
            "dense_card_without_header_evidence": (
                b"BT /F1 10 Tf 1 0 0 1 82 475 Tm (Left panel) Tj "
                b"1 0 0 1 310 475 Tm (Right panel) Tj "
                b"1 0 0 1 82 430 Tm (Ordinary explanatory sentence continues in this card) Tj "
                b"1 0 0 1 310 430 Tm (Another explanatory sentence continues in this card) Tj "
                b"1 0 0 1 82 400 Tm (More prose remains in the left presentation panel) Tj "
                b"1 0 0 1 310 400 Tm (More prose remains in the right presentation panel) Tj "
                b"1 0 0 1 82 370 Tm (Final ordinary sentence is not structured table data) Tj "
                b"1 0 0 1 310 370 Tm (Final ordinary sentence is not structured table data) Tj ET"
            ),
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                result = convert(one_page_pdf(grid + text))
                self.assertEqual(semantic_nodes(result, "table"), [])
                self.assertNotIn("<table>", result.markdown)

    def test_closed_artifact_path_outlines_feed_complete_lattice_only(self) -> None:
        content = b" ".join([
            b"/Artifact BMC 0 g",
            b"72 433 m 500 433 l 499.25 432.25 l 72.75 432.25 l h f",
            b"72 366 m 500 366 l 499.25 365.25 l 72.75 365.25 l h f",
            b"215 500 m 215 300 l 214.25 300.75 l 214.25 499.25 l h f",
            b"360 500 m 360 300 l 359.25 300.75 l 359.25 499.25 l h f",
            b"72 300 m 72 500 l 500 500 l 500 300 l 72 300 l "
            b"72.75 300.75 l 499.25 300.75 l 499.25 499.25 l "
            b"72.75 499.25 l 72.75 300.75 l h f EMC",
            b"/TH << /MCID 1 >> BDC BT /F1 10 Tf 1 0 0 1 82 470 Tm (Name) Tj ET EMC "
            b"/TH << /MCID 2 >> BDC BT /F1 10 Tf 1 0 0 1 225 470 Tm (Year) Tj ET EMC "
            b"/TH << /MCID 3 >> BDC BT /F1 10 Tf 1 0 0 1 370 470 Tm (Status) Tj ET EMC",
            b"BT /F1 10 Tf 1 0 0 1 82 405 Tm (Alpha) Tj "
            b"1 0 0 1 225 405 Tm (2024) Tj 1 0 0 1 370 405 Tm (Open) Tj "
            b"1 0 0 1 82 338 Tm (Bravo) Tj "
            b"1 0 0 1 225 338 Tm (2025) Tj 1 0 0 1 370 338 Tm (Closed) Tj ET",
        ])
        converter = Converter(one_page_pdf(content))
        result = converter.convert()
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.semantic.to_dict())
        self.assertEqual(tables[0].attrs["row_count"], 3)
        self.assertEqual(tables[0].attrs["column_count"], 3)
        self.assertEqual(len(converter._artifact_rule_segments), 8)
        evidence = next(
            item for item in tables[0].evidence
            if item.kind == "artifact_filled_lattice"
        )
        self.assertTrue(evidence.data["complete_edge_coverage"])
        self.assertIn("<th scope=\"col\">Name</th>", result.html)

    def test_artifact_path_normalization_rejects_non_rule_shapes(self) -> None:
        cases = {
            "open": b"72 600 m 500 600 l 499.25 599.25 l 72.75 599.25 l f",
            "curved": b"72 600 m 200 602 350 598 500 600 c h f",
            "rotated": b"72 600 m 400 500 l 400.75 500.75 l 72.75 600.75 l h f",
            "multiple_subpaths": (
                b"72 600 m 500 600 l 499.25 599.25 l 72.75 599.25 l h "
                b"72 500 m 500 500 l 499.25 499.25 l 72.75 499.25 l h f"
            ),
            "non_thin_card": b"72 600 m 300 600 l 300 500 l 72 500 l h f",
        }
        for name, path in cases.items():
            with self.subTest(name=name):
                converter = Converter(
                    one_page_pdf(b"/Artifact BMC 0 g " + path + b" EMC")
                )
                converter.convert()
                self.assertEqual(converter._artifact_rule_segments, [])

    def test_local_artifact_backgrounds_respect_paint_order(self) -> None:
        content = b" ".join([
            b"/Artifact BMC 1 g 72 650 220 36 re f EMC",
            b"/Artifact BMC 0 g 72 650 220 36 re f EMC",
            b"BT /F1 12 Tf 1 g 1 0 0 1 82 662 Tm (Visible on latest dark paint) Tj ET",
        ])
        result = convert(one_page_pdf(content))
        self.assertIn("Visible on latest dark paint", result.markdown)
        self.assertNotIn(
            "INVISIBLE_TEXT",
            {warning.code for warning in result.warnings},
        )

    def test_evenodd_artifact_frame_does_not_become_solid_background(self) -> None:
        content = b" ".join([
            b"/Artifact BMC 0.93 g 72 600 220 60 re f* EMC",
            b"/Artifact BMC 0 g 72 600 220 60 re 73 601 218 58 re f* EMC",
            b"BT /F1 12 Tf 0 g 1 0 0 1 82 624 Tm (Visible inside frame) Tj ET",
        ])
        converter = Converter(one_page_pdf(content))
        result = converter.convert()
        self.assertIn("Visible inside frame", result.markdown)
        self.assertEqual(len(converter._artifact_local_backgrounds), 1)
        self.assertNotIn(
            "INVISIBLE_TEXT",
            {warning.code for warning in result.warnings},
        )

    def test_evenodd_page_frame_does_not_become_page_background(self) -> None:
        content = b" ".join([
            b"/Artifact BMC 0 g 0 0 612 792 re 1 1 610 790 re f* EMC",
            b"BT /F1 12 Tf 0 g 1 0 0 1 82 624 Tm (Visible inside page frame) Tj ET",
        ])
        converter = Converter(one_page_pdf(content))
        result = converter.convert()
        self.assertIn("Visible inside page frame", result.markdown)
        self.assertNotIn(1, converter._artifact_page_backgrounds)
        self.assertNotIn(
            "INVISIBLE_TEXT",
            {warning.code for warning in result.warnings},
        )

    def test_later_artifact_overlay_does_not_revive_concealed_text(self) -> None:
        content = b" ".join([
            b"BT /F1 12 Tf 1 g 1 0 0 1 82 662 Tm (Concealed before overlay) Tj ET",
            b"/Artifact BMC 0 g 72 650 220 36 re f EMC",
        ])
        result = convert(one_page_pdf(content))
        self.assertNotIn("Concealed before overlay", result.markdown)
        self.assertIn(
            "INVISIBLE_TEXT",
            {warning.code for warning in result.warnings},
        )

    def test_complex_pre_text_path_supplies_the_visible_background(self) -> None:
        content = b" ".join([
            b"0.05 0.3 0.7 rg "
            b"60 500 m 60 650 300 650 300 500 c "
            b"300 450 l 60 450 l h f",
            b"BT /F1 12 Tf 1 g 1 0 0 1 100 540 Tm "
            b"(Visible curved background) Tj ET",
        ])
        converter = Converter(one_page_pdf(content))
        result = converter.convert()

        self.assertIn("Visible curved background", result.markdown)
        path = max(converter.painted_paths, key=lambda item: len(item.commands))
        visible = next(
            char for char in converter.chars
            if char.text == "V" and not char.invisible
        )
        self.assertGreater(path.paint_order, 0)
        self.assertLess(path.paint_order, visible.paint_order)
        self.assertTrue(
            painted_path_contains_point(
                path,
                (visible.x0 + visible.x1) / 2.0,
                (visible.y0 + visible.y1) / 2.0,
            )
        )

    def test_concave_path_bbox_does_not_paint_its_notch(self) -> None:
        content = b" ".join([
            b"0.05 0.3 0.7 rg "
            b"60 450 m 300 450 l 300 700 l 220 700 l "
            b"220 560 l 140 560 l 140 700 l 60 700 l h f",
            b"BT /F1 12 Tf 1 g 1 0 0 1 80 620 Tm (Visible arm) Tj "
            b"1 0 0 1 155 620 Tm (Hidden notch) Tj ET",
        ])
        converter = Converter(one_page_pdf(content))
        result = converter.convert()

        self.assertIn("Visible arm", result.markdown)
        self.assertNotIn("Hidden notch", result.markdown)
        path = max(converter.painted_paths, key=lambda item: len(item.commands))
        hidden = next(
            char for char in converter.chars
            if char.text == "H" and char.invisible
        )
        center = ((hidden.x0 + hidden.x1) / 2.0, (hidden.y0 + hidden.y1) / 2.0)
        self.assertTrue(
            path.bbox[0] <= center[0] <= path.bbox[2]
            and path.bbox[1] <= center[1] <= path.bbox[3]
        )
        self.assertFalse(painted_path_contains_point(path, *center))

    def test_evenodd_complex_path_preserves_a_transparent_hole(self) -> None:
        content = b" ".join([
            b"0.05 0.3 0.7 rg "
            b"60 450 m 300 450 l 300 700 l 60 700 l h "
            b"130 540 m 230 540 l 230 640 l 130 640 l h f*",
            b"BT /F1 12 Tf 1 g 1 0 0 1 75 580 Tm (Visible ring) Tj "
            b"1 0 0 1 150 580 Tm (Hidden hole) Tj ET",
        ])
        converter = Converter(one_page_pdf(content))
        result = converter.convert()

        self.assertIn("Visible ring", result.markdown)
        self.assertNotIn("Hidden hole", result.markdown)
        path = max(converter.painted_paths, key=lambda item: len(item.commands))
        self.assertEqual(path.fill_rule, "evenodd")
        hidden = next(
            char for char in converter.chars
            if char.text == "H" and char.invisible
        )
        self.assertFalse(
            painted_path_contains_point(
                path,
                (hidden.x0 + hidden.x1) / 2.0,
                (hidden.y0 + hidden.y1) / 2.0,
            )
        )

    def test_complex_path_painted_after_text_is_not_its_background(self) -> None:
        content = b" ".join([
            b"BT /F1 12 Tf 1 g 1 0 0 1 100 540 Tm "
            b"(Hidden before path) Tj ET",
            b"0.05 0.3 0.7 rg "
            b"60 500 m 60 650 300 650 300 500 c "
            b"300 450 l 60 450 l h f",
        ])
        converter = Converter(one_page_pdf(content))
        result = converter.convert()

        self.assertNotIn("Hidden before path", result.markdown)
        path = max(converter.painted_paths, key=lambda item: len(item.commands))
        hidden = next(
            char for char in converter.chars
            if char.text == "H" and char.invisible
        )
        self.assertGreater(path.paint_order, hidden.paint_order)
        self.assertTrue(
            painted_path_contains_point(
                path,
                (hidden.x0 + hidden.x1) / 2.0,
                (hidden.y0 + hidden.y1) / 2.0,
            )
        )

    def test_captioned_marked_bookend_table_preserves_span_and_mcids(self) -> None:
        content = b" ".join([
            b"/Artifact BMC 0 g 72 700 240 0.75 re f EMC",
            b"/Artifact BMC 0.1 0.2 0.1 rg 72 670 240 30 re f EMC",
            b"/Artifact BMC 0 g 72 570 240 0.75 re f EMC",
            b"/TH << /MCID 5 >> BDC BT /F1 10 Tf 1 g "
            b"1 0 0 1 82 670 Tm (Species conservation register) Tj ET EMC",
            b"/TD << /MCID 6 >> BDC BT /F1 10 Tf 0 g 1 0 0 1 82 650 Tm (Pupfish) Tj ET EMC "
            b"/Span << /MCID 7 >> BDC BT /F1 10 Tf 1 0 0 1 185 650 Tm (Cyprinodon alpha) Tj ET EMC",
            b"/TD << /MCID 8 >> BDC BT /F1 10 Tf 1 0 0 1 82 630 Tm (Splitfin) Tj ET EMC "
            b"/Span << /MCID 9 >> BDC BT /F1 10 Tf 1 0 0 1 185 630 Tm (Ameca beta) Tj ET EMC",
            b"/TD << /MCID 10 >> BDC BT /F1 10 Tf 1 0 0 1 82 610 Tm (Skiffia) Tj ET EMC "
            b"/Span << /MCID 11 >> BDC BT /F1 10 Tf 1 0 0 1 185 610 Tm (Skiffia gamma) Tj ET EMC",
            b"/TD << /MCID 12 >> BDC BT /F1 10 Tf 1 0 0 1 82 590 Tm (Topminnow) Tj ET EMC "
            b"/Span << /MCID 13 >> BDC BT /F1 10 Tf 1 0 0 1 185 590 Tm (Fundulus delta) Tj ET EMC",
            b"BT /F1 9 Tf 1 0 0 1 72 550 Tm (Table 4.1: Captive species register.) Tj ET",
        ])
        result = convert(one_page_pdf(content))
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.semantic.to_dict())
        table = tables[0]
        self.assertEqual(table.attrs["row_count"], 5)
        self.assertEqual(table.attrs["column_count"], 2)
        self.assertEqual(table.attrs["header_rows"], 1)
        self.assertTrue(any(
            item.kind == "captioned_marked_bookend" for item in table.evidence
        ))
        header = next(
            cell for cell in semantic_nodes(result, "table_cell")
            if cell.attrs["row"] == 0
        )
        self.assertEqual(header.attrs["colspan"], 2)
        self.assertTrue(any(item.kind == "explicit_table_span" for item in header.evidence))
        self.assertEqual(
            {mcid for source in header.sources for mcid in source.mcids},
            {5},
        )
        first_left = next(
            cell for cell in semantic_nodes(result, "table_cell")
            if cell.attrs["row"] == 1 and cell.attrs["col"] == 0
        )
        first_right = next(
            cell for cell in semantic_nodes(result, "table_cell")
            if cell.attrs["row"] == 1 and cell.attrs["col"] == 1
        )
        self.assertEqual(
            {mcid for source in first_left.sources for mcid in source.mcids},
            {6},
        )
        self.assertEqual(
            {mcid for source in first_right.sources for mcid in source.mcids},
            {7},
        )
        self.assertTrue(any(source.glyph_ids for source in first_right.sources))
        captions = [child for child in table.children if child.kind == "caption"]
        self.assertEqual(captions[0].attrs["placement"], "after")
        self.assertIn('<th colspan="2"', result.html)
        self.assertIn("Table 4.1: Captive species register.", result.markdown)

    def test_bookend_near_miss_without_artifact_header_background_is_prose(self) -> None:
        content = b" ".join([
            b"/Artifact BMC 0 g 72 700 240 0.75 re f EMC",
            b"/Artifact BMC 0 g 72 570 240 0.75 re f EMC",
            b"/TH << /MCID 5 >> BDC BT /F1 10 Tf 1 0 0 1 82 680 Tm (Ordinary marked heading) Tj ET EMC",
            b"/TD << /MCID 6 >> BDC BT /F1 10 Tf 1 0 0 1 82 650 Tm (Alpha) Tj ET EMC "
            b"/Span << /MCID 7 >> BDC BT /F1 10 Tf 1 0 0 1 185 650 Tm (First value) Tj ET EMC",
            b"/TD << /MCID 8 >> BDC BT /F1 10 Tf 1 0 0 1 82 630 Tm (Bravo) Tj ET EMC "
            b"/Span << /MCID 9 >> BDC BT /F1 10 Tf 1 0 0 1 185 630 Tm (Second value) Tj ET EMC",
            b"/TD << /MCID 10 >> BDC BT /F1 10 Tf 1 0 0 1 82 610 Tm (Charlie) Tj ET EMC "
            b"/Span << /MCID 11 >> BDC BT /F1 10 Tf 1 0 0 1 185 610 Tm (Third value) Tj ET EMC",
            b"/TD << /MCID 12 >> BDC BT /F1 10 Tf 1 0 0 1 82 590 Tm (Delta) Tj ET EMC "
            b"/Span << /MCID 13 >> BDC BT /F1 10 Tf 1 0 0 1 185 590 Tm (Fourth value) Tj ET EMC",
            b"BT /F1 9 Tf 1 0 0 1 72 550 Tm (Table 4.2: Marked prose example.) Tj ET",
        ])
        result = convert(one_page_pdf(content))
        self.assertEqual(semantic_nodes(result, "table"), [])
        self.assertNotIn("<table>", result.markdown)

    def test_incomplete_artifact_rule_grid_is_not_a_table(self) -> None:
        content = b" ".join([
            b"/Artifact BMC 0 g",
            b"72 360 0.5 320 re f 180 360 0.5 320 re f "
            b"300 360 0.5 320 re f 430 360 0.5 320 re f",
            b"72 680 358 0.5 re f 72 600 358 0.5 re f "
            b"72 520 358 0.5 re f "
            # Deliberately omit the final column from the bottom edge.
            b"72 360 228 0.5 re f EMC",
            b"BT /F1 10 Tf 1 0 0 1 82 650 Tm (Topic) Tj "
            b"1 0 0 1 190 650 Tm (Summary) Tj "
            b"1 0 0 1 310 650 Tm (Status) Tj",
            b"1 0 0 1 82 570 Tm (First) Tj "
            b"1 0 0 1 190 570 Tm (Ordinary prose) Tj "
            b"1 0 0 1 310 570 Tm (Open) Tj",
            b"1 0 0 1 82 490 Tm (Second) Tj "
            b"1 0 0 1 190 490 Tm (More prose) Tj "
            b"1 0 0 1 310 490 Tm (Open) Tj ET",
        ])
        result = convert(one_page_pdf(content))
        self.assertEqual(semantic_nodes(result, "table"), [])
        self.assertNotIn("<table>", result.markdown)
        self.assertIn("Ordinary prose", result.markdown)

    def test_artifact_sidebar_cards_remain_prose(self) -> None:
        content = b" ".join([
            b"/Artifact BMC 0 g",
            # Three independent outlined cards, not a shared closed lattice.
            b"72 610 0.5 70 re f 72 680 180 0.5 re f "
            b"252 610 0.5 70 re f 72 610 180 0.5 re f",
            b"72 500 0.5 70 re f 72 570 180 0.5 re f "
            b"252 500 0.5 70 re f 72 500 180 0.5 re f",
            b"72 390 0.5 70 re f 72 460 180 0.5 re f "
            b"252 390 0.5 70 re f 72 390 180 0.5 re f EMC",
            b"BT /F1 10 Tf 1 0 0 1 84 650 Tm (A sidebar card explains the first idea.) Tj "
            b"1 0 0 1 84 540 Tm (Another card contains ordinary prose.) Tj "
            b"1 0 0 1 84 430 Tm (The last card remains a paragraph.) Tj ET",
        ])
        result = convert(one_page_pdf(content))
        self.assertEqual(semantic_nodes(result, "table"), [])
        self.assertNotIn("<table>", result.markdown)
        self.assertIn("ordinary prose", result.markdown)

    def test_simple_booktabs_table_uses_repeated_numeric_anchors(self) -> None:
        content = b" ".join([
            b"0.5 w 72 700 m 500 700 l 72 670 m 500 670 l 72 580 m 500 580 l S",
            b"BT /F1 11 Tf 1 0 0 1 80 680 Tm (Model) Tj 1 0 0 1 250 680 Tm (Score) Tj 1 0 0 1 350 680 Tm (Rate) Tj",
            b"1 0 0 1 80 650 Tm (Alpha) Tj 1 0 0 1 250 650 Tm (10) Tj 1 0 0 1 350 650 Tm (20) Tj",
            b"1 0 0 1 80 620 Tm (Bravo) Tj 1 0 0 1 250 620 Tm (30) Tj 1 0 0 1 350 620 Tm (40) Tj",
            b"1 0 0 1 80 590 Tm (Charlie) Tj 1 0 0 1 250 590 Tm (50) Tj 1 0 0 1 350 590 Tm (60) Tj",
            b"1 0 0 1 72 550 Tm (Table 1: Numeric comparison) Tj ET",
        ])
        result = convert(one_page_pdf(content))
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.semantic.to_dict())
        self.assertEqual(tables[0].attrs["row_count"], 4)
        self.assertEqual(tables[0].attrs["column_count"], 3)
        self.assertTrue(any(item.kind == "booktabs" for item in tables[0].evidence))
        self.assertIn("<table>", result.markdown)
        self.assertIn("<td>Charlie</td><td>50</td><td>60</td>", result.markdown)
        self.assertIn("Table 1: Numeric comparison", result.markdown)

    def test_horizontal_rules_and_numbers_without_table_caption_are_not_booktabs(self) -> None:
        content = b" ".join([
            b"0.5 w 72 700 m 500 700 l 72 670 m 500 670 l 72 580 m 500 580 l S",
            b"BT /F1 11 Tf 1 0 0 1 80 680 Tm (Quarterly indicators) Tj",
            b"1 0 0 1 80 650 Tm (First observation is 10 and 20.) Tj",
            b"1 0 0 1 80 620 Tm (Second observation is 30 and 40.) Tj",
            b"1 0 0 1 80 590 Tm (Third observation is 50 and 60.) Tj ET",
        ])
        result = convert(one_page_pdf(content))
        self.assertEqual(semantic_nodes(result, "table"), [])
        self.assertNotIn("<table>", result.markdown)
        self.assertIn("First observation", result.markdown)

    def test_complete_captioned_lattice_precedes_partial_grid_fallback(self) -> None:
        content = b" ".join([
            b"0.5 w "
            b"72 500 m 300 500 l 72 540 m 300 540 l 72 580 m 300 580 l 72 620 m 300 620 l "
            b"72 500 m 72 540 l 72 540 m 72 580 l 72 580 m 72 620 l "
            b"186 500 m 186 540 l 186 540 m 186 580 l 186 580 m 186 620 l "
            b"300 500 m 300 540 l 300 540 m 300 580 l 300 580 m 300 620 l S",
            b"BT /F1 11 Tf 1 0 0 1 72 650 Tm (Table 2: Complete lattice) Tj",
            b"/F2 11 Tf 1 0 0 1 90 595 Tm (Name) Tj 1 0 0 1 205 595 Tm (Value) Tj",
            b"/F1 11 Tf 1 0 0 1 90 555 Tm (Alpha) Tj 1 0 0 1 205 555 Tm (10) Tj",
            b"1 0 0 1 90 515 Tm (Bravo) Tj 1 0 0 1 205 515 Tm (20) Tj ET",
        ])
        resources = b"/Resources << /Font << /F1 1 0 R /F2 2 0 R >> >>"
        objects = [
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Page /Parent 5 0 R /MediaBox [0 0 612 792] " + resources + b" /Contents 3 0 R >>",
            b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
            b"<< /Type /Catalog /Pages 5 0 R >>",
        ]
        result = convert(render_pdf(objects, 6))
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.semantic.to_dict())
        evidence = {item.kind for item in tables[0].evidence}
        self.assertIn("lattice_table", evidence)
        self.assertNotIn("captioned_partial_grid", evidence)
        self.assertIn("| Name | Value |", result.markdown)

    def test_recovered_grid_precedes_overlapping_incomplete_lattice_fallback(self) -> None:
        content = b" ".join([
            b"q 0.90 g 72 600 268 40 re f 72 520 268 40 re f Q",
            b"0.5 w "
            b"110 520 m 110 560 l 110 560 m 110 600 l 110 600 m 110 640 l 110 640 m 110 680 l "
            b"260 520 m 260 560 l 260 560 m 260 600 l 260 600 m 260 640 l 260 640 m 260 680 l "
            # This connected header component has a deliberately incomplete
            # center edge. It is useful evidence, but not a lossless table.
            b"110 640 m 260 640 l 110 680 m 260 680 l 185 640 m 185 660 l S",
            b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (Table: Quarterly totals) Tj",
            b"1 0 0 1 80 655 Tm (No.) Tj 1 0 0 1 130 655 Tm (Organization) Tj 1 0 0 1 280 655 Tm (Total) Tj",
            b"1 0 0 1 80 615 Tm (1) Tj 1 0 0 1 130 615 Tm (Alpha Group) Tj 1 0 0 1 280 615 Tm (120) Tj",
            b"1 0 0 1 80 575 Tm (2) Tj 1 0 0 1 130 575 Tm (Bravo Group) Tj 1 0 0 1 280 575 Tm (95) Tj",
            b"1 0 0 1 80 535 Tm (3) Tj 1 0 0 1 130 535 Tm (Gamma Group) Tj 1 0 0 1 280 535 Tm (70) Tj ET",
        ])
        result = convert(one_page_pdf(content))
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.semantic.to_dict())
        self.assertEqual(tables[0].attrs["row_count"], 4)
        self.assertEqual(tables[0].attrs["column_count"], 3)
        evidence = {item.kind for item in tables[0].evidence}
        self.assertIn("captioned_partial_grid", evidence)
        self.assertNotIn("lattice_table", evidence)
        self.assertIn("<td>Bravo Group</td><td>95</td>", result.markdown)
        self.assertNotIn("TABLE_SPAN_UNSUPPORTED", {warning.code for warning in result.warnings})

    def test_captioned_fragmented_grid_uses_row_fills_and_preserves_wrapped_cells(self) -> None:
        content = b" ".join([
            b"q 0.90 g 72 600 268 40 re f 72 520 268 40 re f Q",
            b"0.5 w "
            b"110 520 m 110 560 l 110 560 m 110 600 l 110 600 m 110 640 l 110 640 m 110 680 l "
            b"260 520 m 260 560 l 260 560 m 260 600 l 260 600 m 260 640 l 260 640 m 260 680 l S",
            b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (Table: Quarterly totals) Tj",
            b"1 0 0 1 80 655 Tm (No.) Tj 1 0 0 1 130 655 Tm (Organization) Tj 1 0 0 1 280 655 Tm (Total) Tj",
            b"1 0 0 1 80 615 Tm (1) Tj 1 0 0 1 130 615 Tm (Alpha Group) Tj 1 0 0 1 280 615 Tm (120) Tj",
            b"1 0 0 1 80 575 Tm (2) Tj 1 0 0 1 130 580 Tm (Long wrapped) Tj "
            b"1 0 0 1 130 566 Tm (organization) Tj 1 0 0 1 280 575 Tm (95) Tj",
            b"1 0 0 1 80 535 Tm (3) Tj 1 0 0 1 130 535 Tm (Gamma Group) Tj 1 0 0 1 280 535 Tm (70) Tj ET",
        ])
        result = convert(one_page_pdf(content))
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.semantic.to_dict())
        self.assertEqual(tables[0].attrs["row_count"], 4)
        self.assertEqual(tables[0].attrs["column_count"], 3)
        self.assertTrue(any(item.kind == "captioned_partial_grid" for item in tables[0].evidence))
        self.assertIn("<table>", result.markdown)
        self.assertIn("Long wrapped<br />organization", result.markdown)
        self.assertIn("<td>95</td>", result.markdown)

    def test_caption_and_zebra_panels_without_repeated_boundaries_are_not_a_table(self) -> None:
        content = b" ".join([
            b"q 0.90 g 72 600 468 30 re f 72 540 468 30 re f Q",
            b"BT /F1 12 Tf 1 0 0 1 72 720 Tm (Table: discussion prompts) Tj",
            b"1 0 0 1 84 610 Tm (This shaded sentence is ordinary explanatory prose.) Tj",
            b"1 0 0 1 84 550 Tm (Another shaded sentence continues the discussion.) Tj ET",
        ])
        result = convert(one_page_pdf(content))
        self.assertEqual(semantic_nodes(result, "table"), [])
        self.assertNotIn("<table>", result.markdown)
        self.assertIn("ordinary explanatory prose", result.markdown)

    def test_partial_vertical_border_infers_only_corroborated_colspan(self) -> None:
        content = (
            b"0.5 w 72 500 m 300 500 l 72 540 m 300 540 l 72 580 m 300 580 l "
            b"72 500 m 72 580 l 300 500 m 300 580 l 186 500 m 186 540 l S "
            b"BT /F1 12 Tf 1 0 0 1 130 555 Tm (Summary) Tj "
            b"1 0 0 1 90 515 Tm (Left) Tj 1 0 0 1 205 515 Tm (Right) Tj ET"
        )
        result = convert(one_page_pdf(content))
        cells = semantic_nodes(result, "table_cell")
        spans = [cell for cell in cells if cell.attrs.get("colspan") == 2]
        self.assertEqual(len(spans), 1, result.semantic.to_dict())
        self.assertEqual(spans[0].attrs["row"], 0)
        self.assertTrue(any(item.kind == "missing_border_span" for item in spans[0].evidence))
        self.assertIn('colspan="2"', result.html)

    def test_partial_horizontal_border_infers_only_corroborated_rowspan(self) -> None:
        content = (
            b"0.5 w 72 500 m 300 500 l 72 580 m 300 580 l 186 540 m 300 540 l "
            b"72 500 m 72 580 l 186 500 m 186 580 l 300 500 m 300 580 l S "
            b"BT /F1 12 Tf 1 0 0 1 90 555 Tm (Group) Tj 1 0 0 1 205 555 Tm (First) Tj "
            b"1 0 0 1 205 515 Tm (Second) Tj ET"
        )
        result = convert(one_page_pdf(content))
        spans = [cell for cell in semantic_nodes(result, "table_cell") if cell.attrs.get("rowspan") == 2]
        self.assertEqual(len(spans), 1, result.semantic.to_dict())
        self.assertEqual(spans[0].attrs["col"], 0)
        self.assertIn('rowspan="2"', result.html)

    def test_article_columns_are_not_hallucinated_as_borderless_table(self) -> None:
        content = b" ".join([
            b"BT /F1 10 Tf 1 0 0 1 72 720 Tm (This is a complete sentence about semantic extraction.) Tj 1 0 0 1 330 720 Tm (Another complete sentence continues the article.) Tj ET",
            b"BT /F1 10 Tf 1 0 0 1 72 690 Tm (The left column contains ordinary prose and punctuation.) Tj 1 0 0 1 330 690 Tm (The right column also contains ordinary prose.) Tj ET",
            b"BT /F1 10 Tf 1 0 0 1 72 660 Tm (Neither repeated x position establishes a data table.) Tj 1 0 0 1 330 660 Tm (Both regions must remain readable paragraphs.) Tj ET",
        ])
        result = convert(one_page_pdf(content))
        self.assertEqual(semantic_nodes(result, "table"), [])
        self.assertIn("complete sentence", result.markdown)
        self.assertIn("remain readable paragraphs", result.markdown)

    def test_dot_leader_financial_statement_recovers_year_columns(self) -> None:
        content = b" ".join([
            b"BT /F2 9 Tf 1 0 0 1 360 700 Tm (2024) Tj 1 0 0 1 430 700 Tm (2023) Tj 1 0 0 1 500 700 Tm (2022) Tj ET",
            b"BT /F1 10 Tf 1 0 0 1 72 680 Tm (Revenue ........................................) Tj 1 0 0 1 350 680 Tm ($ 100) Tj 1 0 0 1 430 680 Tm ($ 90) Tj 1 0 0 1 500 680 Tm ($ 80) Tj ET",
            b"BT /F1 10 Tf 1 0 0 1 72 660 Tm (Expense ........................................) Tj 1 0 0 1 350 660 Tm (($ 40)) Tj 1 0 0 1 430 660 Tm (($ 35)) Tj 1 0 0 1 500 660 Tm (($ 30)) Tj ET",
            b"BT /F1 10 Tf 1 0 0 1 72 640 Tm (Net income .....................................) Tj 1 0 0 1 350 640 Tm ($ 60) Tj 1 0 0 1 430 640 Tm ($ 55) Tj 1 0 0 1 500 640 Tm ($ 50) Tj ET",
        ])
        resources = b"/Resources << /Font << /F1 1 0 R /F2 2 0 R >> >>"
        objects = [
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Page /Parent 5 0 R /MediaBox [0 0 612 792] " + resources + b" /Contents 3 0 R >>",
            b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
            b"<< /Type /Catalog /Pages 5 0 R >>",
        ]
        result = convert(render_pdf(objects, 6))
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.semantic.to_dict())
        self.assertIn('<th scope="col">2024</th>', result.markdown)
        self.assertIn('<th scope="row">Net income</th>', result.markdown)
        self.assertNotIn("................................", result.markdown)

    def test_year_columns_without_leaders_do_not_force_financial_html(self) -> None:
        content = b" ".join([
            b"BT /F2 9 Tf 1 0 0 1 360 700 Tm (2024) Tj 1 0 0 1 450 700 Tm (2023) Tj ET",
            b"BT /F1 10 Tf 1 0 0 1 72 680 Tm (Alpha) Tj 1 0 0 1 360 680 Tm (100) Tj 1 0 0 1 450 680 Tm (90) Tj ET",
            b"BT /F1 10 Tf 1 0 0 1 72 660 Tm (Bravo) Tj 1 0 0 1 360 660 Tm (80) Tj 1 0 0 1 450 660 Tm (70) Tj ET",
            b"BT /F1 10 Tf 1 0 0 1 72 640 Tm (Charlie) Tj 1 0 0 1 360 640 Tm (60) Tj 1 0 0 1 450 640 Tm (50) Tj ET",
        ])
        resources = b"/Resources << /Font << /F1 1 0 R /F2 2 0 R >> >>"
        objects = [
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Page /Parent 5 0 R /MediaBox [0 0 612 792] " + resources + b" /Contents 3 0 R >>",
            b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
            b"<< /Type /Catalog /Pages 5 0 R >>",
        ]
        result = convert(render_pdf(objects, 6))
        self.assertNotIn('<th scope="row">Alpha</th>', result.markdown)
        self.assertNotIn('scope="rowgroup"', result.markdown)

    def test_rotated_header_forces_loss_aware_html_table(self) -> None:
        content = (
            b"0.5 w 72 500 m 300 500 l 72 540 m 300 540 l 72 580 m 300 580 l "
            b"72 500 m 72 580 l 186 500 m 186 580 l 300 500 m 300 580 l S "
            b"BT /F1 10 Tf 0 1 -1 0 120 545 Tm (Header) Tj 1 0 0 1 205 555 Tm (Value) Tj "
            b"1 0 0 1 90 515 Tm (Alpha) Tj 1 0 0 1 205 515 Tm (10) Tj ET"
        )
        result = convert(one_page_pdf(content))
        table = semantic_nodes(result, "table")[0]
        self.assertEqual(table.attrs["output_mode"], "html")
        self.assertTrue(any(int(cell.attrs.get("rotation", 0)) % 360 for cell in semantic_nodes(result, "table_cell")))
        self.assertIn("writing-mode: vertical-rl", result.html)


class TableEvaluationTests(unittest.TestCase):
    def test_locked_table_corpus_has_exact_self_scores_and_detects_span_error(self) -> None:
        from pathlib import Path
        from cocoapdf.eval.tables import evaluate_table

        manifest = json.loads((Path(__file__).parent / "fixtures" / "tables" / "manifest.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(manifest["fixtures"]), 3)
        for fixture in manifest["fixtures"]:
            expected = fixture["expected"]
            metrics = evaluate_table(expected, expected)
            self.assertEqual(metrics.cell_exact, 1.0)
            self.assertEqual(metrics.span_exact, 1.0)
            self.assertEqual(metrics.teds, 1.0)
        altered = json.loads(json.dumps(manifest["fixtures"][1]["expected"]))
        altered["children"][0]["children"][0]["attrs"]["colspan"] = 1
        metrics = evaluate_table(manifest["fixtures"][1]["expected"], altered)
        self.assertEqual(metrics.cell_exact, 0.0)
        self.assertEqual(metrics.span_exact, 0.0)
        self.assertLess(metrics.teds, 1.0)


class UnicodeBracketDataTests(unittest.TestCase):
    def test_unicode_17_normative_bracket_relation_is_exact_for_edge_pairs(self) -> None:
        from cocoapdf.text.bidi import BIDI_BRACKET_DATA_VERSION, _BRACKET_CLOSE, _BRACKET_PAIRS

        self.assertEqual(BIDI_BRACKET_DATA_VERSION, "17.0.0")
        self.assertEqual(_BRACKET_PAIRS[chr(0x298D)], chr(0x2990))
        self.assertEqual(_BRACKET_PAIRS[chr(0x298F)], chr(0x298E))
        self.assertEqual(_BRACKET_PAIRS[chr(0x2E55)], chr(0x2E56))
        self.assertNotIn(chr(0x27C0), _BRACKET_PAIRS)
        self.assertEqual(_BRACKET_CLOSE[chr(0x298E)], chr(0x298F))


class BidiResolutionMetadataTests(unittest.TestCase):
    def test_resolved_levels_and_visual_order_are_exposed_for_unicode_corpus(self) -> None:
        from cocoapdf.text.bidi import resolve_text

        result = resolve_text("abc אבג 123")
        self.assertEqual(len(result.levels), len("abc אבג 123"))
        self.assertEqual(sorted(result.visual_order), list(range(len("abc אבג 123"))))
        self.assertEqual(result.text, reorder_text("abc אבג 123"))


class CompleteTaggedBindingTests(unittest.TestCase):
    def test_objr_link_binds_by_annotation_object_and_inherits_language(self) -> None:
        content = b"/Span <</MCID 0>> BDC BT /F1 12 Tf 1 0 0 1 72 720 Tm (OpenAI) Tj ET EMC"
        objects = [
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Page /Parent 4 0 R /StructParents 0 /MediaBox [0 0 612 792] /Resources << /Font << /F1 1 0 R >> >> /Contents 2 0 R /Annots [8 0 R] >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /StructElem /S /Link /P 9 0 R /Pg 3 0 R /K [0 6 0 R] >>",
            b"<< /Type /OBJR /Obj 8 0 R /Pg 3 0 R >>",
            b"<< /Nums [0 [5 0 R] 5 5 0 R] >>",
            b"<< /Type /Annot /Subtype /Link /StructParent 5 /P 3 0 R /Rect [70 710 140 735] /A << /S /URI /URI (https://openai.com) >> >>",
            b"<< /Type /StructTreeRoot /K [5 0 R] /ParentTree 7 0 R >>",
            b"<< /Type /Catalog /Pages 4 0 R /StructTreeRoot 9 0 R /Lang (en-US) /MarkInfo << /Marked true >> >>",
        ]
        result = convert(render_pdf(objects, 10))
        links = semantic_nodes(result, "link")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].attrs["tag_role"], "Link")
        self.assertEqual(links[0].attrs["lang"], "en-US")
        self.assertIn("8 0 R", links[0].sources[0].object_refs)
        self.assertIn("[OpenAI](https://openai.com)", result.markdown)
        self.assertEqual(result.semantic.metadata["tagged_pdf"]["conflicts"], [])

    def test_mcr_in_form_xobject_uses_stream_structparents(self) -> None:
        form = b"/P <</MCID 0>> BDC BT /F1 12 Tf 1 0 0 1 0 0 Tm (Form text) Tj ET EMC"
        page_content = b"q 1 0 0 1 72 700 cm /Fm1 Do Q"
        objects = [
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(page_content) + page_content + b"\nendstream",
            b"<< /Type /Page /Parent 4 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 1 0 R >> /XObject << /Fm1 5 0 R >> >> /Contents 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /XObject /Subtype /Form /BBox [0 0 200 30] /StructParents 1 /Resources << /Font << /F1 1 0 R >> >> /Length %d >>\nstream\n" % len(form) + form + b"\nendstream",
            b"<< /Type /StructElem /S /P /P 9 0 R /Pg 3 0 R /K [7 0 R] >>",
            b"<< /Type /MCR /Pg 3 0 R /Stm 5 0 R /MCID 0 >>",
            b"<< /Nums [1 [6 0 R]] >>",
            b"<< /Type /StructTreeRoot /K [6 0 R] /ParentTree 8 0 R >>",
            b"<< /Type /Catalog /Pages 4 0 R /StructTreeRoot 9 0 R /MarkInfo << /Marked true >> >>",
        ]
        result = convert(render_pdf(objects, 10))
        paragraphs = semantic_nodes(result, "paragraph")
        self.assertEqual(len(paragraphs), 1)
        self.assertEqual(paragraphs[0].attrs["tag_role"], "P")
        self.assertEqual(result.semantic.metadata["tagged_pdf"]["conflicts"], [])
        self.assertNotIn("TAGGED_PARENTTREE_MCID_MISSING", " ".join(result.semantic.warnings))


class CompleteTableStructureTests(unittest.TestCase):
    @staticmethod
    def _artifact_boundary_continuation_fixture(
        mode: str = "positive",
    ) -> bytes:
        def lattice(
            xs: list[float],
            ys: list[float],
            header: tuple[str, ...],
            body: list[tuple[str, ...]],
        ) -> bytes:
            parts = [b"/Artifact BMC"]
            for x in xs:
                parts.append(
                    rect_fill_op(x, ys[0], 0.5, ys[-1] - ys[0], 0.0)
                )
            for y in ys:
                parts.append(
                    rect_fill_op(xs[0], y, xs[-1] - xs[0], 0.5, 0.0)
                )
            parts.append(b"EMC")
            header_y = (ys[-2] + ys[-1]) / 2.0 - 3.0
            for column, text in enumerate(header):
                parts.append(
                    text_op(xs[column] + 10.0, header_y, text, font="F2", size=10)
                )
            for row_index, row in enumerate(body):
                lower_index = len(ys) - 3 - row_index
                row_y = (ys[lower_index] + ys[lower_index + 1]) / 2.0 - 3.0
                for column, text in enumerate(row):
                    parts.append(
                        text_op(xs[column] + 10.0, row_y, text, size=10)
                    )
            return b" ".join(parts)

        header = ("Field", "Policy", "Status")
        fragment_header = (
            ("Field", "Category", "Status")
            if mode == "mismatched_header"
            else header
        )
        fragment_xs = (
            [72.0, 250.0, 395.0, 540.0]
            if mode == "shifted_grid"
            else [72.0, 220.0, 380.0, 540.0]
        )
        fragment_ys = (
            [330.0, 380.0, 430.0]
            if mode == "midpage"
            else [40.0, 62.0, 84.0]
            if mode == "compact_height"
            else [40.0, 57.0, 74.0]
            if mode == "subminimum_height"
            else [40.0, 90.0, 140.0]
        )
        first = lattice(
            fragment_xs,
            fragment_ys,
            fragment_header,
            [("Aster", "Active", "Ready")],
        )
        peer_ys = (
            [620.0, 670.0, 720.0]
            if mode == "weak_peer"
            else [520.0, 570.0, 620.0, 670.0, 720.0]
        )
        peer_body = (
            [("Birch", "Review", "Open")]
            if mode == "weak_peer"
            else [
                ("Birch", "Review", "Open"),
                ("Cedar", "Active", "Ready"),
                ("Dogwood", "Closed", "Filed"),
            ]
        )
        second = lattice(
            [72.0, 220.0, 380.0, 540.0],
            peer_ys,
            header,
            peer_body,
        )
        if mode != "different_page_width":
            return make_pdf([first, second])

        resources = b"/Resources << /Font << /F1 1 0 R /F2 2 0 R >> >>"
        objects = [
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(first) + first + b"\nendstream",
            b"<< /Length %d >>\nstream\n" % len(second) + second + b"\nendstream",
            b"<< /Type /Page /Parent 7 0 R /MediaBox [0 0 612 792] " + resources + b" /Contents 3 0 R >>",
            b"<< /Type /Page /Parent 7 0 R /MediaBox [0 0 660 792] " + resources + b" /Contents 4 0 R >>",
            b"<< /Type /Pages /Kids [5 0 R 6 0 R] /Count 2 >>",
            b"<< /Type /Catalog /Pages 7 0 R >>",
        ]
        return render_pdf(objects, 8)

    def test_nested_list_blocks_inside_cell_force_html_without_flattening(self) -> None:
        content = (
            b"0.5 w 72 500 m 430 500 l 72 580 m 430 580 l "
            b"72 500 m 72 580 l 250 500 m 250 580 l 430 500 m 430 580 l S "
            b"BT /F1 10 Tf 1 0 0 1 90 555 Tm (- First) Tj "
            b"1 0 0 1 90 530 Tm (- Second) Tj "
            b"1 0 0 1 270 545 Tm (Details) Tj ET"
        )
        result = convert(one_page_pdf(content))
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].attrs["output_mode"], "html")
        lists = semantic_nodes(result, "list")
        self.assertEqual(len(lists), 1)
        self.assertEqual([node_text(item) for item in lists[0].children], ["First", "Second"])
        self.assertIn("<table>", result.markdown)
        self.assertIn("- First", result.markdown)

    def test_repeated_header_table_continues_across_pages_with_grid_precision(self) -> None:
        first = (
            b"0.5 w 72 40 m 300 40 l 72 80 m 300 80 l 72 120 m 300 120 l "
            b"72 40 m 72 120 l 186 40 m 186 120 l 300 40 m 300 120 l S "
            b"BT /F2 10 Tf 1 0 0 1 90 95 Tm (Name) Tj 1 0 0 1 205 95 Tm (Value) Tj "
            b"/F1 10 Tf 1 0 0 1 90 55 Tm (Alpha) Tj 1 0 0 1 205 55 Tm (10) Tj ET"
        )
        second = (
            b"0.5 w 72 670 m 300 670 l 72 710 m 300 710 l 72 750 m 300 750 l "
            b"72 670 m 72 750 l 186 670 m 186 750 l 300 670 m 300 750 l S "
            b"BT /F2 10 Tf 1 0 0 1 90 725 Tm (Name) Tj 1 0 0 1 205 725 Tm (Value) Tj "
            b"/F1 10 Tf 1 0 0 1 90 685 Tm (Beta) Tj 1 0 0 1 205 685 Tm (20) Tj ET"
        )
        resources = b"/Resources << /Font << /F1 1 0 R /F2 2 0 R >> >>"
        objects = [
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(first) + first + b"\nendstream",
            b"<< /Length %d >>\nstream\n" % len(second) + second + b"\nendstream",
            b"<< /Type /Page /Parent 7 0 R /MediaBox [0 0 612 792] " + resources + b" /Contents 3 0 R >>",
            b"<< /Type /Page /Parent 7 0 R /MediaBox [0 0 612 792] " + resources + b" /Contents 4 0 R >>",
            b"<< /Type /Pages /Kids [5 0 R 6 0 R] /Count 2 >>",
            b"<< /Type /Catalog /Pages 7 0 R >>",
        ]
        result = convert(render_pdf(objects, 8))
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.semantic.to_dict())
        self.assertEqual(tables[0].source_pages(), [1, 2])
        self.assertEqual(tables[0].attrs["row_count"], 3)
        self.assertEqual(result.markdown.count("Name"), 1)
        self.assertIn("Beta", result.markdown)
        self.assertEqual(result.html.count("Name"), 1)
        self.assertIn("Alpha", result.html)
        self.assertIn("Beta", result.html)
        self.assertIn('data-source-pages="1 2"', result.html)

    def test_artifact_boundary_fragment_requires_strong_repeated_peer(self) -> None:
        result = convert(self._artifact_boundary_continuation_fixture())
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.semantic.to_dict())
        table = tables[0]
        self.assertEqual(table.source_pages(), [1, 2])
        self.assertEqual(table.attrs["row_count"], 5)
        self.assertEqual(table.attrs["column_count"], 3)
        self.assertEqual(table.attrs["header_rows"], 1)
        evidence = next(
            item
            for item in table.evidence
            if item.kind == "artifact_boundary_continuation_lattice"
        )
        self.assertTrue(evidence.data["complete_edge_coverage"])
        self.assertTrue(evidence.data["repeated_header_match"])
        self.assertTrue(evidence.data["normalized_boundary_match"])
        self.assertEqual(evidence.data["adjacent_peer_page"], 2)
        self.assertTrue(any(source.page == 1 for source in table.sources))
        self.assertTrue(any(source.page == 2 for source in table.sources))
        self.assertTrue(any(source.glyph_ids for source in table.sources))
        self.assertFalse(any(
            "Field Policy Status" in node_text(node)
            for node in semantic_nodes(result, "heading")
        ))
        self.assertEqual(result.markdown.count("Field"), 1)
        self.assertEqual(result.html.count("Field"), 1)
        self.assertEqual(render_semantic_html(result.semantic), result.html)

        with_breaks = convert(
            self._artifact_boundary_continuation_fixture(),
            ConvertOptions(page_breaks=True),
        )
        # Explicit physical-page mode deliberately retains both fragments and
        # their repeated headers around the sentinel.
        self.assertEqual(with_breaks.markdown.count("Field"), 2)
        self.assertIn("<!-- page 2 -->", with_breaks.markdown)

    def test_missing_merged_layout_fragment_does_not_partially_overlay(self) -> None:
        first = "<table><tr><td>First</td></tr></table>"
        missing = "<table><tr><td>Missing</td></tr></table>"
        table = SemanticNode(
            "continued-table",
            "table",
            attrs={
                "_layout_markdown": first,
                "_merged_layout_markdown_fragments": [first, missing],
            },
            evidence=[Evidence("multi_page_table_continuation", 0.95)],
        )
        original = first + "\n\nUnrelated content\n"
        self.assertEqual(
            _overlay_changed_blocks(original, SemanticDocument([table])),
            original,
        )

    def test_artifact_boundary_fragment_is_rejected_midpage(self) -> None:
        result = convert(self._artifact_boundary_continuation_fixture("midpage"))
        self.assertEqual(len(semantic_nodes(result, "table")), 1)
        self.assertFalse(any(
            item.kind == "artifact_boundary_continuation_lattice"
            for table in semantic_nodes(result, "table")
            for item in table.evidence
        ))

    def test_compact_artifact_boundary_fragment_uses_peer_specific_height(self) -> None:
        result = convert(
            self._artifact_boundary_continuation_fixture("compact_height")
        )
        self.assertEqual(len(semantic_nodes(result, "table")), 1)
        self.assertTrue(any(
            item.kind == "artifact_boundary_continuation_lattice"
            for table in semantic_nodes(result, "table")
            for item in table.evidence
        ))

    def test_subminimum_artifact_boundary_fragment_remains_prose(self) -> None:
        result = convert(
            self._artifact_boundary_continuation_fixture("subminimum_height")
        )
        self.assertEqual(len(semantic_nodes(result, "table")), 1)
        self.assertFalse(any(
            item.kind == "artifact_boundary_continuation_lattice"
            for table in semantic_nodes(result, "table")
            for item in table.evidence
        ))

    def test_artifact_boundary_fragment_requires_exact_header_match(self) -> None:
        result = convert(
            self._artifact_boundary_continuation_fixture("mismatched_header")
        )
        self.assertEqual(len(semantic_nodes(result, "table")), 1)
        self.assertFalse(any(
            item.kind == "artifact_boundary_continuation_lattice"
            for table in semantic_nodes(result, "table")
            for item in table.evidence
        ))

    def test_artifact_boundary_fragment_requires_matching_grid(self) -> None:
        result = convert(
            self._artifact_boundary_continuation_fixture("shifted_grid")
        )
        self.assertEqual(len(semantic_nodes(result, "table")), 1)
        self.assertFalse(any(
            item.kind == "artifact_boundary_continuation_lattice"
            for table in semantic_nodes(result, "table")
            for item in table.evidence
        ))

    def test_two_weak_artifact_boundary_fragments_cannot_bootstrap(self) -> None:
        result = convert(self._artifact_boundary_continuation_fixture("weak_peer"))
        self.assertEqual(semantic_nodes(result, "table"), [])

    def test_artifact_boundary_fragment_rejects_incompatible_page_widths(self) -> None:
        result = convert(
            self._artifact_boundary_continuation_fixture("different_page_width")
        )
        self.assertEqual(len(semantic_nodes(result, "table")), 1)
        self.assertFalse(any(
            item.kind == "artifact_boundary_continuation_lattice"
            for table in semantic_nodes(result, "table")
            for item in table.evidence
        ))

    def test_table_caption_and_note_are_typed_children(self) -> None:
        content = (
            b"0.5 w 72 500 m 300 500 l 72 540 m 300 540 l 72 580 m 300 580 l "
            b"72 500 m 72 580 l 186 500 m 186 580 l 300 500 m 300 580 l S "
            b"BT /F1 10 Tf 1 0 0 1 72 610 Tm (Table 1. Results) Tj "
            b"1 0 0 1 90 555 Tm (Name) Tj 1 0 0 1 205 555 Tm (Value) Tj "
            b"1 0 0 1 90 515 Tm (Alpha) Tj 1 0 0 1 205 515 Tm (10) Tj "
            b"1 0 0 1 72 475 Tm (Note: Values are illustrative.) Tj ET"
        )
        result = convert(one_page_pdf(content))
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.semantic.to_dict())
        self.assertEqual(tables[0].attrs.get("label"), "1")
        self.assertEqual(len([child for child in tables[0].children if child.kind == "caption"]), 1)
        self.assertEqual(len([child for child in tables[0].children if child.kind == "table_note"]), 1)


def node_text(node):
    return node.text or "".join(node_text(child) for child in node.children)


# ---- semantic IR, tagged reconciliation, and provenance contracts ----


class CMapTests(unittest.TestCase):
	def test_codespace_controls_variable_width_segmentation(self) -> None:
		cmap = b"""
		2 begincodespacerange
		<00> <7f>
		<8100> <81ff>
		endcodespacerange
		2 beginbfchar
		<41> <0041>
		<8140> <4e00>
		endbfchar
		"""
		mapping = parse_tounicode(cmap)
		self.assertEqual(mapping.code_space_ranges, [(b"\x00", b"\x7f"), (b"\x81\x00", b"\x81\xff")])
		font = SimpleNamespace(composite=True, to_unicode=mapping, encoding="Custom-H", width_for_code=lambda _code: 1000.0)
		self.assertEqual([text for _code, text, _width in decode_font(font, b"A\x81@")], ["A", "\u4e00"])

	def test_unmapped_valid_code_preserves_width_without_unicode_fabrication(self) -> None:
		mapping = CMapMapping()
		mapping.add_codespace(b"\x81\x00", b"\x81\xff")
		font = SimpleNamespace(composite=True, to_unicode=mapping, encoding="Custom-H", width_for_code=lambda _code: 900.0)
		self.assertEqual(decode_font(font, b"\x81A"), [(b"\x81A", "", 900.0)])


class ProvenanceTests(unittest.TestCase):
	def test_synthetic_bidi_boundary_inherits_adjacent_glyph_provenance(self) -> None:
		style = (False,) * 8
		nodes = inline_nodes_from_tokens(
			NodeFactory(),
			[
				{"text": "עברית", "style": style, "link": None, "page": 1, "glyph_ids": (1, 2), "bbox": (10, 20, 30, 40)},
				{"text": " ", "style": style, "link": None, "synthetic_space": True},
				{"text": "ABC", "style": style, "link": None, "page": 1, "glyph_ids": (3, 4, 5), "bbox": (31, 20, 50, 40)},
			],
		)
		self.assertEqual(len(nodes), 3)
		self.assertEqual(nodes[1].sources[0].page, 1)
		self.assertEqual(nodes[1].sources[0].glyph_ids, (1, 2, 3, 4, 5))
		self.assertEqual(nodes[1].sources[0].bbox, (10.0, 20.0, 50.0, 40.0))
		self.assertEqual(SemanticDocument(nodes).validate(), [])

	def test_marked_content_is_attached_to_character(self) -> None:
		character = SimpleNamespace()
		_attach_marked_content(character, [
			{"tag": "P", "mcid": 7, "actual_text": None},
			{"tag": "Artifact", "mcid": None, "actual_text": None},
		])
		self.assertEqual(character.mc[0], {"tag": "P", "mcid": 7})
		self.assertTrue(character.artifact)

	def test_mcid_reconciliation_binds_text_and_bbox(self) -> None:
		leaf = SemanticNode("leaf", "text", attrs={"mcid": 7}, sources=[SourceRef(page=1, mcids=(7,))])
		document = SemanticDocument([leaf])
		characters = [
			SimpleNamespace(page=1, seq=2, text="B", x0=11.0, y0=2.0, x1=12.0, y1=4.0, mc=({"tag": "P", "mcid": 7},), artifact=False),
			SimpleNamespace(page=1, seq=1, text="A", x0=10.0, y0=2.0, x1=11.0, y1=4.0, mc=({"tag": "P", "mcid": 7},), artifact=False),
		]
		reconcile_tagged_content(document, characters)
		self.assertEqual(leaf.text, "AB")
		self.assertEqual(leaf.sources[0].bbox, (10.0, 2.0, 12.0, 4.0))
		self.assertEqual(leaf.sources[0].glyph_ids, (1, 2))


class SemanticGraphTests(unittest.TestCase):
	def test_validation_catches_duplicate_ids_and_missing_provenance(self) -> None:
		document = SemanticDocument(children=[
			SemanticNode("same", "paragraph"),
			SemanticNode("same", "paragraph", sources=[SourceRef(page=1)]),
		])
		errors = document.validate()
		self.assertTrue(any("duplicate" in error for error in errors))
		self.assertTrue(any("missing provenance" in error for error in errors))

	def test_cycle_is_rejected(self) -> None:
		node = SemanticNode("cycle", "section")
		node.children.append(node)
		self.assertTrue(any("cycle" in error for error in SemanticDocument([node]).validate(False)))

	def test_shared_graph_renders_markdown_and_html(self) -> None:
		factory = NodeFactory()
		source = [SourceRef(page=1, glyph_ids=(1, 2, 3))]
		heading = factory.make("heading", attrs={"level": 1}, sources=source).add(factory.make("text", text="Title", sources=source))
		paragraph = factory.make("paragraph", sources=source).add(
			factory.make("text", text="See ", sources=source),
			factory.make("link", attrs={"href": "https://example.com"}, sources=source).add(factory.make("text", text="example", sources=source)),
		)
		document = SemanticDocument([heading, paragraph], metadata={"title": "Test"})
		markdown = render_semantic_markdown(document)
		html = render_semantic_html(document)
		self.assertIn("# Title", markdown)
		self.assertIn("[example](https://example.com)", markdown)
		self.assertIn("<h1", html)
		self.assertIn('href="https://example.com"', html)

	def test_html_image_markup_preserves_compound_figures_and_inline_caption_semantics(self) -> None:
		source = [SourceRef(page=1, glyph_ids=(1, 2, 3))]
		first = SemanticNode(
			"first-image",
			"image",
			attrs={
				"src": "assets/first.png",
				"alt": "First",
				"display_width_pt": 144.0,
				"link": "https://example.com/details",
			},
			sources=source,
		)
		second = SemanticNode(
			"second-image",
			"image",
			attrs={
				"src": "assets/second.png",
				"alt": "Second",
				"display_height_pt": 96.0,
			},
			sources=source,
		)
		caption = SemanticNode(
			"caption",
			"caption",
			children=[
				SemanticNode(
					"caption-strong",
					"strong",
					children=[SemanticNode("caption-label", "text", text="Figure 1", sources=source)],
					sources=source,
				),
				SemanticNode("caption-space", "text", text=" — ", sources=source),
				SemanticNode(
					"caption-link",
					"link",
					children=[SemanticNode("caption-link-text", "text", text="details", sources=source)],
					attrs={"href": "https://example.com/caption"},
					sources=source,
				),
			],
			sources=source,
		)
		document = SemanticDocument([
			SemanticNode(
				"compound-figure",
				"figure",
				children=[first, second, caption],
				sources=source,
			),
		])
		rendered = render_semantic_markdown(document, image_markup="html")
		self.assertEqual(rendered.count("<img "), 2)
		self.assertIn("width: 144.000pt", rendered)
		self.assertIn("height: 96.000pt", rendered)
		self.assertIn('<strong>Figure 1</strong>', rendered)
		self.assertIn('href="https://example.com/caption"', rendered)
		self.assertIn('href="https://example.com/details"', rendered)

	def test_remote_image_sources_are_suppressed_without_suppressing_links(self) -> None:
		source = [SourceRef(page=1)]
		remote = SemanticNode(
			"remote",
			"image",
			attrs={"src": "https://example.com/tracker.png", "alt": "Remote"},
			sources=source,
		)
		linked_local = SemanticNode(
			"local",
			"image",
			attrs={
				"src": "assets/local.png",
				"alt": "Local",
				"link": "https://example.com/target",
			},
			sources=source,
		)
		document = SemanticDocument([
			SemanticNode("remote-figure", "figure", children=[remote], sources=source),
			SemanticNode("local-figure", "figure", children=[linked_local], sources=source),
		])
		markdown = render_semantic_markdown(document, image_markup="html")
		html_output = render_semantic_html(document)
		for rendered in (markdown, html_output):
			self.assertNotIn("tracker.png", rendered)
			self.assertIn("assets/local.png", rendered)
			self.assertIn("https://example.com/target", rendered)

	def test_aligned_raw_html_fallback_keeps_inline_semantics(self) -> None:
		paragraph = SemanticNode(
			"aligned",
			"paragraph",
			attrs={"alignment": "center"},
			children=[SemanticNode(
				"strong",
				"strong",
				children=[SemanticNode("text", "text", text="Centered label")],
			)],
		)
		rendered = render_semantic_markdown(SemanticDocument([paragraph]))
		self.assertIn("<strong>Centered label</strong>", rendered)
		self.assertNotIn("**Centered label**", rendered)

	def test_html_projects_sectioned_tables_expansions_and_exact_list_ordinals(self) -> None:
		source = [SourceRef(page=1, glyph_ids=(1, 2, 3))]

		def text(node_id: str, value: str) -> SemanticNode:
			return SemanticNode(
				node_id,
				"text",
				text=value,
				sources=source,
			)

		head = SemanticNode(
			"head",
			"table_head",
			children=[
				SemanticNode(
					"head-row",
					"table_row",
					children=[
						SemanticNode(
							"head-cell",
							"table_cell",
							children=[text("head-text", "Term")],
							attrs={"role": "th"},
							sources=source,
						)
					],
					sources=source,
				)
			],
			sources=source,
		)
		body = SemanticNode(
			"body",
			"table_body",
			children=[
				SemanticNode(
					"body-row",
					"table_row",
					children=[
						SemanticNode(
							"body-cell",
							"table_cell",
							children=[text("body-text", "Value")],
							sources=source,
						)
					],
					sources=source,
				)
			],
			sources=source,
		)
		second_body = SemanticNode(
			"second-body",
			"table_body",
			children=[
				SemanticNode(
					"second-body-row",
					"table_row",
					children=[
						SemanticNode(
							"second-body-cell",
							"table_cell",
							children=[text("second-body-text", "Total")],
							sources=source,
						)
					],
					sources=source,
				)
			],
			sources=source,
		)
		table = SemanticNode(
			"sectioned-table",
			"table",
			children=[head, body, second_body],
			sources=source,
		)
		abbreviation = SemanticNode(
			"abbr-paragraph",
			"paragraph",
			children=[text("abbr-text", "WWW")],
			attrs={"expanded_text": "World Wide Web"},
			sources=source,
		)
		ordered = SemanticNode(
			"roman-list",
			"list",
			children=[
				SemanticNode(
					"roman-four",
					"item",
					children=[text("roman-four-text", "Fourth")],
					attrs={"marker": "(IV)"},
					sources=source,
				),
				SemanticNode(
					"roman-six",
					"item",
					children=[text("roman-six-text", "Sixth")],
					attrs={"marker": "(VI)"},
					sources=source,
				),
			],
			attrs={
				"ordered": True,
				"start": 1,
				"marker_style": "upper-roman",
			},
			sources=source,
		)
		document = SemanticDocument(
			[abbreviation, ordered, table],
			metadata={"title": "Rich HTML"},
		)
		rendered = render_semantic_html(document)
		self.assertIn(
			'<abbr title="World Wide Web">WWW</abbr>',
			rendered,
		)
		self.assertIn('<ol type="I" start="4"', rendered)
		self.assertIn('<li value="4"', rendered)
		self.assertIn('<li value="6"', rendered)
		self.assertIn("<thead>", rendered)
		self.assertEqual(rendered.count("<tbody>"), 2)
		self.assertIn('scope="col"', rendered)
		self.assertIn('class="cocoapdf-table-container"', rendered)
		self.assertIn("| Term |", render_semantic_markdown(document))
		self.assertIn("| Value |", render_semantic_markdown(document))
		self.assertIn("| Total |", render_semantic_markdown(document))
		self.assertIn(
			'http-equiv="Content-Security-Policy"',
			rendered,
		)

	def test_lossless_complex_table_gets_html_only_header_scopes(self) -> None:
		source = [SourceRef(page=1)]
		fragment = (
			"<table><thead><tr>"
			'<th rowspan="2">Component</th>'
			'<th colspan="2">Evidence</th>'
			"</tr><tr><th>Geometry</th><th>Tags</th></tr></thead>"
			"<tbody><tr><th>Heading</th><td>Large</td><td>H1</td></tr>"
			"</tbody></table>"
		)
		table = SemanticNode(
			"lossless",
			"table",
			attrs={"_layout_html": fragment},
			sources=source,
		)
		rendered = render_semantic_html(SemanticDocument([table]))
		self.assertIn(
			'<th rowspan="2" scope="col">Component</th>',
			rendered,
		)
		self.assertIn(
			'<th colspan="2" scope="colgroup">Evidence</th>',
			rendered,
		)
		self.assertIn('<th scope="row">Heading</th>', rendered)
		self.assertEqual(table.attrs["_layout_html"], fragment)

	def test_semantic_html_uses_native_structure_and_a_closed_trust_boundary(self) -> None:
		source = [SourceRef(page=1)]
		task = SemanticNode(
			"task",
			"item",
			children=[SemanticNode("task-text", "text", text="Verified", sources=source)],
			attrs={"task": True, "checked": True},
			sources=source,
		)
		task_list = SemanticNode(
			"tasks",
			"list",
			children=[task],
			attrs={"ordered": False},
			sources=source,
		)
		caption = SemanticNode(
			"caption",
			"caption",
			children=[SemanticNode("caption-text", "text", text="Results", sources=source)],
			attrs={"placement": "after"},
			sources=source,
		)
		header = SemanticNode(
			"header",
			"table_cell",
			children=[SemanticNode("header-text", "text", text="Period", sources=source)],
			attrs={"role": "th", "scope": "Column", "colspan": 2},
			sources=source,
		)
		row = SemanticNode(
			"row",
			"table_row",
			children=[header],
			sources=source,
		)
		table = SemanticNode(
			"table",
			"table",
			children=[caption, row],
			attrs={"header_rows": 1},
			sources=source,
		)
		untrusted = SemanticNode(
			"raw",
			"html",
			text="<table><iframe srcdoc=\"unsafe\"></iframe></table>",
			attrs={"trusted_generated": True},
			sources=source,
		)
		document = SemanticDocument(
			[task_list, table, untrusted],
			metadata={"title": "Native HTML", "tagged_pdf": {"language": "en-US"}},
		)
		rendered = render_semantic_html(document)
		self.assertIn('<html lang="en-US">', rendered)
		self.assertIn(
			'<input type="checkbox" disabled checked '
			'aria-label="Checked task: Verified" />',
			rendered,
		)
		self.assertIn('scope="colgroup"', rendered)
		self.assertIn('class="cocoapdf-caption-bottom"', rendered)
		self.assertNotIn("<iframe", rendered)
		self.assertIn("&lt;iframe", rendered)

	def test_task_item_markers_are_suppressed_without_flattening_mixed_or_nested_lists(self) -> None:
		source = [SourceRef(page=1)]

		def text(node_id: str, value: str) -> SemanticNode:
			return SemanticNode(node_id, "text", text=value, sources=source)

		nested = SemanticNode(
			"nested-list",
			"list",
			children=[
				SemanticNode(
					"nested-item",
					"item",
					children=[text("nested-text", "Nested bullet")],
					sources=source,
				)
			],
			attrs={"ordered": False, "marker_style": "disc"},
			sources=source,
		)
		mixed = SemanticNode(
			"mixed-list",
			"list",
			children=[
				SemanticNode(
					"checked-task",
					"item",
					children=[text("checked-text", "Checked"), nested],
					attrs={"task": True, "checked": True},
					sources=source,
				),
				SemanticNode(
					"ordinary-item",
					"item",
					children=[text("ordinary-text", "Ordinary bullet")],
					sources=source,
				),
				SemanticNode(
					"open-task",
					"item",
					children=[text("open-text", "Open")],
					attrs={"task": True, "checked": False},
					sources=source,
				),
			],
			attrs={"ordered": False, "marker_style": "disc"},
			sources=source,
		)
		document = SemanticDocument([mixed])
		rendered = render_semantic_html(document)
		self.assertEqual(rendered.count('class="cocoapdf-task-item"'), 2)
		self.assertEqual(rendered.count('<input type="checkbox" disabled'), 2)
		self.assertIn(
			'<li class="cocoapdf-task-item"',
			rendered,
		)
		self.assertIn(
			'aria-label="Checked task: Checked"',
			rendered,
		)
		self.assertRegex(rendered, r"<li[^>]*>Ordinary bullet</li>")
		self.assertNotRegex(
			rendered,
			r'<li[^>]*class="cocoapdf-task-item"[^>]*>Ordinary bullet',
		)
		self.assertIn(
			'<ul style="list-style-type: disc"',
			rendered,
		)
		self.assertIn(
			'.cocoapdf-task-item { list-style-type: none; }',
			rendered,
		)
		markdown = render_semantic_markdown(document)
		self.assertIn("- [x] Checked", markdown)
		self.assertIn("- Ordinary bullet", markdown)
		self.assertIn("- [ ] Open", markdown)
		self.assertIn("- Nested bullet", markdown)

	def test_generated_html_allowlist_rejects_attribute_smuggling(self) -> None:
		safe_fragments = (
			'<p dir="rtl"><strong>مرحبا</strong></p>',
			'<math display="block"><mrow><mi>x</mi><mo>+</mo>'
			'<mn>1</mn></mrow></math>',
			'<div class="cocoapdf-form-appearance" '
			'data-cocoapdf-kind="printed"><label><input '
			'type="checkbox" checked disabled /> Done</label></div>',
			'<figure class="cocoapdf-figure cocoapdf-align-left">'
			'<img src="assets/plot.svg" alt="Plot" '
			'style="width: 10.000pt; height: 5.000pt; '
			'max-width: 100%; object-fit: contain;" /></figure>',
			'<table><thead><tr><th scope="col">Term</th></tr></thead>'
			'<tbody><tr><td><ul><li>Value</li></ul></td></tr>'
			'</tbody></table>',
		)
		for fragment in safe_fragments:
			self.assertTrue(is_safe_generated_html(fragment), fragment)
		unsafe_fragments = (
			'<p align="center"><img src="x" onerror = "alert(1)" /></p>',
			'<math display="block"><mi href="https://example.com">x</mi></math>',
			'<img src="javascript:alert(1)" alt="" width="1" height="1" />',
			'<table><tr><td><form action="https://example.com">'
			'x</form></td></tr></table>',
			'<div class="cocoapdf-form-appearance" '
			'data-cocoapdf-kind="printed"><label><input '
			'type="checkbox" disabled autofocus /> Done</label></div>',
			'<figure class="cocoapdf-figure cocoapdf-align-left">'
			'<img src="assets/plot.svg" alt="Plot" srcset="remote 2x" '
			'style="width: 10.000pt; height: 5.000pt; '
			'max-width: 100%; object-fit: contain;" /></figure>',
			"<table><td>orphaned cell</td></table>",
			"<table><tr><td><ul>orphaned list text</ul></td></tr></table>",
			'<figure class="cocoapdf-figure cocoapdf-align-left">'
			'<img src="https://example.com/tracker.png" alt="Remote" '
			'style="width: 10.000pt; height: 5.000pt; '
			'max-width: 100%; object-fit: contain;" /></figure>',
		)
		for fragment in unsafe_fragments:
			self.assertFalse(is_safe_generated_html(fragment), fragment)

	def test_complex_table_uses_html_fallback_in_markdown(self) -> None:
		source = [SourceRef(page=1)]
		cell = SemanticNode("cell", "table_cell", text="A", attrs={"colspan": 2}, sources=source)
		row = SemanticNode("row", "table_row", children=[cell], sources=source)
		table = SemanticNode("table", "table", children=[row], attrs={"header_rows": 1, "html_fallback": "<table><tr><th colspan=\"2\">A</th></tr></table>"}, sources=source)
		document = SemanticDocument([table])
		self.assertIn("colspan", render_semantic_markdown(document))
		self.assertIn('colspan="2"', render_semantic_html(document))

	def test_report_contains_real_semantic_nodes(self) -> None:
		document = SemanticDocument([SemanticNode("p1", "paragraph", text="x", sources=[SourceRef(page=1)])])
		report = attach_semantic_document({}, document)
		self.assertTrue(report["semantic_valid"])
		self.assertEqual(report["semantic_node_count"], 1)


class TaggedStructureTests(unittest.TestCase):
	def test_role_map_mcid_actualtext_and_objr(self) -> None:
		page = {"Type": "Page"}
		struct = {
			"RoleMap": {"MyHeading": "H2"},
			"ParentTree": {"Nums": [0, ["parent-zero"]]},
			"K": [{
				"S": "MyHeading", "Pg": page, "ActualText": b"\xfe\xff\x00T\x00i\x00t\x00l\x00e",
				"K": [3, {"Type": "MCR", "Pg": page, "MCID": 4}, {"Type": "OBJR", "Pg": page, "Obj": {"Subtype": "Link"}}],
			}],
		}

		class Document:
			def resolve(self, value):
				return value

			def catalog(self):
				return {"StructTreeRoot": struct}

			def pages(self):
				return [page]

		document = parse_tagged_structure(Document())
		heading = document.children[0]
		self.assertEqual(heading.kind, "heading")
		self.assertEqual(heading.attrs["level"], 2)
		self.assertEqual(heading.text, "Title")
		self.assertEqual([child.attrs.get("mcid") for child in heading.children[:2]], [3, 4])
		self.assertEqual(heading.children[2].kind, "annotation")
		self.assertEqual(document.metadata["parent_tree_keys"], [0])

	def test_role_map_cycle_degrades_without_recursing(self) -> None:
		class Document:
			def resolve(self, value):
				return value

			def catalog(self):
				return {"StructTreeRoot": {"RoleMap": {"A": "B", "B": "A"}, "K": {"S": "A"}}}

			def pages(self):
				return []

		document = parse_tagged_structure(Document())
		self.assertEqual(document.children[0].kind, "unknown")
		self.assertTrue(any("ROLEMAP_CYCLE" in warning for warning in document.warnings))


# ---- region inference corpus ----


class RegionCorpusTests(unittest.TestCase):
	def test_two_column_body_regions(self):
		stream = b"\n".join(
			[
				text_op(72, 720, "Left one", "F1", 10),
				text_op(72, 704, "Left two", "F1", 10),
				text_op(320, 720, "Right one", "F1", 10),
				text_op(320, 704, "Right two", "F1", 10),
				line_op(296, 640, 296, 748, 1),
			]
		)
		result = convert(make_pdf([stream]), ConvertOptions())
		self.assertGreaterEqual(sum(1 for r in result.report["regions"] if r["kind"] == "column"), 2)

	def test_callout_box_region(self):
		stream = b"\n".join(
			[
				text_op(72, 740, "Body before callout.", "F1", 10),
				rect_fill_op(70, 680, 240, 44, 0.94),
				text_op(84, 710, "Important callout text.", "F1", 10),
				text_op(72, 640, "Body after callout.", "F1", 10),
			]
		)
		result = convert(make_pdf([stream]), ConvertOptions())
		self.assertIn("callout", {r["kind"] for r in result.report["regions"]})

	def test_duplicate_callout_regions_are_suppressed(self):
		stream = b"\n".join(
			[
				rect_fill_op(70, 680, 240, 44, 0.94),
				rect_fill_op(70, 680, 240, 44, 0.94),
				text_op(84, 710, "Important callout text.", "F1", 10),
			]
		)
		result = convert(make_pdf([stream]), ConvertOptions())
		callouts = [r for r in result.report["regions"] if r["kind"] == "callout"]
		self.assertEqual(len(callouts), 1)


class AlignedColumnTableTests(unittest.TestCase):
    """Cover the unruled table recovered from repeated body-row alignment.

    The body defines the columns here, so these cases pin the two independent
    signals that keep aligned prose, forms, and cards out of the model.
    """

    @staticmethod
    def _numeric_rows(caption: bytes = b"(Table 9. Annual growth by market.) Tj") -> bytes:
        rows = [
            (b"Cambodia", b"7.5%", b"-0.7%", b"50.6%"),
            (b"Indonesia", b"9.4%", b"29.5%", b"4.7%"),
            (b"Malaysia", b"18.6%", b"7.1%", b"6.9%"),
            (b"Thailand", b"-0.9%", b"18.6%", b"11.4%"),
        ]
        parts = [b"BT /F1 10 Tf 1 0 0 1 72 740 Tm " + caption]
        y = 710
        for name, a, b, c in rows:
            parts.append(
                b"1 0 0 1 72 %d Tm (%s) Tj " % (y, name)
                + b"1 0 0 1 220 %d Tm (%s) Tj " % (y, a)
                + b"1 0 0 1 330 %d Tm (%s) Tj " % (y, b)
                + b"1 0 0 1 440 %d Tm (%s) Tj" % (y, c)
            )
            y -= 20
        parts.append(b"ET")
        return b" ".join(parts)

    def test_captioned_numeric_alignment_becomes_a_table(self) -> None:
        result = convert(one_page_pdf(self._numeric_rows()))
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1)
        self.assertIn("| Cambodia | 7.5% | -0.7% | 50.6% |", result.markdown)
        self.assertIn("| Thailand | -0.9% | 18.6% | 11.4% |", result.markdown)

    def test_numeric_alignment_without_caption_is_not_a_table(self) -> None:
        content = self._numeric_rows(caption=b"(Quarterly notes for the market.) Tj")
        result = convert(one_page_pdf(content))
        self.assertEqual(semantic_nodes(result, "table"), [])
        self.assertIn("Cambodia", result.markdown)

    def test_contents_listing_with_leaders_is_not_a_table(self) -> None:
        parts = [b"BT /F1 10 Tf 1 0 0 1 72 740 Tm (Table 4. Chapter index.) Tj"]
        y = 710
        for name, page in ((b"Alpha", b"11"), (b"Bravo", b"24"), (b"Cairo", b"37"), (b"Delta", b"48")):
            parts.append(
                b"1 0 0 1 72 %d Tm (%s) Tj " % (y, name)
                + b"1 0 0 1 220 %d Tm (..........) Tj " % y
                + b"1 0 0 1 330 %d Tm (%s) Tj " % (y, page)
                + b"1 0 0 1 440 %d Tm (%s) Tj" % (y, page)
            )
            y -= 20
        parts.append(b"ET")
        result = convert(one_page_pdf(b" ".join(parts)))
        self.assertEqual(semantic_nodes(result, "table"), [])

    def test_word_crossing_a_gutter_rejects_the_alignment(self) -> None:
        content = self._numeric_rows()
        # One row carries a phrase that straddles the second gutter, which is
        # what an incidental whitespace run in prose looks like.
        content = content.replace(
            b"1 0 0 1 220 690 Tm (9.4%) Tj ",
            b"1 0 0 1 220 690 Tm (9.4% carried across the gutter here) Tj ",
        )
        result = convert(one_page_pdf(content))
        self.assertEqual(semantic_nodes(result, "table"), [])


class ResidualPhysicalTableRecoveryTests(unittest.TestCase):
    """Pin conservative PDF-native gates added for residual table dialects."""

    @staticmethod
    def _artifact_one_column(*, two_frames: bool = False) -> bytes:
        parts = []
        origins = [72.0, 330.0] if two_frames else [72.0]
        for origin in origins:
            for bottom in (650.0, 600.0, 550.0, 500.0):
                parts.append(rect_fill_op(origin, bottom, 200.0, 50.0, 0.96))
            rules = []
            for y in (700, 650, 600, 550, 500):
                rules.append(b"%d %d 200 0.5 re f" % (origin, y))
            rules.extend(
                [
                    b"%d 500 0.5 200 re f" % origin,
                    b"%d 500 0.5 200 re f" % (origin + 200),
                ]
            )
            parts.append(b"/Artifact BMC 0 g " + b" ".join(rules) + b" EMC")
            for row, y in enumerate((675, 625, 575, 525), 1):
                parts.append(text_op(origin + 12, y, "Entry %d" % row, size=10))
        return make_pdf([b" ".join(parts)])

    @staticmethod
    def _artifact_partial_fill(*, caption: bool = True) -> bytes:
        parts = []
        row_bottoms = (650, 600, 550, 500, 450, 400)
        for row, bottom in enumerate(row_bottoms):
            shade = 0.94 if row % 2 == 0 else 0.98
            for x in (72, 172, 272):
                parts.append(rect_fill_op(x, bottom, 100, 50, shade))
        rules = [
            b"72 700 100 0.5 re f",
            b"172 700 100 0.5 re f",
            b"272 700 100 0.5 re f",
        ]
        for bottom in row_bottoms:
            rules.append(b"172 %d 0.5 50 re f" % bottom)
        parts.append(b"/Artifact BMC 0 g " + b" ".join(rules) + b" EMC")
        for row, y in enumerate((675, 625, 575, 525, 475, 425)):
            first = "Metric" if row == 0 else "Item %d" % row
            second = "Value A" if row == 0 else str(row * 10)
            third = "Value B" if row == 0 else "%d%%" % (row * 5)
            parts.extend(
                [
                    text_op(82, y, first, size=10),
                    text_op(182, y, second, size=10),
                    text_op(282, y, third, size=10),
                ]
            )
        if caption:
            parts.append(text_op(72, 370, "Table 8. Fill-backed values.", size=10))
        return make_pdf([b" ".join(parts)])

    @staticmethod
    def _dense_fragmented(*, preceding_prose: bool = False) -> bytes:
        parts = []
        boundaries = (740, 700, 660, 620, 580, 540, 500, 460, 420)
        for x in (133, 216, 299, 382, 465):
            for upper, lower in zip(boundaries, boundaries[1:]):
                parts.append(line_op(x, lower + 0.6, x, upper - 0.6, 0.5))
        for y in (700, 460):
            for left, right in (
                (50, 130),
                (136, 213),
                (219, 296),
                (302, 379),
                (385, 462),
                (468, 550),
            ):
                parts.append(line_op(left, y, right, y, 0.5))
        if preceding_prose:
            parts.append(text_op(50, 770, "Introductory prose", size=10))
        starts = (60, 143, 226, 309, 392, 475)
        for row, y in enumerate((720, 680, 640, 600, 560, 520, 480, 440)):
            for column, x in enumerate(starts):
                value = "H%d" % column if row == 0 else (
                    "R%d" % row if column == 0 else str(row * 10 + column)
                )
                parts.append(text_op(x, y, value, size=9))
        return make_pdf([b" ".join(parts)])

    @staticmethod
    def _overlaid_image(
        *,
        bold_functions: bool = True,
        image_first: bool = True,
        mixed_preimage_glyph: bool = False,
    ) -> bytes:
        image_op = b"q 532 0 0 550 40 142 cm /Im1 Do Q"
        parts = []
        if mixed_preimage_glyph:
            parts.append(text_op(100, 270, "X", size=8))
        if image_first or mixed_preimage_glyph:
            parts.append(image_op)
        for x, text in zip(
            (50, 180, 315, 450),
            ("Service Stage", "Function Name", "Explanation", "Expected Benefit"),
        ):
            parts.append(text_op(x, 660, text, size=10))
        stages = ("Stage 1", "Stage 2", "", "", "Stage 3", "Stage 4", "Stage 5", "Stage 6")
        for row, (stage, y) in enumerate(zip(stages, (620, 575, 530, 485, 440, 395, 350, 305)), 1):
            if stage:
                parts.append(text_op(50, y, stage, size=9))
            parts.extend(
                [
                    text_op(180, y, "Function %d" % row, font="F2" if bold_functions else "F1", size=9),
                    text_op(315, y, "Explain %d" % row, size=9),
                    text_op(450, y, "Benefit %d" % row, size=9),
                    text_op(315, y - 12, "E-detail %d" % row, size=8),
                    text_op(450, y - 12, "B-detail %d" % row, size=8),
                ]
            )
        if not image_first:
            parts.append(image_op)
        return make_pdf(
            [b" ".join(parts)],
            xobjects={"Im1": image_xobject_rgb(1, 1, b"\xff\xff\xff")},
        )

    @staticmethod
    def _open_internal_grid(*, omit_body_cell: bool = False) -> bytes:
        parts = [
            line_op(30, 360, 930, 360, 0.5),
            line_op(30, 270, 930, 270, 0.5),
            line_op(30, 180, 930, 180, 0.5),
            line_op(124, 50, 124, 360, 0.5),
            line_op(383, 50, 383, 360, 0.5),
            line_op(652, 50, 652, 360, 0.5),
            text_op(145, 380, "OCR", size=10),
            text_op(405, 380, "Recommendation", size=10),
            text_op(675, 380, "Semantic Search", size=10),
        ]
        for row, y in enumerate((320, 230, 120), 1):
            values = ("Band %d" % row, "Alpha %d" % row, "Beta %d" % row, "Gamma %d" % row)
            for column, (x, value) in enumerate(zip((50, 145, 405, 675), values)):
                if omit_body_cell and row == 2 and column == 3:
                    continue
                parts.append(text_op(x, y, value, size=10))
        return make_pdf([b" ".join(parts)], page_size=(960, 540))

    @staticmethod
    def _single_row_booktabs(*, aligned_gutters: bool = True) -> bytes:
        third_body_x = 458 if aligned_gutters else 420
        parts = [
            line_op(60, 700, 540, 700, 0.5),
            line_op(60, 650, 540, 650, 0.5),
            line_op(60, 600, 540, 600, 0.5),
            text_op(80, 675, "Model Name", size=10),
            text_op(250, 675, "Score Value", size=10),
            text_op(420, 675, "Rate Percent", size=10),
            text_op(80, 625, "Alpha Model", size=10),
            text_op(249, 625, "97K", size=10),
            text_op(third_body_x, 625, "88%", size=10),
            text_op(60, 570, "Table 3. One-row summary.", size=10),
        ]
        return make_pdf([b" ".join(parts)])

    @staticmethod
    def _multilevel_booktabs(*, aligned_underlines: bool = True) -> bytes:
        group_start = 98 if aligned_underlines else 130
        parts = [
            line_op(50, 700, 550, 700, 0.5),
            line_op(group_start, 660, 308, 660, 0.5),
            line_op(308, 660, 550, 660, 0.5),
            line_op(50, 620, 550, 620, 0.5),
            line_op(50, 520, 550, 520, 0.5),
            text_op(282, 680, "Training Datasets", size=10),
            text_op(52, 650, "Properties", size=10),
            text_op(177, 650, "Instruction", size=10),
            text_op(407, 650, "Alignment", size=10),
        ]
        for column, x in enumerate((130, 200, 270, 340, 410, 480)):
            parts.append(text_op(x, 630, "H%d" % (column + 1), size=10))
        for row, y in enumerate((580, 540), 1):
            parts.append(text_op(60, y, "P%d" % row, size=10))
            for column, x in enumerate((130, 200, 270, 340, 410, 480), 1):
                parts.append(text_op(x, y, str(row * 10 + column), size=10))
        parts.append(text_op(50, 490, "Table 6. Training comparison.", size=10))
        return make_pdf([b" ".join(parts)])

    def test_unique_fill_backed_one_column_artifact_lattice_is_recovered(self) -> None:
        result = convert(self._artifact_one_column())
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.markdown)
        self.assertEqual(tables[0].attrs["column_count"], 1)
        self.assertEqual(tables[0].attrs["row_count"], 4)
        evidence = next(
            item for item in tables[0].evidence
            if item.kind == "artifact_filled_lattice"
        )
        self.assertTrue(evidence.data["unique_filled_one_column"])
        self.assertEqual(evidence.data["fill_backed_rows"], 4)

    def test_repeated_fill_backed_artifact_frames_remain_cards(self) -> None:
        result = convert(self._artifact_one_column(two_frames=True))
        self.assertEqual(semantic_nodes(result, "table"), [], result.markdown)
        self.assertIn("Entry 1", result.markdown)

    def test_captioned_artifact_partial_fill_grid_is_recovered(self) -> None:
        result = convert(self._artifact_partial_fill())
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.markdown)
        self.assertEqual(tables[0].attrs["column_count"], 3)
        self.assertEqual(tables[0].attrs["row_count"], 6)
        evidence = next(
            item for item in tables[0].evidence
            if item.kind == "artifact_partial_fill_grid"
        )
        self.assertGreaterEqual(evidence.data["fill_backed_rows"], 3)
        self.assertGreaterEqual(evidence.data["numeric_body_rows"], 4)
        self.assertIn("Table 8. Fill-backed values.", result.markdown)
        captions = semantic_nodes(result, "caption")
        self.assertEqual(len(captions), 1)
        self.assertEqual(captions[0].attrs["placement"], "after")
        self.assertTrue(any(source.glyph_ids for source in captions[0].sources))
        self.assertIn('class="cocoapdf-caption-bottom"', result.html)

    def test_artifact_partial_fill_grid_requires_explicit_caption(self) -> None:
        result = convert(self._artifact_partial_fill(caption=False))
        self.assertFalse(any(
            item.kind == "artifact_partial_fill_grid"
            for table in semantic_nodes(result, "table")
            for item in table.evidence
        ))

    def test_page_top_dense_fragmented_numeric_grid_is_recovered(self) -> None:
        result = convert(self._dense_fragmented())
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.markdown)
        self.assertEqual(tables[0].attrs["column_count"], 6)
        self.assertEqual(tables[0].attrs["row_count"], 8)
        evidence = next(
            item for item in tables[0].evidence
            if item.kind == "dense_fragmented_grid"
        )
        self.assertGreaterEqual(evidence.data["fragmented_vertical_boundaries"], 5)
        self.assertGreaterEqual(evidence.data["numeric_body_rows"], 4)

    def test_dense_fragmented_grid_rejects_preceding_prose(self) -> None:
        result = convert(self._dense_fragmented(preceding_prose=True))
        self.assertFalse(any(
            item.kind == "dense_fragmented_grid"
            for table in semantic_nodes(result, "table")
            for item in table.evidence
        ))

    def test_single_body_row_booktabs_uses_matching_multiword_gutters(self) -> None:
        result = convert(self._single_row_booktabs())
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.markdown)
        self.assertEqual(tables[0].attrs["header_rows"], 1)
        self.assertEqual(tables[0].attrs["column_count"], 3)
        self.assertIn("<th", result.html)
        self.assertIn("Model Name", result.html)
        captions = semantic_nodes(result, "caption")
        self.assertEqual(len(captions), 1)
        self.assertEqual(captions[0].attrs["placement"], "after")
        self.assertTrue(any(source.glyph_ids for source in captions[0].sources))
        self.assertIn('class="cocoapdf-caption-bottom"', result.html)
        evidence = next(
            item for item in tables[0].evidence if item.kind == "booktabs"
        )
        self.assertEqual(evidence.data["body_rows"], 1)

    def test_single_body_row_booktabs_rejects_mismatched_gutters(self) -> None:
        result = convert(self._single_row_booktabs(aligned_gutters=False))
        self.assertFalse(any(
            item.kind == "booktabs"
            for table in semantic_nodes(result, "table")
            for item in table.evidence
        ))

    def test_multilevel_booktabs_uses_only_physical_header_spans(self) -> None:
        result = convert(self._multilevel_booktabs())
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.markdown)
        table = tables[0]
        self.assertEqual(table.attrs["header_rows"], 3)
        cells = semantic_nodes(result, "table_cell")
        self.assertTrue(any(cell.attrs.get("rowspan") == 3 for cell in cells))
        self.assertTrue(any(cell.attrs.get("colspan") == 6 for cell in cells))
        evidence = next(
            item for item in table.evidence if item.kind == "multilevel_booktabs"
        )
        self.assertEqual(evidence.data["group_underlines"], 2)
        self.assertGreaterEqual(evidence.data["physical_header_spans"], 4)
        captions = semantic_nodes(result, "caption")
        self.assertEqual(len(captions), 1)
        self.assertEqual(captions[0].attrs["placement"], "after")
        self.assertTrue(any(source.glyph_ids for source in captions[0].sources))
        self.assertIn('class="cocoapdf-caption-bottom"', result.html)

    def test_multilevel_booktabs_rejects_misaligned_group_underlines(self) -> None:
        result = convert(self._multilevel_booktabs(aligned_underlines=False))
        self.assertFalse(any(
            item.kind == "multilevel_booktabs"
            for table in semantic_nodes(result, "table")
            for item in table.evidence
        ))

    def test_overlaid_image_table_uses_glyph_text_for_rowspan_anchor(self) -> None:
        result = convert(self._overlaid_image())
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.markdown)
        table = tables[0]
        self.assertEqual(table.attrs["column_count"], 4)
        self.assertEqual(table.attrs["row_count"], 9)
        evidence = next(
            item for item in table.evidence if item.kind == "overlaid_image_table"
        )
        self.assertTrue(evidence.data["image_region_geometry_only"])
        self.assertFalse(evidence.data["raster_text_used"])
        self.assertTrue(evidence.data["image_painted_before_text"])
        self.assertTrue(evidence.data["image_object_ref"])
        self.assertEqual(len(evidence.data["image_bbox"]), 4)
        self.assertTrue(any(source.object_refs for source in table.sources))
        anchor = next(
            cell for cell in semantic_nodes(result, "table_cell")
            if cell.attrs.get("rowspan") == 3 and cell.attrs.get("col") == 0
        )
        anchor_text = " ".join(
            node.text or "" for node in anchor.walk() if node.kind == "text"
        )
        self.assertEqual(anchor_text, "Stage 3")
        self.assertTrue(anchor.sources)
        self.assertTrue(any(source.glyph_ids for source in anchor.sources))
        span_evidence = next(
            item for item in anchor.evidence
            if item.kind == "geometry_inferred_table_span"
        )
        self.assertEqual(span_evidence.confidence, 0.88)
        self.assertFalse(any(
            item.kind == "explicit_table_span" for item in anchor.evidence
        ))
        self.assertIn('rowspan="3"', result.html)

    def test_overlaid_image_table_requires_bold_function_band(self) -> None:
        result = convert(self._overlaid_image(bold_functions=False))
        self.assertEqual(semantic_nodes(result, "table"), [], result.markdown)

    def test_overlaid_image_table_rejects_text_painted_beneath_image(self) -> None:
        result = convert(self._overlaid_image(image_first=False))
        self.assertFalse(any(
            item.kind == "overlaid_image_table"
            for table in semantic_nodes(result, "table")
            for item in table.evidence
        ))

    def test_overlaid_image_table_rejects_even_one_preimage_glyph(self) -> None:
        result = convert(self._overlaid_image(mixed_preimage_glyph=True))
        self.assertFalse(any(
            item.kind == "overlaid_image_table"
            for table in semantic_nodes(result, "table")
            for item in table.evidence
        ))

    def test_open_internal_grid_encloses_nested_physical_sublattice(self) -> None:
        result = convert(self._open_internal_grid())
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.markdown)
        table = tables[0]
        self.assertEqual(table.attrs["column_count"], 4)
        self.assertEqual(table.attrs["row_count"], 4)
        self.assertEqual(table.attrs["header_rows"], 1)
        evidence = next(
            item for item in table.evidence if item.kind == "open_internal_grid"
        )
        self.assertTrue(evidence.data["empty_corner_header"])
        self.assertTrue(evidence.data["complete_body_occupancy"])
        self.assertIn("<th scope=\"col\">OCR</th>", result.html)
        self.assertTrue(all(cell.sources for cell in semantic_nodes(result, "table_cell")))

    def test_open_internal_grid_requires_every_body_cell(self) -> None:
        result = convert(self._open_internal_grid(omit_body_cell=True))
        self.assertFalse(any(
            item.kind == "open_internal_grid"
            for table in semantic_nodes(result, "table")
            for item in table.evidence
        ))

    def test_same_box_rejected_candidate_cannot_replace_accepted_model(self) -> None:
        converter = Converter(make_pdf([[text_op(72, 720, "Source line")][0]]))
        converter.convert()
        renderer = MarkdownRenderer(converter)
        renderer.lines_by_page = converter.lines_by_page
        line = converter.lines_by_page[1][0]
        box = (60.0, 50.0, 300.0, 100.0)
        accepted = (50.0, "accepted", [line], box)
        rejected = (50.0, "rejected", [line], box)

        def first(_page):
            renderer._partial_table_models[(1, box)] = {
                "model_kind": "accepted_model"
            }
            return [accepted]

        def second(_page):
            renderer._partial_table_models[(1, box)] = {
                "model_kind": "rejected_model"
            }
            return [rejected]

        with patch.object(renderer, "_form_grid_candidates", side_effect=first), patch.object(
            renderer,
            "_artifact_filled_lattice_candidates",
            side_effect=second,
        ):
            candidates = renderer._table_candidates(1)
        self.assertEqual([candidate[1] for candidate in candidates], ["accepted"])
        self.assertEqual(
            renderer._partial_table_models[(1, box)]["model_kind"],
            "accepted_model",
        )


class FilledSidebarRecoveryTests(unittest.TestCase):
    @staticmethod
    def _node_text(node: SemanticNode) -> str:
        return "".join(
            child.text or "" for child in node.walk() if child.kind == "text"
        )

    @staticmethod
    def _main_rows(prefix: str) -> list[bytes]:
        return [
            text_op(
                72,
                680 - index * 30,
                "%s row %d carries ordinary explanatory words." % (prefix, index),
                font="F1",
                size=13,
            )
            for index in range(6)
        ]

    def test_fill_backed_sidebar_is_an_independent_provenanced_heading_stream(self) -> None:
        parts = [
            b"1 0.15 0 rg 454 80 118 630 re f 0 g",
            # Repeated short rules are independent evidence that the narrow
            # edge fill is a structured sidebar, not incidental decoration.
            b"1 1 1 RG 1 w "
            b"462 610 m 564 610 l 462 430 m 564 430 l "
            b"462 250 m 564 250 l S 0 G",
            text_op(72, 740, "Main Study Overview", font="F2", size=16),
            *self._main_rows("Main narrative"),
            text_op(462, 650, "Cellular Cycle", font="F2", size=10),
            text_op(462, 635, "and Replication", font="F2", size=10),
            text_op(462, 610, "A short sidebar", font="F1", size=11),
            text_op(462, 594, "description follows", font="F1", size=11),
            text_op(462, 560, "Mitosis and", font="F2", size=10),
            text_op(462, 545, "Meiosis", font="F2", size=10),
            text_op(462, 520, "Different results", font="F1", size=11),
            text_op(462, 504, "are summarized here", font="F1", size=11),
        ]
        result = convert(make_pdf([b"\n".join(parts)]))

        headings = {
            self._node_text(node): node
            for node in semantic_nodes(result, "heading")
        }
        for expected in (
            "Main Study Overview",
            "Cellular Cycle and Replication",
            "Mitosis and Meiosis",
        ):
            self.assertIn(expected, headings, result.markdown)
        self.assertLess(
            result.markdown.index("Main narrative row 5"),
            result.markdown.index("Cellular Cycle and Replication"),
        )
        self.assertLess(
            result.markdown.index("description follows"),
            result.markdown.index("Mitosis and Meiosis"),
        )
        for expected in ("Cellular Cycle and Replication", "Mitosis and Meiosis"):
            heading = headings[expected]
            self.assertEqual({source.page for source in heading.sources}, {1})
            self.assertTrue(
                {glyph_id for source in heading.sources for glyph_id in source.glyph_ids},
                heading.to_dict(),
            )
        self.assertEqual(semantic_nodes(result, "table"), [])
        self.assertEqual(semantic_nodes(result, "list"), [])
        self.assertTrue(result.report["semantic_valid"], result.report["semantic_errors"])

    def test_unanchored_edge_fill_does_not_invent_a_sidebar_or_heading(self) -> None:
        side_text = [
            "Sidebar Emphasis",
            "continues here",
            "A short note follows",
            "with ordinary wording",
            "Another note remains",
            "inside the decoration",
        ]
        parts = [
            b"1 0.15 0 rg 454 80 118 630 re f 0 g",
            text_op(72, 740, "Context before the decorative band.", size=13),
        ]
        for index, (main, side) in enumerate(
            zip(self._main_rows("Main"), side_text)
        ):
            parts.append(main)
            parts.append(
                text_op(
                    462,
                    680 - index * 30,
                    side,
                    font="F2" if index < 2 else "F1",
                    size=10 if index < 2 else 11,
                )
            )
        result = convert(make_pdf([b"\n".join(parts)]))

        heading_texts = {
            self._node_text(node) for node in semantic_nodes(result, "heading")
        }
        self.assertNotIn("Sidebar Emphasis continues here", heading_texts)
        paragraph_nodes = semantic_nodes(result, "paragraph")
        paragraph_texts = [self._node_text(node) for node in paragraph_nodes]
        self.assertLess(
            next(index for index, text in enumerate(paragraph_texts) if "Sidebar Emphasis" in text),
            next(index for index, text in enumerate(paragraph_texts) if "Main row 1" in text),
            paragraph_texts,
        )
        paragraphs = [
            node
            for node in paragraph_nodes
            if "Sidebar Emphasis" in self._node_text(node)
        ]
        self.assertTrue(paragraphs, result.semantic.to_dict())
        self.assertTrue(any(source.glyph_ids for source in paragraphs[0].sources))
        self.assertEqual(semantic_nodes(result, "table"), [])
        self.assertEqual(semantic_nodes(result, "list"), [])

    def test_ruled_edge_grid_is_not_reinterpreted_as_a_sidebar(self) -> None:
        parts = [
            b"0.2 0.6 0.9 rg 454 80 118 630 re f 0 g",
            b"1 1 1 RG 1 w "
            b"462 610 m 564 610 l 462 430 m 564 430 l "
            b"462 250 m 564 250 l "
            # Long internal verticals are grid/form evidence and veto the
            # otherwise sidebar-like fill, text, and horizontal separators.
            b"510 200 m 510 600 l 540 200 m 540 600 l S 0 G",
            text_op(72, 740, "Context before the ruled edge panel.", size=13),
        ]
        side_text = [
            "Panel Emphasis",
            "continues here",
            "Ruled panel note two",
            "Ruled panel note three",
            "Ruled panel note four",
            "Ruled panel note five",
        ]
        for index, (main, side) in enumerate(
            zip(self._main_rows("Main ruled"), side_text)
        ):
            parts.append(main)
            parts.append(
                text_op(
                    462,
                    680 - index * 30,
                    side,
                    font="F2" if index < 2 else "F1",
                    size=10 if index < 2 else 11,
                )
            )
        result = convert(make_pdf([b"\n".join(parts)]))

        heading_texts = {
            self._node_text(node) for node in semantic_nodes(result, "heading")
        }
        self.assertNotIn("Panel Emphasis continues here", heading_texts)
        paragraph_texts = [
            self._node_text(node) for node in semantic_nodes(result, "paragraph")
        ]
        self.assertLess(
            next(index for index, text in enumerate(paragraph_texts) if "Panel Emphasis" in text),
            next(index for index, text in enumerate(paragraph_texts) if "Main ruled row 1" in text),
            paragraph_texts,
        )
        self.assertEqual(semantic_nodes(result, "table"), [])
        self.assertEqual(semantic_nodes(result, "list"), [])


class NumberedHangingDisplayHeadingTests(unittest.TestCase):
    @staticmethod
    def _body() -> list[bytes]:
        return [
            text_op(
                72,
                678,
                "According to the survey, ordinary explanatory prose follows here.",
                font="F1",
                size=11,
            ),
            text_op(
                72,
                662,
                "A second body line confirms the regular reading flow.",
                font="F1",
                size=11,
            ),
            text_op(
                72,
                646,
                "A third body line stabilizes the body font size.",
                font="F1",
                size=11,
            ),
        ]

    @staticmethod
    def _node_text(node: SemanticNode) -> str:
        return "".join(
            child.text or "" for child in node.walk() if child.kind == "text"
        )

    def test_numbered_hanging_display_wrap_is_one_provenanced_heading(self) -> None:
        expected = (
            "3. Perspective of supply and demand balance of wood pellets and cost "
            "structure in Japan"
        )
        content = b" ".join([
            # The marker occupies its own field. Both physical title lines begin
            # at x=100, while the complete semantic heading begins at x=72.
            text_op(72, 720, "3.", font="F2", size=13),
            text_op(
                100,
                720,
                "Perspective of supply and demand balance of wood pellets and cost",
                font="F2",
                size=13,
            ),
            text_op(100, 702, "structure in Japan", font="F2", size=13),
            *self._body(),
        ])
        result = convert(make_pdf([content]))

        headings = semantic_nodes(result, "heading")
        self.assertEqual(len(headings), 1, result.markdown)
        heading = headings[0]
        self.assertEqual(self._node_text(heading), expected)
        heading_lines = [
            line for line in result.markdown.splitlines() if line.startswith("#")
        ]
        self.assertEqual(len(heading_lines), 1, result.markdown)
        self.assertTrue(heading_lines[0].endswith(expected), result.markdown)
        self.assertLess(
            result.markdown.index(expected),
            result.markdown.index("According to the survey"),
        )
        semantic_order = [
            node.kind
            for node in result.semantic.walk()
            if node.kind in {"heading", "paragraph"}
        ]
        self.assertEqual(semantic_order[:2], ["heading", "paragraph"])
        glyph_ids = {
            glyph_id
            for source in heading.sources
            for glyph_id in source.glyph_ids
        }
        self.assertGreaterEqual(
            len(glyph_ids),
            len(expected.replace(" ", "")),
            heading.to_dict(),
        )
        self.assertEqual({source.page for source in heading.sources}, {1})
        self.assertEqual(semantic_nodes(result, "list"), [])
        self.assertEqual(semantic_nodes(result, "table"), [])
        self.assertTrue(result.report["semantic_valid"], result.report["semantic_errors"])

    def test_ordered_list_hanging_wrap_remains_a_list(self) -> None:
        content = b" ".join([
            text_op(72, 720, "3.", font="F2", size=13),
            text_op(100, 720, "Required supporting documents", font="F2", size=13),
            text_op(100, 702, "submitted with the application", font="F2", size=13),
            text_op(72, 678, "4.", font="F2", size=13),
            text_op(100, 678, "Approval records remain attached", font="F2", size=13),
            text_op(100, 660, "for final review", font="F2", size=13),
            text_op(
                72,
                630,
                "Ordinary prose establishes the body size for this fixture.",
                font="F1",
                size=11,
            ),
            text_op(
                72,
                614,
                "A second ordinary line keeps the body-size mode stable.",
                font="F1",
                size=11,
            ),
        ])
        result = convert(make_pdf([content]))

        self.assertEqual(semantic_nodes(result, "heading"), [], result.markdown)
        lists = semantic_nodes(result, "list")
        self.assertEqual(len(lists), 1, result.semantic.to_dict())
        self.assertTrue(lists[0].attrs["ordered"])
        items = semantic_nodes(result, "item")
        self.assertEqual([item.attrs["marker"] for item in items], [3, 4])
        self.assertLess(
            result.markdown.index("submitted with the application"),
            result.markdown.index("Approval records remain attached"),
        )
        self.assertTrue(all(source.glyph_ids for item in items for source in item.sources))
        self.assertEqual(semantic_nodes(result, "table"), [])

    def test_wrapped_numbered_figure_caption_remains_figure_content(self) -> None:
        content = b" ".join([
            text_op(
                72,
                700,
                "Figure 3. Perspective of supply and demand balance of wood pellets and cost",
                font="F1",
                size=11,
            ),
            text_op(126, 684, "structure in Japan", font="F1", size=11),
            b"q 360 0 0 100 110 560 cm /Im1 Do Q",
            text_op(
                72,
                520,
                "Ordinary prose follows the preserved figure and its caption.",
                font="F1",
                size=11,
            ),
            text_op(
                72,
                504,
                "A second body line keeps the document body size stable.",
                font="F1",
                size=11,
            ),
        ])
        result = convert(
            make_pdf(
                [content],
                xobjects={"Im1": image_xobject_rgb(2, 2, b"\x33\x66\x99" * 4)},
            )
        )

        self.assertEqual(semantic_nodes(result, "heading"), [], result.markdown)
        figures = semantic_nodes(result, "figure")
        self.assertEqual(len(figures), 1, result.semantic.to_dict())
        self.assertIn("Figure 3.", result.markdown)
        self.assertLess(
            result.markdown.index("Figure 3."),
            result.markdown.index("Ordinary prose follows"),
        )
        self.assertEqual(semantic_nodes(result, "list"), [])
        self.assertEqual(semantic_nodes(result, "table"), [])

    def test_standalone_chapter_marker_joins_its_display_title(self) -> None:
        content = b" ".join([
            text_op(72, 740, "4", font="F2", size=25),
            text_op(72, 706, "Basis Fields", font="F2", size=17),
            text_op(72, 660, "Ordinary explanatory prose begins below the title.", size=11),
            text_op(72, 644, "A second body line stabilizes the document font size.", size=11),
            text_op(72, 628, "A third body line confirms the governed content block.", size=11),
        ])
        result = convert(make_pdf([content]))

        headings = semantic_nodes(result, "heading")
        self.assertEqual(len(headings), 1, result.markdown)
        self.assertEqual(self._node_text(headings[0]), "4 Basis Fields")
        self.assertTrue(all(source.glyph_ids for source in headings[0].sources))
        self.assertIn('id="4-basis-fields"', result.html)
        self.assertEqual(semantic_nodes(result, "table"), [])
        self.assertTrue(result.report["semantic_valid"], result.report["semantic_errors"])

    def test_repeated_display_markers_do_not_merge_navigation_peers(self) -> None:
        content = b" ".join([
            text_op(72, 740, "1", font="F2", size=25),
            text_op(72, 706, "Installation", font="F2", size=17),
            text_op(72, 650, "2", font="F2", size=25),
            text_op(72, 616, "Configuration", font="F2", size=17),
            text_op(96, 570, "Ordinary prose uses a deliberately different content margin.", size=11),
            text_op(96, 554, "A second ordinary line keeps the size mode stable.", size=11),
        ])
        result = convert(make_pdf([content]))

        heading_texts = [self._node_text(node) for node in semantic_nodes(result, "heading")]
        self.assertNotIn("1 Installation", heading_texts)
        self.assertNotIn("2 Configuration", heading_texts)
        self.assertEqual(semantic_nodes(result, "table"), [])

    def test_numbered_outdented_title_continuation_is_one_heading(self) -> None:
        expected = (
            "1. Shipping as a vector for marine species "
            "List of regional ports is provided in Appendix 3"
        )
        content = b" ".join([
            text_op(90, 720, "1. Shipping as a vector for marine species", font="F2", size=12),
            text_op(72, 698, "List of regional ports is provided in Appendix 3", font="F2", size=12),
            text_op(72, 676, "Ordinary prose begins at the full content margin below it.", size=12),
            text_op(72, 654, "A second body line confirms the producer's regular leading.", size=12),
            text_op(72, 632, "A third ordinary line stabilizes the body-size mode.", size=12),
        ])
        result = convert(make_pdf([content]))

        headings = semantic_nodes(result, "heading")
        self.assertEqual(len(headings), 1, result.markdown)
        self.assertEqual(self._node_text(headings[0]), expected)
        self.assertTrue(all(source.glyph_ids for source in headings[0].sources))
        self.assertEqual(semantic_nodes(result, "list"), [])
        self.assertEqual(semantic_nodes(result, "table"), [])

    def test_outdented_bold_subtitle_without_governed_margin_stays_separate(self) -> None:
        content = b" ".join([
            text_op(90, 720, "1. Shipping policy and operational controls", font="F2", size=12),
            text_op(72, 698, "Background notes for participating organizations", font="F2", size=12),
            text_op(108, 676, "Ordinary prose starts on a distinct nested content margin.", size=12),
            text_op(108, 654, "A second body line confirms that independent margin.", size=12),
            text_op(108, 632, "A third ordinary line keeps the body-size mode stable.", size=12),
        ])
        result = convert(make_pdf([content]))

        heading_texts = [self._node_text(node) for node in semantic_nodes(result, "heading")]
        self.assertNotIn(
            "1. Shipping policy and operational controls Background notes for participating organizations",
            heading_texts,
        )
        self.assertEqual(semantic_nodes(result, "table"), [])


class SideDisplayProseColumnTests(unittest.TestCase):
    @staticmethod
    def _right_column() -> list[bytes]:
        return [
            text_op(
                330,
                740 - index * 20,
                "Right article row %02d carries ordinary prose." % index,
                size=10,
            )
            for index in range(28)
        ]

    @staticmethod
    def _node_text(node: SemanticNode) -> str:
        return "".join(child.text or "" for child in node.walk() if child.kind == "text")

    def test_display_title_rail_precedes_the_independent_prose_stream(self) -> None:
        content = b" ".join([
            text_op(72, 740, "Executive", font="F2", size=30),
            text_op(72, 700, "Summary", font="F2", size=30),
            *self._right_column(),
        ])
        result = convert(make_pdf([content]))

        headings = semantic_nodes(result, "heading")
        self.assertEqual(len(headings), 1, result.markdown)
        self.assertEqual(self._node_text(headings[0]), "Executive Summary")
        self.assertLess(result.markdown.index("Executive Summary"), result.markdown.index("Right article row 00"))
        self.assertIn('id="executive-summary"', result.html)
        self.assertTrue(all(source.glyph_ids for source in headings[0].sources))
        self.assertEqual(semantic_nodes(result, "table"), [])
        self.assertTrue(result.report["semantic_valid"], result.report["semantic_errors"])

    def test_left_prose_rows_veto_the_sparse_title_rail_model(self) -> None:
        content = b" ".join([
            text_op(72, 740, "Executive", font="F2", size=30),
            text_op(72, 700, "Summary", font="F2", size=30),
            *self._right_column(),
            text_op(72, 620, "Left card row zero carries prose.", size=10),
            text_op(72, 600, "Left card row one carries prose.", size=10),
            text_op(72, 580, "Left card row two carries prose.", size=10),
        ])
        result = convert(make_pdf([content]))

        self.assertLess(
            result.markdown.index("Right article row 00"),
            result.markdown.index("Left card row zero"),
            result.markdown,
        )
        self.assertEqual(semantic_nodes(result, "table"), [])


class TriFoldBrochureTests(unittest.TestCase):
    @staticmethod
    def _panel_text(x: float, y: float, text: str, *, font: str = "F1", size: float = 11) -> bytes:
        color = b"1 g " if x < 264 else b"0 g "
        return color + text_op(x, y, text, font=font, size=size)

    @classmethod
    def _brochure_pdf(
        cls,
        *,
        complex_background: bool = True,
        grid_rules: bool = False,
    ) -> bytes:
        if complex_background:
            background = (
                b"0.08 0.35 0.65 rg 0 0 m 245 0 l 250 35 l 244 70 l "
                b"250 105 l 244 140 l 250 175 l 244 210 l 250 245 l "
                b"244 280 l 250 315 l 244 350 l 250 385 l 244 420 l "
                b"250 455 l 244 490 l 250 525 l 244 560 l 250 612 l "
                b"0 612 l h f"
            )
        else:
            background = b"0.08 0.35 0.65 rg 0 0 250 612 re f"
        parts = [
            background,
            b"q 100 0 0 100 340 235 cm /ImMiddle Do Q",
            b"q 100 0 0 150 630 60 cm /ImRight Do Q",
            cls._panel_text(48, 560, "LOCAL ACTION", font="F2", size=18),
            cls._panel_text(322, 560, "GARDEN FIELD", font="F2", size=18),
            cls._panel_text(568, 560, "BUILD COMMUNITY", font="F2", size=18),
            cls._panel_text(65, 536, "HANDBOOK", font="F2", size=18),
            cls._panel_text(362, 536, "NOTES", font="F2", size=18),
            cls._panel_text(650, 536, "&", font="F2", size=18),
            cls._panel_text(40, 512, "Plan with care", size=11),
            cls._panel_text(300, 512, "Practical methods", size=11),
            cls._panel_text(588, 512, "SHARE SKILLS", font="F2", size=18),
            cls._panel_text(38, 480, "Neighbors shape places.", size=11),
            cls._panel_text(292, 480, "Soil supports healthy roots.", size=11),
            cls._panel_text(558, 480, "Skills strengthen a block.", size=11),
            cls._panel_text(38, 458, "Small steps build trust.", size=11),
            cls._panel_text(292, 458, "Water carefully each week.", size=11),
            cls._panel_text(558, 458, "Share tools with friends.", size=11),
            cls._panel_text(38, 436, "For project leads:", font="F2", size=11),
            cls._panel_text(292, 436, "Track seasonal changes.", size=11),
            cls._panel_text(558, 436, "Meet local partners often.", size=11),
            cls._panel_text(38, 414, "Invite every neighbor.", size=11),
            cls._panel_text(292, 414, "Record lessons learned.", size=11),
            cls._panel_text(620, 414, "CC BY-SA, 2024", size=8),
        ]
        if grid_rules:
            parts.extend([
                line_op(264, 40, 264, 572, width=1),
                line_op(528, 40, 528, 572, width=1),
            ])
        return make_pdf(
            [b"\n".join(parts)],
            page_size=(792, 612),
            xobjects={
                "ImMiddle": image_xobject_rgb(2, 2, b"\x55\x99\x55" * 4),
                "ImRight": image_xobject_rgb(2, 2, b"\x99\x77\x44" * 4),
            },
        )

    @staticmethod
    def _node_text(node: SemanticNode) -> str:
        return "".join(child.text or "" for child in node.walk() if child.kind == "text").strip()

    def test_landscape_brochure_recovers_panel_order_headings_images_and_provenance(self) -> None:
        result = convert(
            self._brochure_pdf(),
            ConvertOptions(assets_dir="assets", image_markup="auto"),
        )

        headings = [self._node_text(node) for node in semantic_nodes(result, "heading")]
        self.assertIn("LOCAL ACTION HANDBOOK", headings, result.markdown)
        self.assertIn("GARDEN FIELD NOTES", headings, result.markdown)
        self.assertIn("BUILD COMMUNITY & SHARE SKILLS", headings, result.markdown)
        self.assertIn("For project leads:", headings, result.markdown)
        self.assertNotIn("CC BY-SA, 2024", headings)
        self.assertLess(result.markdown.index("Invite every neighbor"), result.markdown.index("GARDEN FIELD NOTES"))
        self.assertLess(result.markdown.index("Record lessons learned"), result.markdown.index("BUILD COMMUNITY"))
        self.assertEqual(semantic_nodes(result, "table"), [])
        self.assertEqual(semantic_nodes(result, "list"), [])

        images = semantic_nodes(result, "image")
        self.assertEqual(len(images), 2, result.semantic.to_dict())
        self.assertTrue(all(image.sources for image in images))
        self.assertTrue(all(source.object_refs for image in images for source in image.sources))
        self.assertEqual(len(result.assets), 2)
        self.assertEqual(result.html.count("<img "), 2)
        self.assertIn('id="build-community-share-skills"', result.html)
        self.assertIn("&amp;", result.html)
        self.assertTrue(result.report["semantic_valid"], result.report["semantic_errors"])
        self.assertFalse(result.report["ocr_used"])
        self.assertFalse(result.report["image_text_extraction_attempted"])
        payload = json.loads(_format_payload(result, "json"))
        self.assertTrue(payload["semantic_document"]["children"])
        serialized = json.dumps(payload["semantic_document"], sort_keys=True)
        self.assertIn('"anchor": "build-community-share-skills"', serialized)
        self.assertIn('"text": "SHARE SKILLS"', serialized)
        self.assertIn('"kind": "image"', serialized)

    def test_ruled_three_column_dashboard_is_not_classified_as_a_brochure(self) -> None:
        result = convert(self._brochure_pdf(grid_rules=True))

        headings = [self._node_text(node) for node in semantic_nodes(result, "heading")]
        self.assertNotIn("For project leads:", headings, result.markdown)
        self.assertLess(result.markdown.index("GARDEN FIELD"), result.markdown.index("BUILD COMMUNITY"))
        self.assertLess(result.markdown.index("BUILD COMMUNITY"), result.markdown.index("HANDBOOK"))
        self.assertEqual(semantic_nodes(result, "table"), [])

    def test_plain_column_fill_is_insufficient_design_evidence(self) -> None:
        result = convert(self._brochure_pdf(complex_background=False))

        headings = [self._node_text(node) for node in semantic_nodes(result, "heading")]
        self.assertNotIn("For project leads:", headings, result.markdown)
        self.assertNotIn("BUILD COMMUNITY & SHARE SKILLS", headings, result.markdown)
        self.assertEqual(semantic_nodes(result, "table"), [])


class TaggedGeometryAuthorityRegressionTests(unittest.TestCase):
    @staticmethod
    def _block(node_id: str, page: int, y: float) -> SemanticNode:
        return SemanticNode(
            id=node_id,
            kind="paragraph",
            text=node_id,
            sources=[SourceRef(page=page, bbox=(72.0, y, 240.0, y + 12.0))],
        )

    def test_unbound_and_cross_page_nodes_anchor_tagged_root_order(self) -> None:
        first = self._block("first", 1, 72.0)
        unbound = self._block("unbound", 1, 100.0)
        second_page = self._block("second-page", 2, 72.0)
        page_break = SemanticNode(
            id="page-break",
            kind="page_break",
            attrs={"page": 2},
        )
        geometric = SemanticDocument([first, unbound, page_break, second_page])
        tag_first = SemanticNode(id="tag-first", kind="paragraph")
        tag_second = SemanticNode(id="tag-second", kind="paragraph")
        tagged = SemanticDocument([tag_second, tag_first])

        _apply_tagged_order(
            geometric,
            tagged,
            {"tag-first": first, "tag-second": second_page},
        )

        self.assertEqual(
            [node.id for node in geometric.children],
            ["first", "unbound", "page-break", "second-page"],
        )

    def test_fully_bound_same_page_cohort_can_follow_verified_tag_order(self) -> None:
        first = self._block("first", 1, 72.0)
        second = SemanticNode(
            id="second",
            kind="paragraph",
            text="second",
            sources=[SourceRef(page=1, bbox=(300.0, 72.0, 500.0, 84.0))],
        )
        geometric = SemanticDocument([first, second])
        tag_first = SemanticNode(id="tag-first", kind="paragraph")
        tag_second = SemanticNode(id="tag-second", kind="paragraph")

        _apply_tagged_order(
            geometric,
            SemanticDocument([tag_second, tag_first]),
            {"tag-first": first, "tag-second": second},
        )

        self.assertEqual([node.id for node in geometric.children], ["second", "first"])

    def test_tag_order_cannot_invert_strict_single_column_geometry(self) -> None:
        first = self._block("first", 1, 72.0)
        second = self._block("second", 1, 100.0)
        geometric = SemanticDocument([first, second])
        tag_first = SemanticNode(id="tag-first", kind="paragraph")
        tag_second = SemanticNode(id="tag-second", kind="paragraph")

        _apply_tagged_order(
            geometric,
            SemanticDocument([tag_second, tag_first]),
            {"tag-first": first, "tag-second": second},
        )

        self.assertEqual([node.id for node in geometric.children], ["first", "second"])
        self.assertIn("TAGGED_ORDER_GEOMETRY_CONFLICT", geometric.warnings)

    def test_conflicting_tag_order_cannot_rotate_geometric_table_rows(self) -> None:
        header = SemanticNode(
            id="header-row",
            kind="table_row",
            attrs={"row": 0, "role": "header"},
            sources=[SourceRef(page=1, bbox=(72.0, 80.0, 500.0, 100.0))],
        )
        body = SemanticNode(
            id="body-row",
            kind="table_row",
            attrs={"row": 1, "role": "body"},
            sources=[SourceRef(page=1, bbox=(72.0, 100.0, 500.0, 124.0))],
        )
        table = SemanticNode(id="table", kind="table", children=[header, body])
        tag_header = SemanticNode(id="tag-header", kind="table_row")
        tag_body = SemanticNode(id="tag-body", kind="table_row")
        tag_table = SemanticNode(
            id="tag-table",
            kind="table",
            children=[tag_body, tag_header],
        )

        _apply_tagged_child_order(
            SemanticDocument([tag_table]),
            {
                "tag-table": table,
                "tag-header": header,
                "tag-body": body,
            },
        )

        self.assertEqual([row.id for row in table.children], ["header-row", "body-row"])
        self.assertIn("TAGGED_ORDER_GEOMETRY_CONFLICT", table.warnings)
        self.assertTrue(any(
            evidence.kind == "tagged_child_order_rejected"
            for evidence in table.evidence
        ))

    def test_tagged_table_order_applies_only_with_verified_proposed_geometry(self) -> None:
        def cell(node_id: str, role: str, bbox, row: int) -> SemanticNode:
            source = [SourceRef(page=1, bbox=bbox)]
            return SemanticNode(
                id=node_id,
                kind="table_cell",
                attrs={"row": row, "col": 0, "rowspan": 1, "colspan": 1, "role": role},
                sources=source,
            )

        header_cell = cell("header-cell", "th", (72, 80, 500, 100), 1)
        body_cell = cell("body-cell", "td", (72, 110, 500, 130), 0)
        header = SemanticNode(
            id="header-row",
            kind="table_row",
            children=[header_cell],
            attrs={"row": 1, "role": "header"},
            sources=list(header_cell.sources),
        )
        body = SemanticNode(
            id="body-row",
            kind="table_row",
            children=[body_cell],
            attrs={"row": 0, "role": "body"},
            sources=list(body_cell.sources),
        )
        table = SemanticNode(
            id="table",
            kind="table",
            children=[body, header],
            attrs={"header_rows": 0, "row_count": 2, "column_count": 1},
        )
        tag_header = SemanticNode(id="tag-header", kind="table_row")
        tag_body = SemanticNode(id="tag-body", kind="table_row")
        tagged_table = SemanticNode(
            id="tag-table",
            kind="table",
            children=[tag_header, tag_body],
        )
        _apply_tagged_child_order(
            SemanticDocument([tagged_table]),
            {
                "tag-table": table,
                "tag-header": header,
                "tag-body": body,
            },
        )
        self.assertEqual([row.id for row in table.children], ["header-row", "body-row"])
        self.assertEqual([row.attrs["row"] for row in table.children], [0, 1])
        self.assertEqual([header_cell.attrs["row"], body_cell.attrs["row"]], [0, 1])
        self.assertEqual(table.attrs["header_rows"], 1)
        self.assertEqual(table.attrs["row_count"], 2)
        self.assertEqual(table.attrs["column_count"], 1)
        self.assertTrue(any(
            evidence.kind == "tagged_child_order_applied"
            for evidence in table.evidence
        ))

    def test_tagged_cell_order_normalizes_column_coordinates(self) -> None:
        def cell(node_id: str, bbox, col: int, colspan: int = 1) -> SemanticNode:
            return SemanticNode(
                id=node_id,
                kind="table_cell",
                attrs={"row": 0, "col": col, "rowspan": 1, "colspan": colspan, "role": "td"},
                sources=[SourceRef(page=1, bbox=bbox)],
            )

        left = cell("left", (72, 80, 250, 100), 2, colspan=2)
        right = cell("right", (250, 80, 500, 100), 0)
        row = SemanticNode(
            id="row",
            kind="table_row",
            children=[right, left],
            attrs={"row": 0, "role": "body"},
        )
        tag_left = SemanticNode(id="tag-left", kind="table_cell")
        tag_right = SemanticNode(id="tag-right", kind="table_cell")
        tag_row = SemanticNode(
            id="tag-row",
            kind="table_row",
            children=[tag_left, tag_right],
        )
        _apply_tagged_child_order(
            SemanticDocument([tag_row]),
            {"tag-row": row, "tag-left": left, "tag-right": right},
        )
        self.assertEqual([cell.id for cell in row.children], ["left", "right"])
        self.assertEqual([cell.attrs["col"] for cell in row.children], [0, 2])
        self.assertEqual([cell.attrs["row"] for cell in row.children], [0, 0])

    def test_tagged_order_final_normalization_honors_rowspans(self) -> None:
        spanning = SemanticNode(
            id="spanning",
            kind="table_cell",
            attrs={"row": 0, "col": 0, "rowspan": 2, "colspan": 1, "role": "th"},
        )
        heading = SemanticNode(
            id="heading",
            kind="table_cell",
            attrs={"row": 0, "col": 1, "rowspan": 1, "colspan": 1, "role": "th"},
        )
        value = SemanticNode(
            id="value",
            kind="table_cell",
            attrs={"row": 1, "col": 0, "rowspan": 1, "colspan": 1, "role": "td"},
        )
        table = SemanticNode(
            id="table",
            kind="table",
            children=[
                SemanticNode(
                    id="header-row",
                    kind="table_row",
                    children=[spanning, heading],
                    attrs={"row": 0, "role": "header"},
                ),
                SemanticNode(
                    id="body-row",
                    kind="table_row",
                    children=[value],
                    attrs={"row": 1, "role": "body"},
                ),
            ],
            attrs={"header_rows": 1, "row_count": 2, "column_count": 2},
        )
        tagged_table = SemanticNode(id="tagged-table", kind="table")
        _apply_tagged_child_order(
            SemanticDocument([tagged_table]),
            {"tagged-table": table},
        )
        self.assertEqual(value.attrs["col"], 1)
        self.assertEqual(table.attrs["column_count"], 2)

    def test_partial_actual_text_never_discards_unowned_geometry(self) -> None:
        def geometric(keys) -> SemanticNode:
            return SemanticNode(
                id="geometry",
                kind="paragraph",
                children=[
                    SemanticNode(
                        id="source-%d" % key,
                        kind="text",
                        text="Source%d" % key,
                        sources=[SourceRef(page=1, mcids=(key,))],
                    )
                    for key in keys
                ],
                sources=[SourceRef(page=1, mcids=tuple(keys))],
            )

        def tagged(keys) -> SemanticNode:
            return SemanticNode(
                id="tagged",
                kind="paragraph",
                text="Replacement",
                attrs={"actual_text": True},
                children=[
                    SemanticNode(
                        id="tag-key-%d" % key,
                        kind="text",
                        attrs={"mcid": key},
                        sources=[SourceRef(page=1, mcids=(key,))],
                    )
                    for key in keys
                ],
            )

        for geometry_keys, tag_keys in (((0, 1), (0,)), ((0, 1, 2), (0, 1))):
            with self.subTest(geometry_keys=geometry_keys, tag_keys=tag_keys):
                node = geometric(geometry_keys)
                tag = tagged(tag_keys)
                if len(tag_keys) * 2 <= len(geometry_keys):
                    self.assertLess(_binding_score(tag, node, len(tag_keys)), 0.55)
                _apply_tagged_prior(node, tag, 1.0)
                self.assertEqual(node_text(node), "".join("Source%d" % key for key in geometry_keys))
                self.assertIn("TAGGED_ACTUALTEXT_OWNERSHIP_CONFLICT", node.warnings)
                self.assertTrue(any(
                    evidence.kind == "tagged_actual_text_rejected"
                    for evidence in node.evidence
                ))

        exact = geometric((0, 1))
        _apply_tagged_prior(exact, tagged((0, 1)), 1.0)
        self.assertEqual(node_text(exact), "Replacement")
        self.assertTrue(exact.attrs["actual_text"])

    def test_unvalidated_parent_tree_cannot_materialize_a_list(self) -> None:
        source = [SourceRef(page=1, mcids=(0, 1), bbox=(72, 80, 300, 112))]
        paragraph = SemanticNode(
            id="geometry",
            kind="paragraph",
            text="Geometry-owned text",
            sources=source,
        )
        tagged_items = []
        for mcid, validated in ((0, True), (1, False)):
            leaf_source = [SourceRef(page=1, mcids=(mcid,))]
            tagged_leaf = SemanticNode(
                id="tagged-leaf-%d" % mcid,
                kind="text",
                text="Tagged text %d" % mcid,
                attrs={
                    "mcid": mcid,
                    "parent_tree_validated": validated,
                    "struct_parents": 0,
                },
                sources=leaf_source,
            )
            tagged_items.append(SemanticNode(
                id="tagged-item-%d" % mcid,
                kind="item",
                children=[tagged_leaf],
            ))
        tagged_list = SemanticNode(id="tagged-list", kind="list", children=tagged_items)
        geometric = SemanticDocument([paragraph])
        conflicts = []
        _materialize_tagged_structures(
            geometric,
            SemanticDocument([tagged_list]),
            {},
            {(1, 0): [paragraph], (1, 1): [paragraph]},
            conflicts,
        )
        self.assertEqual([node.kind for node in geometric.children], ["paragraph"])
        self.assertEqual(
            conflicts,
            ["TAGGED_STRUCTURE_UNVALIDATED_PARENTTREE:tagged-list"],
        )

    def test_validated_parent_tree_materialization_records_authorized_claims(self) -> None:
        geometry = []
        tagged_items = []
        by_mcid = {}
        for mcid, text in ((0, "Alpha"), (1, "Bravo")):
            source = [SourceRef(page=1, mcids=(mcid,), bbox=(72, 80 + mcid * 20, 300, 92 + mcid * 20))]
            paragraph = SemanticNode(
                id="geometry-%d" % mcid,
                kind="paragraph",
                text=text,
                sources=source,
            )
            geometry.append(paragraph)
            by_mcid[(1, mcid)] = [paragraph]
            leaf = SemanticNode(
                id="leaf-%d" % mcid,
                kind="text",
                text=text,
                attrs={
                    "mcid": mcid,
                    "parent_tree_validated": True,
                    "struct_parents": 4,
                },
                sources=source,
            )
            tagged_items.append(SemanticNode(
                id="item-%d" % mcid,
                kind="item",
                children=[leaf],
            ))
        tagged_list = SemanticNode(id="validated-list", kind="list", children=tagged_items)
        document = SemanticDocument(geometry)
        bindings = {}
        conflicts = []
        _materialize_tagged_structures(
            document,
            SemanticDocument([tagged_list]),
            bindings,
            by_mcid,
            conflicts,
        )
        self.assertEqual(conflicts, [])
        self.assertEqual([node.kind for node in document.children], ["list"])
        evidence = next(
            item for item in document.children[0].evidence
            if item.kind == "tagged_structure_parent_tree_validated"
        )
        self.assertEqual(evidence.data["claim_count"], 2)
        self.assertEqual(
            [(claim["page"], claim["mcid"]) for claim in evidence.data["authorized_mcid_claims"]],
            [(1, 0), (1, 1)],
        )

    def test_duplicate_or_stream_scoped_mcid_claims_cannot_materialize(self) -> None:
        paragraph = SemanticNode(
            id="geometry",
            kind="paragraph",
            text="One source block",
            sources=[SourceRef(page=1, mcids=(0,), bbox=(72, 80, 300, 92))],
        )

        def tagged_list(*, stream_ref: str = "") -> SemanticNode:
            items = []
            for index in range(2):
                attrs = {
                    "mcid": 0,
                    "parent_tree_validated": True,
                    "struct_parents": 7,
                }
                if stream_ref:
                    attrs["stream_ref"] = stream_ref
                leaf = SemanticNode(
                    id="leaf-%d" % index,
                    kind="text",
                    text="Repeated",
                    attrs=attrs,
                    sources=[SourceRef(page=1, mcids=(0,))],
                )
                items.append(SemanticNode(id="item-%d" % index, kind="item", children=[leaf]))
            return SemanticNode(id="candidate", kind="list", children=items)

        for stream_ref, code in (
            ("", "TAGGED_STRUCTURE_DUPLICATE_MCID_CLAIM:candidate"),
            ("9 0 R", "TAGGED_STRUCTURE_STREAM_SCOPE_UNRESOLVED:candidate"),
        ):
            with self.subTest(stream_ref=stream_ref):
                document = SemanticDocument([paragraph])
                conflicts = []
                _materialize_tagged_structures(
                    document,
                    SemanticDocument([tagged_list(stream_ref=stream_ref)]),
                    {},
                    {(1, 0): [paragraph]},
                    conflicts,
                )
                self.assertEqual([node.kind for node in document.children], ["paragraph"])
                self.assertEqual(conflicts, [code])

    def test_nested_validated_list_materializes_without_false_conflict(self) -> None:
        outer_source = [SourceRef(page=1, mcids=(0,), bbox=(72, 80, 300, 92))]
        inner_source = [SourceRef(page=1, mcids=(1,), bbox=(90, 100, 300, 112))]
        geometry = [
            SemanticNode(id="outer-geometry", kind="paragraph", text="Outer", sources=outer_source),
            SemanticNode(id="inner-geometry", kind="paragraph", text="Inner", sources=inner_source),
        ]
        outer_leaf = SemanticNode(
            id="outer-leaf",
            kind="text",
            text="Outer",
            attrs={"mcid": 0, "parent_tree_validated": True, "struct_parents": 3},
            sources=outer_source,
        )
        inner_leaf = SemanticNode(
            id="inner-leaf",
            kind="text",
            text="Inner",
            attrs={"mcid": 1, "parent_tree_validated": True, "struct_parents": 3},
            sources=inner_source,
        )
        nested = SemanticNode(
            id="nested-list",
            kind="list",
            children=[SemanticNode(id="inner-item", kind="item", children=[inner_leaf])],
        )
        outer = SemanticNode(
            id="outer-list",
            kind="list",
            children=[SemanticNode(
                id="outer-item",
                kind="item",
                children=[outer_leaf, nested],
            )],
        )
        document = SemanticDocument(geometry)
        bindings = {}
        conflicts = []
        _materialize_tagged_structures(
            document,
            SemanticDocument([outer]),
            bindings,
            {(1, 0): [geometry[0]], (1, 1): [geometry[1]]},
            conflicts,
        )
        self.assertEqual(conflicts, [])
        self.assertEqual(len([node for node in document.walk() if node.kind == "list"]), 2)
        self.assertEqual(bindings["nested-list"].kind, "list")

    def test_materialized_tagged_table_repairs_only_unambiguous_row_geometry(self) -> None:
        def cell(node_id: str, text: str, y: float, role: str) -> SemanticNode:
            source = [SourceRef(page=1, glyph_ids=(int(y),), bbox=(72.0, y, 240.0, y + 12.0))]
            return SemanticNode(
                id=node_id,
                kind="table_cell",
                children=[SemanticNode(id=node_id + "-text", kind="text", text=text, sources=source)],
                attrs={"cell_role": role, "tag_role": role.upper()},
                sources=source,
            )

        header = SemanticNode(
            id="tag-row-header",
            kind="table_row",
            children=[cell("tag-cell-header", "Column", 80.0, "th")],
        )
        body = SemanticNode(
            id="tag-row-body",
            kind="table_row",
            children=[cell("tag-cell-body", "Value", 110.0, "td")],
        )
        materialized = _tagged_table_node(SemanticNode(
            id="tag-table",
            kind="table",
            children=[body, header],
        ))

        self.assertIsNotNone(materialized)
        assert materialized is not None
        rows = [child for child in materialized.children if child.kind == "table_row"]
        self.assertEqual([row.attrs["row"] for row in rows], [0, 1])
        self.assertEqual([row.attrs["role"] for row in rows], ["header", "body"])
        self.assertEqual(materialized.attrs["header_rows"], 1)
        self.assertIn("TAGGED_TABLE_ORDER_GEOMETRY_REPAIRED", materialized.warnings)

    def test_materialized_tagged_table_reserves_rowspan_columns(self) -> None:
        def tagged_cell(
            node_id: str,
            text: str,
            bbox,
            role: str,
            *,
            rowspan: int = 1,
            colspan: int = 1,
        ) -> SemanticNode:
            source = [SourceRef(page=1, bbox=bbox, glyph_ids=(len(node_id),))]
            return SemanticNode(
                id=node_id,
                kind="table_cell",
                attrs={
                    "cell_role": role,
                    "tag_role": role.upper(),
                    "structure_attributes": {
                        "RowSpan": rowspan,
                        "ColSpan": colspan,
                    },
                },
                children=[SemanticNode(id=node_id + "-text", kind="text", text=text, sources=source)],
                sources=source,
            )

        first_row = SemanticNode(
            id="first-row",
            kind="table_row",
            children=[
                tagged_cell("spanning", "Group", (72, 80, 180, 120), "th", rowspan=2),
                tagged_cell("heading", "Value", (180, 80, 320, 100), "th"),
            ],
        )
        second_row = SemanticNode(
            id="second-row",
            kind="table_row",
            children=[tagged_cell("value", "42", (180, 100, 320, 120), "td")],
        )
        table = _tagged_table_node(SemanticNode(
            id="rowspan-table",
            kind="table",
            children=[first_row, second_row],
        ))
        self.assertIsNotNone(table)
        assert table is not None
        rows = [node for node in table.children if node.kind == "table_row"]
        self.assertEqual([cell.attrs["col"] for cell in rows[0].children], [0, 1])
        self.assertEqual([cell.attrs["col"] for cell in rows[1].children], [1])
        self.assertEqual(table.attrs["column_count"], 2)
        self.assertEqual(table.attrs["row_count"], 2)
        rendered = render_semantic_html(SemanticDocument([table]))
        self.assertIn('rowspan="2"', rendered)
        payload = table.to_dict()
        self.assertEqual(payload["children"][1]["children"][0]["attrs"]["col"], 1)

    def test_first_page_level_one_geometry_survives_generic_paragraph_tag(self) -> None:
        heading = SemanticNode(
            id="cover-title",
            kind="heading",
            attrs={
                "level": 1,
                "bottom_zone": False,
                "upper_page_zone": True,
            },
            confidence=0.92,
            evidence=[Evidence(
                "geometric_semantic_detector",
                0.92,
                detail="heading",
                page=1,
            )],
            sources=[SourceRef(page=1, bbox=(90.0, 100.0, 520.0, 150.0))],
        )
        _apply_tagged_prior(
            heading,
            SemanticNode(id="tag-title", kind="paragraph", attrs={"tag_role": "P"}),
            0.94,
        )
        self.assertEqual(heading.kind, "heading")
        self.assertIn("TAGGED_ROLE_CONFLICT_GEOMETRY_PRESERVED", heading.warnings)

        subsection = SemanticNode(
            id="ordinary-subsection",
            kind="heading",
            attrs={"level": 2, "bottom_zone": False},
            confidence=0.92,
            sources=[SourceRef(page=1, bbox=(90.0, 180.0, 420.0, 200.0))],
        )
        _apply_tagged_prior(
            subsection,
            SemanticNode(id="tag-subsection", kind="paragraph", attrs={"tag_role": "P"}),
            0.94,
        )
        self.assertEqual(subsection.kind, "paragraph")

    def test_cover_override_requires_complete_high_confidence_display_evidence(self) -> None:
        cases = (
            ({"level": "1", "bottom_zone": False, "upper_page_zone": True}, 0.95),
            ({"level": 1, "bottom_zone": False, "upper_page_zone": False}, 0.95),
            ({"level": 1, "bottom_zone": False, "upper_page_zone": True}, 0.70),
        )
        for index, (attrs, score) in enumerate(cases):
            with self.subTest(index=index):
                heading = SemanticNode(
                    id="candidate-%d" % index,
                    kind="heading",
                    attrs=attrs,
                    confidence=0.92,
                    evidence=[Evidence(
                        "geometric_semantic_detector",
                        0.92,
                        detail="heading",
                        page=1,
                    )],
                    sources=[SourceRef(page=1, bbox=(90.0, 100.0, 520.0, 150.0))],
                )
                _apply_tagged_prior(
                    heading,
                    SemanticNode(id="tag-%d" % index, kind="paragraph"),
                    score,
                )
                self.assertEqual(heading.kind, "paragraph")
                self.assertNotIn(
                    "TAGGED_ROLE_CONFLICT_GEOMETRY_PRESERVED",
                    heading.warnings,
                )

    def test_unknown_or_descendant_only_geometry_cannot_authorize_tag_inversion(self) -> None:
        missing_a = SemanticNode(id="missing-a", kind="paragraph", sources=[SourceRef(page=1)])
        missing_b = SemanticNode(id="missing-b", kind="paragraph", sources=[SourceRef(page=1)])
        missing = SemanticDocument([missing_a, missing_b])
        tag_a = SemanticNode(id="tag-a", kind="paragraph")
        tag_b = SemanticNode(id="tag-b", kind="paragraph")
        _apply_tagged_order(
            missing,
            SemanticDocument([tag_b, tag_a]),
            {"tag-a": missing_a, "tag-b": missing_b},
        )
        self.assertEqual([node.id for node in missing.children], ["missing-a", "missing-b"])
        self.assertIn("TAGGED_ORDER_GEOMETRY_CONFLICT", missing.warnings)

        descendant_a = SemanticNode(
            id="parent-a",
            kind="paragraph",
            children=[self._block("child-a", 1, 72.0)],
        )
        descendant_b = SemanticNode(
            id="parent-b",
            kind="paragraph",
            children=[self._block("child-b", 1, 100.0)],
        )
        descendant = SemanticDocument([descendant_a, descendant_b])
        _apply_tagged_order(
            descendant,
            SemanticDocument([tag_b, tag_a]),
            {"tag-a": descendant_a, "tag-b": descendant_b},
        )
        self.assertEqual([node.id for node in descendant.children], ["parent-a", "parent-b"])

    def test_partial_table_binding_is_an_anchor_without_false_conflict(self) -> None:
        header = SemanticNode(id="header", kind="table_row")
        body = SemanticNode(id="body", kind="table_row")
        table = SemanticNode(id="table", kind="table", children=[header, body])
        tagged_body = SemanticNode(id="tagged-body", kind="table_row")
        tagged_table = SemanticNode(id="tagged-table", kind="table", children=[tagged_body])

        _apply_tagged_child_order(
            SemanticDocument([tagged_table]),
            {"tagged-table": table, "tagged-body": body},
        )

        self.assertEqual([child.id for child in table.children], ["header", "body"])
        self.assertNotIn("TAGGED_ORDER_GEOMETRY_CONFLICT", table.warnings)

    def test_overlapping_tagged_rows_are_not_claimed_as_geometric_repair(self) -> None:
        def row(node_id: str, y0: float, y1: float) -> SemanticNode:
            source = [SourceRef(page=1, bbox=(72.0, y0, 500.0, y1))]
            return SemanticNode(
                id=node_id,
                kind="table_row",
                sources=source,
                children=[SemanticNode(
                    id=node_id + "-cell",
                    kind="table_cell",
                    attrs={"cell_role": "td"},
                    sources=source,
                )],
            )

        lower = row("lower", 81.1, 101.1)
        upper = row("upper", 80.0, 100.0)
        table = _tagged_table_node(SemanticNode(
            id="tagged-table-overlap",
            kind="table",
            children=[lower, upper],
        ))
        self.assertIsNotNone(table)
        assert table is not None
        self.assertNotIn("TAGGED_TABLE_ORDER_GEOMETRY_REPAIRED", table.warnings)

    def test_empty_tagged_rows_do_not_leave_sparse_row_indices(self) -> None:
        empty = SemanticNode(id="empty", kind="table_row")
        source = [SourceRef(page=1, bbox=(72.0, 100.0, 240.0, 112.0))]
        cell = SemanticNode(
            id="cell",
            kind="table_cell",
            attrs={"cell_role": "th", "tag_role": "TH"},
            children=[SemanticNode(id="text", kind="text", text="Header", sources=source)],
            sources=source,
        )
        populated = SemanticNode(id="populated", kind="table_row", children=[cell], sources=source)
        table = _tagged_table_node(SemanticNode(
            id="tagged-table-empty",
            kind="table",
            children=[empty, populated],
        ))
        self.assertIsNotNone(table)
        assert table is not None
        rows = [child for child in table.children if child.kind == "table_row"]
        self.assertEqual([row.attrs["row"] for row in rows], [0])
        self.assertEqual(rows[0].children[0].attrs["row"], 0)
        self.assertEqual(table.attrs["header_rows"], 1)

    def test_empty_tagged_row_does_not_block_reversed_row_repair(self) -> None:
        def tagged_row(node_id: str, text: str, y: float, role: str) -> SemanticNode:
            source = [SourceRef(page=1, bbox=(72, y, 300, y + 14))]
            cell = SemanticNode(
                id=node_id + "-cell",
                kind="table_cell",
                attrs={"cell_role": role, "tag_role": role.upper()},
                children=[SemanticNode(id=node_id + "-text", kind="text", text=text, sources=source)],
                sources=source,
            )
            return SemanticNode(id=node_id, kind="table_row", children=[cell], sources=source)

        header = tagged_row("header", "Header", 80.0, "th")
        body = tagged_row("body", "Body", 110.0, "td")
        empty = SemanticNode(id="phantom", kind="table_row")
        table = _tagged_table_node(SemanticNode(
            id="tagged-table",
            kind="table",
            children=[body, empty, header],
        ))
        self.assertIsNotNone(table)
        assert table is not None
        rows = [child for child in table.children if child.kind == "table_row"]
        self.assertEqual([node_text(row) for row in rows], ["Header", "Body"])
        self.assertEqual(table.attrs["header_rows"], 1)
        self.assertTrue(any(
            evidence.kind == "tagged_table_empty_rows_ignored"
            for evidence in table.evidence
        ))


class ArtifactCalloutGeometryRegressionTests(unittest.TestCase):
    def test_chromatic_artifact_background_can_support_authored_callout(self) -> None:
        content = b" ".join([
            b"/Artifact BMC 1 0.95 0.78 rg 60 600 500 72 re f EMC",
            b"BT /F1 10 Tf 0 g 1 0 0 1 72 646 Tm (Attention: verify the configuration.) Tj",
            b"1 0 0 1 72 628 Tm (Keep the prior settings for rollback.) Tj",
            b"1 0 0 1 72 610 Tm (Record the final validation result.) Tj ET",
        ])
        result = convert(one_page_pdf(content))

        callouts = semantic_nodes(result, "callout")
        self.assertEqual(len(callouts), 1, result.semantic.to_dict())
        callout = callouts[0]
        self.assertTrue(callout.attrs["artifact_background_geometry"])
        self.assertTrue(any(
            evidence.kind == "artifact_callout_background"
            and evidence.data["source_marked_artifact"]
            for evidence in callout.evidence
        ))
        self.assertIn("ARTIFACT_CALLOUT_BACKGROUND_RECOVERED", callout.warnings)
        self.assertIn(
            "ARTIFACT_CALLOUT_BACKGROUND_RECOVERED",
            {warning.code for warning in result.warnings},
        )
        self.assertTrue(callout.sources)
        self.assertTrue(any(source.glyph_ids for source in callout.sources))
        self.assertIn('background: #fff2c7', result.markdown)
        self.assertIn('<aside class="cocoapdf-callout', result.html)
        graph_markdown = render_semantic_markdown(result.semantic)
        graph_html = render_semantic_html(result.semantic)
        self.assertIn("background-color: #fff2c7", graph_markdown)
        self.assertIn("background-color: rgb(255, 242, 199)", graph_html)
        self.assertEqual(result.html, graph_html)
        self.assertNotIn("_layout_html", callout.attrs)

    def test_artifact_highlight_code_and_grid_near_misses_are_not_callouts(self) -> None:
        cases = {
            "single_line_highlight": b" ".join([
                b"/Artifact BMC 1 0.90 0.70 rg 60 650 500 32 re f EMC",
                b"BT /F1 10 Tf 0 g 1 0 0 1 72 662 Tm (One highlighted sentence.) Tj ET",
            ]),
            "neutral_code_panel": b" ".join([
                b"/Artifact BMC 0.94 g 60 600 500 72 re f EMC",
                b"BT /F1 10 Tf 0 g 1 0 0 1 72 646 Tm (first code-like line) Tj",
                b"1 0 0 1 72 628 Tm (second code-like line) Tj ET",
            ]),
            "internal_grid": b" ".join([
                b"/Artifact BMC 1 0.90 0.70 rg 60 600 500 72 re f EMC",
                b"0 g 60 636 m 560 636 l S",
                b"BT /F1 10 Tf 0 g 1 0 0 1 72 646 Tm (Upper band content.) Tj",
                b"1 0 0 1 72 618 Tm (Lower band content.) Tj ET",
            ]),
            "post_text_overlay": b" ".join([
                b"BT /F1 10 Tf 0 g 1 0 0 1 72 646 Tm (First visible source line.) Tj",
                b"1 0 0 1 72 628 Tm (Second visible source line.) Tj ET",
                b"/Artifact BMC 1 0.90 0.70 rg 60 600 500 72 re f EMC",
            ]),
        }
        for name, content in cases.items():
            with self.subTest(name=name):
                result = convert(one_page_pdf(content))
                self.assertEqual(semantic_nodes(result, "callout"), [], result.markdown)
                self.assertNotIn(
                    "ARTIFACT_CALLOUT_BACKGROUND_RECOVERED",
                    {warning.code for warning in result.warnings},
                )

    def test_disjoint_rule_does_not_veto_verified_artifact_callout(self) -> None:
        content = b" ".join([
            b"/Artifact BMC 1 0.95 0.78 rg 300 600 250 72 re f EMC",
            b"0 g 20 636 m 120 636 l S",
            b"BT /F1 10 Tf 0 g 1 0 0 1 312 646 Tm (Attention: verify the setting.) Tj",
            b"1 0 0 1 312 628 Tm (Retain the prior value for rollback.) Tj ET",
        ])
        result = convert(one_page_pdf(content))
        self.assertEqual(len(semantic_nodes(result, "callout")), 1, result.markdown)

    def test_repeated_same_style_artifact_cards_are_not_callouts(self) -> None:
        content = b" ".join([
            b"/Artifact BMC 1 0.95 0.78 rg 60 680 150 48 re f EMC",
            b"/Artifact BMC 1 0.95 0.78 rg 230 680 150 48 re f EMC",
            b"/Artifact BMC 1 0.95 0.78 rg 400 680 150 48 re f EMC",
            b"BT /F1 8 Tf 0 g 1 0 0 1 68 710 Tm (Card one title.) Tj",
            b"1 0 0 1 68 694 Tm (Card one detail.) Tj",
            b"1 0 0 1 238 710 Tm (Card two title.) Tj",
            b"1 0 0 1 238 694 Tm (Card two detail.) Tj",
            b"1 0 0 1 408 710 Tm (Card three title.) Tj",
            b"1 0 0 1 408 694 Tm (Card three detail.) Tj ET",
        ])
        result = convert(one_page_pdf(content))
        self.assertEqual(semantic_nodes(result, "callout"), [], result.markdown)

    def test_upper_page_artifact_title_banner_remains_heading_content(self) -> None:
        content = b" ".join([
            b"/Artifact BMC 0.20 0.55 0.85 rg 54 680 504 82 re f EMC",
            b"BT /F1 24 Tf 1 g 1 0 0 1 84 730 Tm (Annual Operations Review) Tj",
            b"/F1 12 Tf 1 0 0 1 84 704 Tm (Evidence and implementation guide) Tj",
            b"/F1 10 Tf 0 g 1 0 0 1 72 640 Tm (This body paragraph establishes the local reading size.) Tj",
            b"1 0 0 1 72 622 Tm (A second body line keeps the title typography distinct.) Tj",
            b"1 0 0 1 72 604 Tm (The banner is not advisory content.) Tj ET",
        ])
        result = convert(one_page_pdf(content))
        self.assertEqual(semantic_nodes(result, "callout"), [], result.semantic.to_dict())
        self.assertIn("Annual Operations Review", result.markdown)
        self.assertNotIn(
            "ARTIFACT_CALLOUT_BACKGROUND_RECOVERED",
            {warning.code for warning in result.warnings},
        )


class TableCellListAdmissionRegressionTests(unittest.TestCase):
    @staticmethod
    def _line(text: str, y: float, seq: int) -> Line:
        font = Font(name="F1", base_font="Helvetica")
        return Line(
            chars=[Char(
                text=text,
                x0=72.0,
                y0=y,
                x1=72.0 + len(text) * 5.0,
                y1=y + 10.0,
                size=10.0,
                font=font,
                page=1,
                seq=seq,
            )],
            page=1,
            seq=seq,
        )

    def test_single_marker_like_table_row_label_remains_paragraph(self) -> None:
        blocks = _cell_blocks(
            NodeFactory(),
            None,
            [self._line("A. Review phase", 72.0, 1)],
            {},
        )
        self.assertEqual([block.kind for block in blocks], ["paragraph"])
        self.assertEqual(node_text(blocks[0]), "A. Review phase")

    def test_repeated_in_cell_markers_still_form_a_list(self) -> None:
        blocks = _cell_blocks(
            NodeFactory(),
            None,
            [
                self._line("1. First action", 72.0, 1),
                self._line("2. Second action", 88.0, 2),
            ],
            {},
        )
        self.assertEqual([block.kind for block in blocks], ["list"])
        self.assertEqual(len(blocks[0].children), 2)
        self.assertEqual(
            {item.kind for item in blocks[0].evidence},
            {"table_cell_list_marker_cohort"},
        )

    def test_mixed_marker_families_remain_literal_cell_paragraphs(self) -> None:
        blocks = _cell_blocks(
            NodeFactory(),
            None,
            [
                self._line("- Unordered action", 72.0, 1),
                self._line("1. Ordered action", 88.0, 2),
            ],
            {},
        )
        self.assertEqual([block.kind for block in blocks], ["paragraph", "paragraph"])

    def test_nonconsecutive_ordered_row_labels_do_not_form_a_list(self) -> None:
        blocks = _cell_blocks(
            NodeFactory(),
            None,
            [
                self._line("1. First record", 72.0, 1),
                self._line("3. Third record", 88.0, 2),
            ],
            {},
        )
        self.assertEqual([block.kind for block in blocks], ["paragraph", "paragraph"])

    def test_single_visible_task_marker_remains_a_list(self) -> None:
        blocks = _cell_blocks(
            NodeFactory(),
            None,
            [self._line("- [ ] Complete review", 72.0, 1)],
            {},
        )
        self.assertEqual([block.kind for block in blocks], ["list"])
        self.assertEqual(len(blocks[0].children), 1)
        item = blocks[0].children[0]
        self.assertEqual(
            item.attrs,
            {"marker": "task-unchecked", "task": True, "checked": False},
        )
        self.assertEqual(
            {evidence.kind for evidence in item.evidence},
            {"table_cell_list_marker"},
        )

        source = [SourceRef(page=1)]
        cell = SemanticNode(
            id="task-cell",
            kind="table_cell",
            children=blocks,
            attrs={"row": 0, "col": 0, "rowspan": 1, "colspan": 1, "role": "td"},
            sources=source,
        )
        row = SemanticNode(
            id="task-row",
            kind="table_row",
            children=[cell],
            attrs={"row": 0, "role": "body"},
            sources=source,
        )
        table = SemanticNode(
            id="task-table",
            kind="table",
            children=[row],
            attrs={"header_rows": 0, "output_mode": "html", "row_count": 1, "column_count": 1},
            sources=source,
        )
        document = SemanticDocument([table])
        markdown = render_semantic_markdown(document)
        html = render_semantic_html(document)
        payload = document.to_dict()
        self.assertIn('type="checkbox"', markdown)
        self.assertNotIn("- [ ]", markdown)
        self.assertIn('type="checkbox"', html)
        payload_item = payload["children"][0]["children"][0]["children"][0]["children"][0]["children"][0]
        self.assertTrue(payload_item["attrs"]["task"])
        self.assertFalse(payload_item["attrs"]["checked"])


class SemanticMarkdownInlineRegressionTests(unittest.TestCase):
    def test_intraword_underscores_remain_readable_without_enabling_emphasis(self) -> None:
        paragraph = SemanticNode(
            id="paragraph",
            kind="paragraph",
            children=[SemanticNode(
                id="text",
                kind="text",
                text="alpha_beta A_B _edge_",
            )],
        )
        self.assertEqual(
            render_semantic_markdown(SemanticDocument([paragraph])),
            "alpha_beta A_B \\_edge\\_\n",
        )

        def table_cell(node_id: str, text: str, row: int) -> SemanticNode:
            return SemanticNode(
                id=node_id,
                kind="table_cell",
                attrs={"row": row, "col": 0, "rowspan": 1, "colspan": 1},
                children=[SemanticNode(id=node_id + "-text", kind="text", text=text)],
            )

        table = SemanticNode(
            id="table",
            kind="table",
            attrs={"header_rows": 1},
            children=[
                SemanticNode(id="header", kind="table_row", children=[table_cell("h", "field_name", 0)]),
                SemanticNode(id="body", kind="table_row", children=[table_cell("b", "value_one", 1)]),
            ],
        )
        rendered_table = render_semantic_markdown(SemanticDocument([table]))
        self.assertIn("| field_name |", rendered_table)
        self.assertIn("| value_one |", rendered_table)
        self.assertNotIn("\\_", rendered_table)

    def test_adjacent_identical_styles_coalesce_with_sources(self) -> None:
        factory = NodeFactory()
        tokens = [
            {
                "text": "Alpha",
                "style": (True, False, False, False, False, False, False, False),
                "page": 1,
                "glyph_ids": (1,),
                "mcids": (),
                "bbox": (72.0, 72.0, 102.0, 84.0),
            },
            {
                "text": "Beta ",
                "style": (True, False, False, False, False, False, False, False),
                "page": 1,
                "glyph_ids": (2,),
                "mcids": (),
                "bbox": (102.0, 72.0, 132.0, 84.0),
            },
        ]
        inlines = inline_nodes_from_tokens(factory, tokens)
        self.assertEqual(len(inlines), 1)
        self.assertEqual(inlines[0].kind, "strong")
        self.assertEqual(
            {glyph for source in inlines[0].sources for glyph in source.glyph_ids},
            {1, 2},
        )
        paragraph = SemanticNode(id="paragraph", kind="paragraph", children=inlines)
        rendered = render_semantic_markdown(SemanticDocument([paragraph]))
        self.assertEqual(rendered, "**AlphaBeta**\n")
        self.assertNotIn("****", rendered)

    def test_code_span_uses_unescaped_descendant_text(self) -> None:
        code = SemanticNode(
            id="code",
            kind="code",
            children=[SemanticNode(
                id="code-text",
                kind="text",
                text="reader_mode_32",
            )],
        )
        paragraph = SemanticNode(id="paragraph", kind="paragraph", children=[code])
        self.assertEqual(
            render_semantic_markdown(SemanticDocument([paragraph])),
            "`reader_mode_32`\n",
        )

    def test_adjacent_code_tokens_coalesce_before_markdown_projection(self) -> None:
        for strong in (False, True):
            with self.subTest(strong=strong):
                style = (strong, False, True, False, False, False, False, False)
                tokens = [
                    {"text": "foo", "style": style},
                    {"text": "bar", "style": style},
                ]
                inlines = inline_nodes_from_tokens(NodeFactory(), tokens)
                paragraph = SemanticNode(
                    id="code-paragraph",
                    kind="paragraph",
                    children=inlines,
                )
                expected = "**`foobar`**\n" if strong else "`foobar`\n"
                self.assertEqual(
                    render_semantic_markdown(SemanticDocument([paragraph])),
                    expected,
                )

    def test_adjacent_superscript_labels_keep_separate_semantic_boundaries(self) -> None:
        superscript_style = (
            False,
            False,
            False,
            False,
            False,
            True,
            False,
            False,
        )
        inlines = inline_nodes_from_tokens(
            NodeFactory(),
            [
                {"text": "2", "style": superscript_style},
                {"text": "3", "style": superscript_style},
            ],
        )
        self.assertEqual([node.kind for node in inlines], ["superscript", "superscript"])
        self.assertEqual([node_text(node) for node in inlines], ["2", "3"])


class HeadingLevelModeTests(unittest.TestCase):
    def _heading_document(self) -> bytes:
        content = b" ".join([
            b"BT /F1 20 Tf 1 0 0 1 72 740 Tm (Primary Title) Tj",
            b"/F1 10 Tf 1 0 0 1 72 700 Tm (Body text under the primary title.) Tj",
            b"/F1 14 Tf 1 0 0 1 72 660 Tm (Secondary Section) Tj",
            b"/F1 10 Tf 1 0 0 1 72 620 Tm (Body text under the secondary section.) Tj ET",
        ])
        return one_page_pdf(content)

    def test_semantic_mode_preserves_inferred_depth(self) -> None:
        result = convert(self._heading_document(), ConvertOptions())
        levels = {
            line.split(" ", 1)[0]
            for line in result.markdown.splitlines()
            if line.startswith("#")
        }
        self.assertIn("#", levels)
        self.assertTrue(any(level != "#" for level in levels))

    def test_flat_mode_projects_every_heading_at_level_one(self) -> None:
        result = convert(self._heading_document(), ConvertOptions(heading_level_mode="flat"))
        headings = [line for line in result.markdown.splitlines() if line.startswith("#")]
        self.assertTrue(headings)
        for heading in headings:
            self.assertTrue(heading.startswith("# "), heading)

    def test_flat_mode_does_not_create_additional_headings(self) -> None:
        semantic = convert(self._heading_document(), ConvertOptions())
        flat = convert(self._heading_document(), ConvertOptions(heading_level_mode="flat"))
        self.assertEqual(
            len(semantic_nodes(semantic, "heading")),
            len(semantic_nodes(flat, "heading")),
        )

    def test_invalid_heading_level_mode_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            convert(self._heading_document(), ConvertOptions(heading_level_mode="H1"))


class ArtifactDisplayHeadingRecoveryTests(unittest.TestCase):
    @staticmethod
    def _body() -> list[bytes]:
        return [
            text_op(72, 650, "The survey describes the participating firms.", size=10),
            text_op(72, 630, "Responses were grouped by operating profile.", size=10),
            text_op(72, 610, "Additional observations follow in this section.", size=10),
        ]

    def test_numbered_artifact_display_title_is_recovered_with_provenance(self) -> None:
        content = b" ".join([
            line_op(54, 732, 558, 732, width=0.75),
            b"/Artifact BMC",
            text_op(
                72,
                700,
                "2. General Profile of Enterprises",
                font="F2",
                size=28,
            ),
            b"EMC",
            line_op(54, 684, 558, 684, width=0.75),
            *self._body(),
        ])
        result = convert(make_pdf([content]))

        headings = semantic_nodes(result, "heading")
        self.assertEqual(len(headings), 1, result.semantic.to_dict())
        heading = headings[0]
        heading_text = "".join(
            node.text or "" for node in heading.walk() if node.kind == "text"
        )
        self.assertEqual(heading_text, "2. General Profile of Enterprises")
        self.assertTrue(heading.attrs["artifact_text_recovered"])
        self.assertTrue(heading.attrs["source_marked_artifact"])
        self.assertEqual(heading.warnings, ["ARTIFACT_TEXT_RECOVERED"])
        self.assertLessEqual(heading.confidence, 0.86)
        self.assertTrue(heading.sources)
        self.assertTrue(heading.sources[0].glyph_ids)
        evidence = next(
            item for item in heading.evidence
            if item.kind == "artifact_text_recovery"
        )
        self.assertTrue(evidence.data["source_marked_artifact"])
        self.assertGreater(evidence.data["glyph_count"], 20)
        self.assertIn("numbered_display_title", evidence.data["admission_reasons"])
        self.assertIn("paired_title_rules", evidence.data["admission_reasons"])
        self.assertIn("# 2. General Profile of Enterprises", result.markdown)
        self.assertIn("2. General Profile of Enterprises</h1>", result.html)
        self.assertIn('data-warning-count="1"', result.html)
        self.assertIn(
            "ARTIFACT_TEXT_RECOVERED",
            {warning.code for warning in result.warnings},
        )
        self.assertTrue(result.report["semantic_valid"], result.report["semantic_errors"])
        report_heading = next(
            node for node in result.report["semantic_nodes"]
            if node["kind"] == "heading"
        )
        self.assertEqual(report_heading["warnings"], ["ARTIFACT_TEXT_RECOVERED"])
        self.assertEqual(
            next(
                item for item in report_heading["evidence"]
                if item["kind"] == "artifact_text_recovery"
            )["data"]["source_marked_artifact"],
            True,
        )
        self.assertFalse(result.report["ocr_used"])
        self.assertFalse(result.report["image_text_extraction_attempted"])

    def test_large_artifact_folio_is_not_recovered(self) -> None:
        content = b" ".join([
            b"/Artifact BMC",
            text_op(285, 700, "14", font="F2", size=30),
            b"EMC",
            *self._body(),
        ])
        result = convert(make_pdf([content]))
        self.assertNotIn("14", result.markdown)
        self.assertEqual(semantic_nodes(result, "heading"), [])
        self.assertNotIn(
            "ARTIFACT_TEXT_RECOVERED",
            {warning.code for warning in result.warnings},
        )

    def test_repeated_artifact_running_title_is_not_recovered(self) -> None:
        page = b" ".join([
            b"/Artifact BMC",
            text_op(72, 700, "2. Quarterly Operations Overview", font="F2", size=28),
            b"EMC",
            *self._body(),
        ])
        result = convert(make_pdf([page, page]))
        self.assertNotIn("Quarterly Operations Overview", result.markdown)
        self.assertEqual(semantic_nodes(result, "heading"), [])
        self.assertNotIn(
            "ARTIFACT_TEXT_RECOVERED",
            {warning.code for warning in result.warnings},
        )

    def test_artifact_logo_in_top_furniture_zone_is_not_recovered(self) -> None:
        content = b" ".join([
            b"/Artifact BMC",
            text_op(72, 765, "2. ACME Global Brand", font="F2", size=28),
            b"EMC",
            *self._body(),
        ])
        result = convert(make_pdf([content]))
        self.assertNotIn("ACME Global Brand", result.markdown)
        self.assertEqual(semantic_nodes(result, "heading"), [])

    def test_artifact_chart_label_over_raster_is_not_recovered(self) -> None:
        content = b" ".join([
            b"q 220 0 0 90 60 590 cm /Im1 Do Q",
            b"/Artifact BMC",
            text_op(82, 635, "2. Quarterly Revenue Growth", font="F2", size=28),
            b"EMC",
            text_op(72, 550, "The narrative beneath the chart remains authored text.", size=10),
            text_op(72, 530, "It describes the measurements without reading the image.", size=10),
            text_op(72, 510, "The original raster is preserved as a figure.", size=10),
        ])
        result = convert(
            make_pdf(
                [content],
                xobjects={
                    "Im1": image_xobject_rgb(
                        2,
                        2,
                        b"\x33\x66\x99" * 4,
                    )
                },
            )
        )
        self.assertNotIn("Quarterly Revenue Growth", result.markdown)
        self.assertEqual(semantic_nodes(result, "heading"), [])
        self.assertEqual(len(semantic_nodes(result, "image")), 1)
        self.assertIn("<img ", result.html)
        self.assertFalse(result.report["image_text_extraction_attempted"])


class TiledCodePanelTests(unittest.TestCase):
    @staticmethod
    def _panel_ops(
        rows,
        *,
        top: float = 700.0,
        left: float = 60.0,
        width: float = 360.0,
        height: float = 14.0,
    ):
        operations = []
        for index, (text, font) in enumerate(rows):
            bottom = top - index * height
            operations.append(rect_fill_op(left, bottom, width, height, 0.94))
            operations.append(
                text_op(left + 10.0, bottom + 3.0, text, font=font, size=9.0)
            )
        return operations

    def test_tiled_fill_code_panel_preserves_blank_and_style_outlier(self) -> None:
        rows = [
            ("alpha_value = transform(source_record)", "F4"),
            ("beta_value = validate(alpha_value)", "F4"),
            ("   ", "F4"),
            ("gamma_value = persist(beta_value)", "F4"),
            ("note", "F1"),
            ("delta_value = publish(gamma_value)", "F4"),
            ("return delta_value", "F4"),
        ]
        result = convert(make_pdf([b"\n".join(self._panel_ops(rows))]))

        blocks = semantic_nodes(result, "code_block")
        self.assertEqual(len(blocks), 1, result.semantic.to_dict())
        block = blocks[0]
        self.assertIn("beta_value = validate(alpha_value)\n\n", block.text)
        self.assertIn("\nnote\n", block.text)
        self.assertEqual(block.attrs["tiled_fill_strip_count"], len(rows))
        self.assertTrue(block.attrs["tiled_fill_paint_order_backed"])
        self.assertEqual(block.attrs["tiled_fill_style_outlier_count"], 1)
        self.assertGreaterEqual(block.attrs["tiled_fill_mono_char_ratio"], 0.85)
        self.assertEqual(block.warnings, ["CODE_PANEL_STYLE_OUTLIER_RECOVERED"])
        evidence = next(
            item for item in block.evidence if item.kind == "tiled_fill_code_panel"
        )
        self.assertEqual(evidence.data["strip_count"], len(rows))
        self.assertTrue(evidence.data["paint_order_backed"])
        self.assertEqual(evidence.data["style_outlier_count"], 1)
        self.assertTrue(block.sources)
        self.assertTrue(block.sources[0].glyph_ids)
        self.assertEqual(result.markdown.count("```"), 2)
        self.assertEqual(result.html.count("<pre"), 1)
        self.assertEqual(result.html.count("<code"), 1)
        self.assertIn(
            "CODE_PANEL_STYLE_OUTLIER_RECOVERED",
            {warning.code for warning in result.warnings},
        )
        self.assertTrue(result.report["semantic_valid"], result.report["semantic_errors"])

    def test_tiled_proportional_prose_rows_are_not_code(self) -> None:
        rows = [
            ("First shaded prose statement remains a paragraph.", "F1"),
            ("Second shaded prose statement remains a paragraph.", "F1"),
            ("Third shaded prose statement remains a paragraph.", "F1"),
            ("Fourth shaded prose statement remains a paragraph.", "F1"),
        ]
        result = convert(make_pdf([b"\n".join(self._panel_ops(rows))]))
        self.assertEqual(semantic_nodes(result, "code_block"), [])
        self.assertNotIn("<pre", result.html)

    def test_isolated_monospace_label_in_shaded_rows_is_not_code(self) -> None:
        rows = [
            ("First descriptive value occupies this shaded row.", "F1"),
            ("STATUS_LABEL", "F4"),
            ("Second descriptive value occupies this shaded row.", "F1"),
            ("Third descriptive value occupies this shaded row.", "F1"),
        ]
        result = convert(make_pdf([b"\n".join(self._panel_ops(rows))]))
        blocks = semantic_nodes(result, "code_block")
        self.assertEqual(len(blocks), 1)
        self.assertNotIn("tiled_fill_code_panel", blocks[0].attrs)
        self.assertNotIn(
            "tiled_fill_code_panel",
            {item.kind for item in blocks[0].evidence},
        )

    def test_disconnected_tiled_code_panels_remain_separate(self) -> None:
        first = [
            ("first_alpha = read()", "F4"),
            ("first_beta = map(first_alpha)", "F4"),
            ("first_gamma = save(first_beta)", "F4"),
        ]
        second = [
            ("second_alpha = read()", "F4"),
            ("second_beta = map(second_alpha)", "F4"),
            ("second_gamma = save(second_beta)", "F4"),
        ]
        stream = b"\n".join([
            *self._panel_ops(first, top=710.0),
            *self._panel_ops(second, top=590.0),
        ])
        result = convert(make_pdf([stream]))
        self.assertEqual(len(semantic_nodes(result, "code_block")), 2)
        self.assertEqual(result.html.count("<pre"), 2)

    def test_shaded_monospace_grid_retains_table_ownership(self) -> None:
        operations = []
        for index, (left_text, right_text) in enumerate([
            ("FIELD", "VALUE"),
            ("alpha", "one"),
            ("beta", "two"),
        ]):
            bottom = 700.0 - index * 24.0
            operations.extend([
                rect_fill_op(60.0, bottom, 360.0, 24.0, 0.94),
                text_op(70.0, bottom + 7.0, left_text, font="F4", size=9.0),
                text_op(250.0, bottom + 7.0, right_text, font="F4", size=9.0),
            ])
        for x in (60.0, 240.0, 420.0):
            operations.append(line_op(x, 652.0, x, 724.0, 1.0))
        for y in (652.0, 676.0, 700.0, 724.0):
            operations.append(line_op(60.0, y, 420.0, y, 1.0))
        result = convert(make_pdf([b"\n".join(operations)]))
        tables = semantic_nodes(result, "table")
        self.assertEqual(len(tables), 1, result.markdown)
        all_code_ids = {
            block.id for block in semantic_nodes(result, "code_block")
        }
        table_code_ids = {
            block.id for block in tables[0].walk() if block.kind == "code_block"
        }
        self.assertEqual(
            all_code_ids,
            table_code_ids,
        )
        self.assertFalse(
            any(
                item.kind == "tiled_fill_code_panel"
                for block in semantic_nodes(result, "code_block")
                for item in block.evidence
            )
        )

    def test_unruled_monospace_record_rows_are_not_tiled_code(self) -> None:
        operations = []
        for index, (label, value) in enumerate([
            ("ALPHA_FIELD", "ready"),
            ("BRAVO_FIELD", "pending"),
            ("CHARLIE_FIELD", "complete"),
            ("DELTA_FIELD", "review"),
        ]):
            bottom = 700.0 - index * 18.0
            operations.extend([
                rect_fill_op(60.0, bottom, 360.0, 18.0, 0.94),
                text_op(70.0, bottom + 5.0, label, font="F4", size=9.0),
                text_op(285.0, bottom + 5.0, value, font="F4", size=9.0),
            ])
        result = convert(make_pdf([b"\n".join(operations)]))
        self.assertFalse(any(
            item.kind == "tiled_fill_code_panel"
            for block in semantic_nodes(result, "code_block")
            for item in block.evidence
        ), result.semantic.to_dict())
        self.assertNotIn("tiled_fill_code_panel", result.markdown)

    def test_record_veto_is_honored_when_region_lookup_discovers_it_first(self) -> None:
        font = Font(name="F4", base_font="Courier")
        line = Line(
            chars=[Char(
                text="FIELD                         value",
                x0=70.0,
                y0=80.0,
                x1=320.0,
                y1=90.0,
                size=9.0,
                font=font,
                page=1,
                seq=1,
            )],
            page=1,
            seq=1,
        )
        renderer = object.__new__(MarkdownRenderer)
        renderer._tiled_noncode_line_ids = set()
        renderer.conv = SimpleNamespace(
            page_sizes={1: (612.0, 792.0)},
            fills=[SimpleNamespace(
                page=1,
                x0=60.0,
                y0=76.0,
                x1=420.0,
                y1=94.0,
            )],
        )

        def discover(instance, candidate):
            instance._tiled_noncode_line_ids.add(id(candidate))
            return None

        with patch.object(
            MarkdownRenderer,
            "_tiled_code_panel_for_line",
            autospec=True,
            side_effect=discover,
        ):
            self.assertIsNone(renderer._code_fill_key(line))


if __name__ == "__main__":
    unittest.main()
