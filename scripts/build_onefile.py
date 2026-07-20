from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
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
) -> Dict[str, object]:
	return {
		"schema": "cocoapdf.binary-manifest/v1",
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
	}


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
	manifest = _manifest(executable, args.version, binary_format, architecture, binary_bits)
	manifest_path = executable.with_name(executable.name + ".manifest.json")
	manifest_path.write_text(
		json.dumps(manifest, indent=2, sort_keys=True) + "\n",
		encoding="utf-8",
	)
	print(json.dumps(manifest, indent=2, sort_keys=True))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
