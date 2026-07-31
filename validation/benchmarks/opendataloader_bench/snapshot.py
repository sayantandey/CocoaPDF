"""Validation helpers for the committed OpenDataLoader-Bench snapshot."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional


BENCHMARK_COMMIT = "7af1d8f4d0c09f51ea1a5c6ba5f66e993286d109"
ENGINE_COMMIT = "97527da3bdf8bd247cf19781a0599c9176e54a33"
SNAPSHOT_ROOT = Path(__file__).resolve().parent / "results" / BENCHMARK_COMMIT
EXPECTED_SCORES = {
	"overall_mean": 0.8435980876181824,
	"nid_mean": 0.8908832817585036,
	"nid_s_mean": 0.8704161814503709,
	"teds_mean": 0.5636823455148973,
	"teds_s_mean": 0.5759866978044572,
	"mhs_mean": 0.7912284341287635,
	"mhs_s_mean": 0.8810292785828795,
}
EXPECTED_COUNTS = {
	"nid_count": 200,
	"teds_count": 42,
	"mhs_count": 107,
	"missing_predictions": 0,
}
DOCUMENT_SCORE_NAMES = ("overall", "nid", "nid_s", "teds", "teds_s", "mhs", "mhs_s")


def _read_json(path: Path) -> Any:
	return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
	return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_close(actual: float, expected: float, label: str) -> None:
	if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-15):
		raise ValueError("%s mismatch: %r != %r" % (label, actual, expected))


def _score_series(documents: List[Dict[str, Any]]) -> Dict[str, List[float]]:
	return {
		"overall_mean": [float(item["scores"]["overall"]) for item in documents],
		"nid_mean": [
			float(item["scores"]["nid"])
			for item in documents
			if item["scores"]["nid"] is not None
		],
		"nid_s_mean": [
			float(item["scores"]["nid_s"])
			for item in documents
			if item["scores"]["nid_s"] is not None
		],
		"teds_mean": [
			float(item["scores"]["teds"])
			for item in documents
			if item["scores"]["teds"] is not None
		],
		"teds_s_mean": [
			float(item["scores"]["teds_s"])
			for item in documents
			if item["scores"]["teds_s"] is not None
		],
		"mhs_mean": [
			float(item["scores"]["mhs"])
			for item in documents
			if item["scores"]["mhs"] is not None
		],
		"mhs_s_mean": [
			float(item["scores"]["mhs_s"])
			for item in documents
			if item["scores"]["mhs_s"] is not None
		],
	}


def validate_snapshot(root: Optional[Path] = None) -> Dict[str, Any]:
	snapshot = (root or SNAPSHOT_ROOT).resolve()
	provenance = _read_json(snapshot / "provenance.json")
	result = _read_json(snapshot / "result.json")
	evaluation = _read_json(snapshot / "evaluation.json")
	summary = _read_json(snapshot / "summary.json")
	failures = _read_json(snapshot / "failures.json")
	predictions = _read_json(snapshot / "prediction-hashes.json")

	if provenance["benchmark"]["commit"] != BENCHMARK_COMMIT:
		raise ValueError("unexpected benchmark commit")
	if provenance["engine"]["commit"] != ENGINE_COMMIT:
		raise ValueError("unexpected engine commit")
	if result["benchmark_commit"] != BENCHMARK_COMMIT:
		raise ValueError("unexpected result benchmark commit")
	if result["engine_commit"] != ENGINE_COMMIT:
		raise ValueError("unexpected result engine commit")
	expected_files = set(provenance["artifacts"]) | {"provenance.json"}
	actual_files = {path.name for path in snapshot.iterdir() if path.is_file()}
	if actual_files != expected_files:
		raise ValueError("unexpected snapshot files")
	for name, expected in provenance["artifacts"].items():
		path = snapshot / name
		if path.stat().st_size != expected["bytes"] or _sha256(path) != expected["sha256"]:
			raise ValueError("artifact digest mismatch: %s" % name)

	metrics = evaluation["metrics"]
	documents = evaluation["documents"]
	if len(documents) != 200:
		raise ValueError("expected 200 evaluation rows")
	document_ids = [str(item["document_id"]) for item in documents]
	if len(set(document_ids)) != 200:
		raise ValueError("evaluation document IDs must be unique")
	for document in documents:
		if not document["prediction_available"]:
			raise ValueError("all predictions must be available")
		scores = document["scores"]
		for name in DOCUMENT_SCORE_NAMES:
			value = scores[name]
			if value is None:
				continue
			if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
				raise ValueError("document score must be finite: %s" % name)
			if not 0.0 <= float(value) <= 1.0:
				raise ValueError("document score must be in [0, 1]: %s" % name)
		available = [
			float(scores[name])
			for name in ("nid", "teds", "mhs")
			if scores[name] is not None
		]
		if not available:
			raise ValueError("every document must have an available primary score")
		_require_close(
			float(scores["overall"]),
			sum(available) / len(available),
			"document overall",
		)

	series = _score_series(documents)
	for name, expected in EXPECTED_SCORES.items():
		_require_close(float(metrics["score"][name]), expected, name)
		_require_close(
			float(metrics["score"][name]),
			sum(series[name]) / len(series[name]),
			"recomputed %s" % name,
		)
	for name, expected in EXPECTED_COUNTS.items():
		if metrics[name] != expected:
			raise ValueError("count mismatch: %s" % name)
	if len(series["nid_mean"]) != metrics["nid_count"]:
		raise ValueError("NID eligibility count mismatch")
	if len(series["teds_mean"]) != metrics["teds_count"]:
		raise ValueError("TEDS eligibility count mismatch")
	if len(series["mhs_mean"]) != metrics["mhs_count"]:
		raise ValueError("MHS eligibility count mismatch")
	if result["metrics"] != metrics or result["speed"] != evaluation["speed"]:
		raise ValueError("normalized result does not match raw evaluation")
	if summary["document_count"] != 200 or evaluation["speed"]["document_count"] != 200:
		raise ValueError("expected 200 documents")
	for name in ("total_elapsed", "elapsed_per_doc"):
		_require_close(
			float(summary[name]),
			float(evaluation["speed"][name]),
			"summary/evaluation %s" % name,
		)
	_require_close(
		float(evaluation["speed"]["elapsed_per_doc"]),
		float(evaluation["speed"]["total_elapsed"]) / 200.0,
		"elapsed_per_doc",
	)
	if summary["processor"] != evaluation["speed"]["processor"]:
		raise ValueError("processor mismatch")
	if failures:
		raise ValueError("expected zero conversion failures")
	if predictions["document_count"] != 200 or len(predictions["documents"]) != 200:
		raise ValueError("expected 200 prediction hashes")
	if set(predictions["documents"]) != set(document_ids):
		raise ValueError("prediction and evaluation IDs differ")
	prediction_bytes = 0
	prediction_aggregate = hashlib.sha256()
	for document_id in sorted(predictions["documents"]):
		record = predictions["documents"][document_id]
		if not isinstance(record["bytes"], int) or record["bytes"] < 0:
			raise ValueError("invalid prediction byte count")
		try:
			digest_bytes = bytes.fromhex(record["sha256"])
		except ValueError as exc:
			raise ValueError("invalid prediction SHA-256") from exc
		if len(digest_bytes) != 32:
			raise ValueError("invalid prediction SHA-256")
		prediction_bytes += record["bytes"]
		prediction_aggregate.update(document_id.encode("ascii"))
		prediction_aggregate.update(b"\0")
		prediction_aggregate.update(str(record["bytes"]).encode("ascii"))
		prediction_aggregate.update(b"\0")
		prediction_aggregate.update(record["sha256"].encode("ascii"))
		prediction_aggregate.update(b"\n")
	if prediction_bytes != predictions["total_bytes"]:
		raise ValueError("prediction byte total mismatch")
	if prediction_aggregate.hexdigest() != predictions["aggregate_sha256"]:
		raise ValueError("prediction inventory digest mismatch")
	if result["completeness"] != {
		"conversion_failures": 0,
		"empty_predictions": 0,
		"evaluated_documents": 200,
		"missing_predictions": 0,
		"prediction_files": 200,
	}:
		raise ValueError("unexpected completeness summary")

	with (snapshot / "evaluation.csv").open("r", encoding="utf-8", newline="") as stream:
		rows = list(csv.DictReader(stream))
		if len(rows) != 200:
			raise ValueError("expected 200 CSV rows")
		csv_ids = [str(row["document_id"]).removeprefix("'") for row in rows]
		if len(set(csv_ids)) != 200 or set(csv_ids) != set(document_ids):
			raise ValueError("CSV and evaluation IDs differ")
	return {
		"provenance": provenance,
		"result": result,
		"evaluation": evaluation,
		"summary": summary,
		"prediction_hashes": predictions,
	}
