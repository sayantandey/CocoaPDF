from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RETIRED_TOKEN = "pdf" + "2md"
TEXT_SUFFIXES = {
	".py",
	".md",
	".txt",
	".toml",
	".json",
	".yml",
	".yaml",
	".ini",
	".cfg",
	".rst",
	".ps1",
	".sh",
}
SKIP_PARTS = {
	".git",
	".mypy_cache",
	".pytest_cache",
	".venv",
	"__pycache__",
	"build",
	"dist",
}
FIXTURE_TEXT_EXCEPTIONS = {
	Path("tests/strategic_corner_cases_v1_4.md"),
	Path("tests/strategic_corner_cases_v1_4_temp.md"),
}
FIXTURE_TREE_EXCEPTIONS = {
	Path("examples/cases/strategic_corner_cases"),
}


def main() -> int:
	failures: list[str] = []

	for path in ROOT.rglob("*"):
		if not path.is_file():
			continue
		if any(part in SKIP_PARTS for part in path.parts):
			continue
		if path.suffix.lower() not in TEXT_SUFFIXES:
			continue
		relative = path.relative_to(ROOT)
		if (
			relative in FIXTURE_TEXT_EXCEPTIONS
			or any(
				relative.parts[:len(prefix.parts)] == prefix.parts
				for prefix in FIXTURE_TREE_EXCEPTIONS
			)
		):
			# These strings are source-document content. The converter must
			# preserve them until the corresponding PDFs and examples are
			# regenerated.
			continue
		try:
			text = path.read_text(encoding="utf-8")
		except UnicodeDecodeError:
			continue
		for number, line in enumerate(text.splitlines(), 1):
			if RETIRED_TOKEN in line.casefold():
				failures.append(
					"%s:%d: retired package token remains"
					% (path.relative_to(ROOT), number)
				)

	for retired_path in (
		ROOT / ("run_" + RETIRED_TOKEN + ".py"),
		ROOT / "src" / RETIRED_TOKEN,
		ROOT / "src" / "cocoapdf" / "hardening.py",
	):
		if retired_path.exists():
			failures.append("%s must not exist" % retired_path.relative_to(ROOT))

	if failures:
		print("\n".join(failures), file=sys.stderr)
		return 1
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
