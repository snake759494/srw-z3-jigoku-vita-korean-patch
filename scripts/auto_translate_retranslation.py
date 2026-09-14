"""일본어 원문만 사용해 재번역 작업 JSON을 자동 초안으로 채운다.

이 스크립트는 공개 번역 오버레이를 만들기 전에 원문을 외부 번역 서비스에
전달한다. 기존 번역 열은 읽지 않으며, 생성한 모든 행은 ``draft`` 상태로
남긴다. 서비스가 실패하거나 제어 토큰을 보존하지 못한 행은 예외로 중단해
부분 결과가 조용히 게시되지 않도록 한다.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from siok_patch.config import project_root
from siok_patch.hashes import sha256_file
from siok_patch.retranslation import (
    FORMAT_VERSION,
    OVERLAY_FORMAT,
    TASK_FORMAT,
    publish_retranslation_task,
    source_binding_sha256,
    source_text_sha256,
)
from siok_patch.translation_io import TranslationRow, read_tsv


# 달러형 제어 코드뿐 아니라 게임의 기호형 제어 코드도 보호한다.
# 기호형 코드는 번역 서비스가 일반 기호로 오인해 누락시키기 쉽다.
CONTROL_TOKEN = re.compile(
    r"[$＄][nNlLcCfFｎＮｌＬｃＣｆＦ]|[⑲⑳㊥㊦㊧㊨]"
)
# 일본어 가운데점(・)은 고유명사 구분자로 한국어에도 사용할 수 있으므로
# 잔류 검사에서 제외하고, 실제 히라가나/가타카나만 잡는다.
JAPANESE_TEXT = re.compile(r"[\u3040-\u309f\u30a0-\u30fa\u30fc-\u30ff]")
PLACEHOLDER = re.compile(r"__SIOK_CTRL_(\d+)__")


def _atomic_json(value: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 여러 자산/프로세스가 동시에 작업 JSON과 진행률을 갱신할 수 있으므로
    # 고정된 임시 파일명을 사용하면 서로 덮어쓰는 충돌이 발생한다.
    temp = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.candidate"
    )
    temp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def _protect_controls(text: str) -> tuple[str, tuple[str, ...]]:
    tokens: list[str] = []

    def replace(match: re.Match[str]) -> str:
        tokens.append(match.group(0))
        return f"__SIOK_CTRL_{len(tokens) - 1}__"

    return CONTROL_TOKEN.sub(replace, text), tuple(tokens)


def _restore_controls(text: str, tokens: tuple[str, ...]) -> str:
    for index, token in enumerate(tokens):
        marker = f"__SIOK_CTRL_{index}__"
        if marker not in text:
            raise RuntimeError(f"번역 서비스가 제어 토큰을 보존하지 않았습니다: {marker}")
        text = text.replace(marker, token)
    if PLACEHOLDER.search(text):
        raise RuntimeError("번역 결과에 알 수 없는 제어 토큰 표식이 남았습니다.")
    return text.strip("\n")


def _request_translation(text: str, *, retries: int = 6) -> list[str]:
    query = urlencode(
        {
            "client": "gtx",
            "sl": "ja",
            "tl": "ko",
            "dt": "t",
            "q": text,
        }
    )
    request = Request(
        "https://translate.googleapis.com/translate_a/single?" + query,
        headers={"User-Agent": "siok-retranslation/1.0"},
    )
    for attempt in range(retries):
        try:
            with urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8"))
            segments = payload[0]
            if not isinstance(segments, list):
                raise RuntimeError("번역 서비스 응답 세그먼트가 배열이 아닙니다.")
            result = [
                str(segment[0])
                for segment in segments
                if isinstance(segment, list) and segment and isinstance(segment[0], str)
            ]
            if not result:
                raise RuntimeError("번역 서비스가 빈 결과를 반환했습니다.")
            return result
        except HTTPError as exc:
            # Google의 URL 길이 제한은 재시도해도 해결되지 않으므로
            # 호출자가 배치를 더 작게 나눌 수 있게 즉시 전달한다.
            if exc.code in {400, 414}:
                raise
            if attempt + 1 >= retries:
                raise
            time.sleep(min(2**attempt, 12))
        except Exception:
            if attempt + 1 >= retries:
                raise
            time.sleep(min(2**attempt, 12))
    raise AssertionError("unreachable")


def _translate_one(source: str) -> str:
    protected, tokens = _protect_controls(source)
    result = "".join(_request_translation(protected))
    return _restore_controls(result, tokens)


def _translate_one_safe(source: str) -> str:
    """제어 토큰 손실 시 자산 전체가 중단되지 않도록 원문을 보류값으로 반환한다."""
    try:
        return _translate_one(source)
    except Exception:
        return source


def _translate_batch(items: list[tuple[int, str]]) -> dict[int, str]:
    protected: list[tuple[str, tuple[str, ...]]] = [
        _protect_controls(source) for _, source in items
    ]
    query = "\n".join(item[0] for item in protected)
    try:
        segments = _request_translation(query)
    except HTTPError as exc:
        if exc.code not in {400, 414}:
            raise
        if len(items) == 1:
            index, source = items[0]
            return {index: _translate_one_safe(source)}
        middle = len(items) // 2
        left = _translate_batch(items[:middle])
        right = _translate_batch(items[middle:])
        return {**left, **right}
    if len(segments) != len(items):
        if len(items) == 1:
            index, source = items[0]
            return {index: _translate_one_safe(source)}
        middle = len(items) // 2
        left = _translate_batch(items[:middle])
        right = _translate_batch(items[middle:])
        return {**left, **right}
    result: dict[int, str] = {}
    for (index, source), translated, (_, tokens) in zip(items, segments, protected):
        try:
            result[index] = _restore_controls(translated, tokens)
        except RuntimeError:
            result[index] = _translate_one_safe(source)
    return result


def _batches(entries: list[dict[str, Any]], max_chars: int) -> list[list[tuple[int, str]]]:
    batches: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    current_size = 0
    for index, entry in enumerate(entries):
        if entry.get("freshTranslation"):
            continue
        source = str(entry["sourceText"])
        protected, _ = _protect_controls(source)
        extra = len(protected) + (1 if current else 0)
        if current and current_size + extra > max_chars:
            batches.append(current)
            current = []
            current_size = 0
        current.append((index, source))
        current_size += len(protected) + (1 if len(current) > 1 else 0)
    if current:
        batches.append(current)
    return batches


def _make_task(rows: list[TranslationRow], source_sha: str, total_rows: int) -> dict[str, Any]:
    first = rows[0]
    return {
        "format": TASK_FORMAT,
        "formatVersion": FORMAT_VERSION,
        "scope": first.scope,
        "assetKey": first.asset_key,
        "sourceSnapshot": {
            "fileName": "translations.tsv",
            "sha256": source_sha,
            "rowCount": total_rows,
        },
        "translationPolicy": {
            "method": "fresh-from-japanese",
            "existingTranslations": "reference-only",
        },
        "entries": [
            {
                "entryId": row.entry_id,
                "sourceText": row.source_text,
                "sourceTextSha256": source_text_sha256(row.source_text),
                "sourceBindingSha256": source_binding_sha256(row),
                "references": {
                    "googleTranslation": row.google_translation,
                    "legacyTranslation": row.legacy_translation,
                    "importedTranslation": row.translation,
                    "previousReplacementText": row.replacement_text,
                },
                "freshTranslation": "",
                "translationStatus": "draft",
                "translator": "",
                "reviewer": "",
                "referenceConsulted": False,
                "byteLimit": row.byte_limit,
                "controlSignature": row.control_signature,
                "notes": "",
            }
            for row in rows
        ],
    }


def _load_or_create_task(
    task_path: Path,
    rows: list[TranslationRow],
    source_sha: str,
    total_rows: int,
) -> dict[str, Any]:
    if task_path.is_file():
        return json.loads(task_path.read_text(encoding="utf-8"))
    task = _make_task(rows, source_sha, total_rows)
    _atomic_json(task, task_path)
    return task


def _update_progress(
    root: Path,
    rows: list[TranslationRow],
    source_sha: str,
    output_root: Path,
) -> None:
    source_total = len(rows)
    by_asset: dict[tuple[str, str], int] = defaultdict(int)
    for row in rows:
        by_asset[(row.scope, row.asset_key)] += 1
    overlays: list[dict[str, Any]] = []
    root_dir = output_root
    for overlay_path in sorted(root_dir.glob("*/*.json")):
        if overlay_path.name in {"progress.json", "glossary.json"}:
            continue
        try:
            overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if overlay.get("format") != OVERLAY_FORMAT:
            continue
        entries = overlay.get("entries")
        if not isinstance(entries, list):
            continue
        counts = defaultdict(int)
        for entry in entries:
            if isinstance(entry, dict):
                counts[str(entry.get("translationStatus", "draft"))] += 1
        scope = str(overlay.get("scope", ""))
        asset_key = str(overlay.get("assetKey", ""))
        if counts.get("reviewed") == len(entries):
            status = "reviewed"
        elif counts.get("blocked") == len(entries):
            status = "blocked"
        else:
            status = "draft"
        overlays.append(
            {
                "scope": scope,
                "assetKey": asset_key,
                "overlay": f"{overlay_path.parent.name}/{overlay_path.name}",
                "rowCount": len(entries),
                "status": status,
            }
        )
    overlays.sort(key=lambda item: (item["scope"], item["assetKey"]))
    covered = sum(int(item["rowCount"]) for item in overlays)
    counts = defaultdict(int)
    for item in overlays:
        status = item["status"]
        if status == "reviewed":
            counts["reviewedAssets"] += 1
            counts["reviewedRows"] += int(item["rowCount"])
        else:
            counts["draftedAssets"] += 1
            counts["draftedRows"] += int(item["rowCount"])
    progress = {
        "$schema": "./progress.schema.json",
        "format": "siok-retranslation-progress",
        "formatVersion": FORMAT_VERSION,
        "baseline": {
            "fileName": "translations.tsv",
            "sha256": source_sha,
            "rowCount": source_total,
            "assetCount": len(by_asset),
        },
        "policy": {
            "method": "fresh-from-japanese",
            "existingTranslations": "reference-only",
            "automaticLegacyFallback": False,
            "mergeOutput": "derived-file-only",
        },
        "counts": {
            "draftedAssets": counts["draftedAssets"],
            "reviewedAssets": counts["reviewedAssets"],
            "draftedRows": counts["draftedRows"],
            "reviewedRows": counts["reviewedRows"],
            "remainingRows": max(0, source_total - covered),
        },
        "assets": overlays,
    }
    _atomic_json(progress, root_dir / "progress.json")


def _process_asset(
    root: Path,
    rows: list[TranslationRow],
    source_sha: str,
    total_rows: int,
    workers: int,
    max_chars: int,
    task_root: Path,
    output_root: Path,
) -> tuple[str, int]:
    first = rows[0]
    task_path = task_root / first.scope / f"{first.asset_key}.json"
    overlay_path = output_root / first.scope / f"{first.asset_key}.json"
    if overlay_path.is_file():
        return f"{first.scope}/{first.asset_key}", 0
    task = _load_or_create_task(task_path, rows, source_sha, total_rows)
    entries = task.get("entries")
    if not isinstance(entries, list) or not entries:
        raise RuntimeError(f"작업 JSON의 entries가 비어 있습니다: {task_path}")
    batches = _batches(entries, max_chars)
    translated = 0
    if batches:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_translate_batch, batch) for batch in batches]
            for future in as_completed(futures):
                result = future.result()
                for index, text in result.items():
                    if not text.strip():
                        source_text = str(entries[index].get("sourceText", ""))
                        trimmed = source_text.strip()
                        if trimmed and trimmed != source_text:
                            # 제목/메뉴 항목처럼 앞뒤 전각 공백이 많은 행은
                            # 번역 서비스가 빈 결과를 돌려줄 수 있으므로 공백을
                            # 제거한 원문으로 한 번 더 요청한다.
                            fallback = _translate_one(trimmed)
                            if fallback.strip():
                                leading = source_text[: len(source_text) - len(source_text.lstrip())]
                                trailing = source_text[len(source_text.rstrip()) :]
                                text = leading + fallback + trailing
                        if not text.strip():
                            # 빈 문자열을 그대로 두면 전체 자산이 중단되므로,
                            # 수동 검수 대상임을 명시하고 원문을 보류값으로 남긴다.
                            text = source_text
                            entries[index]["translator"] = "Codex 보류 - 빈 결과"
                            entries[index]["notes"] = (
                                "자동 번역 서비스가 빈 결과를 반환하여 원문 보류; 수동 번역 필요"
                            )
                    if JAPANESE_TEXT.search(text):
                        print(
                            f"  경고: 일본어 잔류 표현 수동 검수 필요 - {entries[index]['entryId']}",
                            flush=True,
                        )
                    entries[index]["freshTranslation"] = text
                    entries[index]["translationStatus"] = "draft"
                    entries[index]["translator"] = "Google Translate ja→ko (자동 초안)"
                    entries[index]["reviewer"] = ""
                    entries[index]["referenceConsulted"] = False
                    entries[index]["notes"] = "일본어 원문 기준 자동 번역 초안; 기존 번역 미참조"
                    translated += 1
                # 긴 자산도 중단 후 이미 완료한 행부터 재개할 수 있게 저장한다.
                _atomic_json(task, task_path)
                print(
                    f"  {first.scope}/{first.asset_key}: {translated}/{sum(len(b) for b in batches)}",
                    flush=True,
                )
    _atomic_json(task, task_path)
    publish_retranslation_task(
        task_path,
        output_path=overlay_path,
        output_root=output_root,
        overwrite=True,
    )
    return f"{first.scope}/{first.asset_key}", translated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=project_root() / "work" / "normalized" / "translations.tsv",
    )
    parser.add_argument(
        "--scope",
        action="append",
        help="지정하면 해당 범위만 번역합니다. 생략하면 전체 범위입니다.",
    )
    parser.add_argument(
        "--asset",
        action="append",
        help="지정하면 해당 자산만 번역합니다. 여러 번 지정할 수 있습니다.",
    )
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--max-chars", type=int, default=1200)
    parser.add_argument("--task-root", type=Path, default=project_root() / "work" / "retranslation" / "tasks", help="Local task JSON root")
    parser.add_argument("--output-root", type=Path, default=project_root() / "translations" / "retranslation", help="Public overlay root")
    args = parser.parse_args()
    if args.workers < 1 or args.workers > 16:
        parser.error("--workers는 1~16이어야 합니다.")
    if args.max_chars < 300 or args.max_chars > 5000:
        parser.error("--max-chars는 300~5000이어야 합니다.")

    root = project_root()
    source = args.source.resolve()
    task_root = args.task_root.resolve()
    output_root = args.output_root.resolve()
    rows = read_tsv(source)
    source_sha = sha256_file(source)
    selected_scopes = set(args.scope) if args.scope else {row.scope for row in rows}
    selected_assets = set(args.asset or ())
    grouped: dict[tuple[str, str], list[TranslationRow]] = defaultdict(list)
    for row in rows:
        if row.scope in selected_scopes and (not selected_assets or row.asset_key in selected_assets):
            grouped[(row.scope, row.asset_key)].append(row)
    print(f"대상 자산: {len(grouped)}개 / 행: {sum(len(v) for v in grouped.values()):,}개")
    for key in sorted(grouped):
        asset, translated = _process_asset(
            root,
            grouped[key],
            source_sha,
            len(rows),
            args.workers,
            args.max_chars,
            task_root,
            output_root,
        )
        _update_progress(root, rows, source_sha, output_root)
        print(f"완료: {asset} ({translated:,}행 자동 초안)", flush=True)
    _update_progress(root, rows, source_sha, output_root)
    print("전체 자동 초안 생성 단계가 끝났습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
