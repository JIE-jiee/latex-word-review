#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$PrepareOnly,
    [string]$RuntimeRoot = "",
    [string]$DataRoot = "",
    [switch]$NoBrowser,
    [ValidateRange(0, 65535)]
    [int]$Port = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$UvVersion = "0.11.16"
$UvArchiveUri = "https://github.com/astral-sh/uv/releases/download/0.11.16/uv-x86_64-pc-windows-msvc.zip"
$UvArchiveSha256 = "dd9d6d6554bfab265bfa98aa8e8a406c5c3a7b97582f93de1f4d48d9154a0395"
$UvArchiveBytes = 23236992
$UvExeSha256 = "c5a583d5f1f6d055fc1c32c87d8eceee90edc69a5b9af5da70811befdfc04880"
$UvExeBytes = 67598848
$PythonRequest = "cpython-3.12.13-windows-x86_64-none"
$PythonVersion = "3.12.13"
$PythonArtifactUri = "https://github.com/astral-sh/python-build-standalone/releases/download/20260510/cpython-3.12.13%2B20260510-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"
$PythonArtifactSha256 = "24168aff2e7d93784c6a436124c4ebb79b076a4e289bde4902c08333507b71d0"
$BootstrapSchema = "latex-word-review/windows-source-bootstrap/v1"
$DefaultIndex = "https://pypi.org/simple"

function Write-Step {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host "[LaTeX Word Review] $Message"
}

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
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    $candidateFull = [IO.Path]::GetFullPath($Candidate)
    if (-not $candidateFull.StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must stay inside the extracted project folder."
    }
    if ($candidateFull.Equals($rootFull.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must not be the project folder itself."
    }
}

function Assert-NotReparsePoint {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $item = Get-Item -Force -LiteralPath $Path
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label must not be a link or reparse point."
    }
}

function Assert-OwnedPathChain {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Label
    )
    Assert-ContainedPath -Root $Root -Candidate $Candidate -Label $Label
    Assert-NotReparsePoint -Path $Root -Label "runtime root"
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $candidateFull = [IO.Path]::GetFullPath($Candidate)
    $relative = $candidateFull.Substring($rootFull.Length).TrimStart('\')
    $current = $rootFull
    foreach ($part in $relative.Split('\')) {
        if ([string]::IsNullOrWhiteSpace($part)) { continue }
        $current = Join-Path $current $part
        Assert-NotReparsePoint -Path $current -Label $Label
    }
}

function New-OwnedDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )
    Assert-OwnedPathChain -Root $Root -Candidate $Path -Label $Label
    [IO.Directory]::CreateDirectory($Path) | Out-Null
    Assert-OwnedPathChain -Root $Root -Candidate $Path -Label $Label
}

function Get-Sha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    $stream = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::Read
    )
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha256.ComputeHash($stream)
        return ([System.BitConverter]::ToString($hash)).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha256.Dispose()
        $stream.Dispose()
    }
}

function Get-StringSha256 {
    param([Parameter(Mandatory = $true)][string]$Value)
    $encoding = New-Object Text.UTF8Encoding($false)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = $algorithm.ComputeHash($encoding.GetBytes($Value))
    }
    finally {
        $algorithm.Dispose()
    }
    return ([BitConverter]::ToString($digest)).Replace("-", "").ToLowerInvariant()
}

function Remove-OwnedFile {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if (-not (Test-Path -LiteralPath $Path)) { return }
    Assert-OwnedPathChain -Root $Root -Candidate $Path -Label $Label
    $item = Get-Item -Force -LiteralPath $Path
    if ($item.PSIsContainer) { throw "$Label must be a regular file." }
    [IO.File]::Delete($Path)
}

function Invoke-VerifiedDownload {
    param(
        [Parameter(Mandatory = $true)][Uri]$Uri,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][long]$ExpectedBytes,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256
    )
    Add-Type -AssemblyName System.Net.Http
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $handler = New-Object Net.Http.HttpClientHandler
    $handler.AllowAutoRedirect = $true
    $client = New-Object Net.Http.HttpClient($handler)
    $client.Timeout = [TimeSpan]::FromMinutes(5)
    $client.DefaultRequestHeaders.UserAgent.ParseAdd("latex-word-review-source-bootstrap/1")
    $response = $null
    $inputStream = $null
    $outputStream = $null
    try {
        $response = $client.GetAsync(
            $Uri,
            [Net.Http.HttpCompletionOption]::ResponseHeadersRead
        ).GetAwaiter().GetResult()
        if (-not $response.IsSuccessStatusCode) {
            throw "download failed with HTTP status $([int]$response.StatusCode)"
        }
        if ($null -ne $response.Content.Headers.ContentLength -and
            $response.Content.Headers.ContentLength -ne $ExpectedBytes) {
            throw "download size does not match the pinned release asset"
        }
        $inputStream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $outputStream = New-Object IO.FileStream(
            $Destination,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        $buffer = New-Object byte[] 65536
        [long]$total = 0
        while (($read = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            $total += $read
            if ($total -gt $ExpectedBytes) {
                throw "download exceeded the pinned release asset size"
            }
            $outputStream.Write($buffer, 0, $read)
        }
        $outputStream.Flush()
        if ($total -ne $ExpectedBytes) { throw "download was incomplete" }
    }
    finally {
        if ($null -ne $outputStream) { $outputStream.Dispose() }
        if ($null -ne $inputStream) { $inputStream.Dispose() }
        if ($null -ne $response) { $response.Dispose() }
        $client.Dispose()
        $handler.Dispose()
    }
    if ((Get-Sha256 -Path $Destination) -ne $ExpectedSha256) {
        throw "download SHA-256 does not match the pinned release asset"
    }
}

function Get-VerifiedUvArchive {
    param(
        [Parameter(Mandatory = $true)][string]$Runtime,
        [Parameter(Mandatory = $true)][string]$Downloads
    )
    $archive = Join-Path $Downloads "uv-$UvVersion-windows-x64.zip"
    if (Test-Path -LiteralPath $archive -PathType Leaf) {
        Assert-OwnedPathChain -Root $Runtime -Candidate $archive -Label "uv archive"
        $item = Get-Item -Force -LiteralPath $archive
        if ($item.Length -eq $UvArchiveBytes -and (Get-Sha256 -Path $archive) -eq $UvArchiveSha256) {
            return $archive
        }
        Remove-OwnedFile -Root $Runtime -Path $archive -Label "invalid uv archive"
    }
    $temporary = Join-Path $Downloads ("uv-download-" + [Guid]::NewGuid().ToString("N") + ".tmp")
    try {
        Write-Step "Downloading the pinned uv $UvVersion bootstrap tool..."
        Invoke-VerifiedDownload `
            -Uri ([Uri]$UvArchiveUri) `
            -Destination $temporary `
            -ExpectedBytes $UvArchiveBytes `
            -ExpectedSha256 $UvArchiveSha256
        [IO.File]::Move($temporary, $archive)
    }
    finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-OwnedFile -Root $Runtime -Path $temporary -Label "temporary uv download"
        }
    }
    return $archive
}

function Install-VerifiedUv {
    param(
        [Parameter(Mandatory = $true)][string]$Runtime,
        [Parameter(Mandatory = $true)][string]$Downloads,
        [Parameter(Mandatory = $true)][string]$UvDirectory
    )
    $archive = Get-VerifiedUvArchive -Runtime $Runtime -Downloads $Downloads
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($archive)
    $temporaryUv = ""
    try {
        $expected = @{
            "uv.exe" = 67598848
            "uvw.exe" = 338432
            "uvx.exe" = 337920
        }
        if ($zip.Entries.Count -ne $expected.Count) {
            throw "uv archive member count is unexpected"
        }
        foreach ($entry in $zip.Entries) {
            if (-not $expected.ContainsKey($entry.FullName) -or
                $entry.Length -ne $expected[$entry.FullName]) {
                throw "uv archive contains an unexpected member"
            }
        }
        $uvEntry = $zip.GetEntry("uv.exe")
        if ($null -eq $uvEntry) { throw "uv archive is missing uv.exe" }
        $temporaryUv = Join-Path $UvDirectory ("uv-" + [Guid]::NewGuid().ToString("N") + ".tmp")
        $entryStream = $null
        $fileStream = $null
        try {
            $entryStream = $uvEntry.Open()
            $fileStream = New-Object IO.FileStream(
                $temporaryUv,
                [IO.FileMode]::CreateNew,
                [IO.FileAccess]::Write,
                [IO.FileShare]::None
            )
            $entryStream.CopyTo($fileStream)
            $fileStream.Flush()
        }
        finally {
            if ($null -ne $fileStream) { $fileStream.Dispose() }
            if ($null -ne $entryStream) { $entryStream.Dispose() }
        }
        if ((Get-Item -Force -LiteralPath $temporaryUv).Length -ne $UvExeBytes -or
            (Get-Sha256 -Path $temporaryUv) -ne $UvExeSha256) {
            throw "extracted uv.exe does not match the pinned executable"
        }
        $uvExe = Join-Path $UvDirectory "uv.exe"
        if (Test-Path -LiteralPath $uvExe) {
            Remove-OwnedFile -Root $Runtime -Path $uvExe -Label "old uv executable"
        }
        [IO.File]::Move($temporaryUv, $uvExe)
        $temporaryUv = ""
        return $uvExe
    }
    finally {
        $zip.Dispose()
        if (-not [string]::IsNullOrWhiteSpace($temporaryUv) -and
            (Test-Path -LiteralPath $temporaryUv)) {
            Remove-OwnedFile -Root $Runtime -Path $temporaryUv -Label "temporary uv executable"
        }
    }
}

function Get-VerifiedUv {
    param(
        [Parameter(Mandatory = $true)][string]$Runtime,
        [Parameter(Mandatory = $true)][string]$Downloads,
        [Parameter(Mandatory = $true)][string]$UvDirectory
    )
    $uvExe = Join-Path $UvDirectory "uv.exe"
    if (-not (Test-Path -LiteralPath $uvExe -PathType Leaf) -or
        (Get-Sha256 -Path $uvExe) -ne $UvExeSha256) {
        $uvExe = Install-VerifiedUv -Runtime $Runtime -Downloads $Downloads -UvDirectory $UvDirectory
    }
    Assert-OwnedPathChain -Root $Runtime -Candidate $uvExe -Label "uv executable"
    $identity = (& $uvExe --version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $identity -notmatch "^uv $([regex]::Escape($UvVersion))(?:\s|$)") {
        throw "the pinned uv executable failed its version check"
    }
    return $uvExe
}

function Invoke-Uv {
    param(
        [Parameter(Mandatory = $true)][string]$UvExe,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Label
    )
    & $UvExe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Label failed with exit code $LASTEXITCODE" }
}

function Test-ApplicationEnvironment {
    param(
        [Parameter(Mandatory = $true)][string]$PythonExe,
        [Parameter(Mandatory = $true)][string]$ApplicationExe
    )
    if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf) -or
        -not (Test-Path -LiteralPath $ApplicationExe -PathType Leaf)) {
        return $false
    }
    try {
        $probeText = (& $PythonExe -I -B -c `
            "import platform,struct;print(platform.python_version());print(struct.calcsize('P')*8)" `
            2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) { return $false }
        $probe = @($probeText -split "`r?`n")
        if ($probe.Count -lt 2 -or $probe[0].Trim() -ne $PythonVersion -or
            $probe[1].Trim() -ne "64") {
            return $false
        }
        $appIdentity = (& $ApplicationExe --version 2>&1 | Out-String).Trim()
        return $LASTEXITCODE -eq 0 -and $appIdentity -match "^latex-word-review\s+"
    }
    catch {
        return $false
    }
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Text
    )
    $encoding = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, $Text, $encoding)
}

function Start-ReviewApplication {
    param(
        [Parameter(Mandatory = $true)][string]$ApplicationExe,
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$LogDirectory,
        [string]$SelectedDataRoot,
        [bool]$DisableBrowser,
        [int]$SelectedPort
    )
    $token = (Get-Date -Format "yyyyMMdd-HHmmss") + "-" + [Guid]::NewGuid().ToString("N")
    $stdoutLog = Join-Path $LogDirectory "app-$token.stdout.log"
    $stderrLog = Join-Path $LogDirectory "app-$token.stderr.log"
    $arguments = @("app")
    if (-not [string]::IsNullOrWhiteSpace($SelectedDataRoot)) {
        $normalizedDataRoot = [IO.Path]::GetFullPath($SelectedDataRoot).TrimEnd('\')
        $arguments += "--data-root"
        $arguments += ('"' + $normalizedDataRoot + '"')
    }
    if ($DisableBrowser) { $arguments += "--no-browser" }
    if ($SelectedPort -ne 0) {
        $arguments += "--port"
        $arguments += [string]$SelectedPort
    }
    Write-Step "Opening the local review interface..."
    $process = Start-Process `
        -FilePath $ApplicationExe `
        -ArgumentList $arguments `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -PassThru
    Start-Sleep -Milliseconds 1800
    if ($process.HasExited) {
        $details = ""
        if (Test-Path -LiteralPath $stderrLog -PathType Leaf) {
            $details = (Get-Content -LiteralPath $stderrLog -Tail 20 | Out-String).Trim()
        }
        if ([string]::IsNullOrWhiteSpace($details)) { $details = "See $stderrLog" }
        throw "the application exited during startup. $details"
    }
    Write-Step "The application is running. This window can now close."
    Write-Step "Startup logs: $LogDirectory"
}

try {
    if ($env:OS -ne "Windows_NT") { throw "Windows is the only supported operating system." }
    if (-not [Environment]::Is64BitOperatingSystem -or -not [Environment]::Is64BitProcess) {
        throw "64-bit Windows and 64-bit Windows PowerShell are required."
    }
    $projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
    foreach ($required in @("pyproject.toml", "uv.lock", "src\latex_word_review\__init__.py")) {
        if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $required) -PathType Leaf)) {
            throw "the extracted project is incomplete: missing $required"
        }
    }

    if ([string]::IsNullOrWhiteSpace($RuntimeRoot) -and
        -not [string]::IsNullOrWhiteSpace($env:LATEX_WORD_REVIEW_BOOTSTRAP_RUNTIME_ROOT)) {
        $RuntimeRoot = $env:LATEX_WORD_REVIEW_BOOTSTRAP_RUNTIME_ROOT
    }
    if ([string]::IsNullOrWhiteSpace($RuntimeRoot)) {
        $RuntimeRoot = Join-Path $projectRoot ".lwr-runtime"
    }
    $RuntimeRoot = Get-FullPath -Path $RuntimeRoot -BasePath $projectRoot
    Assert-ContainedPath -Root $projectRoot -Candidate $RuntimeRoot -Label "runtime root"
    [IO.Directory]::CreateDirectory($RuntimeRoot) | Out-Null
    Assert-NotReparsePoint -Path $RuntimeRoot -Label "runtime root"

    if (-not $PrepareOnly -and $env:LATEX_WORD_REVIEW_BOOTSTRAP_PREPARE_ONLY -eq "1") {
        $PrepareOnly = $true
    }
    if ([string]::IsNullOrWhiteSpace($DataRoot) -and
        -not [string]::IsNullOrWhiteSpace($env:LATEX_WORD_REVIEW_BOOTSTRAP_DATA_ROOT)) {
        $DataRoot = $env:LATEX_WORD_REVIEW_BOOTSTRAP_DATA_ROOT
    }

    $downloads = Join-Path $RuntimeRoot "downloads"
    $uvDirectory = Join-Path $RuntimeRoot "uv\$UvVersion"
    $pythonDirectory = Join-Path $RuntimeRoot "python"
    $cacheDirectory = Join-Path $RuntimeRoot "cache"
    $venvDirectory = Join-Path $RuntimeRoot "venv"
    $tempDirectory = Join-Path $RuntimeRoot "temp"
    $logDirectory = Join-Path $RuntimeRoot "logs"
    foreach ($directorySpec in @(
        @($downloads, "download directory"),
        @($uvDirectory, "uv directory"),
        @($pythonDirectory, "Python directory"),
        @($cacheDirectory, "cache directory"),
        @($tempDirectory, "temporary directory"),
        @($logDirectory, "log directory")
    )) {
        New-OwnedDirectory -Root $RuntimeRoot -Path $directorySpec[0] -Label $directorySpec[1]
    }

    $lockPath = Join-Path $RuntimeRoot "bootstrap.lock"
    Assert-OwnedPathChain -Root $RuntimeRoot -Candidate $lockPath -Label "bootstrap lock"
    $lockStream = $null
    $lockDeadline = [DateTime]::UtcNow.AddSeconds(30)
    while ($null -eq $lockStream -and [DateTime]::UtcNow -lt $lockDeadline) {
        try {
            $lockStream = New-Object IO.FileStream(
                $lockPath,
                [IO.FileMode]::OpenOrCreate,
                [IO.FileAccess]::ReadWrite,
                [IO.FileShare]::None
            )
        }
        catch [IO.IOException] {
            Start-Sleep -Milliseconds 250
        }
    }
    if ($null -eq $lockStream) {
        throw "another initialization is still running; wait for it to finish and try again"
    }
    Assert-OwnedPathChain -Root $RuntimeRoot -Candidate $lockPath -Label "bootstrap lock"

    try {
        foreach ($entry in @(Get-ChildItem Env:)) {
            if ($entry.Name -like "UV_*" -or $entry.Name -like "PIP_*" -or
                $entry.Name -in @("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX")) {
                Remove-Item -LiteralPath ("Env:" + $entry.Name) -ErrorAction SilentlyContinue
            }
        }
        $env:UV_CACHE_DIR = $cacheDirectory
        $env:UV_PROJECT_ENVIRONMENT = $venvDirectory
        $env:UV_PYTHON_INSTALL_DIR = $pythonDirectory
        $env:UV_PYTHON_INSTALL_BIN = "0"
        $env:UV_PYTHON_INSTALL_REGISTRY = "0"
        $env:UV_MANAGED_PYTHON = "1"
        $env:UV_NO_SYSTEM_CONFIG = "1"
        $env:UV_NO_ENV_FILE = "1"
        $env:UV_DEFAULT_INDEX = $DefaultIndex
        $env:PYTHONNOUSERSITE = "1"
        $env:PYTHONUTF8 = "1"
        $env:TEMP = $tempDirectory
        $env:TMP = $tempDirectory

        $pyprojectHash = Get-Sha256 -Path (Join-Path $projectRoot "pyproject.toml")
        $lockHash = Get-Sha256 -Path (Join-Path $projectRoot "uv.lock")
        $scriptHash = Get-Sha256 -Path $PSCommandPath
        $identityMaterial = @(
            $BootstrapSchema,
            $projectRoot,
            $pyprojectHash,
            $lockHash,
            $scriptHash,
            $UvVersion,
            $UvArchiveSha256,
            $UvExeSha256,
            $PythonRequest,
            $PythonArtifactSha256,
            "pdf-figures",
            "no-default-groups"
        ) -join "`n"
        $identity = Get-StringSha256 -Value $identityMaterial
        $readyPath = Join-Path $RuntimeRoot "ready.sha256"
        $evidencePath = Join-Path $RuntimeRoot "bootstrap-evidence.json"
        $pythonExe = Join-Path $venvDirectory "Scripts\python.exe"
        $applicationExe = Join-Path $venvDirectory "Scripts\latex-word-review.exe"
        $ready = $false
        if ((Test-Path -LiteralPath $readyPath -PathType Leaf) -and
            (Test-Path -LiteralPath $evidencePath -PathType Leaf)) {
            Assert-OwnedPathChain -Root $RuntimeRoot -Candidate $readyPath -Label "readiness receipt"
            $ready = (Get-Content -Raw -LiteralPath $readyPath).Trim() -eq $identity
        }
        if ($ready) {
            $ready = Test-ApplicationEnvironment -PythonExe $pythonExe -ApplicationExe $applicationExe
        }

        if (-not $ready) {
            $uvExe = Get-VerifiedUv -Runtime $RuntimeRoot -Downloads $downloads -UvDirectory $uvDirectory
            Write-Step "First-run setup is preparing a private Python $PythonVersion runtime."
            Invoke-Uv -UvExe $uvExe -Label "managed Python installation" -Arguments @(
                "python", "install", $PythonRequest,
                "--managed-python", "--no-bin", "--no-registry",
                "--install-dir", $pythonDirectory,
                "--cache-dir", $cacheDirectory
            )
            $lockBefore = Get-Sha256 -Path (Join-Path $projectRoot "uv.lock")
            Invoke-Uv -UvExe $uvExe -Label "lock-file validation" -Arguments @(
                "lock", "--check",
                "--project", $projectRoot,
                "--python", $PythonRequest,
                "--managed-python", "--no-python-downloads", "--offline"
            )
            if ((Get-Sha256 -Path (Join-Path $projectRoot "uv.lock")) -ne $lockBefore) {
                throw "lock-file validation changed uv.lock"
            }
            Write-Step "Installing the locked application dependencies..."
            Invoke-Uv -UvExe $uvExe -Label "locked dependency installation" -Arguments @(
                "sync", "--frozen",
                "--no-default-groups",
                "--extra", "pdf-figures",
                "--project", $projectRoot,
                "--python", $PythonRequest,
                "--managed-python", "--no-python-downloads",
                "--cache-dir", $cacheDirectory,
                "--default-index", $DefaultIndex
            )
            if (-not (Test-ApplicationEnvironment -PythonExe $pythonExe -ApplicationExe $applicationExe)) {
                throw "the prepared application environment failed its version or architecture check"
            }
            & $uvExe cache clean --cache-dir $cacheDirectory
            $cacheCleaned = $LASTEXITCODE -eq 0
            if (-not $cacheCleaned) {
                Write-Step "The download cache could not be fully cleaned; the application is still ready."
            }
            $uvArchive = Join-Path $downloads "uv-$UvVersion-windows-x64.zip"
            if (Test-Path -LiteralPath $uvArchive) {
                Remove-OwnedFile -Root $RuntimeRoot -Path $uvArchive -Label "verified uv archive"
            }
            Remove-OwnedFile -Root $RuntimeRoot -Path $uvExe -Label "verified uv executable"
            $readyTemporary = Join-Path $RuntimeRoot ("ready-" + [Guid]::NewGuid().ToString("N") + ".tmp")
            Write-Utf8NoBom -Path $readyTemporary -Text ($identity + "`n")
            if (Test-Path -LiteralPath $readyPath) {
                Remove-OwnedFile -Root $RuntimeRoot -Path $readyPath -Label "old readiness receipt"
            }
            [IO.File]::Move($readyTemporary, $readyPath)
            $evidence = [ordered]@{
                schema = $BootstrapSchema
                identity_sha256 = $identity
                uv_version = $UvVersion
                uv_archive_sha256 = $UvArchiveSha256
                uv_exe_sha256 = $UvExeSha256
                python_request = $PythonRequest
                python_artifact_uri = $PythonArtifactUri
                python_artifact_sha256 = $PythonArtifactSha256
                pyproject_sha256 = $pyprojectHash
                uv_lock_sha256 = $lockHash
                dependency_groups = @()
                extras = @("pdf-figures")
                bootstrap_executable_retained = $false
                bootstrap_archive_retained = $false
                dependency_cache_cleaned = $cacheCleaned
            } | ConvertTo-Json -Depth 4
            $evidenceTemporary = Join-Path $RuntimeRoot ("evidence-" + [Guid]::NewGuid().ToString("N") + ".tmp")
            Write-Utf8NoBom -Path $evidenceTemporary -Text ($evidence + "`n")
            if (Test-Path -LiteralPath $evidencePath) {
                Remove-OwnedFile -Root $RuntimeRoot -Path $evidencePath -Label "old bootstrap evidence"
            }
            [IO.File]::Move($evidenceTemporary, $evidencePath)
            Write-Step "First-run setup completed. Later starts use the local prepared runtime."
        }
        else {
            Write-Step "Prepared runtime verified; no download or dependency sync is needed."
        }

        if ($PrepareOnly) {
            Write-Step "Preparation check completed successfully."
        }
        else {
            Start-ReviewApplication `
                -ApplicationExe $applicationExe `
                -ProjectRoot $projectRoot `
                -LogDirectory $logDirectory `
                -SelectedDataRoot $DataRoot `
                -DisableBrowser ([bool]$NoBrowser) `
                -SelectedPort $Port
        }
    }
    finally {
        $lockStream.Dispose()
    }
}
catch {
    Write-Host ""
    Write-Host "[ERROR] $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "The original LaTeX and Word files were not modified by the launcher."
    Write-Host "If the download was interrupted, check the network and double-click again."
    exit 1
}
