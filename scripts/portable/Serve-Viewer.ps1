param(
    [string]$Root = ".",
    [int]$Port = 8765,
    [switch]$NoBrowser
)

# Windows PowerShell 5.1은 BOM이 없으면 이 파일을 ANSI로 읽는다.
# 이 스크립트는 반드시 UTF-8 BOM으로 저장해야 한다.
$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$Root = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $Root).Path).TrimEnd('\')

$mime = @{
    ".html" = "text/html; charset=utf-8"
    ".htm"  = "text/html; charset=utf-8"
    ".js"   = "text/javascript; charset=utf-8"
    ".json" = "application/json; charset=utf-8"
    ".css"  = "text/css; charset=utf-8"
    ".txt"  = "text/plain; charset=utf-8"
    ".md"   = "text/plain; charset=utf-8"
    ".svg"  = "image/svg+xml"
    ".png"  = "image/png"
    ".ico"  = "image/x-icon"
}

$page = Get-ChildItem -LiteralPath (Join-Path $Root "viewer") -Filter *.html |
    Select-Object -First 1
if (-not $page) {
    Write-Host "[!] viewer 폴더에서 HTML을 찾지 못했습니다. ZIP을 폴더째 다시 푸세요."
    exit 1
}

$listener = New-Object System.Net.HttpListener
# 루프백 접두사는 URL 예약(netsh urlacl)도 관리자 권한도 필요 없다.
$listener.Prefixes.Add("http://127.0.0.1:$Port/")
try {
    $listener.Start()
} catch {
    Write-Host "[!] 포트 $Port 를 열지 못했습니다. 다른 포트로 다시 실행하세요."
    Write-Host "    예) set SIOK_VIEWER_PORT=8790"
    exit 1
}

$url = "http://127.0.0.1:$Port/viewer/" + [Uri]::EscapeDataString($page.Name)
Write-Host ""
Write-Host "  뷰어 주소 : $url"
Write-Host "  종료      : 이 창에서 Ctrl+C"
Write-Host ""
if (-not $NoBrowser) { Start-Process $url | Out-Null }

$offlineBody = [Text.Encoding]::UTF8.GetBytes(
    '{"ok":false,"error":"이 오프라인 배포판에는 CPK 빌드 기능이 없습니다. 저장한 JSON을 원본 작업 PC로 옮겨 빌드하세요."}')

try {
    while ($listener.IsListening) {
        $context = $listener.GetContext()
        $request = $context.Request
        $response = $context.Response
        try {
            # Request.Url은 시스템 코드페이지의 영향을 받을 수 있으므로
            # RawUrl을 직접 UTF-8 퍼센트 디코딩한다.
            $path = [Uri]::UnescapeDataString($request.RawUrl.Split('?')[0].Split('#')[0])
            if ($path -eq "/") { $path = "/viewer/" + $page.Name }

            if ($path -eq "/__siok__/build-cpk") {
                if ($request.HasEntityBody) {
                    $request.InputStream.CopyTo([System.IO.Stream]::Null)
                }
                $response.StatusCode = 501
                $response.ContentType = "application/json; charset=utf-8"
                $response.ContentLength64 = $offlineBody.Length
                $response.OutputStream.Write($offlineBody, 0, $offlineBody.Length)
            } else {
                $relative = $path.TrimStart('/').Replace('/', '\')
                $full = [System.IO.Path]::GetFullPath((Join-Path $Root $relative))
                if (-not $full.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase)) {
                    $response.StatusCode = 403
                } elseif (-not [System.IO.File]::Exists($full)) {
                    $response.StatusCode = 404
                } else {
                    $extension = [System.IO.Path]::GetExtension($full).ToLowerInvariant()
                    if ($mime.ContainsKey($extension)) {
                        $response.ContentType = $mime[$extension]
                    } else {
                        $response.ContentType = "application/octet-stream"
                    }
                    $response.Headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
                    $stream = [System.IO.File]::OpenRead($full)
                    try {
                        $response.ContentLength64 = $stream.Length
                        $stream.CopyTo($response.OutputStream, 131072)
                    } finally {
                        $stream.Dispose()
                    }
                }
            }
        } catch {
            try { $response.StatusCode = 500 } catch { }
        } finally {
            try { $response.OutputStream.Close() } catch { }
        }
    }
} finally {
    $listener.Stop()
    $listener.Close()
}
