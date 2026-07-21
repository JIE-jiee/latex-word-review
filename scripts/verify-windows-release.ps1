#requires -Version 5.1

[CmdletBinding()]
param(
    [string]$ReleaseRoot = "",
    [string]$Version = "",
    [switch]$SkipExecutableSmokeTest
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

function Assert-NoReparsePoint {
    param([Parameter(Mandatory = $true)][string]$Path)

    $item = Get-Item -Force -LiteralPath $Path
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "release path is a reparse point: $Path"
    }
    if ($item.PSIsContainer) {
        $linked = Get-ChildItem -Force -Recurse -LiteralPath $Path | Where-Object {
            ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
        } | Select-Object -First 1
        if ($null -ne $linked) {
            throw "release tree contains a reparse point: $($linked.FullName)"
        }
    }
}

function Get-RelativeFilePath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path
    )

    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $pathFull = [IO.Path]::GetFullPath($Path)
    $prefix = $rootFull + [IO.Path]::DirectorySeparatorChar
    if (-not $pathFull.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "release file escapes its root: $pathFull"
    }
    return $pathFull.Substring($rootFull.Length).TrimStart('\', '/').Replace('\', '/')
}

function Assert-NormalizedRelativePath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if ([string]::IsNullOrWhiteSpace($Path) -or $Path.Contains('\') -or
        $Path.StartsWith('/') -or $Path.Contains(':')) {
        throw "$Label is not a normalized relative path: $Path"
    }
    $segments = $Path.Split('/')
    if ($segments -contains "" -or $segments -contains "." -or $segments -contains "..") {
        throw "$Label contains an unsafe segment: $Path"
    }
}

function Get-Sha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = $null
    $sha256 = $null
    try {
        $stream = [IO.File]::Open(
            $Path,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::Read
        )
        $sha256 = [Security.Cryptography.SHA256]::Create()
        $digest = $sha256.ComputeHash($stream)
        return [BitConverter]::ToString($digest).Replace("-", "").ToLowerInvariant()
    }
    finally {
        if ($null -ne $sha256) {
            $sha256.Dispose()
        }
        if ($null -ne $stream) {
            $stream.Dispose()
        }
    }
}

function Assert-ExactJsonProperties {
    param(
        [Parameter(Mandatory = $true)][object]$Value,
        [Parameter(Mandatory = $true)][string[]]$Expected,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if ($null -eq $Value) {
        throw "$Label is missing"
    }
    $actual = @($Value.PSObject.Properties.Name | Sort-Object)
    $required = @($Expected | Sort-Object)
    $difference = @(Compare-Object -CaseSensitive -ReferenceObject $required -DifferenceObject $actual)
    if ($actual.Count -ne $required.Count -or $difference.Count -ne 0) {
        throw "$Label has an invalid shape"
    }
}

function Read-AuthoritativeSchemaCatalog {
    param([Parameter(Mandatory = $true)][string]$SchemaRoot)

    $catalogPath = Join-Path $SchemaRoot "catalog.json"
    if (-not (Test-Path -LiteralPath $catalogPath -PathType Leaf) -or
        (Get-Item -LiteralPath $catalogPath).Length -gt 65536) {
        throw "authoritative schema catalog is missing or oversized"
    }
    try {
        $catalog = Get-Content -Raw -Encoding utf8 -LiteralPath $catalogPath | ConvertFrom-Json
    }
    catch {
        throw "authoritative schema catalog is not valid JSON"
    }
    Assert-ExactJsonProperties -Value $catalog `
        -Expected @("catalog_format", "schema_version", "common", "objects") `
        -Label "schema catalog"
    if ($catalog.catalog_format -isnot [long] -and $catalog.catalog_format -isnot [int]) {
        throw "schema catalog format is invalid"
    }
    if ([int64]$catalog.catalog_format -ne 1 -or
        $catalog.schema_version -cne "1.0.0-alpha.1") {
        throw "schema catalog format or version is unsupported"
    }
    Assert-ExactJsonProperties -Value $catalog.common `
        -Expected @("file", "sha256") -Label "schema catalog common entry"
    $objects = @($catalog.objects)
    if ($objects.Count -lt 1 -or $objects.Count -gt 64) {
        throw "schema catalog object count is invalid"
    }

    $entries = @{}
    $objectNames = @{}
    $catalogEntries = @(@{ Value = $catalog.common; Common = $true })
    foreach ($objectEntry in $objects) {
        $catalogEntries += @{ Value = $objectEntry; Common = $false }
    }
    foreach ($wrapped in $catalogEntries) {
        $entry = $wrapped.Value
        $isCommon = [bool]$wrapped.Common
        if ($isCommon) {
            Assert-ExactJsonProperties -Value $entry -Expected @("file", "sha256") `
                -Label "schema catalog common entry"
        }
        else {
            Assert-ExactJsonProperties -Value $entry -Expected @("name", "file", "sha256") `
                -Label "schema catalog object entry"
            if ($entry.name -isnot [string] -or
                $entry.name -cnotmatch '^[A-Z][A-Za-z0-9]{0,63}$' -or
                $objectNames.ContainsKey([string]$entry.name)) {
                throw "schema catalog object identity is invalid or duplicated"
            }
            $objectNames[[string]$entry.name] = $true
        }
        if ($entry.file -isnot [string] -or
            $entry.file -cnotmatch '^[a-z0-9]+(?:-[a-z0-9]+)*\.schema\.json$' -or
            $entry.sha256 -isnot [string] -or $entry.sha256 -cnotmatch '^[0-9a-f]{64}$' -or
            $entries.ContainsKey([string]$entry.file)) {
            throw "schema catalog file identity or SHA-256 is invalid or duplicated"
        }
        $entries[[string]$entry.file] = [string]$entry.sha256
    }

    $requiredFiles = @("catalog.json") + @($entries.Keys | Sort-Object)
    $actualFiles = @(
        Get-ChildItem -File -Recurse -Filter "*.json" -LiteralPath $SchemaRoot | ForEach-Object {
            if (($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "authoritative schema set contains a reparse point"
            }
            Get-RelativeFilePath -Root $SchemaRoot -Path $_.FullName
        } | Sort-Object
    )
    $schemaDifference = @(
        Compare-Object -CaseSensitive -ReferenceObject $requiredFiles -DifferenceObject $actualFiles
    )
    if ($actualFiles.Count -ne $requiredFiles.Count -or $schemaDifference.Count -ne 0) {
        throw "authoritative schema set differs from its catalog"
    }
    foreach ($filename in $entries.Keys) {
        if ((Get-Sha256 -Path (Join-Path $SchemaRoot $filename)) -cne $entries[$filename]) {
            throw "authoritative schema hash differs from catalog: $filename"
        }
    }
    return $entries
}

function Get-StreamSha256 {
    param([Parameter(Mandatory = $true)][IO.Stream]$Stream)

    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $sha.ComputeHash($Stream)
        return [BitConverter]::ToString($bytes).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Assert-PeExecutable {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label is missing: $Path"
    }
    $item = Get-Item -Force -LiteralPath $Path
    if ($item.Length -lt 1024) {
        throw "$Label is unexpectedly small"
    }
    $stream = [IO.File]::OpenRead($Path)
    try {
        if ($stream.ReadByte() -ne 0x4D -or $stream.ReadByte() -ne 0x5A) {
            throw "$Label does not have a PE MZ header"
        }
    }
    finally {
        $stream.Dispose()
    }
}

function Read-StrictHashFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $result = @{}
    $lines = @(Get-Content -Encoding utf8 -LiteralPath $Path)
    if ($lines.Count -eq 0) {
        throw "$Label is empty"
    }
    foreach ($line in $lines) {
        $match = [regex]::Match($line, '^([0-9a-f]{64}) \*(.+)$')
        if (-not $match.Success) {
            throw "$Label contains a malformed line"
        }
        $relative = $match.Groups[2].Value
        Assert-NormalizedRelativePath -Path $relative -Label $Label
        if ($result.ContainsKey($relative)) {
            throw "$Label contains a duplicate path: $relative"
        }
        $result[$relative] = $match.Groups[1].Value
    }
    return $result
}

function Invoke-ExecutableVersionSmoke {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion
    )

    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $Executable
    $start.Arguments = "--version"
    $start.WorkingDirectory = $WorkingDirectory
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $start
    if (-not $process.Start()) {
        throw "frozen executable did not start"
    }
    try {
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(15000)) {
            $process.Kill()
            throw "frozen executable version probe timed out"
        }
        $stdout = $stdoutTask.Result
        $stderr = $stderrTask.Result
        if ($stdout.Length -gt 8192 -or $stderr.Length -gt 8192) {
            throw "frozen executable version probe exceeded its output bound"
        }
        if ($process.ExitCode -ne 0 -or -not $stdout.Contains($ExpectedVersion)) {
            throw "frozen executable version probe failed"
        }
    }
    finally {
        $process.Dispose()
    }
}

if ($env:OS -ne "Windows_NT") {
    throw "Windows is the only supported verification host"
}

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($ReleaseRoot)) {
    $ReleaseRoot = Join-Path $repoRoot "build\windows-release"
}
$releaseRootFull = Get-FullPath -Path $ReleaseRoot -BasePath $repoRoot
if (-not (Test-Path -LiteralPath $releaseRootFull -PathType Container)) {
    throw "release root is missing: $releaseRootFull"
}
Assert-NoReparsePoint -Path $releaseRootFull

if ([string]::IsNullOrWhiteSpace($Version)) {
    $versionSource = Join-Path $repoRoot "src\latex_word_review\__about__.py"
    $versionText = Get-Content -Raw -Encoding utf8 -LiteralPath $versionSource
    $versionMatch = [regex]::Match($versionText, '__version__\s*=\s*"([0-9A-Za-z.+-]+)"')
    if (-not $versionMatch.Success) {
        throw "project version could not be read without executing project code"
    }
    $Version = $versionMatch.Groups[1].Value
}
if ($Version -notmatch '^[0-9A-Za-z.+-]+$') {
    throw "release version is unsafe"
}

$onedir = Join-Path $releaseRootFull "dist\latex-word-review"
$artifactRoot = Join-Path $releaseRootFull "artifacts"
$executable = Join-Path $onedir "latex-word-review.exe"
$guiExecutable = Join-Path $onedir "LatexWordReview.exe"
$portableName = "latex-word-review-$Version-windows-x64-portable.zip"
$setupName = "latex-word-review-$Version-windows-x64-setup.exe"
$portable = Join-Path $artifactRoot $portableName
$setup = Join-Path $artifactRoot $setupName
$contentManifest = Join-Path $onedir "CONTENTS.sha256"
$artifactManifest = Join-Path $artifactRoot "SHA256SUMS.txt"

foreach ($directory in @($onedir, $artifactRoot)) {
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        throw "required release directory is missing: $directory"
    }
}
Assert-PeExecutable -Path $executable -Label "frozen CLI"
Assert-PeExecutable -Path $guiExecutable -Label "frozen GUI"
Assert-PeExecutable -Path $setup -Label "Inno Setup installer"
if (-not (Test-Path -LiteralPath $portable -PathType Leaf)) {
    throw "portable ZIP is missing"
}
if (-not (Test-Path -LiteralPath $contentManifest -PathType Leaf) -or
    -not (Test-Path -LiteralPath $artifactManifest -PathType Leaf)) {
    throw "release hash manifest is missing"
}

$requiredData = @(
    (Join-Path $onedir "_internal\LICENSE"),
    (Join-Path $onedir "_internal\LICENSE.txt"),
    (Join-Path $onedir "_internal\THIRD_PARTY_NOTICES.md"),
    (Join-Path $onedir "_internal\licenses\cpython-3.12.13\cpython-3.12.13-license.rst"),
    (Join-Path $onedir "_internal\libcrypto-3-x64.dll"),
    (Join-Path $onedir "_internal\libffi-8.dll"),
    (Join-Path $onedir "_internal\libssl-3-x64.dll"),
    (Join-Path $onedir "_internal\latex_word_review\py.typed"),
    (Join-Path $onedir "_internal\latex_word_review\assets\app.css"),
    (Join-Path $onedir "_internal\latex_word_review\assets\finalize_review_fields.ps1"),
    (Join-Path $onedir "_internal\pypdfium2_raw\pdfium.dll"),
    (Join-Path $onedir "_internal\pypdfium2_raw\version.json")
)
foreach ($path in $requiredData) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "required bundled data is missing: $path"
    }
}
$cpythonLicenseBundle = Join-Path $onedir (
    "_internal\licenses\cpython-3.12.13\cpython-3.12.13-license.rst"
)
$expectedCpythonLicenseSha256 = (
    "341832873fd316a37927e79385093fbbfd40a467428480835fe435a80cadf4e5"
)
if ((Get-Sha256 -Path $cpythonLicenseBundle) -ne $expectedCpythonLicenseSha256) {
    throw "the pinned CPython incorporated-software license bundle was modified"
}
$schemaRoot = Join-Path $onedir "_internal\latex_word_review\schemas"
$assetRoot = Join-Path $onedir "_internal\latex_word_review\assets"
if (-not (Test-Path -LiteralPath $schemaRoot -PathType Container)) {
    throw "bundled JSON Schema directory is missing"
}
$sourceSchemaRoot = Join-Path $repoRoot "src\latex_word_review\schemas\v1alpha"
$bundledSchemaVersionRoot = Join-Path $schemaRoot "v1alpha"
if (-not (Test-Path -LiteralPath $sourceSchemaRoot -PathType Container) -or
    -not (Test-Path -LiteralPath $bundledSchemaVersionRoot -PathType Container)) {
    throw "source or bundled JSON Schema version directory is missing"
}
$schemaEntries = Read-AuthoritativeSchemaCatalog -SchemaRoot $sourceSchemaRoot
$sourceSchemaCatalog = Join-Path $sourceSchemaRoot "catalog.json"
$bundledSchemaCatalog = Join-Path $bundledSchemaVersionRoot "catalog.json"
if (-not (Test-Path -LiteralPath $bundledSchemaCatalog -PathType Leaf) -or
    (Get-Sha256 -Path $bundledSchemaCatalog) -cne (Get-Sha256 -Path $sourceSchemaCatalog)) {
    throw "bundled schema catalog differs from the authoritative source catalog"
}
$requiredSchemas = @("catalog.json") + @($schemaEntries.Keys | Sort-Object)
$actualSchemas = @(
    Get-ChildItem -File -Recurse -Filter "*.json" -LiteralPath $bundledSchemaVersionRoot |
        ForEach-Object {
            Get-RelativeFilePath -Root $bundledSchemaVersionRoot -Path $_.FullName
        } | Sort-Object
)
$schemaDifference = @(
    Compare-Object -CaseSensitive -ReferenceObject $requiredSchemas -DifferenceObject $actualSchemas
)
if ($actualSchemas.Count -ne $requiredSchemas.Count -or $schemaDifference.Count -ne 0) {
    throw "bundled schema payload set differs from the authoritative catalog"
}
foreach ($schemaFilename in $schemaEntries.Keys) {
    $bundledSchemaPath = Join-Path $bundledSchemaVersionRoot $schemaFilename
    if ((Get-Sha256 -Path $bundledSchemaPath) -cne $schemaEntries[$schemaFilename]) {
        throw "bundled schema hash differs from catalog: $schemaFilename"
    }
}
if (-not (Test-Path -LiteralPath $assetRoot -PathType Container)) {
    throw "bundled application assets are missing"
}
$requiredAssets = @(
    "app.css",
    "finalize_review_fields.ps1"
)
$actualAssets = @(
    Get-ChildItem -File -Recurse -LiteralPath $assetRoot | ForEach-Object {
        Get-RelativeFilePath -Root $assetRoot -Path $_.FullName
    } | Sort-Object
)
$assetDifference = @(
    Compare-Object -CaseSensitive -ReferenceObject $requiredAssets -DifferenceObject $actualAssets
)
if ($actualAssets.Count -ne $requiredAssets.Count -or $assetDifference.Count -ne 0) {
    throw "bundled application assets differ from the required exact set"
}
$metadataPatterns = @(
    "attrs-*.dist-info",
    "jsonschema-*.dist-info",
    "jsonschema_specifications-*.dist-info",
    "latex_word_review-*.dist-info",
    "lxml-*.dist-info",
    "pillow-*.dist-info",
    "pylatexenc-*.dist-info",
    "pypdfium2-*.dist-info",
    "referencing-*.dist-info",
    "regex-*.dist-info",
    "rfc8785-*.dist-info",
    "rpds_py-*.dist-info",
    "tex2word-*.dist-info",
    "typing_extensions-*.dist-info"
)
foreach ($pattern in $metadataPatterns) {
    $matches = @(
        Get-ChildItem -Directory -Filter $pattern -LiteralPath (Join-Path $onedir "_internal")
    )
    if ($matches.Count -ne 1 -or
        -not (Test-Path -LiteralPath (Join-Path $matches[0].FullName "METADATA") -PathType Leaf)) {
        throw "required frozen distribution metadata is missing or ambiguous: $pattern"
    }
}
$projectMetadataDirectory = @(
    Get-ChildItem -Directory -Filter "latex_word_review-*.dist-info" -LiteralPath (
        Join-Path $onedir "_internal"
    )
)
if ($projectMetadataDirectory.Count -ne 1 -or
    $projectMetadataDirectory[0].Name -ne "latex_word_review-$Version.dist-info") {
    throw "frozen project metadata directory does not match release version"
}
$projectMetadataText = Get-Content -Raw -Encoding utf8 -LiteralPath (
    Join-Path $projectMetadataDirectory[0].FullName "METADATA"
)
$metadataNameMatches = [regex]::IsMatch(
    $projectMetadataText,
    '(?m)^Name: latex-word-review\r?$'
)
$metadataVersionMatches = [regex]::IsMatch(
    $projectMetadataText,
    "(?m)^Version: $([regex]::Escape($Version))\r?$"
)
if (-not $metadataNameMatches -or -not $metadataVersionMatches) {
    throw "frozen project metadata name or version does not match release version"
}

$licenseRequirements = @(
    @{ Pattern = "attrs-*.dist-info"; Paths = @("licenses\LICENSE") },
    @{ Pattern = "jsonschema-*.dist-info"; Paths = @("licenses\COPYING") },
    @{ Pattern = "jsonschema_specifications-*.dist-info"; Paths = @("licenses\COPYING") },
    @{ Pattern = "latex_word_review-*.dist-info"; Paths = @("licenses\LICENSE") },
    @{ Pattern = "lxml-*.dist-info"; Paths = @(
        "licenses\LICENSE.txt", "licenses\LICENSES.txt"
    ) },
    @{ Pattern = "pillow-*.dist-info"; Paths = @("licenses\LICENSE") },
    @{ Pattern = "pylatexenc-*.dist-info"; Paths = @("licenses\LICENSE.txt") },
    @{ Pattern = "pypdfium2-*.dist-info"; Paths = @(
        "licenses\LICENSES\Apache-2.0.txt",
        "licenses\LICENSES\BSD-3-Clause.txt",
        "licenses\LICENSES\CC-BY-4.0.txt",
        "licenses\data\windows_x64\BUILD_LICENSES\abseil.txt",
        "licenses\data\windows_x64\BUILD_LICENSES\agg23.txt",
        "licenses\data\windows_x64\BUILD_LICENSES\fast_float.txt",
        "licenses\data\windows_x64\BUILD_LICENSES\freetype.txt",
        "licenses\data\windows_x64\BUILD_LICENSES\icu.txt",
        "licenses\data\windows_x64\BUILD_LICENSES\lcms.txt",
        "licenses\data\windows_x64\BUILD_LICENSES\libjpeg_turbo.ijg",
        "licenses\data\windows_x64\BUILD_LICENSES\libjpeg_turbo.md",
        "licenses\data\windows_x64\BUILD_LICENSES\libopenjpeg.txt",
        "licenses\data\windows_x64\BUILD_LICENSES\libpng.txt",
        "licenses\data\windows_x64\BUILD_LICENSES\libtiff.txt",
        "licenses\data\windows_x64\BUILD_LICENSES\llvm-libc.txt",
        "licenses\data\windows_x64\BUILD_LICENSES\pdfium.txt",
        "licenses\data\windows_x64\BUILD_LICENSES\pdfium-binaries.txt",
        "licenses\data\windows_x64\BUILD_LICENSES\simdutf.txt",
        "licenses\data\windows_x64\BUILD_LICENSES\zlib.txt"
    ) },
    @{ Pattern = "referencing-*.dist-info"; Paths = @("licenses\COPYING") },
    @{ Pattern = "regex-*.dist-info"; Paths = @("licenses\LICENSE.txt") },
    @{ Pattern = "rfc8785-*.dist-info"; Paths = @("LICENSE") },
    @{ Pattern = "rpds_py-*.dist-info"; Paths = @("licenses\LICENSE") },
    @{ Pattern = "tex2word-*.dist-info"; Paths = @("licenses\LICENSE") },
    @{ Pattern = "typing_extensions-*.dist-info"; Paths = @("licenses\LICENSE") }
)
foreach ($requirement in $licenseRequirements) {
    $metadataDirectory = @(
        Get-ChildItem -Directory -Filter $requirement.Pattern -LiteralPath (Join-Path $onedir "_internal")
    )
    if ($metadataDirectory.Count -ne 1) {
        throw "license metadata directory is missing or ambiguous: $($requirement.Pattern)"
    }
    foreach ($relativeLicense in $requirement.Paths) {
        $licensePath = Join-Path $metadataDirectory[0].FullName $relativeLicense
        if (-not (Test-Path -LiteralPath $licensePath -PathType Leaf) -or
            (Get-Item -LiteralPath $licensePath).Length -eq 0) {
            throw "required frozen license evidence is missing: $($requirement.Pattern)/$relativeLicense"
        }
    }
}

$forbiddenExecutables = @(
    "initexmf.exe", "latex.exe", "latexdiff.exe", "miktex.exe", "mpm.exe",
    "pandoc.exe", "pdflatex.exe", "winword.exe", "xelatex.exe"
)
$forbiddenPythonComponents = @(
    "citeproc", "contourpy", "cycler", "dateutil", "fonttools", "kiwisolver",
    "latex2mathml", "matplotlib", "numpy", "playwright", "pyparsing", "scipy"
)
foreach ($file in Get-ChildItem -Force -File -Recurse -LiteralPath $onedir) {
    $relative = Get-RelativeFilePath -Root $onedir -Path $file.FullName
    $lowerRelative = $relative.ToLowerInvariant()
    $pathComponents = $lowerRelative.Split('/')
    if ($forbiddenExecutables -contains $file.Name.ToLowerInvariant() -or
        $file.Name.ToLowerInvariant().StartsWith("miktex") -or
        @($pathComponents | Where-Object {
            $component = $_
            @($forbiddenPythonComponents | Where-Object {
                $component -eq $_ -or
                $component.StartsWith($_ + "-") -or
                $component.StartsWith($_ + ".")
            }).Count -gt 0
        }).Count -gt 0) {
        throw "forbidden external application entered the bundle: $relative"
    }
}

$contentHashes = Read-StrictHashFile -Path $contentManifest -Label "CONTENTS.sha256"
$contentFiles = @(
    Get-ChildItem -Force -File -Recurse -LiteralPath $onedir | Where-Object {
        $_.FullName -ne $contentManifest
    }
)
if ($contentHashes.Count -ne $contentFiles.Count) {
    throw "CONTENTS.sha256 does not enumerate the complete onedir"
}
foreach ($file in $contentFiles) {
    $relative = Get-RelativeFilePath -Root $onedir -Path $file.FullName
    if (-not $contentHashes.ContainsKey($relative) -or
        $contentHashes[$relative] -ne (Get-Sha256 -Path $file.FullName)) {
        throw "onedir content hash mismatch: $relative"
    }
}

$artifactHashes = Read-StrictHashFile -Path $artifactManifest -Label "SHA256SUMS.txt"
if ($artifactHashes.Count -ne 2 -or -not $artifactHashes.ContainsKey($portableName) -or
    -not $artifactHashes.ContainsKey($setupName)) {
    throw "SHA256SUMS.txt must enumerate exactly the portable ZIP and installer"
}
if ($artifactHashes[$portableName] -ne (Get-Sha256 -Path $portable) -or
    $artifactHashes[$setupName] -ne (Get-Sha256 -Path $setup)) {
    throw "release artifact hash mismatch"
}

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$expectedFiles = @{}
foreach ($file in Get-ChildItem -Force -File -Recurse -LiteralPath $onedir) {
    $relative = Get-RelativeFilePath -Root $onedir -Path $file.FullName
    $expectedFiles[$relative] = $file.FullName
}
$seenEntries = @{}
$archive = [IO.Compression.ZipFile]::OpenRead($portable)
try {
    foreach ($entry in $archive.Entries) {
        if ([string]::IsNullOrEmpty($entry.Name)) {
            throw "portable ZIP contains a directory entry"
        }
        $prefix = "latex-word-review/"
        if (-not $entry.FullName.StartsWith($prefix, [StringComparison]::Ordinal)) {
            throw "portable ZIP entry is outside its single root"
        }
        $relative = $entry.FullName.Substring($prefix.Length)
        Assert-NormalizedRelativePath -Path $relative -Label "portable ZIP entry"
        if ($seenEntries.ContainsKey($relative)) {
            throw "portable ZIP contains a duplicate entry: $relative"
        }
        if (-not $expectedFiles.ContainsKey($relative)) {
            throw "portable ZIP contains an unexpected entry: $relative"
        }
        $source = Get-Item -Force -LiteralPath $expectedFiles[$relative]
        if ($entry.Length -ne $source.Length) {
            throw "portable ZIP size differs from onedir: $relative"
        }
        $entryStream = $entry.Open()
        try {
            $entryHash = Get-StreamSha256 -Stream $entryStream
        }
        finally {
            $entryStream.Dispose()
        }
        if ($entryHash -ne (Get-Sha256 -Path $source.FullName)) {
            throw "portable ZIP bytes differ from onedir: $relative"
        }
        $seenEntries[$relative] = $true
    }
}
finally {
    $archive.Dispose()
}
if ($seenEntries.Count -ne $expectedFiles.Count) {
    throw "portable ZIP omits files from the installer source onedir"
}

if (-not $SkipExecutableSmokeTest) {
    Invoke-ExecutableVersionSmoke -Executable $executable -WorkingDirectory $onedir `
        -ExpectedVersion $Version
}

[ordered]@{
    status = "verified"
    version = $Version
    onedir_file_count = $expectedFiles.Count
    executable_sha256 = (Get-Sha256 -Path $executable)
    gui_executable_sha256 = (Get-Sha256 -Path $guiExecutable)
    portable_sha256 = (Get-Sha256 -Path $portable)
    installer_sha256 = (Get-Sha256 -Path $setup)
    libcrypto_sha256 = (Get-Sha256 -Path (Join-Path $onedir "_internal\libcrypto-3-x64.dll"))
    libffi_sha256 = (Get-Sha256 -Path (Join-Path $onedir "_internal\libffi-8.dll"))
    libssl_sha256 = (Get-Sha256 -Path (Join-Path $onedir "_internal\libssl-3-x64.dll"))
    executable_smoke_test = (-not $SkipExecutableSmokeTest)
} | ConvertTo-Json -Compress
