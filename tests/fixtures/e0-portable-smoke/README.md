# E0 portable smoke fixture

This directory contains a synthetic, single-file LaTeX document for the
installed-package smoke test. It contains one conservatively scannable plain-text
paragraph and deliberately contains no labels, references, captions, bibliography,
or other constructs that produce `SEQ`, `REF`, or `PAGEREF` fields in Word.

The fixture exists to exercise the real
`snapshot -> export -> archive -> ingest -> approve -> plan -> apply` loop on a
clean Windows machine where Microsoft Word is not installed. It is not a substitute
for the full E0 fixture or the separate real-Word contract tests. Production export
continues to require Microsoft Word whenever a generated review document contains
live fields.

All content is synthetic, contains no personal or private-paper material, and is
licensed under the repository's Apache-2.0 license.
