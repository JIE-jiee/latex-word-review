param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $SourceDocx,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $WorkingDocx,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $DestinationDocx,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $ReportPath,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $PidStatePath,

    [Parameter(Mandatory = $true)]
    [ValidateRange(0, 100000)]
    [int] $ExpectedFieldCount,

    [Parameter(Mandatory = $true)]
    [ValidateRange(0, 100000)]
    [int] $ExpectedUnresolvedFields,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $UnresolvedPlaceholder
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$WdAlertsNone = 0
$WdDoNotSaveChanges = 0
$WdFormatXmlDocument = 12
$MsoAutomationSecurityForceDisable = 3

function Get-WinWordProcessSnapshot {
    $snapshot = @()
    $processes = @(Get-Process -Name "WINWORD" -ErrorAction SilentlyContinue)
    foreach ($process in $processes) {
        try {
            $snapshot += [pscustomobject]@{
                pid = [int] $process.Id
                started_filetime_utc = [long] $process.StartTime.ToUniversalTime().ToFileTimeUtc()
            }
        }
        catch {
            throw "Unable to inspect every existing WINWORD process before automation."
        }
        finally {
            $process.Dispose()
        }
    }
    return @($snapshot)
}

function Get-WinWordIdentityKey {
    param(
        [Parameter(Mandatory = $true)]
        [object] $Identity
    )

    return "{0}:{1}" -f ([int] $Identity.pid), ([long] $Identity.started_filetime_utc)
}

function Get-Sha256 {
    param([Parameter(Mandatory = $true)][string] $LiteralPath)
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $LiteralPath).Hash.ToLowerInvariant()
}

function Release-ComObject {
    param(
        [Parameter(Mandatory = $false)]
        [AllowNull()]
        [object] $ComObject
    )
    if ($null -ne $ComObject -and [System.Runtime.InteropServices.Marshal]::IsComObject($ComObject)) {
        try {
            [void] [System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($ComObject)
        }
        catch {
            # Cleanup must continue if Word disconnected an RCW during Close or Quit.
        }
    }
}

function Write-Utf8Json {
    param(
        [Parameter(Mandatory = $true)][object] $Value,
        [Parameter(Mandatory = $true)][string] $LiteralPath
    )
    $json = ($Value | ConvertTo-Json -Depth 4 -Compress) + [System.Environment]::NewLine
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($LiteralPath, $json, $encoding)
}

function Get-StringSha256 {
    param([Parameter(Mandatory = $true)][string] $Value)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
        return ([System.BitConverter]::ToString($algorithm.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $algorithm.Dispose()
    }
}

function Get-DocumentRevisionCount {
    param([Parameter(Mandatory = $true)][object] $Document)
    $revisions = $null
    try {
        $revisions = $Document.Revisions
        return [int] $revisions.Count
    }
    finally {
        Release-ComObject -ComObject $revisions
    }
}

function Invoke-TransientComAction {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock] $Action
    )
    $transientHResults = @(
        [int64] -2147418111, # RPC_E_CALL_REJECTED
        [int64] -2147417846  # RPC_E_SERVERCALL_RETRYLATER
    )
    for ($attempt = 1; $attempt -le 5; $attempt += 1) {
        try {
            return (& $Action)
        }
        catch {
            $exception = $_.Exception
            while ($null -ne $exception.InnerException) {
                $exception = $exception.InnerException
            }
            $hresult = [int64] $exception.HResult
            if ($attempt -ge 5 -or $transientHResults -notcontains $hresult) {
                throw
            }
            Start-Sleep -Milliseconds (100 * $attempt)
        }
    }
}


function Get-DocumentBookmarkCount {
    param([Parameter(Mandatory = $true)][object] $Document)
    $bookmarks = $null
    try {
        $bookmarks = $Document.Bookmarks
        $bookmarks.ShowHidden = $true
        return [int] $bookmarks.Count
    }
    finally {
        Release-ComObject -ComObject $bookmarks
    }
}

function Test-DocumentBookmark {
    param(
        [Parameter(Mandatory = $true)][object] $Document,
        [Parameter(Mandatory = $true)][string] $Name
    )
    $bookmarks = $null
    try {
        $bookmarks = $Document.Bookmarks
        $bookmarks.ShowHidden = $true
        return [bool] $bookmarks.Exists($Name)
    }
    finally {
        Release-ComObject -ComObject $bookmarks
    }
}

function Stop-OwnedWordProcess {
    param(
        [Parameter(Mandatory = $true)][int] $ProcessId,
        [Parameter(Mandatory = $true)][int64] $StartedFileTimeUtc
    )
    if ($ProcessId -le 0 -or $StartedFileTimeUtc -le 0) {
        return $false
    }
    for ($attempt = 0; $attempt -lt 26; $attempt++) {
        $candidate = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
        if ($null -eq $candidate) {
            return $true
        }
        try {
            if ([int64] $candidate.StartTime.ToFileTimeUtc() -ne $StartedFileTimeUtc) {
                return $true
            }
            if ($attempt -ge 24) {
                Stop-Process -InputObject $candidate -Force -ErrorAction SilentlyContinue
            }
        }
        finally {
            $candidate.Dispose()
        }
        Start-Sleep -Milliseconds 100
    }
    $remaining = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $remaining) {
        return $true
    }
    try {
        return [int64] $remaining.StartTime.ToFileTimeUtc() -ne $StartedFileTimeUtc
    }
    finally {
        $remaining.Dispose()
    }
}

function Invoke-FieldRefreshPass {
    param(
        [Parameter(Mandatory = $true)][object] $Document,
        [Parameter(Mandatory = $true)][int] $ExpectedCount,
        [Parameter(Mandatory = $true)][int] $ExpectedUnresolved,
        [Parameter(Mandatory = $true)][string] $Placeholder
    )

    $fields = $null
    $records = New-Object 'System.Collections.Generic.List[string]'
    try {
        $fields = $Document.Fields
        $count = [int] $fields.Count
        if ($count -ne $ExpectedCount) {
            throw "The Word main-story field count changed during refresh."
        }
        $updated = 0
        $unresolved = 0
        for ($index = 1; $index -le $count; $index++) {
            $field = $null
            $codeRange = $null
            $resultRange = $null
            try {
                $field = $fields.Item($index)
                $codeRange = $field.Code
                $normalized = [System.Text.RegularExpressions.Regex]::Replace(
                    [string] $codeRange.Text,
                    "\s+",
                    " "
                ).Trim()
                $reference = [System.Text.RegularExpressions.Regex]::Match(
                    $normalized,
                    "^(?<kind>REF|PAGEREF)\s+(?<target>[A-Za-z_][A-Za-z0-9_]{0,254})(?<switches>(?:\s+\\[hr])*)$",
                    [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
                )
                if ($reference.Success) {
                    $switches = @(
                        [System.Text.RegularExpressions.Regex]::Matches(
                            $reference.Groups["switches"].Value,
                            "\\[hr]",
                            [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
                        ) | ForEach-Object { $_.Value.ToLowerInvariant() }
                    )
                    if ($switches.Count -ne @($switches | Select-Object -Unique).Count -or
                        ($reference.Groups["kind"].Value -ieq "PAGEREF" -and
                            $switches -contains "\r")) {
                        throw (
                            "The Word reference field switches differ from the validated " +
                            "preflight contract."
                        )
                    }
                }
                $targetMissing = $reference.Success -and -not (
                    Test-DocumentBookmark `
                        -Document $Document `
                        -Name $reference.Groups["target"].Value)
                if ($targetMissing) {
                    $resultRange = $field.Result
                    $resultRange.Text = $Placeholder
                    $unresolved++
                }
                else {
                    if (-not [bool] $field.Update()) {
                        throw "Microsoft Word reported a field update failure."
                    }
                    $resultRange = $field.Result
                    $updated++
                }
                $record = (
                    ([string] $index) + [char] 0 +
                    $normalized + [char] 0 +
                    ([string] $resultRange.Text)
                )
                [void] $records.Add($record)
            }
            finally {
                Release-ComObject -ComObject $resultRange
                Release-ComObject -ComObject $codeRange
                Release-ComObject -ComObject $field
            }
        }
        if ($updated -ne ($ExpectedCount - $ExpectedUnresolved) -or
            $unresolved -ne $ExpectedUnresolved) {
            throw "The refreshed and unresolved field counts differ from preflight."
        }
        $snapshot = [string]::Join(([string] [char] 31), [string[]] $records.ToArray())
        return [pscustomobject]@{
            updated = $updated
            unresolved = $unresolved
            results_sha256 = Get-StringSha256 -Value $snapshot
        }
    }
    finally {
        Release-ComObject -ComObject $fields
    }
}

$document = $null
$word = $null
$ownedWordPid = 0
$ownedWordStartFileTimeUtc = [int64] 0
$ownedWord = $false
$exitCode = 1
$reportValue = $null
$failureStage = "preflight"
$SourceFullPath = $null
$WorkingFullPath = $null
$DestinationFullPath = $null
$ReportFullPath = $null
$PidStateFullPath = $null

try {
    if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
        throw "Microsoft Word field refresh is supported only on Windows."
    }
    if ($UnresolvedPlaceholder.Length -gt 128 -or $UnresolvedPlaceholder.Contains("`0")) {
        throw "The unresolved-reference placeholder is invalid."
    }

    $SourceFullPath = [System.IO.Path]::GetFullPath($SourceDocx)
    $WorkingFullPath = [System.IO.Path]::GetFullPath($WorkingDocx)
    $DestinationFullPath = [System.IO.Path]::GetFullPath($DestinationDocx)
    $ReportFullPath = [System.IO.Path]::GetFullPath($ReportPath)
    $PidStateFullPath = [System.IO.Path]::GetFullPath($PidStatePath)
    $ownedPaths = @(
        $WorkingFullPath,
        $DestinationFullPath,
        $ReportFullPath,
        $PidStateFullPath
    )
    if (-not (Test-Path -LiteralPath $SourceFullPath -PathType Leaf) -or
        [System.IO.Path]::GetExtension($SourceFullPath) -ine ".docx") {
        throw "The field-refresh input must be an existing DOCX."
    }
    if (($ownedPaths | Select-Object -Unique).Count -ne $ownedPaths.Count -or
        $ownedPaths -contains $SourceFullPath) {
        throw "Field-refresh paths must be distinct."
    }
    $outputParent = Split-Path -Parent $DestinationFullPath
    if ([string]::IsNullOrWhiteSpace($outputParent) -or
        -not (Test-Path -LiteralPath $outputParent -PathType Container)) {
        throw "The field-refresh output parent must already exist."
    }
    foreach ($path in $ownedPaths) {
        if ((Split-Path -Parent $path) -cne $outputParent) {
            throw "All field-refresh artifacts must share one owned parent."
        }
        if (Test-Path -LiteralPath $path) {
            throw "A field-refresh artifact already exists."
        }
    }

    $sourceHashBefore = Get-Sha256 -LiteralPath $SourceFullPath
    Copy-Item -LiteralPath $SourceFullPath -Destination $WorkingFullPath

    $failureStage = "word_create"
    $beforeWordIdentities = @(Get-WinWordProcessSnapshot)
    $beforeWordKeys = @{}
    foreach ($identity in $beforeWordIdentities) {
        $beforeWordKeys[(Get-WinWordIdentityKey -Identity $identity)] = $true
    }
    try {
        $word = New-Object -ComObject Word.Application
    }
    catch {
        $exitCode = 3
        throw "Microsoft Word desktop automation is unavailable."
    }

    $failureStage = "word_ownership"
    $newWordIdentities = @()
    for ($attempt = 0; $attempt -lt 50; $attempt++) {
        $newWordIdentities = @(
            Get-WinWordProcessSnapshot | Where-Object {
                -not $beforeWordKeys.ContainsKey((Get-WinWordIdentityKey -Identity $_))
            }
        )
        if ($newWordIdentities.Count -ne 0) {
            break
        }
        Start-Sleep -Milliseconds 100
    }
    if ($newWordIdentities.Count -ne 1) {
        $failureStage = if ($newWordIdentities.Count -eq 0) {
            "word_ownership_no_new_process"
        }
        else {
            "word_ownership_multiple_new_processes"
        }
        throw "Word automation did not create exactly one isolated process."
    }
    $ownedIdentity = $newWordIdentities[0]
    $ownedWordPid = [int] $ownedIdentity.pid
    $ownedWordStartFileTimeUtc = [int64] $ownedIdentity.started_filetime_utc
    $ownedWord = (
        $ownedWordPid -gt 0 -and
        $ownedWordStartFileTimeUtc -gt 0 -and
        -not $beforeWordKeys.ContainsKey((Get-WinWordIdentityKey -Identity $ownedIdentity))
    )
    if (-not $ownedWord) {
        $failureStage = "word_ownership_unknown"
        throw "Word automation process identity is invalid."
    }
    Write-Utf8Json -Value ([ordered]@{
        schema_version = "word-field-process/v1"
        owned = $true
        pid = $ownedWordPid
        started_filetime_utc = $ownedWordStartFileTimeUtc
    }) -LiteralPath $PidStateFullPath

    $failureStage = "word_configuration"
    $word.AutomationSecurity = $MsoAutomationSecurityForceDisable
    $word.DisplayAlerts = $WdAlertsNone
    $word.Visible = $false
    $word.ScreenUpdating = $false
    $wordVersion = [string] $word.Version

    # FileName, ConfirmConversions, ReadOnly, AddToRecentFiles.
    $failureStage = "document_open"
    $documents = $null
    try {
        $documents = $word.Documents
        $document = $documents.Open($WorkingFullPath, $false, $false, $false)
    }
    finally {
        Release-ComObject -ComObject $documents
    }
    $failureStage = "document_preflight_disable_tracking"
    [void] (Invoke-TransientComAction -Action {
        $document.TrackRevisions = $false
    })
    $failureStage = "document_preflight_revision_count"
    $preflightRevisionCount = [int] (Invoke-TransientComAction -Action {
        Get-DocumentRevisionCount -Document $document
    })
    if ($preflightRevisionCount -ne 0) {
        $failureStage = "document_preflight_revisions_present"
        throw "The field-refresh input unexpectedly contains revisions."
    }

    $failureStage = "first_update"
    $firstPass = Invoke-FieldRefreshPass `
        -Document $document `
        -ExpectedCount $ExpectedFieldCount `
        -ExpectedUnresolved $ExpectedUnresolvedFields `
        -Placeholder $UnresolvedPlaceholder
    $failureStage = "first_repaginate"
    [void] $document.Repaginate()
    $failureStage = "second_update"
    $secondPass = Invoke-FieldRefreshPass `
        -Document $document `
        -ExpectedCount $ExpectedFieldCount `
        -ExpectedUnresolved $ExpectedUnresolvedFields `
        -Placeholder $UnresolvedPlaceholder
    $failureStage = "second_repaginate"
    [void] $document.Repaginate()
    $failureStage = "third_update"
    $thirdPass = Invoke-FieldRefreshPass `
        -Document $document `
        -ExpectedCount $ExpectedFieldCount `
        -ExpectedUnresolved $ExpectedUnresolvedFields `
        -Placeholder $UnresolvedPlaceholder

    $failureStage = "post_update_validation"
    if ((Get-DocumentRevisionCount -Document $document) -ne 0 -or
        [bool] $document.TrackRevisions) {
        throw "Word created revisions while refreshing the field baseline."
    }
    if ([int] $firstPass.updated -ne [int] $secondPass.updated -or
        [int] $secondPass.updated -ne [int] $thirdPass.updated -or
        [int] $firstPass.unresolved -ne [int] $secondPass.unresolved -or
        [int] $secondPass.unresolved -ne [int] $thirdPass.unresolved -or
        [string] $secondPass.results_sha256 -cne [string] $thirdPass.results_sha256) {
        throw "Word field results did not reach a stable fixed point."
    }

    $failureStage = "save"
    $bookmarkCount = Get-DocumentBookmarkCount -Document $document
    $document.SaveAs2(
        $DestinationFullPath,
        $WdFormatXmlDocument,
        [System.Type]::Missing,
        [System.Type]::Missing,
        $false
    )
    $reportRevisionCount = Get-DocumentRevisionCount -Document $document
    if ($reportRevisionCount -ne 0) {
        throw "Word created revisions while saving the field baseline."
    }

    $failureStage = "document_close"
    $document.Close($WdDoNotSaveChanges)
    Release-ComObject -ComObject $document
    $document = $null
    $failureStage = "word_quit"
    try {
        $word.Quit($WdDoNotSaveChanges)
    }
    catch {
        # A disconnected RCW is acceptable only if the exact owned process exits below.
    }
    finally {
        Release-ComObject -ComObject $word
        $word = $null
    }
    [System.GC]::Collect()
    [System.GC]::WaitForPendingFinalizers()
    $failureStage = "word_exit"
    if (-not (Stop-OwnedWordProcess `
        -ProcessId $ownedWordPid `
        -StartedFileTimeUtc $ownedWordStartFileTimeUtc)) {
        throw "The isolated Word process did not exit."
    }

    $failureStage = "report_output_hash"
    $reportOutputHash = Get-Sha256 -LiteralPath $DestinationFullPath
    $failureStage = "report_object"
    $reportValue = [ordered]@{
        schema_version = "word-field-refresh/v1"
        status = "passed"
        word_version = $wordVersion
        field_count = $ExpectedFieldCount
        updated_fields = [int] $thirdPass.updated
        unresolved_fields = [int] $thirdPass.unresolved
        revision_count = $reportRevisionCount
        bookmark_count = $bookmarkCount
        source_sha256 = $sourceHashBefore
        output_sha256 = $reportOutputHash
    }
    $exitCode = 0
}
catch {
    [Console]::Error.WriteLine("LWR_WORD_FAILURE_STAGE=" + $failureStage)
    $failureException = $_.Exception
    while ($null -ne $failureException.InnerException) {
        $failureException = $failureException.InnerException
    }
    [Console]::Error.WriteLine(
        "LWR_WORD_FAILURE_EXCEPTION=" + $failureException.GetType().FullName
    )
    [Console]::Error.WriteLine("LWR_WORD_FAILURE_HRESULT=" + [int64] $failureException.HResult)
}
finally {
    if ($null -ne $document) {
        try {
            $document.Close($WdDoNotSaveChanges)
        }
        catch {
            # Word.Quit remains the final cleanup boundary.
        }
        finally {
            Release-ComObject -ComObject $document
            $document = $null
        }
    }
    if ($null -ne $word) {
        if ($ownedWord) {
            try {
                $word.Quit($WdDoNotSaveChanges)
            }
            catch {
                # The owned process check below is the final fallback.
            }
        }
        try {
            Release-ComObject -ComObject $word
            $word = $null
        }
        catch { }
    }
    [System.GC]::Collect()
    [System.GC]::WaitForPendingFinalizers()
    [System.GC]::Collect()
    [System.GC]::WaitForPendingFinalizers()

    if ($ownedWord -and $ownedWordPid -gt 0 -and $ownedWordStartFileTimeUtc -gt 0) {
        [void] (Stop-OwnedWordProcess `
            -ProcessId $ownedWordPid `
            -StartedFileTimeUtc $ownedWordStartFileTimeUtc)
    }
    if ($null -ne $WorkingFullPath) {
        Remove-Item -LiteralPath $WorkingFullPath -Force -ErrorAction SilentlyContinue
    }
}

if ($exitCode -eq 0) {
    $sourceHashAfter = Get-Sha256 -LiteralPath $SourceFullPath
    if ($sourceHashAfter -cne $sourceHashBefore) {
        Remove-Item -LiteralPath $DestinationFullPath -Force -ErrorAction SilentlyContinue
        [Console]::Error.WriteLine("Word field refresh changed its input unexpectedly.")
        exit 1
    }
    Write-Utf8Json -Value $reportValue -LiteralPath $ReportFullPath
    exit 0
}

if ($null -ne $DestinationFullPath) {
    Remove-Item -LiteralPath $DestinationFullPath -Force -ErrorAction SilentlyContinue
}
if ($null -ne $ReportFullPath) {
    Remove-Item -LiteralPath $ReportFullPath -Force -ErrorAction SilentlyContinue
}
exit $exitCode
