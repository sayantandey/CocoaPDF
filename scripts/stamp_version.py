from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Optional, Sequence


VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def _replace_once(path: Path, pattern: str, replacement: str) -> None:
	text = path.read_text(encoding="utf-8")
	updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
	if count != 1:
		raise ValueError("expected one version declaration in %s" % path)
	with path.open("w", encoding="utf-8", newline="\n") as stream:
		stream.write(updated)


def stamp(root: Path, version: str) -> None:
	if not VERSION_RE.fullmatch(version):
		raise ValueError("version must be unpadded MAJOR.MINOR.PATCH")
	_replace_once(
		root / "src" / "cocoapdf" / "_version.py",
		r'^__version__ = "[^"]+"$',
		'__version__ = "%s"' % version,
	)
	_replace_once(
		root / "pyproject.toml",
		r'^version = "[^"]+"$',
		'version = "%s"' % version,
	)


def main(argv: Optional[Sequence[str]] = None) -> int:
	parser = argparse.ArgumentParser(description="Stamp a release version into build inputs")
	parser.add_argument("version")
	args = parser.parse_args(argv)
	stamp(Path(__file__).resolve().parents[1], args.version)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
