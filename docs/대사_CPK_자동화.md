# 대사 CPK 자동화

이 기능은 다음 과정을 한 명령으로 수행합니다.

```text
읽기 전용 원본 CPK
  → ID 멤버 추출
  → 번역 XLSX 원문 대조
  → 정규 dialogue-manifest.json 생성·엄격 재로딩
  → CP932 대사 재조립
  → 명시적 ID로 CPK 리팩
  → 새 CPK 재추출
  → 모든 ID와 payload SHA-256 검증
```

원본 CPK와 XLSX에는 쓰지 않습니다. 중간 결과는 `work/cpk-runs/`, 통과한 CPK·JSON·보고서는 `output/dialogue/` 아래의 새 실행 폴더에만 생성됩니다.

XLSX는 사람이 번역을 편집하는 입력 화면이고, 생성된 JSON이 실제 빌드 입력입니다. 한 번에 만들기를 실행해도 XLSX 객체에서 payload를 바로 만들지 않습니다. 먼저 JSON을 파일로 저장한 뒤 다시 엄격하게 읽고, 그 JSON의 행만으로 대사를 조립합니다.

## 가장 쉬운 사용법

1. `시작하기.cmd`를 더블클릭합니다.
2. 처음 한 번 `1. 이 PC의 경로 설정`에서 `cpkmakec.exe` 경로를 확인합니다.
3. `7. 대사 CPK 한 번에 만들기`를 선택합니다.
4. 본인이 덤프한 읽기 전용 원본 CPK 경로를 입력합니다.
5. 새 XLSX 빌드라면 JSON 질문에서 Enter를 누릅니다.
6. 번역을 넣을 대상 ID와 번역 XLSX를 입력합니다.
7. 추가 ID가 있으면 계속 입력하고, 없으면 Enter를 누릅니다.
8. 검사에서 승인한 제어 토큰 변경이 있을 때만 `ID:XLSX행`을 입력합니다.

이미 성공한 실행의 `dialogue-manifest.json`을 그대로 재빌드하려면 5번 질문에 그 JSON 경로를 입력합니다. 이 경우 XLSX를 다시 읽지 않습니다.

일반적인 대상은 `ID00003` 또는 `ID00004`입니다. ID는 XLSX 파일명이나 시트명에서 자동 추론하지 않습니다.

## 원본에 대상 ID가 없을 때

`STG0001a.cpk` 원본에는 ID4가 있지만 ID3은 없습니다. 기존 패치와 같이 ID4 대사를 바탕으로 ID3도 만들려면 메뉴에 다음 두 매핑을 입력합니다.

| 대상 ID | 원본 ID | XLSX |
|---|---|---|
| `ID00003` | `ID00004` | ID4 대사 XLSX |
| `ID00004` | Enter | 같은 ID4 대사 XLSX |

이 동작은 명시적으로 요청했을 때만 새 ID를 추가합니다. 빈 ID를 자동으로 채우거나 파일명 정렬 순서로 번호를 다시 매기지 않습니다.

### 시나리오 JSON의 파일명/시트명 충돌

일괄 시나리오 JSON에는 번역 XLSX의 파일명과 시트명이 달라
`ID00003@FILE-ID00004`처럼 기록된 그룹이 있을 수 있습니다. 같은 CPK에
실제 `ID00003` 그룹도 있으면 별칭 그룹은 `ID00004`에 출력하고 `ID00003`은
그대로 보존합니다. 이 충돌 방지 결과는 개별 CPK 보고서의
`collisionTargetRemaps`와 `modified[].requestedTargetId`에 기록됩니다.

## 명령줄 사용법

PowerShell에서 프로젝트 폴더를 연 뒤 실행합니다.

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m siok_patch.cli dialogue-build `
  --cpk "C:\내 원본\STG0001a.cpk" `
  --entry "ID00003@ID00004=C:\내 번역\STG0001a-ID00004.xlsx" `
  --entry "ID00004=C:\내 번역\STG0001a-ID00004.xlsx"
```

- `ID00004=파일.xlsx`: 원본 ID4를 읽어 ID4에 적용합니다.
- `ID00003@ID00004=파일.xlsx`: 원본 ID4를 읽어 새 ID3을 만듭니다.
- `--entry`는 여러 번 지정할 수 있습니다.

성공 결과의 JSON만으로 다시 만들 때는 다음처럼 실행합니다.

```powershell
python -m siok_patch.cli dialogue-build `
  --cpk "C:\내 원본\STG0001a.cpk" `
  --manifest "C:\검증 결과\dialogue-manifest.json"
```

`--entry`와 `--manifest`는 동시에 사용할 수 없습니다. JSON 재빌드에서는 제어 코드 승인도 JSON 행에 이미 결박되어 있으므로 `--allow-control-change`를 다시 지정하지 않습니다.

검수 후 의도적으로 제어 토큰을 바꾼 행은 정확한 Excel 행 번호로 승인합니다.

```powershell
--allow-control-change "ID00003:163" `
--allow-control-change "ID00003:167"
```

승인한 행에 실제 불일치가 없거나 행 번호가 틀리면 작업이 중단됩니다. JSON에는 승인 행 번호뿐 아니라 대사 번호, 원본 물리행 SHA-256, 변경 전후 제어 토큰을 함께 기록합니다. 번역이나 행이 바뀐 뒤 오래된 예외가 다른 대사에 붙는 것을 막기 위한 검사입니다.

## JSON 구조와 검사

정식 스키마는 `config/dialogue-manifest.schema.json`, 게임 자료가 없는 예시는 `translations/dialogue-manifest.example.json`에 있습니다.

- `sourceCpk`: 원본 CPK 파일명·크기·SHA-256과 모든 멤버의 ID·크기·SHA-256
- `pack`: 고정 리팩 프로필과 `cpkmakec.exe` 해시·버전
- `scripts`: 원본 ID, XLSX 출처, 원본 물리행 번호·태그·줄바꿈·raw SHA-256, 교체문
- `outputs`: 어느 스크립트를 어느 대상 ID에 쓸지 나타내는 명시적 연결

같은 ID4 대사를 ID3과 ID4에 모두 넣을 때 `scripts`에는 대사를 한 번만 기록하고 `outputs` 두 개가 같은 `scriptId`를 가리킵니다. 파일명·시트명·배열 순서로 ID를 추론하지 않습니다.

JSON은 UTF-8, LF, 고정 필드 순서로 만들어지며 실행 시각이나 절대경로를 포함하지 않습니다. 알 수 없는 키, 중복 키, 지원하지 않는 버전, 잘못된 ID·해시·행 순서, 원본과 다른 행 번호·태그·raw 해시, CP932로 쓸 수 없는 문자, 승인 없는 제어 토큰 변경은 모두 중단 사유입니다. JSON을 손으로 수정하기보다 XLSX를 수정해 새 JSON을 만드는 방식을 권장합니다.

## XLSX 규칙

자동 인식하는 열 이름은 다음과 같습니다.

- 원문 열: `원문` 또는 `문자열`
- 최종 삽입 열: `한글폰트로` 또는 `한글폰트`

파일명과 시트명은 대상 ID 판단에 사용하지 않습니다. 실제 자료에는 ID4 XLSX의 시트명이 ID3으로 되어 있거나, STG0211 파일명이 STG0210으로 되어 있는 사례가 있기 때문입니다.

다음 조건을 모두 통과해야 합니다.

- XLSX 의미 행 수와 추출한 대사 슬롯 수가 같음
- 원문 순서가 같음
- 순수 전각 공백 행을 포함해 빈 대사 슬롯이 보존됨
- 원문의 양끝 전각 공백 개수만 다른 기존 자료는 경고로 기록됨
- 최종 삽입문을 CP932로 인코딩할 수 있음
- 원문·최종 삽입 열에 수식이 없음
- 매크로와 외부 링크가 없음
- 승인하지 않은 제어 토큰 누락·추가·순서 변경이 없음

서식만 남은 빈 행은 무시하지만, 전각 공백 한 칸이 들어 있는 행은 실제 대사 슬롯으로 유지합니다.

## CPK 리팩 방식

로컬의 CRI `cpkmakec.exe`를 사용합니다. 이 실행 파일은 독점 도구이므로 프로젝트에 복사하거나 Git에 넣지 않습니다. 설정된 실행 파일은 실행 전에 `config/tools.lock.json`의 SHA-256과 대조합니다.

리팩 옵션은 다음으로 고정합니다.

- 모드: `ID`
- ID 지정: 생성 CSV의 ID 열에 각 번호를 명시
- 정렬: 16바이트
- 압축: 무압축(`UC`)
- 디렉터리 정보: 마스킹
- 날짜·시간 정보: 제외

폴더를 그대로 리팩하면 빠진 ID 뒤의 번호가 당겨질 수 있습니다. 이 프로젝트는 반드시 명시적 ID CSV를 생성하므로 `STG0202`에서 확인된 ID3/ID4 뒤바뀜을 반복하지 않습니다.

기존 `CriPakTools.exe`는 추출 조사에는 쓸 수 있지만 리팩에는 사용하지 않습니다. 이 도구의 교체 기능은 0x800 강제 정렬, ContentOffset 갱신 문제, 새 ID 추가 불가 때문에 Vita판 결과를 안전하게 보장하지 못합니다.

## 자동 검증과 결과

리팩이 끝나면 새 CPK를 다른 폴더에 다시 추출합니다. 다음 중 하나라도 다르면 최종 CPK를 게시하지 않습니다.

- 전체 ID 집합
- 변경하지 않은 payload의 SHA-256
- 수정한 payload의 SHA-256
- 원본 CPK의 작업 전후 SHA-256

성공 결과에는 다음 파일이 생깁니다.

```text
work/cpk-runs/<CPK>/<실행ID>/
  extracted-original/
  build/
  verified-extract/
  tool-invocations.jsonl
  dialogue-manifest.json
  report.json

output/dialogue/<CPK>/<실행ID>/
  <원본과 같은 이름>.cpk
  dialogue-manifest.json
  report.json
```

실패한 실행은 `work/cpk-runs/.../failure.json`에 중단 이유를 기록하고 `output/dialogue`에는 CPK를 만들지 않습니다.

## 현재 검증 범위

- 일반 STAGE의 SP/SB 화자 줄과 S1/S2/S3/SM/SE/ST 대사
- 분기 STAGE
- DLC에서 쓰는 SF/SG 화자 들여쓰기
- `$ｎ/$ｌ/$ｃ/$Ｆ`의 ASCII 제어 토큰 복원
- 기존 괄호·물결표·빈 화자 후처리
- CRLF/LF와 마지막 줄바꿈 보존

대표 `STG0001a` 실제 왕복에서 새 ID3/ID4 payload는 기존 `zID00004`와 바이트 단위로 일치했고, 완성 CPK의 8개 ID를 다시 추출해 전부 해시 검증했습니다.

대사형 CP932 payload와 ID00007/Sheet1 고정 길이 슬롯을 함께 다룹니다. 고정 슬롯은
`byteLimit`을 검사하고, 사람이 읽는 번역문이 패딩 제어문자 때문에 한도를 넘으면
JSON의 `metadata.replacementText` 게임용 글리프열로 안전하게 폴백합니다. CPK에
없는 구판 행은 원본을 보존하고 `unmatchedSlots`로 보고합니다. 팀명, GXT, 폰트,
텍스처는 별도 파이프라인 대상입니다. 생성된 CPK는 검증 산출물이며, Vita3K
복사본 시험과 실기 Vita 시험을 각각 마치기 전에는 배포본으로 간주하지 않습니다.
