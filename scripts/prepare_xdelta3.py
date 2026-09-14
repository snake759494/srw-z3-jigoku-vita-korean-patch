#!/usr/bin/env python3
"""잠금된 공식 xdelta3 배포물을 내려받아 개인 도구 폴더에 준비한다.

압축 파일과 실행 파일의 SHA-256을 모두 확인하며, 실행 파일을 실행하지 않는다.
결과는 ``private/`` 또는 ``work/`` 아래에만 저장한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from urllib.request import Request, urlopen
import zipfile


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = REPOSITORY_ROOT / "config" / "tools.lock.json"
DEFAULT_OUTPUT = REPOSITORY_ROOT / "private" / "tools" / "xdelta3.exe"
ALLOWED_OUTPUT_ROOTS = (REPOSITORY_ROOT / "private", REPOSITORY_ROOT / "work")


class ToolPreparationError(RuntimeError):
    """도구 아카이브 검증 또는 추출에 실패했을 때 발생한다."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _load_lock(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ToolPreparationError(f"도구 잠금 파일을 읽을 수 없습니다: {path}") from error
    if not isinstance(value, dict) or not isinstance(value.get("xdelta3"), dict):
        raise ToolPreparationError("tools.lock.json에 xdelta3 항목이 없습니다.")
    return value["xdelta3"]


def _safe_output(path: Path) -> Path:
    candidate = path.expanduser().resolve(strict=False)
    if not any(candidate.is_relative_to(root) for root in ALLOWED_OUTPUT_ROOTS):
        raise ToolPreparationError("도구 출력은 저장소의 private/ 또는 work/ 아래여야 합니다.")
    if candidate.name.lower() != "xdelta3.exe":
        raise ToolPreparationError("출력 파일 이름은 xdelta3.exe여야 합니다.")
    return candidate


def _download(url: str, destination: Path) -> None:
    request = Request(url, headers={"User-Agent": "siok-vita-patch-preparer/1.0"})
    try:
        with urlopen(request, timeout=60) as response, destination.open("wb") as stream:
            while chunk := response.read(1024 * 1024):
                stream.write(chunk)
    except OSError as error:
        raise ToolPreparationError(f"도구 아카이브를 내려받지 못했습니다: {url}") from error


def prepare(lock_path: Path, output_path: Path, archive_path: Path | None, overwrite: bool) -> dict[str, str | int]:
    lock = _load_lock(lock_path.expanduser().resolve(strict=True))
    asset_url = lock.get("assetUrl")
    asset_hash = lock.get("assetSha256")
    executable_hash = lock.get("executableSha256")
    if not all(isinstance(value, str) and len(value) == 64 for value in (asset_hash, executable_hash)):
        raise ToolPreparationError("xdelta3 잠금값의 SHA-256이 올바르지 않습니다.")
    if archive_path is None and not isinstance(asset_url, str):
        raise ToolPreparationError("xdelta3 assetUrl이 잠금 파일에 없습니다.")

    destination = _safe_output(output_path)
    if destination.exists() and not overwrite:
        raise ToolPreparationError(f"출력 파일이 이미 있습니다: {destination} (교체하려면 --overwrite)")
    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary_archive: Path | None = None
    archive = archive_path.expanduser().resolve(strict=True) if archive_path is not None else None
    try:
        if archive is None:
            (REPOSITORY_ROOT / "work").mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix="xdelta3-", suffix=".zip", dir=REPOSITORY_ROOT / "work", delete=False
            ) as stream:
                temporary_archive = Path(stream.name)
            _download(asset_url, temporary_archive)
            archive = temporary_archive
        if not archive.is_file() or archive.is_symlink():
            raise ToolPreparationError(f"xdelta3 압축 파일이 일반 파일이 아닙니다: {archive}")
        actual_asset_hash = sha256_file(archive)
        if actual_asset_hash.lower() != asset_hash.lower():
            raise ToolPreparationError(
                f"xdelta3 압축 파일 SHA-256이 다릅니다: 예상 {asset_hash}, 실제 {actual_asset_hash}"
            )
        try:
            with zipfile.ZipFile(archive) as bundle:
                matches = [info for info in bundle.infolist() if Path(info.filename).name.lower() == "xdelta3.exe"]
                if len(matches) != 1:
                    raise ToolPreparationError("xdelta3.exe를 압축 파일에서 하나만 찾을 수 없습니다.")
                info = matches[0]
                executable = bundle.read(info)
        except (OSError, zipfile.BadZipFile, KeyError) as error:
            raise ToolPreparationError("xdelta3 압축 파일을 읽지 못했습니다.") from error
        actual_executable_hash = hashlib.sha256(executable).hexdigest()
        if actual_executable_hash.lower() != executable_hash.lower():
            raise ToolPreparationError(
                f"xdelta3 실행 파일 SHA-256이 다릅니다: 예상 {executable_hash}, 실제 {actual_executable_hash}"
            )
        temporary_output: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".xdelta3.", suffix=".tmp", dir=destination.parent, delete=False
            ) as stream:
                temporary_output = Path(stream.name)
                stream.write(executable)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_output, destination)
            temporary_output = None
        finally:
            if temporary_output is not None:
                temporary_output.unlink(missing_ok=True)
    finally:
        if temporary_archive is not None:
            temporary_archive.unlink(missing_ok=True)
    return {
        "output": str(destination),
        "archiveSha256": asset_hash.lower(),
        "executableSha256": executable_hash.lower(),
        "bytes": destination.stat().st_size,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK, help="공식 도구 잠금 JSON")
    parser.add_argument("--archive", type=Path, help="이미 내려받은 xdelta3 ZIP(없으면 공식 URL에서 다운로드)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="개인 도구 출력 경로")
    parser.add_argument("--overwrite", action="store_true", help="기존 xdelta3.exe 교체")
    args = parser.parse_args()
    try:
        result = prepare(args.lock, args.output, args.archive, args.overwrite)
    except (ToolPreparationError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
