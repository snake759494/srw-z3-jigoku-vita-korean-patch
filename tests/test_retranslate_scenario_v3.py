import json
from pathlib import Path

from scripts.retranslate_scenario_v3 import (
    GlossaryIndex,
    control_tokens,
    normalize_punctuation,
    place_tokens_like_source,
    quality_penalty,
)


ROOT = Path(__file__).resolve().parents[1]


def test_glossary_exact_name_and_project_term_are_canonicalized():
    document = json.loads(
        (ROOT / "translations" / "retranslation" / "scenario_glossary_v2.json").read_text(
            encoding="utf-8"
        )
    )
    glossary = GlossaryIndex.from_document(document)
    value, _ = glossary.canonicalize("スズネ", "수즈네")
    assert value == "스즈네"
    value, _ = glossary.canonicalize("破界事変と再世戦争", "파계 사변과 재생 전쟁")
    assert value == "파계사변과 재세전쟁"


def test_control_tokens_keep_source_order_when_candidate_lost_tokens():
    source = "…どうしたの、$l君？$n"
    restored = place_tokens_like_source(source, "…무슨 일이야, 군?")
    assert control_tokens(restored) == ["$l", "$n"]
    assert restored.index("$l") < restored.index("$n")


def test_punctuation_normalization_does_not_remove_controls():
    source = "あれ…！？$l"
    value = normalize_punctuation(source, "저건 ...! ? $l")
    assert "…" in value
    assert control_tokens(value) == ["$l"]


def test_quality_penalty_detects_truncated_or_literal_candidate():
    assert quality_penalty("…", "長い日本語の문장입니다") >= 500
    assert quality_penalty("센티미터로 갈 수 없어", "センチでいけねえな") > 0
