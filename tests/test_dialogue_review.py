"""공통 대사 검수 JSON과 전용 실행기 연결 회귀 검사."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]

# 패치 스크립트가 공유 라이브러리(src/siok_patch)를 불러오므로 테스트
# 로더보다 먼저 저장소의 src 경로를 파이썬 모듈 검색 경로에 추가한다.
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"스크립트를 읽을 수 없습니다: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _has_source_text() -> bool:
    """공개 저장소 데이터에는 일본어 원문(sourceText)이 없다. 그때는 원문이
    있어야만 성립하는 검사를 건너뛴다."""

    sample = ROOT / "translations" / "dialogue" / "scenario_STG0001a.json"
    if not sample.is_file():
        return False
    return '"sourceText"' in sample.open("rb").read(2 * 1024 * 1024).decode("utf-8", "ignore")


class DialogueReviewBundleTests(unittest.TestCase):
    @unittest.skipUnless(_has_source_text(), "공개 데이터에는 일본어 원문이 없어 건너뜀")
    def test_bundle_has_one_common_format_for_all_three_types(self) -> None:
        checker = _load_script("dialogue_review_checker", ROOT / "scripts" / "check_dialogue_review_bundle.py")
        report = checker.check_bundle(ROOT / "translations" / "dialogue")
        self.assertEqual(report["files"], 176)
        self.assertEqual(report["scenarioFiles"], 174)
        self.assertEqual(report["battleFiles"], 1)
        self.assertEqual(report["dictionaryFiles"], 1)
        self.assertEqual(report["entries"], 197068)

    @unittest.skipUnless(_has_source_text(), "공개 데이터에는 일본어 원문이 없어 건너뜀")
    def test_common_battle_and_dictionary_documents_are_accepted(self) -> None:
        battle = _load_script("battle_patch_review", ROOT / "scripts" / "apply_battle_dialogue_patch.py")
        dictionary = _load_script("dictionary_patch_review", ROOT / "scripts" / "apply_dictionary_patch.py")
        battle_metadata, masters, slots = battle._load_document(ROOT / "translations" / "dialogue" / "battle_SRVC.json")
        dictionary_metadata, entries = dictionary._load_document(ROOT / "translations" / "dialogue" / "dictionary_MtZkn_KW.json")
        self.assertEqual(len(masters), 25758)
        self.assertEqual(len(slots), 31983)
        self.assertEqual(dictionary_metadata["assetSize"], 131143)
        self.assertEqual(len(entries), 564)


if __name__ == "__main__":
    unittest.main()
