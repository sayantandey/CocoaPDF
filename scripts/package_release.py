from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import tarfile
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


EXPECTED_BINARIES = {
	"windows-x86_64": {
		"filename": "cocoapdf-windows-x86_64.exe",
		"format": "PE",
		"architecture": "x86_64",
	},
	"linux-x86_64": {
		"filename": "cocoapdf-linux-x86_64",
		"format": "ELF",
		"architecture": "x86_64",
	},
	"macos-x86_64": {
		"filename": "cocoapdf-macos-x86_64",
		"format": "Mach-O",
		"architecture": "x86_64",
	},
	"macos-arm64": {
		"filename": "cocoapdf-macos-arm64",
		"format": "Mach-O",
		"architecture": "arm64",
	},
}

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BRAND_MANIFEST_NAME = "BRAND_ASSET_MANIFEST.json"


def _sha256(data: bytes) -> str:
	return hashlib.sha256(data).hexdigest()


def _find_one(root: Path, filename: str) -> Path:
	# download-artifact creates a directory with the artifact name.  The Linux
	# artifact directory and executable are both named cocoapdf-linux-x86_64,
	# so only filesystem entries that are actual files may be considered.
	matches = [path for path in root.rglob(filename) if path.is_file()]
	if len(matches) != 1:
		raise ValueError("expected one %s below %s, found %d" % (filename, root, len(matches)))
	return matches[0]


def load_branding(project_root: Path = PROJECT_ROOT) -> Dict[str, Any]:
	brand_root = project_root / "docs" / "assets" / "brand"
	manifest_path = brand_root / BRAND_MANIFEST_NAME
	try:
		manifest_bytes = manifest_path.read_bytes()
		canonical_manifest = manifest_bytes.replace(b"\r\n", b"\n")
		manifest = json.loads(canonical_manifest.decode("utf-8"))
	except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
		raise ValueError("cannot read CocoaPDF brand manifest: %s" % exc) from exc

	if manifest.get("project") != "CocoaPDF" or not isinstance(manifest.get("version"), str):
		raise ValueError("invalid CocoaPDF brand manifest identity")
	records = manifest.get("files")
	if not isinstance(records, list) or manifest.get("asset_count") != len(records):
		raise ValueError("brand manifest asset count is inconsistent")
	listed_sizes = {
		str(item.get("path")): item.get("bytes")
		for item in records
		if isinstance(item, dict)
	}
	if len(listed_sizes) != len(records):
		raise ValueError("brand manifest contains invalid or duplicate file records")
	actual_paths = {
		path.relative_to(brand_root).as_posix()
		for path in brand_root.rglob("*")
		if path.is_file()
	}
	if actual_paths != set(listed_sizes):
		missing = sorted(set(listed_sizes) - actual_paths)
		extra = sorted(actual_paths - set(listed_sizes))
		raise ValueError("brand asset inventory mismatch; missing=%r extra=%r" % (missing, extra))
	text_suffixes = {".json", ".md", ".svg", ".webmanifest"}
	for relative, expected_size in listed_sizes.items():
		data = (brand_root / relative).read_bytes()
		# The untracked local kit may retain CRLF until Git first normalizes it;
		# repository checkouts are LF per .gitattributes. Accept either byte form.
		normalized = data.replace(b"\r\n", b"\n") if Path(relative).suffix in text_suffixes else data
		if expected_size not in {len(data), len(normalized)}:
			raise ValueError("brand asset %s does not match its manifest" % relative)
	selected_paths = {
		"windows_icon": "icons/app/cocoapdf-app-icon.ico",
		"portable_icon": "icons/app/cocoapdf-app-icon-512x512.png",
	}
	selected: Dict[str, bytes] = {}
	for key, relative in selected_paths.items():
		path = brand_root / relative
		try:
			data = path.read_bytes()
		except OSError as exc:
			raise ValueError("cannot read required brand asset %s: %s" % (relative, exc)) from exc
		if listed_sizes.get(relative) != len(data):
			raise ValueError("brand asset %s does not match its manifest" % relative)
		selected[key] = data

	try:
		license_bytes = (project_root / "LICENSE").read_bytes()
		notice_bytes = (project_root / "NOTICE").read_bytes()
	except OSError as exc:
		raise ValueError("cannot read release legal files: %s" % exc) from exc

	return {
		"version": manifest["version"],
		"asset_count": manifest["asset_count"],
		"manifest": canonical_manifest,
		"manifest_sha256": _sha256(canonical_manifest),
		"license": license_bytes,
		"notice": notice_bytes,
		**selected,
	}


def validate_artifacts(
	root: Path,
	version: str,
	branding: Optional[Dict[str, Any]] = None,
) -> Dict[str, Tuple[Path, Path, Dict[str, Any]]]:
	branding = branding or load_branding()
	validated: Dict[str, Tuple[Path, Path, Dict[str, Any]]] = {}
	for key, expected in EXPECTED_BINARIES.items():
		binary = _find_one(root, str(expected["filename"]))
		manifest_path = _find_one(root, str(expected["filename"]) + ".manifest.json")
		manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
		checks = {
			"schema": "cocoapdf.binary-manifest/v1",
			"product": "CocoaPDF",
			"artifact": expected["filename"],
			"version": version,
			"binary_format": expected["format"],
			"architecture": expected["architecture"],
			"python_bits": 64,
			"brand_manifest_version": branding["version"],
			"brand_manifest_sha256": branding["manifest_sha256"],
			"icon_embedded": expected["format"] == "PE",
		}
		for field, value in checks.items():
			if manifest.get(field) != value:
				raise ValueError(
					"%s manifest %s=%r, expected %r"
					% (key, field, manifest.get(field), value)
				)
		binary_bytes = binary.read_bytes()
		if manifest.get("bytes") != len(binary_bytes):
			raise ValueError("%s byte count does not match its manifest" % key)
		if manifest.get("sha256") != _sha256(binary_bytes):
			raise ValueError("%s digest does not match its manifest" % key)
		validated[key] = (binary, manifest_path, manifest)
	return validated


def _package_readme(platform_name: str, version: str) -> bytes:
	if platform_name == "Windows x86_64":
		run = "Run cocoapdf.exe from PowerShell or Command Prompt."
		trust = "This build is not code-signed."
	elif platform_name.startswith("Linux x86_64"):
		run = "Make cocoapdf executable if needed (chmod +x cocoapdf), then run ./cocoapdf."
		trust = "This build is not code-signed."
	else:
		run = (
			"Run ./cocoapdf-arm64 on Apple Silicon or ./cocoapdf-x86_64 on Intel Macs. "
			"Make the selected file executable with chmod +x if needed."
		)
		trust = "These builds are not code-signed or notarized."
	return (
		"CocoaPDF %s for %s\n"
		"PDF to Markdown/HTML for structured text-layer PDFs. No OCR. No AI.\n\n"
		"%s\n\n"
		"%s Verify the package "
		"against SHA256SUMS.txt and RELEASE.json on the GitHub release page.\n"
		% (version, platform_name, run, trust)
	).encode("utf-8")


def _common_entries(branding: Dict[str, Any], platform_name: str, version: str) -> List[Tuple[str, bytes, int]]:
	icon_name = "CocoaPDF.ico" if platform_name == "Windows x86_64" else "CocoaPDF.png"
	icon = branding["windows_icon"] if platform_name == "Windows x86_64" else branding["portable_icon"]
	return [
		("README.txt", _package_readme(platform_name, version), 0o644),
		("LICENSE.txt", branding["license"], 0o644),
		("NOTICE.txt", branding["notice"], 0o644),
		(BRAND_MANIFEST_NAME, branding["manifest"], 0o644),
		(icon_name, icon, 0o644),
	]


def _zip(output: Path, entries: Iterable[Tuple[str, bytes, int]], epoch: int) -> None:
	date_time = tuple(__import__("time").gmtime(max(epoch, 315532800))[:6])
	with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
		for name, data, mode in entries:
			info = zipfile.ZipInfo(name, date_time=date_time)
			info.compress_type = zipfile.ZIP_DEFLATED
			info.create_system = 3
			info.external_attr = (mode & 0xFFFF) << 16
			archive.writestr(info, data)


def _tar_gz(output: Path, entries: Iterable[Tuple[str, bytes, int]], epoch: int) -> None:
	with output.open("wb") as raw:
		with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=epoch) as compressed:
			with tarfile.open(fileobj=compressed, mode="w") as archive:
				for name, data, mode in entries:
					info = tarfile.TarInfo(name)
					info.size = len(data)
					info.mode = mode
					info.mtime = epoch
					info.uid = 0
					info.gid = 0
					info.uname = "root"
					info.gname = "root"
					archive.addfile(info, __import__("io").BytesIO(data))


def package(root: Path, output: Path, version: str, epoch: int) -> List[Path]:
	branding = load_branding()
	artifacts = validate_artifacts(root, version, branding)
	output.mkdir(parents=True, exist_ok=True)

	windows_binary, windows_manifest, _ = artifacts["windows-x86_64"]
	windows_package = output / "cocoapdf-windows-x86_64.zip"
	_zip(
		windows_package,
		[
			("cocoapdf.exe", windows_binary.read_bytes(), 0o755),
			("cocoapdf.exe.manifest.json", windows_manifest.read_bytes(), 0o644),
		] + _common_entries(branding, "Windows x86_64", version),
		epoch,
	)

	linux_binary, linux_manifest, _ = artifacts["linux-x86_64"]
	linux_package = output / "cocoapdf-linux-x86_64.tar.gz"
	_tar_gz(
		linux_package,
		[
			("cocoapdf", linux_binary.read_bytes(), 0o755),
			("cocoapdf.manifest.json", linux_manifest.read_bytes(), 0o644),
		] + _common_entries(branding, "Linux x86_64 (Ubuntu 22.04 build baseline)", version),
		epoch,
	)

	mac_intel, mac_intel_manifest, _ = artifacts["macos-x86_64"]
	mac_arm, mac_arm_manifest, _ = artifacts["macos-arm64"]
	mac_package = output / "cocoapdf-macos.tar.gz"
	_tar_gz(
		mac_package,
		[
			("cocoapdf-arm64", mac_arm.read_bytes(), 0o755),
			("cocoapdf-arm64.manifest.json", mac_arm_manifest.read_bytes(), 0o644),
			("cocoapdf-x86_64", mac_intel.read_bytes(), 0o755),
			("cocoapdf-x86_64.manifest.json", mac_intel_manifest.read_bytes(), 0o644),
		] + _common_entries(branding, "macOS (Apple Silicon and Intel)", version),
		epoch,
	)

	packages = [windows_package, linux_package, mac_package]
	metadata = {
		"schema": "cocoapdf.release/v1",
		"product": "CocoaPDF",
		"version": version,
		"tag": "v%s" % version,
		"branding": {
			"manifest_version": branding["version"],
			"manifest_sha256": branding["manifest_sha256"],
			"windows_executable_icon_embedded": True,
			"package_icons": {
				"windows": "CocoaPDF.ico",
				"linux": "CocoaPDF.png",
				"macos": "CocoaPDF.png",
			},
		},
		"packages": {
			path.name: {"bytes": path.stat().st_size, "sha256": _sha256(path.read_bytes())}
			for path in packages
		},
		"binaries": {key: value[2] for key, value in sorted(artifacts.items())},
	}
	metadata_path = output / "RELEASE.json"
	metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")

	checksummed = packages + [metadata_path]
	checksums = "".join(
		"%s  %s\n" % (_sha256(path.read_bytes()), path.name)
		for path in sorted(checksummed, key=lambda item: item.name)
	)
	checksums_path = output / "SHA256SUMS.txt"
	checksums_path.write_text(checksums, encoding="ascii")
	return checksummed + [checksums_path]


def main(argv: Optional[Sequence[str]] = None) -> int:
	parser = argparse.ArgumentParser(description="Validate and package CocoaPDF native binaries")
	parser.add_argument("--artifacts", type=Path, required=True)
	parser.add_argument("--output", type=Path, required=True)
	parser.add_argument("--version", required=True)
	args = parser.parse_args(argv)
	epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "315532800"))
	for path in package(args.artifacts, args.output, args.version, epoch):
		print(path)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
