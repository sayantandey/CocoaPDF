from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--source-commit")
    parser.add_argument("--head-commit")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    checkout = output_dir.parent / "opendataloader-bench"
    if checkout.exists():
        shutil.rmtree(checkout)

    subprocess.run(["git", "lfs", "install"], check=True)
    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "https://github.com/opendataloader-project/opendataloader-bench.git",
            str(checkout),
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(checkout), "lfs", "pull"], check=True)

    pdfs = list((checkout / "pdfs").glob("*.pdf"))
    if len(pdfs) < 200:
        raise RuntimeError(f"expected at least 200 benchmark PDFs, found {len(pdfs)}")

    for name in ("pdfs", "ground-truth", "src"):
        shutil.copytree(checkout / name, output_dir / name)
    for name in ("pyproject.toml", "uv.lock", "README.md"):
        shutil.copy2(checkout / name, output_dir / name)

    commit = subprocess.check_output(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
    ).strip()
    (output_dir / "benchmark-commit.txt").write_text(commit + "\n", encoding="utf-8")
    (output_dir / "review.html").write_text(
        "<!doctype html><title>ODL benchmark corpus export</title>", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
