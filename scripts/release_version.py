from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


TAG_RE = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
PATCH_BRANCH_RE = re.compile(r"^(?:fix|bugfix|hotfix|patch)(?:[/-]|$)", re.IGNORECASE)
PATCH_TITLE_RE = re.compile(r"^(?:fix|bugfix|hotfix|patch)(?:\([^)]*\))?!?:", re.IGNORECASE)


@dataclass(frozen=True, order=True)
class Version:
	major: int
	minor: int
	patch: int

	@property
	def tag(self) -> str:
		return "v%s" % self

	def __str__(self) -> str:
		return "%d.%d.%d" % (self.major, self.minor, self.patch)


def parse_tag(tag: str) -> Optional[Version]:
	match = TAG_RE.fullmatch(tag.strip())
	if not match:
		return None
	return Version(*(int(part) for part in match.groups()))


def _label_names(pr: Dict[str, Any]) -> set[str]:
	names: set[str] = set()
	for label in pr.get("labels") or []:
		name = label.get("name") if isinstance(label, dict) else label
		if name:
			names.add(str(name).casefold())
	return names


def classify_release(
	pr: Optional[Dict[str, Any]],
	commit_subject: str = "",
) -> Tuple[str, bool, str]:
	labels = _label_names(pr or {})
	patch_label = "release:patch" in labels
	minor_label = "release:minor" in labels
	if patch_label and minor_label:
		raise ValueError("apply at most one of release:patch and release:minor")

	breaking = "breaking" in labels
	if patch_label:
		return "patch", breaking, "release:patch label"
	if minor_label:
		return "minor", breaking, "release:minor label"

	pr = pr or {}
	head = pr.get("head") or {}
	branch = str(head.get("ref") or pr.get("headRefName") or "")
	title = str(pr.get("title") or "")
	if PATCH_BRANCH_RE.match(branch):
		return "patch", breaking, "patch branch %s" % branch
	if PATCH_TITLE_RE.match(title):
		return "patch", breaking, "fix-style pull request title"
	if PATCH_TITLE_RE.match(commit_subject.strip()):
		return "patch", breaking, "fix-style commit subject"
	return "minor", breaking, "default minor policy"


def compute_next_version(
	configured_major: int,
	tags: Iterable[str],
	kind: str,
	breaking: bool = False,
	head_tags: Iterable[str] = (),
) -> Tuple[Version, bool]:
	if configured_major < 0:
		raise ValueError("VERSION_MAJOR must be a non-negative integer")
	if kind not in {"minor", "patch"}:
		raise ValueError("release kind must be minor or patch")

	versions = sorted(version for tag in tags if (version := parse_tag(tag)) is not None)
	versions_at_head = sorted(
		version for tag in head_tags if (version := parse_tag(tag)) is not None
	)
	if versions_at_head:
		version = versions_at_head[-1]
		if version.major != configured_major:
			raise ValueError(
				"release tag %s disagrees with VERSION_MAJOR=%d"
				% (version.tag, configured_major)
			)
		return version, True

	if not versions:
		if configured_major > 0:
			if not breaking:
				raise ValueError("an initial non-zero major requires the breaking label")
			return Version(configured_major, 0, 0), False
		if breaking:
			raise ValueError("breaking requires incrementing VERSION_MAJOR")
		return (Version(0, 0, 1) if kind == "patch" else Version(0, 1, 0)), False

	latest = versions[-1]
	if configured_major < latest.major:
		raise ValueError(
			"VERSION_MAJOR=%d cannot be lower than latest release %s"
			% (configured_major, latest)
		)
	if configured_major > latest.major:
		if configured_major != latest.major + 1:
			raise ValueError("increment VERSION_MAJOR by exactly one")
		if not breaking:
			raise ValueError("a major increment requires the breaking label")
		return Version(configured_major, 0, 0), False
	if breaking:
		raise ValueError("breaking requires manually incrementing VERSION_MAJOR")
	if kind == "patch":
		return Version(configured_major, latest.minor, latest.patch + 1), False
	return Version(configured_major, latest.minor + 1, 0), False


def _run_git(root: Path, *args: str) -> List[str]:
	result = subprocess.run(
		["git", *args],
		cwd=root,
		check=True,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		text=True,
	)
	return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _load_pr(event_file: Optional[Path], associated_prs_file: Optional[Path]) -> Optional[Dict[str, Any]]:
	if event_file:
		event = json.loads(event_file.read_text(encoding="utf-8"))
		if isinstance(event, dict) and isinstance(event.get("pull_request"), dict):
			return event["pull_request"]
	if associated_prs_file:
		pulls = json.loads(associated_prs_file.read_text(encoding="utf-8"))
		if isinstance(pulls, list) and pulls:
			return max(pulls, key=lambda pr: str(pr.get("merged_at") or pr.get("updated_at") or ""))
	return None


def _read_major(path: Path) -> int:
	value = path.read_text(encoding="utf-8").strip()
	if not re.fullmatch(r"0|[1-9][0-9]*", value):
		raise ValueError("VERSION_MAJOR must contain one non-negative integer")
	return int(value)


def _write_outputs(path: Path, values: Dict[str, str]) -> None:
	with path.open("a", encoding="utf-8", newline="\n") as output:
		for key, value in values.items():
			output.write("%s=%s\n" % (key, value.replace("\n", " ")))


def main(argv: Optional[Sequence[str]] = None) -> int:
	parser = argparse.ArgumentParser(description="Resolve CocoaPDF's deterministic next release version")
	parser.add_argument("--event-file", type=Path)
	parser.add_argument("--associated-prs-file", type=Path)
	parser.add_argument("--major-file", type=Path, default=Path("VERSION_MAJOR"))
	parser.add_argument("--github-output", type=Path)
	args = parser.parse_args(argv)

	root = Path(__file__).resolve().parents[1]
	pr = _load_pr(args.event_file, args.associated_prs_file)
	commit_subject = ""
	try:
		commit_subject = _run_git(root, "log", "-1", "--pretty=%s")[0]
	except IndexError:
		pass
	kind, breaking, reason = classify_release(pr, commit_subject)
	version, existing = compute_next_version(
		_read_major(root / args.major_file),
		_run_git(root, "tag", "--list"),
		kind,
		breaking,
		_run_git(root, "tag", "--points-at", "HEAD"),
	)
	values = {
		"version": str(version),
		"tag": version.tag,
		"kind": kind,
		"breaking": str(breaking).lower(),
		"existing_tag": str(existing).lower(),
		"reason": reason,
	}
	if args.github_output:
		_write_outputs(args.github_output, values)
	print(json.dumps(values, indent=2, sort_keys=True))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
