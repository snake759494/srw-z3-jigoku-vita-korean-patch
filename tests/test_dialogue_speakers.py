"""화자명 행 판정 회귀 테스트.

이 규칙은 뷰어의 ``isSpeakerEntry``와 같아야 한다. 갈라지면 일괄 찾아
바꾸기가 뷰어와 다른 행을 건드리게 된다.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from siok_patch.dialogue_speakers import (
    SpeakerCatalog,
    clean_text,
    control_tokens,
    has_visible_text,
    is_speaker_entry,
    load_speaker_catalog,
    visible_text,
)


def _entry(
    source: str, translation: str, *, asset: str = "STG0001A", member: str = "ID00003", row: int = 1
) -> dict:
    return {
        "entryId": f"{asset}/{member}/{row:06d}",
        "sourceText": source,
        "translation": translation,
        "location": {"assetKey": asset, "internalId": member, "sourceRow": row},
    }


class TextHelperTests(unittest.TestCase):
    def test_제어문자를_빼고_본문만_남긴다(self) -> None:
        self.assertEqual(visible_text("어때요, $n 씨?"), "어때요,  씨?")
        self.assertEqual(control_tokens("㊦앞$n뒤⑲"), ["㊦", "$n", "⑲"])

    def test_용어_괄호와_대사창_따옴표는_비교에서_뺀다(self) -> None:
        self.assertEqual(clean_text("「《천사》」"), "천사")
        self.assertFalse(has_visible_text("「」《》"))
        self.assertTrue(has_visible_text("「신」"))

    def test_전각_제어문자도_인식한다(self) -> None:
        self.assertEqual(control_tokens("＄Ｎ"), ["＄Ｎ"])


class SpeakerCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = SpeakerCatalog({"stg0001a/id00003": [2, 5]})

    def test_카탈로그에_있는_행은_화자로_본다(self) -> None:
        entry = _entry("シン", "신", row=2)
        self.assertTrue(self.catalog.is_speaker(entry))
        self.assertTrue(is_speaker_entry(entry, 0, [entry], self.catalog))

    def test_카탈로그에_없는_행은_카탈로그로는_화자가_아니다(self) -> None:
        entry = _entry("見ろよ、みんな！", "봐, 모두!", row=3)
        self.assertFalse(self.catalog.is_speaker(entry))

    def test_대소문자와_무관하게_찾는다(self) -> None:
        self.assertTrue(self.catalog.covers(_entry("シン", "신", row=2)))

    def test_행_번호가_없으면_덮지_않는다(self) -> None:
        entry = _entry("シン", "신")
        entry["location"]["sourceRow"] = 0
        self.assertFalse(self.catalog.covers(entry))

    def test_카탈로그_파일이_없으면_빈_카탈로그다(self) -> None:
        with TemporaryDirectory() as directory:
            catalog = load_speaker_catalog(Path(directory) / "없는파일.json")
        self.assertEqual(len(catalog), 0)

    def test_형식이_다르면_거부한다(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "speakers.json"
            path.write_text(json.dumps({"format": "다른형식"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_speaker_catalog(path)


class SpeakerHeuristicTests(unittest.TestCase):
    """카탈로그가 없는 멤버(현재 DLC)에만 쓰는 추정 규칙."""

    def test_알려진_화자_이름은_화자다(self) -> None:
        entries = [_entry("シン", "신"), _entry("見ろよ！", "봐!")]
        self.assertTrue(is_speaker_entry(entries[0], 0, entries))

    def test_문장부호가_있으면_화자가_아니다(self) -> None:
        entries = [_entry("見ろよ、みんな！", "봐, 모두!"), _entry("次", "다음")]
        self.assertFalse(is_speaker_entry(entries[0], 0, entries))

    def test_원문이_조사로_끝나면_화자가_아니다(self) -> None:
        entries = [_entry("俺達の", "우리들의"), _entry("地球だ", "지구다 그것도 아주 크게")]
        self.assertFalse(is_speaker_entry(entries[0], 0, entries))

    def test_짧은_이름_다음에_긴_대사가_오면_화자로_본다(self) -> None:
        entries = [_entry("ムーサ", "무사"), _entry("長い台詞", "아주 긴 대사가 이어집니다")]
        self.assertTrue(is_speaker_entry(entries[0], 0, entries))

    def test_다음_행이_더_짧으면_화자가_아니다(self) -> None:
        entries = [_entry("ムーサ", "무사무사무사"), _entry("短い", "짧다")]
        self.assertFalse(is_speaker_entry(entries[0], 0, entries))

    def test_제어문자만_있는_행은_다음_행이_있어야_화자다(self) -> None:
        entries = [_entry("㊦", "㊦"), _entry("台詞", "대사")]
        self.assertTrue(is_speaker_entry(entries[0], 0, entries))
        alone = [_entry("㊦", "㊦")]
        self.assertFalse(is_speaker_entry(alone[0], 0, alone))

    def test_마지막_행은_뒤가_없으므로_화자가_아니다(self) -> None:
        entries = [_entry("台詞", "대사"), _entry("ムーサ", "무사")]
        self.assertFalse(is_speaker_entry(entries[1], 1, entries))

    def test_너무_긴_이름은_화자가_아니다(self) -> None:
        long_name = "가" * 25
        entries = [_entry("ムーサ", long_name), _entry("台詞", "대사" * 30)]
        self.assertFalse(is_speaker_entry(entries[0], 0, entries))

    def test_카탈로그가_아니어도_추정으로_화자를_찾는다(self) -> None:
        catalog = SpeakerCatalog({"stg9999/id00003": [7]})
        entries = [_entry("シン", "신", asset="DLC0305"), _entry("台詞", "긴 대사입니다")]
        self.assertTrue(is_speaker_entry(entries[0], 0, entries, catalog))


if __name__ == "__main__":
    unittest.main()
