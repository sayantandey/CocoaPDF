"""Deterministic UTF-8 text output helpers."""

from pathlib import Path
from typing import Union


PathLike = Union[str, Path]


def canonical_newlines(text: str) -> str:
	"""Return text with platform-independent LF line endings."""
	return text.replace("\r\n", "\n").replace("\r", "\n")


def write_utf8_lf(path: PathLike, text: str) -> int:
	"""Write exact UTF-8 bytes with LF endings on every operating system."""
	return Path(path).write_bytes(canonical_newlines(text).encode("utf-8"))
