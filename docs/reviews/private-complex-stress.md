# Local complex-project stress test (aggregate evidence only)

Date: 2026-07-16

This run used a pre-copied, Git-ignored private working copy. Stress commands
did not modify or run conversion against the original source. This record intentionally omits
paths, filenames, text, author metadata, document hashes, and generated
artifacts; it contains only aggregate counts, statuses, and stable error codes.
It is not a public fixture and cannot be used as reproducible release evidence.

## Regression exercised

The project contains extensionless `\includegraphics` references whose
basenames contain dots and whose assets are found through `\graphicspath`.
The former detector treated every final dotted component as an explicit file
extension. The public synthetic regression now proves that a dotted basename
can resolve to the corresponding `.pdf` asset while ambiguous matches still
fail closed.

## Aggregate results

| Stage | Status | Exit | Aggregate evidence | Stable codes |
| --- | --- | ---: | --- | --- |
| discovery | pass | n/a | 78 files; 77 dependency edges; 70 images; 0 external references | none |
| immutable snapshot | pass | 0 | 78 files; 27,424,401 bytes; source pre/post state unchanged | none |
| tex2word export | partial | 0 | DOCX published; 426 paragraphs; 20 tables; 77 OMML objects; 102 live fields; 91 bookmarks; 0 images | `W_EXPORT_DEGRADED` ×5; `E_MAP_UNMATCHED` ×7 |
| read-only DOCX inspection | pass | 0 | package, structure, and relationships passed; no external relationships | none |

The first export attempt exposed a separate tex2word 1.0.5 compatibility case:
some public report entries carry string severities while others carry enum
objects. The worker now accepts both shapes, with synthetic tests for string
warning/error entries and the existing enum tests retained. The rerun produced
the partial DOCX summarized above.

The zero-image output means this result is **not** evidence of image
preservation for a complex project. It records an upstream/backend capability
gap without treating the partial artifact as a full conversion pass.
