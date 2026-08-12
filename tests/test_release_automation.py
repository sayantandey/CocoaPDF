from __future__ import annotations

import base64
import email.parser
import email.policy
import hashlib
import json
import os
import re
import shutil
import struct
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.build_onefile import (
	PINNED_LICENSES,
	PYINSTALLER_VERSION,
	_pinned_license,
	_windows_version_file,
	inspect_binary,
)
from scripts.package_release import EXPECTED_BINARIES, load_release_inputs, package
from scripts.release_version import classify_release, compute_next_version, parse_tag
from scripts.stamp_version import stamp
from tools.update_strategic_raster_fixture import PDF_PATH, SOURCE_PATH, _legacy_pdf, build_pdf
from validation.pr_visual.build import (
	build_scope_and_adversarial_pdf,
	build_tagged_semantics_pdf,
)


class ReleaseVersionTests(unittest.TestCase):
	def test_three_segment_progression_and_idempotence(self):
		self.assertEqual(str(compute_next_version(0, [], "minor")[0]), "0.1.0")
		self.assertEqual(str(compute_next_version(0, [], "patch")[0]), "0.0.1")
		self.assertEqual(str(compute_next_version(0, ["v0.1.0"], "minor")[0]), "0.2.0")
		self.assertEqual(str(compute_next_version(0, ["v0.1.0"], "patch")[0]), "0.1.1")
		version, existing = compute_next_version(
			0,
			["v0.1.0", "not-a-release"],
			"minor",
			head_tags=["v0.1.0"],
		)
		self.assertEqual(str(version), "0.1.0")
		self.assertTrue(existing)

	def test_major_is_manual_and_one_step_only(self):
		version, existing = compute_next_version(1, ["v0.9.4"], "minor", breaking=True)
		self.assertEqual(str(version), "1.0.0")
		self.assertFalse(existing)
		with self.assertRaisesRegex(ValueError, "breaking label"):
			compute_next_version(1, ["v0.9.4"], "minor")
		with self.assertRaisesRegex(ValueError, "exactly one"):
			compute_next_version(2, ["v0.9.4"], "minor", breaking=True)
		with self.assertRaisesRegex(ValueError, "manually incrementing"):
			compute_next_version(0, ["v0.9.4"], "minor", breaking=True)

	def test_release_classification_precedence(self):
		kind, breaking, reason = classify_release({"labels": [{"name": "release:patch"}]})
		self.assertEqual((kind, breaking), ("patch", False))
		self.assertIn("label", reason)
		self.assertEqual(classify_release({"head": {"ref": "hotfix/x"}})[0], "patch")
		self.assertEqual(classify_release({"title": "fix(parser): bounds"})[0], "patch")
		self.assertEqual(classify_release({"title": "Add tables"})[0], "minor")
		with self.assertRaisesRegex(ValueError, "at most one"):
			classify_release({"labels": ["release:patch", "release:minor"]})

	def test_tag_parser_rejects_padding_and_prereleases(self):
		self.assertEqual(str(parse_tag("v12.34.5")), "12.34.5")
		self.assertIsNone(parse_tag("v01.2.3"))
		self.assertIsNone(parse_tag("v1.2.3-rc.1"))


class BuildAutomationTests(unittest.TestCase):
	def test_source_distributions_have_no_runtime_dependencies_and_carry_notices(self):
		root = Path(__file__).resolve().parents[1]
		required_legal_files = (
			"LICENSE",
			"NOTICE",
			"THIRD_PARTY_NOTICES.txt",
		)
		with tempfile.TemporaryDirectory() as directory:
			build_root = Path(directory)
			source = build_root / "source"
			artifacts = build_root / "artifacts"
			source.mkdir()
			artifacts.mkdir()
			for name in (
				"CODE_OF_CONDUCT.md",
				"CONTRIBUTING.md",
				"MANIFEST.in",
				"README.md",
				"SECURITY.md",
				"pyproject.toml",
				*required_legal_files,
			):
				shutil.copy2(root / name, source / name)
			shutil.copytree(root / "licenses", source / "licenses")
			shutil.copytree(
				root / "src" / "cocoapdf",
				source / "src" / "cocoapdf",
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
				wheel_name = build_meta.build_wheel(str(artifacts))
				sdist_name = build_meta.build_sdist(str(artifacts))
			finally:
				os.chdir(previous)

			expected_legal_bytes = {
				name: (root / name).read_bytes()
				for name in required_legal_files
			}
			with zipfile.ZipFile(artifacts / wheel_name) as wheel:
				wheel_legal_files = {
					Path(name).name: name
					for name in wheel.namelist()
					if ".dist-info/" in name
					and Path(name).name in expected_legal_bytes
				}
				self.assertEqual(
					set(wheel_legal_files),
					set(required_legal_files),
				)
				for name, expected in expected_legal_bytes.items():
					self.assertEqual(wheel.read(wheel_legal_files[name]), expected)
				metadata_names = [
					name
					for name in wheel.namelist()
					if name.endswith(".dist-info/METADATA")
				]
				self.assertEqual(len(metadata_names), 1)
				metadata = email.parser.BytesParser(
					policy=email.policy.compat32
				).parsebytes(wheel.read(metadata_names[0]))
				self.assertEqual(metadata["License-Expression"], "MIT")
				self.assertEqual(
					set(metadata.get_all("License-File", [])),
					set(required_legal_files),
				)
				requirements = metadata.get_all("Requires-Dist", [])
				self.assertTrue(requirements)
				for requirement in requirements:
					self.assertRegex(
						requirement,
						r";\s*extra\s*==\s*['\"]build['\"]\s*$",
					)

			with tarfile.open(artifacts / sdist_name, "r:gz") as sdist:
				sdist_legal_files = {
					Path(name).name: name
					for name in sdist.getnames()
					if Path(name).name in expected_legal_bytes
				}
				self.assertEqual(
					set(sdist_legal_files),
					set(required_legal_files),
				)
				for name, expected in expected_legal_bytes.items():
					member = sdist.extractfile(sdist_legal_files[name])
					self.assertIsNotNone(member)
					self.assertEqual(member.read(), expected)

	def test_strategic_raster_fixture_is_reproducible_and_source_matched(self):
		committed_pdf = PDF_PATH.read_bytes()
		rebuilt_pdf, rebuilt_png = build_pdf(_legacy_pdf(committed_pdf))
		self.assertEqual(rebuilt_pdf, committed_pdf)
		source = SOURCE_PATH.read_text(encoding="utf-8")
		match = re.search(r"data:image/png;base64,([A-Za-z0-9+/=]+)", source)
		self.assertIsNotNone(match)
		self.assertEqual(base64.b64decode(match.group(1)), rebuilt_png)
		self.assertIn("Raster image preservation sentinel RASTER-001", source)
		self.assertNotIn("OCR-ONLY", source)

	def test_pr_visual_inputs_are_first_party_and_deterministic(self):
		for builder in (
			build_tagged_semantics_pdf,
			build_scope_and_adversarial_pdf,
		):
			first = builder()
			second = builder()
			self.assertEqual(first, second)
			self.assertTrue(first.startswith(b"%PDF-1.7"))
			self.assertTrue(first.endswith(b"%%EOF\n"))

	def test_pr_visual_workflow_is_ephemeral_and_release_queue_is_lossless(self):
		root = Path(__file__).resolve().parents[1]
		visual = (root / ".github" / "workflows" / "pr-visual-validation.yml").read_text(
			encoding="utf-8"
		)
		visual_report = (root / ".github" / "workflows" / "pr-visual-report.yml").read_text(
			encoding="utf-8"
		)
		visual_reporter = (root / "validation" / "pr_visual" / "report.py").read_text(
			encoding="utf-8"
		)
		release = (root / ".github" / "workflows" / "ci-release.yml").read_text(
			encoding="utf-8"
		)
		pages_path = root / ".github" / "workflows" / "pages.yml"
		self.assertIn("branches: [main]", visual)
		self.assertIn("retention-days: 7", visual)
		self.assertNotIn("actions: write", visual)
		self.assertNotIn("pull-requests: write", visual)
		self.assertNotIn("closed", visual)
		self.assertIn("workflow_run:", visual_report)
		self.assertIn("types: [completed]", visual_report)
		self.assertIn("actions: read", visual_report)
		self.assertIn("pull-requests: write", visual_report)
		self.assertNotIn("actions: write", visual_report)
		self.assertIn(
			"rawcdn.githack.com/${HEAD_REPOSITORY}/${HEAD_SHA}/examples/review.html",
			visual,
		)
		self.assertIn(
			"HEAD_REPOSITORY: ${{ github.event.pull_request.head.repo.full_name }}",
			visual,
		)
		self.assertIn(
			'if [[ "${HEAD_REPOSITORY}" == "${BASE_REPOSITORY}" ]]',
			visual,
		)
		self.assertIn(
			"External HTML preview is intentionally omitted for fork pull requests",
			visual,
		)
		self.assertIn('WORKFLOW_PATH = ".github/workflows/pr-visual-validation.yml"', visual_reporter)
		self.assertIn("MAX_ARTIFACT_BYTES", visual_reporter)
		self.assertNotIn("download-artifact", visual_report)
		self.assertNotIn("pull_request_target:", visual)
		self.assertIn("queue: max", release)
		self.assertIn("if: github.event_name == 'push'", release)
		self.assertIn("python scripts/refresh_examples.py --check", release)
		self.assertLess(
			release.index("python scripts/refresh_examples.py --check"),
			release.index("python scripts/stamp_version.py"),
		)
		self.assertIn("gh release create", release)
		self.assertIn("sha256sum --check SHA256SUMS.txt", release)
		self.assertIn(".third-party-licenses.txt", release)
		self.assertIn('python-version: "3.13.14"', release)
		self.assertFalse(pages_path.exists())

	def test_committed_capability_demo_has_locked_provenance_and_hashes(self):
		root = Path(__file__).resolve().parents[1]
		examples = root / "examples"
		manifest = json.loads((examples / "manifest.json").read_text(encoding="utf-8"))
		self.assertEqual(manifest["schema"], "cocoapdf.capability-demo/v2")
		self.assertEqual(manifest["profile"], "permanent")
		self.assertEqual(manifest["license"]["spdx"], "MIT")
		self.assertTrue(manifest["license"]["third_party_content_added"])
		self.assertEqual(manifest["license"]["network_fetches"], 0)
		self.assertEqual(len(manifest["license"]["third_party"]), 1)
		self.assertFalse(
			manifest["license"]["third_party"][0]["source_content_redistributed"]
		)
		self.assertFalse(manifest["fixture_isolation"]["combined_pdf"])
		self.assertEqual(
			manifest["fixture_isolation"]["catalog_scoped_features"],
			["StructTreeRoot", "ParentTree", "AcroForm", "Outlines"],
		)
		self.assertTrue(manifest["lifecycle"]["committed_outputs"])
		self.assertEqual(manifest["lifecycle"]["location"], "examples")
		self.assertEqual(len(manifest["cases"]), 3)
		self.assertTrue((examples / "README.md").is_file())
		self.assertTrue((examples / "review.html").is_file())
		self.assertFalse((examples / "robots.txt").exists())
		self.assertFalse((examples / "sitemap.xml").exists())
		examples_readme = (examples / "README.md").read_text(encoding="utf-8")
		self.assertTrue(examples_readme.startswith("# CocoaPDF conversion examples\n"))
		self.assertIn(
			"[Open the rendered side-by-side PDF-to-HTML demo from `main`]"
			"(https://raw.githack.com/sayantandey/CocoaPDF/main/examples/review.html)",
			examples_readme,
		)
		self.assertIn(
			"[Browse this revision's committed demo source](review.html)",
			examples_readme,
		)
		self.assertNotIn("rendered HTML on main", examples_readme)
		self.assertIn(
			"[Markdown](cases/strategic_corner_cases/full/output.md)<br/>"
			"[HTML](cases/strategic_corner_cases/full/output.html)<br/>"
			"[Semantic JSON](cases/strategic_corner_cases/full/output.json)<br/>"
			"[Report](cases/strategic_corner_cases/full/output.report.summary.json)",
			examples_readme,
		)
		self.assertIn("## OpenDataLoader-Bench results", examples_readme)
		self.assertIn("`0.9020490607`", examples_readme)
		self.assertIn("`0.9086028983`", examples_readme)
		self.assertIn("`0.9251323351`", examples_readme)
		self.assertIn("`0.8791989022`", examples_readme)
		self.assertIn("**200 evaluated, 200 prediction files, 0 missing, 0 empty, 0 conversion failures**", examples_readme)
		self.assertIn("These numbers do not measure CocoaPDF's HTML fidelity.", examples_readme)
		# The published table is provenance for one commit. Keep it explicit that
		# it says nothing about an uncommitted working tree, so a reader never
		# mistakes a pinned historical score for the current implementation.
		self.assertIn("> Scope: this table pins commit", examples_readme)
		self.assertIn(
			"must never be published under this commit's identifier.",
			examples_readme,
		)
		self.assertEqual(len(manifest["benchmarks"]), 1)
		benchmark = manifest["benchmarks"][0]
		self.assertEqual(benchmark["id"], "opendataloader-bench")
		self.assertEqual(
			benchmark["benchmark_commit"],
			"7af1d8f4d0c09f51ea1a5c6ba5f66e993286d109",
		)
		self.assertEqual(
			benchmark["engine_commit"],
			"59a544a3cfc6e94e72dce4f22f2b334819c818e8",
		)
		self.assertFalse(benchmark["source_content_redistributed"])
		self.assertEqual(
			benchmark["result"]["metrics"]["nid_count"],
			200,
		)
		self.assertEqual(
			benchmark["result"]["metrics"]["teds_count"],
			42,
		)
		self.assertEqual(
			benchmark["result"]["metrics"]["mhs_count"],
			107,
		)
		benchmark_root = (
			examples
			/ "benchmarks"
			/ "opendataloader-bench"
			/ benchmark["benchmark_commit"]
		)
		self.assertFalse(any(benchmark_root.rglob("*.pdf")))
		self.assertFalse(any(benchmark_root.rglob("*.md")))
		self.assertFalse(any(benchmark_root.rglob("*.pyc")))
		self.assertFalse(any(path.name == "__pycache__" for path in benchmark_root.rglob("*")))
		for name, record in benchmark["files"].items():
			data = (benchmark_root / name).read_bytes()
			self.assertEqual(len(data), record["bytes"])
			self.assertEqual(hashlib.sha256(data).hexdigest(), record["sha256"])
		from validation.benchmarks.opendataloader_bench.snapshot import validate_snapshot
		validate_snapshot()
		for index_name in ("README.md", "review.html"):
			text = (examples / index_name).read_text(encoding="utf-8")
			links = re.findall(r"\]\(([^)]+)\)", text)
			links.extend(
				re.findall(r'(?:data|href|src)="([^"]+)"', text)
			)
			for link in links:
				if "://" in link or link.startswith("#"):
					continue
				self.assertTrue(
					(examples / link).is_file(),
					"%s has a broken local link: %s" % (index_name, link),
				)
		for html_path in examples.rglob("*.html"):
			text = html_path.read_text(encoding="utf-8")
			for link in re.findall(r'(?:data|href|src)="([^"]+)"', text):
				if (
					link.startswith("#")
					or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", link)
				):
					continue
				target = html_path.parent / link.split("#", 1)[0]
				self.assertTrue(
					target.is_file(),
					"%s has a broken local link: %s" % (
						html_path.relative_to(root),
						link,
					),
				)
		for case in manifest["cases"]:
			self.assertEqual(case["provenance"]["license"], "MIT")
			case_root = examples / "cases" / case["id"]
			input_path = case_root / "input.pdf"
			input_data = input_path.read_bytes()
			self.assertEqual(len(input_data), case["input"]["bytes"])
			self.assertEqual(
				hashlib.sha256(input_data).hexdigest(),
				case["input"]["sha256"],
			)
			for conversion in case["conversions"]:
				self.assertTrue(conversion["passed"])
				conversion_root = case_root / conversion["name"]
				for relative, expected in conversion["files"].items():
					data = (conversion_root / relative).read_bytes()
					self.assertEqual(len(data), expected["bytes"])
					self.assertEqual(
						hashlib.sha256(data).hexdigest(),
						expected["sha256"],
					)

	def test_release_inputs_lock_the_actual_windows_icon(self):
		root = Path(__file__).resolve().parents[1]
		release_inputs = load_release_inputs()
		icon = release_inputs["windows_icon"]
		icon_path = root / icon["path"]
		icon_bytes = icon_path.read_bytes()
		self.assertEqual(icon["bytes"], len(icon_bytes))
		self.assertEqual(icon["sha256"], hashlib.sha256(icon_bytes).hexdigest())
		self.assertTrue(release_inputs["license"].startswith(b"MIT License"))

	def test_pinned_runtime_licenses_are_complete_and_digest_locked(self):
		root = Path(__file__).resolve().parents[1]
		for component, (relative, expected_sha256) in PINNED_LICENSES.items():
			name, data = _pinned_license(root, component)
			self.assertEqual(name, relative.name)
			self.assertGreater(len(data), 10_000)
			self.assertEqual(hashlib.sha256(data).hexdigest(), expected_sha256)
		pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
		self.assertIn('pyinstaller==%s' % PYINSTALLER_VERSION, pyproject)
		notices = (root / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
		self.assertIn(
			"fd17997c3866d61e0e7bd8201b1d8f35b40a40bd/LICENSE",
			notices,
		)
		self.assertIn(
			"1cda34561015a90b6e1ae31dc89703799adaf13e/COPYING.txt",
			notices,
		)

	def test_binary_header_inspection(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			pe = bytearray(192)
			pe[:2] = b"MZ"
			struct.pack_into("<I", pe, 0x3C, 128)
			pe[128:132] = b"PE\x00\x00"
			struct.pack_into("<H", pe, 132, 0x8664)
			struct.pack_into("<H", pe, 152, 0x020B)
			(root / "app.exe").write_bytes(pe)
			self.assertEqual(inspect_binary(root / "app.exe"), ("PE", "x86_64", 64))

			elf = bytearray(64)
			elf[:6] = b"\x7fELF\x02\x01"
			struct.pack_into("<H", elf, 18, 62)
			(root / "app-linux").write_bytes(elf)
			self.assertEqual(inspect_binary(root / "app-linux"), ("ELF", "x86_64", 64))

			macho = b"\xcf\xfa\xed\xfe" + struct.pack("<I", 0x0100000C) + b"\x00" * 24
			(root / "app-macos").write_bytes(macho)
			self.assertEqual(inspect_binary(root / "app-macos"), ("Mach-O", "arm64", 64))

	def test_windows_metadata_uses_three_segment_release_version(self):
		metadata = _windows_version_file("12.34.5")
		self.assertIn("filevers=(12, 34, 5, 0)", metadata)
		self.assertIn("CompanyName', u'Sayantan Dey", metadata)
		self.assertIn("ProductName', u'CocoaPDF", metadata)
		self.assertIn("ProductVersion', u'12.34.5", metadata)
		with self.assertRaisesRegex(ValueError, "unpadded"):
			_windows_version_file("01.2.3")

	def test_stamp_updates_both_build_inputs(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "src" / "cocoapdf").mkdir(parents=True)
			(root / "src" / "cocoapdf" / "_version.py").write_text(
				'__version__ = "0.1.0"\n', encoding="utf-8"
			)
			(root / "pyproject.toml").write_text(
				'[project]\nversion = "0.1.0"\n', encoding="utf-8"
			)
			stamp(root, "2.7.3")
			self.assertIn("2.7.3", (root / "pyproject.toml").read_text(encoding="utf-8"))
			self.assertIn("2.7.3", (root / "src" / "cocoapdf" / "_version.py").read_text(encoding="utf-8"))

	def test_release_packages_validate_manifests_and_names(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			artifacts = root / "artifacts"
			release_inputs = load_release_inputs()
			for key, expected in EXPECTED_BINARIES.items():
				# Match download-artifact's real layout.  For Linux, the artifact
				# directory and the executable intentionally have the same name.
				folder = artifacts / ("cocoapdf-" + key)
				folder.mkdir(parents=True)
				binary = folder / str(expected["filename"])
				data = ("binary-%s" % key).encode("ascii")
				binary.write_bytes(data)
				third_party = folder / (
					str(expected["filename"]) + ".third-party-licenses.txt"
				)
				third_party_data = (
					"third-party notices for %s\n" % key
				).encode("ascii")
				third_party.write_bytes(third_party_data)
				icon_embedded = expected["format"] == "PE"
				manifest = {
					"schema": "cocoapdf.binary-manifest/v2",
					"product": "CocoaPDF",
					"artifact": expected["filename"],
					"version": "1.2.3",
					"binary_format": expected["format"],
					"architecture": expected["architecture"],
					"python_bits": 64,
					"bytes": len(data),
					"sha256": hashlib.sha256(data).hexdigest(),
					"icon_embedded": icon_embedded,
					"icon_source": (
						release_inputs["windows_icon"]
						if icon_embedded
						else None
					),
					"third_party_licenses": {
						"filename": third_party.name,
						"bytes": len(third_party_data),
						"sha256": hashlib.sha256(third_party_data).hexdigest(),
					},
				}
				(folder / (str(expected["filename"]) + ".manifest.json")).write_text(
					json.dumps(manifest), encoding="utf-8"
				)

			output = root / "release"
			created = package(artifacts, output, "1.2.3", 315532800)
			self.assertEqual(len(created), 5)
			with zipfile.ZipFile(output / "cocoapdf-windows-x86_64.zip") as archive:
				self.assertEqual(
					set(archive.namelist()),
					{
						"cocoapdf.exe",
						"LICENSE.txt",
						"THIRD_PARTY_NOTICES.txt",
					},
				)
				self.assertTrue(archive.read("LICENSE.txt").startswith(b"MIT License"))
				self.assertIn(
					b"third-party notices for windows-x86_64",
					archive.read("THIRD_PARTY_NOTICES.txt"),
				)
			with tarfile.open(output / "cocoapdf-linux-x86_64.tar.gz", "r:gz") as archive:
				self.assertEqual(
					set(archive.getnames()),
					{"cocoapdf", "LICENSE.txt", "THIRD_PARTY_NOTICES.txt"},
				)
				self.assertEqual(archive.getmember("cocoapdf").mode, 0o755)
			with tarfile.open(output / "cocoapdf-macos.tar.gz", "r:gz") as archive:
				self.assertEqual(
					set(archive.getnames()),
					{
						"cocoapdf-arm64",
						"cocoapdf-x86_64",
						"LICENSE.txt",
						"THIRD_PARTY_NOTICES-arm64.txt",
						"THIRD_PARTY_NOTICES-x86_64.txt",
					},
				)
				self.assertEqual(archive.getmember("cocoapdf-arm64").mode, 0o755)
				self.assertEqual(archive.getmember("cocoapdf-x86_64").mode, 0o755)
			metadata = json.loads((output / "RELEASE.json").read_text(encoding="utf-8"))
			self.assertEqual(metadata["schema"], "cocoapdf.release/v2")
			self.assertEqual(metadata["product"], "CocoaPDF")
			self.assertEqual(
				metadata["application_icon"]["sha256"],
				release_inputs["windows_icon"]["sha256"],
			)
			self.assertTrue(
				metadata["application_icon"]["embedded_in_windows_executable"]
			)
			self.assertEqual(set(metadata["binaries"]), set(EXPECTED_BINARIES))
			self.assertEqual(
				metadata["binaries"]["windows-x86_64"]["sha256"],
				hashlib.sha256(b"binary-windows-x86_64").hexdigest(),
			)
			self.assertEqual(
				metadata["package_contents"]["cocoapdf-windows-x86_64.zip"],
				["cocoapdf.exe", "LICENSE.txt", "THIRD_PARTY_NOTICES.txt"],
			)
			checksums = (output / "SHA256SUMS.txt").read_text(encoding="ascii")
			self.assertIn("cocoapdf-windows-x86_64.zip", checksums)

			second_output = root / "release-again"
			package(artifacts, second_output, "1.2.3", 315532800)
			for path in created:
				self.assertEqual(
					path.read_bytes(),
					(second_output / path.name).read_bytes(),
					path.name,
				)


if __name__ == "__main__":
	unittest.main()
