# Pull-request visual validation corpus

This directory defines CocoaPDF's small, high-signal corpus for manual pull
request review. It is CI infrastructure, not the permanent user-facing
capability demo in [`examples/`](../../examples/README.md). Both use the same
deterministic case definitions so a pull request reviews the exact capabilities
represented by the committed demo without writing generated files into it.

`build.py` creates exactly three complementary inputs:

1. `strategic_corner_cases` copies the existing project-authored V1–V4 fixture
   and its Markdown source into the review artifact.
2. `tagged_semantics` deterministically generates a tagged PDF containing a
   heading, sibling list items, and a semantic table.
3. `scope_and_adversarial` deterministically generates a two-page PDF with
   outlines, page-bound AcroForm widgets, a dot-leader financial statement,
   and diagram boxes that must not be hallucinated as form controls.

These are deliberately not concatenated into one PDF. `StructTreeRoot` and its
parent tree, AcroForm, and outlines are catalog-scoped object graphs, not
page-local decorations. A page append would either discard those semantics or
create a partially tagged hybrid whose evidence no longer matches the isolated
test. The permanent demo presents one corpus, while retaining three inputs is
what keeps each failure attributable and the source/output comparison honest.

For each input, the runner emits CocoaPDF Markdown, HTML, semantic JSON, report
JSON, extracted assets, hashes, and explicit contract results. The artifact
also contains `review.html` for side-by-side input/output inspection and
`REVIEW.md` for a text-only inventory.

## Legal and provenance policy

- No network content is downloaded.
- No third-party prose, images, PDFs, or embedded font programs are added.
- Newly generated PDFs use only first-party fixture text, elementary drawing
  operators, and references to PDF standard fonts.
- The strategic fixture is project material. Its original producer PDF remains
  an immutable, hash-verified prefix; `tools/update_strategic_raster_fixture.py`
  appends only a deterministic raster/text incremental update and rebuilds the
  matching source PNG.
- The corpus and generated artifacts are covered by the repository's MIT
  license; `LICENSE.txt` and a machine-readable manifest accompany every run.
- A future external fixture may be admitted only with an explicit,
  redistribution-compatible license and recorded source/hash provenance.

## Artifact lifecycle

The generation workflow writes only beneath the runner's temporary directory.
It never writes generated inputs or outputs into the checkout, so it cannot
alter the permanent `examples/` demo. Artifacts expire automatically after at
most seven days; no candidate-controlled job has artifact-delete or PR-write
authority.

After generation completes, a separate `workflow_run` reporter loaded from the
default branch validates the repository, workflow path, exact current head,
run attempt, artifact name, size, ID, and digest. It publishes only that
metadata in one delimited PR-description block and never downloads or executes
the candidate-produced artifact. Same-repository pull requests also receive an
immutable rendered-demo URL pinned to the exact head commit; fork previews are
intentionally omitted. The artifact link remains available in the unprivileged
workflow summary even if repository policy prevents a description update.

## Local run

From the repository root:

```text
python validation/pr_visual/build.py --output-dir <empty-directory>
```

The output directory must be empty. Open `review.html` after the command
completes.

The permanent demo is refreshed and verified separately:

```text
python tools/update_strategic_raster_fixture.py --check
python scripts/refresh_examples.py --write
python scripts/refresh_examples.py --check
```

The permanent profile also validates and copies the pinned OpenDataLoader-Bench
result snapshot from `validation/benchmarks/opendataloader_bench/results/`.
Benchmark PDFs, ground truth, generated predictions, and hardware-mislabelled
upstream charts are not copied into `examples/`; only scores, timing,
prediction hashes, adapter/integration evidence, and explicit provenance are
published.
