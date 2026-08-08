from __future__ import annotations

import html
import math
import re
from contextvars import ContextVar
from typing import Any, List

from ..html.sanitize import safe_embedded_image_href, safe_href
from ..ir.semantic import SemanticDocument, SemanticNode

# The image markup policy belongs to the conversion request, not to a node, and
# every block/inline renderer below would otherwise have to forward it. Hold it
# for the duration of one render instead of widening each signature.
_IMAGE_MARKUP: ContextVar[str] = ContextVar("cocoapdf_markdown_image_markup", default="markdown")


def render_semantic_markdown(
    document: SemanticDocument,
    image_markup: str = "markdown",
) -> str:
    errors = document.validate(require_provenance=False)
    if errors:
        raise ValueError("invalid semantic document: %s" % "; ".join(errors[:8]))
    token = _IMAGE_MARKUP.set(image_markup or "markdown")
    try:
        blocks = [_render_block(node) for node in document.children if node.kind not in {"artifact", "outline"}]
    finally:
        _IMAGE_MARKUP.reset(token)
    rendered = "\n\n".join(block for block in blocks if block.strip()).strip()
    return rendered + ("\n" if rendered else "")


def _render_block(node: SemanticNode) -> str:
    kind = node.kind
    if kind in {"document", "section", "table_head", "table_body", "form"}:
        if kind == "section" and node.attrs.get("layout") == "columns":
            body = "\n".join(
                filter(None, (_render_html_block(child) for child in node.children))
            )
            return '<div class="cocoapdf-columns">\n%s\n</div>' % body
        return "\n\n".join(filter(None, (_render_block(child) for child in node.children)))
    if kind == "anchor":
        name = re.sub(r"[^A-Za-z0-9_.:-]", "-", str(node.attrs.get("name", node.id)))
        return '<a id="%s"></a>' % html.escape(name, quote=True)
    if kind == "page_break":
        return "---\n\n<!-- page %s -->" % node.attrs.get("page", "")
    if kind == "heading":
        level = max(1, min(6, int(node.attrs.get("level", 2))))
        return "%s %s" % ("#" * level, _render_inlines(node))
    if kind == "paragraph":
        body = _render_inlines(node)
        alignment = node.attrs.get("alignment")
        writing = node.attrs.get("writing_mode")
        if writing and writing != "horizontal":
            return (
                '<p style="writing-mode: vertical-rl; text-orientation: mixed;">'
                "%s</p>" % _render_html_inlines(node)
            )
        if alignment in {"center", "right"}:
            return '<p align="%s">%s</p>' % (
                alignment,
                _render_html_inlines(node),
            )
        return body
    if kind == "list":
        return _render_list(node)
    if kind == "item":
        return _render_item(node)
    if kind == "quote":
        body = "\n\n".join(filter(None, (_render_block(child) for child in node.children))) or _escape(node.text)
        return "\n".join("> " + line if line else ">" for line in body.splitlines())
    if kind == "code_block":
        body = node.text or "\n".join(child.text for child in node.children)
        fence = "`" * max(3, _longest_run(body, "`") + 1)
        info = re.sub(r"[^A-Za-z0-9_+.-]", "", str(node.attrs.get("info", "")))
        return "%s%s\n%s\n%s" % (fence, info, body.rstrip("\n"), fence)
    if kind == "thematic_break":
        return "---"
    if kind == "table":
        return _render_table(node)
    if kind == "figure":
        return _render_figure(node)
    if kind == "caption":
        return "*%s*" % _render_inlines(node)
    if kind == "table_note":
        return "*%s*" % _render_inlines(node)
    if kind == "footnote":
        label = _safe_label(str(node.attrs.get("label", node.id)))
        body = _render_inlines(node) or " ".join(_render_block(child) for child in node.children)
        return "[^%s]: %s" % (label, body)
    if kind == "reference_section":
        return "\n\n".join(_render_block(child) for child in node.children)
    if kind == "reference":
        label = node.attrs.get("label")
        body = _render_inlines(node)
        anchor = node.attrs.get("anchor")
        prefix = '<a id="%s"></a>' % html.escape(str(anchor), quote=True) if anchor else ""
        return prefix + ("[%s] %s" % (label, body) if label else body)
    if kind == "toc":
        return _render_toc(node)
    if kind == "toc_item":
        return _render_toc_item(node)
    if kind in {"callout", "sidebar"}:
        body = _render_html_mixed_content(node)
        return '<aside class="cocoapdf-%s"%s>%s</aside>' % (
            kind,
            _artifact_callout_style(node),
            body,
        )
    if kind == "equation":
        body = _render_html_mixed_content(node)
        return '<div class="cocoapdf-equation">%s</div>' % body
    if kind == "form_field":
        return _render_form_field(node)
    if kind == "annotation":
        return "<!-- annotation: %s -->" % _render_inlines(node)
    if kind == "html":
        return node.text if node.attrs.get("trusted_generated") else _escape(node.text)
    return _render_inlines(node) or "\n\n".join(filter(None, (_render_block(child) for child in node.children)))


def _render_inlines(node: SemanticNode) -> str:
    if node.attrs.get("actual_text") and node.text:
        return _escape(node.text)
    if node.children:
        return "".join(_render_inline(child) for child in node.children)
    return _escape(node.text)


def _render_inline(node: SemanticNode) -> str:
    body = _render_inlines(node)
    if node.kind == "text":
        return body
    if node.kind == "strong":
        return _wrap_inline_delimiter("**", body)
    if node.kind == "emphasis":
        return _wrap_inline_delimiter("*", body)
    if node.kind == "strikethrough":
        return _wrap_inline_delimiter("~~", body)
    if node.kind == "underline":
        return "<u>%s</u>" % body
    if node.kind == "superscript":
        return "<sup>%s</sup>" % body
    if node.kind == "subscript":
        return "<sub>%s</sub>" % body
    if node.kind == "mark":
        return "<mark>%s</mark>" % body
    if node.kind == "code":
        content = _raw_inline_text(node)
        fence = "`" * max(1, _longest_run(content, "`") + 1)
        if content.startswith("`") or content.endswith("`"):
            content = " " + content + " "
        return fence + content + fence
    if node.kind == "link":
        href = safe_href(str(node.attrs.get("href", "")))
        # A link already carries its own visual affordance. PDF producers often
        # encode that appearance as an underline, which would otherwise create
        # redundant ``[<u>text</u>](...)`` markup.
        underline = re.fullmatch(r"<u>(.*)</u>", body, re.S)
        if underline:
            body = underline.group(1)
        if href and body == href and re.match(r"^(?:https?://|mailto:)", href, re.I):
            return "<%s>" % href
        return "[%s](%s)" % (body, _escape_destination(href)) if href else body
    if node.kind == "cross_reference":
        target = re.sub(
            r"[^A-Za-z0-9_.:-]",
            "-",
            str(node.attrs.get("target_anchor") or node.attrs.get("target_id") or ""),
        )
        return "[%s](#%s)" % (body, target) if target else body
    if node.kind == "footnote_ref":
        return "[^%s]" % _safe_label(str(node.attrs.get("label", node.id)))
    if node.kind == "image":
        return _render_image(node)
    return body


def _render_image(node: SemanticNode) -> str:
    source = safe_embedded_image_href(str(node.attrs.get("src", "")))
    if not source:
        return ""
    if _IMAGE_MARKUP.get() != "markdown":
        fragment = _html_image_fragment(node, "")
        if fragment:
            return fragment
    alt = _escape(str(node.attrs.get("alt", node.text)))
    width = _safe_dimension(node.attrs.get("display_width_pt"))
    height = _safe_dimension(node.attrs.get("display_height_pt"))
    alignment = str(node.attrs.get("alignment", "left"))
    link = safe_href(str(node.attrs.get("link", ""))) if node.attrs.get("link") else None
    if width > 0 or height > 0 or alignment != "left":
        style = []
        if width > 0:
            style.append("width: %.3fpt" % width)
        if height > 0:
            style.append("height: %.3fpt" % height)
        image = '<img src="%s" alt="%s" style="%s" />' % (html.escape(source, quote=True), html.escape(str(node.attrs.get("alt", node.text)), quote=True), "; ".join(style))
        if link:
            image = '<a href="%s">%s</a>' % (html.escape(link, quote=True), image)
        return '<div class="cocoapdf-align-%s">%s</div>' % (alignment if alignment in {"left", "center", "right"} else "left", image)
    image = "![%s](%s)" % (alt, _escape_destination(source))
    return "[%s](%s)" % (image, _escape_destination(link)) if link else image



def _render_item(node: SemanticNode) -> str:
    inline_children = [child for child in node.children if child.kind not in {"list", "paragraph", "code_block", "quote", "table", "figure"}]
    block_children = [child for child in node.children if child.kind in {"list", "paragraph", "code_block", "quote", "table", "figure"}]
    parts: List[str] = []
    if inline_children:
        proxy = SemanticNode(id=node.id + "-inline", kind="item", children=inline_children)
        parts.append(_render_inlines(proxy))
    parts.extend(_render_block(child) for child in block_children)
    return "\n\n".join(part for part in parts if part)

def _render_list(node: SemanticNode) -> str:
    ordered = bool(node.attrs.get("ordered"))
    start = int(node.attrs.get("start", 1))
    lines: List[str] = []
    for index, child in enumerate(node.children):
        marker = "%d." % (start + index) if ordered else "-"
        if child.attrs.get("task"):
            marker += " [%s]" % ("x" if child.attrs.get("checked") else " ")
        body = _render_block(child).strip()
        body_lines = body.splitlines() or [""]
        lines.append("%s %s" % (marker, body_lines[0]))
        lines.extend("   " + line for line in body_lines[1:])
    return "\n".join(lines)


def _render_table(node: SemanticNode) -> str:
    rows, header_rows = _semantic_table_rows(node)
    captions = [child for child in node.children if child.kind == "caption"]
    notes = [child for child in node.children if child.kind == "table_note"]
    if not rows:
        return ""
    complex_table = node.attrs.get("output_mode") == "html" or header_rows not in {0, 1} or any(
        int(cell.attrs.get("rowspan", 1)) != 1
        or int(cell.attrs.get("colspan", 1)) != 1
        or int(cell.attrs.get("rotation", 0)) % 360
        or any(not _simple_table_cell_child(child) for child in cell.children)
        for row in rows for cell in row.children if cell.kind == "table_cell"
    )
    if complex_table:
        return _render_table_html(node)
    grid = [[_render_inlines(cell).replace("|", r"\|") for cell in row.children if cell.kind == "table_cell"] for row in rows]
    width = max((len(row) for row in grid), default=0)
    if not width:
        return ""
    grid = [row + [""] * (width - len(row)) for row in grid]
    if header_rows == 0:
        grid.insert(0, ["" for _ in range(width)])
    out = ["| " + " | ".join(grid[0]) + " |", "| " + " | ".join("---" for _ in range(width)) + " |"]
    out.extend("| " + " | ".join(row) + " |" for row in grid[1:])
    parts = ["*%s*" % _render_inlines(caption) for caption in captions]
    parts.append("\n".join(out))
    parts.extend("*%s*" % _render_inlines(note) for note in notes)
    return "\n\n".join(filter(None, parts))


def _simple_table_cell_child(node: SemanticNode) -> bool:
    if node.kind in {"text", "strong", "emphasis", "strikethrough", "underline", "superscript", "subscript", "mark", "code", "link", "cross_reference", "footnote_ref"}:
        return all(_simple_table_cell_child(child) for child in node.children)
    if node.kind == "paragraph":
        return all(_simple_table_cell_child(child) for child in node.children)
    return False


def _render_table_html(node: SemanticNode) -> str:
    rows, header_rows = _semantic_table_rows(node)
    caption = next((child for child in node.children if child.kind == "caption"), None)
    notes = [child for child in node.children if child.kind == "table_note"]
    out = ["<table>"]
    if caption:
        out.append("<caption>%s</caption>" % _render_html_inlines(caption))
    if header_rows:
        out.append("<thead>")
    for row_index, row in enumerate(rows):
        if header_rows and row_index == header_rows:
            out.extend(["</thead>", "<tbody>"])
        out.append("<tr>")
        for cell in row.children:
            if cell.kind != "table_cell":
                continue
            tag = "th" if cell.attrs.get("role") == "th" or row_index < header_rows else "td"
            attrs = []
            rowspan = int(cell.attrs.get("rowspan", 1))
            colspan = int(cell.attrs.get("colspan", 1))
            rotation = int(cell.attrs.get("rotation", 0))
            if rowspan > 1:
                attrs.append('rowspan="%d"' % rowspan)
            if colspan > 1:
                attrs.append('colspan="%d"' % colspan)
            if rotation:
                attrs.append('style="writing-mode: vertical-rl; text-orientation: mixed;"')
            body = _render_html_cell_content(cell)
            out.append("<%s%s>%s</%s>" % (tag, (" " + " ".join(attrs)) if attrs else "", body, tag))
        out.append("</tr>")
    if header_rows:
        out.append("</tbody>" if len(rows) > header_rows else "</thead>")
    out.append("</table>")
    out.extend(
        '<p class="cocoapdf-table-note">%s</p>'
        % _render_html_inlines(note)
        for note in notes
    )
    return "\n".join(out)


def _semantic_table_rows(
    node: SemanticNode,
) -> tuple[List[SemanticNode], int]:
    def rows_under(container: SemanticNode) -> List[SemanticNode]:
        rows: List[SemanticNode] = []
        for child in container.children:
            if child.kind == "table_row":
                rows.append(child)
            elif child.kind in {"table_head", "table_body"}:
                rows.extend(rows_under(child))
        return rows

    head_rows: List[SemanticNode] = []
    body_rows: List[SemanticNode] = []
    loose_rows: List[SemanticNode] = []
    has_sections = False
    for child in node.children:
        if child.kind == "table_head":
            has_sections = True
            head_rows.extend(rows_under(child))
        elif child.kind == "table_body":
            has_sections = True
            body_rows.extend(rows_under(child))
        elif child.kind == "table_row":
            loose_rows.append(child)
    if has_sections:
        rows = head_rows + body_rows + loose_rows
        if head_rows:
            return rows, len(head_rows)
    else:
        rows = loose_rows
    try:
        header_rows = int(node.attrs.get("header_rows", 0))
    except (TypeError, ValueError, OverflowError):
        header_rows = 0
    return rows, min(len(rows), max(0, header_rows))


def _render_figure(node: SemanticNode) -> str:
    if _IMAGE_MARKUP.get() != "markdown":
        figure = _render_html_figure(node)
        if figure:
            return figure
    parts = [_render_inline(child) if child.kind == "image" else "*%s*" % _render_inlines(child) if child.kind == "caption" else _render_block(child) for child in node.children]
    return "\n\n".join(filter(None, parts))


def _render_html_figure(node: SemanticNode) -> str:
    """Project every child of one semantic figure as safe graph-native HTML.

    Markdown cannot carry an image's intrinsic dimensions, alignment, or a
    caption bound to the image.  An explicit HTML request therefore renders a
    closed fragment directly from typed nodes, preserving compound figures and
    inline caption semantics without reparsing Markdown.
    """
    images = [child for child in node.children if child.kind == "image"]
    rendered_images = [
        (image, _html_image_element(image))
        for image in images
    ]
    rendered_images = [item for item in rendered_images if item[1]]
    if not rendered_images:
        return ""
    first_image = rendered_images[0][0]
    alignment = _safe_alignment(first_image.attrs.get("alignment"))
    image_markup = {id(image): fragment for image, fragment in rendered_images}
    parts = ['<figure class="cocoapdf-figure cocoapdf-align-%s">' % alignment]
    caption_seen = False
    for child in node.children:
        if child.kind == "image":
            fragment = image_markup.get(id(child), "")
            if fragment:
                parts.append(fragment)
        elif child.kind == "caption":
            caption = _render_html_inlines(child).strip()
            if not caption:
                continue
            if not caption_seen:
                parts.append("<figcaption>%s</figcaption>" % caption)
                caption_seen = True
            else:
                parts.append(
                    '<p class="cocoapdf-caption">%s</p>' % caption
                )
        else:
            fragment = _render_html_block(child)
            if fragment:
                parts.append(fragment)
    parts.append("</figure>")
    return "\n".join(parts)


def _html_image_fragment(image: SemanticNode, caption_html: str) -> str:
    image_html = _html_image_element(image)
    if not image_html:
        return ""
    alignment = _safe_alignment(image.attrs.get("alignment"))
    parts = [
        '<figure class="cocoapdf-figure cocoapdf-align-%s">' % alignment,
        image_html,
    ]
    if caption_html:
        parts.append("<figcaption>%s</figcaption>" % caption_html)
    parts.append("</figure>")
    return "\n".join(parts)


def _html_image_element(image: SemanticNode) -> str:
    source = safe_embedded_image_href(str(image.attrs.get("src", "")))
    if not source:
        return ""
    alt = str(image.attrs.get("alt", image.text) or "")
    width = _safe_dimension(image.attrs.get("display_width_pt"))
    height = _safe_dimension(image.attrs.get("display_height_pt"))
    style_parts: List[str] = []
    if width > 0:
        style_parts.append("width: %.3fpt" % width)
    if height > 0:
        style_parts.append("height: %.3fpt" % height)
    style_parts.extend(["max-width: 100%", "object-fit: contain"])
    style = "; ".join(style_parts) + ";"
    image_html = '<img src="%s" alt="%s" style="%s" />' % (
        html.escape(source, quote=True),
        html.escape(alt, quote=True),
        style,
    )
    link = safe_href(str(image.attrs.get("link", ""))) if image.attrs.get("link") else None
    if link:
        image_html = '<a href="%s" rel="noopener noreferrer">%s</a>' % (
            html.escape(link, quote=True),
            image_html,
        )
    return image_html


_HTML_BLOCK_KINDS = {
    "document",
    "section",
    "paragraph",
    "heading",
    "list",
    "item",
    "quote",
    "code_block",
    "thematic_break",
    "table",
    "figure",
    "caption",
    "table_note",
    "callout",
    "sidebar",
    "equation",
    "page_break",
}


def _render_html_inlines(node: SemanticNode) -> str:
    """Render typed inline semantics for a Markdown raw-HTML container."""
    if node.attrs.get("actual_text") and node.text:
        body = html.escape(node.text)
    elif node.children:
        body = "".join(_render_html_inline(child) for child in node.children)
    else:
        body = html.escape(node.text)
    expanded = str(node.attrs.get("expanded_text") or "").strip()
    if expanded and body:
        return '<abbr title="%s">%s</abbr>' % (
            html.escape(expanded, quote=True),
            body,
        )
    return body


def _render_html_inline(node: SemanticNode) -> str:
    body = _render_html_inlines(node)
    if node.kind == "text":
        return "<br />\n" if node.attrs.get("hard_break") else body
    if node.kind == "strong":
        return "<strong>%s</strong>" % body
    if node.kind == "emphasis":
        return "<em>%s</em>" % body
    if node.kind == "strikethrough":
        return "<del>%s</del>" % body
    if node.kind == "underline":
        return "<u>%s</u>" % body
    if node.kind == "superscript":
        return "<sup>%s</sup>" % body
    if node.kind == "subscript":
        return "<sub>%s</sub>" % body
    if node.kind == "mark":
        return "<mark>%s</mark>" % body
    if node.kind == "code":
        return "<code>%s</code>" % html.escape(_raw_inline_text(node))
    if node.kind == "link":
        href = safe_href(str(node.attrs.get("href", "")))
        return (
            '<a href="%s">%s</a>' % (html.escape(href, quote=True), body)
            if href
            else body
        )
    if node.kind == "cross_reference":
        target = re.sub(
            r"[^A-Za-z0-9_.:-]",
            "-",
            str(
                node.attrs.get("target_anchor")
                or node.attrs.get("target_id")
                or ""
            ),
        )
        return (
            '<a class="cocoapdf-cross-reference" href="#%s">%s</a>'
            % (html.escape(target, quote=True), body)
            if target
            else body
        )
    if node.kind == "footnote_ref":
        label = _safe_label(str(node.attrs.get("label", node.id)))
        return '<sup><a href="#fn-%s" role="doc-noteref">%s</a></sup>' % (
            html.escape(label, quote=True),
            html.escape(label),
        )
    if node.kind == "image":
        return _html_image_element(node)
    return body


def _render_html_mixed_content(node: SemanticNode) -> str:
    if not node.children:
        return html.escape(node.text)
    if not any(child.kind in _HTML_BLOCK_KINDS for child in node.children):
        return _render_html_inlines(node)
    return "".join(
        _render_html_block(child)
        if child.kind in _HTML_BLOCK_KINDS
        else _render_html_inline(child)
        for child in node.children
    )


def _render_html_block(node: SemanticNode) -> str:
    kind = node.kind
    if kind in {"document", "section"}:
        return "".join(
            _render_html_block(child)
            if child.kind in _HTML_BLOCK_KINDS
            else _render_html_inline(child)
            for child in node.children
        ) or html.escape(node.text)
    if kind == "paragraph":
        attrs = ""
        writing = str(node.attrs.get("writing_mode") or "")
        alignment = str(node.attrs.get("alignment") or "")
        if writing and writing != "horizontal":
            attrs = (
                ' style="writing-mode: vertical-rl; '
                'text-orientation: mixed;"'
            )
        elif alignment in {"center", "right"}:
            attrs = ' align="%s"' % alignment
        return "<p%s>%s</p>" % (attrs, _render_html_inlines(node))
    if kind == "heading":
        level = _safe_integer(node.attrs.get("level"), 2, 1, 6)
        return "<h%d>%s</h%d>" % (level, _render_html_inlines(node), level)
    if kind == "list":
        return _render_html_list(node)
    if kind == "item":
        return _render_html_list_item(node, ordered=False, marker_style="")
    if kind == "quote":
        return "<blockquote>%s</blockquote>" % _render_html_mixed_content(node)
    if kind == "code_block":
        info = re.sub(r"[^A-Za-z0-9_+.-]", "", str(node.attrs.get("info", "")))
        class_attr = ' class="language-%s"' % info if info else ""
        return "<pre><code%s>%s</code></pre>" % (
            class_attr,
            html.escape(node.text),
        )
    if kind == "thematic_break":
        return "<hr />"
    if kind == "table":
        return _render_table_html(node)
    if kind == "figure":
        return _render_html_figure(node)
    if kind == "image":
        return _html_image_fragment(node, "")
    if kind == "caption":
        return '<p class="cocoapdf-caption">%s</p>' % _render_html_inlines(node)
    if kind == "table_note":
        return '<p class="cocoapdf-table-note">%s</p>' % _render_html_inlines(node)
    if kind in {"callout", "sidebar"}:
        return '<aside class="cocoapdf-%s"%s>%s</aside>' % (
            kind,
            _artifact_callout_style(node),
            _render_html_mixed_content(node),
        )
    if kind == "equation":
        return '<div class="cocoapdf-equation">%s</div>' % _render_html_mixed_content(node)
    if kind == "page_break":
        return '<hr class="cocoapdf-page-break" data-page="%s" />' % html.escape(
            str(node.attrs.get("page", "")),
            quote=True,
        )
    return _render_html_inlines(node)


def _render_html_cell_content(cell: SemanticNode) -> str:
    parts: List[str] = []
    for child in cell.children:
        # Preserve the historical compact cell shape for simple paragraphs,
        # while projecting genuinely nested structures as HTML rather than
        # inert Markdown inside a raw <td>/<th> block.
        if child.kind == "paragraph":
            parts.append(_render_html_inlines(child))
        elif child.kind in _HTML_BLOCK_KINDS:
            parts.append(_render_html_block(child))
        else:
            parts.append(_render_html_inline(child))
    if not parts and cell.text:
        parts.append(html.escape(cell.text))
    return "".join(parts)


def _render_html_list(node: SemanticNode) -> str:
    ordered = bool(node.attrs.get("ordered"))
    tag = "ol" if ordered else "ul"
    marker_style = str(node.attrs.get("marker_style") or "").lower()
    attrs: List[str] = []
    if ordered:
        start = _safe_integer(node.attrs.get("start"), 1, -(10**9), 10**9)
        if start != 1:
            attrs.append('start="%d"' % start)
        list_type = {
            "upper-alpha": "A",
            "lower-alpha": "a",
            "upper-roman": "I",
            "lower-roman": "i",
        }.get(marker_style)
        if list_type:
            attrs.append('type="%s"' % list_type)
    elif marker_style in {"disc", "circle", "square", "none"}:
        attrs.append('style="list-style-type: %s"' % marker_style)
    body = "".join(
        _render_html_list_item(
            child,
            ordered=ordered,
            marker_style=marker_style,
        )
        if child.kind == "item"
        else _render_html_block(child)
        for child in node.children
    )
    explicit = " " + " ".join(attrs) if attrs else ""
    return "<%s%s>%s</%s>" % (tag, explicit, body, tag)


def _render_html_list_item(
    node: SemanticNode,
    *,
    ordered: bool,
    marker_style: str,
) -> str:
    parts: List[str] = []
    for child in node.children:
        if child.kind == "paragraph":
            parts.append(_render_html_inlines(child))
        elif child.kind in _HTML_BLOCK_KINDS:
            parts.append(_render_html_block(child))
        else:
            parts.append(_render_html_inline(child))
    if not parts and node.text:
        parts.append(html.escape(node.text))
    body = "".join(parts)
    is_task = bool(node.attrs.get("task"))
    if is_task:
        checked = " checked" if node.attrs.get("checked") else ""
        task_text = _html_task_text(node)
        state = "Checked" if node.attrs.get("checked") else "Unchecked"
        label = "%s task%s" % (
            state,
            ": " + task_text if task_text else "",
        )
        body = '<input type="checkbox" disabled%s aria-label="%s" /> %s' % (
            checked,
            html.escape(label, quote=True),
            body,
        )
    attrs: List[str] = []
    if ordered:
        marker = node.attrs.get("marker")
        if isinstance(marker, int) and not isinstance(marker, bool):
            attrs.append('value="%d"' % marker)
    if is_task:
        attrs.append('class="cocoapdf-task-item"')
    explicit = " " + " ".join(attrs) if attrs else ""
    return "<li%s>%s</li>" % (explicit, body)


def _html_task_text(node: SemanticNode) -> str:
    text = " ".join(
        _raw_node_text(child)
        for child in node.children
        if child.kind != "list"
    ) or node.text
    return re.sub(r"\s+", " ", text).strip()


def _raw_node_text(node: SemanticNode) -> str:
    return node.text or "".join(_raw_node_text(child) for child in node.children)


def _safe_alignment(raw: Any) -> str:
    value = str(raw or "left").lower()
    return value if value in {"left", "center", "right"} else "left"


def _safe_dimension(raw: Any) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0.0
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        return 0.0
    return min(value, 100_000.0)


def _safe_integer(raw: Any, default: int, minimum: int, maximum: int) -> int:
    if isinstance(raw, bool):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(maximum, max(minimum, value))


def _render_toc(node: SemanticNode) -> str:
    return "\n".join(_render_toc_item(child) for child in node.children if child.kind == "toc_item")


def _render_toc_item(node: SemanticNode) -> str:
    level = max(1, int(node.attrs.get("level", 1)))
    target = str(node.attrs.get("target_anchor") or node.attrs.get("target_id") or "")
    body = _escape(node.text)
    if target:
        body = "[%s](#%s)" % (body, re.sub(r"[^A-Za-z0-9_.:-]", "-", target))
    page = node.attrs.get("page_label") or node.attrs.get("page")
    suffix = " — %s" % page if page else ""
    line = "%s- %s%s" % ("  " * (level - 1), body, suffix)
    descendants = "\n".join(_render_toc_item(child) for child in node.children if child.kind == "toc_item")
    return line + ("\n" + descendants if descendants else "")


def _render_form_field(node: SemanticNode) -> str:
    field_type = str(node.attrs.get("field_type", "unknown"))
    name = _escape(str(node.attrs.get("name", "")))
    value = _escape(str(node.attrs.get("value", node.text)))
    if field_type in {"checkbox", "radio"}:
        return "- [%s] %s" % ("x" if node.attrs.get("checked") else " ", name or value)
    if field_type == "signature":
        signature = node.attrs.get("signature") or {}
        status = "Signature present" if signature.get("contents_present") else "Unsigned signature field"
        signer = _escape(str(signature.get("signer_name") or ""))
        details = "%s%s" % (status, " — %s" % signer if signer else "")
        return "**%s:** %s" % (name, details) if name else details
    return "**%s:** %s" % (name, value) if name else value


def _escape(text: str) -> str:
    value = str(text).replace("\\", "\\\\")
    for character in ("`", "*", "~", "[", "]", "<", ">"):
        value = value.replace(character, "\\" + character)
    # Intraword underscores cannot open or close CommonMark emphasis.  Keep
    # identifiers readable while escaping delimiter-capable boundary forms.
    value = re.sub(r"(?<![\w])_|_(?![\w])", r"\\_", value, flags=re.UNICODE)
    return value


def _wrap_inline_delimiter(delimiter: str, body: str) -> str:
    """Keep whitespace outside Markdown emphasis delimiters."""
    leading = body[: len(body) - len(body.lstrip())]
    trailing = body[len(body.rstrip()) :]
    core = body.strip()
    return leading + delimiter + core + delimiter + trailing if core else body


def _raw_inline_text(node: SemanticNode) -> str:
    """Read authored code-span text before Markdown escaping."""
    if node.text:
        return node.text
    return "".join(_raw_inline_text(child) for child in node.children)


def _artifact_callout_style(node: SemanticNode) -> str:
    """Return fixed, sanitized styling for a verified artifact background."""
    if not node.attrs.get("artifact_background_geometry"):
        return ""
    raw: Any = node.attrs.get("artifact_background_color")
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        return ""
    channels: List[int] = []
    for value in raw:
        if isinstance(value, bool):
            return ""
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return ""
        if not math.isfinite(number) or not 0.0 <= number <= 1.0:
            return ""
        channels.append(round(number * 255.0))
    color = "#%02x%02x%02x" % tuple(channels)
    return (
        ' style="background-color: %s; border-left: 0.25rem solid #6b8fb3; '
        'padding: 0.75rem"' % color
    )


def _escape_destination(value: object) -> str:
    return str(value or "").replace("\\", "%5C").replace("(", "%28").replace(")", "%29").replace(" ", "%20")


def _safe_label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", value).strip("-") or "note"


def _longest_run(text: str, character: str) -> int:
    return max((len(match.group(0)) for match in re.finditer(re.escape(character) + "+", text)), default=0)
