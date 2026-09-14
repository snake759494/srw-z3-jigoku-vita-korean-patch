"""STAGE 대사 payload의 추출·재조립 회귀 테스트."""

from __future__ import annotations

import unittest

from siok_patch.stage_script import (
    StageScriptDecodeError,
    StageScriptEncodeError,
    StageScriptError,
    StageScriptRowCountError,
    parse_stage_script,
    rebuild_stage_script,
)


def _cp932(text: str) -> bytes:
    return text.encode("cp932")


class StageScriptTests(unittest.TestCase):
    def test_기존_모든_태그와_전각공백_슬롯을_추출한다(self) -> None:
        original = b"".join(
            (
                b"header\r\n",
                _cp932("  [[甲") + b"\r\n",
                _cp932("      [[乙") + b"\n",
                _cp932("          [[丙") + b"\r\n",
                _cp932("              [[丁") + b"\n",
                _cp932("「一行」]],") + b"\r\n",
                _cp932("「複数行") + b"\n",
                _cp932("（考え始め") + b"\r\n",
                _cp932("（一行）]],") + b"\n",
                _cp932("　中間") + b"\r\n",
                _cp932("　最後」]],") + b"\n",
                _cp932("　東京]],") + b"\r\n",
                _cp932("　") + b"\n",
                b"tail",
            )
        )

        parsed = parse_stage_script(original)

        self.assertEqual(
            tuple(slot.tag for slot in parsed.slots),
            (
                "SP",
                "SB",
                "SF",
                "SG",
                "S1",
                "S2",
                "S2",
                "S3",
                "SM",
                "SE",
                "ST",
                "SM",
            ),
        )
        self.assertEqual(
            parsed.source_rows,
            (
                "甲",
                "乙",
                "丙",
                "丁",
                "一行",
                "複数行",
                "（考え始め",
                "（一行）",
                "中間",
                "最後",
                "東京",
                "",
            ),
        )
        self.assertEqual(parsed.slots[-1].line_number, 13)
        self.assertEqual(parsed.slots[-1].ending, b"\n")

    def test_무변경_교체는_혼합_줄바꿈과_마지막_무개행까지_동일하다(self) -> None:
        original = b"".join(
            (
                b"before\r\n",
                _cp932("  [[名前") + b"\n",
                _cp932("「一行」]],") + b"\r\n",
                _cp932("　") + b"\n",
                _cp932("　終端]],"),
            )
        )
        parsed = parse_stage_script(original)

        rebuilt = rebuild_stage_script(parsed, parsed.source_rows)

        self.assertEqual(rebuilt, original)

    def test_변경한_행에만_기존_보정과_태그_틀을_적용한다(self) -> None:
        original = b"".join(
            (
                _cp932("  [[元") + b"\r\n",
                _cp932("      [[元") + b"\n",
                _cp932("「元」]],") + b"\r\n",
                _cp932("「元") + b"\n",
                _cp932("（元") + b"\r\n",
                _cp932("（元）]],") + b"\n",
                _cp932("　元") + b"\r\n",
                _cp932("　元」]],") + b"\n",
                _cp932("　元]],"),
            )
        )
        parsed = parse_stage_script(original)
        replacements = (
            "",
            "$ｎ名",
            "（心）",
            "$ｌ続き",
            "（考え",
            "（完）",
            "$ｃ中",
            "終～",
            "$Ｆ場所",
        )

        rebuilt = rebuild_stage_script(parsed, replacements)

        expected = b"".join(
            (
                _cp932("  [[　　　　　") + b"\r\n",
                _cp932("      [[$n名") + b"\n",
                _cp932("（心）]],") + b"\r\n",
                _cp932("「$l続き") + b"\n",
                _cp932("（考え") + b"\r\n",
                _cp932("（完）]],") + b"\n",
                _cp932("　$c中") + b"\r\n",
                _cp932("　終～]],") + b"\n",
                _cp932("　$F場所]],"),
            )
        )
        self.assertEqual(rebuilt, expected)

    def test_삽입_번역의_공백을_게임의_fefe로_정규화한다(self) -> None:
        parsed = parse_stage_script(_cp932("「原文」]],\n"))

        rebuilt = rebuild_stage_script(parsed, ("前\u3000後",))

        expected = _cp932("「前") + b"\xFE\xFE" + _cp932("後」]],\n")
        self.assertEqual(rebuilt, expected)

    def test_비대사에_있는_전각_제어문자_비슷한_문자열은_바꾸지_않는다(self) -> None:
        original = _cp932("comment $ｎ 「（ ）」 ～」") + b"\r\n" + _cp932("「元」]],")
        parsed = parse_stage_script(original)

        rebuilt = rebuild_stage_script(parsed, ("新",))

        self.assertTrue(rebuilt.startswith(_cp932("comment $ｎ 「（ ）」 ～」") + b"\r\n"))
        self.assertTrue(rebuilt.endswith(_cp932("「新」]],")))

    def test_교체_행_수가_부족하거나_많으면_거부한다(self) -> None:
        parsed = parse_stage_script(_cp932("  [[甲\n「乙」]],\n"))

        for replacements in ((), ("甲",), ("甲", "乙", "丙")):
            with self.subTest(replacements=replacements):
                with self.assertRaises(StageScriptRowCountError):
                    rebuild_stage_script(parsed, replacements)

    def test_cp932_불가_문자는_슬롯과_원본행을_포함해_거부한다(self) -> None:
        parsed = parse_stage_script(b"header\r\n" + _cp932("  [[元") + b"\r\n")

        with self.assertRaises(StageScriptEncodeError) as caught:
            rebuild_stage_script(parsed, ("한글",))

        message = str(caught.exception)
        self.assertIn("슬롯 #1", message)
        self.assertIn("원본 2행", message)
        self.assertIn("CP932", message)

    def test_원본의_잘못된_cp932와_교체값_줄바꿈을_거부한다(self) -> None:
        with self.assertRaises(StageScriptDecodeError) as caught:
            parse_stage_script(b"ok\r\n\x81\r\n")
        self.assertIn("원본 2행", str(caught.exception))

        parsed = parse_stage_script(_cp932("「元」]],\n"))
        with self.assertRaises(StageScriptError):
            rebuild_stage_script(parsed, ("두\n줄",))


if __name__ == "__main__":
    unittest.main()
