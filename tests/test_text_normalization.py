"""번역 공백과 게임 바이트 공백의 경계 테스트."""

from __future__ import annotations

import unittest

from siok_patch.text_normalization import (
    GAME_HALF_WIDTH_SPACE_BYTES,
    encode_game_dialogue_spaces,
    normalize_dialogue_spaces,
)


class TextNormalizationTests(unittest.TestCase):
    def test_json_translation_uses_ascii_space(self) -> None:
        self.assertEqual(normalize_dialogue_spaces("A\u3000B"), "A B")

    def test_game_text_uses_two_fe_bytes_for_one_space(self) -> None:
        value = encode_game_dialogue_spaces("A\u3000B")
        self.assertEqual(value.count("\uf8f2"), 2)
        self.assertEqual(value.encode("cp932"), b"A" + GAME_HALF_WIDTH_SPACE_BYTES + b"B")


if __name__ == "__main__":
    unittest.main()
