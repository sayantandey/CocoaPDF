#!/usr/bin/env python3
"""Trusted PR reporting and fixed-path live-badge publication for ODL."""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
	sys.path.insert(0, str(ROOT))

from tools.update_pr_body_block import (  # noqa: E402
	MarkerError,
	StaleHeadError,
	payload_for_pull_request,
)
from validation.benchmarks.opendataloader_bench.ci_runner import (  # noqa: E402
	BenchmarkValidationError,
	COMMIT_SHA,
	load_policy,
	load_run_context,
	validate_artifact_directory,
)


EXPECTED_REPOSITORY = "sayantandey/CocoaPDF"
EXECUTION_WORKFLOW_NAME = "OpenDataLoader benchmark"
EXECUTION_WORKFLOW_PATH = ".github/workflows/opendataloader-benchmark.yml"
CHECK_NAME = "ODL 200 / trusted gate"
PR_MARKER = "cocoapdf-odl"
BADGE_BRANCH = "odl-badge"
BADGE_PATH = "badges/opendataloader.json"
SPEED_BADGE_PATH = "badges/opendataloader-speed.json"
BADGE_PROVENANCE_PATH = "badges/opendataloader.provenance.json"
BADGE_LABEL = "ODL Markdown (200)"
SPEED_BADGE_LABEL = "ODL time (200)"
REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class GitHubApiError(RuntimeError):
	"""Raised when a trusted GitHub API operation fails."""


class GhApi:
	"""Small JSON-only wrapper constrained to CocoaPDF repository endpoints."""

	def __init__(self, repository: str, token: str) -> None:
		if repository != EXPECTED_REPOSITORY or REPOSITORY_NAME.fullmatch(repository) is None:
			raise GitHubApiError("unexpected repository identity")
		if not token:
			raise GitHubApiError("missing GitHub token")
		self.repository = repository
		self.environment = os.environ.copy()
		self.environment["GH_TOKEN"] = token

	def request(
		self,
		method: str,
		endpoint: str,
		payload: Optional[Mapping[str, Any]] = None,
		*,
		allow_not_found: bool = False,
	) -> Any:
		if not endpoint.startswith("/repos/%s/" % self.repository):
			raise GitHubApiError("API endpoint escaped the fixed repository")
		command = ["gh", "api", "--method", method, endpoint]
		input_data = None
		if payload is not None:
			command.extend(["--input", "-"])
			input_data = json.dumps(payload, ensure_ascii=False, allow_nan=False)
		completed = subprocess.run(
			command,
			input=input_data,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			text=True,
			encoding="utf-8",
			errors="replace",
			env=self.environment,
		)
		if completed.returncode != 0:
			if allow_not_found and "HTTP 404" in completed.stderr:
				return None
			raise GitHubApiError("GitHub API request failed: %s %s" % (method, endpoint))
		if not completed.stdout.strip():
			return None
		try:
			return json.loads(completed.stdout)
		except json.JSONDecodeError as exc:
			raise GitHubApiError("GitHub API returned non-JSON data") from exc

	def get(self, endpoint: str, *, allow_not_found: bool = False) -> Any:
		return self.request("GET", endpoint, allow_not_found=allow_not_found)

	def post(self, endpoint: str, payload: Mapping[str, Any]) -> Any:
		return self.request("POST", endpoint, payload)

	def patch(self, endpoint: str, payload: Mapping[str, Any]) -> Any:
		return self.request("PATCH", endpoint, payload)

	def put(self, endpoint: str, payload: Mapping[str, Any]) -> Any:
		return self.request("PUT", endpoint, payload)


def _read_event(path: Path) -> Dict[str, Any]:
	try:
		value = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		raise BenchmarkValidationError("invalid workflow_run event payload") from exc
	if not isinstance(value, dict):
		raise BenchmarkValidationError("workflow_run event must be an object")
	return value


def _validate_workflow_run(
	event: Mapping[str, Any],
	repository: str,
	expected_event: str,
	*,
	expected_action: str = "completed",
) -> Dict[str, Any]:
	if event.get("action") != expected_action:
		raise BenchmarkValidationError("unexpected workflow_run action")
	repository_payload = event.get("repository")
	if not isinstance(repository_payload, dict) or repository_payload.get("full_name") != repository:
		raise BenchmarkValidationError("workflow_run repository mismatch")
	run = event.get("workflow_run")
	if not isinstance(run, dict):
		raise BenchmarkValidationError("missing workflow_run payload")
	if run.get("name") != EXECUTION_WORKFLOW_NAME:
		raise BenchmarkValidationError("unexpected triggering workflow name")
	if str(run.get("path", "")).split("@", 1)[0] != EXECUTION_WORKFLOW_PATH:
		raise BenchmarkValidationError("unexpected triggering workflow path")
	if run.get("event") != expected_event:
		raise BenchmarkValidationError("unexpected triggering event")
	head_sha = run.get("head_sha")
	if not isinstance(head_sha, str) or COMMIT_SHA.fullmatch(head_sha) is None:
		raise BenchmarkValidationError("invalid workflow_run head SHA")
	head_repository = run.get("head_repository")
	if not isinstance(head_repository, dict) or not isinstance(head_repository.get("full_name"), str):
		raise BenchmarkValidationError("missing workflow_run head repository")
	if REPOSITORY_NAME.fullmatch(head_repository["full_name"]) is None:
		raise BenchmarkValidationError("invalid workflow_run head repository")
	for name in ("id", "run_attempt"):
		value = run.get(name)
		if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
			raise BenchmarkValidationError("invalid workflow_run %s" % name)
	if expected_event == "push":
		if head_repository["full_name"] != repository or run.get("head_branch") != "main":
			raise BenchmarkValidationError("main push identity mismatch")
	return dict(run)


def _is_latest_run(api: GhApi, run: Mapping[str, Any]) -> bool:
	query = urllib.parse.urlencode(
		{"event": run["event"], "head_sha": run["head_sha"], "per_page": "100"}
	)
	endpoint = "/repos/%s/actions/workflows/opendataloader-benchmark.yml/runs?%s" % (
		api.repository,
		query,
	)
	response = api.get(endpoint)
	values = response.get("workflow_runs", []) if isinstance(response, dict) else []
	matches = [
		item
		for item in values
		if isinstance(item, dict)
		and item.get("name") == EXECUTION_WORKFLOW_NAME
		and str(item.get("path", "")).split("@", 1)[0] == EXECUTION_WORKFLOW_PATH
		and item.get("event") == run["event"]
		and item.get("head_sha") == run["head_sha"]
		and isinstance(item.get("id"), int)
	]
	if not matches:
		raise BenchmarkValidationError("triggering run was absent from workflow history")
	latest = max(matches, key=lambda item: int(item["id"]))
	return (
		int(run["id"]) == int(latest["id"])
		and int(run["run_attempt"]) == int(latest.get("run_attempt", 0))
	)


def _main_is_current(api: GhApi, head_sha: str) -> bool:
	value = api.get("/repos/%s/git/ref/heads/main" % api.repository)
	return (
		isinstance(value, dict)
		and isinstance(value.get("object"), dict)
		and value["object"].get("sha") == head_sha
	)


def resolve_pull_request(api: GhApi, run: Mapping[str, Any]) -> Dict[str, Any]:
	head_sha = str(run["head_sha"])
	head_repository = run["head_repository"]["full_name"]
	values = api.get("/repos/%s/commits/%s/pulls" % (api.repository, head_sha))
	if not isinstance(values, list):
		raise BenchmarkValidationError("commit-to-PR lookup did not return a list")
	candidates = []
	for item in values:
		if not isinstance(item, dict):
			continue
		base = item.get("base")
		head = item.get("head")
		if not isinstance(base, dict) or not isinstance(head, dict):
			continue
		base_repo = base.get("repo")
		head_repo = head.get("repo")
		if (
			item.get("state") == "open"
			and base.get("ref") == "main"
			and isinstance(base_repo, dict)
			and base_repo.get("full_name") == api.repository
			and isinstance(head_repo, dict)
			and head_repo.get("full_name") == head_repository
			and head.get("sha") == head_sha
		):
			candidates.append(item)
	if len(candidates) != 1:
		raise BenchmarkValidationError("workflow head must resolve to exactly one current PR to main")
	return candidates[0]


def _trusted_artifact_metadata(api: GhApi, trusted_run_id: int, trusted_run_attempt: int) -> Dict[str, Any]:
	name = "cocoapdf-odl-trusted-%d-%d" % (trusted_run_id, trusted_run_attempt)
	response = api.get("/repos/%s/actions/runs/%d/artifacts?per_page=100" % (api.repository, trusted_run_id))
	values = response.get("artifacts", []) if isinstance(response, dict) else []
	artifacts = [item for item in values if isinstance(item, dict) and item.get("name") == name]
	if len(artifacts) != 1:
		raise BenchmarkValidationError("expected exactly one trusted result artifact")
	artifact = artifacts[0]
	if artifact.get("expired") is not False:
		raise BenchmarkValidationError("trusted result artifact is expired")
	identifier = artifact.get("id")
	if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier <= 0:
		raise BenchmarkValidationError("invalid trusted artifact ID")
	digest = artifact.get("digest")
	if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
		raise BenchmarkValidationError("invalid trusted artifact digest")
	workflow_run = artifact.get("workflow_run")
	if isinstance(workflow_run, dict) and workflow_run.get("id") != trusted_run_id:
		raise BenchmarkValidationError("trusted artifact run mismatch")
	return artifact


def _validate_result_identity(result: Mapping[str, Any], artifact_root: Path, run: Mapping[str, Any]) -> None:
	context = load_run_context(artifact_root / "run-context.json")
	candidate = context["candidate"]
	context_run = context["run"]
	if candidate["head_sha"] != run["head_sha"]:
		raise BenchmarkValidationError("artifact candidate SHA mismatch")
	if candidate["head_repository"] != run["head_repository"]["full_name"]:
		raise BenchmarkValidationError("artifact candidate repository mismatch")
	if context_run != {
		"attempt": run["run_attempt"],
		"event": run["event"],
		"id": run["id"],
	}:
		raise BenchmarkValidationError("artifact triggering-run mismatch")
	trusted_sha = os.environ.get("GITHUB_SHA", "")
	if COMMIT_SHA.fullmatch(trusted_sha) is None or context["trusted_harness_sha"] != trusted_sha:
		raise BenchmarkValidationError("artifact trusted-harness SHA mismatch")
	if result.get("engine", {}).get("head_sha") != run["head_sha"]:
		raise BenchmarkValidationError("result head mismatch")


def _report_state(args: argparse.Namespace, run: Mapping[str, Any], api: GhApi) -> Dict[str, Any]:
	state: Dict[str, Any] = {
		"artifact": None,
		"reason": None,
		"result": None,
		"state": "failed",
		"trusted_run_id": args.trusted_run_id,
	}
	if args.download_outcome != "success":
		state.update(
			{
				"state": "failed" if args.evaluation_outcome != "success" else "unverified",
				"reason": "The trusted evaluation failed and its complete evidence was unavailable."
				if args.evaluation_outcome != "success"
				else "The trusted evidence artifact was unavailable.",
			}
		)
		return state
	try:
		artifact = _trusted_artifact_metadata(api, args.trusted_run_id, args.trusted_run_attempt)
		result = validate_artifact_directory(args.artifact_root, load_policy(args.policy))
		_validate_result_identity(result, args.artifact_root, run)
		if result["gate"]["passed"] is not True:
			state.update({"artifact": artifact, "result": result, "reason": "The trusted accuracy policy failed."})
		elif args.evaluation_outcome != "success":
			state.update(
				{
					"artifact": artifact,
					"result": result,
					"reason": "The trusted workflow failed after producing otherwise valid evidence.",
				}
			)
		else:
			state.update({"artifact": artifact, "result": result, "state": "passed"})
	except (BenchmarkValidationError, OSError, ValueError, KeyError, TypeError) as exc:
		print("trusted evidence validation error: %s" % exc, file=sys.stderr)
		if args.evaluation_outcome != "success":
			state.update(
				{
					"state": "failed",
					"reason": "The trusted evaluation failed before complete evidence was produced.",
				}
			)
		else:
			state.update({"state": "unverified", "reason": "The evidence failed default-branch validation."})
	return state


def _report_markdown(state: Mapping[str, Any], run: Mapping[str, Any], repository: str) -> str:
	upstream_url = "https://github.com/%s/actions/runs/%d" % (repository, run["id"])
	lines = ["## OpenDataLoader benchmark", ""]
	if state["state"] == "passed":
		lines.append("**Passed** — the exact head cleared the absolute and no-regression gates.")
	else:
		lines.append("**%s** — %s" % (str(state["state"]).capitalize(), state["reason"]))
	lines.extend(["", "- Head commit: `%s`" % run["head_sha"], "- [Trigger run](%s)" % upstream_url])
	result = state.get("result")
	if isinstance(result, dict):
		scores = result["metrics"]["score"]
		deltas = result["gate"]["deltas"]
		counts = result["metrics"]
		lines.extend(
			[
				"",
				"| Metric | Score | Baseline delta | Eligible |",
				"| --- | ---: | ---: | ---: |",
				"| Overall | `%.10f` | `%+.10f` | 200 |" % (scores["overall_mean"], deltas["overall_mean"]),
				"| NID | `%.10f` | `%+.10f` | %d |" % (scores["nid_mean"], deltas["nid_mean"], counts["nid_count"]),
				"| TEDS | `%.10f` | `%+.10f` | %d |" % (scores["teds_mean"], deltas["teds_mean"], counts["teds_count"]),
				"| MHS | `%.10f` | `%+.10f` | %d |" % (scores["mhs_mean"], deltas["mhs_mean"], counts["mhs_count"]),
				"",
				"Policy: overall `>= %.3f`; every primary score may regress by at most `%.3f` from the published baseline."
				% (result["gate"]["overall_floor"], result["gate"]["material_regression"]),
			]
		)
		component_floors = result["gate"].get("component_floors", {})
		if component_floors:
			lines.append(
				"Component floors: %s."
				% ", ".join(
					"%s `>= %.3f`" % (name.removesuffix("_mean").upper(), floor)
					for name, floor in component_floors.items()
				)
			)
		performance = result["performance"]
		lines.append(
			"Trusted conversion time: `%.3f s/page` (`%.6f s` / %d pages)."
			% (
				performance["seconds_per_page"],
				performance["total_seconds"],
				performance["page_count"],
			)
		)
	artifact = state.get("artifact")
	if isinstance(artifact, dict):
		trusted_run_id = state.get("trusted_run_id")
		if isinstance(trusted_run_id, bool) or not isinstance(trusted_run_id, int) or trusted_run_id <= 0:
			raise BenchmarkValidationError("trusted evidence run ID is missing")
		artifact_url = "https://github.com/%s/actions/runs/%d/artifacts/%d" % (
			repository,
			trusted_run_id,
			artifact["id"],
		)
		lines.extend(
			[
				"- [Trusted result evidence](%s)" % artifact_url,
				"- Trusted artifact digest: `%s`" % artifact["digest"],
			]
		)
	lines.extend(
		[
			"",
			"Candidate code ran only in a tokenless, network-disabled container. Scoring used the pinned evaluator on the trusted host.",
			"The retained evidence excludes PDFs, ground truth, and predicted Markdown.",
		]
	)
	return "\n".join(lines) + "\n"


def _check_payload(state: Mapping[str, Any], run: Mapping[str, Any], repository: str) -> Dict[str, Any]:
	status = str(state["state"])
	trusted_run_id = state.get("trusted_run_id")
	if isinstance(trusted_run_id, bool) or not isinstance(trusted_run_id, int) or trusted_run_id <= 0:
		raise BenchmarkValidationError("trusted reporter run ID is missing")
	return {
		"conclusion": "success" if status == "passed" else "failure",
		"details_url": "https://github.com/%s/actions/runs/%d" % (repository, trusted_run_id),
		"external_id": "cocoapdf-odl:%s" % run["head_sha"],
		"head_sha": run["head_sha"],
		"name": CHECK_NAME,
		"output": {
			"summary": _report_markdown(state, run, repository)[:65000],
			"title": "ODL benchmark %s" % status,
		},
		"status": "completed",
	}


def _upsert_check(api: GhApi, state: Mapping[str, Any], run: Mapping[str, Any]) -> None:
	query = urllib.parse.urlencode({"check_name": CHECK_NAME, "per_page": "100"})
	response = api.get("/repos/%s/commits/%s/check-runs?%s" % (api.repository, run["head_sha"], query))
	checks = response.get("check_runs", []) if isinstance(response, dict) else []
	external_id = "cocoapdf-odl:%s" % run["head_sha"]
	existing = [item for item in checks if isinstance(item, dict) and item.get("external_id") == external_id]
	if len(existing) > 1:
		raise GitHubApiError("multiple authoritative checks share one external ID")
	payload = _check_payload(state, run, api.repository)
	if existing:
		identifier = existing[0].get("id")
		if isinstance(identifier, bool) or not isinstance(identifier, int):
			raise GitHubApiError("existing check has an invalid ID")
		update = dict(payload)
		update.pop("head_sha", None)
		update.pop("name", None)
		api.patch("/repos/%s/check-runs/%d" % (api.repository, identifier), update)
	else:
		api.post("/repos/%s/check-runs" % api.repository, payload)


def _pull_request_number(api: GhApi, run: Mapping[str, Any]) -> Optional[int]:
	"""Resolve the still-open PR for *run*, or return ``None`` when it went stale."""
	try:
		pull_request = resolve_pull_request(api, run)
	except BenchmarkValidationError as exc:
		print("stale or closed PR; no state changed: %s" % exc)
		return None
	number = pull_request.get("number")
	if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
		raise BenchmarkValidationError("invalid pull-request number")
	return number


def _current_pull_request(api: GhApi, number: int, run: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
	"""Refetch immediately before a write and reject a moved or closed PR."""
	current = api.get("/repos/%s/pulls/%d" % (api.repository, number))
	if not isinstance(current, dict):
		raise BenchmarkValidationError("pull-request lookup is malformed")
	current_head = current.get("head")
	if (
		current.get("state") != "open"
		or not isinstance(current_head, dict)
		or current_head.get("sha") != run["head_sha"]
	):
		print("PR head changed before reporting; no state changed")
		return None
	return current


def report_pull_request_check(args: argparse.Namespace, api: GhApi) -> int:
	"""Publish only the SHA-bound authoritative check.

	This path intentionally has no dependency on PR body ownership markers or the
	shared serialization used by informational body writers.
	"""
	run = _validate_workflow_run(_read_event(args.event_json), api.repository, "pull_request")
	if not _is_latest_run(api, run):
		print("superseded trigger run; no PR state changed")
		return 0
	number = _pull_request_number(api, run)
	if number is None:
		return 0
	state = _report_state(args, run, api)
	if _current_pull_request(api, number, run) is None:
		return 0
	_upsert_check(api, state, run)
	return 0 if state["state"] == "passed" else 1


def report_pull_request_body(args: argparse.Namespace, api: GhApi) -> int:
	"""Best-effort informational PR-body projection of the trusted result."""
	run = _validate_workflow_run(_read_event(args.event_json), api.repository, "pull_request")
	if not _is_latest_run(api, run):
		print("superseded trigger run; no PR state changed")
		return 0
	number = _pull_request_number(api, run)
	if number is None:
		return 0
	state = _report_state(args, run, api)
	current = _current_pull_request(api, number, run)
	if current is None:
		return 0
	try:
		payload = payload_for_pull_request(
			current,
			expected_head=run["head_sha"],
			marker=PR_MARKER,
			replacement=_report_markdown(state, run, api.repository),
		)
		api.patch("/repos/%s/pulls/%d" % (api.repository, number), payload)
	except StaleHeadError as exc:
		print("PR head changed during body update; no state changed: %s" % exc)
		return 0
	except MarkerError as exc:
		print("PR body update refused: %s" % exc, file=sys.stderr)
		return 1
	return 0


def badge_document(state: str, result: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
	if state == "passed" and result is not None:
		message = "%.4f" % float(result["metrics"]["score"]["overall_mean"])
		color = "brightgreen"
	elif state == "failed":
		message = "failed"
		color = "critical"
	else:
		message = "unverified"
		color = "critical"
	return {"cacheSeconds": 300, "color": color, "label": BADGE_LABEL, "message": message, "schemaVersion": 1}


def speed_badge_document(state: str, result: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
	"""Return the public timing badge derived only from trusted performance data."""
	if state == "passed" and result is not None:
		performance = result.get("performance")
		if not isinstance(performance, Mapping):
			raise BenchmarkValidationError("trusted result is missing performance evidence")
		seconds_per_page = performance.get("seconds_per_page")
		if (
			isinstance(seconds_per_page, bool)
			or not isinstance(seconds_per_page, (int, float))
			or not math.isfinite(float(seconds_per_page))
			or float(seconds_per_page) <= 0.0
		):
			raise BenchmarkValidationError("trusted result has invalid seconds_per_page")
		message = "%.3f s/page" % float(seconds_per_page)
		color = "blue"
	else:
		message = "unverified"
		color = "critical"
	return {
		"cacheSeconds": 300,
		"color": color,
		"label": SPEED_BADGE_LABEL,
		"message": message,
		"schemaVersion": 1,
	}


def badge_provenance_document(
	state: str,
	run: Mapping[str, Any],
	result: Optional[Mapping[str, Any]],
	*,
	trusted_run_id: int,
	trusted_run_attempt: int,
	policy: Mapping[str, Any],
) -> Dict[str, Any]:
	artifact = result.get("_artifact") if isinstance(result, dict) else None
	return {
		"benchmark": {
			"commit": policy["benchmark"]["commit"],
			"tree": policy["benchmark"]["tree"],
		},
		"published_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
		"result_artifact": (
			{"digest": artifact.get("digest"), "id": artifact.get("id")}
			if isinstance(artifact, dict)
			else None
		),
		"performance": result.get("performance") if result is not None else None,
		"schema": "cocoapdf.opendataloader-badge-provenance/v2",
		"scores": result["metrics"]["score"] if result is not None else None,
		"state": state,
		"tested_sha": run["head_sha"],
		"trigger": {
			"attempt": run["run_attempt"],
			"event": run["event"],
			"run_id": run["id"],
		},
		"trusted_harness_sha": (
			result["engine"]["trusted_harness_sha"]
			if result is not None
			else os.environ.get("GITHUB_SHA")
		),
		"trusted_reporter": {
			"attempt": trusted_run_attempt,
			"run_id": trusted_run_id,
		},
		"worker_sha": policy["worker"]["commit"],
	}


def _publish_fixed_document(
	api: GhApi,
	trusted_sha: str,
	path: str,
	document: Mapping[str, Any],
	message: str,
) -> bool:
	if COMMIT_SHA.fullmatch(trusted_sha) is None:
		raise BenchmarkValidationError("invalid trusted main SHA")
	ref_endpoint = "/repos/%s/git/ref/heads/%s" % (api.repository, BADGE_BRANCH)
	if api.get(ref_endpoint, allow_not_found=True) is None:
		api.post("/repos/%s/git/refs" % api.repository, {"ref": "refs/heads/%s" % BADGE_BRANCH, "sha": trusted_sha})
	content_endpoint = "/repos/%s/contents/%s" % (api.repository, path)
	existing = api.get(content_endpoint + "?ref=" + BADGE_BRANCH, allow_not_found=True)
	payload: Dict[str, Any] = {
		"branch": BADGE_BRANCH,
		"content": base64.b64encode(
			(json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
		).decode("ascii"),
		"message": message,
	}
	if existing is not None:
		if not isinstance(existing, dict) or not isinstance(existing.get("sha"), str):
			raise GitHubApiError("existing badge response is malformed")
		payload["sha"] = existing["sha"]
	if not _main_is_current(api, trusted_sha):
		print("main advanced before badge write; stale publication skipped")
		return False
	api.put(content_endpoint, payload)
	return True


def _response_sha(value: Any, context: str) -> str:
	if not isinstance(value, dict):
		raise GitHubApiError("%s response is malformed" % context)
	sha = value.get("sha")
	if not isinstance(sha, str) or COMMIT_SHA.fullmatch(sha) is None:
		raise GitHubApiError("%s response has an invalid SHA" % context)
	return sha


def _badge_ref_sha(api: GhApi) -> Optional[str]:
	value = api.get(
		"/repos/%s/git/ref/heads/%s" % (api.repository, BADGE_BRANCH),
		allow_not_found=True,
	)
	if value is None:
		return None
	if not isinstance(value, dict) or not isinstance(value.get("object"), dict):
		raise GitHubApiError("badge ref response is malformed")
	sha = value["object"].get("sha")
	if not isinstance(sha, str) or COMMIT_SHA.fullmatch(sha) is None:
		raise GitHubApiError("badge ref has an invalid SHA")
	return sha


def _commit_tree_sha(api: GhApi, commit_sha: str) -> str:
	value = api.get("/repos/%s/git/commits/%s" % (api.repository, commit_sha))
	if not isinstance(value, dict) or not isinstance(value.get("tree"), dict):
		raise GitHubApiError("badge base commit response is malformed")
	tree_sha = value["tree"].get("sha")
	if not isinstance(tree_sha, str) or COMMIT_SHA.fullmatch(tree_sha) is None:
		raise GitHubApiError("badge base tree has an invalid SHA")
	return tree_sha


def _document_blob(api: GhApi, document: Mapping[str, Any]) -> str:
	try:
		encoded = (
			json.dumps(
				document,
				ensure_ascii=False,
				sort_keys=True,
				separators=(",", ":"),
				allow_nan=False,
			)
			+ "\n"
		).encode("utf-8")
	except (TypeError, ValueError) as exc:
		raise BenchmarkValidationError("badge document is not canonical JSON") from exc
	value = api.post(
		"/repos/%s/git/blobs" % api.repository,
		{
			"content": base64.b64encode(encoded).decode("ascii"),
			"encoding": "base64",
		},
	)
	return _response_sha(value, "badge blob")


def _publish_badge_documents_atomically(
	api: GhApi,
	trusted_sha: str,
	documents: Mapping[str, Mapping[str, Any]],
) -> bool:
	"""Publish all live badge documents in one compare-and-swap ref update."""
	if COMMIT_SHA.fullmatch(trusted_sha) is None:
		raise BenchmarkValidationError("invalid trusted main SHA")
	if set(documents) != {BADGE_PATH, SPEED_BADGE_PATH, BADGE_PROVENANCE_PATH}:
		raise BenchmarkValidationError("badge transaction must contain every fixed document")
	if not _main_is_current(api, trusted_sha):
		print("main advanced before badge transaction; stale publication skipped")
		return False

	base_ref_sha = _badge_ref_sha(api)
	base_commit_sha = base_ref_sha or trusted_sha
	base_tree_sha = _commit_tree_sha(api, base_commit_sha)
	tree_entries = []
	for path in (BADGE_PATH, SPEED_BADGE_PATH, BADGE_PROVENANCE_PATH):
		tree_entries.append(
			{
				"mode": "100644",
				"path": path,
				"sha": _document_blob(api, documents[path]),
				"type": "blob",
			}
		)
	tree_sha = _response_sha(
		api.post(
			"/repos/%s/git/trees" % api.repository,
			{"base_tree": base_tree_sha, "tree": tree_entries},
		),
		"badge tree",
	)
	commit_sha = _response_sha(
		api.post(
			"/repos/%s/git/commits" % api.repository,
			{
				"message": "Update verified OpenDataLoader benchmark badges",
				"parents": [base_commit_sha],
				"tree": tree_sha,
			},
		),
		"badge commit",
	)

	# Objects created above are harmless until referenced. Recheck both mutable
	# refs immediately before the single publication write.
	if not _main_is_current(api, trusted_sha):
		print("main advanced before badge ref update; stale publication skipped")
		return False
	if _badge_ref_sha(api) != base_ref_sha:
		print("badge ref advanced concurrently; stale publication skipped")
		return False
	if base_ref_sha is None:
		api.post(
			"/repos/%s/git/refs" % api.repository,
			{"ref": "refs/heads/%s" % BADGE_BRANCH, "sha": commit_sha},
		)
	else:
		# force=false also makes a race after the explicit recheck fail closed:
		# the new commit has exactly base_ref_sha as its parent.
		api.patch(
			"/repos/%s/git/refs/heads/%s" % (api.repository, BADGE_BRANCH),
			{"force": False, "sha": commit_sha},
		)
	return True


def publish_badge(
	api: GhApi,
	trusted_sha: str,
	document: Mapping[str, Any],
	*,
	speed: Optional[Mapping[str, Any]] = None,
	provenance: Optional[Mapping[str, Any]] = None,
) -> bool:
	# The optional bundle parameters preserve the original public helper while
	# routing every production publication through one Git Data transaction.
	if speed is not None or provenance is not None:
		if speed is None or provenance is None:
			raise BenchmarkValidationError("atomic badge publication requires speed and provenance")
		return _publish_badge_documents_atomically(
			api,
			trusted_sha,
			{
				BADGE_PATH: document,
				SPEED_BADGE_PATH: speed,
				BADGE_PROVENANCE_PATH: provenance,
			},
		)
	return _publish_fixed_document(
		api,
		trusted_sha,
		BADGE_PATH,
		document,
		"Update verified OpenDataLoader benchmark badge",
	)


def publish_badge_provenance(api: GhApi, trusted_sha: str, document: Mapping[str, Any]) -> bool:
	return _publish_fixed_document(
		api,
		trusted_sha,
		BADGE_PROVENANCE_PATH,
		document,
		"Update OpenDataLoader badge provenance",
	)


def publish_badge_bundle(
	api: GhApi,
	trusted_sha: str,
	badge: Mapping[str, Any],
	provenance: Mapping[str, Any],
	*,
	speed: Mapping[str, Any],
	fail_closed_first: bool,
) -> bool:
	# Retain the argument for call-site compatibility. One ref update now makes
	# ordering unnecessary: no observer can see a mixed score/speed/evidence set.
	_ = fail_closed_first
	return publish_badge(
		api,
		trusted_sha,
		badge,
		speed=speed,
		provenance=provenance,
	)


def publish_main_badge(args: argparse.Namespace, api: GhApi) -> int:
	run = _validate_workflow_run(_read_event(args.event_json), api.repository, "push")
	if not _is_latest_run(api, run):
		print("superseded main attempt; badge left unchanged")
		return 0
	if not _main_is_current(api, run["head_sha"]):
		print("superseded main run; badge left unchanged")
		return 0
	state = _report_state(args, run, api)
	verified_result = state.get("result")
	badge_result = verified_result if state["state"] == "passed" else None
	provenance_result = dict(verified_result) if isinstance(verified_result, dict) else None
	if provenance_result is not None:
		provenance_result["_artifact"] = state.get("artifact")
	provenance = badge_provenance_document(
		str(state["state"]),
		run,
		provenance_result,
		trusted_run_id=args.trusted_run_id,
		trusted_run_attempt=args.trusted_run_attempt,
		policy=load_policy(args.policy),
	)
	if not publish_badge_bundle(
		api,
		run["head_sha"],
		badge_document(str(state["state"]), badge_result),
		provenance,
		speed=speed_badge_document(str(state["state"]), badge_result),
		fail_closed_first=state["state"] != "passed",
	):
		return 0
	# The evaluate job remains authoritative for an explicit failed state, so a
	# successfully published red failure must not create a second publisher
	# failure. Unverified evidence remains a publisher failure because no other
	# job necessarily represents that integrity problem.
	return 1 if state["state"] == "unverified" else 0


def mark_main_pending(args: argparse.Namespace, api: GhApi) -> int:
	run = _validate_workflow_run(
		_read_event(args.event_json),
		api.repository,
		"push",
		expected_action="requested",
	)
	if not _main_is_current(api, run["head_sha"]):
		print("superseded main run; pending badge write skipped")
		return 0
	provenance = badge_provenance_document(
		"unverified",
		run,
		None,
		trusted_run_id=args.trusted_run_id,
		trusted_run_attempt=args.trusted_run_attempt,
		policy=load_policy(args.policy),
	)
	publish_badge_bundle(
		api,
		run["head_sha"],
		badge_document("unverified"),
		provenance,
		speed=speed_badge_document("unverified"),
		fail_closed_first=True,
	)
	return 0


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
	parser.add_argument("--policy", type=Path, default=Path(__file__).with_name("policy.json"))
	subparsers = parser.add_subparsers(dest="command", required=True)
	for name in ("report-pr-check", "report-pr-body", "publish-badge"):
		command = subparsers.add_parser(name)
		command.add_argument("--event-json", type=Path, required=True)
		command.add_argument("--artifact-root", type=Path, required=True)
		command.add_argument("--download-outcome", choices=("success", "failure", "cancelled", "skipped"), required=True)
		command.add_argument("--evaluation-outcome", choices=("success", "failure", "cancelled", "skipped"), required=True)
		command.add_argument("--trusted-run-id", type=int, required=True)
		command.add_argument("--trusted-run-attempt", type=int, required=True)
	pending = subparsers.add_parser("mark-pending")
	pending.add_argument("--event-json", type=Path, required=True)
	pending.add_argument("--trusted-run-id", type=int, required=True)
	pending.add_argument("--trusted-run-attempt", type=int, required=True)
	return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
	args = _parse_args(argv)
	try:
		api = GhApi(args.repository, os.environ.get("GH_TOKEN", ""))
		if args.trusted_run_id <= 0 or args.trusted_run_attempt <= 0:
			raise BenchmarkValidationError("invalid trusted reporter run identity")
		if args.command == "mark-pending":
			return mark_main_pending(args, api)
		if args.command == "report-pr-check":
			return report_pull_request_check(args, api)
		if args.command == "report-pr-body":
			return report_pull_request_body(args, api)
		if args.command == "publish-badge":
			return publish_main_badge(args, api)
		raise AssertionError("unreachable command")
	except (BenchmarkValidationError, GitHubApiError, OSError, subprocess.SubprocessError) as exc:
		print("trusted OpenDataLoader reporting failed: %s" % exc, file=sys.stderr)
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
