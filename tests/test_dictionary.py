"""사전 CPK XOR·태그·번역 JSON의 회귀 검사."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from siok_patch.dictionary import (
    encode_logical,
    find_field,
    logical_view,
    parse_member_fields,
    replace_member_fields,
)


ROOT = Path(__file__).resolve().parents[1]


def _has_source_text() -> bool:
    """공개 저장소 데이터에는 일본어 원문(sourceText)이 없다. 그때는 원문이
    있어야만 성립하는 검사를 건너뛴다."""

    sample = ROOT / "translations" / "dictionary" / "MtZkn_KW.json"
    if not sample.is_file():
        return False
    return '"sourceText"' in sample.open("rb").read(2 * 1024 * 1024).decode("utf-8", "ignore")


class DictionaryCodecTests(unittest.TestCase):
    def test_xor_round_trip_preserves_zero_and_literal_five_e(self) -> None:
        logical = b"ZKANKYWD\x00\x5E\x83\x5E\x00"
        self.assertEqual(logical_view(encode_logical(logical)), logical)

    def test_tag_parser_and_variable_repack(self) -> None:
        logical = (
            b"ZKANKYWD"
            + b"WORD"
            + (2).to_bytes(4, "little")
            + b"\x83\x5E"
            + b"SRCE"
            + (2).to_bytes(4, "little")
            + b"AB"
        )
        raw = encode_logical(logical)
        fields = parse_member_fields(raw, 0)
        self.assertEqual([field.tag for field in fields], ["WORD", "SRCE"])
        word = find_field(fields, "WORD")
        changed = replace_member_fields(raw, fields, {"WORD": b"\x83\x5E\x83\x5E\x83\x5E"})
        changed_fields = parse_member_fields(changed, 0)
        self.assertEqual(find_field(changed_fields, "WORD").capacity, 6)
        self.assertEqual(find_field(changed_fields, "WORD").payload, b"\x83\x5E\x83\x5E\x83\x5E")
        self.assertEqual(find_field(changed_fields, "SRCE").payload, b"AB")


class DictionaryDocumentTests(unittest.TestCase):
    @unittest.skipUnless(_has_source_text(), "공개 데이터에는 일본어 원문이 없어 건너뜀")
    def test_public_document_contains_all_source_translation_pairs(self) -> None:
        path = ROOT / "translations" / "dictionary" / "MtZkn_KW.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["format"], "siok.dictionary-dialogue")
        self.assertEqual(document["counts"]["members"], 141)
        self.assertEqual(document["counts"]["fields"], 564)
        self.assertEqual(len(document["entries"]), 564)
        self.assertEqual(document["localizedAsset"]["bytes"], 157722)
        self.assertRegex(document["localizedAsset"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(all(item["sourceText"] and item["translation"] for item in document["entries"]))
        self.assertTrue(all(item["appliedPayloadHex"] for item in document["entries"]))


if __name__ == "__main__":
    unittest.main()
