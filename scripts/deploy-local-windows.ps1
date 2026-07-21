#requires -Version 5.1

[CmdletBinding()]
param(
    [string]$CandidateApp = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-FullPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$BasePath
    )

    if ([IO.Path]::IsPathRooted($Path)) {
        return [IO.Path]::GetFullPath($Path)
    }
    return [IO.Path]::GetFullPath((Join-Path $BasePath $Path))
}

function Assert-DirectChild {
    param(
        [Parameter(Mandatory = $true)][string]$Parent,
        [Parameter(Mandatory = $true)][string]$Child,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $parentFull = [IO.Path]::GetFullPath($Parent).TrimEnd('\', '/')
    $childFull = [IO.Path]::GetFullPath($Child).TrimEnd('\', '/')
    $childParent = [IO.Path]::GetDirectoryName($childFull)
    if (-not $childParent.Equals($parentFull, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must be one direct child of $parentFull"
    }
}

function Assert-ContainedPath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $candidateFull = [IO.Path]::GetFullPath($Candidate)
    $prefix = $rootFull + [IO.Path]::DirectorySeparatorChar
    if (-not $candidateFull.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must remain below $rootFull"
    }
}

function Assert-RealTree {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "$Label is missing: $Path"
    }
    $root = Get-Item -Force -LiteralPath $Path
    if (($root.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label must not be a reparse point: $Path"
    }
    $linked = Get-ChildItem -Force -Recurse -LiteralPath $Path | Where-Object {
        ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
    } | Select-Object -First 1
    if ($null -ne $linked) {
        throw "$Label contains a reparse point: $($linked.FullName)"
    }
}

function Get-RelativeFilePath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path
    )

    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $pathFull = [IO.Path]::GetFullPath($Path)
    Assert-ContainedPath -Root $rootFull -Candidate $pathFull -Label "manifest file"
    return $pathFull.Substring($rootFull.Length).TrimStart('\', '/').Replace('\', '/')
}

function Assert-AppManifest {
    param([Parameter(Mandatory = $true)][string]$AppRoot)

    Assert-RealTree -Path $AppRoot -Label "application tree"
    $manifestPath = Join-Path $AppRoot "CONTENTS.sha256"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "application content manifest is missing: $manifestPath"
    }

    $expected = New-Object 'Collections.Generic.Dictionary[string,string]' (
        [StringComparer]::OrdinalIgnoreCase
    )
    $orderedPaths = New-Object 'Collections.Generic.List[string]'
    foreach ($line in @(Get-Content -Encoding utf8 -LiteralPath $manifestPath)) {
        if ($line -notmatch '^(?<digest>[0-9a-f]{64}) \*(?<path>[^\\]+)$') {
            throw "application content manifest contains an invalid row"
        }
        $relative = $Matches.path
        if ($relative.StartsWith('/') -or $relative.Contains(':') -or
            $relative.Contains([char]0) -or $relative.Split('/') -contains '..' -or
            $relative.Split('/') -contains '.' -or $relative.Split('/') -contains '') {
            throw "application content manifest contains an unsafe path"
        }
        if ($relative.Equals("CONTENTS.sha256", [StringComparison]::OrdinalIgnoreCase) -or
            $expected.ContainsKey($relative)) {
            throw "application content manifest contains a duplicate path"
        }
        $expected.Add($relative, $Matches.digest)
        $orderedPaths.Add($relative)
    }
    if ($expected.Count -eq 0) {
        throw "application content manifest is empty"
    }
    $sortedPaths = @($orderedPaths | Sort-Object { $_.ToLowerInvariant() })
    if (($orderedPaths -join "`n") -cne ($sortedPaths -join "`n")) {
        throw "application content manifest is not deterministically sorted"
    }

    $actual = New-Object 'Collections.Generic.Dictionary[string,string]' (
        [StringComparer]::OrdinalIgnoreCase
    )
    foreach ($file in @(Get-ChildItem -Force -File -Recurse -LiteralPath $AppRoot)) {
        if ($file.FullName -eq $manifestPath) {
            continue
        }
        $relative = Get-RelativeFilePath -Root $AppRoot -Path $file.FullName
        if ($actual.ContainsKey($relative)) {
            throw "application tree contains a portable path collision"
        }
        $actual.Add($relative, $file.FullName)
    }
    if ($actual.Count -ne $expected.Count) {
        throw "application tree differs from CONTENTS.sha256"
    }
    foreach ($relative in $expected.Keys) {
        if (-not $actual.ContainsKey($relative)) {
            throw "application tree is missing manifest path: $relative"
        }
        $digest = (Get-FileHash -Algorithm SHA256 -LiteralPath $actual[$relative]).Hash
        if (-not $digest.Equals($expected[$relative], [StringComparison]::OrdinalIgnoreCase)) {
            throw "application content hash mismatch: $relative"
        }
    }
    foreach ($required in @("LatexWordReview.exe", "latex-word-review.exe")) {
        if (-not $expected.ContainsKey($required)) {
            throw "application tree is missing required executable: $required"
        }
    }

    return [ordered]@{
        files = $expected.Count + 1
        bytes = (Get-ChildItem -Force -File -Recurse -LiteralPath $AppRoot |
            Measure-Object -Property Length -Sum).Sum
        manifest_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $manifestPath).Hash.ToLowerInvariant()
    }
}

function Get-TopManifestLines {
    param(
        [Parameter(Mandatory = $true)][string]$LocalRoot,
        [Parameter(Mandatory = $true)][string]$AppRoot
    )

    $guideName = -join @(
        [char]0x4F7F,
        [char]0x7528,
        [char]0x8BF4,
        [char]0x660E,
        ".txt"
    )
    $launcher = Join-Path $LocalRoot "Start-Latex-Word-Review.cmd"
    $guide = Join-Path $LocalRoot $guideName
    $appManifest = Join-Path $AppRoot "CONTENTS.sha256"
    foreach ($required in @($launcher, $guide, $appManifest)) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "local delivery file is missing: $required"
        }
        $item = Get-Item -Force -LiteralPath $required
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "local delivery file must not be a reparse point: $required"
        }
    }
    return @(
        "$((Get-FileHash -Algorithm SHA256 -LiteralPath $launcher).Hash.ToLowerInvariant()) *Start-Latex-Word-Review.cmd",
        "$((Get-FileHash -Algorithm SHA256 -LiteralPath $guide).Hash.ToLowerInvariant()) *$guideName",
        "$((Get-FileHash -Algorithm SHA256 -LiteralPath $appManifest).Hash.ToLowerInvariant()) *app/CONTENTS.sha256"
    )
}

function Assert-TopManifest {
    param(
        [Parameter(Mandatory = $true)][string]$LocalRoot,
        [Parameter(Mandatory = $true)][string]$AppRoot
    )

    $manifest = Join-Path $LocalRoot "SHA256SUMS.txt"
    if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) {
        throw "local delivery manifest is missing"
    }
    $actual = @(Get-Content -Encoding utf8 -LiteralPath $manifest)
    $expected = @(Get-TopManifestLines -LocalRoot $LocalRoot -AppRoot $AppRoot)
    if (($actual -join "`n") -cne ($expected -join "`n")) {
        throw "local delivery SHA256SUMS.txt is stale or malformed"
    }
}

function Remove-OwnedDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Parent,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    Assert-DirectChild -Parent $Parent -Child $Path -Label $Label
    Assert-RealTree -Path $Path -Label $Label
    [IO.Directory]::Delete($Path, $true)
}

if ($env:OS -ne "Windows_NT") {
    throw "Windows is the only supported deployment host"
}

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$buildRoot = Join-Path $repoRoot "build"
$localRoot = Join-Path $repoRoot "output\local-windows"
if ([string]::IsNullOrWhiteSpace($CandidateApp)) {
    $CandidateApp = Join-Path $buildRoot "windows-release\dist\latex-word-review"
}
$candidate = Get-FullPath -Path $CandidateApp -BasePath $repoRoot
Assert-ContainedPath -Root $buildRoot -Candidate $candidate -Label "candidate application"
Assert-RealTree -Path $candidate -Label "candidate application"

Assert-RealTree -Path $localRoot -Label "local delivery root"
$localRootItem = Get-Item -Force -LiteralPath $localRoot
if (($localRootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw "local delivery root must not be a reparse point"
}
$guideName = -join @(
    [char]0x4F7F,
    [char]0x7528,
    [char]0x8BF4,
    [char]0x660E,
    ".txt"
)
$allowedEntries = @(
    "app",
    "user-data",
    "SHA256SUMS.txt",
    "Start-Latex-Word-Review.cmd",
    $guideName
)
$unexpected = Get-ChildItem -Force -LiteralPath $localRoot | Where-Object {
    $allowedEntries -notcontains $_.Name
} | Select-Object -First 1
if ($null -ne $unexpected) {
    throw "local delivery root contains an unexpected entry: $($unexpected.Name)"
}
$userData = Join-Path $localRoot "user-data"
if (-not (Test-Path -LiteralPath $userData -PathType Container)) {
    throw "local user-data boundary is missing"
}
$userDataItem = Get-Item -Force -LiteralPath $userData
if (($userDataItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw "local user-data boundary must not be a reparse point"
}

$running = @(Get-Process -Name "LatexWordReview", "latex-word-review" -ErrorAction SilentlyContinue)
if ($running.Count -ne 0) {
    throw "close the local application before deploying a new build"
}

$appRoot = Join-Path $localRoot "app"
$null = Assert-AppManifest -AppRoot $appRoot
Assert-TopManifest -LocalRoot $localRoot -AppRoot $appRoot
$candidateInfo = Assert-AppManifest -AppRoot $candidate

$token = [Guid]::NewGuid().ToString("N")
$stage = Join-Path $localRoot ".app.stage-$token"
$rollback = Join-Path $localRoot ".app.rollback-$token"
$shaStage = Join-Path $localRoot ".SHA256SUMS.stage-$token"
$shaRollback = Join-Path $localRoot ".SHA256SUMS.rollback-$token"
$oldMoved = $false
$newPublished = $false
$shaPublished = $false

try {
    $null = New-Item -ItemType Directory -Path $stage
    foreach ($item in @(Get-ChildItem -Force -LiteralPath $candidate)) {
        Copy-Item -Force -Recurse -LiteralPath $item.FullName -Destination (
            Join-Path $stage $item.Name
        )
    }
    $null = Assert-AppManifest -AppRoot $stage
    $encoding = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllLines(
        $shaStage,
        @(Get-TopManifestLines -LocalRoot $localRoot -AppRoot $stage),
        $encoding
    )

    [IO.Directory]::Move($appRoot, $rollback)
    $oldMoved = $true
    [IO.Directory]::Move($stage, $appRoot)
    $newPublished = $true
    [IO.File]::Replace($shaStage, (Join-Path $localRoot "SHA256SUMS.txt"), $shaRollback, $true)
    $shaPublished = $true

    $deployedInfo = Assert-AppManifest -AppRoot $appRoot
    Assert-TopManifest -LocalRoot $localRoot -AppRoot $appRoot
    if ($deployedInfo.manifest_sha256 -ne $candidateInfo.manifest_sha256 -or
        $deployedInfo.files -ne $candidateInfo.files -or
        $deployedInfo.bytes -ne $candidateInfo.bytes) {
        throw "deployed application differs from the verified candidate"
    }

    Remove-OwnedDirectory -Parent $localRoot -Path $rollback -Label "deployment rollback"
    $oldMoved = $false
    if (Test-Path -LiteralPath $shaRollback) {
        Remove-Item -Force -LiteralPath $shaRollback
    }
    $shaPublished = $false

    [ordered]@{
        status = "deployed"
        app = $appRoot
        files = $deployedInfo.files
        bytes = $deployedInfo.bytes
        contents_sha256 = $deployedInfo.manifest_sha256
        user_data_preserved = $true
    } | ConvertTo-Json -Compress
}
catch {
    $failure = $_
    try {
        if ($newPublished -and (Test-Path -LiteralPath $appRoot)) {
            Remove-OwnedDirectory -Parent $localRoot -Path $appRoot -Label "failed deployed application"
            $newPublished = $false
        }
        if ($oldMoved -and (Test-Path -LiteralPath $rollback)) {
            [IO.Directory]::Move($rollback, $appRoot)
            $oldMoved = $false
        }
        if ($shaPublished -and (Test-Path -LiteralPath $shaRollback)) {
            Remove-Item -Force -LiteralPath (Join-Path $localRoot "SHA256SUMS.txt")
            Move-Item -LiteralPath $shaRollback -Destination (
                Join-Path $localRoot "SHA256SUMS.txt"
            )
            $shaPublished = $false
        }
        if (Test-Path -LiteralPath $stage) {
            Remove-OwnedDirectory -Parent $localRoot -Path $stage -Label "deployment stage"
        }
        foreach ($temporary in @($shaStage, $shaRollback)) {
            if (Test-Path -LiteralPath $temporary) {
                Remove-Item -Force -LiteralPath $temporary
            }
        }
    }
    catch {
        throw "local deployment failed and rollback was incomplete: $($failure.Exception.Message); $($_.Exception.Message)"
    }
    throw $failure
}
