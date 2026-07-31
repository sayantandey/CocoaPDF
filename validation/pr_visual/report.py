#!/usr/bin/env python3
"""Publish visual-artifact metadata from trusted default-branch code."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
	sys.path.insert(0, str(ROOT))

from tools.update_pr_body_block import MarkerError, StaleHeadError, payload_for_pull_request  # noqa: E402
from validation.benchmarks.opendataloader_bench.ci_runner import BenchmarkValidationError, COMMIT_SHA  # noqa: E402
from validation.benchmarks.opendataloader_bench.report import (  # noqa: E402
	EXPECTED_REPOSITORY,
	GhApi,
	GitHubApiError,
)


WORKFLOW_NAME = "PR visual validation"
WORKFLOW_PATH = ".github/workflows/pr-visual-validation.yml"
PR_MARKER = "cocoapdf-pr-visual"
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024


def _event(path: Path) -> Dict[str, Any]:
	try:
		value = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		raise BenchmarkValidationError("invalid visual workflow_run event") from exc
	if not isinstance(value, dict) or value.get("action") != "completed":
		raise BenchmarkValidationError("visual reporter only accepts completed workflow_run events")
	return value


def _run(event: Mapping[str, Any]) -> Dict[str, Any]:
	repository = event.get("repository")
	if not isinstance(repository, dict) or repository.get("full_name") != EXPECTED_REPOSITORY:
		raise BenchmarkValidationError("visual workflow repository mismatch")
	run = event.get("workflow_run")
	if not isinstance(run, dict):
		raise BenchmarkValidationError("visual workflow_run payload is missing")
	if run.get("name") != WORKFLOW_NAME or str(run.get("path", "")).split("@", 1)[0] != WORKFLOW_PATH:
		raise BenchmarkValidationError("unexpected visual workflow identity")
	if run.get("event") != "pull_request":
		raise BenchmarkValidationError("visual reporter only accepts pull-request runs")
	if not isinstance(run.get("head_sha"), str) or COMMIT_SHA.fullmatch(run["head_sha"]) is None:
		raise BenchmarkValidationError("invalid visual workflow head SHA")
	head_repository = run.get("head_repository")
	if not isinstance(head_repository, dict):
		raise BenchmarkValidationError("visual workflow head repository is missing")
	if not isinstance(head_repository.get("full_name"), str) or re.fullmatch(
		r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", head_repository["full_name"]
	) is None:
		raise BenchmarkValidationError("invalid visual workflow head repository")
	for name in ("id", "run_attempt"):
		value = run.get(name)
		if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
			raise BenchmarkValidationError("invalid visual workflow %s" % name)
	return dict(run)


def _is_latest(api: GhApi, run: Mapping[str, Any]) -> bool:
	query = urllib.parse.urlencode({"event": "pull_request", "head_sha": run["head_sha"], "per_page": "100"})
	response = api.get("/repos/%s/actions/workflows/pr-visual-validation.yml/runs?%s" % (api.repository, query))
	values = response.get("workflow_runs", []) if isinstance(response, dict) else []
	matches = [
		item
		for item in values
		if isinstance(item, dict)
		and item.get("name") == WORKFLOW_NAME
		and str(item.get("path", "")).split("@", 1)[0] == WORKFLOW_PATH
		and item.get("head_sha") == run["head_sha"]
		and isinstance(item.get("id"), int)
	]
	if not matches:
		raise BenchmarkValidationError("visual trigger run was absent from workflow history")
	latest = max(matches, key=lambda item: int(item["id"]))
	return (
		int(run["id"]) == int(latest["id"])
		and int(run["run_attempt"]) == int(latest.get("run_attempt", 0))
	)


def _pull_request(api: GhApi, run: Mapping[str, Any]) -> Dict[str, Any]:
	values = api.get("/repos/%s/commits/%s/pulls" % (api.repository, run["head_sha"]))
	if not isinstance(values, list):
		raise BenchmarkValidationError("visual commit-to-PR lookup is malformed")
	candidates = []
	for item in values:
		if not isinstance(item, dict):
			continue
		base = item.get("base")
		head = item.get("head")
		base_repo = base.get("repo") if isinstance(base, dict) else None
		head_repo = head.get("repo") if isinstance(head, dict) else None
		if (
			item.get("state") == "open"
			and isinstance(base, dict)
			and base.get("ref") == "main"
			and isinstance(base_repo, dict)
			and base_repo.get("full_name") == api.repository
			and isinstance(head, dict)
			and head.get("sha") == run["head_sha"]
			and isinstance(head_repo, dict)
			and head_repo.get("full_name") == run["head_repository"]["full_name"]
		):
			candidates.append(item)
	if len(candidates) != 1:
		raise BenchmarkValidationError("visual run does not describe exactly one current PR")
	return candidates[0]


def _artifact(api: GhApi, run: Mapping[str, Any], number: int) -> Dict[str, Any]:
	name = "cocoapdf-pr-visual-pr-%d-%d-%d" % (number, run["id"], run["run_attempt"])
	response = api.get("/repos/%s/actions/runs/%d/artifacts?per_page=100" % (api.repository, run["id"]))
	values = response.get("artifacts", []) if isinstance(response, dict) else []
	artifacts = [item for item in values if isinstance(item, dict) and item.get("name") == name]
	if len(artifacts) != 1:
		raise BenchmarkValidationError("expected exactly one visual artifact")
	artifact = artifacts[0]
	identifier = artifact.get("id")
	size = artifact.get("size_in_bytes")
	digest = artifact.get("digest")
	if artifact.get("expired") is not False:
		raise BenchmarkValidationError("visual artifact is expired")
	if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier <= 0:
		raise BenchmarkValidationError("invalid visual artifact ID")
	if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_ARTIFACT_BYTES:
		raise BenchmarkValidationError("visual artifact exceeds metadata size policy")
	if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
		raise BenchmarkValidationError("invalid visual artifact digest")
	workflow_run = artifact.get("workflow_run")
	if isinstance(workflow_run, dict) and workflow_run.get("id") != run["id"]:
		raise BenchmarkValidationError("visual artifact run mismatch")
	return artifact


def _replacement(run: Mapping[str, Any], artifact: Optional[Mapping[str, Any]], repository: str) -> str:
	lines = ["## PR visual-validation artifact", ""]
	if artifact is None:
		lines.append("**Unavailable** — the read-only visual generation run did not produce one valid artifact.")
	else:
		url = "https://github.com/%s/actions/runs/%d/artifacts/%d" % (repository, run["id"], artifact["id"])
		lines.extend(
			[
				"[Download three input PDFs and their CocoaPDF Markdown/HTML/JSON outputs](%s)." % url,
				"",
				"- Head commit: `%s`" % run["head_sha"],
				"- Artifact SHA-256: `%s`" % artifact["digest"],
				"- Open `review.html` after unzipping for side-by-side inspection.",
				"- The artifact expires automatically after at most seven days.",
			]
		)
	lines.extend(
		[
			"",
			"This is an informational artifact produced by a read-only candidate workflow. Review its contents; it is not an authoritative accuracy check.",
		]
	)
	if run["head_repository"]["full_name"] == repository:
		lines.extend(
			[
				"",
				"[Open this exact commit's rendered capability demo](https://rawcdn.githack.com/%s/%s/examples/review.html)."
				% (repository, run["head_sha"]),
			]
		)
	return "\n".join(lines) + "\n"


def report(args: argparse.Namespace, api: GhApi) -> int:
	run = _run(_event(args.event_json))
	if not _is_latest(api, run):
		print("superseded visual run; no PR state changed")
		return 0
	try:
		pull_request = _pull_request(api, run)
	except BenchmarkValidationError as exc:
		print("stale or closed visual PR; no state changed: %s" % exc)
		return 0
	number = pull_request.get("number")
	if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
		raise BenchmarkValidationError("invalid visual PR number")
	artifact = None
	if run.get("conclusion") == "success":
		try:
			artifact = _artifact(api, run, number)
		except BenchmarkValidationError as exc:
			print("visual artifact metadata rejected: %s" % exc, file=sys.stderr)
	current = api.get("/repos/%s/pulls/%d" % (api.repository, number))
	if not isinstance(current, dict):
		raise BenchmarkValidationError("visual PR lookup is malformed")
	payload = payload_for_pull_request(
		current,
		expected_head=run["head_sha"],
		marker=PR_MARKER,
		replacement=_replacement(run, artifact, api.repository),
	)
	api.patch("/repos/%s/pulls/%d" % (api.repository, number), payload)
	return 0 if artifact is not None else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
	parser.add_argument("--event-json", type=Path, required=True)
	args = parser.parse_args(argv)
	try:
		api = GhApi(args.repository, os.environ.get("GH_TOKEN", ""))
		return report(args, api)
	except (BenchmarkValidationError, GitHubApiError, MarkerError, StaleHeadError, OSError) as exc:
		print("trusted visual reporting failed: %s" % exc, file=sys.stderr)
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
