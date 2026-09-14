"""기존 번역을 보존한 채 새 번역 JSON을 만들고 검증·병합합니다."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Mapping, Sequence

from .config import project_root
from .control_codes import compare_control_tokens, describe_control_mismatch
from .hashes import sha256_file
from .translation_io import TranslationRow, read_tsv, write_tsv


TASK_FORMAT = "siok-retranslation-task"
OVERLAY_FORMAT = "siok-retranslation-overlay"
FORMAT_VERSION = 1
TRANSLATION_STATUSES = frozenset({"draft", "reviewed", "blocked"})
_OVERLAY_ENTRY_FIELDS = frozenset(
    {
        "entryId",
        "sourceBindingSha256",
        "sourceTextSha256",
        "freshTranslation",
        "translationStatus",
        "translator",
        "reviewer",
        "referenceConsulted",
        "notes",
    }
)
_OVERLAY_FIELDS = frozenset(
    {
        "$schema",
        "format",
        "formatVersion",
        "scope",
        "assetKey",
        "baseline",
        "translationPolicy",
        "entries",
    }
)
_POLICY_FIELDS = frozenset({"method", "existingTranslations"})
_BASELINE_FIELDS = frozenset({"sha256", "rowCount"})


class RetranslationError(ValueError):
    """재번역 작업물의 구조나 원문 결박이 올바르지 않을 때 발생합니다."""


def _restricted_output_path(
    path: str | Path,
    *,
    allowed_root: Path,
    suffix: str,
    label: str,
) -> Path:
    """재번역 전용 출력 경계와 확장자를 강제합니다."""

    destination = Path(path).resolve(strict=False)
    boundary = allowed_root.resolve(strict=False)
    if destination == boundary or not destination.is_relative_to(boundary):
        raise RetranslationError(f"{label}은 {boundary} 아래에만 쓸 수 있습니다: {destination}")
    if destination.suffix.lower() != suffix:
        raise RetranslationError(f"{label} 확장자는 {suffix}여야 합니다: {destination}")
    return destination


def _safe_tree_root(
    value: str | Path,
    *,
    parent: Path,
    label: str,
) -> Path:
    """프로젝트 전용 work/translations 트리 안의 사용자 지정 루트를 검증한다."""

    candidate = Path(value).resolve(strict=False)
    boundary = parent.resolve(strict=False)
    if not candidate.is_relative_to(boundary):
        raise RetranslationError(
            f"{label}은(는) {boundary} 아래에 있어야 합니다: {candidate}"
        )
    return candidate


def _require_safe_component(value: str, label: str) -> None:
    if value in {"", ".", ".."} or "/" in value or "\\" in value:
        raise RetranslationError(f"{label}을 경로 구성요소로 사용할 수 없습니다: {value!r}")


def source_text_sha256(text: str) -> str:
    """원문 문자열 자체의 UTF-8 SHA-256을 반환합니다."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def source_binding_sha256(row: TranslationRow) -> str:
    """번역 행의 원문·위치·payload 결박을 고정 직렬화로 해시합니다."""

    binding = {
        "assetKey": row.asset_key,
        "byteLimit": row.byte_limit,
        "controlSignature": row.control_signature,
        "entryId": row.entry_id,
        "internalId": row.internal_id,
        "payloadSha256": row.payload_sha256,
        "scope": row.scope,
        "sourceArtifactSha256": row.source_artifact_sha256,
        "sourceOffset": row.source_offset,
        "sourceRow": row.source_row,
        "sourceTextSha256": source_text_sha256(row.source_text),
    }
    serialized = json.dumps(
        binding,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def export_retranslation_task(
    source_tsv: str | Path,
    *,
    scope: str,
    asset_key: str,
    output_path: str | Path | None = None,
    root: str | Path | None = None,
    task_root: str | Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """원문과 기존 번역을 함께 보는 로컬 전용 작업 JSON을 만듭니다.

    일본어 원문과 기존 번역을 포함하므로 출력은 반드시
    ``work/retranslation/tasks`` 아래에만 쓸 수 있습니다.
    """

    base = Path(root).resolve() if root is not None else project_root()
    source = Path(source_tsv).resolve()
    _require_safe_component(scope, "scope")
    _require_safe_component(asset_key, "assetKey")
    task_boundary = (
        _safe_tree_root(task_root, parent=base / "work", label="작업 JSON 루트")
        if task_root is not None
        else (base / "work" / "retranslation" / "tasks").resolve()
    )
    destination = _restricted_output_path(
        Path(output_path)
        if output_path is not None
        else task_boundary / scope / f"{asset_key}.json",
        allowed_root=task_boundary,
        suffix=".json",
        label="재번역 작업 JSON",
    )
    if destination == source:
        raise RetranslationError("작업 JSON이 원본 TSV를 덮어쓸 수 없습니다.")
    if destination.exists() and not overwrite:
        raise RetranslationError(
            f"기존 재번역 작업 JSON을 덮어쓰지 않습니다: {destination}"
        )

    source_rows = read_tsv(source)
    selected = [row for row in source_rows if row.scope == scope and row.asset_key == asset_key]
    if not selected:
        raise RetranslationError(f"대상 자산을 찾지 못했습니다: {scope}/{asset_key}")

    task = {
        "format": TASK_FORMAT,
        "formatVersion": FORMAT_VERSION,
        "scope": scope,
        "assetKey": asset_key,
        "sourceSnapshot": {
            "fileName": source.name,
            "sha256": sha256_file(source),
            "rowCount": len(source_rows),
        },
        "translationPolicy": {
            "method": "fresh-from-japanese",
            "existingTranslations": "reference-only",
        },
        "entries": [
            {
                "entryId": row.entry_id,
                "sourceText": row.source_text,
                "sourceTextSha256": source_text_sha256(row.source_text),
                "sourceBindingSha256": source_binding_sha256(row),
                "references": {
                    "googleTranslation": row.google_translation,
                    "legacyTranslation": row.legacy_translation,
                    "importedTranslation": row.translation,
                    "previousReplacementText": row.replacement_text,
                },
                "freshTranslation": "",
                "translationStatus": "draft",
                "translator": "",
                "reviewer": "",
                "referenceConsulted": False,
                "byteLimit": row.byte_limit,
                "controlSignature": row.control_signature,
                "notes": "",
            }
            for row in selected
        ],
    }
    _write_json_atomic(task, destination)
    return {
        "scope": scope,
        "assetKey": asset_key,
        "rowCount": len(selected),
        "outputPath": str(destination),
        "sourceSha256": task["sourceSnapshot"]["sha256"],
    }


def publish_retranslation_task(
    task_path: str | Path,
    *,
    output_path: str | Path | None = None,
    root: str | Path | None = None,
    output_root: str | Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """로컬 작업 JSON에서 원문·기존 번역을 제거한 오버레이를 만듭니다."""

    base = Path(root).resolve() if root is not None else project_root()
    task_file = Path(task_path).resolve()
    task = _read_json_object(task_file, "재번역 작업 JSON")
    if task.get("format") != TASK_FORMAT or task.get("formatVersion") != FORMAT_VERSION:
        raise RetranslationError("지원하지 않는 재번역 작업 JSON입니다.")
    scope = task.get("scope")
    asset_key = task.get("assetKey")
    snapshot = task.get("sourceSnapshot")
    policy = task.get("translationPolicy")
    entries = task.get("entries")
    if not isinstance(scope, str) or not scope.strip():
        raise RetranslationError("작업 JSON의 scope가 비어 있습니다.")
    if not isinstance(asset_key, str) or not asset_key.strip():
        raise RetranslationError("작업 JSON의 assetKey가 비어 있습니다.")
    _require_safe_component(scope, "scope")
    _require_safe_component(asset_key, "assetKey")
    overlay_boundary = (
        _safe_tree_root(
            output_root,
            parent=base / "translations",
            label="공개 오버레이 루트",
        )
        if output_root is not None
        else (base / "translations" / "retranslation").resolve()
    )
    if not isinstance(snapshot, Mapping):
        raise RetranslationError("작업 JSON의 sourceSnapshot이 없습니다.")
    baseline_sha = snapshot.get("sha256")
    baseline_rows = snapshot.get("rowCount")
    if not _is_sha256(baseline_sha):
        raise RetranslationError("작업 JSON의 기준 SHA-256이 올바르지 않습니다.")
    if not isinstance(baseline_rows, int) or isinstance(baseline_rows, bool) or baseline_rows < 1:
        raise RetranslationError("작업 JSON의 기준 행 수가 올바르지 않습니다.")
    if not isinstance(policy, Mapping) or policy.get("method") != "fresh-from-japanese":
        raise RetranslationError("작업 JSON의 번역 방식이 fresh-from-japanese가 아닙니다.")
    if policy.get("existingTranslations") != "reference-only":
        raise RetranslationError("작업 JSON에서 기존 번역이 참조 전용이 아닙니다.")
    if not isinstance(entries, list) or not entries:
        raise RetranslationError("작업 JSON에 번역 행이 없습니다.")

    destination = _restricted_output_path(
        Path(output_path)
        if output_path is not None
        else overlay_boundary / scope / f"{asset_key}.json",
        allowed_root=overlay_boundary / scope,
        suffix=".json",
        label="재번역 오버레이",
    )
    if destination == task_file:
        raise RetranslationError("오버레이가 로컬 작업 JSON을 덮어쓸 수 없습니다.")
    if destination.exists() and not overwrite:
        raise RetranslationError(f"기존 재번역 오버레이를 덮어쓰지 않습니다: {destination}")

    public_entries: list[dict[str, Any]] = []
    seen_entry_ids: set[str] = set()
    for index, item in enumerate(entries, start=1):
        if not isinstance(item, Mapping):
            raise RetranslationError(f"작업 JSON entries[{index}]가 객체가 아닙니다.")
        entry_id = item.get("entryId")
        if not isinstance(entry_id, str) or not entry_id:
            raise RetranslationError(f"작업 JSON entries[{index}]의 entryId가 비어 있습니다.")
        if entry_id in seen_entry_ids:
            raise RetranslationError(f"작업 JSON에서 entryId가 중복됩니다: {entry_id}")
        seen_entry_ids.add(entry_id)
        if not _is_sha256(item.get("sourceBindingSha256")):
            raise RetranslationError(f"작업 JSON의 원문 결박 해시가 올바르지 않습니다: {entry_id}")
        if not _is_sha256(item.get("sourceTextSha256")):
            raise RetranslationError(f"작업 JSON의 원문 해시가 올바르지 않습니다: {entry_id}")
        translation = item.get("freshTranslation")
        if not isinstance(translation, str) or not translation.strip():
            raise RetranslationError(f"작업 JSON의 새 번역이 비어 있습니다: {entry_id}")
        if any(character in translation for character in ("\r", "\n", "\x00")):
            raise RetranslationError(f"작업 JSON의 새 번역에 실제 제어문자가 있습니다: {entry_id}")
        status = item.get("translationStatus")
        if status not in TRANSLATION_STATUSES:
            raise RetranslationError(f"작업 JSON의 번역 상태가 올바르지 않습니다: {entry_id}")
        translator = item.get("translator")
        if not isinstance(translator, str) or not translator.strip():
            raise RetranslationError(f"작업 JSON의 번역자가 비어 있습니다: {entry_id}")
        reviewer = item.get("reviewer", "")
        if not isinstance(reviewer, str) or (status == "reviewed" and not reviewer.strip()):
            raise RetranslationError(f"작업 JSON의 검수자가 올바르지 않습니다: {entry_id}")
        reference_consulted = item.get("referenceConsulted")
        if not isinstance(reference_consulted, bool):
            raise RetranslationError(f"작업 JSON의 참고 여부가 boolean이 아닙니다: {entry_id}")
        notes = item.get("notes", "")
        if not isinstance(notes, str):
            raise RetranslationError(f"작업 JSON의 notes가 문자열이 아닙니다: {entry_id}")
        public_entries.append(
            {
                "entryId": entry_id,
                "sourceBindingSha256": item.get("sourceBindingSha256"),
                "sourceTextSha256": item.get("sourceTextSha256"),
                "freshTranslation": translation,
                "translationStatus": status,
                "translator": translator,
                "reviewer": reviewer,
                "referenceConsulted": reference_consulted,
                "notes": notes,
            }
        )
    overlay = {
        "format": OVERLAY_FORMAT,
        "formatVersion": FORMAT_VERSION,
        "scope": scope,
        "assetKey": asset_key,
        "baseline": {"sha256": baseline_sha, "rowCount": baseline_rows},
        "translationPolicy": {
            "method": "fresh-from-japanese",
            "existingTranslations": "reference-only",
        },
        "entries": public_entries,
    }
    _write_json_atomic(overlay, destination)
    return {
        "scope": scope,
        "assetKey": asset_key,
        "rowCount": len(public_entries),
        "outputPath": str(destination),
        "baselineSha256": baseline_sha,
    }


def load_retranslation_progress(
    source_tsv: str | Path,
    progress_path: str | Path,
    *,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """진행 매니페스트를 검증하고 등록된 모든 오버레이 경로를 반환합니다."""

    base = Path(root).resolve() if root is not None else project_root()
    progress_file = _restricted_output_path(
        progress_path,
        allowed_root=base / "translations" / "retranslation",
        suffix=".json",
        label="재번역 진행 매니페스트",
    )
    progress = _read_json_object(progress_file, "재번역 진행 매니페스트")
    if progress.get("format") != "siok-retranslation-progress":
        raise RetranslationError("지원하지 않는 재번역 진행 매니페스트입니다.")
    if progress.get("formatVersion") != FORMAT_VERSION:
        raise RetranslationError("재번역 진행 매니페스트 버전이 맞지 않습니다.")
    policy = progress.get("policy")
    if not isinstance(policy, Mapping):
        raise RetranslationError("진행 매니페스트의 policy가 없습니다.")
    if policy.get("method") != "fresh-from-japanese":
        raise RetranslationError("진행 매니페스트의 번역 방식이 올바르지 않습니다.")
    if policy.get("existingTranslations") != "reference-only":
        raise RetranslationError("진행 매니페스트에서 기존 번역이 참조 전용이 아닙니다.")
    if policy.get("automaticLegacyFallback") is not False:
        raise RetranslationError("진행 매니페스트에서 기존 번역 자동 대체가 금지되지 않았습니다.")

    source = Path(source_tsv).resolve()
    source_rows = read_tsv(source)
    baseline = progress.get("baseline")
    if not isinstance(baseline, Mapping):
        raise RetranslationError("진행 매니페스트의 baseline이 없습니다.")
    if baseline.get("sha256") != sha256_file(source):
        raise RetranslationError("진행 매니페스트와 기준 TSV SHA-256이 일치하지 않습니다.")
    if baseline.get("rowCount") != len(source_rows):
        raise RetranslationError("진행 매니페스트와 기준 TSV 행 수가 일치하지 않습니다.")

    assets = progress.get("assets")
    if not isinstance(assets, list) or not assets:
        raise RetranslationError("진행 매니페스트에 등록된 자산이 없습니다.")
    overlay_paths: list[Path] = []
    seen_paths: set[Path] = set()
    for index, asset in enumerate(assets, start=1):
        if not isinstance(asset, Mapping) or not isinstance(asset.get("overlay"), str):
            raise RetranslationError(f"진행 매니페스트 assets[{index}]의 overlay가 없습니다.")
        overlay_path = _restricted_output_path(
            progress_file.parent / str(asset["overlay"]),
            allowed_root=base / "translations" / "retranslation",
            suffix=".json",
            label="재번역 오버레이",
        )
        if overlay_path == progress_file:
            raise RetranslationError("진행 매니페스트 자신을 오버레이로 지정할 수 없습니다.")
        if overlay_path in seen_paths:
            raise RetranslationError(f"진행 매니페스트에 오버레이가 중복됩니다: {overlay_path}")
        if not overlay_path.is_file():
            raise RetranslationError(f"등록된 재번역 오버레이가 없습니다: {overlay_path}")
        seen_paths.add(overlay_path)
        overlay_paths.append(overlay_path)
    return {
        "progressPath": str(progress_file),
        "overlayPaths": overlay_paths,
        "baselineSha256": str(baseline["sha256"]),
        "baselineRows": len(source_rows),
    }


def validate_retranslation_overlay(
    source_tsv: str | Path,
    overlay_path: str | Path,
) -> dict[str, Any]:
    """새 번역 오버레이를 원문 TSV와 대조해 구조·해시·제어코드를 검사합니다."""

    source = Path(source_tsv).resolve()
    overlay_file = Path(overlay_path).resolve()
    rows = read_tsv(source)
    overlay = _read_overlay(overlay_file)
    return _validate_retranslation_overlay_rows(
        source,
        overlay_file,
        rows,
        overlay,
        sha256_file(source),
    )


def validate_retranslation_overlays(
    source_tsv: str | Path,
    overlay_paths: Sequence[str | Path],
) -> dict[str, Any]:
    """여러 오버레이를 한 기준본에 대해 검사하고 파일 간 중복도 찾습니다."""

    source = Path(source_tsv).resolve()
    overlay_files = [Path(path).resolve() for path in overlay_paths]
    if not overlay_files:
        raise RetranslationError("검사할 재번역 오버레이가 없습니다.")
    rows = read_tsv(source)
    combined, _ = _load_and_validate_overlay_bundle(
        source,
        overlay_files,
        rows,
        sha256_file(source),
    )
    return combined


def _load_and_validate_overlay_bundle(
    source: Path,
    overlay_files: Sequence[Path],
    rows: Sequence[TranslationRow],
    source_sha256: str,
) -> tuple[dict[str, Any], list[tuple[Path, dict[str, Any]]]]:
    loaded: list[tuple[Path, dict[str, Any]]] = []
    reports: list[dict[str, Any]] = []
    bundle_errors: list[dict[str, str]] = []
    entry_origins: dict[str, Path] = {}
    for overlay_file in overlay_files:
        overlay = _read_overlay(overlay_file)
        loaded.append((overlay_file, overlay))
        report = _validate_retranslation_overlay_rows(
            source,
            overlay_file,
            rows,
            overlay,
            source_sha256,
        )
        reports.append(report)
        entries = overlay.get("entries")
        if not isinstance(entries, list):
            continue
        for item in entries:
            if not isinstance(item, Mapping) or not isinstance(item.get("entryId"), str):
                continue
            entry_id = str(item["entryId"])
            previous = entry_origins.get(entry_id)
            if previous is not None:
                _issue(
                    bundle_errors,
                    "duplicate-entry-across-overlays",
                    entry_id,
                    f"{previous.name}과 {overlay_file.name}에 함께 있습니다.",
                )
            else:
                entry_origins[entry_id] = overlay_file
    return (
        {
            "ok": all(report["ok"] for report in reports) and not bundle_errors,
            "overlayCount": len(overlay_files),
            "rowCount": sum(report["rowCount"] for report in reports),
            "reports": reports,
            "errors": bundle_errors,
        },
        loaded,
    )


def _validate_retranslation_overlay_rows(
    source: Path,
    overlay_file: Path,
    rows: Sequence[TranslationRow],
    overlay: Mapping[str, Any],
    source_sha256: str,
) -> dict[str, Any]:
    source_by_id = {row.entry_id: row for row in rows}
    if len(source_by_id) != len(rows):
        raise RetranslationError("원본 TSV에 중복 entry_id가 있습니다.")
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    seen: set[str] = set()
    scope = overlay.get("scope")
    asset_key = overlay.get("assetKey")
    raw_entries = overlay.get("entries")
    entries = raw_entries if isinstance(raw_entries, list) else []

    unknown_overlay_fields = sorted(str(name) for name in set(overlay) - _OVERLAY_FIELDS)
    if unknown_overlay_fields:
        _issue(
            errors,
            "unknown-overlay-field",
            "",
            "허용되지 않는 최상위 필드: " + ", ".join(unknown_overlay_fields),
        )

    if overlay.get("format") != OVERLAY_FORMAT:
        _issue(errors, "invalid-format", "", f"format은 {OVERLAY_FORMAT}이어야 합니다.")
    if overlay.get("formatVersion") != FORMAT_VERSION:
        _issue(errors, "invalid-version", "", f"formatVersion은 {FORMAT_VERSION}이어야 합니다.")
    if not isinstance(scope, str) or not scope.strip():
        _issue(errors, "invalid-scope", "", "scope가 비어 있습니다.")
    if not isinstance(asset_key, str) or not asset_key.strip():
        _issue(errors, "invalid-asset-key", "", "assetKey가 비어 있습니다.")
    baseline = overlay.get("baseline")
    if not isinstance(baseline, Mapping):
        _issue(errors, "missing-baseline", "", "baseline 객체가 필요합니다.")
    else:
        unknown_baseline_fields = sorted(str(name) for name in set(baseline) - _BASELINE_FIELDS)
        if unknown_baseline_fields:
            _issue(
                errors,
                "unknown-baseline-field",
                "",
                "허용되지 않는 기준 필드: " + ", ".join(unknown_baseline_fields),
            )
        if baseline.get("sha256") != source_sha256:
            _issue(errors, "baseline-hash-mismatch", "", "기준 TSV SHA-256이 일치하지 않습니다.")
        baseline_rows = baseline.get("rowCount")
        if not isinstance(baseline_rows, int) or isinstance(baseline_rows, bool):
            _issue(errors, "invalid-baseline-rows", "", "기준 TSV 행 수가 정수가 아닙니다.")
        elif baseline_rows != len(rows):
            _issue(errors, "baseline-row-count-mismatch", "", "기준 TSV 행 수가 일치하지 않습니다.")
    policy = overlay.get("translationPolicy")
    if not isinstance(policy, Mapping):
        _issue(errors, "missing-policy", "", "translationPolicy 객체가 필요합니다.")
    else:
        unknown_policy_fields = sorted(str(name) for name in set(policy) - _POLICY_FIELDS)
        if unknown_policy_fields:
            _issue(
                errors,
                "unknown-policy-field",
                "",
                "허용되지 않는 정책 필드: " + ", ".join(unknown_policy_fields),
            )
        if policy.get("method") != "fresh-from-japanese":
            _issue(errors, "invalid-policy", "", "method는 fresh-from-japanese여야 합니다.")
        if policy.get("existingTranslations") != "reference-only":
            _issue(errors, "invalid-policy", "", "기존 번역은 reference-only여야 합니다.")
    if not isinstance(raw_entries, list) or not raw_entries:
        _issue(errors, "empty-entries", "", "entries에 번역 행이 하나 이상 필요합니다.")

    status_counts: dict[str, int] = {}
    for index, item in enumerate(entries, start=1):
        if not isinstance(item, Mapping):
            _issue(errors, "invalid-entry", "", f"entries[{index}]가 객체가 아닙니다.")
            continue
        entry_id = item.get("entryId")
        shown_id = entry_id if isinstance(entry_id, str) else ""
        unknown = sorted(str(name) for name in set(item) - _OVERLAY_ENTRY_FIELDS)
        if unknown:
            _issue(
                errors,
                "private-field-in-overlay",
                shown_id,
                "공개 오버레이에 허용되지 않는 필드: " + ", ".join(unknown),
            )
        if not shown_id:
            _issue(errors, "missing-entry-id", "", f"entries[{index}]의 entryId가 비어 있습니다.")
            continue
        if shown_id in seen:
            _issue(errors, "duplicate-entry-id", shown_id, "오버레이에서 entryId가 중복됩니다.")
            continue
        seen.add(shown_id)
        source_row = source_by_id.get(shown_id)
        if source_row is None:
            _issue(errors, "unknown-entry-id", shown_id, "원본 TSV에 없는 entryId입니다.")
            continue
        if source_row.scope != scope or source_row.asset_key != asset_key:
            _issue(
                errors,
                "asset-mismatch",
                shown_id,
                f"원본 자산은 {source_row.scope}/{source_row.asset_key}입니다.",
            )
        if source_row.status == "blocked":
            _issue(
                warnings,
                "source-row-blocked",
                shown_id,
                "기준 TSV에서 이미 차단된 행이므로 재번역 후에도 빌드 차단 상태를 유지합니다.",
            )

        expected_hash = source_text_sha256(source_row.source_text)
        if item.get("sourceTextSha256") != expected_hash:
            _issue(errors, "source-hash-mismatch", shown_id, "일본어 원문 해시가 일치하지 않습니다.")
        expected_binding = source_binding_sha256(source_row)
        if item.get("sourceBindingSha256") != expected_binding:
            _issue(
                errors,
                "source-binding-mismatch",
                shown_id,
                "원문 행의 자산·위치·payload 결박 해시가 일치하지 않습니다.",
            )
        translation = item.get("freshTranslation")
        if not isinstance(translation, str) or not translation.strip():
            _issue(errors, "empty-translation", shown_id, "freshTranslation이 비어 있습니다.")
        else:
            if any(character in translation for character in ("\r", "\n", "\x00")):
                _issue(
                    errors,
                    "forbidden-literal-control",
                    shown_id,
                    "번역문에 실제 CR, LF 또는 NUL 문자를 넣을 수 없습니다.",
                )
            comparison = compare_control_tokens(source_row.source_text, translation)
            if not comparison.matches:
                _issue(
                    errors,
                    "control-code-mismatch",
                    shown_id,
                    describe_control_mismatch(comparison),
                )
        status = item.get("translationStatus")
        if status not in TRANSLATION_STATUSES:
            _issue(
                errors,
                "invalid-status",
                shown_id,
                "translationStatus는 draft/reviewed/blocked 중 하나여야 합니다.",
            )
        else:
            status_counts[str(status)] = status_counts.get(str(status), 0) + 1
        translator = item.get("translator")
        if not isinstance(translator, str) or not translator.strip():
            _issue(errors, "missing-translator", shown_id, "translator가 비어 있습니다.")
        reviewer = item.get("reviewer", "")
        if not isinstance(reviewer, str):
            _issue(errors, "invalid-reviewer", shown_id, "reviewer는 문자열이어야 합니다.")
        elif status == "reviewed" and not reviewer.strip():
            _issue(errors, "missing-reviewer", shown_id, "reviewed 상태에는 reviewer가 필요합니다.")
        consulted = item.get("referenceConsulted")
        if not isinstance(consulted, bool):
            _issue(errors, "invalid-reference-flag", shown_id, "referenceConsulted는 boolean이어야 합니다.")
        notes = item.get("notes")
        if not isinstance(notes, str):
            _issue(errors, "invalid-notes", shown_id, "notes는 문자열이어야 합니다.")

    return {
        "ok": not errors,
        "scope": scope if isinstance(scope, str) else "",
        "assetKey": asset_key if isinstance(asset_key, str) else "",
        "rowCount": len(entries),
        "uniqueEntryIds": len(seen),
        "statusCounts": status_counts,
        "errors": errors,
        "warnings": warnings,
        "sourcePath": str(source),
        "overlayPath": str(overlay_file),
    }


def apply_retranslation_overlay(
    source_tsv: str | Path,
    overlay_path: str | Path,
    *,
    output_path: str | Path | None = None,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """단일 오버레이를 적용하는 호환용 래퍼입니다."""

    return apply_retranslation_overlays(
        source_tsv,
        [overlay_path],
        output_path=output_path,
        root=root,
    )


def apply_retranslation_overlays(
    source_tsv: str | Path,
    overlay_paths: Sequence[str | Path],
    *,
    output_path: str | Path | None = None,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """검증된 새 번역을 원본 복사본에 적용하고 별도 TSV로 씁니다.

    ``google_translation``과 ``legacy_translation``은 참고 열로 유지하지만 활성
    ``translation``에는 오버레이의 새 번역만 넣습니다. 새 문장을 게임 문자표로
    아직 바꾸지 않았으므로 ``replacement_text``와 ``encoded_length``는 비웁니다.
    """

    base = Path(root).resolve() if root is not None else project_root()
    source = Path(source_tsv).resolve()
    overlay_files = [Path(path).resolve() for path in overlay_paths]
    if not overlay_files:
        raise RetranslationError("적용할 재번역 오버레이가 없습니다.")
    destination = _restricted_output_path(
        Path(output_path)
        if output_path is not None
        else base / "work" / "retranslation" / "merged" / "translations.tsv",
        allowed_root=base / "work" / "retranslation" / "merged",
        suffix=".tsv",
        label="재번역 병합 TSV",
    )
    if destination == source:
        raise RetranslationError("병합 결과가 원본 TSV를 덮어쓸 수 없습니다.")
    if destination in overlay_files:
        raise RetranslationError("병합 결과가 재번역 오버레이 JSON을 덮어쓸 수 없습니다.")

    baseline_sha256 = sha256_file(source)
    source_rows = read_tsv(source)
    bundle, loaded_overlays = _load_and_validate_overlay_bundle(
        source,
        overlay_files,
        source_rows,
        baseline_sha256,
    )
    for validation in bundle["reports"]:
        if not validation["ok"]:
            first = validation["errors"][0]
            location = f" ({first['entryId']})" if first["entryId"] else ""
            raise RetranslationError(
                f"재번역 오버레이 검증 실패: {first['code']}{location} — {first['detail']}"
            )
    if bundle["errors"]:
        first = bundle["errors"][0]
        raise RetranslationError(
            f"재번역 오버레이 묶음 검증 실패: {first['code']} ({first['entryId']}) — "
            f"{first['detail']}"
        )

    replacements: dict[str, tuple[Mapping[str, Any], Path]] = {}
    for overlay_file, overlay in loaded_overlays:
        for item in overlay["entries"]:
            if not isinstance(item, Mapping):
                continue
            entry_id = str(item["entryId"])
            replacements[entry_id] = (item, overlay_file)

    updated: list[TranslationRow] = []
    applied = 0
    pending = 0
    drafted = 0
    blocked = 0
    for row in source_rows:
        replacement = replacements.get(row.entry_id)
        if replacement is None:
            marker = "재번역=미작성; 기존번역=참조전용"
            notes = "; ".join(part for part in (row.notes.strip(), marker) if part)
            updated.append(
                replace(
                    row,
                    translation="",
                    replacement_text="",
                    encoded_length=None,
                    status="blocked",
                    reviewer="",
                    notes=notes,
                )
            )
            pending += 1
            continue
        item, overlay_file = replacement
        translation_status = str(item["translationStatus"])
        marker = (
            f"재번역={overlay_file.name}; 언어상태={translation_status}; "
            "기존번역=참조전용"
        )
        notes = "; ".join(part for part in (row.notes.strip(), marker) if part)
        if translation_status != "reviewed" or row.status == "blocked":
            updated.append(
                replace(
                    row,
                    translation="",
                    replacement_text="",
                    encoded_length=None,
                    status="blocked",
                    reviewer="",
                    notes=notes,
                )
            )
            if translation_status == "draft":
                drafted += 1
            else:
                blocked += 1
            continue
        updated.append(
            replace(
                row,
                translation=str(item["freshTranslation"]),
                replacement_text="",
                encoded_length=None,
                status="review",
                reviewer=str(item.get("reviewer", "")),
                notes=notes,
            )
        )
        applied += 1

    if sha256_file(source) != baseline_sha256:
        raise RetranslationError("병합 전에 원본 TSV가 변경되었습니다.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    candidate_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".candidate",
            delete=False,
        ) as candidate:
            candidate_path = Path(candidate.name)
        write_tsv(updated, candidate_path)
        verified = read_tsv(candidate_path)
        if verified != updated:
            raise RetranslationError("병합 TSV 후보의 전체 필드 재독해 검증에 실패했습니다.")
        if sha256_file(source) != baseline_sha256:
            raise RetranslationError("병합 도중 원본 TSV가 변경되었습니다.")
        os.replace(candidate_path, destination)
        candidate_path = None
    finally:
        if candidate_path is not None:
            candidate_path.unlink(missing_ok=True)
    return {
        "assets": [
            {"scope": validation["scope"], "assetKey": validation["assetKey"]}
            for validation in bundle["reports"]
        ],
        "overlayCount": len(overlay_files),
        "appliedRows": applied,
        "pendingRows": pending,
        "draftRows": drafted,
        "blockedRows": blocked,
        "totalRows": len(updated),
        "outputPath": str(destination),
        "outputSha256": sha256_file(destination),
        "buildStatus": "blocked" if applied != len(updated) else "review",
        "replacementTextCleared": len(updated),
        "baselineSha256": baseline_sha256,
    }


def _read_overlay(path: Path) -> dict[str, Any]:
    return _read_json_object(path, "재번역 JSON")


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        data = json.loads(
            path.read_text(encoding="utf-8-sig"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except FileNotFoundError as exc:
        raise RetranslationError(f"{label}이 없습니다: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RetranslationError(
            f"{label} 형식이 올바르지 않습니다: {path} ({exc.lineno}행 {exc.colno}열)"
        ) from exc
    if not isinstance(data, dict):
        raise RetranslationError(f"{label} 최상위 값은 객체여야 합니다.")
    return data


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RetranslationError(f"재번역 JSON에 중복 키가 있습니다: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise RetranslationError(f"재번역 JSON에 허용되지 않는 숫자 상수가 있습니다: {value}")


def _write_json_atomic(data: Mapping[str, Any], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_name = stream.name
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, destination)
    except Exception:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
        raise


def _issue(
    destination: list[dict[str, str]],
    code: str,
    entry_id: str,
    detail: str,
) -> None:
    destination.append({"code": code, "entryId": entry_id, "detail": detail})
