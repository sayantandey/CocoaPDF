from __future__ import annotations

import argparse
from dataclasses import dataclass
import difflib
import json
import re
import sys
from pathlib import Path
from typing import List, Optional

from .core import ConvertOptions, Converter, convert_file
from .layout.regions import detect_regions
from .reporting.explain import explain_report
from .reporting.report import build_summary

REPO_ROOT = Path(__file__).resolve().parents[2]

def main(argv: Optional[List[str]] = None) -> int:
	parser = argparse.ArgumentParser(prog="cocoapdf", description="CocoaPDF diagnostic tools")
	sub = parser.add_subparsers(dest="cmd", required=True)
	bench = sub.add_parser("bench", help="Run a benchmark suite")
	bench.add_argument("suite", choices=["v1"])
	diff = sub.add_parser("diff", help="Diff normalized Markdown files")
	diff.add_argument("expected")
	diff.add_argument("actual")
	inspect = sub.add_parser("inspect", help="Inspect PDF structure summary")
	inspect.add_argument("pdf")
	trace = sub.add_parser("trace", help="Trace extracted lines")
	trace.add_argument("pdf")
	trace.add_argument("--page", type=int, default=1)
	overlay = sub.add_parser("overlay", help="Write SVG region overlay")
	overlay.add_argument("pdf")
	overlay.add_argument("--page", type=int, default=1)
	overlay.add_argument("-o", "--output")
	score = sub.add_parser("score", help="Score a fixture set")
	score.add_argument("fixture_set", nargs="?", default="v1")
	explain = sub.add_parser("explain", help="Explain a conversion report")
	explain.add_argument("pdf")
	args = parser.parse_args(argv)

	if args.cmd == "bench":
		return bench_v1()
	if args.cmd == "diff":
		return diff_markdown(Path(args.expected), Path(args.actual))
	if args.cmd == "inspect":
		result = convert_file(args.pdf, ConvertOptions())
		print(json.dumps(build_summary(result.report), indent=2))
		return 0
	if args.cmd == "trace":
		print(trace_pdf(Path(args.pdf), args.page))
		return 0
	if args.cmd == "overlay":
		out = Path(args.output) if args.output else Path("overlay-page-%d.svg" % args.page)
		out.parent.mkdir(parents=True, exist_ok=True)
		out.write_text(region_overlay_svg(Path(args.pdf), args.page), encoding="utf-8")
		print(str(out))
		return 0
	if args.cmd == "score":
		return score_fixture_set(args.fixture_set)
	if args.cmd == "explain":
		result = convert_file(args.pdf, ConvertOptions())
		print("\n".join(explain_report(result.report)))
		return 0
	return 2


@dataclass(frozen=True)
class FixtureCase:
	pdf: Path
	golden: Path
	name: str


def bench_v1() -> int:
	cases = discover_fixture_cases("v1")
	if not cases:
		print("No V1 PDF/golden fixture pairs were found", file=sys.stderr)
		return 2
	failures = []
	regions = 0
	for case in cases:
		result = convert_file(case.pdf, ConvertOptions(image_mode="embed"))
		expected = case.golden.read_text(encoding="utf-8")
		if normalize_md(result.markdown) != normalize_md(expected):
			failures.append(case.name)
		regions += len(result.report.get("regions", []))
	summary = {
		"suite": "v1",
		"fixtures": len(cases),
		"passed": len(cases) - len(failures),
		"failed": len(failures),
		"failures": failures,
		"regions": regions,
	}
	print(json.dumps(summary, indent=2))
	return 0 if not failures else 1


def diff_markdown(expected: Path, actual: Path) -> int:
	left = normalize_md(expected.read_text(encoding="utf-8")).splitlines()
	right = normalize_md(actual.read_text(encoding="utf-8")).splitlines()
	delta = list(difflib.unified_diff(left, right, fromfile=str(expected), tofile=str(actual), lineterm=""))
	if delta:
		print("\n".join(delta))
		return 1
	print("No normalized Markdown differences.")
	return 0


def score_fixture_set(name: str) -> int:
	if name != "v1":
		print(json.dumps({"fixture_set": name, "status": "unknown"}, indent=2))
		return 2
	cases = discover_fixture_cases(name)
	if not cases:
		print(json.dumps({"fixture_set": name, "status": "empty"}, indent=2))
		return 2
	rows = []
	for case in cases:
		actual = convert_file(case.pdf, ConvertOptions(image_mode="embed")).markdown
		expected = case.golden.read_text(encoding="utf-8")
		rows.append({
			"name": case.name,
			"normalized_exact": exact_score(expected, actual),
			"character_accuracy": character_accuracy(expected, actual),
		})
	passed = all(row["normalized_exact"] == 1.0 for row in rows)
	print(json.dumps({"fixture_set": name, "cases": rows, "passed": passed}, indent=2))
	return 0 if passed else 1


def discover_fixture_cases(name: str) -> List[FixtureCase]:
	tests = REPO_ROOT / "tests"
	cases: List[FixtureCase] = []
	seen_pdfs = set()
	# Prefer an explicitly locked golden, while retaining the historical
	# ``_output`` and inspection-oriented ``_temp`` fixture conventions.
	for suffix in ("_golden", "_output", "_temp"):
		for golden in sorted(tests.rglob("*%s.md" % suffix)):
			stem = golden.stem[: -len(suffix)]
			pdf = golden.with_name(stem + ".pdf")
			if not pdf.exists() or pdf in seen_pdfs:
				continue
			if name == "v1" and "v1" not in stem.lower() and "v1" not in str(golden.parent).lower():
				continue
			seen_pdfs.add(pdf)
			cases.append(FixtureCase(pdf=pdf, golden=golden, name=str(golden.relative_to(tests))))
	return cases


def character_accuracy(expected: str, actual: str) -> float:
	left = normalize_md(expected)
	right = normalize_md(actual)
	if not left and not right:
		return 1.0
	matcher = difflib.SequenceMatcher(a=left, b=right, autojunk=False)
	matches = sum(block.size for block in matcher.get_matching_blocks())
	return matches / max(len(left), len(right), 1)


def trace_pdf(path: Path, page: int) -> str:
	conv = Converter(path.read_bytes(), ConvertOptions())
	conv.convert()
	rows = []
	for idx, line in enumerate(conv.lines_by_page.get(page, [])):
		text = "".join(ch.text for ch in line.chars).strip()
		rows.append("%03d x0=%.1f y0=%.1f x1=%.1f y1=%.1f size=%.1f text=%r" % (idx, line.x0, line.y0, line.x1, line.y1, line.size, text))
	return "\n".join(rows)


def region_overlay_svg(path: Path, page: int) -> str:
	conv = Converter(path.read_bytes(), ConvertOptions())
	conv.convert()
	regions = detect_regions(conv.lines_by_page, conv.segments, conv.fills, conv.images, conv.page_sizes)
	width, height = conv.page_sizes.get(page, (612.0, 792.0))
	colors = {"body": "#2f80ed", "column": "#27ae60", "table": "#9b51e0", "figure": "#f2994a", "callout": "#eb5757", "header": "#56ccf2", "footer": "#bdbdbd", "footnote": "#f2c94c"}
	parts = [
		'<svg xmlns="http://www.w3.org/2000/svg" width="%s" height="%s" viewBox="0 0 %s %s">' % (width, height, width, height),
		'<rect x="0" y="0" width="%s" height="%s" fill="white"/>' % (width, height),
	]
	for region in regions:
		if region.page != page:
			continue
		color = colors.get(region.kind, "#333")
		b = region.bbox
		parts.append('<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" fill="none" stroke="%s" stroke-width="2"/>' % (b.x0, b.y0, b.width, b.height, color))
		parts.append('<text x="%.2f" y="%.2f" fill="%s" font-size="10">%s %.2f</text>' % (b.x0, max(10, b.y0 - 3), color, region.kind, region.confidence))
	parts.append("</svg>")
	return "\n".join(parts)


def normalize_md(text: str) -> str:
	text = text.replace("\r\n", "\n")
	text = re.sub(r"[ \t]+$", "", text, flags=re.M)
	text = re.sub(r"\n{3,}", "\n\n", text)
	return text.strip()


def exact_score(expected: str, actual: str) -> float:
	return 1.0 if normalize_md(expected) == normalize_md(actual) else 0.0


if __name__ == "__main__":
	raise SystemExit(main())
