"""SRVC JSON만으로 고정 슬롯 패치를 만드는 도구의 왕복 검사."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts import apply_battle_dialogue_patch as patcher


class ApplyBattleDialoguePatchTests(unittest.TestCase):
    """원본 해시·슬롯 해시·결과 해시가 모두 묶이는지 확인한다."""

    def test_json_only_patch_round_trip(self) -> None:
        original = b"ORIGINAL-BYTES"
        offset = 2
        size = 6
        replacement = b"PATCH!"
        expected_source = original[offset : offset + size]
        result = original[:offset] + replacement + original[offset + size :]

        with tempfile.TemporaryDirectory(dir=patcher.REPOSITORY_ROOT / "work") as name:
            root = Path(name)
            original_path = root / "SRVC-ori.BIN"
            json_path = root / "SRVC_BATTLE.json"
            output_path = root / "SRVC-patched.BIN"
            report_path = root / "SRVC-patched.report.json"
            original_path.write_bytes(original)
            document = {
                "format": "siok.srvc-battle-dialogue",
                "formatVersion": 2,
                "gameId": "PCSG00264",
                "policy": {"requiresUserOwnedGame": True},
                "asset": {
                    "fileName": "SRVC.BIN",
                    "bytes": len(original),
                    "originalSha256": hashlib.sha256(original).hexdigest(),
                    "appliedSha256": hashlib.sha256(result).hexdigest(),
                },
                "counts": {"masterRows": 1, "slotRows": 1},
                "master": [
                    {
                        "id": 1,
                        "offset": offset,
                        "sourceText": "원문",
                        "translation": "번역",
                        "sourceTextSha256": hashlib.sha256("원문".encode()).hexdigest(),
                        "sourceByteLength": size,
                        "originalPayloadSha256": hashlib.sha256(expected_source).hexdigest(),
                        "appliedPayloadSha256": hashlib.sha256(replacement).hexdigest(),
                        "appliedPayloadHex": replacement.hex(),
                    }
                ],
                "slots": [
                    {
                        "slot": 1,
                        "offset": offset,
                        "masterId": 1,
                        "sourceByteLength": size,
                        "applied": True,
                    }
                ],
            }
            json_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

            report = patcher.apply_patch(
                document_path=json_path,
                original_path=original_path,
                output_path=output_path,
                report_path=report_path,
                overwrite=False,
            )

            self.assertEqual(output_path.read_bytes(), result)
            self.assertEqual(report["output"]["sha256"], hashlib.sha256(result).hexdigest())
            self.assertTrue(report_path.is_file())


if __name__ == "__main__":
    unittest.main()
