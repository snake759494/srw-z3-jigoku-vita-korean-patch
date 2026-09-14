"""초보자용 명령이 공유하는 준비·검사 파이프라인."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Iterable, Sequence

from .config import (
    ConfigError,
    ProjectConfig,
    ensure_output_path,
    load_asset_groups,
    project_root,
)
from .control_codes import compare_control_tokens, extract_control_tokens


TSV_FIELDS = (
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


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    required: bool = True


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _count_group_files(archive_root: Path, definition: dict[str, Any]) -> int:
    if "files" in definition:
        return sum(1 for name in definition["files"] if (archive_root / name).is_file())
    group_root = archive_root / str(definition.get("root", ""))
    if not group_root.is_dir():
        return 0
    pattern = str(definition.get("pattern", "*.xlsx"))
    iterator = group_root.rglob(pattern) if definition.get("recursive") else group_root.glob(pattern)
    return sum(1 for path in iterator if path.is_file() and not path.name.startswith("~$"))


def doctor(config: ProjectConfig) -> dict[str, Any]:
    root = project_root()
    group_config = load_asset_groups()
    checks: list[CheckResult] = []
    checks.append(
        CheckResult(
            "기존 작업 아카이브",
            config.archive_root.is_dir(),
            str(config.archive_root),
        )
    )
    checks.append(
        CheckResult(
            "새 프로젝트가 원본과 분리됨",
            root != config.archive_root and not root.is_relative_to(config.archive_root),
            str(root),
        )
    )

    for name in group_config.get("defaultGroups", []):
        definition = group_config["groups"].get(name, {})
        actual = _count_group_files(config.archive_root, definition)
        expected = definition.get("expectedRecognizedFiles")
        ok = actual == expected if isinstance(expected, int) else actual > 0
        label = definition.get("label", name)
        expected_text = str(expected) if expected is not None else "1개 이상"
        checks.append(
            CheckResult(
                f"{label} XLSX",
                ok,
                f"발견 {actual}개 / 기준 {expected_text}",
            )
        )

    if config.xdelta_path is None:
        checks.append(CheckResult("xdelta 도구", False, "경로가 비어 있음", required=False))
    else:
        checks.append(
            CheckResult(
                "xdelta 도구",
                config.xdelta_path.is_file(),
                str(config.xdelta_path),
                required=False,
            )
        )
    for label, path in (
        ("본편 원본 덤프", config.main_game_root),
        ("DLC 원본 덤프", config.dlc_game_root),
    ):
        checks.append(
            CheckResult(
                label,
                path is not None and path.is_dir(),
                str(path) if path is not None else "아직 설정하지 않음",
                required=False,
            )
        )

    mandatory_ok = all(item.ok for item in checks if item.required)
    return {
        "schemaVersion": 1,
        "generatedAtUtc": _utc_now(),
        "ok": mandatory_ok,
        "checks": [asdict(item) for item in checks],
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "__dataclass_fields__"):
        return {key: _json_safe(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def write_report(name: str, data: dict[str, Any]) -> Path:
    report_dir = ensure_output_path(project_root() / "work" / "reports")
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / name
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(_json_safe(data), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def import_legacy_translations(
    config: ProjectConfig, *, groups: Sequence[str] | None = None
) -> dict[str, Any]:
    """기존 XLSX를 하나의 정규 UTF-8 TSV로 가져옵니다."""

    from .translation_io import import_archive

    output_dir = ensure_output_path(project_root() / "work" / "normalized")
    output_dir.mkdir(parents=True, exist_ok=True)
    raw = import_archive(
        archive_root=config.archive_root,
        config_path=project_root() / "config" / "asset-groups.json",
        output_dir=output_dir,
        groups=groups,
    )
    rows = raw.get("rows", [])
    assets = raw.get("assets", [])
    report = {
        "schemaVersion": 1,
        "generatedAtUtc": _utc_now(),
        "archiveRoot": str(config.archive_root),
        "counts": raw.get("counts", {}),
        "warnings": list(raw.get("warnings", [])),
        "outputFiles": [str(path) for path in raw.get("output_files", [])],
        "assetCount": len(assets),
        "rowCount": len(rows),
        "assets": [_json_safe(item) for item in assets],
    }
    report_path = write_report("import-report.json", report)
    report["reportPath"] = str(report_path)
    return report


def _tsv_paths() -> list[Path]:
    normalized = project_root() / "work" / "normalized"
    return sorted(normalized.glob("*.tsv")) if normalized.is_dir() else []


def _looks_sha256(value: str) -> bool:
    return re.fullmatch(r"[0-9a-f]{64}", value) is not None


def check_translations(*, release: bool = False, max_examples: int = 25) -> dict[str, Any]:
    """정규 TSV의 구조를 검사합니다.

    기본 검사는 안전한 이관 여부만 봅니다. ``release=True``이면 검수자,
    승인 상태, payload 해시와 인코딩 길이까지 요구합니다.
    """

    paths = _tsv_paths()
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    seen: set[str] = set()
    status_counts: dict[str, int] = {}
    scope_counts: dict[str, int] = {}
    total = 0
    error_total = 0
    warning_total = 0
    allowed_statuses = {"imported", "review", "approved", "blocked"}
    entry_pattern = re.compile(r"^[^/\s]+/[^/\s]+/[0-9]{6}$")

    def add(bucket: list[dict[str, Any]], code: str, path: Path, line: int, detail: str) -> None:
        nonlocal error_total, warning_total
        if bucket is errors:
            error_total += 1
        else:
            warning_total += 1
        if len(bucket) < max_examples:
            bucket.append(
                {"code": code, "file": str(path), "line": line, "detail": detail}
            )

    for path in paths:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            missing = [field for field in TSV_FIELDS if field not in (reader.fieldnames or [])]
            if missing:
                add(errors, "missing-columns", path, 1, ", ".join(missing))
                continue
            for line, row in enumerate(reader, start=2):
                total += 1
                entry_id = (row.get("entry_id") or "").strip()
                source_text = row.get("source_text") or ""
                sha = (row.get("source_artifact_sha256") or "").strip().lower()
                status = (row.get("status") or "").strip()
                scope = (row.get("scope") or "").strip()
                if not entry_id:
                    add(errors, "empty-entry-id", path, line, "entry_id가 비어 있음")
                elif entry_pattern.fullmatch(entry_id) is None:
                    add(errors, "invalid-entry-id", path, line, entry_id)
                elif entry_id in seen:
                    add(errors, "duplicate-entry-id", path, line, entry_id)
                else:
                    seen.add(entry_id)
                if not source_text.strip():
                    add(errors, "empty-source", path, line, entry_id)
                if not _looks_sha256(sha):
                    add(errors, "invalid-source-sha256", path, line, entry_id)
                try:
                    signature = json.loads(row.get("control_signature") or "")
                    if not isinstance(signature, list):
                        raise ValueError
                except (json.JSONDecodeError, ValueError):
                    add(errors, "invalid-control-signature", path, line, entry_id)
                else:
                    expected_signature = list(extract_control_tokens(source_text))
                    if signature != expected_signature:
                        add(errors, "stale-control-signature", path, line, entry_id)
                source_row = (row.get("source_row") or "").strip()
                if not source_row.isdigit() or int(source_row) < 2:
                    add(errors, "invalid-source-row", path, line, entry_id)
                if status not in allowed_statuses:
                    add(errors, "invalid-status", path, line, f"{entry_id}: {status}")
                status_counts[status] = status_counts.get(status, 0) + 1
                scope_counts[scope] = scope_counts.get(scope, 0) + 1

                replacement = row.get("replacement_text") or ""
                translation = row.get("translation") or ""
                # ⑲는 일본어 원문에는 없고 한국어 띄어쓰기 표지로 추가될 수
                # 있으므로, 자동 차단은 사람이 읽는 번역문과 실제 삽입용
                # 치환문 사이에서 토큰이 유실된 경우에만 적용합니다.
                if translation.strip() and replacement.strip():
                    controls = compare_control_tokens(translation, replacement)
                    if not controls.matches and status != "blocked":
                        add(errors, "control-token-mismatch", path, line, entry_id)

                byte_limit_text = (row.get("byte_limit") or "").strip()
                encoded_text = (row.get("encoded_length") or "").strip()
                if byte_limit_text and not byte_limit_text.isdigit():
                    add(errors, "invalid-byte-limit", path, line, entry_id)
                if encoded_text and not encoded_text.isdigit():
                    add(errors, "invalid-encoded-length", path, line, entry_id)
                if (
                    byte_limit_text.isdigit()
                    and encoded_text.isdigit()
                    and int(encoded_text) > int(byte_limit_text)
                    and status != "blocked"
                ):
                    add(errors, "encoded-length-overflow", path, line, entry_id)

                if release:
                    if status != "approved":
                        add(errors, "not-approved", path, line, entry_id)
                    if not (row.get("reviewer") or "").strip():
                        add(errors, "missing-reviewer", path, line, entry_id)
                    payload_sha = (row.get("payload_sha256") or "").strip().lower()
                    if not _looks_sha256(payload_sha):
                        add(errors, "missing-payload-sha256", path, line, entry_id)
                    encoded = (row.get("encoded_length") or "").strip()
                    if not encoded.isdigit():
                        add(errors, "missing-encoded-length", path, line, entry_id)
                else:
                    if not replacement and not translation:
                        add(warnings, "no-translation-yet", path, line, entry_id)

    if not paths:
        errors.append(
            {
                "code": "no-tsv",
                "file": "",
                "line": 0,
                "detail": "먼저 기존 번역 가져오기를 실행하세요.",
            }
        )
    report = {
        "schemaVersion": 1,
        "generatedAtUtc": _utc_now(),
        "mode": "release" if release else "import",
        "ok": not errors,
        "files": [str(path) for path in paths],
        "rows": total,
        "uniqueEntryIds": len(seen),
        "statusCounts": status_counts,
        "scopeCounts": scope_counts,
        "errorCount": error_total,
        "warningCount": warning_total,
        "errorCountShown": len(errors),
        "warningCountShown": len(warnings),
        "examplesLimitedTo": max_examples,
        "errors": errors,
        "warnings": warnings,
    }
    report["reportPath"] = str(
        write_report("translation-check-release.json" if release else "translation-check.json", report)
    )
    return report


def status() -> dict[str, Any]:
    import_report_path = project_root() / "work" / "reports" / "import-report.json"
    import_report: dict[str, Any] | None = None
    if import_report_path.is_file():
        try:
            import_report = json.loads(import_report_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            import_report = {"error": "가져오기 보고서 JSON이 손상되었습니다."}
    paths = _tsv_paths()
    return {
        "projectRoot": str(project_root()),
        "normalizedFiles": [str(path) for path in paths],
        "importReport": import_report,
        "releaseReady": False,
        "releaseNote": "릴리스 빌드 단계와 실기 검증은 아직 구현되지 않았습니다.",
    }


def release_readiness() -> dict[str, Any]:
    translation = check_translations(release=True)
    blockers = [
        "원본 파일별 SHA-256 결합 프로필이 아직 확정되지 않음",
        "STAGE/ID00001 무수정 라운드트립 재조립기가 아직 미구현",
        "eboot UTF-8→Shift-JIS 2단계 재현이 아직 미검증",
        "대사 외 CPK 자산과 GXT 생성 도구·옵션이 아직 고정되지 않음",
        "기존 STG0202의 ID3/ID4 매핑 오류를 명시적 ID로 다시 빌드해야 함",
        "xdelta 생성·역적용을 새 파이프라인에서 아직 연결하지 않음",
        "Vita3K 및 실제 PS Vita 검증 절차가 아직 완료되지 않음",
        "폰트·텍스처 재배포 권리 확인이 아직 완료되지 않음",
    ]
    if not translation["ok"]:
        blockers.insert(0, "모든 번역 행이 승인·해시·길이 검사를 통과하지 않음")
    report = {
        "schemaVersion": 1,
        "generatedAtUtc": _utc_now(),
        "ready": False,
        "translationCheck": translation,
        "blockers": blockers,
        "message": "현재 프로젝트는 자료 이관·대사 CPK 구축 단계이며 배포본을 만들 수 없습니다.",
    }
    report["reportPath"] = str(write_report("release-readiness.json", report))
    return report
