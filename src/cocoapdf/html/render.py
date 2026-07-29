from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .css import DEFAULT_CSS
from .sanitize import (
	escape_text,
	is_safe_generated_html,
	safe_asset_href,
	safe_href,
)


def render_html(markdown: str, report: Optional[Dict[str, Any]] = None) -> str:
	slug_counts: Dict[str, int] = {}
	body = _render_blocks(_split_blocks(markdown), slug_counts)
	title = "CocoaPDF Document"
	if report:
		title = "%s output" % report.get("tool", "CocoaPDF")
	return "<!doctype html>\n<html>\n<head>\n<meta charset=\"utf-8\" />\n<title>%s</title>\n<style>%s</style>\n</head>\n<body>\n%s\n</body>\n</html>\n" % (
		escape_text(title),
		DEFAULT_CSS,
		body,
	)


def _split_blocks(markdown: str) -> List[str]:
	blocks: List[str] = []
	current: List[str] = []
	fence_char = ""
	fence_length = 0
	generated_form = False
	for line in markdown.strip().splitlines():
		fence = re.match(r"^[ \t]*(`{3,}|~{3,})", line)
		if fence_char:
			current.append(line)
			if re.match(r"^[ \t]*%s{%d,}[ \t]*$" % (re.escape(fence_char), fence_length), line):
				fence_char = ""
				fence_length = 0
			continue
		if fence:
			marker = fence.group(1)
			fence_char = marker[0]
			fence_length = len(marker)
			current.append(line)
			continue
		if line.startswith('<div class="cocoapdf-form-appearance" data-cocoapdf-kind="printed">'):
			generated_form = True
		if generated_form:
			current.append(line)
			if line.strip() == "</div>":
				generated_form = False
			continue
		if not line.strip():
			if current:
				blocks.append("\n".join(current))
				current = []
			continue
		current.append(line)
	if current:
		blocks.append("\n".join(current))
	return blocks


def _render_blocks(blocks: List[str], slug_counts: Dict[str, int]) -> str:
	rendered: List[str] = []
	i = 0
	while i < len(blocks):
		if _is_list_block(blocks[i]):
			list_blocks = [blocks[i]]
			i += 1
			while i < len(blocks) and _is_list_block(blocks[i]):
				list_blocks.append(blocks[i])
				i += 1
			rendered.append(_render_list_blocks(list_blocks))
			continue
		rendered.append(_render_block(blocks[i], slug_counts))
		i += 1
	return "\n".join(rendered)


def _render_block(block: str, slug_counts: Optional[Dict[str, int]] = None) -> str:
	stripped = block.strip()
	footnote = re.match(r"^\[\^([^\]\n]+)\]:\s*([\s\S]*)$", stripped)
	if footnote:
		label = re.sub(r"[^A-Za-z0-9_.-]", "-", footnote.group(1)).strip("-") or "note"
		return '<aside id="fn-%s" role="doc-footnote">%s</aside>' % (
			escape_text(label),
			_inline(footnote.group(2)),
		)
	if stripped.startswith("```"):
		lines = stripped.splitlines()
		code = "\n".join(lines[1:-1])
		return "<pre><code>%s</code></pre>" % escape_text(code)
	if is_safe_generated_html(stripped):
		return stripped
	if re.match(r"^!\[[^\]]*\]\([^()]+\)$", stripped):
		return _render_image(stripped)
	if stripped.startswith("|") and "\n|" in stripped:
		return _render_table(stripped)
	if stripped.startswith(">"):
		inner_lines = [re.sub(r"^> ?", "", line) if line.startswith(">") else line for line in stripped.splitlines()]
		inner = "\n".join(inner_lines)
		return "<blockquote>\n%s\n</blockquote>" % _render_blocks(
			_split_blocks(inner),
			slug_counts if slug_counts is not None else {},
		)
	if re.match(r"^#{1,6} ", stripped):
		level = len(stripped.split(" ", 1)[0])
		text = stripped[level + 1 :]
		anchor = re.sub(r"[^a-z0-9]+", "-", re.sub(r"[*_`~<>]", "", text.lower())).strip("-") or "section"
		counts = slug_counts if slug_counts is not None else {}
		ordinal = counts.get(anchor, 0)
		counts[anchor] = ordinal + 1
		if ordinal:
			anchor = "%s-%d" % (anchor, ordinal + 1)
		return "<h%d id=\"%s\">%s</h%d>" % (level, escape_text(anchor), _inline(text), level)
	if re.fullmatch(r"(?:-{3,}|\*{3,}|_{3,})", stripped):
		return "<hr />"
	return "<p>%s</p>" % "<br />\n".join(_inline(line) for line in stripped.splitlines())


def _is_list_block(block: str) -> bool:
	lines = [line for line in block.splitlines() if line.strip()]
	first = lines[0] if lines else ""
	marker = re.match(r"^[ \t]*(-|\d+\.|[A-Za-z]\.)\s+", first)
	if marker is None:
		return False
	if not re.fullmatch(r"[A-Za-z]\.", marker.group(1)):
		return True
	alpha_markers = [
		match.group(1)
		for line in lines
		for match in [re.match(r"^[ \t]*([A-Za-z])\.\s+", line)]
		if match is not None
	]
	return len(alpha_markers) >= 2


def _render_list_blocks(blocks: List[str]) -> str:
	roots: List[Dict[str, Any]] = []
	stack: List[Dict[str, Any]] = []
	current: Optional[Dict[str, Any]] = None
	for block in blocks:
		for raw_line in block.splitlines():
			line = raw_line.expandtabs(4)
			marker = re.match(r"^( *)(-|\d+\.|[A-Za-z]\.)\s+(.*)$", line)
			if marker is None:
				if current is not None and line.strip():
					current["content"] = (current["content"] + " " + line.strip()).strip()
				continue
			indent = len(marker.group(1))
			mark = marker.group(2)
			if mark == "-":
				kind = "ul"
				style = ""
				ordinal = 1
			elif mark[0].isdigit():
				kind = "ol"
				style = "numeric"
				ordinal = int(mark[:-1])
			else:
				kind = "ol"
				style = "A" if mark[0].isupper() else "a"
				ordinal = ord(mark[0].lower()) - ord("a") + 1
			node: Dict[str, Any] = {
				"indent": indent,
				"kind": kind,
				"style": style,
				"ordinal": ordinal,
				"content": marker.group(3).strip(),
				"children": [],
			}
			while stack and indent <= stack[-1]["indent"]:
				stack.pop()
			if stack:
				stack[-1]["children"].append(node)
			else:
				roots.append(node)
			stack.append(node)
			current = node
	return _render_list_siblings(roots)


def _render_list_siblings(nodes: List[Dict[str, Any]]) -> str:
	parts: List[str] = []
	i = 0
	while i < len(nodes):
		signature = (nodes[i]["kind"], nodes[i]["style"])
		group: List[Dict[str, Any]] = []
		while i < len(nodes) and (nodes[i]["kind"], nodes[i]["style"]) == signature:
			group.append(nodes[i])
			i += 1
		tag, style = signature
		attrs = ""
		if tag == "ol":
			if style in ("a", "A"):
				attrs += ' type="%s"' % style
			if group[0]["ordinal"] != 1:
				attrs += ' start="%d"' % group[0]["ordinal"]
		items: List[str] = []
		for node in group:
			content = node["content"]
			task = re.match(r"^\[([ xX])\]\s*(.*)$", content)
			if task:
				checked = " checked" if task.group(1).lower() == "x" else ""
				html = '<input type="checkbox" disabled%s /> %s' % (checked, _inline(task.group(2)))
			else:
				html = _inline(content)
			if node["children"]:
				html += "\n" + _render_list_siblings(node["children"])
			items.append("<li>%s</li>" % html)
		parts.append("<%s%s>\n%s\n</%s>" % (tag, attrs, "\n".join(items), tag))
	return "\n".join(parts)


# Kept as a private compatibility alias for callers that imported the helper
# before it became part of the semantic-output boundary.
_looks_like_safe_generated_html = is_safe_generated_html


def render_inline_fragment(markdown: str) -> str:
	"""Render a converter-generated inline fragment for a safe raw-HTML wrapper."""
	return "<br />\n".join(_inline(line) for line in markdown.splitlines())


def _render_image(block: str) -> str:
	match = re.match(r"^!\[([^\]]*)\]\(([^()]+)\)$", block)
	if not match:
		return "<p>%s</p>" % escape_text(block)
	alt = escape_text(match.group(1))
	src = safe_asset_href(match.group(2))
	if src is None:
		return "<p>%s</p>" % escape_text(block)
	src = escape_text(src)
	return '<img src="%s" alt="%s" />' % (src, alt)


def _render_table(block: str) -> str:
	rows = [_split_table_row(line) for line in block.splitlines() if line.strip()]
	if len(rows) < 2:
		return "<p>%s</p>" % escape_text(block)
	header = rows[0]
	has_separator = all(set(cell.strip()) <= {"-", ":"} and "-" in cell for cell in rows[1])
	body = rows[2:] if has_separator else rows[1:]
	alignments: List[str] = []
	for index in range(len(header)):
		separator = rows[1][index].strip() if has_separator and index < len(rows[1]) else ""
		if separator.startswith(":") and separator.endswith(":"):
			alignments.append("center")
		elif separator.endswith(":"):
			alignments.append("right")
		elif separator.startswith(":"):
			alignments.append("left")
		else:
			alignments.append("")

	def cell(tag: str, value: str, column: int) -> str:
		style = ' style="text-align: %s;"' % alignments[column] if column < len(alignments) and alignments[column] else ""
		return "<%s%s>%s</%s>" % (tag, style, _inline(value.strip()), tag)

	out = ["<table>", "<thead><tr>"]
	out.extend(cell("th", value, column) for column, value in enumerate(header))
	out.append("</tr></thead>")
	out.append("<tbody>")
	for row in body:
		out.append("<tr>" + "".join(cell("td", value, column) for column, value in enumerate(row)) + "</tr>")
	out.append("</tbody></table>")
	return "\n".join(out)


def _split_table_row(line: str) -> List[str]:
	text = line.strip()
	if text.startswith("|"):
		text = text[1:]
	if text.endswith("|") and not text.endswith(r"\|"):
		text = text[:-1]
	row: List[str] = []
	cell: List[str] = []
	escaped = False
	for char in text:
		if escaped:
			cell.append("\\" + char if char != "|" else "|")
			escaped = False
		elif char == "\\":
			escaped = True
		elif char == "|":
			row.append("".join(cell))
			cell = []
		else:
			cell.append(char)
	if escaped:
		cell.append("\\")
	row.append("".join(cell))
	return row


def _inline(text: str) -> str:
	placeholders: List[str] = []

	def hold(value: str) -> str:
		placeholders.append(value)
		return "\x00%d\x00" % (len(placeholders) - 1)

	text = re.sub(
		r"\[\^([^\]\n]+)\]",
		lambda match: hold(
			'<sup><a href="#fn-%s" role="doc-noteref">%s</a></sup>'
			% (
				escape_text(re.sub(r"[^A-Za-z0-9_.-]", "-", match.group(1)).strip("-") or "note"),
				escape_text(match.group(1)),
			)
		),
		text,
	)

	text = re.sub(
		r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])",
		lambda match: hold(escape_text(match.group(1))),
		text,
	)
	def code_span(match: re.Match[str]) -> str:
		body = match.group(2).replace("\n", " ")
		if body.startswith(" ") and body.endswith(" ") and body.strip():
			body = body[1:-1]
		return hold("<code>%s</code>" % escape_text(body))

	text = re.sub(r"(`+)(?!`)(.*?)\1(?!`)", code_span, text)

	text = escape_text(text)
	text = re.sub(r"\*\*\*([^*]+)\*\*\*", r"<strong><em>\1</em></strong>", text)
	text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
	text = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", text)
	text = re.sub(r"~~([^~]+)~~", r"<del>\1</del>", text)
	def render_link(match: re.Match[str]) -> str:
		label = match.group(1)
		href = safe_href(match.group(2))
		if href is None:
			return label
		return '<a href="%s" rel="noopener noreferrer">%s</a>' % (escape_text(href), label)

	def render_bare_link(match: re.Match[str]) -> str:
		href = safe_href(match.group(1))
		if href is None:
			return match.group(1)
		escaped = escape_text(href)
		return '<a href="%s" rel="noopener noreferrer">%s</a>' % (escaped, escaped)

	text = re.sub(r"\[([^\]]+)\]\(([^()\s]*(?:\([^)]*\)[^()\s]*)?)\)", render_link, text)
	text = re.sub(r"&lt;((?:https?://|mailto:)[^&]+)&gt;", render_bare_link, text)
	text = text.replace("&lt;mark&gt;", "<mark>").replace("&lt;/mark&gt;", "</mark>")
	text = text.replace("&lt;sup&gt;", "<sup>").replace("&lt;/sup&gt;", "</sup>")
	text = text.replace("&lt;sub&gt;", "<sub>").replace("&lt;/sub&gt;", "</sub>")
	text = text.replace("&lt;u&gt;", "<u>").replace("&lt;/u&gt;", "</u>")
	for idx, value in enumerate(placeholders):
		text = text.replace("\x00%d\x00" % idx, value)
	return text
