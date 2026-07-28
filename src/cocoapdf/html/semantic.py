from __future__ import annotations

import html
from typing import List

from .css import DEFAULT_CSS
from .sanitize import safe_asset_href, safe_href
from ..ir.semantic import SemanticDocument, SemanticNode


def render_semantic_html(document: SemanticDocument, full_document: bool = True) -> str:
    errors = document.validate(require_provenance=False)
    if errors:
        raise ValueError("invalid semantic document: %s" % "; ".join(errors[:8]))
    body = "\n".join(_render_node(node) for node in document.children if node.kind not in {"artifact", "outline"})
    if not full_document:
        return body
    title = html.escape(str(document.metadata.get("title", "CocoaPDF Document")), quote=True)
    return '<!doctype html>\n<html>\n<head>\n<meta charset="utf-8" />\n<title>%s</title>\n<style>%s</style>\n</head>\n<body>\n%s\n</body>\n</html>\n' % (title, DEFAULT_CSS, body)


def _render_node(node: SemanticNode) -> str:
    kind = node.kind
    data_attrs = ' data-cocoapdf-node="%s" data-confidence="%.4f"' % (html.escape(node.id, quote=True), node.confidence)
    if kind in {"document", "section", "table_head", "table_body", "form"}:
        tag = "form" if kind == "form" else "section" if kind == "section" else "div"
        cls = ' class="cocoapdf-columns"' if node.attrs.get("layout") == "columns" else ' class="cocoapdf-form"' if kind == "form" else ""
        return "<%s%s%s>%s</%s>" % (tag, cls, data_attrs, "".join(_render_node(child) for child in node.children), tag)
    if kind == "anchor":
        return '<a id="%s"%s></a>' % (html.escape(str(node.attrs.get("name", node.id)), quote=True), data_attrs)
    if kind == "page_break":
        return '<hr class="cocoapdf-page-break" data-page="%s"%s />' % (html.escape(str(node.attrs.get("page", "")), quote=True), data_attrs)
    if kind == "heading":
        level = max(1, min(6, int(node.attrs.get("level", 2))))
        anchor = html.escape(str(node.attrs.get("anchor", node.id)), quote=True)
        return "<h%d id=\"%s\"%s>%s</h%d>" % (level, anchor, data_attrs, _render_inlines(node), level)
    if kind == "paragraph":
        styles = []
        alignment = node.attrs.get("alignment")
        if alignment in {"center", "right"}:
            styles.append("text-align: %s" % alignment)
        if node.attrs.get("writing_mode") not in {None, "horizontal"}:
            styles.extend(["writing-mode: vertical-rl", "text-orientation: mixed"])
        style = ' style="%s"' % html.escape("; ".join(styles), quote=True) if styles else ""
        direction = ' dir="rtl"' if node.attrs.get("direction") == "rtl" else ""
        return "<p%s%s%s>%s</p>" % (style, direction, data_attrs, _render_inlines(node))
    if kind == "list":
        ordered = bool(node.attrs.get("ordered"))
        tag = "ol" if ordered else "ul"
        start = int(node.attrs.get("start", 1))
        start_attr = ' start="%d"' % start if ordered and start != 1 else ""
        return "<%s%s%s>%s</%s>" % (tag, start_attr, data_attrs, "".join(_render_node(child) for child in node.children), tag)
    if kind == "item":
        inline_children = [child for child in node.children if child.kind not in {"list", "paragraph", "code_block", "quote", "table", "figure"}]
        block_children = [child for child in node.children if child.kind in {"list", "paragraph", "code_block", "quote", "table", "figure"}]
        proxy = SemanticNode(id=node.id + "-inline", kind="item", children=inline_children)
        body = _render_inlines(proxy) if inline_children else ""
        body += "".join(_render_node(child) for child in block_children)
        return "<li%s>%s</li>" % (data_attrs, body)
    if kind == "quote":
        return "<blockquote%s>%s</blockquote>" % (data_attrs, _render_inlines(node) or "".join(_render_node(child) for child in node.children))
    if kind == "code_block":
        info = html.escape(str(node.attrs.get("info", "")), quote=True)
        class_attr = ' class="language-%s"' % info if info else ""
        return "<pre%s><code%s>%s</code></pre>" % (data_attrs, class_attr, html.escape(node.text))
    if kind == "thematic_break":
        return "<hr%s />" % data_attrs
    if kind == "table":
        return _render_table(node, data_attrs)
    if kind == "table_note":
        return '<p class="cocoapdf-table-note"%s>%s</p>' % (data_attrs, _render_inlines(node))
    if kind == "figure":
        return "<figure%s>%s</figure>" % (data_attrs, "".join(_render_node(child) for child in node.children))
    if kind == "caption":
        return "<figcaption%s>%s</figcaption>" % (data_attrs, _render_inlines(node))
    if kind == "image":
        return _render_image(node, data_attrs)
    if kind == "footnote_ref":
        label = html.escape(str(node.attrs.get("label", node.id)), quote=True)
        return '<sup%s><a href="#fn-%s" role="doc-noteref">%s</a></sup>' % (data_attrs, label, label)
    if kind == "footnote":
        label = html.escape(str(node.attrs.get("label", node.id)), quote=True)
        return '<aside id="fn-%s" role="doc-footnote"%s>%s</aside>' % (label, data_attrs, _render_inlines(node) or "".join(_render_node(child) for child in node.children))
    if kind == "toc":
        return '<nav class="cocoapdf-toc" aria-label="Table of contents"%s><ol>%s</ol></nav>' % (data_attrs, "".join(_render_node(child) for child in node.children))
    if kind == "toc_item":
        target = html.escape(
            str(node.attrs.get("target_anchor") or node.attrs.get("target_id") or ""),
            quote=True,
        )
        body = html.escape(node.text)
        if target:
            body = '<a href="#%s">%s</a>' % (target, body)
        descendants = [child for child in node.children if child.kind == "toc_item"]
        return '<li%s>%s%s</li>' % (data_attrs, body, "<ol>%s</ol>" % "".join(_render_node(child) for child in descendants) if descendants else "")
    if kind == "reference_section":
        return '<section role="doc-bibliography"%s>%s</section>' % (data_attrs, "".join(_render_node(child) for child in node.children))
    if kind == "reference":
        label = node.attrs.get("label")
        anchor = html.escape(str(node.attrs.get("anchor", "")), quote=True)
        anchor_attr = ' id="%s"' % anchor if anchor else ""
        return '<p role="doc-biblioentry"%s%s>%s%s</p>' % (anchor_attr, data_attrs, '<span class="cocoapdf-reference-label">[%s]</span> ' % html.escape(str(label)) if label else "", _render_inlines(node))
    if kind in {"callout", "sidebar"}:
        return '<aside class="cocoapdf-%s"%s>%s</aside>' % (kind, data_attrs, _render_inlines(node) or "".join(_render_node(child) for child in node.children))
    if kind == "equation":
        return '<div class="cocoapdf-equation"%s>%s</div>' % (data_attrs, _render_inlines(node) or html.escape(node.text))
    if kind == "form_field":
        return _render_form_field(node, data_attrs)
    if kind == "annotation":
        return '<aside class="cocoapdf-annotation"%s>%s</aside>' % (data_attrs, _render_inlines(node))
    if kind == "html" and node.attrs.get("trusted_generated"):
        return node.text
    return _render_inline(node)


def _render_inlines(node: SemanticNode) -> str:
    if node.attrs.get("actual_text") and node.text:
        return html.escape(node.text)
    if node.children:
        return "".join(_render_inline(child) for child in node.children)
    return html.escape(node.text)


def _render_inline(node: SemanticNode) -> str:
    body = _render_inlines(node)
    data_attrs = ' data-cocoapdf-node="%s"' % html.escape(node.id, quote=True)
    if node.kind == "text":
        return body
    tags = {"strong": "strong", "emphasis": "em", "strikethrough": "del", "underline": "u", "superscript": "sup", "subscript": "sub", "mark": "mark", "code": "code"}
    if node.kind in tags:
        tag = tags[node.kind]
        return "<%s%s>%s</%s>" % (tag, data_attrs, body, tag)
    if node.kind == "link":
        href = safe_href(str(node.attrs.get("href", "")))
        return '<a href="%s"%s>%s</a>' % (html.escape(href, quote=True), data_attrs, body) if href else body
    if node.kind == "cross_reference":
        target = html.escape(
            str(node.attrs.get("target_anchor") or node.attrs.get("target_id") or ""),
            quote=True,
        )
        return '<a class="cocoapdf-cross-reference" href="#%s"%s>%s</a>' % (target, data_attrs, body) if target else body
    if node.kind in {"footnote_ref", "image"}:
        return _render_node(node)
    return body or "".join(_render_node(child) for child in node.children)


def _render_table(node: SemanticNode, attrs: str) -> str:
    caption = next((child for child in node.children if child.kind == "caption"), None)
    notes = [child for child in node.children if child.kind == "table_note"]
    rows = [child for child in node.children if child.kind == "table_row"]
    header_rows = int(node.attrs.get("header_rows", 0))
    parts = ["<table%s>" % attrs]
    if caption:
        parts.append("<caption>%s</caption>" % _render_inlines(caption))
    if header_rows:
        parts.append("<thead>")
    for row_index, row in enumerate(rows):
        if header_rows and row_index == header_rows:
            parts.extend(["</thead>", "<tbody>"])
        parts.append('<tr data-cocoapdf-node="%s">' % html.escape(row.id, quote=True))
        for cell in row.children:
            if cell.kind != "table_cell":
                continue
            tag = "th" if cell.attrs.get("role") == "th" or row_index < header_rows else "td"
            cell_attrs: List[str] = ['data-cocoapdf-node="%s"' % html.escape(cell.id, quote=True)]
            rowspan = int(cell.attrs.get("rowspan", 1))
            colspan = int(cell.attrs.get("colspan", 1))
            if rowspan > 1:
                cell_attrs.append('rowspan="%d"' % rowspan)
            if colspan > 1:
                cell_attrs.append('colspan="%d"' % colspan)
            if int(cell.attrs.get("rotation", 0)):
                cell_attrs.append('style="writing-mode: vertical-rl; text-orientation: mixed;"')
            body = "".join(_render_node(child) if child.kind in {"paragraph", "list", "code_block", "figure"} else _render_inline(child) for child in cell.children)
            parts.append("<%s %s>%s</%s>" % (tag, " ".join(cell_attrs), body, tag))
        parts.append("</tr>")
    if header_rows:
        parts.append("</tbody>" if len(rows) > header_rows else "</thead>")
    parts.append("</table>")
    parts.extend(_render_node(note) for note in notes)
    return "".join(parts)


def _render_image(node: SemanticNode, attrs: str) -> str:
    source = safe_asset_href(str(node.attrs.get("src", "")))
    if not source:
        return ""
    alt = html.escape(str(node.attrs.get("alt", node.text)), quote=True)
    width = float(node.attrs.get("display_width_pt", 0.0) or 0.0)
    height = float(node.attrs.get("display_height_pt", 0.0) or 0.0)
    alignment = str(node.attrs.get("alignment", "left"))
    style = []
    if width > 0:
        style.append("width: %.3fpt" % width)
    if height > 0:
        style.append("height: %.3fpt" % height)
    style.append("max-width: 100%")
    image = '<img src="%s" alt="%s" style="%s"%s />' % (html.escape(source, quote=True), alt, html.escape("; ".join(style), quote=True), attrs)
    link = safe_href(str(node.attrs.get("link", ""))) if node.attrs.get("link") else None
    if link:
        image = '<a href="%s">%s</a>' % (html.escape(link, quote=True), image)
    return '<div class="cocoapdf-align-%s">%s</div>' % (alignment if alignment in {"left", "center", "right"} else "left", image)


def _render_form_field(node: SemanticNode, attrs: str) -> str:
    kind = str(node.attrs.get("field_type", "unknown"))
    name = html.escape(str(node.attrs.get("name", "")))
    value = html.escape(str(node.attrs.get("value", node.text)))
    state = ""
    if kind in {"checkbox", "radio"}:
        state = "checked" if node.attrs.get("checked") else "not checked"
    elif kind == "signature":
        signature = node.attrs.get("signature") or {}
        state = "signature present" if signature.get("contents_present") else "unsigned"
        signer = str(signature.get("signer_name") or "")
        if signer:
            state += " — " + html.escape(signer)
    elif kind in {"combo", "listbox"}:
        selected = node.attrs.get("selected_indices") or []
        state = "selected indices: " + ", ".join(str(item) for item in selected) if selected else ""
    display = value or state
    if state and value and state != value:
        display = "%s (%s)" % (value, state)
    display_name = (
        name
        if not name or name.rstrip().endswith(":")
        else name + ":"
    )
    # Form extraction is documentary only: never emit active controls or copy
    # PDF actions into HTML. The typed semantic JSON retains options, flags,
    # widget rectangles, export states, and signature metadata.
    return (
        '<div class="cocoapdf-form-field" data-field-type="%s" data-name="%s"%s>'
        '<span class="cocoapdf-form-field-name">%s</span> '
        '<span class="cocoapdf-form-field-value">%s</span></div>'
        % (
            html.escape(kind, quote=True),
            html.escape(str(node.attrs.get("name", "")), quote=True),
            attrs,
            display_name,
            display,
        )
    )
