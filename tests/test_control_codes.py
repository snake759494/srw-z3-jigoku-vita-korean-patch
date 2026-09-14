import unittest

from siok_patch.control_codes import (
    compare_control_tokens,
    control_signature,
    describe_control_mismatch,
    extract_control_tokens,
    has_matching_control_tokens,
)


class ControlCodesTest(unittest.TestCase):
    def test_전각_영문_토큰만_같은_토큰으로_본다(self):
        source = "⑲원문$n 다음$l 색$c㊥"
        target = "⑲번역$ｎ 다음$Ｌ 색＄ｃ㊥"

        self.assertEqual(
            extract_control_tokens(target),
            ("⑲", "$n", "$l", "$c", "㊥"),
        )
        self.assertTrue(has_matching_control_tokens(source, target))

    def test_원문_전체를_nfkc로_변형하지_않는다(self):
        text = "⑲ ⑳ ㊥ ㊦ ㊧ ㊨ １２３"

        self.assertEqual(
            extract_control_tokens(text),
            ("⑲", "⑳", "㊥", "㊦", "㊧", "㊨"),
        )
        self.assertEqual(
            control_signature(text),
            '["⑲","⑳","㊥","㊦","㊧","㊨"]',
        )

    def test_순서가_바뀌면_불일치다(self):
        comparison = compare_control_tokens("⑲$n", "$n⑲")

        self.assertFalse(comparison.matches)
        self.assertTrue(comparison.order_changed)
        self.assertEqual(comparison.missing, ())
        self.assertEqual(comparison.extra, ())
        self.assertIn("순서", describe_control_mismatch(comparison))

    def test_누락과_추가_개수를_보고한다(self):
        comparison = compare_control_tokens("$n$n⑲", "$n$c")

        self.assertEqual(comparison.missing, ("$n", "⑲"))
        self.assertEqual(comparison.extra, ("$c",))
        self.assertIn("누락", describe_control_mismatch(comparison))
        self.assertIn("추가", describe_control_mismatch(comparison))

    def test_F_제어토큰과_전각_변형을_검사한다(self):
        self.assertEqual(extract_control_tokens("$Ｆ"), ("$F",))
        self.assertTrue(has_matching_control_tokens("$F", "＄ｆ"))
        self.assertFalse(has_matching_control_tokens("$F", ""))


if __name__ == "__main__":
    unittest.main()
