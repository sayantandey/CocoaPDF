from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "src"
LICENSE_PATH = ROOT / "LICENSE"
STRATEGIC_PDF = ROOT / "tests" / "strategic_corner_cases_v1_4.pdf"
STRATEGIC_SOURCE = ROOT / "tests" / "strategic_corner_cases_v1_4.md"
PULL_REQUEST_PROFILE = "pull-request"
PERMANENT_PROFILE = "permanent"
VALID_PROFILES = (PULL_REQUEST_PROFILE, PERMANENT_PROFILE)


def _source_imports() -> None:
	if str(SOURCE_ROOT) not in sys.path:
		sys.path.insert(0, str(SOURCE_ROOT))


_source_imports()

from cocoapdf._textio import write_utf8_lf  # noqa: E402
from cocoapdf.synthetic import line_op, text_op  # noqa: E402


def render_pdf(objects: Iterable[bytes], root: int) -> bytes:
	"""Render deterministic indirect objects with a classic xref table."""
	objects = list(objects)
	data = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
	offsets = [0]
	for number, body in enumerate(objects, 1):
		offsets.append(len(data))
		data.extend(("%d 0 obj\n" % number).encode("ascii"))
		data.extend(body)
		data.extend(b"\nendobj\n")
	xref_offset = len(data)
	data.extend(("xref\n0 %d\n" % (len(objects) + 1)).encode("ascii"))
	data.extend(b"0000000000 65535 f \n")
	for offset in offsets[1:]:
		data.extend(("%010d 00000 n \n" % offset).encode("ascii"))
	trailer = (
		"trailer\n<< /Size %d /Root %d 0 R >>\n"
		"startxref\n%d\n%%%%EOF\n"
	) % (len(objects) + 1, root, xref_offset)
	data.extend(trailer.encode("ascii"))
	return bytes(data)


def _stream(data: bytes) -> bytes:
	return b"<< /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


def _marked(mcid: int, operator: bytes) -> bytes:
	return (
		b"/Span <</MCID %d>> BDC\n" % mcid
		+ operator
		+ b"\nEMC"
	)


def build_tagged_semantics_pdf() -> bytes:
	"""Build a tagged heading/list/table fixture with exact MCID ownership."""
	content = b"\n".join(
		[
			_marked(
				0,
				text_op(72, 720, "Tagged Semantics Review", "F1", 18),
			),
			_marked(1, text_op(72, 680, "1.", "F1", 11)),
			_marked(2, text_op(98, 680, "First tagged item", "F1", 11)),
			_marked(3, text_op(72, 655, "2.", "F1", 11)),
			_marked(4, text_op(98, 655, "Second tagged item", "F1", 11)),
			_marked(5, text_op(72, 585, "Evidence", "F2", 11)),
			_marked(6, text_op(300, 585, "Result", "F2", 11)),
			_marked(7, text_op(72, 555, "MCID isolation", "F1", 11)),
			_marked(8, text_op(300, 555, "Pass", "F1", 11)),
		]
	)
	objects = [
		b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
		b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
		_stream(content),
		(
			b"<< /Type /Page /Parent 5 0 R /StructParents 0 "
			b"/MediaBox [0 0 612 792] "
			b"/Resources << /Font << /F1 1 0 R /F2 2 0 R >> >> "
			b"/Contents 3 0 R >>"
		),
		b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
		b"<< /Type /StructElem /S /H2 /P 22 0 R /Pg 4 0 R /K 0 >>",
		(
			b"<< /Type /StructElem /S /L /P 22 0 R /K [8 0 R 9 0 R] "
			b"/A << /O /List /ListNumbering /Decimal >> >>"
		),
		b"<< /Type /StructElem /S /LI /P 7 0 R /K [10 0 R 11 0 R] >>",
		b"<< /Type /StructElem /S /LI /P 7 0 R /K [12 0 R 13 0 R] >>",
		b"<< /Type /StructElem /S /Lbl /P 8 0 R /Pg 4 0 R /K 1 >>",
		b"<< /Type /StructElem /S /LBody /P 8 0 R /Pg 4 0 R /K 2 >>",
		b"<< /Type /StructElem /S /Lbl /P 9 0 R /Pg 4 0 R /K 3 >>",
		b"<< /Type /StructElem /S /LBody /P 9 0 R /Pg 4 0 R /K 4 >>",
		b"<< /Type /StructElem /S /Table /P 22 0 R /K [15 0 R 16 0 R] >>",
		b"<< /Type /StructElem /S /TR /P 14 0 R /K [17 0 R 18 0 R] >>",
		b"<< /Type /StructElem /S /TR /P 14 0 R /K [19 0 R 20 0 R] >>",
		(
			b"<< /Type /StructElem /S /TH /P 15 0 R /Pg 4 0 R /K 5 "
			b"/A << /O /Table /Scope /Column >> >>"
		),
		(
			b"<< /Type /StructElem /S /TH /P 15 0 R /Pg 4 0 R /K 6 "
			b"/A << /O /Table /Scope /Column >> >>"
		),
		b"<< /Type /StructElem /S /TD /P 16 0 R /Pg 4 0 R /K 7 >>",
		b"<< /Type /StructElem /S /TD /P 16 0 R /Pg 4 0 R /K 8 >>",
		(
			b"<< /Nums [0 [6 0 R 10 0 R 11 0 R 12 0 R 13 0 R "
			b"17 0 R 18 0 R 19 0 R 20 0 R]] >>"
		),
		(
			b"<< /Type /StructTreeRoot /K [6 0 R 7 0 R 14 0 R] "
			b"/ParentTree 21 0 R >>"
		),
		(
			b"<< /Type /Catalog /Pages 5 0 R /StructTreeRoot 22 0 R "
			b"/Lang (en-US) /MarkInfo << /Marked true >> >>"
		),
	]
	return render_pdf(objects, 23)


def _outline(x0: float, y0: float, x1: float, y1: float) -> List[bytes]:
	return [
		line_op(x0, y0, x1, y0, 1),
		line_op(x1, y0, x1, y1, 1),
		line_op(x1, y1, x0, y1, 1),
		line_op(x0, y1, x0, y0, 1),
	]


def build_scope_and_adversarial_pdf() -> bytes:
	"""Build page-scope semantics plus a diagram/form near-miss."""
	page_one = b"\n".join(
		[
			text_op(72, 720, "Page Scope Review", "F2", 18),
			text_op(72, 680, "First page body and first field.", "F1", 11),
			text_op(72, 640, "FirstField: Alpha", "F1", 11),
		]
	)
	page_two_parts: List[bytes] = [
		text_op(72, 720, "Selected Evidence Page", "F2", 18),
		text_op(360, 675, "2024", "F2", 9),
		text_op(430, 675, "2023", "F2", 9),
		text_op(500, 675, "2022", "F2", 9),
		text_op(72, 650, "Revenue ........................................", "F1", 10),
		text_op(350, 650, "$ 100", "F1", 10),
		text_op(430, 650, "$ 90", "F1", 10),
		text_op(500, 650, "$ 80", "F1", 10),
		text_op(72, 628, "Expense ........................................", "F1", 10),
		text_op(350, 628, "($ 40)", "F1", 10),
		text_op(430, 628, "($ 35)", "F1", 10),
		text_op(500, 628, "($ 30)", "F1", 10),
		text_op(72, 606, "Net income .....................................", "F1", 10),
		text_op(350, 606, "$ 60", "F1", 10),
		text_op(430, 606, "$ 55", "F1", 10),
		text_op(500, 606, "$ 50", "F1", 10),
		text_op(72, 545, "SecondField: Beta", "F1", 11),
		text_op(72, 510, "Diagram boxes below are not form controls.", "F2", 11),
	]
	for index, (x0, label) in enumerate(
		[
			(45.0, "Parse"),
			(155.0, "Decode"),
			(265.0, "Layout"),
			(375.0, "Reconcile"),
			(485.0, "Render"),
		]
	):
		y0 = 455.0 - (index % 2) * 34.0
		page_two_parts.extend(_outline(x0, y0, x0 + 82.0, y0 + 24.0))
		page_two_parts.append(text_op(x0 + 8.0, y0 + 8.0, label, "F1", 8))
	page_two = b"\n".join(page_two_parts)
	objects = [
		b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
		b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
		_stream(page_one),
		_stream(page_two),
		(
			b"<< /Type /Page /Parent 7 0 R /MediaBox [0 0 612 792] "
			b"/Resources << /Font << /F1 1 0 R /F2 2 0 R >> >> "
			b"/Contents 3 0 R /Annots [10 0 R] >>"
		),
		(
			b"<< /Type /Page /Parent 7 0 R /MediaBox [0 0 612 792] "
			b"/Resources << /Font << /F1 1 0 R /F2 2 0 R >> >> "
			b"/Contents 4 0 R /Annots [11 0 R] >>"
		),
		b"<< /Type /Pages /Kids [5 0 R 6 0 R] /Count 2 >>",
		b"<< /Fields [10 0 R 11 0 R] /NeedAppearances false >>",
		b"<< /Type /Outlines /First 12 0 R /Last 13 0 R /Count 2 >>",
		(
			b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (FirstField) "
			b"/V (Alpha) /Rect [200 630 360 655] /P 5 0 R >>"
		),
		(
			b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (SecondField) "
			b"/V (Beta) /Rect [200 535 360 560] /P 6 0 R >>"
		),
		(
			b"<< /Title (First Page) /Parent 9 0 R /Next 13 0 R "
			b"/Dest [5 0 R /XYZ null null null] >>"
		),
		(
			b"<< /Title (Selected Evidence) /Parent 9 0 R /Prev 12 0 R "
			b"/Dest [6 0 R /XYZ null null null] >>"
		),
		(
			b"<< /Type /Catalog /Pages 7 0 R /AcroForm 8 0 R "
			b"/Outlines 9 0 R >>"
		),
	]
	return render_pdf(objects, 14)


def generate_inputs() -> Dict[str, bytes]:
	inputs = {
		"strategic_corner_cases": STRATEGIC_PDF.read_bytes(),
		"tagged_semantics": build_tagged_semantics_pdf(),
		"scope_and_adversarial": build_scope_and_adversarial_pdf(),
	}
	# Run the pure generators twice. Any hidden clock, randomness, or mutable
	# global state must fail before an artifact is published.
	if inputs["tagged_semantics"] != build_tagged_semantics_pdf():
		raise RuntimeError("tagged_semantics input is not deterministic")
	if inputs["scope_and_adversarial"] != build_scope_and_adversarial_pdf():
		raise RuntimeError("scope_and_adversarial input is not deterministic")
	return inputs


def _sha256(data: bytes) -> str:
	return hashlib.sha256(data).hexdigest()


def _file_hashes(root: Path) -> Dict[str, Dict[str, Any]]:
	files: Dict[str, Dict[str, Any]] = {}
	for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
		data = path.read_bytes()
		files[path.relative_to(root).as_posix()] = {
			"bytes": len(data),
			"sha256": _sha256(data),
		}
	return files


def _conversion_environment() -> Dict[str, str]:
	environment = os.environ.copy()
	existing = environment.get("PYTHONPATH")
	environment["PYTHONPATH"] = (
		str(SOURCE_ROOT)
		if not existing
		else os.pathsep.join([str(SOURCE_ROOT), existing])
	)
	environment["PYTHONHASHSEED"] = "0"
	environment["TZ"] = "UTC"
	return environment


def run_conversion(
	input_path: Path,
	output_dir: Path,
	*,
	pages: Optional[str] = None,
) -> None:
	output_dir.mkdir(parents=True, exist_ok=False)
	command = [
		sys.executable,
		"-m",
		"cocoapdf",
		str(input_path),
		"--format",
		"both",
		"--output",
		str(output_dir / "output.md"),
		"--assets",
		str(output_dir / "assets"),
		"--image-mode",
		"reference",
		"--image-markup",
		"auto",
	]
	if pages:
		command.extend(["--pages", pages])
	recorded_command = [
		"python",
		"-m",
		"cocoapdf",
		"../input.pdf",
		"--format",
		"both",
		"--output",
		"output.md",
		"--assets",
		"assets",
		"--image-mode",
		"reference",
		"--image-markup",
		"auto",
	]
	if pages:
		recorded_command.extend(["--pages", pages])
	completed = subprocess.run(
		command,
		cwd=ROOT,
		env=_conversion_environment(),
		check=False,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		text=True,
		encoding="utf-8",
		errors="replace",
	)
	write_utf8_lf(
		output_dir / "command.json",
		json.dumps(
			{
				"argv": recorded_command,
				"pages": pages,
				"returncode": completed.returncode,
				"stdout": completed.stdout,
				"stderr": completed.stderr,
			},
			indent=2,
		)
		+ "\n",
	)
	if completed.returncode:
		raise RuntimeError(
			"CocoaPDF conversion failed for %s: %s"
			% (input_path.name, completed.stderr.strip())
		)


def _require(condition: bool, detail: str, results: List[Dict[str, Any]]) -> None:
	results.append({"passed": bool(condition), "detail": detail})
	if not condition:
		raise AssertionError(detail)


def _common_contract(output_dir: Path, results: List[Dict[str, Any]]) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
	markdown = (output_dir / "output.md").read_text(encoding="utf-8")
	semantic = json.loads((output_dir / "output.json").read_text(encoding="utf-8"))
	report = json.loads((output_dir / "output.report.json").read_text(encoding="utf-8"))
	_require(bool(markdown.strip()), "Markdown output is non-empty", results)
	_require(bool((output_dir / "output.html").read_text(encoding="utf-8").strip()), "HTML output is non-empty", results)
	_require(semantic.get("schema") == "cocoapdf.semantic-document", "semantic JSON uses the CocoaPDF schema", results)
	_require(report.get("semantic_valid") is True, "semantic graph validates", results)
	_require(report.get("ocr_used") is False, "OCR remains disabled", results)
	return markdown, semantic, report


def verify_strategic(output_dir: Path) -> List[Dict[str, Any]]:
	results: List[Dict[str, Any]] = []
	markdown, _semantic, _report = _common_contract(output_dir, results)
	source = STRATEGIC_SOURCE.read_text(encoding="utf-8")
	sentinels = sorted(set(re.findall(r"SENTINEL-[A-Z0-9-]+", source)))
	missing = [sentinel for sentinel in sentinels if sentinel not in markdown]
	_require(not missing, "all %d strategic sentinels survive" % len(sentinels), results)
	_require("## <u>**" not in markdown, "headings do not acquire redundant inline styles", results)
	_require(r"`inline\_code()`" not in markdown, "code spans are not over-escaped", results)
	_require("- [x] checked task item" in markdown, "task-list state is preserved", results)
	_require("<caption>Complex semantic table" in markdown, "complex table fallback remains structural", results)
	return results


def verify_tagged(output_dir: Path) -> List[Dict[str, Any]]:
	results: List[Dict[str, Any]] = []
	markdown, semantic, _report = _common_contract(output_dir, results)
	_require("## Tagged Semantics Review" in markdown, "tagged heading level is preserved", results)
	_require(markdown.count("First tagged item") == 1, "first tagged item occurs exactly once", results)
	_require(markdown.count("Second tagged item") == 1, "second tagged item occurs exactly once", results)
	_require(
		re.search(r"(?m)^1\. First tagged item$", markdown) is not None,
		"first ordered item is materialized",
		results,
	)
	_require(
		re.search(r"(?m)^2\. Second tagged item$", markdown) is not None,
		"second ordered item is materialized",
		results,
	)
	_require("Evidence" in markdown and "MCID isolation" in markdown, "tagged table cells survive", results)
	metadata = semantic.get("metadata") if isinstance(semantic.get("metadata"), dict) else {}
	_require(bool(metadata.get("tagged_pdf")), "tagged-PDF metadata is recorded", results)
	return results


def verify_scope_selected(output_dir: Path) -> List[Dict[str, Any]]:
	results: List[Dict[str, Any]] = []
	markdown, semantic, report = _common_contract(output_dir, results)
	_require("Selected Evidence Page" in markdown, "selected page body is present", results)
	_require("First page body" not in markdown, "unselected page body is absent", results)
	_require("First Page" not in markdown, "full-document outline is not injected", results)
	_require("FirstField" not in markdown and "Alpha" not in markdown, "unselected form field is absent", results)
	_require("**SecondField:** Beta" in markdown, "selected form field is retained", results)
	_require("cocoapdf-form-appearance" not in markdown, "diagram boxes are not emitted as form controls", results)
	_require('<th scope="col">2024</th>' in markdown, "financial year columns are structural", results)
	_require('<th scope="row">Net income</th>' in markdown, "financial row labels are structural", results)
	_require("................................" not in markdown, "dot leaders are removed from table output", results)
	metadata = semantic.get("metadata") if isinstance(semantic.get("metadata"), dict) else {}
	_require(metadata.get("processed_pages") == [2], "semantic metadata records page 2 only", results)
	warning_codes = {
		str(warning.get("code"))
		for warning in report.get("warnings", [])
		if isinstance(warning, dict)
	}
	_require("FORM_APPEARANCE_CONTROLS" not in warning_codes, "diagram form-control warning is absent", results)
	return results


def _write_assertions(path: Path, assertions: Sequence[Dict[str, Any]]) -> None:
	write_utf8_lf(
		path,
		json.dumps(
			{
				"passed": all(bool(item.get("passed")) for item in assertions),
				"assertions": list(assertions),
			},
			indent=2,
		)
		+ "\n",
	)


def _replace_full_report_with_summary(output_dir: Path) -> None:
	report_path = output_dir / "output.report.json"
	report = json.loads(report_path.read_text(encoding="utf-8"))
	omitted = ("nodes", "regions", "semantic_document", "semantic_nodes")
	summary = {
		key: value
		for key, value in report.items()
		if key not in omitted
	}
	summary["summary_policy"] = {
		"full_semantic_graph": "output.json",
		"omitted_duplicate_or_glyph_heavy_report_fields": list(omitted),
		"full_report_available_in_pull_request_artifacts": True,
	}
	write_utf8_lf(
		output_dir / "output.report.summary.json",
		json.dumps(summary, indent=2, sort_keys=True) + "\n",
	)
	report_path.unlink()


def _write_review_files(
	output_root: Path,
	cases: Sequence[Dict[str, Any]],
	*,
	profile: str,
) -> None:
	permanent = profile == PERMANENT_PROFILE
	title = (
		"CocoaPDF permanent capability demo"
		if permanent
		else "CocoaPDF pull-request visual review"
	)
	index_name = "README.md" if permanent else "REVIEW.md"
	lines = [
		"# %s" % title,
		"",
		"All inputs and fixture prose are first-party project material under the bundled MIT license.",
		"No network content, OCR, AI, or ML was used.",
		"",
	]
	if permanent:
		lines.extend(
			[
				"This directory is the committed, reproducible capability demo. "
				"Pull-request review artifacts are generated separately and are never written here.",
				"",
				"Each row links the source PDF to the exact output committed from the same CocoaPDF revision.",
				"",
				"Full semantic JSON is committed. Report summaries omit only duplicate semantic graphs "
				"and glyph-heavy internals; the temporary PR artifact retains every full report.",
				"",
			]
		)
	lines.extend(
		[
		"| Case | Coverage | Input | Outputs |",
		"| --- | --- | --- | --- |",
		]
	)
	sections: List[str] = []
	for case in cases:
		case_id = str(case["id"])
		description = str(case["description"])
		input_link = "cases/%s/input.pdf" % case_id
		output_links = []
		for conversion in case["conversions"]:
			name = str(conversion["name"])
			base = "cases/%s/%s" % (case_id, name)
			report_name = (
				"output.report.summary.json"
				if permanent
				else "output.report.json"
			)
			output_links.append(
				"[%s Markdown](%s/output.md), [%s HTML](%s/output.html), "
				"[%s semantic JSON](%s/output.json), [%s report](%s/%s)"
				% (
					name,
					base,
					name,
					base,
					name,
					base,
					name,
					base,
					report_name,
				)
			)
		lines.append(
			"| `%s` | %s | [PDF](%s) | %s |"
			% (
				case_id,
				description.replace("|", r"\|"),
				input_link,
				"<br>".join(output_links),
			)
		)
		primary = case["conversions"][0]
		primary_base = "cases/%s/%s" % (case_id, primary["name"])
		sections.append(
			"""
			<section>
			  <h2>{case_id}</h2>
			  <p>{description}</p>
			  <p>
			    <a href="{input_link}">Open input PDF</a> ·
			    <a href="{primary_base}/output.md">Open Markdown</a> ·
			    <a href="{primary_base}/output.html">Open HTML separately</a>
			  </p>
			  <div class="comparison">
			    <object data="{input_link}" type="application/pdf">
			      <a href="{input_link}">Open the input PDF</a>
			    </object>
			    <iframe src="{primary_base}/output.html" title="{case_id} CocoaPDF HTML output"></iframe>
			  </div>
			</section>
			""".format(
				case_id=html.escape(case_id),
				description=html.escape(description),
				input_link=html.escape(input_link, quote=True),
				primary_base=html.escape(primary_base, quote=True),
			)
		)
	write_utf8_lf(output_root / index_name, "\n".join(lines) + "\n")
	review_page = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__COCOAPDF_REVIEW_TITLE__</title>
  <style>
    :root { color-scheme: light dark; font-family: system-ui, sans-serif; }
    body { margin: 2rem auto; max-width: 1500px; padding: 0 1rem; }
    section { border-top: 1px solid #8888; margin-top: 2rem; padding-top: 1rem; }
    .comparison { display: grid; gap: 1rem; grid-template-columns: repeat(2, minmax(0, 1fr)); }
    object, iframe { background: white; border: 1px solid #8888; height: 75vh; width: 100%; }
    @media (max-width: 900px) { .comparison { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <h1>__COCOAPDF_REVIEW_TITLE__</h1>
  <p>Left: source PDF. Right: CocoaPDF HTML. See <a href="__COCOAPDF_INDEX_NAME__">__COCOAPDF_INDEX_NAME__</a>
     and <a href="manifest.json">manifest.json</a> for every output and hash.</p>
__COCOAPDF_REVIEW_SECTIONS__
</body>
</html>
"""
	write_utf8_lf(
		output_root / "review.html",
		review_page
		.replace("__COCOAPDF_REVIEW_TITLE__", html.escape(title))
		.replace("__COCOAPDF_INDEX_NAME__", index_name)
		.replace(
			"__COCOAPDF_REVIEW_SECTIONS__",
			"\n".join(
				"\n".join(line.rstrip() for line in section.splitlines())
				for section in sections
			),
		),
	)


def build_bundle(
	output_root: Path,
	*,
	source_commit: str = "",
	head_commit: str = "",
	profile: str = PULL_REQUEST_PROFILE,
) -> Dict[str, Any]:
	if profile not in VALID_PROFILES:
		raise ValueError("unknown visual bundle profile: %s" % profile)
	output_root = output_root.resolve()
	if output_root.exists() and any(output_root.iterdir()):
		raise ValueError("output directory must be empty: %s" % output_root)
	output_root.mkdir(parents=True, exist_ok=True)
	(output_root / "LICENSE.txt").write_bytes(LICENSE_PATH.read_bytes())

	inputs = generate_inputs()
	case_specs = [
		{
			"id": "strategic_corner_cases",
			"description": "Broad V1-V4 formatting, Unicode, lists, tables, figures, forms, columns, security, and fallback coverage.",
			"input": inputs["strategic_corner_cases"],
			"source": STRATEGIC_SOURCE,
			"provenance": {
				"origin": "first-party CocoaPDF regression fixture",
				"license": "MIT",
				"source": "source.md",
				"generation": "locked project fixture copied byte-for-byte",
			},
			"conversions": [("full", None, verify_strategic)],
		},
		{
			"id": "tagged_semantics",
			"description": "Tagged heading, sibling ordered-list isolation, MCID provenance, and tagged table structure.",
			"input": inputs["tagged_semantics"],
			"source": None,
			"provenance": {
				"origin": "first-party deterministic raw-PDF generator",
				"license": "MIT",
				"generator": "build_tagged_semantics_pdf",
				"font_programs_embedded": False,
				"pdf_standard_fonts": ["Helvetica", "Helvetica-Bold"],
			},
			"conversions": [("full", None, verify_tagged)],
		},
		{
			"id": "scope_and_adversarial",
			"description": "Page-range outline/AcroForm scope, dot-leader finance recovery, and diagram-versus-form false-positive resistance.",
			"input": inputs["scope_and_adversarial"],
			"source": None,
			"provenance": {
				"origin": "first-party deterministic raw-PDF generator",
				"license": "MIT",
				"generator": "build_scope_and_adversarial_pdf",
				"font_programs_embedded": False,
				"pdf_standard_fonts": ["Helvetica", "Helvetica-Bold"],
			},
			"conversions": [
				("full", None, None),
				("page-2", "2", verify_scope_selected),
			],
		},
	]
	manifest_cases: List[Dict[str, Any]] = []
	for spec in case_specs:
		case_dir = output_root / "cases" / str(spec["id"])
		case_dir.mkdir(parents=True, exist_ok=False)
		input_path = case_dir / "input.pdf"
		input_bytes = bytes(spec["input"])
		input_path.write_bytes(input_bytes)
		source_path = spec.get("source")
		if isinstance(source_path, Path):
			shutil.copyfile(source_path, case_dir / "source.md")
		conversions: List[Dict[str, Any]] = []
		for name, pages, verifier in spec["conversions"]:
			conversion_dir = case_dir / str(name)
			run_conversion(input_path, conversion_dir, pages=pages)
			assertions: List[Dict[str, Any]] = []
			if verifier is not None:
				assertions = verifier(conversion_dir)
			else:
				_common_contract(conversion_dir, assertions)
			_write_assertions(conversion_dir / "assertions.json", assertions)
			if profile == PERMANENT_PROFILE:
				_replace_full_report_with_summary(conversion_dir)
			conversions.append(
				{
					"name": name,
					"pages": pages,
					"passed": all(bool(item.get("passed")) for item in assertions),
					"files": _file_hashes(conversion_dir),
				}
			)
		manifest_cases.append(
			{
				"id": spec["id"],
				"description": spec["description"],
				"provenance": spec["provenance"],
				"input": {
					"bytes": len(input_bytes),
					"sha256": _sha256(input_bytes),
					"deterministic": True,
				},
				"conversions": conversions,
			}
		)

	_write_review_files(output_root, manifest_cases, profile=profile)
	permanent = profile == PERMANENT_PROFILE
	manifest = {
		"schema": (
			"cocoapdf.capability-demo/v1"
			if permanent
			else "cocoapdf.pr-visual-corpus/v1"
		),
		"profile": profile,
		"source_commit": source_commit or None,
		"head_commit": head_commit or None,
		"generator": "validation/pr_visual/build.py",
		"generator_sha256": _sha256(Path(__file__).read_bytes()),
		"license": {
			"spdx": "MIT",
			"file": "LICENSE.txt",
			"origin": "first-party CocoaPDF project fixtures",
			"network_fetches": 0,
			"third_party_content_added": False,
		},
		"lifecycle": (
			{
				"committed_outputs": True,
				"location": "examples",
				"refresh_command": "python scripts/refresh_examples.py --write",
				"verification_command": "python scripts/refresh_examples.py --check",
			}
			if permanent
			else {
				"committed_outputs": False,
				"maximum_cases": 3,
				"retention_days": 7,
				"delete_on_pull_request_close": True,
			}
		),
		"cases": manifest_cases,
	}
	write_utf8_lf(
		output_root / "manifest.json",
		json.dumps(manifest, indent=2, sort_keys=True) + "\n",
	)
	return manifest


def main(argv: Optional[Sequence[str]] = None) -> int:
	parser = argparse.ArgumentParser(
		description="Build CocoaPDF's first-party PR visual-validation artifact",
	)
	parser.add_argument("--output-dir", type=Path, required=True)
	parser.add_argument("--source-commit", default="")
	parser.add_argument("--head-commit", default="")
	parser.add_argument(
		"--profile",
		choices=VALID_PROFILES,
		default=PULL_REQUEST_PROFILE,
	)
	args = parser.parse_args(argv)
	manifest = build_bundle(
		args.output_dir,
		source_commit=args.source_commit,
		head_commit=args.head_commit,
		profile=args.profile,
	)
	print(
		json.dumps(
			{
				"schema": manifest["schema"],
				"cases": len(manifest["cases"]),
				"output_dir": str(args.output_dir.resolve()),
			},
			sort_keys=True,
		)
	)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
