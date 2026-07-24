from __future__ import annotations

import hashlib
import json
import struct
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.build_onefile import _windows_version_file, inspect_binary
from scripts.package_release import EXPECTED_BINARIES, load_branding, package
from scripts.release_version import classify_release, compute_next_version, parse_tag
from scripts.stamp_version import stamp
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
		release = (root / ".github" / "workflows" / "ci-release.yml").read_text(
			encoding="utf-8"
		)
		self.assertIn("branches: [main]", visual)
		self.assertIn("retention-days: 7", visual)
		self.assertIn("github.event.action == 'closed'", visual)
		self.assertIn("actions: write", visual)
		self.assertIn("pull-requests: write", visual)
		self.assertNotIn("pull_request_target:", visual)
		self.assertIn("queue: max", release)
		self.assertIn("if: github.event_name == 'push'", release)
		self.assertIn("gh release create", release)
		self.assertIn("sha256sum --check SHA256SUMS.txt", release)

	def test_brand_manifest_inventory_is_complete(self):
		branding = load_branding()
		self.assertEqual(branding["asset_count"], 47)
		self.assertEqual(branding["version"], "1.0.2")
		self.assertEqual(len(branding["manifest_sha256"]), 64)

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
			branding = load_branding()
			for key, expected in EXPECTED_BINARIES.items():
				# Match download-artifact's real layout.  For Linux, the artifact
				# directory and the executable intentionally have the same name.
				folder = artifacts / ("cocoapdf-" + key)
				folder.mkdir(parents=True)
				binary = folder / str(expected["filename"])
				data = ("binary-%s" % key).encode("ascii")
				binary.write_bytes(data)
				manifest = {
					"schema": "cocoapdf.binary-manifest/v1",
					"product": "CocoaPDF",
					"artifact": expected["filename"],
					"version": "1.2.3",
					"binary_format": expected["format"],
					"architecture": expected["architecture"],
					"python_bits": 64,
					"bytes": len(data),
					"sha256": hashlib.sha256(data).hexdigest(),
					"brand_manifest_version": branding["version"],
					"brand_manifest_sha256": branding["manifest_sha256"],
					"icon_embedded": expected["format"] == "PE",
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
					{"cocoapdf.exe", "LICENSE.txt"},
				)
				self.assertTrue(archive.read("LICENSE.txt").startswith(b"MIT License"))
			with tarfile.open(output / "cocoapdf-linux-x86_64.tar.gz", "r:gz") as archive:
				self.assertEqual(
					set(archive.getnames()),
					{"cocoapdf", "LICENSE.txt"},
				)
				self.assertEqual(archive.getmember("cocoapdf").mode, 0o755)
			with tarfile.open(output / "cocoapdf-macos.tar.gz", "r:gz") as archive:
				self.assertEqual(
					set(archive.getnames()),
					{"cocoapdf-arm64", "cocoapdf-x86_64", "LICENSE.txt"},
				)
				self.assertEqual(archive.getmember("cocoapdf-arm64").mode, 0o755)
				self.assertEqual(archive.getmember("cocoapdf-x86_64").mode, 0o755)
			metadata = json.loads((output / "RELEASE.json").read_text(encoding="utf-8"))
			self.assertEqual(metadata["product"], "CocoaPDF")
			self.assertEqual(metadata["branding"]["manifest_version"], branding["version"])
			self.assertEqual(metadata["branding"]["asset_count"], 47)
			self.assertTrue(metadata["branding"]["windows_executable_icon_embedded"])
			self.assertEqual(set(metadata["binaries"]), set(EXPECTED_BINARIES))
			self.assertEqual(
				metadata["binaries"]["windows-x86_64"]["sha256"],
				hashlib.sha256(b"binary-windows-x86_64").hexdigest(),
			)
			self.assertEqual(
				metadata["package_contents"]["cocoapdf-windows-x86_64.zip"],
				["cocoapdf.exe", "LICENSE.txt"],
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
