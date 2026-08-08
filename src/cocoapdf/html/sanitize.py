from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit

SAFE_HREF_SCHEMES = {"http", "https", "mailto", "tel"}
UNSAFE_HREF_SCHEMES = {"javascript", "data", "file", "vbscript", "livescript"}


def escape_text(text: str) -> str:
	return html.escape(text, quote=True)


def safe_href(uri: str) -> Optional[str]:
	href = html.unescape(str(uri or "")).strip()
	if not href or re.search(r"[\x00-\x20\x7f]", href):
		return None
	if href.startswith("#"):
		return href if re.match(r"^#[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+$", href) else None
	parsed = urlsplit(href)
	if parsed.scheme.lower() in SAFE_HREF_SCHEMES:
		return href
	return None


def is_unsafe_href(uri: str) -> bool:
	href = html.unescape(str(uri or "")).strip()
	if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", href):
		return True
	return urlsplit(href).scheme.lower() in UNSAFE_HREF_SCHEMES


def safe_asset_href(uri: str) -> Optional[str]:
	href = html.unescape(str(uri or "")).strip()
	if not href or re.search(r"[\x00-\x20\x7f]", href):
		return None
	absolute = safe_href(href)
	if absolute is not None:
		return absolute
	if re.fullmatch(
		r"data:image/(?:png|jpeg|jpg|gif|webp|svg\+xml);base64,[A-Za-z0-9+/=]+",
		href,
		re.I,
	):
		return href
	parsed = urlsplit(href)
	if parsed.scheme or parsed.netloc or href.startswith(("/", "\\", "~")):
		return None
	parts = re.split(r"[\\/]+", href)
	if any(part in ("", ".", "..") for part in parts):
		return None
	if not all(re.fullmatch(r"[A-Za-z0-9._~@%+\-]+", part) for part in parts):
		return None
	return href.replace("\\", "/")


def safe_embedded_image_href(uri: str) -> Optional[str]:
	"""Allow only packaged/local image sources that cannot fetch a network URI.

	Clickable image links use :func:`safe_href` and may intentionally be
	external.  The image ``src`` itself has a stricter contract: a generated
	document must not initiate a request merely because a PDF or reconstructed
	semantic graph supplied an HTTP URL.
	"""
	source = safe_asset_href(uri)
	if source is None:
		return None
	scheme = urlsplit(source).scheme.lower()
	return source if scheme in {"", "data"} else None


def is_safe_generated_html(block: str) -> bool:
	"""Recognize CocoaPDF's closed set of internally generated HTML fragments.

	This is intentionally narrower than a general HTML sanitizer.  PDF text is
	never admitted as arbitrary markup; only deterministic shapes emitted by
	CocoaPDF itself can cross this boundary.
	"""
	if re.match(r"^<table>[\s\S]*</table>$", block):
		return _is_safe_generated_table(block)
	if re.match(
		r'^<div style="border: 1px solid #[0-9a-fA-F]{6}; '
		r'background: #[0-9a-fA-F]{6}; padding: 12px;">[\s\S]*</div>$',
		block,
	):
		return _is_safe_generated_callout(block)
	if re.match(
		r'^<div class="cocoapdf-columns" style="columns: 2; column-gap: 2rem; '
		r'border-left: [1-9][0-9]*px solid #[0-9a-fA-F]{6}; '
		r'padding-left: [0-9.]+rem;">[\s\S]*</div>$',
		block,
	):
		return _is_safe_generated_columns(block)
	if re.match(
		r'^<div class="cocoapdf-form-appearance" '
		r'data-cocoapdf-kind="printed">[\s\S]*</div>$',
		block,
	):
		return _is_safe_generated_form_appearance(block)
	if re.match(r'^<p align="(?:center|right)">[\s\S]*</p>$', block):
		return _is_safe_generated_paragraph(block, "align")
	if re.match(r'^<p style="text-indent: [0-9.]+em;">[\s\S]*</p>$', block):
		return _is_safe_generated_paragraph(block, "indent")
	if re.match(
		r'^<p style="writing-mode: vertical-rl; '
		r'text-orientation: mixed;">[\s\S]*</p>$',
		block,
	):
		return _is_safe_generated_paragraph(block, "vertical")
	if re.match(r'^<p dir="rtl">[\s\S]*</p>$', block):
		return _is_safe_generated_paragraph(block, "rtl")
	if re.match(r'^<math display="block">[\s\S]*</math>$', block):
		return _is_safe_generated_math(block)
	if re.match(
		r'^<img src="[^"]+" alt="[^"]*" width="[0-9]+" height="[0-9]+"'
		r'(?: style="[^"]*")? />$',
		block,
	):
		return _is_safe_generated_image(block)
	if re.match(r'^<a id="[A-Za-z0-9._~:-]+"></a>$', block):
		return True
	if re.match(r"^<!-- page \d+(?:: [A-Za-z0-9 ()?_-]+)? -->$", block):
		return True
	return _looks_like_safe_generated_figure(block)


_INLINE_FRAGMENT_ATTRIBUTES: Dict[str, Set[str]] = {
	"strong": set(),
	"em": set(),
	"code": set(),
	"del": set(),
	"u": set(),
	"sup": set(),
	"sub": set(),
	"mark": set(),
	"a": {"href", "rel", "role"},
	"br": set(),
}


class _ClosedFragmentValidator(HTMLParser):
	"""Validate one well-nested fragment against exact tags and attributes."""

	def __init__(
		self,
		allowed_attributes: Dict[str, Set[str]],
		attribute_validator: Callable[[str, str, Optional[str]], bool],
		void_tags: Optional[Set[str]] = None,
	) -> None:
		super().__init__(convert_charrefs=False)
		self.allowed_attributes = allowed_attributes
		self.attribute_validator = attribute_validator
		self.void_tags = set(void_tags or ())
		self.valid = True
		self.stack: List[str] = []
		self.roots: List[str] = []
		self.elements: List[
			Tuple[str, Tuple[str, ...], Dict[str, Optional[str]]]
		] = []

	def handle_starttag(
		self,
		tag: str,
		attrs: List[Tuple[str, Optional[str]]],
	) -> None:
		tag = tag.lower()
		path = tuple(self.stack)
		if not self.stack:
			self.roots.append(tag)
		allowed = self.allowed_attributes.get(tag)
		if allowed is None:
			self.valid = False
			allowed = set()
		attribute_map: Dict[str, Optional[str]] = {}
		for raw_name, value in attrs:
			name = raw_name.lower()
			if name in attribute_map or name not in allowed:
				self.valid = False
				continue
			attribute_map[name] = value
			if not self.attribute_validator(tag, name, value):
				self.valid = False
		self.elements.append((tag, path, attribute_map))
		if tag not in self.void_tags:
			self.stack.append(tag)

	def handle_startendtag(
		self,
		tag: str,
		attrs: List[Tuple[str, Optional[str]]],
	) -> None:
		self.handle_starttag(tag, attrs)
		if tag.lower() not in self.void_tags and self.stack:
			self.stack.pop()

	def handle_endtag(self, tag: str) -> None:
		tag = tag.lower()
		if tag in self.void_tags:
			return
		if not self.stack or self.stack[-1] != tag:
			self.valid = False
			return
		self.stack.pop()

	def handle_comment(self, _data: str) -> None:
		self.valid = False

	def handle_decl(self, _decl: str) -> None:
		self.valid = False

	def handle_pi(self, _data: str) -> None:
		self.valid = False

	def unknown_decl(self, _data: str) -> None:
		self.valid = False


def _validate_closed_fragment(
	block: str,
	root: str,
	allowed_attributes: Dict[str, Set[str]],
	attribute_validator: Callable[[str, str, Optional[str]], bool],
	void_tags: Optional[Set[str]] = None,
) -> Tuple[bool, _ClosedFragmentValidator]:
	if re.search(
		r"<script|<style|<iframe|<object|<embed|<template|"
		r"on\w+\s*=|javascript:|vbscript:",
		block,
		re.I,
	):
		validator = _ClosedFragmentValidator(
			allowed_attributes,
			attribute_validator,
			void_tags,
		)
		validator.valid = False
		return False, validator
	validator = _ClosedFragmentValidator(
		allowed_attributes,
		attribute_validator,
		void_tags,
	)
	try:
		validator.feed(block)
		validator.close()
	except Exception:
		validator.valid = False
	return (
		validator.valid
		and not validator.stack
		and validator.roots == [root],
		validator,
	)


def _valid_inline_attribute(
	tag: str,
	name: str,
	value: Optional[str],
) -> bool:
	text = html.unescape(value or "")
	if tag != "a":
		return False
	if name == "href":
		return safe_href(text) is not None
	if name == "rel":
		return text == "noopener noreferrer"
	if name == "role":
		return text == "doc-noteref"
	return False


def _is_safe_generated_callout(block: str) -> bool:
	allowed = {"div": {"style"}, "strong": set()}

	def valid(tag: str, name: str, value: Optional[str]) -> bool:
		return bool(
			tag == "div"
			and name == "style"
			and re.fullmatch(
				r"border: 1px solid #[0-9a-fA-F]{6}; "
				r"background: #[0-9a-fA-F]{6}; padding: 12px;",
				value or "",
			)
		)

	ok, validator = _validate_closed_fragment(block, "div", allowed, valid)
	return ok and all(
		tag == "div" or (tag == "strong" and path == ("div",))
		for tag, path, _attrs in validator.elements
	)


def _is_safe_generated_columns(block: str) -> bool:
	allowed = {
		"div": {"class", "style"},
		"p": set(),
		**_INLINE_FRAGMENT_ATTRIBUTES,
	}

	def valid(tag: str, name: str, value: Optional[str]) -> bool:
		text = value or ""
		if tag == "div" and name == "class":
			return text == "cocoapdf-columns"
		if tag == "div" and name == "style":
			return bool(
				re.fullmatch(
					r"columns: 2; column-gap: 2rem; "
					r"border-left: [1-9][0-9]*px solid #[0-9a-fA-F]{6}; "
					r"padding-left: [0-9.]+rem;",
					text,
				)
			)
		return _valid_inline_attribute(tag, name, value)

	ok, validator = _validate_closed_fragment(
		block,
		"div",
		allowed,
		valid,
		{"br"},
	)
	for tag, path, attrs in validator.elements:
		if tag == "div" and (
			path
			or set(attrs) != {"class", "style"}
		):
			return False
		if tag == "p" and path != ("div",):
			return False
		if tag not in {"div", "p"} and "p" not in path:
			return False
	return ok


def _is_safe_generated_form_appearance(block: str) -> bool:
	allowed = {
		"div": {"class", "data-cocoapdf-kind"},
		"label": set(),
		"input": {"type", "value", "checked", "disabled"},
		"select": {"disabled"},
		"option": {"selected"},
	}

	def valid(tag: str, name: str, value: Optional[str]) -> bool:
		if tag == "div":
			return (
				(name == "class" and value == "cocoapdf-form-appearance")
				or (name == "data-cocoapdf-kind" and value == "printed")
			)
		if tag == "input" and name == "type":
			return value in {"text", "checkbox"}
		if tag == "input" and name == "value":
			return value is not None
		if (
			(tag == "input" and name in {"checked", "disabled"})
			or (tag == "select" and name == "disabled")
			or (tag == "option" and name == "selected")
		):
			return value is None
		return False

	ok, validator = _validate_closed_fragment(
		block,
		"div",
		allowed,
		valid,
		{"input"},
	)
	inputs = []
	selects = 0
	options = 0
	for tag, path, attrs in validator.elements:
		keys = set(attrs)
		if tag == "div":
			if path or keys != {"class", "data-cocoapdf-kind"}:
				return False
		elif tag == "label":
			if path != ("div",) or keys:
				return False
		elif tag == "input":
			if path != ("div", "label"):
				return False
			if attrs.get("type") == "text":
				if keys != {"type", "value", "disabled"}:
					return False
			elif keys not in (
				{"type", "disabled"},
				{"type", "checked", "disabled"},
			):
				return False
			inputs.append(attrs)
		elif tag == "select":
			if path != ("div",) or keys != {"disabled"}:
				return False
			selects += 1
		elif tag == "option":
			if path != ("div", "select") or keys != {"selected"}:
				return False
			options += 1
	return (
		ok
		and bool(inputs)
		and any(item.get("type") == "checkbox" for item in inputs)
		and options == selects
	)


def _is_safe_generated_paragraph(block: str, mode: str) -> bool:
	allowed = {"p": {"align", "style", "dir"}, **_INLINE_FRAGMENT_ATTRIBUTES}

	def valid(tag: str, name: str, value: Optional[str]) -> bool:
		if tag == "p":
			if mode == "align":
				return name == "align" and value in {"center", "right"}
			if mode == "indent":
				return bool(
					name == "style"
					and re.fullmatch(
						r"text-indent: [0-9.]+em;",
						value or "",
					)
				)
			if mode == "vertical":
				return (
					name == "style"
					and value
					== "writing-mode: vertical-rl; text-orientation: mixed;"
				)
			return name == "dir" and value == "rtl"
		return _valid_inline_attribute(tag, name, value)

	ok, validator = _validate_closed_fragment(
		block,
		"p",
		allowed,
		valid,
		{"br"},
	)
	return ok and all(
		(tag == "p" and not path and len(attrs) == 1)
		or (tag != "p" and bool(path) and path[0] == "p")
		for tag, path, attrs in validator.elements
	)


def _is_safe_generated_math(block: str) -> bool:
	allowed = {
		tag: set()
		for tag in {
			"math", "mrow", "mi", "mn", "mo", "msup", "msub",
			"msubsup", "mfrac", "msqrt",
		}
	}
	allowed["math"] = {"display"}

	def valid(tag: str, name: str, value: Optional[str]) -> bool:
		return tag == "math" and name == "display" and value == "block"

	ok, validator = _validate_closed_fragment(block, "math", allowed, valid)
	return ok and all(
		(tag == "math" and not path and attrs == {"display": "block"})
		or (tag != "math" and bool(path) and path[0] == "math" and not attrs)
		for tag, path, attrs in validator.elements
	)


def _is_safe_generated_image(block: str) -> bool:
	allowed = {"img": {"src", "alt", "width", "height", "style"}}

	def valid(tag: str, name: str, value: Optional[str]) -> bool:
		text = html.unescape(value or "")
		if tag != "img":
			return False
		if name == "src":
			return _is_safe_generated_asset_source(text)
		if name == "alt":
			return value is not None
		if name in {"width", "height"}:
			return bool(re.fullmatch(r"[0-9]{1,6}", text))
		if name == "style":
			return _valid_generated_image_style(text)
		return False

	ok, validator = _validate_closed_fragment(
		block,
		"img",
		allowed,
		valid,
		{"img"},
	)
	return ok and len(validator.elements) == 1 and set(
		validator.elements[0][2]
	) in (
		{"src", "alt", "width", "height"},
		{"src", "alt", "width", "height", "style"},
	)


class _GeneratedTableValidator(HTMLParser):
	_ALLOWED_ATTRIBUTES = {
		"table": set(),
		"caption": set(),
		"thead": set(),
		"tbody": set(),
		"tfoot": set(),
		"tr": set(),
		"th": {"rowspan", "colspan", "scope", "style"},
		"td": {"rowspan", "colspan", "style"},
		"p": set(),
		"strong": set(),
		"em": set(),
		"code": set(),
		"del": set(),
		"u": set(),
		"sup": set(),
		"sub": set(),
		"mark": set(),
		"span": {"style"},
		"a": {"href", "rel", "role"},
		"ul": set(),
		"ol": {"type", "start"},
		"li": {"value"},
		"br": set(),
	}
	_VOID = {"br"}

	def __init__(self) -> None:
		super().__init__(convert_charrefs=False)
		self.valid = True
		self.stack: List[str] = []
		self.roots: List[str] = []
		self.elements: List[Tuple[str, Tuple[str, ...]]] = []
		self.text_paths: List[Tuple[str, ...]] = []

	def handle_starttag(
		self,
		tag: str,
		attrs: List[Tuple[str, Optional[str]]],
	) -> None:
		tag = tag.lower()
		path = tuple(self.stack)
		if not self.stack:
			self.roots.append(tag)
		if tag not in self._ALLOWED_ATTRIBUTES:
			self.valid = False
			return
		self.elements.append((tag, path))
		seen = set()
		for raw_name, raw_value in attrs:
			name = raw_name.lower()
			value = raw_value or ""
			if name in seen or name not in self._ALLOWED_ATTRIBUTES[tag]:
				self.valid = False
				continue
			seen.add(name)
			if not self._valid_attribute(tag, name, value):
				self.valid = False
		if tag not in self._VOID:
			self.stack.append(tag)

	def handle_startendtag(
		self,
		tag: str,
		attrs: List[Tuple[str, Optional[str]]],
	) -> None:
		self.handle_starttag(tag, attrs)
		if tag.lower() not in self._VOID and self.stack:
			self.stack.pop()

	def handle_endtag(self, tag: str) -> None:
		tag = tag.lower()
		if tag in self._VOID:
			return
		if not self.stack or self.stack[-1] != tag:
			self.valid = False
			return
		self.stack.pop()

	def handle_comment(self, _data: str) -> None:
		self.valid = False

	def handle_decl(self, _decl: str) -> None:
		self.valid = False

	def handle_pi(self, _data: str) -> None:
		self.valid = False

	def unknown_decl(self, _data: str) -> None:
		self.valid = False

	def handle_data(self, data: str) -> None:
		if data.strip():
			self.text_paths.append(tuple(self.stack))

	@staticmethod
	def _valid_attribute(tag: str, name: str, value: str) -> bool:
		if name in {"rowspan", "colspan"}:
			return bool(re.fullmatch(r"[1-9][0-9]{0,8}", value))
		if name in {"start", "value"}:
			return bool(re.fullmatch(r"-?[0-9]{1,9}", value))
		if name == "scope":
			return value in {"row", "col", "rowgroup", "colgroup"}
		if name == "type":
			return value in {"1", "a", "A", "i", "I"}
		if name == "href":
			return safe_href(html.unescape(value)) is not None
		if name == "rel":
			return value == "noopener noreferrer"
		if name == "role":
			return value == "doc-noteref"
		if name == "style":
			return bool(
				re.fullmatch(
					r"writing-mode: vertical-rl; text-orientation: mixed;",
					value,
				)
			)
		return False


def _is_safe_generated_table(block: str) -> bool:
	if re.search(
		r"<script|<style|<iframe|<object|<embed|on\w+\s*=|javascript:",
		block,
		re.I,
	):
		return False
	validator = _GeneratedTableValidator()
	try:
		validator.feed(block)
		validator.close()
	except Exception:
		return False
	return (
		validator.valid
		and not validator.stack
		and validator.roots == ["table"]
		and _valid_generated_table_structure(validator)
	)


def _valid_generated_table_structure(
	validator: _GeneratedTableValidator,
) -> bool:
	direct = [
		tag
		for tag, path in validator.elements
		if path == ("table",)
	]
	if direct.count("caption") > 1 or direct.count("thead") > 1:
		return False
	if direct.count("tfoot") > 1:
		return False
	if "caption" in direct and direct[0] != "caption":
		return False
	if "thead" in direct and any(
		tag in {"tbody", "tfoot", "tr"}
		for tag in direct[: direct.index("thead")]
	):
		return False
	if "tbody" in direct and "tr" in direct:
		return False

	rows = 0
	cells = 0
	for tag, path in validator.elements:
		parent = path[-1] if path else ""
		if tag == "table":
			if path:
				return False
		elif tag == "caption":
			if path != ("table",):
				return False
		elif tag in {"thead", "tbody", "tfoot"}:
			if path != ("table",):
				return False
		elif tag == "tr":
			if parent not in {"table", "thead", "tbody", "tfoot"}:
				return False
			rows += 1
		elif tag in {"th", "td"}:
			if parent != "tr":
				return False
			cells += 1
		elif tag == "p":
			if parent not in {"th", "td", "li"}:
				return False
		elif tag in {"ul", "ol"}:
			if parent not in {"th", "td", "li"}:
				return False
		elif tag == "li":
			if parent not in {"ul", "ol"}:
				return False
		else:
			if parent in {
				"table",
				"thead",
				"tbody",
				"tfoot",
				"tr",
				"ul",
				"ol",
			}:
				return False
			if not any(
				ancestor in {"caption", "th", "td", "li", "p"}
				for ancestor in path
			):
				return False
			if tag == "a" and "a" in path:
				return False
	for path in validator.text_paths:
		if not path or path[-1] in {
			"table",
			"thead",
			"tbody",
			"tfoot",
			"tr",
			"ul",
			"ol",
		}:
			return False
		if not any(
			ancestor in {"caption", "th", "td", "li", "p"}
			for ancestor in path
		):
			return False
	return rows > 0 and cells > 0


def _looks_like_safe_generated_figure(block: str) -> bool:
	if not re.match(
		r'^<figure class="cocoapdf-figure '
		r'cocoapdf-align-(?:left|center|right)">[\s\S]*</figure>$',
		block,
	):
		return False
	allowed = {
		"figure": {"class"},
		"img": {"src", "alt", "style"},
		"a": {"href", "rel"},
		"figcaption": set(),
	}

	def valid(tag: str, name: str, value: Optional[str]) -> bool:
		text = html.unescape(value or "")
		if tag == "figure" and name == "class":
			return bool(
				re.fullmatch(
					r"cocoapdf-figure cocoapdf-align-(?:left|center|right)",
					text,
				)
			)
		if tag == "img" and name == "src":
			return _is_safe_generated_asset_source(text)
		if tag == "img" and name == "alt":
			return value is not None
		if tag == "img" and name == "style":
			return _valid_generated_image_style(text)
		if tag == "a" and name == "href":
			return safe_href(text) is not None
		if tag == "a" and name == "rel":
			return text == "noopener noreferrer"
		return False

	ok, validator = _validate_closed_fragment(
		block,
		"figure",
		allowed,
		valid,
		{"img"},
	)
	images = 0
	links = 0
	captions = 0
	for tag, path, attrs in validator.elements:
		if tag == "figure":
			if path or set(attrs) != {"class"}:
				return False
		elif tag == "img":
			if path not in {("figure",), ("figure", "a")}:
				return False
			if set(attrs) != {"src", "alt", "style"}:
				return False
			images += 1
		elif tag == "a":
			if path != ("figure",) or set(attrs) != {"href", "rel"}:
				return False
			links += 1
		elif tag == "figcaption":
			if path != ("figure",) or attrs:
				return False
			captions += 1
	if not ok or images != 1 or links > 1 or captions > 1:
		return False
	if links:
		image_path = next(
			path
			for tag, path, _attrs in validator.elements
			if tag == "img"
		)
		if image_path != ("figure", "a"):
			return False
	return True


def _valid_generated_image_style(style: str) -> bool:
	return bool(
		re.fullmatch(
			r"width: [0-9.]+pt; height: [0-9.]+pt; "
			r"max-width: 100%; object-fit: contain;"
			r"(?: display: block; margin-left: auto;"
			r"(?: margin-right: auto;)?)?",
			style,
		)
	)


def _is_safe_generated_asset_source(source: str) -> bool:
	if safe_embedded_image_href(source) is not None:
		return True
	if not re.fullmatch(
		r"[A-Za-z]:[/\\][^\x00-\x1f\x7f<>\"|?*]+\."
		r"(?:png|jpe?g|gif|webp|svg)",
		source,
		re.I,
	):
		return False
	parts = [part for part in re.split(r"[/\\]+", source[3:]) if part]
	return bool(parts) and all(part not in (".", "..") for part in parts)
