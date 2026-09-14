import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from siok_patch.retranslation import (
    OVERLAY_FORMAT,
    apply_retranslation_overlay,
    apply_retranslation_overlays,
    export_retranslation_task,
    load_retranslation_progress,
    publish_retranslation_task,
    source_binding_sha256,
    source_text_sha256,
    validate_retranslation_overlay,
    validate_retranslation_overlays,
)
from siok_patch.hashes import sha256_file
from siok_patch.translation_io import TranslationRow, read_tsv, write_tsv


def _row(entry_id: str, source_text: str, *, source_row: int) -> TranslationRow:
    return TranslationRow(
        entry_id=entry_id,
        scope="stage",
        asset_key="STG0001a",
        internal_id="ID00003",
        source_row=source_row,
        source_artifact="stage/STG0001a.xlsx",
        source_artifact_sha256="1" * 64,
        payload_sha256="2" * 64,
        source_offset=None,
        source_text=source_text,
        google_translation="구글 참고",
        legacy_translation="기존 참고",
        translation="가져온 번역",
        replacement_text="기존 치환",
        byte_limit=52,
        encoded_length=10,
        control_signature="[]",
        status="imported",
        reviewer="기존 검수자",
        notes="기존 메모",
    )


class RetranslationTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "work" / "normalized" / "translations.tsv"
        self.rows = [
            _row("STG0001a/ID00003/000002", "シン$n", source_row=2),
            _row("STG0001a/ID00003/000003", "見ろよ！", source_row=3),
        ]
        write_tsv(self.rows, self.source)

    def _overlay(self, *, first_translation: str = "신$n") -> Path:
        path = self.root / "translations" / "retranslation" / "STG0001a.json"
        path.parent.mkdir(parents=True)
        data = {
            "format": OVERLAY_FORMAT,
            "formatVersion": 1,
            "scope": "stage",
            "assetKey": "STG0001a",
            "baseline": {
                "sha256": sha256_file(self.source),
                "rowCount": len(self.rows),
            },
            "translationPolicy": {
                "method": "fresh-from-japanese",
                "existingTranslations": "reference-only",
            },
            "entries": [
                {
                    "entryId": self.rows[0].entry_id,
                    "sourceBindingSha256": source_binding_sha256(self.rows[0]),
                    "sourceTextSha256": source_text_sha256(self.rows[0].source_text),
                    "freshTranslation": first_translation,
                    "translationStatus": "reviewed",
                    "translator": "Codex",
                    "reviewer": "검수자",
                    "referenceConsulted": True,
                    "notes": "원문 기준 신규 번역",
                },
                {
                    "entryId": self.rows[1].entry_id,
                    "sourceBindingSha256": source_binding_sha256(self.rows[1]),
                    "sourceTextSha256": source_text_sha256(self.rows[1].source_text),
                    "freshTranslation": "봐!",
                    "translationStatus": "reviewed",
                    "translator": "Codex",
                    "reviewer": "검수자",
                    "referenceConsulted": False,
                    "notes": "",
                },
            ],
        }
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_작업_JSON은_원문과_기존번역을_참조로만_내보낸다(self) -> None:
        output = self.root / "work" / "retranslation" / "tasks" / "STG0001a.json"

        report = export_retranslation_task(
            self.source,
            scope="stage",
            asset_key="STG0001a",
            output_path=output,
            root=self.root,
        )

        task = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["rowCount"], 2)
        self.assertEqual(task["sourceSnapshot"]["rowCount"], 2)
        self.assertEqual(task["translationPolicy"]["existingTranslations"], "reference-only")
        self.assertEqual(task["entries"][0]["sourceText"], "シン$n")
        self.assertEqual(
            task["entries"][0]["sourceBindingSha256"],
            source_binding_sha256(self.rows[0]),
        )
        self.assertEqual(task["entries"][0]["references"]["legacyTranslation"], "기존 참고")
        self.assertEqual(task["entries"][0]["freshTranslation"], "")

        with self.assertRaisesRegex(ValueError, "덮어쓰지"):
            export_retranslation_task(
                self.source,
                scope="stage",
                asset_key="STG0001a",
                output_path=output,
                root=self.root,
            )

    def test_오버레이는_원문_해시와_제어코드를_검사한다(self) -> None:
        overlay = self._overlay(first_translation="신")

        report = validate_retranslation_overlay(self.source, overlay)

        self.assertFalse(report["ok"])
        self.assertIn("control-code-mismatch", {item["code"] for item in report["errors"]})

    def test_공개_오버레이에_원문이나_기존번역_필드를_허용하지_않는다(self) -> None:
        overlay = self._overlay()
        data = json.loads(overlay.read_text(encoding="utf-8"))
        data["entries"][0]["sourceText"] = "シン$n"
        overlay.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        report = validate_retranslation_overlay(self.source, overlay)

        self.assertFalse(report["ok"])
        self.assertIn("private-field-in-overlay", {item["code"] for item in report["errors"]})

    def test_병합은_기존_참고열을_보존하고_치환문을_비운다(self) -> None:
        overlay = self._overlay()
        output = self.root / "work" / "retranslation" / "merged" / "translations.tsv"
        baseline = sha256_file(self.source)

        report = apply_retranslation_overlay(
            self.source,
            overlay,
            output_path=output,
            root=self.root,
        )

        merged = read_tsv(output)
        self.assertEqual(report["appliedRows"], 2)
        self.assertEqual(report["baselineSha256"], baseline)
        self.assertEqual(sha256_file(self.source), baseline)
        self.assertEqual(merged[0].translation, "신$n")
        self.assertEqual(merged[0].google_translation, "구글 참고")
        self.assertEqual(merged[0].legacy_translation, "기존 참고")
        self.assertEqual(merged[0].replacement_text, "")
        self.assertIsNone(merged[0].encoded_length)
        self.assertEqual(merged[0].status, "review")
        self.assertEqual(merged[0].reviewer, "검수자")
        self.assertIn("기존번역=참조전용", merged[0].notes)

    def test_새로_번역하지_않은_행은_기존_활성번역을_사용하지_않는다(self) -> None:
        overlay = self._overlay()
        data = json.loads(overlay.read_text(encoding="utf-8"))
        data["entries"] = data["entries"][:1]
        overlay.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        output = self.root / "work" / "retranslation" / "merged" / "translations.tsv"

        report = apply_retranslation_overlay(
            self.source,
            overlay,
            output_path=output,
            root=self.root,
        )

        merged = read_tsv(output)
        self.assertEqual(report["pendingRows"], 1)
        self.assertEqual(report["buildStatus"], "blocked")
        self.assertEqual(merged[1].translation, "")
        self.assertEqual(merged[1].replacement_text, "")
        self.assertEqual(merged[1].status, "blocked")
        self.assertIn("재번역=미작성", merged[1].notes)

    def test_여러_오버레이를_한_병합본에_누적한다(self) -> None:
        first = self._overlay()
        first_data = json.loads(first.read_text(encoding="utf-8"))
        second_data = dict(first_data)
        first_data["entries"] = first_data["entries"][:1]
        second_data["entries"] = second_data["entries"][1:]
        first.write_text(json.dumps(first_data, ensure_ascii=False), encoding="utf-8")
        second = first.with_name("STG0001a-part2.json")
        second.write_text(json.dumps(second_data, ensure_ascii=False), encoding="utf-8")
        output = self.root / "work" / "retranslation" / "merged" / "translations.tsv"

        report = apply_retranslation_overlays(
            self.source,
            [first, second],
            output_path=output,
            root=self.root,
        )

        merged = read_tsv(output)
        self.assertEqual(report["overlayCount"], 2)
        self.assertEqual(report["appliedRows"], 2)
        self.assertEqual(report["pendingRows"], 0)
        self.assertEqual(report["buildStatus"], "review")
        self.assertEqual([row.translation for row in merged], ["신$n", "봐!"])

    def test_알_수_없는_최상위_필드를_거부한다(self) -> None:
        overlay = self._overlay()
        data = json.loads(overlay.read_text(encoding="utf-8"))
        data["legacyFallback"] = True
        overlay.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        report = validate_retranslation_overlay(self.source, overlay)

        self.assertFalse(report["ok"])
        self.assertIn("unknown-overlay-field", {item["code"] for item in report["errors"]})

    def test_작업_JSON에서_비공개_필드를_제거해_오버레이를_게시한다(self) -> None:
        task_path = self.root / "work" / "retranslation" / "tasks" / "stage" / "STG0001a.json"
        export_retranslation_task(
            self.source,
            scope="stage",
            asset_key="STG0001a",
            output_path=task_path,
            root=self.root,
        )
        task = json.loads(task_path.read_text(encoding="utf-8"))
        for entry, translation in zip(task["entries"], ("신$n", "봐!"), strict=True):
            entry["freshTranslation"] = translation
            entry["translationStatus"] = "draft"
            entry["translator"] = "Codex"
            entry["referenceConsulted"] = True
        task_path.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        output = self.root / "translations" / "retranslation" / "stage" / "STG0001a.json"

        report = publish_retranslation_task(
            task_path,
            output_path=output,
            root=self.root,
        )

        overlay = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["rowCount"], 2)
        self.assertEqual(overlay["baseline"]["sha256"], sha256_file(self.source))
        self.assertNotIn("sourceText", overlay["entries"][0])
        self.assertNotIn("references", overlay["entries"][0])
        self.assertEqual(overlay["entries"][0]["freshTranslation"], "신$n")

    def test_재번역_출력은_보호된_전용_폴더만_허용한다(self) -> None:
        forbidden_task = self.root / "work" / "normalized" / "task.json"
        with self.assertRaisesRegex(ValueError, "tasks"):
            export_retranslation_task(
                self.source,
                scope="stage",
                asset_key="STG0001a",
                output_path=forbidden_task,
                root=self.root,
            )

        overlay = self._overlay()
        with self.assertRaisesRegex(ValueError, "merged"):
            apply_retranslation_overlay(
                self.source,
                overlay,
                output_path=self.root / "work" / "normalized" / "translations.tsv",
                root=self.root,
            )

    def test_기준_TSV_전체_해시와_행_수가_바뀌면_거부한다(self) -> None:
        overlay = self._overlay()
        data = json.loads(overlay.read_text(encoding="utf-8"))
        data["baseline"]["sha256"] = "0" * 64
        data["baseline"]["rowCount"] = 999
        overlay.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        report = validate_retranslation_overlay(self.source, overlay)

        codes = {item["code"] for item in report["errors"]}
        self.assertIn("baseline-hash-mismatch", codes)
        self.assertIn("baseline-row-count-mismatch", codes)

    def test_draft는_병합_TSV의_활성_번역으로_승격하지_않는다(self) -> None:
        overlay = self._overlay()
        data = json.loads(overlay.read_text(encoding="utf-8"))
        data["entries"][0]["translationStatus"] = "draft"
        data["entries"][0]["reviewer"] = ""
        overlay.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        output = self.root / "work" / "retranslation" / "merged" / "translations.tsv"

        report = apply_retranslation_overlay(
            self.source,
            overlay,
            output_path=output,
            root=self.root,
        )

        merged = read_tsv(output)
        self.assertEqual(report["draftRows"], 1)
        self.assertEqual(report["appliedRows"], 1)
        self.assertEqual(merged[0].translation, "")
        self.assertEqual(merged[0].status, "blocked")
        self.assertEqual(merged[1].translation, "봐!")

    def test_오버레이_간_중복_entry_id를_check에서도_거부한다(self) -> None:
        first = self._overlay()
        second = first.with_name("duplicate.json")
        second.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")

        report = validate_retranslation_overlays(self.source, [first, second])

        self.assertFalse(report["ok"])
        self.assertIn(
            "duplicate-entry-across-overlays",
            {item["code"] for item in report["errors"]},
        )

    def test_reviewed에는_별도_검수자가_필요하다(self) -> None:
        overlay = self._overlay()
        data = json.loads(overlay.read_text(encoding="utf-8"))
        data["entries"][0]["reviewer"] = ""
        overlay.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        report = validate_retranslation_overlay(self.source, overlay)

        self.assertIn("missing-reviewer", {item["code"] for item in report["errors"]})

    def test_진행_매니페스트가_전체_오버레이와_기준본을_결박한다(self) -> None:
        overlay = self._overlay()
        progress_path = self.root / "translations" / "retranslation" / "progress.json"
        progress = {
            "format": "siok-retranslation-progress",
            "formatVersion": 1,
            "baseline": {
                "sha256": sha256_file(self.source),
                "rowCount": len(self.rows),
            },
            "policy": {
                "method": "fresh-from-japanese",
                "existingTranslations": "reference-only",
                "automaticLegacyFallback": False,
            },
            "assets": [{"overlay": overlay.name}],
        }
        progress_path.write_text(json.dumps(progress), encoding="utf-8")

        loaded = load_retranslation_progress(
            self.source,
            progress_path,
            root=self.root,
        )

        self.assertEqual(loaded["overlayPaths"], [overlay.resolve()])
        self.assertEqual(loaded["baselineSha256"], sha256_file(self.source))


if __name__ == "__main__":
    unittest.main()
