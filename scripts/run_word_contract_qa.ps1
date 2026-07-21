#Requires -Version 5.1

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $BaselineDocx,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $OutputDir
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$WdAlertsNone = 0
$WdDoNotSaveChanges = 0
$WdFormatXmlDocument = 12
$MsoAutomationSecurityForceDisable = 3

$ExtendedPathPrefix = "\\?\"
$DevicePathPrefix = "\\.\"
$NtPathPrefix = "\??\"

function Get-NormalWin32FullPath {
    param(
        [Parameter(Mandatory = $true)]
        [string] $LiteralPath,

        [Parameter(Mandatory = $true)]
        [string] $Description
    )

    foreach ($prefix in @($ExtendedPathPrefix, $DevicePathPrefix, $NtPathPrefix)) {
        if ($LiteralPath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "$Description must use an ordinary Win32 path, not an extended or device path."
        }
    }

    $fullPath = [System.IO.Path]::GetFullPath($LiteralPath)
    foreach ($prefix in @($ExtendedPathPrefix, $DevicePathPrefix, $NtPathPrefix)) {
        if ($fullPath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "$Description resolved to an extended or device path; pass an ordinary Win32 path."
        }
    }
    return $fullPath
}

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

function Wait-ForOwnedWinWordIdentity {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [object[]] $BeforeSnapshot
    )

    $beforeKeys = @{}
    foreach ($identity in $BeforeSnapshot) {
        $beforeKeys[(Get-WinWordIdentityKey -Identity $identity)] = $true
    }

    for ($attempt = 1; $attempt -le 50; $attempt++) {
        $newIdentities = @(
            Get-WinWordProcessSnapshot | Where-Object {
                -not $beforeKeys.ContainsKey((Get-WinWordIdentityKey -Identity $_))
            }
        )
        if ($newIdentities.Count -eq 1) {
            return $newIdentities[0]
        }
        if ($newIdentities.Count -gt 1) {
            throw "More than one new WINWORD process appeared; ownership is ambiguous."
        }
        Start-Sleep -Milliseconds 100
    }

    throw "No unique new WINWORD process could be attributed to this QA run."
}

function Open-ExactWinWordProcess {
    param(
        [Parameter(Mandatory = $true)]
        [object] $Identity
    )

    $process = $null
    try {
        $process = [System.Diagnostics.Process]::GetProcessById([int] $Identity.pid)
        $started = [long] $process.StartTime.ToUniversalTime().ToFileTimeUtc()
        if ($started -ne [long] $Identity.started_filetime_utc) {
            $process.Dispose()
            return $null
        }
        return $process
    }
    catch [System.ArgumentException] {
        if ($null -ne $process) {
            $process.Dispose()
        }
        return $null
    }
    catch [System.InvalidOperationException] {
        if ($null -ne $process) {
            $process.Dispose()
        }
        return $null
    }
    catch {
        if ($null -ne $process) {
            $process.Dispose()
        }
        throw "Unable to verify the owned WINWORD process identity."
    }
}

function Get-Sha256 {
    param(
        [Parameter(Mandatory = $true)]
        [string] $LiteralPath
    )

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
            # Word may already have disconnected an RCW during Close/Quit. Cleanup must continue.
        }
    }
}

function Invoke-ComGarbageCollection {
    [System.GC]::Collect()
    [System.GC]::WaitForPendingFinalizers()
    [System.GC]::Collect()
    [System.GC]::WaitForPendingFinalizers()
}

function Get-WordCollectionCount {
    param(
        [Parameter(Mandatory = $true)]
        [object] $Document,

        [Parameter(Mandatory = $true)]
        [ValidateSet("Bookmarks", "Comments", "Revisions")]
        [string] $CollectionName
    )

    $collection = $null
    try {
        switch ($CollectionName) {
            "Bookmarks" {
                $collection = $Document.Bookmarks
                $collection.ShowHidden = $true
            }
            "Comments" {
                $collection = $Document.Comments
            }
            "Revisions" {
                $collection = $Document.Revisions
            }
        }
        return [int] $collection.Count
    }
    finally {
        Release-ComObject -ComObject $collection
    }
}

function Add-WordComment {
    param(
        [Parameter(Mandatory = $true)]
        [object] $Document,

        [Parameter(Mandatory = $true)]
        [object] $Range,

        [Parameter(Mandatory = $true)]
        [string] $Text
    )

    $comments = $null
    try {
        $comments = $Document.Comments
        return $comments.Add($Range, $Text)
    }
    finally {
        Release-ComObject -ComObject $comments
    }
}

function Get-WordBookmark {
    param(
        [Parameter(Mandatory = $true)]
        [object] $Document,

        [Parameter(Mandatory = $true)]
        [int] $Index
    )

    $bookmarks = $null
    try {
        $bookmarks = $Document.Bookmarks
        $bookmarks.ShowHidden = $true
        return $bookmarks.Item($Index)
    }
    finally {
        Release-ComObject -ComObject $bookmarks
    }
}

function Wait-ForExactWinWordExit {
    param(
        [Parameter(Mandatory = $true)]
        [object] $Identity,

        [Parameter(Mandatory = $false)]
        [int] $TimeoutMilliseconds = 5000
    )

    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        while ($timer.ElapsedMilliseconds -lt $TimeoutMilliseconds) {
            $process = Open-ExactWinWordProcess -Identity $Identity
            if ($null -eq $process) {
                return $true
            }
            try {
                if ($process.HasExited) {
                    return $true
                }
            }
            finally {
                $process.Dispose()
            }
            Start-Sleep -Milliseconds 100
        }

        $process = Open-ExactWinWordProcess -Identity $Identity
        if ($null -eq $process) {
            return $true
        }
        $process.Dispose()
        return $false
    }
    finally {
        $timer.Stop()
    }
}

function Stop-ExactOwnedWinWordProcess {
    param(
        [Parameter(Mandatory = $true)]
        [object] $Identity
    )

    $process = Open-ExactWinWordProcess -Identity $Identity
    if ($null -eq $process) {
        return
    }
    try {
        $process.Kill()
        if (-not $process.WaitForExit(5000)) {
            throw "The owned WINWORD process did not exit after exact termination."
        }
    }
    finally {
        $process.Dispose()
    }
}

function Close-OwnedWordApplication {
    param(
        [Parameter(Mandatory = $false)]
        [AllowNull()]
        [object] $Word,

        [Parameter(Mandatory = $false)]
        [AllowNull()]
        [object] $Identity,

        [Parameter(Mandatory = $true)]
        [bool] $OwnershipConfirmed
    )

    if ($null -eq $Word) {
        return
    }

    if (-not $OwnershipConfirmed -or $null -eq $Identity) {
        Release-ComObject -ComObject $Word
        Invoke-ComGarbageCollection
        return
    }

    $ownedProcess = Open-ExactWinWordProcess -Identity $Identity
    if ($null -eq $ownedProcess) {
        Release-ComObject -ComObject $Word
        Invoke-ComGarbageCollection
        return
    }
    $ownedProcess.Dispose()

    try {
        $Word.Quit($WdDoNotSaveChanges)
    }
    catch {
        # Exact-process termination below is the bounded fallback for our own instance only.
    }
    finally {
        Release-ComObject -ComObject $Word
        Invoke-ComGarbageCollection
    }

    if (-not (Wait-ForExactWinWordExit -Identity $Identity)) {
        Stop-ExactOwnedWinWordProcess -Identity $Identity
    }
    if (-not (Wait-ForExactWinWordExit -Identity $Identity -TimeoutMilliseconds 1000)) {
        throw "The exactly owned WINWORD process could not be stopped safely."
    }
}

function Close-WordDocument {
    param(
        [Parameter(Mandatory = $false)]
        [AllowNull()]
        [object] $Document
    )

    if ($null -eq $Document) {
        return
    }

    try {
        $Document.Close($WdDoNotSaveChanges)
    }
    catch {
        # The outer Word.Quit call is the final cleanup boundary.
    }
    finally {
        Release-ComObject -ComObject $Document
    }
}

function Get-EditableTextSpan {
    param(
        [Parameter(Mandatory = $true)]
        [object] $Document
    )

    $content = $null
    try {
        $content = $Document.Content
        $text = [string] $content.Text
        # Word control characters include paragraph/cell marks. The contract edits only an
        # ordinary, contiguous run so that no structure is accidentally removed.
        $match = [System.Text.RegularExpressions.Regex]::Match(
            $text,
            "[^\u0000-\u001f]{12,}"
        )
        if (-not $match.Success) {
            throw "The synthetic baseline has no editable ordinary-text run of 12 characters."
        }

        return [pscustomobject]@{
            Start = [int] $content.Start + [int] $match.Index
            Text = [string] $match.Value
        }
    }
    finally {
        Release-ComObject -ComObject $content
    }
}

function Open-WordWorkingCopy {
    param(
        [Parameter(Mandatory = $true)]
        [object] $Word,

        [Parameter(Mandatory = $true)]
        [string] $BaselineFullPath,

        [Parameter(Mandatory = $true)]
        [string] $WorkingPath
    )

    if (Test-Path -LiteralPath $WorkingPath) {
        throw "A private working copy already exists."
    }
    Copy-Item -LiteralPath $BaselineFullPath -Destination $WorkingPath

    $documents = $null
    try {
        $documents = $Word.Documents
        # FileName, ConfirmConversions, ReadOnly, AddToRecentFiles.
        return $documents.Open($WorkingPath, $false, $false, $false)
    }
    finally {
        Release-ComObject -ComObject $documents
    }
}

function Save-AsDocx {
    param(
        [Parameter(Mandatory = $true)]
        [object] $Document,

        [Parameter(Mandatory = $true)]
        [string] $Destination
    )

    if (Test-Path -LiteralPath $Destination) {
        throw "A contract-case destination already exists."
    }
    $Document.SaveAs2(
        $Destination,
        $WdFormatXmlDocument,
        [System.Type]::Missing,
        [System.Type]::Missing,
        $false
    )
}

function Publish-StageDirectory {
    param(
        [Parameter(Mandatory = $true)] [string] $StageRoot,
        [Parameter(Mandatory = $true)] [string] $OutputFullPath
    )

    # Word or an Office add-in can release the final package handle shortly after Quit returns.
    # Keep the wait bounded, and re-check the no-clobber invariant before every atomic attempt.
    for ($attempt = 1; $attempt -le 30; $attempt++) {
        if (Test-Path -LiteralPath $OutputFullPath) {
            throw "The output directory appeared during publish; refusing to overwrite it."
        }
        try {
            [System.IO.Directory]::Move($StageRoot, $OutputFullPath)
            return
        }
        catch {
            if ($attempt -eq 30) {
                throw
            }
            Start-Sleep -Milliseconds 100
        }
    }
}

function Invoke-RoundtripCase {
    param(
        [Parameter(Mandatory = $true)] [object] $Word,
        [Parameter(Mandatory = $true)] [string] $BaselineFullPath,
        [Parameter(Mandatory = $true)] [string] $WorkingPath,
        [Parameter(Mandatory = $true)] [string] $Destination
    )

    $document = $null
    try {
        $document = Open-WordWorkingCopy $Word $BaselineFullPath $WorkingPath
        if ((Get-WordCollectionCount -Document $document -CollectionName "Revisions") -ne 0) {
            throw "The synthetic baseline must not contain pre-existing revisions."
        }
        if ((Get-WordCollectionCount -Document $document -CollectionName "Bookmarks") -lt 1) {
            throw "The synthetic baseline must contain at least one bookmark."
        }
        [void] (Get-EditableTextSpan -Document $document)

        $bookmarkCount = Get-WordCollectionCount -Document $document -CollectionName "Bookmarks"
        Save-AsDocx -Document $document -Destination $Destination

        return [pscustomobject]@{
            revision_count = Get-WordCollectionCount -Document $document -CollectionName "Revisions"
            comment_count = Get-WordCollectionCount -Document $document -CollectionName "Comments"
            bookmark_count = $bookmarkCount
        }
    }
    finally {
        Close-WordDocument -Document $document
    }
}

function Invoke-TrackedReviewCase {
    param(
        [Parameter(Mandatory = $true)] [object] $Word,
        [Parameter(Mandatory = $true)] [string] $BaselineFullPath,
        [Parameter(Mandatory = $true)] [string] $WorkingPath,
        [Parameter(Mandatory = $true)] [string] $Destination
    )

    $document = $null
    $replacementRange = $null
    $deletionRange = $null
    $insertionRange = $null
    $commentRange = $null
    $comment = $null
    try {
        $document = Open-WordWorkingCopy $Word $BaselineFullPath $WorkingPath
        if ((Get-WordCollectionCount -Document $document -CollectionName "Revisions") -ne 0) {
            throw "The synthetic baseline must not contain pre-existing revisions."
        }

        $span = Get-EditableTextSpan -Document $document
        $document.TrackRevisions = $true

        # Work right-to-left. Replacement is expressed as deletion plus insertion by Word,
        # deletion removes one ordinary character, and insertion is zero-width.
        $replacementRange = $document.Range([int] $span.Start + 8, [int] $span.Start + 9)
        $replacementRange.Text = "[LWR-REPLACE]"
        Release-ComObject -ComObject $replacementRange
        $replacementRange = $null

        $deletionRange = $document.Range([int] $span.Start + 5, [int] $span.Start + 6)
        [void] $deletionRange.Delete()
        Release-ComObject -ComObject $deletionRange
        $deletionRange = $null

        $insertionRange = $document.Range([int] $span.Start + 2, [int] $span.Start + 2)
        $insertionRange.InsertBefore("[LWR-INSERT]")
        Release-ComObject -ComObject $insertionRange
        $insertionRange = $null

        $commentRange = $document.Range([int] $span.Start, [int] $span.Start + 1)
        $comment = Add-WordComment -Document $document -Range $commentRange -Text "Synthetic contract comment."
        Release-ComObject -ComObject $comment
        $comment = $null
        Release-ComObject -ComObject $commentRange
        $commentRange = $null

        if ((Get-WordCollectionCount -Document $document -CollectionName "Revisions") -lt 3) {
            throw "Word did not retain the expected local tracked revisions."
        }
        if ((Get-WordCollectionCount -Document $document -CollectionName "Comments") -lt 1) {
            throw "Word did not retain the synthetic comment."
        }

        Save-AsDocx -Document $document -Destination $Destination
        return [pscustomobject]@{
            revision_count = Get-WordCollectionCount -Document $document -CollectionName "Revisions"
            comment_count = Get-WordCollectionCount -Document $document -CollectionName "Comments"
            bookmark_count = Get-WordCollectionCount -Document $document -CollectionName "Bookmarks"
        }
    }
    finally {
        Release-ComObject -ComObject $comment
        Release-ComObject -ComObject $commentRange
        Release-ComObject -ComObject $insertionRange
        Release-ComObject -ComObject $deletionRange
        Release-ComObject -ComObject $replacementRange
        Close-WordDocument -Document $document
    }
}

function Invoke-UntrackedDriftCase {
    param(
        [Parameter(Mandatory = $true)] [object] $Word,
        [Parameter(Mandatory = $true)] [string] $BaselineFullPath,
        [Parameter(Mandatory = $true)] [string] $WorkingPath,
        [Parameter(Mandatory = $true)] [string] $Destination
    )

    $document = $null
    $insertionRange = $null
    try {
        $document = Open-WordWorkingCopy $Word $BaselineFullPath $WorkingPath
        if ((Get-WordCollectionCount -Document $document -CollectionName "Revisions") -ne 0) {
            throw "The synthetic baseline must not contain pre-existing revisions."
        }
        $span = Get-EditableTextSpan -Document $document
        $document.TrackRevisions = $false
        $insertionRange = $document.Range([int] $span.Start + 2, [int] $span.Start + 2)
        $insertionRange.InsertBefore("[LWR-UNTRACKED-DRIFT]")
        Release-ComObject -ComObject $insertionRange
        $insertionRange = $null

        if ((Get-WordCollectionCount -Document $document -CollectionName "Revisions") -ne 0) {
            throw "The untracked-drift negative control unexpectedly contains revisions."
        }

        Save-AsDocx -Document $document -Destination $Destination
        return [pscustomobject]@{
            revision_count = Get-WordCollectionCount -Document $document -CollectionName "Revisions"
            comment_count = Get-WordCollectionCount -Document $document -CollectionName "Comments"
            bookmark_count = Get-WordCollectionCount -Document $document -CollectionName "Bookmarks"
        }
    }
    finally {
        Release-ComObject -ComObject $insertionRange
        Close-WordDocument -Document $document
    }
}

function Invoke-AcceptAllCase {
    param(
        [Parameter(Mandatory = $true)] [object] $Word,
        [Parameter(Mandatory = $true)] [string] $BaselineFullPath,
        [Parameter(Mandatory = $true)] [string] $WorkingPath,
        [Parameter(Mandatory = $true)] [string] $Destination
    )

    $document = $null
    $insertionRange = $null
    try {
        $document = Open-WordWorkingCopy $Word $BaselineFullPath $WorkingPath
        if ((Get-WordCollectionCount -Document $document -CollectionName "Revisions") -ne 0) {
            throw "The synthetic baseline must not contain pre-existing revisions."
        }
        $span = Get-EditableTextSpan -Document $document
        $document.TrackRevisions = $true
        $insertionRange = $document.Range([int] $span.Start + 2, [int] $span.Start + 2)
        $insertionRange.InsertBefore("[LWR-ACCEPTED-ALL]")
        Release-ComObject -ComObject $insertionRange
        $insertionRange = $null

        if ((Get-WordCollectionCount -Document $document -CollectionName "Revisions") -lt 1) {
            throw "Word did not create the acceptance negative control."
        }
        $document.AcceptAllRevisions()
        if ((Get-WordCollectionCount -Document $document -CollectionName "Revisions") -ne 0) {
            throw "Word did not accept every revision in the negative control."
        }

        Save-AsDocx -Document $document -Destination $Destination
        return [pscustomobject]@{
            revision_count = Get-WordCollectionCount -Document $document -CollectionName "Revisions"
            comment_count = Get-WordCollectionCount -Document $document -CollectionName "Comments"
            bookmark_count = Get-WordCollectionCount -Document $document -CollectionName "Bookmarks"
        }
    }
    finally {
        Release-ComObject -ComObject $insertionRange
        Close-WordDocument -Document $document
    }
}

function Invoke-BrokenBookmarkCase {
    param(
        [Parameter(Mandatory = $true)] [object] $Word,
        [Parameter(Mandatory = $true)] [string] $BaselineFullPath,
        [Parameter(Mandatory = $true)] [string] $WorkingPath,
        [Parameter(Mandatory = $true)] [string] $Destination,
        [Parameter(Mandatory = $true)] [int] $ExpectedBookmarkCount
    )

    $document = $null
    $bookmark = $null
    try {
        $document = Open-WordWorkingCopy $Word $BaselineFullPath $WorkingPath
        if ((Get-WordCollectionCount -Document $document -CollectionName "Bookmarks") -ne $ExpectedBookmarkCount) {
            throw "The working copy does not match the synthetic baseline bookmark count."
        }
        $document.TrackRevisions = $false
        $bookmark = Get-WordBookmark -Document $document -Index 1
        $bookmark.Delete()
        Release-ComObject -ComObject $bookmark
        $bookmark = $null

        if ((Get-WordCollectionCount -Document $document -CollectionName "Bookmarks") -ne ($ExpectedBookmarkCount - 1)) {
            throw "Word did not remove exactly one bookmark in the negative control."
        }

        Save-AsDocx -Document $document -Destination $Destination
        return [pscustomobject]@{
            revision_count = Get-WordCollectionCount -Document $document -CollectionName "Revisions"
            comment_count = Get-WordCollectionCount -Document $document -CollectionName "Comments"
            bookmark_count = Get-WordCollectionCount -Document $document -CollectionName "Bookmarks"
        }
    }
    finally {
        Release-ComObject -ComObject $bookmark
        Close-WordDocument -Document $document
    }
}

function New-CaseManifestEntry {
    param(
        [Parameter(Mandatory = $true)] [string] $CaseId,
        [Parameter(Mandatory = $true)] [string] $RelativeFile,
        [Parameter(Mandatory = $true)] [string] $StagedFile,
        [Parameter(Mandatory = $true)] [object] $Facts
    )

    return [ordered]@{
        case_id = $CaseId
        status = "passed"
        relative_file = $RelativeFile
        sha256 = Get-Sha256 -LiteralPath $StagedFile
        revision_count = [int] $Facts.revision_count
        comment_count = [int] $Facts.comment_count
        bookmark_count = [int] $Facts.bookmark_count
    }
}

$stageRoot = $null
$word = $null
$published = $false
$wordIdentity = $null
$wordOwnershipConfirmed = $false
$winWordBefore = @()

try {
    if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
        throw "Microsoft Word contract QA is supported only on Windows."
    }

    $BaselineFullPath = Get-NormalWin32FullPath -LiteralPath $BaselineDocx -Description "The synthetic baseline"
    $OutputFullPath = Get-NormalWin32FullPath -LiteralPath $OutputDir -Description "The output directory"

    if (-not (Test-Path -LiteralPath $BaselineFullPath -PathType Leaf)) {
        throw "The explicitly supplied synthetic baseline DOCX does not exist."
    }
    if ([System.IO.Path]::GetExtension($BaselineFullPath) -ine ".docx") {
        throw "The synthetic baseline must have a .docx extension."
    }
    if (Test-Path -LiteralPath $OutputFullPath) {
        throw "The explicitly supplied output directory already exists; refusing to overwrite it."
    }

    $outputParent = Split-Path -Parent $OutputFullPath
    if ([string]::IsNullOrWhiteSpace($outputParent) -or
        -not (Test-Path -LiteralPath $outputParent -PathType Container)) {
        throw "The parent of the explicitly supplied output directory must already exist."
    }

    $baselineHashBefore = Get-Sha256 -LiteralPath $BaselineFullPath
    $stageName = ".lwr-word-contract-{0}.stage" -f [System.Guid]::NewGuid().ToString("N")
    $stageRoot = Join-Path -Path $outputParent -ChildPath $stageName
    $workRoot = Join-Path -Path $stageRoot -ChildPath "private-working-copies"
    [void] (New-Item -ItemType Directory -Path $stageRoot)
    [void] (New-Item -ItemType Directory -Path $workRoot)

    try {
        $winWordBefore = @(Get-WinWordProcessSnapshot)
        $word = New-Object -ComObject Word.Application
        $wordIdentity = Wait-ForOwnedWinWordIdentity -BeforeSnapshot $winWordBefore
        $wordOwnershipConfirmed = $true
        # Force-disable VBA automation before any document is opened.
        $word.AutomationSecurity = $MsoAutomationSecurityForceDisable
        $word.DisplayAlerts = $WdAlertsNone
        $word.Visible = $false
        $wordVersion = [string] $word.Version

        $roundtripFile = "roundtrip-unchanged.docx"
        $trackedFile = "tracked-review.docx"
        $untrackedFile = "negative-untracked-drift.docx"
        $acceptedFile = "negative-accepted-all.docx"
        $bookmarkFile = "negative-broken-bookmark.docx"

        $roundtripFacts = Invoke-RoundtripCase `
            $word `
            $BaselineFullPath `
            (Join-Path $workRoot "roundtrip-input.docx") `
            (Join-Path $stageRoot $roundtripFile)

        $trackedFacts = Invoke-TrackedReviewCase `
            $word `
            $BaselineFullPath `
            (Join-Path $workRoot "tracked-input.docx") `
            (Join-Path $stageRoot $trackedFile)

        $untrackedFacts = Invoke-UntrackedDriftCase `
            $word `
            $BaselineFullPath `
            (Join-Path $workRoot "untracked-input.docx") `
            (Join-Path $stageRoot $untrackedFile)

        $acceptedFacts = Invoke-AcceptAllCase `
            $word `
            $BaselineFullPath `
            (Join-Path $workRoot "accepted-input.docx") `
            (Join-Path $stageRoot $acceptedFile)

        $bookmarkFacts = Invoke-BrokenBookmarkCase `
            $word `
            $BaselineFullPath `
            (Join-Path $workRoot "bookmark-input.docx") `
            (Join-Path $stageRoot $bookmarkFile) `
            ([int] $roundtripFacts.bookmark_count)

        $caseEntries = @(
            (New-CaseManifestEntry `
                "roundtrip_unchanged" `
                $roundtripFile `
                (Join-Path $stageRoot $roundtripFile) `
                $roundtripFacts),
            (New-CaseManifestEntry `
                "tracked_review" `
                $trackedFile `
                (Join-Path $stageRoot $trackedFile) `
                $trackedFacts),
            (New-CaseManifestEntry `
                "negative_untracked_drift" `
                $untrackedFile `
                (Join-Path $stageRoot $untrackedFile) `
                $untrackedFacts),
            (New-CaseManifestEntry `
                "negative_accepted_all" `
                $acceptedFile `
                (Join-Path $stageRoot $acceptedFile) `
                $acceptedFacts),
            (New-CaseManifestEntry `
                "negative_broken_bookmark" `
                $bookmarkFile `
                (Join-Path $stageRoot $bookmarkFile) `
                $bookmarkFacts)
        )
    }
    finally {
        Close-OwnedWordApplication -Word $word -Identity $wordIdentity -OwnershipConfirmed $wordOwnershipConfirmed
        $word = $null
        $wordIdentity = $null
        $wordOwnershipConfirmed = $false
    }

    $baselineHashAfter = Get-Sha256 -LiteralPath $BaselineFullPath
    if ($baselineHashAfter -cne $baselineHashBefore) {
        throw "The supplied baseline changed during Word contract QA."
    }

    Remove-Item -LiteralPath $workRoot -Recurse -Force

    $manifest = [ordered]@{
        schema_version = "word-contract-qa/v1"
        status = "passed"
        baseline_sha256 = $baselineHashBefore
        word_version = $wordVersion
        cases = $caseEntries
    }
    $manifestJson = ($manifest | ConvertTo-Json -Depth 6) + [System.Environment]::NewLine
    $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText(
        (Join-Path $stageRoot "manifest.json"),
        $manifestJson,
        $utf8WithoutBom
    )

    # Same-parent Directory.Move is an atomic, no-clobber publish. Unlike Move-Item, it cannot
    # reinterpret a destination that appears in a race as a directory to move underneath.
    Publish-StageDirectory -StageRoot $stageRoot -OutputFullPath $OutputFullPath
    $published = $true
    Write-Output "Word contract QA passed; see manifest.json in the selected output directory."
}
catch {
    if (-not $published -and $null -ne $stageRoot -and (Test-Path -LiteralPath $stageRoot)) {
        Remove-Item -LiteralPath $stageRoot -Recurse -Force
    }
    Write-Error ("Word contract QA failed: {0}" -f $_.Exception.Message)
    exit 1
}

exit 0
