from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cocoapdf import __version__
from scripts.verify_pypi_artifacts import verify_local, verify_remote


class PyPIWorkflowTests(unittest.TestCase):
	def test_trusted_publisher_is_isolated_and_least_privilege(self):
		root = Path(__file__).resolve().parents[1]
		workflow = (root / ".github/workflows/publish-pypi.yml").read_text(encoding="utf-8")
		release = (root / ".github/workflows/ci-release.yml").read_text(encoding="utf-8")

		self.assertIn('workflows: ["CI and release"]', workflow)
		self.assertIn("types: [completed]", workflow)
		self.assertIn("github.event.workflow_run.conclusion == 'success'", workflow)
		self.assertIn("github.event.workflow_run.event == 'push'", workflow)
		self.assertIn("github.event.workflow_run.head_branch == 'main'", workflow)
		self.assertIn(
			"github.event.workflow_run.head_repository.full_name == github.repository",
			workflow,
		)
		self.assertIn("ref: ${{ github.event.workflow_run.head_sha }}", workflow)
		self.assertIn("fetch-depth: 0", workflow)
		self.assertGreaterEqual(workflow.count("persist-credentials: false"), 3)
		self.assertIn("environment: pypi", workflow)
		self.assertEqual(workflow.count("id-token: write"), 1)
		self.assertNotIn("secrets.", workflow)
		self.assertNotIn("PYPI_TOKEN", workflow)
		self.assertNotIn("UV_PUBLISH_TOKEN", workflow)
		self.assertNotIn("TWINE_PASSWORD", workflow)
		self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_TOKEN", workflow)
		self.assertNotIn("password:", workflow)
		self.assertNotIn("pypa/gh-action-pypi-publish", workflow)
		self.assertIn("uv==0.12.0", workflow)
		self.assertIn(
			"sha256:cbff74f884846d794713670faf8abe10db3bd70c43b01e63223f74eb7d958689",
			workflow,
		)
		self.assertIn("--only-binary=:all:", workflow)
		self.assertIn("--require-hashes", workflow)
		self.assertIn("--trusted-publishing always", workflow)
		self.assertIn("--publish-url https://upload.pypi.org/legacy/", workflow)
		self.assertIn("--check-url https://pypi.org/simple/", workflow)
		self.assertIn("--no-config", workflow)

		actions = re.findall(r"^\s*uses:\s*([^#\s]+)", workflow, flags=re.MULTILINE)
		self.assertTrue(actions)
		for action in actions:
			self.assertTrue(
				action.startswith("actions/"),
				"publisher workflow uses an action forbidden by repository policy: %s" % action,
			)
			self.assertRegex(
				action,
				r"^actions/[A-Za-z0-9_.-]+@[0-9a-f]{40}$",
				"every publisher action must be GitHub-owned and pinned to a full SHA",
			)

		self.assertIn("scripts/verify_pypi_artifacts.py local", workflow)
		self.assertIn("scripts/verify_pypi_artifacts.py remote", workflow)
		self.assertIn('"cocoapdf==${VERSION}"', workflow)
		self.assertIn("examples/cases/tagged_semantics/full/output.md", workflow)

		self.assertIn("queue: max", release)
		self.assertIn("gh release create", release)
		self.assertIn("sha256sum --check SHA256SUMS.txt", release)
		self.assertNotIn("id-token: write", release)
		self.assertNotIn("gh-action-pypi-publish", release)


class PyPIArtifactVerificationTests(unittest.TestCase):
	def _build_distributions(self, source: Path, artifacts: Path) -> None:
		root = Path(__file__).resolve().parents[1]
		for name in (
			"CODE_OF_CONDUCT.md",
			"CONTRIBUTING.md",
			"MANIFEST.in",
			"README.md",
			"SECURITY.md",
			"pyproject.toml",
			"LICENSE",
			"NOTICE",
			"THIRD_PARTY_NOTICES.txt",
		):
			shutil.copy2(root / name, source / name)
		shutil.copytree(root / "licenses", source / "licenses")
		shutil.copytree(
			root / "src/cocoapdf",
			source / "src/cocoapdf",
			ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
		)
		for relative in (
			Path("docs/assets/brand/logo/cocoapdf-mark.svg"),
			Path("docs/assets/brand/icons/app/cocoapdf-app-icon.ico"),
			Path("docs/assets/brand/source/cocoapdf-brand-tokens.json"),
			Path("docs/assets/brand/source/cocoapdf-logo-construction.svg"),
		):
			target = source / relative
			target.parent.mkdir(parents=True, exist_ok=True)
			shutil.copy2(root / relative, target)

		from setuptools import build_meta

		previous = Path.cwd()
		try:
			os.chdir(source)
			build_meta.build_wheel(str(artifacts))
			build_meta.build_sdist(str(artifacts))
		finally:
			os.chdir(previous)

	def test_local_artifacts_are_release_exact_and_tamper_evident(self):
		with tempfile.TemporaryDirectory() as directory:
			work = Path(directory)
			source = work / "source"
			artifacts = work / "dist"
			source.mkdir()
			artifacts.mkdir()
			self._build_distributions(source, artifacts)

			result = verify_local(source, artifacts, __version__)
			self.assertEqual(result["project"], "cocoapdf")
			self.assertEqual(result["version"], __version__)
			self.assertEqual(len(result["files"]), 2)

			(source / "NOTICE").write_text("tampered\n", encoding="utf-8")
			with self.assertRaisesRegex(ValueError, "legal file changed"):
				verify_local(source, artifacts, __version__)

	def test_remote_verification_requires_exact_pypi_hashes(self):
		version = "9.8.7"
		with tempfile.TemporaryDirectory() as directory:
			dist = Path(directory)
			files = {
				"cocoapdf-9.8.7-py3-none-any.whl": b"wheel",
				"cocoapdf-9.8.7.tar.gz": b"sdist",
			}
			for name, content in files.items():
				(dist / name).write_bytes(content)
			payload = {
				"info": {"name": "CocoaPDF", "version": version},
				"urls": [
					{
						"filename": name,
						"packagetype": "bdist_wheel" if name.endswith(".whl") else "sdist",
						"digests": {"sha256": hashlib.sha256(content).hexdigest()},
					}
					for name, content in files.items()
				],
			}
			response = io.BytesIO(json.dumps(payload).encode("utf-8"))
			with mock.patch("urllib.request.urlopen", return_value=response):
				result = verify_remote(dist, version, attempts=1, delay_seconds=0)
			self.assertEqual(result["version"], version)
			self.assertEqual(set(result["files"]), set(files))

			payload["urls"][0]["digests"]["sha256"] = "0" * 64
			response = io.BytesIO(json.dumps(payload).encode("utf-8"))
			with mock.patch("urllib.request.urlopen", return_value=response):
				with self.assertRaisesRegex(ValueError, "differ"):
					verify_remote(dist, version, attempts=1, delay_seconds=0)


if __name__ == "__main__":
	unittest.main()
