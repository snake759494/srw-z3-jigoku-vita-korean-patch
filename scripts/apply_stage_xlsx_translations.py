#!/usr/bin/env python3
"""스테이지 XLSX의 최종 번역 열을 시나리오 검수 JSON에 반영한다.

기본 원칙은 ``제미니번역``(열 사이의 공백 유무는 무시)이다. 구형 원고에
제미니 열이 없는 경우에만 고정 슬롯의 ``번역`` 열, 분기 원고의 ``웹번역``
열을 순서대로 fallback으로 사용한다. JSON의 sourceArtifact/sourceRow와
원문을 함께 대조하므로 행 번호가 어긋난 파일은 조용히 덮어쓰지 않는다.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import unicodedata
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from siok_patch.control_codes import control_signature, extract_control_tokens  # noqa: E402
from siok_patch.xlsx_reader import XlsxError, open_xlsx  # noqa: E402


SOURCE_HEADERS = {"원문", "내용", "문자열"}
TARGET_PRIORITY = (
    ("gemini", "제미니번역"),
    ("fixed-slot", "번역"),
    ("web-fallback", "웹번역"),
    ("legacy-fallback", "기존번역"),
)
COMPILED_HEADERS = {"한글폰트로", "한글폰트"}
LENGTH_HEADERS = {"길이"}
BYTE_LIMIT_HEADERS = {"바이트길이", "크기"}


class ApplyError(RuntimeError):
    """엑셀과 JSON을 안전하게 대조하지 못했을 때 발생한다."""


@dataclass(frozen=True)
class XlsxTable:
    path: Path
    sheet_name: str
    source_column: int
    translation_column: int
    translation_kind: str
    compiled_column: int | None
    length_column: int | None
    byte_limit_column: int | None
    rows: dict[int, tuple[str, str, str, int | None, int | None]]

    def row(self, number: int, source_text: str) -> tuple[str, str, int | None, int | None]:
        value = self.rows.get(number)
        if value is None:
            raise ApplyError(
                f"{self.path.name} [{self.sheet_name}]의 {number}행을 찾을 수 없습니다."
            )
        actual_source, translation, compiled, length, byte_limit = value
        if actual_source != source_text:
            raise ApplyError(
                f"{self.path.name} [{self.sheet_name}] {number}행 원문이 다릅니다: "
                f"JSON={source_text!r}, XLSX={actual_source!r}"
            )
        return translation, compiled, length, byte_limit


def _header_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "")
    normalized = normalized.replace("\ufeff", "").strip()
    normalized = re.sub(r"[\s\u3000]+", "", normalized)
    return normalized.casefold()


def _parse_integer(value: str) -> int | None:
    text = (value or "").strip().replace(",", "")
    if not text or not re.fullmatch(r"[0-9]+", text):
        return None
    return int(text, 10)


def _find_header(rows: list[Any]) -> tuple[int, list[str]]:
    source_keys = {_header_key(item) for item in SOURCE_HEADERS}
    for index, row in enumerate(rows[:30]):
        headers = list(row.values)
        if any(_header_key(value) in source_keys for value in headers):
            return index, headers
    raise ApplyError("앞 30행 안에서 원문/내용/문자열 열을 찾지 못했습니다.")


def _choose_column(headers: list[str]) -> tuple[int, str]:
    keys = [_header_key(value) for value in headers]
    for kind, wanted in TARGET_PRIORITY:
        wanted_key = _header_key(wanted)
        matches = [
            index
            for index, key in enumerate(keys)
            if key == wanted_key
            or (wanted_key == _header_key("웹번역") and key.startswith(wanted_key))
        ]
        if matches:
            return matches[0], kind
    raise ApplyError(
        "제미니번역/번역/웹번역/기존번역 열을 찾지 못했습니다: "
        + repr(headers)
    )


def _load_tables(xlsx_root: Path) -> tuple[dict[str, list[XlsxTable]], dict[str, int]]:
    if not xlsx_root.is_dir():
        raise ApplyError(f"XLSX 폴더가 없습니다: {xlsx_root}")
    tables: dict[str, list[XlsxTable]] = {}
    kind_counts: dict[str, int] = {}
    paths = sorted(xlsx_root.glob("*.xlsx"), key=lambda item: item.name.casefold())
    if not paths:
        raise ApplyError(f"XLSX 파일이 없습니다: {xlsx_root}")
    for path in paths:
        try:
            workbook = open_xlsx(path)
            for sheet in workbook.sheets:
                rows = list(workbook.iter_rows(sheet))
                header_index, headers = _find_header(rows)
                header_keys = [_header_key(value) for value in headers]
                source_column = next(
                    (
                        index
                        for index, key in enumerate(header_keys)
                        if key in {_header_key(item) for item in SOURCE_HEADERS}
                    ),
                    None,
                )
                if source_column is None:
                    raise ApplyError(f"{path.name} [{sheet.name}] 원문 열이 없습니다.")
                translation_column, kind = _choose_column(headers)
                compiled_column = next(
                    (
                        index
                        for index, key in enumerate(header_keys)
                        if key in {_header_key(item) for item in COMPILED_HEADERS}
                    ),
                    None,
                )
                length_column = next(
                    (
                        index
                        for index, key in enumerate(header_keys)
                        if key in {_header_key(item) for item in LENGTH_HEADERS}
                    ),
                    None,
                )
                byte_limit_column = next(
                    (
                        index
                        for index, key in enumerate(header_keys)
                        if key in {_header_key(item) for item in BYTE_LIMIT_HEADERS}
                    ),
                    None,
                )
                row_map: dict[int, tuple[str, str, str, int | None, int | None]] = {}
                for row in rows[header_index + 1 :]:
                    values = row.values
                    source = values[source_column] if source_column < len(values) else ""
                    if not source:
                        continue
                    translation = (
                        values[translation_column]
                        if translation_column < len(values)
                        else ""
                    )
                    compiled = (
                        values[compiled_column]
                        if compiled_column is not None and compiled_column < len(values)
                        else ""
                    )
                    length = (
                        _parse_integer(values[length_column])
                        if length_column is not None and length_column < len(values)
                        else None
                    )
                    byte_limit = (
                        _parse_integer(values[byte_limit_column])
                        if byte_limit_column is not None
                        and byte_limit_column < len(values)
                        else None
                    )
                    row_map[row.number] = (source, translation, compiled, length, byte_limit)
                table = XlsxTable(
                    path=path,
                    sheet_name=sheet.name,
                    source_column=source_column,
                    translation_column=translation_column,
                    translation_kind=kind,
                    compiled_column=compiled_column,
                    length_column=length_column,
                    byte_limit_column=byte_limit_column,
                    rows=row_map,
                )
                tables.setdefault(path.name.casefold(), []).append(table)
                kind_counts[kind] = kind_counts.get(kind, 0) + 1
        except (OSError, XlsxError) as error:
            raise ApplyError(f"XLSX 읽기 실패: {path} ({error})") from error
    return tables, kind_counts


def _find_table(
    tables: dict[str, list[XlsxTable]], artifact_name: str, row_number: int, source_text: str
) -> XlsxTable:
    candidates = tables.get(Path(artifact_name.replace("\\", "/")).name.casefold(), [])
    if not candidates:
        raise ApplyError(f"sourceArtifact XLSX를 찾지 못했습니다: {artifact_name}")
    matches: list[XlsxTable] = []
    for table in candidates:
        value = table.rows.get(row_number)
        if value is not None and value[0] == source_text:
            matches.append(table)
    if len(matches) != 1:
        detail = ", ".join(table.sheet_name for table in matches) or "일치 없음"
        raise ApplyError(
            f"{Path(artifact_name).name} {row_number}행의 원문/시트 매칭이 모호합니다: {detail}"
        )
    return matches[0]


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _backup_files(paths: list[Path], backup_dir: Path) -> None:
    backup_dir.mkdir(parents=True, exist_ok=False)
    for path in paths:
        shutil.copy2(path, backup_dir / path.name)


def apply(
    *,
    xlsx_root: Path,
    json_root: Path,
    backup_dir: Path | None,
) -> dict[str, Any]:
    tables, kind_counts = _load_tables(xlsx_root)
    json_paths = sorted(json_root.glob("scenario_STG*.json"), key=lambda item: item.name.casefold())
    if not json_paths:
        raise ApplyError(f"스테이지 JSON이 없습니다: {json_root}")
    if backup_dir is not None:
        _backup_files(json_paths, backup_dir)

    changed_entries = 0
    changed_files = 0
    total_entries = 0
    source_checks = 0
    for path in json_paths:
        try:
            document = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ApplyError(f"JSON 읽기 실패: {path} ({error})") from error
        entries = document.get("entries")
        if not isinstance(entries, list) or not entries:
            raise ApplyError(f"entries가 비어 있거나 배열이 아닙니다: {path}")
        file_changed = False
        translated_entries = 0
        for entry in entries:
            if not isinstance(entry, dict):
                raise ApplyError(f"entry가 객체가 아닙니다: {path}")
            location = entry.get("location") or {}
            artifact = str(location.get("sourceArtifact") or "")
            row_number = int(location.get("sourceRow") or 0)
            source_text = str(entry.get("sourceText") or "")
            table = _find_table(tables, artifact, row_number, source_text)
            translation, compiled, length, byte_limit = table.row(row_number, source_text)
            source_checks += 1
            old_translation = str(entry.get("translation") or "")
            if old_translation != translation:
                changed_entries += 1
                file_changed = True
            entry["translation"] = translation
            entry["translationTextSha256"] = _hash_text(translation)
            if translation.strip():
                translated_entries += 1

            references = entry.get("references")
            if isinstance(references, dict):
                references["importedTranslation"] = translation

            controls = entry.get("controls")
            if isinstance(controls, dict):
                controls["sourceTokens"] = list(extract_control_tokens(source_text))
                controls["translationTokens"] = list(extract_control_tokens(translation))
                controls["sourceSignature"] = control_signature(source_text)
                controls["translationSignature"] = control_signature(translation)

            metadata = entry.get("metadata")
            if isinstance(metadata, dict):
                if compiled:
                    metadata["replacementText"] = compiled
                if length is not None:
                    metadata["encodedLength"] = length
                if byte_limit is not None:
                    metadata["byteLimit"] = byte_limit
        counts = document.get("counts")
        if isinstance(counts, dict):
            counts["translatedEntries"] = translated_entries
        total_entries += len(entries)
        if file_changed:
            changed_files += 1
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    return {
        "xlsxRoot": str(xlsx_root.resolve()),
        "jsonRoot": str(json_root.resolve()),
        "xlsxFiles": len({table.path for values in tables.values() for table in values}),
        "jsonFiles": len(json_paths),
        "entries": total_entries,
        "sourceChecks": source_checks,
        "changedEntries": changed_entries,
        "changedFiles": changed_files,
        "columnKinds": kind_counts,
        "backupDir": str(backup_dir.resolve()) if backup_dir else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xlsx-root", type=Path, required=True, help="01_스테이지 xlsx 모음 폴더")
    parser.add_argument(
        "--json-root",
        type=Path,
        default=ROOT / "translations" / "dialogue",
        help="시나리오 JSON 폴더",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        help="교체 전 JSON 백업 폴더(생략하면 백업하지 않음)",
    )
    args = parser.parse_args()
    backup_dir = args.backup_dir
    if backup_dir is not None and not backup_dir.is_absolute():
        backup_dir = ROOT / backup_dir
    try:
        report = apply(
            xlsx_root=args.xlsx_root.resolve(),
            json_root=args.json_root.resolve(),
            backup_dir=backup_dir.resolve() if backup_dir else None,
        )
    except (ApplyError, OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
