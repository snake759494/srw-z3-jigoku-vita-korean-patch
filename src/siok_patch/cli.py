"""한국어 단일 명령줄 진입점."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import traceback

from .config import (
    ConfigError,
    ProjectConfig,
    load_json,
    load_project_config,
    project_root,
)
from .cpk_tool import CpkMakerTool, CpkToolError, parse_member_id
from .dialogue_pipeline import (
    AllowedControlChange,
    DialogueBuildError,
    DialogueEntryRequest,
    build_dialogue_cpk,
    build_dialogue_cpk_from_manifest,
)
from .dialogue_manifest import DialogueManifestError
from .pipeline import (
    check_translations,
    doctor,
    import_legacy_translations,
    release_readiness,
    status,
    write_report,
)
from .retranslation import (
    RetranslationError,
    apply_retranslation_overlays,
    export_retranslation_task,
    load_retranslation_progress,
    publish_retranslation_task,
    validate_retranslation_overlays,
)


def _console_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def _yes_no(value: bool) -> str:
    return "통과" if value else "확인 필요"


def _show_doctor(report: dict) -> None:
    print(f"\n환경 검사: {_yes_no(bool(report['ok']))}\n")
    for item in report["checks"]:
        mark = "[통과]" if item["ok"] else ("[경고]" if not item["required"] else "[실패]")
        print(f"{mark} {item['name']}: {item['detail']}")


def _show_import(report: dict) -> None:
    counts = report.get("counts", {})
    print("\n기존 XLSX 가져오기가 끝났습니다.")
    print(f"- 인식한 XLSX: {counts.get('files', report.get('assetCount', 0))}개")
    print(f"- 가져온 번역 행: {counts.get('rows', report.get('rowCount', 0)):,}개")
    if counts.get("blocked"):
        print(f"- 차단된 행: {counts['blocked']:,}개")
    for path in report.get("outputFiles", []):
        print(f"- TSV: {path}")
    print(f"- 보고서: {report['reportPath']}")
    warnings = report.get("warnings", [])
    if warnings:
        print(f"- 경고 {len(warnings)}개(보고서에서 확인)")


def _show_check(report: dict) -> None:
    print(f"\n번역 자료 검사: {_yes_no(bool(report['ok']))}")
    print(f"- 행: {report['rows']:,}개")
    print(f"- 고유 ID: {report['uniqueEntryIds']:,}개")
    print(f"- 상태: {json.dumps(report['statusCounts'], ensure_ascii=False)}")
    if report["errors"]:
        print("- 대표 오류:")
        for item in report["errors"][:10]:
            location = f"{item['file']}:{item['line']}" if item["file"] else "프로젝트"
            print(f"  · {item['code']} — {location} — {item['detail']}")
    if report["warnings"]:
        print(f"- 경고 예시 {len(report['warnings'])}개가 기록되었습니다.")
    print(f"- 보고서: {report['reportPath']}")


def _show_retranslation_check(report: dict) -> None:
    print(f"\n재번역 JSON 검사: {_yes_no(bool(report['ok']))}")
    print(f"- 자산: {report['scope']}/{report['assetKey']}")
    print(f"- 행: {report['rowCount']:,}개")
    print(f"- 상태: {json.dumps(report['statusCounts'], ensure_ascii=False)}")
    for item in report["errors"][:10]:
        location = f" ({item['entryId']})" if item["entryId"] else ""
        print(f"  · {item['code']}{location} — {item['detail']}")
    if report["warnings"]:
        print(f"- 경고: {len(report['warnings'])}개")


def _prompt_path(label: str, current: Path | None, *, required: bool = False) -> Path | None:
    shown = str(current) if current is not None else "비어 있음"
    while True:
        value = input(f"{label} [{shown}] (그대로 두려면 Enter): ").strip().strip('"')
        if not value:
            if required and current is None:
                print("이 경로는 반드시 입력해야 합니다.")
                continue
            return current
        return Path(value).expanduser().resolve()


def _configure() -> int:
    path = project_root() / "private" / "project.local.json"
    try:
        current = load_project_config(path)
    except ConfigError:
        current = None
    print("\n로컬 경로 설정입니다. 게임 파일은 새 프로젝트로 복사되지 않습니다.")
    archive = _prompt_path(
        "기존 PCSG00264 작업 아카이브",
        current.archive_root if current else None,
        required=True,
    )
    xdelta = _prompt_path("xdelta3.exe", current.xdelta_path if current else None)
    cpk_tool = _prompt_path(
        "CRI cpkmakec.exe",
        current.cpk_tool_path if current else None,
    )
    main = _prompt_path("본인이 덤프한 본편 원본(선택)", current.main_game_root if current else None)
    dlc = _prompt_path("본인이 덤프한 DLC 원본(선택)", current.dlc_game_root if current else None)
    assert archive is not None
    updated = ProjectConfig(
        archive_root=archive,
        xdelta_path=xdelta,
        cpk_tool_path=cpk_tool,
        main_game_root=main,
        dlc_game_root=dlc,
        config_path=path,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(updated.as_json(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    print(f"설정을 저장했습니다: {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="siok-patch",
        description="시옥편 한글패치 자료를 안전하게 정리하고 검사합니다.",
    )
    parser.add_argument("--debug", action="store_true", help="오류의 상세 추적을 표시합니다.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("configure", help="이 PC의 입력 경로를 설정합니다.")
    sub.add_parser("doctor", help="폴더와 필수 XLSX를 검사합니다.")
    importer = sub.add_parser("import", help="기존 XLSX를 정규 TSV로 가져옵니다.")
    importer.add_argument(
        "--groups",
        nargs="+",
        choices=("stage", "dlc", "dictionary", "eboot", "aid", "rpw", "srvc", "kdata"),
        help="생략하면 stage, dlc, dictionary만 가져옵니다.",
    )
    sub.add_parser("check", help="가져온 번역 TSV의 구조를 검사합니다.")
    sub.add_parser("status", help="현재 진행 상태를 요약합니다.")
    sub.add_parser("release-check", help="배포 가능 여부를 엄격하게 검사합니다.")
    retranslate = sub.add_parser(
        "retranslate",
        help="기존 번역을 참조 전용으로 두고 새 번역 JSON을 관리합니다.",
    )
    retranslate_sub = retranslate.add_subparsers(
        dest="retranslate_command",
        required=True,
    )
    retranslate_export = retranslate_sub.add_parser(
        "export",
        help="원문과 기존 번역을 담은 로컬 작업 JSON을 만듭니다.",
    )
    retranslate_export.add_argument("--scope", required=True, help="자산 범위(예: stage)")
    retranslate_export.add_argument("--asset", required=True, help="자산 키(예: STG0001a)")
    retranslate_export.add_argument(
        "--source",
        type=Path,
        default=project_root() / "work" / "normalized" / "translations.tsv",
        help="원문이 포함된 정규 TSV",
    )
    retranslate_export.add_argument(
        "--output",
        type=Path,
        help="로컬 작업 JSON 출력 경로(work 또는 output 아래)",
    )
    retranslate_export.add_argument(
        "--force",
        action="store_true",
        help="이미 있는 로컬 작업 JSON을 명시적으로 다시 만듭니다.",
    )
    retranslate_publish = retranslate_sub.add_parser(
        "publish",
        help="작업 JSON에서 원문·기존 번역을 제거한 오버레이를 만듭니다.",
    )
    retranslate_publish.add_argument("--task", required=True, type=Path, help="로컬 작업 JSON")
    retranslate_publish.add_argument("--output", type=Path, help="공개 오버레이 JSON 경로")
    retranslate_publish.add_argument(
        "--force",
        action="store_true",
        help="이미 있는 오버레이를 명시적으로 다시 만듭니다.",
    )
    for command_name, command_help in (
        ("check", "새 번역 JSON의 원문 해시와 제어코드를 검사합니다."),
        ("apply", "검증된 새 번역을 별도 정규 TSV로 병합합니다."),
    ):
        command = retranslate_sub.add_parser(command_name, help=command_help)
        inputs = command.add_mutually_exclusive_group()
        inputs.add_argument(
            "--overlay",
            action="append",
            type=Path,
            help="검사할 오버레이 JSON(여러 번 지정 가능)",
        )
        inputs.add_argument(
            "--progress",
            type=Path,
            help="등록된 모든 오버레이를 읽을 진행 매니페스트",
        )
        command.add_argument(
            "--source",
            type=Path,
            default=project_root() / "work" / "normalized" / "translations.tsv",
            help="원문이 포함된 정규 TSV",
        )
        if command_name == "apply":
            command.add_argument(
                "--output",
                type=Path,
                help="병합 TSV 출력 경로(work/retranslation/merged 아래)",
            )
            command.add_argument(
                "--allow-partial",
                action="store_true",
                help="진행 매니페스트 대신 일부 --overlay만 병합함을 명시적으로 허용합니다.",
            )
    dialogue = sub.add_parser(
        "dialogue-build",
        help="CPK 추출→XLSX/JSON 대사 반영→리팩→재추출 검증을 수행합니다.",
    )
    dialogue.add_argument("--cpk", required=True, type=Path, help="읽기 전용 원본 CPK")
    dialogue_input = dialogue.add_mutually_exclusive_group(required=True)
    dialogue_input.add_argument(
        "--entry",
        action="append",
        metavar="대상ID[@원본ID]=번역.xlsx",
        help=(
            "예: ID00004=번역.xlsx. 원본에 없는 ID3을 ID4에서 만들 때는 "
            "ID00003@ID00004=번역.xlsx (여러 번 지정 가능)"
        ),
    )
    dialogue_input.add_argument(
        "--manifest",
        type=Path,
        help="이전에 생성·검증한 dialogue-manifest.json",
    )
    dialogue.add_argument(
        "--allow-control-change",
        action="append",
        default=[],
        metavar="대상ID:XLSX행",
        help="검수한 제어 토큰 변경만 예외 승인합니다(예: ID00003:163).",
    )
    return parser


def _parse_dialogue_entry(value: str) -> DialogueEntryRequest:
    mapping, separator, workbook_text = value.partition("=")
    if not separator or not mapping.strip() or not workbook_text.strip():
        raise DialogueBuildError(
            "--entry 형식은 대상ID[@원본ID]=번역.xlsx 입니다: " + value
        )
    parts = mapping.split("@")
    if len(parts) > 2 or any(not part.strip() for part in parts):
        raise DialogueBuildError(
            "--entry의 ID 매핑 형식이 올바르지 않습니다: " + mapping
        )
    target_id = parse_member_id(parts[0])
    source_id = parse_member_id(parts[1]) if len(parts) == 2 else None
    return DialogueEntryRequest(
        target_id=target_id,
        source_id=source_id,
        workbook_path=Path(workbook_text.strip().strip('"')).expanduser().resolve(),
    )


def _parse_control_change(value: str) -> AllowedControlChange:
    member_text, separator, row_text = value.rpartition(":")
    if not separator or not member_text.strip() or not row_text.isdigit():
        raise DialogueBuildError(
            "--allow-control-change 형식은 대상ID:XLSX행 입니다: " + value
        )
    return AllowedControlChange(parse_member_id(member_text), int(row_text, 10))


def _configured_cpk_tool(config: ProjectConfig) -> CpkMakerTool:
    if config.cpk_tool_path is None:
        raise ConfigError(
            "cpkToolPath가 비어 있습니다. 메뉴 1번 또는 configure로 cpkmakec.exe를 설정하세요."
        )
    lock = load_json(project_root() / "config" / "tools.lock.json")
    item = lock.get("cpkmakec") if isinstance(lock, dict) else None
    expected = item.get("executableSha256") if isinstance(item, dict) else None
    if not isinstance(expected, str):
        raise ConfigError("tools.lock.json에 cpkmakec 실행 파일 SHA-256이 없습니다.")
    return CpkMakerTool(config.cpk_tool_path, expected)


def main(argv: list[str] | None = None) -> int:
    _console_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "configure":
            return _configure()
        if args.command == "retranslate":
            if args.retranslate_command == "export":
                report = export_retranslation_task(
                    args.source,
                    scope=args.scope,
                    asset_key=args.asset,
                    output_path=args.output,
                    overwrite=args.force,
                )
                print("\n재번역 작업 JSON을 만들었습니다.")
                print(f"- 자산: {report['scope']}/{report['assetKey']}")
                print(f"- 행: {report['rowCount']:,}개")
                print(f"- 작업 JSON: {report['outputPath']}")
                print("- 기존 번역은 references 아래의 참조값이며 새 번역으로 자동 채택되지 않습니다.")
                return 0
            if args.retranslate_command == "publish":
                report = publish_retranslation_task(
                    args.task,
                    output_path=args.output,
                    overwrite=args.force,
                )
                print("\n원문과 기존 번역을 제거한 재번역 오버레이를 만들었습니다.")
                print(f"- 자산: {report['scope']}/{report['assetKey']}")
                print(f"- 행: {report['rowCount']:,}개")
                print(f"- 오버레이: {report['outputPath']}")
                return 0
            if args.overlay:
                if args.retranslate_command == "apply" and not args.allow_partial:
                    raise RetranslationError(
                        "일부 --overlay만 병합하려면 --allow-partial을 함께 지정하세요. "
                        "기본값은 progress.json의 전체 오버레이입니다."
                    )
                overlay_paths = args.overlay
            else:
                progress_path = (
                    args.progress
                    if args.progress is not None
                    else project_root() / "translations" / "retranslation" / "progress.json"
                )
                progress = load_retranslation_progress(args.source, progress_path)
                overlay_paths = progress["overlayPaths"]
            if args.retranslate_command == "check":
                bundle = validate_retranslation_overlays(args.source, overlay_paths)
                for validation in bundle["reports"]:
                    _show_retranslation_check(validation)
                for item in bundle["errors"]:
                    print(f"  · {item['code']} ({item['entryId']}) — {item['detail']}")
                return 0 if bundle["ok"] else 2
            report = apply_retranslation_overlays(
                args.source,
                overlay_paths,
                output_path=args.output,
            )
            print("\n새 번역을 별도 TSV에 병합했습니다.")
            print(f"- 반영 행: {report['appliedRows']:,}개")
            print(f"- 미검수 초안 행: {report['draftRows']:,}개")
            print(f"- 차단된 오버레이 행: {report['blockedRows']:,}개")
            print(f"- 재번역 대기 행: {report['pendingRows']:,}개")
            print(f"- 전체 행: {report['totalRows']:,}개")
            print(f"- 병합 TSV: {report['outputPath']}")
            print("- 문자표 치환 전이므로 반영 행은 빌드 승인 상태가 아닙니다.")
            return 0
        config = load_project_config()
        if args.command == "doctor":
            report = doctor(config)
            report["reportPath"] = str(write_report("doctor.json", report))
            _show_doctor(report)
            print(f"\n보고서: {report['reportPath']}")
            return 0 if report["ok"] else 2
        if args.command == "import":
            environment = doctor(config)
            if not environment["ok"]:
                _show_doctor(environment)
                print("\n필수 입력이 맞지 않아 가져오기를 시작하지 않았습니다.")
                return 2
            report = import_legacy_translations(config, groups=args.groups)
            _show_import(report)
            return 0
        if args.command == "check":
            report = check_translations()
            _show_check(report)
            return 0 if report["ok"] else 2
        if args.command == "dialogue-build":
            tool = _configured_cpk_tool(config)
            if args.manifest is not None:
                if args.allow_control_change:
                    raise DialogueBuildError(
                        "--allow-control-change는 XLSX를 JSON으로 만들 때만 사용합니다. "
                        "JSON 재빌드에서는 매니페스트에 결박된 승인을 사용합니다."
                    )
                report = build_dialogue_cpk_from_manifest(
                    args.cpk,
                    args.manifest,
                    tool,
                )
            else:
                requests = [_parse_dialogue_entry(value) for value in args.entry]
                allowed = [
                    _parse_control_change(value)
                    for value in args.allow_control_change
                ]
                report = build_dialogue_cpk(
                    args.cpk,
                    requests,
                    tool,
                    allowed_control_changes=allowed,
                )
            print("\n대사 CPK 생성과 재추출 검증을 완료했습니다.")
            print(f"- 입력 방식: {report['inputMode'].upper()}")
            print(f"- 원본 SHA-256: {report['sourceSha256']}")
            print(f"- 결과 SHA-256: {report['outputSha256']}")
            print(f"- 수정 ID: {', '.join(item['targetId'] for item in report['entries'])}")
            print(f"- 재사용 JSON: {report['dialogueManifest']}")
            print(f"- 결과 CPK: {report['outputCpk']}")
            print(f"- 검증 보고서: {report['reportPath']}")
            print("- 다음 단계: Vita3K 복사본에서 시험한 뒤 실기 Vita는 별도로 검증하세요.")
            return 0
        if args.command == "status":
            report = status()
            print(f"\n프로젝트: {report['projectRoot']}")
            if report["normalizedFiles"]:
                print("가져온 번역 TSV: " + ", ".join(report["normalizedFiles"]))
            else:
                print("가져온 번역 TSV: 아직 없음")
            imported = report.get("importReport") or {}
            counts = imported.get("counts", {}) if isinstance(imported, dict) else {}
            if counts:
                print(f"마지막 가져오기: XLSX {counts.get('files', 0)}개, 행 {counts.get('rows', 0):,}개")
            print("배포 상태: 차단됨 — " + report["releaseNote"])
            return 0
        if args.command == "release-check":
            report = release_readiness()
            print("\n배포 가능 여부: 차단됨")
            for blocker in report["blockers"]:
                print(f"- {blocker}")
            print(f"- 보고서: {report['reportPath']}")
            return 2
        parser.error("알 수 없는 명령입니다.")
    except (
        ConfigError,
        CpkToolError,
        DialogueBuildError,
        DialogueManifestError,
        RetranslationError,
        OSError,
        ValueError,
    ) as exc:
        print(f"\n[중단] {exc}", file=sys.stderr)
        if getattr(args, "debug", False):
            traceback.print_exc()
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
