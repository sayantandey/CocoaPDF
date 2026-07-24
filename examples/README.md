# CocoaPDF permanent capability demo

All inputs and fixture prose are first-party project material under the bundled MIT license.
No network content, OCR, AI, or ML was used.

This directory is the committed, reproducible capability demo. Pull-request review artifacts are generated separately and are never written here.

Each row links the source PDF to the exact output committed from the same CocoaPDF revision.

Full semantic JSON is committed. Report summaries omit only duplicate semantic graphs and glyph-heavy internals; the temporary PR artifact retains every full report.

| Case | Coverage | Input | Outputs |
| --- | --- | --- | --- |
| `strategic_corner_cases` | Broad V1-V4 formatting, Unicode, lists, tables, figures, forms, columns, security, and fallback coverage. | [PDF](cases/strategic_corner_cases/input.pdf) | [full Markdown](cases/strategic_corner_cases/full/output.md), [full HTML](cases/strategic_corner_cases/full/output.html), [full semantic JSON](cases/strategic_corner_cases/full/output.json), [full report](cases/strategic_corner_cases/full/output.report.summary.json) |
| `tagged_semantics` | Tagged heading, sibling ordered-list isolation, MCID provenance, and tagged table structure. | [PDF](cases/tagged_semantics/input.pdf) | [full Markdown](cases/tagged_semantics/full/output.md), [full HTML](cases/tagged_semantics/full/output.html), [full semantic JSON](cases/tagged_semantics/full/output.json), [full report](cases/tagged_semantics/full/output.report.summary.json) |
| `scope_and_adversarial` | Page-range outline/AcroForm scope, dot-leader finance recovery, and diagram-versus-form false-positive resistance. | [PDF](cases/scope_and_adversarial/input.pdf) | [full Markdown](cases/scope_and_adversarial/full/output.md), [full HTML](cases/scope_and_adversarial/full/output.html), [full semantic JSON](cases/scope_and_adversarial/full/output.json), [full report](cases/scope_and_adversarial/full/output.report.summary.json)<br>[page-2 Markdown](cases/scope_and_adversarial/page-2/output.md), [page-2 HTML](cases/scope_and_adversarial/page-2/output.html), [page-2 semantic JSON](cases/scope_and_adversarial/page-2/output.json), [page-2 report](cases/scope_and_adversarial/page-2/output.report.summary.json) |
