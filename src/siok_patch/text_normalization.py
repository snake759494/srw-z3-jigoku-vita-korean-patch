"""게임에 삽입하는 번역 문자열의 공백 규칙."""

from __future__ import annotations


FULLWIDTH_SPACE = "\u3000"
HALFWIDTH_SPACE = " "
# CP932의 사용자 정의 단일 바이트 글리프 U+F8F2는 FE로 인코딩된다.
# 게임은 반각 공백 하나를 이 글리프 두 개(FE FE)로 저장한다.
GAME_HALF_WIDTH_SPACE_TEXT = "\uf8f2\uf8f2"
GAME_HALF_WIDTH_SPACE_BYTES = bytes((0xFE, 0xFE))


def normalize_dialogue_spaces(value: str) -> str:
    """번역 문자열의 전각 공백을 일반 반각 공백으로 바꾼다.

    일본어 원문과 CPK 스크립트의 구조용 전각 공백은 이 함수의 입력으로
    넘기지 않는다. 즉, 원문 매칭·구조 판별 데이터는 보존하고 실제로 게임에
    삽입할 번역문에만 적용하는 것이 계약이다.
    """

    if not isinstance(value, str):
        raise TypeError("대사 공백 정규화 입력은 문자열이어야 합니다.")
    return value.replace(FULLWIDTH_SPACE, HALFWIDTH_SPACE)


def encode_game_dialogue_spaces(value: str) -> str:
    """번역 공백을 게임의 FE FE 글리프 표현으로 바꾼다.

    반환 문자열은 사람이 읽는 번역 JSON에 저장하지 않는다. CP932로
    인코딩하는 CPK·고정 슬롯 경계에서만 사용하며, U+F8F2 두 글자는
    CP932에서 각각 FE 한 바이트가 된다.
    """

    # 엑셀에서 유입되는 zero-width/BOM 문자는 게임 글리프가 아니므로
    # 삽입 전에 제거한다. NBSP는 일반 반각 공백으로 취급한다.
    normalized = (
        normalize_dialogue_spaces(value)
        .replace("\u200b", "")
        .replace("\u200c", "")
        .replace("\ufeff", "")
        .replace("\u00a0", HALFWIDTH_SPACE)
    )
    return normalized.replace(HALFWIDTH_SPACE, GAME_HALF_WIDTH_SPACE_TEXT)


__all__ = [
    "FULLWIDTH_SPACE",
    "HALFWIDTH_SPACE",
    "GAME_HALF_WIDTH_SPACE_TEXT",
    "GAME_HALF_WIDTH_SPACE_BYTES",
    "encode_game_dialogue_spaces",
    "normalize_dialogue_spaces",
]
