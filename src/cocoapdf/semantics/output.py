from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Tuple

from ..html.semantic import render_semantic_html
from ..ir.semantic import SemanticDocument, SemanticNode
from ..markdown.semantic import render_semantic_markdown


def render_reconciled_outputs(
    layout_markdown: str,
    document: SemanticDocument,
    report: Dict[str, Any],
    image_markup: str = "markdown",
) -> Tuple[str, str]:
    """Render independent Markdown and HTML projections of one semantic graph.

    Verified tagged structure is authoritative and is rendered directly.  For
    untagged PDFs, the mature geometry renderer remains the lossless Markdown
    projection; semantic nodes replace only blocks they actually enrich.  HTML
    is always rendered directly from the typed semantic document.  A small,
    closed set of verified generated fragments preserves HTML-only geometry
    such as MathML, printed controls, and styled column/callout containers.
    """
    report["html_projection"] = "direct_semantic_html"
    tagged = document.metadata.get("tagged_pdf")
    if isinstance(tagged, dict) and tagged.get("present"):
        report["markdown_projection"] = "direct_verified_tagged_semantics"
        for node in document.walk():
            if node.kind == "table" and any(
                candidate.attrs.get("tagged_node_id")
                for candidate in node.walk()
            ):
                node.attrs.pop("_layout_html", None)
        markdown = render_semantic_markdown(document, image_markup)
        html = render_semantic_html(document)
        _strip_layout_hints(document)
        return markdown, html

    enriched_layout = _overlay_changed_blocks(layout_markdown, document)
    report["markdown_projection"] = "lossless_layout_reconciliation"
    enriched_layout = _remove_reconciled_footnote_anchors(enriched_layout, document)
    toc_nodes = _outline_toc_nodes(document, enriched_layout)
    form_nodes = _acroform_nodes(document)
    emitted_toc_ids = {node.id for node in toc_nodes}
    for node in document.children:
        if (
            node.kind == "toc"
            and node.attrs.get("source") == "pdf_outline"
            and "_layout_markdown" not in node.attrs
            and node.id not in emitted_toc_ids
        ):
            node.attrs["_html_suppressed"] = True

    markdown_parts: List[str] = []
    if toc_nodes:
        markdown_parts.append(_render_markdown_nodes(toc_nodes, document))
    if enriched_layout.strip():
        markdown_parts.append(enriched_layout.strip())
    if form_nodes:
        markdown_parts.append(_render_markdown_nodes(form_nodes, document))
    markdown = "\n\n".join(part.strip() for part in markdown_parts if part.strip())
    markdown += "\n" if markdown else ""

    html = render_semantic_html(document)
    _strip_layout_hints(document)
    return markdown, html


def _overlay_changed_blocks(markdown: str, document: SemanticDocument) -> str:
    rendered = markdown
    for node in document.walk():
        original = node.attrs.get("_layout_markdown")
        if not isinstance(original, str) or not original or not _node_needs_overlay(node):
            continue
        replacement = _render_markdown_nodes([node], document).strip()
        if original in rendered:
            rendered = rendered.replace(original, replacement, 1)
    return rendered


def _remove_reconciled_footnote_anchors(markdown: str, document: SemanticDocument) -> str:
    names = {
        str(name)
        for node in document.walk()
        if node.kind == "footnote"
        for key in ("source_destinations", "source_backlinks")
        for name in node.attrs.get(key, ())
        if name
    }
    rendered = markdown
    for name in names:
        rendered = re.sub(
            r'<a id="%s"></a>[ \t]*(?:\n[ \t]*){0,2}' % re.escape(name),
            "",
            rendered,
            count=1,
        )
    return rendered


def _node_needs_overlay(node: SemanticNode) -> bool:
    original_kind = str(node.attrs.get("_layout_kind", node.kind))
    if node.kind == "heading" and node.kind != original_kind:
        return True
    if node.kind in {"footnote", "reference"}:
        return True
    if node.kind == "table" and any(
        int(candidate.attrs.get("rotation", 0) or 0) % 360
        for candidate in node.walk()
        if candidate.kind == "table_cell"
    ):
        return True
    if node.kind not in {"paragraph", "heading", "item"}:
        return False
    descendants = [candidate for candidate in node.walk() if candidate is not node]
    if any(candidate.kind == "footnote_ref" for candidate in descendants):
        return True
    cross_references = [candidate for candidate in descendants if candidate.kind == "cross_reference"]
    return bool(cross_references) and all(
        candidate.attrs.get("reference_kind") == "reference"
        for candidate in cross_references
    )


def _outline_toc_nodes(document: SemanticDocument, layout_markdown: str) -> List[SemanticNode]:
    if document.metadata.get("page_selection_active"):
        return []
    if not document.metadata.get("outline"):
        return []
    import re

    if re.search(r"(?im)^#{1,6}\s+(?:table\s+of\s+contents|contents)\s*$", layout_markdown):
        return []
    return [
        node
        for node in document.children
        if node.kind == "toc" and "_layout_markdown" not in node.attrs
    ]


def _acroform_nodes(document: SemanticDocument) -> List[SemanticNode]:
    return [
        node
        for node in document.children
        if node.kind == "form"
        and "_layout_markdown" not in node.attrs
        and any(child.kind == "form_field" for child in node.children)
    ]


def _render_markdown_nodes(nodes: Iterable[SemanticNode], source: SemanticDocument) -> str:
    fragment = SemanticDocument(
        children=list(nodes),
        metadata=dict(source.metadata),
        warnings=[],
        version=source.version,
    )
    return render_semantic_markdown(fragment)


def _strip_layout_hints(document: SemanticDocument) -> None:
    for node in document.walk():
        node.attrs.pop("_layout_markdown", None)
        node.attrs.pop("_layout_kind", None)
        node.attrs.pop("_layout_html", None)
        node.attrs.pop("_html_suppressed", None)
