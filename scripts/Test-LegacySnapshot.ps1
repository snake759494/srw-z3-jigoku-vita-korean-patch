[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $SourceRoot,

    [string] $ManifestDirectory,
    [string] $ReleasePlanPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ([string]::IsNullOrWhiteSpace($ManifestDirectory)) {
    $ManifestDirectory = Join-Path $PSScriptRoot '..\manifests'
}
if ([string]::IsNullOrWhiteSpace($ReleasePlanPath)) {
    $ReleasePlanPath = Join-Path $PSScriptRoot '..\manifests\next-release-plan.json'
}

$rootItem = Get-Item -LiteralPath $SourceRoot -Force
if (-not $rootItem.PSIsContainer) {
    throw "디렉터리가 아닙니다: $SourceRoot"
}
$rootFullPath = [IO.Path]::GetFullPath($rootItem.FullName)
if ($rootFullPath.Equals([IO.Path]::GetPathRoot($rootFullPath), [StringComparison]::OrdinalIgnoreCase)) {
    throw '파일시스템 루트는 원본 경로로 허용하지 않습니다.'
}
$root = $rootFullPath.TrimEnd('\', '/')
$failures = [System.Collections.Generic.List[string]]::new()
$checkedFiles = 0
$newerThanDelta = [System.Collections.Generic.List[string]]::new()
$unchangedTargets = [System.Collections.Generic.List[string]]::new()
$releasePlan = Get-Content -LiteralPath $ReleasePlanPath -Raw -Encoding UTF8 | ConvertFrom-Json

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

function Test-OneFile {
    param(
        [string] $Label,
        [string] $Path,
        [object] $Expected
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        $failures.Add("[$Label] 파일 없음: $Path")
        return
    }

    $item = Get-Item -LiteralPath $Path -Force
    if ([long] $item.Length -ne [long] $Expected.sizeBytes) {
        $failures.Add("[$Label] 크기 불일치: $Path")
        return
    }

    $actualHash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    if (-not $actualHash.Equals([string] $Expected.sha256, [StringComparison]::OrdinalIgnoreCase)) {
        $failures.Add("[$Label] SHA-256 불일치: $Path")
    }
}

foreach ($manifestName in @('legacy-main.json', 'legacy-dlc.json')) {
    $manifestPath = Get-ChildPath -Root $ManifestDirectory -RelativePath $manifestName -Label '매니페스트'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        $failures.Add("매니페스트 없음: $manifestPath")
        continue
    }

    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $baseline = @($releasePlan.baselineManifests | Where-Object { [string] $_.path -eq $manifestName })
    if ($baseline.Count -ne 1) {
        $failures.Add("[$manifestName] 릴리스 계획에서 고정 기준을 유일하게 찾지 못했습니다.")
    }
    else {
        $manifestHash = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash
        if (-not $manifestHash.Equals([string] $baseline[0].sha256, [StringComparison]::OrdinalIgnoreCase)) {
            $failures.Add("[$manifestName] 기준 매니페스트 SHA-256이 next-release-plan.json과 다릅니다.")
        }
    }

    $originalRoot = Get-ChildPath -Root $root -RelativePath ([string] $manifest.roots.original).Replace('/', '\') -Label '원본 루트'
    $localizedRoot = Get-ChildPath -Root $root -RelativePath ([string] $manifest.roots.localized).Replace('/', '\') -Label '한글화 루트'
    $deltaRoot = Get-ChildPath -Root $root -RelativePath ([string] $manifest.roots.delta).Replace('/', '\') -Label '델타 루트'

    $entryCount = @($manifest.entries).Count
    if ($entryCount -ne [int] $manifest.counts.total) {
        $failures.Add("[$manifestName] counts.total과 entries 개수가 다릅니다.")
    }

    foreach ($entry in $manifest.entries) {
        $targetPath = ([string] $entry.targetPath).Replace('/', '\')
        $deltaPath = ([string] $entry.deltaFile).Replace('/', '\')

        $originalPath = Get-ChildPath -Root $originalRoot -RelativePath $targetPath -Label '원본 파일'
        $localizedPath = Get-ChildPath -Root $localizedRoot -RelativePath $targetPath -Label '한글화 파일'
        $deltaFilePath = Get-ChildPath -Root $deltaRoot -RelativePath $deltaPath -Label '델타 파일'

        Test-OneFile -Label "$manifestName original" -Path $originalPath -Expected $entry.original
        Test-OneFile -Label "$manifestName localized" -Path $localizedPath -Expected $entry.localized
        Test-OneFile -Label "$manifestName delta" -Path $deltaFilePath -Expected $entry.delta
        $checkedFiles += 3

        if ([string] $entry.deltaProvenance.status -eq 'localized-newer-than-delta') {
            $newerThanDelta.Add("$manifestName::$($entry.targetPath)")
        }
        if (-not [bool] $entry.contentChanged) {
            $unchangedTargets.Add("$manifestName::$($entry.targetPath)")
        }
    }

    Write-Host ("[검사] {0}: {1}개 패치 항목" -f $manifest.name, $entryCount)
}

$credentialCandidates = @(
    Get-ChildItem -LiteralPath $root -Recurse -File -Force |
        Where-Object {
            $_.Name -match '(?i)(credential|secret|service.?account|^gen-lang-client.*\.json$)' -or
            $_.Extension -in @('.pem', '.p12', '.pfx', '.key')
        }
)
if ($credentialCandidates.Count -gt 0) {
    Write-Warning ("자격 증명 후보 {0}개가 원본 아카이브에 있습니다. 내용은 검사하지 않았으며 저장소에 복사하면 안 됩니다." -f $credentialCandidates.Count)
}

if ($newerThanDelta.Count -gt 0) {
    Write-Warning ("한글화 결과가 델타보다 나중에 수정된 항목 {0}개: {1}. 공식 xdelta로 라운드트립을 검증하거나 델타를 다시 만들어야 합니다." -f $newerThanDelta.Count, ($newerThanDelta -join ', '))
}
if ($unchangedTargets.Count -gt 0) {
    Write-Warning ("원본과 한글화 결과의 해시가 같은 패치 항목 {0}개가 있습니다. 새 빌드에서는 제외 가능 여부를 검토하세요." -f $unchangedTargets.Count)
}

if ($failures.Count -gt 0) {
    foreach ($failure in $failures) {
        [Console]::Error.WriteLine("[실패] $failure")
    }
    throw "스냅숏 검증 실패: $($failures.Count)건"
}

Write-Host ("[통과] {0}개 파일 스냅숏의 크기와 SHA-256이 기준과 일치합니다." -f $checkedFiles)
Write-Host '[주의] xdelta 디코딩 결과가 localized 해시와 같은지는 아직 라운드트립 검증하지 않았습니다.'
