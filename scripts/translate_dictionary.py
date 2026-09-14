#!/usr/bin/env python3
"""추출한 사전 원문을 일본어→한국어로 새로 번역하고 고정 페이로드를 만든다.

기존 번역 파일은 읽지 않는다. 원문 JSON만 읽어 Google의 공개 번역
엔드포인트에 전달하고, 번역 결과의 ``<BR>/<NUL>/<Bxx>`` 제어 토큰을
보호한다. 게임의 한글 글리프 치환과 ``shift-jis2`` 매핑으로 고정 슬롯
페이로드를 계산하므로, 결과 JSON만으로 별도 엑셀이나 네트워크 없이 패치할
수 있다.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from siok_patch.dictionary import encode_logical, sha256_bytes, sha256_file
from siok_patch.text_normalization import GAME_HALF_WIDTH_SPACE_BYTES


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENCODING = REPOSITORY_ROOT / "config" / "encoding" / "dictionary.json"
DEFAULT_INPUT = REPOSITORY_ROOT / "work" / "dictionary-source.json"
DEFAULT_OUTPUT = REPOSITORY_ROOT / "translations" / "dictionary" / "MtZkn_KW.json"
DEFAULT_CACHE = REPOSITORY_ROOT / "work" / "dictionary-translation-cache.json"
TOKEN = re.compile(r"<(?:BR|NUL|B[0-9A-Fa-f]{2})>")
PLACEHOLDER = re.compile(r"__SIOK_DICT_CTRL_(\d+)__")

# 번역 서비스가 고유명사를 일본어 그대로 반환해도 게임 문자표로 쓸 수
# 있도록, 사전 항목의 표제어만 한국어 음역을 명시한다. 기존 번역 파일에서
# 가져온 값이 아니라 이번 추출 원문을 기준으로 만든 최소 표제어 보정이다.
NAME_OVERRIDES = {
    "徳川喜一郎": "도쿠가와키이치로",
    "姫": "히메",
    "アクエリアの舞う空": "아쿠에리아가춤추는하늘",
    "次元獣": "차원수",
    "桜田栄二郎": "사쿠라다에이지로",
}
# 작품 고유명사처럼 번역 서비스가 원문 표기를 그대로 남기는 경우의
# 최소 용어 보정이다. `ヂヂリウム`은 보톰즈의 고유 광물명으로 `지지리움`을 쓴다.
TERM_OVERRIDES = {
    "ヂヂリウム": "지지리움",
    "ヂリウム": "지지리움",
    "ヂ리움": "지지리움",
}


class DictionaryTranslationError(ValueError):
    """번역·인코딩 결과가 패치 조건을 만족하지 않을 때 발생한다."""


def _replace_terms(text: str) -> str:
    for source, target in sorted(TERM_OVERRIDES.items(), key=lambda item: (-len(item[0]), item[0])):
        text = text.replace(source, target)
    return text


def _protect(text: str) -> tuple[str, tuple[str, ...]]:
    tokens: list[str] = []

    def replace(match: re.Match[str]) -> str:
        tokens.append(match.group(0))
        return f"__SIOK_DICT_CTRL_{len(tokens) - 1}__"

    return TOKEN.sub(replace, text), tuple(tokens)


def _restore(text: str, tokens: tuple[str, ...]) -> str:
    for index, token in enumerate(tokens):
        marker = f"__SIOK_DICT_CTRL_{index}__"
        if marker not in text:
            raise DictionaryTranslationError(f"번역 결과에서 제어 토큰이 사라졌습니다: {token}")
        text = text.replace(marker, token)
    if PLACEHOLDER.search(text):
        raise DictionaryTranslationError("번역 결과에 알 수 없는 제어 토큰이 남았습니다.")
    return text.strip("\n")


def _request(text: str, *, retries: int = 6) -> str:
    query = urlencode({"client": "gtx", "sl": "ja", "tl": "ko", "dt": "t", "q": text})
    request = Request(
        "https://translate.googleapis.com/translate_a/single?" + query,
        headers={"User-Agent": "siok-dictionary/1.0"},
    )
    for attempt in range(retries):
        try:
            with urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8"))
            segments = payload[0]
            result = "".join(
                str(segment[0])
                for segment in segments
                if isinstance(segment, list) and segment and isinstance(segment[0], str)
            )
            if not result:
                raise DictionaryTranslationError("번역 서비스가 빈 결과를 반환했습니다.")
            return result
        except HTTPError as error:
            if error.code in {400, 414}:
                raise
            if attempt + 1 >= retries:
                raise
            time.sleep(min(2**attempt, 12))
        except Exception:
            if attempt + 1 >= retries:
                raise
            time.sleep(min(2**attempt, 12))
    raise AssertionError("unreachable")


def translate_one(source: str) -> str:
    # 긴 사전 설명에서는 번역 서비스가 자리표시자나 줄바꿈 표식을
    # 문장부호로 오인해 삭제하는 경우가 있다. 토큰을 번역 요청에서 완전히
    # 분리하면 줄 수와 특수 바이트를 결정적으로 보존할 수 있다.
    translated: list[str] = []
    cursor = 0
    for match in TOKEN.finditer(source):
        if match.start() > cursor:
            translated.append(_request(source[cursor : match.start()]))
        translated.append(match.group(0))
        cursor = match.end()
    if cursor < len(source):
        translated.append(_request(source[cursor:]))
    return "".join(translated)


def _load_encoding(path: Path) -> tuple[dict[bytes, str], dict[str, str]]:
    root = json.loads(path.read_text(encoding="utf-8"))
    if root.get("format") != "siok.dictionary-encoding":
        raise DictionaryTranslationError("지원하지 않는 사전 인코딩 JSON입니다.")
    table = {bytes.fromhex(key): value for key, value in root["table"].items()}
    replacement = {str(key): str(value) for key, value in root["replacement"].items()}
    return table, replacement


def _apply_replacements(text: str, replacement: dict[str, str]) -> str:
    # wReplace는 한글을 게임 글리프로 바꾸는 표다. 긴 토큰 우선으로 한 번만
    # 치환해, 새로 생긴 글리프가 다시 치환되는 연쇄를 막는다.
    tokens = sorted(replacement, key=lambda value: (-len(value), value))
    output: list[str] = []
    position = 0
    while position < len(text):
        matched = next((token for token in tokens if text.startswith(token, position)), None)
        if matched is None:
            output.append(text[position])
            position += 1
        else:
            output.append(replacement[matched])
            position += len(matched)
    return "".join(output)


def encode_translation(
    text: str,
    table: dict[bytes, str],
    replacement: dict[str, str],
    capacity: int | None,
) -> bytes:
    text = (
        text.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\u3000", " ")
        .replace("\u200b", "")
        .replace("\u200c", "")
        .replace("\ufeff", "")
        .replace("\u00a0", " ")
    )
    text = text.replace("\n", "<BR>")
    # wReplace에는 레이아웃용 ASCII 공백을 전각 공백으로 바꾸는 항목이
    # 있을 수 있다. 번역 공백은 반각을 유지해야 하므로 해당 한 글자는
    # 문자표 치환에서 제외한다.
    replacement_without_space = {key: value for key, value in replacement.items() if key != " "}
    text = _apply_replacements(text, replacement_without_space)
    by_first: dict[str, list[str]] = {}
    text_to_bytes: dict[str, bytes] = {}
    for sequence, token in table.items():
        by_first.setdefault(token[0], []).append(token)
        text_to_bytes[token] = sequence
    for values in by_first.values():
        values.sort(key=len, reverse=True)
    output = bytearray()
    position = 0
    while position < len(text):
        if text[position] == " ":
            output.extend(GAME_HALF_WIDTH_SPACE_BYTES)
            position += 1
            continue
        if text.startswith("<BR>", position):
            output.append(0x0A)
            position += 4
            continue
        if text.startswith("<NUL>", position):
            output.append(0)
            position += 5
            continue
        marker = re.match(r"<B([0-9A-Fa-f]{2})>", text[position:])
        if marker:
            output.append(int(marker.group(1), 16))
            position += len(marker.group(0))
            continue
        candidates = by_first.get(text[position], ())
        matched = next((token for token in candidates if text.startswith(token, position)), None)
        if matched is None:
            raise DictionaryTranslationError(
                f"게임 문자표에 없는 번역 문자입니다: {text[position:position + 12]!r}"
            )
        output.extend(text_to_bytes[matched])
        position += len(matched)
    if capacity is not None and len(output) > capacity:
        raise DictionaryTranslationError(f"번역 페이로드가 슬롯을 초과합니다: {len(output)} > {capacity}")
    logical = bytes(output) if capacity is None else bytes(output + b"\0" * (capacity - len(output)))
    return encode_logical(logical)


def _encoded_length(text: str, table: dict[bytes, str], replacement: dict[str, str]) -> int:
    """패딩을 제외한 논리 페이로드 길이를 계산한다."""

    return len(encode_translation(text, table, replacement, None))


def _fit_translation(
    text: str,
    *,
    field: str,
    capacity: int,
    source: str,
    table: dict[bytes, str],
    replacement: dict[str, str],
) -> tuple[str, str]:
    """번역을 게임 문자표로 표현할 수 있는지 확인한다.

    WORD/SRCE처럼 화면에서 짧게 보이는 항목은 이름 안의 불필요한 ASCII
    공백을 제거한다. 설명(DSCR/DSC2)은 원문 슬롯보다 길어질 수 있으므로
    문장을 잘라내지 않고, CPK 리팩 단계에서 태그 길이와 ITOC 멤버 크기를
    함께 늘린다.
    """

    normalized = (
        text.replace("\u200b", "")
        .replace("\u200c", "")
        .replace("\ufeff", "")
        .replace("\u00a0", " ")
    )
    candidates = [normalized]
    if field in {"WORD", "SRCE"}:
        candidates.append(text.replace(" ", ""))
    candidates.append(text.replace(" ", "").replace("　", ""))
    overflow_candidate: tuple[str, int] | None = None
    for candidate in candidates:
        try:
            length = _encoded_length(candidate, table, replacement)
            if field in {"WORD", "SRCE"} and length <= capacity:
                return candidate, "draft"
            if field in {"WORD", "SRCE"} and length > capacity:
                overflow_candidate = (candidate, length)
                continue
            return candidate, "overflow-repacked" if length > capacity else "draft"
        except DictionaryTranslationError:
            continue
    if overflow_candidate is not None:
        return overflow_candidate[0], "overflow-repacked"
    try:
        if _encoded_length(source, table, replacement) <= 1_000_000:
            return source, "fallback-source"
    except DictionaryTranslationError:
        pass
    raise DictionaryTranslationError(f"번역을 고정 슬롯에 넣을 수 없습니다: {field} {capacity}바이트")


def translate_document(
    source_path: Path,
    encoding_path: Path,
    workers: int,
    cache_path: Path,
    previous_output_path: Path | None = None,
) -> dict[str, object]:
    source = json.loads(source_path.read_text(encoding="utf-8"))
    if source.get("format") != "siok.dictionary-dialogue-source":
        raise DictionaryTranslationError("지원하지 않는 사전 원문 JSON입니다.")
    table, replacement = _load_encoding(encoding_path)
    entries = source.get("entries")
    if not isinstance(entries, list) or not entries:
        raise DictionaryTranslationError("원문 entries가 비어 있습니다.")
    unique: dict[str, str] = {str(entry["sourceText"]): "" for entry in entries}
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.is_file() else {}
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DictionaryTranslationError(f"번역 캐시를 읽을 수 없습니다: {cache_path}") from error
    results: dict[str, str] = {
        text: str(cached[text])
        for text in unique
        if isinstance(cached, dict)
        and isinstance(cached.get(text), str)
        and TOKEN.findall(str(cached[text])) == TOKEN.findall(text)
    }
    results.update({text: NAME_OVERRIDES[text] for text in unique if text in NAME_OVERRIDES})
    pending = {text for text in unique if text not in results}

    def save_cache() -> None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_suffix(cache_path.suffix + ".tmp")
        temporary.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(cache_path)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(translate_one, text): text for text in pending}
        for future in as_completed(futures):
            text = futures[future]
            try:
                results[text] = future.result()
                save_cache()
            except Exception as error:
                raise DictionaryTranslationError(f"번역 실패: {text[:40]!r}: {error}") from error

    output_entries: list[dict[str, object]] = []
    for raw in entries:
        entry = dict(raw)
        translation, fit_status = _fit_translation(
            _replace_terms(results[str(entry["sourceText"])]),
            field=str(entry["field"]),
            capacity=int(entry["capacityBytes"]),
            source=str(entry["sourceText"]),
            table=table,
            replacement=replacement,
        )
        entry["translation"] = translation
        entry["translationStatus"] = fit_status
        entry["translator"] = "Google Translate ja→ko (일본어 원문 신규 번역 초안)"
        entry["reviewer"] = ""
        entry["referenceConsulted"] = False
        entry["notes"] = "기존 번역은 참조하지 않고 원문에서 새로 번역함; 제어 토큰은 자동 보존."
        entry["translationTextSha256"] = sha256_bytes(translation.encode("utf-8"))
        applied_payload = encode_translation(translation, table, replacement, None)
        entry["translationByteLength"] = len(applied_payload)
        entry["appliedPayloadHex"] = applied_payload.hex()
        entry["appliedPayloadSha256"] = sha256_bytes(bytes.fromhex(entry["appliedPayloadHex"]))
        output_entries.append(entry)
    output = dict(source)
    output["format"] = "siok.dictionary-dialogue"
    output["formatVersion"] = 1
    output["encoding"]["fileName"] = encoding_path.name
    output["encoding"]["sha256"] = sha256_file(encoding_path)
    output["counts"] = {
        "members": source["counts"]["members"],
        "fields": len(output_entries),
        "translatedFields": len(output_entries),
        "uniqueSourceTexts": len(unique),
    }
    # 번역 내용이 그대로인 재실행에서는 마지막 패치 산출물의 검증 해시를
    # 보존한다. 번역이 하나라도 바뀌면 이전 CPK 해시를 복사하지 않는다.
    if previous_output_path is not None and previous_output_path.is_file():
        try:
            previous = json.loads(previous_output_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            previous = {}
        previous_entries = previous.get("entries") if isinstance(previous, dict) else None
        same_translations = (
            isinstance(previous_entries, list)
            and len(previous_entries) == len(output_entries)
            and all(
                isinstance(old, dict)
                and old.get("entryId") == new.get("entryId")
                and old.get("translationTextSha256") == new.get("translationTextSha256")
                for old, new in zip(previous_entries, output_entries)
            )
        )
        if same_translations and isinstance(previous.get("localizedAsset"), dict):
            output["localizedAsset"] = previous["localizedAsset"]
    output["entries"] = output_entries
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--encoding", type=Path, default=DEFAULT_ENCODING)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    try:
        output = args.output.expanduser().resolve()
        document = translate_document(
            args.input.resolve(strict=True),
            args.encoding.resolve(strict=True),
            args.workers,
            args.cache.expanduser().resolve(),
            output,
        )
        if not output.is_relative_to(REPOSITORY_ROOT / "translations"):
            parser.error("출력은 translations/ 안이어야 합니다.")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, UnicodeError, json.JSONDecodeError, DictionaryTranslationError) as error:
        parser.error(str(error))
    print(json.dumps({"output": str(output), **document["counts"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
