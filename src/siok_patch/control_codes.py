"""게임 대사의 제어 토큰을 원문 손상 없이 비교한다."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
import re


_SYMBOL_TOKENS = ("⑲", "⑳", "㊥", "㊦", "㊧", "㊨")
_DOLLARS = "$＄"
_LETTER_MAP = {
    "n": "n",
    "N": "n",
    "ｎ": "n",
    "Ｎ": "n",
    "l": "l",
    "L": "l",
    "ｌ": "l",
    "Ｌ": "l",
    "c": "c",
    "C": "c",
    "ｃ": "c",
    "Ｃ": "c",
    "f": "F",
    "F": "F",
    "ｆ": "F",
    "Ｆ": "F",
}
_TOKEN_PATTERN = re.compile(
    "[" + re.escape(_DOLLARS) + "][nNlLcCfFｎＮｌＬｃＣｆＦ]|["
    + "".join(_SYMBOL_TOKENS)
    + "]"
)


@dataclass(frozen=True, slots=True)
class ControlComparison:
    """두 문자열의 제어 토큰 비교 결과."""

    matches: bool
    source_tokens: tuple[str, ...]
    target_tokens: tuple[str, ...]
    missing: tuple[str, ...]
    extra: tuple[str, ...]
    order_changed: bool

    @property
    def source_signature(self) -> str:
        return _signature(self.source_tokens)

    @property
    def target_signature(self) -> str:
        return _signature(self.target_tokens)


def extract_control_tokens(text: str | None) -> tuple[str, ...]:
    """등장 순서대로 제어 토큰을 추출하고 전각 영문 변형만 정규화한다.

    문자열 전체에 NFKC 같은 정규화를 적용하지 않으므로 `⑲`가 `19`로
    변하거나 번역 문장이 달라지는 일이 없다.
    """

    if not text:
        return ()
    result: list[str] = []
    for match in _TOKEN_PATTERN.finditer(text):
        token = match.group(0)
        if token in _SYMBOL_TOKENS:
            result.append(token)
        else:
            result.append(f"${_LETTER_MAP[token[1]]}")
    return tuple(result)


def control_signature(text: str | None) -> str:
    """제어 토큰 순서와 개수가 드러나는 안정적인 JSON 서명을 만든다."""

    return _signature(extract_control_tokens(text))


def compare_control_tokens(
    source_text: str | None, target_text: str | None
) -> ControlComparison:
    """원문과 번역문의 제어 토큰 개수 및 순서를 모두 비교한다."""

    source = extract_control_tokens(source_text)
    target = extract_control_tokens(target_text)
    source_counts = Counter(source)
    target_counts = Counter(target)
    missing = tuple(
        token
        for token in _stable_token_order(source)
        for _ in range(max(0, source_counts[token] - target_counts[token]))
    )
    extra = tuple(
        token
        for token in _stable_token_order(target)
        for _ in range(max(0, target_counts[token] - source_counts[token]))
    )
    same_counts = not missing and not extra
    return ControlComparison(
        matches=source == target,
        source_tokens=source,
        target_tokens=target,
        missing=missing,
        extra=extra,
        order_changed=same_counts and source != target,
    )


def has_matching_control_tokens(
    source_text: str | None, target_text: str | None
) -> bool:
    """제어 토큰이 순서까지 같으면 참을 반환한다."""

    return compare_control_tokens(source_text, target_text).matches


def describe_control_mismatch(comparison: ControlComparison) -> str:
    """사람이 검수하기 쉬운 짧은 불일치 설명을 반환한다."""

    if comparison.matches:
        return ""
    if comparison.order_changed:
        return "제어 토큰의 순서가 바뀌었습니다."
    details: list[str] = []
    if comparison.missing:
        details.append("누락=" + ",".join(comparison.missing))
    if comparison.extra:
        details.append("추가=" + ",".join(comparison.extra))
    return "제어 토큰 불일치(" + "; ".join(details) + ")"


def _signature(tokens: tuple[str, ...]) -> str:
    return json.dumps(tokens, ensure_ascii=False, separators=(",", ":"))


def _stable_token_order(tokens: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(tokens))
