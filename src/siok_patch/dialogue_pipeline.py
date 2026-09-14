"""CPK 추출부터 JSON 기반 대사 조립, 리팩, 재추출 검증까지 수행한다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
from typing import Iterable, Mapping, Sequence

from .config import ensure_output_path, project_root
from .cpk_tool import (
    CpkEntry,
    CpkMakerTool,
    CpkTool,
    CpkToolError,
    member_filename,
    parse_member_id,
)
from .dialogue_manifest import (
    DialogueManifest,
    DialogueManifestError,
    ManifestScript,
    apply_manifest_script,
    compile_manifest_script,
    compose_dialogue_manifest,
    load_dialogue_manifest,
    write_dialogue_manifest,
)
from .dialogue_workbook import DialogueWorkbookError, load_dialogue_workbook
from .hashes import sha256_file
from .stage_script import StageScriptError, parse_stage_script


class DialogueBuildError(RuntimeError):
    """대사 CPK의 입력 검증, 생성 또는 역검증이 실패했을 때 발생한다."""


@dataclass(frozen=True, slots=True)
class DialogueEntryRequest:
    """한 XLSX를 어느 CPK ID에 적용할지 나타내는 명시적 매핑.

    ``source_id``를 생략하면 대상 ID와 같은 원본 payload를 사용한다. 원본에 없는
    ID를 새로 만들 때만 ``source_id``를 별도로 지정한다.
    """

    target_id: int
    workbook_path: Path
    source_id: int | None = None

    @property
    def effective_source_id(self) -> int:
        return self.target_id if self.source_id is None else self.source_id


@dataclass(frozen=True, slots=True)
class AllowedControlChange:
    """검수자가 승인한 특정 XLSX 행의 제어 토큰 변경."""

    target_id: int
    workbook_row: int


def build_dialogue_cpk(
    source_cpk: Path,
    entries: Sequence[DialogueEntryRequest],
    cpk_tool: CpkTool,
    *,
    allowed_control_changes: Iterable[AllowedControlChange] = (),
    workspace_root: Path | None = None,
) -> dict[str, object]:
    """XLSX를 정규 JSON으로 만든 뒤 그 JSON만 사용해 대사 CPK를 생성한다.

    원본 CPK와 XLSX는 읽기만 한다. 정규 JSON은 실행 폴더에 먼저 기록하고 다시
    엄격하게 읽는다. payload 조립 단계는 XLSX 객체를 전달받지 않는다.
    """

    root, source, source_sha256 = _prepare_source(source_cpk, workspace_root)
    requests = _validate_requests(entries)
    allowed = _validate_allowed_changes(allowed_control_changes, requests)
    run_id, slug, run_dir = _create_run(root, source, source_sha256, cpk_tool)
    failure_path = run_dir / "failure.json"

    try:
        extracted_dir = run_dir / "extracted-original"
        original_entries = cpk_tool.extract(source, extracted_dir)
        _validate_tool_entries(original_entries, extracted_dir)

        compiled_outputs: list[tuple[int, ManifestScript]] = []
        for request in requests:
            source_id = request.effective_source_id
            source_entry = original_entries.get(source_id)
            if source_entry is None:
                raise DialogueBuildError(
                    f"원본 CPK에 {member_filename(source_id)}가 없습니다. "
                    "새 ID의 원본으로 사용할 실제 ID를 @원본ID로 명시하세요."
                )
            payload = source_entry.path.read_bytes()
            parsed = parse_stage_script(payload)
            if not parsed.slots:
                raise DialogueBuildError(
                    f"{member_filename(source_id)}에서 대사 슬롯을 찾지 못했습니다."
                )
            workbook = load_dialogue_workbook(request.workbook_path, parsed.source_rows)
            approved_rows = frozenset(
                item.workbook_row
                for item in allowed
                if item.target_id == request.target_id
            )
            script = compile_manifest_script(
                source_id,
                payload,
                source_entry.sha256,
                workbook,
                approved_workbook_rows=approved_rows,
            )
            compiled_outputs.append((request.target_id, script))

        manifest = compose_dialogue_manifest(
            source_cpk_name=source.name,
            source_cpk_size=source.stat().st_size,
            source_cpk_sha256=source_sha256,
            original_entries=original_entries,
            tool_sha256=cpk_tool.executable_sha256,
            tool_version=cpk_tool.executable_version,
            compiled_outputs=compiled_outputs,
        )
        manifest_path = write_dialogue_manifest(
            run_dir / "dialogue-manifest.json", manifest
        )
        # JSON이 보고서가 아니라 실제 빌드 입력이 되도록 반드시 디스크에서 다시 읽는다.
        manifest = load_dialogue_manifest(manifest_path)
        report = _execute_manifest_build(
            source,
            source_sha256,
            manifest,
            manifest_path,
            "xlsx",
            cpk_tool,
            root,
            run_dir,
            run_id,
            slug,
            original_entries,
        )
    except _BUILD_EXCEPTIONS as error:
        _record_failure(failure_path, run_id, source, source_sha256, error)
        if isinstance(error, DialogueBuildError):
            raise
        raise DialogueBuildError(f"대사 CPK 생성이 중단되었습니다: {error}") from error

    _publish_reports(run_dir, report)
    return report


def build_dialogue_cpk_from_manifest(
    source_cpk: Path,
    manifest_path: Path,
    cpk_tool: CpkTool,
    *,
    workspace_root: Path | None = None,
) -> dict[str, object]:
    """기존 정규 JSON을 직접 사용해 XLSX 없이 동일한 CPK를 다시 만든다."""

    # JSON 문법·스키마 오류는 CPK 도구를 호출하기 전에 차단한다.
    manifest = load_dialogue_manifest(manifest_path)
    root, source, source_sha256 = _prepare_source(source_cpk, workspace_root)
    run_id, slug, run_dir = _create_run(root, source, source_sha256, cpk_tool)
    failure_path = run_dir / "failure.json"

    try:
        _validate_manifest_source_file(manifest, source, source_sha256)
        _validate_manifest_tool(manifest, cpk_tool)
        canonical_manifest_path = write_dialogue_manifest(
            run_dir / "dialogue-manifest.json", manifest
        )
        manifest = load_dialogue_manifest(canonical_manifest_path)
        extracted_dir = run_dir / "extracted-original"
        original_entries = cpk_tool.extract(source, extracted_dir)
        _validate_tool_entries(original_entries, extracted_dir)
        report = _execute_manifest_build(
            source,
            source_sha256,
            manifest,
            canonical_manifest_path,
            "json",
            cpk_tool,
            root,
            run_dir,
            run_id,
            slug,
            original_entries,
        )
    except _BUILD_EXCEPTIONS as error:
        _record_failure(failure_path, run_id, source, source_sha256, error)
        if isinstance(error, DialogueBuildError):
            raise
        raise DialogueBuildError(f"대사 CPK 생성이 중단되었습니다: {error}") from error

    _publish_reports(run_dir, report)
    return report


_BUILD_EXCEPTIONS = (
    DialogueBuildError,
    DialogueManifestError,
    CpkToolError,
    DialogueWorkbookError,
    StageScriptError,
    OSError,
    TypeError,
    ValueError,
)


def _execute_manifest_build(
    source: Path,
    source_sha256: str,
    manifest: DialogueManifest,
    manifest_path: Path,
    input_mode: str,
    cpk_tool: CpkTool,
    root: Path,
    run_dir: Path,
    run_id: str,
    slug: str,
    original_entries: Mapping[int, CpkEntry],
) -> dict[str, object]:
    _validate_manifest_source_file(manifest, source, source_sha256)
    _validate_manifest_tool(manifest, cpk_tool)
    _validate_manifest_members(manifest, original_entries)

    payload_dir = run_dir / "build" / "payloads"
    payload_dir.mkdir(parents=True)
    for member_id, entry in original_entries.items():
        shutil.copyfile(entry.path, payload_dir / member_filename(member_id))

    scripts_by_id = {script.script_id: script for script in manifest.scripts}
    rebuilt_by_script: dict[str, bytes] = {}
    for script in manifest.scripts:
        source_id = parse_member_id(script.source_member_id)
        source_entry = original_entries[source_id]
        rebuilt_by_script[script.script_id] = apply_manifest_script(
            script, source_entry.path.read_bytes()
        )

    entry_reports: list[dict[str, object]] = []
    for output in manifest.outputs:
        target_id = parse_member_id(output.target_member_id)
        script = scripts_by_id[output.script_id]
        source_id = parse_member_id(script.source_member_id)
        rebuilt = rebuilt_by_script[output.script_id]
        target_path = payload_dir / output.target_member_id
        target_path.write_bytes(rebuilt)
        approvals = [
            {
                "ordinal": row.ordinal,
                "workbookRow": row.workbook_row,
                "sourceRawLineSha256": row.source_raw_line_sha256,
                "sourceTokens": list(row.control.source_tokens),
                "replacementTokens": list(row.control.replacement_tokens),
            }
            for row in script.rows
            if row.control.approval is not None
        ]
        warnings = [
            f"대사 {row.ordinal}번(XLSX {row.workbook_row}행): 원문 양끝 전각 공백 개수만 다릅니다."
            for row in script.rows
            if row.source_match == "edge-fullwidth-space"
        ]
        entry_reports.append(
            {
                "targetId": output.target_member_id,
                "sourceId": script.source_member_id,
                "scriptId": script.script_id,
                "targetExistedInSource": target_id in original_entries,
                "sourcePayloadSha256": script.source_payload_sha256,
                "rebuiltPayloadSha256": script.expected_payload_sha256,
                "rebuiltPayloadBytes": script.expected_payload_size,
                "workbook": script.workbook.file_name,
                "workbookSha256": script.workbook.sha256,
                "sheet": script.workbook.sheet_name,
                "headerRow": script.workbook.header_row,
                "sourceColumn": script.workbook.source_column,
                "replacementColumn": script.workbook.replacement_column,
                "dialogueRows": len(script.rows),
                "edgeFullwidthSpaceWarnings": len(warnings),
                "warnings": warnings,
                "allowedControlChanges": approvals,
            }
        )

    target_ids = {parse_member_id(output.target_member_id) for output in manifest.outputs}
    expected_ids = tuple(sorted(set(original_entries) | target_ids))
    expected_hashes = {
        member_id: sha256_file(payload_dir / member_filename(member_id))
        for member_id in expected_ids
    }
    build_dir = run_dir / "build"
    built_cpk = build_dir / source.name
    cpk_tool.pack(payload_dir, expected_ids, built_cpk)

    verified_dir = run_dir / "verified-extract"
    verified_entries = cpk_tool.extract(built_cpk, verified_dir)
    _validate_tool_entries(verified_entries, verified_dir)
    if tuple(verified_entries) != expected_ids:
        raise DialogueBuildError(
            "리팩 CPK의 ID 목록이 JSON 출력 매핑과 다릅니다: "
            f"기대={[member_filename(i) for i in expected_ids]}, "
            f"실제={[member_filename(i) for i in verified_entries]}"
        )
    for member_id in expected_ids:
        actual = verified_entries[member_id].sha256
        expected = expected_hashes[member_id]
        if actual != expected:
            raise DialogueBuildError(
                f"리팩 후 {member_filename(member_id)} 해시가 달라졌습니다: "
                f"기대={expected}, 실제={actual}"
            )

    if sha256_file(source) != source_sha256:
        raise DialogueBuildError("작업 중 원본 CPK의 SHA-256이 변경되었습니다.")

    output_dir = ensure_output_path(root / "output" / "dialogue" / slug / run_id, root)
    output_dir.mkdir(parents=True, exist_ok=False)
    output_cpk = output_dir / source.name

    # JSON도 실제 빌드 입력과 바이트가 같은지 먼저 검증한 뒤 CPK를 마지막에 게시한다.
    output_manifest = write_dialogue_manifest(
        output_dir / "dialogue-manifest.json", manifest
    )
    manifest_sha256 = sha256_file(manifest_path)
    if sha256_file(output_manifest) != manifest_sha256:
        raise DialogueBuildError("게시한 JSON 매니페스트가 실제 빌드 입력과 다릅니다.")

    temporary = output_dir / f".{source.name}.tmp"
    shutil.copyfile(built_cpk, temporary)
    built_sha256 = sha256_file(built_cpk)
    if sha256_file(temporary) != built_sha256:
        raise DialogueBuildError("게시 직전 복사본이 검증된 작업 CPK와 다릅니다.")
    os.replace(temporary, output_cpk)
    final_sha256 = sha256_file(output_cpk)
    if final_sha256 != built_sha256:
        raise DialogueBuildError("게시한 최종 CPK가 검증된 작업 CPK와 다릅니다.")

    report_path = output_dir / "report.json"
    return {
        "schemaVersion": 2,
        "ok": True,
        "process": "extract-json-rebuild-repack-reextract-verify",
        "inputMode": input_mode,
        "runId": run_id,
        "sourceCpk": str(source),
        "sourceBytes": source.stat().st_size,
        "sourceSha256": source_sha256,
        "sourceMemberIds": [member_filename(i) for i in original_entries],
        "outputMemberIds": [member_filename(i) for i in expected_ids],
        "scripts": [script.script_id for script in manifest.scripts],
        "entries": entry_reports,
        "tool": {
            "version": cpk_tool.executable_version,
            "executableSha256": cpk_tool.executable_sha256,
            "packMode": "ID",
            "alignment": 16,
            "compression": "uncompressed",
            "dateTimeInformation": False,
        },
        "verification": {
            "manifestStrictlyReloaded": True,
            "reextracted": True,
            "allMemberIdsMatch": True,
            "allPayloadHashesMatch": True,
            "sourceUnchanged": True,
        },
        "dialogueManifest": str(output_manifest),
        "dialogueManifestSha256": manifest_sha256,
        "workManifest": str(manifest_path),
        "workDir": str(run_dir),
        "outputCpk": str(output_cpk),
        "outputBytes": output_cpk.stat().st_size,
        "outputSha256": final_sha256,
        "reportPath": str(report_path),
        "nextStep": "Vita3K에서 별도 복사본으로 시험한 뒤 실기 Vita를 별도로 검증하세요.",
    }


def _prepare_source(
    source_cpk: Path, workspace_root: Path | None
) -> tuple[Path, Path, str]:
    root = (workspace_root or project_root()).resolve()
    source = source_cpk.expanduser().resolve()
    if not source.is_file():
        raise DialogueBuildError(f"원본 CPK를 찾을 수 없습니다: {source}")
    if source.suffix.casefold() != ".cpk":
        raise DialogueBuildError(f"입력 파일의 확장자가 .cpk가 아닙니다: {source}")
    return root, source, sha256_file(source)


def _create_run(
    root: Path,
    source: Path,
    source_sha256: str,
    cpk_tool: CpkTool,
) -> tuple[str, str, Path]:
    run_id = _new_run_id(source_sha256)
    slug = _safe_slug(source.stem)
    run_dir = ensure_output_path(root / "work" / "cpk-runs" / slug / run_id, root)
    run_dir.mkdir(parents=True, exist_ok=False)
    if isinstance(cpk_tool, CpkMakerTool):
        cpk_tool.log_path = run_dir / "tool-invocations.jsonl"
    return run_id, slug, run_dir


def _validate_manifest_source_file(
    manifest: DialogueManifest, source: Path, actual_sha256: str
) -> None:
    expected = manifest.source_cpk
    if source.name != expected.file_name:
        raise DialogueBuildError(
            f"JSON의 원본 CPK 파일명과 다릅니다: 기대={expected.file_name}, 실제={source.name}"
        )
    if source.stat().st_size != expected.size:
        raise DialogueBuildError(
            f"JSON의 원본 CPK 크기와 다릅니다: 기대={expected.size}, 실제={source.stat().st_size}"
        )
    if actual_sha256 != expected.sha256:
        raise DialogueBuildError(
            f"JSON의 원본 CPK SHA-256과 다릅니다: 기대={expected.sha256}, 실제={actual_sha256}"
        )


def _validate_manifest_tool(manifest: DialogueManifest, cpk_tool: CpkTool) -> None:
    if cpk_tool.executable_sha256 != manifest.pack.tool_sha256:
        raise DialogueBuildError(
            "JSON을 만든 CPK 도구 SHA-256과 현재 도구가 다릅니다: "
            f"기대={manifest.pack.tool_sha256}, 실제={cpk_tool.executable_sha256}"
        )
    if cpk_tool.executable_version != manifest.pack.tool_version:
        raise DialogueBuildError(
            "JSON을 만든 CPK 도구 버전과 현재 도구가 다릅니다: "
            f"기대={manifest.pack.tool_version}, 실제={cpk_tool.executable_version}"
        )


def _validate_manifest_members(
    manifest: DialogueManifest, entries: Mapping[int, CpkEntry]
) -> None:
    expected_ids = tuple(parse_member_id(item.member_id) for item in manifest.source_cpk.members)
    if tuple(entries) != expected_ids:
        raise DialogueBuildError(
            "JSON의 원본 멤버 목록과 실제 CPK가 다릅니다: "
            f"기대={[member_filename(i) for i in expected_ids]}, "
            f"실제={[member_filename(i) for i in entries]}"
        )
    for snapshot in manifest.source_cpk.members:
        member_id = parse_member_id(snapshot.member_id)
        actual = entries[member_id]
        if actual.size != snapshot.size or actual.sha256 != snapshot.sha256:
            raise DialogueBuildError(
                f"JSON의 {snapshot.member_id} 크기 또는 해시가 실제 추출물과 다릅니다."
            )


def _validate_requests(
    entries: Sequence[DialogueEntryRequest],
) -> tuple[DialogueEntryRequest, ...]:
    if not entries:
        raise DialogueBuildError("적용할 대사 XLSX 매핑을 하나 이상 지정해야 합니다.")
    result: list[DialogueEntryRequest] = []
    targets: set[int] = set()
    for request in tuple(entries):
        if not isinstance(request, DialogueEntryRequest):
            raise DialogueBuildError("대사 ID 매핑 형식이 올바르지 않습니다.")
        member_filename(request.target_id)
        member_filename(request.effective_source_id)
        if request.target_id in targets:
            raise DialogueBuildError(
                f"대상 ID가 중복되었습니다: {member_filename(request.target_id)}"
            )
        targets.add(request.target_id)
        workbook = request.workbook_path.expanduser().resolve()
        if not workbook.is_file():
            raise DialogueBuildError(f"번역 XLSX를 찾을 수 없습니다: {workbook}")
        if workbook.suffix.casefold() not in (".xlsx", ".xlsm"):
            raise DialogueBuildError(f"번역 파일이 XLSX가 아닙니다: {workbook}")
        result.append(DialogueEntryRequest(request.target_id, workbook, request.source_id))
    return tuple(result)


def _validate_allowed_changes(
    changes: Iterable[AllowedControlChange],
    requests: Sequence[DialogueEntryRequest],
) -> frozenset[AllowedControlChange]:
    try:
        result = frozenset(changes)
    except TypeError as error:
        raise DialogueBuildError("제어 토큰 예외 목록 형식이 올바르지 않습니다.") from error
    targets = {request.target_id for request in requests}
    for item in result:
        if not isinstance(item, AllowedControlChange):
            raise DialogueBuildError("제어 토큰 예외 형식이 올바르지 않습니다.")
        member_filename(item.target_id)
        if item.target_id not in targets:
            raise DialogueBuildError(
                f"제어 토큰 예외 대상이 --entry에 없습니다: {member_filename(item.target_id)}"
            )
        if item.workbook_row < 1:
            raise DialogueBuildError("제어 토큰 예외의 XLSX 행 번호는 1 이상이어야 합니다.")
    return result


def _validate_tool_entries(entries: Mapping[int, CpkEntry], root: Path) -> None:
    base = root.resolve()
    if not entries:
        raise DialogueBuildError("CPK 추출 결과가 비어 있습니다.")
    if tuple(entries) != tuple(sorted(entries)):
        raise DialogueBuildError("CPK 도구가 멤버 ID를 정렬된 순서로 반환하지 않았습니다.")
    for member_id, entry in entries.items():
        if member_id != entry.member_id:
            raise DialogueBuildError("CPK 도구가 ID와 항목 정보를 서로 다르게 반환했습니다.")
        member_filename(member_id)
        path = entry.path.resolve()
        if not path.is_file() or not path.is_relative_to(base):
            raise DialogueBuildError(f"CPK 도구가 작업 폴더 밖의 파일을 반환했습니다: {path}")
        if path.name != member_filename(member_id):
            raise DialogueBuildError(f"CPK 멤버 파일명이 ID와 다릅니다: {path.name}")
        if entry.size != path.stat().st_size or entry.sha256 != sha256_file(path):
            raise DialogueBuildError(f"CPK 멤버 보고값이 실제 파일과 다릅니다: {path.name}")


def _record_failure(
    path: Path,
    run_id: str,
    source: Path,
    source_sha256: str,
    error: Exception,
) -> None:
    value: dict[str, object] = {
        "schemaVersion": 2,
        "ok": False,
        "runId": run_id,
        "sourceCpk": str(source),
        "sourceSha256": source_sha256,
        "errorType": type(error).__name__,
        "error": str(error),
    }
    if isinstance(error, DialogueManifestError):
        value["errorCode"] = error.code
        value["jsonPath"] = error.json_path
    _write_json(path, value)


def _publish_reports(run_dir: Path, report: dict[str, object]) -> None:
    _write_json(run_dir / "report.json", report)
    _write_json(Path(str(report["reportPath"])), report)


def _new_run_id(source_sha256: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{timestamp}-{source_sha256[:8]}"


def _safe_slug(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return (result or "cpk")[:80]


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)


__all__ = [
    "AllowedControlChange",
    "DialogueBuildError",
    "DialogueEntryRequest",
    "build_dialogue_cpk",
    "build_dialogue_cpk_from_manifest",
]
