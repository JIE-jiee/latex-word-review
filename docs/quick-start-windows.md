# Windows Quick Start

LaTeX Word Review `0.2.0b1` is a local Windows application for authors who write in LaTeX and
collaborate with people who review in Word. It brings approved edits back into a new LaTeX copy in
four steps:

1. choose `main.tex`;
2. generate `review.docx`, save its exact editable copy, send only that copy to the reviewer, and select the same-round returned `.docx`;
3. decide each tracked change;
4. inspect the exact diff, confirm a second time, and generate the revised copy and evidence.

The original LaTeX project and the returned Word original remain immutable. For the complete Chinese
walkthrough, see [`guide.zh-CN.md`](guide.zh-CN.md).

> [!IMPORTANT]
> The current download is a beta source ZIP, not a signed installer. Download it only from this
> repository, keep a backup of your paper, and inspect every generated result.

## Start from the GitHub source ZIP

The public bootstrap requires 64-bit Windows capable of running x64
applications and 64-bit Windows PowerShell. The included CMD launcher selects
the system Windows PowerShell automatically. Other operating systems and
32-bit shells are not supported.

You do not need to install Python, Git, uv, or use administrator permission.

1. Download the [current source ZIP](https://github.com/JIE-jiee/latex-word-review/archive/refs/heads/main.zip).
2. In File Explorer, choose **Extract All**. Do not run the project inside the ZIP preview.
3. Open the extracted folder and double-click **`Start-Latex-Word-Review.cmd`**.
4. Keep this window open and stay online during the first setup. The local page opens automatically
   when preparation finishes. Later, double-click the same file to start again.

The first start downloads and verifies a private Python runtime and the locked application
dependencies inside the extracted project folder. It does not install them system-wide. If setup is
interrupted, note the stable error code shown in the window, check the network connection, and
double-click the launcher again.

The application binds only to a random `127.0.0.1` port. The browser is a local interface, not a
cloud upload page. Keep the extracted folder if you want later starts to reuse the prepared runtime.

> [!NOTE]
> The application interface is currently Simplified Chinese. The Chinese, English, and Japanese
> README links switch the GitHub documentation only.

## Complete one review in four steps

## Step 1: choose the project

Select **New review**, choose the paper's main `.tex` file, inspect the preflight, and select
**Generate review Word**.

The application:

- conservatively discovers static dependencies below the selected project root;
- rejects traversal, link/junction escape, ambiguous inputs, and unsafe references;
- creates a new random session and immutable source snapshot;
- generates the Word review in a bounded local background job;
- applies the deterministic, SHA-256-bound `academic-review-v1` reference profile through
  tex2word's public `reference_doc` API. The default is A4, single-column, Times New Roman/SimSun,
  compact tables, and images bounded to the text width. A failed template load blocks export.

It never writes generated files into the original source tree.

## Step 2: review in Word and import the return

Before conversion, the application scans canonical `\added`, `\deleted`, and `\replaced` calls only
when a supported static `changes` package declaration is visible in the bound project dependencies.
Bare canonical calls, a discovery-bound project-local `changes.sty`, a visible `\input@path`
modification, and direct or dynamic canonical definitions/redefinitions fail closed. Statically
recognized `\add` and `\delete` aliases retain their separate conservative rules. The clean review
view keeps new text for additions and replacements and removes old text for deletions.

| Generated file | Purpose |
|---|---|
| `review.docx` | The sealed internal clean baseline. Use the application to save its exact editable copy. Only a copy in this review role may be sent out and returned |
| `existing-changes-display.docx` | Generated only when existing change macros are detected. It is solely for viewing changes already present in the source and must never be imported as a returned Word file |

The display file uses one consistent `0000FF` blue style: additions are blue; deletions use blue single strikethrough; replacements show old blue single-struck text immediately followed by new blue text. This feature adds no highlighting. The file contains static formatting rather than native Word revisions, and Track Changes is not enabled.

`\add` is accepted only when exactly one static source is recognized: a supported `trackchanges`
package declaration or a static local direct wrapper to `\added`. A package-backed `\add` is rejected
when a discovery-bound `trackchanges.sty` or visible `\input@path` override could alter package
selection. `\delete` is accepted only as exactly one static local direct wrapper to `\deleted`. A local
wrapper target must also pass the static `changes` declaration gate. Bare calls, conflicting or
multiple sources, incompatible signatures, and dynamic definitions fail closed before conversion.

This gate does not run `kpsewhich` or resolve system/user TeX package trees. It verifies only a
statically visible declaration plus the absence of a discovered project-tree shadow or detected
`\input@path` override; it does not attest the actual package path or file hash loaded by TeX.

The display artifact embeds its role, profile, and task ID in standard Word document variables, and
the task seals its final digest. Import rejects the exact sealed display file and saved or repacked copies
that retain the display-role marker. Blue text alone is not treated as role evidence.

Validation derives expected visible text and a per-character strike mask for every top-level source
macro tree. It checks non-overlapping matches and repeated-instance multiplicity, compares the
clean/display deltas for blue, strike, and highlight formatting, and requires equations, images, tables,
fields, and other non-revision structures to remain consistent. Identical blue text elsewhere cannot
stand in for a missing displayed change. For deleted text, neighboring text in the same paragraph must
uniquely prove the deletion boundary; missing or ambiguous context blocks generation.

The same-paragraph check prevents a deletion from being moved to a guessed boundary. It is not an
exact SourceMap for automatic writeback and does not cryptographically bind a LaTeX call to a Word
coordinate. Later edits to macro-derived text may therefore be recorded but routed to manual handling
when the LaTeX location cannot be proven.

Only direct, literal, static, safe inline calls are handled; dynamic conditional regions are skipped as a whole. A directly recognized argument containing structured content such as mathematics, references, images, environments, footnotes, or paragraph breaks fails closed before conversion. When macros are found, both Word files must pass this validation in the same staged export before either is published.

Use **Save editable Word** to create the exact review copy, and send only that copy to the reviewer.
Ask the reviewer to use Microsoft Word with Track Changes enabled, use comments for discussion, and
avoid Accept All, deleting bookmarks, or saving as legacy `.doc`.

This is an editable semantic review layout, not a pixel reproduction of the LaTeX PDF or a journal
submission template. Explicit source font declarations may override the default profile, and figures,
pagination, formulas, tables, and special fields should still be reviewed in Word.

When the `.docx` returns, select **Choose returned Word and read changes**. The application first
archives the original bytes read-only, then compares the returned reject-changes view with the
sealed export baseline before extracting revisions. Baseline drift, Accept All, untracked visible
text edits, a wrong review round, or damaged anchors fail closed.

The reviewer may use Word on another Windows computer. A normal paper containing live `SEQ`, `REF`,
or `PAGEREF` fields also needs Microsoft Word on the application machine so the program can refresh
and freeze those fields; a document with no live fields skips that automation. Returned-DOCX parsing
does not launch Word. Word is never bundled or installed silently.

## Step 3: decide every change

Each Chinese approval card shows before/after text, author, timestamp, context, source location,
confidence, safety class, and diagnostics. Record one of:

- accept;
- accept with edited final text;
- reject;
- manual;
- conflict.

The restricted **accept all safe text** action fills only still-undecided exact
`plain_text_candidate` changes. It never overwrites an existing decision, and it excludes formulas,
references, structure, moves, formatting, comments, low-confidence mappings, and conflicts.

For long reviews, cards are paginated at 25 items per page and can be filtered
without changing the ledger. The bulk **mark risky undecided items
as manual** action affects only still-undecided non-exact-safe items. It never
overwrites a decision, accepts an edit, or makes an automatic source change;
those entries are thereby resolved as manual for approval, remain excluded
from automatic writeback, and can still be changed individually before finalization.

Select **Finish approval and preview patch** only after every item has a decision. This is the first
human gate. It seals intent and produces a dry-run preview; it does not change LaTeX.

## Step 4: inspect the diff and confirm again

Review counts and the unified diff for every affected file. A plan containing
`accepted_but_blocked` cannot continue. Explicitly select **重新审批** (re-approve), change the
relevant items to manual/rejected handling, and create a new immutable approval/plan revision.
Existing decisions and old evidence remain intact; neither the bulk action nor replanning overwrites
them.

For a ready/noop plan, tick the explicit confirmation and select **Confirm and generate all
results**. This is the independent second gate. The application rechecks the exact PatchPlan hash,
source hashes, UTF-8 byte spans, overlap, and safety policy before creating `revised-clean/`.

It then attempts verification, ledger generation, and the allowlisted audit bundle. Missing TeX
tools produce an honest partial result instead of discarding the safely revised LaTeX tree.

## Results

Typical artifacts are:

```text
export/
├─ review.docx
└─ existing-changes-display.docx   # optional: static reference only, not review evidence
receive/original/returned-original.docx
receive/changeset.json
approvals/approval-rN.json
plans/plan-rN/changes.patch
revised-clean/
verification/revised-clean.pdf
verification/latexdiff.tex
verification/latexdiff.pdf
ledger/ledger.json
ledger/ledger.html
audit.zip
```

The returned review evidence must come from the same-round exact editable copy of `review.docx`. The display file does not participate in import, approval, or LaTeX writeback; its sealed digest and embedded role marker are explicit rejection signals.

`revised-clean/` is the clean authoritative candidate. When there is an actual accepted source
difference, `latexdiff.tex` contains the derived add/delete markup and a successfully compiled
`latexdiff.pdf` shows it visually; a no-op plan has no artificial marks. Word authors, timestamps,
comments, and decisions remain in ChangeSet/ledger evidence rather than in `latexdiff`.

PDF figures are rendered only in a derived overlay as canonical PNG review previews. The original
PDF and LaTeX stay unchanged. Unsupported SVG/EPS, `pagebox`, dynamic paths, or ambiguous operations
remain manual.

## Resume and exit

Sessions live below:

```text
%LOCALAPPDATA%\LatexWordReview\runs\session_<random-id>\
```

The workspace-only launcher instead keeps sessions below
`output\local-windows\user-data\runs` and `TEMP/TMP` below its `temp` directory.

Reopen the application and choose a recent task. It reconstructs the phase from sealed evidence,
not browser cache. Explicit recovery controls may create the first approval ledger, rebuild a
dry-run preview, or start a new immutable verification attempt, but never make user decisions or
cross the apply gate.

Closing the browser tab does not stop the process. Use **Exit application** on the home or result
page. If a background job is active, shutdown waits for it to finish safely. In a source terminal,
`Ctrl+C` also stops the server.

## External tools

- A reviewer needs Microsoft Word for Windows to produce native tracked-change evidence.
- The application machine also needs Word when the generated review contains live fields.
- Clean PDF output needs the document's TeX engine/packages/fonts and `latexmk`.
- Marked PDF output additionally needs `latexdiff`.
- Missing tools are reported as partial/blocked; they are never installed silently.

Automatic PDF verification on Windows currently accepts only one coherent MiKTeX installation.
Default tool names are resolved from absolute `PATH` entries, then from the standard per-user
MiKTeX location. `latexmk` and `latexdiff` must belong to that same installation, and a regular
Perl executable must also resolve from an absolute `PATH` entry. The verifier creates private
MiKTeX config/data, HOME, and temporary directories and does not install or update packages.
TeX Live, mixed roots, missing packages, or missing Perl remain explicit partial/blocked results.

## Advanced setup and maintainer notes

The ordinary source-ZIP path above is the supported public starting point. The options below are for
people who need a reviewed commit, development tools, or local release-candidate testing.

### Pin and run a reviewed source commit

Install Git and uv, then replace the placeholder with the exact 40-character commit SHA you have
reviewed:

```powershell
git clone https://github.com/JIE-jiee/latex-word-review.git
Set-Location latex-word-review
$ReviewedCommit = "PASTE_THE_REVIEWED_40_CHARACTER_COMMIT_SHA_HERE"
git checkout --detach $ReviewedCommit
if ((git rev-parse HEAD).Trim() -ne $ReviewedCommit) { throw "Commit verification failed" }
uv sync --frozen --extra pdf-figures --python 3.12
uv run --frozen latex-word-review --version
uv run --frozen latex-word-review doctor
uv run --frozen latex-word-review app
```

The placeholder is intentionally invalid so the command fails instead of silently following a
moving branch.

### Workspace-only frozen candidate

A maintainer workspace uses the same root `Start-Latex-Word-Review.cmd` entry as a source ZIP.
When a structurally complete ignored delivery is present below `output\local-windows`, the root
launcher delegates to its frozen launcher first; otherwise it automatically uses the source
bootstrap. `LaTeX Word Review（双击启动）.lnk` may point to this single root entry. The frozen
delivery keeps its application, review data, and temporary files below `output\local-windows`.
It is not part of the public source ZIP and does not represent a published GitHub binary.

### Binary publication status

There is no GitHub Release, PyPI publication, signed installer, or public portable binary yet. Local
installer and portable candidates have passed build and runtime checks, but public redistribution
remains blocked while native-library licensing and relinking evidence is incomplete. See the
[Windows binary license audit](reviews/windows-binary-license-audit-2026-07.md).

One historical local `0.2.0b1` Windows x64 candidate built on 2026-07-20 measured:

| Asset | Measured size | SHA-256 |
|---|---:|---|
| setup executable | **21,444,947 bytes (20.45 MiB)** | `6028a469f571f29c92219c36e23f2bd47d85515b99065ca50c9d82ef6d532904` |
| portable ZIP | **33,421,203 bytes (31.87 MiB)** | `e95f5b6bb90017c0f0f25f811b3f25b0c882dbe5bfb876ee253304f5d9fe13dd` |
| installed/extracted application | **65,767,086 bytes (62.72 MiB)** (438 files) | — |

A future installer is intended to be per-user and require no administrator permission. A future
portable ZIP is intended to start with `LatexWordReview.exe`. Neither option is available for public
download today. If a future beta is not Authenticode-signed, verify the GitHub Release checksum and
signing status before running it. Do not obtain a same-named executable from a third-party mirror.

## Codex Skill and advanced CLI

The `$latex-word-review` Skill is a thin orchestrator. For an ordinary review it should launch
`latex-word-review app` and leave file selection, per-change decisions, and both human gates to the
user. Granular commands are for an explicit CLI/agent request or recovery; the Skill must never
infer blanket acceptance, overwrite an existing decision, or cross either gate. A blocked plan must
return through the application's explicit re-approval action.

Advanced entry points:

```powershell
latex-word-review app --data-root C:\review-data
latex-word-review app --no-browser
latex-word-review workflow status <run-root>
latex-word-review workflow clean <run-root>
```

`workflow clean` is a dry run. Use `workflow clean <run-root> --execute` only after reviewing the
allowlisted staging directories. It cannot delete the immutable snapshot, export, returned
original, ChangeSet, approvals, plans, revised tree, or delivery evidence.

The full granular contract remains documented in [CLI and run directory](reference/cli.md).
Do not edit sealed JSON or guess safety-critical command arguments.
