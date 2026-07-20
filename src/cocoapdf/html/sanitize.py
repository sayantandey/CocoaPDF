from __future__ import annotations

import html
import re
from typing import Optional
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
