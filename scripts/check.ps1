#requires -Version 5.1

[CmdletBinding()]
param(
    [ValidateSet("Quick", "Full", "Release")]
    [string]$Profile = "Quick",
    [string[]]$ChangedPath = @(),
    [switch]$ListOnly,
    [string]$ReleaseRoot = "",
    [string]$Version = "",
    [switch]$SkipExecutableSmokeTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$uv = Get-Command "uv" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1

function ConvertTo-RepoRelativePath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $normalized = $Path.Replace("\", "/")
    while ($normalized.StartsWith("./", [StringComparison]::Ordinal)) {
        $normalized = $normalized.Substring(2)
    }
    if ([string]::IsNullOrWhiteSpace($normalized) -or
        $normalized.StartsWith("/", [StringComparison]::Ordinal) -or
        $normalized.StartsWith("-", [StringComparison]::Ordinal) -or
        $normalized -match '^[A-Za-z]:' -or
        @($normalized.Split("/") | Where-Object { $_ -eq "." -or $_ -eq ".." }).Count -ne 0) {
        throw "changed paths must be safe repository-relative paths"
    }
    $absolute = [IO.Path]::GetFullPath((Join-Path $repoRoot $normalized))
    $boundary = $repoRoot.TrimEnd("\") + "\"
    if (-not $absolute.StartsWith($boundary, [StringComparison]::OrdinalIgnoreCase)) {
        throw "changed path escapes the repository"
    }
    return $normalized
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    Write-Host ""
    Write-Host "==> $Label"
    Write-Host ("    " + $Command + " " + ($Arguments -join " "))
    if ($ListOnly) {
        return
    }
    $global:LASTEXITCODE = 0
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

function Invoke-Uv {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    if ($null -eq $uv) {
        throw "uv is required. Run the documented development setup first."
    }
    Invoke-Checked -Label $Label -Command $uv.Source -Arguments $Arguments
}

function Get-ChangedPaths {
    if ($ChangedPath.Count -gt 0) {
        return @($ChangedPath)
    }

    $git = Get-Command "git" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $git -or -not (Test-Path -LiteralPath (Join-Path $repoRoot ".git"))) {
        return @()
    }

    $paths = [Collections.Generic.List[string]]::new()
    foreach ($arguments in @(
        @("diff", "--name-only", "--"),
        @("diff", "--cached", "--name-only", "--"),
        @("ls-files", "--others", "--exclude-standard")
    )) {
        $output = @(& $git.Source @arguments)
        if ($LASTEXITCODE -ne 0) {
            throw "git could not determine the changed paths"
        }
        foreach ($path in $output) {
            if (-not [string]::IsNullOrWhiteSpace($path)) {
                $paths.Add($path)
            }
        }
    }
    return @($paths)
}

function Add-Test {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [Collections.Generic.HashSet[string]]$Tests,
        [Parameter(Mandatory = $true)][string]$Path
    )

    $absolute = Join-Path $repoRoot ($Path.Replace("/", "\"))
    if (Test-Path -LiteralPath $absolute -PathType Leaf) {
        $null = $Tests.Add($Path)
    }
}

function Add-Tests {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [Collections.Generic.HashSet[string]]$Tests,
        [Parameter(Mandatory = $true)][string[]]$Paths
    )

    foreach ($path in $Paths) {
        Add-Test -Tests $Tests -Path $path
    }
}

function Get-QuickTests {
    param([Parameter(Mandatory = $true)][string[]]$Paths)

    $tests = [Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    $blockedOptIn = @(
        "tests/test_app_browser_e2e.py",
        "tests/test_word_contract_harness.py"
    )

    foreach ($rawPath in $Paths) {
        $path = $rawPath.Replace("\", "/").TrimStart("./")

        if ($path -like "tests/test_*.py") {
            if ($blockedOptIn -notcontains $path) {
                Add-Test -Tests $tests -Path $path
            }
            continue
        }

        if ($path -match '^src/latex_word_review/([^/]+)[.]py$') {
            Add-Test -Tests $tests -Path ("tests/test_" + $Matches[1] + ".py")
        }

        switch -Regex ($path) {
            '^src/latex_word_review/(app_|application[.]py|windows_dialogs[.]py|user_messages[.]py)' {
                Add-Tests -Tests $tests -Paths @(
                    "tests/test_application.py",
                    "tests/test_app_server.py",
                    "tests/test_app_presenter.py",
                    "tests/test_app_views.py",
                    "tests/test_app_jobs.py"
                )
            }
            '^src/latex_word_review/(workflow|workflow_objects|worker_dispatch|session_status)[.]py$' {
                Add-Tests -Tests $tests -Paths @(
                    "tests/test_workflow.py",
                    "tests/test_cli_workflow.py",
                    "tests/test_workflow_objects.py"
                )
            }
            '^src/latex_word_review/(export|export_models|export_limits|tex2word_compat)[.]py$|^src/latex_word_review/backends/' {
                Add-Tests -Tests $tests -Paths @(
                    "tests/test_export_backends.py",
                    "tests/test_export_inspection.py",
                    "tests/test_export_timeout_contract.py"
                )
            }
            '^src/latex_word_review/(source_features|source_units)[.]py$' {
                Add-Tests -Tests $tests -Paths @(
                    "tests/test_source_features.py",
                    "tests/test_source_units.py",
                    "tests/test_workflow_source_features.py"
                )
            }
            '^src/latex_word_review/(revision_display|revision_macros)[.]py$' {
                Add-Tests -Tests $tests -Paths @(
                    "tests/test_revision_macros.py",
                    "tests/test_revision_display_integrity.py",
                    "tests/test_revision_display_workflow.py"
                )
            }
            '^src/latex_word_review/(image_|graphic_targets[.]py)' {
                Add-Tests -Tests $tests -Paths @(
                    "tests/test_image_materializer.py",
                    "tests/test_image_overlay.py",
                    "tests/test_image_worker.py"
                )
            }
            '^src/latex_word_review/(ingest|revisions|docx_reader|offset_mapping)[.]py$' {
                Add-Tests -Tests $tests -Paths @(
                    "tests/test_revision_ingest.py",
                    "tests/test_ingest_archive.py",
                    "tests/test_docx_reader.py",
                    "tests/test_offset_mapping.py"
                )
            }
            '^src/latex_word_review/(approval|planner|applier)[.]py$' {
                Add-Tests -Tests $tests -Paths @(
                    "tests/test_approval.py",
                    "tests/test_plan_apply.py"
                )
            }
            '^src/latex_word_review/(latex_verify|ledger|bundle|support_bundle)[.]py$' {
                Add-Tests -Tests $tests -Paths @(
                    "tests/test_latex_verify.py",
                    "tests/test_ledger_bundle.py",
                    "tests/test_support_bundle.py"
                )
            }
            '^src/latex_word_review/(discovery|snapshot|doctor|runtime|frozen_runtime|paths|hashing)[.]py$' {
                Add-Tests -Tests $tests -Paths @(
                    "tests/test_discovery.py",
                    "tests/test_snapshot.py",
                    "tests/test_doctor_runtime.py",
                    "tests/test_paths_hashing.py"
                )
            }
            '^src/latex_word_review/(domain_values|run_layout)[.]py$' {
                Add-Tests -Tests $tests -Paths @(
                    "tests/test_shared_domain_layout.py",
                    "tests/test_application.py",
                    "tests/test_workflow.py"
                )
            }
            '^src/latex_word_review/(contracts|schema_catalog|canonical|jsonio|ids|errors)[.]py$|^src/latex_word_review/schemas/' {
                Add-Tests -Tests $tests -Paths @(
                    "tests/test_contracts.py",
                    "tests/test_jsonio.py",
                    "tests/test_core_primitives_coverage.py"
                )
            }
            '^src/latex_word_review/(word_|docx_anchor|review_layout|review_reference)[^/]*[.]py$' {
                Add-Tests -Tests $tests -Paths @(
                    "tests/test_word_semantics.py",
                    "tests/test_word_fields.py",
                    "tests/test_docx_anchor.py",
                    "tests/test_review_layout.py",
                    "tests/test_review_reference.py"
                )
            }
            '^src/latex_word_review/(cli|__main__)[.]py$' {
                Add-Tests -Tests $tests -Paths @(
                    "tests/test_cli.py",
                    "tests/test_cli_workflow.py"
                )
            }
            '^(README[^/]*[.]md|CONTRIBUTING[.]md|docs/|scripts/README[.]md)' {
                Add-Tests -Tests $tests -Paths @(
                    "tests/test_public_onboarding_contract.py",
                    "tests/test_maintenance_entrypoint.py"
                )
            }
            '^(scripts/check[.]ps1|docs/architecture/code-map[.]md)$' {
                Add-Test -Tests $tests -Path "tests/test_maintenance_entrypoint.py"
            }
            '^(scripts/(bootstrap-windows|build-windows|deploy-local-windows|verify-windows-release|test-windows-installer)[.]ps1|packaging/|installer/|[.]github/workflows/)' {
                Add-Tests -Tests $tests -Paths @(
                    "tests/test_windows_bootstrap.py",
                    "tests/test_windows_packaging.py"
                )
            }
            '^(Start-Latex-Word-Review[.]cmd|[.]lwr-runtime/)' {
                Add-Test -Tests $tests -Path "tests/test_windows_bootstrap.py"
            }
            '^(skills/|plugins/|[.]agents/)' {
                Add-Test -Tests $tests -Path "tests/test_skill_contract.py"
            }
            '^(pyproject[.]toml|uv[.]lock|[.]github/scripts/)' {
                Add-Tests -Tests $tests -Paths @(
                    "tests/test_coverage_configuration.py",
                    "tests/test_windows_packaging.py"
                )
            }
        }
    }

    if ($tests.Count -eq 0) {
        Add-Tests -Tests $tests -Paths @(
            "tests/test_coverage_configuration.py",
            "tests/test_public_onboarding_contract.py"
        )
    }
    return @($tests | Sort-Object)
}

function Invoke-QuickProfile {
    $paths = @(
        Get-ChangedPaths |
            ForEach-Object { ConvertTo-RepoRelativePath -Path $_ } |
            Sort-Object -Unique
    )
    Write-Host "Quick profile: $($paths.Count) changed path(s)"
    foreach ($path in $paths) {
        Write-Host "  $path"
    }

    $pythonPaths = @(
        $paths |
            Where-Object { $_ -like "*.py" -and (Test-Path -LiteralPath (Join-Path $repoRoot $_)) } |
            Sort-Object -Unique
    )
    if ($pythonPaths.Count -gt 0) {
        Invoke-Uv -Label "Lint changed Python files" `
            -Arguments (@("run", "--frozen", "ruff", "check", "--") + $pythonPaths)
        Invoke-Uv -Label "Check formatting of changed Python files" `
            -Arguments (@("run", "--frozen", "ruff", "format", "--check", "--") + $pythonPaths)
        Invoke-Uv -Label "Type-check changed Python files" `
            -Arguments (@("run", "--frozen", "mypy", "--") + $pythonPaths)
    }

    if ($paths -contains "pyproject.toml" -or $paths -contains "uv.lock") {
        Invoke-Uv -Label "Verify the offline lock file" `
            -Arguments @("lock", "--check", "--offline")
    }

    $tests = @(Get-QuickTests -Paths $paths)
    Invoke-Uv -Label "Run focused tests" `
        -Arguments (@("run", "--frozen", "pytest", "--no-cov") + $tests)

    $git = Get-Command "git" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $git -and (Test-Path -LiteralPath (Join-Path $repoRoot ".git"))) {
        Invoke-Checked -Label "Check patch whitespace" -Command $git.Source `
            -Arguments @("diff", "--check")
    }
}

function Invoke-FullProfile {
    Invoke-Uv -Label "Verify the offline lock file" `
        -Arguments @("lock", "--check", "--offline")
    Invoke-Uv -Label "Check repository release boundaries" -Arguments @(
        "run", "--frozen", "python", ".github/scripts/release_checks.py",
        "repo", "--root", ".", "--include-untracked"
    )
    Invoke-Uv -Label "Lint source and tests" `
        -Arguments @("run", "--frozen", "ruff", "check", ".")
    Invoke-Uv -Label "Check source and test formatting" `
        -Arguments @("run", "--frozen", "ruff", "format", "--check", ".")
    Invoke-Uv -Label "Lint release trust-root scripts" -Arguments @(
        "run", "--frozen", "ruff", "check", ".github/scripts"
    )
    Invoke-Uv -Label "Check release trust-root script formatting" -Arguments @(
        "run", "--frozen", "ruff", "format", "--check", ".github/scripts"
    )
    Invoke-Uv -Label "Lint helper scripts" -Arguments @(
        "run", "--frozen", "ruff", "check", "scripts", "--ignore", "E501,I001"
    )
    Invoke-Uv -Label "Self-test release tools" -Arguments @(
        "run", "--frozen", "python", ".github/scripts/selftest_release_tools.py"
    )
    Invoke-Uv -Label "Type-check the project" `
        -Arguments @("run", "--frozen", "mypy")
    Invoke-Uv -Label "Run the complete test suite with branch coverage" -Arguments @(
        "run", "--frozen", "pytest", "--cov=latex_word_review",
        "--cov-report=term-missing"
    )

    $git = Get-Command "git" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $git -and (Test-Path -LiteralPath (Join-Path $repoRoot ".git"))) {
        Invoke-Checked -Label "Check patch whitespace" -Command $git.Source `
            -Arguments @("diff", "--check")
    }
}

function Invoke-ReleaseProfile {
    if ([string]::IsNullOrWhiteSpace($ReleaseRoot)) {
        throw "Release requires -ReleaseRoot pointing to an existing built candidate."
    }
    $verifier = Join-Path $PSScriptRoot "verify-windows-release.ps1"
    if (-not (Test-Path -LiteralPath $verifier -PathType Leaf)) {
        throw "The authoritative Windows release verifier is missing."
    }

    $arguments = @("-ReleaseRoot", $ReleaseRoot)
    if (-not [string]::IsNullOrWhiteSpace($Version)) {
        $arguments += @("-Version", $Version)
    }
    if ($SkipExecutableSmokeTest) {
        $arguments += "-SkipExecutableSmokeTest"
    }
    Invoke-Checked -Label "Run the authoritative Windows release verifier" `
        -Command $verifier -Arguments $arguments
}

Push-Location $repoRoot
try {
    switch ($Profile) {
        "Quick" { Invoke-QuickProfile }
        "Full" { Invoke-FullProfile }
        "Release" { Invoke-ReleaseProfile }
    }
}
finally {
    Pop-Location
}
