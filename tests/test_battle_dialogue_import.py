"""SRVC 전투 대사 공개 산출물 도구의 안전 경계를 검사한다."""

from __future__ import annotations

import unittest
from pathlib import Path

from scripts import import_battle_dialogue as importer


class BattleDialogueImportTests(unittest.TestCase):
    """외부 자료를 저장소 산출물로 바꾸는 작은 순수 함수 테스트."""

    def test_semantic_normalization_handles_game_layout(self) -> None:
        value = "A１\u3001B\u3002C\u30fbD\u3000\nE\u25bd"
        self.assertEqual(
            importer._normalise_for_comparison(value, drop_spaces=True),
            "A1,B.C\u00b7D\\nE~",
        )

    def test_decoder_uses_first_byte_index(self) -> None:
        table = {
            b"\x81\x40": "\u3000",
            b"\x81\x49": "\uff01",
            b" ": " ",
        }
        replacement = {"\u3000": " ", "\uff01": "!"}
        decoder = importer._compile_decoder(table, replacement)
        self.assertEqual(
            importer._decode_payload(b"\x81\x40A\x81\x49\x00", decoder),
            " A!",
        )

    def test_output_cannot_escape_repository(self) -> None:
        with self.assertRaises(importer.BattleDialogueError):
            importer._safe_output(Path("..") / "outside.json")
        with self.assertRaises(importer.BattleDialogueError):
            importer._safe_output(Path("output") / "game.BIN")


if __name__ == "__main__":
    unittest.main()
