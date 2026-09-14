"""시나리오 대사 뷰어 휴대판 ZIP을 만든다.

두 가지 배포판을 같은 코드에서 만든다.

``--profile offline``
    파이썬도 게임 원본도 없이 뷰어만 쓰는 가벼운 배포판. 모든 JSON을
    ``window.__SIOK_CHUNK__=<원본 JSON>;`` 형태의 고전 스크립트로 감싸서
    ``file://``에서도 열리게 하고, CPK 빌드 버튼은 비활성화한다.

``--profile full``
    파이썬 임베더블 런타임, 사용자의 ``cpkmakec.exe``와 실행 DLL, 원본 CPK,
    한글 문자표까지 넣어 대상 PC에 아무것도 설치하지 않고 CPK 빌드까지 하는
    완전판. 뷰어 HTML은 저장소 원본을 그대로 쓰고 실제 파이썬 서버가 뜬다.

사용법:
    python scripts/build_portable_viewer.py --profile offline
    python scripts/build_portable_viewer.py --profile full --verify-builds
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from datetime import date
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VIEWER_DIR = REPOSITORY_ROOT / "viewer"
VIEWER_HTML = VIEWER_DIR / "시나리오_대사_뷰어.html"
CATALOG_JSON = VIEWER_DIR / "scenario-index.json"
DIALOGUE_DIR = REPOSITORY_ROOT / "translations" / "dialogue"
SUB_CATALOGS = (
    "scenario-titles.json",
    "scenario-cpk-locations.json",
    "scenario-speakers.json",
)

BOM = b"\xef\xbb\xbf"
CHUNK_PREFIX = b"window.__SIOK_CHUNK__="
CHUNK_SUFFIX = b";\n"

# 파일명에 쓰인 한글은 이 네 글자뿐이다. 대상 PC의 압축 해제 프로그램이
# CP949로 이름을 읽어도 깨지지 않도록 데이터 파일명만 ASCII로 바꾼다.
SLUG_MAP = {"화": "hwa", "후": "hu", "분": "bun", "기": "gi"}

# scenario_cpk.py가 CPK 파일명에 허용하는 문자.
SAFE_CPK_NAME = re.compile(r"[A-Za-z0-9_.-]+\.cpk", re.IGNORECASE)

# 시나리오 JSON의 asset.fileName이 실제 CPK 이름과 다른 자산.
# 규칙은 assetKey의 첫 토막 + ".cpk" 이고, 예외는 아래 표에 명시한다.
SOURCE_NAME_EXCEPTIONS = {
    "STG0210-8화후A분기": "STG0211.cpk",
    "DLC0231": "DLC0323.cpk",
}

TOOL_FILES = (
    "cpkmakec.exe",
    "CpkMaker.dll",
    "CpkBinder.dll",
    "msvcp100.dll",
    "msvcr100.dll",
)
EXPECTED_TOOL_SHA = "8cea88e11a5460ff8c4a2cc6b5fd214ebeb40e5a661aec596925538267ab4954"

WREPLACE_MAIN = "Japanese - Hangul to Kanji.wReplace"
WREPLACE_OPERATION = "Japanese - Hangul to Kanji(oper).wReplace"

PORTABLE_SCRIPTS = (
    "portable_bootstrap.py",
    "portable_selfcheck.py",
    "merge_dlc0323.py",
)

PYTHON_PTH = "python313.zip\n.\n..\\..\\src\n..\\..\\scripts\n"


class BuildError(RuntimeError):
    """배포판을 만들 수 없을 때 즉시 멈춘다."""


# ---------------------------------------------------------------- 공통 유틸


def ascii_slug(name: str) -> str:
    slug = "".join(SLUG_MAP.get(char, char) for char in name)
    if not slug.isascii():
        remainder = sorted({char for char in slug if not char.isascii()})
        raise BuildError(f"파일명을 ASCII로 바꾸지 못했습니다: {name} → {remainder}")
    return slug


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def assert_wrappable(raw: bytes, label: str) -> None:
    if raw.startswith(BOM):
        raise BuildError(f"{label}: 원본 JSON에 BOM이 있습니다.")
    if b"</script" in raw.lower():
        raise BuildError(f"{label}: JSON 안에 </script 문자열이 있습니다.")
    for marker, codepoint in ((b"\xe2\x80\xa8", "U+2028"), (b"\xe2\x80\xa9", "U+2029")):
        if marker in raw:
            raise BuildError(f"{label}: JSON 안에 {codepoint} 줄바꿈 문자가 있습니다.")
    try:
        json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BuildError(f"{label}: JSON을 해석하지 못했습니다 - {error}") from error


def wrap_json_bytes(raw: bytes, label: str) -> bytes:
    assert_wrappable(raw, label)
    return BOM + CHUNK_PREFIX + raw + CHUNK_SUFFIX


def read_catalog() -> dict:
    catalog = json.loads(CATALOG_JSON.read_text(encoding="utf-8"))
    if catalog.get("format") != "siok.scenario-viewer-catalog":
        raise BuildError("scenario-index.json 형식이 올바르지 않습니다.")
    if not isinstance(catalog.get("assets"), list) or not catalog["assets"]:
        raise BuildError("scenario-index.json에 assets가 없습니다.")
    return catalog


def read_asset_block(path: Path) -> dict:
    """시나리오 JSON에서 asset 블록만 읽는다. 큰 파일을 다 파싱하지 않는다."""

    with path.open("rb") as handle:
        head = handle.read(8192).decode("utf-8", "ignore")
    match = re.search(r'"asset"\s*:\s*\{', head)
    if not match:
        raise BuildError(f"asset 블록을 찾지 못했습니다: {path.name}")
    start = match.end() - 1
    depth = 0
    for index in range(start, len(head)):
        if head[index] == "{":
            depth += 1
        elif head[index] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(head[start : index + 1])
    raise BuildError(f"asset 블록이 잘렸습니다: {path.name}")


# ------------------------------------------------------------------ HTML 패치


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise BuildError(f"HTML 패치 '{label}' 대상이 {count}곳입니다. 1곳이어야 합니다.")
    return text.replace(old, new, 1)


LOADER_ANCHOR = '    const SPEAKER_CATALOG_URL = "./scenario-speakers.json";\n'

LOADER_BLOCK = '''    const SPEAKER_CATALOG_URL = "./scenario-speakers.json";

    // ── 오프라인 배포판 로더 ──────────────────────────────────────────────
    // 모든 데이터는 fetch가 아니라 <script src>로 읽는다. file://에서도
    // http://에서도 같은 경로를 쓰므로 전송 방식에 따라 코드가 갈리지 않는다.
    // 각 *.json.js 파일은 "window.__SIOK_CHUNK__=<원본 JSON 그대로>;" 이다.
    const OFFLINE_PACKAGE = true;
    const DATA_SUFFIX = ".js";
    const capabilities = { cpkBuild: !OFFLINE_PACKAGE };

    function loadJson(url) {
      return new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = url + DATA_SUFFIX;
        script.charset = "utf-8";
        script.async = false;
        script.onload = () => {
          const payload = window.__SIOK_CHUNK__;
          window.__SIOK_CHUNK__ = undefined;
          script.remove();
          if (payload === undefined) reject(new Error(`데이터가 비어 있습니다: ${url}${DATA_SUFFIX}`));
          else resolve(payload);
        };
        script.onerror = () => {
          script.remove();
          reject(new Error(`데이터 파일을 열지 못했습니다: ${url}${DATA_SUFFIX}`));
        };
        document.head.appendChild(script);
      });
    }

    function copyTextFallback(text) {
      const area = document.createElement("textarea");
      area.value = String(text ?? "");
      area.setAttribute("readonly", "readonly");
      area.style.cssText = "position:fixed;top:-1000px;opacity:0;";
      document.body.appendChild(area);
      area.select();
      let copied = false;
      try { copied = document.execCommand("copy"); } catch (error) { copied = false; }
      area.remove();
      return copied;
    }
'''

ASSET_FETCH_OLD = '''        const response = await fetch(asset.path, { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
'''
ASSET_FETCH_NEW = "        const data = await loadJson(asset.path);\n"

CATALOG_FETCH_OLD = '''        const response = await fetch(CATALOG_URL, { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const catalog = await response.json();
'''
CATALOG_FETCH_NEW = "        const catalog = await loadJson(CATALOG_URL);\n"

TITLE_FETCH_OLD = '''          const titleResponse = await fetch(TITLE_CATALOG_URL, { cache: "no-store" });
          if (titleResponse.ok) {
            const titleCatalog = await titleResponse.json();
            if (titleCatalog.format === "siok.scenario-title-catalog" && titleCatalog.titles && typeof titleCatalog.titles === "object") {
              state.scenarioTitles = titleCatalog.titles;
            }
          }
'''
TITLE_FETCH_NEW = '''          const titleCatalog = await loadJson(TITLE_CATALOG_URL);
          if (titleCatalog.format === "siok.scenario-title-catalog" && titleCatalog.titles && typeof titleCatalog.titles === "object") {
            state.scenarioTitles = titleCatalog.titles;
          }
'''

LOCATION_FETCH_OLD = '''          const locationResponse = await fetch(CPK_LOCATION_CATALOG_URL, { cache: "no-store" });
          if (locationResponse.ok) {
            const locationCatalog = await locationResponse.json();
            if (locationCatalog.format === "siok.cpk-location-boundary-catalog" && locationCatalog.members && typeof locationCatalog.members === "object") {
              state.cpkLocations = locationCatalog.members;
            }
          }
'''
LOCATION_FETCH_NEW = '''          const locationCatalog = await loadJson(CPK_LOCATION_CATALOG_URL);
          if (locationCatalog.format === "siok.cpk-location-boundary-catalog" && locationCatalog.members && typeof locationCatalog.members === "object") {
            state.cpkLocations = locationCatalog.members;
          }
'''

SPEAKER_FETCH_OLD = '''          const speakerResponse = await fetch(SPEAKER_CATALOG_URL, { cache: "no-store" });
          if (speakerResponse.ok) {
            const speakerCatalog = await speakerResponse.json();
            if (speakerCatalog.format === "siok.scenario-speaker-catalog" && speakerCatalog.members && typeof speakerCatalog.members === "object") {
              state.speakerRows = Object.fromEntries(
                Object.entries(speakerCatalog.members).map(([key, rows]) => [
                  String(key).toUpperCase(),
                  new Set(Array.isArray(rows) ? rows.map((row) => Number(row)).filter((row) => Number.isInteger(row) && row > 0) : []),
                ]),
              );
            }
          }
'''
SPEAKER_FETCH_NEW = '''          const speakerCatalog = await loadJson(SPEAKER_CATALOG_URL);
          if (speakerCatalog.format === "siok.scenario-speaker-catalog" && speakerCatalog.members && typeof speakerCatalog.members === "object") {
            state.speakerRows = Object.fromEntries(
              Object.entries(speakerCatalog.members).map(([key, rows]) => [
                String(key).toUpperCase(),
                new Set(Array.isArray(rows) ? rows.map((row) => Number(row)).filter((row) => Number.isInteger(row) && row > 0) : []),
              ]),
            );
          }
'''

ASSET_ERROR_OLD = (
    '        catalogStatus.textContent = "시나리오 JSON을 읽지 못했습니다. '
    'viewer/시나리오_대사_뷰어_실행.cmd로 열어 주세요.";\n'
)
ASSET_ERROR_NEW = (
    '        catalogStatus.textContent = "시나리오 JSON을 읽지 못했습니다. '
    'translations/dialogue 폴더에 해당 *.json.js 파일이 있는지 확인하세요.";\n'
)

CATALOG_ERROR_OLD = (
    '        catalogStatus.textContent = "카탈로그를 읽지 못했습니다. '
    'viewer/시나리오_대사_뷰어_실행.cmd로 열어 주세요.";\n'
)
CATALOG_ERROR_NEW = (
    '        catalogStatus.textContent = "카탈로그를 읽지 못했습니다. '
    'ZIP을 폴더째 다시 풀고 viewer/scenario-index.json.js가 있는지 확인하세요.";\n'
)

SAVE_PICKER_OLD = '''      if (typeof window.showSaveFilePicker === "function" && window.isSecureContext) {
        const handle = await window.showSaveFilePicker({
          suggestedName,
          types: [{ description: "시나리오 JSON", accept: { "application/json": [".json"] } }],
        });
        const writable = await handle.createWritable();
        await writable.write(json);
        await writable.close();
        return "파일 저장 완료";
      }
'''
SAVE_PICKER_NEW = '''      if (typeof window.showSaveFilePicker === "function" && window.isSecureContext) {
        try {
          const handle = await window.showSaveFilePicker({
            suggestedName,
            types: [{ description: "시나리오 JSON", accept: { "application/json": [".json"] } }],
          });
          const writable = await handle.createWritable();
          await writable.write(json);
          await writable.close();
          return "파일 저장 완료";
        } catch (pickerError) {
          // 사용자가 직접 취소한 경우에만 중단하고, file:// 처럼 대화상자를
          // 쓸 수 없는 환경에서는 아래 다운로드 방식으로 넘어간다.
          if (pickerError && pickerError.name === "AbortError") throw pickerError;
          console.warn("파일 저장 대화상자를 쓰지 못해 다운로드로 대체합니다.", pickerError);
        }
      }
'''

COPY_OLD = '''      try { await navigator.clipboard.writeText(pageTranslation(page, "newTranslation")); showToast("신규 번역을 클립보드에 복사했습니다."); }
      catch { showToast("브라우저가 클립보드 접근을 허용하지 않았습니다."); }
'''
COPY_NEW = '''      const text = pageTranslation(page, "newTranslation");
      try { await navigator.clipboard.writeText(text); showToast("신규 번역을 클립보드에 복사했습니다."); }
      catch {
        if (copyTextFallback(text)) showToast("신규 번역을 클립보드에 복사했습니다.");
        else showToast("브라우저가 클립보드 접근을 허용하지 않았습니다.");
      }
'''

BUILD_STATUS_OLD = '''    function setBuildStatus(level, message, links = []) {
      buildCpkStatus.className = `build-status${level ? ` ${level}` : ""}`;
'''
BUILD_STATUS_NEW = '''    function setBuildStatus(level, message, links = []) {
      if (!capabilities.cpkBuild) {
        buildCpkStatus.className = "build-status error";
        buildCpkStatus.innerHTML = "<strong>이 오프라인 배포판에는 CPK 빌드 기능이 없습니다. 저장한 JSON을 원본 작업 PC로 옮겨 빌드하세요.</strong>";
        return;
      }
      buildCpkStatus.className = `build-status${level ? ` ${level}` : ""}`;
'''

BUILD_ENTRY_OLD = '''    async function buildCurrentScenarioCpk() {
      const asset = state.assets[state.assetIndex];
      if (!asset || !asset.data) return;
'''
BUILD_ENTRY_NEW = '''    async function buildCurrentScenarioCpk() {
      if (!capabilities.cpkBuild) {
        setBuildStatus();
        showToast("이 배포판에서는 CPK를 만들 수 없습니다.");
        return;
      }
      const asset = state.assets[state.assetIndex];
      if (!asset || !asset.data) return;
'''

BUILD_HINT_OLD = (
    '          <p class="hint">뷰어 실행 명령으로 연 로컬 서버가 본인 소유의 원본 CPK·cpkmakec·wReplace를 '
    "사용해 `output/dialogue`에 검증된 새 CPK를 만듭니다. 원본 파일은 덮어쓰지 않습니다.</p>\n"
)
BUILD_HINT_NEW = (
    '          <p class="hint">CPK 빌드는 파이썬·cpkmakec·본인 소유 원본 CPK가 있는 작업 PC에서만 '
    "동작합니다. 이 오프라인 배포판에서는 비활성화되어 있습니다.</p>\n"
)

BOOTSTRAP_OLD = "    loadCatalog();\n"
BOOTSTRAP_NEW = '''    window.addEventListener("beforeunload", (event) => {
      if (!state.dirty) return;
      event.preventDefault();
      event.returnValue = "";
    });
    if (!capabilities.cpkBuild) setBuildStatus();
    loadCatalog();
'''

DISABLE_SITES = (
    (
        "        buildCpkButton.disabled = !(state.assets[state.assetIndex] && state.assets[state.assetIndex].data);\n",
        "        buildCpkButton.disabled = !capabilities.cpkBuild || !(state.assets[state.assetIndex] && state.assets[state.assetIndex].data);\n",
        "buildCpkButton.disabled(A)",
    ),
    (
        "      buildCpkButton.disabled = !asset || !asset.data;\n",
        "      buildCpkButton.disabled = !capabilities.cpkBuild || !asset || !asset.data;\n",
        "buildCpkButton.disabled(B)",
    ),
)


def patch_viewer_html(text: str) -> str:
    steps = [
        (LOADER_ANCHOR, LOADER_BLOCK, "loader"),
        (ASSET_FETCH_OLD, ASSET_FETCH_NEW, "loadAsset"),
        (CATALOG_FETCH_OLD, CATALOG_FETCH_NEW, "loadCatalog"),
        (TITLE_FETCH_OLD, TITLE_FETCH_NEW, "titleCatalog"),
        (LOCATION_FETCH_OLD, LOCATION_FETCH_NEW, "locationCatalog"),
        (SPEAKER_FETCH_OLD, SPEAKER_FETCH_NEW, "speakerCatalog"),
        (ASSET_ERROR_OLD, ASSET_ERROR_NEW, "assetError"),
        (CATALOG_ERROR_OLD, CATALOG_ERROR_NEW, "catalogError"),
        (SAVE_PICKER_OLD, SAVE_PICKER_NEW, "savePicker"),
        (COPY_OLD, COPY_NEW, "clipboard"),
        (BUILD_STATUS_OLD, BUILD_STATUS_NEW, "setBuildStatus"),
        (BUILD_ENTRY_OLD, BUILD_ENTRY_NEW, "buildCurrentScenarioCpk"),
        (BUILD_HINT_OLD, BUILD_HINT_NEW, "buildHint"),
        (BOOTSTRAP_OLD, BOOTSTRAP_NEW, "bootstrap"),
        *DISABLE_SITES,
    ]
    for old, new, label in steps:
        text = _replace_once(text, old, new, label)
    remaining = [
        line.strip()
        for line in text.splitlines()
        if "fetch(" in line and "__siok__" not in line
    ]
    if remaining:
        raise BuildError(f"패치 후에도 남은 fetch 호출이 있습니다: {remaining}")
    return text


# ------------------------------------------------------------- 실행 파일/안내


def offline_launcher_cmd() -> bytes:
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "setlocal",
        'set "BROWSER="',
        r'if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "BROWSER=%ProgramFiles%\Google\Chrome\Application\chrome.exe"',
        r'if not defined BROWSER if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "BROWSER=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"',
        r'if not defined BROWSER if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" set "BROWSER=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"',
        r'if not defined BROWSER if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" set "BROWSER=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"',
        r'for %%F in ("%~dp0viewer\*.html") do (',
        '  if defined BROWSER ( start "" "%BROWSER%" "%%~fF" ) else ( start "" "%%~fF" )',
        "  goto :opened",
        ")",
        "echo [!] viewer folder is empty. Extract the whole ZIP folder again.",
        "pause",
        "goto :eof",
        ":opened",
        "endlocal",
    ]
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


def offline_server_cmd() -> bytes:
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "setlocal",
        'set "PSExecutionPolicyPreference="',
        'if not defined SIOK_VIEWER_PORT set "SIOK_VIEWER_PORT=8765"',
        r'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\Serve-Viewer.ps1" -Root "%~dp0." -Port %SIOK_VIEWER_PORT%',
        "pause",
        "endlocal",
    ]
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


def full_launcher_cmd() -> bytes:
    """파이썬 서버를 띄우고 브라우저를 여는 완전판 실행 파일. 내용은 ASCII만."""

    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "setlocal",
        'set "ROOT=%~dp0"',
        r'set "PY=%ROOT%runtime\python\python.exe"',
        'if not exist "%PY%" (',
        r"  echo [!] runtime\python\python.exe not found. Extract the whole ZIP folder again.",
        "  pause",
        "  goto :eof",
        ")",
        'if not defined SIOK_VIEWER_PORT set "SIOK_VIEWER_PORT=8765"',
        r'"%PY%" -X utf8 "%ROOT%scripts\portable_bootstrap.py"',
        "if errorlevel 1 (",
        "  pause",
        "  goto :eof",
        ")",
        'set "VIEWER_PATH=/viewer/%%EC%%8B%%9C%%EB%%82%%98%%EB%%A6%%AC%%EC%%98%%A4_%%EB%%8C%%80%%EC%%82%%AC_%%EB%%B7%%B0%%EC%%96%%B4.html"',
        'start "" "http://127.0.0.1:%SIOK_VIEWER_PORT%%VIEWER_PATH%?build=%RANDOM%%RANDOM%"',
        r'"%PY%" -X utf8 "%ROOT%scripts\scenario_viewer_server.py" --port %SIOK_VIEWER_PORT%',
        "pause",
        "endlocal",
    ]
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


def full_selfcheck_cmd() -> bytes:
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "setlocal",
        'set "ROOT=%~dp0"',
        r'set "PY=%ROOT%runtime\python\python.exe"',
        "powershell -NoProfile -Command \"Get-ChildItem -LiteralPath '%ROOT%.' -Recurse -File | Unblock-File\" >nul 2>nul",
        r'"%PY%" -X utf8 "%ROOT%scripts\portable_bootstrap.py"',
        r'"%PY%" -X utf8 "%ROOT%scripts\portable_selfcheck.py"',
        "pause",
        "endlocal",
    ]
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


def full_merge_cmd() -> bytes:
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "setlocal",
        'set "ROOT=%~dp0"',
        r'set "PY=%ROOT%runtime\python\python.exe"',
        r'"%PY%" -X utf8 "%ROOT%scripts\portable_bootstrap.py"',
        r'"%PY%" -X utf8 "%ROOT%scripts\merge_dlc0323.py"',
        "pause",
        "endlocal",
    ]
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


# ------------------------------------------------------- 원본 CPK 찾기/대응표


def source_cpk_candidates(asset_key: str, file_name: str) -> list[str]:
    """이 자산의 원본 CPK로 쓸 수 있는 파일명 후보를 우선순위대로 준다."""

    names: list[str] = []
    exception = SOURCE_NAME_EXCEPTIONS.get(asset_key)
    if exception:
        names.append(exception)
    if SAFE_CPK_NAME.fullmatch(file_name):
        names.append(file_name)
    # STG0200-4화-A분기 → STG0200.cpk 처럼 키의 첫 토막을 쓴다.
    head = asset_key.split("-", 1)[0]
    if head:
        names.append(f"{head}.cpk")
    seen: set[str] = set()
    return [name for name in names if not (name in seen or seen.add(name))]


def index_source_cpks(game_root: Path) -> dict[str, Path]:
    """원본 CPK 이름 → 경로 색인. 스테이지 원본과 DLC 원본을 모두 훑는다."""

    index: dict[str, Path] = {}
    roots = [
        game_root / "0_STAGE" / "!!!원본",
        game_root / "!DLC" / "!PCSG00264-원본",
    ]
    for base in roots:
        if not base.is_dir():
            continue
        for path in base.rglob("*.cpk"):
            if path.is_file():
                index.setdefault(path.name, path)
    if not index:
        raise BuildError(f"원본 CPK를 찾지 못했습니다: {roots}")
    return index


def resolve_sources(assets: list[dict], game_root: Path) -> dict[str, dict]:
    """자산별로 실제 원본 CPK와 번들 안에서 쓸 fileName/target을 결정한다."""

    index = index_source_cpks(game_root)
    resolved: dict[str, dict] = {}
    missing: list[str] = []
    for asset in assets:
        path = (VIEWER_DIR / asset["path"]).resolve()
        block = read_asset_block(path)
        asset_key = str(block.get("assetKey") or asset["key"])
        file_name = str(block.get("fileName") or f"{asset_key}.cpk")
        target = str(block.get("target") or "")
        chosen: Path | None = None
        chosen_name = ""
        for candidate in source_cpk_candidates(asset_key, file_name):
            if candidate in index:
                chosen = index[candidate]
                chosen_name = candidate
                break
        if chosen is None:
            missing.append(f"{asset_key} ({file_name})")
            continue
        # 번들 안의 target은 실제 파일명과 맞추고 ASCII만 쓴다.
        directory = target.rsplit("/", 1)[0] if "/" in target else "DATA/STAGE"
        if not directory.isascii():
            directory = "DATA/STAGE"
        if directory.endswith("/DLC"):
            # DLC0231처럼 다른 팩 안에 들어 있는 자산은 실제 팩 폴더에 둔다.
            directory = f"{chosen_name.rsplit('.', 1)[0]}/DLC"
        bundle_target = f"{directory}/{chosen_name}"
        resolved[asset_key] = {
            "sourcePath": chosen,
            "fileName": chosen_name,
            "target": bundle_target,
            "renamed": chosen_name != file_name,
        }
    if missing:
        raise BuildError("원본 CPK가 없는 자산: " + ", ".join(missing))
    return resolved


def expected_wreplace_hashes() -> tuple[str, str]:
    """파이프라인이 요구하는 문자표 해시를 한 곳에서 가져온다."""

    sys.path.insert(0, str(REPOSITORY_ROOT / "src"))
    from siok_patch.scenario_cpk import (  # noqa: PLC0415
        _EXPECTED_OPERATION_WREPLACE_SHA256,
        _EXPECTED_WREPLACE_SHA256,
    )

    return _EXPECTED_WREPLACE_SHA256.lower(), _EXPECTED_OPERATION_WREPLACE_SHA256.lower()


def find_wreplace(game_root: Path, extra_roots: list[Path]) -> tuple[Path, Path | None]:
    """한글 문자표를 찾는다.

    같은 이름의 변종이 원본 폴더에 여러 개 있으므로 이름이 아니라 SHA-256으로
    고른다. 파이프라인은 정확히 이 해시의 표만 받아들인다.
    """

    main_sha, operation_sha = expected_wreplace_hashes()
    main_path: Path | None = None
    operation_path: Path | None = None
    for base in [game_root, *extra_roots]:
        if not base.is_dir():
            continue
        for name, wanted, slot in (
            (WREPLACE_MAIN, main_sha, "main"),
            (WREPLACE_OPERATION, operation_sha, "operation"),
        ):
            if slot == "main" and main_path is not None:
                continue
            if slot == "operation" and operation_path is not None:
                continue
            for candidate in sorted(base.rglob(name)):
                if not candidate.is_file():
                    continue
                if sha256_file(candidate).lower() != wanted:
                    continue
                if slot == "main":
                    main_path = candidate
                else:
                    operation_path = candidate
                break
        if main_path is not None and operation_path is not None:
            break
    if main_path is None:
        raise BuildError(
            f"{WREPLACE_MAIN} 문자표를 찾지 못했습니다 (기대 SHA-256 {main_sha[:16]}…)."
        )
    return main_path, operation_path


# ---------------------------------------------------------------------- 안내문


def offline_readme(asset_count: int, built_on: str) -> str:
    return f"""시나리오 대사 뷰어 · 오프라인 배포판 (뷰어 전용)
==================================================

빌드일 : {built_on}
시나리오 : {asset_count}개

파이썬을 설치하지 않아도 됩니다. 압축을 폴더째 푼 뒤 아래 파일을 실행하세요.

[기본]  뷰어_열기.cmd            Chrome/Edge로 바로 열기
[선택]  뷰어_서버로_열기.cmd     PowerShell 로컬 서버로 열기(저장 위치 선택 가능)

되는 기능
  - {asset_count}개 시나리오 열람 · 원문/기존/신규 3단 비교 · 원고지 격자
  - 검색 · 검수 상태 필터 · 키보드 이동 · 화자명 자동 묶기
  - 신규 번역 편집, 저장 규칙 검사, JSON 내보내기

안 되는 기능
  - "현재 시나리오 CPK 빌드" 는 비활성화되어 있습니다.
    CPK까지 만들려면 완전판(FULL) 배포판을 쓰세요.

폴더 구조를 바꾸지 마세요. viewer 와 translations 는 나란히 있어야 합니다.
"""


def full_readme(asset_count: int, cpk_count: int, built_on: str) -> str:
    return rf"""시나리오 대사 뷰어 · 완전판 (뷰어 + CPK 빌드)
=================================================

빌드일 : {built_on}
시나리오 : {asset_count}개 · 원본 CPK : {cpk_count}개

대상 PC에 아무것도 설치하지 않습니다. 파이썬도 이 폴더 안에 들어 있습니다.


0. 압축을 풀기 전에 (중요)
--------------------------

ZIP 파일을 마우스 오른쪽 클릭 → 속성 → 아래쪽 "차단 해제" 체크 → 확인.
그 다음 압축을 푸세요. 이 과정을 건너뛰면 Windows가 CPK 도구 실행을 막습니다.

경로가 짧은 곳에 푸세요. 예) C:\SIOK
경로가 너무 길면 실행기가 알려 주고 멈춥니다.

압축 해제에 몇 분 걸립니다. 파일이 600개 가까이 됩니다.


1. 실행 순서
------------

  1) 점검.cmd          먼저 한 번 실행해 환경을 확인합니다.
                       모든 항목이 PASS 여야 합니다.
  2) 뷰어_실행.cmd     로컬 서버가 뜨고 브라우저에 뷰어가 열립니다.
                       검은 창은 닫지 마세요. 끄면 서버도 꺼집니다.
                       종료는 검은 창에서 Ctrl+C 입니다.

포트를 바꾸려면 실행 전에 명령 프롬프트에서
    set SIOK_VIEWER_PORT=8790
을 지정하세요.


2. CPK 만들기
-------------

  1) 왼쪽 목록에서 시나리오를 고릅니다.
  2) 필요하면 "신규 번역 수정" 칸에서 대사를 고칩니다.
  3) "현재 시나리오 CPK 빌드" 를 누릅니다.
     저장하지 않은 화면 편집까지 그대로 반영됩니다.
  4) 결과는 output\dialogue\<시나리오>\<실행ID>\ 아래에 생기고,
     버튼 밑에 "CPK 다운로드" 와 "빌드 보고서" 링크가 나옵니다.

원본 CPK는 절대 덮어쓰지 않습니다. 항상 새 파일이 만들어집니다.
빌드가 끝나면 재추출로 멤버 ID와 내용 해시를 다시 대조해 검증합니다.

DLC0231 만 예외입니다. 이 시나리오는 독립 CPK가 아니라 DLC0323.cpk 안의
한 멤버라서, 두 번역을 한 팩에 넣으려면 DLC0231_병합.cmd 를 실행하세요.
결과는 output\dlc0323-merged\DLC0323.cpk 입니다.


3. 폴더 구조
------------

  뷰어_실행.cmd           뷰어 + CPK 빌드 서버 실행
  점검.cmd                환경 점검
  DLC0231_병합.cmd        DLC0323 팩 두 멤버 동시 반영
  읽어보세요.txt          이 문서
  runtime\python\         내장 파이썬 (설치 불필요)
  private\tools\          CPK 생성 도구
  game\PCSG00264\         원본 CPK와 한글 문자표
  viewer\                 뷰어 HTML과 카탈로그
  translations\dialogue\  시나리오 번역 {asset_count}개
  src\, scripts\, config\ 빌드 파이프라인
  work\, output\          작업 폴더와 결과물


4. 문제가 생기면
----------------

  - 점검.cmd 에서 "도구 추출 시험" 이 FAIL
      → ZIP 차단 해제를 안 했거나 Windows Defender가 도구를 지웠습니다.
        설정 → 바이러스 및 위협 방지 → 제외 항목에 이 폴더를 추가한 뒤
        압축을 다시 푸세요.
  - "폴더 경로가 너무 깁니다"
      → C:\SIOK 처럼 짧은 경로로 옮기세요.
  - 포트 오류
      → set SIOK_VIEWER_PORT=8790 후 다시 실행하세요.
  - 브라우저는 Chrome 또는 Edge를 권장합니다.


5. 주의
-------

  private\tools\cpkmakec.exe 는 CRI Middleware의 독점 도구입니다.
  본인이 보유한 도구를 본인 PC 사이에서 옮기는 용도로만 쓰세요.
  이 폴더를 그대로 다른 사람에게 배포하지 마세요.

  서버는 127.0.0.1(내 PC)에서만 열리며 인터넷에 연결하지 않습니다.
  다 쓴 뒤에는 검은 창을 닫아 서버를 끄세요.
"""


# ------------------------------------------------------------------ 스테이징


def stage_offline(stage: Path, catalog: dict) -> int:
    assets = catalog["assets"]
    (stage / "viewer").mkdir(parents=True)
    (stage / "translations" / "dialogue").mkdir(parents=True)
    (stage / "tools").mkdir(parents=True)

    patched = patch_viewer_html(VIEWER_HTML.read_text(encoding="utf-8"))
    (stage / "viewer" / VIEWER_HTML.name).write_text(patched, encoding="utf-8", newline="")

    slugs: dict[str, str] = {}
    for asset in assets:
        source = (VIEWER_DIR / asset["path"]).resolve()
        slug = ascii_slug(source.name)
        if slug in slugs and slugs[slug] != source.name:
            raise BuildError(f"ASCII 파일명이 충돌합니다: {slug}")
        slugs[slug] = source.name
        raw = source.read_bytes()
        (stage / "translations" / "dialogue" / (slug + ".js")).write_bytes(
            wrap_json_bytes(raw, source.name)
        )
        asset["path"] = "../translations/dialogue/" + slug

    if isinstance(catalog.get("basePath"), str):
        catalog["basePath"] = "../translations/dialogue"
    catalog["offlinePackage"] = True
    catalog_bytes = json.dumps(catalog, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    (stage / "viewer" / "scenario-index.json.js").write_bytes(
        wrap_json_bytes(catalog_bytes, "scenario-index.json")
    )
    for name in SUB_CATALOGS:
        source = VIEWER_DIR / name
        (stage / "viewer" / (name + ".js")).write_bytes(
            wrap_json_bytes(source.read_bytes(), name)
        )

    (stage / "뷰어_열기.cmd").write_bytes(offline_launcher_cmd())
    (stage / "뷰어_서버로_열기.cmd").write_bytes(offline_server_cmd())
    serve_ps1 = REPOSITORY_ROOT / "scripts" / "portable" / "Serve-Viewer.ps1"
    if not serve_ps1.is_file():
        raise BuildError(f"PowerShell 서버 파일이 없습니다: {serve_ps1}")
    (stage / "tools" / "Serve-Viewer.ps1").write_bytes(serve_ps1.read_bytes())
    return len(assets)


def stage_full(
    stage: Path,
    catalog: dict,
    game_root: Path,
    tool_dir: Path,
    embed_zip: Path,
) -> dict:
    assets = catalog["assets"]
    # asset["path"]를 슬러그로 바꾸기 전에 원본 CPK를 먼저 해석한다.
    resolved = resolve_sources(assets, game_root)

    # 1) 뷰어: 저장소 원본을 그대로 쓴다. CPK 버튼이 살아 있어야 한다.
    html = VIEWER_HTML.read_text(encoding="utf-8")
    if "OFFLINE_PACKAGE" in html:
        raise BuildError("완전판에는 오프라인 패치가 들어가면 안 됩니다.")
    if 'fetch("/__siok__/build-cpk"' not in html:
        raise BuildError("뷰어 HTML에 CPK 빌드 호출이 없습니다.")
    (stage / "viewer").mkdir(parents=True)
    (stage / "viewer" / VIEWER_HTML.name).write_bytes(VIEWER_HTML.read_bytes())
    for name in SUB_CATALOGS:
        (stage / "viewer" / name).write_bytes((VIEWER_DIR / name).read_bytes())

    # 2) 번역 JSON: 내용은 그대로, 파일명만 ASCII로
    (stage / "translations" / "dialogue").mkdir(parents=True)
    for asset in assets:
        source = (VIEWER_DIR / asset["path"]).resolve()
        slug = ascii_slug(source.name)
        (stage / "translations" / "dialogue" / slug).write_bytes(source.read_bytes())
        asset["path"] = "../translations/dialogue/" + slug
    if isinstance(catalog.get("basePath"), str):
        catalog["basePath"] = "../translations/dialogue"
    catalog["portablePackage"] = "full"
    (stage / "viewer" / "scenario-index.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # 3) 파이프라인 코드
    (stage / "src" / "siok_patch").mkdir(parents=True)
    for module in sorted((REPOSITORY_ROOT / "src" / "siok_patch").glob("*.py")):
        (stage / "src" / "siok_patch" / module.name).write_bytes(module.read_bytes())
    (stage / "scripts").mkdir(parents=True)
    server = REPOSITORY_ROOT / "scripts" / "scenario_viewer_server.py"
    (stage / "scripts" / server.name).write_bytes(server.read_bytes())
    for name in PORTABLE_SCRIPTS:
        source = REPOSITORY_ROOT / "scripts" / "portable" / name
        if not source.is_file():
            raise BuildError(f"휴대판 보조 스크립트가 없습니다: {source}")
        (stage / "scripts" / name).write_bytes(source.read_bytes())

    # 4) 파이썬 임베더블 런타임
    runtime = stage / "runtime" / "python"
    runtime.mkdir(parents=True)
    with zipfile.ZipFile(embed_zip) as archive:
        archive.extractall(runtime)
    pth = next(iter(sorted(runtime.glob("python*._pth"))), None)
    if pth is None:
        raise BuildError("임베더블 파이썬에 ._pth 파일이 없습니다.")
    pth.write_text(PYTHON_PTH, encoding="ascii")
    if not (runtime / "python.exe").is_file():
        raise BuildError("임베더블 파이썬에 python.exe가 없습니다.")

    # 5) CPK 생성 도구
    tools = stage / "private" / "tools"
    tools.mkdir(parents=True)
    for name in TOOL_FILES:
        source = tool_dir / name
        if not source.is_file():
            raise BuildError(f"CPK 도구 파일이 없습니다: {source}")
        (tools / name).write_bytes(source.read_bytes())
    digest = sha256_file(tools / "cpkmakec.exe")
    if digest != EXPECTED_TOOL_SHA:
        raise BuildError(f"cpkmakec.exe 해시가 다릅니다: {digest}")

    # 6) 원본 CPK와 문자표
    bundle_game = stage / "game" / "PCSG00264"
    copied: dict[str, Path] = {}
    for asset_key, info in resolved.items():
        destination = bundle_game / info["target"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        if info["fileName"] not in copied:
            destination.write_bytes(info["sourcePath"].read_bytes())
            copied[info["fileName"]] = destination
        info["sourceSha256"] = sha256_file(copied[info["fileName"]])
    main_table, operation_table = find_wreplace(game_root, [game_root.parent])
    (bundle_game / WREPLACE_MAIN).write_bytes(main_table.read_bytes())
    if operation_table is not None:
        (bundle_game / WREPLACE_OPERATION).write_bytes(operation_table.read_bytes())

    # 7) 원본 대응표
    (stage / "config").mkdir(parents=True)
    table = {
        "format": "siok.portable-source-map",
        "formatVersion": 1,
        "builtOn": date.today().isoformat(),
        "assets": {
            key: {
                "fileName": info["fileName"],
                "target": info["target"],
                "sourceSha256": info["sourceSha256"],
            }
            for key, info in sorted(resolved.items())
        },
    }
    (stage / "config" / "portable-sources.json").write_text(
        json.dumps(table, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # 8) 실행 파일과 안내문
    (stage / "뷰어_실행.cmd").write_bytes(full_launcher_cmd())
    (stage / "점검.cmd").write_bytes(full_selfcheck_cmd())
    (stage / "DLC0231_병합.cmd").write_bytes(full_merge_cmd())
    for name in ("work", "output"):
        (stage / name).mkdir(parents=True, exist_ok=True)
        (stage / name / ".keep").write_bytes(b"")

    return {
        "assets": len(assets),
        "cpks": len(copied),
        "renamed": sum(1 for info in resolved.values() if info["renamed"]),
    }


# ------------------------------------------------------------------ 빌드 검증


def verify_full_builds(stage: Path, limit: int | None) -> dict:
    """번들 안의 내장 파이썬으로 실제 CPK 빌드를 돌려 본다."""

    import subprocess  # noqa: PLC0415 - 검증 단계에서만 필요하다

    python = stage / "runtime" / "python" / "python.exe"
    bootstrap = stage / "scripts" / "portable_bootstrap.py"
    ready = subprocess.run(
        [str(python), "-X", "utf8", str(bootstrap)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if ready.returncode != 0:
        raise BuildError(f"휴대판 설정 준비 실패: {ready.stdout}{ready.stderr}")

    sweep = stage / "_sweep.py"
    sweep.write_text(
        "\n".join(
            [
                "import json, sys, time",
                "from pathlib import Path",
                "ROOT = Path(__file__).resolve().parent",
                "sys.path.insert(0, str(ROOT / 'src'))",
                "from siok_patch.scenario_cpk import build_scenario_cpk",
                "table = json.loads((ROOT / 'config' / 'portable-sources.json').read_text(encoding='utf-8'))['assets']",
                "catalog = json.loads((ROOT / 'viewer' / 'scenario-index.json').read_text(encoding='utf-8'))",
                "limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0",
                "rows = []",
                "assets = catalog['assets'][:limit] if limit else catalog['assets']",
                "for item in assets:",
                "    path = (ROOT / 'viewer' / item['path']).resolve()",
                "    data = json.loads(path.read_text(encoding='utf-8'))",
                "    key = str(data['asset']['assetKey'])",
                "    entry = table.get(key) or {}",
                "    if entry.get('fileName'):",
                "        data['asset']['fileName'] = entry['fileName']",
                "    if entry.get('target'):",
                "        data['asset']['target'] = entry['target']",
                "    started = time.time()",
                "    try:",
                "        report = build_scenario_cpk(data, repository_root=ROOT)",
                "        verification = report.get('verification') or {}",
                "        rows.append({",
                "            'key': key, 'ok': True,",
                "            'sourceCpk': Path(str(report['sourceCpk'])).name,",
                "            'outputBytes': report.get('outputBytes'),",
                "            'outputSha256': report.get('outputSha256'),",
                "            'sourceSha256': report.get('sourceSha256'),",
                "            'verified': bool(verification.get('allPayloadHashesMatch')) and bool(verification.get('sourceUnchanged')),",
                "            'seconds': round(time.time() - started, 2),",
                "        })",
                "    except Exception as error:",
                "        rows.append({'key': key, 'ok': False, 'error': f'{type(error).__name__}: {error}'})",
                "print(json.dumps(rows, ensure_ascii=False))",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [str(python), "-X", "utf8", str(sweep), str(limit or 0)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    sweep.unlink(missing_ok=True)
    if completed.returncode != 0:
        raise BuildError(f"빌드 검증 실패:\n{completed.stdout}\n{completed.stderr}")
    rows = json.loads(completed.stdout.strip().splitlines()[-1])
    failed = [row for row in rows if not row.get("ok")]
    unverified = [row for row in rows if row.get("ok") and not row.get("verified")]
    if failed:
        detail = "\n".join(f"  - {row['key']}: {row['error']}" for row in failed[:10])
        raise BuildError(f"CPK 빌드에 실패한 자산 {len(failed)}개:\n{detail}")
    if unverified:
        keys = ", ".join(row["key"] for row in unverified[:10])
        raise BuildError(f"재추출 검증을 통과하지 못한 자산 {len(unverified)}개: {keys}")

    # 검증에 성공한 결과 해시를 대응표에 남겨 대상 PC에서 대조할 수 있게 한다.
    table_path = stage / "config" / "portable-sources.json"
    table = json.loads(table_path.read_text(encoding="utf-8"))
    for row in rows:
        entry = table["assets"].get(row["key"])
        if entry is not None:
            entry["goldenOutputSha256"] = row["outputSha256"]
            entry["goldenOutputBytes"] = row["outputBytes"]
    table["verifiedAssets"] = len(rows)
    table_path.write_text(
        json.dumps(table, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # 검증하며 생긴 작업물은 배포판에 넣지 않는다.
    for name in ("work", "output"):
        shutil.rmtree(stage / name, ignore_errors=True)
        (stage / name).mkdir(parents=True, exist_ok=True)
        (stage / name / ".keep").write_bytes(b"")
    (stage / "private" / "project.local.json").unlink(missing_ok=True)
    return {
        "count": len(rows),
        "seconds": round(sum(row.get("seconds") or 0 for row in rows), 1),
    }


# --------------------------------------------------------------------- 마무리


def make_zip(stage: Path, output_dir: Path, name: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    zip_path = output_dir / name
    if zip_path.exists():
        zip_path.unlink()
    files = sorted(path for path in stage.rglob("*") if path.is_file())
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in files:
            arcname = stage.name + "/" + path.relative_to(stage).as_posix()
            archive.write(path, arcname)
    return zip_path


def report(stage: Path, zip_path: Path, extra: dict) -> None:
    files = [path for path in stage.rglob("*") if path.is_file()]
    staged_total = sum(path.stat().st_size for path in files)
    for label, value in extra.items():
        print(f"{label:18}: {value}")
    print(f"{'묶은 파일':18}: {len(files):,}개")
    print(f"{'배포판 폴더 합계':18}: {staged_total:,} 바이트")
    print(f"{'ZIP 크기':18}: {zip_path.stat().st_size:,} 바이트")
    print(f"{'ZIP SHA-256':18}: {sha256_file(zip_path)}")
    print(f"{'ZIP 경로':18}: {zip_path}")


def build(args: argparse.Namespace) -> Path:
    catalog = read_catalog()
    built_on = date.today().isoformat()
    stamp = built_on.replace("-", "")
    stage = Path(args.stage_dir) if args.stage_dir else None

    if args.profile == "offline":
        stage = stage or REPOSITORY_ROOT / "output" / "시나리오_대사_뷰어_오프라인"
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True)
        count = stage_offline(stage, catalog)
        (stage / "읽어보세요.txt").write_bytes(
            BOM + offline_readme(count, built_on).replace("\n", "\r\n").encode("utf-8")
        )
        zip_path = make_zip(
            stage, Path(args.output_dir), f"시나리오_대사_뷰어_오프라인_{stamp}.zip"
        )
        report(stage, zip_path, {"배포판": "오프라인(뷰어 전용)", "시나리오": f"{count}개"})
    else:
        stage = stage or REPOSITORY_ROOT / "output" / "SIOK_VIEWER_FULL"
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True)
        embed_zip = Path(args.python_embed)
        if not embed_zip.is_file():
            raise BuildError(f"파이썬 임베더블 ZIP이 없습니다: {embed_zip}")
        summary = stage_full(
            stage,
            catalog,
            Path(args.game_root),
            Path(args.tool_dir),
            embed_zip,
        )
        verified = {}
        if args.verify_builds:
            print("CPK 빌드 검증 중… (자산 수에 따라 몇 분 걸립니다)")
            verified = verify_full_builds(stage, args.verify_limit)
            print(f"  검증 완료 · {verified['count']}개 · {verified['seconds']}초")
        (stage / "읽어보세요.txt").write_bytes(
            BOM
            + full_readme(summary["assets"], summary["cpks"], built_on)
            .replace("\n", "\r\n")
            .encode("utf-8")
        )
        zip_path = make_zip(stage, Path(args.output_dir), f"SIOK_VIEWER_FULL_{stamp}.zip")
        extra = {
            "배포판": "완전판(뷰어 + CPK 빌드)",
            "시나리오": f"{summary['assets']}개",
            "원본 CPK": f"{summary['cpks']}개",
            "이름 대응": f"{summary['renamed']}개",
        }
        if verified:
            extra["빌드 검증"] = f"{verified['count']}개 통과"
        report(stage, zip_path, extra)

    if not args.keep_stage:
        shutil.rmtree(stage, ignore_errors=True)
    else:
        print(f"{'배포판 폴더':18}: {stage}")
    return zip_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        choices=("offline", "full"),
        default="offline",
        help="offline: 뷰어 전용 · full: CPK 빌드까지 되는 완전판",
    )
    parser.add_argument("--output-dir", default=str(REPOSITORY_ROOT / "output"))
    parser.add_argument("--stage-dir", default="")
    parser.add_argument("--keep-stage", action="store_true")
    parser.add_argument(
        "--game-root",
        default=str(REPOSITORY_ROOT.parent / "PCSG00264"),
        help="원본 게임 폴더 (완전판)",
    )
    parser.add_argument(
        "--tool-dir",
        default=str(Path(os.path.expanduser("~")) / "Desktop" / "crifilesystem"),
        help="cpkmakec.exe와 실행 DLL이 있는 폴더 (완전판)",
    )
    parser.add_argument(
        "--python-embed",
        default="",
        help="파이썬 임베더블 ZIP 경로 (완전판)",
    )
    parser.add_argument(
        "--verify-builds",
        action="store_true",
        help="완전판을 만든 뒤 내장 파이썬으로 실제 CPK 빌드를 검증한다",
    )
    parser.add_argument(
        "--verify-limit",
        type=int,
        default=0,
        help="검증할 자산 수 상한 (0이면 전체)",
    )
    args = parser.parse_args(argv)
    try:
        build(args)
    except BuildError as error:
        print(f"[!] {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
