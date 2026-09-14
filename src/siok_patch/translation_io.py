"""기존 XLSX 번역 원고를 정규 TSV 행으로 가져온다."""

from __future__ import annotations

import csv
from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from typing import Any, Iterable, Mapping, Sequence

from .control_codes import (
    compare_control_tokens,
    control_signature,
    describe_control_mismatch,
)
from .xlsx_reader import XlsxRow, open_xlsx


TSV_COLUMNS = (
    "entry_id",
    "scope",
    "asset_key",
    "internal_id",
    "source_row",
    "source_artifact",
    "source_artifact_sha256",
    "payload_sha256",
    "source_offset",
    "source_text",
    "google_translation",
    "legacy_translation",
    "translation",
    "replacement_text",
    "byte_limit",
    "encoded_length",
    "control_signature",
    "status",
    "reviewer",
    "notes",
)

_DEFAULT_ALIASES: dict[str, tuple[str, ...]] = {
    "source_text": ("원문", "문자열", "내용"),
    "translation": ("제미니번역", "기존번역", "웹번역", "번역"),
    "replacement_text": ("한글폰트로", "한글폰트"),
    "google_translation": ("구글번역",),
    "legacy_translation": ("기존번역", "웹번역", "번역"),
    "source_offset": ("시작주소", "위치"),
    "source_end_offset": ("끝주소",),
    "byte_limit": ("바이트길이", "크기"),
    "encoded_length": ("길이",),
    "reviewer": ("검수",),
}
_ID_PATTERN = re.compile(r"(?i)ID[0-9]{5}")
_ASSET_PATTERN = re.compile(r"(?i)(?:STG[0-9]+[A-Za-z]?|DLC[0-9]+)")
_HEADER_SCAN_LIMIT = 30


class TranslationImportError(RuntimeError):
    """번역 원고를 모호함 없이 가져올 수 없을 때 발생한다."""


@dataclass(frozen=True, slots=True)
class AssetSource:
    """원본 아카이브 안의 읽기 전용 XLSX 자산."""

    group: str
    path: Path
    relative_path: str


@dataclass(frozen=True, slots=True)
class TranslationRow:
    """정규 번역 TSV 한 행."""

    entry_id: str
    scope: str
    asset_key: str
    internal_id: str
    source_row: int
    source_artifact: str
    source_artifact_sha256: str
    payload_sha256: str
    source_offset: int | None
    source_text: str
    google_translation: str
    legacy_translation: str
    translation: str
    replacement_text: str
    byte_limit: int | None
    encoded_length: int | None
    control_signature: str
    status: str
    reviewer: str
    notes: str

    def as_dict(self) -> dict[str, Any]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    def as_tsv_dict(self) -> dict[str, str | int]:
        result: dict[str, str | int] = {}
        for name, value in self.as_dict().items():
            result[name] = "" if value is None else value
        return result


@dataclass(frozen=True, slots=True)
class AssetImportResult:
    """한 XLSX 파일의 가져오기 결과."""

    asset: AssetSource
    rows: tuple[TranslationRow, ...]
    warnings: tuple[str, ...]
    recognized: bool


def load_asset_config(config_path: str | Path) -> dict[str, Any]:
    """자산 그룹 JSON을 읽고 필요한 최상위 구조를 검사한다."""

    path = Path(config_path)
    try:
        with path.open("r", encoding="utf-8") as stream:
            config = json.load(stream)
    except json.JSONDecodeError as error:
        raise TranslationImportError(
            f"자산 설정 JSON이 올바르지 않습니다: {path} ({error})"
        ) from error
    if not isinstance(config, dict) or not isinstance(config.get("groups"), dict):
        raise TranslationImportError("자산 설정에 groups 객체가 없습니다.")
    if not isinstance(config.get("headerAliases", {}), dict):
        raise TranslationImportError("자산 설정의 headerAliases가 객체가 아닙니다.")
    return config


def discover_assets(
    archive_root: str | Path,
    config_path: str | Path,
    groups: Sequence[str] | None = None,
) -> tuple[AssetSource, ...]:
    """설정에 지정된 XLSX만 원본 아카이브에서 결정적인 순서로 찾는다."""

    root = Path(archive_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"원본 아카이브 폴더를 찾을 수 없습니다: {root}")
    config = load_asset_config(config_path)
    selected_groups = _selected_groups(config, groups)
    return _discover_assets(root, config, selected_groups)


def import_asset(
    asset: AssetSource,
    archive_root: str | Path,
    header_aliases: Mapping[str, Sequence[str]],
) -> AssetImportResult:
    """XLSX 한 파일을 정규 행으로 변환한다."""

    root = Path(archive_root).resolve()
    path = asset.path.resolve()
    if not path.is_file() or not path.is_relative_to(root):
        raise TranslationImportError(
            f"원본 아카이브 밖의 XLSX는 읽지 않습니다: {path}"
        )

    aliases = _merge_aliases(header_aliases)
    workbook = open_xlsx(path)
    asset_key = _asset_key(path)
    filename_id = _extract_internal_id(path.stem)
    imported_rows: list[TranslationRow] = []
    warnings: list[str] = []
    recognized = False

    for sheet in workbook.sheets:
        iterator = workbook.iter_rows(sheet)
        header_row: XlsxRow | None = None
        columns: dict[str, tuple[int, ...]] = {}
        for candidate in iterator:
            if candidate.number > _HEADER_SCAN_LIMIT:
                break
            candidate_columns = _header_columns(candidate.values, aliases)
            if candidate_columns.get("source_text"):
                header_row = candidate
                columns = candidate_columns
                break
        if header_row is None:
            warnings.append(
                f"{asset.relative_path} [{sheet.name}]: "
                "첫 30행에서 원문 열을 찾지 못해 시트를 건너뛰었습니다."
            )
            continue

        recognized = True
        internal_id = _internal_id(sheet.name)
        sheet_id = _extract_internal_id(sheet.name)
        if filename_id and sheet_id and filename_id != sheet_id:
            # 같은 스테이지에 실제 ID00003 파일과, 시트명이 잘못 복사된
            # ID00004 파일이 함께 존재한다. 시트 ID를 앞에 유지하되 파일 ID를
            # 보조자로 붙여 두 원고가 같은 entry_id를 만들지 않게 한다.
            internal_id = f"{sheet_id}@FILE-{filename_id}"
            warnings.append(
                f"{asset.relative_path} [{sheet.name}]: 파일명 내부 ID "
                f"{filename_id}와 시트 내부 ID {sheet_id}가 다릅니다. "
                f"구분용 내부 ID {internal_id}를 사용했습니다."
            )

        for row in iterator:
            source_text = _pick(row.values, columns.get("source_text", ()))
            if not source_text or not source_text.strip():
                continue

            translation = _pick(row.values, columns.get("translation", ()))
            replacement = _pick(row.values, columns.get("replacement_text", ()))
            google = _pick(row.values, columns.get("google_translation", ()))
            legacy = _pick(row.values, columns.get("legacy_translation", ()))
            reviewer = _pick(row.values, columns.get("reviewer", ()))

            notes: list[str] = []
            source_offset, error = _optional_hex_address(
                _pick(row.values, columns.get("source_offset", ()))
            )
            if error:
                notes.append(error)
            byte_limit, error = _optional_integer(
                _pick(row.values, columns.get("byte_limit", ())), "바이트길이"
            )
            if error:
                notes.append(error)
            encoded_length, error = _optional_integer(
                _pick(row.values, columns.get("encoded_length", ())), "길이"
            )
            if error:
                notes.append(error)
            if (
                byte_limit is not None
                and encoded_length is not None
                and encoded_length > byte_limit
            ):
                notes.append(
                    f"기록된 길이 {encoded_length}가 바이트 한도 "
                    f"{byte_limit}를 초과합니다."
                )

            # ⑲는 한국어 띄어쓰기 표지라 일본어 원문에는 없을 수 있다. 따라서
            # 문자표 변환 단계에서 번역문과 치환문 사이의 토큰 보존을 검사한다.
            if translation.strip() and replacement.strip():
                comparison = compare_control_tokens(translation, replacement)
                if not comparison.matches:
                    notes.append(describe_control_mismatch(comparison))

            imported_rows.append(
                TranslationRow(
                    entry_id=f"{asset_key}/{internal_id}/{row.number:06d}",
                    scope=asset.group,
                    asset_key=asset_key,
                    internal_id=internal_id,
                    source_row=row.number,
                    source_artifact=asset.relative_path,
                    source_artifact_sha256=workbook.file_sha256,
                    payload_sha256="",
                    source_offset=source_offset,
                    source_text=source_text,
                    google_translation=google,
                    legacy_translation=legacy,
                    translation=translation,
                    replacement_text=replacement,
                    byte_limit=byte_limit,
                    encoded_length=encoded_length,
                    control_signature=control_signature(source_text),
                    status="blocked" if notes else "imported",
                    reviewer=reviewer,
                    notes=" ".join(notes),
                )
            )

    if recognized and not imported_rows:
        warnings.append(f"{asset.relative_path}: 원문이 있는 데이터 행이 없습니다.")
    return AssetImportResult(
        asset=asset,
        rows=tuple(imported_rows),
        warnings=tuple(warnings),
        recognized=recognized,
    )


def import_archive(
    archive_root: str | Path,
    config_path: str | Path,
    output_dir: str | Path | None = None,
    groups: Sequence[str] | None = None,
) -> dict[str, Any]:
    """설정된 원고를 모두 읽고 선택적으로 `translations.tsv`를 쓴다.

    반환값은 `rows`, `assets`, `warnings`, `counts`, `output_files` 키를
    갖는다. 원본 아카이브 안에는 어떤 파일도 쓰지 않는다.
    """

    root = Path(archive_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"원본 아카이브 폴더를 찾을 수 없습니다: {root}")
    config = load_asset_config(config_path)
    selected_groups = _selected_groups(config, groups)
    assets = _discover_assets(root, config, selected_groups)
    aliases = config.get("headerAliases", {})

    rows: list[TranslationRow] = []
    warnings: list[str] = []
    recognized_by_group = {name: 0 for name in selected_groups}
    files_by_group = {name: 0 for name in selected_groups}
    rows_by_group = {name: 0 for name in selected_groups}
    seen_ids: dict[str, str] = {}

    for asset in assets:
        files_by_group[asset.group] += 1
        result = import_asset(asset, root, aliases)
        warnings.extend(result.warnings)
        if result.recognized:
            recognized_by_group[asset.group] += 1
        rows_by_group[asset.group] += len(result.rows)
        for row in result.rows:
            previous = seen_ids.get(row.entry_id)
            if previous is not None:
                raise TranslationImportError(
                    f"중복 entry_id입니다: {row.entry_id} "
                    f"({previous}, {row.source_artifact})"
                )
            seen_ids[row.entry_id] = row.source_artifact
            rows.append(row)

    group_counts: dict[str, dict[str, int]] = {}
    for name in selected_groups:
        group_config = config["groups"][name]
        expected = group_config.get("expectedRecognizedFiles")
        recognized = recognized_by_group[name]
        if isinstance(expected, int) and recognized != expected:
            warnings.append(
                f"{name}: 인식된 XLSX가 {recognized}개입니다. "
                f"설정의 예상값은 {expected}개입니다."
            )
        group_counts[name] = {
            "files": files_by_group[name],
            "recognized_files": recognized,
            "rows": rows_by_group[name],
        }

    output_files: list[str] = []
    if output_dir is not None:
        output_root = Path(output_dir).resolve()
        if output_root == root or output_root.is_relative_to(root):
            raise TranslationImportError(
                "출력 폴더는 원본 아카이브 밖에 있어야 합니다."
            )
        output_path = output_root / "translations.tsv"
        write_tsv(rows, output_path)
        output_files.append(str(output_path))

    return {
        "rows": rows,
        "assets": list(assets),
        "warnings": warnings,
        "counts": {
            "groups": group_counts,
            "files": len(assets),
            "recognized_files": sum(recognized_by_group.values()),
            "rows": len(rows),
            "blocked": sum(row.status == "blocked" for row in rows),
        },
        "output_files": output_files,
    }


def write_tsv(rows: Iterable[TranslationRow], path: str | Path) -> Path:
    """정규 행을 같은 폴더의 임시 파일에 쓴 뒤 원자적으로 교체한다."""

    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_name = stream.name
            writer = csv.DictWriter(
                stream,
                fieldnames=TSV_COLUMNS,
                dialect="excel-tab",
                lineterminator="\n",
                extrasaction="raise",
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(row.as_tsv_dict())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, destination)
    except Exception:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
        raise
    return destination


def read_tsv(path: str | Path) -> list[TranslationRow]:
    """정규 TSV를 다시 읽어 형식이 유지되는지 확인한다."""

    source = Path(path)
    result: list[TranslationRow] = []
    with source.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, dialect="excel-tab")
        missing = [name for name in TSV_COLUMNS if name not in (reader.fieldnames or ())]
        if missing:
            raise TranslationImportError(
                "TSV 필수 열이 없습니다: " + ", ".join(missing)
            )
        for line_number, raw in enumerate(reader, start=2):
            try:
                result.append(_row_from_tsv(raw))
            except (TypeError, ValueError) as error:
                raise TranslationImportError(
                    f"TSV {line_number}행을 읽을 수 없습니다: {error}"
                ) from error
    return result


def _row_from_tsv(raw: Mapping[str, str | None]) -> TranslationRow:
    values = {name: raw.get(name) or "" for name in TSV_COLUMNS}
    source_row = _required_tsv_integer(values["source_row"], "source_row")
    source_offset = _optional_tsv_integer(values["source_offset"], "source_offset")
    byte_limit = _optional_tsv_integer(values["byte_limit"], "byte_limit")
    encoded_length = _optional_tsv_integer(values["encoded_length"], "encoded_length")
    return TranslationRow(
        entry_id=values["entry_id"],
        scope=values["scope"],
        asset_key=values["asset_key"],
        internal_id=values["internal_id"],
        source_row=source_row,
        source_artifact=values["source_artifact"],
        source_artifact_sha256=values["source_artifact_sha256"],
        payload_sha256=values["payload_sha256"],
        source_offset=source_offset,
        source_text=values["source_text"],
        google_translation=values["google_translation"],
        legacy_translation=values["legacy_translation"],
        translation=values["translation"],
        replacement_text=values["replacement_text"],
        byte_limit=byte_limit,
        encoded_length=encoded_length,
        control_signature=values["control_signature"],
        status=values["status"],
        reviewer=values["reviewer"],
        notes=values["notes"],
    )


def _selected_groups(
    config: Mapping[str, Any], groups: Sequence[str] | None
) -> tuple[str, ...]:
    raw = groups if groups is not None else config.get("defaultGroups", ())
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        raise TranslationImportError("가져올 그룹 목록이 올바르지 않습니다.")
    selected = tuple(dict.fromkeys(str(name) for name in raw))
    if not selected:
        raise TranslationImportError("가져올 자산 그룹이 하나도 없습니다.")
    unknown = [name for name in selected if name not in config["groups"]]
    if unknown:
        raise TranslationImportError("알 수 없는 자산 그룹: " + ", ".join(unknown))
    return selected


def _discover_assets(
    root: Path, config: Mapping[str, Any], groups: Sequence[str]
) -> tuple[AssetSource, ...]:
    assets: list[AssetSource] = []
    seen: set[Path] = set()
    for group in groups:
        group_config = config["groups"][group]
        if not isinstance(group_config, Mapping):
            raise TranslationImportError(f"{group} 그룹 설정이 객체가 아닙니다.")
        paths: list[Path] = []
        configured_files = group_config.get("files")
        if configured_files is not None:
            if isinstance(configured_files, str) or not isinstance(
                configured_files, Sequence
            ):
                raise TranslationImportError(f"{group}.files가 목록이 아닙니다.")
            paths.extend(_safe_archive_path(root, str(item)) for item in configured_files)
        else:
            relative_root = group_config.get("root")
            if not isinstance(relative_root, str) or not relative_root:
                raise TranslationImportError(f"{group} 그룹에 root 또는 files가 없습니다.")
            search_root = _safe_archive_path(root, relative_root)
            if not search_root.is_dir():
                raise FileNotFoundError(f"자산 폴더를 찾을 수 없습니다: {search_root}")
            pattern = group_config.get("pattern", "*.xlsx")
            if not isinstance(pattern, str) or not pattern:
                raise TranslationImportError(f"{group}.pattern이 문자열이 아닙니다.")
            finder = search_root.rglob if group_config.get("recursive", False) else search_root.glob
            paths.extend(finder(pattern))

        valid_paths: list[Path] = []
        for path in paths:
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                raise TranslationImportError(
                    f"원본 아카이브 밖을 가리키는 자산입니다: {path}"
                )
            if not resolved.is_file():
                if configured_files is not None:
                    raise FileNotFoundError(f"자산 파일을 찾을 수 없습니다: {resolved}")
                continue
            if resolved.suffix.lower() != ".xlsx" or resolved.name.startswith("~$"):
                continue
            valid_paths.append(resolved)

        for path in sorted(valid_paths, key=lambda item: item.relative_to(root).as_posix().casefold()):
            if path in seen:
                raise TranslationImportError(f"여러 그룹에 중복 등록된 자산입니다: {path}")
            seen.add(path)
            assets.append(
                AssetSource(group, path, path.relative_to(root).as_posix())
            )
    return tuple(assets)


def _safe_archive_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise TranslationImportError(f"원본 아카이브 밖을 가리키는 경로입니다: {relative}")
    return candidate


def _merge_aliases(
    configured: Mapping[str, Sequence[str]],
) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for field_name, defaults in _DEFAULT_ALIASES.items():
        values = configured.get(field_name, ())
        if isinstance(values, str) or not isinstance(values, Sequence):
            raise TranslationImportError(
                f"headerAliases.{field_name}는 문자열 목록이어야 합니다."
            )
        cleaned = tuple(
            dict.fromkeys(
                str(value).strip()
                for value in (*values, *defaults)
                if str(value).strip()
            )
        )
        result[field_name] = cleaned or defaults
    return result


def _header_columns(
    values: Sequence[str], aliases: Mapping[str, Sequence[str]]
) -> dict[str, tuple[int, ...]]:
    headers = tuple(_clean_header(value) for value in values)
    result: dict[str, tuple[int, ...]] = {}
    for field_name, field_aliases in aliases.items():
        matches: list[int] = []
        for alias in field_aliases:
            cleaned_alias = _clean_header(alias)
            for index, header in enumerate(headers):
                if index not in matches and _header_matches(header, cleaned_alias):
                    matches.append(index)
        result[field_name] = tuple(matches)
    return result


def _clean_header(value: str) -> str:
    return value.lstrip("\ufeff").strip()


def _header_matches(header: str, alias: str) -> bool:
    if header == alias:
        return True
    if not alias or not header.startswith(alias):
        return False
    remainder = header[len(alias) :]
    return bool(remainder) and remainder[0] in ":：\r\n "


def _pick(values: Sequence[str], columns: Sequence[int]) -> str:
    for index in columns:
        if index < len(values):
            value = values[index]
            if value and value.strip():
                return value
    return ""


def _asset_key(path: Path) -> str:
    match = _ASSET_PATTERN.search(path.stem)
    matched_name = path.stem
    if match is None:
        for parent in path.parents:
            match = _ASSET_PATTERN.search(parent.name)
            if match is not None:
                matched_name = parent.name
                break
    if match:
        candidate = match.group(0).upper()
        # STG의 영문 분기 표시는 원래 대소문자를 유지한다.
        if candidate.startswith("STG"):
            candidate = "STG" + match.group(0)[3:]
            qualifier_match = re.search(
                re.escape(match.group(0)) + r"\(([^)]*분기[^)]*)\)",
                matched_name,
                flags=re.IGNORECASE,
            )
            if qualifier_match:
                candidate += "-" + qualifier_match.group(1)
    else:
        candidate = re.split(r"(?i)-ID[0-9]{5}", path.stem, maxsplit=1)[0]
        candidate = candidate.split("(", 1)[0]
    return _safe_segment(candidate, "자산 키")


def _internal_id(sheet_name: str) -> str:
    identifier = _extract_internal_id(sheet_name)
    return identifier or _safe_segment(sheet_name, "시트 내부 ID")


def _extract_internal_id(value: str) -> str | None:
    match = _ID_PATTERN.search(value)
    return match.group(0).upper() if match else None


def _safe_segment(value: str, label: str) -> str:
    segment = re.sub(r"[\s/\\]+", "_", value.strip())
    segment = "".join(character for character in segment if ord(character) >= 32)
    if not segment:
        raise TranslationImportError(f"{label}가 비어 있습니다: {value!r}")
    return segment


def _optional_integer(value: str, label: str) -> tuple[int | None, str]:
    text = value.strip()
    if not text:
        return None, ""
    try:
        if text.lower().startswith("0x"):
            number = int(text, 16)
        else:
            decimal = Decimal(text.replace(",", ""))
            if decimal != decimal.to_integral_value():
                raise ValueError
            number = int(decimal)
        if number < 0:
            raise ValueError
        return number, ""
    except (InvalidOperation, ValueError):
        return None, f"{label} 값이 0 이상의 정수가 아닙니다: {value!r}"


def _optional_hex_address(value: str) -> tuple[int | None, str]:
    """기존 원고의 접두사 없는 8자리 16진 주소를 정수로 바꾼다."""

    text = value.strip()
    if not text:
        return None, ""
    candidate = text[2:] if text.lower().startswith("0x") else text
    if not re.fullmatch(r"[0-9A-Fa-f]+", candidate):
        return None, f"시작주소 값이 16진 정수가 아닙니다: {value!r}"
    return int(candidate, 16), ""


def _required_tsv_integer(value: str, label: str) -> int:
    if not value:
        raise ValueError(f"{label} 값이 비어 있습니다.")
    return int(value)


def _optional_tsv_integer(value: str, label: str) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"{label} 값이 정수가 아닙니다: {value}") from error
