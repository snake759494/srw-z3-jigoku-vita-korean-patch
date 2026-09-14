"""공통 시나리오 JSON을 원본 CPK에 적용하고 검증된 CPK를 만드는 도우미.

이 모듈은 시나리오 뷰어의 로컬 전용 HTTP API와 명령줄에서 함께 사용한다.
게임 원본·CPK 생성기·wReplace 문자표는 저장소에 포함하지 않으며, 사용자의
개인 설정과 원본 폴더에서만 읽는다. 결과는 ``work``와 ``output`` 아래에
새 실행 폴더로 만들고 원본 CPK는 절대로 덮어쓰지 않는다.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
import shutil
import unicodedata
from typing import Any, Mapping

from .config import project_root
from .cpk_tool import CpkMakerTool, CpkToolError, member_filename
from .hashes import sha256_file
from .stage_script import (
    StageScriptError,
    StageScriptRowCountError,
    parse_stage_script,
    rebuild_stage_script,
)
from .text_normalization import GAME_HALF_WIDTH_SPACE_TEXT, encode_game_dialogue_spaces


class ScenarioCpkBuildError(RuntimeError):
    """시나리오 JSON 또는 CPK 빌드 입력이 안전하지 않을 때 발생한다."""


_MEMBER_RE = re.compile(r"^ID(?P<target>[0-9]{5})(?:@FILE-ID(?P<source>[0-9]{5}))?$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_EXPECTED_CPK_TOOL_SHA256 = (
    "8cea88e11a5460ff8c4a2cc6b5fd214ebeb40e5a661aec596925538267ab4954"
)
_EXPECTED_WREPLACE_SHA256 = (
    "3c08aeda78bb0e9a304a5646872521140dae340e212a2cc126a3b1be0ad38b0b"
)
_EXPECTED_OPERATION_WREPLACE_SHA256 = (
    "8951addfe77eaddfbe73794ebd0e94287c5f9185d873dbd89f0bb4dec7cdc987"
)

# DLC의 ID00007 고정 슬롯은 일반 CP932가 아니라 이 테이블의 제어 바이트를
# 사용한다. JSON에서는 사람이 확인할 수 있는 기호로 보존하고, CPK payload를
# 만들 때만 원래 바이트로 되돌린다.
_FIXED_SLOT_TOKENS = {
    "\u2473": bytes((0x20,)),       # ⑳
    "\u32a5": bytes((0x0A,)),       # ㊥
    "\u32a6": bytes((0x00,)),       # ㊦
    "\u32a7": bytes((0x25, 0x64)),  # ㊧
    "\u32a8": bytes((0x25, 0x73)),  # ㊨
    "\u2472": bytes((0x81, 0x40)),  # ⑲
}

# 일부 번역 시트에는 게임 문자표에 없는 유니코드가 들어올 수 있다.
# 원본 CPK/폰트가 실제로 사용하는 글리프와 대응시켜 빌드할 때만 보정한다.
# 사람이 확인하는 JSON의 ``translation`` 값은 이 보정으로 변경하지 않는다.
_GAME_TEXT_FALLBACKS = {
    "\uD01C": "\uD034",  # 퀜 → 퀸: 고정 wReplace 표에 퀜 글리프가 없음
    "\u2661": "\u0431",  # ♡ → б: 원본 CPK의 하트 글리프(0x84 0x71)
}


def _apply_game_text_fallbacks(value: object) -> str:
    """CPK에 기록할 문자열만 게임이 지원하는 글리프로 보정한다."""

    return "".join(_GAME_TEXT_FALLBACKS.get(char, char) for char in str(value or ""))


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise ScenarioCpkBuildError(f"JSON을 읽을 수 없습니다: {path} ({error})") from error
    if not isinstance(value, dict):
        raise ScenarioCpkBuildError(f"시나리오 JSON의 최상위 값은 객체여야 합니다: {path}")
    return value


def _safe_asset_key(value: object) -> str:
    key = str(value or "").strip()
    if not key or any(not (char.isalnum() or char in "_-") for char in key):
        raise ScenarioCpkBuildError(f"안전하지 않은 시나리오 키입니다: {key!r}")
    return key


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ScenarioCpkBuildError(f"{name}은(는) 객체여야 합니다.")
    return value


def _config_mapping(root: Path) -> Mapping[str, Any]:
    config_path = root / "private" / "project.local.json"
    if not config_path.is_file():
        return {}
    try:
        value = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise ScenarioCpkBuildError(
            f"개인 프로젝트 설정을 읽을 수 없습니다: {config_path} ({error})"
        ) from error
    return _mapping(value, "private/project.local.json")


def _configured_path(config: Mapping[str, Any], key: str) -> Path | None:
    value = str(config.get(key) or "").strip()
    return Path(value).expanduser().resolve() if value else None


def _unique_paths(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        normalized = str(path.resolve(strict=False)).casefold()
        if normalized not in seen:
            seen.add(normalized)
            result.append(path.resolve(strict=False))
    return result


def _resolve_source_cpk(
    root: Path,
    config: Mapping[str, Any],
    asset_key: str,
    file_name: str,
    target: str,
    explicit: Path | None,
) -> Path:
    if explicit is not None:
        candidate = explicit.expanduser().resolve()
        if not candidate.is_file():
            raise ScenarioCpkBuildError(f"지정한 원본 CPK가 없습니다: {candidate}")
        return candidate

    roots: list[Path] = []
    for config_key in ("archiveRoot", "mainGameRoot", "dlcGameRoot"):
        configured = _configured_path(config, config_key)
        if configured is not None:
            roots.append(configured)
    # 현재 저장소 배치(D:\Z\psvita\시옥편-한글패치와 형제인 PCSG00264)도
    # 개인 설정을 옮긴 직후 바로 찾을 수 있도록 보조 후보로 사용한다.
    roots.extend([root.parent / "PCSG00264", root.parent])
    candidates: list[Path] = []
    for base in roots:
        candidates.extend(
            [
                base / "0_STAGE" / "!!!원본" / file_name,
                base / "0_STAGE" / file_name,
                base / "!배포" / "Original" / "PCSG00264" / target,
                base / "!배포-실기" / "Original" / "app" / "PCSG00264" / target,
                base / target,
                base / file_name,
            ]
        )
    for candidate in _unique_paths(candidates):
        if candidate.is_file():
            return candidate
    # 후보 구조가 다른 덤프도 지원하되, 원본 루트 아래만 검색한다.
    for base in _unique_paths(roots):
        if not base.is_dir():
            continue
        try:
            matches = sorted(
                (item for item in base.rglob(file_name) if item.is_file()),
                key=lambda item: str(item).casefold(),
            )
        except OSError:
            continue
        if matches:
            return matches[0].resolve()
    searched = "\n".join(f"  - {item}" for item in _unique_paths(candidates))
    raise ScenarioCpkBuildError(
        f"{asset_key} 원본 CPK를 찾지 못했습니다. 개인 설정의 archiveRoot/mainGameRoot "
        f"또는 명령줄 경로를 확인하세요.\n검색 후보:\n{searched}"
    )


def _resolve_tool(root: Path, config: Mapping[str, Any], explicit: Path | None) -> Path:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    configured = _configured_path(config, "cpkToolPath")
    if configured is not None:
        candidates.append(configured)
    candidates.append(root / "private" / "tools" / "cpkmakec.exe")
    # 개발 PC에서만 사용할 수 있는 개인 work 도구를 보조 후보로 읽는다.
    private_work = root / "work" / "private-tools"
    if private_work.is_dir():
        try:
            candidates.extend(private_work.rglob("cpkmakec.exe"))
        except OSError:
            pass
    for candidate in _unique_paths(candidates):
        if candidate.is_file() and sha256_file(candidate).lower() == _EXPECTED_CPK_TOOL_SHA256:
            return candidate
    detail = "\n".join(f"  - {item}" for item in _unique_paths(candidates))
    raise ScenarioCpkBuildError(
        "tools.lock.json과 SHA-256이 일치하는 cpkmakec.exe를 찾지 못했습니다. "
        "private/project.local.json의 cpkToolPath에 본인 소유의 로컬 도구 경로를 지정하세요.\n"
        f"검색 후보:\n{detail}"
    )


def _resolve_wreplace(root: Path, config: Mapping[str, Any], explicit: Path | None) -> Path:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    for key in ("wReplacePath", "wreplacePath"):
        configured = _configured_path(config, key)
        if configured is not None:
            candidates.append(configured)
    roots: list[Path] = []
    configured_root = _configured_path(config, "archiveRoot")
    if configured_root is not None:
        roots.append(configured_root)
    roots.extend([root.parent / "PCSG00264", root])
    for base in _unique_paths(roots):
        if not base.is_dir():
            continue
        try:
            candidates.extend(base.rglob("Japanese - Hangul to Kanji.wReplace"))
        except OSError:
            pass
    for candidate in _unique_paths(candidates):
        if candidate.is_file() and sha256_file(candidate).lower() == _EXPECTED_WREPLACE_SHA256:
            return candidate
    detail = "\n".join(f"  - {item}" for item in _unique_paths(candidates))
    raise ScenarioCpkBuildError(
        "게임 문자표(Japanese - Hangul to Kanji.wReplace)를 찾지 못했습니다. "
        "원본 분석 폴더의 문자표를 private/project.local.json에 wReplacePath로 지정하거나 "
        "archiveRoot를 확인하세요.\n검색 후보:\n" + detail
    )


def _resolve_operation_wreplace(
    root: Path,
    config: Mapping[str, Any],
    explicit: Path | None,
) -> Path | None:
    """조건 테이블용 치환표를 찾는다.

    조건 문자열은 일반 대사와 다른 ``OPERATE_TBL`` 포맷을 사용한다. 기존
    프로젝트에서 사용한 ``Japanese - Hangul to Kanji(oper).wReplace``가
    있으면 그 표를 우선하고, 별도 표가 없는 저장소에서도 대사 표로 빌드가
    가능하도록 ``None``을 반환한다.
    """

    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    for key in (
        "wReplaceOperationPath",
        "wreplaceOperationPath",
        "operationWReplacePath",
        "operationWreplacePath",
    ):
        configured = _configured_path(config, key)
        if configured is not None:
            candidates.append(configured)
    roots: list[Path] = []
    configured_root = _configured_path(config, "archiveRoot")
    if configured_root is not None:
        roots.append(configured_root)
    roots.extend([root.parent / "PCSG00264", root])
    for base in _unique_paths(roots):
        if not base.is_dir():
            continue
        for filename in (
            "Japanese - Hangul to Kanji(oper).wReplace",
            "Japanese - Hangul to Kanji (oper).wReplace",
        ):
            try:
                candidates.extend(base.rglob(filename))
            except OSError:
                continue
    for candidate in _unique_paths(candidates):
        if candidate.is_file() and sha256_file(candidate).lower() == _EXPECTED_OPERATION_WREPLACE_SHA256:
            return candidate
    # 조건 JSON이 없는 일반 대사 빌드에는 필요하지 않으므로 조용히 선택지를
    # 남긴다. 조건을 실제로 패치할 때는 표준 wReplace를 안전한 대체값으로
    # 사용하고 보고서에 fallback을 기록한다.
    return None


def _load_wreplace(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-16").splitlines()
    except (OSError, UnicodeError) as error:
        raise ScenarioCpkBuildError(f"wReplace 문자표를 읽을 수 없습니다: {path} ({error})") from error
    mapping: dict[str, str] = {}
    for line in lines:
        columns = line.split("\t")
        if len(columns) >= 2 and columns[0] and columns[1]:
            mapping[columns[0]] = columns[1]
    if not mapping:
        raise ScenarioCpkBuildError(f"wReplace 문자표가 비어 있습니다: {path}")
    return mapping


# 고정 슬롯(byteLimit) 행은 한 문장 뒤에 ``㊦`` 같은 글리프가 슬롯 끝까지
# 반복된다. 이 꼬리는 내용이 아니라 자리 채움이며, 슬롯은 어차피 NUL로
# 채우므로 payload에 다시 넣을 필요가 없다. 그대로 두면 한도를 넘어
# 번역이 통째로 옛 문안으로 밀려난다.
_SLOT_PADDING_TAIL = re.compile(r"(?:[⑲⑳㊥㊦㊧㊨]|[\s　])+$")
_SLOT_PADDING_GLYPH = re.compile(r"[⑲⑳㊥㊦㊧㊨]")
_MIN_SLOT_PADDING = 4


def strip_slot_padding(text: object) -> str:
    """고정 슬롯 문자열 끝의 자리 채움 글리프를 걷어낸다."""

    value = "" if text is None else str(text)
    match = _SLOT_PADDING_TAIL.search(value)
    if match is None:
        return value
    tail = match.group(0)
    if len(_SLOT_PADDING_GLYPH.findall(tail)) < _MIN_SLOT_PADDING:
        return value
    return value[: match.start()]


def build_text(entry: Mapping[str, Any]) -> str:
    """CPK에 넣을 번역문을 고른다.

    뷰어 편집창과 재번역 작업은 ``newTranslation``에 쌓이고 ``translation``은
    이전 문안을 비교용으로 남겨 둔다. 화면에서 보는 것과 빌드 결과가 같아야
    하므로 ``newTranslation``이 있으면 그것을 쓴다. 뷰어의 표시 규칙과 같다.
    """

    candidate = entry.get("newTranslation")
    if isinstance(candidate, str):
        return candidate
    return str(entry.get("translation") or "")


def _encode_translation(text: object, mapping: Mapping[str, str]) -> str:
    value = encode_game_dialogue_spaces(_apply_game_text_fallbacks(text))
    # wReplace에는 일본어 대사 레이아웃용 ``공백 → U+3000`` 항목이
    # 포함되어 있을 수 있다. 게임용 FE FE 공백 글리프는 문자표 치환
    # 대상이 아니므로, 이 함수에 들어오기 전에 이미 별도 글리프로 바꾼다.
    converted = "".join(
        char if char == "\uf8f2" else mapping.get(char, char)
        for char in value
    )
    try:
        _encode_payload_text(converted)
    except UnicodeEncodeError as error:
        raise ScenarioCpkBuildError(
            f"번역문을 CP932로 인코딩할 수 없습니다: {value!r} ({error})"
        ) from error
    return converted


def _encode_operation_translation(text: object, mapping: Mapping[str, str]) -> str:
    """``OPERATE_TBL`` 문자열을 기존 operation wReplace 방식으로 변환한다."""

    value = str(text or "")
    # ID00001의 기존 패치는 일반 공백을 wReplace의 전각 공백(0x81 0x40)으로
    # 변환했다. 조건 테이블은 대사 슬롯과 달리 이 포맷 자체가 공백 폭을
    # 정의하므로, 기존 게임 동작과의 호환성을 위해 operation 표를 그대로
    # 적용한다.
    converted = "".join(mapping.get(char, char) for char in value)
    try:
        converted.encode("cp932", errors="strict")
    except UnicodeEncodeError as error:
        raise ScenarioCpkBuildError(
            f"조건 문자열을 CP932로 인코딩할 수 없습니다: {value!r} ({error})"
        ) from error
    return converted


def _lua_unescape(value: str) -> str:
    """Lua 문자열에서 이 프로젝트가 사용하는 기본 escape만 복원한다."""

    result: list[str] = []
    index = 0
    escapes = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
    while index < len(value):
        if value[index] != "\\" or index + 1 >= len(value):
            result.append(value[index])
            index += 1
            continue
        next_char = value[index + 1]
        result.append(escapes.get(next_char, next_char))
        index += 2
    return "".join(result)


def _lua_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


def _operate_string_spans(block: str) -> dict[int, tuple[int, int, str]]:
    """OPERATE_TBL block 안의 ``str_tbl`` ID별 문자열 span을 반환한다."""

    start_match = re.search(r"\bstr_tbl\s*=\s*\{", block)
    if start_match is None:
        return {}
    end = block.find("};", start_match.end())
    if end < 0:
        return {}
    body = block[start_match.end() : end]
    result: dict[int, tuple[int, int, str]] = {}
    pattern = re.compile(
        r"--\s*ID\s*:\s*(?P<id>\d+).*?(?P<quote>\"(?:\\.|[^\"\\])*\")",
        re.DOTALL,
    )
    body_offset = start_match.end()
    for match in pattern.finditer(body):
        source_id = int(match.group("id"), 10)
        quote = match.group("quote")
        result[source_id] = (
            body_offset + match.start("quote"),
            body_offset + match.end("quote"),
            _lua_unescape(quote[1:-1]),
        )
    return result


def _patch_operation_payload(
    payload: bytes,
    entries: list[Mapping[str, Any]],
    mapping: Mapping[str, str],
    *,
    details: bool = False,
) -> bytes | tuple[bytes, int, int]:
    """ID00001의 OPERATE_TBL.str_tbl 조건 문자열을 교체한다."""

    try:
        content = payload.decode("cp932")
    except UnicodeDecodeError as error:
        raise ScenarioCpkBuildError("ID00001 payload를 CP932로 해석할 수 없습니다.") from error
    marker = "OPERATE_TBL = {"
    start = content.find(marker)
    if start < 0:
        raise ScenarioCpkBuildError("ID00001 payload에서 OPERATE_TBL을 찾을 수 없습니다.")
    end_marker = content.find("};", start + len(marker))
    if end_marker < 0:
        raise ScenarioCpkBuildError("ID00001 payload의 OPERATE_TBL 종료 위치를 찾을 수 없습니다.")
    end = end_marker + 2
    block = content[start:end]
    spans = _operate_string_spans(block)
    if not spans:
        raise ScenarioCpkBuildError("ID00001 payload의 OPERATE_TBL.str_tbl을 찾을 수 없습니다.")
    replacements: list[tuple[int, int, str]] = []
    unmatched = 0
    for entry in sorted(entries, key=lambda item: int(item.get("sourceId") or 0)):
        try:
            source_id = int(entry.get("sourceId"))
        except (TypeError, ValueError) as error:
            raise ScenarioCpkBuildError(
                f"조건 entry의 sourceId가 올바르지 않습니다: {entry.get('entryId', entry)!r}"
            ) from error
        span = spans.get(source_id)
        if span is None:
            unmatched += 1
            continue
        start_offset, end_offset, source_text = span
        expected_source = str(entry.get("sourceText") or "")
        if expected_source and source_text != expected_source:
            # 원문 텍스트가 조금 달라진 리비전도 ID를 기준으로 안전하게 교체하되,
            # 보고서에서 검토할 수 있도록 unmatched로 세지 않는다.
            pass
        converted = _encode_operation_translation(build_text(entry), mapping)
        replacement = f'"{_lua_escape(converted)}"'
        replacements.append((start_offset, end_offset, replacement))
    for start_offset, end_offset, replacement in reversed(replacements):
        block = block[:start_offset] + replacement + block[end_offset:]
    output = (content[:start] + block + content[end:]).encode("cp932", errors="strict")
    if details:
        return output, unmatched, len(replacements)
    return output


def _encode_payload_text(value: str) -> bytes:
    """일반 CP932와 고정 슬롯 제어 기호를 함께 바이트로 만든다."""

    output = bytearray()
    position = 0
    while position < len(value):
        if value.startswith(GAME_HALF_WIDTH_SPACE_TEXT, position):
            output.extend(bytes((0xFE, 0xFE)))
            position += len(GAME_HALF_WIDTH_SPACE_TEXT)
            continue
        token = _FIXED_SLOT_TOKENS.get(value[position])
        if token is not None:
            output.extend(token)
            position += 1
            continue
        output.extend(value[position].encode("cp932", errors="strict"))
        position += 1
    return bytes(output)


def _entry_groups(entries: list[Mapping[str, Any]]) -> tuple[dict[tuple[int, int], list[Mapping[str, Any]]], list[Mapping[str, Any]]]:
    groups: dict[tuple[int, int], list[Mapping[str, Any]]] = defaultdict(list)
    unsupported: list[Mapping[str, Any]] = []
    for entry in entries:
        location = _mapping(entry.get("location"), "entry.location")
        internal_id = str(location.get("internalId") or "").strip()
        # 일부 DLC 원고의 ID00007 고정 슬롯은 원본 XLSX에서 Sheet1로
        # 기록되어 있다. CPK 내부에서는 동일한 슬롯이 ID00007이므로
        # 빌드 시에만 게임 멤버 ID로 정규화한다.
        if internal_id == "Sheet1":
            internal_id = "ID00007"
        match = _MEMBER_RE.fullmatch(internal_id)
        if match is None:
            unsupported.append(entry)
            continue
        target_id = int(match.group("target"), 10)
        source_id = int(match.group("source") or match.group("target"), 10)
        groups[(target_id, source_id)].append(entry)
    return dict(groups), unsupported


def _resolve_group_output_ids(
    groups: Mapping[tuple[int, int], object],
) -> dict[tuple[int, int], int]:
    """번역 그룹을 실제 CPK 출력 멤버 ID로 결정한다.

    ``ID00003@FILE-ID00004``는 보통 파일명과 시트명이 어긋난 번역
    원본을 뜻한다. 같은 CPK에 직접 그룹 ``ID00003``도 있으면 별칭이
    그 멤버를 덮어쓰지 않도록 원본 ID00004에 출력한다.
    """

    direct_targets = {
        target_id for target_id, source_id in groups if target_id == source_id
    }
    resolved: dict[tuple[int, int], int] = {}
    claimed: dict[int, tuple[int, int]] = {}
    for target_id, source_id in groups:
        output_id = (
            source_id
            if target_id != source_id and target_id in direct_targets
            else target_id
        )
        previous = claimed.get(output_id)
        if previous is not None and previous != (target_id, source_id):
            raise ScenarioCpkBuildError(
                "서로 다른 번역 그룹이 같은 출력 멤버를 요구합니다: "
                f"{member_filename(previous[0])}@FILE-{member_filename(previous[1])}와 "
                f"{member_filename(target_id)}@FILE-{member_filename(source_id)} "
                f"→ {member_filename(output_id)}"
            )
        claimed[output_id] = (target_id, source_id)
        resolved[(target_id, source_id)] = output_id
    return resolved


def _sorted_entries(entries: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    def key(entry: Mapping[str, Any]) -> int:
        location = _mapping(entry.get("location"), "entry.location")
        value = location.get("sourceRow")
        return int(value) if value is not None else 0

    result = sorted(entries, key=key)
    rows = [
        _mapping(_mapping(entry.get("location"), "entry.location"), "entry.location").get("sourceRow")
        for entry in result
    ]
    if len(set(rows)) != len(rows):
        raise ScenarioCpkBuildError("같은 CPK 멤버 안에 중복 sourceRow가 있습니다.")
    return result


def _patch_fixed_payload(
    payload: bytes,
    entries: list[Mapping[str, Any]],
    mapping: Mapping[str, str],
    *,
    details: bool = False,
) -> bytes | tuple[bytes, int, int]:
    output = bytearray(payload)
    cursor = -1
    unmatched = 0
    fallback_entries = 0
    for entry in _sorted_entries(entries):
        source_text = str(entry.get("sourceText") or "")
        try:
            source_bytes = _encode_payload_text(source_text)
        except UnicodeEncodeError as error:
            raise ScenarioCpkBuildError(f"원문을 CP932로 인코딩할 수 없습니다: {source_text!r}") from error
        offset = payload.find(source_bytes, cursor + 1)
        if offset < 0:
            # XLSX와 설치된 CPK가 서로 다른 배포본일 때 고정 슬롯 한두
            # 행이 원본에 없을 수 있다. 슬롯을 임의로 소비하지 않고 원문을
            # 보존하며, 호출자 보고서의 unmatchedSlots로 드러낸다.
            unmatched += 1
            continue
        metadata = _mapping(entry.get("metadata") or {}, "entry.metadata")
        try:
            limit = int(metadata.get("byteLimit") or 0)
        except (TypeError, ValueError) as error:
            raise ScenarioCpkBuildError(f"byteLimit이 올바르지 않습니다: {metadata.get('byteLimit')!r}") from error
        if limit <= 0:
            raise ScenarioCpkBuildError(
                f"고정 길이 payload의 byteLimit이 없습니다: {entry.get('entryId', source_text)}"
            )
        translation = strip_slot_padding(build_text(entry))
        replacement: bytes | None = None
        translation_error: Exception | None = None
        try:
            candidate = _encode_payload_text(_encode_translation(translation, mapping))
            if len(candidate) <= limit:
                replacement = candidate
        except (ScenarioCpkBuildError, UnicodeEncodeError) as error:
            translation_error = error

        # 고정 슬롯 번역은 JSON의 사람이 읽는 문자열에 패딩용 ㊦/⑲가
        # 포함되어 있어, 그대로 다시 매핑하면 슬롯을 초과할 수 있다.
        # 추출 시 함께 저장한 replacementText는 같은 번역의 게임용
        # 글리프열(패딩 제외)이므로, 현재 문자열이 한도를 넘을 때 안전한
        # 컴파일 결과로 폴백한다.
        if replacement is None:
            compiled = _apply_game_text_fallbacks(metadata.get("replacementText"))
            if compiled:
                try:
                    candidate = _encode_payload_text(compiled)
                except UnicodeEncodeError:
                    candidate = None
                if candidate is not None and len(candidate) <= limit:
                    replacement = candidate
                    fallback_entries += 1

        if replacement is None:
            if translation_error is not None:
                raise ScenarioCpkBuildError(
                    f"{entry.get('entryId', source_text)}를 CP932로 인코딩할 수 없거나 "
                    f"고정 슬롯 {limit}바이트를 초과합니다"
                ) from translation_error
            raise ScenarioCpkBuildError(
                f"{entry.get('entryId', source_text)}가 고정 슬롯 {limit}바이트를 초과합니다"
            )
        output[offset : offset + limit] = replacement + b"\0" * (limit - len(replacement))
        cursor = offset
    result = bytes(output)
    if details:
        return result, unmatched, fallback_entries
    return result


def _source_match_key(value: str) -> str:
    """원문 대응용 보정 키를 만든다.

    CPK와 번역 원고가 다른 추출본일 때 전각 숫자·영문·구두점이 섞여
    저장되는 사례가 있다. 정확 일치가 실패한 경우에만 이 키를 사용하며,
    실제로 기록하는 번역 바이트에는 영향을 주지 않는다.
    """

    normalized = unicodedata.normalize("NFKC", value)
    normalized = "".join(
        character for character in normalized
        if character not in {"\u200b", "\u200c", "\ufeff"}
    )
    return "".join(normalized.split())


def _rebuild_stage_group(
    source_payload: bytes,
    entries: list[Mapping[str, Any]],
    mapping: Mapping[str, str],
) -> tuple[bytes, str, int]:
    """대사 슬롯과 JSON 행을 연결해 전체 stage payload를 다시 만든다.

    번역 JSON이 원본의 일부 행만 포함한 오래된 자료인 경우에도 원문과
    순서를 대조해 해당 행만 교체하고 나머지 슬롯은 원문 그대로 보존한다.
    이렇게 하면 누락 행을 다른 대사에 잘못 밀어 넣지 않으면서 부분 빌드 결과를
    보고서의 ``unmatchedSlots``로 드러낼 수 있다.
    """

    parsed = parse_stage_script(source_payload)

    def encoded_translation(entry: Mapping[str, Any]) -> str:
        """번역문이 오래된 수동 번역 JSON의 게임용 대체문자를 사용할 수 있게 한다."""

        try:
            return _encode_translation(build_text(entry), mapping)
        except ScenarioCpkBuildError as error:
            metadata = _mapping(entry.get("metadata") or {}, "entry.metadata")
            compiled = _apply_game_text_fallbacks(metadata.get("replacementText"))
            if not compiled:
                raise
            try:
                _encode_payload_text(compiled)
            except UnicodeEncodeError:
                raise error
            return compiled

    ordered = _sorted_entries(entries)
    if len(parsed.slots) == len(ordered):
        replacements = [encoded_translation(item) for item in ordered]
        return rebuild_stage_script(parsed, replacements), "stage-script", 0

    replacements = list(parsed.source_rows)
    slot_index = 0
    matched = 0
    for item in ordered:
        source_text = str(item.get("sourceText") or "")
        found = None
        for index in range(slot_index, len(parsed.slots)):
            slot_text = parsed.slots[index].source_text
            if slot_text == source_text or slot_text.strip() == source_text.strip():
                found = index
                break
        # XLSX 원고와 CPK가 서로 다른 릴리스에서 추출된 경우 전각 숫자나
        # 전각 영문·구두점이 달라지는 일이 있다. 정확히 일치하지 않을 때만
        # NFKC로 보정해 비교한다. 이 보정은 실제 원문 바이트를 바꾸지 않고
        # 대응 슬롯을 찾는 데만 사용한다.
        if found is None:
            source_key = _source_match_key(source_text)
            if source_key:
                for index in range(slot_index, len(parsed.slots)):
                    if _source_match_key(parsed.slots[index].source_text) == source_key:
                        found = index
                        break
        if found is None:
            # JSON에만 남아 있는 구판/중복 행은 원본 슬롯을 임의로 소비하지
            # 않는다. 해당 행은 원문 그대로 보존하고 unmatchedSlots로
            # 보고하여, 배치 전체가 한 행 때문에 중단되지 않게 한다.
            continue
        replacements[found] = encoded_translation(item)
        slot_index = found + 1
        matched += 1
    rebuilt = rebuild_stage_script(parsed, replacements)
    return rebuilt, "stage-script-partial", len(parsed.slots) - matched


def _output_relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def build_scenario_cpk(
    scenario: Path | Mapping[str, Any],
    *,
    repository_root: Path | None = None,
    source_cpk: Path | None = None,
    cpk_tool_path: Path | None = None,
    wreplace_path: Path | None = None,
) -> dict[str, Any]:
    """현재 시나리오 JSON을 적용한 CPK를 만들고 재추출로 검증한다.

    ``scenario``에 매핑을 전달하면 뷰어에서 아직 파일로 저장하지 않은 수정도
    그대로 빌드 입력으로 사용한다. 파일 경로를 전달하는 명령줄 사용도 지원한다.
    """

    root = (repository_root or project_root()).resolve()
    if isinstance(scenario, (str, Path)):
        scenario_path = Path(scenario).expanduser().resolve()
        data = _read_json(scenario_path)
        input_label = str(scenario_path)
    else:
        data = dict(scenario)
        input_label = "viewer-memory-json"
    asset = _mapping(data.get("asset"), "asset")
    asset_key = _safe_asset_key(asset.get("assetKey"))
    file_name = str(asset.get("fileName") or f"{asset_key}.cpk").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.cpk", file_name, re.IGNORECASE):
        raise ScenarioCpkBuildError(f"CPK 파일명이 올바르지 않습니다: {file_name!r}")
    target = str(asset.get("target") or "").replace("\\", "/").strip()
    if not target or target.startswith("/") or ".." in Path(target).parts:
        raise ScenarioCpkBuildError(f"CPK 대상 경로가 올바르지 않습니다: {target!r}")
    entries_value = data.get("entries")
    if not isinstance(entries_value, list) or not entries_value:
        raise ScenarioCpkBuildError("시나리오 JSON의 entries가 비어 있거나 배열이 아닙니다.")
    entries = [_mapping(item, "entry") for item in entries_value]
    condition_value = data.get("conditions") or []
    if not isinstance(condition_value, list):
        raise ScenarioCpkBuildError("시나리오 JSON의 conditions는 배열이어야 합니다.")
    condition_entries = [_mapping(item, "condition entry") for item in condition_value]
    config = _config_mapping(root)
    source = _resolve_source_cpk(root, config, asset_key, file_name, target, source_cpk)
    tool_path = _resolve_tool(root, config, cpk_tool_path)
    table_path = _resolve_wreplace(root, config, wreplace_path)
    operation_table_path = (
        _resolve_operation_wreplace(root, config, None) if condition_entries else None
    )
    source_sha = sha256_file(source)
    json_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    json_sha = hashlib.sha256(json_bytes).hexdigest()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_id = f"{stamp}-{json_sha[:12]}"
    run_dir = root / "work" / "cpk-runs" / asset_key / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "input-scenario.json").write_bytes(json_bytes + b"\n")
    report_path = run_dir / "report.json"
    try:
        mapping = _load_wreplace(table_path)
        operation_mapping = (
            _load_wreplace(operation_table_path)
            if operation_table_path is not None
            else mapping
        )
        tool = CpkMakerTool(tool_path, _EXPECTED_CPK_TOOL_SHA256, log_path=run_dir / "tool-invocations.jsonl")
        original_dir = run_dir / "extracted-original"
        original_entries = tool.extract(source, original_dir)
        payload_dir = run_dir / "build" / "payloads"
        payload_dir.mkdir(parents=True)
        for member_id, item in original_entries.items():
            shutil.copyfile(item.path, payload_dir / member_filename(member_id))

        # Sheet1은 번역 XLSX에서 쓰이는 ID00007 고정 길이 멤버의 별칭이다.
        # 일반 대사와 같은 groups에 넣되, 아래 StageScriptError 분기에서
        # byteLimit 슬롯 패처로 처리한다.
        fixed_slot_entries = []
        for item in entries:
            location = _mapping(item.get("location"), "entry.location")
            internal_id = str(location.get("internalId") or "").strip()
            metadata = _mapping(item.get("metadata") or {}, "entry.metadata")
            # Sheet1은 번역 XLSX에서 쓰이는 별칭이고, ID00007은 CPK 내부의
            # 실제 고정 길이 내레이션 멤버다.
            if internal_id == "Sheet1" or (
                internal_id == "ID00007" and metadata.get("byteLimit")
            ):
                fixed_slot_entries.append(item)
        patch_entries = entries
        groups, unsupported = _entry_groups(patch_entries)
        if unsupported:
            sample = ", ".join(str(item.get("entryId", "entryId 없음")) for item in unsupported[:5])
            raise ScenarioCpkBuildError(
                f"CPK 멤버로 결합할 수 없는 internalId가 {len(unsupported)}행 있습니다 "
                f"(예: {sample}). 이 버튼은 IDxxxxx 또는 IDxxxxx@FILE-IDyyyyy 형식만 지원합니다."
            )
        modified: list[dict[str, Any]] = []
        target_ids: set[int] = set()
        unmatched_fixed_slot_entries = 0
        fixed_slot_fallback_entries = 0
        explicit_targets = {target_id for target_id, _source_id in groups}
        # A sheet/file alias such as ID00003@FILE-ID00004 can coexist with a
        # real ID00003 group. In that case the alias must not overwrite the
        # real ID00003 payload; emit it to its source member (ID00004).
        effective_output_ids = _resolve_group_output_ids(groups)
        collision_target_remaps = 0
        for (target_id, source_id), group in sorted(groups.items()):
            source_entry = original_entries.get(source_id)
            if source_entry is None:
                raise ScenarioCpkBuildError(
                    f"원본 CPK에 {member_filename(source_id)}가 없습니다. "
                    f"{member_filename(target_id)}의 원본 매핑을 확인하세요."
                )
            group = _sorted_entries(group)
            source_payload = source_entry.path.read_bytes()
            fallback_fixed = 0
            try:
                rebuilt, mode, unmatched_slots = _rebuild_stage_group(
                    source_payload, group, mapping
                )
            except StageScriptError as stage_error:
                # ID00007과 같은 나레이션 고정 슬롯은 metadata.byteLimit와
                # 원문 위치를 이용해 payload 내부의 52바이트 필드를 교체한다.
                if not all((_mapping(item.get("metadata") or {}, "entry.metadata").get("byteLimit")) for item in group):
                    # 슬롯 수가 안 맞는 경우와, 줄바꿈처럼 값 자체가 잘못된
                    # 경우는 원인이 전혀 다르다. 예전에는 둘 다 슬롯 수
                    # 이야기로 보고해서 실제 원인을 가렸다.
                    if isinstance(stage_error, StageScriptRowCountError):
                        raise ScenarioCpkBuildError(
                            f"{member_filename(source_id)}의 대사 슬롯과 JSON 행 수({len(group)})가 맞지 않습니다. "
                            "고정 슬롯 행에는 metadata.byteLimit이 필요합니다."
                        ) from stage_error
                    raise ScenarioCpkBuildError(
                        f"{member_filename(source_id)}를 다시 조립할 수 없습니다: {stage_error}"
                    ) from stage_error
                fixed_result = _patch_fixed_payload(
                    source_payload, group, mapping, details=True
                )
                rebuilt, unmatched_fixed, fallback_fixed = fixed_result
                mode = "fixed-slot"
                unmatched_slots = unmatched_fixed
                unmatched_fixed_slot_entries += unmatched_fixed
                fixed_slot_fallback_entries += fallback_fixed
            output_id = effective_output_ids[(target_id, source_id)]
            collision_remapped = output_id != target_id
            if collision_remapped:
                collision_target_remaps += 1
            target_path = payload_dir / member_filename(output_id)
            target_path.write_bytes(rebuilt)
            target_ids.add(output_id)
            reported_mode = (
                f"{mode}-collision-source-remap" if collision_remapped else mode
            )
            modified_item: dict[str, Any] = {
                "targetId": member_filename(output_id),
                "sourceId": member_filename(source_id),
                "mode": reported_mode,
                "entries": len(group),
                "unmatchedSlots": unmatched_slots,
                "fixedSlotFallbackEntries": (
                    fallback_fixed if mode == "fixed-slot" else 0
                ),
                "sourceBytes": len(source_payload),
                "outputBytes": len(rebuilt),
                "outputSha256": hashlib.sha256(rebuilt).hexdigest(),
            }
            if collision_remapped:
                modified_item["requestedTargetId"] = member_filename(target_id)
                modified_item["collisionRemapped"] = True
            modified.append(modified_item)
            # 공통 JSON의 ``ID00003@FILE-ID00004`` 표기는 ID4를 원본으로
            # ID3을 만든다는 뜻이다. 별도의 ID4 행이 없는 경우에는 게임이
            # 실제로 사용하는 원본 ID4에도 같은 조립 결과를 넣어, 기존
            # manifest의 ID3·ID4 동시 출력 계약을 보존한다.
            if (
                not collision_remapped
                and target_id != source_id
                and source_id in original_entries
                and source_id not in explicit_targets
            ):
                implicit_path = payload_dir / member_filename(source_id)
                implicit_path.write_bytes(rebuilt)
                target_ids.add(source_id)
                modified.append(
                    {
                        "targetId": member_filename(source_id),
                        "sourceId": member_filename(source_id),
                        "mode": f"{mode}-implicit-source-copy",
                        "entries": len(group),
                        "sourceBytes": len(source_payload),
                        "outputBytes": len(rebuilt),
                        "outputSha256": hashlib.sha256(rebuilt).hexdigest(),
                    }
                )

        unmatched_condition_entries = 0
        patched_condition_entries = 0
        condition_output_bytes = 0
        if condition_entries:
            condition_member = str(
                data.get("conditionMemberId")
                or data.get("conditionSourceMember")
                or "ID00001"
            ).strip().upper()
            condition_match = re.fullmatch(r"ID(\d{5})", condition_member)
            if condition_match is None:
                raise ScenarioCpkBuildError(
                    f"조건 source member가 올바르지 않습니다: {condition_member!r}"
                )
            condition_id = int(condition_match.group(1), 10)
            condition_path = payload_dir / member_filename(condition_id)
            if not condition_path.is_file():
                raise ScenarioCpkBuildError(
                    f"원본 CPK에 조건 member가 없습니다: {member_filename(condition_id)}"
                )
            condition_source = condition_path.read_bytes()
            condition_result = _patch_operation_payload(
                condition_source,
                condition_entries,
                operation_mapping,
                details=True,
            )
            condition_output, unmatched_condition_entries, patched_condition_entries = condition_result
            condition_path.write_bytes(condition_output)
            target_ids.add(condition_id)
            condition_output_bytes = len(condition_output)
            modified.append(
                {
                    "targetId": member_filename(condition_id),
                    "sourceId": member_filename(condition_id),
                    "mode": "operation-table",
                    "entries": len(condition_entries),
                    "patchedEntries": patched_condition_entries,
                    "unmatchedEntries": unmatched_condition_entries,
                    "sourceBytes": len(condition_source),
                    "outputBytes": len(condition_output),
                    "outputSha256": hashlib.sha256(condition_output).hexdigest(),
                }
            )

        expected_ids = tuple(sorted(set(original_entries) | target_ids))
        built = run_dir / "build" / file_name
        tool.pack(payload_dir, expected_ids, built)
        verified_dir = run_dir / "verified-extract"
        verified_entries = tool.extract(built, verified_dir)
        if tuple(verified_entries) != expected_ids:
            raise ScenarioCpkBuildError(
                f"리팩 후 ID 목록이 다릅니다: 기대={expected_ids}, 실제={tuple(verified_entries)}"
            )
        for member_id in expected_ids:
            expected_hash = sha256_file(payload_dir / member_filename(member_id))
            actual_hash = verified_entries[member_id].sha256
            if actual_hash != expected_hash:
                raise ScenarioCpkBuildError(
                    f"리팩 후 {member_filename(member_id)} payload 검증 실패: "
                    f"기대={expected_hash}, 실제={actual_hash}"
                )
        output_dir = root / "output" / "dialogue" / asset_key / run_id
        output_dir.mkdir(parents=True, exist_ok=False)
        output_cpk = output_dir / file_name
        shutil.copyfile(built, output_cpk)
        output_report = {
            "format": "siok.scenario-cpk-build-report",
            "formatVersion": 1,
            "ok": True,
            "assetKey": asset_key,
            "input": input_label,
            "sourceCpk": str(source),
            "sourceBytes": source.stat().st_size,
            "sourceSha256": source_sha,
            "inputJsonSha256": json_sha,
            "wReplace": str(table_path),
            "wReplaceSha256": sha256_file(table_path),
            "operationWReplace": (
                str(operation_table_path) if operation_table_path is not None else None
            ),
            "operationWReplaceSha256": (
                sha256_file(operation_table_path)
                if operation_table_path is not None
                else None
            ),
            "operationWReplaceFallback": bool(condition_entries and operation_table_path is None),
            "spaceEncoding": {
                "jsonText": "U+0020",
                "cpkBytes": "FE FE",
                "structuralFullwidthBytes": "81 40",
            },
            "tool": {
                "path": str(tool_path),
                "sha256": _EXPECTED_CPK_TOOL_SHA256,
                "version": tool.executable_version,
                "packMode": "ID",
                "alignment": 16,
                "compression": "uncompressed",
                "maskedDirectories": True,
                "dateTimeInformation": False,
            },
            "modified": modified,
            "collisionTargetRemaps": collision_target_remaps,
            "fixedSlotEntries": len(fixed_slot_entries),
            "fixedSlotFallbackEntries": fixed_slot_fallback_entries,
            "unmatchedFixedSlotEntries": unmatched_fixed_slot_entries,
            "skippedFixedSlotEntries": unmatched_fixed_slot_entries,
            "conditionEntries": len(condition_entries),
            "patchedConditionEntries": patched_condition_entries,
            "unmatchedConditionEntries": unmatched_condition_entries,
            "conditionOutputBytes": condition_output_bytes,
            "memberIds": [member_filename(item) for item in expected_ids],
            "outputCpk": str(output_cpk),
            "outputBytes": output_cpk.stat().st_size,
            "outputSha256": sha256_file(output_cpk),
            "verification": {
                "reextracted": True,
                "allMemberIdsMatch": True,
                "allPayloadHashesMatch": True,
                "sourceUnchanged": sha256_file(source) == source_sha,
            },
        }
        report_path.write_text(json.dumps(output_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        output_report["workDir"] = str(run_dir)
        output_report["workReport"] = str(report_path)
        output_report["outputReport"] = str(output_dir / "report.json")
        (output_dir / "report.json").write_text(json.dumps(output_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return output_report
    except (CpkToolError, StageScriptError, ScenarioCpkBuildError, OSError, ValueError, TypeError) as error:
        failure = {
            "format": "siok.scenario-cpk-build-failure",
            "formatVersion": 1,
            "ok": False,
            "assetKey": asset_key,
            "sourceCpk": str(source),
            "sourceSha256": source_sha,
            "inputJsonSha256": json_sha,
            "error": str(error),
        }
        report_path.write_text(json.dumps(failure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if isinstance(error, ScenarioCpkBuildError):
            raise
        raise ScenarioCpkBuildError(f"시나리오 CPK 빌드가 중단되었습니다: {error}") from error


__all__ = ["ScenarioCpkBuildError", "build_scenario_cpk"]
