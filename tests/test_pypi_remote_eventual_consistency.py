from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.verify_pypi_artifacts import verify_remote


class PyPIRemoteEventualConsistencyTests(unittest.TestCase):
	def test_remote_verifier_retries_until_all_hashes_are_exact(self) -> None:
		version = "9.8.7"
		with tempfile.TemporaryDirectory() as directory:
			dist = Path(directory)
			files = {
				"cocoapdf-9.8.7-py3-none-any.whl": b"wheel",
				"cocoapdf-9.8.7.tar.gz": b"sdist",
			}
			for name, content in files.items():
				(dist / name).write_bytes(content)

			entries = [
				{
					"filename": name,
					"packagetype": "bdist_wheel" if name.endswith(".whl") else "sdist",
					"digests": {"sha256": hashlib.sha256(content).hexdigest()},
				}
				for name, content in files.items()
			]
			base = {"info": {"name": "CocoaPDF", "version": version}}
			partial = dict(base, urls=entries[:1])
			complete = dict(base, urls=entries)
			responses = [
				io.BytesIO(json.dumps(partial).encode("utf-8")),
				io.BytesIO(json.dumps(complete).encode("utf-8")),
			]

			with mock.patch("urllib.request.urlopen", side_effect=responses), mock.patch(
				"time.sleep"
			) as sleep:
				result = verify_remote(dist, version, attempts=2, delay_seconds=0)

			self.assertEqual(result["version"], version)
			self.assertEqual(set(result["files"]), set(files))
			sleep.assert_called_once_with(0)


if __name__ == "__main__":
	unittest.main()
