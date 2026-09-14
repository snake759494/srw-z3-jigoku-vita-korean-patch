"""자동 번역 결과에서 누락된 기호형 제어 코드를 복원한다.

번역 서비스가 ``⑲``/``㊥``/``㊦``/``㊧``/``㊨``를 일반 문자로 오인해
삭제하는 경우가 있어, 로컬 작업 JSON의 새 번역에 원문과 같은 기호 토큰
수열을 다시 넣고 공개 오버레이를 재생성한다. 달러형 제어 코드의 위치는
그대로 보존한다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path

from auto_translate_retranslation import _update_progress
from siok_patch.config import project_root
from siok_patch.hashes import sha256_file
from siok_patch.retranslation import publish_retranslation_task
from siok_patch.translation_io import read_tsv


SYMBOL_TOKEN = re.compile(r"[⑲⑳㊥㊦㊧㊨]")


def _write_json_atomic(value: object, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=f"{path.name}.", suffix=".candidate", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _repair_task(task_path: Path, root: Path) -> int:
    task = json.loads(task_path.read_text(encoding="utf-8"))
    changed = 0
    for entry in task.get("entries", []):
        source = str(entry.get("sourceText", ""))
        translation = str(entry.get("freshTranslation", ""))
        source_symbols = SYMBOL_TOKEN.findall(source)
        target_symbols = SYMBOL_TOKEN.findall(translation)
        if source_symbols == target_symbols:
            continue
        # 기호형 토큰을 한 번 모두 제거한 뒤 원문 수열을 끝에 붙인다.
        # 달러형 토큰($n/$l/$c/$F)은 건드리지 않아 대사 삽입 위치를 보존한다.
        cleaned = SYMBOL_TOKEN.sub("", translation)
        entry["freshTranslation"] = cleaned + "".join(source_symbols)
        previous = str(entry.get("translator", "")).strip()
        entry["translator"] = (
            f"{previous}; Codex 제어 토큰 보정" if previous else "Codex 제어 토큰 보정"
        )
        entry["notes"] = (
            str(entry.get("notes", "")).strip()
            + ("; " if entry.get("notes") else "")
            + "원문 기호형 제어 토큰 수열을 자동 복원"
        )
        changed += 1
    if not changed:
        return 0
    _write_json_atomic(task, task_path)
    scope = str(task["scope"])
    asset = str(task["assetKey"])
    output = root / "translations" / "retranslation" / scope / f"{asset}.json"
    publish_retranslation_task(task_path, output_path=output, overwrite=True)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scope", action="append", choices=("stage", "dlc", "dictionary")
    )
    args = parser.parse_args()
    root = project_root()
    scopes = set(args.scope or ("stage", "dlc", "dictionary"))
    total = 0
    assets = 0
    for task_path in sorted((root / "work" / "retranslation" / "tasks").glob("*/*.json")):
        task = json.loads(task_path.read_text(encoding="utf-8"))
        if task.get("scope") not in scopes:
            continue
        changed = _repair_task(task_path, root)
        if changed:
            assets += 1
            total += changed
            print(f"보정: {task['scope']}/{task['assetKey']} ({changed}행)", flush=True)
    source = root / "work" / "normalized" / "translations.tsv"
    _update_progress(root, read_tsv(source), sha256_file(source))
    print(f"제어 토큰 보정 완료: 자산 {assets}개 / 행 {total:,}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
