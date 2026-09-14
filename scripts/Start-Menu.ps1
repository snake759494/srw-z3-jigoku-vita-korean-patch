[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$SourceRoot = Join-Path $ProjectRoot 'src'

try {
    [Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $null = & chcp.com 65001
} catch {
    # 오래된 콘솔에서는 Python 쪽 UTF-8 설정을 사용합니다.
}

function Find-PatchPython {
    $candidates = @(
        (Join-Path $ProjectRoot '.venv\Scripts\python.exe'),
        (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return [pscustomobject]@{ Command = $candidate; Prefix = @() }
        }
    }
    foreach ($name in @('python.exe', 'python3.exe')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($null -ne $command) {
            return [pscustomobject]@{ Command = $command.Source; Prefix = @() }
        }
    }
    $launcher = Get-Command 'py.exe' -ErrorAction SilentlyContinue
    if ($null -ne $launcher) {
        return [pscustomobject]@{ Command = $launcher.Source; Prefix = @('-3') }
    }
    throw 'Python 3을 찾을 수 없습니다. Python 3.10 이상을 설치한 뒤 다시 실행하세요.'
}

$Python = Find-PatchPython
$env:PYTHONPATH = $SourceRoot

function Invoke-PatchCommand {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    & $Python.Command @($Python.Prefix) -m siok_patch.cli @Arguments
    $exit = $LASTEXITCODE
    if ($exit -ne 0) {
        Write-Host "`n명령이 안전하게 중단되었습니다(종료 코드 $exit). 위 안내를 확인하세요." -ForegroundColor Yellow
    }
}

function Read-RequiredValue {
    param([Parameter(Mandatory = $true)][string]$Label)
    while ($true) {
        $value = (Read-Host $Label).Trim().Trim('"')
        if ($value) { return $value }
        Write-Host '값을 입력해야 합니다.' -ForegroundColor Yellow
    }
}

function Invoke-DialogueBuild {
    $cpk = Read-RequiredValue '읽기 전용 원본 CPK 경로'
    $arguments = [System.Collections.Generic.List[string]]::new()
    $arguments.Add('dialogue-build')
    $arguments.Add('--cpk')
    $arguments.Add($cpk)

    $manifest = (Read-Host '이전에 만든 dialogue-manifest.json으로 재빌드 (새 XLSX 빌드는 Enter)').Trim().Trim('"')
    if ($manifest) {
        $arguments.Add('--manifest')
        $arguments.Add($manifest)
        Invoke-PatchCommand $arguments.ToArray()
        return
    }

    $first = $true
    while ($true) {
        if ($first) {
            $target = Read-RequiredValue '번역을 넣을 대상 ID (예: ID00004)'
        } else {
            $target = (Read-Host '추가 대상 ID (없으면 Enter)').Trim()
            if (-not $target) { break }
        }
        $source = (Read-Host '대상 ID가 원본 CPK에 없을 때 사용할 원본 ID (보통은 Enter)').Trim()
        $xlsx = Read-RequiredValue '이 ID에 적용할 번역 XLSX 경로'
        $mapping = if ($source) { "$target@$source" } else { $target }
        $arguments.Add('--entry')
        $arguments.Add("$mapping=$xlsx")
        $first = $false
    }

    $exceptions = (Read-Host '검수 승인한 제어 토큰 변경 (예: ID00003:163,ID00003:167 / 없으면 Enter)').Trim()
    if ($exceptions) {
        foreach ($item in $exceptions.Split(',')) {
            if ($item.Trim()) {
                $arguments.Add('--allow-control-change')
                $arguments.Add($item.Trim())
            }
        }
    }
    Invoke-PatchCommand $arguments.ToArray()
}

while ($true) {
    Clear-Host
    Write-Host '==============================================' -ForegroundColor Cyan
    Write-Host ' 제3차 슈퍼로봇대전 Z 시옥편 한글패치 준비'
    Write-Host '==============================================' -ForegroundColor Cyan
    Write-Host '1. 이 PC의 경로 설정'
    Write-Host '2. 환경·입력 검사'
    Write-Host '3. 기존 XLSX 번역 가져오기'
    Write-Host '4. 가져온 번역 검사'
    Write-Host '5. 현재 상태 보기'
    Write-Host '6. 배포 가능 여부 검사'
    Write-Host '7. 대사 CPK 한 번에 만들기'
    Write-Host '0. 끝내기'
    Write-Host ''
    $choice = Read-Host '번호를 입력하세요'
    switch ($choice) {
        '1' { Invoke-PatchCommand @('configure') }
        '2' { Invoke-PatchCommand @('doctor') }
        '3' { Invoke-PatchCommand @('import') }
        '4' { Invoke-PatchCommand @('check') }
        '5' { Invoke-PatchCommand @('status') }
        '6' { Invoke-PatchCommand @('release-check') }
        '7' { Invoke-DialogueBuild }
        '0' { return }
        default { Write-Host '0부터 7 사이의 번호를 입력하세요.' -ForegroundColor Yellow }
    }
    Write-Host ''
    $null = Read-Host '메뉴로 돌아가려면 Enter를 누르세요'
}
