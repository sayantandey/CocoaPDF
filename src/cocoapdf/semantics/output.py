from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Tuple

from ..html.render import render_html
from ..html.semantic import render_semantic_html
from ..ir.semantic import SemanticDocument, SemanticNode
from ..markdown.semantic import render_semantic_markdown


def render_reconciled_outputs(
    layout_markdown: str,
    document: SemanticDocument,
    report: Dict[str, Any],
) -> Tuple[str, str]:
    """Render one semantic graph without sacrificing lossless layout blocks.

    Verified tagged structure is authoritative and is rendered directly. For
    untagged PDFs, the mature geometry renderer remains the lossless projection;
    semantic nodes replace only blocks they actually enrich (notes, references,
    cross-references, or rotated tables), while PDF-native outline and AcroForm
    nodes are inserted as new documentary content.
    """
    tagged = document.metadata.get("tagged_pdf")
    if isinstance(tagged, dict) and tagged.get("present"):
        markdown = render_semantic_markdown(document)
        html = render_semantic_html(document)
        _strip_layout_hints(document)
        return markdown, html

    enriched_layout = _overlay_changed_blocks(layout_markdown, document)
    enriched_layout = _remove_reconciled_footnote_anchors(enriched_layout, document)
    toc_nodes = _outline_toc_nodes(document, enriched_layout)
    form_nodes = _acroform_nodes(document)

    markdown_parts: List[str] = []
    if toc_nodes:
        markdown_parts.append(_render_markdown_nodes(toc_nodes, document))
    if enriched_layout.strip():
        markdown_parts.append(enriched_layout.strip())
    if form_nodes:
        markdown_parts.append(_render_markdown_nodes(form_nodes, document))
    markdown = "\n\n".join(part.strip() for part in markdown_parts if part.strip())
    markdown += "\n" if markdown else ""

    html = render_html(enriched_layout, report)
    if toc_nodes:
        html = _insert_after_body(html, _render_html_nodes(toc_nodes, document))
    if form_nodes:
        html = _insert_before_body_end(html, _render_html_nodes(form_nodes, document))
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


def _render_html_nodes(nodes: Iterable[SemanticNode], source: SemanticDocument) -> str:
    fragment = SemanticDocument(
        children=list(nodes),
        metadata=dict(source.metadata),
        warnings=[],
        version=source.version,
    )
    return render_semantic_html(fragment, full_document=False)


def _insert_after_body(html: str, fragment: str) -> str:
    if not fragment.strip():
        return html
    return html.replace("<body>\n", "<body>\n%s\n" % fragment.strip(), 1)


def _insert_before_body_end(html: str, fragment: str) -> str:
    if not fragment.strip():
        return html
    return html.replace("\n</body>", "\n%s\n</body>" % fragment.strip(), 1)


def _strip_layout_hints(document: SemanticDocument) -> None:
    for node in document.walk():
        node.attrs.pop("_layout_markdown", None)
        node.attrs.pop("_layout_kind", None)
