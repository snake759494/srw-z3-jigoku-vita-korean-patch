"""휴대판 번들 사전 점검.

브라우저를 열기 전에 CPK 빌드가 실제로 가능한지 확인한다. DLL 누락,
SmartScreen/Defender 차단, .NET 부재, 쓰기 권한 문제를 사용자가 알아볼 수 있는
문장으로 알려 준다.

이 파일은 배포 번들에만 들어간다.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

EXPECTED_TOOL_SHA = "8cea88e11a5460ff8c4a2cc6b5fd214ebeb40e5a661aec596925538267ab4954"
TOOL_FILES = (
    "cpkmakec.exe",
    "CpkMaker.dll",
    "CpkBinder.dll",
    "msvcp100.dll",
    "msvcr100.dll",
)
CLR_FAILURE_HINT = {
    "0xe0434352": ".NET 예외 · CpkMaker.dll/CpkBinder.dll 누락이거나 .NET Framework 문제",
    "0xc0000135": "필요한 DLL을 찾지 못했습니다 · msvcp100.dll / msvcr100.dll 확인",
}

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    suffix = f" · {detail}" if detail else ""
    print(f"  [{mark}] {label}{suffix}")
    if not ok:
        failures.append(label)
    return ok


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    print("휴대판 번들 점검")
    print(f"  번들 위치 : {ROOT}")
    print()

    print("[1] 실행 환경")
    check("파이썬 3.10 이상", sys.version_info >= (3, 10), sys.version.split()[0])
    check("src 경로 연결", (ROOT / "src") in [Path(p) for p in sys.path])
    check(
        f"폴더 경로 길이 {len(str(ROOT))}자",
        len(str(ROOT)) <= 120,
        "한도 120자 · 넘으면 C:\\SIOK 로 옮기세요",
    )
    try:
        probe = ROOT / "work" / ".selfcheck"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"ok")
        probe.unlink()
        writable = True
    except OSError:
        writable = False
    check("work 폴더 쓰기 가능", writable)

    print()
    print("[2] 파일 확인")
    catalog_path = ROOT / "viewer" / "scenario-index.json"
    catalog_ok = catalog_path.is_file()
    asset_count = 0
    if catalog_ok:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        asset_count = len(catalog.get("assets") or [])
    check("시나리오 카탈로그", catalog_ok, f"{asset_count}개")
    json_count = len(list((ROOT / "translations" / "dialogue").glob("*.json")))
    check("시나리오 JSON", json_count == asset_count, f"{json_count}개")
    cpk_count = len(list((ROOT / "game" / "PCSG00264").rglob("*.cpk")))
    check("원본 CPK", cpk_count > 0, f"{cpk_count}개")
    table_ok = (ROOT / "game" / "PCSG00264" / "Japanese - Hangul to Kanji.wReplace").is_file()
    check("한글 문자표(wReplace)", table_ok)

    tools_dir = ROOT / "private" / "tools"
    for name in TOOL_FILES:
        check(f"도구 {name}", (tools_dir / name).is_file())
    tool_path = tools_dir / "cpkmakec.exe"
    if tool_path.is_file():
        digest = sha256_file(tool_path)
        check(
            "cpkmakec.exe 해시",
            digest == EXPECTED_TOOL_SHA,
            digest[:16] + "…",
        )
    else:
        print("      → Windows Defender가 지웠을 수 있습니다.")
        print("        설정 → 바이러스 및 위협 방지 → 제외 항목에 이 폴더를 추가하세요.")

    print()
    print("[3] CPK 도구 실제 동작")
    sample = next((ROOT / "game" / "PCSG00264").rglob("*.cpk"), None)
    if sample is None or not tool_path.is_file():
        check("도구 추출 시험", False, "시험할 파일이 없습니다")
    else:
        from siok_patch.cpk_tool import CpkMakerTool, CpkToolError  # noqa: PLC0415

        work = Path(tempfile.mkdtemp(prefix="siok-selfcheck-", dir=str(ROOT / "work")))
        try:
            tool = CpkMakerTool(
                tool_path, EXPECTED_TOOL_SHA, log_path=work / "tool.jsonl"
            )
            entries = tool.extract(sample, work / "extracted")
            check("도구 추출 시험", bool(entries), f"{sample.name} · 멤버 {len(entries)}개")
        except CpkToolError as error:
            text = str(error)
            hint = ""
            for code, message in CLR_FAILURE_HINT.items():
                if code in text.lower():
                    hint = message
                    break
            check("도구 추출 시험", False, hint or text.splitlines()[0][:120])
            print("      → ZIP 속성에서 '차단 해제'를 켠 뒤 다시 풀어 보세요.")
        except Exception as error:  # pragma: no cover - 예상 못 한 환경 문제
            check("도구 추출 시험", False, f"{type(error).__name__}: {error}")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    print()
    print("[4] 경로 설정")
    config_path = ROOT / "private" / "project.local.json"
    if check("project.local.json 생성됨", config_path.is_file()):
        config = json.loads(config_path.read_text(encoding="utf-8"))
        for key in ("archiveRoot", "cpkToolPath", "wReplacePath"):
            value = config.get(key) or ""
            check(f"  {key}", bool(value) and Path(value).exists(), value)

    print()
    if failures:
        print(f"[!] {len(failures)}개 항목이 실패했습니다:")
        for item in failures:
            print(f"    - {item}")
        print("    읽어보세요.txt의 '문제가 생기면' 항목을 확인하세요.")
        return 1
    print("모든 점검을 통과했습니다. 뷰어_실행.cmd 로 시작하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
