#!/usr/bin/env python3
"""Run and validate the pinned 200-document OpenDataLoader benchmark in CI."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib.util
import io
import json
import math
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple


DEFAULT_POLICY = Path(__file__).with_name("policy.json")
ADAPTER_PATH = Path(__file__).with_name("adapter.py")
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
DOCUMENT_ID = re.compile(r"^[0-9]{14}$")
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
COMPONENT_SCORE_NAMES = ("nid_mean", "teds_mean", "mhs_mean")
ARTIFACT_FILES = {
	"evaluation.csv",
	"evaluation.json",
	"failures.json",
	"run-context.json",
	"prediction-hashes.json",
	"provenance.json",
	"result.json",
	"timing.json",
	"manifest.sha256",
}
TIMING_SCHEMA = "cocoapdf.opendataloader-worker-timing/v1"
TIMING_SCOPE = "canonical_adapter_batch_conversion_container_wall_time"
TIMING_RUNNER = {
	"architecture": "linux/amd64",
	"container_image": "python@sha256:dd86541a59b252667f4c12f8b2ee17216de37dd65ac773bf097bef996fa78860",
	"cpu_limit": 2,
	"memory_limit_bytes": 2 * 1024 * 1024 * 1024,
	"network": "none",
	"os": "ubuntu-24.04",
	"pids_limit": 256,
	"read_only_root": True,
	"timer": "host_python_time.monotonic_ns",
}
MAX_CONVERSION_ELAPSED_NANOSECONDS = 20 * 60 * 1_000_000_000
MAX_ARTIFACT_FILE_BYTES = 2 * 1024 * 1024
MAX_ARTIFACT_TOTAL_BYTES = 5 * 1024 * 1024
MAX_PREDICTION_FILE_BYTES = 256 * 1024
MAX_PREDICTION_TOTAL_BYTES = 8 * 1024 * 1024
MAX_CANDIDATE_ARTIFACT_BYTES = 12 * 1024 * 1024
MAX_BOUNDARY_FILE_BYTES = 1024 * 1024
PRIVILEGED_BOUNDARY_PATHS = (
	".github/workflows/opendataloader-benchmark.yml",
	".github/workflows/opendataloader-report.yml",
	".github/workflows/opendataloader-worker.yml",
	".github/workflows/pr-visual-report.yml",
	"tools/update_pr_body_block.py",
	"validation/benchmarks/opendataloader_bench/adapter.py",
	"validation/benchmarks/opendataloader_bench/ci_runner.py",
	"validation/benchmarks/opendataloader_bench/evaluator-requirements.txt",
	"validation/benchmarks/opendataloader_bench/policy.json",
	"validation/benchmarks/opendataloader_bench/report.py",
	"validation/pr_visual/report.py",
)


class BenchmarkValidationError(ValueError):
	"""Raised when benchmark inputs or results violate the pinned contract."""


def _read_json(path: Path) -> Any:
	return json.loads(
		path.read_text(encoding="utf-8"),
		parse_constant=lambda value: (_ for _ in ()).throw(
			BenchmarkValidationError("non-finite JSON value: %s" % value)
		),
	)


def _write_json(path: Path, value: Any) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	with path.open("w", encoding="utf-8", newline="\n") as stream:
		json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
		stream.write("\n")


def _write_text(path: Path, value: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	with path.open("w", encoding="utf-8", newline="\n") as stream:
		stream.write(value)


def _sha256(path: Path) -> str:
	return hashlib.sha256(path.read_bytes()).hexdigest()


def _finite_number(value: Any, label: str, *, minimum: float = 0.0, maximum: float = 1.0) -> float:
	if isinstance(value, bool) or not isinstance(value, (int, float)):
		raise BenchmarkValidationError("%s must be a number" % label)
	number = float(value)
	if not math.isfinite(number) or not minimum <= number <= maximum:
		raise BenchmarkValidationError("%s must be finite and in [%s, %s]" % (label, minimum, maximum))
	return number


def _positive_int(value: Any, label: str) -> int:
	if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
		raise BenchmarkValidationError("%s must be a positive integer" % label)
	return value


def _require(condition: bool, message: str) -> None:
	if not condition:
		raise BenchmarkValidationError(message)


def _require_close(actual: float, expected: float, label: str) -> None:
	if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-15):
		raise BenchmarkValidationError("%s mismatch: %r != %r" % (label, actual, expected))


def load_policy(path: Path = DEFAULT_POLICY) -> Dict[str, Any]:
	policy = _read_json(path)
	_require(isinstance(policy, dict), "policy must be an object")
	_require(policy.get("schema") == "cocoapdf.opendataloader-ci-policy/v1", "unexpected policy schema")
	benchmark = policy.get("benchmark")
	_require(isinstance(benchmark, dict), "policy benchmark must be an object")
	for label in ("commit", "tree"):
		value = benchmark.get(label)
		_require(isinstance(value, str) and COMMIT_SHA.fullmatch(value) is not None, "invalid benchmark %s" % label)
	adapter = policy.get("adapter")
	_require(isinstance(adapter, dict), "policy adapter must be an object")
	_require(
		isinstance(adapter.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", adapter["sha256"]) is not None,
		"invalid adapter SHA-256",
	)
	counts = policy.get("required_counts")
	_require(isinstance(counts, dict), "required_counts must be an object")
	for name in ("document_count", "nid_count", "teds_count", "mhs_count"):
		_positive_int(counts.get(name), "required_counts.%s" % name)
	_require(counts.get("missing_predictions") == 0, "required missing_predictions must be zero")
	corpus = benchmark.get("corpus")
	_require(isinstance(corpus, dict), "policy benchmark corpus must be an object")
	_require(
		_positive_int(corpus.get("page_count"), "benchmark.corpus.page_count")
		>= _positive_int(corpus.get("pdf_count"), "benchmark.corpus.pdf_count"),
		"pinned corpus page count cannot be smaller than its PDF count",
	)
	_require(
		corpus["pdf_count"] == counts["document_count"],
		"pinned PDF count must match the required document count",
	)
	gates = policy.get("gates")
	_require(isinstance(gates, dict), "gates must be an object")
	_finite_number(gates.get("overall_floor"), "gates.overall_floor")
	_finite_number(gates.get("material_regression"), "gates.material_regression")
	component_floors = gates.get("component_floors")
	_require(isinstance(component_floors, dict), "gates.component_floors must be an object")
	_require(
		set(component_floors).issubset(COMPONENT_SCORE_NAMES),
		"gates.component_floors contains an unexpected score",
	)
	for name, value in component_floors.items():
		_finite_number(value, "gates.component_floors.%s" % name)
	primary = gates.get("primary_scores")
	_require(
		primary == ["overall_mean", "nid_mean", "teds_mean", "mhs_mean"],
		"unexpected primary score policy",
	)
	baseline_scores = policy.get("baseline", {}).get("scores")
	_require(isinstance(baseline_scores, dict), "baseline scores must be an object")
	for name in primary:
		_finite_number(baseline_scores.get(name), "baseline.%s" % name)
	worker = policy.get("worker")
	_require(isinstance(worker, dict), "policy worker must be an object")
	_require(
		set(worker) == {"commit", "path", "repository"},
		"unexpected worker policy fields",
	)
	_require(
		isinstance(worker.get("commit"), str) and COMMIT_SHA.fullmatch(worker["commit"]) is not None,
		"invalid worker commit",
	)
	_require(
		worker.get("repository") == "sayantandey/CocoaPDF"
		and worker.get("path") == ".github/workflows/opendataloader-worker.yml",
		"unexpected worker identity",
	)
	return policy


def _inventory(paths: Iterable[Path]) -> Tuple[Dict[str, Dict[str, Any]], int, str]:
	records: Dict[str, Dict[str, Any]] = {}
	total_bytes = 0
	aggregate = hashlib.sha256()
	for path in sorted(paths, key=lambda item: item.name):
		data = path.read_bytes()
		if path.stem in records:
			raise BenchmarkValidationError("duplicate document ID: %s" % path.stem)
		record = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
		records[path.stem] = record
		total_bytes += len(data)
		aggregate.update(path.stem.encode("ascii"))
		aggregate.update(b"\0")
		aggregate.update(str(len(data)).encode("ascii"))
		aggregate.update(b"\0")
		aggregate.update(record["sha256"].encode("ascii"))
		aggregate.update(b"\n")
	return records, total_bytes, aggregate.hexdigest()


def verify_pdf_corpus(corpus_root: Path, policy: Mapping[str, Any]) -> Set[str]:
	pdf_root = corpus_root / "pdfs"
	_require(pdf_root.is_dir() and not pdf_root.is_symlink(), "PDF corpus directory is invalid")
	pdf_children = list(pdf_root.iterdir())
	_require(
		all(path.is_file() and not path.is_symlink() and path.suffix == ".pdf" for path in pdf_children),
		"PDF corpus contains an unexpected or non-regular entry",
	)
	pdfs = sorted(pdf_children)
	corpus = policy["benchmark"]["corpus"]
	_require(len(pdfs) == corpus["pdf_count"], "expected exactly 200 PDFs")
	for pdf in pdfs:
		_require(DOCUMENT_ID.fullmatch(pdf.stem) is not None, "invalid PDF document ID")
		_require(pdf.read_bytes()[:5] == b"%PDF-", "invalid PDF or pointer file: %s" % pdf.name)
	pdf_inventory, pdf_bytes, pdf_digest = _inventory(pdfs)
	_require(pdf_bytes == corpus["pdf_bytes"], "PDF byte count mismatch")
	_require(pdf_digest == corpus["pdf_inventory_sha256"], "PDF inventory digest mismatch")
	return set(pdf_inventory)


def verify_corpus(corpus_root: Path, benchmark_root: Path, policy: Mapping[str, Any]) -> Set[str]:
	pdf_root = corpus_root / "pdfs"
	_require(pdf_root.is_dir() and not pdf_root.is_symlink(), "PDF corpus directory is invalid")
	pdf_children = list(pdf_root.iterdir())
	_require(
		all(path.is_file() and not path.is_symlink() and path.suffix == ".pdf" for path in pdf_children),
		"PDF corpus contains an unexpected or non-regular entry",
	)
	ground_truth_root = benchmark_root / "ground-truth" / "markdown"
	_require(
		ground_truth_root.is_dir() and not ground_truth_root.is_symlink(),
		"ground-truth directory is invalid",
	)
	ground_truth_children = list(ground_truth_root.iterdir())
	_require(
		all(path.is_file() and not path.is_symlink() and path.suffix == ".md" for path in ground_truth_children),
		"ground truth contains an unexpected or non-regular entry",
	)
	ground_truth = sorted(ground_truth_children)
	corpus = policy["benchmark"]["corpus"]
	_require(len(ground_truth) == corpus["ground_truth_count"], "expected exactly 200 ground truths")
	for markdown in ground_truth:
		_require(DOCUMENT_ID.fullmatch(markdown.stem) is not None, "invalid ground-truth document ID")
	pdf_ids = verify_pdf_corpus(corpus_root, policy)
	gt_inventory, gt_bytes, gt_digest = _inventory(ground_truth)
	_require(gt_bytes == corpus["ground_truth_bytes"], "ground-truth byte count mismatch")
	_require(gt_digest == corpus["ground_truth_inventory_sha256"], "ground-truth inventory digest mismatch")
	_require(pdf_ids == set(gt_inventory), "PDF and ground-truth IDs differ")
	return pdf_ids


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


def verify_benchmark(checkout: Path, policy: Mapping[str, Any]) -> None:
	benchmark = policy["benchmark"]
	_require(_git(checkout, "rev-parse", "HEAD") == benchmark["commit"], "benchmark commit mismatch")
	_require(
		_git(checkout, "show", "-s", "--format=%T", "HEAD") == benchmark["tree"],
		"benchmark tree mismatch",
	)
	for relative, expected in benchmark["evaluator_files"].items():
		path = checkout / relative
		_require(path.is_file(), "missing evaluator file: %s" % relative)
		_require(_sha256(path) == expected, "evaluator file hash mismatch: %s" % relative)


def verify_privileged_boundary(trusted_root: Path, candidate_root: Path) -> None:
	for label, root in (("trusted", trusted_root), ("candidate", candidate_root)):
		_require(root.is_dir() and not root.is_symlink(), "%s checkout root is invalid" % label)
	for relative in PRIVILEGED_BOUNDARY_PATHS:
		trusted = trusted_root / relative
		candidate = candidate_root / relative
		_require(trusted.is_file() and not trusted.is_symlink(), "trusted boundary file is invalid: %s" % relative)
		_require(candidate.is_file() and not candidate.is_symlink(), "candidate changed privileged boundary: %s" % relative)
		_require(trusted.read_bytes() == candidate.read_bytes(), "candidate changed privileged boundary: %s" % relative)


def _github_api_json(endpoint: str, token: str) -> Any:
	request = urllib.request.Request(
		"https://api.github.com" + endpoint,
		headers={
			"Accept": "application/vnd.github+json",
			"Authorization": "Bearer %s" % token,
			"User-Agent": "CocoaPDF-ODL-boundary-verifier",
			"X-GitHub-Api-Version": "2022-11-28",
		},
	)
	try:
		with urllib.request.urlopen(request, timeout=30) as response:
			return json.loads(response.read().decode("utf-8"))
	except (urllib.error.HTTPError, urllib.error.URLError, UnicodeDecodeError, json.JSONDecodeError) as exc:
		raise BenchmarkValidationError("GitHub boundary API request failed") from exc


def verify_privileged_boundary_ref(
	trusted_root: Path,
	candidate_repository: str,
	candidate_sha: str,
	token: str,
	*,
	api_get: Optional[Callable[[str], Any]] = None,
) -> None:
	"""Compare protected candidate blobs without checking untrusted code out."""

	_require(trusted_root.is_dir() and not trusted_root.is_symlink(), "trusted checkout root is invalid")
	_require(
		re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", candidate_repository) is not None,
		"invalid candidate repository",
	)
	_require(COMMIT_SHA.fullmatch(candidate_sha) is not None, "invalid candidate SHA")
	_require(bool(token), "GitHub boundary token is missing")
	getter = api_get if api_get is not None else lambda endpoint: _github_api_json(endpoint, token)
	repository = urllib.parse.quote(candidate_repository, safe="/")
	commit = getter("/repos/%s/git/commits/%s" % (repository, candidate_sha))
	_require(isinstance(commit, dict) and commit.get("sha") == candidate_sha, "candidate commit identity mismatch")
	tree_record = commit.get("tree")
	_require(isinstance(tree_record, dict), "candidate commit tree is missing")
	tree_sha = tree_record.get("sha")
	_require(isinstance(tree_sha, str) and COMMIT_SHA.fullmatch(tree_sha) is not None, "invalid candidate tree SHA")
	tree = getter("/repos/%s/git/trees/%s?recursive=1" % (repository, tree_sha))
	_require(isinstance(tree, dict) and tree.get("truncated") is False, "candidate tree is incomplete")
	entries = tree.get("tree")
	_require(isinstance(entries, list), "candidate tree entries are missing")
	protected: Dict[str, Mapping[str, Any]] = {}
	for entry in entries:
		if not isinstance(entry, dict):
			continue
		path = entry.get("path")
		if path in PRIVILEGED_BOUNDARY_PATHS:
			_require(path not in protected, "duplicate candidate boundary path")
			protected[path] = entry
	for relative in PRIVILEGED_BOUNDARY_PATHS:
		trusted = trusted_root / relative
		_require(trusted.is_file() and not trusted.is_symlink(), "trusted boundary file is invalid: %s" % relative)
		entry = protected.get(relative)
		_require(isinstance(entry, dict), "candidate changed privileged boundary: %s" % relative)
		_require(
			entry.get("type") == "blob" and entry.get("mode") in {"100644", "100755"},
			"candidate boundary is not a regular file: %s" % relative,
		)
		trusted_content = trusted.read_bytes()
		_require(
			entry.get("size") == len(trusted_content) <= MAX_BOUNDARY_FILE_BYTES,
			"candidate boundary size mismatch: %s" % relative,
		)
		blob_sha = entry.get("sha")
		_require(isinstance(blob_sha, str) and COMMIT_SHA.fullmatch(blob_sha) is not None, "invalid boundary blob SHA")
		blob = getter("/repos/%s/git/blobs/%s" % (repository, blob_sha))
		_require(isinstance(blob, dict) and blob.get("sha") == blob_sha, "boundary blob identity mismatch")
		_require(blob.get("encoding") == "base64" and isinstance(blob.get("content"), str), "invalid boundary blob encoding")
		try:
			content = base64.b64decode(blob["content"].replace("\n", ""), validate=True)
		except (ValueError, TypeError) as exc:
			raise BenchmarkValidationError("invalid boundary blob content") from exc
		_require(blob.get("size") == len(content), "boundary blob size mismatch")
		_require(content == trusted_content, "candidate changed privileged boundary: %s" % relative)


def candidate_artifact_name(context: Mapping[str, Any]) -> str:
	candidate = context["candidate"]
	run = context["run"]
	return "cocoapdf-odl-candidate-%s-%s-%s" % (
		candidate["head_sha"],
		run["id"],
		run["attempt"],
	)


def verify_candidate_artifact_metadata(
	context_path: Path,
	token: str,
	*,
	policy: Optional[Mapping[str, Any]] = None,
	api_get: Optional[Callable[[str], Any]] = None,
) -> Mapping[str, Any]:
	"""Authenticate and bound the prediction archive before extraction."""

	context = load_run_context(context_path)
	policy = load_policy() if policy is None else policy
	_require(bool(token), "GitHub artifact token is missing")
	getter = api_get if api_get is not None else lambda endpoint: _github_api_json(endpoint, token)
	run_id = context["run"]["id"]
	run = getter("/repos/sayantandey/CocoaPDF/actions/runs/%s" % run_id)
	_require(isinstance(run, dict) and run.get("id") == run_id, "candidate workflow run identity mismatch")
	_require(run.get("run_attempt") == context["run"]["attempt"], "candidate workflow attempt mismatch")
	_require(run.get("name") == context["workflow"]["name"], "candidate workflow name mismatch")
	_require(
		str(run.get("path", "")).split("@", 1)[0] == context["workflow"]["path"],
		"candidate workflow path mismatch",
	)
	_require(
		run.get("event") == context["run"]["event"]
		and run.get("head_sha") == context["candidate"]["head_sha"]
		and run.get("conclusion") == "success",
		"candidate workflow execution identity mismatch",
	)
	head_repository = run.get("head_repository")
	_require(
		isinstance(head_repository, dict)
		and head_repository.get("full_name") == context["candidate"]["head_repository"],
		"candidate workflow repository mismatch",
	)
	worker = policy["worker"]
	expected_worker_path = "%s/%s@%s" % (
		worker["repository"],
		worker["path"],
		worker["commit"],
	)
	referenced = run.get("referenced_workflows")
	_require(isinstance(referenced, list), "candidate workflow references are missing")
	matches = [
		item
		for item in referenced
		if isinstance(item, dict)
		and item.get("path") == expected_worker_path
		and item.get("sha") == worker["commit"]
	]
	_require(len(matches) == 1, "candidate workflow did not use the pinned worker")
	payload = getter(
		"/repos/sayantandey/CocoaPDF/actions/runs/%s/artifacts?per_page=100" % run_id
	)
	_require(isinstance(payload, dict) and isinstance(payload.get("artifacts"), list), "candidate artifact listing is invalid")
	expected_name = candidate_artifact_name(context)
	matches = [
		artifact
		for artifact in payload["artifacts"]
		if isinstance(artifact, dict) and artifact.get("name") == expected_name
	]
	_require(len(matches) == 1, "expected exactly one candidate prediction artifact")
	artifact = matches[0]
	_require(artifact.get("expired") is False, "candidate prediction artifact expired")
	_require(_positive_int(artifact.get("id"), "candidate artifact ID") > 0, "invalid candidate artifact ID")
	size = artifact.get("size_in_bytes")
	_require(
		isinstance(size, int) and not isinstance(size, bool) and 0 < size <= MAX_CANDIDATE_ARTIFACT_BYTES,
		"candidate prediction artifact exceeds its size limit",
	)
	_require(
		isinstance(artifact.get("digest"), str)
		and re.fullmatch(r"sha256:[0-9a-f]{64}", artifact["digest"]) is not None,
		"candidate prediction artifact digest is invalid",
	)
	workflow_run = artifact.get("workflow_run")
	_require(
		isinstance(workflow_run, dict) and workflow_run.get("id") == run_id,
		"candidate artifact triggering-run mismatch",
	)
	return artifact


class _SafeGithubRedirect(urllib.request.HTTPRedirectHandler):
	def redirect_request(self, request, file_pointer, code, message, headers, new_url):  # type: ignore[override]
		redirected = super().redirect_request(request, file_pointer, code, message, headers, new_url)
		if redirected is None:
			return None
		target = urllib.parse.urlsplit(redirected.full_url)
		_require(target.scheme == "https" and bool(target.hostname), "unsafe artifact redirect")
		if target.hostname != urllib.parse.urlsplit(request.full_url).hostname:
			redirected.remove_header("Authorization")
			redirected.remove_header("X-GitHub-Api-Version")
		return redirected


def _github_archive_bytes(url: str, token: str, maximum: int) -> bytes:
	parsed = urllib.parse.urlsplit(url)
	_require(
		parsed.scheme == "https" and parsed.hostname == "api.github.com",
		"invalid candidate artifact download URL",
	)
	request = urllib.request.Request(
		url,
		headers={
			"Accept": "application/vnd.github+json",
			"Authorization": "Bearer %s" % token,
			"User-Agent": "CocoaPDF-ODL-artifact-downloader",
			"X-GitHub-Api-Version": "2022-11-28",
		},
	)
	try:
		with urllib.request.build_opener(_SafeGithubRedirect()).open(request, timeout=60) as response:
			length = response.headers.get("Content-Length")
			if length is not None:
				_require(int(length) <= maximum, "candidate artifact download exceeds its size limit")
			data = response.read(maximum + 1)
	except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as exc:
		raise BenchmarkValidationError("candidate artifact download failed") from exc
	_require(0 < len(data) <= maximum, "candidate artifact download exceeds its size limit")
	return data


def _extract_candidate_archive(data: bytes, destination: Path, policy: Mapping[str, Any]) -> None:
	if destination.exists() and any(destination.iterdir()):
		raise BenchmarkValidationError("candidate extraction directory must be empty")
	destination.mkdir(parents=True, exist_ok=True)
	allowed_directories = {
		"prediction/",
		"prediction/cocoapdf/",
		"prediction/cocoapdf/markdown/",
	}
	markdown_pattern = re.compile(r"prediction/cocoapdf/markdown/([0-9]{14})\.md")
	allowed_metadata = "prediction/cocoapdf/failures.json"
	trusted_timing = "timing.json"
	seen: Set[str] = set()
	markdown_ids: Set[str] = set()
	total_bytes = 0
	try:
		with zipfile.ZipFile(io.BytesIO(data), "r") as archive:
			infos = archive.infolist()
			_require(len(infos) <= policy["required_counts"]["document_count"] + 5, "candidate archive has too many entries")
			for info in infos:
				name = info.filename
				_require(
					name not in seen
					and "\\" not in name
					and not name.startswith("/")
					and "\x00" not in name
					and all(part not in {"", ".", ".."} for part in name.rstrip("/").split("/")),
					"candidate archive contains an unsafe or duplicate path",
				)
				seen.add(name)
				mode = (info.external_attr >> 16) & 0xFFFF
				_require(not stat.S_ISLNK(mode), "candidate archive contains a symbolic link")
				_require(info.flag_bits & 0x1 == 0, "candidate archive contains encrypted data")
				_require(info.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}, "candidate archive compression is unsupported")
				if info.is_dir():
					_require(name in allowed_directories, "candidate archive contains an unexpected directory")
					continue
				match = markdown_pattern.fullmatch(name)
				_require(
					match is not None or name in {allowed_metadata, trusted_timing},
					"candidate archive contains an unexpected file",
				)
				limit = MAX_PREDICTION_FILE_BYTES if match is not None else MAX_ARTIFACT_FILE_BYTES
				_require(0 <= info.file_size <= limit, "candidate archive entry exceeds its size limit")
				total_bytes += info.file_size
				_require(
					total_bytes <= MAX_PREDICTION_TOTAL_BYTES + MAX_ARTIFACT_FILE_BYTES,
					"candidate archive expands beyond its total size limit",
				)
				content = archive.read(info)
				_require(len(content) == info.file_size, "candidate archive entry size mismatch")
				target = destination.joinpath(*name.split("/"))
				target.parent.mkdir(parents=True, exist_ok=True)
				_require(not target.exists(), "candidate archive target already exists")
				target.write_bytes(content)
				if match is not None:
					markdown_ids.add(match.group(1))
	except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
		raise BenchmarkValidationError("candidate prediction archive is invalid") from exc
	_require(allowed_metadata in seen, "candidate archive metadata is missing")
	_require(trusted_timing in seen, "trusted worker timing is missing")
	_require(
		len(markdown_ids) == policy["required_counts"]["document_count"],
		"candidate archive must contain exactly 200 predictions",
	)
	validate_candidate_output(destination / "prediction" / "cocoapdf", markdown_ids)
	validate_timing_document(_read_json(destination / trusted_timing), policy)


def download_candidate_artifact(
	context_path: Path,
	destination: Path,
	token: str,
	*,
	api_get: Optional[Callable[[str], Any]] = None,
	archive_get: Optional[Callable[[str, str, int], bytes]] = None,
) -> Mapping[str, Any]:
	policy = load_policy()
	artifact = verify_candidate_artifact_metadata(
		context_path,
		token,
		policy=policy,
		api_get=api_get,
	)
	archive_url = artifact.get("archive_download_url")
	_require(isinstance(archive_url, str), "candidate artifact download URL is missing")
	getter = archive_get if archive_get is not None else _github_archive_bytes
	data = getter(archive_url, token, MAX_CANDIDATE_ARTIFACT_BYTES)
	_require(
		"sha256:" + hashlib.sha256(data).hexdigest() == artifact["digest"],
		"candidate artifact download digest mismatch",
	)
	_extract_candidate_archive(data, destination, policy)
	return artifact


def _score_series(documents: Sequence[Mapping[str, Any]]) -> Dict[str, List[float]]:
	return {
		"overall_mean": [float(item["scores"]["overall"]) for item in documents],
		"nid_mean": [float(item["scores"]["nid"]) for item in documents if item["scores"]["nid"] is not None],
		"nid_s_mean": [float(item["scores"]["nid_s"]) for item in documents if item["scores"]["nid_s"] is not None],
		"teds_mean": [float(item["scores"]["teds"]) for item in documents if item["scores"]["teds"] is not None],
		"teds_s_mean": [float(item["scores"]["teds_s"]) for item in documents if item["scores"]["teds_s"] is not None],
		"mhs_mean": [float(item["scores"]["mhs"]) for item in documents if item["scores"]["mhs"] is not None],
		"mhs_s_mean": [float(item["scores"]["mhs_s"]) for item in documents if item["scores"]["mhs_s"] is not None],
	}


def _baseline_eligibility(policy: Mapping[str, Any]) -> Optional[Dict[str, Tuple[bool, bool, bool]]]:
	commit = policy.get("baseline", {}).get("engine_commit")
	benchmark_commit = policy.get("benchmark", {}).get("commit")
	if not isinstance(commit, str) or not isinstance(benchmark_commit, str):
		return None
	path = Path(__file__).with_name("results") / benchmark_commit / "evaluation.json"
	if not path.is_file():
		return None
	evaluation = _read_json(path)
	eligibility: Dict[str, Tuple[bool, bool, bool]] = {}
	for item in evaluation.get("documents", []):
		scores = item.get("scores", {})
		eligibility[str(item.get("document_id"))] = tuple(
			scores.get(name) is not None for name in ("nid", "teds", "mhs")
		)  # type: ignore[assignment]
	return eligibility


def validate_evaluation(
	evaluation: Any,
	policy: Mapping[str, Any],
	*,
	expected_ids: Optional[Set[str]] = None,
	check_baseline_eligibility: bool = False,
) -> Dict[str, Any]:
	_require(isinstance(evaluation, dict), "evaluation must be an object")
	documents = evaluation.get("documents")
	_require(isinstance(documents, list), "evaluation.documents must be a list")
	required = policy["required_counts"]
	_require(len(documents) == required["document_count"], "evaluation must contain 200 documents")
	seen: Set[str] = set()
	eligibility = _baseline_eligibility(policy) if check_baseline_eligibility else None
	for item in documents:
		_require(isinstance(item, dict), "each document result must be an object")
		document_id = item.get("document_id")
		_require(isinstance(document_id, str) and DOCUMENT_ID.fullmatch(document_id) is not None, "invalid document ID")
		_require(document_id not in seen, "duplicate evaluation document ID")
		seen.add(document_id)
		_require(item.get("prediction_available") is True, "every prediction must be available")
		scores = item.get("scores")
		_require(isinstance(scores, dict), "document scores must be an object")
		_require(set(scores) == set(DOCUMENT_SCORE_NAMES), "unexpected document score fields")
		for name in DOCUMENT_SCORE_NAMES:
			value = scores[name]
			if value is not None:
				_finite_number(value, "%s.%s" % (document_id, name))
		_require(scores["nid"] is not None and scores["nid_s"] is not None, "NID scores must be present")
		available = [float(scores[name]) for name in ("nid", "teds", "mhs") if scores[name] is not None]
		_require_close(float(scores["overall"]), sum(available) / len(available), "%s overall" % document_id)
		if eligibility is not None:
			_require(document_id in eligibility, "document absent from baseline eligibility")
			actual = tuple(scores.get(name) is not None for name in ("nid", "teds", "mhs"))
			_require(actual == eligibility[document_id], "eligibility changed for %s" % document_id)
	if expected_ids is not None:
		_require(seen == expected_ids, "evaluation IDs do not match the corpus")
	if eligibility is not None:
		_require(seen == set(eligibility), "evaluation IDs differ from baseline")

	metrics = evaluation.get("metrics")
	_require(isinstance(metrics, dict), "evaluation.metrics must be an object")
	_require(set(metrics) == {"score", "nid_count", "teds_count", "mhs_count", "missing_predictions"}, "unexpected metric fields")
	scores = metrics.get("score")
	_require(isinstance(scores, dict) and set(scores) == set(SCORE_NAMES), "unexpected aggregate score fields")
	series = _score_series(documents)
	for name, values in series.items():
		_require(bool(values), "aggregate series is empty: %s" % name)
		actual = _finite_number(scores[name], "metrics.score.%s" % name)
		_require_close(actual, sum(values) / len(values), "aggregate %s" % name)
	for name in ("nid_count", "teds_count", "mhs_count", "missing_predictions"):
		value = metrics.get(name)
		_require(isinstance(value, int) and not isinstance(value, bool), "%s must be an integer" % name)
		_require(value == required[name], "%s changed" % name)
	_require(len(series["nid_mean"]) == required["nid_count"], "NID eligibility count mismatch")
	_require(len(series["teds_mean"]) == required["teds_count"], "TEDS eligibility count mismatch")
	_require(len(series["mhs_mean"]) == required["mhs_count"], "MHS eligibility count mismatch")
	return evaluation


def evaluate_gate(metrics: Mapping[str, Any], completeness: Mapping[str, Any], policy: Mapping[str, Any]) -> Dict[str, Any]:
	scores = metrics["score"]
	gates = policy["gates"]
	baseline = policy["baseline"]["scores"]
	tolerance = float(gates["material_regression"])
	deltas = {name: float(scores[name]) - float(baseline[name]) for name in gates["primary_scores"]}
	failures: List[str] = []
	warnings: List[str] = []
	if float(scores["overall_mean"]) < float(gates["overall_floor"]):
		failures.append("overall score is below the absolute floor")
	component_floors = {
		name: float(gates["component_floors"][name])
		for name in COMPONENT_SCORE_NAMES
		if name in gates["component_floors"]
	}
	for name, floor in component_floors.items():
		if float(scores[name]) < floor:
			failures.append("%s is below its absolute floor" % name)
	for name, delta in deltas.items():
		if delta < -tolerance:
			failures.append("%s regressed by more than %.4f" % (name, tolerance))
		elif delta < 0.0:
			warnings.append("%s is below baseline within tolerance" % name)
	expected_completeness = {
		"conversion_failures": 0,
		"empty_predictions": 0,
		"evaluated_documents": policy["required_counts"]["document_count"],
		"missing_predictions": 0,
		"prediction_files": policy["required_counts"]["document_count"],
	}
	if dict(completeness) != expected_completeness:
		failures.append("conversion completeness contract failed")
	return {
		"baseline": {name: baseline[name] for name in gates["primary_scores"]},
		"component_floors": component_floors,
		"deltas": deltas,
		"failures": failures,
		"material_regression": tolerance,
		"overall_floor": gates["overall_floor"],
		"passed": not failures,
		"warnings": warnings,
	}


def _load_adapter(path: Path):
	spec = importlib.util.spec_from_file_location("cocoapdf_odl_ci_adapter", path)
	if spec is None or spec.loader is None:
		raise BenchmarkValidationError("unable to load the benchmark adapter")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	handler = getattr(module, "to_markdown", None)
	if not callable(handler):
		raise BenchmarkValidationError("adapter has no to_markdown handler")
	return handler


def _prediction_hashes(markdown_paths: Sequence[Path]) -> Dict[str, Any]:
	records, total_bytes, digest = _inventory(markdown_paths)
	return {
		"aggregate_sha256": digest,
		"document_count": len(markdown_paths),
		"documents": records,
		"schema": "cocoapdf.opendataloader-prediction-hashes/v1",
		"total_bytes": total_bytes,
	}


def _copy_lf(source: Path, destination: Path) -> None:
	destination.write_bytes(source.read_bytes().replace(b"\r\n", b"\n"))


def _timing_document(elapsed_nanoseconds: int, policy: Mapping[str, Any]) -> Dict[str, Any]:
	"""Build the immutable worker's externally measured conversion timing."""

	_require(
		isinstance(elapsed_nanoseconds, int) and not isinstance(elapsed_nanoseconds, bool),
		"conversion elapsed nanoseconds must be an integer",
	)
	_require(
		0 < elapsed_nanoseconds <= MAX_CONVERSION_ELAPSED_NANOSECONDS,
		"conversion elapsed nanoseconds are outside the workflow bound",
	)
	document_count = _positive_int(
		policy["benchmark"]["corpus"].get("pdf_count"),
		"benchmark.corpus.pdf_count",
	)
	page_count = _positive_int(
		policy["benchmark"]["corpus"].get("page_count"),
		"benchmark.corpus.page_count",
	)
	total_seconds = elapsed_nanoseconds / 1_000_000_000.0
	return {
		"document_count": document_count,
		"elapsed_nanoseconds": elapsed_nanoseconds,
		"page_count": page_count,
		"runner": dict(TIMING_RUNNER),
		"schema": TIMING_SCHEMA,
		"scope": TIMING_SCOPE,
		"seconds_per_page": total_seconds / page_count,
		"total_seconds": total_seconds,
	}


def validate_timing_document(value: Any, policy: Mapping[str, Any]) -> Dict[str, Any]:
	"""Validate trusted worker timing and recompute every derived value."""

	_require(isinstance(value, dict), "worker timing must be an object")
	_require(
		set(value) == {
			"document_count",
			"elapsed_nanoseconds",
			"page_count",
			"runner",
			"schema",
			"scope",
			"seconds_per_page",
			"total_seconds",
		},
		"unexpected worker timing fields",
	)
	_require(value.get("schema") == TIMING_SCHEMA, "unexpected worker timing schema")
	_require(value.get("scope") == TIMING_SCOPE, "unexpected worker timing scope")
	_require(value.get("runner") == TIMING_RUNNER, "unexpected worker timing runner")
	elapsed_nanoseconds = value.get("elapsed_nanoseconds")
	expected = _timing_document(elapsed_nanoseconds, policy)
	_require(value.get("document_count") == expected["document_count"], "worker timing document count mismatch")
	_require(value.get("page_count") == expected["page_count"], "worker timing page count mismatch")
	total_seconds = value.get("total_seconds")
	seconds_per_page = value.get("seconds_per_page")
	_require(
		isinstance(total_seconds, (int, float)) and not isinstance(total_seconds, bool),
		"worker total seconds must be a number",
	)
	_require(
		isinstance(seconds_per_page, (int, float)) and not isinstance(seconds_per_page, bool),
		"worker seconds per page must be a number",
	)
	_require_close(float(total_seconds), expected["total_seconds"], "worker total seconds")
	_require_close(float(seconds_per_page), expected["seconds_per_page"], "worker seconds per page")
	return expected


def _manifest(artifact_root: Path) -> None:
	lines = []
	for path in sorted(artifact_root.iterdir(), key=lambda item: item.name):
		if path.name == "manifest.sha256":
			continue
		_require(path.is_file() and not path.is_symlink(), "artifact contains a non-file")
		lines.append("%s  %s" % (_sha256(path), path.name))
	_write_text(artifact_root / "manifest.sha256", "\n".join(lines) + "\n")


def write_run_context(args: argparse.Namespace) -> Dict[str, Any]:
	_require(COMMIT_SHA.fullmatch(args.trusted_sha) is not None, "invalid trusted_sha")
	event = _read_json(args.event_json)
	_require(isinstance(event, dict) and event.get("action") == "completed", "expected a completed workflow_run event")
	repository = event.get("repository")
	_require(isinstance(repository, dict) and repository.get("full_name") == "sayantandey/CocoaPDF", "workflow_run repository mismatch")
	run = event.get("workflow_run")
	_require(isinstance(run, dict), "workflow_run payload is missing")
	_require(run.get("name") == "OpenDataLoader benchmark", "unexpected triggering workflow name")
	_require(str(run.get("path", "")).split("@", 1)[0] == ".github/workflows/opendataloader-benchmark.yml", "unexpected triggering workflow path")
	_require(run.get("conclusion") == "success", "trusted evaluation requires a successful trigger")
	event_name = run.get("event")
	_require(event_name in {"pull_request", "push"}, "unexpected triggering event")
	head_sha = run.get("head_sha")
	_require(isinstance(head_sha, str) and COMMIT_SHA.fullmatch(head_sha) is not None, "invalid candidate head SHA")
	head_repository = run.get("head_repository")
	_require(isinstance(head_repository, dict), "workflow_run head repository is missing")
	head_repository_name = head_repository.get("full_name")
	_require(
		isinstance(head_repository_name, str)
		and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", head_repository_name) is not None,
		"invalid head repository",
	)
	run_id = _positive_int(run.get("id"), "run_id")
	run_attempt = _positive_int(run.get("run_attempt"), "run_attempt")
	if event_name == "push":
		_require(head_repository_name == "sayantandey/CocoaPDF", "push context repository mismatch")
		_require(run.get("head_branch") == "main", "push context branch mismatch")
	context = {
		"candidate": {
			"head_repository": head_repository_name,
			"head_sha": head_sha,
			"pull_request_number": None,
		},
		"repository": "sayantandey/CocoaPDF",
		"run": {
			"attempt": run_attempt,
			"event": event_name,
			"id": run_id,
		},
		"schema": "cocoapdf.opendataloader-run-context/v1",
		"trusted_harness_sha": args.trusted_sha,
		"workflow": {
			"name": "OpenDataLoader benchmark",
			"path": ".github/workflows/opendataloader-benchmark.yml",
		},
	}
	args.artifact_root.mkdir(parents=True, exist_ok=True)
	_require(not any(args.artifact_root.iterdir()), "artifact directory must start empty")
	_write_json(args.artifact_root / "run-context.json", context)
	if args.github_output is not None:
		with args.github_output.open("a", encoding="utf-8", newline="\n") as stream:
			stream.write("candidate_repository=%s\n" % head_repository_name)
			stream.write("candidate_sha=%s\n" % head_sha)
			stream.write("upstream_event=%s\n" % event_name)
	return context


def load_run_context(path: Path) -> Dict[str, Any]:
	context = _read_json(path)
	_require(isinstance(context, dict), "run context must be an object")
	_require(
		set(context) == {"candidate", "repository", "run", "schema", "trusted_harness_sha", "workflow"},
		"unexpected run context fields",
	)
	_require(context.get("schema") == "cocoapdf.opendataloader-run-context/v1", "unexpected run context schema")
	_require(context.get("repository") == "sayantandey/CocoaPDF", "run context repository mismatch")
	workflow = context.get("workflow")
	_require(workflow == {
		"name": "OpenDataLoader benchmark",
		"path": ".github/workflows/opendataloader-benchmark.yml",
	}, "run context workflow mismatch")
	candidate = context.get("candidate")
	run = context.get("run")
	_require(isinstance(candidate, dict) and isinstance(run, dict), "run context is incomplete")
	_require(set(candidate) == {"head_repository", "head_sha", "pull_request_number"}, "unexpected candidate context fields")
	_require(set(run) == {"attempt", "event", "id"}, "unexpected run context fields")
	_require(isinstance(candidate.get("head_sha"), str) and COMMIT_SHA.fullmatch(candidate["head_sha"]) is not None, "invalid context head SHA")
	_require(isinstance(context.get("trusted_harness_sha"), str) and COMMIT_SHA.fullmatch(context["trusted_harness_sha"]) is not None, "invalid trusted harness SHA")
	_require(run.get("event") in {"pull_request", "push"}, "invalid context event")
	_positive_int(run.get("id"), "context run ID")
	_positive_int(run.get("attempt"), "context run attempt")
	if run["event"] == "pull_request":
		_require(candidate.get("pull_request_number") is None, "workflow_run context cannot assert a PR number")
		_require(
			isinstance(candidate.get("head_repository"), str)
			and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", candidate["head_repository"]) is not None,
			"invalid context head repository",
		)
	else:
		_require(candidate.get("pull_request_number") is None, "push context names a pull request")
		_require(candidate.get("head_repository") == "sayantandey/CocoaPDF", "push context repository mismatch")
	return context


def convert_candidate(args: argparse.Namespace) -> bool:
	policy = load_policy(args.policy)
	_require(_sha256(args.adapter) == policy["adapter"]["sha256"], "canonical adapter hash changed")
	if args.output_root.exists() and any(args.output_root.iterdir()):
		raise BenchmarkValidationError("candidate output directory must be empty")
	args.output_root.mkdir(parents=True, exist_ok=True)
	engine_root = args.output_root
	markdown_root = engine_root / "markdown"
	pdf_paths = sorted((args.corpus_root / "pdfs").glob("*.pdf"))
	_require(len(pdf_paths) == policy["benchmark"]["corpus"]["pdf_count"], "candidate container did not receive 200 PDFs")
	for path in pdf_paths:
		_require(path.read_bytes()[:5] == b"%PDF-", "candidate container received an invalid PDF")
	_load_adapter(args.adapter)(pdf_paths, args.corpus_root / "pdfs", markdown_root)
	markdown_paths = sorted(markdown_root.glob("*.md"))
	_require(len(markdown_paths) == 200, "adapter did not write 200 predictions")
	return True


def validate_candidate_output(engine_root: Path, expected_ids: Set[str]) -> Tuple[List[Path], List[Dict[str, str]]]:
	_require(engine_root.is_dir() and not engine_root.is_symlink(), "candidate output is missing")
	children = list(engine_root.iterdir())
	_require({path.name for path in children} == {"markdown", "failures.json"}, "candidate output file allowlist mismatch")
	markdown_root = engine_root / "markdown"
	_require(markdown_root.is_dir() and not markdown_root.is_symlink(), "candidate Markdown directory is invalid")
	markdown_paths = list(markdown_root.iterdir())
	_require(len(markdown_paths) == len(expected_ids), "candidate must write exactly 200 predictions")
	total = 0
	for path in markdown_paths:
		_require(path.parent == markdown_root and path.is_file() and not path.is_symlink(), "candidate output contains a non-regular prediction")
		_require(path.suffix == ".md" and DOCUMENT_ID.fullmatch(path.stem) is not None, "candidate output has an invalid prediction name")
		size = path.stat().st_size
		_require(size <= MAX_PREDICTION_FILE_BYTES, "candidate prediction exceeds its size limit")
		total += size
		try:
			path.read_text(encoding="utf-8")
		except UnicodeDecodeError as exc:
			raise BenchmarkValidationError("candidate prediction is not UTF-8") from exc
	_require(total <= MAX_PREDICTION_TOTAL_BYTES, "candidate predictions exceed their total size limit")
	_require({path.stem for path in markdown_paths} == expected_ids, "prediction IDs differ from corpus")
	failures_path = engine_root / "failures.json"
	_require(failures_path.is_file() and not failures_path.is_symlink(), "candidate metadata is not a regular file")
	_require(failures_path.stat().st_size <= MAX_ARTIFACT_FILE_BYTES, "candidate metadata exceeds its size limit")
	failures_source = _read_json(engine_root / "failures.json")
	_require(isinstance(failures_source, list), "failures.json must contain a list")
	failures: List[Dict[str, str]] = []
	seen_failures: Set[str] = set()
	for failure in failures_source:
		_require(isinstance(failure, dict), "failure record must be an object")
		document = failure.get("document")
		exception_type = failure.get("exception_type")
		_require(
			isinstance(document, str)
			and document.endswith(".pdf")
			and document[:-4] in expected_ids,
			"invalid failure document",
		)
		_require(document not in seen_failures, "duplicate failure document")
		seen_failures.add(document)
		_require(isinstance(exception_type, str) and exception_type, "invalid failure type")
		failures.append({"document": document, "exception_type": exception_type[:120]})
	return sorted(markdown_paths), failures


def stage_candidate_output(args: argparse.Namespace) -> bool:
	"""Copy only validated regular prediction files into the upload directory."""

	policy = load_policy(args.policy)
	document_ids = verify_pdf_corpus(args.corpus_root, policy)
	_require(args.candidate_output.name == "cocoapdf", "candidate output engine name mismatch")
	markdown_paths, failures = validate_candidate_output(args.candidate_output, document_ids)
	if args.artifact_root.exists() and any(args.artifact_root.iterdir()):
		raise BenchmarkValidationError("candidate artifact directory must be empty")
	destination = args.artifact_root / "prediction" / "cocoapdf"
	markdown_root = destination / "markdown"
	markdown_root.mkdir(parents=True, exist_ok=True)
	for source in markdown_paths:
		(markdown_root / source.name).write_bytes(source.read_bytes())
	_write_json(destination / "failures.json", failures)
	_write_json(
		args.artifact_root / "timing.json",
		_timing_document(args.elapsed_nanoseconds, policy),
	)
	validate_candidate_output(destination, document_ids)
	return True


def score_predictions(args: argparse.Namespace) -> bool:
	policy = load_policy(args.policy)
	verify_benchmark(args.benchmark_root, policy)
	document_ids = verify_corpus(args.corpus_root, args.benchmark_root, policy)
	benchmark_root = args.benchmark_root.resolve()
	_require(_sha256(args.adapter) == policy["adapter"]["sha256"], "canonical adapter hash changed")
	context = load_run_context(args.artifact_root / "run-context.json")
	engine_root = args.candidate_output
	markdown_paths, failures = validate_candidate_output(engine_root, document_ids)
	prediction_root = engine_root.parent
	_require(engine_root.name == "cocoapdf" and prediction_root.name == "prediction", "candidate output layout mismatch")
	timing_path = prediction_root.parent / "timing.json"
	_require(timing_path.is_file() and not timing_path.is_symlink(), "trusted worker timing is missing")
	performance = validate_timing_document(_read_json(timing_path), policy)
	environment = {
		"LANG": "C.UTF-8",
		"LC_ALL": "C.UTF-8",
		"PATH": os.environ.get("PATH", os.defpath),
		"PYTHONDONTWRITEBYTECODE": "1",
		"PYTHONHASHSEED": "0",
		"PYTHONPATH": str(benchmark_root / "src"),
		"TZ": "UTC",
	}
	for name in ("SYSTEMROOT", "WINDIR"):
		if name in os.environ:
			environment[name] = os.environ[name]
	subprocess.run(
		[
			sys.executable,
			str(benchmark_root / "src" / "evaluator.py"),
			"--ground-truth-dir",
			str(benchmark_root / "ground-truth" / "markdown"),
			"--prediction-root",
			str(prediction_root.resolve()),
			"--engine",
			"cocoapdf",
		],
		cwd=str(benchmark_root),
		env=environment,
		check=True,
	)
	evaluation_path = engine_root / "evaluation.json"
	evaluation_csv_path = engine_root / "evaluation.csv"
	evaluation = validate_evaluation(_read_json(evaluation_path), policy, expected_ids=document_ids)
	empty = [path.stem for path in markdown_paths if not path.read_text(encoding="utf-8").strip()]
	completeness = {
		"conversion_failures": len(failures),
		"empty_predictions": len(empty),
		"evaluated_documents": len(evaluation["documents"]),
		"missing_predictions": evaluation["metrics"]["missing_predictions"],
		"prediction_files": len(markdown_paths),
	}
	gate = evaluate_gate(evaluation["metrics"], completeness, policy)
	result = {
		"benchmark": {
			"commit": policy["benchmark"]["commit"],
			"tree": policy["benchmark"]["tree"],
		},
		"completeness": completeness,
		"engine": {
			"adapter_sha256": _sha256(args.adapter),
			"head_sha": context["candidate"]["head_sha"],
			"tested_sha": context["candidate"]["head_sha"],
			"trusted_harness_sha": context["trusted_harness_sha"],
			"worker_sha": policy["worker"]["commit"],
		},
		"gate": gate,
		"metrics": evaluation["metrics"],
		"performance": performance,
		"schema": "cocoapdf.opendataloader-ci-result/v2",
	}
	prediction_hashes = _prediction_hashes(markdown_paths)
	provenance = {
		"artifact_excludes": ["source PDFs", "ground truth", "predicted Markdown"],
		"benchmark": {
			"commit": policy["benchmark"]["commit"],
			"corpus": policy["benchmark"]["corpus"],
			"evaluator_files": policy["benchmark"]["evaluator_files"],
			"repository": policy["benchmark"]["repository"],
			"tree": policy["benchmark"]["tree"],
		},
		"engine": result["engine"],
		"environment": {
			"machine": platform.machine(),
			"platform": platform.platform(),
			"python": platform.python_version(),
			"python_implementation": platform.python_implementation(),
		},
		"run": {
			"event": context["run"]["event"],
			"id": context["run"]["id"],
			"attempt": context["run"]["attempt"],
			"cache_hit": args.cache_hit,
			"cache_key": args.cache_key,
		},
		"performance": performance,
		"schema": "cocoapdf.opendataloader-ci-provenance/v2",
	}
	_copy_lf(evaluation_path, args.artifact_root / "evaluation.json")
	_copy_lf(evaluation_csv_path, args.artifact_root / "evaluation.csv")
	_write_json(args.artifact_root / "failures.json", failures)
	_write_json(args.artifact_root / "prediction-hashes.json", prediction_hashes)
	_write_json(args.artifact_root / "provenance.json", provenance)
	_write_json(args.artifact_root / "result.json", result)
	_write_json(args.artifact_root / "timing.json", performance)
	_manifest(args.artifact_root)
	print(json.dumps({"passed": gate["passed"], "scores": evaluation["metrics"]["score"]}, sort_keys=True))
	return bool(gate["passed"])


def _parse_manifest(path: Path) -> Dict[str, str]:
	records: Dict[str, str] = {}
	for line in path.read_text(encoding="utf-8").splitlines():
		match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9.-]+)", line)
		_require(match is not None, "invalid artifact manifest line")
		digest, name = match.groups()
		_require(name not in records, "duplicate artifact manifest entry")
		records[name] = digest
	return records


def validate_artifact_directory(artifact_root: Path, policy: Mapping[str, Any]) -> Dict[str, Any]:
	_require(artifact_root.is_dir() and not artifact_root.is_symlink(), "artifact directory is missing")
	paths = list(artifact_root.rglob("*"))
	for path in paths:
		_require(path.parent == artifact_root, "artifact contains nested paths")
		_require(path.is_file() and not path.is_symlink(), "artifact contains a non-regular file")
	actual = {path.name for path in paths}
	_require(actual == ARTIFACT_FILES, "artifact file allowlist mismatch")
	total = 0
	for path in paths:
		size = path.stat().st_size
		_require(size <= MAX_ARTIFACT_FILE_BYTES, "artifact file exceeds size limit")
		total += size
	_require(total <= MAX_ARTIFACT_TOTAL_BYTES, "artifact exceeds total size limit")
	manifest = _parse_manifest(artifact_root / "manifest.sha256")
	_require(set(manifest) == ARTIFACT_FILES - {"manifest.sha256"}, "manifest file set mismatch")
	for name, digest in manifest.items():
		_require(_sha256(artifact_root / name) == digest, "artifact digest mismatch: %s" % name)
	context = load_run_context(artifact_root / "run-context.json")
	evaluation = validate_evaluation(
		_read_json(artifact_root / "evaluation.json"),
		policy,
		check_baseline_eligibility=True,
	)
	result = _read_json(artifact_root / "result.json")
	_require(isinstance(result, dict), "result must be an object")
	_require(
		set(result) == {"benchmark", "completeness", "engine", "gate", "metrics", "performance", "schema"},
		"unexpected result fields",
	)
	_require(result.get("schema") == "cocoapdf.opendataloader-ci-result/v2", "unexpected result schema")
	_require(result.get("metrics") == evaluation["metrics"], "result/evaluation metrics differ")
	_require(result.get("benchmark") == {
		"commit": policy["benchmark"]["commit"],
		"tree": policy["benchmark"]["tree"],
	}, "result benchmark identity mismatch")
	engine = result.get("engine")
	_require(isinstance(engine, dict), "result engine must be an object")
	_require(
		set(engine) == {"adapter_sha256", "head_sha", "tested_sha", "trusted_harness_sha", "worker_sha"},
		"unexpected result engine fields",
	)
	_require(engine.get("adapter_sha256") == policy["adapter"]["sha256"], "result adapter hash mismatch")
	for name in ("head_sha", "tested_sha", "trusted_harness_sha", "worker_sha"):
		_require(isinstance(engine.get(name), str) and COMMIT_SHA.fullmatch(engine[name]) is not None, "invalid engine %s" % name)
	_require(engine["head_sha"] == engine["tested_sha"], "tested and candidate SHAs differ")
	_require(engine["head_sha"] == context["candidate"]["head_sha"], "result/context candidate SHA mismatch")
	_require(engine["trusted_harness_sha"] == context["trusted_harness_sha"], "result/context harness SHA mismatch")
	_require(engine["worker_sha"] == policy["worker"]["commit"], "result worker SHA mismatch")
	completeness = result.get("completeness")
	_require(isinstance(completeness, dict), "result completeness must be an object")
	_require(
		set(completeness) == {
			"conversion_failures",
			"empty_predictions",
			"evaluated_documents",
			"missing_predictions",
			"prediction_files",
		},
		"unexpected completeness fields",
	)
	recomputed_gate = evaluate_gate(evaluation["metrics"], completeness, policy)
	_require(result.get("gate") == recomputed_gate, "result gate was not computed from trusted policy")
	performance = validate_timing_document(result.get("performance"), policy)
	_require(
		validate_timing_document(_read_json(artifact_root / "timing.json"), policy)
		== performance,
		"timing artifact differs from result",
	)
	failures = _read_json(artifact_root / "failures.json")
	_require(isinstance(failures, list), "failures artifact must be a list")
	_require(len(failures) == completeness.get("conversion_failures"), "failure count mismatch")
	seen_failures: Set[str] = set()
	for failure in failures:
		_require(isinstance(failure, dict) and set(failure) == {"document", "exception_type"}, "invalid failure record")
		document = failure["document"]
		exception_type = failure["exception_type"]
		_require(isinstance(document, str) and document.endswith(".pdf") and DOCUMENT_ID.fullmatch(document[:-4]) is not None, "invalid failure document")
		_require(document not in seen_failures, "duplicate failure document")
		seen_failures.add(document)
		_require(isinstance(exception_type, str) and 0 < len(exception_type) <= 120, "invalid failure exception type")
	predictions = _read_json(artifact_root / "prediction-hashes.json")
	_require(isinstance(predictions, dict), "prediction hashes must be an object")
	_require(
		set(predictions) == {"aggregate_sha256", "document_count", "documents", "schema", "total_bytes"},
		"unexpected prediction hash fields",
	)
	_require(predictions.get("schema") == "cocoapdf.opendataloader-prediction-hashes/v1", "unexpected prediction hash schema")
	_require(predictions.get("document_count") == 200, "prediction hash count mismatch")
	_require(isinstance(predictions.get("total_bytes"), int) and predictions["total_bytes"] >= 0, "invalid prediction byte count")
	_require(isinstance(predictions.get("aggregate_sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", predictions["aggregate_sha256"]) is not None, "invalid aggregate prediction hash")
	documents = predictions.get("documents")
	_require(isinstance(documents, dict) and set(documents) == {item["document_id"] for item in evaluation["documents"]}, "prediction hash IDs differ")
	calculated_bytes = 0
	aggregate = hashlib.sha256()
	for document_id in sorted(documents):
		record = documents[document_id]
		_require(isinstance(record, dict) and set(record) == {"bytes", "sha256"}, "invalid prediction hash record")
		_require(isinstance(record["bytes"], int) and 0 <= record["bytes"] <= MAX_PREDICTION_FILE_BYTES, "invalid prediction size")
		_require(isinstance(record["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is not None, "invalid prediction SHA-256")
		calculated_bytes += record["bytes"]
		aggregate.update(document_id.encode("ascii"))
		aggregate.update(b"\0")
		aggregate.update(str(record["bytes"]).encode("ascii"))
		aggregate.update(b"\0")
		aggregate.update(record["sha256"].encode("ascii"))
		aggregate.update(b"\n")
	_require(calculated_bytes == predictions["total_bytes"], "prediction byte total mismatch")
	_require(calculated_bytes <= MAX_PREDICTION_TOTAL_BYTES, "prediction byte total exceeds policy")
	_require(aggregate.hexdigest() == predictions["aggregate_sha256"], "aggregate prediction hash mismatch")
	with (artifact_root / "evaluation.csv").open("r", encoding="utf-8", newline="") as stream:
		rows = list(csv.DictReader(stream))
	_require(len(rows) == 200, "evaluation CSV must contain 200 rows")
	_require({str(row.get("document_id", "")).removeprefix("'") for row in rows} == set(documents), "evaluation CSV IDs differ")
	provenance = _read_json(artifact_root / "provenance.json")
	_require(isinstance(provenance, dict), "provenance must be an object")
	_require(provenance.get("schema") == "cocoapdf.opendataloader-ci-provenance/v2", "unexpected provenance schema")
	_require(provenance.get("engine") == engine, "provenance engine identity mismatch")
	_require(provenance.get("performance") == performance, "provenance performance differs from result")
	_require(provenance.get("artifact_excludes") == ["source PDFs", "ground truth", "predicted Markdown"], "artifact redistribution policy changed")
	_require(provenance.get("benchmark") == {
		"commit": policy["benchmark"]["commit"],
		"corpus": policy["benchmark"]["corpus"],
		"evaluator_files": policy["benchmark"]["evaluator_files"],
		"repository": policy["benchmark"]["repository"],
		"tree": policy["benchmark"]["tree"],
	}, "provenance benchmark identity mismatch")
	provenance_run = provenance.get("run")
	_require(isinstance(provenance_run, dict), "provenance run must be an object")
	_require(provenance_run.get("event") == context["run"]["event"], "provenance/context event mismatch")
	_require(provenance_run.get("id") == context["run"]["id"], "provenance/context run ID mismatch")
	_require(provenance_run.get("attempt") == context["run"]["attempt"], "provenance/context attempt mismatch")
	return result


def assert_workflow(args: argparse.Namespace) -> bool:
	_require(args.benchmark_outcome == "success", "benchmark step did not succeed")
	_require(args.upload_outcome == "success", "artifact upload did not succeed")
	result = validate_artifact_directory(args.artifact_root, load_policy(args.policy))
	_require(result["gate"]["passed"] is True, "benchmark policy gate failed")
	return True


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
	parser = argparse.ArgumentParser(description=__doc__)
	subparsers = parser.add_subparsers(dest="command", required=True)

	verify = subparsers.add_parser("verify-corpus")
	verify.add_argument("--benchmark-root", type=Path, required=True)
	verify.add_argument("--corpus-root", type=Path, required=True)
	verify.add_argument("--policy", type=Path, default=DEFAULT_POLICY)

	verify_pdfs = subparsers.add_parser("verify-pdf-corpus")
	verify_pdfs.add_argument("--corpus-root", type=Path, required=True)
	verify_pdfs.add_argument("--policy", type=Path, default=DEFAULT_POLICY)

	boundary = subparsers.add_parser("verify-boundary")
	boundary.add_argument("--trusted-root", type=Path, required=True)
	boundary.add_argument("--candidate-root", type=Path, required=True)

	boundary_ref = subparsers.add_parser("verify-boundary-ref")
	boundary_ref.add_argument("--trusted-root", type=Path, required=True)
	boundary_ref.add_argument("--candidate-repository", required=True)
	boundary_ref.add_argument("--candidate-sha", required=True)

	download = subparsers.add_parser("download-candidate-artifact")
	download.add_argument("--context", type=Path, required=True)
	download.add_argument("--destination", type=Path, required=True)

	context = subparsers.add_parser("init-context")
	context.add_argument("--artifact-root", type=Path, required=True)
	context.add_argument("--event-json", type=Path, required=True)
	context.add_argument("--github-output", type=Path)
	context.add_argument("--trusted-sha", required=True)

	convert = subparsers.add_parser("convert")
	convert.add_argument("--corpus-root", type=Path, required=True)
	convert.add_argument("--output-root", type=Path, required=True)
	convert.add_argument("--adapter", type=Path, default=ADAPTER_PATH)
	convert.add_argument("--policy", type=Path, default=DEFAULT_POLICY)

	stage = subparsers.add_parser("stage-candidate")
	stage.add_argument("--corpus-root", type=Path, required=True)
	stage.add_argument("--candidate-output", type=Path, required=True)
	stage.add_argument("--artifact-root", type=Path, required=True)
	stage.add_argument("--elapsed-nanoseconds", type=int, required=True)
	stage.add_argument("--policy", type=Path, default=DEFAULT_POLICY)

	score = subparsers.add_parser("score")
	score.add_argument("--benchmark-root", type=Path, required=True)
	score.add_argument("--corpus-root", type=Path, required=True)
	score.add_argument("--candidate-output", type=Path, required=True)
	score.add_argument("--artifact-root", type=Path, required=True)
	score.add_argument("--adapter", type=Path, default=ADAPTER_PATH)
	score.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
	score.add_argument("--cache-hit", choices=("true", "false"), required=True)
	score.add_argument("--cache-key", required=True)

	workflow = subparsers.add_parser("assert-workflow")
	workflow.add_argument("--artifact-root", type=Path, required=True)
	workflow.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
	workflow.add_argument("--benchmark-outcome", choices=("success", "failure", "cancelled", "skipped"), required=True)
	workflow.add_argument("--upload-outcome", choices=("success", "failure", "cancelled", "skipped"), required=True)
	return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
	args = _parse_args(argv)
	try:
		if args.command == "verify-corpus":
			policy = load_policy(args.policy)
			verify_benchmark(args.benchmark_root, policy)
			verify_corpus(args.corpus_root, args.benchmark_root, policy)
			print("verified pinned 200-document OpenDataLoader corpus")
			return 0
		if args.command == "verify-pdf-corpus":
			verify_pdf_corpus(args.corpus_root, load_policy(args.policy))
			print("verified pinned 200-document OpenDataLoader PDF corpus")
			return 0
		if args.command == "verify-boundary":
			verify_privileged_boundary(args.trusted_root, args.candidate_root)
			print("verified candidate did not change the privileged CI boundary")
			return 0
		if args.command == "verify-boundary-ref":
			verify_privileged_boundary_ref(
				args.trusted_root,
				args.candidate_repository,
				args.candidate_sha,
				os.environ.get("GH_TOKEN", ""),
			)
			print("verified candidate did not change the privileged CI boundary")
			return 0
		if args.command == "download-candidate-artifact":
			artifact = download_candidate_artifact(
				args.context,
				args.destination,
				os.environ.get("GH_TOKEN", ""),
			)
			print("downloaded validated candidate artifact %s" % artifact["id"])
			return 0
		if args.command == "init-context":
			write_run_context(args)
			return 0
		if args.command == "convert":
			return 0 if convert_candidate(args) else 1
		if args.command == "stage-candidate":
			return 0 if stage_candidate_output(args) else 1
		if args.command == "score":
			return 0 if score_predictions(args) else 1
		if args.command == "assert-workflow":
			assert_workflow(args)
			return 0
		raise AssertionError("unreachable command")
	except (BenchmarkValidationError, OSError, subprocess.SubprocessError) as exc:
		print("OpenDataLoader CI validation failed: %s" % exc, file=sys.stderr)
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
