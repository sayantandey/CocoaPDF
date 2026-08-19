from __future__ import annotations

import argparse
import email.parser
import email.policy
import hashlib
import json
import re
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Dict, Optional, Sequence


PROJECT_NAME = "cocoapdf"
VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
REQUIRED_LEGAL_FILES = ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.txt")


def _require(condition: bool, message: str) -> None:
	if not condition:
		raise ValueError(message)


def _sha256(path: Path) -> str:
	digest = hashlib.sha256()
	with path.open("rb") as stream:
		for block in iter(lambda: stream.read(1024 * 1024), b""):
			digest.update(block)
	return digest.hexdigest()


def _expected_paths(dist: Path, version: str) -> Dict[str, Path]:
	_require(VERSION_RE.fullmatch(version) is not None, "version must be unpadded MAJOR.MINOR.PATCH")
	return {
		"wheel": dist / ("%s-%s-py3-none-any.whl" % (PROJECT_NAME, version)),
		"sdist": dist / ("%s-%s.tar.gz" % (PROJECT_NAME, version)),
	}


def _read_wheel_metadata(wheel: zipfile.ZipFile) -> email.message.Message:
	metadata_names = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
	_require(len(metadata_names) == 1, "wheel must contain exactly one METADATA file")
	return email.parser.BytesParser(policy=email.policy.compat32).parsebytes(
		wheel.read(metadata_names[0])
	)


def verify_local(root: Path, dist: Path, version: str) -> Dict[str, object]:
	root = root.resolve()
	dist = dist.resolve()
	expected = _expected_paths(dist, version)
	_require(dist.is_dir(), "distribution directory does not exist: %s" % dist)

	actual_files = sorted(path.name for path in dist.iterdir() if path.is_file())
	expected_files = sorted(path.name for path in expected.values())
	_require(
		actual_files == expected_files,
		"distribution directory must contain only %s; found %s"
		% (expected_files, actual_files),
	)
	for path in expected.values():
		_require(path.is_file() and path.stat().st_size > 0, "missing or empty artifact: %s" % path)

	legal_bytes = {name: (root / name).read_bytes() for name in REQUIRED_LEGAL_FILES}
	wheel_path = expected["wheel"]
	with zipfile.ZipFile(wheel_path) as wheel:
		metadata = _read_wheel_metadata(wheel)
		_require(metadata["Name"] == PROJECT_NAME, "unexpected wheel project name")
		_require(metadata["Version"] == version, "wheel version does not match release")
		_require(metadata["Requires-Python"] == ">=3.9", "unexpected Requires-Python metadata")
		_require(metadata["License-Expression"] == "MIT", "wheel must declare MIT")
		_require(
			set(metadata.get_all("License-File", [])) == set(REQUIRED_LEGAL_FILES),
			"wheel License-File metadata is incomplete",
		)
		requirements = metadata.get_all("Requires-Dist", [])
		_require(bool(requirements), "expected the build-only optional dependency metadata")
		for requirement in requirements:
			_require(
				re.search(r";\s*extra\s*==\s*['\"]build['\"]\s*$", requirement) is not None,
				"wheel contains a non-optional runtime dependency: %s" % requirement,
			)

		wheel_metadata_names = [name for name in wheel.namelist() if name.endswith(".dist-info/WHEEL")]
		_require(len(wheel_metadata_names) == 1, "wheel must contain exactly one WHEEL file")
		wheel_metadata = wheel.read(wheel_metadata_names[0]).decode("utf-8")
		_require("Root-Is-Purelib: true" in wheel_metadata, "wheel must be pure Python")
		_require("Tag: py3-none-any" in wheel_metadata, "wheel must use the py3-none-any tag")

		entry_point_names = [
			name for name in wheel.namelist() if name.endswith(".dist-info/entry_points.txt")
		]
		_require(len(entry_point_names) == 1, "wheel must contain one entry_points.txt")
		entry_points = wheel.read(entry_point_names[0]).decode("utf-8")
		_require("[console_scripts]" in entry_points, "console script group is missing")
		_require(
			"cocoapdf = cocoapdf.cli:main" in entry_points,
			"cocoapdf console entry point is missing",
		)

		wheel_legal = {
			PurePosixPath(name).name: name
			for name in wheel.namelist()
			if ".dist-info/" in name and PurePosixPath(name).name in legal_bytes
		}
		_require(set(wheel_legal) == set(REQUIRED_LEGAL_FILES), "wheel legal files are incomplete")
		for name, expected_bytes in legal_bytes.items():
			_require(wheel.read(wheel_legal[name]) == expected_bytes, "wheel legal file changed: %s" % name)

	sdist_path = expected["sdist"]
	prefix = "%s-%s" % (PROJECT_NAME, version)
	with tarfile.open(sdist_path, "r:gz") as sdist:
		names = set(sdist.getnames())
		for name, expected_bytes in legal_bytes.items():
			member_name = "%s/%s" % (prefix, name)
			_require(member_name in names, "sdist legal file is missing: %s" % name)
			member = sdist.extractfile(member_name)
			_require(member is not None and member.read() == expected_bytes, "sdist legal file changed: %s" % name)

		pyproject_name = "%s/pyproject.toml" % prefix
		version_file_name = "%s/src/cocoapdf/_version.py" % prefix
		_require(pyproject_name in names, "sdist pyproject.toml is missing")
		_require(version_file_name in names, "sdist version module is missing")
		pyproject = sdist.extractfile(pyproject_name)
		version_file = sdist.extractfile(version_file_name)
		_require(pyproject is not None and version_file is not None, "sdist version inputs are unreadable")
		_require(
			('version = "%s"' % version) in pyproject.read().decode("utf-8"),
			"sdist pyproject version does not match release",
		)
		_require(
			('__version__ = "%s"' % version) in version_file.read().decode("utf-8"),
			"sdist package version does not match release",
		)

	return {
		"project": PROJECT_NAME,
		"version": version,
		"files": {
			path.name: {"bytes": path.stat().st_size, "sha256": _sha256(path)}
			for path in expected.values()
		},
	}


def verify_remote(
	dist: Path,
	version: str,
	attempts: int = 24,
	delay_seconds: float = 5.0,
) -> Dict[str, object]:
	dist = dist.resolve()
	expected = _expected_paths(dist, version)
	local_hashes = {path.name: _sha256(path) for path in expected.values()}
	url = "https://pypi.org/pypi/%s/%s/json" % (PROJECT_NAME, version)
	last_error: Optional[BaseException] = None
	payload = None
	for attempt in range(1, attempts + 1):
		try:
			request = urllib.request.Request(url, headers={"User-Agent": "CocoaPDF-release-verifier/1"})
			with urllib.request.urlopen(request, timeout=20) as response:
				payload = json.load(response)
			break
		except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
			last_error = exc
			if attempt == attempts:
				break
			time.sleep(delay_seconds)
	_require(payload is not None, "PyPI release did not become visible: %s" % last_error)
	_require(payload["info"]["name"].casefold() == PROJECT_NAME, "unexpected PyPI project identity")
	_require(payload["info"]["version"] == version, "unexpected PyPI release version")
	remote_hashes = {
		entry["filename"]: entry["digests"]["sha256"]
		for entry in payload.get("urls", [])
		if entry.get("packagetype") in {"bdist_wheel", "sdist"}
	}
	_require(
		remote_hashes == local_hashes,
		"PyPI artifacts differ from the locally verified distributions: local=%s remote=%s"
		% (local_hashes, remote_hashes),
	)
	return {
		"project": PROJECT_NAME,
		"version": version,
		"url": url,
		"files": remote_hashes,
	}


def main(argv: Optional[Sequence[str]] = None) -> int:
	parser = argparse.ArgumentParser(description="Verify CocoaPDF Python distributions")
	subparsers = parser.add_subparsers(dest="command", required=True)

	local = subparsers.add_parser("local", help="Verify locally built wheel and sdist")
	local.add_argument("--root", type=Path, default=Path("."))
	local.add_argument("--dist", type=Path, default=Path("dist"))
	local.add_argument("--version", required=True)
	local.add_argument("--summary", type=Path)

	remote = subparsers.add_parser("remote", help="Verify published PyPI files by SHA-256")
	remote.add_argument("--dist", type=Path, default=Path("dist"))
	remote.add_argument("--version", required=True)
	remote.add_argument("--attempts", type=int, default=24)
	remote.add_argument("--delay-seconds", type=float, default=5.0)
	remote.add_argument("--summary", type=Path)

	args = parser.parse_args(argv)
	if args.command == "local":
		result = verify_local(args.root, args.dist, args.version)
	else:
		result = verify_remote(args.dist, args.version, args.attempts, args.delay_seconds)
	encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
	if args.summary:
		args.summary.parent.mkdir(parents=True, exist_ok=True)
		with args.summary.open("w", encoding="utf-8", newline="\n") as stream:
			stream.write(encoded)
	print(encoded, end="")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
