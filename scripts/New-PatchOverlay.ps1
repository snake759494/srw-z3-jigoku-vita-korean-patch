[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $GameRoot,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $PatchRoot,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $ManifestPath,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $XdeltaPath,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $OutputDirectory,

    [string] $ToolLockPath,

    [ValidateSet('verification', 'release')]
    [string] $Purpose = 'verification',

    [ValidateSet('vita3k', 'hardware')]
    [string] $ReleaseTrack = 'vita3k',

    [string] $ReleasePlanPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Get-DirectoryPath {
    param([string] $Path, [string] $Label)
    $item = Get-Item -LiteralPath $Path -Force
    if (-not $item.PSIsContainer) {
        throw "$Label 경로가 디렉터리가 아닙니다: $Path"
    }
    $fullPath = [IO.Path]::GetFullPath($item.FullName)
    if ($fullPath.Equals([IO.Path]::GetPathRoot($fullPath), [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label 경로로 파일시스템 루트를 허용하지 않습니다."
    }
    return $fullPath.TrimEnd('\', '/')
}

function Assert-ChildPath {
    param([string] $Root, [string] $Path)
    $prefix = $Root.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    if (-not $Path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "허용된 출력 경로 밖을 가리킵니다: $Path"
    }
}

function Test-ExpectedFile {
    param([string] $Path, [object] $Expected, [string] $Label)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label 파일이 없습니다: $Path"
    }
    $item = Get-Item -LiteralPath $Path -Force
    if ([long] $item.Length -ne [long] $Expected.sizeBytes) {
        throw "$Label 크기가 매니페스트와 다릅니다: $Path"
    }
    $hash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    if (-not $hash.Equals([string] $Expected.sha256, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label SHA-256이 매니페스트와 다릅니다: $Path"
    }

    return [pscustomobject][ordered]@{
        sizeBytes = [long] $item.Length
        sha256    = $hash.ToLowerInvariant()
    }
}

function Get-RequiredPropertyValue {
    param(
        [object] $Object,
        [string] $Name,
        [string] $Label
    )

    if ($null -eq $Object) {
        throw "$Label 개체가 없습니다."
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property -or $null -eq $property.Value) {
        throw "$Label 필수 속성이 없습니다: $Name"
    }
    return $property.Value
}

function Get-ValidatedNonNegativeInt64 {
    param(
        [object] $Value,
        [string] $Label
    )

    if ($null -eq $Value) {
        throw "$Label 값이 없습니다."
    }
    $typeCode = [Type]::GetTypeCode($Value.GetType())
    if ($typeCode -notin @(
        [TypeCode]::Byte,
        [TypeCode]::SByte,
        [TypeCode]::Int16,
        [TypeCode]::UInt16,
        [TypeCode]::Int32,
        [TypeCode]::UInt32,
        [TypeCode]::Int64,
        [TypeCode]::UInt64
    )) {
        throw "$Label 값은 JSON 정수여야 합니다."
    }
    $parsed = [long] 0
    $text = [Convert]::ToString($Value, [Globalization.CultureInfo]::InvariantCulture)
    if (-not [long]::TryParse(
        $text,
        [Globalization.NumberStyles]::None,
        [Globalization.CultureInfo]::InvariantCulture,
        [ref] $parsed
    )) {
        throw "$Label 값은 음수가 아닌 정수여야 합니다: $text"
    }
    return $parsed
}

function Get-ValidatedSha256 {
    param(
        [object] $Value,
        [string] $Label
    )

    if ($Value -isnot [string]) {
        throw "$Label 값은 문자열이어야 합니다."
    }
    $hash = [string] $Value
    if ($hash -notmatch '^[0-9a-fA-F]{64}$') {
        throw "$Label 값은 64자리 SHA-256이어야 합니다."
    }
    return $hash.ToLowerInvariant()
}

function Get-ValidatedFileMetadata {
    param(
        [object] $Metadata,
        [string] $Label
    )

    $size = Get-ValidatedNonNegativeInt64 `
        -Value (Get-RequiredPropertyValue -Object $Metadata -Name 'sizeBytes' -Label $Label) `
        -Label "$Label sizeBytes"
    $sha256 = Get-ValidatedSha256 `
        -Value (Get-RequiredPropertyValue -Object $Metadata -Name 'sha256' -Label $Label) `
        -Label "$Label sha256"

    return [pscustomobject][ordered]@{
        sizeBytes = $size
        sha256    = $sha256
    }
}

function Get-ValidatedPortablePath {
    param(
        [object] $Value,
        [string] $Label,
        [switch] $RejectOverlayMetadata
    )

    if ($Value -isnot [string]) {
        throw "$Label 경로는 문자열이어야 합니다."
    }
    $path = [string] $Value
    if ([string]::IsNullOrWhiteSpace($path)) {
        throw "$Label 경로가 비어 있습니다."
    }
    if ($path -match '[\x00-\x1f]' -or $path.Contains(':')) {
        throw "$Label 경로에 제어 문자 또는 ADS 구분자(:)가 있습니다: $path"
    }

    $portable = $path.Replace('\', '/')
    $windowsPath = $portable.Replace('/', '\')
    if ([IO.Path]::IsPathRooted($windowsPath) -or
        $portable.StartsWith('/') -or
        $portable.EndsWith('/') -or
        $portable.Contains('//')) {
        throw "$Label 경로는 비어 있는 구간이 없는 상대 경로여야 합니다: $path"
    }

    $segments = $portable.Split('/')
    foreach ($segment in $segments) {
        if ([string]::IsNullOrEmpty($segment) -or $segment -eq '.' -or $segment -eq '..') {
            throw "$Label 경로에 허용되지 않는 구간이 있습니다: $path"
        }
        if ($segment.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0) {
            throw "$Label 경로에 Windows 파일명 금지 문자가 있습니다: $path"
        }
        $lastCharacter = $segment[$segment.Length - 1]
        if ($lastCharacter -eq '.' -or [char]::IsWhiteSpace($lastCharacter)) {
            throw "$Label 경로 구간은 점이나 공백으로 끝날 수 없습니다: $path"
        }
        if ($segment -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$') {
            throw "$Label 경로에 Windows 예약 이름이 있습니다: $path"
        }
    }

    if ($RejectOverlayMetadata -and
        $segments[0].Equals('overlay-manifest.json', [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label 경로가 오버레이 메타데이터와 충돌합니다: $path"
    }

    return $portable
}

$projectRoot = (Get-Item -LiteralPath (Join-Path $PSScriptRoot '..')).FullName
if ([string]::IsNullOrWhiteSpace($ToolLockPath)) {
    $ToolLockPath = Join-Path $projectRoot 'config\tools.lock.json'
}
if ([string]::IsNullOrWhiteSpace($ReleasePlanPath)) {
    $ReleasePlanPath = Join-Path $projectRoot 'manifests\next-release-plan.json'
}

$gameRootResolved = Get-DirectoryPath -Path $GameRoot -Label '게임 원본'
$patchRootResolved = Get-DirectoryPath -Path $PatchRoot -Label '패치'
$manifestItem = Get-Item -LiteralPath $ManifestPath -Force
$xdeltaItem = Get-Item -LiteralPath $XdeltaPath -Force
if ($manifestItem.PSIsContainer) {
    throw "매니페스트 경로가 파일이 아닙니다: $ManifestPath"
}
if ($xdeltaItem.PSIsContainer) {
    throw "xdelta 경로가 파일이 아닙니다: $XdeltaPath"
}
$manifest = Get-Content -LiteralPath $manifestItem.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
$toolLock = Get-Content -LiteralPath $ToolLockPath -Raw -Encoding UTF8 | ConvertFrom-Json
$actualManifestHash = (Get-FileHash -LiteralPath $manifestItem.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
$actualReleasePlanHash = $null
$releasePlanItem = $null

$schemaVersion = Get-ValidatedNonNegativeInt64 `
    -Value (Get-RequiredPropertyValue -Object $manifest -Name 'schemaVersion' -Label '매니페스트') `
    -Label '매니페스트 schemaVersion'
if ($schemaVersion -ne 1) {
    throw "지원하지 않는 매니페스트 schemaVersion입니다: $schemaVersion"
}

$manifestNameValue = Get-RequiredPropertyValue -Object $manifest -Name 'name' -Label '매니페스트'
$manifestGameIdValue = Get-RequiredPropertyValue -Object $manifest -Name 'gameId' -Label '매니페스트'
$manifestRequiredVersionValue = Get-RequiredPropertyValue -Object $manifest -Name 'requiredVersion' -Label '매니페스트'
if ($manifestNameValue -isnot [string] -or
    $manifestGameIdValue -isnot [string] -or
    $manifestRequiredVersionValue -isnot [string]) {
    throw '매니페스트 name, gameId, requiredVersion은 문자열이어야 합니다.'
}
$manifestName = [string] $manifestNameValue
$manifestGameId = [string] $manifestGameIdValue
$manifestRequiredVersion = [string] $manifestRequiredVersionValue
if ([string]::IsNullOrWhiteSpace($manifestName) -or
    [string]::IsNullOrWhiteSpace($manifestGameId) -or
    [string]::IsNullOrWhiteSpace($manifestRequiredVersion)) {
    throw '매니페스트 name, gameId, requiredVersion은 비어 있을 수 없습니다.'
}
if (-not $manifestGameId.Equals('PCSG00264', [StringComparison]::OrdinalIgnoreCase)) {
    throw "지원하지 않는 게임 ID입니다: $manifestGameId"
}

$manifestFormat = Get-RequiredPropertyValue -Object $manifest -Name 'format' -Label '매니페스트'
$manifestFormatNameValue = Get-RequiredPropertyValue -Object $manifestFormat -Name 'name' -Label '매니페스트 format'
if ($manifestFormatNameValue -isnot [string]) {
    throw '매니페스트 format.name은 문자열이어야 합니다.'
}
$manifestFormatName = [string] $manifestFormatNameValue
if (-not $manifestFormatName.Equals('VCDIFF/xdelta3', [StringComparison]::OrdinalIgnoreCase)) {
    throw "지원하지 않는 델타 형식입니다: $manifestFormatName"
}

$manifestRoots = Get-RequiredPropertyValue -Object $manifest -Name 'roots' -Label '매니페스트'
foreach ($rootName in @('original', 'localized', 'delta')) {
    $null = Get-ValidatedPortablePath `
        -Value (Get-RequiredPropertyValue -Object $manifestRoots -Name $rootName -Label '매니페스트 roots') `
        -Label "매니페스트 roots.$rootName"
}

$manifestCounts = Get-RequiredPropertyValue -Object $manifest -Name 'counts' -Label '매니페스트'
$declaredEntryCount = Get-ValidatedNonNegativeInt64 `
    -Value (Get-RequiredPropertyValue -Object $manifestCounts -Name 'total' -Label '매니페스트 counts') `
    -Label '매니페스트 counts.total'
$declaredCategoryCounts = Get-RequiredPropertyValue -Object $manifestCounts -Name 'byCategory' -Label '매니페스트 counts'
$manifestEntriesValue = Get-RequiredPropertyValue -Object $manifest -Name 'entries' -Label '매니페스트'
$manifestEntries = @($manifestEntriesValue)
if ($manifestEntries.Count -eq 0 -or $declaredEntryCount -ne $manifestEntries.Count) {
    throw "매니페스트 counts.total과 entries 개수가 다르거나 항목이 없습니다: 선언=$declaredEntryCount, 실제=$($manifestEntries.Count)"
}

$validatedEntries = [System.Collections.Generic.List[object]]::new()
$targetPaths = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
$deltaPaths = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
$actualCategoryCounts = @{}

for ($index = 0; $index -lt $manifestEntries.Count; $index++) {
    $entry = $manifestEntries[$index]
    $expectedOrder = $index + 1
    $actualOrder = Get-ValidatedNonNegativeInt64 `
        -Value (Get-RequiredPropertyValue -Object $entry -Name 'order' -Label "매니페스트 항목 $expectedOrder") `
        -Label "매니페스트 항목 $expectedOrder order"
    if ($actualOrder -ne $expectedOrder) {
        throw "매니페스트 order가 연속적이지 않습니다: 위치=$expectedOrder, 값=$actualOrder"
    }

    $categoryValue = Get-RequiredPropertyValue -Object $entry -Name 'category' -Label "매니페스트 항목 $expectedOrder"
    if ($categoryValue -isnot [string]) {
        throw "매니페스트 항목 $expectedOrder category는 문자열이어야 합니다."
    }
    $category = [string] $categoryValue
    if ([string]::IsNullOrWhiteSpace($category)) {
        throw "매니페스트 항목 $expectedOrder category가 비어 있습니다."
    }
    $targetPath = Get-ValidatedPortablePath `
        -Value (Get-RequiredPropertyValue -Object $entry -Name 'targetPath' -Label "매니페스트 항목 $expectedOrder") `
        -Label "매니페스트 항목 $expectedOrder targetPath" `
        -RejectOverlayMetadata
    $deltaFile = Get-ValidatedPortablePath `
        -Value (Get-RequiredPropertyValue -Object $entry -Name 'deltaFile' -Label "매니페스트 항목 $expectedOrder") `
        -Label "매니페스트 항목 $expectedOrder deltaFile"
    if (-not $deltaFile.EndsWith('.xdelta', [StringComparison]::OrdinalIgnoreCase)) {
        throw "매니페스트 항목 $expectedOrder deltaFile 확장자는 .xdelta여야 합니다: $deltaFile"
    }
    if (-not $targetPaths.Add($targetPath)) {
        throw "매니페스트 targetPath가 대소문자를 무시하면 중복됩니다: $targetPath"
    }
    if (-not $deltaPaths.Add($deltaFile)) {
        throw "매니페스트 deltaFile이 대소문자를 무시하면 중복됩니다: $deltaFile"
    }

    $originalMetadata = Get-ValidatedFileMetadata `
        -Metadata (Get-RequiredPropertyValue -Object $entry -Name 'original' -Label "매니페스트 항목 $expectedOrder") `
        -Label "매니페스트 항목 $expectedOrder original"
    $localizedMetadata = Get-ValidatedFileMetadata `
        -Metadata (Get-RequiredPropertyValue -Object $entry -Name 'localized' -Label "매니페스트 항목 $expectedOrder") `
        -Label "매니페스트 항목 $expectedOrder localized"
    $deltaMetadata = Get-ValidatedFileMetadata `
        -Metadata (Get-RequiredPropertyValue -Object $entry -Name 'delta' -Label "매니페스트 항목 $expectedOrder") `
        -Label "매니페스트 항목 $expectedOrder delta"

    $contentChanged = Get-RequiredPropertyValue -Object $entry -Name 'contentChanged' -Label "매니페스트 항목 $expectedOrder"
    if ($contentChanged -isnot [bool]) {
        throw "매니페스트 항목 $expectedOrder contentChanged는 Boolean이어야 합니다."
    }
    $hashesDiffer = -not $originalMetadata.sha256.Equals($localizedMetadata.sha256, [StringComparison]::OrdinalIgnoreCase)
    if ([bool] $contentChanged -ne $hashesDiffer) {
        throw "매니페스트 항목 $expectedOrder contentChanged가 원본/한글화 SHA-256 관계와 다릅니다."
    }

    if (-not $actualCategoryCounts.ContainsKey($category)) {
        $actualCategoryCounts[$category] = 0
    }
    $actualCategoryCounts[$category]++
    $validatedEntries.Add([pscustomobject][ordered]@{
        entry      = $entry
        order      = $expectedOrder
        category   = $category
        targetPath = $targetPath
        deltaFile  = $deltaFile
    })
}

foreach ($targetPath in $targetPaths) {
    $segments = $targetPath.Split('/')
    for ($segmentCount = 1; $segmentCount -lt $segments.Length; $segmentCount++) {
        $prefixPath = ($segments[0..($segmentCount - 1)] -join '/')
        if ($targetPaths.Contains($prefixPath)) {
            throw "매니페스트 targetPath 사이에 파일/디렉터리 충돌이 있습니다: $prefixPath <> $targetPath"
        }
    }
}

$declaredCategoryProperties = @($declaredCategoryCounts.PSObject.Properties)
if ($declaredCategoryProperties.Count -ne $actualCategoryCounts.Count) {
    throw '매니페스트 counts.byCategory의 범주 수가 실제 entries와 다릅니다.'
}
foreach ($category in $actualCategoryCounts.Keys) {
    $declaredProperty = $declaredCategoryCounts.PSObject.Properties[$category]
    if ($null -eq $declaredProperty) {
        throw "매니페스트 counts.byCategory에 범주가 없습니다: $category"
    }
    $declaredCategoryCount = Get-ValidatedNonNegativeInt64 `
        -Value $declaredProperty.Value `
        -Label "매니페스트 counts.byCategory.$category"
    if ($declaredCategoryCount -ne [long] $actualCategoryCounts[$category]) {
        throw "매니페스트 counts.byCategory가 실제 entries와 다릅니다: $category"
    }
}

if ($Purpose -eq 'release') {
    $releasePlanItem = Get-Item -LiteralPath $ReleasePlanPath -Force
    if ($releasePlanItem.PSIsContainer) {
        throw "릴리스 계획 경로가 파일이 아닙니다: $ReleasePlanPath"
    }
    $releasePlan = Get-Content -LiteralPath $releasePlanItem.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    $actualReleasePlanHash = (Get-FileHash -LiteralPath $releasePlanItem.FullName -Algorithm SHA256).Hash.ToLowerInvariant()

    if (-not ([string] $releasePlan.gameId).Equals([string] $manifest.gameId, [StringComparison]::OrdinalIgnoreCase)) {
        throw '릴리스 계획과 매니페스트의 게임 ID가 다릅니다.'
    }
    if (-not ([string] $releasePlan.requiredGameVersion).Equals([string] $manifest.requiredVersion, [StringComparison]::OrdinalIgnoreCase)) {
        throw '릴리스 계획과 매니페스트의 요구 게임 버전이 다릅니다.'
    }

    $baseline = @($releasePlan.baselineManifests | Where-Object { [string] $_.path -eq $manifestItem.Name })
    if ($baseline.Count -ne 1) {
        throw '릴리스 계획에서 선택한 기준 매니페스트를 유일하게 찾지 못했습니다.'
    }
    if (-not $actualManifestHash.Equals([string] $baseline[0].sha256, [StringComparison]::OrdinalIgnoreCase)) {
        throw '기준 매니페스트 SHA-256이 릴리스 계획에 고정된 값과 다릅니다.'
    }

    $trackProperty = $releasePlan.releaseTracks.PSObject.Properties[$ReleaseTrack]
    if ($null -eq $trackProperty) {
        throw "릴리스 계획에 대상 트랙이 없습니다: $ReleaseTrack"
    }
    $track = $trackProperty.Value
    $allowedManifests = @($track.manifests | ForEach-Object { [string] $_ })
    if ($allowedManifests -notcontains $manifestItem.Name) {
        throw "선택한 릴리스 트랙에서 허용하지 않는 매니페스트입니다: $($manifestItem.Name)"
    }

    $gateById = @{}
    foreach ($gate in $releasePlan.releaseGates) {
        $gateId = [string] $gate.id
        if ([string]::IsNullOrWhiteSpace($gateId) -or $gateById.ContainsKey($gateId)) {
            throw "릴리스 게이트 ID가 비어 있거나 중복됩니다: $gateId"
        }
        $gateById[$gateId] = $gate
    }
    $trackGateIds = @($track.blockingGateIds | ForEach-Object { [string] $_ })
    foreach ($gateId in $trackGateIds) {
        if (-not $gateById.ContainsKey($gateId)) {
            throw "대상 트랙이 존재하지 않는 게이트를 참조합니다: $gateId"
        }
    }
    $blockedGates = @($trackGateIds | ForEach-Object { $gateById[$_] } | Where-Object { [string] $_.status -ne 'satisfied' })
    if ([string] $releasePlan.status -ne 'ready' -or [string] $track.status -ne 'ready' -or $blockedGates.Count -gt 0) {
        $blockedIds = ($blockedGates | ForEach-Object { [string] $_.id }) -join ', '
        throw "릴리스 게이트가 닫혀 있습니다. 계획=$($releasePlan.status), 트랙=$($track.status), 미통과=$blockedIds"
    }
}

$actualToolHash = (Get-FileHash -LiteralPath $xdeltaItem.FullName -Algorithm SHA256).Hash
if (-not $actualToolHash.Equals([string] $toolLock.xdelta3.executableSha256, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'xdelta3.exe가 config/tools.lock.json에 고정된 공식 실행 파일과 다릅니다.'
}

$outputResolved = [IO.Path]::GetFullPath($OutputDirectory).TrimEnd('\', '/')
$gamePrefix = $gameRootResolved.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
$patchPrefix = $patchRootResolved.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
if ($outputResolved.Equals($gameRootResolved, [StringComparison]::OrdinalIgnoreCase) -or
    $outputResolved.StartsWith($gamePrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw '출력은 게임 원본/설치 폴더 밖의 별도 경로여야 합니다.'
}
if ($outputResolved.Equals($patchRootResolved, [StringComparison]::OrdinalIgnoreCase) -or
    $outputResolved.StartsWith($patchPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw '출력은 패치 입력 폴더 밖의 별도 경로여야 합니다.'
}
if (Test-Path -LiteralPath $outputResolved) {
    throw "출력 경로가 이미 존재합니다. 기존 파일을 덮어쓰지 않습니다: $outputResolved"
}

$workItems = [System.Collections.Generic.List[object]]::new()
foreach ($validatedEntry in $validatedEntries) {
    $targetRelative = $validatedEntry.targetPath.Replace('/', '\')
    $deltaRelative = $validatedEntry.deltaFile.Replace('/', '\')
    $sourcePath = [IO.Path]::GetFullPath((Join-Path $gameRootResolved $targetRelative))
    $deltaPath = [IO.Path]::GetFullPath((Join-Path $patchRootResolved $deltaRelative))

    Assert-ChildPath -Root $gameRootResolved -Path $sourcePath
    Assert-ChildPath -Root $patchRootResolved -Path $deltaPath
    $null = Test-ExpectedFile -Path $sourcePath -Expected $validatedEntry.entry.original -Label '원본'
    $null = Test-ExpectedFile -Path $deltaPath -Expected $validatedEntry.entry.delta -Label '델타'

    $workItems.Add([pscustomobject][ordered]@{
        entry      = $validatedEntry.entry
        targetPath = $validatedEntry.targetPath
        sourcePath = $sourcePath
        deltaPath  = $deltaPath
    })
}

$outputParent = Split-Path -Parent $outputResolved
[IO.Directory]::CreateDirectory($outputParent) | Out-Null
$outputLeaf = Split-Path -Leaf $outputResolved
$stagingLeaf = $outputLeaf + '.staging-' + [Guid]::NewGuid().ToString('N')
$stagingLeafPattern = '^' + [regex]::Escape($outputLeaf) + '\.staging-[0-9a-f]{32}$'
if (-not [regex]::IsMatch($stagingLeaf, $stagingLeafPattern)) {
    throw "내부 오류: 스테이징 폴더 이름이 안전 패턴과 다릅니다: $stagingLeaf"
}
$stagingRoot = [IO.Path]::GetFullPath((Join-Path $outputParent $stagingLeaf))
$stagingCreated = $false
$published = $false
$completed = 0
$outputFiles = [System.Collections.Generic.List[object]]::new()

try {
    [IO.Directory]::CreateDirectory($stagingRoot) | Out-Null
    $stagingCreated = $true

    foreach ($workItem in $workItems) {
        $targetRelative = $workItem.targetPath.Replace('/', '\')
        $destinationPath = [IO.Path]::GetFullPath((Join-Path $stagingRoot $targetRelative))

        Assert-ChildPath -Root $stagingRoot -Path $destinationPath

        [IO.Directory]::CreateDirectory((Split-Path -Parent $destinationPath)) | Out-Null
        $savedErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $toolOutput = @(& $xdeltaItem.FullName -d -f -s $workItem.sourcePath $workItem.deltaPath $destinationPath 2>&1)
            $exitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $savedErrorActionPreference
        }
        if ($exitCode -ne 0) {
            throw ("xdelta 디코딩 실패: {0}: {1}" -f $workItem.targetPath, (($toolOutput | ForEach-Object { [string] $_ }) -join ' '))
        }
        $outputMetadata = Test-ExpectedFile -Path $destinationPath -Expected $workItem.entry.localized -Label '결과'
        $outputFiles.Add([ordered]@{
            path      = $workItem.targetPath
            sizeBytes = [long] $outputMetadata.sizeBytes
            sha256    = [string] $outputMetadata.sha256
        })
        $completed++
    }

    $overlayMetadata = [ordered]@{
        schemaVersion   = 2
        gameId          = $manifestGameId
        requiredVersion = $manifestRequiredVersion
        manifest        = $manifestName
        manifestFile    = $manifestItem.Name
        manifestSha256  = $actualManifestHash
        purpose         = $Purpose
        releaseTrack    = if ($Purpose -eq 'release') { $ReleaseTrack } else { $null }
        fileCount       = $completed
        outputs         = @($outputFiles)
        xdelta3         = [ordered]@{
            version = [string] $toolLock.xdelta3.version
            sha256  = $actualToolHash.ToLowerInvariant()
        }
    }
    if ($Purpose -eq 'release') {
        $overlayMetadata['releasePlanFile'] = $releasePlanItem.Name
        $overlayMetadata['releasePlanSha256'] = $actualReleasePlanHash
    }
    $metadataPath = Join-Path $stagingRoot 'overlay-manifest.json'
    $overlayJson = $overlayMetadata | ConvertTo-Json -Depth 8
    $overlayJson = $overlayJson.Replace("`r`n", "`n").Replace("`r", "`n")
    [IO.File]::WriteAllText($metadataPath, ($overlayJson + "`n"), [Text.UTF8Encoding]::new($false))

    if (Test-Path -LiteralPath $outputResolved) {
        throw "최종 이동 직전에 출력 경로가 생성되었습니다. 덮어쓰지 않습니다: $outputResolved"
    }
    [IO.Directory]::Move($stagingRoot, $outputResolved)
    $published = $true
}
catch {
    $primaryError = $_
    $cleanupError = $null
    if ($stagingCreated -and -not $published) {
        try {
            $stagingResolved = [IO.Path]::GetFullPath($stagingRoot)
            $expectedParent = [IO.Path]::GetFullPath($outputParent).TrimEnd('\', '/')
            $actualParent = [IO.Path]::GetDirectoryName($stagingResolved).TrimEnd('\', '/')
            $actualLeaf = [IO.Path]::GetFileName($stagingResolved)
            if (-not $actualParent.Equals($expectedParent, [StringComparison]::OrdinalIgnoreCase) -or
                -not [regex]::IsMatch($actualLeaf, $stagingLeafPattern)) {
                throw "안전 검증에 실패해 스테이징 폴더를 정리하지 않습니다: $stagingResolved"
            }
            if (Test-Path -LiteralPath $stagingResolved -PathType Container) {
                Remove-Item -LiteralPath $stagingResolved -Recurse -Force -ErrorAction Stop
            }
        }
        catch {
            $cleanupError = $_
        }
    }
    if ($null -ne $cleanupError) {
        [Console]::Error.WriteLine("[주의] 스테이징 정리 실패: $($cleanupError.Exception.Message)")
    }
    throw $primaryError
}

Write-Host ("[통과] 원본을 수정하지 않고 {0}개 파일 오버레이를 생성했습니다: {1}" -f $completed, $outputResolved)
