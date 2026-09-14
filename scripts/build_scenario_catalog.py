#!/usr/bin/env python3
"""시나리오 HTML 뷰어용 카탈로그를 생성한다.

카탈로그에는 파일 목록과 표시용 메타데이터만 저장하고, 실제 대사는 뷰어가
사용자가 고른 JSON 하나만 지연 로딩한다. 게임 원본이나 번역 데이터 자체를
새로 만들지 않으므로 저장소에서 반복 실행해도 안전하다.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any


CATALOG_FORMAT = "siok.scenario-viewer-catalog"


def _asset_key(data: dict[str, Any], filename: str) -> str:
    asset = data.get("asset")
    if isinstance(asset, dict) and asset.get("assetKey"):
        return str(asset["assetKey"])
    if data.get("assetKey"):
        return str(data["assetKey"])
    for entry in data.get("entries", []):
        location = entry.get("location") if isinstance(entry, dict) else None
        if isinstance(location, dict) and location.get("assetKey"):
            return str(location["assetKey"])
    return Path(filename).stem.removeprefix("scenario_")


def build_catalog(repo_root: Path, generated_at: str | None = None) -> dict[str, Any]:
    dialogue_dir = repo_root / "translations" / "dialogue"
    assets: list[dict[str, Any]] = []
    for path in sorted(dialogue_dir.glob("scenario_*.json"), key=lambda item: item.name.casefold()):
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if data.get("dialogueType") != "scenario" or not isinstance(data.get("entries"), list):
            continue
        key = _asset_key(data, path.name)
        is_dlc = key.upper().startswith("DLC") or path.name.upper().startswith("SCENARIO_DLC")
        assets.append(
            {
                "name": path.name,
                "key": key,
                "label": f"{key} · {path.name}",
                "scope": "dlc" if is_dlc else "stage",
                "path": f"../translations/dialogue/{path.name}",
                "entries": len(data["entries"]),
            }
        )
    assets.sort(key=lambda item: (item["scope"] != "stage", item["key"].casefold(), item["name"].casefold()))
    return {
        "$schema": "./scenario-index.schema.json",
        "format": CATALOG_FORMAT,
        "formatVersion": 1,
        "generatedAt": generated_at or date.today().isoformat(),
        "basePath": "../translations/dialogue/",
        "assets": assets,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="시나리오 뷰어 카탈로그 생성")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--generated-at", default=None, help="카탈로그 생성일(기본값: 오늘 날짜)")
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    output = (args.output or repo_root / "viewer" / "scenario-index.json").resolve()
    catalog = build_catalog(repo_root, args.generated_at)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"카탈로그 생성: {output}")
    print(f"시나리오 수: {len(catalog['assets'])}")
    print(f"대사 행 수: {sum(item['entries'] for item in catalog['assets']):,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
