# Contributing to CocoaPDF

Thank you for helping improve deterministic, explainable PDF conversion.
CocoaPDF accepts focused pull requests backed by PDF-native evidence,
adversarial tests, and a clear account of user-visible behavior.

## Repository workflow

The `main` branch is protected. Every change must arrive through a pull
request, pass the required checks, and receive approval from the repository
owner before merge.

If you are not an explicitly added repository collaborator:

1. Fork `sayantandey/CocoaPDF`.
2. Create a focused topic branch in your fork.
3. Push the branch to your fork.
4. Open a pull request from that branch to CocoaPDF's `main` branch.

Only collaborators whom the owner has deliberately granted write access may
create branches in the upstream repository. Upstream collaborators still use a
topic branch and pull request; nobody pushes directly to `main`.

Keep one conceptual change per pull request. Explain the semantic behavior,
evidence, risks, and verification in the pull-request template.

## Development and verification

CocoaPDF uses Python 3.9 or later and has no runtime dependency outside the
standard library.

Run the repository gates before submitting:

```bash
python scripts/check_repository_invariants.py
python scripts/refresh_examples.py --check
python -m unittest discover -s tests -v
python -m compileall -q src tests tools scripts validation
```

Changes to parsing or semantics should include:

- a positive case proving the intended PDF evidence and output;
- an adversarial near-miss proving false-positive resistance;
- assertions for confidence, evidence, warnings, and provenance where
  applicable;
- coverage across the affected Markdown, HTML, JSON, and report surfaces;
- no fixture-name, file-hash, producer-name, or document-text shortcut.

Prefer small first-party generators over opaque binary fixtures. Regenerate
the permanent capability demo with
`python scripts/refresh_examples.py --write` only when its inputs, generator,
or expected engine output intentionally changes.

## Rights, licenses, and document safety

By intentionally submitting a contribution for inclusion in CocoaPDF, you
represent that you have the right to submit it and agree that it is provided
under the repository's [MIT License](LICENSE), unless the owner agrees to
different terms in writing before submission. You retain any copyright you
already hold.

Do not submit confidential documents, personal data, trade secrets, leaked
material, credentials, or content whose redistribution rights are unclear.
Public availability is not permission to copy a PDF into this repository.

Every committed PDF, source document, font, image, or other fixture asset must
be either:

- original project material that you have the authority to license under MIT;
  or
- material whose license expressly permits repository redistribution and the
  intended derivative/test use.

For non-original material, record the author or owner, canonical source,
exact license and version, retrieval or generation method, relevant tool
versions, and cryptographic hash. Preserve required attribution and license
texts. When those facts cannot be established, create an equivalent
first-party fixture instead.

Do not paste code or fixtures from another project merely because that project
is visible online. Do not submit generated material unless you can grant all
rights required by the MIT license and can account for its source inputs.

## Security reports

Do not report vulnerabilities in a public issue or pull request, and never
attach a sensitive customer PDF. Follow [SECURITY.md](SECURITY.md) and use the
private GitHub Security Advisory flow.
