#!/usr/bin/env python3
"""Safely replace one machine-owned block in a pull-request description."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence


MARKER_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


class MarkerError(ValueError):
	"""Raised when a body contains ambiguous or malformed ownership markers."""


class StaleHeadError(ValueError):
	"""Raised when a completed run no longer describes the pull request head."""


def marker_lines(marker: str) -> tuple[str, str]:
	if not MARKER_NAME.fullmatch(marker):
		raise MarkerError("invalid marker name")
	return "<!-- %s:start -->" % marker, "<!-- %s:end -->" % marker


def replace_owned_block(body: Optional[str], marker: str, replacement: str) -> str:
	"""Return ``body`` with exactly one canonical machine-owned block.

	Zero existing marker pairs appends the block. Exactly one ordered pair is
	replaced. Any other shape is rejected without guessing which user text is
	machine-owned.
	"""

	if body is None:
		body = ""
	if not isinstance(body, str) or not isinstance(replacement, str):
		raise TypeError("body and replacement must be strings")
	start, end = marker_lines(marker)
	if start in replacement or end in replacement:
		raise MarkerError("replacement must not contain ownership markers")

	start_count = body.count(start)
	end_count = body.count(end)
	if start_count == 0 and end_count == 0:
		prefix = body
		if prefix and not prefix.endswith("\n"):
			prefix += "\n"
		if prefix and not prefix.endswith("\n\n"):
			prefix += "\n"
		return "%s%s\n%s\n%s\n" % (
			prefix,
			start,
			replacement.rstrip("\n"),
			end,
		)
	if start_count != 1 or end_count != 1:
		raise MarkerError("body must contain zero or one ownership-marker pair")

	start_at = body.index(start)
	end_at = body.index(end)
	if end_at < start_at:
		raise MarkerError("ownership markers are reversed")
	owned_end = end_at + len(end)
	block = "%s\n%s\n%s" % (start, replacement.rstrip("\n"), end)
	return body[:start_at] + block + body[owned_end:]


def payload_for_pull_request(
	pull_request: Mapping[str, Any],
	*,
	expected_head: str,
	marker: str,
	replacement: str,
) -> Dict[str, str]:
	"""Build a PATCH payload only when ``expected_head`` is still current."""

	if not COMMIT_SHA.fullmatch(expected_head):
		raise StaleHeadError("expected head must be a lowercase 40-character SHA")
	head = pull_request.get("head")
	actual = head.get("sha") if isinstance(head, Mapping) else None
	if actual != expected_head:
		raise StaleHeadError("pull request head changed before body update")
	body = pull_request.get("body")
	if body is not None and not isinstance(body, str):
		raise TypeError("pull-request body must be a string or null")
	return {"body": replace_owned_block(body, marker, replacement)}


def main(argv: Optional[Sequence[str]] = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--pull-request-json", type=Path, required=True)
	parser.add_argument("--expected-head", required=True)
	parser.add_argument("--marker", required=True)
	parser.add_argument("--replacement", type=Path, required=True)
	parser.add_argument("--output", type=Path, required=True)
	args = parser.parse_args(argv)

	pull_request = json.loads(args.pull_request_json.read_text(encoding="utf-8"))
	if not isinstance(pull_request, dict):
		raise TypeError("pull-request JSON must contain an object")
	replacement = args.replacement.read_text(encoding="utf-8")
	payload = payload_for_pull_request(
		pull_request,
		expected_head=args.expected_head,
		marker=args.marker,
		replacement=replacement,
	)
	args.output.write_text(
		json.dumps(payload, ensure_ascii=False) + "\n",
		encoding="utf-8",
		newline="\n",
	)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
