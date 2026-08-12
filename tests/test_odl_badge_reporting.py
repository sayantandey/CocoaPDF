from __future__ import annotations

import base64
import json
import math
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
from unittest import mock

from validation.benchmarks.opendataloader_bench.ci_runner import BenchmarkValidationError
from validation.benchmarks.opendataloader_bench.report import (
	BADGE_PATH,
	BADGE_PROVENANCE_PATH,
	SPEED_BADGE_PATH,
	_publish_badge_documents_atomically,
	badge_document,
	badge_provenance_document,
	publish_main_badge,
	speed_badge_document,
)


class MainPublicationFreshnessTests(unittest.TestCase):
	def test_same_sha_older_attempt_is_not_latest(self) -> None:
		from validation.benchmarks.opendataloader_bench.report import _is_latest_run

		run = {
			"event": "push",
			"head_sha": MAIN_SHA,
			"id": 100,
			"name": "OpenDataLoader benchmark",
			"path": ".github/workflows/opendataloader-benchmark.yml@refs/heads/main",
			"run_attempt": 1,
		}
		newer = dict(run, id=101, run_attempt=2)

		class RunsApi:
			repository = "sayantandey/CocoaPDF"

			def get(self, endpoint: str, *, allow_not_found: bool = False) -> Any:
				self.assertions.append(endpoint)
				return {"workflow_runs": [newer]}

			def __init__(self) -> None:
				self.assertions: list[str] = []

		api = RunsApi()
		self.assertFalse(_is_latest_run(api, run))
		self.assertTrue(api.assertions)

	def test_superseded_main_attempt_performs_no_evaluation_or_publication(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			event_path = Path(directory) / "event.json"
			event_path.write_text(
				json.dumps(
					{
						"action": "completed",
						"repository": {"full_name": "sayantandey/CocoaPDF"},
						"workflow_run": {
							"event": "push",
							"head_branch": "main",
							"head_repository": {"full_name": "sayantandey/CocoaPDF"},
							"head_sha": MAIN_SHA,
							"id": 100,
							"name": "OpenDataLoader benchmark",
							"path": ".github/workflows/opendataloader-benchmark.yml@refs/heads/main",
							"run_attempt": 1,
						},
					}
				),
				encoding="utf-8",
			)
			args = type(
				"Args",
				(),
				{
					"event_json": event_path,
					"trusted_run_id": 900,
					"trusted_run_attempt": 1,
				},
			)()
			api = type("Api", (), {"repository": "sayantandey/CocoaPDF"})()
			with mock.patch(
				"validation.benchmarks.opendataloader_bench.report._is_latest_run",
				return_value=False,
			), mock.patch(
				"validation.benchmarks.opendataloader_bench.report._report_state"
			) as report_state, mock.patch(
				"validation.benchmarks.opendataloader_bench.report.publish_badge_bundle"
			) as publish:
				self.assertEqual(publish_main_badge(args, api), 0)
			report_state.assert_not_called()
			publish.assert_not_called()


MAIN_SHA = "1" * 40
BADGE_SHA = "2" * 40
BADGE_TREE_SHA = "3" * 40
NEW_TREE_SHA = "4" * 40
NEW_COMMIT_SHA = "5" * 40
BLOB_SHAS = ("6" * 40, "7" * 40, "8" * 40)


def _result(seconds_per_page: float = 0.3754) -> Dict[str, Any]:
	return {
		"metrics": {"score": {"overall_mean": 0.9019}},
		"performance": {
			"document_count": 200,
			"elapsed_nanoseconds": 75_080_000_000,
			"page_count": 200,
			"runner": {
				"architecture": "linux/amd64",
				"container_image": "python@sha256:dd86541a59b252667f4c12f8b2ee17216de37dd65ac773bf097bef996fa78860",
				"cpu_limit": 2,
				"memory_limit_bytes": 2_147_483_648,
				"network": "none",
				"os": "ubuntu-24.04",
				"pids_limit": 256,
				"read_only_root": True,
				"timer": "host_python_time.monotonic_ns",
			},
			"schema": "cocoapdf.opendataloader-worker-timing/v1",
			"scope": "canonical_adapter_batch_conversion_container_wall_time",
			"seconds_per_page": seconds_per_page,
			"total_seconds": 75.08,
		},
	}


class GitDataApi:
	repository = "sayantandey/CocoaPDF"

	def __init__(
		self,
		*,
		main_shas: tuple[str, ...] = (MAIN_SHA, MAIN_SHA),
		badge_shas: tuple[Optional[str], ...] = (BADGE_SHA, BADGE_SHA),
	) -> None:
		self.main_shas = list(main_shas)
		self.badge_shas = list(badge_shas)
		self.writes: list[tuple[str, str, Dict[str, Any]]] = []
		self._blob_index = 0

	@staticmethod
	def _next(values: list[Optional[str]]) -> Optional[str]:
		if not values:
			raise AssertionError("unexpected repeated mutable-ref lookup")
		return values.pop(0)

	def get(self, endpoint: str, *, allow_not_found: bool = False) -> Any:
		if "/git/ref/heads/main" in endpoint:
			return {"object": {"sha": self._next(self.main_shas)}}
		if "/git/ref/heads/odl-badge" in endpoint:
			sha = self._next(self.badge_shas)
			return None if sha is None else {"object": {"sha": sha}}
		if "/git/commits/" in endpoint:
			return {"tree": {"sha": BADGE_TREE_SHA}}
		raise AssertionError("unexpected GET %s" % endpoint)

	def post(self, endpoint: str, payload: Mapping[str, Any]) -> Any:
		self.writes.append(("POST", endpoint, dict(payload)))
		if endpoint.endswith("/git/blobs"):
			sha = BLOB_SHAS[self._blob_index]
			self._blob_index += 1
			return {"sha": sha}
		if endpoint.endswith("/git/trees"):
			return {"sha": NEW_TREE_SHA}
		if endpoint.endswith("/git/commits"):
			return {"sha": NEW_COMMIT_SHA}
		if endpoint.endswith("/git/refs"):
			return {"ref": "refs/heads/odl-badge"}
		raise AssertionError("unexpected POST %s" % endpoint)

	def patch(self, endpoint: str, payload: Mapping[str, Any]) -> Any:
		self.writes.append(("PATCH", endpoint, dict(payload)))
		return {"object": {"sha": payload["sha"]}}


class SpeedBadgeTests(unittest.TestCase):
	def test_passed_badge_is_one_three_decimal_seconds_per_page_value(self) -> None:
		document = speed_badge_document("passed", _result())
		self.assertEqual(document["label"], "ODL time (200)")
		self.assertEqual(document["message"], "0.375 s/page")
		self.assertEqual(document["color"], "blue")
		self.assertNotIn("-", document["message"])

	def test_pending_and_failure_never_retain_a_previous_timing(self) -> None:
		for state in ("unverified", "failed"):
			with self.subTest(state=state):
				document = speed_badge_document(state, _result())
				self.assertEqual(document["message"], "unverified")
				self.assertEqual(document["color"], "critical")

	def test_passed_badge_rejects_missing_nonfinite_or_nonpositive_timing(self) -> None:
		with self.assertRaisesRegex(BenchmarkValidationError, "missing performance"):
			speed_badge_document("passed", {"metrics": {}})
		for value in (True, 0.0, -0.1, math.inf, math.nan, "0.1"):
			with self.subTest(value=value), self.assertRaisesRegex(
				BenchmarkValidationError, "invalid seconds_per_page"
			):
				speed_badge_document("passed", _result(value))  # type: ignore[arg-type]

	def test_provenance_retains_the_complete_trusted_performance_record(self) -> None:
		result = _result()
		result.update(
			{
				"_artifact": {"digest": "sha256:" + "a" * 64, "id": 77},
				"engine": {"trusted_harness_sha": "9" * 40},
			}
		)
		document = badge_provenance_document(
			"passed",
			{
				"event": "push",
				"head_sha": MAIN_SHA,
				"id": 101,
				"run_attempt": 1,
			},
			result,
			trusted_run_id=900,
			trusted_run_attempt=2,
			policy={
				"benchmark": {"commit": "a" * 40, "tree": "b" * 40},
				"worker": {"commit": "c" * 40},
			},
		)
		self.assertEqual(document["performance"], result["performance"])
		self.assertEqual(document["performance"]["scope"], "canonical_adapter_batch_conversion_container_wall_time")


class AtomicBadgePublicationTests(unittest.TestCase):
	@staticmethod
	def _documents() -> Dict[str, Mapping[str, Any]]:
		return {
			BADGE_PATH: badge_document("passed", _result()),
			SPEED_BADGE_PATH: speed_badge_document("passed", _result()),
			BADGE_PROVENANCE_PATH: {"performance": _result()["performance"], "state": "passed"},
		}

	def test_score_speed_and_provenance_publish_in_one_ref_update(self) -> None:
		api = GitDataApi()
		self.assertTrue(_publish_badge_documents_atomically(api, MAIN_SHA, self._documents()))

		blobs = [write for write in api.writes if write[1].endswith("/git/blobs")]
		self.assertEqual(len(blobs), 3)
		decoded = [
			json.loads(base64.b64decode(write[2]["content"]).decode("utf-8"))
			for write in blobs
		]
		self.assertEqual(decoded[0]["message"], "0.9019")
		self.assertEqual(decoded[1]["message"], "0.375 s/page")
		self.assertEqual(decoded[2]["performance"]["page_count"], 200)

		tree_writes = [write for write in api.writes if write[1].endswith("/git/trees")]
		self.assertEqual(len(tree_writes), 1)
		self.assertEqual(
			[entry["path"] for entry in tree_writes[0][2]["tree"]],
			[BADGE_PATH, SPEED_BADGE_PATH, BADGE_PROVENANCE_PATH],
		)
		commit_writes = [write for write in api.writes if write[1].endswith("/git/commits")]
		self.assertEqual(commit_writes[0][2]["parents"], [BADGE_SHA])
		ref_writes = [write for write in api.writes if "/git/refs/heads/odl-badge" in write[1]]
		self.assertEqual(
			ref_writes,
			[(
				"PATCH",
				"/repos/sayantandey/CocoaPDF/git/refs/heads/odl-badge",
				{"force": False, "sha": NEW_COMMIT_SHA},
			)],
		)
		self.assertFalse(any("/contents/" in endpoint for _, endpoint, _ in api.writes))

	def test_main_move_after_object_creation_prevents_publication(self) -> None:
		api = GitDataApi(main_shas=(MAIN_SHA, "9" * 40))
		self.assertFalse(_publish_badge_documents_atomically(api, MAIN_SHA, self._documents()))
		self.assertFalse(any("/git/refs/heads/odl-badge" in endpoint for _, endpoint, _ in api.writes))

	def test_concurrent_badge_ref_move_prevents_publication(self) -> None:
		api = GitDataApi(badge_shas=(BADGE_SHA, "9" * 40))
		self.assertFalse(_publish_badge_documents_atomically(api, MAIN_SHA, self._documents()))
		self.assertFalse(any("/git/refs/heads/odl-badge" in endpoint for _, endpoint, _ in api.writes))

	def test_missing_bundle_member_is_rejected_before_any_api_call(self) -> None:
		api = GitDataApi()
		documents = self._documents()
		del documents[SPEED_BADGE_PATH]
		with self.assertRaisesRegex(BenchmarkValidationError, "every fixed document"):
			_publish_badge_documents_atomically(api, MAIN_SHA, documents)
		self.assertEqual(api.writes, [])


if __name__ == "__main__":
	unittest.main()
