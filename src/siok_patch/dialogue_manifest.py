"""XLSX와 대사 payload 사이의 엄격하고 재현 가능한 JSON 빌드 경계."""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from .control_codes import extract_control_tokens
from .cpk_tool import CpkEntry, CpkToolError, member_filename, parse_member_id
from .dialogue_workbook import DialogueWorkbookData
from .stage_script import (
    DialogueSlot,
    ParsedStageScript,
    normalize_replacement_source,
    parse_stage_script,
    rebuild_stage_script,
)


DOCUMENT_TYPE = "siok.dialogue-build-manifest"
SCHEMA_VERSION = 1
PARSER_PROFILE = "stage-script-cp932-v1"
NORMALIZATION_PROFILE = "legacy-dialogue-insert-v1"
PACK_PROFILE = "cpkmakec-id-align16-uc-v1"

_HASH = re.compile(r"^[0-9a-f]{64}$")
_SCRIPT_ID = re.compile(r"^script-[0-9]{3,}$")
_TAGS = frozenset(("SP", "SB", "SF", "SG", "S1", "S2", "S3", "SM", "SE", "ST"))
_LINE_ENDINGS = frozenset(("crlf", "lf", "none"))
_SOURCE_MATCHES = frozenset(("exact", "edge-fullwidth-space"))


class DialogueManifestError(ValueError):
    """대사 빌드 매니페스트가 엄격한 계약을 만족하지 않을 때 발생한다."""

    def __init__(self, code: str, json_path: str, detail: str) -> None:
        self.code = code
        self.json_path = json_path
        self.detail = detail
        super().__init__(f"[{code}] {json_path}: {detail}")


@dataclass(frozen=True, slots=True)
class ManifestMember:
    member_id: str
    size: int
    sha256: str

    def to_dict(self) -> dict[str, object]:
        return {"id": self.member_id, "bytes": self.size, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class ManifestSourceCpk:
    file_name: str
    size: int
    sha256: str
    members: tuple[ManifestMember, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "fileName": self.file_name,
            "bytes": self.size,
            "sha256": self.sha256,
            "members": [item.to_dict() for item in self.members],
        }


@dataclass(frozen=True, slots=True)
class ManifestPack:
    profile: str
    tool_sha256: str
    tool_version: str

    def to_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "toolSha256": self.tool_sha256,
            "toolVersion": self.tool_version,
        }


@dataclass(frozen=True, slots=True)
class ManifestWorkbook:
    file_name: str
    sha256: str
    sheet_name: str
    header_row: int
    source_header: str
    replacement_header: str
    source_column: int
    replacement_column: int
    edge_fullwidth_space_warnings: int

    def to_dict(self) -> dict[str, object]:
        return {
            "fileName": self.file_name,
            "sha256": self.sha256,
            "sheetName": self.sheet_name,
            "headerRow": self.header_row,
            "sourceHeader": self.source_header,
            "replacementHeader": self.replacement_header,
            "sourceColumn": self.source_column,
            "replacementColumn": self.replacement_column,
            "edgeFullwidthSpaceWarnings": self.edge_fullwidth_space_warnings,
        }


@dataclass(frozen=True, slots=True)
class ManifestApproval:
    ordinal: int
    workbook_row: int
    source_raw_line_sha256: str
    source_tokens: tuple[str, ...]
    replacement_tokens: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": "approved-change",
            "ordinal": self.ordinal,
            "workbookRow": self.workbook_row,
            "sourceRawLineSha256": self.source_raw_line_sha256,
            "sourceTokens": list(self.source_tokens),
            "replacementTokens": list(self.replacement_tokens),
        }


@dataclass(frozen=True, slots=True)
class ManifestControl:
    source_tokens: tuple[str, ...]
    replacement_tokens: tuple[str, ...]
    approval: ManifestApproval | None

    def to_dict(self) -> dict[str, object]:
        return {
            "sourceTokens": list(self.source_tokens),
            "replacementTokens": list(self.replacement_tokens),
            "approval": None if self.approval is None else self.approval.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ManifestRow:
    ordinal: int
    source_line_number: int
    tag: str
    source_raw_line_sha256: str
    line_ending: str
    source_text: str
    workbook_row: int
    workbook_source_text: str
    source_match: str
    replacement_text: str
    control: ManifestControl

    def to_dict(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "sourceLineNumber": self.source_line_number,
            "tag": self.tag,
            "sourceRawLineSha256": self.source_raw_line_sha256,
            "lineEnding": self.line_ending,
            "sourceText": self.source_text,
            "workbookRow": self.workbook_row,
            "workbookSourceText": self.workbook_source_text,
            "sourceMatch": self.source_match,
            "replacementText": self.replacement_text,
            "control": self.control.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ManifestScript:
    script_id: str
    source_member_id: str
    source_payload_size: int
    source_payload_sha256: str
    parser_profile: str
    normalization_profile: str
    encoding: str
    workbook: ManifestWorkbook
    rows: tuple[ManifestRow, ...]
    expected_payload_size: int
    expected_payload_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "scriptId": self.script_id,
            "sourceMemberId": self.source_member_id,
            "sourcePayloadBytes": self.source_payload_size,
            "sourcePayloadSha256": self.source_payload_sha256,
            "parserProfile": self.parser_profile,
            "normalizationProfile": self.normalization_profile,
            "encoding": self.encoding,
            "workbook": self.workbook.to_dict(),
            "rows": [row.to_dict() for row in self.rows],
            "expectedPayloadBytes": self.expected_payload_size,
            "expectedPayloadSha256": self.expected_payload_sha256,
        }


@dataclass(frozen=True, slots=True)
class ManifestOutput:
    target_member_id: str
    script_id: str

    def to_dict(self) -> dict[str, object]:
        return {"targetMemberId": self.target_member_id, "scriptId": self.script_id}


@dataclass(frozen=True, slots=True)
class DialogueManifest:
    source_cpk: ManifestSourceCpk
    pack: ManifestPack
    scripts: tuple[ManifestScript, ...]
    outputs: tuple[ManifestOutput, ...]
    document_type: str = DOCUMENT_TYPE
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "documentType": self.document_type,
            "schemaVersion": self.schema_version,
            "sourceCpk": self.source_cpk.to_dict(),
            "pack": self.pack.to_dict(),
            "scripts": [script.to_dict() for script in self.scripts],
            "outputs": [output.to_dict() for output in self.outputs],
        }


def compile_manifest_script(
    source_member_id: int,
    source_payload: bytes,
    source_payload_sha256: str,
    workbook: DialogueWorkbookData,
    *,
    approved_workbook_rows: Iterable[int] = (),
) -> ManifestScript:
    """검증한 XLSX와 원본 payload를 불변 JSON 스크립트로 정규화한다."""

    raw = bytes(source_payload)
    actual_hash = sha256(raw).hexdigest()
    if actual_hash != source_payload_sha256:
        _fail(
            "source-member-hash-mismatch",
            "$.scripts",
            f"전달된 원본 payload 해시가 실제 값과 다릅니다: {actual_hash}",
        )
    parsed = parse_stage_script(raw)
    if len(parsed.slots) != len(workbook.rows):
        _fail(
            "manifest-row-count",
            "$.scripts",
            f"원본 슬롯 {len(parsed.slots)}개와 XLSX 행 {len(workbook.rows)}개가 다릅니다.",
        )

    approved = _approved_row_set(approved_workbook_rows)
    used_approvals: set[int] = set()
    rows: list[ManifestRow] = []
    for slot, workbook_row in zip(parsed.slots, workbook.rows, strict=True):
        source_tokens = extract_control_tokens(slot.source_text)
        replacement_tokens = extract_control_tokens(workbook_row.replacement_text)
        raw_hash = sha256(slot.raw_line).hexdigest()
        approval: ManifestApproval | None = None
        if source_tokens != replacement_tokens:
            if workbook_row.workbook_row not in approved:
                _fail(
                    "approval-required",
                    f"$.scripts[].rows[{slot.ordinal - 1}].control",
                    f"XLSX {workbook_row.workbook_row}행의 제어 토큰 변경을 승인해야 합니다.",
                )
            used_approvals.add(workbook_row.workbook_row)
            approval = ManifestApproval(
                ordinal=slot.ordinal,
                workbook_row=workbook_row.workbook_row,
                source_raw_line_sha256=raw_hash,
                source_tokens=source_tokens,
                replacement_tokens=replacement_tokens,
            )
        rows.append(
            ManifestRow(
                ordinal=slot.ordinal,
                source_line_number=slot.line_number,
                tag=slot.tag,
                source_raw_line_sha256=raw_hash,
                line_ending=_line_ending_name(slot.ending),
                source_text=slot.source_text,
                workbook_row=workbook_row.workbook_row,
                workbook_source_text=workbook_row.source_text,
                source_match=workbook_row.source_match.replace("_", "-"),
                replacement_text=workbook_row.replacement_text,
                control=ManifestControl(source_tokens, replacement_tokens, approval),
            )
        )

    unused = approved - used_approvals
    if unused:
        values = ", ".join(str(value) for value in sorted(unused))
        _fail(
            "approval-stale",
            "$.scripts[].rows",
            f"실제 제어 토큰 변경과 맞지 않는 XLSX 승인 행입니다: {values}",
        )

    rebuilt = rebuild_stage_script(
        parsed,
        tuple(row.replacement_text for row in workbook.rows),
    )
    manifest_workbook = ManifestWorkbook(
        file_name=workbook.path.name,
        sha256=workbook.file_sha256,
        sheet_name=workbook.sheet_name,
        header_row=workbook.header_row,
        source_header=workbook.source_header,
        replacement_header=workbook.replacement_header,
        source_column=workbook.source_column,
        replacement_column=workbook.replacement_column,
        edge_fullwidth_space_warnings=workbook.edge_space_mismatch_count,
    )
    return ManifestScript(
        script_id="pending",
        source_member_id=member_filename(source_member_id),
        source_payload_size=len(raw),
        source_payload_sha256=actual_hash,
        parser_profile=PARSER_PROFILE,
        normalization_profile=NORMALIZATION_PROFILE,
        encoding="cp932",
        workbook=manifest_workbook,
        rows=tuple(rows),
        expected_payload_size=len(rebuilt),
        expected_payload_sha256=sha256(rebuilt).hexdigest(),
    )


def compose_dialogue_manifest(
    *,
    source_cpk_name: str,
    source_cpk_size: int,
    source_cpk_sha256: str,
    original_entries: Mapping[int, CpkEntry],
    tool_sha256: str,
    tool_version: str,
    compiled_outputs: Sequence[tuple[int, ManifestScript]],
) -> DialogueManifest:
    """컴파일된 스크립트를 중복 제거하고 명시적 출력 ID와 연결한다."""

    if not compiled_outputs:
        _fail("manifest-schema", "$.outputs", "출력을 하나 이상 지정해야 합니다.")

    unique_by_fingerprint: dict[str, ManifestScript] = {}
    target_to_fingerprint: dict[int, str] = {}
    for target_id, script in compiled_outputs:
        member_filename(target_id)
        if target_id in target_to_fingerprint:
            _fail(
                "manifest-reference",
                "$.outputs",
                f"대상 ID가 중복되었습니다: {member_filename(target_id)}",
            )
        fingerprint = _script_fingerprint(script)
        unique_by_fingerprint.setdefault(fingerprint, script)
        target_to_fingerprint[target_id] = fingerprint

    ordered_fingerprints = sorted(
        unique_by_fingerprint,
        key=lambda value: (
            parse_member_id(unique_by_fingerprint[value].source_member_id),
            unique_by_fingerprint[value].workbook.file_name.casefold(),
            unique_by_fingerprint[value].workbook.sha256,
            value,
        ),
    )
    fingerprint_to_id: dict[str, str] = {}
    scripts: list[ManifestScript] = []
    for index, fingerprint in enumerate(ordered_fingerprints, start=1):
        script_id = f"script-{index:03d}"
        fingerprint_to_id[fingerprint] = script_id
        scripts.append(replace(unique_by_fingerprint[fingerprint], script_id=script_id))

    outputs = tuple(
        ManifestOutput(member_filename(target), fingerprint_to_id[fingerprint])
        for target, fingerprint in sorted(target_to_fingerprint.items())
    )
    members = tuple(
        ManifestMember(member_filename(member_id), entry.size, entry.sha256)
        for member_id, entry in sorted(original_entries.items())
    )
    manifest = DialogueManifest(
        source_cpk=ManifestSourceCpk(
            file_name=source_cpk_name,
            size=source_cpk_size,
            sha256=source_cpk_sha256,
            members=members,
        ),
        pack=ManifestPack(PACK_PROFILE, tool_sha256, tool_version),
        scripts=tuple(scripts),
        outputs=outputs,
    )
    return parse_dialogue_manifest_dict(manifest.to_dict())


def apply_manifest_script(script: ManifestScript, source_payload: bytes) -> bytes:
    """JSON 스크립트를 원본 payload에 적용하고 모든 바인딩을 다시 검증한다."""

    raw = bytes(source_payload)
    path = f"$.scripts[{script.script_id}]"
    if len(raw) != script.source_payload_size:
        _fail(
            "source-member-size-mismatch",
            path + ".sourcePayloadBytes",
            f"기대 {script.source_payload_size}바이트, 실제 {len(raw)}바이트입니다.",
        )
    actual_hash = sha256(raw).hexdigest()
    if actual_hash != script.source_payload_sha256:
        _fail(
            "source-member-hash-mismatch",
            path + ".sourcePayloadSha256",
            f"기대 {script.source_payload_sha256}, 실제 {actual_hash}입니다.",
        )
    parsed = parse_stage_script(raw)
    if len(parsed.slots) != len(script.rows):
        _fail(
            "manifest-row-count",
            path + ".rows",
            f"JSON {len(script.rows)}행, 원본 슬롯 {len(parsed.slots)}개입니다.",
        )
    for row, slot in zip(script.rows, parsed.slots, strict=True):
        _validate_row_binding(row, slot, path)

    replacements = tuple(row.replacement_text for row in script.rows)
    rebuilt = rebuild_stage_script(parsed, replacements)
    if len(rebuilt) != script.expected_payload_size:
        _fail(
            "expected-payload-size-mismatch",
            path + ".expectedPayloadBytes",
            f"기대 {script.expected_payload_size}바이트, 실제 {len(rebuilt)}바이트입니다.",
        )
    rebuilt_hash = sha256(rebuilt).hexdigest()
    if rebuilt_hash != script.expected_payload_sha256:
        _fail(
            "expected-payload-hash-mismatch",
            path + ".expectedPayloadSha256",
            f"기대 {script.expected_payload_sha256}, 실제 {rebuilt_hash}입니다.",
        )

    reparsed = parse_stage_script(rebuilt)
    expected_rows = tuple(
        normalize_replacement_source(row.replacement_text, row.tag)
        for row in script.rows
    )
    if reparsed.source_rows != expected_rows:
        index = _first_difference(reparsed.source_rows, expected_rows)
        _fail(
            "rebuilt-reparse-mismatch",
            path + ".rows",
            f"재파싱한 대사 {index}번이 JSON 교체값과 다릅니다.",
        )
    return rebuilt


def dump_dialogue_manifest(manifest: DialogueManifest) -> bytes:
    """매니페스트를 UTF-8·LF·고정 필드 순서로 직렬화한다."""

    validated = parse_dialogue_manifest_dict(manifest.to_dict())
    text = json.dumps(
        validated.to_dict(),
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    )
    return (text + "\n").encode("utf-8")


def write_dialogue_manifest(path: str | Path, manifest: DialogueManifest) -> Path:
    """정규 JSON을 임시 파일에 쓴 뒤 원자적으로 게시한다."""

    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(dump_dialogue_manifest(manifest))
    os.replace(temporary, destination)
    return destination


def load_dialogue_manifest(path: str | Path) -> DialogueManifest:
    """중복 키와 비표준 숫자까지 거부하며 JSON 파일을 읽는다."""

    source = Path(path).expanduser().resolve()
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        _fail("manifest-read", "$", f"JSON을 읽을 수 없습니다: {source} ({error})")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except DialogueManifestError:
        raise
    except json.JSONDecodeError as error:
        _fail(
            "manifest-json",
            "$",
            f"JSON 문법 오류입니다({error.lineno}행 {error.colno}열): {error.msg}",
        )
    return parse_dialogue_manifest_dict(value)


def parse_dialogue_manifest_dict(value: object) -> DialogueManifest:
    root = _mapping(value, "$", "manifest-type")
    _keys(
        root,
        ("documentType", "schemaVersion", "sourceCpk", "pack", "scripts", "outputs"),
        "$",
    )
    document_type = _string(root, "documentType", "$", nonempty=True)
    if document_type != DOCUMENT_TYPE:
        _fail(
            "manifest-schema",
            "$.documentType",
            f"{DOCUMENT_TYPE!r}만 지원합니다.",
        )
    schema_version = _integer(root, "schemaVersion", "$", minimum=1)
    if schema_version != SCHEMA_VERSION:
        _fail(
            "manifest-schema",
            "$.schemaVersion",
            f"버전 {SCHEMA_VERSION}만 지원합니다.",
        )

    source_cpk = _parse_source_cpk(root["sourceCpk"], "$.sourceCpk")
    pack = _parse_pack(root["pack"], "$.pack")
    raw_scripts = _array(root["scripts"], "$.scripts", nonempty=True)
    scripts = tuple(
        _parse_script(item, f"$.scripts[{index}]")
        for index, item in enumerate(raw_scripts)
    )
    raw_outputs = _array(root["outputs"], "$.outputs", nonempty=True)
    outputs = tuple(
        _parse_output(item, f"$.outputs[{index}]")
        for index, item in enumerate(raw_outputs)
    )

    script_ids = [script.script_id for script in scripts]
    if len(script_ids) != len(set(script_ids)):
        _fail("manifest-reference", "$.scripts", "scriptId가 중복되었습니다.")
    expected_script_ids = [
        f"script-{index:03d}" for index in range(1, len(script_ids) + 1)
    ]
    if script_ids != expected_script_ids:
        _fail(
            "manifest-row-order",
            "$.scripts",
            "scriptId는 script-001부터 빠짐없이 순서대로 기록해야 합니다.",
        )

    source_members = {item.member_id: item for item in source_cpk.members}
    for index, script in enumerate(scripts):
        member = source_members.get(script.source_member_id)
        if member is None:
            _fail(
                "manifest-reference",
                f"$.scripts[{index}].sourceMemberId",
                "sourceCpk.members에 없는 ID입니다.",
            )
        if (
            member.size != script.source_payload_size
            or member.sha256 != script.source_payload_sha256
        ):
            _fail(
                "manifest-reference",
                f"$.scripts[{index}]",
                "source member의 크기 또는 해시가 sourceCpk.members와 다릅니다.",
            )

    known_scripts = set(script_ids)
    targets: list[int] = []
    referenced_scripts: set[str] = set()
    for index, output in enumerate(outputs):
        target = parse_member_id(output.target_member_id)
        targets.append(target)
        if output.script_id not in known_scripts:
            _fail(
                "manifest-reference",
                f"$.outputs[{index}].scriptId",
                "존재하지 않는 scriptId입니다.",
            )
        referenced_scripts.add(output.script_id)
    if len(targets) != len(set(targets)):
        _fail("manifest-reference", "$.outputs", "targetMemberId가 중복되었습니다.")
    if targets != sorted(targets):
        _fail("manifest-row-order", "$.outputs", "targetMemberId 순서가 정렬되어 있지 않습니다.")
    unused_scripts = known_scripts - referenced_scripts
    if unused_scripts:
        _fail(
            "manifest-reference",
            "$.scripts",
            "출력에서 사용하지 않는 scriptId가 있습니다: " + ", ".join(sorted(unused_scripts)),
        )

    return DialogueManifest(
        document_type=document_type,
        schema_version=schema_version,
        source_cpk=source_cpk,
        pack=pack,
        scripts=scripts,
        outputs=outputs,
    )


def _parse_source_cpk(value: object, path: str) -> ManifestSourceCpk:
    item = _mapping(value, path, "manifest-type")
    _keys(item, ("fileName", "bytes", "sha256", "members"), path)
    file_name = _file_name(_string(item, "fileName", path, nonempty=True), path + ".fileName", ".cpk")
    size = _integer(item, "bytes", path, minimum=0)
    digest = _digest(item, "sha256", path)
    raw_members = _array(item["members"], path + ".members", nonempty=True)
    members = tuple(
        _parse_member(member, f"{path}.members[{index}]")
        for index, member in enumerate(raw_members)
    )
    ids = [parse_member_id(member.member_id) for member in members]
    if len(ids) != len(set(ids)):
        _fail("manifest-reference", path + ".members", "멤버 ID가 중복되었습니다.")
    if ids != sorted(ids):
        _fail("manifest-row-order", path + ".members", "멤버 ID가 정렬되어 있지 않습니다.")
    return ManifestSourceCpk(file_name, size, digest, members)


def _parse_member(value: object, path: str) -> ManifestMember:
    item = _mapping(value, path, "manifest-type")
    _keys(item, ("id", "bytes", "sha256"), path)
    return ManifestMember(
        _member_id(_string(item, "id", path, nonempty=True), path + ".id"),
        _integer(item, "bytes", path, minimum=0),
        _digest(item, "sha256", path),
    )


def _parse_pack(value: object, path: str) -> ManifestPack:
    item = _mapping(value, path, "manifest-type")
    _keys(item, ("profile", "toolSha256", "toolVersion"), path)
    profile = _string(item, "profile", path, nonempty=True)
    if profile != PACK_PROFILE:
        _fail("manifest-schema", path + ".profile", f"{PACK_PROFILE!r}만 지원합니다.")
    return ManifestPack(
        profile,
        _digest(item, "toolSha256", path),
        _string(item, "toolVersion", path, nonempty=True),
    )


def _parse_script(value: object, path: str) -> ManifestScript:
    item = _mapping(value, path, "manifest-type")
    _keys(
        item,
        (
            "scriptId",
            "sourceMemberId",
            "sourcePayloadBytes",
            "sourcePayloadSha256",
            "parserProfile",
            "normalizationProfile",
            "encoding",
            "workbook",
            "rows",
            "expectedPayloadBytes",
            "expectedPayloadSha256",
        ),
        path,
    )
    script_id = _string(item, "scriptId", path, nonempty=True)
    if not _SCRIPT_ID.fullmatch(script_id):
        _fail("manifest-schema", path + ".scriptId", "script-001 형식이어야 합니다.")
    parser_profile = _string(item, "parserProfile", path, nonempty=True)
    normalization = _string(item, "normalizationProfile", path, nonempty=True)
    encoding = _string(item, "encoding", path, nonempty=True)
    if parser_profile != PARSER_PROFILE:
        _fail("manifest-schema", path + ".parserProfile", f"{PARSER_PROFILE!r}만 지원합니다.")
    if normalization != NORMALIZATION_PROFILE:
        _fail(
            "manifest-schema",
            path + ".normalizationProfile",
            f"{NORMALIZATION_PROFILE!r}만 지원합니다.",
        )
    if encoding != "cp932":
        _fail("manifest-schema", path + ".encoding", "cp932만 지원합니다.")
    raw_rows = _array(item["rows"], path + ".rows", nonempty=True)
    rows = tuple(
        _parse_row(row, f"{path}.rows[{index}]")
        for index, row in enumerate(raw_rows)
    )
    previous_line = 0
    previous_workbook_row = 0
    for index, row in enumerate(rows, start=1):
        if row.ordinal != index:
            _fail(
                "manifest-row-order",
                f"{path}.rows[{index - 1}].ordinal",
                f"ordinal은 {index}이어야 합니다.",
            )
        if row.source_line_number <= previous_line:
            _fail(
                "manifest-row-order",
                f"{path}.rows[{index - 1}].sourceLineNumber",
                "원본 행 번호는 엄격히 증가해야 합니다.",
            )
        if row.workbook_row <= previous_workbook_row:
            _fail(
                "manifest-row-order",
                f"{path}.rows[{index - 1}].workbookRow",
                "XLSX 행 번호는 엄격히 증가해야 합니다.",
            )
        previous_line = row.source_line_number
        previous_workbook_row = row.workbook_row
    workbook = _parse_workbook(item["workbook"], path + ".workbook")
    edge_warning_count = sum(
        row.source_match == "edge-fullwidth-space" for row in rows
    )
    if edge_warning_count != workbook.edge_fullwidth_space_warnings:
        _fail(
            "workbook-row-mismatch",
            path + ".workbook.edgeFullwidthSpaceWarnings",
            f"행에서 다시 계산한 값 {edge_warning_count}과 다릅니다.",
        )
    return ManifestScript(
        script_id=script_id,
        source_member_id=_member_id(
            _string(item, "sourceMemberId", path, nonempty=True),
            path + ".sourceMemberId",
        ),
        source_payload_size=_integer(item, "sourcePayloadBytes", path, minimum=0),
        source_payload_sha256=_digest(item, "sourcePayloadSha256", path),
        parser_profile=parser_profile,
        normalization_profile=normalization,
        encoding=encoding,
        workbook=workbook,
        rows=rows,
        expected_payload_size=_integer(item, "expectedPayloadBytes", path, minimum=0),
        expected_payload_sha256=_digest(item, "expectedPayloadSha256", path),
    )


def _parse_workbook(value: object, path: str) -> ManifestWorkbook:
    item = _mapping(value, path, "manifest-type")
    _keys(
        item,
        (
            "fileName",
            "sha256",
            "sheetName",
            "headerRow",
            "sourceHeader",
            "replacementHeader",
            "sourceColumn",
            "replacementColumn",
            "edgeFullwidthSpaceWarnings",
        ),
        path,
    )
    source_column = _integer(item, "sourceColumn", path, minimum=1)
    replacement_column = _integer(item, "replacementColumn", path, minimum=1)
    if source_column == replacement_column:
        _fail("manifest-schema", path, "원문 열과 치환문 열은 달라야 합니다.")
    return ManifestWorkbook(
        file_name=_file_name(
            _string(item, "fileName", path, nonempty=True),
            path + ".fileName",
            ".xlsx",
        ),
        sha256=_digest(item, "sha256", path),
        sheet_name=_string(item, "sheetName", path, nonempty=True),
        header_row=_integer(item, "headerRow", path, minimum=1),
        source_header=_string(item, "sourceHeader", path, nonempty=True),
        replacement_header=_string(item, "replacementHeader", path, nonempty=True),
        source_column=source_column,
        replacement_column=replacement_column,
        edge_fullwidth_space_warnings=_integer(
            item, "edgeFullwidthSpaceWarnings", path, minimum=0
        ),
    )


def _parse_row(value: object, path: str) -> ManifestRow:
    item = _mapping(value, path, "manifest-type")
    _keys(
        item,
        (
            "ordinal",
            "sourceLineNumber",
            "tag",
            "sourceRawLineSha256",
            "lineEnding",
            "sourceText",
            "workbookRow",
            "workbookSourceText",
            "sourceMatch",
            "replacementText",
            "control",
        ),
        path,
    )
    tag = _string(item, "tag", path, nonempty=True)
    if tag not in _TAGS:
        _fail("manifest-schema", path + ".tag", f"알 수 없는 태그입니다: {tag!r}")
    line_ending = _string(item, "lineEnding", path, nonempty=True)
    if line_ending not in _LINE_ENDINGS:
        _fail("manifest-schema", path + ".lineEnding", "crlf, lf, none 중 하나여야 합니다.")
    source_match = _string(item, "sourceMatch", path, nonempty=True)
    if source_match not in _SOURCE_MATCHES:
        _fail(
            "manifest-schema",
            path + ".sourceMatch",
            "exact 또는 edge-fullwidth-space여야 합니다.",
        )
    source_text = _string(item, "sourceText", path)
    workbook_source = _string(item, "workbookSourceText", path)
    replacement_text = _string(item, "replacementText", path)
    for key, text in (
        ("sourceText", source_text),
        ("workbookSourceText", workbook_source),
        ("replacementText", replacement_text),
    ):
        if "\r" in text or "\n" in text:
            _fail("manifest-schema", path + "." + key, "줄바꿈을 넣을 수 없습니다.")
        _require_cp932(text, path + "." + key)
    if source_match == "exact" and workbook_source != source_text:
        _fail("workbook-row-mismatch", path + ".sourceMatch", "exact인데 두 원문이 다릅니다.")
    if source_match == "edge-fullwidth-space" and not _edge_space_equal(
        workbook_source, source_text
    ):
        _fail(
            "workbook-row-mismatch",
            path + ".sourceMatch",
            "양끝 전각 공백 차이만 허용됩니다.",
        )

    ordinal = _integer(item, "ordinal", path, minimum=1)
    workbook_row = _integer(item, "workbookRow", path, minimum=1)
    raw_hash = _digest(item, "sourceRawLineSha256", path)
    control = _parse_control(
        item["control"],
        path + ".control",
        ordinal=ordinal,
        workbook_row=workbook_row,
        raw_hash=raw_hash,
        source_text=source_text,
        replacement_text=replacement_text,
    )
    return ManifestRow(
        ordinal=ordinal,
        source_line_number=_integer(item, "sourceLineNumber", path, minimum=1),
        tag=tag,
        source_raw_line_sha256=raw_hash,
        line_ending=line_ending,
        source_text=source_text,
        workbook_row=workbook_row,
        workbook_source_text=workbook_source,
        source_match=source_match,
        replacement_text=replacement_text,
        control=control,
    )


def _parse_control(
    value: object,
    path: str,
    *,
    ordinal: int,
    workbook_row: int,
    raw_hash: str,
    source_text: str,
    replacement_text: str,
) -> ManifestControl:
    item = _mapping(value, path, "manifest-type")
    _keys(item, ("sourceTokens", "replacementTokens", "approval"), path)
    source_tokens = _token_array(item["sourceTokens"], path + ".sourceTokens")
    replacement_tokens = _token_array(
        item["replacementTokens"], path + ".replacementTokens"
    )
    actual_source = extract_control_tokens(source_text)
    actual_replacement = extract_control_tokens(replacement_text)
    if source_tokens != actual_source or replacement_tokens != actual_replacement:
        _fail(
            "approval-binding-mismatch",
            path,
            "제어 토큰 배열이 원문 또는 치환문에서 다시 계산한 값과 다릅니다.",
        )

    raw_approval = item["approval"]
    if source_tokens == replacement_tokens:
        if raw_approval is not None:
            _fail("approval-stale", path + ".approval", "일치하는 행에는 승인을 둘 수 없습니다.")
        approval = None
    else:
        if raw_approval is None:
            _fail("approval-required", path + ".approval", "제어 토큰 변경 승인이 없습니다.")
        approval = _parse_approval(raw_approval, path + ".approval")
        expected = ManifestApproval(
            ordinal,
            workbook_row,
            raw_hash,
            source_tokens,
            replacement_tokens,
        )
        if approval != expected:
            _fail(
                "approval-binding-mismatch",
                path + ".approval",
                "승인이 이 행의 번호·원본 raw 해시·제어 토큰과 정확히 결박되지 않았습니다.",
            )
    return ManifestControl(source_tokens, replacement_tokens, approval)


def _parse_approval(value: object, path: str) -> ManifestApproval:
    item = _mapping(value, path, "manifest-type")
    _keys(
        item,
        (
            "status",
            "ordinal",
            "workbookRow",
            "sourceRawLineSha256",
            "sourceTokens",
            "replacementTokens",
        ),
        path,
    )
    status = _string(item, "status", path, nonempty=True)
    if status != "approved-change":
        _fail("manifest-schema", path + ".status", "approved-change만 허용합니다.")
    return ManifestApproval(
        _integer(item, "ordinal", path, minimum=1),
        _integer(item, "workbookRow", path, minimum=1),
        _digest(item, "sourceRawLineSha256", path),
        _token_array(item["sourceTokens"], path + ".sourceTokens"),
        _token_array(item["replacementTokens"], path + ".replacementTokens"),
    )


def _parse_output(value: object, path: str) -> ManifestOutput:
    item = _mapping(value, path, "manifest-type")
    _keys(item, ("targetMemberId", "scriptId"), path)
    return ManifestOutput(
        _member_id(
            _string(item, "targetMemberId", path, nonempty=True),
            path + ".targetMemberId",
        ),
        _string(item, "scriptId", path, nonempty=True),
    )


def _validate_row_binding(row: ManifestRow, slot: DialogueSlot, script_path: str) -> None:
    path = f"{script_path}.rows[{row.ordinal - 1}]"
    expected = (
        row.ordinal,
        row.source_line_number,
        row.tag,
        row.source_raw_line_sha256,
        row.line_ending,
        row.source_text,
    )
    actual = (
        slot.ordinal,
        slot.line_number,
        slot.tag,
        sha256(slot.raw_line).hexdigest(),
        _line_ending_name(slot.ending),
        slot.source_text,
    )
    if expected != actual:
        labels = (
            "ordinal",
            "sourceLineNumber",
            "tag",
            "sourceRawLineSha256",
            "lineEnding",
            "sourceText",
        )
        first = next(
            label
            for label, wanted, found in zip(labels, expected, actual, strict=True)
            if wanted != found
        )
        _fail(
            "expected-line-binding-mismatch",
            path + "." + first,
            "JSON 행이 실제 원본 payload 행과 다릅니다.",
        )


def _script_fingerprint(script: ManifestScript) -> str:
    value = script.to_dict()
    value["scriptId"] = ""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _approved_row_set(values: Iterable[int]) -> frozenset[int]:
    try:
        result = frozenset(values)
    except TypeError as error:
        _fail("manifest-type", "$.scripts", f"승인 행 목록 형식이 잘못되었습니다: {error}")
    for value in result:
        if type(value) is not int or value < 1:
            _fail("manifest-type", "$.scripts", "승인 XLSX 행은 1 이상의 정수여야 합니다.")
    return result


def _line_ending_name(ending: bytes) -> str:
    if ending == b"\r\n":
        return "crlf"
    if ending == b"\n":
        return "lf"
    if ending == b"":
        return "none"
    _fail("manifest-schema", "$.scripts[].rows[].lineEnding", "알 수 없는 줄바꿈입니다.")


def _first_difference(left: Sequence[str], right: Sequence[str]) -> int:
    for index, (one, two) in enumerate(zip(left, right), start=1):
        if one != two:
            return index
    return min(len(left), len(right)) + 1


def _edge_space_equal(one: str, two: str) -> bool:
    return one != two and one.strip("\u3000") == two.strip("\u3000")


def _require_cp932(value: str, path: str) -> None:
    try:
        value.encode("cp932", errors="strict")
    except UnicodeEncodeError as error:
        character = value[error.start : error.end]
        _fail("manifest-encoding", path, f"{character!r} 문자는 CP932로 쓸 수 없습니다.")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail("manifest-duplicate-key", "$", f"JSON 키가 중복되었습니다: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    _fail("manifest-type", "$", f"JSON 표준 숫자가 아닙니다: {value}")


def _mapping(value: object, path: str, code: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        _fail(code, path, "객체여야 합니다.")
    return value


def _array(value: object, path: str, *, nonempty: bool = False) -> list[object]:
    if not isinstance(value, list):
        _fail("manifest-type", path, "배열이어야 합니다.")
    if nonempty and not value:
        _fail("manifest-schema", path, "빈 배열은 허용하지 않습니다.")
    return value


def _keys(value: Mapping[str, object], required: Sequence[str], path: str) -> None:
    expected = set(required)
    actual = set(value)
    missing = expected - actual
    extra = actual - expected
    if missing:
        _fail("manifest-schema", path, "필수 키가 없습니다: " + ", ".join(sorted(missing)))
    if extra:
        _fail("manifest-unknown-key", path, "알 수 없는 키입니다: " + ", ".join(sorted(extra)))


def _string(
    value: Mapping[str, object],
    key: str,
    path: str,
    *,
    nonempty: bool = False,
) -> str:
    item = value[key]
    if not isinstance(item, str):
        _fail("manifest-type", path + "." + key, "문자열이어야 합니다.")
    if nonempty and item == "":
        _fail("manifest-schema", path + "." + key, "빈 문자열은 허용하지 않습니다.")
    return item


def _integer(value: Mapping[str, object], key: str, path: str, *, minimum: int) -> int:
    item = value[key]
    if type(item) is not int:
        _fail("manifest-type", path + "." + key, "정수여야 합니다.")
    if item < minimum:
        _fail("manifest-schema", path + "." + key, f"{minimum} 이상이어야 합니다.")
    return item


def _digest(value: Mapping[str, object], key: str, path: str) -> str:
    item = _string(value, key, path, nonempty=True)
    if not _HASH.fullmatch(item):
        _fail("manifest-hash", path + "." + key, "소문자 SHA-256 64자리여야 합니다.")
    return item


def _member_id(value: str, path: str) -> str:
    try:
        parsed = parse_member_id(value)
    except (ValueError, CpkToolError) as error:
        _fail("manifest-schema", path, str(error))
    canonical = member_filename(parsed)
    if value != canonical:
        _fail("manifest-schema", path, f"정규 ID 표기는 {canonical}입니다.")
    return canonical


def _file_name(value: str, path: str, suffix: str) -> str:
    if Path(value).name != value or "/" in value or "\\" in value:
        _fail("manifest-schema", path, "경로가 아닌 파일명만 기록해야 합니다.")
    if not value.casefold().endswith(suffix):
        _fail("manifest-schema", path, f"{suffix} 파일명이어야 합니다.")
    return value


def _token_array(value: object, path: str) -> tuple[str, ...]:
    raw = _array(value, path)
    result: list[str] = []
    for index, token in enumerate(raw):
        if not isinstance(token, str):
            _fail("manifest-type", f"{path}[{index}]", "제어 토큰은 문자열이어야 합니다.")
        if extract_control_tokens(token) != (token,):
            _fail("manifest-schema", f"{path}[{index}]", f"정규 제어 토큰이 아닙니다: {token!r}")
        result.append(token)
    return tuple(result)


def _fail(code: str, path: str, detail: str) -> None:
    raise DialogueManifestError(code, path, detail)


__all__ = [
    "DOCUMENT_TYPE",
    "DialogueManifest",
    "DialogueManifestError",
    "ManifestOutput",
    "ManifestScript",
    "apply_manifest_script",
    "compile_manifest_script",
    "compose_dialogue_manifest",
    "dump_dialogue_manifest",
    "load_dialogue_manifest",
    "parse_dialogue_manifest_dict",
    "write_dialogue_manifest",
]
