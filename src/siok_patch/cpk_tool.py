"""CRI CPK를 명시적인 ID 매핑으로 추출하고 다시 만드는 어댑터."""

from __future__ import annotations

import csv
from dataclasses import dataclass
import json
import locale
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Protocol, Sequence

from .hashes import sha256_file


_ID_FILENAME = re.compile(r"^ID([0-9]{5})$")
_MAX_CPK_ID = 63354


class CpkToolError(RuntimeError):
    """CPK 도구 실행이나 결과 검증에 실패했을 때 발생한다."""


@dataclass(frozen=True, slots=True)
class CpkEntry:
    """추출된 ID 전용 CPK 멤버 하나."""

    member_id: int
    path: Path
    size: int
    sha256: str

    @property
    def filename(self) -> str:
        return member_filename(self.member_id)


@dataclass(frozen=True, slots=True)
class CpkCommandResult:
    """외부 도구 한 번의 실행 결과."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    elapsed_seconds: float


class CpkTool(Protocol):
    """대사 파이프라인에서 사용하는 최소 CPK 도구 인터페이스."""

    executable_sha256: str
    executable_version: str

    def extract(self, source_cpk: Path, output_dir: Path) -> dict[int, CpkEntry]: ...

    def pack(
        self,
        payload_dir: Path,
        member_ids: Sequence[int],
        output_cpk: Path,
    ) -> None: ...


def member_filename(member_id: int) -> str:
    """정수 ID를 CRI 도구의 추출 파일명으로 바꾼다."""

    if isinstance(member_id, bool) or not isinstance(member_id, int):
        raise CpkToolError(f"CPK 멤버 ID는 정수여야 합니다: {member_id!r}")
    if not 0 <= member_id <= _MAX_CPK_ID:
        raise CpkToolError(
            f"CPK 멤버 ID 범위를 벗어났습니다: {member_id} (0~{_MAX_CPK_ID})"
        )
    return f"ID{member_id:05d}"


def parse_member_id(value: str | int) -> int:
    """``3``, ``00003``, ``ID00003`` 표기를 같은 정수 ID로 읽는다."""

    if isinstance(value, bool):
        raise CpkToolError(f"올바르지 않은 CPK 멤버 ID입니다: {value!r}")
    if isinstance(value, int):
        member_filename(value)
        return value
    text = str(value).strip().upper()
    if text.startswith("ID"):
        text = text[2:]
    if not text.isdigit():
        raise CpkToolError(f"올바르지 않은 CPK 멤버 ID입니다: {value!r}")
    result = int(text, 10)
    member_filename(result)
    return result


def collect_id_entries(output_dir: Path) -> dict[int, CpkEntry]:
    """추출 폴더가 평면 ID 전용 구조인지 확인하고 해시를 계산한다."""

    root = output_dir.resolve()
    if not root.is_dir():
        raise CpkToolError(f"CPK 추출 폴더가 만들어지지 않았습니다: {root}")

    entries: dict[int, CpkEntry] = {}
    children = sorted(root.iterdir(), key=lambda item: item.name.casefold())
    if not children:
        raise CpkToolError(f"CPK에서 추출된 멤버가 없습니다: {root}")

    for path in children:
        if _is_reparse_point(path) or path.is_symlink():
            raise CpkToolError(f"추출 결과에 링크 또는 리파스 포인트가 있습니다: {path}")
        if not path.is_file():
            raise CpkToolError(
                "이 파이프라인은 ID 전용 평면 CPK만 지원합니다. "
                f"예상하지 못한 폴더가 있습니다: {path}"
            )
        match = _ID_FILENAME.fullmatch(path.name)
        if match is None:
            raise CpkToolError(
                "이 파이프라인은 ID00000 형식의 멤버만 지원합니다. "
                f"예상하지 못한 파일: {path.name}"
            )
        member_id = int(match.group(1), 10)
        member_filename(member_id)
        if member_id in entries:
            raise CpkToolError(f"중복 CPK 멤버 ID가 있습니다: {member_filename(member_id)}")
        entries[member_id] = CpkEntry(
            member_id=member_id,
            path=path.resolve(),
            size=path.stat().st_size,
            sha256=sha256_file(path),
        )
    return dict(sorted(entries.items()))


class CpkMakerTool:
    """로컬 ``cpkmakec.exe``를 고정 옵션으로 호출한다.

    디렉터리를 직접 입력하면 빈 ID가 있을 때 번호가 당겨질 수 있으므로, 리팩은
    반드시 ID 열이 들어간 CSV를 만든 뒤 ``-mode=ID``로 수행한다.
    """

    executable_version = "2.49.32.00 / CpkMaker.dll 3.24.00"

    def __init__(
        self,
        executable: Path,
        expected_sha256: str,
        *,
        log_path: Path | None = None,
        timeout_seconds: int = 300,
    ) -> None:
        self.executable = executable.resolve()
        self.executable_sha256 = expected_sha256.strip().lower()
        self.log_path = log_path.resolve() if log_path is not None else None
        self.timeout_seconds = timeout_seconds
        self._verified = False

    def verify_executable(self) -> None:
        """실행 전 도구 파일과 공개 lock의 SHA-256을 대조한다."""

        if not self.executable.is_file():
            raise CpkToolError(f"cpkmakec.exe를 찾을 수 없습니다: {self.executable}")
        if not re.fullmatch(r"[0-9a-f]{64}", self.executable_sha256):
            raise CpkToolError("tools.lock.json의 cpkmakec SHA-256 형식이 잘못되었습니다.")
        actual = sha256_file(self.executable).lower()
        if actual != self.executable_sha256:
            raise CpkToolError(
                "cpkmakec.exe의 SHA-256이 tools.lock.json과 다릅니다. "
                f"기대={self.executable_sha256}, 실제={actual}"
            )
        self._verified = True

    def extract(self, source_cpk: Path, output_dir: Path) -> dict[int, CpkEntry]:
        """CPK를 별도 빈 폴더에 추출하고 ID·경로·해시를 자체 검증한다."""

        self._ensure_verified()
        source = source_cpk.resolve()
        destination = output_dir.resolve(strict=False)
        if not source.is_file():
            raise CpkToolError(f"원본 CPK를 찾을 수 없습니다: {source}")
        if destination.exists():
            raise CpkToolError(f"CPK 추출 폴더가 이미 존재합니다: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.parent == destination:
            raise CpkToolError(f"안전하지 않은 CPK 추출 경로입니다: {destination}")

        result = self._run(
            [str(source), f"-extract={destination.name}", "-noerrorstop"],
            cwd=destination.parent,
        )
        if result.returncode != 0:
            raise CpkToolError(
                f"CPK 추출 도구가 실패했습니다(종료 코드 {result.returncode}). "
                f"로그: {self.log_path or '기록 안 함'}"
            )
        return collect_id_entries(destination)

    def pack(
        self,
        payload_dir: Path,
        member_ids: Sequence[int],
        output_cpk: Path,
    ) -> None:
        """명시적 ID CSV와 16바이트 정렬·무압축으로 CPK를 새로 만든다."""

        self._ensure_verified()
        payload_root = payload_dir.resolve()
        output = output_cpk.resolve(strict=False)
        if not payload_root.is_dir():
            raise CpkToolError(f"리팩 payload 폴더가 없습니다: {payload_root}")
        if output.exists():
            raise CpkToolError(f"리팩 출력 CPK가 이미 존재합니다: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)

        normalized_ids = tuple(sorted(member_ids))
        if len(set(normalized_ids)) != len(normalized_ids):
            raise CpkToolError("리팩 ID 목록에 중복이 있습니다.")
        if not normalized_ids:
            raise CpkToolError("리팩할 CPK 멤버가 없습니다.")
        for member_id in normalized_ids:
            filename = member_filename(member_id)
            path = payload_root / filename
            if not path.is_file() or path.is_symlink() or _is_reparse_point(path):
                raise CpkToolError(f"리팩 payload가 없거나 안전하지 않습니다: {path}")

        try:
            relative_payload = payload_root.relative_to(output.parent)
        except ValueError as error:
            raise CpkToolError(
                "payload 폴더와 출력 CPK는 같은 실행 폴더 아래에 있어야 합니다."
            ) from error
        if relative_payload.parts and relative_payload.parts[0] == "..":
            raise CpkToolError("리팩 payload 상대 경로가 실행 폴더를 벗어납니다.")

        manifest = output.parent / "cpk-build.csv"
        if manifest.exists():
            raise CpkToolError(f"리팩 CSV가 이미 존재합니다: {manifest}")
        with manifest.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\r\n")
            for member_id in normalized_ids:
                local_path = (relative_payload / member_filename(member_id)).as_posix()
                writer.writerow((local_path, "", str(member_id), "UC"))

        result = self._run(
            [
                manifest.name,
                output.name,
                "-align=16",
                "-code=UTF-8",
                "-mode=ID",
                "-mask",
                "-nodatetime",
                "-noerrorstop",
                "-view",
            ],
            cwd=output.parent,
        )
        if result.returncode != 0:
            raise CpkToolError(
                f"CPK 리팩 도구가 실패했습니다(종료 코드 {result.returncode}). "
                f"로그: {self.log_path or '기록 안 함'}"
            )
        if not output.is_file() or output.stat().st_size < 0x800:
            raise CpkToolError(f"리팩 CPK가 생성되지 않았거나 너무 작습니다: {output}")
        with output.open("rb") as stream:
            signature = stream.read(4)
        if signature != b"CPK ":
            raise CpkToolError(f"리팩 결과의 CPK 서명이 올바르지 않습니다: {output}")

    def _ensure_verified(self) -> None:
        if not self._verified:
            self.verify_executable()

    def _run(self, arguments: list[str], *, cwd: Path) -> CpkCommandResult:
        argv = [str(self.executable), *arguments]
        started = time.monotonic()
        try:
            completed = subprocess.run(
                argv,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                check=False,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise CpkToolError(
                f"CPK 도구가 {self.timeout_seconds}초 안에 끝나지 않았습니다."
            ) from error
        except OSError as error:
            raise CpkToolError(f"CPK 도구를 실행할 수 없습니다: {self.executable}") from error

        elapsed = time.monotonic() - started
        # UTF-8 모드(-X utf8, 휴대판 실행기가 사용)에서 getpreferredencoding은
        # utf-8을 돌려주지만 cpkmakec는 시스템 ANSI 코드페이지로 출력한다.
        # getencoding은 UTF-8 모드의 영향을 받지 않으므로 로그가 깨지지 않는다.
        encoding = (
            locale.getencoding()
            if hasattr(locale, "getencoding")
            else locale.getpreferredencoding(False)
        ) or "utf-8"
        stdout = completed.stdout.decode(encoding, errors="replace")
        stderr = completed.stderr.decode(encoding, errors="replace")
        result = CpkCommandResult(
            argv=tuple(argv),
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            elapsed_seconds=elapsed,
        )
        self._write_log(result, cwd)
        return result

    def _write_log(self, result: CpkCommandResult, cwd: Path) -> None:
        if self.log_path is None:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "argv": list(result.argv),
            "cwd": str(cwd),
            "returnCode": result.returncode,
            "elapsedSeconds": round(result.elapsed_seconds, 6),
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        with self.log_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.stat(follow_symlinks=False).st_file_attributes
    except AttributeError:
        return False
    return bool(attributes & getattr(os.stat_result, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


__all__ = [
    "CpkCommandResult",
    "CpkEntry",
    "CpkMakerTool",
    "CpkTool",
    "CpkToolError",
    "collect_id_entries",
    "member_filename",
    "parse_member_id",
]
