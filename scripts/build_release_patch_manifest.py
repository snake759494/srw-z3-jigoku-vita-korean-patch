#!/usr/bin/env python3
"""기존 검증 산출물에서 공개 릴리스 패치 매니페스트를 만든다.

원본/완성 게임 파일은 읽기만 하며, 저장소에는 델타 파일과 해시·경로 메타데이터만
기록한다. 이 스크립트는 유지보수자가 새 번역 산출물을 릴리스 묶음으로 갱신할 때
사용한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SRVC_JSON = REPOSITORY_ROOT / "translations" / "dialogue" / "battle_SRVC.json"
DICTIONARY_JSON = REPOSITORY_ROOT / "translations" / "dialogue" / "dictionary_MtZkn_KW.json"
FORMAT = "siok-release-patch-plan"
FORMAT_VERSION = 1


class ManifestBuildError(RuntimeError):
    """릴리스 매니페스트 입력이 일관되지 않을 때 발생한다."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _file_record(path: Path) -> dict[str, Any]:
    return {
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _require_file(path: Path, label: str) -> Path:
    if not path.is_file():
        raise ManifestBuildError(f"{label}을(를) 찾을 수 없습니다: {path}")
    return path


def _main_target_for_patch(name: str) -> tuple[str, str] | None:
    if name == "eboot_bin.xdelta":
        return "eboot.bin", "eboot-menu"
    if name == "rpw_data_cpk.xdelta":
        return "CommonData/MtData/rpw_data.cpk", "menu-unit"
    if name == "AIDDataPack_cpk.xdelta":
        return "DATA/AIDDATA/AIDDataPack.cpk", "menu-unit"
    if name == "KDataVITA_cpk.xdelta":
        return "DATA/kurodata/KDataVITA.cpk", "menu-unit"
    if name == "SRVC_BIN.xdelta":
        # SRVC는 일본어-한국어 쌍과 슬롯 검사를 가진 JSON 실행기를 사용한다.
        return None
    match = re.fullmatch(r"map_(\d{3})_zld\.xdelta", name, flags=re.IGNORECASE)
    if match:
        return f"DATA/mapetc/attr/map_{match.group(1)}.zld", "map-data"
    match = re.fullmatch(r"(STG\d+[A-Za-z]?)_cpk\.xdelta", name, flags=re.IGNORECASE)
    if match:
        return f"DATA/STAGE/{match.group(1)}.cpk", "scenario-dialogue"
    if name == "param_sfo.xdelta":
        return "sce_sys/param.sfo", "system"
    raise ManifestBuildError(f"알 수 없는 본편 델타 이름입니다: {name}")


def _dlc_target_for_patch(name: str) -> str:
    match = re.fullmatch(r"(DLC\d{4})_cpk\.xdelta", name, flags=re.IGNORECASE)
    if match is None:
        raise ManifestBuildError(f"알 수 없는 DLC 델타 이름입니다: {name}")
    identifier = match.group(1).upper()
    number = identifier.removeprefix("DLC")
    # 실제 addcont 폴더명은 DLC 식별자 앞에 0을 하나 더 붙인다
    # (예: DLC0301 -> SRWZ3OF1DLC00301).
    return f"SRWZ3OF1DLC0{number}/DLC/{identifier}.cpk"


def _xdelta_entry(
    *,
    patch_path: Path,
    source_root: Path,
    target_root: Path,
    target: str,
    group: str,
    root_name: str,
) -> dict[str, Any]:
    source = _require_file(source_root / Path(target), f"원본 {target}")
    localized = _require_file(target_root / Path(target), f"적용 결과 {target}")
    patch = _require_file(patch_path, f"델타 {patch_path.name}")
    return {
        "kind": "xdelta",
        "profile": "vita3k",
        "root": root_name,
        "group": group,
        "target": target,
        "patch": patch.relative_to(REPOSITORY_ROOT).as_posix(),
        "source": _file_record(source),
        "localized": _file_record(localized),
        "delta": _file_record(patch),
    }


def _srvc_entry() -> dict[str, Any]:
    document = json.loads(_require_file(SRVC_JSON, "SRVC JSON").read_text(encoding="utf-8"))
    asset = document.get("asset")
    if not isinstance(asset, dict):
        raise ManifestBuildError("SRVC JSON에 asset 객체가 없습니다.")
    if document.get("format") == "siok.dialogue-review":
        source = asset.get("source")
        localized = asset.get("localized")
        if not isinstance(source, dict) or not isinstance(localized, dict):
            raise ManifestBuildError("SRVC 공통 JSON의 asset.source/localized가 없습니다.")
        entries = document.get("entries", [])
        slots = sum(
            len(item.get("location", {}).get("slots", []))
            for item in entries
            if isinstance(item, dict) and isinstance(item.get("location"), dict)
        )
        return {
            "kind": "battle-json",
            "profile": "vita3k",
            "root": "app",
            "group": "battle-dialogue",
            "target": "DATA/BTLC/SRVC.BIN",
            "patch": SRVC_JSON.relative_to(REPOSITORY_ROOT).as_posix(),
            "source": {"bytes": source["bytes"], "sha256": source["sha256"]},
            "localized": {"bytes": localized["bytes"], "sha256": localized["sha256"]},
            "delta": None,
            "counts": {
                "masterRows": len(entries),
                "slotRows": slots,
                "uniqueSourceRows": document.get("counts", {}).get("uniqueSourceTexts", 0),
            },
        }
    return {
        "kind": "battle-json",
        "profile": "vita3k",
        "root": "app",
        "group": "battle-dialogue",
        "target": "DATA/BTLC/SRVC.BIN",
        "patch": SRVC_JSON.relative_to(REPOSITORY_ROOT).as_posix(),
        "source": {
            "bytes": asset["bytes"],
            "sha256": asset["originalSha256"],
        },
        "localized": {
            "bytes": asset["bytes"],
            "sha256": asset["appliedSha256"],
        },
        "delta": None,
        "counts": document.get("counts", {}),
    }


def _dictionary_entry() -> dict[str, Any]:
    document = json.loads(_require_file(DICTIONARY_JSON, "사전 대사 JSON").read_text(encoding="utf-8"))
    asset = document.get("asset")
    localized = document.get("localizedAsset")
    if document.get("format") == "siok.dialogue-review":
        source = asset.get("source")
        localized_value = asset.get("localized")
        if not isinstance(source, dict) or not isinstance(localized_value, dict):
            raise ManifestBuildError("사전 공통 JSON의 asset.source/localized가 없습니다.")
        entries = document.get("entries", [])
        counts = document.get("counts", {})
        return {
            "kind": "dictionary-json",
            "profile": "vita3k",
            "root": "app",
            "group": "dictionary-dialogue",
            "target": "CommonData/MtData/MtZkn_KW.cpk",
            "patch": DICTIONARY_JSON.relative_to(REPOSITORY_ROOT).as_posix(),
            "source": {"bytes": source["bytes"], "sha256": source["sha256"]},
            "localized": {"bytes": localized_value["bytes"], "sha256": localized_value["sha256"]},
            "delta": None,
            "counts": {
                "members": asset.get("memberCount", 0),
                "fields": len(entries),
                "translatedFields": counts.get("translatedEntries", 0),
                "uniqueSourceTexts": counts.get("uniqueSourceTexts", 0),
            },
        }
    if not isinstance(asset, dict) or not isinstance(localized, dict):
        raise ManifestBuildError("사전 JSON에 asset/localizedAsset 메타데이터가 없습니다.")
    return {
        "kind": "dictionary-json",
        "profile": "vita3k",
        "root": "app",
        "group": "dictionary-dialogue",
        "target": "CommonData/MtData/MtZkn_KW.cpk",
        "patch": DICTIONARY_JSON.relative_to(REPOSITORY_ROOT).as_posix(),
        "source": {"bytes": asset["bytes"], "sha256": asset["sha256"]},
        "localized": {"bytes": localized["bytes"], "sha256": localized["sha256"]},
        "delta": None,
        "counts": document.get("counts", {}),
    }


def build_manifest(legacy_root: Path, output_path: Path, patches_root: Path) -> dict[str, Any]:
    main_base = legacy_root / "!배포" / "Original" / "PCSG00264"
    main_localized = legacy_root / "!배포" / "Korean" / "PCSG00264"
    main_patches = patches_root / "main"
    dlc_base = legacy_root / "!배포-DLC" / "Original" / "PCSG00264"
    dlc_localized = legacy_root / "!배포-DLC" / "Korean" / "PCSG00264"
    dlc_patches = patches_root / "dlc"
    for path, label in (
        (main_base, "본편 원본 기준 폴더"),
        (main_localized, "본편 적용 결과 폴더"),
        (dlc_base, "DLC 원본 기준 폴더"),
        (dlc_localized, "DLC 적용 결과 폴더"),
    ):
        if not path.is_dir():
            raise ManifestBuildError(f"{label}을(를) 찾을 수 없습니다: {path}")

    entries: list[dict[str, Any]] = []
    for patch in sorted(main_patches.glob("*.xdelta"), key=lambda item: item.name.casefold()):
        mapped = _main_target_for_patch(patch.name)
        if mapped is None:
            continue
        target, group = mapped
        entries.append(
            _xdelta_entry(
                patch_path=patch,
                source_root=main_base,
                target_root=main_localized,
                target=target,
                group=group,
                root_name="app",
            )
        )
    entries.append(_srvc_entry())
    entries.append(_dictionary_entry())

    for patch in sorted(dlc_patches.glob("*.xdelta"), key=lambda item: item.name.casefold()):
        target = _dlc_target_for_patch(patch.name)
        entries.append(
            _xdelta_entry(
                patch_path=patch,
                source_root=dlc_base,
                target_root=dlc_localized,
                target=target,
                group="scenario-dialogue",
                root_name="dlc",
            )
        )

    entries.sort(key=lambda item: (item["root"], item["target"]))
    group_counts: dict[str, int] = {}
    for item in entries:
        group_counts[item["group"]] = group_counts.get(item["group"], 0) + 1
    manifest: dict[str, Any] = {
        "$schema": "release-patch.schema.json",
        "format": FORMAT,
        "formatVersion": FORMAT_VERSION,
        "project": "PCSG00264",
        "release": "vita3k-2025-01-10-baseline",
        "policy": {
            "requiresUserOwnedGame": True,
            "gameBinaryIncluded": False,
            "completedGameIncluded": False,
            "deltaOnly": True,
            "originalJapaneseTextIncluded": True,
            "notice": "본인 소유·합법 덤프에만 사용하며 원본/완성 게임 파일을 공유하지 않습니다.",
        },
        "profiles": {
            "vita3k": {
                "enabled": True,
                "appOutputDirectory": "app",
                "dlcOutputDirectory": "addcont",
                "note": "Vita3K app와 addcont를 서로 다른 출력 트리로 만듭니다.",
            },
            "vita": {
                "enabled": False,
                "note": "실기용 eboot·패키지 서명 절차는 별도 검증 전까지 차단합니다.",
            },
        },
        "tool": {
            "name": "xdelta3",
            "version": "3.2.0",
            "assetSha256": "af8ef036cb077a48df080c9a8ac1be4a6e7511c32d11f8bec89b6803a9e52576",
            "executableSha256": "53d90226615f217d3380c39892833311b4e24acd863e1ca01f14b5e772e2e6d0",
            "downloadUrl": "https://github.com/jmacd/xdelta/releases/download/v3.2.0/xdelta3-3.2.0-windows-x86_64.zip",
        },
        "coverage": {
            "entryCount": len(entries),
            "groupCounts": group_counts,
            "mainXdeltaCount": sum(1 for item in entries if item["root"] == "app" and item["kind"] == "xdelta"),
            "dlcXdeltaCount": sum(1 for item in entries if item["root"] == "dlc"),
            "battleJsonCount": sum(1 for item in entries if item["kind"] == "battle-json"),
            "dictionaryJsonCount": sum(1 for item in entries if item["kind"] == "dictionary-json"),
        },
        "entries": entries,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-root", type=Path, required=True, help="읽기 전용 기존 PCSG00264 작업 폴더")
    parser.add_argument("--patches-root", type=Path, default=REPOSITORY_ROOT / "patches" / "release" / "vita3k")
    parser.add_argument("--output", type=Path, default=REPOSITORY_ROOT / "config" / "release-patch.json")
    args = parser.parse_args()
    manifest = build_manifest(args.legacy_root.resolve(), args.output.resolve(), args.patches_root.resolve())
    print(json.dumps({"entries": manifest["coverage"]["entryCount"], "groups": manifest["coverage"]["groupCounts"], "output": str(args.output.resolve())}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
