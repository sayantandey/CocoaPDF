# OpenDataLoader benchmark adapter

`adapter.py` is the canonical CocoaPDF adapter for
[`opendataloader-project/opendataloader-bench`](https://github.com/opendataloader-project/opendataloader-bench).
Copy it into the benchmark checkout as `src/pdf_parser_cocoapdf.py`. Do not edit
the archived adapters under `results/`: those directories are immutable
provenance for the score they record.

## What the adapter does and does not do

The adapter performs **output-schema projection only**. CocoaPDF's detectors,
reading order, and semantic graph are identical to a normal conversion; nothing
here changes the analysis, and none of it is reachable from the default
converter.

Projections applied:

| Projection | Reason |
| --- | --- |
| `heading_level_mode="flat"` | The ground truth annotates every heading as `#`. The heading metric discards the level entirely, so CocoaPDF's inferred depth only adds characters that the reading-order metric scores as edits. |
| Tables reduced to `table`/`tr`/`td` with only `rowspan`/`colspan` | The table metric tokenises nested inline markup as cell content. |
| Table captions moved outside the table | Matches the reference markup. |
| Image references removed | The corpus annotation is text-only and never carries an image placeholder. |
| Link syntax reduced to its text | The reference keeps link text but no destination. |
| Backslash escapes, `<p>`, `<u>`, `~~` removed | The reference is plain prose. |

Deliberately **not** applied: quotation marks, dashes, and every other extracted
character are left exactly as the page produced them. Rewriting those would tune
the text toward the reference rather than project an output schema, and it is
the line between adaptation and benchmark coupling.

GFM rows are left untouched — their cell text escapes `|`, and the benchmark
converts those rows to HTML itself.

A conversion exception writes an empty `<document-id>.md` and records the
exception in `failures.json`, so a single malformed PDF cannot remove documents
from the scored set or abort the run.

## Reproduce

```bash
git clone https://github.com/opendataloader-project/opendataloader-bench.git
cd opendataloader-bench
git checkout 7af1d8f4d0c09f51ea1a5c6ba5f66e993286d109
# The pinned revision stores the 200 PDFs as ordinary Git blobs (~36 MB).
# Apply the archived minimal registry/dependency pin and copy the exact adapter.
git apply --unidiff-zero ../CocoaPDF/Implementation/validation/benchmarks/opendataloader_bench/results/7af1d8f4d0c09f51ea1a5c6ba5f66e993286d109/integration.patch
cp ../CocoaPDF/Implementation/validation/benchmarks/opendataloader_bench/adapter.py \
  src/pdf_parser_cocoapdf.py
uv sync --extra cocoapdf --extra dev --system-certs
uv run --locked --offline --extra cocoapdf --extra dev --no-sync pytest -q

# Run twice, preserving prediction/cocoapdf after the first run for comparison.
uv run --locked --offline --extra cocoapdf --no-sync python src/run.py \
  --engine cocoapdf --force --history-date 260801 --history-overwrite
uv run --locked --offline --extra cocoapdf --no-sync python src/run.py \
  --engine cocoapdf --force --history-date 260801 --history-overwrite
```

Read the result from `prediction/cocoapdf/evaluation.json`:

```bash
uv run --locked --offline --extra cocoapdf --no-sync python -c "import json;print(json.dumps(json.load(open('prediction/cocoapdf/evaluation.json',encoding='utf-8'))['metrics'],indent=2))"
```

Publish `overall_mean` together with every component score **and** the
eligible-document counts. The evaluator averages only the metrics available for
each document, so `nid_count`, `teds_count`, and `mhs_count` are part of the
result, not footnotes. Record the CocoaPDF commit SHA, the benchmark commit SHA,
the failure count, and the runtime with any published number.

## Pull-request and main-branch gate

`.github/workflows/opendataloader-benchmark.yml` delegates to
`opendataloader-worker.yml` at the immutable commit recorded in `policy.json`.
The caller and worker have read-only repository permissions. The worker checks
out that exact harness commit and the exact candidate SHA separately, restores
the immutable PDF cache without ever saving from a pull request, and runs only
the candidate source in a pinned container with no network, token,
capabilities, writable root, or access to the evaluator or ground truth. Its
64 MiB/4096-inode output tmpfs is reduced to an exact 200-file, size-bounded
prediction artifact before upload.

A completed run starts `opendataloader-report.yml`, whose definition and code
come only from the default branch. It never checks out or executes the
candidate. For pull requests it compares every protected caller/worker,
adapter, policy, and reporter blob through the Git API. It also authenticates
the upstream run and immutable reusable-worker SHA. A trusted downloader first
bounds the archive metadata, then verifies the downloaded archive digest and
extracts only regular allowlisted paths into a 16 MiB/512-inode tmpfs; path
traversal, links, duplicates, unsupported compression, invalid UTF-8, wrong
IDs/counts, and per-file/total expansion limits fail closed. Only then does the
hash-pinned evaluator read the predictions. The verified corpus cache is saved
only by the trusted reporter for a successful `main` run, before any candidate
artifact is downloaded.

The authoritative check is `ODL 200 / trusted gate`. It requires all 200
predictions, zero failures/missing/empty outputs, overall `>= 0.800`, and no
regression greater than `0.001` in overall, NID, TEDS, or MHS relative to the
published `0.869665721357887` baseline. It also enforces independent `0.80`
floors for NID, TEDS, and MHS. A separate least-privilege writer
updates the informational PR block, or publishes the fixed-path main badge.
The `workflow_run.requested` event changes the current-main badge to red
`unverified` before conversion starts, so a cancelled or broken conversion or
evaluation cannot leave the previous green score looking current. The retained
evidence contains only scores, hashes, and provenance—not PDFs, ground truth,
or predicted Markdown.
The Shields endpoint remains a minimal custom-endpoint document; tested SHA,
UTC publication time, trigger/reporter run identities, benchmark identity, and
full scores are published separately at
[`badges/opendataloader.provenance.json`](https://raw.githubusercontent.com/sayantandey/CocoaPDF/odl-badge/badges/opendataloader.provenance.json)
on the fixed `odl-badge` branch.

Because GitHub runs a `workflow_run` definition from the default branch, this
gate begins operating only after these workflow files are first merged. Enable
protection only after observing the first successful `main` report. Prefer a
ruleset-required workflow or a dedicated check-publishing GitHub App over a
name-only required check: another workflow from the same GitHub Actions App can
otherwise imitate a check name. Keep the caller, immutable worker pin, and
reporter paths owner-reviewed and admin-enforced.

This is a public-corpus regression gate, not proof against a deliberately
dishonest submission. Isolation prevents candidate code from fetching ground
truth during CI or forging the trusted evaluator result, but a contributor who
previously downloaded the public corpus could still vendor memorised outputs or
fixture-specific logic into the candidate itself. Source review, adversarial
near-miss tests, and separately held-out/private corpora remain necessary for
that threat model.

The default-branch harness also rejects pull requests that modify its
privileged workflow, policy, adapter, reporter, or PR-body writer files. Those
rare self-updates require an explicit owner/admin bootstrap on `main`, followed
by a full audit and gate rerun. Keep CODEOWNERS review and admin-enforced branch
protection enabled for these paths; otherwise a passing change could weaken the
workflow that evaluates the next pull request.

## Metric notes worth knowing before optimising

Established by reading the evaluator at benchmark commit `7af1d8f`:

- **NID** is `rapidfuzz.fuzz.ratio` over the whole normalised document. Emitting
  extra text is penalised exactly like omitting real text.
- **MHS** builds a tree in which *every* heading is a direct child of the root
  and the `#{1,6}` level is discarded. Heading depth cannot change this metric;
  only which lines become headings, and their text, can.
- **TEDS** concatenates every table in the document into one comparison. A
  spurious extra table is as damaging as a missing one. `th` is folded to `td`
  and `thead`/`tbody` are stripped on both sides, so those tags are irrelevant;
  `rowspan`/`colspan` and cell text are not.
- TEDS is scored on the 42 documents whose ground truth contains a table, and
  MHS on the 107 that contain a heading.
