from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_control_tokens_are_split_and_reassembled() -> None:
    module = _load("retranslate_scenario_offline_test", ROOT / "scripts" / "retranslate_scenario_offline.py")
    source = "（…悪いな、$n。㊦"
    entry = {"entryId": "x", "sourceText": source, "sourceTextSha256": module.sha256_text(source), "metadata": {}}
    work = module.make_entry_work(entry)
    translated, misses = module.join_entry(work, {part.task_id: "미안하다" for part in work.parts if isinstance(part, module.TextPart) and part.task_id is not None})
    assert misses == 0
    assert module.source_tokens(source) == module.source_tokens(translated)
    assert translated.endswith("㊦")


def test_canonical_terms_do_not_change_unrelated_lines() -> None:
    module = _load("apply_scenario_retranslation_v2_test", ROOT / "scripts" / "apply_scenario_retranslation_v2.py")
    assert module.canonicalize("時獄戦役", "감옥 전쟁이 시작된다") == "시옥전쟁이 시작된다"
    assert module.canonicalize("普通の台詞", "감옥 전쟁") == "감옥 전쟁"


def test_blank_or_marker_miss_uses_reference_and_repairs_controls() -> None:
    module = _load("apply_scenario_retranslation_v2_fallback_test", ROOT / "scripts" / "apply_scenario_retranslation_v2.py")
    entry = {
        "sourceText": "次元の檻을 통과한다㊦㊦",
        "translation": "",
        "references": {"importedTranslation": "차원의 새장을 통과한다"},
    }
    blank, used = module.resolve_translation(entry, {"translation": ""})
    assert used is True
    assert blank.endswith("㊦㊦")
    assert module.tokens(blank) == ["㊦", "㊦"]

    marker, used = module.resolve_translation(
        entry,
        {"translation": "이름이 사라진 문장㊦㊦", "markerMisses": 1},
    )
    assert used is True
    assert marker.startswith("차원의 새장을")
    assert module.tokens(marker) == ["㊦", "㊦"]
