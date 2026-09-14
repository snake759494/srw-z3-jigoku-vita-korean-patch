#!/usr/bin/env python3
"""용어사전과 이전 손번역을 참고해 시나리오 JSON을 재구성한다.

이 도구는 외부 번역 서비스나 게임 롬을 사용하지 않는다. 현재 시나리오 JSON의
일본어 원문과 참고 후보, Git에 보존된 이전 손번역을 읽고 다음을 수행한다.

* 용어사전의 고유명사·세계관 용어를 문장 안에서 정규화한다.
* series_reference.json의 참전작 공식 제목을 보조 표기로 정규화 후보에 포함한다.
* 이전 손번역을 기본 후보로 삼고, 일본어 잔존·제어문자 손상·표시 한도 초과 시
  다른 로컬 참고 후보를 비교한다.
* 제어문자의 종류·개수·순서를 원문과 맞추고, 토큰의 대략적인 위치도 보존한다.
* 변경 전후의 잔여 일본어·길이·제어문자 통계를 JSON 보고서로 남긴다.

기본 실행은 보고서만 만들며, ``--apply``를 붙였을 때만
``translations/dialogue/scenario_*.json``을 갱신한다. 결과는 사람이 검토해야 하는
``draft`` 상태로 저장한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIALOGUE_ROOT = ROOT / "translations" / "dialogue"
DEFAULT_GLOSSARY = ROOT / "translations" / "retranslation" / "scenario_glossary_v2.json"
DEFAULT_SERIES_REFERENCE = ROOT / "translations" / "retranslation" / "series_reference.json"
DEFAULT_REPORT = ROOT / "work" / "retranslation-v3" / "qa-report.json"
DEFAULT_BASELINE_COMMIT = "156096c"

# $는 문자 클래스 안에 넣어 PowerShell/셸 보간과 무관하게 그대로 매칭한다.
CONTROL_RE = re.compile(r"(?:⑲|⑳|㊥|㊦|㊧|㊨|[$＄][nlcF])")
JAPANESE_RE = re.compile(r"[ぁ-ゖァ-ヺ一-龯々〆ヵヶ]")
MARKER_RE = re.compile(r"(?:SIKMARK|__TRANSLATION|<TRANSLATION_ERROR>)")
SPACE_RE = re.compile(r"[ \t\u3000]+")

KIND_PRIORITY = {
    "project_term": 120,
    "term": 110,
    "character": 105,
    "pilot": 103,
    "robot": 100,
    "weapon": 98,
    "keyword_dictionary": 95,
    "keyword_definition": 94,
    "pilot_string": 90,
    "robot_string": 88,
    "shared_string": 60,
    "series_reference": 85,
}

# 원문에 따라 의미가 달라지는 용어는 전역 치환하지 않는다.
SOURCE_TERM_OVERRIDES: dict[str, tuple[tuple[str, str], ...]] = {
    "インサラウム": (("인살라움", "인사라움"),),
    "破界事変": (("파계 사변", "파계사변"), ("파계 전쟁", "파계사변")),
    "再世戦争": (("재세 전쟁", "재세전쟁"), ("재생 전쟁", "재세전쟁")),
    "時獄戦役": (("시옥 전쟁", "시옥전쟁"), ("시옥 전역", "시옥전쟁")),
    "時空震動": (("차원진동", "시공진동"), ("시공 진동", "시공진동")),
    "大時空震動": (("대차원진동", "대시공진동"), ("대 시공진동", "대시공진동")),
    "次元震": (("시공진동", "차원진동"),),
    "ＺＥＵＴＨ": (("ZEUTH", "ZEUTH"),),
    "ＺＥＸＩＳ": (("ZEXIS", "ZEXIS"),),
    "Ｚ－ＢＬＵＥ": (("Z-BLUE", "Z-BLUE"),),
}

# 이전 초안에서 반복적으로 확인된 직역·오인식 표현이다. 이 목록은 번역기를
# 호출하기 위한 사전이 아니라, 이미 보존된 로컬 후보 중 자연스러운 문장을
# 선택하기 위한 감점 규칙이다. 후보 양쪽이 모두 걸리면 문장 전체를 draft로
# 남기고, 더 높은 점수의 후보를 유지한다.
QUALITY_BAD_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        r"센티미터", r"높은 견해", r"고위 관측", r"생각했던 곳에서", r"바반",
        r"그림 별", r"입을 내", r"^무엇(?:이|인가)?[?？]?$", r"^무엇[?？]?$",
        r"^어땠어[?？]?$", r"포기하고 싶니", r"와타시", r"^살아남다[.!。]?$",
        r"기분이 좋거나", r"그것을 알고, 어떻게 될까요", r"쓰고 싶다고 생각",
        r"업어 주려고", r"성분이 아니네", r"노노노우", r"입으로 말해도 모르면",
        r"그들의 무능", r"저녀석 등", r"순 일본식", r"^그녀[!?？]?$",
        r"^그래서 무엇", r"그런 일을 하고 무슨 의미", r"기억해 버린다",
        r"그 정도밖에 하는 일이", r"여자라면", r"네엔이야", r"무슨 일이야\?$",
        r"^살아나다[.!。]?$", r"기분이 좋거나 화가", r"나에게 준비시킨 것으로부터",
    )
)


SOURCE_PHRASE_OVERRIDES: dict[str, str] = {
    "ばばーんと俺様とジェミニスが登場！": "짜잔 하고 나님과 제미니스가 등장!",
    "ま…それまで俺達は、ここで高みの見物といこうぜ": "뭐… 그때까지 우리는 여기서 구경이나 하자고.",
    "…女ってのは、どうにもセンチでいけねえな": "…여자란 도무지 감상적이라서 안 되겠어.",
    "この星の人間達が自らの無力さを思い知ったところで": "이 별의 인간들이 자신의 무력함을 깨달았을 때",
    "もうすぐ次元の檻は完成する…": "곧 차원의 감옥이 완성돼…",
    "で、奴等に絶望と破滅をくれてやるさ": "그래서 녀석들에게 절망과 파멸을 선사하는 거지.",
    "…そんな事をして何の意味があるんだろうな…": "…그런 짓을 해서 무슨 의미가 있을까…",
    "（意味なんて、最初からねえんだよ。": "(의미 같은 건 애초에 없었어.",
    "いや…それを失ったのは、あの日からか…）": "아니… 그걸 잃은 건 그날부터인가…)",
    "（フ…俺もアンナロッタを笑えんな…。": "(후… 나도 안나롯타를 비웃을 수 없어….",
    "こんな夜は、どうしてもあの日を思い出しちまう…）": "이런 밤은 도무지 그날을 떠올리게 돼…)",
    "一つ高みに上った…）": "한 단계 높아졌어…)",
    "どうにもそれは性分じゃねえ…）": "도무지 그건 내 성격이 아니야…)",
    "お前達が自分の無力さを思い知る瞬間を": "네놈들이 자신의 무력함을 깨닫는 순간을",
    "特等席で見させてもらうとするぜ）": "특등석에서 구경해 주도록 하지)",
    "助かる": "고맙군",
    "どうなさいました？": "왜 그러시죠?",
    "諦めさせたいのですよね？": "포기하게 하고 싶은 거죠？",
    "おや？": "어라?",
    "私がみんなと一緒に戦うって話の事？": "내가 모두와 함께 싸운다는 이야기？",
    "そうです": "맞습니다",
    "でも、もう決めたの": "하지만 이제 결정했어",
    "私があなた達の戦いに口を出さないように": "내가 너희들의 싸움에 끼어들지 않도록",
    "あなたも私を認めて欲しいんだけど": "너도 나를 인정해 줬으면 하는데",
    "口で言ってもわからないなら…": "말로 해도 모르겠다면…",
    "そこでもう一度、話をしましょう": "거기서 다시 한번 이야기합시다",
    "あなたにとって、私は邪魔なの…": "너에게 나는 방해가 되는 거야…",
    "助かる": "고맙군",
    "いえいえ。": "아니요.",
    "出来ましたよ": "되었습니다",
    "残念だな。": "유감이군",
    "図星を突かれて、思わず素直になってしまったんですな": "정곡을 찔려서 나도 모르게 솔직해진 거군요",
}


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def compact_spaces(value: str) -> str:
    return SPACE_RE.sub(" ", str(value).replace("\r\n", "\n").replace("\r", "\n")).strip()


def control_tokens(value: str) -> list[str]:
    return CONTROL_RE.findall(str(value))


def strip_controls(value: str) -> str:
    return CONTROL_RE.sub("", str(value))


def japanese_count(value: str) -> int:
    return len(JAPANESE_RE.findall(strip_controls(value)))


def is_name_like(value: str) -> bool:
    value = compact_spaces(value)
    if not value or len(value) > 32:
        return False
    return not any(mark in value for mark in "。、！？!?…「」『』()（）$＄\n")


def visual_length(value: str) -> int:
    # 게임 대사창의 표시는 제어문자를 한 칸으로 세지 않는다. 실제 바이트는
    # CPK 재삽입 단계에서 다시 확인하므로, 여기서는 표시 길이만 보수적으로 본다.
    return len(strip_controls(value).replace("\n", ""))


def visual_overflow(value: str, metadata: dict[str, Any]) -> int:
    limit = metadata.get("byteLimit")
    if limit in (None, "", 0):
        return 0
    try:
        return max(0, visual_length(value) - int(limit))
    except (TypeError, ValueError):
        return 0


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_git_json(commit: str, relative_path: str) -> dict[str, Any] | None:
    try:
        completed = subprocess.run(
            ["git", "show", f"{commit}:{relative_path.replace(chr(92), '/') }"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    try:
        return json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


@dataclass
class GlossaryIndex:
    """사전의 여러 출처를 충돌 우선순위와 함께 빠르게 조회한다."""

    exact: dict[str, str] = field(default_factory=dict)
    exact_priority: dict[str, int] = field(default_factory=dict)
    source_terms: list[tuple[str, str, str]] = field(default_factory=list)
    source_aliases: dict[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)
    term_aliases: dict[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)
    profile_by_ja: dict[str, str] = field(default_factory=dict)
    profile_by_ko: dict[str, str] = field(default_factory=dict)
    conflicts: list[dict[str, str]] = field(default_factory=list)

    @classmethod
    def from_document(
        cls, document: dict[str, Any], series_reference: dict[str, Any] | None = None
    ) -> "GlossaryIndex":
        index = cls()

        def add_exact(ja: str, ko: str, kind: str) -> None:
            ja_key = compact_spaces(ja)
            ko_value = compact_spaces(ko)
            if not ja_key or not ko_value:
                return
            priority = KIND_PRIORITY.get(kind, 10)
            previous = index.exact.get(ja_key)
            if previous and previous != ko_value:
                index.conflicts.append({"ja": ja_key, "kept": previous, "ignored": ko_value, "kind": kind})
            if previous is None or priority > index.exact_priority.get(ja_key, -1):
                index.exact[ja_key] = ko_value
                index.exact_priority[ja_key] = priority

        for record in document.get("records", []):
            if not isinstance(record, dict):
                continue
            ja = str(record.get("ja", ""))
            ko = str(record.get("preferredKo", ""))
            kind = str(record.get("kind", ""))
            if not ja or not ko:
                continue
            add_exact(ja, ko, kind)
            if len(compact_spaces(ja)) <= 80 and "\n" not in ja:
                index.source_terms.append((compact_spaces(ja), compact_spaces(ko), kind))
            aliases: list[tuple[str, str]] = []
            for alias in (record.get("koCandidates", []) or []) + (record.get("aliasesKo", []) or []):
                alias_text = compact_spaces(alias)
                if alias_text and alias_text != compact_spaces(ko) and (alias_text, compact_spaces(ko)) not in aliases:
                    aliases.append((alias_text, compact_spaces(ko)))
            if aliases:
                index.term_aliases[compact_spaces(ja)] = tuple(aliases)

        # 프로젝트에서 확정한 표기는 엑셀에 직접 등장하지 않는 축약형도 포함한다.
        for ja, ko in {
            "時獄戦役": "시옥전쟁",
            "インサラウム": "인사라움",
            "ＺＥＵＴＨ": "ZEUTH",
            "ＺＥＸＩＳ": "ZEXIS",
            "Ｚ－ＢＬＵＥ": "Z-BLUE",
        }.items():
            add_exact(ja, ko, "project_term")

        for source, pairs in SOURCE_TERM_OVERRIDES.items():
            index.source_aliases[source] = pairs

        # 작품별 공식 제목도 대사 안에서 등장할 수 있으므로, 엑셀에 없는
        # 변형판 제목을 참고 자료에서 보충한다. 인물·기체 레코드보다 낮은
        # 우선순위를 사용해 기존 게임 표기를 덮어쓰지 않는다.
        for work in (series_reference or {}).get("works", []):
            if not isinstance(work, dict):
                continue
            ja_title = compact_spaces(work.get("jaTitle", ""))
            ko_title = compact_spaces(work.get("koTitle", ""))
            if ja_title and ko_title:
                add_exact(ja_title, ko_title, "series_reference")
                index.source_terms.append((ja_title, ko_title, "series_reference"))

        # 중복·부분 문자열 충돌을 줄이기 위해 긴 일본어 표기를 먼저 처리한다.
        index.source_terms = sorted(
            set(index.source_terms), key=lambda item: (len(item[0]), KIND_PRIORITY.get(item[2], 0)), reverse=True
        )

        profiles = document.get("speechProfiles", [])
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            profile_id = str(profile.get("id", ""))
            for name in profile.get("namesJa", []) or []:
                index.profile_by_ja[compact_spaces(name)] = profile_id
            for name in profile.get("namesKo", []) or []:
                index.profile_by_ko[compact_spaces(name)] = profile_id
        return index

    def exact_name(self, source: str) -> str | None:
        key = compact_spaces(strip_controls(source))
        if is_name_like(key):
            return self.exact.get(key)
        return None

    def profile_for_source(self, source: str) -> str | None:
        return self.profile_by_ja.get(compact_spaces(strip_controls(source)))

    def canonicalize(self, source: str, value: str) -> tuple[str, list[str]]:
        """원문에 실제로 등장하는 용어만 후보 번역 안에서 정규화한다."""

        source_key = compact_spaces(strip_controls(source))
        raw = str(value)
        source_tokens = control_tokens(source)
        candidate_tokens = control_tokens(raw)
        base = strip_controls(raw)
        matched: list[str] = []

        # 이름/부대명만 단독으로 표시되는 필드는 사전의 확정 표기를 그대로 사용한다.
        exact = self.exact_name(source)
        if exact:
            base = exact
            matched.append(source_key)
        else:
            # 후보 표기(23개)와 프로젝트가 명시한 예외만 순회한다. 전체 사전에는
            # 수천 개의 고유명사가 있으므로, 모든 레코드를 문장마다 검사하면
            # 17만 항목 처리 시간이 불필요하게 커진다.
            for ja, aliases in self.term_aliases.items():
                if ja not in source_key:
                    continue
                # source_terms 자체의 preferredKo는 치환 대상이 아니며, 사전의
                # 과거 후보 표기와 SOURCE_TERM_OVERRIDES의 명백한 오탈자만 교정한다.
                for old, new in aliases:
                    if old in base:
                        base = base.replace(old, new)
                        matched.append(ja)
                for old, new in self.source_aliases.get(ja, ()):
                    if old and old != new and old in base:
                        base = base.replace(old, new)
                        matched.append(ja)
            # 세계관 표기는 원문 용어가 있을 때만 후보의 흔한 변형을 교정한다.
            for ja, pairs in self.source_aliases.items():
                if ja not in source_key:
                    continue
                for old, new in pairs:
                    if old and old in base:
                        base = base.replace(old, new)
                        matched.append(ja)

        base = normalize_punctuation(source, base)
        if source_tokens == candidate_tokens:
            # 토큰이 이미 올바른 위치에 있으면 원래 위치를 보존한다.
            result = reinsert_existing_tokens(raw, base)
        else:
            result = place_tokens_like_source(source, base)
        return result, sorted(set(matched))


def reinsert_existing_tokens(raw: str, base: str) -> str:
    """정규화한 본문에 후보가 가지고 있던 토큰을 원래 경계에 다시 삽입한다."""

    matches = list(CONTROL_RE.finditer(raw))
    if not matches:
        return base
    raw_base = strip_controls(raw)
    if raw_base == base:
        return raw
    boundaries = [len(strip_controls(raw[:match.start()])) for match in matches]
    result = base
    shift = 0
    for match, boundary in zip(matches, boundaries):
        index = max(0, min(len(result), boundary + shift))
        token = match.group(0)
        result = result[:index] + token + result[index:]
        shift += len(token)
    return result


def place_tokens_like_source(source: str, base: str) -> str:
    """토큰이 빠졌거나 순서가 달라졌을 때 원문 경계를 비율로 복원한다."""

    matches = list(CONTROL_RE.finditer(source))
    if not matches:
        return base
    source_base = strip_controls(source)
    result = base
    shift = 0
    denominator = max(1, len(source_base))
    for match in matches:
        boundary = len(strip_controls(source[:match.start()]))
        index = round(boundary / denominator * len(base)) + shift
        index = max(0, min(len(result), index))
        token = match.group(0)
        result = result[:index] + token + result[index:]
        shift += len(token)
    return result


def normalize_punctuation(source: str, value: str) -> str:
    """후보에 생긴 기계적인 공백과 ASCII 이중 문장부호만 정리한다."""

    result = str(value)
    result = re.sub(r"[ \t]+([,.;:，。、:；！!?！？…」』）)〉》])", r"\1", result)
    result = re.sub(r"([「『（(〈《])\s+", r"\1", result)
    source_body = strip_controls(source)
    if re.search(r"[!！]\s*[?？]", result) and re.search(r"[!！][?？]", source_body):
        result = re.sub(r"[!！]\s*[?？]", "！？", result)
    if re.search(r"\.\.\.", result) and "…" in source_body:
        result = result.replace("...", "…")
    return result.strip()


@dataclass
class Candidate:
    origin: str
    value: str
    score: int = 0
    normalized: str = ""
    matched_terms: list[str] = field(default_factory=list)


ORIGIN_BONUS = {
    "manualPhrase": 600,
    "baseline": 400,
    "legacyReference": 350,
    "importedReference": 340,
    "googleReference": 250,
    "current": 100,
}


def quality_penalty(value: str, source: str = "") -> int:
    """문장 후보에 남은 명백한 직역/오인식 표현의 감점량."""

    penalty = 0
    for pattern in QUALITY_BAD_PATTERNS:
        if pattern.search(value):
            penalty += 180
    # 한글 문장 안에 ASCII 마침표가 세 개 이상이면 기계식 말줄임표일 가능성이
    # 높다. 원문에 말줄임표가 있으면 normalize_punctuation이 먼저 정리한다.
    if value.count("..."):
        penalty += 240 * value.count("...")
    if re.search(r"[가-힣][가-힣]{8,}", value) and " " not in value and len(value) > 12:
        penalty += 40
    source_len = len(strip_controls(source).replace("\n", ""))
    value_len = len(strip_controls(value).replace("\n", ""))
    # 긴 원문이 한두 음절이나 말줄임표로 잘린 후보는 직역 품질과 무관하게
    # 대사 누락으로 판단한다. 짧은 감탄사·화자명에는 적용하지 않는다.
    if source_len >= 10 and value_len <= 3:
        penalty += 500
    elif source_len >= 16 and value_len / max(1, source_len) < 0.30:
        penalty += 320
    return penalty


def score_candidate(source: str, candidate: Candidate, metadata: dict[str, Any]) -> int:
    value = candidate.normalized or candidate.value
    source_tokens = control_tokens(source)
    target_tokens = control_tokens(value)
    score = ORIGIN_BONUS.get(candidate.origin, 0)
    if not value.strip() and source.strip():
        return -100000
    if MARKER_RE.search(value):
        score -= 100000
    score -= quality_penalty(value, source)
    if source_tokens != target_tokens:
        score -= 10000
    jp = japanese_count(value)
    score -= jp * 40
    overflow = visual_overflow(value, metadata)
    score -= overflow * 8
    if value.strip() == source.strip() and JAPANESE_RE.search(source):
        score -= 2000
    # 원문이 이름인 경우 사전 확정값과 일치하는 후보를 강하게 우선한다.
    if is_name_like(source) and candidate.normalized == candidate.value:
        score += 20
    if source.strip() and not strip_controls(value).strip():
        score -= 5000
    return score


def candidate_values(entry: dict[str, Any], baseline_entry: dict[str, Any] | None) -> Iterable[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()

    def emit(origin: str, value: Any) -> Iterable[tuple[str, str]]:
        text = str(value or "")
        key = (origin, text)
        if key not in seen:
            seen.add(key)
            yield key

    if baseline_entry is not None:
        yield from emit("baseline", baseline_entry.get("translation", ""))
    references = entry.get("references") if isinstance(entry.get("references"), dict) else {}
    for key, origin in (
        ("legacyTranslation", "legacyReference"),
        ("importedTranslation", "importedReference"),
        ("googleTranslation", "googleReference"),
    ):
        yield from emit(origin, references.get(key, ""))
    yield from emit("current", entry.get("translation", ""))


def choose_translation(
    entry: dict[str, Any],
    baseline_entry: dict[str, Any] | None,
    glossary: GlossaryIndex,
) -> tuple[str, str, list[str], list[Candidate]]:
    source = str(entry.get("sourceText", ""))
    metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
    candidates: list[Candidate] = []
    for origin, value in candidate_values(entry, baseline_entry):
        normalized, matched = glossary.canonicalize(source, value)
        phrase_key = compact_spaces(strip_controls(source))
        phrase_override = SOURCE_PHRASE_OVERRIDES.get(phrase_key)
        if phrase_override:
            normalized = place_tokens_like_source(source, phrase_override)
            origin = "manualPhrase"
            matched = matched + [phrase_key]
        candidate = Candidate(origin=origin, value=value, normalized=normalized, matched_terms=matched)
        candidate.score = score_candidate(source, candidate, metadata)
        candidates.append(candidate)
    if not candidates:
        candidates.append(Candidate(origin="empty", value="", normalized="", score=-100000))
    candidates.sort(key=lambda item: (item.score, ORIGIN_BONUS.get(item.origin, 0)), reverse=True)
    selected = candidates[0]
    return selected.normalized, selected.origin, selected.matched_terms, candidates


def entry_stats(entry: dict[str, Any], translation: str | None = None) -> dict[str, int]:
    source = str(entry.get("sourceText", ""))
    value = str(entry.get("translation", "") if translation is None else translation)
    metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
    return {
        "entries": 1,
        "empty": int(bool(source.strip()) and not value.strip()),
        "japaneseRemainder": int(japanese_count(value) > 0),
        "japaneseCharacters": japanese_count(value),
        "controlMismatch": int(control_tokens(source) != control_tokens(value)),
        "lengthOverflow": int(visual_overflow(value, metadata) > 0),
        "overflowCharacters": visual_overflow(value, metadata),
    }


def add_stats(target: dict[str, int], value: dict[str, int]) -> None:
    for key, number in value.items():
        target[key] = target.get(key, 0) + int(number)


def apply_entry(entry: dict[str, Any], translation: str, origin: str, matched_terms: list[str], baseline_commit: str) -> None:
    entry["translation"] = translation
    entry["translationTextSha256"] = sha256_text(translation)
    entry["translationStatus"] = "draft"
    entry["translator"] = "문맥·용어사전 기반 재번역 v3(이전 손번역 참고)"
    entry["reviewer"] = ""
    entry["referenceConsulted"] = True
    notes = [
        "일본어 원문과 통합 용어사전 기준으로 재구성",
        f"후보 출처: {origin}",
        f"기준 커밋: {baseline_commit}",
        "이전 번역은 참고 자료로만 사용; 최종 검수 필요",
    ]
    if matched_terms:
        notes.append("정규화 용어: " + ", ".join(matched_terms[:12]))
    entry["notes"] = "; ".join(notes)
    controls = entry.setdefault("controls", {})
    controls["sourceTokens"] = control_tokens(entry.get("sourceText", ""))
    controls["translationTokens"] = control_tokens(translation)
    controls["sourceSignature"] = "|".join(controls["sourceTokens"])
    controls["translationSignature"] = "|".join(controls["translationTokens"])


def update_document_counts(document: dict[str, Any]) -> None:
    entries = document.get("entries", [])
    translations = [str(entry.get("translation", "")) for entry in entries if isinstance(entry, dict)]
    document["counts"] = {
        "entries": len(entries),
        "uniqueSourceTexts": len({str(entry.get("sourceText", "")) for entry in entries if isinstance(entry, dict)}),
        "translatedEntries": sum(bool(value.strip()) for value in translations),
    }


def process_asset(
    source_path: Path,
    document: dict[str, Any],
    baseline_document: dict[str, Any] | None,
    glossary: GlossaryIndex,
    baseline_commit: str,
    apply: bool,
) -> dict[str, Any]:
    baseline_by_id = {
        str(entry.get("entryId")): entry
        for entry in (baseline_document or {}).get("entries", [])
        if isinstance(entry, dict) and entry.get("entryId")
    }
    before: dict[str, int] = {}
    after: dict[str, int] = {}
    origins: Counter[str] = Counter()
    changed = 0
    baseline_missing = 0
    source_mismatch = 0
    profile_transitions = 0
    current_profile: str | None = None
    examples: list[dict[str, Any]] = []

    for entry in document.get("entries", []):
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("sourceText", ""))
        add_stats(before, entry_stats(entry))
        entry_id = str(entry.get("entryId", ""))
        baseline_entry = baseline_by_id.get(entry_id)
        if baseline_entry is None:
            baseline_missing += 1
        elif baseline_entry.get("sourceText") != entry.get("sourceText"):
            source_mismatch += 1

        profile = glossary.profile_for_source(source)
        if profile and profile != current_profile:
            current_profile = profile
            profile_transitions += 1

        translation, origin, matched_terms, candidates = choose_translation(entry, baseline_entry, glossary)
        add_stats(after, entry_stats(entry, translation))
        origins[origin] += 1
        if translation != str(entry.get("translation", "")):
            changed += 1
        if len(examples) < 120 and (
            translation != str(entry.get("translation", ""))
            or origin != "baseline"
            or matched_terms
            or entry_stats(entry, translation)["japaneseRemainder"]
        ):
            examples.append(
                {
                    "entryId": entry_id,
                    "sourceText": source,
                    "before": str(entry.get("translation", "")),
                    "after": translation,
                    "origin": origin,
                    "matchedTerms": matched_terms,
                    "candidateScores": [{"origin": c.origin, "score": c.score} for c in candidates[:5]],
                }
            )
        if apply:
            apply_entry(entry, translation, origin, matched_terms, baseline_commit)

    if apply:
        update_document_counts(document)
        atomic_json(source_path, document)

    return {
        "asset": source_path.name,
        "entries": len(document.get("entries", [])),
        "changed": changed,
        "baselineMissing": baseline_missing,
        "sourceMismatch": source_mismatch,
        "profileTransitions": profile_transitions,
        "origins": dict(origins),
        "before": before,
        "after": after,
        "examples": examples,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dialogue-root", type=Path, default=DEFAULT_DIALOGUE_ROOT)
    parser.add_argument("--glossary", type=Path, default=DEFAULT_GLOSSARY)
    parser.add_argument("--series-reference", type=Path, default=DEFAULT_SERIES_REFERENCE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--baseline-commit", default=DEFAULT_BASELINE_COMMIT)
    parser.add_argument("--baseline-root", type=Path, help="Git 커밋 대신 같은 구조의 기준 JSON 폴더를 읽는다.")
    parser.add_argument("--asset", action="append", help="특정 scenario_*.json만 처리한다. 여러 번 지정 가능")
    parser.add_argument("--apply", action="store_true", help="선택 결과를 실제 시나리오 JSON에 저장한다.")
    parser.add_argument("--fail-on-warning", action="store_true", help="일본어 잔존·길이 초과도 종료 코드 1로 처리한다.")
    return parser.parse_args()


def load_baseline(args: argparse.Namespace, path: Path) -> dict[str, Any] | None:
    if args.baseline_root:
        candidate = args.baseline_root / path.name
        if candidate.exists():
            return load_json(candidate)
    relative = path.relative_to(ROOT).as_posix()
    return load_git_json(args.baseline_commit, relative)


def main() -> int:
    args = parse_args()
    glossary_document = load_json(args.glossary)
    series_reference = load_json(args.series_reference) if args.series_reference.exists() else {}
    glossary = GlossaryIndex.from_document(glossary_document, series_reference)
    selected_names = set(args.asset or [])
    report: dict[str, Any] = {
        "format": "siok.scenario-retranslation-v3-qa",
        "generatedAt": "2026-08-12",
        "apply": bool(args.apply),
        "dialogueRoot": str(args.dialogue_root),
        "glossary": {
            "path": str(args.glossary),
            "sha256": hashlib.sha256(args.glossary.read_bytes()).hexdigest(),
            "records": len(glossary_document.get("records", [])),
            "conflicts": glossary.conflicts[:200],
        },
        "seriesReference": {
            "path": str(args.series_reference),
            "sha256": hashlib.sha256(args.series_reference.read_bytes()).hexdigest()
            if args.series_reference.exists()
            else None,
            "works": len(series_reference.get("works", [])),
        },
        "baseline": {"commit": args.baseline_commit, "root": str(args.baseline_root) if args.baseline_root else None},
        "assets": [],
        "totals": {"before": {}, "after": {}},
        "errors": [],
    }

    paths = sorted(args.dialogue_root.glob("scenario_*.json"))
    if selected_names:
        paths = [path for path in paths if path.name in selected_names or path.stem in selected_names]
    for path in paths:
        try:
            document = load_json(path)
            baseline_document = load_baseline(args, path)
            result = process_asset(path, document, baseline_document, glossary, args.baseline_commit, args.apply)
            report["assets"].append(result)
            add_stats(report["totals"]["before"], result["before"])
            add_stats(report["totals"]["after"], result["after"])
        except Exception as exc:  # 한 파일의 이상으로 전체 보고서를 잃지 않도록 한다.
            report["errors"].append({"asset": path.name, "error": f"{type(exc).__name__}: {exc}"})

    report["summary"] = {
        "assets": len(report["assets"]),
        "entries": sum(int(item.get("entries", 0)) for item in report["assets"]),
        "changed": sum(int(item.get("changed", 0)) for item in report["assets"]),
        "baselineMissing": sum(int(item.get("baselineMissing", 0)) for item in report["assets"]),
        "sourceMismatch": sum(int(item.get("sourceMismatch", 0)) for item in report["assets"]),
    }
    report["ok"] = not report["errors"] and report["summary"]["baselineMissing"] == 0 and report["summary"]["sourceMismatch"] == 0
    after = report["totals"]["after"]
    if args.fail_on_warning and (after.get("japaneseRemainder", 0) or after.get("lengthOverflow", 0)):
        report["ok"] = False
    atomic_json(args.report, report)
    print(json.dumps({"ok": report["ok"], "summary": report["summary"], "before": report["totals"]["before"], "after": report["totals"]["after"], "report": str(args.report)}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
