from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from unittest.mock import patch

from tools.update_pr_body_block import MarkerError, StaleHeadError, payload_for_pull_request, replace_owned_block
from validation.benchmarks.opendataloader_bench.ci_runner import (
	BenchmarkValidationError,
	MAX_CANDIDATE_ARTIFACT_BYTES,
	MAX_PREDICTION_FILE_BYTES,
	PRIVILEGED_BOUNDARY_PATHS,
	candidate_artifact_name,
	download_candidate_artifact,
	evaluate_gate,
	load_policy,
	score_predictions,
	validate_candidate_output,
	verify_corpus,
	verify_privileged_boundary,
	verify_privileged_boundary_ref,
	write_run_context,
)
from validation.benchmarks.opendataloader_bench.report import (
	_check_payload,
	_is_latest_run,
	_report_state,
	_report_markdown,
	_trusted_artifact_metadata,
	badge_document,
	badge_provenance_document,
	mark_main_pending,
	publish_badge,
	publish_main_badge,
	report_pull_request_body,
	report_pull_request_check,
)
from validation.benchmarks.opendataloader_bench.adapter import to_markdown
from validation.pr_visual.report import _artifact as visual_artifact
from validation.pr_visual.report import _is_latest as visual_is_latest


ROOT = Path(__file__).resolve().parents[1]
SHA = "1" * 40
TRUSTED_SHA = "2" * 40


def workflow_event(
	*,
	action: str = "completed",
	event_name: str = "pull_request",
	run_id: int = 101,
	attempt: int = 1,
	head_sha: str = SHA,
	workflow_name: str = "OpenDataLoader benchmark",
	workflow_path: str = ".github/workflows/opendataloader-benchmark.yml",
) -> Dict[str, Any]:
	return {
		"action": action,
		"repository": {"full_name": "sayantandey/CocoaPDF"},
		"workflow_run": {
			"conclusion": "success",
			"event": event_name,
			"head_branch": "main" if event_name == "push" else "feature",
			"head_repository": {"full_name": "sayantandey/CocoaPDF", "id": 12345},
			"head_sha": head_sha,
			"id": run_id,
			"name": workflow_name,
			"path": workflow_path + "@refs/heads/main",
			"run_attempt": attempt,
		},
	}


class FakeApi:
	repository = "sayantandey/CocoaPDF"

	def __init__(self, responses: Optional[Mapping[str, Any]] = None) -> None:
		self.responses = dict(responses or {})
		self.writes: List[tuple] = []

	def get(self, endpoint: str, *, allow_not_found: bool = False) -> Any:
		for marker, value in self.responses.items():
			if marker in endpoint:
				return value(endpoint) if callable(value) else value
		if allow_not_found:
			return None
		raise AssertionError("unexpected GET %s" % endpoint)

	def post(self, endpoint: str, payload: Mapping[str, Any]) -> Any:
		self.writes.append(("POST", endpoint, dict(payload)))
		return {}

	def patch(self, endpoint: str, payload: Mapping[str, Any]) -> Any:
		self.writes.append(("PATCH", endpoint, dict(payload)))
		return {}

	def put(self, endpoint: str, payload: Mapping[str, Any]) -> Any:
		self.writes.append(("PUT", endpoint, dict(payload)))
		return {}


class BenchmarkPolicyTests(unittest.TestCase):
	def test_trusted_scorer_uses_absolute_evaluator_paths(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			benchmark_root = root / "benchmark"
			benchmark_root.mkdir()
			engine_root = root / "prediction" / "cocoapdf"
			prediction = engine_root / "markdown" / "001.md"
			prediction.parent.mkdir(parents=True)
			prediction.write_text("prediction\n", encoding="utf-8")
			args = argparse.Namespace(
				adapter=root / "adapter.py",
				artifact_root=root / "artifact",
				benchmark_root=Path("benchmark"),
				cache_hit="false",
				cache_key="cache-key",
				candidate_output=engine_root,
				corpus_root=root / "corpus",
				policy=root / "policy.json",
			)
			policy = {
				"adapter": {"sha256": "adapter-sha"},
				"benchmark": {
					"commit": SHA,
					"corpus": {},
					"evaluator_files": {},
					"repository": "example/benchmark",
					"tree": TRUSTED_SHA,
				},
				"worker": {"commit": SHA},
			}
			evaluation = {
				"documents": [{"id": "001"}],
				"metrics": {"missing_predictions": 0, "score": {"overall_mean": 0.9}},
			}
			context = {
				"candidate": {"head_sha": SHA},
				"run": {"attempt": 1, "event": "push", "id": 1},
				"trusted_harness_sha": TRUSTED_SHA,
			}
			old_cwd = Path.cwd()
			try:
				os.chdir(root)
				with patch("validation.benchmarks.opendataloader_bench.ci_runner.load_policy", return_value=policy), \
					patch("validation.benchmarks.opendataloader_bench.ci_runner.verify_benchmark"), \
					patch("validation.benchmarks.opendataloader_bench.ci_runner.verify_corpus", return_value={"001"}), \
					patch("validation.benchmarks.opendataloader_bench.ci_runner._sha256", return_value="adapter-sha"), \
					patch("validation.benchmarks.opendataloader_bench.ci_runner.load_run_context", return_value=context), \
					patch("validation.benchmarks.opendataloader_bench.ci_runner.validate_candidate_output", return_value=([prediction], [])), \
					patch("validation.benchmarks.opendataloader_bench.ci_runner._read_json", return_value={}), \
					patch("validation.benchmarks.opendataloader_bench.ci_runner.validate_evaluation", return_value=evaluation), \
					patch("validation.benchmarks.opendataloader_bench.ci_runner.evaluate_gate", return_value={"passed": True}), \
					patch("validation.benchmarks.opendataloader_bench.ci_runner._copy_lf"), \
					patch("validation.benchmarks.opendataloader_bench.ci_runner._write_json"), \
					patch("validation.benchmarks.opendataloader_bench.ci_runner._manifest"), \
					patch("validation.benchmarks.opendataloader_bench.ci_runner.platform.machine", return_value="test"), \
					patch("validation.benchmarks.opendataloader_bench.ci_runner.platform.platform", return_value="test"), \
					patch("validation.benchmarks.opendataloader_bench.ci_runner.platform.python_version", return_value="test"), \
					patch("validation.benchmarks.opendataloader_bench.ci_runner.platform.python_implementation", return_value="test"), \
					patch("validation.benchmarks.opendataloader_bench.ci_runner.subprocess.run") as run:
					self.assertTrue(score_predictions(args))
			finally:
				os.chdir(old_cwd)
			command = run.call_args.args[0]
			options = run.call_args.kwargs
			self.assertEqual(Path(command[1]), benchmark_root / "src" / "evaluator.py")
			self.assertEqual(Path(options["cwd"]), benchmark_root)
			self.assertEqual(Path(options["env"]["PYTHONPATH"]), benchmark_root / "src")

	def test_adapter_does_not_export_unscored_image_assets(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			# A regular file makes the legacy convert_file() path fail on every OS
			# when it tries to create the default relative assets directory.
			(root / "assets").write_bytes(b"sentinel")
			output = root / "prediction" / "cocoapdf" / "markdown"
			fixture = ROOT / "examples/cases/scope_and_adversarial/input.pdf"
			old_cwd = Path.cwd()
			try:
				os.chdir(root)
				to_markdown([fixture], fixture.parent, output)
			finally:
				os.chdir(old_cwd)
			prediction = output / "input.md"
			self.assertTrue(prediction.read_text(encoding="utf-8").strip())
			self.assertEqual(
				json.loads((output.parent / "failures.json").read_text(encoding="utf-8")),
				[],
			)
			self.assertEqual((root / "assets").read_bytes(), b"sentinel")

	def test_exact_published_baseline_and_regression_gates(self):
		policy = load_policy()
		self.assertEqual(policy["benchmark"]["corpus"]["ground_truth_bytes"], 428917)
		self.assertEqual(
			policy["benchmark"]["evaluator_files"]["src/converter_markdown_table.py"],
			"48879a0a5033e26a0f59a34ebcaf29dc322f4947e019955767f38adfdf0b63c1",
		)
		self.assertEqual(policy["baseline"]["scores"]["overall_mean"], 0.869665721357887)
		self.assertEqual(policy["worker"]["commit"], "4a9a8f766da4b90f6f1f5f48c77d03456b9cd9b2")
		self.assertEqual(policy["gates"]["overall_floor"], 0.8)
		self.assertEqual(
			policy["gates"]["component_floors"],
			{"nid_mean": 0.8, "teds_mean": 0.8, "mhs_mean": 0.8},
		)
		self.assertEqual(policy["gates"]["material_regression"], 0.001)
		scores = dict(policy["baseline"]["scores"])
		scores.update({"nid_s_mean": 0.0, "teds_s_mean": 0.0, "mhs_s_mean": 0.0})
		metrics = {
			"score": scores,
			"nid_count": 200,
			"teds_count": 42,
			"mhs_count": 107,
			"missing_predictions": 0,
		}
		complete = {
			"conversion_failures": 0,
			"empty_predictions": 0,
			"evaluated_documents": 200,
			"missing_predictions": 0,
			"prediction_files": 200,
		}
		self.assertTrue(evaluate_gate(metrics, complete, policy)["passed"])
		for name in policy["gates"]["primary_scores"]:
			regressed = json.loads(json.dumps(metrics))
			regressed["score"][name] = policy["baseline"]["scores"][name] - 0.0011
			self.assertFalse(evaluate_gate(regressed, complete, policy)["passed"], name)
		floor_policy = json.loads(json.dumps(policy))
		component_names = ("nid_mean", "teds_mean", "mhs_mean")
		floor_policy["gates"]["component_floors"] = {
			name: metrics["score"][name] for name in component_names
		}
		self.assertTrue(evaluate_gate(metrics, complete, floor_policy)["passed"])
		for name in component_names:
			below_floor = json.loads(json.dumps(metrics))
			below_floor["score"][name] -= 0.0001
			gate = evaluate_gate(below_floor, complete, floor_policy)
			self.assertFalse(gate["passed"], name)
			self.assertIn("%s is below its absolute floor" % name, gate["failures"])
		incomplete = dict(complete)
		incomplete["prediction_files"] = 199
		self.assertFalse(evaluate_gate(metrics, incomplete, policy)["passed"])

	def test_context_authenticates_default_workflow_identity(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			event_path = root / "event.json"
			event_path.write_text(json.dumps(workflow_event()), encoding="utf-8")
			args = argparse.Namespace(
				artifact_root=root / "artifact",
				event_json=event_path,
				github_output=root / "output",
				trusted_sha=TRUSTED_SHA,
			)
			context = write_run_context(args)
			self.assertEqual(context["candidate"]["head_sha"], SHA)
			self.assertEqual(context["trusted_harness_sha"], TRUSTED_SHA)
			malicious = workflow_event(workflow_path=".github/workflows/other.yml")
			event_path.write_text(json.dumps(malicious), encoding="utf-8")
			args.artifact_root = root / "rejected"
			with self.assertRaisesRegex(BenchmarkValidationError, "workflow path"):
				write_run_context(args)
			failed = workflow_event()
			failed["workflow_run"]["conclusion"] = "cancelled"
			event_path.write_text(json.dumps(failed), encoding="utf-8")
			args.artifact_root = root / "cancelled"
			with self.assertRaisesRegex(BenchmarkValidationError, "successful trigger"):
				write_run_context(args)

	def test_privileged_boundary_is_byte_identical_or_requires_bootstrap(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			trusted = root / "trusted"
			candidate = root / "candidate"
			for relative in PRIVILEGED_BOUNDARY_PATHS:
				for checkout in (trusted, candidate):
					path = checkout / relative
					path.parent.mkdir(parents=True, exist_ok=True)
					path.write_text("trusted\n", encoding="utf-8")
			verify_privileged_boundary(trusted, candidate)
			(candidate / PRIVILEGED_BOUNDARY_PATHS[-1]).write_text("changed\n", encoding="utf-8")
			with self.assertRaisesRegex(BenchmarkValidationError, "privileged boundary"):
				verify_privileged_boundary(trusted, candidate)

	def test_remote_boundary_uses_regular_git_blobs_without_candidate_checkout(self):
		with tempfile.TemporaryDirectory() as directory:
			trusted = Path(directory) / "trusted"
			content = b"trusted\n"
			for relative in PRIVILEGED_BOUNDARY_PATHS:
				path = trusted / relative
				path.parent.mkdir(parents=True, exist_ok=True)
				path.write_bytes(content)
			blob_sha = "3" * 40
			tree_sha = "4" * 40
			entries = [
				{"mode": "100644", "path": relative, "sha": blob_sha, "size": len(content), "type": "blob"}
				for relative in PRIVILEGED_BOUNDARY_PATHS
			]

			def api_get(endpoint: str):
				if "/git/commits/" in endpoint:
					return {"sha": SHA, "tree": {"sha": tree_sha}}
				if "/git/trees/" in endpoint:
					return {"tree": entries, "truncated": False}
				if "/git/blobs/" in endpoint:
					return {
						"content": base64.b64encode(content).decode("ascii"),
						"encoding": "base64",
						"sha": blob_sha,
						"size": len(content),
					}
				raise AssertionError(endpoint)

			verify_privileged_boundary_ref(
				trusted,
				"sayantandey/CocoaPDF",
				SHA,
				"token",
				api_get=api_get,
			)
			entries[-1]["mode"] = "120000"
			with self.assertRaisesRegex(BenchmarkValidationError, "not a regular file"):
				verify_privileged_boundary_ref(
					trusted,
					"sayantandey/CocoaPDF",
					SHA,
					"token",
					api_get=api_get,
				)


class CandidateOutputBoundaryTests(unittest.TestCase):
	def _candidate(self, root: Path) -> tuple[Path, set[str]]:
		engine = root / "cocoapdf"
		markdown = engine / "markdown"
		markdown.mkdir(parents=True)
		ids = {"%014d" % index for index in range(200)}
		for document_id in ids:
			(markdown / (document_id + ".md")).write_text("x\n", encoding="utf-8")
		(engine / "failures.json").write_text("[]\n", encoding="utf-8")
		return engine, ids

	def test_nested_and_oversize_predictions_are_rejected(self):
		with tempfile.TemporaryDirectory() as directory:
			engine, ids = self._candidate(Path(directory))
			victim = engine / "markdown" / (sorted(ids)[0] + ".md")
			victim.unlink()
			victim.mkdir()
			with self.assertRaisesRegex(BenchmarkValidationError, "non-regular"):
				validate_candidate_output(engine, ids)
		with tempfile.TemporaryDirectory() as directory:
			engine, ids = self._candidate(Path(directory))
			victim = engine / "markdown" / (sorted(ids)[0] + ".md")
			victim.write_bytes(b"x" * (MAX_PREDICTION_FILE_BYTES + 1))
			with self.assertRaisesRegex(BenchmarkValidationError, "size limit"):
				validate_candidate_output(engine, ids)

	def test_prediction_symlink_is_rejected_when_platform_supports_it(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			engine, ids = self._candidate(root)
			victim = engine / "markdown" / (sorted(ids)[0] + ".md")
			target = root / "target.md"
			target.write_text("x", encoding="utf-8")
			victim.unlink()
			try:
				victim.symlink_to(target)
			except OSError as exc:
				self.skipTest("symlink creation is unavailable: %s" % exc)
			with self.assertRaisesRegex(BenchmarkValidationError, "non-regular"):
				validate_candidate_output(engine, ids)

	def test_candidate_timing_metadata_is_rejected_not_published(self):
		with tempfile.TemporaryDirectory() as directory:
			engine, ids = self._candidate(Path(directory))
			(engine / "summary.json").write_text(
				json.dumps({"total_elapsed": 0.000001, "elapsed_per_doc": 0.0}),
				encoding="utf-8",
			)
			with self.assertRaisesRegex(BenchmarkValidationError, "allowlist"):
				validate_candidate_output(engine, ids)

	def test_poisoned_corpus_extra_entry_and_symlink_are_rejected(self):
		policy = load_policy()
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			pdfs = root / "corpus" / "pdfs"
			pdfs.mkdir(parents=True)
			(pdfs / "unexpected.txt").write_text("poison", encoding="utf-8")
			with self.assertRaisesRegex(BenchmarkValidationError, "unexpected or non-regular"):
				verify_corpus(root / "corpus", root / "benchmark", policy)
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			pdfs = root / "corpus" / "pdfs"
			pdfs.mkdir(parents=True)
			target = root / "target.pdf"
			target.write_bytes(b"%PDF-1.4\n")
			try:
				(pdfs / "00000000000000.pdf").symlink_to(target)
			except OSError as exc:
				self.skipTest("symlink creation is unavailable: %s" % exc)
			with self.assertRaisesRegex(BenchmarkValidationError, "unexpected or non-regular"):
				verify_corpus(root / "corpus", root / "benchmark", policy)

	def test_ground_truth_extra_entry_is_rejected_before_scoring(self):
		policy = load_policy()
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			pdfs = root / "corpus" / "pdfs"
			truth = root / "benchmark" / "ground-truth" / "markdown"
			pdfs.mkdir(parents=True)
			truth.mkdir(parents=True)
			for index in range(200):
				document_id = "%014d" % index
				(pdfs / (document_id + ".pdf")).write_bytes(b"%PDF-1.4\n")
				(truth / (document_id + ".md")).write_text("x", encoding="utf-8")
			(truth / "unexpected.txt").write_text("poison", encoding="utf-8")
			with self.assertRaisesRegex(BenchmarkValidationError, "ground truth contains"):
				verify_corpus(root / "corpus", root / "benchmark", policy)


class CandidateArtifactDownloadTests(unittest.TestCase):
	def _archive(self, extra: Optional[zipfile.ZipInfo] = None, extra_data: bytes = b"") -> bytes:
		stream = io.BytesIO()
		with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
			archive.writestr("prediction/cocoapdf/failures.json", "[]\n")
			for index in range(200):
				archive.writestr(
					"prediction/cocoapdf/markdown/%014d.md" % index,
					"document %d\n" % index,
				)
			if extra is not None:
				archive.writestr(extra, extra_data)
		return stream.getvalue()

	def _download(
		self,
		root: Path,
		data: bytes,
		*,
		digest: Optional[str] = None,
		size: Optional[int] = None,
		worker_sha: Optional[str] = None,
	):
		event_path = root / "event.json"
		event_path.write_text(json.dumps(workflow_event()), encoding="utf-8")
		artifact_root = root / "trusted-artifact"
		context = write_run_context(
			argparse.Namespace(
				artifact_root=artifact_root,
				event_json=event_path,
				github_output=None,
				trusted_sha=TRUSTED_SHA,
			)
		)
		artifact = {
			"archive_download_url": "https://api.github.com/repos/sayantandey/CocoaPDF/actions/artifacts/9/zip",
			"digest": digest or "sha256:" + hashlib.sha256(data).hexdigest(),
			"expired": False,
			"id": 9,
			"name": candidate_artifact_name(context),
			"size_in_bytes": len(data) if size is None else size,
			"workflow_run": {"id": context["run"]["id"]},
		}

		def api_get(endpoint: str):
			if endpoint.endswith("/actions/runs/101"):
				worker = load_policy()["worker"]
				reported_worker_sha = worker_sha or worker["commit"]
				return {
					"conclusion": "success",
					"event": context["run"]["event"],
					"head_repository": {"full_name": context["candidate"]["head_repository"]},
					"head_sha": context["candidate"]["head_sha"],
					"id": context["run"]["id"],
					"name": context["workflow"]["name"],
					"path": context["workflow"]["path"],
					"referenced_workflows": [
						{
							"path": "%s/%s@%s" % (worker["repository"], worker["path"], reported_worker_sha),
							"sha": reported_worker_sha,
						}
					],
					"run_attempt": context["run"]["attempt"],
				}
			self.assertIn("/actions/runs/101/artifacts", endpoint)
			return {"artifacts": [artifact], "total_count": 1}

		def archive_get(url: str, token: str, maximum: int) -> bytes:
			self.assertEqual(url, artifact["archive_download_url"])
			self.assertEqual(token, "token")
			self.assertEqual(maximum, MAX_CANDIDATE_ARTIFACT_BYTES)
			return data

		destination = root / "candidate"
		result = download_candidate_artifact(
			artifact_root / "run-context.json",
			destination,
			"token",
			api_get=api_get,
			archive_get=archive_get,
		)
		return destination, result

	def test_download_binds_run_digest_and_exact_prediction_allowlist(self):
		with tempfile.TemporaryDirectory() as directory:
			destination, artifact = self._download(Path(directory), self._archive())
			self.assertEqual(artifact["id"], 9)
			markdown = destination / "prediction" / "cocoapdf" / "markdown"
			self.assertEqual(len(list(markdown.glob("*.md"))), 200)

	def test_download_rejects_digest_traversal_symlink_and_oversize_metadata(self):
		with tempfile.TemporaryDirectory() as directory:
			with self.assertRaisesRegex(BenchmarkValidationError, "pinned worker"):
				self._download(Path(directory), self._archive(), worker_sha="9" * 40)

		with tempfile.TemporaryDirectory() as directory:
			with self.assertRaisesRegex(BenchmarkValidationError, "digest mismatch"):
				self._download(Path(directory), self._archive(), digest="sha256:" + "0" * 64)

		traversal = zipfile.ZipInfo("../escape.md")
		with tempfile.TemporaryDirectory() as directory:
			with self.assertRaisesRegex(BenchmarkValidationError, "unsafe or duplicate path"):
				self._download(Path(directory), self._archive(traversal, b"escape"))

		symlink = zipfile.ZipInfo("prediction/cocoapdf/markdown/99999999999999.md")
		symlink.create_system = 3
		symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
		with tempfile.TemporaryDirectory() as directory:
			with self.assertRaisesRegex(BenchmarkValidationError, "symbolic link"):
				self._download(Path(directory), self._archive(symlink, b"target"))

		with tempfile.TemporaryDirectory() as directory:
			with self.assertRaisesRegex(BenchmarkValidationError, "size limit"):
				self._download(
					Path(directory),
					self._archive(),
					size=MAX_CANDIDATE_ARTIFACT_BYTES + 1,
				)


class ReporterSecurityTests(unittest.TestCase):
	def test_incomplete_failed_evaluation_is_failed_but_success_claim_is_unverified(self):
		artifact = {
			"digest": "sha256:" + "a" * 64,
			"expired": False,
			"id": 9,
			"name": "cocoapdf-odl-trusted-900-1",
			"workflow_run": {"id": 900},
		}
		api = FakeApi({"/artifacts?": {"artifacts": [artifact]}})
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "run-context.json").write_text("{}\n", encoding="utf-8")
			args = argparse.Namespace(
				artifact_root=root,
				download_outcome="success",
				evaluation_outcome="failure",
				policy=ROOT / "validation/benchmarks/opendataloader_bench/policy.json",
				trusted_run_id=900,
				trusted_run_attempt=1,
			)
			run = workflow_event(event_name="push")["workflow_run"]
			failed = _report_state(args, run, api)
			self.assertEqual(failed["state"], "failed")
			self.assertIn("before complete evidence", failed["reason"])
			args.evaluation_outcome = "success"
			self.assertEqual(_report_state(args, run, api)["state"], "unverified")

	def test_successfully_published_red_badge_does_not_create_a_second_failure(self):
		with tempfile.TemporaryDirectory() as directory:
			event_path = Path(directory) / "event.json"
			event_path.write_text(
				json.dumps(workflow_event(event_name="push")),
				encoding="utf-8",
			)
			api = FakeApi({"/git/ref/heads/main": {"object": {"sha": SHA}}})
			args = argparse.Namespace(
				artifact_root=Path(directory),
				download_outcome="success",
				evaluation_outcome="failure",
				event_json=event_path,
				policy=ROOT / "validation/benchmarks/opendataloader_bench/policy.json",
				trusted_run_id=900,
				trusted_run_attempt=1,
			)
			state = {
				"artifact": None,
				"reason": "trusted evaluation failed",
				"result": None,
				"state": "failed",
				"trusted_run_id": 900,
			}
			with patch(
				"validation.benchmarks.opendataloader_bench.report._report_state",
				return_value=state,
			), patch(
				"validation.benchmarks.opendataloader_bench.report.publish_badge_bundle",
				return_value=True,
			):
				self.assertEqual(publish_main_badge(args, api), 0)
				state["state"] = "unverified"
				self.assertEqual(publish_main_badge(args, api), 1)

	def test_main_badge_is_marked_pending_when_conversion_is_requested(self):
		with tempfile.TemporaryDirectory() as directory:
			event_path = Path(directory) / "event.json"
			event_path.write_text(
				json.dumps(workflow_event(action="requested", event_name="push")),
				encoding="utf-8",
			)
			api = FakeApi({"/git/ref/heads/main": {"object": {"sha": SHA}}})
			args = argparse.Namespace(
				event_json=event_path,
				policy=ROOT / "validation/benchmarks/opendataloader_bench/policy.json",
				trusted_run_id=900,
				trusted_run_attempt=1,
			)
			with patch(
				"validation.benchmarks.opendataloader_bench.report.publish_badge",
				return_value=True,
			) as publish:
				self.assertEqual(mark_main_pending(args, api), 0)
			publish.assert_called_once()

	def test_old_attempt_is_not_latest_for_odl_or_visual(self):
		run = workflow_event(attempt=1)["workflow_run"]
		latest = dict(run, run_attempt=2)
		api = FakeApi({"/runs?": {"workflow_runs": [latest]}})
		self.assertFalse(_is_latest_run(api, run))
		visual_run = workflow_event(
			attempt=1,
			workflow_name="PR visual validation",
			workflow_path=".github/workflows/pr-visual-validation.yml",
		)["workflow_run"]
		visual_latest = dict(visual_run, run_attempt=2)
		api = FakeApi({"/runs?": {"workflow_runs": [visual_latest]}})
		self.assertFalse(visual_is_latest(api, visual_run))

	def test_stale_current_pr_head_causes_no_write(self):
		with tempfile.TemporaryDirectory() as directory:
			event_path = Path(directory) / "event.json"
			event_path.write_text(json.dumps(workflow_event()), encoding="utf-8")
			pr = {
				"number": 11,
				"state": "open",
				"base": {"ref": "main", "repo": {"full_name": "sayantandey/CocoaPDF"}},
				"head": {"sha": SHA, "repo": {"full_name": "sayantandey/CocoaPDF"}},
			}
			current = {"number": 11, "state": "open", "body": "", "head": {"sha": "3" * 40}}
			api = FakeApi(
				{
					"/runs?": {"workflow_runs": [workflow_event()["workflow_run"]]},
					"/commits/": [pr],
					"/pulls/11": current,
				}
			)
			args = argparse.Namespace(event_json=event_path)
			with patch(
				"validation.benchmarks.opendataloader_bench.report._report_state",
				return_value={"artifact": None, "reason": None, "result": None, "state": "passed", "trusted_run_id": 900},
			):
				self.assertEqual(report_pull_request_check(args, api), 0)
			self.assertEqual(api.writes, [])

	def test_malformed_body_markers_cannot_affect_authoritative_check(self):
		with tempfile.TemporaryDirectory() as directory:
			event_path = Path(directory) / "event.json"
			event_path.write_text(json.dumps(workflow_event()), encoding="utf-8")
			pr = {
				"number": 11,
				"state": "open",
				"base": {"ref": "main", "repo": {"full_name": "sayantandey/CocoaPDF"}},
				"head": {"sha": SHA, "repo": {"full_name": "sayantandey/CocoaPDF"}},
			}
			current = {
				"number": 11,
				"state": "open",
				"body": "<!-- cocoapdf-odl:start -->",
				"head": {"sha": SHA},
			}
			api = FakeApi(
				{
					"/runs?": {"workflow_runs": [workflow_event()["workflow_run"]]},
					"/commits/": [pr],
					"/pulls/11": current,
				}
			)
			args = argparse.Namespace(event_json=event_path)
			state = {"artifact": None, "reason": None, "result": None, "state": "passed", "trusted_run_id": 900}
			with patch(
				"validation.benchmarks.opendataloader_bench.report._report_state",
				return_value=state,
			), patch("validation.benchmarks.opendataloader_bench.report._upsert_check") as upsert:
				self.assertEqual(report_pull_request_body(args, api), 1)
			upsert.assert_not_called()
			self.assertEqual(api.writes, [])

	def test_artifact_metadata_rejects_wrong_run_digest_name_and_size(self):
		base = {
			"digest": "sha256:" + "a" * 64,
			"expired": False,
			"id": 9,
			"name": "cocoapdf-odl-trusted-700-2",
			"workflow_run": {"id": 700},
		}
		for mutation in (
			{"name": "wrong"},
			{"digest": "sha256:nope"},
			{"workflow_run": {"id": 701}},
		):
			artifact = dict(base)
			artifact.update(mutation)
			api = FakeApi({"/artifacts?": {"artifacts": [artifact]}})
			with self.assertRaises(BenchmarkValidationError):
				_trusted_artifact_metadata(api, 700, 2)

		visual_run = workflow_event(
			workflow_name="PR visual validation",
			workflow_path=".github/workflows/pr-visual-validation.yml",
		)["workflow_run"]
		visual = {
			"digest": "sha256:" + "b" * 64,
			"expired": False,
			"id": 10,
			"name": "cocoapdf-pr-visual-pr-11-101-1",
			"size_in_bytes": 100 * 1024 * 1024 + 1,
			"workflow_run": {"id": 101},
		}
		api = FakeApi({"/artifacts?": {"artifacts": [visual]}})
		with self.assertRaisesRegex(BenchmarkValidationError, "size"):
			visual_artifact(api, visual_run, 11)

	def test_badge_ref_change_before_put_prevents_stale_write(self):
		class BadgeApi(FakeApi):
			def get(self, endpoint: str, *, allow_not_found: bool = False) -> Any:
				if "/git/ref/heads/odl-badge" in endpoint:
					return {"object": {"sha": "4" * 40}}
				if "/contents/badges/opendataloader.json" in endpoint:
					return None
				if "/git/ref/heads/main" in endpoint:
					return {"object": {"sha": "5" * 40}}
				raise AssertionError(endpoint)

		api = BadgeApi()
		self.assertFalse(publish_badge(api, SHA, badge_document("passed", {"metrics": {"score": {"overall_mean": 0.9}}})))
		self.assertEqual(api.writes, [])

	def test_check_and_evidence_links_use_validated_trusted_run(self):
		run = workflow_event()["workflow_run"]
		state = {
			"artifact": {"digest": "sha256:" + "a" * 64, "id": 77},
			"reason": None,
			"result": None,
			"state": "passed",
			"trusted_run_id": 900,
		}
		markdown = _report_markdown(state, run, "sayantandey/CocoaPDF")
		self.assertIn("/actions/runs/900/artifacts/77", markdown)
		payload = _check_payload(state, run, "sayantandey/CocoaPDF")
		self.assertEqual(payload["details_url"], "https://github.com/sayantandey/CocoaPDF/actions/runs/900")

	def test_shields_document_stays_minimal_and_provenance_is_separate(self):
		badge = badge_document("passed", {"metrics": {"score": {"overall_mean": 0.9}}})
		self.assertEqual(set(badge), {"cacheSeconds", "color", "label", "message", "schemaVersion"})
		run = workflow_event(event_name="push")["workflow_run"]
		result = {
			"engine": {"trusted_harness_sha": TRUSTED_SHA},
			"metrics": {"score": {"overall_mean": 0.9}},
			"_artifact": {"digest": "sha256:" + "a" * 64, "id": 77},
		}
		provenance = badge_provenance_document(
			"passed",
			run,
			result,
			trusted_run_id=900,
			trusted_run_attempt=2,
			policy=load_policy(),
		)
		self.assertEqual(provenance["tested_sha"], SHA)
		self.assertEqual(provenance["trigger"], {"attempt": 1, "event": "push", "run_id": 101})
		self.assertEqual(provenance["trusted_reporter"], {"attempt": 2, "run_id": 900})
		self.assertEqual(provenance["result_artifact"]["id"], 77)
		self.assertEqual(provenance["worker_sha"], load_policy()["worker"]["commit"])


class WorkflowBoundaryTests(unittest.TestCase):
	def test_candidate_workflows_are_read_only_and_reporters_are_serialized(self):
		caller = (ROOT / ".github/workflows/opendataloader-benchmark.yml").read_text(encoding="utf-8")
		worker = (ROOT / ".github/workflows/opendataloader-worker.yml").read_text(encoding="utf-8")
		odl = (ROOT / ".github/workflows/opendataloader-report.yml").read_text(encoding="utf-8")
		visual = (ROOT / ".github/workflows/pr-visual-validation.yml").read_text(encoding="utf-8")
		visual_report = (ROOT / ".github/workflows/pr-visual-report.yml").read_text(encoding="utf-8")
		self.assertNotIn(": write", caller + worker)
		self.assertRegex(
			caller,
			r"uses: sayantandey/CocoaPDF/\.github/workflows/opendataloader-worker\.yml@[0-9a-f]{40}",
		)
		self.assertNotIn("runs-on:", caller)
		self.assertIn("workflow_call:", worker)
		self.assertIn("actions/cache/restore@", worker)
		self.assertNotIn("actions/cache/save@", worker)
		self.assertIn("cocoapdf-odl-candidate-${{ env.CANDIDATE_SHA }}-${{ github.run_id }}-${{ github.run_attempt }}", worker)
		self.assertNotIn(": write", visual)
		self.assertNotIn("pull_request_target", caller + worker + odl + visual + visual_report)
		self.assertIn("types: [requested, completed]", odl)
		self.assertIn("github.event.action == 'requested'", odl)
		self.assertNotIn("in_progress", odl)
		stable_body_group = "group: pr-body-${{ github.event.workflow_run.head_repository.id }}-${{ github.event.workflow_run.head_branch }}"
		self.assertIn(stable_body_group, odl)
		self.assertIn(stable_body_group, visual_report)
		self.assertIn(
			"group: odl-trusted-${{ github.event.workflow_run.event }}-${{ github.event.workflow_run.head_repository.id }}-${{ github.event.workflow_run.head_branch }}",
			odl,
		)
		self.assertIn("github.event.workflow_run.conclusion == 'success'", odl)
		self.assertIn("verify-boundary-ref", odl)
		self.assertIn("download-candidate-artifact", odl)
		self.assertNotIn("path: candidate", odl)
		self.assertNotIn("--candidate-root candidate", odl)
		self.assertLess(odl.index("actions/cache/save@"), odl.index("download-candidate-artifact"))
		trusted_group_line = next(line for line in odl.splitlines() if "group: odl-trusted-" in line)
		self.assertIn("workflow_run.event", trusted_group_line)
		self.assertIn("workflow_run.head_branch", trusted_group_line)
		self.assertNotIn("workflow_run.head_sha", trusted_group_line)
		self.assertEqual(odl.count("group: odl-main-badge"), 2)
		self.assertGreaterEqual(odl.count("cancel-in-progress: false"), 3)

	def test_optional_body_contention_cannot_displace_authoritative_check(self):
		odl = (ROOT / ".github/workflows/opendataloader-report.yml").read_text(encoding="utf-8")
		visual = (ROOT / ".github/workflows/pr-visual-report.yml").read_text(encoding="utf-8")
		check_job = odl.split("\n  report-pr-check:\n", 1)[1].split("\n  report-pr-body:\n", 1)[0]
		body_job = odl.split("\n  report-pr-body:\n", 1)[1].split("\n  publish-main:\n", 1)[0]
		body_group = "group: pr-body-${{ github.event.workflow_run.head_repository.id }}-${{ github.event.workflow_run.head_branch }}"
		self.assertNotIn("concurrency:", check_job)
		self.assertIn("checks: write", check_job)
		self.assertNotIn("pull-requests: write", check_job)
		self.assertIn("report-pr-check", check_job)
		self.assertIn(body_group, body_job)
		self.assertIn(body_group, visual)
		self.assertNotIn("checks: write", body_job)
		self.assertIn("report-pr-body", body_job)
		reporter = (ROOT / "validation/benchmarks/opendataloader_bench/report.py").read_text(encoding="utf-8")
		check_path = reporter.split("def report_pull_request_check", 1)[1].split(
			"def report_pull_request_body", 1
		)[0]
		body_path = reporter.split("def report_pull_request_body", 1)[1].split("def badge_document", 1)[0]
		self.assertIn("_upsert_check", check_path)
		self.assertNotIn("_upsert_check", body_path)

	def test_candidate_container_has_no_host_path_or_network_escape(self):
		worker = (ROOT / ".github/workflows/opendataloader-worker.yml").read_text(encoding="utf-8")
		reporter = (ROOT / ".github/workflows/opendataloader-report.yml").read_text(encoding="utf-8")
		for required in (
			"--network none",
			"--read-only",
			"--cap-drop ALL",
			"--security-opt no-new-privileges",
			"--pids-limit 256",
			"--memory 2g",
			"size=64m,nr_inodes=4096,nodev,nosuid,noexec",
			"src=${GITHUB_WORKSPACE}/candidate,dst=/candidate,readonly",
			"[[ ! -L \"${GITHUB_WORKSPACE}/candidate/src\" ]]",
			"rev-parse HEAD)\" == \"${CANDIDATE_SHA}",
			"src=${GITHUB_WORKSPACE}/harness,dst=/harness,readonly",
			"src=${RUNNER_TEMP}/cocoapdf-odl-corpus,dst=/corpus,readonly",
			"python /harness/validation/benchmarks/opendataloader_bench/ci_runner.py convert",
			"python /harness/validation/benchmarks/opendataloader_bench/ci_runner.py stage-candidate",
		):
			self.assertIn(required, worker)
		self.assertNotIn("src=${GITHUB_WORKSPACE}/candidate/src", worker)
		self.assertNotIn("python candidate/", worker)
		self.assertNotIn("actions/cache/save@", worker)
		docker_block = worker[worker.index("docker run --rm"):worker.index("Upload bounded predictions only")]
		self.assertNotIn("_odl-benchmark", docker_block)
		self.assertNotIn("ground-truth", docker_block)
		self.assertNotIn("GITHUB_TOKEN", docker_block)
		self.assertNotIn("GH_TOKEN", docker_block)
		self.assertNotIn("docker run --rm", reporter)
		self.assertNotIn("PYTHONPATH=/candidate/src", reporter)
		score_block = reporter.split("- name: Score validated predictions on the trusted host", 1)[1].split(
			"- name: Release bounded candidate artifact storage", 1
		)[0]
		self.assertNotIn("GH_TOKEN", score_block)
		runner = (ROOT / "validation/benchmarks/opendataloader_bench/ci_runner.py").read_text(encoding="utf-8")
		self.assertNotIn("Path(__file__).resolve().parents[3]", runner)
		self.assertNotIn("environment = os.environ.copy()", runner)
		self.assertIn('"PYTHONPATH": str(benchmark_root / "src")', runner)

	def test_actions_are_full_sha_pinned_and_badge_is_fixed_path(self):
		workflows = "\n".join(
			path.read_text(encoding="utf-8")
			for path in (
				ROOT / ".github/workflows/opendataloader-benchmark.yml",
				ROOT / ".github/workflows/opendataloader-report.yml",
				ROOT / ".github/workflows/opendataloader-worker.yml",
				ROOT / ".github/workflows/pr-visual-validation.yml",
				ROOT / ".github/workflows/pr-visual-report.yml",
			)
		)
		pins = re.findall(r"uses:\s+[^\s@]+@([^\s#]+)", workflows)
		self.assertTrue(pins)
		self.assertTrue(all(re.fullmatch(r"[0-9a-f]{40}", pin) for pin in pins))
		reporter = (ROOT / "validation/benchmarks/opendataloader_bench/report.py").read_text(encoding="utf-8")
		self.assertIn('BADGE_BRANCH = "odl-badge"', reporter)
		self.assertIn('BADGE_PATH = "badges/opendataloader.json"', reporter)
		self.assertIn('BADGE_PROVENANCE_PATH = "badges/opendataloader.provenance.json"', reporter)
		self.assertIn("cocoapdf.opendataloader-badge-provenance/v1", reporter)
		self.assertIn("mark-pending", workflows)


class PullRequestBlockTests(unittest.TestCase):
	def test_marker_append_replace_and_ambiguity(self):
		first = replace_owned_block("User text", "cocoapdf-odl", "score one")
		self.assertIn("User text", first)
		self.assertIn("score one", first)
		second = replace_owned_block(first, "cocoapdf-odl", "score two")
		self.assertNotIn("score one", second)
		self.assertEqual(second.count("<!-- cocoapdf-odl:start -->"), 1)
		with self.assertRaises(MarkerError):
			replace_owned_block(first + "<!-- cocoapdf-odl:start -->", "cocoapdf-odl", "x")

	def test_stale_head_refuses_payload(self):
		with self.assertRaises(StaleHeadError):
			payload_for_pull_request(
				{"body": "", "head": {"sha": "9" * 40}},
				expected_head=SHA,
				marker="cocoapdf-odl",
				replacement="x",
			)


if __name__ == "__main__":
	unittest.main()
