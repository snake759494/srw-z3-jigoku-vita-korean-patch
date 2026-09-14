# SRVC 전투 대사 번역 보관

`PCSG00264`의 `SRVC.BIN` 전투 대사 번역표와 적용 결과를 원본 보존 정책에 맞게
기록한 자료다. 기존 원본 자료는 [공통 검수 JSON](../translations/dialogue/battle_SRVC.json)으로
통합했으며, 이 문서의 이전 생성 경로는
`translations/battle-dialogue/srvc/SRVC_BATTLE.json`이었다.
각 행에 일본어 원문과 한국어 번역을 함께 보관하고, 자기 소유 원본 BIN에 적용할
고정 길이 델타 페이로드와 원본 페이로드 해시도 포함한다. 게임 원본·완성 BIN·XLSX
파일 자체는 저장소에 복사하지 않는다.

## 입력 자료

사용자가 제공한 외부 폴더는 다음 두 파일로 구성되어 있었다.

- `SRVC_번역.xlsx`: `진행 현황`, `마스터 번역표`, `슬롯 위치` 3개 시트
- `SRVC.BIN`: 번역을 적용한 결과

비교에 사용한 원본과 문자표는 원본 아카이브의 별도 폴더에서 읽기만 했다.

| 자료 | 크기 | SHA-256 |
|---|---:|---|
| `SRVC_번역.xlsx` | 4,402,497바이트 | `7d92279447d54582418ff49075b4637fa8b7321c941df3fa481f2c33aad6ce` |
| 적용 `SRVC.BIN` | 2,193,440바이트 | `43d2cb427dea5c9026b4a4de6c51b47df392d419f164652302169f098aee4c1b` |
| 비교 원본 `SRVC-ori.BIN` | 2,193,440바이트 | `47c34677df2d11c152ac4741c9a3b4886215d19b23626057a01f7a16185356bf` |
| `shift-jis2.tbl` | 116,958바이트 | `d3ba05fb3936dbe931c7bfabd85db766c44a0388a143434b2de9203ae99b8899` |
| `Japanese - Hangul to Kanji.wReplace` | 29,686바이트 | `f4b1cff541a05a3b51bcbefe6c2ef4a9b7618cf7b1ea5a22e2f125e6dabbbdf3` |

## 번역표 검사 결과

- 고유 마스터 번역: 25,758행
- 슬롯 위치: 31,983행
- 고유 원문: 25,758개
- 빈 원문·빈 번역: 0행
- `1차 번역` 상태: 25,758행
- 모든 슬롯의 `1차 반영`: `예`
- 원문 바이트·번역 바이트·여유 바이트 대응 오류: 0행

마스터 행은 다음처럼 원문과 번역이 한 객체에 들어간다. `sourceTextSha256`는
행이 바뀌지 않았는지 확인하는 결박 값이고, `appliedPayloadHex`는 게임 문자표를
외부에 다시 설치하지 않아도 같은 고정 슬롯을 재현하기 위한 델타 데이터다.

```json
{
  "id": 1,
  "sourceText": "「行くぞ！」",
  "translation": "「간다！」",
  "sourceTextSha256": "...",
  "originalPayloadSha256": "...",
  "appliedPayloadSha256": "...",
  "appliedPayloadHex": "..."
}
```

## 적용 BIN 역검증

게임용 문자표는 일부 구두점과 공백을 전각 또는 대체 문자로 저장하고, 줄바꿈을
문자열 `\\n`으로 보관한다. 그래서 단순 문자열 비교가 아니라
`srvc-hangul-wreplace-v1` 정규화 프로필로 두 수준을 검사했다.

공개 JSON의 번역 공백은 ASCII 반각 공백으로 보관하고, 새 인코더가 게임
payload를 만들 때는 반각 공백 하나를 `FE FE`로 기록한다. `81 40`은 원문 또는
구조용 전각 공백이므로 반각 공백 치환 대상과 구분한다. 기존 BIN을 JSON으로
가져올 때도 `FE FE`를 사람이 읽는 반각 공백으로 되돌린다.

1. NFKC 호환 정규화
2. 일본어 쉼표·마침표·중간점과 번역표 구두점의 대응
3. 실제 줄바꿈과 게임 문자열의 `\\n` 대응
4. 게임 인코딩의 `▽`와 번역표의 물결표 대응
5. 의미 비교에서 ASCII·전각 공백 제거

그 결과는 다음과 같다.

- 마스터 행 의미 일치: 25,758/25,758
- 슬롯 의미 일치: 31,983/31,983
- 공백을 보존한 마스터 표기 일치: 20,567/25,758
- 공백을 보존한 슬롯 표기 일치: 25,740/31,983
- 원본 대비 변경 바이트: 881,077바이트
- 원본 대비 연속 변경 구간: 61,539개

공백을 보존한 수치가 낮은 것은 번역 내용이 다르다는 뜻이 아니라, 적용 BIN이
고정 길이 슬롯에 맞춰 일부 공백을 줄이고 게임용 줄바꿈·구두점 표기를 사용하기
때문이다. 모든 마스터와 슬롯이 의미 정규화 후 일치하므로 적용 파일의 번역
내용은 번역표와 대응한다.

## JSON을 다시 생성하는 방법

아래 명령은 외부 자료를 읽고 저장소 안의 공개 JSON만 새로 만든다. 원본 폴더에는
파일을 쓰지 않는다.

```powershell
python scripts/import_battle_dialogue.py `
  --workbook '<로컬 아카이브 경로> Ho Lee\Desktop\Z\PCSG00264\시옥편 전투대사 번역\SRVC_번역.xlsx' `
  --applied-bin '<로컬 아카이브 경로> Ho Lee\Desktop\Z\PCSG00264\시옥편 전투대사 번역\SRVC.BIN' `
  --original-bin '<로컬 아카이브 경로> Ho Lee\Desktop\Z\PCSG00264\4_SRVC_BIN (36836)\SRVC-ori.BIN' `
  --encoding-table '<로컬 아카이브 경로> Ho Lee\Desktop\Z\PCSG00264\4_SRVC_BIN (36836)\shift-jis2.tbl' `
  --wreplace '<로컬 아카이브 경로> Ho Lee\Desktop\Z\PCSG00264\4_SRVC_BIN (36836)\bak\Japanese - Hangul to Kanji.wReplace' `
  --output translations/battle-dialogue/srvc/SRVC_BATTLE.json
```

스크립트는 다음을 거부한다.

- 저장소 밖으로 나가는 출력 경로
- BIN·CPK·VPK·실행 파일·XLSX 출력
- ID·슬롯 순서가 끊긴 행
- 슬롯과 마스터의 원문·바이트 길이 불일치
- 번역 바이트가 원문 슬롯을 초과하는 행
- 적용 BIN에서 범위를 벗어난 오프셋

## GitHub만으로 자기 소유 게임에 패치하는 방법

저장소를 내려받으면 Python 표준 라이브러리만으로 JSON의 고정 슬롯 델타를
사용할 수 있다. 사용자는 반드시 자기 소유 또는 합법적으로 보유·덤프한 원본
`SRVC.BIN`을 별도로 준비해야 한다.

```powershell
python scripts/apply_battle_dialogue_patch.py `
  --original 'C:\내 게임 덤프\SRVC.BIN'
```

기본 출력은 `output/battle-dialogue/SRVC.BIN`과 같은 폴더의 검증 보고서다.
원본 파일에는 쓰지 않으며, 다음을 모두 통과해야만 결과를 만든다.

- JSON의 원본 파일 크기·SHA-256과 입력 BIN 일치
- 모든 슬롯의 원본 페이로드 SHA-256 일치
- 슬롯 범위의 파일 내 위치·겹침 검사
- JSON에 저장된 결과 SHA-256과 생성 결과 일치
- 출력 경로가 저장소 `output/` 또는 `work/` 아래이고 원본과 다름

즉, GitHub 저장소의 코드와 JSON만으로 패치 로직을 재현할 수 있지만, 게임
원본 자체를 GitHub에서 내려받거나 게임 설치 폴더에 자동으로 덮어쓰지는 않는다.
`--overwrite`를 지정해도 저장소의 출력 파일만 교체한다.

## 보안·저작권 사용 조건

- 이 자료와 도구는 사용자가 본인 소유 또는 합법적으로 보유한 게임을 개인적으로
  패치하는 상황을 전제로 한다.
- 원본 CPK/BIN/EBOOT, 완성 게임 파일, 계정 정보, 키, 외부 실행 파일을 저장소에
  올리지 않는다.
- 일본어 원문·게임 자료의 권리는 각 권리자에게 있다. JSON의 원문-번역 쌍과
  델타 데이터는 이 프로젝트의 권리 안내와 게임 권리자의 허용 범위 안에서만
  사용해야 하며, 저장소가 재배포 권리를 새로 부여하지 않는다.
- 다른 사람의 원본 파일이나 공유 링크를 입력하지 말고, SHA-256이 맞지 않으면
  버전이 다른 자료로 보고 작업을 중단한다.

## GitHub 보관 범위

GitHub에는 다음만 올린다.

- `scripts/import_battle_dialogue.py`: 외부 자료를 읽어 원문-번역 쌍 JSON과 델타 페이로드를 재생성하고 역검증하는 코드
- `scripts/apply_battle_dialogue_patch.py`: JSON과 자기 소유 원본 BIN만으로 별도 출력에 패치하는 코드
- `translations/dialogue/battle_SRVC.json`: 공통 검수 형식의 일본어-한국어 쌍, 행 결박 해시, 오프셋, 고정 페이로드, BIN 해시, 검증 결과
- 이 설명서

원본·적용 게임 BIN과 XLSX는 프로젝트 규칙상 커밋하지 않는다. JSON의
`appliedSha256`은 재현 결과를 확인하는 기준이며, 그 파일을 GitHub에서 직접
내려받는 기능을 뜻하지 않는다. 이 번역은 표의 상태를 그대로 보존했으므로
모두 `1차 번역`으로 남아 있고, 배포 전 사람 검수가 필요하다.
