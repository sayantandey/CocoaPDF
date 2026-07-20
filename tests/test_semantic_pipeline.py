from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Iterable

from cocoapdf import convert
from cocoapdf.cli import _format_payload
from cocoapdf.content.runtime import _attach_marked_content
from cocoapdf.core import ConvertOptions, Converter
from cocoapdf.fonts.decoding import CMapMapping, decode_font, parse_tounicode
from cocoapdf.html.semantic import render_semantic_html
from cocoapdf.ir.semantic import NodeFactory, SemanticDocument, SemanticNode, SourceRef
from cocoapdf.markdown.semantic import render_semantic_markdown
from cocoapdf.reporting.report import attach_semantic_document
from cocoapdf.semantics.reconcile import reconcile_tagged_content
from cocoapdf.semantics.tagged import parse_tagged_structure
from cocoapdf.synthetic import line_op, make_pdf, rect_fill_op, text_op
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
        self.assertTrue(result.report["semantic_valid"], result.report["semantic_errors"])
        self.assertIn("Graph source", result.markdown)
        self.assertIn("Graph source", result.html)
        payload = json.loads(_format_payload(result, "json"))
        self.assertEqual(payload["semantic_document"]["schema"], "cocoapdf.semantic-document")
        paragraphs = semantic_nodes(result, "paragraph")
        self.assertEqual(len(paragraphs), 1)
        self.assertTrue(paragraphs[0].sources[0].glyph_ids)

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
        self.assertIn("width: 100.000pt", result.markdown)
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
            b"<< /Type /StructElem /S /TH /P 6 0 R /Pg 3 0 R /K 0 /A << /O /Table /ColSpan 2 /Scope /Column >> >>",
            b"<< /Type /StructElem /S /TD /P 7 0 R /Pg 3 0 R /K 1 >>",
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
        self.assertTrue(all(cell.sources and cell.sources[0].mcids for cell in cells))
        self.assertIn('colspan="2"', result.markdown)
        self.assertIn('colspan="2"', result.html)

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


class AdvancedTableTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
