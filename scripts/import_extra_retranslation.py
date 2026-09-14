"""추가 CPK/BIN 자산의 기준 TSV를 재번역 전용 트리로 만든다.

기존 번역 열은 ``references``로만 보존되고, ``source_text``에서 새 번역을
생성하는 후속 단계가 사용할 수 있도록 자산별 고유 ID를 부여한다.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import re
from pathlib import Path
from typing import Any, Iterable

from siok_patch.hashes import sha256_file
from siok_patch.translation_io import (
    AssetSource,
    TranslationImportError,
    TranslationRow,
    import_asset,
    load_asset_config,
    write_tsv,
)


FORMAT = "siok-extra-asset-baseline"
FORMAT_VERSION = 1


def _read_config(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or data.get("format") != "siok-extra-asset-sources":
        raise TranslationImportError(f"추가 자산 설정 형식이 올바르지 않습니다: {path}")
    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        raise TranslationImportError("추가 자산 설정에 sources가 없습니다.")
    return data


def _safe_archive_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise TranslationImportError(f"원본 아카이브 밖의 경로입니다: {relative}")
    return candidate


def _expand_source(root: Path, spec: dict[str, Any]) -> list[Path]:
    if isinstance(spec.get("path"), str):
        path = _safe_archive_path(root, spec["path"])
        if not path.is_file():
            raise FileNotFoundError(f"추가 자산 XLSX를 찾을 수 없습니다: {path}")
        return [path]
    pattern = spec.get("glob")
    if not isinstance(pattern, str) or not pattern:
        raise TranslationImportError("추가 자산 source에 path 또는 glob이 필요합니다.")
    paths = sorted(
        {
            path.resolve()
            for path in root.glob(pattern)
            if path.is_file() and path.suffix.lower() == ".xlsx"
        },
        key=lambda path: path.relative_to(root).as_posix().casefold(),
    )
    if not paths:
        raise FileNotFoundError(f"추가 자산 glob에 맞는 XLSX가 없습니다: {pattern}")
    return paths


def _asset_key(spec: dict[str, Any], path: Path, root: Path) -> str:
    fixed = spec.get("assetKey")
    if isinstance(fixed, str) and fixed.strip():
        return fixed.strip()
    template = spec.get("assetKeyTemplate")
    expression = spec.get("idRegex")
    if not isinstance(template, str) or not isinstance(expression, str):
        raise TranslationImportError("glob source에는 assetKeyTemplate과 idRegex가 필요합니다.")
    match = re.search(expression, path.name, flags=re.IGNORECASE)
    if match is None:
        match = re.search(expression, path.relative_to(root).as_posix(), flags=re.IGNORECASE)
    if match is None:
        raise TranslationImportError(f"자산 ID를 파일명에서 찾을 수 없습니다: {path}")
    identifier = (match.group(1) if match.lastindex else match.group(0)).upper()
    return template.replace("{id}", identifier).strip()


def _selected_rows(
    root: Path,
    source: Path,
    scope: str,
    asset_key: str,
    sheet_name: str,
    aliases: dict[str, Any],
) -> tuple[list[TranslationRow], list[str]]:
    relative = source.relative_to(root).as_posix()
    result = import_asset(AssetSource(scope, source, relative), root, aliases)
    selected = [row for row in result.rows if row.internal_id == sheet_name]
    if not selected:
        available = sorted({row.internal_id for row in result.rows})
        raise TranslationImportError(
            f"{relative}에서 sheet '{sheet_name}'를 찾지 못했습니다. "
            f"인식된 시트: {', '.join(available) or '(없음)'}"
        )
    normalized: list[TranslationRow] = []
    for row in selected:
        normalized.append(
            replace(
                row,
                scope=scope,
                asset_key=asset_key,
                entry_id=f"{asset_key}/{row.internal_id}/{row.source_row:06d}",
            )
        )
    return normalized, list(result.warnings)


def build_baseline(
    archive_root: Path,
    config_path: Path,
    asset_config_path: Path,
    output_tsv: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    root = archive_root.resolve()
    config = _read_config(asset_config_path)
    aliases = load_asset_config(config_path).get("headerAliases", {})
    rows: list[TranslationRow] = []
    manifest_sources: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_paths: set[Path] = set()

    for spec_index, raw_spec in enumerate(config["sources"], start=1):
        if not isinstance(raw_spec, dict):
            raise TranslationImportError(f"sources[{spec_index}]가 객체가 아닙니다.")
        scope = str(raw_spec.get("scope", "")).strip()
        sheet = str(raw_spec.get("sheet", "")).strip()
        if not scope or not sheet:
            raise TranslationImportError(f"sources[{spec_index}]에 scope/sheet가 필요합니다.")
        for source in _expand_source(root, raw_spec):
            if source in seen_paths:
                raise TranslationImportError(f"추가 자산 경로가 중복됩니다: {source}")
            seen_paths.add(source)
            asset_key = _asset_key(raw_spec, source, root)
            selected, warnings = _selected_rows(
                root, source, scope, asset_key, sheet, aliases
            )
            for row in selected:
                if row.entry_id in seen_ids:
                    raise TranslationImportError(f"추가 자산 entry_id가 중복됩니다: {row.entry_id}")
                seen_ids.add(row.entry_id)
            rows.extend(selected)
            manifest_sources.append(
                {
                    "scope": scope,
                    "assetKey": asset_key,
                    "path": source.relative_to(root).as_posix(),
                    "sha256": sha256_file(source),
                    "sheet": sheet,
                    "rowCount": len(selected),
                    "warnings": warnings,
                }
            )

    rows.sort(key=lambda row: (row.scope, row.asset_key, row.internal_id, row.source_row))
    output_tsv.parent.mkdir(parents=True, exist_ok=True)
    write_tsv(rows, output_tsv)
    manifest = {
        "format": FORMAT,
        "formatVersion": FORMAT_VERSION,
        "policy": config.get("policy", {}),
        "baseline": {
            "fileName": output_tsv.name,
            "sha256": sha256_file(output_tsv),
            "rowCount": len(rows),
            "assetCount": len({(row.scope, row.asset_key) for row in rows}),
        },
        "sources": manifest_sources,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument(
        "--asset-config",
        type=Path,
        default=Path("config/extra-asset-sources.json"),
    )
    parser.add_argument(
        "--groups-config",
        type=Path,
        default=Path("config/asset-groups.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("work/extra-normalized/translations.tsv"),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("translations/retranslation-extra/source-manifest.json"),
    )
    args = parser.parse_args()
    manifest = build_baseline(
        args.archive_root.resolve(),
        args.groups_config.resolve(),
        args.asset_config.resolve(),
        args.output.resolve(),
        args.manifest.resolve(),
    )
    baseline = manifest["baseline"]
    print(
        json.dumps(
            {
                "assets": baseline["assetCount"],
                "rows": baseline["rowCount"],
                "sha256": baseline["sha256"],
                "sources": len(manifest["sources"]),
                "output": str(args.output.resolve()),
                "manifest": str(args.manifest.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
