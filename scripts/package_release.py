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


def _sha256(data: bytes) -> str:
	return hashlib.sha256(data).hexdigest()


def _find_one(root: Path, filename: str) -> Path:
	matches = list(root.rglob(filename))
	if len(matches) != 1:
		raise ValueError("expected one %s below %s, found %d" % (filename, root, len(matches)))
	return matches[0]


def validate_artifacts(root: Path, version: str) -> Dict[str, Tuple[Path, Path, Dict[str, Any]]]:
	validated: Dict[str, Tuple[Path, Path, Dict[str, Any]]] = {}
	for key, expected in EXPECTED_BINARIES.items():
		binary = _find_one(root, str(expected["filename"]))
		manifest_path = _find_one(root, str(expected["filename"]) + ".manifest.json")
		manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
		checks = {
			"schema": "cocoapdf.binary-manifest/v1",
			"artifact": expected["filename"],
			"version": version,
			"binary_format": expected["format"],
			"architecture": expected["architecture"],
			"python_bits": 64,
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
	artifacts = validate_artifacts(root, version)
	output.mkdir(parents=True, exist_ok=True)

	windows_binary, windows_manifest, _ = artifacts["windows-x86_64"]
	windows_package = output / "cocoapdf-windows-x86_64.zip"
	_zip(
		windows_package,
		[
			("cocoapdf.exe", windows_binary.read_bytes(), 0o755),
			("cocoapdf.exe.manifest.json", windows_manifest.read_bytes(), 0o644),
		],
		epoch,
	)

	linux_binary, linux_manifest, _ = artifacts["linux-x86_64"]
	linux_package = output / "cocoapdf-linux-x86_64.tar.gz"
	_tar_gz(
		linux_package,
		[
			("cocoapdf", linux_binary.read_bytes(), 0o755),
			("cocoapdf.manifest.json", linux_manifest.read_bytes(), 0o644),
		],
		epoch,
	)

	mac_intel, mac_intel_manifest, _ = artifacts["macos-x86_64"]
	mac_arm, mac_arm_manifest, _ = artifacts["macos-arm64"]
	mac_package = output / "cocoapdf-macos.tar.gz"
	mac_readme = (
		"CocoaPDF %s for macOS\n\n"
		"Use cocoapdf-arm64 on Apple Silicon or cocoapdf-x86_64 on Intel Macs.\n"
		% version
	).encode("utf-8")
	_tar_gz(
		mac_package,
		[
			("cocoapdf-arm64", mac_arm.read_bytes(), 0o755),
			("cocoapdf-arm64.manifest.json", mac_arm_manifest.read_bytes(), 0o644),
			("cocoapdf-x86_64", mac_intel.read_bytes(), 0o755),
			("cocoapdf-x86_64.manifest.json", mac_intel_manifest.read_bytes(), 0o644),
			("README.txt", mac_readme, 0o644),
		],
		epoch,
	)

	packages = [windows_package, linux_package, mac_package]
	metadata = {
		"schema": "cocoapdf.release/v1",
		"version": version,
		"tag": "v%s" % version,
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
