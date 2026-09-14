"""시나리오 HTML 뷰어용 로컬 정적 서버와 CPK 빌드 API."""

from __future__ import annotations

import argparse
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
from typing import Any
from urllib.parse import quote


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from siok_patch.scenario_cpk import (  # noqa: E402
    ScenarioCpkBuildError,
    build_scenario_cpk,
)


MAX_REQUEST_BYTES = 25 * 1024 * 1024
BUILD_ENDPOINT = "/__siok__/build-cpk"
# 휴대판 번들에만 들어가는 원본 CPK 대응표. 저장소에는 없으므로 아무 영향이 없다.
PORTABLE_SOURCES = REPOSITORY_ROOT / "config" / "portable-sources.json"


def _portable_source_entry(asset_key: str) -> dict[str, Any] | None:
    """번들에 포함된 원본 CPK 파일명·경로 대응표에서 한 항목을 읽는다.

    시나리오 JSON의 ``asset.fileName``이 실제 CPK 이름과 다른 분기 시나리오를
    번들에서 빌드할 수 있게 한다. 저장소의 번역 JSON은 건드리지 않는다.
    """

    if not PORTABLE_SOURCES.is_file():
        return None
    try:
        table = json.loads(PORTABLE_SOURCES.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise ScenarioCpkBuildError(
            f"휴대판 원본 대응표를 읽지 못했습니다: {error}"
        ) from error
    assets = table.get("assets") if isinstance(table, dict) else None
    entry = assets.get(asset_key) if isinstance(assets, dict) else None
    return entry if isinstance(entry, dict) else None


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")


class ScenarioViewerHandler(SimpleHTTPRequestHandler):
    """저장소 파일은 읽기만 제공하고 빌드 POST만 별도 처리한다."""

    server_version = "SiokScenarioViewer/1.0"

    def end_headers(self) -> None:  # noqa: N802 - stdlib handler API
        # 뷰어를 다시 실행했을 때 예전 HTML·카탈로그가 브라우저 캐시에 남아
        # 이전 화면으로 보이지 않도록 문서·JSON은 항상 최신 파일을 읽는다.
        path = self.path.split("?", 1)[0].lower()
        if path.endswith((".html", ".json")):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    def _send_json(self, status: int, value: object) -> None:
        payload = _json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _read_json_body(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length)
        except ValueError as error:
            raise ScenarioCpkBuildError("요청 본문의 크기를 확인할 수 없습니다.") from error
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ScenarioCpkBuildError(
                f"요청 본문은 1바이트 이상 {MAX_REQUEST_BYTES:,}바이트 이하여야 합니다."
            )
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ScenarioCpkBuildError(f"CPK 빌드 요청 JSON이 올바르지 않습니다: {error}") from error
        if not isinstance(value, dict):
            raise ScenarioCpkBuildError("CPK 빌드 요청의 최상위 값은 객체여야 합니다.")
        return value

    def _validate_asset_path(self, asset_key: str) -> None:
        """브라우저가 임의의 저장소 파일을 빌드 입력으로 지정하지 못하게 한다."""

        catalog_path = REPOSITORY_ROOT / "viewer" / "scenario-index.json"
        try:
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ScenarioCpkBuildError(f"시나리오 카탈로그를 읽지 못했습니다: {error}") from error
        assets = catalog.get("assets") if isinstance(catalog, dict) else None
        if not isinstance(assets, list):
            raise ScenarioCpkBuildError("시나리오 카탈로그의 assets가 올바르지 않습니다.")
        for item in assets:
            if isinstance(item, dict) and str(item.get("key")) == asset_key:
                relative = str(item.get("path") or "")
                path = (REPOSITORY_ROOT / "viewer" / relative).resolve()
                translations_root = (REPOSITORY_ROOT / "translations" / "dialogue").resolve()
                if path.is_file() and path.is_relative_to(translations_root):
                    return
                raise ScenarioCpkBuildError(f"카탈로그의 JSON 경로가 안전하지 않습니다: {path}")
        raise ScenarioCpkBuildError(f"카탈로그에 없는 시나리오입니다: {asset_key}")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path.rstrip("/") != BUILD_ENDPOINT:
            self._send_json(404, {"ok": False, "error": "지원하지 않는 API 경로입니다."})
            return
        try:
            request = self._read_json_body()
            data = request.get("data")
            if not isinstance(data, dict):
                raise ScenarioCpkBuildError("요청에 현재 시나리오 data 객체가 없습니다.")
            asset = data.get("asset")
            if not isinstance(asset, dict):
                raise ScenarioCpkBuildError("현재 시나리오 data.asset이 없습니다.")
            asset_key = str(request.get("assetKey") or asset.get("assetKey") or "").strip()
            if asset_key != str(asset.get("assetKey") or "").strip():
                raise ScenarioCpkBuildError("요청의 assetKey와 JSON asset.assetKey가 다릅니다.")
            self._validate_asset_path(asset_key)
            portable = _portable_source_entry(asset_key)
            if portable:
                if portable.get("fileName"):
                    asset["fileName"] = str(portable["fileName"])
                if portable.get("target"):
                    asset["target"] = str(portable["target"])
            report = build_scenario_cpk(data, repository_root=REPOSITORY_ROOT)
            expected_source = str((portable or {}).get("sourceSha256") or "").lower()
            actual_source = str(report.get("sourceSha256") or "").lower()
            if expected_source and actual_source != expected_source:
                raise ScenarioCpkBuildError(
                    "원본 CPK가 번들에 들어 있던 파일과 다릅니다. "
                    f"기대 {expected_source[:12]}…, 실제 {actual_source[:12]}… "
                    "번들을 다시 풀고 다시 시도하세요."
                )
            output_cpk = Path(str(report["outputCpk"])).resolve()
            output_report = Path(str(report["outputReport"])).resolve()
            if not output_cpk.is_relative_to(REPOSITORY_ROOT / "output"):
                raise ScenarioCpkBuildError("빌드 결과가 허용된 output 경계를 벗어났습니다.")
            result = dict(report)
            result["outputUrl"] = "/" + quote(
                output_cpk.relative_to(REPOSITORY_ROOT).as_posix(), safe="/"
            )
            result["reportUrl"] = "/" + quote(
                output_report.relative_to(REPOSITORY_ROOT).as_posix(), safe="/"
            )
            self._send_json(200, result)
        except ScenarioCpkBuildError as error:
            self._send_json(400, {"ok": False, "error": str(error)})
        except Exception as error:  # pragma: no cover - 최후의 API 안전망
            self._send_json(500, {"ok": False, "error": f"예상하지 못한 서버 오류: {error}"})

    def log_message(self, format: str, *args: object) -> None:
        print(f"[viewer] {format % args}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="시옥편 시나리오 뷰어 로컬 서버")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("포트는 1024~65535 범위여야 합니다.")
    handler = lambda *handler_args, **handler_kwargs: ScenarioViewerHandler(  # noqa: E731
        *handler_args, directory=str(REPOSITORY_ROOT), **handler_kwargs
    )
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"시옥편 뷰어 서버: http://127.0.0.1:{args.port}/viewer/시나리오_대사_뷰어.html")
    print("CPK 빌드 API: POST /__siok__/build-cpk")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n뷰어 서버를 종료합니다.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
