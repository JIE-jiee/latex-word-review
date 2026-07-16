# Support

## Supported releases

Until `1.0`, only the newest published prerelease is maintained. Security fixes may be backported
when risk and compatibility permit, but there is no guaranteed backport window. Unreleased source
from `main` is development material, not a supported release.

The detailed operating-system, Python, backend, and external-tool tiers are maintained in
[`docs/compat/platform-support.md`](docs/compat/platform-support.md). The initial release target is:

- Windows and Ubuntu with CPython 3.12 or 3.13 as Tier 1 for the repository-controlled workflow;
- Ubuntu with CPython 3.12 additionally requires the configured real XeLaTeX/CTeX/`latexmk`/
  `latexdiff` public E2E lane with one pass and zero skips;
- macOS with CPython 3.12 as Tier 2 for the core library, CLI, tests, and wheel installation;
- Python 3.14 is outside the current `Requires-Python` range; unlisted external-tool combinations
  remain experimental until CI evidence and an explicit support decision are added.

The canonical repository is
[`JIE-jiee/latex-word-review`](https://github.com/JIE-jiee/latex-word-review). For this public beta
source, the authoritative remote validation state is the GitHub Actions result attached to the exact
commit. A tagged public prerelease remains blocked until the required lanes pass.
Support means that the declared, repository-controlled checks pass. It does not imply that every
LaTeX package, Word layout, TeX distribution, or third-party converter behavior is covered.
The Ubuntu real-TeX workflow and its release rerun are configured. Pandoc remains a detected
baseline/degraded backend and is not part of the heavyweight TeX CI lane.

## Getting help

Use [GitHub Issues](https://github.com/JIE-jiee/latex-word-review/issues) for reproducible,
non-sensitive defects and compatibility questions. Include:

- the package version or commit SHA;
- operating system, Python version, backend, and external-tool versions;
- the exact command and stable error code;
- the smallest synthetic or explicitly redistributable reproducer;
- whether the problem occurs before approval, during planning, or during apply/verify.

Do not upload a private paper, a returned reviewer Word file, reviewer identity, local absolute path,
credentials, or an audit bundle containing sensitive content. Redact logs before posting. Follow
[`SECURITY.md`](SECURITY.md) and use the
[private vulnerability reporting form](https://github.com/JIE-jiee/latex-word-review/security/advisories/new)
for vulnerabilities instead of opening a public issue.

## What maintainers can promise

Maintainers can triage repository-controlled code and documented upstream combinations. They cannot
guarantee support for proprietary Word behavior, arbitrary LaTeX macros, unlisted Pandoc/filter
pairs, or third-party tools installed outside their supported version range. A failure in those
areas must remain explicit and fail closed; it must not be worked around by disabling hashes,
approval gates, path checks, or immutable-input protection.
