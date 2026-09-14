#!/usr/bin/env python3
"""시나리오 대사를 오프라인 일본어→한국어 모델로 새로 번역한다.

이 도구는 기존 한국어 문장을 입력으로 삼지 않는다. `translations/dialogue`의
일본어 원문을 읽고 NLLB 모델로 새 초안을 만든 다음, 용어집 보호·제어코드
복원·대사창 길이 검사를 거쳐 `work/retranslation-v2`에 체크포인트를 남긴다.
원본 게임 파일은 읽거나 수정하지 않는다.

필수 패키지(선택 기능): transformers, sentencepiece, ctranslate2
모델 변환 예:
  ct2-transformers-converter --model facebook/nllb-200-distilled-600M \
    --output_dir work/models/nllb-200-distilled-600M-int8 --quantization int8
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIALOGUE = REPOSITORY_ROOT / "translations" / "dialogue"
DEFAULT_WORK = REPOSITORY_ROOT / "work" / "retranslation-v2"
DEFAULT_MODEL = DEFAULT_WORK / "models" / "nllb-200-distilled-600M-int8"
MODEL_ID = "facebook/nllb-200-distilled-600M"

# 게임 문자열에 들어가는 토큰은 일반 문장처럼 번역기에 넘기지 않는다.
CONTROL_RE = re.compile(r"(⑲|⑳|㊥|㊦|㊧|㊨|(?:\$|＄)[nlcF])")
JAPANESE_RE = re.compile(r"[ぁ-ゖァ-ヺ一-龯々〆ヵヶ]")
SENTENCE_RE = re.compile(r"(?<=[。！？!?])")
MARKER_RE = re.compile(r"__SIK_[A-Z0-9_]+__")

# 고유명사와 세계관 용어는 모델의 음역 흔들림을 막기 위해 보호한다.
# 표기는 프로젝트 glossary.json의 한국어 표기와 맞춘다.
PROTECTED_TERMS: tuple[tuple[str, str], ...] = (
    ("シン・アスカ", "신 아스카"),
    ("キラ・ヤマト", "키라 야마토"),
    ("アスラン・ザラ", "아스란 자라"),
    ("カミーユ・ビダン", "카미유 비단"),
    ("アムロ・レイ", "아무로 레이"),
    ("クワトロ・バジーナ", "크와트로 바지나"),
    ("シャア・アズナブル", "샤아 아즈나블"),
    ("ロジャー・スミス", "로저 스미스"),
    ("ドロシー", "도로시"),
    ("モーム", "모므"),
    ("刹那・F・セイエイ", "세츠나 F 세이에이"),
    ("ヒイロ・ユイ", "히이로 유이"),
    ("アイム・ライアード", "아임・라이어드"),
    ("スズネ・アネモネ", "스즈네 아네모네"),
    ("スズネ", "스즈네"),
    ("ジェニオン", "제니온"),
    ("ジェミニオン", "제미니온"),
    ("インサラウム", "인사라움"),
    ("ガイオウ", "가이오"),
    ("ガドライト", "가드라이트"),
    ("アンナロッタ", "안나로타"),
    ("ジェミニス", "제미니스"),
    ("ジェミナイ", "제미나이"),
    ("惑星ジェミナイ", "행성 제미나이"),
    ("ハニー", "허니"),
    ("次元の檻", "차원의 새장"),
    ("センチ", "감상적"),
    ("ZEUTH", "ZEUTH"),
    ("ZEXIS", "ZEXIS"),
    ("時獄戦役", "시옥전쟁"),
    ("時獄篇", "시옥편"),
    ("破界事変", "파계사변"),
    ("再世戦争", "재세전쟁"),
    ("新多元世紀", "신다원세기"),
    ("大時空震", "대시공진동"),
    ("時空震", "시공진동"),
    ("次元震", "시공진동"),
    ("多元世界", "다원세계"),
    ("平行世界", "평행세계"),
    ("並行世界", "평행세계"),
    ("次元の壁", "차원의 벽"),
    ("次元の穴", "차원의 구멍 《어비스》"),
    ("アビス", "어비스"),
    ("スフィア・リアクター", "스피어 리액터"),
    ("スフィア", "스피어"),
    ("次元獣", "차원수"),
    ("黒の英知", "검은 영지"),
    ("特異点", "특이점"),
    ("UCW", "UCW"),
    ("ADW", "ADW"),
)


@dataclass
class TextPart:
    """제어코드 사이의 번역 단위."""

    text: str
    output: str | None = None
    task_id: int | None = None
    marker_map: dict[str, str] = field(default_factory=dict)


@dataclass
class EntryWork:
    entry_id: str
    source: str
    source_hash: str
    parts: list[str | TextPart]
    metadata: dict[str, Any]
    remaining: int = 0
    marker_misses: int = 0


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def source_tokens(value: str) -> list[str]:
    return CONTROL_RE.findall(value)


def is_translatable(value: str) -> bool:
    return bool(JAPANESE_RE.search(value))


def protect_terms(value: str) -> tuple[str, dict[str, str]]:
    """일본어 고유명사를 모델이 보존할 수 있는 ASCII marker로 바꾼다."""

    markers: dict[str, str] = {}
    protected = value
    # 긴 표현부터 처리해야 짧은 표현이 먼저 치환되지 않는다.
    for index, (source, target) in enumerate(sorted(PROTECTED_TERMS, key=lambda item: len(item[0]), reverse=True)):
        if source not in protected:
            continue
        # FFXIV 일본어→한국어 토크나이저는 밑줄을 반복 출력할 수 있으므로
        # 영숫자 marker를 사용한다. 복원 시 marker 내부 공백도 허용한다.
        marker = f"SIKMARK{index:03d}"
        protected = protected.replace(source, marker)
        markers[marker] = target
    return protected, markers


def restore_terms(value: str, markers: dict[str, str]) -> tuple[str, int]:
    misses = 0
    restored = value
    for marker, target in markers.items():
        # 모델이 강조용 따옴표·공백을 marker 주변에 붙이는 경우를 정리한다.
        marker_pattern = re.escape(marker[:7]) + r"\s*" + re.escape(marker[7:])
        pattern = re.compile(r"[\s\"'「『（(]*" + marker_pattern + r"[\s\"'」』）)]*")
        if not pattern.search(restored):
            misses += 1
            continue
        restored = pattern.sub(target, restored)
    return restored, misses


def preserve_outer_space(source: str, translated: str) -> str:
    leading = re.match(r"^\s*", source).group(0)
    trailing = re.search(r"\s*$", source).group(0)
    body = translated.strip()
    if not body:
        body = translated.strip(" ")
    return leading + body + trailing


def clean_model_output(source: str, translated: str) -> str:
    """짧은 문장 앞에 모델이 덧붙이는 목록 기호를 제거한다."""

    value = translated.strip()
    if not source.lstrip().startswith(("-", "－", "―")):
        value = re.sub(r"^(?:[-－―]\s*)+", "", value)
    return value


def split_long_part(value: str, max_chars: int = 320) -> list[str]:
    """모델 입력 한도를 넘는 긴 문장을 문장부호 기준으로 나눈다."""

    if len(value) <= max_chars:
        return [value]
    pieces: list[str] = []
    start = 0
    for match in SENTENCE_RE.finditer(value):
        end = match.end()
        if end - start >= max_chars:
            pieces.append(value[start:end])
            start = end
    if start < len(value):
        pieces.append(value[start:])
    # 문장부호가 없는 초장문은 안전한 문자 경계로 나눈다.
    result: list[str] = []
    for piece in pieces:
        while len(piece) > max_chars:
            result.append(piece[:max_chars])
            piece = piece[max_chars:]
        if piece:
            result.append(piece)
    return result


def make_entry_work(entry: dict[str, Any]) -> EntryWork:
    source = str(entry.get("sourceText", ""))
    parts: list[str | TextPart] = []
    cursor = 0
    for match in CONTROL_RE.finditer(source):
        raw = source[cursor : match.start()]
        if raw:
            parts.append(TextPart(raw))
        parts.append(match.group(0))
        cursor = match.end()
    if cursor < len(source):
        parts.append(TextPart(source[cursor:]))
    if not parts:
        parts = [TextPart(source)]

    tasks = 0
    prepared: list[str | TextPart] = []
    for part in parts:
        if isinstance(part, str):
            prepared.append(part)
            continue
        if not is_translatable(part.text):
            part.output = part.text
            prepared.append(part)
            continue
        protected, marker_map = protect_terms(part.text)
        part.text = protected
        part.marker_map = marker_map
        # 인명·용어 단독 행은 모델을 거치지 않고 용어집 표기를 사용한다.
        if marker_map and not is_translatable(protected):
            part.output, _ = restore_terms(protected, marker_map)
            part.marker_map = {}
            prepared.append(part)
            continue
        # 아주 긴 조각은 여러 TextPart로 분할하되, 제어코드 경계는 유지한다.
        chunks = split_long_part(protected)
        if len(chunks) == 1:
            part.task_id = tasks
            tasks += 1
            prepared.append(part)
            continue
        for chunk in chunks:
            child = TextPart(chunk, marker_map=dict(marker_map), task_id=tasks)
            tasks += 1
            prepared.append(child)
    return EntryWork(
        entry_id=str(entry["entryId"]),
        source=source,
        source_hash=str(entry.get("sourceTextSha256") or sha256_text(source)),
        parts=prepared,
        metadata=entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {},
        remaining=tasks,
    )


def join_entry(work: EntryWork, translated_by_task: dict[int, str]) -> tuple[str, int]:
    output: list[str] = []
    misses = 0
    for part in work.parts:
        if isinstance(part, str):
            output.append(part)
            continue
        value = part.output
        if part.task_id is not None:
            value = translated_by_task.get(part.task_id, part.text)
        value = value if value is not None else part.text
        value, part_misses = restore_terms(value, part.marker_map)
        misses += part_misses
        value = preserve_outer_space(part.text, value)
        output.append(value)
    return "".join(output), misses


def estimate_visual_bytes(value: str) -> int:
    """게임 글리프표가 없는 환경에서 쓰는 보수적 길이 추정치.

    ASCII는 1바이트, 그 밖의 문자는 2바이트로 계산한다. 실제 TBL 인코딩
    검사는 패치 빌드 단계에서 수행하므로 이 값은 번역 초안의 선별용이다.
    """

    total = 0
    for char in value:
        total += 1 if ord(char) < 0x80 else 2
    return total


def length_report(text: str, metadata: dict[str, Any]) -> dict[str, Any]:
    limit = metadata.get("byteLimit")
    estimated = estimate_visual_bytes(text)
    # 시나리오 필드의 byteLimit은 실제 패치 글리프에서 한 칸으로 소비되는
    # 고정 슬롯 수로 관리되는 항목이 많다. 제어코드도 한 칸으로 계산한다.
    visual_slots = len(CONTROL_RE.sub("X", text))
    report: dict[str, Any] = {
        "estimatedBytes": estimated,
        "visualSlots": visual_slots,
        "byteLimit": limit,
        "overflow": bool(limit and visual_slots > int(limit)),
        "characters": len(text),
    }
    return report


def load_jsonl(path: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        result[str(item["entryId"])] = item
    return result


def append_jsonl(path: Path, item: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")


def build_translator(model_dir: Path, threads: int, backend: str):
    if backend == "ffxiv":
        try:
            from optimum.onnxruntime import ORTModelForSeq2SeqLM
            from transformers import BertJapaneseTokenizer, PreTrainedTokenizerFast
        except ImportError as exc:  # pragma: no cover - 환경 의존 오류
            raise SystemExit(
                "FFXIV 번역 모델에는 optimum[onnxruntime], transformers, fugashi, unidic-lite가 필요합니다. "
                "scripts/requirements-translation.txt를 설치하세요."
            ) from exc
        if not (model_dir / "onnxq").exists():
            raise SystemExit(
                f"FFXIV 번역 모델이 없습니다: {model_dir}\n"
                "Hugging Face sappho192/ffxiv-ja-ko-translator의 onnxq, src_tokenizer, "
                "trg_tokenizer를 해당 경로에 내려받으세요."
            )
        source_tokenizer = BertJapaneseTokenizer.from_pretrained(str(model_dir / "src_tokenizer"))
        target_tokenizer = PreTrainedTokenizerFast.from_pretrained(str(model_dir / "trg_tokenizer"))
        translator = ORTModelForSeq2SeqLM.from_pretrained(str(model_dir), subfolder="onnxq")
        return backend, source_tokenizer, target_tokenizer, translator, None

    try:
        import ctranslate2
        from transformers import AutoTokenizer
    except ImportError as exc:  # pragma: no cover - 환경 의존 오류
        raise SystemExit(
            "번역 실행에는 ctranslate2, transformers, sentencepiece가 필요합니다. "
            "scripts/requirements-translation.txt를 설치하세요."
        ) from exc
    if not model_dir.exists():
        raise SystemExit(
            f"변환된 모델이 없습니다: {model_dir}\n"
            "ct2-transformers-converter --model facebook/nllb-200-distilled-600M "
            f"--output_dir {model_dir} --quantization int8"
        )
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, src_lang="jpn_Jpan")
    translator = ctranslate2.Translator(
        str(model_dir), device="cpu", inter_threads=threads, intra_threads=4
    )
    target_token = tokenizer.convert_ids_to_tokens([tokenizer.convert_tokens_to_ids("kor_Hang")])[0]
    return backend, tokenizer, None, translator, target_token


def translate_tasks(tasks: list[tuple[int, str, dict[str, str]]], backend: str, tokenizer, target_tokenizer, translator, target_token, batch_size: int) -> dict[int, str]:
    if not tasks:
        return {}
    outputs: dict[int, str] = {}
    for start in range(0, len(tasks), batch_size):
        batch = tasks[start : start + batch_size]
        if backend == "ffxiv":
            texts = [text for _, text, _ in batch]
            encoded = tokenizer(
                texts,
                padding=True,
                truncation=True,
                max_length=512,
                return_attention_mask=True,
                return_token_type_ids=False,
                return_tensors="pt",
            )
            generated = translator.generate(**encoded, max_length=128, num_beams=1)
            decoded = target_tokenizer.batch_decode(generated, skip_special_tokens=True)
            for (task_id, _text, _markers), value in zip(batch, decoded):
                outputs[task_id] = clean_model_output(_text, value)
        else:
            source_tokens = [tokenizer.convert_ids_to_tokens(tokenizer(text).input_ids) for _, text, _ in batch]
            prefixes = [[target_token] for _ in batch]
            results = translator.translate_batch(
                source_tokens,
                target_prefix=prefixes,
                max_batch_size=batch_size,
                batch_type="examples",
                beam_size=1,
                max_input_length=1024,
                max_decoding_length=128,
                return_scores=False,
            )
            for (task_id, _text, _markers), result in zip(batch, results):
                token_ids = tokenizer.convert_tokens_to_ids(result.hypotheses[0])
                outputs[task_id] = clean_model_output(
                    _text, tokenizer.decode(token_ids, skip_special_tokens=True)
                )
        done = min(start + len(batch), len(tasks))
        print(f"  번역 단위 {done}/{len(tasks)}", flush=True)
    return outputs


def process_asset(path: Path, output_root: Path, backend: str, tokenizer, target_tokenizer, translator, target_token, batch_size: int) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    entries = document.get("entries") if isinstance(document.get("entries"), list) else []
    result_path = output_root / "results" / f"{path.stem}.jsonl"
    existing = load_jsonl(result_path)
    works: list[EntryWork] = []
    tasks: list[tuple[int, str, dict[str, str]]] = []
    task_id = 0
    for entry in entries:
        entry_id = str(entry.get("entryId", ""))
        source = str(entry.get("sourceText", ""))
        source_hash = str(entry.get("sourceTextSha256") or sha256_text(source))
        if entry_id in existing and existing[entry_id].get("sourceTextSha256") == source_hash:
            continue
        work = make_entry_work(entry)
        for part in work.parts:
            if not isinstance(part, TextPart) or part.task_id is None:
                continue
            part.task_id = task_id
            tasks.append((task_id, part.text, part.marker_map))
            task_id += 1
        works.append(work)

    print(f"[{path.name}] 신규 항목 {len(works)}개, 번역 단위 {len(tasks)}개", flush=True)
    translated = translate_tasks(tasks, backend, tokenizer, target_tokenizer, translator, target_token, batch_size)
    new_count = 0
    for work in works:
        text, marker_misses = join_entry(work, translated)
        report = length_report(text, work.metadata)
        item = {
            "entryId": work.entry_id,
            "sourceTextSha256": work.source_hash,
            "translation": text,
            "translationTextSha256": sha256_text(text),
            "translationStatus": "draft",
            "translator": f"NLLB {MODEL_ID} (offline, context glossary)",
            "reviewer": "",
            "referenceConsulted": False,
            "notes": "일본어 원문 기반 새 자동 초안; 기존 번역은 입력으로 사용하지 않음",
            "controls": {
                "sourceTokens": source_tokens(work.source),
                "translationTokens": source_tokens(text),
                "controlMatch": source_tokens(work.source) == source_tokens(text),
            },
            "length": report,
            "markerMisses": marker_misses,
        }
        append_jsonl(result_path, item)
        existing[work.entry_id] = item
        new_count += 1

    # 파일 단위 완료 표시는 재개 시 원문 SHA-256과 함께 확인한다.
    complete_path = output_root / "completed" / f"{path.stem}.json"
    json_dump(
        complete_path,
        {
            "format": "siok.scenario-retranslation-v2",
            "formatVersion": 1,
            "assetFile": path.name,
            "sourceFileSha256": sha256_text(path.read_text(encoding="utf-8")),
            "model": MODEL_ID,
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "entries": list(existing.values()),
        },
    )
    return {"asset": path.name, "entries": len(entries), "generated": new_count, "total": len(existing)}


def iter_assets(dialogue_root: Path, requested: set[str] | None) -> Iterable[Path]:
    for path in sorted(dialogue_root.glob("scenario_*.json")):
        if requested and path.stem not in requested and path.name not in requested:
            continue
        yield path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dialogue-root", type=Path, default=DEFAULT_DIALOGUE)
    parser.add_argument("--work-root", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_WORK / "models" / "ffxiv-ja-ko-translator")
    parser.add_argument("--backend", choices=("ffxiv", "nllb"), default="ffxiv")
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--asset", action="append", help="특정 scenario 파일 stem/name만 실행")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    requested = set(args.asset or [])
    backend, tokenizer, target_tokenizer, translator, target_token = build_translator(args.model_dir, max(1, args.threads), args.backend)
    args.work_root.mkdir(parents=True, exist_ok=True)
    summary: list[dict[str, Any]] = []
    assets = list(iter_assets(args.dialogue_root, requested))
    if not assets:
        raise SystemExit("처리할 scenario_*.json 파일이 없습니다.")
    print(f"시나리오 파일 {len(assets)}개를 처리합니다.", flush=True)
    for index, path in enumerate(assets, 1):
        print(f"[{index}/{len(assets)}] {path.name}", flush=True)
        summary.append(process_asset(path, args.work_root, backend, tokenizer, target_tokenizer, translator, target_token, args.batch_size))
    json_dump(
        args.work_root / "summary.json",
        {
            "format": "siok.scenario-retranslation-v2-summary",
            "model": MODEL_ID,
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "assets": summary,
        },
    )
    print("전체 번역 초안 생성 완료", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
