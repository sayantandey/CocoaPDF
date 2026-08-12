from __future__ import annotations

import argparse
import json
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from validation.benchmarks.opendataloader_bench.ci_runner import (
	BenchmarkValidationError,
	MAX_CONVERSION_ELAPSED_NANOSECONDS,
	TIMING_RUNNER,
	TIMING_SCHEMA,
	TIMING_SCOPE,
	_extract_candidate_archive,
	_timing_document,
	load_policy,
	stage_candidate_output,
	validate_timing_document,
)


ROOT = Path(__file__).resolve().parents[1]


class TrustedOdlTimingTests(unittest.TestCase):
	def test_timing_is_recomputed_from_integer_nanoseconds_and_pinned_pages(self):
		policy = load_policy()
		timing = _timing_document(25_000_000_000, policy)
		self.assertEqual(
			timing,
			{
				"document_count": 200,
				"elapsed_nanoseconds": 25_000_000_000,
				"page_count": 200,
				"runner": TIMING_RUNNER,
				"schema": TIMING_SCHEMA,
				"scope": TIMING_SCOPE,
				"seconds_per_page": 0.125,
				"total_seconds": 25.0,
			},
		)
		self.assertEqual(validate_timing_document(timing, policy), timing)
		multi_page_policy = json.loads(json.dumps(policy))
		multi_page_policy["benchmark"]["corpus"]["page_count"] = 400
		self.assertEqual(
			_timing_document(25_000_000_000, multi_page_policy)["seconds_per_page"],
			0.0625,
		)

	def test_timing_rejects_invalid_bounds_and_derived_values(self):
		policy = load_policy()
		for elapsed in (0, -1, MAX_CONVERSION_ELAPSED_NANOSECONDS + 1, True, 1.5):
			with self.subTest(elapsed=elapsed), self.assertRaises(BenchmarkValidationError):
				_timing_document(elapsed, policy)
		for field, value in (
			("page_count", 199),
			("document_count", 199),
			("seconds_per_page", 0.001),
			("total_seconds", 0.001),
			("scope", "candidate_reported"),
			("runner", {}),
		):
			timing = _timing_document(1_000_000_000, policy)
			timing[field] = value
			with self.subTest(field=field), self.assertRaises(BenchmarkValidationError):
				validate_timing_document(timing, policy)

	def test_stage_constructs_root_timing_without_accepting_candidate_metadata(self):
		policy = load_policy()
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			engine = root / "raw" / "cocoapdf"
			markdown = engine / "markdown"
			markdown.mkdir(parents=True)
			document_ids = {"%014d" % index for index in range(200)}
			for document_id in document_ids:
				(markdown / (document_id + ".md")).write_text("x\n", encoding="utf-8")
			(engine / "failures.json").write_text("[]\n", encoding="utf-8")
			args = argparse.Namespace(
				artifact_root=root / "artifact",
				candidate_output=engine,
				corpus_root=root / "corpus",
				elapsed_nanoseconds=25_000_000_000,
				policy=ROOT / "validation/benchmarks/opendataloader_bench/policy.json",
			)
			with patch(
				"validation.benchmarks.opendataloader_bench.ci_runner.verify_pdf_corpus",
				return_value=document_ids,
			):
				self.assertTrue(stage_candidate_output(args))
			timing_path = args.artifact_root / "timing.json"
			self.assertTrue(timing_path.is_file())
			self.assertEqual(
				json.loads(timing_path.read_text(encoding="utf-8")),
				_timing_document(args.elapsed_nanoseconds, policy),
			)
			self.assertFalse((args.artifact_root / "prediction/cocoapdf/timing.json").exists())

	def test_uploaded_artifact_requires_valid_worker_timing_at_the_root(self):
		policy = load_policy()

		def archive(timing):
			stream = BytesIO()
			with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as bundle:
				bundle.writestr("prediction/cocoapdf/failures.json", "[]\n")
				for index in range(200):
					bundle.writestr(
						"prediction/cocoapdf/markdown/%014d.md" % index,
						"x\n",
					)
				if timing is not None:
					bundle.writestr("timing.json", json.dumps(timing))
			return stream.getvalue()

		with tempfile.TemporaryDirectory() as directory:
			with self.assertRaisesRegex(BenchmarkValidationError, "timing is missing"):
				_extract_candidate_archive(archive(None), Path(directory) / "missing", policy)
		tampered = _timing_document(1_000_000_000, policy)
		tampered["seconds_per_page"] = 0.000001
		with tempfile.TemporaryDirectory() as directory:
			with self.assertRaisesRegex(BenchmarkValidationError, "seconds per page mismatch"):
				_extract_candidate_archive(archive(tampered), Path(directory) / "tampered", policy)

	def test_workflow_timer_wraps_only_conversion_and_is_outside_candidate_container(self):
		workflow = (ROOT / ".github/workflows/opendataloader-worker.yml").read_text(encoding="utf-8")
		for pinned_value in (
			TIMING_RUNNER["container_image"],
			"runs-on: %s" % TIMING_RUNNER["os"],
			"--cpus %s" % TIMING_RUNNER["cpu_limit"],
			"--memory 2g",
			"--pids-limit %s" % TIMING_RUNNER["pids_limit"],
			"--network %s" % TIMING_RUNNER["network"],
			"--read-only",
		):
			self.assertIn(str(pinned_value), workflow)
		start = workflow.index('conversion_started_ns="$(python3')
		convert = workflow.index("ci_runner.py convert", start)
		finish = workflow.index('conversion_finished_ns="$(python3', convert)
		stage = workflow.index("ci_runner.py stage-candidate", finish)
		self.assertLess(start, convert)
		self.assertLess(convert, finish)
		self.assertLess(finish, stage)
		conversion_block = workflow[start:finish]
		self.assertIn("--network none", conversion_block)
		self.assertIn("--read-only", conversion_block)
		self.assertNotIn("elapsed-nanoseconds", conversion_block)
		stage_block = workflow[finish:]
		self.assertIn('--elapsed-nanoseconds "${conversion_elapsed_ns}"', stage_block)


if __name__ == "__main__":
	unittest.main()
