from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from unittest.mock import patch

from tools.update_pr_body_block import MarkerError, StaleHeadError, payload_for_pull_request, replace_owned_block
from validation.benchmarks.opendataloader_bench.ci_runner import (
	BenchmarkValidationError,
	MAX_PREDICTION_FILE_BYTES,
	evaluate_gate,
	load_policy,
	validate_candidate_output,
	verify_corpus,
	verify_privileged_boundary,
	write_run_context,
)
from validation.benchmarks.opendataloader_bench.report import (
	_check_payload,
	_is_latest_run,
	_report_markdown,
	_trusted_artifact_metadata,
	badge_document,
	badge_provenance_document,
	publish_badge,
	report_pull_request_body,
	report_pull_request_check,
)
from validation.pr_visual.report import _artifact as visual_artifact
from validation.pr_visual.report import _is_latest as visual_is_latest


ROOT = Path(__file__).resolve().parents[1]
SHA = "1" * 40
TRUSTED_SHA = "2" * 40


def workflow_event(
	*,
	event_name: str = "pull_request",
	run_id: int = 101,
	attempt: int = 1,
	head_sha: str = SHA,
	workflow_name: str = "OpenDataLoader benchmark",
	workflow_path: str = ".github/workflows/opendataloader-benchmark.yml",
) -> Dict[str, Any]:
	return {
		"action": "completed",
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
	def test_exact_published_baseline_and_regression_gates(self):
		policy = load_policy()
		self.assertEqual(policy["baseline"]["scores"]["overall_mean"], 0.869665721357887)
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
		from validation.benchmarks.opendataloader_bench.ci_runner import PRIVILEGED_BOUNDARY_PATHS

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


class ReporterSecurityTests(unittest.TestCase):
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


class WorkflowBoundaryTests(unittest.TestCase):
	def test_candidate_workflows_are_read_only_and_reporters_are_serialized(self):
		trigger = (ROOT / ".github/workflows/opendataloader-benchmark.yml").read_text(encoding="utf-8")
		odl = (ROOT / ".github/workflows/opendataloader-report.yml").read_text(encoding="utf-8")
		visual = (ROOT / ".github/workflows/pr-visual-validation.yml").read_text(encoding="utf-8")
		visual_report = (ROOT / ".github/workflows/pr-visual-report.yml").read_text(encoding="utf-8")
		self.assertNotIn(": write", trigger)
		self.assertNotIn("checkout@", trigger)
		self.assertNotIn("upload-artifact", trigger)
		self.assertNotIn(": write", visual)
		self.assertNotIn("pull_request_target", trigger + odl + visual + visual_report)
		self.assertIn("types: [completed]", odl)
		self.assertNotIn("requested", odl)
		self.assertNotIn("in_progress", odl)
		stable_body_group = "group: pr-body-${{ github.event.workflow_run.head_repository.id }}-${{ github.event.workflow_run.head_branch }}"
		self.assertIn(stable_body_group, odl)
		self.assertIn(stable_body_group, visual_report)
		self.assertIn(
			"group: odl-trusted-${{ github.event.workflow_run.event }}-${{ github.event.workflow_run.head_repository.id }}-${{ github.event.workflow_run.head_branch }}",
			odl,
		)
		self.assertIn("github.event.workflow_run.conclusion == 'success'", odl)
		self.assertIn("verify-boundary", odl)
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

	def test_trusted_container_has_no_host_path_or_network_escape(self):
		workflow = (ROOT / ".github/workflows/opendataloader-report.yml").read_text(encoding="utf-8")
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
			"rev-parse --is-inside-work-tree",
			"rev-parse HEAD)\" == \"${CANDIDATE_SHA}",
			"src=${GITHUB_WORKSPACE}/trusted/validation/benchmarks/opendataloader_bench,dst=/harness,readonly",
			"src=${RUNNER_TEMP}/cocoapdf-odl-corpus,dst=/corpus,readonly",
			"Smoke-test minimal harness in the exact pinned container",
			"python /harness/ci_runner.py --help",
		):
			self.assertIn(required, workflow)
		self.assertNotIn("src=${GITHUB_WORKSPACE}/candidate/src", workflow)
		self.assertNotIn("src=${GITHUB_WORKSPACE}/trusted,dst=/trusted", workflow)
		docker_block = workflow[workflow.index("docker run --rm"):workflow.index("python trusted/validation", workflow.index("docker run --rm"))]
		self.assertNotIn("_odl-benchmark", docker_block)
		self.assertNotIn("/trusted/src", docker_block)
		self.assertNotIn("GITHUB_TOKEN", docker_block)
		self.assertNotIn("GH_TOKEN", docker_block)
		runner = (ROOT / "validation/benchmarks/opendataloader_bench/ci_runner.py").read_text(encoding="utf-8")
		self.assertNotIn("Path(__file__).resolve().parents[3]", runner)
		self.assertNotIn("environment = os.environ.copy()", runner)
		self.assertIn('"PYTHONPATH": str(args.benchmark_root / "src")', runner)

	def test_actions_are_full_sha_pinned_and_badge_is_fixed_path(self):
		workflows = "\n".join(
			path.read_text(encoding="utf-8")
			for path in (
				ROOT / ".github/workflows/opendataloader-report.yml",
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
