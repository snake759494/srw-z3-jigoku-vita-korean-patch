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
    # 인코딩 설정이 불가능해도 검사는 계속합니다.
}
$PythonCandidates = @(
    (Join-Path $ProjectRoot '.venv\Scripts\python.exe'),
    (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')
)
$Python = $PythonCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $Python) {
    $PythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -eq $PythonCommand) {
        throw 'Python 3을 찾을 수 없습니다.'
    }
    $Python = $PythonCommand.Source
}

$env:PYTHONPATH = $SourceRoot
Push-Location $ProjectRoot
try {
    & $Python -m compileall -q $SourceRoot
    if ($LASTEXITCODE -ne 0) { throw 'Python 문법 검사 실패' }

    & $Python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw '단위 테스트 실패' }

    & $Python -m siok_patch.cli doctor
    if ($LASTEXITCODE -ne 0) { throw '환경 검사 실패' }

    foreach ($jsonFile in Get-ChildItem -LiteralPath (Join-Path $ProjectRoot 'config') -Filter '*.json' -File) {
        $null = Get-Content -Raw -LiteralPath $jsonFile.FullName -Encoding UTF8 | ConvertFrom-Json
    }
    Write-Host '프로젝트 기본 검사를 모두 통과했습니다.' -ForegroundColor Green
} finally {
    Pop-Location
}
