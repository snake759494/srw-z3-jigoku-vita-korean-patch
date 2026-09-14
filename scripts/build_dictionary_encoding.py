#!/usr/bin/env python3
"""사전 추출·패치에 필요한 최소 인코딩 표를 저장소 JSON으로 만든다.

원본 작업 폴더의 ``shift-jis2.tbl``과 ``Japanese - Hangul to Kanji.wReplace``를
읽지만 게임 파일은 읽지 않는다. 생성 결과는 문자표의 바이트 매핑과
한글→게임 글리프 치환표뿐이며, 게임 아카이브·실행 파일·비밀키를 포함하지
않는다.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
from typing import Iterable


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPOSITORY_ROOT / "config" / "encoding" / "dictionary.json"


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _tbl(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-16").splitlines(), 1):
        if "=" not in line or line.lstrip().startswith(("#", ";", "//")):
            continue
        key, text = line.split("=", 1)
        key = key.strip().upper()
        if not key or not text:
            continue
        try:
            bytes.fromhex(key)
        except ValueError as error:
            raise ValueError(f"TBL {line_number}행의 바이트가 잘못되었습니다: {key!r}") from error
        if text in result.values():
            raise ValueError(f"TBL 문자 토큰이 중복됩니다: {text!r}")
        result[key] = text
    if not result:
        raise ValueError(f"TBL 항목이 없습니다: {path}")
    return result


def _wreplace(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    duplicate_targets: dict[str, list[str]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-16").splitlines(), 1):
        if not line or line.lstrip().startswith(("#", ";", "//")):
            continue
        columns = line.split("\t")
        if len(columns) < 2 or not columns[0] or not columns[1]:
            continue
        source, target = columns[0], columns[1]
        result.setdefault(source, target)
        duplicate_targets.setdefault(target, []).append(source)
    if not result:
        raise ValueError(f"wReplace 항목이 없습니다: {path}")
    return result


def build_document(tbl_path: Path, wreplace_path: Path) -> dict[str, object]:
    table = _tbl(tbl_path)
    replacement = _wreplace(wreplace_path)
    return {
        "format": "siok.dictionary-encoding",
        "formatVersion": 1,
        "encoding": "shift-jis2.tbl",
        "table": table,
        "replacement": replacement,
        "source": {
            "tableFileName": tbl_path.name,
            "tableSha256": _sha256(tbl_path),
            "wReplaceFileName": wreplace_path.name,
            "wReplaceSha256": _sha256(wreplace_path),
        },
        "policy": {
            "gameBinaryIncluded": False,
            "requiresUserOwnedGame": True,
            "note": "문자표와 치환 규칙만 보관하며, 원본 게임 파일은 포함하지 않는다.",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", type=Path, required=True, help="shift-jis2.tbl")
    parser.add_argument("--wreplace", type=Path, required=True, help="한글→한자 wReplace")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    if not output.is_relative_to(REPOSITORY_ROOT):
        parser.error("출력은 저장소 안이어야 합니다.")
    try:
        document = build_document(args.table.resolve(strict=True), args.wreplace.resolve(strict=True))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps({"output": str(output), "tableRows": len(document["table"]), "replacementRows": len(document["replacement"])}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
