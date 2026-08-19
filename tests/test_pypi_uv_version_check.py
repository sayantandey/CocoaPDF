from __future__ import annotations

import unittest
from pathlib import Path


class PyPIUvPublisherCheckTests(unittest.TestCase):
	def test_publisher_uses_distribution_metadata_not_banner_format(self) -> None:
		root = Path(__file__).resolve().parents[1]
		workflow = (root / ".github/workflows/publish-pypi.yml").read_text(
			encoding="utf-8"
		)

		# uv's human-readable banner may append build and target metadata. The
		# installed wheel metadata is the stable authority for the exact pin.
		self.assertIn('actual = importlib.metadata.version("uv")', workflow)
		self.assertIn('expected = "0.12.0"', workflow)
		self.assertIn('Path(sysconfig.get_path("scripts")) / "uv"', workflow)
		self.assertIn('"${publisher}" publish --help >/dev/null', workflow)
		self.assertIn("UV_PUBLISHER", workflow)
		self.assertIn('publish --dry-run "${publish_args[@]}"', workflow)
		self.assertNotIn('uv_version="$(uv -V)"', workflow)
		self.assertNotIn('[[ "${uv_version}" == "uv 0.12.0" ]]', workflow)
		self.assertNotIn('[[ "$(uv --version)" == "uv 0.12.0" ]]', workflow)


if __name__ == "__main__":
	unittest.main()
