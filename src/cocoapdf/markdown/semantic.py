from __future__ import annotations

import html
import re
from typing import List

from ..html.sanitize import safe_asset_href, safe_href
from ..ir.semantic import SemanticDocument, SemanticNode


def render_semantic_markdown(document: SemanticDocument) -> str:
    errors = document.validate(require_provenance=False)
    if errors:
        raise ValueError("invalid semantic document: %s" % "; ".join(errors[:8]))
    blocks = [_render_block(node) for node in document.children if node.kind not in {"artifact", "outline"}]
    rendered = "\n\n".join(block for block in blocks if block.strip()).strip()
    return rendered + ("\n" if rendered else "")


def _render_block(node: SemanticNode) -> str:
    kind = node.kind
    if kind in {"document", "section", "table_head", "table_body", "form"}:
        if kind == "section" and node.attrs.get("layout") == "columns":
            body = "\n\n".join(filter(None, (_render_block(child) for child in node.children)))
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
            return '<p style="writing-mode: vertical-rl; text-orientation: mixed;">%s</p>' % body
        if alignment in {"center", "right"}:
            return '<p align="%s">%s</p>' % (alignment, body)
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
        body = _render_inlines(node) or "\n\n".join(_render_block(child) for child in node.children)
        return '<aside class="cocoapdf-%s">%s</aside>' % (kind, body)
    if kind == "equation":
        body = _render_inlines(node) or _escape(node.text)
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
        return "**%s**" % body
    if node.kind == "emphasis":
        return "*%s*" % body
    if node.kind == "strikethrough":
        return "~~%s~~" % body
    if node.kind == "underline":
        return "<u>%s</u>" % body
    if node.kind == "superscript":
        return "<sup>%s</sup>" % body
    if node.kind == "subscript":
        return "<sub>%s</sub>" % body
    if node.kind == "mark":
        return "<mark>%s</mark>" % body
    if node.kind == "code":
        fence = "`" * max(1, _longest_run(node.text or body, "`") + 1)
        content = node.text or body
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
    source = safe_asset_href(str(node.attrs.get("src", "")))
    if not source:
        return ""
    alt = _escape(str(node.attrs.get("alt", node.text)))
    width = float(node.attrs.get("display_width_pt", 0.0) or 0.0)
    height = float(node.attrs.get("display_height_pt", 0.0) or 0.0)
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
    rows = [child for child in node.children if child.kind == "table_row"]
    captions = [child for child in node.children if child.kind == "caption"]
    notes = [child for child in node.children if child.kind == "table_note"]
    if not rows:
        return ""
    complex_table = node.attrs.get("output_mode") == "html" or int(node.attrs.get("header_rows", 0)) not in {0, 1} or any(
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
    header_rows = int(node.attrs.get("header_rows", 0))
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
    rows = [child for child in node.children if child.kind == "table_row"]
    caption = next((child for child in node.children if child.kind == "caption"), None)
    notes = [child for child in node.children if child.kind == "table_note"]
    header_rows = int(node.attrs.get("header_rows", 0))
    out = ["<table>"]
    if caption:
        out.append("<caption>%s</caption>" % _render_inlines(caption))
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
            body = "".join(_render_block(child) if child.kind in {"paragraph", "list", "code_block", "figure"} else _render_inline(child) for child in cell.children)
            out.append("<%s%s>%s</%s>" % (tag, (" " + " ".join(attrs)) if attrs else "", body, tag))
        out.append("</tr>")
    if header_rows:
        out.append("</tbody>" if len(rows) > header_rows else "</thead>")
    out.append("</table>")
    out.extend('<p class="cocoapdf-table-note">%s</p>' % _render_inlines(note) for note in notes)
    return "\n".join(out)


def _render_figure(node: SemanticNode) -> str:
    parts = [_render_inline(child) if child.kind == "image" else "*%s*" % _render_inlines(child) if child.kind == "caption" else _render_block(child) for child in node.children]
    return "\n\n".join(filter(None, parts))


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
    for character in ("`", "*", "_", "~", "[", "]", "<", ">"):
        value = value.replace(character, "\\" + character)
    return value


def _escape_destination(value: object) -> str:
    return str(value or "").replace("\\", "%5C").replace("(", "%28").replace(")", "%29").replace(" ", "%20")


def _safe_label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", value).strip("-") or "note"


def _longest_run(text: str, character: str) -> int:
    return max((len(match.group(0)) for match in re.finditer(re.escape(character) + "+", text)), default=0)
