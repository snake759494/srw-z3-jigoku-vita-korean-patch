[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $SourceRoot,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $XdeltaPath,

    [string] $ManifestDirectory,
    [string] $ToolLockPath,
    [string] $ReleasePlanPath,
    [string] $OutputPath,
    [switch] $AllowMismatch
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
    param(
        [string] $Root,
        [string] $RelativePath,
        [string] $Label
    )

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

function Assert-ExpectedFile {
    param([string] $Path, [object] $Expected, [string] $Label)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label 파일이 없습니다: $Path"
    }
    $item = Get-Item -LiteralPath $Path -Force
    if ([long] $item.Length -ne [long] $Expected.sizeBytes) {
        throw "$Label 크기가 매니페스트와 다릅니다: $Path"
    }
    $actualHash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    if (-not $actualHash.Equals([string] $Expected.sha256, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label SHA-256이 매니페스트와 다릅니다: $Path"
    }
}

$projectRoot = (Get-Item -LiteralPath (Join-Path $PSScriptRoot '..')).FullName
if ([string]::IsNullOrWhiteSpace($ManifestDirectory)) {
    $ManifestDirectory = Join-Path $projectRoot 'manifests'
}
if ([string]::IsNullOrWhiteSpace($ToolLockPath)) {
    $ToolLockPath = Join-Path $projectRoot 'config\tools.lock.json'
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
$xdeltaItem = Get-Item -LiteralPath $XdeltaPath -Force
if ($xdeltaItem.PSIsContainer) {
    throw "xdelta 경로가 파일이 아닙니다: $XdeltaPath"
}

$toolLock = Get-Content -LiteralPath $ToolLockPath -Raw -Encoding UTF8 | ConvertFrom-Json
$actualToolHash = (Get-FileHash -LiteralPath $xdeltaItem.FullName -Algorithm SHA256).Hash
if (-not $actualToolHash.Equals([string] $toolLock.xdelta3.executableSha256, [StringComparison]::OrdinalIgnoreCase)) {
    throw "xdelta3.exe SHA-256이 config/tools.lock.json과 다릅니다. 실제=$actualToolHash"
}

$tempBase = [IO.Path]::GetFullPath((Join-Path $projectRoot '.local\tmp'))
[IO.Directory]::CreateDirectory($tempBase) | Out-Null
$tempRoot = Join-Path $tempBase ("xdelta-roundtrip-" + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($tempRoot) | Out-Null

$failures = [System.Collections.Generic.List[object]]::new()
$results = [System.Collections.Generic.List[object]]::new()
$checked = 0

try {
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

        foreach ($entry in $manifest.entries) {
            $checked++
            $targetRelative = ([string] $entry.targetPath).Replace('/', '\')
            $deltaRelative = ([string] $entry.deltaFile).Replace('/', '\')
            $originalPath = Get-ChildPath -Root $originalRoot -RelativePath $targetRelative -Label '원본 파일'
            $localizedPath = Get-ChildPath -Root $localizedRoot -RelativePath $targetRelative -Label '한글화 파일'
            $deltaPath = Get-ChildPath -Root $deltaRoot -RelativePath $deltaRelative -Label '델타 파일'
            $decodedPath = Join-Path $tempRoot ("{0:D4}.decoded" -f $checked)

            Assert-ExpectedFile -Path $originalPath -Expected $entry.original -Label '원본'
            Assert-ExpectedFile -Path $localizedPath -Expected $entry.localized -Label '한글화 기준'
            Assert-ExpectedFile -Path $deltaPath -Expected $entry.delta -Label '델타'

            $savedErrorActionPreference = $ErrorActionPreference
            try {
                $ErrorActionPreference = 'Continue'
                $toolOutput = @(& $xdeltaItem.FullName -d -f -s $originalPath $deltaPath $decodedPath 2>&1)
                $exitCode = $LASTEXITCODE
            }
            finally {
                $ErrorActionPreference = $savedErrorActionPreference
            }
            $status = 'passed'
            $actualHash = $null

            if ($exitCode -ne 0 -or -not (Test-Path -LiteralPath $decodedPath -PathType Leaf)) {
                $status = 'decode-failed'
            }
            else {
                $actualHash = (Get-FileHash -LiteralPath $decodedPath -Algorithm SHA256).Hash.ToLowerInvariant()
                if (-not $actualHash.Equals([string] $entry.localized.sha256, [StringComparison]::OrdinalIgnoreCase)) {
                    $status = 'hash-mismatch'
                }
            }

            $result = [ordered]@{
                manifest      = [string] $manifest.name
                targetPath    = [string] $entry.targetPath
                status        = $status
                expectedSha256 = [string] $entry.localized.sha256
                actualSha256  = $actualHash
            }
            $results.Add($result)
            if ($status -ne 'passed') {
                $failures.Add([ordered]@{
                    manifest      = [string] $manifest.name
                    targetPath    = [string] $entry.targetPath
                    status        = $status
                    expectedSha256 = [string] $entry.localized.sha256
                    actualSha256  = $actualHash
                    exitCode      = $exitCode
                    toolMessage   = (($toolOutput | ForEach-Object { [string] $_ }) -join ' ').Trim()
                })
            }

            if (Test-Path -LiteralPath $decodedPath -PathType Leaf) {
                [IO.File]::Delete($decodedPath)
            }
        }

        Write-Host ("[검사] {0}: {1}개 델타 디코딩" -f $manifest.name, @($manifest.entries).Count)
    }
}
finally {
    $tempRootResolved = [IO.Path]::GetFullPath($tempRoot)
    $safePrefix = $tempBase.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $tempLeaf = Split-Path -Leaf $tempRootResolved
    if ($tempRootResolved.StartsWith($safePrefix, [StringComparison]::OrdinalIgnoreCase) -and
        $tempLeaf -match '^xdelta-roundtrip-[0-9a-f]{32}$' -and
        (Test-Path -LiteralPath $tempRootResolved -PathType Container)) {
        try {
            [IO.Directory]::Delete($tempRootResolved, $true)
        }
        catch {
            Write-Warning "xdelta 임시 디렉터리 정리에 실패했습니다: $tempRootResolved"
        }
    }
}

$report = [ordered]@{
    schemaVersion = 1
    tool          = [ordered]@{
        name       = 'xdelta3'
        version    = [string] $toolLock.xdelta3.version
        sha256     = $actualToolHash.ToLowerInvariant()
        releaseUrl = [string] $toolLock.xdelta3.releaseUrl
    }
    totals        = [ordered]@{
        checked  = $checked
        passed   = @($results | Where-Object { $_['status'] -eq 'passed' }).Count
        failed   = $failures.Count
    }
    failures      = $failures
}

if (-not [string]::IsNullOrWhiteSpace($OutputPath)) {
    $fullOutputPath = [IO.Path]::GetFullPath($OutputPath)
    $sourcePrefix = $sourceRootResolved + [IO.Path]::DirectorySeparatorChar
    if ($fullOutputPath.StartsWith($sourcePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw '보고서 출력 경로는 원본 아카이브 밖이어야 합니다.'
    }
    $json = $report | ConvertTo-Json -Depth 8
    $json = $json.Replace("`r`n", "`n").Replace("`r", "`n")
    Write-AtomicUtf8File -Path $fullOutputPath -Content ($json + "`n")
    Write-Host "[생성] $fullOutputPath"
}

if ($AllowMismatch) {
    $releasePlan = Get-Content -LiteralPath $ReleasePlanPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $expectedExceptionKeys = @(
        $releasePlan.expectedValidationExceptions.xdeltaRoundTrip |
            ForEach-Object { "{0}::{1}::{2}" -f $_.manifest, $_.targetPath, $_.status } |
            Sort-Object -Unique
    )
    $actualExceptionKeys = @(
        $failures |
            ForEach-Object { "{0}::{1}::{2}" -f $_['manifest'], $_['targetPath'], $_['status'] } |
            Sort-Object -Unique
    )
    $exceptionDifference = @()
    if ($expectedExceptionKeys.Count -gt 0 -or $actualExceptionKeys.Count -gt 0) {
        $exceptionDifference = @(Compare-Object -ReferenceObject @($expectedExceptionKeys) -DifferenceObject @($actualExceptionKeys))
    }
    if ($expectedExceptionKeys.Count -ne $actualExceptionKeys.Count -or $exceptionDifference.Count -gt 0) {
        throw '실제 xdelta 예외 집합이 next-release-plan.json의 예상 집합과 정확히 일치하지 않습니다.'
    }
}

if ($failures.Count -gt 0) {
    Write-Warning ("xdelta 라운드트립 실패 {0}/{1}건" -f $failures.Count, $checked)
    foreach ($failure in $failures) {
        Write-Warning ("{0}::{1} - {2}" -f $failure['manifest'], $failure['targetPath'], $failure['status'])
    }
    if (-not $AllowMismatch) {
        throw "xdelta 라운드트립 검증 실패"
    }
    Write-Host '[통과] 실패 집합이 릴리스 계획에 고정된 예상 예외와 정확히 일치합니다.'
}
else {
    Write-Host ("[통과] {0}개 델타가 매니페스트의 한글화 SHA-256을 재현했습니다." -f $checked)
}
