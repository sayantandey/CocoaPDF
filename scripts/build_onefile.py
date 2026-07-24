from __future__ import annotations

import argparse
import hashlib
from importlib import metadata as importlib_metadata
import json
import os
import platform
import re
import struct
import subprocess
import sys
from pathlib import Path
from typing import Dict, Tuple


PE_MACHINES = {
	0x014C: "x86",
	0x8664: "x86_64",
	0xAA64: "arm64",
}
ELF_MACHINES = {
	3: "x86",
	62: "x86_64",
	183: "arm64",
}
MACHO_CPUS = {
	0x00000007: "x86",
	0x01000007: "x86_64",
	0x0100000C: "arm64",
}

VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def _normalized_text_bytes(path: Path) -> bytes:
	try:
		data = path.read_bytes()
	except OSError as exc:
		raise ValueError("cannot read license file %s: %s" % (path, exc)) from exc
	try:
		data.decode("utf-8")
	except UnicodeDecodeError as exc:
		raise ValueError("license file is not UTF-8: %s" % path) from exc
	return data.replace(b"\r\n", b"\n")


def _python_license() -> Tuple[str, bytes]:
	bases = []
	for candidate in (
		Path(sys.base_prefix),
		Path(sys.prefix),
		Path(sys.executable).resolve().parent,
		Path(sys.executable).resolve().parent.parent,
	):
		if candidate not in bases:
			bases.append(candidate)
	for base in bases:
		for filename in ("LICENSE.txt", "LICENSE", "LICENSE.rst"):
			path = base / filename
			if path.is_file():
				return filename, _normalized_text_bytes(path)
	raise ValueError(
		"cannot locate the CPython license beside interpreter %s"
		% sys.executable
	)


def _pyinstaller_license() -> Tuple[str, str, bytes]:
	try:
		distribution = importlib_metadata.distribution("pyinstaller")
	except importlib_metadata.PackageNotFoundError as exc:
		raise ValueError("PyInstaller distribution metadata is unavailable") from exc
	matches = []
	for entry in distribution.files or ():
		name = Path(str(entry)).name.casefold()
		if name != "copying.txt":
			continue
		path = Path(distribution.locate_file(entry))
		if path.is_file():
			matches.append(path)
	if not matches:
		raise ValueError("cannot locate PyInstaller COPYING.txt in the installed wheel")
	path = sorted(matches, key=lambda item: item.as_posix())[0]
	return distribution.version, path.name, _normalized_text_bytes(path)


def _license_section(title: str, data: bytes) -> bytes:
	return (
		"\n\n\n%s\n%s\n\n" % (title, "=" * len(title))
	).encode("utf-8") + data.rstrip()


def build_third_party_license_bundle(root: Path) -> bytes:
	notice_path = root / "THIRD_PARTY_NOTICES.txt"
	if not notice_path.is_file():
		raise ValueError("missing repository third-party notice: %s" % notice_path)
	python_name, python_license = _python_license()
	pyinstaller_version, pyinstaller_name, pyinstaller_license = _pyinstaller_license()
	sections = [
		_normalized_text_bytes(notice_path).rstrip(),
		_license_section(
			"Exact CPython %s license (%s)"
			% (platform.python_version(), python_name),
			python_license,
		),
		_license_section(
			"Exact PyInstaller %s license (%s)"
			% (pyinstaller_version, pyinstaller_name),
			pyinstaller_license,
		),
	]
	return b"".join(sections) + b"\n"


def inspect_binary(path: Path) -> Tuple[str, str, int]:
	"""Return (format, architecture, bits) from the executable header."""
	with path.open("rb") as stream:
		header = stream.read(64)
		if header[:2] == b"MZ":
			if len(header) < 64:
				raise ValueError("truncated PE header")
			pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
			stream.seek(pe_offset)
			pe = stream.read(26)
			if len(pe) < 26 or pe[:4] != b"PE\x00\x00":
				raise ValueError("invalid PE signature")
			machine = struct.unpack_from("<H", pe, 4)[0]
			optional_magic = struct.unpack_from("<H", pe, 24)[0]
			bits = {0x010B: 32, 0x020B: 64}.get(optional_magic)
			if bits is None:
				raise ValueError("unknown PE optional-header magic 0x%04x" % optional_magic)
			return "PE", PE_MACHINES.get(machine, "unknown-0x%04x" % machine), bits

	if header[:4] == b"\x7fELF":
		bits = {1: 32, 2: 64}.get(header[4])
		byte_order = {1: "<", 2: ">"}.get(header[5])
		if bits is None or byte_order is None:
			raise ValueError("invalid ELF class or byte order")
		machine = struct.unpack_from(byte_order + "H", header, 18)[0]
		return "ELF", ELF_MACHINES.get(machine, "unknown-%d" % machine), bits

	macho_formats = {
		b"\xce\xfa\xed\xfe": ("<", 32),
		b"\xfe\xed\xfa\xce": (">", 32),
		b"\xcf\xfa\xed\xfe": ("<", 64),
		b"\xfe\xed\xfa\xcf": (">", 64),
	}
	if header[:4] in macho_formats:
		byte_order, bits = macho_formats[header[:4]]
		cpu = struct.unpack_from(byte_order + "I", header, 4)[0]
		return "Mach-O", MACHO_CPUS.get(cpu, "unknown-0x%08x" % cpu), bits

	raise ValueError("unrecognized executable format")


def _smoke_test(executable: Path, expected_version: str) -> None:
	version = subprocess.run(
		[str(executable), "--version"],
		cwd=executable.parents[1],
		check=True,
		stdout=subprocess.PIPE,
		stderr=subprocess.STDOUT,
		text=True,
		timeout=60,
	).stdout.strip()
	if version != "cocoapdf %s" % expected_version:
		raise SystemExit("unexpected --version output: %r" % version)
	subprocess.run(
		[str(executable), "--help"],
		cwd=executable.parents[1],
		check=True,
		stdout=subprocess.PIPE,
		stderr=subprocess.STDOUT,
		timeout=60,
	)


def _manifest(
	executable: Path,
	version: str,
	binary_format: str,
	architecture: str,
	bits: int,
	brand_manifest_version: str,
	brand_manifest_sha256: str,
	icon_embedded: bool,
	third_party_licenses: Path,
) -> Dict[str, object]:
	third_party_bytes = third_party_licenses.read_bytes()
	return {
		"schema": "cocoapdf.binary-manifest/v1",
		"product": "CocoaPDF",
		"artifact": executable.name,
		"version": version,
		"sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
		"bytes": executable.stat().st_size,
		"binary_format": binary_format,
		"architecture": architecture,
		"python": platform.python_version(),
		"python_bits": bits,
		"platform": platform.platform(),
		"machine": platform.machine(),
		"source_commit": os.environ.get("GITHUB_SHA", "local"),
		"runner_image": os.environ.get("ImageOS", "local"),
		"brand_manifest_version": brand_manifest_version,
		"brand_manifest_sha256": brand_manifest_sha256,
		"icon_embedded": icon_embedded,
		"third_party_licenses": {
			"filename": third_party_licenses.name,
			"bytes": len(third_party_bytes),
			"sha256": hashlib.sha256(third_party_bytes).hexdigest(),
		},
	}


def _windows_version_file(version: str) -> str:
	match = VERSION_PATTERN.fullmatch(version)
	if match is None:
		raise ValueError("version must use unpadded MAJOR.MINOR.PATCH integers")
	major, minor, patch = (int(value) for value in match.groups())
	quad = "(%d, %d, %d, 0)" % (major, minor, patch)
	return """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=%s,
    prodvers=%s,
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(u'040904B0', [
        StringStruct(u'CompanyName', u'Sayantan Dey'),
        StringStruct(u'FileDescription', u'CocoaPDF PDF to Markdown/HTML engine'),
        StringStruct(u'FileVersion', u'%s'),
        StringStruct(u'InternalName', u'cocoapdf'),
        StringStruct(u'LegalCopyright', u'Copyright (c) 2026 Sayantan Dey'),
        StringStruct(u'OriginalFilename', u'cocoapdf.exe'),
        StringStruct(u'ProductName', u'CocoaPDF'),
        StringStruct(u'ProductVersion', u'%s')
      ])
    ]),
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])
  ]
)
""" % (quad, quad, version, version)


def main() -> int:
	parser = argparse.ArgumentParser()
	parser.add_argument("--name", required=True)
	parser.add_argument("--expected-bits", type=int, choices=[32, 64], required=True)
	parser.add_argument("--expected-arch", choices=["x86", "x86_64", "arm64"], required=True)
	parser.add_argument("--expected-format", choices=["PE", "ELF", "Mach-O"], required=True)
	parser.add_argument("--version", required=True)
	args = parser.parse_args()

	actual_bits = struct.calcsize("P") * 8
	if actual_bits != args.expected_bits:
		raise SystemExit(
			"requested %d-bit artifact from a %d-bit Python interpreter"
			% (args.expected_bits, actual_bits)
		)

	root = Path(__file__).resolve().parents[1]
	dist = root / "dist"
	build = root / "build"
	dist.mkdir(exist_ok=True)
	build.mkdir(exist_ok=True)
	brand_manifest_path = root / "docs" / "assets" / "brand" / "BRAND_ASSET_MANIFEST.json"
	try:
		brand_manifest_bytes = brand_manifest_path.read_bytes()
		canonical_brand_manifest = brand_manifest_bytes.replace(b"\r\n", b"\n")
		brand_manifest = json.loads(canonical_brand_manifest.decode("utf-8"))
	except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
		raise SystemExit("cannot read CocoaPDF brand manifest: %s" % exc) from exc
	if brand_manifest.get("project") != "CocoaPDF" or not isinstance(brand_manifest.get("version"), str):
		raise SystemExit("invalid CocoaPDF brand manifest identity")
	brand_manifest_sha256 = hashlib.sha256(canonical_brand_manifest).hexdigest()

	command = [
		sys.executable,
		"-m",
		"PyInstaller",
		"--noconfirm",
		"--clean",
		"--onefile",
		"--name",
		args.name,
		"--paths",
		str(root / "src"),
		"--collect-submodules",
		"cocoapdf",
		"--distpath",
		str(dist),
		"--workpath",
		str(build / args.name),
		"--specpath",
		str(build / "spec"),
		str(root / "run_cocoapdf.py"),
	]
	icon_embedded = os.name == "nt"
	if icon_embedded:
		icon_path = root / "docs" / "assets" / "brand" / "icons" / "app" / "cocoapdf-app-icon.ico"
		if not icon_path.is_file():
			raise SystemExit("missing Windows application icon: %s" % icon_path)
		version_file = build / "cocoapdf-version-info.txt"
		try:
			version_file.write_text(_windows_version_file(args.version), encoding="utf-8")
		except ValueError as exc:
			raise SystemExit(str(exc)) from exc
		command[-1:-1] = ["--icon", str(icon_path), "--version-file", str(version_file)]
	subprocess.run(command, cwd=root, check=True)

	suffix = ".exe" if os.name == "nt" else ""
	executable = dist / (args.name + suffix)
	if not executable.is_file():
		raise SystemExit("PyInstaller did not create %s" % executable)

	binary_format, architecture, binary_bits = inspect_binary(executable)
	if binary_format != args.expected_format:
		raise SystemExit("expected %s executable, found %s" % (args.expected_format, binary_format))
	if architecture != args.expected_arch:
		raise SystemExit("expected %s executable, found %s" % (args.expected_arch, architecture))
	if binary_bits != args.expected_bits:
		raise SystemExit("expected %d-bit executable, found %d-bit" % (args.expected_bits, binary_bits))

	_smoke_test(executable, args.version)
	third_party_licenses = executable.with_name(
		executable.name + ".third-party-licenses.txt"
	)
	try:
		third_party_licenses.write_bytes(build_third_party_license_bundle(root))
	except ValueError as exc:
		raise SystemExit(str(exc)) from exc
	manifest = _manifest(
		executable,
		args.version,
		binary_format,
		architecture,
		binary_bits,
		str(brand_manifest["version"]),
		brand_manifest_sha256,
		icon_embedded,
		third_party_licenses,
	)
	manifest_path = executable.with_name(executable.name + ".manifest.json")
	manifest_path.write_text(
		json.dumps(manifest, indent=2, sort_keys=True) + "\n",
		encoding="utf-8",
	)
	print(json.dumps(manifest, indent=2, sort_keys=True))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
