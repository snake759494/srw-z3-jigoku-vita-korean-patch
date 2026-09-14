"""DLC0323 팩 하나에 DLC0231·DLC0323 번역을 함께 넣는다.

``DLC0231``은 독립된 CPK가 아니라 ``DLC0323.cpk`` 안의 ``ID00003``이고,
``DLC0323``은 같은 팩의 ``ID00004``다. 뷰어의 CPK 빌드 버튼은 한 번에 하나만
반영하므로, 두 번역을 모두 담으려면 첫 결과를 두 번째 빌드의 원본으로 넘겨
연쇄로 만들어야 한다.

이 파일은 배포 번들에만 들어간다.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from siok_patch.scenario_cpk import (  # noqa: E402
    ScenarioCpkBuildError,
    build_scenario_cpk,
)

PACK_FILE_NAME = "DLC0323.cpk"
PACK_TARGET = "DLC0323/DLC/DLC0323.cpk"


def _load_scenario(asset_key: str) -> dict:
    for path in sorted((ROOT / "translations" / "dialogue").glob("scenario_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        asset = data.get("asset")
        if isinstance(asset, dict) and str(asset.get("assetKey")) == asset_key:
            return data
    raise SystemExit(f"[!] {asset_key} 시나리오 JSON을 찾지 못했습니다.")


def main() -> int:
    try:
        print("1/2 · DLC0323(ID00004) 번역 적용 중…")
        first = build_scenario_cpk(_load_scenario("DLC0323"), repository_root=ROOT)

        print("2/2 · DLC0231(ID00003) 번역을 같은 팩에 적용 중…")
        second_input = _load_scenario("DLC0231")
        second_input["asset"]["fileName"] = PACK_FILE_NAME
        second_input["asset"]["target"] = PACK_TARGET
        second = build_scenario_cpk(
            second_input,
            repository_root=ROOT,
            source_cpk=Path(str(first["outputCpk"])),
        )
    except ScenarioCpkBuildError as error:
        print(f"[!] 빌드 실패: {error}")
        return 1

    final = ROOT / "output" / "dlc0323-merged" / PACK_FILE_NAME
    final.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(second["outputCpk"]), final)
    print()
    print(f"완성 · {final}")
    print(f"크기 · {int(second['outputBytes']):,} 바이트")
    print("두 번역이 모두 들어간 DLC0323.cpk 입니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
