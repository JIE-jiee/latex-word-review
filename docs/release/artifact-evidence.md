# Release artifact evidence

Each successful release-candidate run uploads the two Python distributions plus four evidence files.

| File | Meaning |
|---|---|
| `SHA256SUMS` | SHA-256 digest of the wheel and sdist, sorted by filename |
| `sbom.cdx.json` | CycloneDX 1.6 inventory of the installed project and its reachable, lock-derived Python runtime closure |
| `provenance.intoto.json` | unsigned in-toto Statement v1 using the SLSA v1 predicate type; a repository-defined, SLSA-compatible record binding source ref/SHA, selected build parameters, and distribution subjects |
| `evidence-manifest.json` | SHA-256 binding for the distributions and the other evidence files; it intentionally does not hash itself |

The release job creates the SBOM from the Python interpreter inside the freshly installed wheel
venv, not from the development environment. It follows package metadata from
`latex-word-review` and records only the reachable Python runtime closure. It does **not** inventory
Hatchling or other build tools, GitHub Actions, the Windows runner image, MiKTeX Setup Utility,
MiKTeX repository, or installed TeX packages/tools; those remain separate workflow/toolchain
evidence and this is not a complete supply-chain SBOM.

Before that venv is used for evidence generation, it completes the public E0
snapshot→export→archive→ingest→approve→plan→apply loop from the installed wheel. A separate fresh
venv proves the same loop from the installed sdist. Both paths export exact runtime pins and hashes
from `uv.lock`, build any source-only runtime wheel without PEP 517 isolation in the verified locked
builder, bind the resulting local wheels to fresh SHA-256 hashes, install them with
`--no-index --no-deps`, then run `pip check`. The sdist itself is safely unpacked and converted to a
wheel by the same locked builder with `python -m build --no-isolation`; pip never creates an implicit build
environment for the project sdist. Before that command can run, the helper byte-binds the sdist
`pyproject.toml` to the reviewed checkout, permits only the declarative `hatchling.build` /
`hatchling==1.31.0` contract, rejects backend paths and custom hooks, and binds the static version,
`PKG-INFO`, archive/root names, and reference-wheel identity. The provenance subjects are the
distribution files proved identical across two
builds. The derived wheel must also have the same ordered member list and member bytes (including
METADATA and RECORD) as the checked release wheel; only ZIP container metadata may differ.

The builder-side pip is version-checked against `uv.lock`. A fresh runtime venv starts with the pip
bundled by that CPython installation; this bootstrap installer is not part of the application
runtime closure or SBOM and is allowed to install only the already local, hash-bound wheel set plus
the separately checked project wheel. It performs no dependency resolution or source build.

## Trust boundary

The provenance is **unsigned** and repository-defined. Although it uses the SLSA v1 predicate URI
and compatible field shapes, it is not a SLSA attestation or a cryptographic proof of runner
identity, and it does not enumerate the complete
build/Actions/Windows-runner/MiKTeX-bootstrap/package-repository dependency graph. The Actions
artifact also is not a public release and expires after 14 days. Consumers must bind the downloaded
files to a reviewed source commit, verify all digests, and apply their own trust policy.

Future maintainers may add GitHub artifact attestations or another keyless signing system only in a
separate workflow review. Such a change requires narrowly scoped OIDC/attestation permissions and
must not add a long-lived PyPI or repository-write secret.

## Verification

From the repository root, with the candidate directories named `dist/` and `release-evidence/`:

```text
uv run --frozen python .github/scripts/release_checks.py dist --dir dist --twine
uv run --frozen python .github/scripts/release_checks.py evidence --dist-dir dist --evidence-dir release-evidence
```

The verifier rejects missing or duplicate artifacts, non-canonical or non-portable paths, path
aliases, archive traversal/links/devices, ZIP symlinks, decompression/member/expanded-size limit
violations, undeclared binary/top-level payloads, forbidden runtime, test, or fixture material,
private-path/secret patterns in text members,
duplicate or drifted identity/Requires-Python/runtime/extra metadata, license/schema loss, checksum
drift, and provenance subject drift. The sdist is an
installation source archive, not a partial test archive; the complete tests and public fixture are
distributed through the matching GitHub source tree.
