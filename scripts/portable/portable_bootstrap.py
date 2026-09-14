"""휴대판 번들 실행 준비.

번들 폴더는 PC마다 위치가 다르고 ``siok_patch``의 경로 설정은 절대 경로만
받아들이므로, 실행할 때마다 ``private/project.local.json``을 번들 위치에 맞춰
다시 만든다. 폴더를 옮기거나 이름을 바꿔도 다음 실행에서 스스로 복구된다.

이 파일은 배포 번들에만 들어간다. 저장소 작업에는 쓰지 않는다.
"""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# work/cpk-runs/<한글 시나리오 키>/<34자 실행ID>/extracted-original/ID00007 까지
# 94자가 더 붙는다. MAX_PATH(260) 안에서 안전하도록 뿌리 경로를 제한한다.
MAX_ROOT_LENGTH = 120


def main() -> int:
    game = ROOT / "game" / "PCSG00264"
    tool = ROOT / "private" / "tools" / "cpkmakec.exe"
    table = game / "Japanese - Hangul to Kanji.wReplace"
    operation_table = game / "Japanese - Hangul to Kanji(oper).wReplace"

    missing = [path for path in (game, tool, table) if not path.exists()]
    if missing:
        print("[!] 번들 파일이 없습니다. ZIP을 폴더째 다시 푸세요.")
        for path in missing:
            print(f"    없음: {path}")
        return 1

    if len(str(ROOT)) > MAX_ROOT_LENGTH:
        print(f"[!] 폴더 경로가 너무 깁니다({len(str(ROOT))}자 · 한도 {MAX_ROOT_LENGTH}자).")
        print(r"    C:\SIOK 처럼 짧은 경로에 다시 푸세요.")
        return 1

    try:
        probe = ROOT / "work" / ".write-probe"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as error:
        print(f"[!] 이 폴더에 파일을 쓸 수 없습니다: {error}")
        print(r"    바탕화면이나 C:\SIOK 같은 쓰기 가능한 위치로 옮기세요.")
        return 1

    (ROOT / "output").mkdir(parents=True, exist_ok=True)

    config = {
        "archiveRoot": str(game),
        "cpkToolPath": str(tool),
        "wReplacePath": str(table),
        "wReplaceOperationPath": str(operation_table) if operation_table.is_file() else "",
        "mainGameRoot": "",
        "dlcGameRoot": "",
    }
    destination = ROOT / "private" / "project.local.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"설정 준비 완료 · {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
