[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $SourceRoot,

    [string] $ManifestDirectory,
    [string] $ReleasePlanPath,
    [string] $OutputPath,
    [switch] $AllowGaps
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Write-AtomicUtf8File {
    param([string] $Path, [string] $Content)

    $parent = Split-Path -Parent $Path
    [IO.Directory]::CreateDirectory($parent) | Out-Null
    $temporaryPath = Join-Path $parent ('.' + [IO.Path]::GetFileName($Path) + '.tmp-' + [Guid]::NewGuid().ToString('N'))
    $backupPath = $temporaryPath + '.backup'
    try {
        [IO.File]::WriteAllText($temporaryPath, $Content, [Text.UTF8Encoding]::new($false))
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            [IO.File]::Replace($temporaryPath, $Path, $backupPath)
        }
        else {
            [IO.File]::Move($temporaryPath, $Path)
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
            try { [IO.File]::Delete($temporaryPath) }
            catch { Write-Warning "임시 보고서 정리에 실패했습니다: $temporaryPath" }
        }
        if (Test-Path -LiteralPath $backupPath -PathType Leaf) {
            try { [IO.File]::Delete($backupPath) }
            catch { Write-Warning "임시 보고서 백업 정리에 실패했습니다: $backupPath" }
        }
    }
}


function Get-ChildPath {
    param([string] $Root, [string] $RelativePath, [string] $Label)

    if ([IO.Path]::IsPathRooted($RelativePath)) {
        throw "$Label 경로는 상대 경로여야 합니다: $RelativePath"
    }
    $normalized = $RelativePath.Replace('\', '/')
    if ($RelativePath.Contains(':') -or $normalized -eq 'overlay-manifest.json') {
        throw "$Label 경로에 금지된 문자 또는 예약 파일명이 있습니다: $RelativePath"
    }
    foreach ($segment in $normalized.Split('/')) {
        $baseName = $segment.Split('.')[0]
        if ([string]::IsNullOrWhiteSpace($segment) -or $segment -in @('.', '..') -or
            $segment.EndsWith('.') -or $segment.EndsWith(' ') -or
            $baseName -match '(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$') {
            throw "$Label 경로가 안전하지 않습니다: $RelativePath"
        }
    }
    $rootResolved = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $rootResolved = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $candidate = [IO.Path]::GetFullPath((Join-Path $rootResolved $RelativePath))
    $prefix = $rootResolved + [IO.Path]::DirectorySeparatorChar
    if (-not $candidate.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label 경로가 허용된 루트 밖을 가리킵니다: $RelativePath"
    }
    return $candidate
}

$projectRoot = (Get-Item -LiteralPath (Join-Path $PSScriptRoot '..')).FullName
if ([string]::IsNullOrWhiteSpace($ManifestDirectory)) {
    $ManifestDirectory = Join-Path $projectRoot 'manifests'
}
if ([string]::IsNullOrWhiteSpace($ReleasePlanPath)) {
    $ReleasePlanPath = Join-Path $projectRoot 'manifests\next-release-plan.json'
}

$sourceItem = Get-Item -LiteralPath $SourceRoot -Force
if (-not $sourceItem.PSIsContainer) {
    throw "원본 경로가 디렉터리가 아닙니다: $SourceRoot"
}
$sourceRootFullPath = [IO.Path]::GetFullPath($sourceItem.FullName)
if ($sourceRootFullPath.Equals([IO.Path]::GetPathRoot($sourceRootFullPath), [StringComparison]::OrdinalIgnoreCase)) {
    throw '파일시스템 루트는 원본 경로로 허용하지 않습니다.'
}
$sourceRootResolved = $sourceRootFullPath.TrimEnd('\', '/')
$reports = [System.Collections.Generic.List[object]]::new()
$coverageIssues = [System.Collections.Generic.List[object]]::new()
$totalGaps = 0

foreach ($manifestName in @('legacy-main.json', 'legacy-dlc.json')) {
    $manifestPath = Get-ChildPath -Root $ManifestDirectory -RelativePath $manifestName -Label '매니페스트'
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $originalRoot = Get-ChildPath -Root $sourceRootResolved -RelativePath ([string] $manifest.roots.original).Replace('/', '\') -Label '원본 루트'
    $localizedRoot = Get-ChildPath -Root $sourceRootResolved -RelativePath ([string] $manifest.roots.localized).Replace('/', '\') -Label '한글화 루트'
    $deltaRoot = Get-ChildPath -Root $sourceRootResolved -RelativePath ([string] $manifest.roots.delta).Replace('/', '\') -Label '델타 루트'
    foreach ($directory in @($originalRoot, $localizedRoot, $deltaRoot)) {
        if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
            throw "매니페스트 루트가 디렉터리가 아닙니다: $directory"
        }
    }

    $manifestTargets = @{}
    $manifestDeltas = @{}
    $targetTopLevels = @{}
    foreach ($entry in $manifest.entries) {
        $manifestTargets[([string] $entry.targetPath).ToLowerInvariant()] = $entry
        $manifestDeltas[([string] $entry.deltaFile).ToLowerInvariant()] = $entry
        $topLevel = (([string] $entry.targetPath).Replace('\', '/').Split('/')[0]).ToLowerInvariant()
        $targetTopLevels[$topLevel] = $true
    }

    $changed = [System.Collections.Generic.List[string]]::new()
    $localizedWithoutOriginal = [System.Collections.Generic.List[string]]::new()
    $comparableLookup = @{}
    foreach ($localizedFile in Get-ChildItem -LiteralPath $localizedRoot -Recurse -File -Force) {
        $relative = $localizedFile.FullName.Substring($localizedRoot.Length).TrimStart('\', '/').Replace('\', '/')
        $originalPath = Get-ChildPath -Root $originalRoot -RelativePath $relative.Replace('/', '\') -Label '원본 대응 파일'
        if (-not (Test-Path -LiteralPath $originalPath -PathType Leaf)) {
            $localizedWithoutOriginal.Add($relative)
            continue
        }
        $comparableLookup[$relative.ToLowerInvariant()] = $true

        $originalFile = Get-Item -LiteralPath $originalPath -Force
        $isChanged = $originalFile.Length -ne $localizedFile.Length
        if (-not $isChanged) {
            $originalHash = (Get-FileHash -LiteralPath $originalFile.FullName -Algorithm SHA256).Hash
            $localizedHash = (Get-FileHash -LiteralPath $localizedFile.FullName -Algorithm SHA256).Hash
            $isChanged = -not $originalHash.Equals($localizedHash, [StringComparison]::OrdinalIgnoreCase)
        }
        if ($isChanged) {
            $changed.Add($relative)
        }
    }

    $changedLookup = @{}
    foreach ($path in $changed) {
        $changedLookup[$path.ToLowerInvariant()] = $true
    }

    $changedNotManifested = @(
        $changed |
            Where-Object { -not $manifestTargets.ContainsKey($_.ToLowerInvariant()) } |
            Sort-Object
    )
    $manifestedButUnchanged = @(
        $manifest.entries |
            Where-Object {
                $key = ([string] $_.targetPath).ToLowerInvariant()
                $comparableLookup.ContainsKey($key) -and -not $changedLookup.ContainsKey($key)
            } |
            ForEach-Object { [string] $_.targetPath } |
            Sort-Object
    )

    $missingInputs = [System.Collections.Generic.List[string]]::new()
    foreach ($entry in $manifest.entries) {
        $relative = ([string] $entry.targetPath).Replace('/', '\')
        $originalPath = Get-ChildPath -Root $originalRoot -RelativePath $relative -Label '원본 파일'
        $localizedPath = Get-ChildPath -Root $localizedRoot -RelativePath $relative -Label '한글화 파일'
        $deltaPath = Get-ChildPath -Root $deltaRoot -RelativePath ([string] $entry.deltaFile).Replace('/', '\') -Label '델타 파일'
        if (-not (Test-Path -LiteralPath $originalPath -PathType Leaf)) {
            $missingInputs.Add("original::$($entry.targetPath)")
        }
        if (-not (Test-Path -LiteralPath $localizedPath -PathType Leaf)) {
            $missingInputs.Add("localized::$($entry.targetPath)")
        }
        if (-not (Test-Path -LiteralPath $deltaPath -PathType Leaf)) {
            $missingInputs.Add("delta::$($entry.deltaFile)")
        }
    }

    $orphanDeltaFiles = @(
        Get-ChildItem -LiteralPath $deltaRoot -Recurse -File -Force -Filter '*.xdelta' |
            ForEach-Object { $_.FullName.Substring($deltaRoot.Length).TrimStart('\', '/').Replace('\', '/') } |
            Where-Object { -not $manifestDeltas.ContainsKey($_.ToLowerInvariant()) } |
            Sort-Object
    )
    $localizedOnlyGameCandidates = @(
        $localizedWithoutOriginal |
            Where-Object {
                $topLevel = ($_.Replace('\', '/').Split('/')[0]).ToLowerInvariant()
                $targetTopLevels.ContainsKey($topLevel)
            } |
            Sort-Object
    )
    $localizedOnlyCandidateLookup = @{}
    foreach ($path in $localizedOnlyGameCandidates) {
        $localizedOnlyCandidateLookup[$path.ToLowerInvariant()] = $true
    }
    $localizedOnlyAncillary = @(
        $localizedWithoutOriginal |
            Where-Object { -not $localizedOnlyCandidateLookup.ContainsKey($_.ToLowerInvariant()) } |
            Sort-Object
    )

    $report = [ordered]@{
        manifest              = [string] $manifest.name
        comparableFiles       = $comparableLookup.Count
        changedFiles          = $changed.Count
        manifestEntries       = @($manifest.entries).Count
        changedNotManifested  = $changedNotManifested
        manifestedButUnchanged = $manifestedButUnchanged
        localizedOnlyGameCandidates = $localizedOnlyGameCandidates
        localizedOnlyAncillary = $localizedOnlyAncillary
        orphanDeltaFiles      = $orphanDeltaFiles
        missingInputs         = $missingInputs
    }
    $reports.Add($report)
    foreach ($path in $changedNotManifested) {
        $coverageIssues.Add([ordered]@{ manifest = [string] $manifest.name; kind = 'changedNotManifested'; path = [string] $path })
    }
    foreach ($path in $localizedOnlyGameCandidates) {
        $coverageIssues.Add([ordered]@{ manifest = [string] $manifest.name; kind = 'localizedOnlyGameCandidate'; path = [string] $path })
    }
    foreach ($path in $orphanDeltaFiles) {
        $coverageIssues.Add([ordered]@{ manifest = [string] $manifest.name; kind = 'orphanDeltaFile'; path = [string] $path })
    }
    foreach ($path in $missingInputs) {
        $coverageIssues.Add([ordered]@{ manifest = [string] $manifest.name; kind = 'missingInput'; path = [string] $path })
    }
    $totalGaps += $changedNotManifested.Count + $localizedOnlyGameCandidates.Count + $orphanDeltaFiles.Count + $missingInputs.Count

    Write-Host ("[검사] {0}: 변경 {1}, 매니페스트 {2}, 누락 변경 {3}, 원본 없는 게임 후보 {4}, 고아 델타 {5}, 무변경 패치 {6}" -f $manifest.name, $changed.Count, @($manifest.entries).Count, $changedNotManifested.Count, $localizedOnlyGameCandidates.Count, $orphanDeltaFiles.Count, $manifestedButUnchanged.Count)
}

$output = [ordered]@{
    schemaVersion = 2
    reports       = $reports
    issues        = $coverageIssues
}
if (-not [string]::IsNullOrWhiteSpace($OutputPath)) {
    $fullOutputPath = [IO.Path]::GetFullPath($OutputPath)
    $sourcePrefix = $sourceRootResolved + [IO.Path]::DirectorySeparatorChar
    if ($fullOutputPath.StartsWith($sourcePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw '보고서 출력 경로는 원본 아카이브 밖이어야 합니다.'
    }
    $json = $output | ConvertTo-Json -Depth 8
    $json = $json.Replace("`r`n", "`n").Replace("`r", "`n")
    Write-AtomicUtf8File -Path $fullOutputPath -Content ($json + "`n")
    Write-Host "[생성] $fullOutputPath"
}

if ($AllowGaps) {
    $releasePlan = Get-Content -LiteralPath $ReleasePlanPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $expectedIssueKeys = @(
        $releasePlan.expectedValidationExceptions.manifestCoverage |
            ForEach-Object { "{0}::{1}::{2}" -f $_.manifest, $_.kind, $_.path } |
            Sort-Object -Unique
    )
    $actualIssueKeys = @(
        $coverageIssues |
            ForEach-Object { "{0}::{1}::{2}" -f $_['manifest'], $_['kind'], $_['path'] } |
            Sort-Object -Unique
    )
    $issueDifference = @()
    if ($expectedIssueKeys.Count -gt 0 -or $actualIssueKeys.Count -gt 0) {
        $issueDifference = @(Compare-Object -ReferenceObject @($expectedIssueKeys) -DifferenceObject @($actualIssueKeys))
    }
    if ($expectedIssueKeys.Count -ne $actualIssueKeys.Count -or $issueDifference.Count -gt 0) {
        throw '실제 커버리지 예외 집합이 next-release-plan.json의 예상 집합과 정확히 일치하지 않습니다.'
    }
}

if ($totalGaps -gt 0) {
    Write-Warning "매니페스트에서 빠진 변경/입력 누락이 $totalGaps 건 있습니다."
    if (-not $AllowGaps) {
        throw '매니페스트 커버리지 검사 실패'
    }
    Write-Host '[통과] 누락 집합이 릴리스 계획에 고정된 예상 예외와 정확히 일치합니다.'
}
