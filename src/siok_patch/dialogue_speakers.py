"""시나리오 대사 행 중 화자명 행을 가려낸다.

뷰어(``viewer/시나리오_대사_뷰어.html``)의 ``isSpeakerEntry``와 같은 판정을
파이썬에서 쓰기 위한 모듈이다. 판단 순서도 뷰어와 같다.

1. ``viewer/scenario-speakers.json`` 카탈로그에 있으면 그대로 따른다.
   본인 소유 게임의 원본 CPK에서 뽑은 ``SP/SB/SF/SG`` 슬롯 경계라
   가장 믿을 만하다.
2. 카탈로그가 없는 멤버(현재 DLC 30편)만 뷰어와 같은 추정 규칙을 쓴다.

두 구현이 갈라지면 일괄 바꾸기가 뷰어와 다르게 동작하므로,
``tests/test_dialogue_speakers.py``가 규칙을 고정해 둔다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "SpeakerCatalog",
    "clean_text",
    "control_tokens",
    "has_visible_text",
    "is_speaker_entry",
    "load_speaker_catalog",
    "visible_text",
]

# 아래 정규식·목록은 뷰어와 글자 단위로 같아야 한다.
CONTROL_RE = re.compile(r"(⑲|⑳|㊥|㊦|㊧|㊨|(?:[$＄&][nNlLcCfFｎＮｌＬｃＣｆＦ]))")
TERM_MARK_RE = re.compile(r"[《》]")
FRAME_MARK_RE = re.compile(r"[「」]")
SOURCE_SENTENCE_RE = re.compile(
    "[\\s　、。，．！？…:："
    ";；「」『』《》〈〉（）]"
)
SOURCE_CLAUSE_END_RE = re.compile(
    "(?:は|が|を|に|へ|と|で|の|も|"
    "から|まで|より|って|ので|"
    "だ|です|ます|ない|なる|する|"
    "だな|かな|よな|だろ|だろう|"
    "ね|よ|ぞ|さ|ぜ)$"
)
KNOWN_SPEAKERS = frozenset(
    {
        "신", "신 아스카", "키라", "키라 야마토", "아스란", "카가리", "카미유", "아무로",
        "로저", "도로시", "모므", "크와트로", "샤아", "세츠나", "히이로", "스즈네",
        "아임・라이어드", "아임", "케이", "코우지", "테츠야", "사이토", "시몬", "요코",
        "미코노", "알토", "쉐릴", "란카", "다나카", "히비키", "카나메", "쿄코",
        "소스케", "마오", "쇼타로", "마키", "쿠르츠", "???", "？？？",
    }
)

CATALOG_FORMAT = "siok.scenario-speaker-catalog"


def visible_text(value: object) -> str:
    return CONTROL_RE.sub("", "" if value is None else str(value))


def has_visible_text(value: object) -> bool:
    stripped = FRAME_MARK_RE.sub("", TERM_MARK_RE.sub("", visible_text(value)))
    return len(stripped.strip()) > 0


def clean_text(value: object) -> str:
    without = CONTROL_RE.sub("", "" if value is None else str(value))
    return FRAME_MARK_RE.sub("", TERM_MARK_RE.sub("", without)).strip()


def control_tokens(value: object) -> list[str]:
    return CONTROL_RE.findall("" if value is None else str(value))


class SpeakerCatalog:
    """``ASSET/MEMBER`` 별 화자 행 번호 모음."""

    __slots__ = ("_members",)

    def __init__(self, members: Mapping[str, Iterable[int]] | None = None) -> None:
        self._members: dict[str, frozenset[int]] = {}
        for key, rows in (members or {}).items():
            numbers = {
                int(row)
                for row in rows
                if isinstance(row, (int, float)) and int(row) == row and int(row) > 0
            }
            self._members[str(key).upper()] = frozenset(numbers)

    def __len__(self) -> int:
        return len(self._members)

    def member_key(self, entry: Mapping[str, Any]) -> str:
        location = entry.get("location")
        if not isinstance(location, Mapping):
            return ""
        asset = str(location.get("assetKey") or entry.get("assetKey") or "").strip().upper()
        member = str(location.get("internalId") or "").strip().upper()
        row = location.get("sourceRow")
        if not asset or not member:
            return ""
        if not isinstance(row, int) or isinstance(row, bool) or row < 1:
            return ""
        return f"{asset}/{member}"

    def covers(self, entry: Mapping[str, Any]) -> bool:
        key = self.member_key(entry)
        return bool(key) and key in self._members

    def is_speaker(self, entry: Mapping[str, Any]) -> bool:
        key = self.member_key(entry)
        if not key:
            return False
        rows = self._members.get(key)
        if rows is None:
            return False
        location = entry.get("location")
        row = location.get("sourceRow") if isinstance(location, Mapping) else None
        return isinstance(row, int) and not isinstance(row, bool) and row in rows


def load_speaker_catalog(path: Path) -> SpeakerCatalog:
    """뷰어용 화자 카탈로그를 읽는다. 없으면 빈 카탈로그를 돌려준다."""

    if not path.is_file():
        return SpeakerCatalog()
    document = json.loads(path.read_text(encoding="utf-8"))
    if str(document.get("format") or "") != CATALOG_FORMAT:
        raise ValueError(f"화자 카탈로그 형식이 올바르지 않습니다: {path}")
    members = document.get("members")
    if not isinstance(members, Mapping):
        raise ValueError(f"화자 카탈로그에 members가 없습니다: {path}")
    return SpeakerCatalog(members)


def _guess_speaker_entry(
    entry: Mapping[str, Any], index: int, entries: Sequence[Mapping[str, Any]]
) -> bool:
    """카탈로그가 없는 멤버에 쓰는 추정 규칙. 뷰어와 같은 순서로 판단한다."""

    raw = str(entry.get("translation") or entry.get("sourceText") or "").strip()
    token_only = bool(raw) and "".join(control_tokens(raw)) == raw
    text = clean_text(raw)
    if token_only:
        following = entries[index + 1] if index + 1 < len(entries) else None
        if following is None:
            return False
        return has_visible_text(following.get("translation") or following.get("sourceText"))
    if not text or len(text) > 24 or re.search(r"[.!?。！？,，、…]", text):
        return False
    if text in KNOWN_SPEAKERS:
        return True
    source = clean_text(entry.get("sourceText"))
    if SOURCE_SENTENCE_RE.search(source) or SOURCE_CLAUSE_END_RE.search(source):
        return False
    following = entries[index + 1] if index + 1 < len(entries) else None
    next_text = (
        clean_text(following.get("translation") or following.get("sourceText"))
        if following is not None
        else ""
    )
    return len(source) <= 12 and len(text) <= 18 and bool(next_text) and len(next_text) > len(text)


def is_speaker_entry(
    entry: Mapping[str, Any],
    index: int,
    entries: Sequence[Mapping[str, Any]],
    catalog: SpeakerCatalog | None = None,
) -> bool:
    """뷰어의 ``isSpeakerEntry``와 같은 판정을 돌려준다."""

    if catalog is not None and catalog.is_speaker(entry):
        return True
    return _guess_speaker_entry(entry, index, entries)
