import unittest

from siok_patch.scenario_cpk import (
    ScenarioCpkBuildError,
    _encode_payload_text,
    _encode_translation,
    _patch_operation_payload,
    _resolve_group_output_ids,
)


class ScenarioCpkOutputMappingTest(unittest.TestCase):
    def test_direct_member_is_not_overwritten_by_file_alias(self) -> None:
        groups = {(3, 3): [], (3, 4): []}

        self.assertEqual(
            _resolve_group_output_ids(groups),
            {(3, 3): 3, (3, 4): 4},
        )

    def test_alias_without_direct_target_keeps_requested_target(self) -> None:
        self.assertEqual(
            _resolve_group_output_ids({(3, 4): []}),
            {(3, 4): 3},
        )

    def test_ambiguous_alias_is_rejected(self) -> None:
        with self.assertRaises(ScenarioCpkBuildError):
            _resolve_group_output_ids({(3, 3): [], (3, 4): [], (4, 4): []})


class ScenarioGameGlyphFallbackTest(unittest.TestCase):
    def test_missing_glyphs_are_only_fallbacks_for_cpk_bytes(self) -> None:
        converted = _encode_translation("\ud01c\u2661", {"\ud034": "\u8a02"})

        self.assertNotIn("\ud01c", converted)
        self.assertNotIn("\u2661", converted)
        self.assertEqual(_encode_payload_text(converted), bytes.fromhex("92f98471"))


class ScenarioConditionPatchTest(unittest.TestCase):
    def test_operate_table_strings_are_replaced_by_source_id(self) -> None:
        payload = (
            'OPERATE_TBL = {\r\n'
            '  str_tbl = {\r\n'
            '    -- ID : 0\r\n'
            '    "敵の全滅。",\r\n'
            '  };\r\n'
            '  id_tbl = {\r\n'
            '  };\r\n'
            '} -- end\r\n'
        ).encode("cp932")
        output, unmatched, patched = _patch_operation_payload(
            payload,
            [{"sourceId": 0, "sourceText": "敵の全滅。", "translation": "적의 전멸"}],
            {"적": "旋", "의": "税", " ": "　", "전": "穿", "멸": "瑚"},
            details=True,
        )

        self.assertEqual((unmatched, patched), (0, 1))
        self.assertIn('"旋税　穿瑚"', output.decode("cp932"))

    def test_missing_operation_id_is_reported(self) -> None:
        payload = (
            'OPERATE_TBL = {\r\n'
            '  str_tbl = {\r\n'
            '    -- ID : 0\r\n'
            '    "既存",\r\n'
            '  };\r\n'
            '}\r\n'
        ).encode("cp932")
        output, unmatched, patched = _patch_operation_payload(
            payload,
            [{"sourceId": 7, "sourceText": "없음", "translation": "없음"}],
            {},
            details=True,
        )
        self.assertEqual(output, payload)
        self.assertEqual((unmatched, patched), (1, 0))


if __name__ == "__main__":
    unittest.main()
