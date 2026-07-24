from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "examples"
if str(ROOT) not in sys.path:
	sys.path.insert(0, str(ROOT))

from validation.pr_visual.build import PERMANENT_PROFILE, build_bundle  # noqa: E402


def _inventory(root: Path) -> Dict[str, Tuple[int, str]]:
	files: Dict[str, Tuple[int, str]] = {}
	if not root.is_dir():
		return files
	for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
		data = path.read_bytes()
		files[path.relative_to(root).as_posix()] = (
			len(data),
			hashlib.sha256(data).hexdigest(),
		)
	return files


def _differences(
	expected: Dict[str, Tuple[int, str]],
	actual: Dict[str, Tuple[int, str]],
) -> Sequence[str]:
	differences = []
	for name in sorted(expected.keys() - actual.keys()):
		differences.append("missing: %s" % name)
	for name in sorted(actual.keys() - expected.keys()):
		differences.append("unexpected: %s" % name)
	for name in sorted(expected.keys() & actual.keys()):
		if expected[name] != actual[name]:
			differences.append("changed: %s" % name)
	return differences


def _replace_examples(generated: Path) -> None:
	root = ROOT.resolve()
	target = TARGET.resolve()
	if target.parent != root or target.name != "examples":
		raise RuntimeError("refusing to replace unexpected path: %s" % target)
	if TARGET.exists():
		shutil.rmtree(TARGET)
	shutil.copytree(generated, TARGET)


def refresh_examples(*, write: bool) -> int:
	with tempfile.TemporaryDirectory(prefix="cocoapdf-examples-") as directory:
		generated = Path(directory) / "examples"
		manifest = build_bundle(generated, profile=PERMANENT_PROFILE)
		expected = _inventory(generated)
		if write:
			_replace_examples(generated)
			print(
				"wrote %d files across %d first-party cases to %s"
				% (len(expected), len(manifest["cases"]), TARGET)
			)
			return 0
		actual = _inventory(TARGET)
		differences = _differences(expected, actual)
		if differences:
			print("committed capability demo is stale:")
			for difference in differences:
				print("- %s" % difference)
			print("run: python scripts/refresh_examples.py --write")
			return 1
		print(
			"verified %d committed files across %d first-party cases"
			% (len(expected), len(manifest["cases"]))
		)
		return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
	parser = argparse.ArgumentParser(
		description="Regenerate or verify CocoaPDF's committed capability demo",
	)
	mode = parser.add_mutually_exclusive_group(required=True)
	mode.add_argument("--check", action="store_true")
	mode.add_argument("--write", action="store_true")
	args = parser.parse_args(argv)
	return refresh_examples(write=bool(args.write))


if __name__ == "__main__":
	raise SystemExit(main())
