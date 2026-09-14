"""통합 패치 매니페스트와 입력 경계의 회귀 검사."""

from __future__ import annotations

import unittest

from scripts import apply_release_patch as runner


class ReleasePatchTests(unittest.TestCase):
    def test_public_manifest_has_three_groups_and_unique_targets(self) -> None:
        manifest = runner._load_manifest(runner.DEFAULT_MANIFEST)
        entries = runner._selected_entries(manifest, "all")
        targets = {(item["root"], item["target"]) for item in entries}
        self.assertEqual(len(entries), 253)
        self.assertEqual(len(targets), len(entries))
        self.assertEqual(sum(item["kind"] == "battle-json" for item in entries), 1)
        self.assertEqual(sum(item["kind"] == "dictionary-json" for item in entries), 1)
        self.assertEqual(sum(item["group"] == "scenario-dialogue" for item in entries), 182)
        self.assertIn(
            ("app", "DATA/kurodata/KDataVITA.cpk"),
            targets,
        )
        self.assertIn(("app", "DATA/BTLC/SRVC.BIN"), targets)
        self.assertIn(("app", "CommonData/MtData/MtZkn_KW.cpk"), targets)
        self.assertTrue(all(item["root"] in {"app", "dlc"} for item in entries))

    def test_relative_path_rejects_traversal_and_windows_paths(self) -> None:
        with self.assertRaises(runner.ReleasePatchError):
            runner._safe_relative("../outside.bin", "target")
        with self.assertRaises(runner.ReleasePatchError):
            runner._safe_relative(r"C:\\outside.bin", "target")

    def test_real_vita_profile_is_explicitly_blocked(self) -> None:
        with self.assertRaises(runner.ReleasePatchError):
            runner.apply_release(
                game_root=runner.REPOSITORY_ROOT,
                dlc_root=None,
                xdelta_path=runner.DEFAULT_XDELTA,
                profile="vita",
            )


if __name__ == "__main__":
    unittest.main()
