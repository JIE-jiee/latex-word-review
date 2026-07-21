#requires -Version 5.1

[CmdletBinding()]
param(
    [string]$Python = "",
    [string]$Iscc = "",
    [string]$OutputRoot = "",
    [ValidatePattern('^[0-9]+$')]
    [string]$SourceDateEpoch = "1784160000",
    [ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+$')]
    [string]$ExpectedPyInstallerVersion = "6.21.0",
    [ValidateRange(30, 1800)]
    [int]$InnoTimeoutSeconds = 300
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

function Assert-NoReparsePoint {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
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

function Reset-OwnedDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$ReleaseRoot,
        [Parameter(Mandatory = $true)][string]$Path
    )

    Assert-ContainedPath -Root $ReleaseRoot -Candidate $Path -Label "owned build directory"
    if (Test-Path -LiteralPath $Path) {
        Assert-NoReparsePoint -Path $Path
        Remove-Item -Force -Recurse -LiteralPath $Path
    }
    $null = New-Item -ItemType Directory -Path $Path
}

function Resolve-ExistingFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$BasePath,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $full = Get-FullPath -Path $Path -BasePath $BasePath
    if (-not (Test-Path -LiteralPath $full -PathType Leaf)) {
        throw "$Label was not found: $full"
    }
    $item = Get-Item -Force -LiteralPath $full
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label must not be a reparse point: $full"
    }
    return $item.FullName
}

function Resolve-Iscc {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Requested,
        [Parameter(Mandatory = $true)][string]$RepoRoot
    )

    if (-not [string]::IsNullOrWhiteSpace($Requested)) {
        return Resolve-ExistingFile -Path $Requested -BasePath $RepoRoot -Label "Inno Setup compiler"
    }

    $candidates = @(
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 7\ISCC.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 7\ISCC.exe")
    )
    foreach ($candidate in $candidates) {
        if (-not [string]::IsNullOrWhiteSpace($candidate) -and
            (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return Resolve-ExistingFile -Path $candidate -BasePath $RepoRoot -Label "Inno Setup compiler"
        }
    }

    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return Resolve-ExistingFile -Path $command.Source -BasePath $RepoRoot -Label "Inno Setup compiler"
    }
    throw "Inno Setup 7 ISCC.exe is required; this offline build script will not install it"
}

function Get-RelativeFilePath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path
    )

    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $pathFull = [IO.Path]::GetFullPath($Path)
    Assert-ContainedPath -Root $rootFull -Candidate $pathFull -Label "release file"
    return $pathFull.Substring($rootFull.Length).TrimStart('\', '/').Replace('\', '/')
}

function Write-Utf8NoBomLines {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string[]]$Lines
    )

    $encoding = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllLines($Path, $Lines, $encoding)
}

function Assert-LastExitCode {
    param([Parameter(Mandatory = $true)][string]$Label)

    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

function New-PortableArchive {
    param(
        [Parameter(Mandatory = $true)][string]$SourceDirectory,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$Epoch
    )

    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $timestamp = [DateTimeOffset]::FromUnixTimeSeconds([long]$Epoch)
    $files = Get-ChildItem -Force -File -Recurse -LiteralPath $SourceDirectory | Sort-Object {
        (Get-RelativeFilePath -Root $SourceDirectory -Path $_.FullName).ToLowerInvariant()
    }
    $fileStream = [IO.File]::Open($Destination, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
    try {
        $archive = [IO.Compression.ZipArchive]::new(
            $fileStream,
            [IO.Compression.ZipArchiveMode]::Create,
            $false,
            [Text.Encoding]::UTF8
        )
        try {
            foreach ($file in $files) {
                $relative = Get-RelativeFilePath -Root $SourceDirectory -Path $file.FullName
                $entry = $archive.CreateEntry(
                    "latex-word-review/$relative",
                    [IO.Compression.CompressionLevel]::Optimal
                )
                $entry.LastWriteTime = $timestamp
                $sourceStream = [IO.File]::OpenRead($file.FullName)
                $entryStream = $entry.Open()
                try {
                    $sourceStream.CopyTo($entryStream)
                }
                finally {
                    $entryStream.Dispose()
                    $sourceStream.Dispose()
                }
            }
        }
        finally {
            $archive.Dispose()
        }
    }
    finally {
        $fileStream.Dispose()
    }
}

if ($env:OS -ne "Windows_NT") {
    throw "Windows is the only supported build host"
}

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$buildRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot "build"))
if (Test-Path -LiteralPath $buildRoot) {
    $buildItem = Get-Item -Force -LiteralPath $buildRoot
    if (-not $buildItem.PSIsContainer -or
        ($buildItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "repository build root must be a real directory"
    }
}
else {
    $null = New-Item -ItemType Directory -Path $buildRoot
}
if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $OutputRoot = Join-Path $buildRoot "windows-release"
}
$releaseRoot = Get-FullPath -Path $OutputRoot -BasePath $repoRoot
Assert-ContainedPath -Root $buildRoot -Candidate $releaseRoot -Label "release output"
if (-not [IO.Path]::GetDirectoryName($releaseRoot).Equals(
    $buildRoot,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw "release output must be one direct child of the repository build directory"
}

$markerName = ".latex-word-review-build-root"
$markerToken = "latex-word-review/windows-release/v1"
if (Test-Path -LiteralPath $releaseRoot) {
    Assert-NoReparsePoint -Path $releaseRoot
    $marker = Join-Path $releaseRoot $markerName
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf) -or
        (Get-Content -Raw -Encoding utf8 -LiteralPath $marker).Trim() -ne $markerToken) {
        throw "refusing to reuse an unowned release directory: $releaseRoot"
    }
    $allowed = @($markerName, "artifacts", "dist", "work")
    $unexpected = Get-ChildItem -Force -LiteralPath $releaseRoot | Where-Object {
        $allowed -notcontains $_.Name
    } | Select-Object -First 1
    if ($null -ne $unexpected) {
        throw "owned release directory contains an unexpected entry: $($unexpected.Name)"
    }
}
else {
    $null = New-Item -ItemType Directory -Path $releaseRoot
    [IO.File]::WriteAllText(
        (Join-Path $releaseRoot $markerName),
        $markerToken + [Environment]::NewLine,
        (New-Object Text.UTF8Encoding($false))
    )
}

$distRoot = Join-Path $releaseRoot "dist"
$workRoot = Join-Path $releaseRoot "work"
$artifactRoot = Join-Path $releaseRoot "artifacts"

if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = Join-Path $repoRoot ".venv\Scripts\python.exe"
}
$pythonExe = Resolve-ExistingFile -Path $Python -BasePath $repoRoot -Label "Python"
$isccExe = Resolve-Iscc -Requested $Iscc -RepoRoot $repoRoot
$isccProbeInfo = New-Object Diagnostics.ProcessStartInfo
$isccProbeInfo.FileName = $isccExe
$isccProbeInfo.Arguments = "/?"
$isccProbeInfo.UseShellExecute = $false
$isccProbeInfo.CreateNoWindow = $true
$isccProbeInfo.RedirectStandardOutput = $true
$isccProbeInfo.RedirectStandardError = $true
$isccProbe = New-Object Diagnostics.Process
$isccProbe.StartInfo = $isccProbeInfo
if (-not $isccProbe.Start()) {
    throw "Inno Setup compiler probe could not be started"
}
$isccProbeOutput = $isccProbe.StandardOutput.ReadToEnd()
$isccProbeError = $isccProbe.StandardError.ReadToEnd()
$isccProbe.WaitForExit()
$isccBanner = ($isccProbeOutput + [Environment]::NewLine + $isccProbeError).Trim()
$isccVersionMatch = [regex]::Match(
    $isccBanner,
    '(?m)^Inno Setup (?<major>[0-9]+) Command-Line Compiler\r?$'
)
if (-not $isccVersionMatch.Success) {
    throw "Inno Setup compiler banner could not be verified"
}
$isccMajor = [int]$isccVersionMatch.Groups["major"].Value
if ($isccMajor -ne 7) {
    throw "Inno Setup major version 7 is required; found $isccMajor"
}

$versionSource = Join-Path $repoRoot "src\latex_word_review\__about__.py"
$versionText = Get-Content -Raw -Encoding utf8 -LiteralPath $versionSource
$versionMatch = [regex]::Match($versionText, '__version__\s*=\s*"([0-9A-Za-z.+-]+)"')
if (-not $versionMatch.Success) {
    throw "project version could not be read without executing project code"
}
$version = $versionMatch.Groups[1].Value

$installedProjectVersion = ((& $pythonExe -I -c (
    "import importlib.metadata as m;print(m.version('latex-word-review'))"
)) | Out-String).Trim()
Assert-LastExitCode -Label "installed project metadata probe"
if ($installedProjectVersion -ne $version) {
    throw (
        "build Python contains latex-word-review $installedProjectVersion metadata; " +
        "refresh it to exact source version $version before building"
    )
}

$readmePath = Join-Path $repoRoot "README.md"
$installedReadmeMatches = ((& $pythonExe -I -c (
    "import importlib.metadata as m,pathlib,sys;" +
    "normalize=lambda value:value.replace(chr(13)+chr(10),chr(10)).strip();" +
    "print(normalize(m.metadata('latex-word-review').get_payload())==" +
    "normalize(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')))"
) $readmePath) | Out-String).Trim()
Assert-LastExitCode -Label "installed project README metadata probe"
if ($installedReadmeMatches -ne "True") {
    throw (
        "build Python contains stale latex-word-review README metadata; " +
        "refresh the installed workspace package before building"
    )
}

$runtimeProbeText = ((& $pythonExe -I -c (
    "import json,struct,sys;print(json.dumps({" +
    "'platform':sys.platform,'implementation':sys.implementation.name," +
    "'major':sys.version_info.major,'minor':sys.version_info.minor," +
    "'micro':sys.version_info.micro,'bits':struct.calcsize('P')*8}))"
)) | Out-String).Trim()
Assert-LastExitCode -Label "Python runtime probe"
$runtimeProbe = $runtimeProbeText | ConvertFrom-Json
if ($runtimeProbe.platform -ne "win32" -or $runtimeProbe.implementation -ne "cpython" -or
    $runtimeProbe.major -ne 3 -or $runtimeProbe.minor -ne 12 -or
    $runtimeProbe.micro -ne 13 -or $runtimeProbe.bits -ne 64) {
    throw "the supported frozen application requires exact 64-bit CPython 3.12.13 on Windows"
}

$pyInstallerVersion = ((& $pythonExe -I -c (
    "import PyInstaller;print(PyInstaller.__version__)"
)) | Out-String).Trim()
Assert-LastExitCode -Label "PyInstaller probe"
if ($pyInstallerVersion -ne $ExpectedPyInstallerVersion) {
    throw "PyInstaller $ExpectedPyInstallerVersion is required; found $pyInstallerVersion"
}

# Do not invalidate a previous verified candidate until every required local
# build tool and its pinned version has passed the offline preflight.
Reset-OwnedDirectory -ReleaseRoot $releaseRoot -Path $distRoot
Reset-OwnedDirectory -ReleaseRoot $releaseRoot -Path $workRoot
Reset-OwnedDirectory -ReleaseRoot $releaseRoot -Path $artifactRoot

$savedEnvironment = @{}
foreach ($name in @("PYTHONHASHSEED", "PYTHONNOUSERSITE", "SOURCE_DATE_EPOCH")) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}
try {
    [Environment]::SetEnvironmentVariable("PYTHONHASHSEED", "0", "Process")
    [Environment]::SetEnvironmentVariable("PYTHONNOUSERSITE", "1", "Process")
    [Environment]::SetEnvironmentVariable("SOURCE_DATE_EPOCH", $SourceDateEpoch, "Process")
    Push-Location $repoRoot
    try {
        $spec = Join-Path $repoRoot "packaging\latex-word-review.spec"
        & $pythonExe -I -m PyInstaller --noconfirm --clean --log-level WARN `
            --distpath $distRoot --workpath $workRoot $spec
        Assert-LastExitCode -Label "PyInstaller onedir build"
    }
    finally {
        Pop-Location
    }
}
finally {
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], "Process")
    }
}

$onedir = Join-Path $distRoot "latex-word-review"
if (-not (Test-Path -LiteralPath $onedir -PathType Container)) {
    throw "PyInstaller did not create the expected onedir"
}
Assert-NoReparsePoint -Path $onedir

$contentManifest = Join-Path $onedir "CONTENTS.sha256"
$contentLines = @(
    Get-ChildItem -Force -File -Recurse -LiteralPath $onedir | Where-Object {
        $_.FullName -ne $contentManifest
    } | Sort-Object {
        (Get-RelativeFilePath -Root $onedir -Path $_.FullName).ToLowerInvariant()
    } | ForEach-Object {
        $relative = Get-RelativeFilePath -Root $onedir -Path $_.FullName
        $digest = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLowerInvariant()
        "$digest *$relative"
    }
)
Write-Utf8NoBomLines -Path $contentManifest -Lines $contentLines

$portable = Join-Path $artifactRoot "latex-word-review-$version-windows-x64-portable.zip"
New-PortableArchive -SourceDirectory $onedir -Destination $portable -Epoch $SourceDateEpoch

$installerRecipe = Join-Path $repoRoot "installer\latex-word-review.iss"
$isccArguments = @(
    "/DAppVersion=$version",
    "/DSourceDir=$onedir",
    "/DOutputDir=$artifactRoot",
    $installerRecipe
)
$quotedIsccArguments = @(
    foreach ($argument in $isccArguments) {
        if ($argument.IndexOfAny([char[]]@([char]0, [char]10, [char]13, [char]34)) -ge 0) {
            throw "Inno Setup argument contains a forbidden control or quote character"
        }
        '"' + $argument + '"'
    }
)
$isccOutput = Join-Path $workRoot "iscc.stdout.log"
$isccError = Join-Path $workRoot "iscc.stderr.log"
$isccBuild = Start-Process -FilePath $isccExe `
    -ArgumentList $quotedIsccArguments `
    -WindowStyle Hidden `
    -PassThru `
    -RedirectStandardOutput $isccOutput `
    -RedirectStandardError $isccError
# Windows PowerShell 5.1 must cache the handle before a redirected process exits;
# otherwise ExitCode can remain $null after a manual timeout-aware WaitForExit.
$isccHandle = $isccBuild.Handle
if ($isccHandle -eq [IntPtr]::Zero) {
    throw "Inno Setup compiler process handle is unavailable"
}
$isccStarted = $isccBuild.StartTime
if (-not $isccBuild.WaitForExit($InnoTimeoutSeconds * 1000)) {
    $owned = Get-Process -Id $isccBuild.Id -ErrorAction SilentlyContinue
    if ($null -ne $owned -and $owned.StartTime -eq $isccStarted -and $owned.Path -eq $isccExe) {
        Stop-Process -Id $owned.Id -Force
        Wait-Process -Id $owned.Id -Timeout 10 -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $isccOutput) {
        Get-Content -Tail 40 -LiteralPath $isccOutput
    }
    if (Test-Path -LiteralPath $isccError) {
        Get-Content -Tail 80 -LiteralPath $isccError | Write-Error
    }
    throw "Inno Setup compiler exceeded the $InnoTimeoutSeconds second build timeout"
}
$isccBuild.WaitForExit()
if (Test-Path -LiteralPath $isccOutput) {
    Get-Content -Tail 40 -LiteralPath $isccOutput
}
if (Test-Path -LiteralPath $isccError) {
    Get-Content -Tail 80 -LiteralPath $isccError | Write-Error
}
if ($isccBuild.ExitCode -ne 0) {
    throw "Inno Setup compiler failed with exit code $($isccBuild.ExitCode)"
}

$setup = Join-Path $artifactRoot "latex-word-review-$version-windows-x64-setup.exe"
if (-not (Test-Path -LiteralPath $setup -PathType Leaf)) {
    throw "Inno Setup did not create the expected installer"
}

$artifactHashes = @(
    "$((Get-FileHash -Algorithm SHA256 -LiteralPath $portable).Hash.ToLowerInvariant()) *$([IO.Path]::GetFileName($portable))",
    "$((Get-FileHash -Algorithm SHA256 -LiteralPath $setup).Hash.ToLowerInvariant()) *$([IO.Path]::GetFileName($setup))"
)
Write-Utf8NoBomLines -Path (Join-Path $artifactRoot "SHA256SUMS.txt") -Lines $artifactHashes

$verifyScript = Join-Path $repoRoot "scripts\verify-windows-release.ps1"
& $verifyScript -ReleaseRoot $releaseRoot -Version $version

Assert-NoReparsePoint -Path $workRoot
Remove-Item -Force -Recurse -LiteralPath $workRoot

[ordered]@{
    status = "verified"
    version = $version
    python = "$($runtimeProbe.major).$($runtimeProbe.minor).$($runtimeProbe.micro)"
    pyinstaller = $pyInstallerVersion
    onedir = $onedir
    portable = $portable
    installer = $setup
    sha256sums = (Join-Path $artifactRoot "SHA256SUMS.txt")
} | ConvertTo-Json -Compress
