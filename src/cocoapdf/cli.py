import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

from ._textio import write_utf8_lf
from ._version import __version__
from .core import ConvertOptions, convert_file
from . import tools

BRAND = "CocoaPDF"
DESCRIPTION = "Deterministic PDF-to-Markdown/HTML conversion for structured text-layer PDFs. No OCR. No AI."


def main(argv=None):
	if hasattr(sys.stdout, "reconfigure"):
		sys.stdout.reconfigure(encoding="utf-8", errors="replace")
	argv = list(sys.argv[1:] if argv is None else argv)
	if argv and argv[0] in {"bench", "diff", "inspect", "trace", "overlay", "score", "explain"}:
		return tools.main(argv)
	parser = argparse.ArgumentParser(
		prog="cocoapdf",
		description="%s: %s" % (BRAND, DESCRIPTION),
		epilog="Structured text-layer PDFs only. No OCR. No AI.",
	)
	parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
	parser.add_argument("pdf", help="Input PDF path")
	parser.add_argument("-o", "--output", help="Output Markdown path")
	parser.add_argument("--assets", default="assets", help="Asset output directory")
	parser.add_argument(
		"--html-underline",
		dest="html_underline",
		action="store_true",
		default=True,
		help="Emit <u> for underline evidence (default)",
	)
	parser.add_argument(
		"--no-html-underline",
		dest="html_underline",
		action="store_false",
		help="Suppress HTML underline fallback",
	)
	parser.add_argument("--page-breaks", action="store_true", help="Emit Markdown page break comments between processed pages")
	parser.add_argument("--pages", help="Page range to process, for example 1,3-5 or 4-")
	parser.add_argument("--image-mode", choices=["reference", "embed"], default="reference", help="Reference extracted image files or embed data URIs")
	parser.add_argument(
		"--image-markup",
		choices=["auto", "markdown", "html"],
		default="auto",
		help=(
			"Use safe size/alignment-preserving HTML automatically, "
			"or force Markdown/HTML image markup"
		),
	)
	parser.add_argument("--report", help="Write JSON conversion report")
	parser.add_argument("--format", choices=["md", "html", "both", "json"], default="md", help="Output format")
	parser.add_argument("--explain", action="store_true", help="Print confidence/provenance explanations after conversion")
	parser.add_argument("--min-confidence", type=float, default=0.0, help="Report low-confidence semantic nodes below this threshold")
	parser.add_argument("--show-low-confidence", action="store_true", help="Print low-confidence semantic nodes")
	args = parser.parse_args(argv)
	asset_reference_dir = _asset_reference_dir(args.output, args.assets, args.format)

	options = ConvertOptions(
		assets_dir=args.assets,
		asset_reference_dir=asset_reference_dir,
		html_underline=args.html_underline,
		report_path=args.report,
		output_format=args.format,
		page_breaks=args.page_breaks,
		pages=args.pages,
		image_mode=args.image_mode,
		image_markup=args.image_markup,
	)
	try:
		result = convert_file(args.pdf, options)
	except ValueError as exc:
		parser.error(str(exc))
	payload = _format_payload(result, args.format)
	if args.output:
		_write_output(Path(args.output), result, args.format)
	else:
		sys.stdout.write(payload)
	if args.explain:
		from .reporting.explain import explain_report

		sys.stderr.write("\n".join(explain_report(result.report)) + "\n")
	if args.show_low_confidence or args.min_confidence > 0:
		threshold = args.min_confidence or 0.80
		low = [
			node
			for node in result.report.get("nodes", [])
			if isinstance(node, dict) and float(node.get("confidence", 1.0)) < threshold
		]
		if low:
			sys.stderr.write(json.dumps({"low_confidence": low}, indent=2) + "\n")
	return 0


def _asset_reference_dir(output: Optional[str], assets: str, output_format: str) -> str:
	"""Return the asset URL prefix relative to the emitted document.

	The filesystem destination and the reference embedded in Markdown/HTML are
	different concerns. Preserve the caller's spelling for stdout, where there
	is no document path, and otherwise relativize the asset directory against
	the directory that will contain the emitted document.
	"""
	if not output:
		return str(assets).replace("\\", "/").rstrip("/")
	output_path = Path(output)
	document_dir = output_path if output_format == "both" and not output_path.suffix else output_path.parent
	try:
		reference = os.path.relpath(Path(assets).resolve(), document_dir.resolve())
	except ValueError:
		# Windows paths on different drives cannot be relativized.
		reference = str(Path(assets).resolve())
	return reference.replace("\\", "/").rstrip("/")


def _format_payload(result, fmt: str) -> str:
	if fmt == "html":
		return result.html
	if fmt == "json":
		return json.dumps({
			"semantic_document": result.semantic.to_dict() if result.semantic is not None else None,
			"report": result.report,
			"markdown": result.markdown,
			"html": result.html,
		}, indent=2) + "\n"
	if fmt == "both":
		return result.markdown + "\n\n<!-- CocoaPDF HTML output follows -->\n\n" + result.html
	return result.markdown


def _write_output(path: Path, result, fmt: str) -> None:
	if fmt == "both":
		if path.suffix:
			path.parent.mkdir(parents=True, exist_ok=True)
			base = path.with_suffix("")
			write_utf8_lf(base.with_suffix(".md"), result.markdown)
			write_utf8_lf(base.with_suffix(".html"), result.html)
			write_utf8_lf(
				base.with_suffix(".json"),
				json.dumps(result.semantic.to_dict() if result.semantic is not None else {}, indent=2),
			)
			write_utf8_lf(base.with_suffix(".report.json"), json.dumps(result.report, indent=2))
		else:
			path.mkdir(parents=True, exist_ok=True)
			write_utf8_lf(path / "document.md", result.markdown)
			write_utf8_lf(path / "document.html", result.html)
			write_utf8_lf(
				path / "document.json",
				json.dumps(result.semantic.to_dict() if result.semantic is not None else {}, indent=2),
			)
			write_utf8_lf(path / "report.json", json.dumps(result.report, indent=2))
		return
	path.parent.mkdir(parents=True, exist_ok=True)
	write_utf8_lf(path, _format_payload(result, fmt))


if __name__ == "__main__":
	raise SystemExit(main())
