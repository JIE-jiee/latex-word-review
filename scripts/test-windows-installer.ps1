#requires -Version 5.1

[CmdletBinding()]
param(
    [string]$ReleaseRoot = "build\windows-release",
    [string]$Version = ""
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

function Get-RelativeFilePath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path
    )

    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $pathFull = [IO.Path]::GetFullPath($Path)
    Assert-ContainedPath -Root $rootFull -Candidate $pathFull -Label "installer payload file"
    return $pathFull.Substring($rootFull.Length).TrimStart('\', '/')
}

function Invoke-HiddenProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "$Label failed with exit code $($process.ExitCode)"
    }
}

function Get-FreeLoopbackPort {
    $listener = New-Object Net.Sockets.TcpListener ([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    try {
        return ([Net.IPEndPoint]$listener.LocalEndpoint).Port
    }
    finally {
        $listener.Stop()
    }
}

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$buildRoot = Join-Path $repoRoot "build"
$releaseRootFull = Get-FullPath -Path $ReleaseRoot -BasePath $repoRoot
Assert-ContainedPath -Root $buildRoot -Candidate $releaseRootFull -Label "release root"

if ([string]::IsNullOrWhiteSpace($Version)) {
    $versionSource = Join-Path $repoRoot "src\latex_word_review\__about__.py"
    $versionText = Get-Content -Raw -Encoding utf8 -LiteralPath $versionSource
    $versionMatch = [regex]::Match($versionText, '__version__\s*=\s*"([0-9A-Za-z.+-]+)"')
    if (-not $versionMatch.Success) {
        throw "project version could not be read without executing project code"
    }
    $Version = $versionMatch.Groups[1].Value
}
if ($Version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+[0-9A-Za-z.+-]*$') {
    throw "version is invalid"
}

$onedir = Join-Path $releaseRootFull "dist\latex-word-review"
$setup = Join-Path $releaseRootFull (
    "artifacts\latex-word-review-$Version-windows-x64-setup.exe"
)
if (-not (Test-Path -LiteralPath $onedir -PathType Container)) {
    throw "verified PyInstaller onedir is missing"
}
if (-not (Test-Path -LiteralPath $setup -PathType Leaf)) {
    throw "Windows installer candidate is missing"
}

$installRoot = Join-Path $buildRoot "windows-installer-smoke"
$installLog = Join-Path $buildRoot "windows-installer-smoke-install.log"
$uninstallLog = Join-Path $buildRoot "windows-installer-smoke-uninstall.log"
$guiDataRoot = Join-Path $buildRoot "windows-installer-smoke-gui-data"
$defaultInstallRoot = Join-Path $env:LOCALAPPDATA "Programs\LatexWordReview"
$uninstallKey = (
    "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\" +
    "{FA0A89F6-357A-4C99-879A-A49B4576BB77}_is1"
)
foreach ($ownedPath in @($installRoot, $installLog, $uninstallLog, $guiDataRoot)) {
    Assert-ContainedPath -Root $buildRoot -Candidate $ownedPath -Label "installer smoke path"
    if (Test-Path -LiteralPath $ownedPath) {
        throw "installer smoke path already exists: $ownedPath"
    }
}
if ((Test-Path -LiteralPath $defaultInstallRoot) -or (Test-Path -LiteralPath $uninstallKey)) {
    throw "an existing user installation was detected; refusing to disturb it"
}

$installed = $false
$payloadFileCount = 0
$versionOutput = ""
$guiHttpStatus = 0
$guiExitCode = -1
$guiProcess = $null
try {
    $installArguments = @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/NOICONS",
        ('/DIR="' + $installRoot + '"'),
        ('/LOG="' + $installLog + '"')
    )
    Invoke-HiddenProcess -FilePath $setup -Arguments $installArguments -Label "installer"
    $installed = $true

    if (-not (Test-Path -LiteralPath $uninstallKey)) {
        throw "the per-user uninstall registration was not created"
    }
    $sourceFiles = @(
        Get-ChildItem -Force -File -Recurse -LiteralPath $onedir |
            Sort-Object FullName
    )
    if ($sourceFiles.Count -eq 0) {
        throw "the verified onedir contains no files"
    }
    $payloadPaths = New-Object "Collections.Generic.HashSet[string]" (
        [StringComparer]::OrdinalIgnoreCase
    )
    foreach ($sourceFile in $sourceFiles) {
        $relative = Get-RelativeFilePath -Root $onedir -Path $sourceFile.FullName
        $null = $payloadPaths.Add($relative)
        $installedPath = Join-Path $installRoot $relative
        if (-not (Test-Path -LiteralPath $installedPath -PathType Leaf)) {
            throw "installer omitted a payload file: $relative"
        }
        $sourceHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $sourceFile.FullName).Hash
        $installedHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $installedPath).Hash
        if ($sourceHash -cne $installedHash) {
            throw "installer changed a payload file: $relative"
        }
    }
    $payloadFileCount = $sourceFiles.Count

    foreach ($installedFile in Get-ChildItem -Force -File -Recurse -LiteralPath $installRoot) {
        $relative = Get-RelativeFilePath -Root $installRoot -Path $installedFile.FullName
        if (-not $payloadPaths.Contains($relative) -and
            $relative -notmatch '^unins[0-9]+\.(dat|exe|msg)$') {
            throw "installer added an unexpected application file: $relative"
        }
    }

    $cli = Join-Path $installRoot "latex-word-review.exe"
    $versionOutput = ((& $cli --version) | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $versionOutput -notmatch [regex]::Escape($Version)) {
        throw "installed CLI version smoke failed"
    }

    $gui = Join-Path $installRoot "LatexWordReview.exe"
    $guiPort = Get-FreeLoopbackPort
    $guiArguments = @(
        "app",
        "--data-root",
        ('"' + $guiDataRoot + '"'),
        "--no-browser",
        "--port",
        [string]$guiPort
    )
    $guiProcess = Start-Process -FilePath $gui -ArgumentList $guiArguments -WindowStyle Hidden -PassThru
    $origin = "http://127.0.0.1:$guiPort"
    $pageResponse = $null
    $guiSession = New-Object Microsoft.PowerShell.Commands.WebRequestSession
    for ($attempt = 0; $attempt -lt 80; $attempt++) {
        if ($guiProcess.HasExited) {
            throw "installed GUI exited before its local page became ready"
        }
        try {
            $pageResponse = Invoke-WebRequest -Uri "$origin/" -WebSession $guiSession -UseBasicParsing -TimeoutSec 1
            if ($pageResponse.StatusCode -eq 200) {
                break
            }
        }
        catch {
            $pageResponse = $null
        }
        Start-Sleep -Milliseconds 250
    }
    if ($null -eq $pageResponse -or $pageResponse.StatusCode -ne 200) {
        throw "installed GUI did not expose its local page within 20 seconds"
    }
    if ($pageResponse.Content -notmatch '<title>LaTeX.Word 审阅助手</title>') {
        throw "installed GUI returned an unexpected page"
    }
    $csrfMatch = [regex]::Match(
        $pageResponse.Content,
        'name="csrf" value="([A-Za-z0-9_-]+)"'
    )
    if (-not $csrfMatch.Success) {
        throw "installed GUI page omitted its CSRF token"
    }
    $shutdownResponse = Invoke-WebRequest -Uri "$origin/app/exit" -Method Post -WebSession $guiSession -Headers @{ Origin = $origin } -ContentType "application/x-www-form-urlencoded" -Body ("csrf=" + $csrfMatch.Groups[1].Value) -UseBasicParsing -TimeoutSec 5
    if ($shutdownResponse.StatusCode -ne 200) {
        throw "installed GUI rejected its graceful shutdown request"
    }
    if (-not $guiProcess.WaitForExit(15000)) {
        throw "installed GUI did not exit after its shutdown request"
    }
    $guiHttpStatus = $pageResponse.StatusCode
    $guiExitCode = $guiProcess.ExitCode
    if ($guiExitCode -ne 0) {
        throw "installed GUI exited with code $guiExitCode"
    }
}
finally {
    if ($null -ne $guiProcess -and -not $guiProcess.HasExited) {
        $guiProcess.Kill()
        $guiProcess.WaitForExit()
    }
    $uninstaller = Join-Path $installRoot "unins000.exe"
    if ($installed -and (Test-Path -LiteralPath $uninstaller -PathType Leaf)) {
        $uninstallArguments = @(
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            ('/LOG="' + $uninstallLog + '"')
        )
        Invoke-HiddenProcess -FilePath $uninstaller -Arguments $uninstallArguments -Label "uninstaller"
    }
    if (Test-Path -LiteralPath $guiDataRoot) {
        Assert-ContainedPath -Root $buildRoot -Candidate $guiDataRoot -Label "installer GUI smoke data"
        Remove-Item -LiteralPath $guiDataRoot -Recurse -Force
    }
}

for ($attempt = 0; $attempt -lt 100 -and (Test-Path -LiteralPath $installRoot); $attempt++) {
    Start-Sleep -Milliseconds 100
}
if (Test-Path -LiteralPath $installRoot) {
    throw "the smoke installation directory was not removed"
}
if (Test-Path -LiteralPath $uninstallKey) {
    throw "the per-user uninstall registration was not removed"
}

[ordered]@{
    status = "verified"
    version = $Version
    payload_file_count = $payloadFileCount
    installed_cli = $versionOutput
    installed_gui_http_status = $guiHttpStatus
    installed_gui_exit_code = $guiExitCode
    install_directory_removed = $true
    uninstall_registration_removed = $true
} | ConvertTo-Json -Compress
