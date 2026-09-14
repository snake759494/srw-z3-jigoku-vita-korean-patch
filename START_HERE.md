# 여기서 시작하세요

## 완성된 한글 패치를 자기 원본에 적용할 때

시나리오 대사, 전투 대사, 메뉴·기체명과 함께 eboot·맵·시스템 파일까지 한 번에
만들려면 [통합 패치 실행](docs/통합_패치_실행.md)을 따릅니다. 저장소 루트에서
다음 두 명령을 실행하면 됩니다.

```powershell
python scripts/prepare_xdelta3.py --output private/tools/xdelta3.exe
python scripts/apply_release_patch.py `
  --game-root 'C:\내_덤프\PCSG00264' `
  --dlc-root 'C:\내_덤프\addcont\PCSG00264' `
  --xdelta 'private\tools\xdelta3.exe'
```

실행기는 `config/release-patch.json`의 253개 항목을 원본 해시와 함께 검사하고
`output/release-candidate/vita3k/<실행시각>/app` 및 `addcont`에만 결과를 만듭니다.
게임 설치 폴더에 자동으로 덮어쓰지 않으며, 본인 소유 원본이 아니거나 버전이 다르면
중단합니다. DLC가 없으면 `--include main`을 지정하고 `--dlc-root`를 생략합니다.

## 가장 쉬운 방법

1. 원본 `PCSG00264` 폴더를 백업합니다.
2. 원본 폴더는 이동하거나 이름을 바꾸지 않습니다.
3. 이 폴더의 `시작하기.cmd`를 더블클릭합니다.
4. 처음 한 번만 메뉴에서 `1. 환경 설정`을 선택합니다.
5. 기존 작업 아카이브 경로를 입력합니다.
6. `2. 환경·입력 검사`를 실행합니다.
7. `3. 기존 번역 가져오기`를 실행합니다.
8. `4. 번역 검사`에서 고칠 항목을 확인합니다.

현재 이 PC의 기존 작업 아카이브 경로는 이미 `private/project.local.json`에 설정되어 있습니다. 폴더를 옮기거나 원본 위치를 바꾼 경우에만 1번 메뉴를 다시 실행하면 됩니다.

대사 CPK를 만들 때는 메뉴 `7. 대사 CPK 한 번에 만들기`를 선택합니다. 원본 CPK, 대상 ID, 번역 XLSX를 입력하면 XLSX를 정규 JSON으로 고정한 뒤 대사 수정·리팩·재추출 검증까지 이어서 수행합니다. 성공 결과의 `dialogue-manifest.json`을 입력하면 XLSX 없이도 같은 빌드를 반복할 수 있습니다. 자세한 입력 예시는 [대사 CPK 자동화](docs/대사_CPK_자동화.md)에 있습니다.

## 만들어지는 파일

- `private/project.local.json`: 내 PC 경로. Git에 들어가지 않습니다.
- `work/normalized/`: 기존 XLSX에서 옮긴 UTF-8 TSV.
- `translations/corpus/data/`: 일본어 원문 대신 대응 SHA-256을 기록한 Git 게시용 자산별 번역 TSV.
- `work/reports/`: 가져오기·검사 결과를 한국어 JSON과 텍스트로 기록.
- `output/verification/`: 원본을 수정하지 않는 검증 출력.
- `work/cpk-runs/`: 대사 CPK의 실행별 추출·정규 JSON·리팩·재검증 기록.
- `output/dialogue/`: 모든 역검증을 통과한 대사 CPK, 재사용 JSON, 검증 보고서.

현재 생성된 `work/normalized/translations.tsv`는 175,210행짜리 정규화 원본입니다. Excel로 직접 열어 다시 저장하면 행이나 인코딩이 바뀔 수 있으므로, 지금은 보고서 확인용으로 두세요.

## 새로 대사를 번역할 때

현재 전체 175,210행(본편·DLC·사전)의 새 번역 초안이 별도 JSON에 작성되어 있습니다.
`STG0001a` 93행만 `reviewed`이며 나머지는 모두 `draft`입니다. 기존 번역은
참고용 `references`로만 격리되어 새 번역에 자동 복사되지 않습니다.

```powershell
$env:PYTHONPATH='src'
# 전체 일본어 원문 기준 자동 초안 생성(이미 생성된 행은 건너뜀)
python scripts/auto_translate_retranslation.py --workers 3
# 누락된 기호형 제어 토큰 복원
python scripts/repair_retranslation_controls.py
# 잔류 일본어 재번역 및 수동 보정
python scripts/repair_retranslation_japanese.py
python scripts/manual_retranslation_cleanup.py
python -m siok_patch.cli retranslate check
# 사람 검수 후에만 적용
python -m siok_patch.cli retranslate apply
```

- 원문과 기존 번역이 함께 보이는 작업 JSON은 `work/retranslation/tasks/`에만 둡니다.
- Git에 기록하는 JSON에는 원문 해시와 새 한국어만 넣습니다.
- 새 오버레이를 `translations/retranslation/progress.json`에 등록해야 기본 검사와
  병합에 포함됩니다.
- 병합 결과는 `work/retranslation/merged/translations.tsv`에 생성되며 원본 TSV를
  수정하지 않습니다.
- `draft`와 새로 번역하지 않은 행은 기존 활성 번역을 비우고 `blocked`로 처리합니다.

자세한 내용과 다중 자산 병합 방법은 [신규 대사 번역](translations/retranslation/README.md)을 참고하세요.

## 다른 CPK와 `eboot.bin`을 재번역할 때

통합 XLSX가 있는 추가 자산은 별도 기준 TSV와 공개 오버레이로 관리합니다. 원본
아카이브를 읽어 목록을 다시 만들고, 일본어 원문만 번역 서비스에 보내는 명령은
다음과 같습니다.

```powershell
$env:PYTHONPATH='src'
python scripts/import_extra_retranslation.py --archive-root '<로컬 아카이브 경로> Ho Lee\Desktop\Z\PCSG00264'
python scripts/auto_translate_retranslation.py `
  --source work/extra-normalized/translations.tsv `
  --task-root work/retranslation-extra/tasks `
  --output-root translations/retranslation-extra `
  --workers 6 --max-chars 4000
python scripts/repair_retranslation_japanese.py `
  --source work/extra-normalized/translations.tsv `
  --task-root work/retranslation-extra/tasks `
  --output-root translations/retranslation-extra `
  --workers 6
python scripts/repair_retranslation_segments.py `
  --source work/extra-normalized/translations.tsv `
  --task-root work/retranslation-extra/tasks `
  --output-root translations/retranslation-extra `
  --workers 8 --max-chars 1200
```

현재 추가 기준은 844개 자산 62,937행이며, `eboot`, `aid`, `rpw`, `srvc`,
`kdata`, `mtzkn_kw`, `mtzkn_pt`, `mtzkn_rt` 범위를 포함합니다. 진행률과 원본
파일 해시는 [추가 자산 재번역](translations/retranslation-extra/README.md)의
`progress.json`과 `source-manifest.json`에서 확인할 수 있습니다. 원본 CPK·EBOOT
바이너리는 읽기 전용으로만 사용합니다.

사용자 제공 전투 대사 번역표는 [SRVC 전투 대사 번역 보관](docs/전투대사_SRVC_번역.md)에
정리했습니다. 25,758개 고유 대사와 31,983개 슬롯의 일본어-한국어 쌍과 고정
페이로드를 JSON으로 재생성·역검증할 수 있으며, 원본 XLSX와 BIN은 저장소에 넣지
않습니다. 저장소를 내려받은 뒤 자기 소유 원본 BIN에 패치하려면 다음만 실행합니다.

```powershell
python scripts/apply_battle_dialogue_patch.py `
  --original 'C:\내 게임 덤프\SRVC.BIN'
```

결과는 `output/battle-dialogue/`에 생성되고 원본 게임 파일에는 쓰지 않습니다.

전투 대사만이 아니라 본편·DLC CPK, 메뉴·기체명, eboot·맵까지 함께 적용하려면 위의
`apply_release_patch.py`를 사용하세요. 이 통합 실행기는 SRVC JSON 실행기도 내부에서
호출하므로 별도 수동 단계가 필요하지 않습니다.

## 미추출 사전 대사를 새로 만들 때

사전 대사는 `CommonData/MtData/MtZkn_KW.cpk` 안에 있으며 `0x5E` XOR와
`ZKANKYWD` 레코드로 저장됩니다. 원본 CPK를 읽기 전용으로 지정해 추출·번역·리팩을
한 번에 준비하려면 [사전 대사 추출과 패치](docs/사전_대사_추출과패치.md)를
따릅니다.

```powershell
$env:PYTHONPATH='src'
python scripts/build_dictionary_encoding.py `
  --table 'C:\내 작업\0_STAGE\shift-jis2.tbl' `
  --wreplace 'C:\내 작업\Japanese - Hangul to Kanji.wReplace'
python scripts/extract_dictionary_dialogue.py `
  --cpk 'C:\내 게임 덤프\CommonData\MtData\MtZkn_KW.cpk'
python scripts/translate_dictionary.py --workers 6
python scripts/apply_dictionary_patch.py `
  --original 'C:\내 게임 덤프\CommonData\MtData\MtZkn_KW.cpk'
```

최종 JSON은 141개 멤버·564개 태그의 일본어 원문과 한국어 번역, 해시, XOR
페이로드를 함께 가지며, 원본보다 긴 설명은 태그 길이와 CPK ITOC를 갱신해
리팩합니다. 원본 게임 파일은 수정하지 않습니다.

## 세 종류 대사를 한 폴더에서 검수할 때

실제로 번역을 읽고 수정할 때는 [공통 대사 검수 폴더](translations/dialogue/README.md)의
JSON을 사용합니다. 시나리오 174개 자산, 전투 1개, 사전 1개가 모두
`siok.dialogue-review` 형식이며, 각 행의 `translation`만 수정 대상으로
표시되어 있습니다.

```powershell
python scripts/check_dialogue_review_bundle.py
```

`sourceText`, `entryId`, 위치, 원문·적용 페이로드 해시는 패치 위치를 묶는
검증 값이므로 임의로 바꾸지 않습니다. 번역을 수정한 뒤에는 해당 자산의
문자표 인코더와 전용 패치 검사를 다시 실행해야 합니다.

## 꼭 기억할 점

- 오류가 나면 억지로 다음 단계로 넘어가지 않습니다.
- `work/normalized`의 상태가 `approved`라고 해도 문자표와 실제 바이트 길이 검사가 끝나기 전에는 배포할 수 없습니다.
- 기존 BAT처럼 게임 설치 폴더를 직접 바꾸지 않습니다.
- `gen-lang-client-*.json`, `g.py`, `trans-test.py`의 기존 키는 먼저 공급자 화면에서 폐기해야 합니다.

자세한 설명은 [현재 상태](docs/현재상태.md), [번역 규칙](docs/번역규칙.md), [빌드와 검사](docs/빌드와검사.md), [다음 작업](docs/다음작업.md)을 참고하세요.
