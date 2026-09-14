#!/usr/bin/env python3
"""원본/기존 한글 CPK에서 스테이지 조건 번역 JSON을 만든다.

게임 파일과 기존 한글 CPK는 로컬에만 두고, 이 스크립트가 생성한 JSON만 저장소에
커밋한다. 조건은 각 CPK의 ``ID00001`` 안 ``OPERATE_TBL.str_tbl``에 저장된다.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from siok_patch.cpk_tool import CpkMakerTool  # noqa: E402
from siok_patch.scenario_cpk import _operate_string_spans, _load_wreplace  # noqa: E402


EXPECTED_TOOL_SHA256 = "8cea88e11a5460ff8c4a2cc6b5fd214ebeb40e5a661aec596925538267ab4954"
OPERATION_WREPLACE_SHA256 = "8951addfe77eaddfbe73794ebd0e94287c5f9185d873dbd89f0bb4dec7cdc987"

# DLC에는 기존 한글 CPK가 없으므로, 조건 화면에서 반복되는 원문과
# 고유명사·게임 용어를 명시적으로 관리한다. 자동 번역 서비스는 사용하지
# 않으며, 새로운 문장이 추가되면 이 표를 검토한 뒤 확장한다.
_DLC_TRANSLATIONS = {
    "敵の全滅。": "적의 전멸",
    "ヒビキＡの撃墜。": "히비키 A의 격추",
    "ダウンロードコンテンツには\nＳＲポイントはありません。": "다운로드 콘텐츠에는\nSR 포인트가 없습니다.",
    "キリコの撃墜。": "키리코의 격추",
    "いずれかの味方ユニットの撃墜。": "아군 유닛 중 하나라도 격추",
    "敵ユニットのライン到達。": "적 유닛이 라인에 도달",
    "５ターン以内に敵を全滅させる。": "5턴 이내에 적을 전멸시킨다",
    "６ターン目を迎える。": "6턴을 맞이한다",
    "５ターン以内にトライダーＧ７を撃墜する。": "5턴 이내에 트라이더 G7을 격추한다",
    "なし。": "없음",
    "なし": "없음",
    "スズネの撃墜。": "스즈네의 격추",
    "このターンで敵を全滅させる。": "이번 턴에 적을 전멸시킨다",
    "次のターンを迎える。": "다음 턴을 맞이한다",
    "６ターン以内に敵を３０機以上、撃墜する。": "6턴 이내에 적을 30기 이상 격추한다",
    "アマタの撃墜。": "아마타의 격추",
    "７ターン目を迎えた場合。": "7턴째를 맞이한 경우",
    "敵ユニットのマップ西端への到達。": "적 유닛이 맵 서쪽 끝에 도달",
    "デストロイモード発動から５ターン以内に敵を全滅させる。": "디스트로이 모드 발동 후 5턴 이내에 적을 전멸시킨다",
    "デストロイモード発動から５ターンが経過する。": "디스트로이 모드 발동 후 5턴이 경과한다",
    "バサラの撃墜。": "바사라의 격추",
    "敵ユニットのマップ端到達。": "적 유닛이 맵 끝에 도달",
    "ロジャーの撃墜。": "로저의 격추",
    "味方の全滅。": "아군의 전멸",
    "グレンラガンの撃墜。": "그렌라간의 격추",
    "ガンバスターの撃墜。": "건버스터의 격추",
    "宇宙怪獣登場から３ターン以内に敵を全滅させる。": "우주괴수 등장 후 3턴 이내에 적을 전멸시킨다",
    "宇宙怪獣登場から３ターン経過する。": "우주괴수 등장 후 3턴이 경과한다",
    "Ｍ９　ガーンズバック（マオ）の撃墜。": "M9 건즈백(마오)의 격추",
    "宗介の撃墜。": "소스케의 격추",
    "テッサの撃墜。": "테사의 격추",
    "味方戦艦の撃墜。": "아군 전함의 격추",
    "真ゲッター１の撃墜。": "진 겟타 1의 격추",
    "４ターン目を迎える。": "4턴을 맞이한다",
    "５ターン以内に敵を全滅させる。\nその際、三人の味方ＮＰＣにそれぞれ敵を１機以上、\n撃墜させる。": "5턴 이내에 적을 전멸시킨다.\n그때 3명의 아군 NPC가 각각 적을 1기 이상\n격추하게 한다.",
    "勝利条件を満たせなかった場合。": "승리 조건을 충족하지 못한 경우",
    "いずれかの味方ユニット（連邦兵を含む）の撃墜。": "아군 유닛(연방병 포함) 중 하나라도 격추",
    "敵ユニットの指定エリア侵入。": "적 유닛이 지정 구역에 침입",
    "いずれかの味方戦艦の撃墜。": "아군 전함 중 하나라도 격추",
    "ガドライト、またはアンナロッタの撃墜。": "가드라이트 또는 안나로타의 격추",
}


def _config() -> dict[str, object]:
    path = ROOT / "private" / "project.local.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _find_tool(config: dict[str, object]) -> Path:
    configured = Path(str(config.get("cpkToolPath") or "")).expanduser()
    candidates = [configured] if configured else []
    candidates.extend(ROOT.glob("work/private-tools/**/cpkmakec.exe"))
    for candidate in candidates:
        if candidate.is_file():
            from siok_patch.hashes import sha256_file

            if sha256_file(candidate).lower() == EXPECTED_TOOL_SHA256:
                return candidate.resolve()
    raise SystemExit("검증된 cpkmakec.exe를 찾을 수 없습니다.")


def _archive_root(config: dict[str, object]) -> Path:
    candidates = [
        Path(str(config.get("archiveRoot") or "")).expanduser(),
        ROOT.parent / "PCSG00264",
        Path(r"D:\Z\psvita\PCSG00264"),
    ]
    for candidate in candidates:
        if (candidate / "0_STAGE").is_dir():
            return candidate.resolve()
    raise SystemExit("원본 PCSG00264/0_STAGE를 찾을 수 없습니다.")


def _sources(archive: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for base in (archive / "0_STAGE", archive / "!DLC"):
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.cpk"), key=lambda item: str(item).casefold()):
            # main은 !!!원본을 우선한다. DLC는 동일 stem이 거의 없지만 같은 규칙을
            # 적용해 이미 선택한 경로를 덮어쓰지 않는다.
            if path.stem not in result or path.parent.name == "!!!원본":
                result[path.stem] = path.resolve()
    return result


def _operation_table(archive: Path) -> tuple[Path, dict[str, str]]:
    candidates = list(archive.rglob("Japanese - Hangul to Kanji(oper).wReplace"))
    for path in candidates:
        from siok_patch.hashes import sha256_file

        if sha256_file(path).lower() == OPERATION_WREPLACE_SHA256:
            return path, _load_wreplace(path)
    raise SystemExit("Japanese - Hangul to Kanji(oper).wReplace를 찾을 수 없습니다.")


def _reverse_text(value: str, mapping: dict[str, str]) -> str:
    reverse: dict[str, str] = {}
    for source, target in mapping.items():
        if target and target not in reverse:
            reverse[target] = source
    return "".join(reverse.get(char, char) for char in value)


def _operate_block(content: str) -> str | None:
    start = content.find("OPERATE_TBL = {")
    if start < 0:
        return None
    end_marker = content.find("};", start)
    if end_marker < 0:
        return None
    return content[start : end_marker + 2]


def _condition_types(block: str) -> dict[int, list[str]]:
    match = re.search(r"\bid_tbl\s*=\s*\{(?P<body>.*?)\};", block, re.DOTALL)
    result: dict[int, list[str]] = defaultdict(list)
    if match is None:
        return result
    for line in match.group("body").splitlines():
        label_match = re.search(r"--\s*(勝利条件|敗北条件|ＳＲ条件|SR条件)", line)
        if label_match is None:
            continue
        label = label_match.group(1)
        condition_type = "win" if "勝利" in label else "defeat" if "敗北" in label else "sr"
        prefix = line[: label_match.start()]
        for number in re.findall(r"(?<![-A-Za-z0-9])\d+", prefix):
            source_id = int(number, 10)
            if condition_type not in result[source_id]:
                result[source_id].append(condition_type)
    return dict(result)


def _extract_member(tool: CpkMakerTool, cpk: Path, parent: Path) -> tuple[bytes, str]:
    destination = parent / (cpk.stem + "-extract")
    entries = tool.extract(cpk, destination)
    member = entries.get(1)
    if member is None:
        raise ValueError(f"{cpk.name}에 ID00001이 없습니다.")
    payload = member.path.read_bytes()
    return payload, hashlib.sha256(payload).hexdigest()


def _make_document(
    asset_key: str,
    source_cpk: Path,
    localized_cpk: Path | None,
    tool: CpkMakerTool,
    mapping: dict[str, str],
    temporary_root: Path,
) -> dict[str, object] | None:
    try:
        source_payload, source_sha = _extract_member(tool, source_cpk, temporary_root / "source")
    except ValueError:
        # 일부 분기/연출 CPK는 전투 조건 테이블 없이 ID00001 자체가 없다.
        return None
    try:
        source_content = source_payload.decode("cp932")
    except UnicodeDecodeError:
        return None
    source_block = _operate_block(source_content)
    if source_block is None:
        return None
    source_spans = _operate_string_spans(source_block)
    if not source_spans:
        return None
    localized_spans = source_spans
    translation_status = "untranslated"
    if localized_cpk is not None and localized_cpk.is_file():
        try:
            localized_payload, _ = _extract_member(tool, localized_cpk, temporary_root / "localized")
            localized_block = _operate_block(localized_payload.decode("cp932"))
            if localized_block is not None:
                localized_spans = _operate_string_spans(localized_block)
                translation_status = "translated"
        except (UnicodeDecodeError, ValueError):
            localized_spans = source_spans

    condition_types = _condition_types(source_content[source_content.find("OPERATE_TBL = {") :])
    entries: list[dict[str, object]] = []
    for source_id, (_start, _end, source_text) in sorted(source_spans.items()):
        localized_text = localized_spans.get(source_id, (_start, _end, source_text))[2]
        entry_status = translation_status
        translation = _reverse_text(localized_text, mapping) if entry_status == "translated" else source_text
        if asset_key.startswith("DLC") and entry_status != "translated":
            direct_translation = _DLC_TRANSLATIONS.get(source_text)
            if direct_translation is not None:
                translation = direct_translation
                entry_status = "translated"
                translator = "직접 검토한 DLC 조건 번역"
            else:
                translator = "미번역"
        else:
            translator = "기존 한글 CPK 역추출" if entry_status == "translated" else "미번역"
        entries.append(
            {
                "entryId": f"{asset_key}/ID00001/OPERATE_TBL/str_tbl/{source_id:03d}",
                "sourceId": source_id,
                "sourceText": source_text,
                "translation": translation,
                "conditionTypes": condition_types.get(source_id, ["other"]),
                "translationStatus": entry_status,
                "translator": translator,
                "notes": "원본 ID00001의 OPERATE_TBL.str_tbl에서 추출함.",
            }
        )
    return {
        "$schema": "../../config/stage-conditions.schema.json",
        "format": "siok.stage-conditions",
        "formatVersion": 1,
        "gameId": "PCSG00264",
        "asset": {
            "assetKey": asset_key,
            "fileName": f"{asset_key}.cpk",
            "target": f"DATA/STAGE/{asset_key}.cpk" if asset_key.startswith("STG") else f"{asset_key}/{asset_key}/{asset_key}.cpk",
        },
        "source": {"memberId": "ID00001", "encoding": "cp932", "sha256": source_sha},
        "entries": entries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="스테이지 조건 JSON 추출")
    parser.add_argument("--only", action="append", help="특정 asset만 추출")
    parser.add_argument("--include-dlc", action="store_true", help="DLC도 포함")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "translations" / "conditions")
    parser.add_argument("--old-korean-root", type=Path, default=None)
    args = parser.parse_args(argv)
    config = _config()
    archive = _archive_root(config)
    old_root = args.old_korean_root or archive / "!배포" / "Korean" / "PCSG00264" / "DATA" / "STAGE"
    tool_path = _find_tool(config)
    _operation_path, mapping = _operation_table(archive)
    tool = CpkMakerTool(tool_path, EXPECTED_TOOL_SHA256)
    selected = set(args.only or [])
    sources = _sources(archive)
    sources = {
        key: value
        for key, value in sources.items()
        if (args.include_dlc or not key.startswith("DLC")) and (not selected or key in selected)
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="siok-stage-conditions-") as temporary:
        temporary_root = Path(temporary)
        for asset_key, source_cpk in sorted(sources.items()):
            localized = old_root / f"{asset_key}.cpk"
            document = _make_document(
                asset_key, source_cpk, localized if localized.is_file() else None,
                tool, mapping, temporary_root / asset_key,
            )
            if document is None:
                continue
            output = args.output_dir / f"scenario_{asset_key}.json"
            output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
            print(f"{asset_key}: {len(document['entries'])}개 조건 → {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
