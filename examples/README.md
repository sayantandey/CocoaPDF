# CocoaPDF conversion examples

All inputs and fixture prose are first-party project material under the bundled MIT license.
No network content, OCR, AI, or ML was used.

The three PDFs are intentionally isolated: Tagged-PDF structure trees, AcroForm fields, and outlines are document-catalog semantics. Concatenating their pages would alter the evidence being tested and make failures less diagnostic.

This directory is the committed, reproducible capability demo. Pull-request review artifacts are generated separately and are never written here.

[Browse this revision's side-by-side PDF-to-HTML demo](review.html). GitHub displays committed HTML files as source code; same-repository pull requests receive an exact commit-pinned rendered link in their description.

All links below are revision-relative, so browsing a branch or commit never silently opens output from `main`.

Full semantic JSON is committed. Report summaries omit only duplicate semantic graphs and glyph-heavy internals; the temporary PR artifact retains every full report.

| Case | Coverage | Input | Outputs |
| --- | --- | --- | --- |
| `strategic_corner_cases` | Broad V1-V4 formatting, Unicode, lists, tables, figures, forms, columns, security, and fallback coverage. | [PDF](cases/strategic_corner_cases/input.pdf) | [Markdown](cases/strategic_corner_cases/full/output.md)<br/>[HTML](cases/strategic_corner_cases/full/output.html)<br/>[Semantic JSON](cases/strategic_corner_cases/full/output.json)<br/>[Report](cases/strategic_corner_cases/full/output.report.summary.json) |
| `tagged_semantics` | Tagged heading, sibling ordered-list isolation, MCID provenance, and tagged table structure. | [PDF](cases/tagged_semantics/input.pdf) | [Markdown](cases/tagged_semantics/full/output.md)<br/>[HTML](cases/tagged_semantics/full/output.html)<br/>[Semantic JSON](cases/tagged_semantics/full/output.json)<br/>[Report](cases/tagged_semantics/full/output.report.summary.json) |
| `scope_and_adversarial` | Page-range outline/AcroForm scope, valid heading anchors, dot-leader finance recovery, and two-sided diagram-versus-form/table fidelity. | [PDF](cases/scope_and_adversarial/input.pdf) | [Markdown](cases/scope_and_adversarial/full/output.md)<br/>[HTML](cases/scope_and_adversarial/full/output.html)<br/>[Semantic JSON](cases/scope_and_adversarial/full/output.json)<br/>[Report](cases/scope_and_adversarial/full/output.report.summary.json)<br/>[Page 2 Markdown](cases/scope_and_adversarial/page-2/output.md)<br/>[Page 2 HTML](cases/scope_and_adversarial/page-2/output.html)<br/>[Page 2 Semantic JSON](cases/scope_and_adversarial/page-2/output.json)<br/>[Page 2 Report](cases/scope_and_adversarial/page-2/output.report.summary.json) |
