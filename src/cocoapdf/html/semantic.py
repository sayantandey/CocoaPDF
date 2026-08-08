from __future__ import annotations

import base64
import html
import math
import re
from typing import Any, List, Optional, Tuple

from .css import DEFAULT_CSS
from .sanitize import (
    is_safe_generated_html,
    safe_embedded_image_href,
    safe_href,
)
from ..ir.semantic import SemanticDocument, SemanticNode


CONTENT_SECURITY_POLICY = (
    "default-src 'none'; "
    "img-src 'self' data: file:; "
    "style-src 'unsafe-inline'; "
    "base-uri 'none'; form-action 'none'; object-src 'none'"
)

_BLOCK_KINDS = {
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
    "table_head",
    "table_body",
    "table_row",
    "figure",
    "caption",
    "footnote",
    "toc",
    "reference_section",
    "reference",
    "table_note",
    "equation",
    "callout",
    "sidebar",
    "form",
    "form_field",
    "annotation",
    "page_break",
    "html",
    "unknown",
}


def render_semantic_html(document: SemanticDocument, full_document: bool = True) -> str:
    errors = document.validate(require_provenance=False)
    if errors:
        raise ValueError("invalid semantic document: %s" % "; ".join(errors[:8]))
    body = "\n".join(_render_node(node) for node in document.children if node.kind not in {"artifact", "outline"})
    if not full_document:
        return body
    title = html.escape(str(document.metadata.get("title", "CocoaPDF Document")), quote=True)
    language = _document_language(document.metadata)
    language_attr = ' lang="%s"' % html.escape(language, quote=True) if language else ""
    return (
        '<!doctype html>\n<html%s>\n<head>\n<meta charset="utf-8" />\n'
        '<meta http-equiv="Content-Security-Policy" content="%s" />\n'
        '<meta name="referrer" content="no-referrer" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        '<meta name="generator" content="CocoaPDF" />\n'
        '<title>%s</title>\n<style>%s</style>\n</head>\n<body>\n'
        '<main class="cocoapdf-document">\n%s\n</main>\n</body>\n</html>\n'
        % (
            language_attr,
            html.escape(CONTENT_SECURITY_POLICY, quote=True),
            title,
            DEFAULT_CSS,
            body,
        )
    )


def render_minimal_semantic_html(document: SemanticDocument) -> str:
    """Return a safe, graph-derived emergency document without Markdown.

    This deliberately small renderer is used only if the rich semantic
    projection raises. It never interprets source text as markup and therefore
    preserves the architectural guarantee that HTML is not reconstructed by
    reparsing the Markdown output.
    """
    title = html.escape(
        str(document.metadata.get("title", "CocoaPDF Document")),
        quote=True,
    )
    language = _document_language(document.metadata)
    language_attr = (
        ' lang="%s"' % html.escape(language, quote=True)
        if language
        else ""
    )
    blocks: List[str] = []
    for node in document.children:
        if node.kind in {"artifact", "outline"}:
            continue
        text = _minimal_node_text(node)
        if not text:
            continue
        blocks.append(
            '<p data-cocoapdf-fallback-kind="%s">%s</p>'
            % (
                html.escape(str(node.kind), quote=True),
                html.escape(text),
            )
        )
    if not blocks:
        blocks.append(
            '<p data-cocoapdf-fallback-kind="empty">'
            "No renderable semantic text.</p>"
        )
    return (
        '<!doctype html>\n<html%s>\n<head>\n<meta charset="utf-8" />\n'
        '<meta http-equiv="Content-Security-Policy" content="%s" />\n'
        '<meta name="referrer" content="no-referrer" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        '<meta name="generator" content="CocoaPDF" />\n'
        '<title>%s</title>\n</head>\n<body>\n'
        '<main class="cocoapdf-document cocoapdf-minimal-fallback">\n%s\n'
        '</main>\n</body>\n</html>\n'
        % (
            language_attr,
            html.escape(CONTENT_SECURITY_POLICY, quote=True),
            title,
            "\n".join(blocks),
        )
    )


def _minimal_node_text(root: SemanticNode) -> str:
    parts: List[str] = []
    pending = [root]
    visited = set()
    while pending:
        node = pending.pop()
        identity = id(node)
        if identity in visited:
            continue
        visited.add(identity)
        if node.text:
            parts.append(str(node.text))
        elif node.kind == "image" and node.attrs.get("alt"):
            parts.append(str(node.attrs["alt"]))
        pending.extend(reversed(node.children))
    return " ".join(" ".join(part.split()) for part in parts if part.strip())


def _render_node(node: SemanticNode) -> str:
    kind = node.kind
    if node.attrs.get("_html_suppressed"):
        return ""
    data_attrs = _node_data_attrs(node)
    lossless_html = node.attrs.get("_layout_html")
    if (
        isinstance(lossless_html, str)
        and kind in {"section", "form", "callout", "equation", "table"}
        and is_safe_generated_html(lossless_html.strip())
        and not (kind == "table" and _table_has_rotated_cells(node))
    ):
        if kind == "table":
            table = _enrich_lossless_table_headers(lossless_html.strip()).replace(
                "<table>",
                "<table%s>" % data_attrs,
                1,
            )
            return _table_container(
                node,
                table,
                caption_has_id=False,
            )
        wrapper = "aside" if kind == "callout" else "section" if kind in {"section", "form"} else "div"
        css_kind = "layout-region" if kind == "section" else "printed-form" if kind == "form" else kind
        return '<%s class="cocoapdf-%s cocoapdf-lossless-html"%s>%s</%s>' % (
            wrapper,
            css_kind,
            data_attrs,
            lossless_html.strip(),
            wrapper,
        )
    if kind in {"document", "section", "table_head", "table_body", "form"}:
        # PDF forms are documentary source material, not live browser forms.
        # A section avoids creating a submit-capable DOM surface while typed
        # form-field attributes remain available in the semantic JSON.
        tag = "article" if kind == "document" else "section" if kind in {"section", "form"} else "div"
        classes: List[str] = []
        extra_attrs = ""
        if node.attrs.get("layout") == "columns":
            classes.append("cocoapdf-columns")
        if kind == "form":
            classes.append("cocoapdf-form")
            extra_attrs = ' role="group" aria-label="PDF form fields"'
        class_attr = ' class="%s"' % " ".join(classes) if classes else ""
        return "<%s%s%s%s%s%s>%s</%s>" % (
            tag,
            class_attr,
            extra_attrs,
            _block_style_attr(node),
            _direction_language_attrs(node),
            data_attrs,
            "".join(_render_node(child) for child in node.children),
            tag,
        )
    if kind == "anchor":
        return '<a id="%s"%s></a>' % (
            _safe_html_id(node.attrs.get("name") or node.id),
            data_attrs,
        )
    if kind == "page_break":
        return '<hr class="cocoapdf-page-break" data-page="%s"%s />' % (html.escape(str(node.attrs.get("page", "")), quote=True), data_attrs)
    if kind == "heading":
        level = _safe_int(node.attrs.get("level"), 2, 1, 6)
        anchor = _safe_html_id(node.attrs.get("anchor") or node.id)
        return "<h%d id=\"%s\"%s%s%s>%s</h%d>" % (
            level,
            anchor,
            _block_style_attr(node),
            _direction_language_attrs(node),
            data_attrs,
            _render_heading_inlines(node),
            level,
        )
    if kind == "paragraph":
        return "<p%s%s%s>%s</p>" % (
            _block_style_attr(node),
            _direction_language_attrs(node),
            data_attrs,
            _render_inlines(node),
        )
    if kind == "list":
        ordered = bool(node.attrs.get("ordered"))
        tag = "ol" if ordered else "ul"
        list_attrs = (
            _ordered_list_attrs(node)
            if ordered
            else _unordered_list_attrs(node)
        )
        body = "".join(
            _render_list_item(
                child,
                ordered=ordered,
                marker_style=str(node.attrs.get("marker_style") or ""),
            )
            if child.kind == "item"
            else _render_node(child)
            for child in node.children
        )
        return "<%s%s%s%s>%s</%s>" % (
            tag,
            list_attrs,
            _direction_language_attrs(node),
            data_attrs,
            body,
            tag,
        )
    if kind == "item":
        return _render_list_item(node, ordered=False, marker_style="")
    if kind == "quote":
        return "<blockquote%s%s%s>%s</blockquote>" % (
            _block_style_attr(node),
            _direction_language_attrs(node),
            data_attrs,
            _render_mixed_content(node),
        )
    if kind == "code_block":
        info = html.escape(str(node.attrs.get("info", "")), quote=True)
        class_attr = ' class="language-%s"' % info if info else ""
        return "<pre%s%s%s><code%s>%s</code></pre>" % (
            _block_style_attr(node),
            _direction_language_attrs(node),
            data_attrs,
            class_attr,
            html.escape(node.text),
        )
    if kind == "thematic_break":
        return "<hr%s />" % data_attrs
    if kind == "table":
        return _render_table(node, data_attrs)
    if kind == "table_note":
        return '<p class="cocoapdf-table-note"%s%s%s>%s</p>' % (
            _block_style_attr(node),
            _direction_language_attrs(node),
            data_attrs,
            _render_inlines(node),
        )
    if kind == "figure":
        return _render_figure(node, data_attrs)
    if kind == "caption":
        return '<p class="cocoapdf-caption"%s%s%s>%s</p>' % (
            _block_style_attr(node),
            _direction_language_attrs(node),
            data_attrs,
            _render_inlines(node),
        )
    if kind == "image":
        return _render_image(node, data_attrs)
    if kind == "footnote_ref":
        raw_label = str(node.attrs.get("label", node.id))
        label = html.escape(raw_label)
        target = _safe_html_id("fn-" + raw_label)
        return '<sup%s%s><a href="#%s" role="doc-noteref">%s</a></sup>' % (
            _direction_language_attrs(node),
            data_attrs,
            target,
            label,
        )
    if kind == "footnote":
        raw_label = str(node.attrs.get("label", node.id))
        target = _safe_html_id("fn-" + raw_label)
        return '<aside id="%s" role="doc-footnote"%s%s%s>%s</aside>' % (
            target,
            _block_style_attr(node),
            _direction_language_attrs(node),
            data_attrs,
            _render_mixed_content(node),
        )
    if kind == "toc":
        return (
            '<nav class="cocoapdf-toc" aria-label="Table of contents"'
            '%s%s%s><ol>%s</ol></nav>'
            % (
                _block_style_attr(node),
                _direction_language_attrs(node),
                data_attrs,
                "".join(_render_node(child) for child in node.children),
            )
        )
    if kind == "toc_item":
        raw_target = str(node.attrs.get("target_anchor") or node.attrs.get("target_id") or "")
        target = _safe_html_id(raw_target) if raw_target else ""
        inline_children = [
            child for child in node.children if child.kind != "toc_item"
        ]
        if node.text:
            body = html.escape(node.text)
        elif inline_children:
            body = _render_inlines(
                SemanticNode(
                    id=node.id + "-toc-label",
                    kind="toc_item",
                    children=inline_children,
                )
            )
        else:
            body = ""
        if target:
            body = '<a href="#%s">%s</a>' % (target, body)
        page = node.attrs.get("page_label") or node.attrs.get("page")
        if page not in (None, ""):
            body += (
                ' <span class="cocoapdf-toc-page">%s</span>'
                % html.escape(str(page))
            )
        descendants = [child for child in node.children if child.kind == "toc_item"]
        return '<li%s%s>%s%s</li>' % (
            _direction_language_attrs(node),
            data_attrs,
            body,
            "<ol>%s</ol>"
            % "".join(_render_node(child) for child in descendants)
            if descendants
            else "",
        )
    if kind == "reference_section":
        return '<section role="doc-bibliography"%s%s%s>%s</section>' % (
            _block_style_attr(node),
            _direction_language_attrs(node),
            data_attrs,
            "".join(_render_node(child) for child in node.children),
        )
    if kind == "reference":
        label = node.attrs.get("label")
        anchor_value = node.attrs.get("anchor")
        raw_anchor = str(anchor_value) if anchor_value not in (None, "") else ""
        anchor = _safe_html_id(raw_anchor) if raw_anchor else ""
        anchor_attr = ' id="%s"' % anchor if anchor else ""
        return '<p role="doc-biblioentry"%s%s%s%s>%s%s</p>' % (
            anchor_attr,
            _block_style_attr(node),
            _direction_language_attrs(node),
            data_attrs,
            '<span class="cocoapdf-reference-label">[%s]</span> '
            % html.escape(str(label))
            if label
            else "",
            _render_inlines(node),
        )
    if kind in {"callout", "sidebar"}:
        return '<aside class="cocoapdf-%s"%s%s%s>%s</aside>' % (
            kind,
            _block_style_attr(node),
            _direction_language_attrs(node),
            data_attrs,
            _render_mixed_content(node),
        )
    if kind == "equation":
        return '<div class="cocoapdf-equation"%s%s%s>%s</div>' % (
            _block_style_attr(node),
            _direction_language_attrs(node),
            data_attrs,
            _render_mixed_content(node),
        )
    if kind == "form_field":
        return _render_form_field(node, data_attrs)
    if kind == "annotation":
        return '<aside class="cocoapdf-annotation"%s%s%s>%s</aside>' % (
            _block_style_attr(node),
            _direction_language_attrs(node),
            data_attrs,
            _render_mixed_content(node),
        )
    if kind == "html":
        trusted = bool(node.attrs.get("trusted_generated"))
        content = (
            node.text
            if trusted and is_safe_generated_html(node.text.strip())
            else html.escape(node.text)
        )
        return '<div class="cocoapdf-generated-html"%s%s%s>%s</div>' % (
            _block_style_attr(node),
            _direction_language_attrs(node),
            data_attrs,
            content,
        )
    if kind in {"artifact", "outline", "outline_item"}:
        return ""
    if kind == "unknown":
        return '<div class="cocoapdf-unknown"%s%s%s>%s</div>' % (
            _block_style_attr(node),
            _direction_language_attrs(node),
            data_attrs,
            _render_mixed_content(node),
        )
    return _render_inline(node)


def _render_inlines(node: SemanticNode) -> str:
    if node.attrs.get("actual_text") and node.text:
        body = html.escape(node.text)
    elif node.children:
        body = "".join(_render_inline(child) for child in node.children)
    else:
        body = html.escape(node.text)
    expanded = str(node.attrs.get("expanded_text") or "").strip()
    if expanded and body:
        return '<abbr title="%s">%s</abbr>' % (
            html.escape(expanded, quote=True),
            body,
        )
    return body


def _render_heading_inlines(node: SemanticNode) -> str:
    # A heading whose complete glyph run is bold/italic already carries that
    # emphasis through h1-h6 semantics.  Avoid redundant wrappers introduced by
    # the visual font evidence while retaining partial inline emphasis.
    if len(node.children) == 1 and node.children[0].kind in {"strong", "emphasis"}:
        return _render_inlines(node.children[0])
    return _render_inlines(node)


def _render_mixed_content(node: SemanticNode) -> str:
    if node.children and any(
        child.kind in _BLOCK_KINDS for child in node.children
    ):
        return "".join(
            _render_node(child)
            if child.kind in _BLOCK_KINDS
            else _render_inline(child)
            for child in node.children
        )
    return _render_inlines(node) if node.children else html.escape(node.text)


def _render_list_item(
    node: SemanticNode,
    *,
    ordered: bool,
    marker_style: str,
) -> str:
    parts: List[str] = []
    inline_buffer: List[SemanticNode] = []

    def flush_inline() -> None:
        if not inline_buffer:
            return
        parts.append(
            _render_inlines(
                SemanticNode(
                    id=node.id + "-inline",
                    kind="item",
                    children=list(inline_buffer),
                )
            )
        )
        inline_buffer.clear()

    for child in node.children:
        if child.kind in _BLOCK_KINDS:
            flush_inline()
            parts.append(_render_node(child))
        else:
            inline_buffer.append(child)
    flush_inline()
    body = "".join(parts)
    is_task = bool(node.attrs.get("task"))
    if is_task:
        checked = " checked" if node.attrs.get("checked") else ""
        task_text = _task_accessible_text(node)
        task_label = (
            ("%s task: %s" % (
                "Checked" if node.attrs.get("checked") else "Unchecked",
                task_text,
            ))
            if task_text
            else (
                "Checked task"
                if node.attrs.get("checked")
                else "Unchecked task"
            )
        )
        body = '<input type="checkbox" disabled%s aria-label="%s" /> %s' % (
            checked,
            html.escape(task_label, quote=True),
            body,
        )
    value_attr = ""
    if ordered:
        marker = node.attrs.get("marker")
        if marker in (None, ""):
            marker = node.attrs.get("label")
        ordinal = _marker_ordinal(marker, marker_style)
        if ordinal is not None:
            value_attr = ' value="%d"' % ordinal
    class_attr = ' class="cocoapdf-task-item"' if is_task else ""
    return "<li%s%s%s%s%s>%s</li>" % (
        value_attr,
        class_attr,
        _block_style_attr(node),
        _direction_language_attrs(node),
        _node_data_attrs(node),
        body,
    )


def _task_accessible_text(node: SemanticNode) -> str:
    """Return task text without inventing spaces or reading nested lists."""
    parts: List[str] = []
    inline_buffer: List[SemanticNode] = []

    def append_text(value: str) -> None:
        normalized = re.sub(r"\s+", " ", value).strip()
        if normalized:
            parts.append(normalized)

    def flush_inline() -> None:
        if inline_buffer:
            append_text("".join(_semantic_node_text(child) for child in inline_buffer))
            inline_buffer.clear()

    for child in node.children:
        if child.kind == "list":
            continue
        if child.kind in _BLOCK_KINDS:
            flush_inline()
            append_text(_semantic_node_text(child))
        else:
            inline_buffer.append(child)
    flush_inline()
    if not parts and node.text:
        append_text(str(node.text))
    return " ".join(parts)


def _render_inline(node: SemanticNode) -> str:
    body = _render_inlines(node)
    data_attrs = _node_data_attrs(node, include_confidence=False)
    if node.kind == "text":
        if node.attrs.get("hard_break"):
            return "<br />\n"
        language_attrs = _direction_language_attrs(node)
        if language_attrs:
            return "<span%s>%s</span>" % (language_attrs, body)
        return body
    tags = {"strong": "strong", "emphasis": "em", "strikethrough": "del", "underline": "u", "superscript": "sup", "subscript": "sub", "mark": "mark", "code": "code"}
    if node.kind in tags:
        tag = tags[node.kind]
        return "<%s%s%s>%s</%s>" % (tag, _direction_language_attrs(node), data_attrs, body, tag)
    if node.kind == "link":
        href = safe_href(str(node.attrs.get("href", "")))
        return (
            '<a href="%s"%s%s>%s</a>'
            % (
                html.escape(href, quote=True),
                _direction_language_attrs(node),
                data_attrs,
                body,
            )
            if href
            else body
        )
    if node.kind == "cross_reference":
        raw_target = str(node.attrs.get("target_anchor") or node.attrs.get("target_id") or "")
        target = _safe_html_id(raw_target) if raw_target else ""
        return (
            '<a class="cocoapdf-cross-reference" href="#%s"%s%s>%s</a>'
            % (
                target,
                _direction_language_attrs(node),
                data_attrs,
                body,
            )
            if target
            else body
        )
    if node.kind in {"footnote_ref", "image"}:
        return _render_node(node)
    return body or "".join(_render_node(child) for child in node.children)


def _render_table(node: SemanticNode, attrs: str) -> str:
    caption = next((child for child in node.children if child.kind == "caption"), None)
    notes = [child for child in node.children if child.kind == "table_note"]
    sections = _table_sections(node)
    table_label = str(node.attrs.get("summary") or "").strip()
    label_attr = ' aria-label="%s"' % html.escape(table_label, quote=True) if table_label else ""
    parts = [
        "<table%s%s%s%s>"
        % (
            label_attr,
            _block_style_attr(node),
            _direction_language_attrs(node),
            attrs,
        )
    ]
    if caption:
        placement = str(caption.attrs.get("placement", "before"))
        caption_class = ' class="cocoapdf-caption-bottom"' if placement == "after" else ""
        caption_id = _safe_html_id(caption.id)
        parts.append(
            '<caption id="%s"%s%s%s>%s</caption>'
            % (
                caption_id,
                caption_class,
                _direction_language_attrs(caption),
                _node_data_attrs(caption),
                _render_inlines(caption),
            )
        )
    for section_kind, rows in sections:
        section_tag = "thead" if section_kind == "head" else "tbody"
        parts.append("<%s>" % section_tag)
        for row in rows:
            row_is_header = section_kind == "head"
            parts.append(
                "<tr%s%s>"
                % (
                    _direction_language_attrs(row),
                    _node_data_attrs(row),
                )
            )
            for cell in row.children:
                if cell.kind != "table_cell":
                    continue
                tag = (
                    "th"
                    if cell.attrs.get("role") == "th" or row_is_header
                    else "td"
                )
                cell_attrs: List[str] = []
                rowspan = _safe_int(cell.attrs.get("rowspan"), 1, 1, 1000)
                colspan = _safe_int(cell.attrs.get("colspan"), 1, 1, 1000)
                if rowspan > 1:
                    cell_attrs.append('rowspan="%d"' % rowspan)
                if colspan > 1:
                    cell_attrs.append('colspan="%d"' % colspan)
                if tag == "th":
                    scope = _table_header_scope(
                        cell,
                        row_is_header,
                        rowspan,
                        colspan,
                    )
                    if scope:
                        cell_attrs.append('scope="%s"' % scope)
                    cell_attrs.append(
                        'id="%s"'
                        % _safe_html_id(
                            cell.attrs.get("header_id", cell.id)
                            or cell.id
                        )
                    )
                headers = _table_headers(cell.attrs.get("headers"))
                if headers:
                    cell_attrs.append('headers="%s"' % headers)
                cell_styles = _block_style_declarations(cell)
                rotation = _safe_int(
                    cell.attrs.get("rotation"),
                    0,
                    -360_000,
                    360_000,
                )
                if (
                    rotation
                    and not any(
                        style.startswith("writing-mode:")
                        for style in cell_styles
                    )
                ):
                    cell_styles.extend(
                        [
                            "writing-mode: vertical-rl",
                            "text-orientation: mixed",
                        ]
                    )
                if cell_styles:
                    cell_attrs.append(
                        'style="%s;"'
                        % html.escape(
                            "; ".join(cell_styles),
                            quote=True,
                        )
                    )
                body = _render_mixed_content(cell)
                explicit = (
                    " " + " ".join(cell_attrs)
                    if cell_attrs
                    else ""
                )
                parts.append(
                    "<%s%s%s%s>%s</%s>"
                    % (
                        tag,
                        explicit,
                        _direction_language_attrs(cell),
                        _node_data_attrs(cell),
                        body,
                        tag,
                    )
                )
            parts.append("</tr>")
        parts.append("</%s>" % section_tag)
    parts.append("</table>")
    table = _table_container(node, "".join(parts), caption=caption)
    return table + "".join(_render_node(note) for note in notes)


def _rows_under_table_section(
    container: SemanticNode,
) -> List[SemanticNode]:
    rows: List[SemanticNode] = []
    for child in container.children:
        if child.kind == "table_row":
            rows.append(child)
        elif child.kind in {"table_head", "table_body"}:
            rows.extend(_rows_under_table_section(child))
    return rows


def _table_sections(
    node: SemanticNode,
) -> List[Tuple[str, List[SemanticNode]]]:
    head_rows: List[SemanticNode] = []
    body_groups: List[List[SemanticNode]] = []
    loose_rows: List[SemanticNode] = []
    has_sections = False
    for child in node.children:
        if child.kind == "table_head":
            has_sections = True
            head_rows.extend(_rows_under_table_section(child))
        elif child.kind == "table_body":
            has_sections = True
            rows = _rows_under_table_section(child)
            if rows:
                body_groups.append(rows)
        elif child.kind == "table_row":
            loose_rows.append(child)
    if has_sections:
        sections: List[Tuple[str, List[SemanticNode]]] = []
        if head_rows:
            sections.append(("head", head_rows))
        sections.extend(("body", rows) for rows in body_groups)
        if loose_rows:
            sections.append(("body", loose_rows))
        return sections

    header_rows = _safe_int(
        node.attrs.get("header_rows"),
        0,
        0,
        len(loose_rows),
    )
    sections = []
    if header_rows:
        sections.append(("head", loose_rows[:header_rows]))
    if len(loose_rows) > header_rows:
        sections.append(("body", loose_rows[header_rows:]))
    return sections


def _table_rows(node: SemanticNode) -> Tuple[List[SemanticNode], int]:
    sections = _table_sections(node)
    rows = [
        row
        for _section_kind, section_rows in sections
        for row in section_rows
    ]
    header_rows = sum(
        len(section_rows)
        for section_kind, section_rows in sections
        if section_kind == "head"
    )
    return rows, header_rows


def _table_container(
    node: SemanticNode,
    table: str,
    caption: Optional[SemanticNode] = None,
    caption_has_id: bool = True,
) -> str:
    if caption is None:
        caption = next(
            (child for child in node.children if child.kind == "caption"),
            None,
        )
    if caption is not None and caption_has_id:
        accessible = ' aria-labelledby="%s"' % _safe_html_id(caption.id)
    else:
        caption_text = _semantic_node_text(caption) if caption is not None else ""
        label = str(
            node.attrs.get("summary") or caption_text or "Table"
        ).strip() or "Table"
        accessible = ' aria-label="%s"' % html.escape(label, quote=True)
    return (
        '<div class="cocoapdf-table-container" role="region" tabindex="0"%s>'
        "%s</div>"
    ) % (accessible, table)


def _semantic_node_text(node: Optional[SemanticNode]) -> str:
    if node is None:
        return ""
    if node.text:
        return str(node.text)
    return "".join(_semantic_node_text(child) for child in node.children)


def _enrich_lossless_table_headers(fragment: str) -> str:
    """Add deterministic scopes to a validated generated table fragment.

    This is an HTML-only accessibility projection. The original generated
    fragment retained by Markdown is not mutated.
    """
    has_explicit_head = bool(re.search(r"<thead(?:\s|>)", fragment, re.I))
    section = "body" if has_explicit_head else "implicit"
    row_index = -1
    output: List[str] = []
    position = 0
    token_re = re.compile(
        r"</?(?:thead|tbody|tfoot|tr)(?:\s[^<>]*)?>|<th(?:\s[^<>]*)?>",
        re.I,
    )
    for match in token_re.finditer(fragment):
        output.append(fragment[position : match.start()])
        token = match.group(0)
        lowered = token.lower()
        if lowered.startswith("<thead"):
            section = "head"
        elif lowered.startswith("<tbody") or lowered.startswith("<tfoot"):
            section = "body"
        elif lowered.startswith("</thead"):
            section = "body"
        elif lowered.startswith("<tr"):
            row_index += 1
        elif lowered.startswith("<th") and not re.search(
            r"\sscope\s*=",
            token,
            re.I,
        ):
            colspan_match = re.search(
                r'\scolspan\s*=\s*"([1-9][0-9]*)"',
                token,
                re.I,
            )
            rowspan_match = re.search(
                r'\srowspan\s*=\s*"([1-9][0-9]*)"',
                token,
                re.I,
            )
            colspan = int(colspan_match.group(1)) if colspan_match else 1
            rowspan = int(rowspan_match.group(1)) if rowspan_match else 1
            column_header = section == "head" or (
                section == "implicit" and row_index == 0
            )
            scope = (
                "colgroup"
                if column_header and colspan > 1
                else "col"
                if column_header
                else "rowgroup"
                if rowspan > 1
                else "row"
            )
            token = token[:-1] + ' scope="%s">' % scope
        output.append(token)
        position = match.end()
    output.append(fragment[position:])
    enriched = "".join(output)
    return enriched if is_safe_generated_html(enriched) else fragment


def _table_has_rotated_cells(node: SemanticNode) -> bool:
    """Prefer typed table rendering when rotation has semantic evidence.

    The generic lossless table fragment predates cell-level rotation metadata.
    Rendering the typed cells keeps that independently recovered evidence in
    the browser projection instead of silently dropping it.
    """
    return any(
        candidate.kind == "table_cell"
        and bool(
            _safe_int(
                candidate.attrs.get("rotation"),
                0,
                -360_000,
                360_000,
            )
        )
        for candidate in node.walk()
    )


def _render_figure(node: SemanticNode, attrs: str) -> str:
    image = next((child for child in node.children if child.kind == "image"), None)
    alignment = str(image.attrs.get("alignment", "left")) if image is not None else "left"
    if alignment not in {"left", "center", "right"}:
        alignment = "left"
    parts = [
        _render_image(child, _node_data_attrs(child), wrap_alignment=False)
        if child.kind == "image"
        else "<figcaption%s%s%s>%s</figcaption>"
        % (
            _block_style_attr(child),
            _direction_language_attrs(child),
            _node_data_attrs(child),
            _render_inlines(child),
        )
        if child.kind == "caption"
        else _render_node(child)
        for child in node.children
    ]
    return (
        '<figure class="cocoapdf-figure cocoapdf-align-%s"%s%s%s>'
        "%s</figure>"
    ) % (
        alignment,
        _block_style_attr(node),
        _direction_language_attrs(node),
        attrs,
        "".join(parts),
    )


def _render_image(node: SemanticNode, attrs: str, wrap_alignment: bool = True) -> str:
    source = _safe_embedded_image_source(node.attrs.get("src", ""))
    if not source:
        return ""
    alt = html.escape(str(node.attrs.get("alt", node.text)), quote=True)
    width = _safe_dimension(node.attrs.get("display_width_pt"))
    height = _safe_dimension(node.attrs.get("display_height_pt"))
    alignment = str(node.attrs.get("alignment", "left"))
    style = []
    if width > 0:
        style.append("width: %.3fpt" % width)
    if height > 0:
        style.append("height: %.3fpt" % height)
    style.extend(["max-width: 100%", "object-fit: contain"])
    image = '<img src="%s" alt="%s" style="%s"%s />' % (
        html.escape(source, quote=True),
        alt,
        html.escape("; ".join(style), quote=True),
        attrs,
    )
    link = safe_href(str(node.attrs.get("link", ""))) if node.attrs.get("link") else None
    if link:
        image = '<a href="%s" rel="noopener noreferrer">%s</a>' % (html.escape(link, quote=True), image)
    if not wrap_alignment:
        return image
    return '<div class="cocoapdf-align-%s">%s</div>' % (
        alignment if alignment in {"left", "center", "right"} else "left",
        image,
    )


def _render_form_field(node: SemanticNode, attrs: str) -> str:
    kind = str(node.attrs.get("field_type", "unknown"))
    name = html.escape(str(node.attrs.get("name", "")))
    value = html.escape(str(node.attrs.get("value", node.text)))
    state = ""
    if kind in {"checkbox", "radio"}:
        state = "checked" if node.attrs.get("checked") else "not checked"
    elif kind == "signature":
        signature = node.attrs.get("signature") or {}
        if not isinstance(signature, dict):
            signature = {}
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
    value_class, value_attrs = _form_value_appearance_attributes(node)
    # Form extraction is documentary only: never emit active controls or copy
    # PDF actions into HTML. The typed semantic JSON retains options, flags,
    # widget rectangles, appearance evidence, export states, and signatures.
    return (
        '<div class="cocoapdf-form-field" data-field-type="%s" data-name="%s"'
        '%s%s%s>'
        '<span class="cocoapdf-form-field-name">%s</span> '
        '<span class="%s"%s>%s</span></div>'
        % (
            html.escape(kind, quote=True),
            html.escape(str(node.attrs.get("name", "")), quote=True),
            _block_style_attr(node),
            _direction_language_attrs(node),
            attrs,
            display_name,
            value_class,
            value_attrs,
            display,
        )
    )


def _safe_embedded_image_source(raw: Any) -> Optional[str]:
    """Allow only packaged/local image references in generated documents.

    Clickable links may remain external, but an image must never trigger an
    automatic network request merely because the source PDF contained a URI.
    """
    return safe_embedded_image_href(str(raw or ""))


def _form_value_appearance_attributes(
    node: SemanticNode,
) -> Tuple[str, str]:
    appearance = node.attrs.get("appearance")
    if not isinstance(appearance, dict):
        return "cocoapdf-form-field-value", ""

    styles: List[str] = []
    text_color = _css_rgb(appearance.get("text_color_rgb"))
    background = _css_rgb(appearance.get("background_color_rgb"))
    border = _css_rgb(appearance.get("border_color_rgb"))
    font_size = _safe_style_number(
        appearance.get("font_size_pt"),
        minimum=0.5,
        maximum=144.0,
    )
    width = _safe_style_number(
        appearance.get("width_pt"),
        minimum=1.0,
        maximum=1_000.0,
    )
    height = _safe_style_number(
        appearance.get("height_pt"),
        minimum=1.0,
        maximum=1_000.0,
    )
    border_width = _safe_style_number(
        appearance.get("border_width_pt"),
        minimum=0.0,
        maximum=12.0,
    )
    if text_color:
        styles.append("color: %s" % text_color)
    if background:
        styles.append("background-color: %s" % background)
    if font_size is not None:
        styles.append("font-size: %.3fpt" % font_size)
    if width is not None:
        styles.extend(
            [
                "inline-size: %.3fpt" % width,
                "max-inline-size: 100%",
            ]
        )
    if height is not None:
        styles.append("min-block-size: %.3fpt" % height)
    if border:
        width_css = (
            "%.3fpt" % border_width
            if border_width is not None and border_width > 0
            else "1px"
        )
        styles.append("border: %s solid %s" % (width_css, border))
    alignment = str(appearance.get("text_alignment", ""))
    if alignment in {"left", "center", "right"}:
        styles.append(
            "justify-content: %s"
            % {"left": "flex-start", "center": "center", "right": "flex-end"}[
                alignment
            ]
        )

    # A parsed font resource name is provenance, not a portable CSS family;
    # silently mapping /F1 to a browser font would invent semantics.
    sources = appearance.get("sources")
    source_attr = ""
    if isinstance(sources, list):
        safe_sources = [
            str(source)
            for source in sources
            if isinstance(source, str) and source
        ]
        if safe_sources:
            source_attr = ' data-appearance-source="%s"' % html.escape(
                ",".join(safe_sources),
                quote=True,
            )
    if not styles:
        return "cocoapdf-form-field-value", source_attr
    style_attr = ' style="%s"' % html.escape("; ".join(styles), quote=True)
    return (
        "cocoapdf-form-field-value cocoapdf-form-field-value-evidenced",
        source_attr + style_attr,
    )


def _node_data_attrs(
    node: SemanticNode,
    include_confidence: bool = True,
) -> str:
    attributes = [
        'data-cocoapdf-node="%s"' % html.escape(node.id, quote=True),
    ]
    if include_confidence:
        attributes.append('data-confidence="%.4f"' % float(node.confidence))
    pages = node.source_pages()
    if not pages:
        page = _safe_int(node.attrs.get("page"), 0, 0, 10**9)
        if page:
            pages = [page]
    if pages:
        attributes.append(
            'data-source-pages="%s"' % " ".join(str(page) for page in pages)
        )
    if node.warnings:
        attributes.append('data-warning-count="%d"' % len(node.warnings))
    return " " + " ".join(attributes)


def _direction_language_attrs(node: SemanticNode) -> str:
    attributes: List[str] = []
    direction = str(node.attrs.get("direction", "")).lower()
    if direction in {"ltr", "rtl", "auto"}:
        attributes.append('dir="%s"' % direction)
    language = _safe_language(node.attrs.get("language") or node.attrs.get("lang"))
    if language:
        attributes.append('lang="%s"' % html.escape(language, quote=True))
    return (" " + " ".join(attributes)) if attributes else ""


def _block_style_attr(node: SemanticNode) -> str:
    styles = _block_style_declarations(node)
    return (
        ' style="%s"' % html.escape("; ".join(styles), quote=True)
        if styles
        else ""
    )


def _block_style_declarations(node: SemanticNode) -> List[str]:
    styles: List[str] = []
    # Artifact paint is never authored content, but a narrowly verified local
    # background remains graph-native appearance evidence for a callout. Reuse
    # the existing RGB sanitizer so JSON round-trips cannot inject CSS.
    if node.kind == "callout" and node.attrs.get("artifact_background_geometry"):
        background = _css_rgb(node.attrs.get("artifact_background_color"))
        if background:
            styles.append("background-color: %s" % background)
    alignment = str(node.attrs.get("alignment") or "").lower()
    if alignment in {
        "left",
        "right",
        "center",
        "justify",
        "start",
        "end",
    }:
        styles.append("text-align: %s" % alignment)
    indent_em = _safe_style_number(
        node.attrs.get("text_indent_em"),
        minimum=0.0,
        maximum=20.0,
    )
    indent_pt = _safe_style_number(
        node.attrs.get("text_indent_pt"),
        minimum=-10_000.0,
        maximum=10_000.0,
    )
    if indent_em is not None and indent_em > 0:
        styles.append("text-indent: %.3fem" % indent_em)
    elif indent_pt is not None and indent_pt:
        styles.append("text-indent: %.3fpt" % indent_pt)
    for attr, css_name in (
        ("start_indent_pt", "margin-inline-start"),
        ("end_indent_pt", "margin-inline-end"),
        ("space_before_pt", "margin-block-start"),
        ("space_after_pt", "margin-block-end"),
    ):
        value = _safe_style_number(
            node.attrs.get(attr),
            minimum=0.0,
            maximum=10_000.0,
        )
        if value is not None:
            styles.append("%s: %.3fpt" % (css_name, value))
    writing_mode = str(node.attrs.get("writing_mode") or "").lower()
    if writing_mode in {"vertical-lr", "tblr"}:
        styles.extend(
            ["writing-mode: vertical-lr", "text-orientation: mixed"]
        )
    elif writing_mode not in {"", "none", "horizontal", "lrtb", "rltb"}:
        styles.extend(
            ["writing-mode: vertical-rl", "text-orientation: mixed"]
        )
    return styles


def _document_language(metadata: Any) -> str:
    if not isinstance(metadata, dict):
        return ""
    language = metadata.get("language")
    tagged = metadata.get("tagged_pdf")
    if not language and isinstance(tagged, dict):
        language = tagged.get("language")
    return _safe_language(language)


def _safe_language(raw: Any) -> str:
    language = str(raw or "").strip().replace("_", "-")
    if not re.fullmatch(r"[A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*", language):
        return ""
    return language


def _safe_html_id(raw: Any) -> str:
    value = str(raw or "")
    reserved = "_cpdf-b64-"
    if (
        value
        and not value.startswith(reserved)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", value)
    ):
        return value
    encoded = base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")
    return reserved + encoded.rstrip("=")


def _ordered_list_attrs(node: SemanticNode) -> str:
    start = _safe_int(node.attrs.get("start"), 1, -(10**9), 10**9)
    marker_style = _normalized_list_marker_style(node)
    list_type = {
        "upper-alpha": "A",
        "lower-alpha": "a",
        "upper-roman": "I",
        "lower-roman": "i",
    }.get(marker_style, "")
    items = [child for child in node.children if child.kind == "item"]
    marker = items[0].attrs.get("marker") if items else None
    if marker in (None, "") and items:
        marker = items[0].attrs.get("label")
    ordinal = _marker_ordinal(marker, marker_style)
    if ordinal is not None:
        start = ordinal
    if isinstance(marker, str):
        marker = _normalized_marker(marker)
        if (
            not list_type
            and re.fullmatch(r"[A-Za-z]+", marker)
            and len(marker) == 1
        ):
            list_type = "A" if marker.isupper() else "a"
        elif (
            not list_type
            and len(marker) > 1
            and re.fullmatch(r"[ivxlcdmIVXLCDM]+", marker)
        ):
            list_type = "I" if marker.isupper() else "i"
    attributes: List[str] = []
    if list_type:
        attributes.append('type="%s"' % list_type)
    if start != 1:
        attributes.append('start="%d"' % start)
    styles = _block_style_declarations(node)
    if styles:
        attributes.append(
            'style="%s"' % html.escape("; ".join(styles), quote=True)
        )
    return (" " + " ".join(attributes)) if attributes else ""


def _unordered_list_attrs(node: SemanticNode) -> str:
    marker_style = _normalized_list_marker_style(node)
    styles = _block_style_declarations(node)
    if marker_style in {"disc", "circle", "square", "none"}:
        styles.insert(0, "list-style-type: %s" % marker_style)
    return (
        ' style="%s"' % html.escape("; ".join(styles), quote=True)
        if styles
        else ""
    )


def _normalized_list_marker_style(node: SemanticNode) -> str:
    marker_style = str(node.attrs.get("marker_style") or "").lower()
    numbering = str(node.attrs.get("list_numbering") or "").lstrip("/").lower()
    aliases = {
        "decimal": "decimal",
        "upperroman": "upper-roman",
        "upper-roman": "upper-roman",
        "lowerroman": "lower-roman",
        "lower-roman": "lower-roman",
        "upperalpha": "upper-alpha",
        "upper-alpha": "upper-alpha",
        "loweralpha": "lower-alpha",
        "lower-alpha": "lower-alpha",
        "disc": "disc",
        "circle": "circle",
        "square": "square",
        "none": "none",
    }
    return aliases.get(marker_style, aliases.get(numbering, marker_style))


def _normalized_marker(raw: Any) -> str:
    value = str(raw or "").strip()
    value = re.sub(r"^[\[(]\s*", "", value)
    value = re.sub(r"\s*[\]).]+$", "", value)
    return value.strip()


def _marker_ordinal(raw: Any, marker_style: str) -> Optional[int]:
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw if -(10**9) <= raw <= 10**9 else None
    value = _normalized_marker(raw)
    if not value:
        return None
    if re.fullmatch(r"[+-]?\d+", value):
        number = int(value)
        return number if -(10**9) <= number <= 10**9 else None
    style = str(marker_style or "").lower()
    if style in {"upper-roman", "lower-roman"}:
        return _roman_value(value) or None
    if style in {"upper-alpha", "lower-alpha"} and re.fullmatch(
        r"[A-Za-z]+",
        value,
    ):
        return _alpha_value(value) or None
    if len(value) > 1 and re.fullmatch(r"[ivxlcdmIVXLCDM]+", value):
        return _roman_value(value) or None
    if re.fullmatch(r"[A-Za-z]", value):
        return _alpha_value(value) or None
    return None


def _alpha_value(raw: str) -> int:
    value = 0
    for character in raw.lower():
        value = value * 26 + ord(character) - ord("a") + 1
    return value if 0 < value <= 10**9 else 0


def _roman_value(raw: str) -> int:
    values = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    total = 0
    previous = 0
    for character in reversed(raw.lower()):
        value = values.get(character, 0)
        total += -value if value < previous else value
        previous = max(previous, value)
    return total if 0 < total <= 10**9 else 0


def _table_header_scope(
    cell: SemanticNode,
    column_header: bool,
    rowspan: int,
    colspan: int,
) -> str:
    explicit = str(cell.attrs.get("scope") or "").strip().lower()
    if explicit in {"column", "col"}:
        return "colgroup" if colspan > 1 else "col"
    if explicit in {"row"}:
        return "rowgroup" if rowspan > 1 else "row"
    if explicit in {"colgroup", "rowgroup"}:
        return explicit
    if column_header:
        return "colgroup" if colspan > 1 else "col"
    return "rowgroup" if rowspan > 1 else "row"


def _table_headers(raw: Any) -> str:
    if raw is None:
        return ""
    values = raw if isinstance(raw, (list, tuple)) else str(raw).split()
    identifiers = [_safe_html_id(value) for value in values if str(value).strip()]
    return " ".join(identifiers)


def _safe_int(
    raw: Any,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(raw, bool):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(maximum, max(minimum, value))


def _safe_dimension(raw: Any) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0.0
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        return 0.0
    return min(value, 100_000.0)


def _css_rgb(raw: Any) -> Optional[str]:
    if (
        not isinstance(raw, (list, tuple))
        or len(raw) != 3
        or any(isinstance(value, bool) for value in raw)
    ):
        return None
    values: List[int] = []
    for value in raw:
        if not isinstance(value, (int, float)):
            return None
        number = float(value)
        if not math.isfinite(number):
            return None
        values.append(int(round(min(1.0, max(0.0, number)) * 255.0)))
    return "rgb(%d, %d, %d)" % tuple(values)


def _safe_style_number(
    raw: Any,
    *,
    minimum: float,
    maximum: float,
) -> Optional[float]:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if not math.isfinite(value) or value < minimum:
        return None
    return min(value, maximum)
