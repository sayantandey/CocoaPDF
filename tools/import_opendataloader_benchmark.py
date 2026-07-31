#!/usr/bin/env python3
"""Validate and import one pinned OpenDataLoader-Bench result snapshot."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_REPOSITORY = "https://github.com/opendataloader-project/opendataloader-bench"
ENGINE_REPOSITORY = "https://github.com/sayantandey/CocoaPDF"
EXPECTED_BENCHMARK_COMMIT = "7af1d8f4d0c09f51ea1a5c6ba5f66e993286d109"
EXPECTED_ENGINE_COMMIT = "937c403ed3b265a14db802b2ced36b3819d20b0f"
EXPECTED_ENGINE_TREE = "f01a809b4e4c48d17716be2ac510e67b66b6ff28"
EXPECTED_COUNTS = {
	"document_count": 200,
	"nid_count": 200,
	"teds_count": 42,
	"mhs_count": 107,
}
RESULT_RELATIVE_ROOT = (
	Path("validation")
	/ "benchmarks"
	/ "opendataloader_bench"
	/ "results"
	/ EXPECTED_BENCHMARK_COMMIT
)
SCORE_NAMES = (
	"overall_mean",
	"nid_mean",
	"nid_s_mean",
	"teds_mean",
	"teds_s_mean",
	"mhs_mean",
	"mhs_s_mean",
)
DOCUMENT_SCORE_NAMES = ("overall", "nid", "nid_s", "teds", "teds_s", "mhs", "mhs_s")


def _read_json(path: Path) -> Any:
	return json.loads(path.read_text(encoding="utf-8"))


def _copy_as_lf(source: Path, destination: Path) -> None:
	"""Copy a text artifact, normalizing CRLF so the archive stays LF-only."""
	destination.write_bytes(source.read_bytes().replace(b"\r\n", b"\n"))


def _write_json(path: Path, value: Any) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	with path.open("w", encoding="utf-8", newline="\n") as stream:
		json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False)
		stream.write("\n")


def _sha256_bytes(data: bytes) -> str:
	return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
	return _sha256_bytes(path.read_bytes())


def _git(checkout: Path, *args: str) -> str:
	completed = subprocess.run(
		["git", "-C", str(checkout), *args],
		check=True,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		text=True,
		encoding="utf-8",
		errors="replace",
	)
	return completed.stdout.strip()


def _command_version(*args: str) -> str:
	completed = subprocess.run(
		list(args),
		check=True,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		text=True,
		encoding="utf-8",
		errors="replace",
	)
	return completed.stdout.strip()


def _inventory(paths: Iterable[Path]) -> Tuple[Dict[str, Dict[str, Any]], int, str]:
	records: Dict[str, Dict[str, Any]] = {}
	total_bytes = 0
	aggregate = hashlib.sha256()
	for path in sorted(paths, key=lambda item: item.name):
		data = path.read_bytes()
		record = {"bytes": len(data), "sha256": _sha256_bytes(data)}
		records[path.stem] = record
		total_bytes += len(data)
		aggregate.update(path.stem.encode("ascii"))
		aggregate.update(b"\0")
		aggregate.update(str(len(data)).encode("ascii"))
		aggregate.update(b"\0")
		aggregate.update(record["sha256"].encode("ascii"))
		aggregate.update(b"\n")
	return records, total_bytes, aggregate.hexdigest()


def _require(condition: bool, message: str) -> None:
	if not condition:
		raise RuntimeError(message)


def _require_close(actual: float, expected: float, label: str) -> None:
	if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-15):
		raise RuntimeError("%s mismatch: %r != %r" % (label, actual, expected))


def _validate_scores(evaluation: Mapping[str, Any]) -> None:
	documents = evaluation.get("documents")
	_require(isinstance(documents, list), "evaluation.documents must be a list")
	_require(len(documents) == EXPECTED_COUNTS["document_count"], "evaluation must contain 200 documents")

	for document in documents:
		scores = document.get("scores")
		_require(isinstance(scores, dict), "each document must contain scores")
		for name in DOCUMENT_SCORE_NAMES:
			value = scores.get(name)
			if value is None:
				continue
			_require(
				isinstance(value, (int, float)) and math.isfinite(float(value)),
				"%s must be finite" % name,
			)
			_require(0.0 <= float(value) <= 1.0, "%s must be in [0, 1]" % name)
		available = [
			float(scores[name])
			for name in ("nid", "teds", "mhs")
			if scores.get(name) is not None
		]
		_require(bool(available), "every evaluated document must have an available score")
		_require_close(
			float(scores["overall"]),
			sum(available) / len(available),
			"document overall",
		)

	metrics = evaluation.get("metrics")
	_require(isinstance(metrics, dict), "evaluation.metrics must be an object")
	score_means = metrics.get("score")
	_require(isinstance(score_means, dict), "evaluation.metrics.score must be an object")
	for name in SCORE_NAMES:
		value = score_means.get(name)
		_require(
			isinstance(value, (int, float)) and math.isfinite(float(value)),
			"%s must be finite" % name,
		)
		_require(0.0 <= float(value) <= 1.0, "%s must be in [0, 1]" % name)

	series = {
		"overall_mean": [float(item["scores"]["overall"]) for item in documents],
		"nid_mean": [float(item["scores"]["nid"]) for item in documents if item["scores"]["nid"] is not None],
		"nid_s_mean": [float(item["scores"]["nid_s"]) for item in documents if item["scores"]["nid_s"] is not None],
		"teds_mean": [float(item["scores"]["teds"]) for item in documents if item["scores"]["teds"] is not None],
		"teds_s_mean": [float(item["scores"]["teds_s"]) for item in documents if item["scores"]["teds_s"] is not None],
		"mhs_mean": [float(item["scores"]["mhs"]) for item in documents if item["scores"]["mhs"] is not None],
		"mhs_s_mean": [float(item["scores"]["mhs_s"]) for item in documents if item["scores"]["mhs_s"] is not None],
	}
	for name, values in series.items():
		_require_close(
			float(score_means[name]),
			sum(values) / len(values),
			"aggregate %s" % name,
		)


def _artifact_record(path: Path) -> Dict[str, Any]:
	return {"bytes": path.stat().st_size, "sha256": _sha256_file(path)}


def import_result(
	benchmark_dir: Path,
	*,
	destination: Path,
	determinism_run: Path,
	physical_memory_bytes: int,
) -> Dict[str, Any]:
	benchmark_dir = benchmark_dir.resolve()
	determinism_run = determinism_run.resolve()
	prediction_root = benchmark_dir / "prediction" / "cocoapdf"
	markdown_root = prediction_root / "markdown"
	evaluation_path = prediction_root / "evaluation.json"
	evaluation_csv_path = prediction_root / "evaluation.csv"
	summary_path = prediction_root / "summary.json"
	failures_path = prediction_root / "failures.json"
	adapter_path = benchmark_dir / "src" / "pdf_parser_cocoapdf.py"
	canonical_adapter_path = (
		ROOT / "validation" / "benchmarks" / "opendataloader_bench" / "adapter.py"
	)

	benchmark_commit = _git(benchmark_dir, "rev-parse", "HEAD")
	benchmark_tree = _git(benchmark_dir, "show", "-s", "--format=%T", benchmark_commit)
	engine_tree = _git(ROOT, "show", "-s", "--format=%T", EXPECTED_ENGINE_COMMIT)
	_require(benchmark_commit == EXPECTED_BENCHMARK_COMMIT, "unexpected benchmark commit")
	_require(engine_tree == EXPECTED_ENGINE_TREE, "unexpected CocoaPDF engine tree")
	_require(evaluation_path.is_file(), "missing evaluation.json")
	_require(evaluation_csv_path.is_file(), "missing evaluation.csv")
	_require(summary_path.is_file(), "missing summary.json")
	_require(adapter_path.is_file(), "missing CocoaPDF adapter")
	_require(canonical_adapter_path.is_file(), "missing canonical CocoaPDF adapter")
	_require(
		adapter_path.read_bytes() == canonical_adapter_path.read_bytes(),
		"benchmark adapter differs from the canonical CocoaPDF adapter",
	)
	_require(physical_memory_bytes > 0, "physical_memory_bytes must be positive")

	pdfs = sorted((benchmark_dir / "pdfs").glob("*.pdf"))
	ground_truth = sorted((benchmark_dir / "ground-truth" / "markdown").glob("*.md"))
	predictions = sorted(markdown_root.glob("*.md"))
	_require(len(pdfs) == EXPECTED_COUNTS["document_count"], "expected 200 PDFs")
	_require(len(ground_truth) == EXPECTED_COUNTS["document_count"], "expected 200 ground truths")
	_require(len(predictions) == EXPECTED_COUNTS["document_count"], "expected 200 predictions")
	for pdf in pdfs:
		_require(pdf.read_bytes()[:5] == b"%PDF-", "invalid PDF or LFS pointer: %s" % pdf.name)

	pdf_inventory, pdf_bytes, pdf_digest = _inventory(pdfs)
	gt_inventory, gt_bytes, gt_digest = _inventory(ground_truth)
	prediction_inventory, prediction_bytes, prediction_digest = _inventory(predictions)
	pdf_ids = set(pdf_inventory)
	gt_ids = set(gt_inventory)
	prediction_ids = set(prediction_inventory)
	_require(pdf_ids == gt_ids == prediction_ids, "PDF, ground-truth, and prediction IDs differ")

	first_markdown_root = determinism_run / "markdown"
	first_evaluation_path = determinism_run / "evaluation.json"
	first_summary_path = determinism_run / "summary.json"
	first_predictions = sorted(first_markdown_root.glob("*.md"))
	_require(first_evaluation_path.is_file(), "first run is missing evaluation.json")
	_require(first_summary_path.is_file(), "first run is missing summary.json")
	_require(len(first_predictions) == EXPECTED_COUNTS["document_count"], "first run must contain 200 predictions")
	first_inventory, first_prediction_bytes, first_prediction_digest = _inventory(first_predictions)
	_require(first_inventory == prediction_inventory, "the two full runs produced different Markdown bytes")

	evaluation = _read_json(evaluation_path)
	summary = _read_json(summary_path)
	first_evaluation = _read_json(first_evaluation_path)
	first_summary = _read_json(first_summary_path)
	_validate_scores(evaluation)
	_validate_scores(first_evaluation)
	_require(
		first_evaluation.get("metrics") == evaluation.get("metrics")
		and first_evaluation.get("documents") == evaluation.get("documents"),
		"the two full runs produced different evaluation scores",
	)
	documents = evaluation["documents"]
	document_ids = [str(item["document_id"]) for item in documents]
	_require(len(document_ids) == len(set(document_ids)), "duplicate evaluation document IDs")
	_require(set(document_ids) == prediction_ids, "evaluation IDs do not match predictions")
	_require(all(bool(item.get("prediction_available")) for item in documents), "missing prediction")

	metrics = evaluation["metrics"]
	for name in ("nid_count", "teds_count", "mhs_count"):
		_require(metrics.get(name) == EXPECTED_COUNTS[name], "unexpected %s" % name)
	_require(metrics.get("missing_predictions") == 0, "missing_predictions must be zero")
	_require(summary.get("engine_name") == "cocoapdf", "unexpected engine name")
	_require(summary.get("engine_version") == "0.1.0", "unexpected engine version")
	_require(summary.get("document_count") == 200, "summary document_count must be 200")
	_require(first_summary.get("engine_name") == "cocoapdf", "unexpected first-run engine name")
	_require(first_summary.get("engine_version") == "0.1.0", "unexpected first-run engine version")
	_require(first_summary.get("document_count") == 200, "first-run summary document_count must be 200")

	speed = evaluation.get("speed")
	_require(isinstance(speed, dict), "full evaluation must contain speed")
	for name in ("total_elapsed", "elapsed_per_doc"):
		value = speed.get(name)
		_require(isinstance(value, (int, float)) and float(value) > 0.0, "invalid speed.%s" % name)
		_require_close(float(value), float(summary[name]), "summary/evaluation %s" % name)
	_require(speed.get("document_count") == 200, "speed document_count must be 200")
	_require_close(
		float(speed["elapsed_per_doc"]),
		float(speed["total_elapsed"]) / 200.0,
		"elapsed_per_doc",
	)
	first_speed = first_evaluation.get("speed")
	_require(isinstance(first_speed, dict), "first full evaluation must contain speed")
	for name in ("total_elapsed", "elapsed_per_doc"):
		value = first_speed.get(name)
		_require(isinstance(value, (int, float)) and float(value) > 0.0, "invalid first speed.%s" % name)
		_require_close(float(value), float(first_summary[name]), "first summary/evaluation %s" % name)
	_require(first_speed.get("document_count") == 200, "first speed document_count must be 200")
	_require_close(
		float(first_speed["elapsed_per_doc"]),
		float(first_speed["total_elapsed"]) / 200.0,
		"first elapsed_per_doc",
	)

	with evaluation_csv_path.open("r", encoding="utf-8", newline="") as stream:
		csv_rows = list(csv.DictReader(stream))
	_require(len(csv_rows) == 200, "evaluation.csv must contain 200 data rows")
	_require(
		{str(row["document_id"]).removeprefix("'") for row in csv_rows} == prediction_ids,
		"CSV IDs differ",
	)
	archive_date = datetime.strptime(str(summary["date"]), "%Y-%m-%d").strftime("%y%m%d")
	archive_root = benchmark_dir / "history" / archive_date / "cocoapdf"
	archived_evaluation = archive_root / "evaluation.json"
	archived_csv = archive_root / "evaluation.csv"
	_require(archived_evaluation.is_file(), "missing archived evaluation.json")
	_require(archived_csv.is_file(), "missing archived evaluation.csv")
	_require(
		_sha256_file(archived_evaluation) == _sha256_file(evaluation_path),
		"archived evaluation.json differs from the final result",
	)
	_require(
		_sha256_file(archived_csv) == _sha256_file(evaluation_csv_path),
		"archived evaluation.csv differs from the final result",
	)

	failures_source_present = failures_path.is_file()
	failures = _read_json(failures_path) if failures_source_present else []
	_require(isinstance(failures, list), "failures.json must contain a list")
	empty_predictions = [
		path.stem
		for path in predictions
		if not path.read_text(encoding="utf-8").strip()
	]

	lock_path = benchmark_dir / "uv.lock"
	lock_text = lock_path.read_text(encoding="utf-8")
	_require(EXPECTED_ENGINE_COMMIT in lock_text, "uv.lock does not pin the benchmarked engine commit")
	runtime_python = benchmark_dir / ".venv" / (
		Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
	)
	_require(runtime_python.is_file(), "missing locked benchmark Python runtime")
	runtime_identity = _command_version(
		str(runtime_python),
		"-c",
		"import platform; print(platform.python_version()); print(platform.python_implementation())",
	).splitlines()
	_require(len(runtime_identity) == 2, "unexpected benchmark Python identity")

	completed_at = datetime.fromtimestamp(evaluation_path.stat().st_mtime).astimezone().isoformat()
	patch = _git(
		benchmark_dir,
		"diff",
		"--no-ext-diff",
		"--unified=0",
		"--",
		"THIRD_PARTY_NOTICES.md",
		"pyproject.toml",
		"src/engine_registry.py",
		"uv.lock",
	)
	result = {
		"schema": "cocoapdf.opendataloader-bench-result/v1",
		"benchmark_commit": benchmark_commit,
		"engine_commit": EXPECTED_ENGINE_COMMIT,
		"metrics": metrics,
		"speed": speed,
		"completeness": {
			"evaluated_documents": len(documents),
			"prediction_files": len(predictions),
			"missing_predictions": metrics["missing_predictions"],
			"empty_predictions": len(empty_predictions),
			"conversion_failures": len(failures),
		},
	}
	prediction_hashes = {
		"schema": "cocoapdf.opendataloader-prediction-hashes/v1",
		"document_count": len(predictions),
		"total_bytes": prediction_bytes,
		"aggregate_sha256": prediction_digest,
		"documents": prediction_inventory,
	}
	determinism = {
		"schema": "cocoapdf.opendataloader-determinism/v1",
		"document_count": len(predictions),
		"prediction_mismatches": 0,
		"score_mismatches": 0,
		"runs": [
			{
				"label": "run-1",
				"prediction_bytes": first_prediction_bytes,
				"prediction_sha256": first_prediction_digest,
				"speed": first_speed,
			},
			{
				"label": "run-2",
				"prediction_bytes": prediction_bytes,
				"prediction_sha256": prediction_digest,
				"speed": speed,
			},
		],
	}

	with tempfile.TemporaryDirectory(prefix="cocoapdf-benchmark-import-") as directory:
		staged = Path(directory) / EXPECTED_BENCHMARK_COMMIT
		staged.mkdir(parents=True)
		# The evaluator writes these on the benchmark host, so on Windows they
		# arrive with CRLF. The archive is committed text governed by
		# .gitattributes `eol=lf`, and its recorded SHA-256 must describe the
		# bytes that are actually stored, so normalize before hashing.
		_copy_as_lf(evaluation_path, staged / "evaluation.json")
		_copy_as_lf(evaluation_csv_path, staged / "evaluation.csv")
		_copy_as_lf(summary_path, staged / "summary.json")
		_copy_as_lf(adapter_path, staged / "adapter.py")
		_write_json(staged / "failures.json", failures)
		_write_json(staged / "determinism.json", determinism)
		_write_json(staged / "prediction-hashes.json", prediction_hashes)
		_write_json(staged / "result.json", result)
		with (staged / "integration.patch").open("w", encoding="utf-8", newline="\n") as stream:
			stream.write(patch)
			if patch and not patch.endswith("\n"):
				stream.write("\n")

		artifact_names = (
			"adapter.py",
			"determinism.json",
			"evaluation.csv",
			"evaluation.json",
			"failures.json",
			"integration.patch",
			"prediction-hashes.json",
			"result.json",
			"summary.json",
		)
		provenance = {
			"schema": "cocoapdf.opendataloader-bench-provenance/v1",
			"engine": {
				"name": "CocoaPDF",
				"version": "0.1.0",
				"repository": ENGINE_REPOSITORY,
				"commit": EXPECTED_ENGINE_COMMIT,
				"tree": engine_tree,
				"license": "MIT",
			},
			"benchmark": {
				"name": "OpenDataLoader-Bench",
				"repository": BENCHMARK_REPOSITORY,
				"commit": benchmark_commit,
				"tree": benchmark_tree,
				"license": "Apache-2.0",
				"corpus": {
					"name": "DP-Bench",
					"license": "MIT",
					"pdf_count": len(pdfs),
					"pdf_bytes": pdf_bytes,
					"pdf_inventory_sha256": pdf_digest,
					"ground_truth_count": len(ground_truth),
					"ground_truth_bytes": gt_bytes,
					"ground_truth_inventory_sha256": gt_digest,
					"redistributed": False,
				},
			},
			"adapter": {
				"interface": "to_markdown(doc_paths, input_path, output_dir)",
				"options": 'ConvertOptions(heading_level_mode="flat")',
				"failure_policy": "write an empty Markdown prediction and record the exception",
				"sha256": _sha256_file(staged / "adapter.py"),
			},
			"run": {
				"completed_at": completed_at,
				"date": summary["date"],
				"full_command": (
					"uv run --locked --offline --extra cocoapdf --no-sync python "
					"src/run.py --engine cocoapdf --force --history-date 260801 "
					"--history-overwrite"
				),
				"full_run_count": 2,
				"determinism_artifact": "determinism.json",
				"failures_source_file_present": failures_source_present,
				"empty_prediction_ids": empty_predictions,
			},
			"environment": {
				"python": runtime_identity[0],
				"python_implementation": runtime_identity[1],
				"provenance_importer_python": platform.python_version(),
				"uv": _command_version("uv", "--version"),
				"os": platform.system(),
				"os_release": platform.release(),
				"os_version": platform.version(),
				"machine": platform.machine(),
				"platform": platform.platform(),
				"logical_cpu_count": os.cpu_count(),
				"physical_memory_bytes": physical_memory_bytes,
				"processor": summary["processor"],
			},
			"dependency_lock": {
				"path": "uv.lock",
				"sha256": _sha256_file(lock_path),
				"pinned_engine_commit": EXPECTED_ENGINE_COMMIT,
			},
			"verification": {
				"expected_counts": EXPECTED_COUNTS,
				"score_ranges_valid": True,
				"aggregate_scores_recomputed": True,
				"two_full_runs_compared": True,
				"prediction_ids_match_corpus": True,
				"pdf_headers_valid": True,
				"archive_checked": True,
			},
			"limitations": [
				"Accuracy metrics evaluate Markdown only and do not measure HTML fidelity.",
				"NID is a whitespace-normalized Markdown text/order proxy.",
				"TEDS concatenates every extracted table into one synthetic comparison per eligible document.",
				"MHS flattens heading levels and measures heading boundaries/text rather than true depth.",
				"Timings are two hardware-bound wall-clock runs and include CocoaPDF semantic HTML generation.",
				"The upstream chart relabels seconds/document as seconds/page and hardcodes Apple M4 hardware; it is not redistributed.",
			],
			"artifacts": {
				name: _artifact_record(staged / name)
				for name in artifact_names
			},
		}
		_write_json(staged / "provenance.json", provenance)

		if destination.exists():
			raise RuntimeError("destination already exists: %s" % destination)
		destination.parent.mkdir(parents=True, exist_ok=True)
		shutil.copytree(staged, destination)
	return provenance


def main(argv: Optional[Sequence[str]] = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--benchmark-dir", type=Path, required=True)
	parser.add_argument(
		"--destination",
		type=Path,
		default=ROOT / RESULT_RELATIVE_ROOT,
	)
	parser.add_argument(
		"--determinism-run",
		type=Path,
		required=True,
		help="preserved prediction/cocoapdf directory from the first full run",
	)
	parser.add_argument("--physical-memory-bytes", type=int, required=True)
	args = parser.parse_args(argv)
	provenance = import_result(
		args.benchmark_dir,
		destination=args.destination.resolve(),
		determinism_run=args.determinism_run,
		physical_memory_bytes=args.physical_memory_bytes,
	)
	print(
		"imported OpenDataLoader-Bench %s for CocoaPDF %s"
		% (
			provenance["benchmark"]["commit"],
			provenance["engine"]["commit"],
		)
	)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
