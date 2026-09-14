#!/usr/bin/env python3
"""자기 소유 게임 덤프에 본편·DLC 통합 한글 패치를 적용한다.

이 실행기는 저장소의 공개 델타와 SRVC JSON을 읽고, 원본 파일의 크기·SHA-256을
먼저 확인한 뒤 ``output/`` 또는 ``work/`` 아래에만 별도 결과를 만든다. 게임 설치
폴더를 수정하거나 원본 파일을 백업·삭제하지 않는다. 실기 Vita 프로필은 서명과
패키지 검증 절차가 남아 있어 의도적으로 거부한다.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
from typing import Any, Iterable, Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPOSITORY_ROOT / "config" / "release-patch.json"
DEFAULT_XDELTA = REPOSITORY_ROOT / "private" / "tools" / "xdelta3.exe"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_OUTPUT_ROOTS = (REPOSITORY_ROOT / "output", REPOSITORY_ROOT / "work")


class ReleasePatchError(RuntimeError):
    """통합 패치 입력·도구·출력 검증에 실패했을 때 발생한다."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReleasePatchError(f"JSON을 읽을 수 없습니다: {path}") from error
    if not isinstance(value, dict):
        raise ReleasePatchError(f"JSON 최상위 값은 객체여야 합니다: {path}")
    return value


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ReleasePatchError(f"{label}은(는) 객체여야 합니다.")
    return value


def _string(value: Mapping[str, Any], key: str, label: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ReleasePatchError(f"{label}.{key}은(는) 비어 있지 않은 문자열이어야 합니다.")
    return item


def _bool(value: Mapping[str, Any], key: str, label: str, expected: bool) -> None:
    if value.get(key) is not expected:
        raise ReleasePatchError(f"{label}.{key}이(가) {expected!r}이어야 합니다.")


def _record(value: object, label: str) -> tuple[int, str]:
    item = _mapping(value, label)
    size = item.get("bytes")
    digest = item.get("sha256")
    if type(size) is not int or size < 1:
        raise ReleasePatchError(f"{label}.bytes가 올바른 양의 정수가 아닙니다.")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest.lower()) is None:
        raise ReleasePatchError(f"{label}.sha256가 올바른 SHA-256이 아닙니다.")
    return size, digest.lower()


def _verify_file(path: Path, record: object, label: str, *, reject_symlink: bool = True) -> tuple[int, str]:
    expected_size, expected_hash = _record(record, label)
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as error:
        raise ReleasePatchError(f"{label} 파일을 찾을 수 없습니다: {path}") from error
    if reject_symlink and path.is_symlink():
        raise ReleasePatchError(f"{label}에 심볼릭 링크를 사용할 수 없습니다: {path}")
    if not resolved.is_file():
        raise ReleasePatchError(f"{label}이(가) 일반 파일이 아닙니다: {resolved}")
    actual_size = resolved.stat().st_size
    if actual_size != expected_size:
        raise ReleasePatchError(
            f"{label} 크기가 다릅니다: 예상 {expected_size}, 실제 {actual_size} ({resolved})"
        )
    actual_hash = sha256_file(resolved)
    if actual_hash != expected_hash:
        raise ReleasePatchError(
            f"{label} SHA-256이 다릅니다: 예상 {expected_hash}, 실제 {actual_hash} ({resolved})"
        )
    return actual_size, actual_hash


def _safe_relative(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReleasePatchError(f"{label}은(는) 비어 있지 않은 상대 경로여야 합니다.")
    if "\\" in value or ":" in value:
        raise ReleasePatchError(f"{label}에 Windows 절대 경로 또는 역슬래시를 사용할 수 없습니다: {value}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ReleasePatchError(f"{label}에 안전하지 않은 상대 경로가 있습니다: {value}")
    return path.as_posix()


def _safe_repository_file(relative_path: object, label: str) -> Path:
    relative = _safe_relative(relative_path, label)
    raw = REPOSITORY_ROOT / Path(*PurePosixPath(relative).parts)
    if raw.is_symlink():
        raise ReleasePatchError(f"{label}에 심볼릭 링크를 사용할 수 없습니다: {raw}")
    candidate = raw.resolve(strict=True)
    if not candidate.is_relative_to(REPOSITORY_ROOT):
        raise ReleasePatchError(f"{label}이 저장소 밖을 가리킵니다: {candidate}")
    if not candidate.is_file() or candidate.is_symlink():
        raise ReleasePatchError(f"{label}이 일반 파일이 아닙니다: {candidate}")
    return candidate


def _safe_input_root(path: Path, label: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as error:
        raise ReleasePatchError(f"{label}을(를) 찾을 수 없습니다: {path}") from error
    if not resolved.is_dir():
        raise ReleasePatchError(f"{label}이(가) 폴더가 아닙니다: {resolved}")
    if path.is_symlink():
        raise ReleasePatchError(f"{label} 자체에 심볼릭 링크를 사용할 수 없습니다: {path}")
    return resolved


def _safe_output_root(path: Path) -> Path:
    candidate = path.expanduser().resolve(strict=False)
    if not any(candidate.is_relative_to(root) for root in _ALLOWED_OUTPUT_ROOTS):
        roots = ", ".join(str(root) for root in _ALLOWED_OUTPUT_ROOTS)
        raise ReleasePatchError(f"출력 폴더는 저장소의 output/ 또는 work/ 아래여야 합니다: {roots}")
    if candidate == REPOSITORY_ROOT:
        raise ReleasePatchError("저장소 루트 자체를 출력 폴더로 사용할 수 없습니다.")
    return candidate


def _target_path(root: Path, relative: str) -> Path:
    candidate = (root / Path(*PurePosixPath(relative).parts)).resolve(strict=False)
    if not candidate.is_relative_to(root):
        raise ReleasePatchError(f"대상 경로가 입력 루트 밖으로 나갑니다: {relative}")
    return candidate


def _load_manifest(path: Path) -> dict[str, Any]:
    raw = path.expanduser()
    if raw.is_symlink():
        raise ReleasePatchError(f"패치 매니페스트에 심볼릭 링크를 사용할 수 없습니다: {raw}")
    manifest_path = raw.resolve(strict=True)
    if not manifest_path.is_file():
        raise ReleasePatchError(f"패치 매니페스트가 일반 파일이 아닙니다: {manifest_path}")
    if not manifest_path.is_relative_to(REPOSITORY_ROOT):
        raise ReleasePatchError(f"패치 매니페스트는 저장소 안에 있어야 합니다: {manifest_path}")
    manifest = _read_json(manifest_path)
    if manifest.get("format") != "siok-release-patch-plan" or manifest.get("formatVersion") != 1:
        raise ReleasePatchError("지원하지 않는 통합 패치 매니페스트 형식입니다.")
    if manifest.get("project") != "PCSG00264":
        raise ReleasePatchError("PCSG00264용 매니페스트가 아닙니다.")
    policy = _mapping(manifest.get("policy"), "manifest.policy")
    _bool(policy, "requiresUserOwnedGame", "manifest.policy", True)
    _bool(policy, "gameBinaryIncluded", "manifest.policy", False)
    _bool(policy, "completedGameIncluded", "manifest.policy", False)
    _bool(policy, "deltaOnly", "manifest.policy", True)
    profiles = _mapping(manifest.get("profiles"), "manifest.profiles")
    vita3k = _mapping(profiles.get("vita3k"), "manifest.profiles.vita3k")
    _bool(vita3k, "enabled", "manifest.profiles.vita3k", True)
    vita = _mapping(profiles.get("vita"), "manifest.profiles.vita")
    _bool(vita, "enabled", "manifest.profiles.vita", False)
    tool = _mapping(manifest.get("tool"), "manifest.tool")
    if tool.get("name") != "xdelta3" or tool.get("version") != "3.2.0":
        raise ReleasePatchError("매니페스트의 xdelta3 도구 버전이 지원 대상이 아닙니다.")
    for key in ("assetSha256", "executableSha256"):
        value = tool.get(key)
        if not isinstance(value, str) or _SHA256.fullmatch(value.lower()) is None:
            raise ReleasePatchError(f"manifest.tool.{key}가 올바른 SHA-256이 아닙니다.")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ReleasePatchError("매니페스트 entries가 비어 있거나 배열이 아닙니다.")
    coverage = _mapping(manifest.get("coverage"), "manifest.coverage")
    if coverage.get("entryCount") != len(entries):
        raise ReleasePatchError("manifest.coverage.entryCount가 entries 개수와 다릅니다.")
    group_counts = coverage.get("groupCounts")
    if not isinstance(group_counts, dict):
        raise ReleasePatchError("manifest.coverage.groupCounts가 객체가 아닙니다.")
    counted: dict[str, int] = {}
    for index, raw in enumerate(entries):
        entry = _mapping(raw, f"manifest.entries[{index}]")
        group = entry.get("group")
        if not isinstance(group, str) or not group:
            raise ReleasePatchError(f"manifest.entries[{index}].group가 비어 있습니다.")
        counted[group] = counted.get(group, 0) + 1
    declared: dict[str, int] = {}
    for key, value in group_counts.items():
        if not isinstance(key, str) or type(value) is not int or value < 0:
            raise ReleasePatchError("manifest.coverage.groupCounts의 값이 올바르지 않습니다.")
        declared[key] = value
    if counted != declared:
        raise ReleasePatchError("manifest.coverage.groupCounts가 entries와 다릅니다.")
    return manifest


def _selected_entries(manifest: Mapping[str, Any], include: str) -> list[dict[str, Any]]:
    entries = manifest["entries"]
    selected: list[dict[str, Any]] = []
    for index, raw in enumerate(entries):
        entry = _mapping(raw, f"manifest.entries[{index}]")
        profile = entry.get("profile")
        if profile != "vita3k":
            raise ReleasePatchError(f"manifest.entries[{index}]에 지원하지 않는 프로필이 있습니다: {profile}")
        root = entry.get("root")
        if root not in ("app", "dlc"):
            raise ReleasePatchError(f"manifest.entries[{index}].root가 app/dlc가 아닙니다.")
        if include == "main" and root != "app":
            continue
        if include == "dlc" and root != "dlc":
            continue
        selected.append(dict(entry))
    if not selected:
        raise ReleasePatchError(f"선택한 범위에 적용할 패치가 없습니다: {include}")
    return selected


def _verify_tool(path: Path, manifest: Mapping[str, Any]) -> Path:
    tool = _mapping(manifest["tool"], "manifest.tool")
    expected = str(tool["executableSha256"]).lower()
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as error:
        raise ReleasePatchError(
            f"xdelta3 실행 파일을 찾을 수 없습니다: {path}\n"
            "먼저 python scripts/prepare_xdelta3.py --output private/tools/xdelta3.exe 를 실행하세요."
        ) from error
    if path.is_symlink() or not resolved.is_file():
        raise ReleasePatchError(f"xdelta3 경로가 일반 파일이 아닙니다: {resolved}")
    actual = sha256_file(resolved)
    if actual != expected:
        raise ReleasePatchError(
            f"xdelta3 실행 파일 SHA-256이 잠금값과 다릅니다: 예상 {expected}, 실제 {actual}"
        )
    return resolved


def _check_output_conflict(path: Path, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise ReleasePatchError(f"출력 파일이 이미 있습니다. 다른 --output을 사용하거나 --overwrite를 지정하세요: {path}")


def _atomic_decode(
    *,
    xdelta: Path,
    source: Path,
    patch: Path,
    destination: Path,
    expected: object,
    overwrite: bool,
) -> tuple[int, str]:
    _check_output_conflict(destination, overwrite)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
        command = [str(xdelta), "-d", "-f", "-s", str(source), str(patch), str(temporary)]
        result = subprocess.run(command, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise ReleasePatchError(f"xdelta3 적용에 실패했습니다 ({result.returncode}): {detail}")
        actual_size, actual_hash = _verify_file(temporary, expected, f"생성 결과 {destination}", reject_symlink=False)
        os.replace(temporary, destination)
        temporary = None
        return actual_size, actual_hash
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _run_battle_json(
    *,
    entry: Mapping[str, Any],
    source: Path,
    destination: Path,
    report_path: Path,
    overwrite: bool,
) -> dict[str, Any]:
    patch_path = _safe_repository_file(entry.get("patch"), "SRVC JSON")
    _verify_file(source, entry["source"], "SRVC 원본")
    _check_output_conflict(destination, overwrite)
    _check_output_conflict(report_path, overwrite)
    destination.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(REPOSITORY_ROOT / "scripts" / "apply_battle_dialogue_patch.py"),
        "--original",
        str(source),
        "--json",
        str(patch_path),
        "--output",
        str(destination),
        "--report",
        str(report_path),
    ]
    if overwrite:
        command.append("--overwrite")
    result = subprocess.run(command, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ReleasePatchError(f"SRVC JSON 패치에 실패했습니다 ({result.returncode}): {detail}")
    try:
        report = _read_json(report_path)
    except ReleasePatchError:
        raise ReleasePatchError("SRVC JSON 실행기가 검증 보고서를 만들지 못했습니다.")
    output = _mapping(report.get("output"), "SRVC 보고서 output")
    expected_size, expected_hash = _record(entry["localized"], "manifest SRVC localized")
    if output.get("bytes") != expected_size or str(output.get("sha256", "")).lower() != expected_hash:
        raise ReleasePatchError("SRVC JSON 결과의 크기·SHA-256이 매니페스트와 다릅니다.")
    return {
        "kind": "battle-json",
        "target": entry["target"],
        "output": destination.relative_to(REPOSITORY_ROOT).as_posix(),
        "report": report_path.relative_to(REPOSITORY_ROOT).as_posix(),
        "bytes": expected_size,
        "sha256": expected_hash,
    }


def _run_dictionary_json(
    *,
    entry: Mapping[str, Any],
    source: Path,
    destination: Path,
    report_path: Path,
    overwrite: bool,
) -> dict[str, Any]:
    patch_path = _safe_repository_file(entry.get("patch"), "사전 대사 JSON")
    _check_output_conflict(destination, overwrite)
    _check_output_conflict(report_path, overwrite)
    destination.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(REPOSITORY_ROOT / "scripts" / "apply_dictionary_patch.py"),
        "--original",
        str(source),
        "--json",
        str(patch_path),
        "--output",
        str(destination),
        "--report",
        str(report_path),
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ReleasePatchError(f"사전 대사 JSON 패치에 실패했습니다 ({result.returncode}): {detail}")
    report = _read_json(report_path)
    expected_size, expected_hash = _record(entry["localized"], "매니페스트 사전 localized")
    if report.get("appliedBytes") != expected_size or str(report.get("appliedSha256", "")).lower() != expected_hash:
        raise ReleasePatchError("사전 JSON 결과의 크기·SHA-256이 매니페스트와 다릅니다.")
    return {
        "kind": "dictionary-json",
        "target": entry["target"],
        "output": destination.relative_to(REPOSITORY_ROOT).as_posix(),
        "report": report_path.relative_to(REPOSITORY_ROOT).as_posix(),
        "bytes": expected_size,
        "sha256": expected_hash,
    }


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def apply_release(
    *,
    game_root: Path,
    dlc_root: Path | None,
    xdelta_path: Path,
    manifest_path: Path = DEFAULT_MANIFEST,
    profile: str = "vita3k",
    include: str = "all",
    output_root: Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    if profile != "vita3k":
        raise ReleasePatchError("현재 공개 실행기는 Vita3K 프로필만 지원합니다. 실기 Vita는 서명 검증 후 별도 추가됩니다.")
    if include not in ("all", "main", "dlc"):
        raise ReleasePatchError(f"지원하지 않는 적용 범위입니다: {include}")
    manifest = _load_manifest(manifest_path)
    selected = _selected_entries(manifest, include)
    app_root = _safe_input_root(game_root, "본편 원본 폴더")
    addcont_root = _safe_input_root(dlc_root, "DLC 원본 폴더") if dlc_root is not None else None
    if any(entry["root"] == "dlc" for entry in selected) and addcont_root is None:
        raise ReleasePatchError("DLC 패치를 포함하려면 --dlc-root를 지정해야 합니다.")
    xdelta = _verify_tool(xdelta_path, manifest)
    if output_root is None:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output_root = REPOSITORY_ROOT / "output" / "release-candidate" / profile / run_id
    destination_root = _safe_output_root(output_root)
    if destination_root.exists() and any(destination_root.iterdir()) and not overwrite:
        raise ReleasePatchError(f"출력 폴더가 비어 있지 않습니다: {destination_root}")

    # 원본과 공개 델타는 쓰기 전에 모두 검사한다. 이 단계에서 실패하면 출력 파일이 생기지 않는다.
    preflight: list[tuple[dict[str, Any], Path, Path | None]] = []
    for index, entry in enumerate(selected):
        kind = entry.get("kind")
        if kind not in ("xdelta", "battle-json", "dictionary-json"):
            raise ReleasePatchError(f"manifest.entries[{index}].kind가 지원 대상이 아닙니다: {kind}")
        group = entry.get("group")
        if not isinstance(group, str) or not group:
            raise ReleasePatchError(f"manifest.entries[{index}].group가 비어 있습니다.")
        target = _safe_relative(entry.get("target"), f"manifest.entries[{index}].target")
        root = entry["root"]
        if kind == "battle-json" and (root != "app" or target != "DATA/BTLC/SRVC.BIN"):
            raise ReleasePatchError("battle-json 항목은 app/DATA/BTLC/SRVC.BIN에만 사용할 수 있습니다.")
        if kind == "dictionary-json" and (root != "app" or target != "CommonData/MtData/MtZkn_KW.cpk"):
            raise ReleasePatchError("dictionary-json 항목은 app/CommonData/MtData/MtZkn_KW.cpk에만 사용할 수 있습니다.")
        source_root = app_root if root == "app" else addcont_root
        if source_root is None:  # 방어적 분기
            raise ReleasePatchError(f"DLC 입력 폴더가 없습니다: {target}")
        source = _target_path(source_root, target)
        _verify_file(source, entry.get("source"), f"원본 {target}")
        destination_base = destination_root / ("app" if root == "app" else "addcont")
        destination = _target_path(destination_base, target)
        _record(entry.get("localized"), f"매니페스트 결과 {target}")
        if kind == "xdelta":
            patch = _safe_repository_file(entry.get("patch"), f"델타 {target}")
            _verify_file(patch, entry.get("delta"), f"델타 {target}")
            _check_output_conflict(destination, overwrite)
            preflight.append((entry, source, patch))
        elif kind == "battle-json":
            patch = _safe_repository_file(entry.get("patch"), "SRVC JSON")
            destination_report = destination.with_name(destination.name + ".report.json")
            _check_output_conflict(destination, overwrite)
            _check_output_conflict(destination_report, overwrite)
            # JSON 자체의 포맷·원본/적용 해시는 전용 실행기가 다시 검사한다.
            preflight.append((entry, source, patch))
        elif kind == "dictionary-json":
            patch = _safe_repository_file(entry.get("patch"), "사전 대사 JSON")
            destination_report = destination.with_name(destination.name + ".report.json")
            _check_output_conflict(destination, overwrite)
            _check_output_conflict(destination_report, overwrite)
            preflight.append((entry, source, patch))
        else:
            raise ReleasePatchError(f"지원하지 않는 패치 종류입니다: {kind}")

    results: list[dict[str, Any]] = []
    group_counts: dict[str, int] = {}
    for entry, source, patch in preflight:
        root = entry["root"]
        target = _safe_relative(entry["target"], "entry.target")
        destination = _target_path(destination_root / ("app" if root == "app" else "addcont"), target)
        group = str(entry["group"])
        group_counts[group] = group_counts.get(group, 0) + 1
        if entry["kind"] == "xdelta":
            bytes_written, digest = _atomic_decode(
                xdelta=xdelta,
                source=source,
                patch=patch,
                destination=destination,
                expected=entry["localized"],
                overwrite=overwrite,
            )
            results.append(
                {
                    "kind": "xdelta",
                    "root": root,
                    "group": group,
                    "target": target,
                    "patch": entry["patch"],
                    "output": destination.relative_to(REPOSITORY_ROOT).as_posix(),
                    "bytes": bytes_written,
                    "sha256": digest,
                }
            )
        elif entry["kind"] == "battle-json":
            report_path = destination.with_name(destination.name + ".report.json")
            results.append(
                {
                    **_run_battle_json(
                        entry=entry,
                        source=source,
                        destination=destination,
                        report_path=report_path,
                        overwrite=overwrite,
                    ),
                    "root": root,
                    "group": group,
                    "patch": entry["patch"],
                }
            )
        else:
            report_path = destination.with_name(destination.name + ".report.json")
            results.append(
                {
                    **_run_dictionary_json(
                        entry=entry,
                        source=source,
                        destination=destination,
                        report_path=report_path,
                        overwrite=overwrite,
                    ),
                    "root": root,
                    "group": group,
                    "patch": entry["patch"],
                }
            )

    expected_count = len(selected)
    report: dict[str, Any] = {
        "format": "siok.release-patch-report",
        "formatVersion": 1,
        "project": "PCSG00264",
        "profile": profile,
        "include": include,
        "requiresUserOwnedGame": True,
        "inputs": {
            "appRoot": str(app_root),
            "dlcRoot": str(addcont_root) if addcont_root is not None else None,
            "originalsWereNotModified": True,
        },
        "tool": {
            "path": str(xdelta),
            "sha256": sha256_file(xdelta),
        },
        "outputRoot": destination_root.relative_to(REPOSITORY_ROOT).as_posix(),
        "coverage": {
            "entryCount": expected_count,
            "groupCounts": group_counts,
            "verifiedOutputs": len(results),
        },
        "entries": results,
    }
    _write_json(destination_root / "release-report.json", report)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-root", type=Path, required=True, help="자기 소유 본편 PCSG00264 원본 폴더")
    parser.add_argument("--dlc-root", type=Path, help="자기 소유 addcont/PCSG00264 원본 폴더")
    parser.add_argument("--xdelta", type=Path, default=DEFAULT_XDELTA, help="잠금 해시를 검증할 공식 xdelta3.exe")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="공개 패치 매니페스트 JSON")
    parser.add_argument("--profile", choices=("vita3k", "vita"), default="vita3k")
    parser.add_argument("--include", choices=("all", "main", "dlc"), default="all", help="본편·DLC 적용 범위")
    parser.add_argument("--output", type=Path, help="output/ 또는 work/ 아래의 별도 결과 폴더")
    parser.add_argument("--overwrite", action="store_true", help="지정한 출력 폴더의 기존 파일을 교체")
    args = parser.parse_args(argv)
    try:
        report = apply_release(
            game_root=args.game_root,
            dlc_root=args.dlc_root,
            xdelta_path=args.xdelta,
            manifest_path=args.manifest,
            profile=args.profile,
            include=args.include,
            output_root=args.output,
            overwrite=args.overwrite,
        )
    except (ReleasePatchError, OSError) as error:
        parser.error(str(error))
    print(
        json.dumps(
            {
                "outputRoot": report["outputRoot"],
                "entries": report["coverage"]["entryCount"],
                "groups": report["coverage"]["groupCounts"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
