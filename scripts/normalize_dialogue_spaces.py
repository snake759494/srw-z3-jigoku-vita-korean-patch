#!/usr/bin/env python3
"""공개 대사 JSON의 번역 문자열을 반각 일반 공백으로 통일한다.

원문(``sourceText``), 원문 매칭용 페이로드, 게임 스크립트 구조용 전각
공백은 건드리지 않는다. 정확히 ``translation`` 키만 바꾸고, 같은 객체에
있는 ``translationTextSha256``만 새 문자열에 맞춰 갱신한다.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPOSITORY_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from siok_patch.text_normalization import FULLWIDTH_SPACE, normalize_dialogue_spaces  # noqa: E402


DEFAULT_ROOTS = (
    REPOSITORY_ROOT / "translations" / "dialogue",
    REPOSITORY_ROOT / "translations" / "battle-dialogue",
    REPOSITORY_ROOT / "translations" / "dictionary",
)


def _hash_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _normalize_node(node: Any) -> tuple[int, int, int]:
    """번역 문자열·해시를 갱신하고 (문자열 수, 공백 수, 해시 수)를 반환한다."""

    changed = 0
    spaces = 0
    hashes = 0
    if isinstance(node, dict):
        value = node.get("translation")
        if isinstance(value, str):
            count = value.count(FULLWIDTH_SPACE)
            normalized = normalize_dialogue_spaces(value)
            if normalized != value:
                node["translation"] = normalized
                changed += 1
                spaces += count
            if isinstance(node.get("translationTextSha256"), str):
                expected_hash = _hash_text(normalized)
                if node["translationTextSha256"].lower() != expected_hash:
                    node["translationTextSha256"] = expected_hash
                    hashes += 1
        for child in node.values():
            child_changed, child_spaces, child_hashes = _normalize_node(child)
            changed += child_changed
            spaces += child_spaces
            hashes += child_hashes
    elif isinstance(node, list):
        for child in node:
            child_changed, child_spaces, child_hashes = _normalize_node(child)
            changed += child_changed
            spaces += child_spaces
            hashes += child_hashes
    return changed, spaces, hashes


def _paths(roots: tuple[Path, ...]) -> list[Path]:
    paths: set[Path] = set()
    for root in roots:
        if root.is_file() and root.suffix.lower() == ".json":
            paths.add(root.resolve())
        elif root.is_dir():
            paths.update(item.resolve() for item in root.rglob("*.json"))
    return sorted(paths, key=lambda item: str(item).casefold())


def normalize_files(paths: list[Path], *, write: bool) -> dict[str, Any]:
    report: dict[str, Any] = {
        "format": "siok.dialogue-space-normalization",
        "mode": "write" if write else "check",
        "files": 0,
        "changedFiles": 0,
        "changedTranslations": 0,
        "replacedFullwidthSpaces": 0,
        "updatedTranslationHashes": 0,
        "paths": [],
    }
    for path in paths:
        raw = path.read_bytes()
        encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
        newline = "\r\n" if b"\r\n" in raw else "\n"
        data = json.loads(raw.decode(encoding))
        changed, spaces, hashes = _normalize_node(data)
        report["files"] += 1
        report["changedTranslations"] += changed
        report["replacedFullwidthSpaces"] += spaces
        report["updatedTranslationHashes"] += hashes
        if changed or hashes:
            report["changedFiles"] += 1
            report["paths"].append({"path": str(path), "translations": changed, "spaces": spaces, "hashes": hashes})
            if write:
                text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
                text = text.replace("\n", newline)
                if encoding == "utf-8-sig":
                    path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
                else:
                    path.write_text(text, encoding="utf-8", newline="")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", type=Path, help="검사할 JSON 파일 또는 폴더(반복 가능)")
    parser.add_argument("--check", action="store_true", help="파일을 쓰지 않고 변경 예정만 보고")
    args = parser.parse_args()
    roots = tuple((item.expanduser().resolve() for item in args.root)) if args.root else DEFAULT_ROOTS
    try:
        report = normalize_files(_paths(roots), write=not args.check)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
