# CocoaPDF conversion examples

The three capability-demo inputs and their fixture prose are first-party project material under the bundled MIT license.
No network content, OCR, AI, or ML was used to create or convert those fixtures.

The three PDFs are intentionally isolated: Tagged-PDF structure trees, AcroForm fields, and outlines are document-catalog semantics. Concatenating their pages would alter the evidence being tested and make failures less diagnostic.

This directory is the committed, reproducible capability demo. Pull-request review artifacts are generated separately and are never written here.

[Open the rendered side-by-side PDF-to-HTML demo from `main`](https://raw.githack.com/sayantandey/CocoaPDF/main/examples/review.html). [Browse this revision's committed demo source](review.html). Same-repository pull requests receive an exact commit-pinned rendered link in their description.

All case and output links below are revision-relative, so browsing a branch or commit never silently opens output from `main`.

Full semantic JSON is committed. Report summaries omit only duplicate semantic graphs and glyph-heavy internals; the temporary PR artifact retains every full report.

| Case | Coverage | Input | Outputs |
| --- | --- | --- | --- |
| `strategic_corner_cases` | Broad V1-V4 formatting, Unicode, lists, tables, figures, forms, columns, security, and fallback coverage. | [PDF](cases/strategic_corner_cases/input.pdf) | [Markdown](cases/strategic_corner_cases/full/output.md)<br/>[HTML](cases/strategic_corner_cases/full/output.html)<br/>[Semantic JSON](cases/strategic_corner_cases/full/output.json)<br/>[Report](cases/strategic_corner_cases/full/output.report.summary.json) |
| `tagged_semantics` | Tagged heading, sibling ordered-list isolation, MCID provenance, and tagged table structure. | [PDF](cases/tagged_semantics/input.pdf) | [Markdown](cases/tagged_semantics/full/output.md)<br/>[HTML](cases/tagged_semantics/full/output.html)<br/>[Semantic JSON](cases/tagged_semantics/full/output.json)<br/>[Report](cases/tagged_semantics/full/output.report.summary.json) |
| `scope_and_adversarial` | Page-range outline/AcroForm scope, valid heading anchors, dot-leader finance recovery, and two-sided diagram-versus-form/table fidelity. | [PDF](cases/scope_and_adversarial/input.pdf) | [Markdown](cases/scope_and_adversarial/full/output.md)<br/>[HTML](cases/scope_and_adversarial/full/output.html)<br/>[Semantic JSON](cases/scope_and_adversarial/full/output.json)<br/>[Report](cases/scope_and_adversarial/full/output.report.summary.json)<br/>[Page 2 Markdown](cases/scope_and_adversarial/page-2/output.md)<br/>[Page 2 HTML](cases/scope_and_adversarial/page-2/output.html)<br/>[Page 2 Semantic JSON](cases/scope_and_adversarial/page-2/output.json)<br/>[Page 2 Report](cases/scope_and_adversarial/page-2/output.report.summary.json) |

## OpenDataLoader-Bench results

CocoaPDF `0.1.0` at [`59a544a3`](https://github.com/sayantandey/CocoaPDF/commit/59a544a3cfc6e94e72dce4f22f2b334819c818e8) was evaluated on all 200 PDFs using [OpenDataLoader-Bench at `7af1d8f4`](https://github.com/opendataloader-project/opendataloader-bench/tree/7af1d8f4d0c09f51ea1a5c6ba5f66e993286d109). The benchmark is Apache-2.0 and identifies its DP-Bench corpus as MIT; no source PDFs, ground truth, or predicted Markdown are redistributed here.

| Metric | Mean | Eligible documents |
| --- | ---: | ---: |
| Overall document-macro score | `0.9020490607` | 200 |
| NID | `0.9086028983` | 200 |
| NID-S (tables removed) | `0.8897361669` | 200 |
| TEDS | `0.9251323351` | 42 |
| TEDS-S (structure only) | `0.9300636650` | 42 |
| MHS | `0.8791989022` | 107 |
| MHS-S (structure only) | `0.9415827438` | 107 |

Completeness: **200 evaluated, 200 prediction files, 0 missing, 0 empty, 0 conversion failures**.

Two clean full runs took **172.049105** and **204.085285 seconds total** (**0.860246** and **1.020426 seconds/document**) on `Intel(R) Core(TM) i5-10300H CPU @ 2.50GHz`, Windows 10 build 19045, CPython 3.13.14, uv 0.12.0, and 25.59 GB physical memory. This hardware-bound time covers CocoaPDF's complete default conversion, including semantic HTML generation, although this benchmark scores Markdown only.

Raw evidence: [exact result](benchmarks/opendataloader-bench/7af1d8f4d0c09f51ea1a5c6ba5f66e993286d109/result.json), [evaluation JSON](benchmarks/opendataloader-bench/7af1d8f4d0c09f51ea1a5c6ba5f66e993286d109/evaluation.json), [evaluation CSV](benchmarks/opendataloader-bench/7af1d8f4d0c09f51ea1a5c6ba5f66e993286d109/evaluation.csv), [timing summary](benchmarks/opendataloader-bench/7af1d8f4d0c09f51ea1a5c6ba5f66e993286d109/summary.json), [provenance](benchmarks/opendataloader-bench/7af1d8f4d0c09f51ea1a5c6ba5f66e993286d109/provenance.json), [prediction hashes](benchmarks/opendataloader-bench/7af1d8f4d0c09f51ea1a5c6ba5f66e993286d109/prediction-hashes.json), [two-run determinism](benchmarks/opendataloader-bench/7af1d8f4d0c09f51ea1a5c6ba5f66e993286d109/determinism.json), [adapter](benchmarks/opendataloader-bench/7af1d8f4d0c09f51ea1a5c6ba5f66e993286d109/adapter.py), and [integration patch](benchmarks/opendataloader-bench/7af1d8f4d0c09f51ea1a5c6ba5f66e993286d109/integration.patch).

> Scope: this table pins commit [`59a544a3`](https://github.com/sayantandey/CocoaPDF/commit/59a544a3cfc6e94e72dce4f22f2b334819c818e8) and is regenerated only by importing a fresh run of that exact tree, which `tools/import_opendataloader_benchmark.py` verifies by tree hash. It therefore does not describe uncommitted work. A measurement of the current working tree, whenever it differs, belongs in [`validation/benchmarks/opendataloader_bench/RESULTS.md`](../validation/benchmarks/opendataloader_bench/RESULTS.md) and must never be published under this commit's identifier.

> Interpretation limits: NID is a whitespace-normalized Markdown text/order proxy; TEDS concatenates every extracted table into one synthetic comparison per eligible document; and this benchmark's MHS implementation flattens heading levels, so it measures heading boundaries/text rather than true hierarchy depth. `overall_mean` is the mean of per-document available metrics, not the mean of the three aggregate metric means. These numbers do not measure CocoaPDF's HTML fidelity. The upstream chart is not included because it relabels seconds/document as seconds/page and hardcodes different hardware.
