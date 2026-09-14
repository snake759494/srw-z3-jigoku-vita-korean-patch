"""원문 기준으로 자동 초안의 잔류 일본어와 오역을 수동 보정한다."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from auto_translate_retranslation import _update_progress
from siok_patch.config import project_root
from siok_patch.hashes import sha256_file
from siok_patch.retranslation import publish_retranslation_task
from siok_patch.translation_io import read_tsv


# 자동 번역 결과에 히라가나/가타카나가 남은 행을 원문 의미에 맞춰 보정한다.
MANUAL_TRANSLATIONS: dict[str, str] = {
    "DLC0317/ID00003/000109": "도스코오오오오!",
    "STG0002/ID00003@FILE-ID00004/000044": "자자… 어제 배운 방법으로 제니온을…!",
    "STG0004/ID00003/000275": "이 변태 자식아아아아아!",
    "STG0004/ID00003@FILE-ID00004/000431": "퇴각이다, 서둘러!",
    "STG0005/ID00003/000502": "역시 이상해! 후후, 후후후, 후후후후후!",
    "STG0005/ID00003/000545": "아, 당신은 정말!",
    "STG0018/ID00003@FILE-ID00004/000303": "카부토 코지여, 지금이야말로 세계는 끝나고,",
    "STG0023/ID00003@FILE-ID00004/000169": "시끄러워어어어어어!",
    "STG0023/ID00003@FILE-ID00004/000256": "레드 숄더 자식아아아!",
    "STG0023/ID00003@FILE-ID00004/000373": "네놈!",
    "STG0025/ID00003/000313": "NERV의 사령관이라고…?",
    "STG0028/ID00003@FILE-ID00004/000250": "네놈!",
    "STG0031b/ID00004/000515": "아야나미이이이!",
    "STG0032/ID00003/000409": "당신 같은 남자는 정말!",
    "STG0032/ID00003/000920": "잔뜩 기대하게 해 놓고, 이게 뭐야…",
    "STG0032/ID00004/000571": "너는 정말!",
    "STG0033b/ID00003/000476": "그들을… $c를 모르기 때문인가…)",
    "STG0038/ID00003/000658": "뭐, 뭐라고오오오!",
    "STG0038/ID00004/000276": "쇼타로… 그리고 $c라는 자…!",
    "STG0039/ID00003/000415": "네놈!",
    "STG0044b/ID00004/000242": "쇼타로… 그리고 $c라는 자…!",
    "STG0045/ID00003/000769": "네놈!",
    "STG0046a/ID00004/000034": "뒈져라아아아아아!",
    "STG0048/ID00003/000539": "뜨거운 키스로!",
    "STG0051a/ID00003/000604": "너는 정말!",
    "STG0056/ID00003/000047": "      ～아일랜드 1 욕탕～",
    "STG0056/ID00003/000112": "뭐라고오오오!?",
    "STG0056/ID00003/000508": "      ～아일랜드 1 욕탕～",
    "STG0056/ID00004/000295": "한 꺼풀 벗어라, 아쿠에리온!",
    "STG0057/ID00003/000451": "젠장!  젠장, 으아아아!",
    "STG0057/ID00003/000621": "젠장!  젠장!  젠장, 으아아아!",
    "STG0061/ID00003/000794": "오늘도 힘차게 장사, 장사아아아!!!!!",
    "STG0067/ID00004/000298": "동귀어진을 노리는 건가!",
    "STG0068b/ID00003/000207": "신출귀몰하네….",
    "STG0068b/ID00003/001258": "네놈!",
    "STG0068b/ID00004/000444": "아바레스트라고!",
    "STG0068b/ID00004/000689": "당신이란 사람은 정말!",
    "STG0069/ID00003/001178": "뻔뻔하게 카미유의 이름을 꺼내다니.",
    "STG0070/ID00003/000742": "뭐어어어어어어!?",
    "STG0071/ID00004/000709": "공주님이이이!",
    "STG0072/ID00003/000443": "카부토 코지여!",
    "STG0072/ID00004/000443": "그러니까! 가아아아아!",
    "STG0073/ID00004/000106": "카부토 코지여!",
    "STG0074/ID00004/000640": "나도… 나도오오오!",
    "STG0081/ID00003/000104": "뭐 하는 거야!?",
    "STG0083/ID00003/000775": "부탁이야, 이제 그만해!",
    "STG0084/ID00004/000237": "카부토 코지여!",
    "STG0086/ID00004/000495": "미코노 씨를 돌려줘어어어!",
    "STG0086/ID00004/000757": "젠장!  젠장, 으아아아!",
    "STG0086/ID00004/000789": "젠장!  젠장, 으아아아!",
    "STG0086/ID00004/000871": "날개도 없는 놈!",
    "STG0086/ID00004/001211": "미코노 씨를 돌려줘어어어!",
    "STG0088a/ID00004/000615": "아직 마음의 준비가 안 됐어!",
    "STG0089b/ID00004/000504": "날개도 없는 놈…!",
    "STG0092/ID00004/000232": "미코노 씨를 돌려줘어어어!",
    "STG0092/ID00004/000866": "아직 마음의 준비가 안 됐어!",
    "STG0093a/ID00004/000670": "인간 따위가!",
    "STG0093a/ID00004/000802": "젠장!  젠장, 으아아아!",
    "STG0093a/ID00004/000884": "날개도 없는 놈!",
    "STG0093a/ID00004/001299": "미코노 씨를 돌려줘어어어!",
    "STG0097/ID00004/001117": "애송이가!",
    "STG0097/ID00004/001176": "너는 정말!",
    "STG0097/ID00004/002171": "빌어먹을!",
    "STG0099/ID00003/000342": "대체 뭐야, 당신은!",
    "STG0099/ID00004/000142": "아니, 인간 놈아!",
    "STG0099/ID00004/000683": "그런 게!",
    "STG0100a/ID00003/000023": "우리의 싸움은 일단락된다.",
    "STG0100a/ID00003/000062": "바나지",
    "STG0100a/ID00004/000023": "샤아",
    "STG0100a/ID00004/000062": "시공",
    "DLC0302/ID00003/000230": "누나라고 알고 있겠지, 이봐?",
    "DLC0315/ID00003/000211": "누나도 알고 있겠지?",
    "DLC0315/ID00003/000241": "누나가 그 정도로는",
    "DLC0315/ID00003/000267": "누나와 어울리는 거야.",
    "DLC0315/ID00004/000074": "룰은, 누나와 테사 중 하나가 끝나면",
    "DLC0315/ID00004/000096": "마오 누나의 진면목을!",
    "DLC0315/ID00004/000389": "누나에게는 내가 잘 말해둘 테니까.",
    "DLC0318/ID00003/000258": "누나는 내 활약, 보고 싶어?",
    "DLC0323/ID00004/000147": "원수는… 반드시 토벌한다!",
    "STG0004/ID00003@FILE-ID00004/000093": "카부토 코지와 활의 딸인가!",
    "STG0009/ID00003/000387": "카부토 군들은 눈에 띄지만, 별로 다른 사람들과 이야기하고 있는 곳,",
    "STG0009/ID00003/000439": "카부토 군들은 눈에 띄지만, 별로 다른 사람들과 이야기하고 있는 곳,",
    "STG0011/ID00003@FILE-ID00004/000313": "누나…!",
    "STG0012/ID00003/000563": "『마오 누나의 해병대식 김 수첩·신병 훈련 편』…",
    "STG0012/ID00003@FILE-ID00004/000291": "누나…!",
    "STG0017/ID00003/000575": "카부토 코지와 그 동료들의 힘을 알면,",
    "STG0021/ID00003@FILE-ID00004/000295": "챙긴 건가, 누나!?",
    "STG0025/ID00003/001664": "누나?",
    "STG0037/ID00003/000635": "으흐… 처녀의 가슴은 산산이 흐트러진다…)",
    "STG0037/ID00003/000644": "으흐… 세 사람의 1만 2000년 사랑의 행방은 어디로 가는가…)",
    "STG0040/ID00003/000699": "으흐… 처녀의 가슴은 산산이 흐트러진다…)",
    "STG0040/ID00003/000708": "으흐… 세 사람의 1만 2000년 사랑의 행방은 어디로 가는가…)",
    "STG0044a/ID00003/000238": "누나, 이 녀석은 한 개 처치했어!",
    "STG0056/ID00003/000534": "신출귀몰한 사람이네.",
    "STG0063/ID00003/000397": "카부토 코지는 살아남았나…",
    "STG0064/ID00004/000047": "나와 누나의 M9도 튜닝됐으니까!",
    "STG0069/ID00003/000449": "나와 마오 누나의 M9도 튜닝받았어.",
    "STG0069/ID00003/000704": "나와 마오 누나의 M9도 튜닝받았어.",
    "STG0070/ID00003/000491": "과연 누나답네…",
    "STG0096/ID00003/000096": "거의 원래대로 돌아왔지만…",
    "STG0098a/ID00003/001381": "늦으면 누나들한테 혼난다!",
    "STG0098a/ID00003/001416": "늦으면 누나들한테 혼난다!",
    "STG0270-45화후분기/ID00003/000221": "협박을 목적으로 하는 콜로니에 대한 공격작전….",
    "STG0700/ID00003@FILE-ID00001/000957": "그럼, 다음에도 저희들과 함께!",
}

# Google 번역이 고유명사로 판단해 원문 한자를 그대로 둔 경우의 표기 사전.
# 기호(⑲/㊦ 등)는 별도로 보존하므로 사전 값에는 본문만 적는다.
EXACT_SOURCE_TRANSLATIONS: dict[str, str] = {
    "兜": "카부토",
    "兜君…": "카부토 군…",
    "兜君、早乙女君！": "카부토 군, 사오토메 군!",
    "安": "안",
    "尸悠": "시유",
    "尸空": "시공",
    "尸逝天": "시세이텐",
    "張": "장",
    "朔哉": "사쿠야",
    "桂": "케이",
    "渚": "나기사",
    "玉蘭": "유이란",
    "百目鬼": "도메키",
    "拓": "타쿠",
    "神代": "진다이",
    "絆": "인연",
    "茂": "시게루",
    "號": "고우",
    "郁絵": "이쿠에",
    "黎": "레이",
}

KANJI_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("兜甲児", "카부토 코지"),
    ("兜博士", "카부토 박사"),
    ("兜甲", "카부토 코지"),
    ("兜君", "카부토 군"),
    ("早乙女君", "사오토메 군"),
    ("早乙女", "사오토메"),
    ("郁絵", "이쿠에"),
    ("朔哉", "사쿠야"),
    ("百目鬼", "도메키"),
    ("翅無し", "날개 없는 자"),
    ("翅犬", "익견"),
    ("天翅", "천익"),
    ("翅", "날개"),
    ("尸逝天", "시세이텐"),
    ("尸悠", "시유"),
    ("尸空", "시공"),
    ("玉蘭", "유이란"),
    ("神代", "진다이"),
    ("正太郎", "쇼타로"),
    ("正真正銘", "진짜배기"),
    ("明朗快活", "밝고 쾌활한"),
    ("仁義", "의리"),
    ("姉さん", "누나"),
    ("姉 씨", "누나"),
    ("姉씨", "누나"),
    ("姉", "누나"),
    ("達", "들"),
    ("木阿弥", "원래대로"),
    ("神出鬼没", "신출귀몰"),
    ("恫喝", "협박"),
    ("卑怯者", "비겁한 자"),
    ("黙れ", "닥쳐"),
    ("唔呼", "으흐"),
    ("仇", "원수"),
    ("硇", "이카리"),
    ("隧道弾", "터널탄"),
    ("篇", "편"),
    ("改", "개조"),
    ("張", "장"),
    ("桂", "케이"),
    ("渚", "나기사"),
    ("拓", "타쿠"),
    ("絆", "인연"),
    ("茂", "시게루"),
    ("號", "고우"),
    ("黎", "레이"),
)
SYMBOL_CHARS = frozenset("⑲⑳㊥㊦㊧㊨")

# 크레디트/사전처럼 이름이 연속된 행은 원문에서 일본어 이름 부분만
# 치환해 ⑲·㊦ 등의 게임 제어기호 위치를 그대로 유지한다.
SOURCE_REWRITES: dict[str, tuple[tuple[str, str], ...]] = {
    "사전2-flowman/정리본/001299": (("エヴァンゲリオン零号機（改）", "에반게리온 0호기(개조)"),),
    "사전2-flowman/정리본/003657": (
        ("劇場版", "극장판"),
        ("天元突破", "천원돌파"),
        ("グレンラガン", "그렌라간"),
        ("螺巌篇", "나암편"),
    ),
    "사전2-flowman/정리본/004017": (("\u5b89", "안"),),
    "사전2-flowman/정리본/004201": (("\u7887", "이카리"),),
    "사전2-flowman/정리본/004307": (("次元隧道弾", "차원 터널탄"),),
    "STG0100a/Sheet1/000008": (("砂原郁絵", "사하라 이쿠에"), ("潘恵子", "한 케이코")),
    "STG0100a/Sheet1/000051": (("張五飛", "장 우페이"), ("石野竜三", "이시노 류조")),
    "STG0100a/Sheet1/000057": (("キラ・ヤマト", "키라·야마토"), ("保志総一朗", "호시 소이치로")),
    "STG0100a/Sheet1/000115": (("神隼人", "진 하야토"), ("内田直哉", "우치다 나오야")),
    "STG0100a/Sheet1/000172": (
        ("劇場版", "극장판"),
        ("天元突破", "천원돌파"),
        ("グレンラガン", "그렌라간"),
        ("螺巌篇", "나암편"),
    ),
    "STG0100a/Sheet1/000226": (("青山穣", "아오야마 유타카"), ("池田知聡", "이케다 토모아키")),
    "STG0100a/Sheet1/000231": (("久保智史", "쿠보 사토시"), ("駒谷昌男", "코마야 마사오")),
    "STG0100a/Sheet1/000235": (("白熊寛嗣", "시로쿠마 히로시"), ("杉崎亮", "스기자키 료")),
    "STG0100a/Sheet1/000237": (("武虎", "타케토라"), ("武政秀一", "타케마사 슈이치")),
    "STG0100a/Sheet1/000259": (("小椋亙", "오구라 와타루"),),
    "STG0100a/Sheet1/000339": (("\u4f43\u4fe1\u662d", "츠쿠다 노부아키"),),
    "STG0100a/Sheet1/000550": (("田中亮", "타나카 료"),),
    "STG0100a/Sheet1/000629": (("\u4f0d\u8cc0\u4e00\u7d71", "고가 카즈노리"),),
    "STG0004/ID00003@FILE-ID00004/000093": (("兜甲児", "카부토 코지"), ("弓の娘", "활의 딸")),
    "STG0009/ID00003/000387": (("兜君達", "카부토 군들"),),
    "STG0009/ID00003/000439": (("兜君達", "카부토 군들"),),
    "STG0016/ID00003@FILE-ID00004/000473": (("張五飛", "장 우페이"),),
    "STG0017/ID00003/000575": (("兜甲児", "카부토 코지"),),
    "STG0056/ID00003/000534": (("神出鬼没", "신출귀몰"),),
    "STG0059/ID00004/000324": (("黙れ", "닥쳐"), ("兜甲児", "카부토 코지")),
    "STG0063/ID00003/000397": (("兜甲児", "카부토 코지"),),
    "STG0070/ID00003/000576": (("くろがね屋", "쿠로가네야"), ("男湯", "남탕")),
    "STG0096/ID00003/000096": (("木阿弥", "원래대로"),),
    "STG0270-45화후분기/ID00003/000221": (("恫喝", "협박"),),
}


def _write_json_atomic(value: object, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=f"{path.name}.", suffix=".candidate", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _stg0100a_translation(entry: dict) -> str | None:
    entry_id = str(entry.get("entryId", ""))
    source = str(entry.get("sourceText", ""))
    if entry_id == "STG0100a/Sheet1/000023":
        marker = "」"
        suffix = source[source.index(marker) + len(marker) :] if marker in source else ""
        return "「장갑기병 보톰즈⑲찬란한 이단」" + suffix
    if entry_id == "STG0100a/Sheet1/000062":
        # 기호(⑲/㊦)는 원문 위치와 개수를 그대로 유지하고 일본어 고유명사만 옮긴다.
        return source.replace("青ハロ", "청 하로").replace("佐藤有世", "사토 아리세")
    return None


def _exact_source_translation(source: str) -> str | None:
    plain = "".join(char for char in source if char not in SYMBOL_CHARS)
    base = EXACT_SOURCE_TRANSLATIONS.get(plain)
    if base is None:
        return None
    symbols = "".join(char for char in source if char in SYMBOL_CHARS)
    return base + symbols


def _replace_kanji_translation(text: str) -> str:
    result = text
    for source, target in KANJI_REPLACEMENTS:
        result = result.replace(source, target)
    return result


def _rewrite_source(entry: dict) -> str | None:
    entry_id = str(entry.get("entryId", ""))
    replacements = SOURCE_REWRITES.get(entry_id)
    if not replacements:
        return None
    result = str(entry.get("sourceText", ""))
    for source, target in replacements:
        result = result.replace(source, target)
    return result


def main() -> int:
    root = project_root()
    touched: dict[Path, dict] = {}
    applied = 0
    missing: list[str] = []
    for task_path in sorted((root / "work" / "retranslation" / "tasks").glob("*/*.json")):
        task = json.loads(task_path.read_text(encoding="utf-8"))
        # STG0001a처럼 이미 별도 검수 오버레이로 관리되는 부분은 로컬
        # 작업 JSON에 미번역 행이 남아 있을 수 있으므로 덮어쓰지 않는다.
        if any(not str(item.get("freshTranslation", "")).strip() for item in task.get("entries", [])):
            continue
        changed = False
        for entry in task.get("entries", []):
            entry_id = str(entry.get("entryId", ""))
            target = MANUAL_TRANSLATIONS.get(entry_id)
            if task.get("assetKey") == "STG0100a":
                target = _stg0100a_translation(entry) or target
            if target is None:
                target = _exact_source_translation(str(entry.get("sourceText", "")))
            if target is None:
                target = _rewrite_source(entry)
            if target is None:
                current = str(entry.get("freshTranslation", ""))
                replaced = _replace_kanji_translation(current)
                if replaced != current:
                    target = replaced
            if target is None:
                continue
            if entry.get("freshTranslation") == target:
                continue
            entry["freshTranslation"] = target
            entry["translationStatus"] = "draft"
            entry["translator"] = "Codex 수동 보정 (원문 기준)"
            entry["referenceConsulted"] = False
            note = "원문 의미와 제어기호를 기준으로 자동 초안 수동 보정"
            old_notes = str(entry.get("notes", "")).strip()
            entry["notes"] = f"{old_notes}; {note}" if old_notes else note
            touched[task_path] = task
            changed = True
            applied += 1
        if changed:
            print(f"보정: {task.get('scope')}/{task.get('assetKey')}")

    for task_path, task in touched.items():
        _write_json_atomic(task, task_path)
        output = (
            root
            / "translations"
            / "retranslation"
            / str(task["scope"])
            / f"{task['assetKey']}.json"
        )
        publish_retranslation_task(task_path, output_path=output, overwrite=True)

    source = root / "work" / "normalized" / "translations.tsv"
    _update_progress(root, read_tsv(source), sha256_file(source))
    print(f"수동 보정 완료: {applied}행, 자산 {len(touched)}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
