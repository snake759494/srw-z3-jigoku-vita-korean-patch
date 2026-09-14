"""프로젝트 설정을 읽고 안전한 출력 경계를 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping


class ConfigError(ValueError):
    """설정이 없거나 안전하게 사용할 수 없을 때 발생합니다."""


def project_root() -> Path:
    """설치 위치와 무관하게 이 프로젝트의 최상위 폴더를 반환합니다."""

    return Path(__file__).resolve().parents[2]


def _optional_path(value: object) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return Path(text).expanduser().resolve()


@dataclass(frozen=True)
class ProjectConfig:
    """사용자 PC에만 존재하는 로컬 경로 설정입니다."""

    archive_root: Path
    xdelta_path: Path | None = None
    cpk_tool_path: Path | None = None
    main_game_root: Path | None = None
    dlc_game_root: Path | None = None
    config_path: Path | None = None

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, object], *, config_path: Path | None = None
    ) -> "ProjectConfig":
        archive_root = _optional_path(data.get("archiveRoot"))
        if archive_root is None:
            raise ConfigError("archiveRoot가 비어 있습니다.")
        return cls(
            archive_root=archive_root,
            xdelta_path=_optional_path(data.get("xdeltaPath")),
            cpk_tool_path=_optional_path(data.get("cpkToolPath")),
            main_game_root=_optional_path(data.get("mainGameRoot")),
            dlc_game_root=_optional_path(data.get("dlcGameRoot")),
            config_path=config_path,
        )

    def as_json(self) -> dict[str, str]:
        def show(path: Path | None) -> str:
            return str(path) if path is not None else ""

        return {
            "archiveRoot": show(self.archive_root),
            "xdeltaPath": show(self.xdelta_path),
            "cpkToolPath": show(self.cpk_tool_path),
            "mainGameRoot": show(self.main_game_root),
            "dlcGameRoot": show(self.dlc_game_root),
        }


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ConfigError(f"설정 파일이 없습니다: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"JSON 형식이 올바르지 않습니다: {path} ({exc.lineno}행 {exc.colno}열)"
        ) from exc


def load_project_config(path: Path | None = None) -> ProjectConfig:
    config_path = (path or project_root() / "private" / "project.local.json").resolve()
    data = load_json(config_path)
    if not isinstance(data, dict):
        raise ConfigError(f"설정 최상위 값은 객체여야 합니다: {config_path}")
    return ProjectConfig.from_mapping(data, config_path=config_path)


def load_asset_groups(path: Path | None = None) -> dict[str, Any]:
    groups_path = (path or project_root() / "config" / "asset-groups.json").resolve()
    data = load_json(groups_path)
    if not isinstance(data, dict) or not isinstance(data.get("groups"), dict):
        raise ConfigError(f"자산 그룹 설정 형식이 올바르지 않습니다: {groups_path}")
    return data


def ensure_output_path(path: Path, root: Path | None = None) -> Path:
    """쓰기 대상이 work 또는 output 아래인지 확인합니다.

    존재하지 않는 경로도 ``resolve(strict=False)``로 정규화해 ``..`` 우회를
    차단합니다. 원본 아카이브와 기존 프로젝트에는 이 함수로 쓸 수 없습니다.
    """

    base = (root or project_root()).resolve()
    candidate = path.resolve(strict=False)
    allowed = [(base / "work").resolve(), (base / "output").resolve()]
    if not any(candidate == parent or candidate.is_relative_to(parent) for parent in allowed):
        raise ConfigError(
            f"안전하지 않은 출력 경로입니다: {candidate}\n"
            f"출력은 {allowed[0]} 또는 {allowed[1]} 아래여야 합니다."
        )
    return candidate
