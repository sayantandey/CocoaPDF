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
- The existing strategic fixture is already project material and is copied
  without modification.
- The corpus and generated artifacts are covered by the repository's MIT
  license; `LICENSE.txt` and a machine-readable manifest accompany every run.
- A future external fixture may be admitted only with an explicit,
  redistribution-compatible license and recorded source/hash provenance.

## Artifact lifecycle

The workflow writes only beneath the runner's temporary directory. It never
writes generated inputs or outputs into the checkout, so it cannot alter the
permanent `examples/` demo. Artifacts are retained for at most seven days and
are deleted when the pull request closes or merges.

For same-repository pull requests, the workflow maintains one delimited block
in the PR description containing the current artifact link and digest. While
the PR is open, the same block includes an immutable rendered-demo URL pinned
to the exact head commit; the stable public demo remains explicitly on `main`.
When the PR closes, the workflow replaces that block with an explicit deletion
notice, the reviewed head SHA, the cleanup run, and a link to the permanent
demo. A merged PR therefore never retains a live-looking link to a deleted
artifact. Fork and automation-token restrictions may prevent description
writes; the artifact link is always available from the workflow summary while
the PR is open.

## Local run

From the repository root:

```text
python validation/pr_visual/build.py --output-dir <empty-directory>
```

The output directory must be empty. Open `review.html` after the command
completes.

The permanent demo is refreshed and verified separately:

```text
python scripts/refresh_examples.py --write
python scripts/refresh_examples.py --check
```
