"""로컬 정규 번역 TSV에서 Git 게시용 정제 corpus를 결정론적으로 만든다.

일본어 원문 전문과 개인 경로는 내보내지 않는다. 원문 대응에는 UTF-8
SHA-256만 사용하고, 번역 후보·최종 번역·게임용 치환문은 자산별 TSV로
분리한다.
"""

from __future__ import annotations

import argparse
import csv
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import tempfile


_EXPECTED_SOURCE_FIELDS = (
    "entry_id",
    "scope",
    "asset_key",
    "internal_id",
    "source_row",
    "source_artifact",
    "source_artifact_sha256",
    "payload_sha256",
    "source_offset",
    "source_text",
    "google_translation",
    "legacy_translation",
    "translation",
    "replacement_text",
    "byte_limit",
    "encoded_length",
    "control_signature",
    "status",
    "reviewer",
    "notes",
)
_OUTPUT_FIELDS = (
    "entry_id",
    "scope",
    "asset_key",
    "internal_id",
    "source_row",
    "source_artifact",
    "source_artifact_sha256",
    "source_text_sha256",
    "google_translation",
    "legacy_translation",
    "translation",
    "replacement_text",
    "byte_limit",
    "encoded_length",
    "control_signature",
    "status",
    "notes",
)
_SCOPES = frozenset(("stage", "dlc", "dictionary"))
_PRIVATE_PATH = re.compile(r"(?i)(?:[A-Z]:\\Users\\|/Users/|/home/)")


class CorpusExportError(RuntimeError):
    """입력이나 출력이 안전한 corpus 계약을 만족하지 않을 때 발생한다."""


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _asset_file_name(asset_key: str) -> str:
    prefix = re.sub(r"[^A-Za-z0-9._-]+", "-", asset_key).strip(".-_")
    prefix = (prefix or "asset")[:72]
    suffix = sha256(asset_key.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}--{suffix}.tsv"


def _parse_args() -> argparse.Namespace:
    root = _project_root()
    parser = argparse.ArgumentParser(
        description="정규 번역 TSV를 원문 없는 Git 게시용 corpus로 분할합니다."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=root / "work" / "normalized" / "translations.tsv",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=root / "translations" / "corpus" / "data",
    )
    return parser.parse_args()


def export_corpus(source: Path, destination: Path) -> dict[str, object]:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    corpus_root = (_project_root() / "translations" / "corpus").resolve()
    if destination.parent != corpus_root or destination.name != "data":
        raise CorpusExportError(
            f"출력은 프로젝트의 translations/corpus/data여야 합니다: {destination}"
        )
    if not source.is_file():
        raise CorpusExportError(f"정규 번역 TSV를 찾을 수 없습니다: {source}")
    if destination.exists():
        raise CorpusExportError(
            f"기존 corpus를 덮어쓰지 않습니다. 먼저 검토하세요: {destination}"
        )

    temporary = Path(
        tempfile.mkdtemp(prefix=".corpus-export-", dir=corpus_root)
    ).resolve()
    handles: dict[tuple[str, str], object] = {}
    writers: dict[tuple[str, str], csv.DictWriter] = {}
    shard_counts: dict[tuple[str, str], int] = {}
    scope_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    entry_ids: set[str] = set()
    row_count = 0

    try:
        with source.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, delimiter="\t")
            if tuple(reader.fieldnames or ()) != _EXPECTED_SOURCE_FIELDS:
                raise CorpusExportError("정규 번역 TSV 헤더가 기대 스키마와 다릅니다.")

            for source_line, row in enumerate(reader, start=2):
                if None in row:
                    raise CorpusExportError(
                        f"TSV {source_line}행의 열 개수가 헤더와 다릅니다."
                    )
                entry_id = row["entry_id"]
                if not entry_id or entry_id in entry_ids:
                    raise CorpusExportError(
                        f"TSV {source_line}행의 entry_id가 비었거나 중복입니다: {entry_id!r}"
                    )
                entry_ids.add(entry_id)

                scope = row["scope"]
                asset_key = row["asset_key"]
                if scope not in _SCOPES:
                    raise CorpusExportError(
                        f"TSV {source_line}행의 scope를 지원하지 않습니다: {scope!r}"
                    )
                if not asset_key or any(
                    character in asset_key for character in ("\0", "\r", "\n")
                ):
                    raise CorpusExportError(
                        f"TSV {source_line}행의 asset_key가 비었거나 제어문자를 포함합니다."
                    )

                key = (scope, asset_key)
                if key not in writers:
                    shard_path = temporary / scope / _asset_file_name(asset_key)
                    shard_path.parent.mkdir(parents=True, exist_ok=True)
                    handle = shard_path.open("w", encoding="utf-8", newline="")
                    writer = csv.DictWriter(
                        handle,
                        fieldnames=_OUTPUT_FIELDS,
                        delimiter="\t",
                        lineterminator="\n",
                        extrasaction="raise",
                    )
                    writer.writeheader()
                    handles[key] = handle
                    writers[key] = writer
                    shard_counts[key] = 0

                source_text = row["source_text"]
                output_row = {
                    field: row[field]
                    for field in _OUTPUT_FIELDS
                    if field != "source_text_sha256"
                }
                output_row["source_text_sha256"] = sha256(
                    source_text.encode("utf-8")
                ).hexdigest()
                if any(_PRIVATE_PATH.search(value or "") for value in output_row.values()):
                    raise CorpusExportError(
                        f"TSV {source_line}행에서 개인 절대경로를 발견했습니다."
                    )
                writers[key].writerow(output_row)
                shard_counts[key] += 1
                scope_counts[scope] = scope_counts.get(scope, 0) + 1
                status = row["status"]
                status_counts[status] = status_counts.get(status, 0) + 1
                row_count += 1
    except Exception:
        for handle in handles.values():
            handle.close()
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    else:
        for handle in handles.values():
            handle.close()

    shards: list[dict[str, object]] = []
    for scope, asset_key in sorted(shard_counts):
        path = temporary / scope / _asset_file_name(asset_key)
        shards.append(
            {
                "scope": scope,
                "assetKey": asset_key,
                "path": path.relative_to(temporary).as_posix(),
                "rows": shard_counts[(scope, asset_key)],
                "bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )

    manifest: dict[str, object] = {
        "schemaVersion": 1,
        "kind": "siok-sanitized-translation-corpus",
        "sourceSnapshot": {
            "fileName": source.name,
            "rows": row_count,
            "bytes": source.stat().st_size,
            "sha256": _sha256_file(source),
        },
        "privacy": {
            "sourceTextIncluded": False,
            "sourceTextSha256Included": True,
            "absolutePathsIncluded": False,
            "sourceWorkbooksIncluded": False,
            "gameBinariesIncluded": False,
        },
        "columns": list(_OUTPUT_FIELDS),
        "counts": {
            "scope": dict(sorted(scope_counts.items())),
            "status": dict(sorted(status_counts.items())),
        },
        "shards": shards,
    }
    manifest_path = temporary / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, destination)
    return manifest


def main() -> int:
    args = _parse_args()
    manifest = export_corpus(args.source, args.output)
    print(
        "번역 corpus 생성 완료: "
        f"{manifest['sourceSnapshot']['rows']:,}행, "
        f"{len(manifest['shards'])}개 자산"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
