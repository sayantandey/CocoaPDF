from __future__ import annotations

import unittest
from pathlib import Path


class PyPIUvVersionCheckTests(unittest.TestCase):
	def test_publisher_uses_stable_short_version_output(self) -> None:
		root = Path(__file__).resolve().parents[1]
		workflow = (root / ".github/workflows/publish-pypi.yml").read_text(
			encoding="utf-8"
		)

		# `uv --version` includes build commit/date metadata. `uv -V` is the
		# documented stable form without that suffix and is suitable for an
		# exact release-pin assertion.
		self.assertIn('uv_version="$(uv -V)"', workflow)
		self.assertIn('[[ "${uv_version}" == "uv 0.12.0" ]]', workflow)
		self.assertNotIn('[[ "$(uv --version)" == "uv 0.12.0" ]]', workflow)
		self.assertIn("Installed publisher: %s", workflow)


if __name__ == "__main__":
	unittest.main()
