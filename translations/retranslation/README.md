# 신규 대사 번역

시나리오 재번역의 통합 기준은 [`scenario_glossary_v2.json`](scenario_glossary_v2.json)이다.
엑셀 원본에서 다시 만들려면 [`docs/시나리오_용어사전_v2.md`](../../docs/시나리오_용어사전_v2.md)의
추출 명령을 실행한다. 이 사전은 인물 관계·작품별 말투·존칭·고유명사 후보와 출처 행을
함께 보존하며, 기존 번역을 자동으로 정답 처리하지 않는다.

이 폴더에는 일본어 원문을 직접 싣지 않고, `entryId`와 원문 SHA-256에 결박된
새 한국어 번역만 저장합니다. 기존 `google_translation`, `legacy_translation`,
`translation` 값은 로컬 작업 JSON에서 참고할 수 있지만 새 번역으로 자동 채택하지
않습니다.

인명, 세계관 용어, 화자별 말투는 `glossary.json`을 공통 기준으로 사용합니다.
전체 진행률과 현재 자산 목록은 `progress.json`에 기록합니다.

## 재번역 참고 자료

- `reference_rules.json`: `D:\\Z\\psvita\\시옥번역엑셀`의 7개 기준 파일을 다시
  점검한 결과입니다. 각 XLSX에서 실제로 편집할 열, 보존할 오프셋/바이트/제어문자,
  시나리오·전투 대사의 표시 한도를 기록합니다.
- `series_reference.json`: 시옥편의 32개 참전작을 작품 변형판별로 나누어 설정,
  핵심 인물 관계, 존칭, 말투, 번역 주의점, 출처 URL을 제공합니다.
- 사람용 요약은 [`docs/참전작_설정_관계_말투_참고.md`](../../docs/참전작_설정_관계_말투_참고.md)를
  사용합니다.

외부 자료는 작품의 분위기와 관계를 해석하기 위한 요약일 뿐이며, 게임 일본어 원문과
로컬 XLSX의 필드 의미·용량 제한을 우선합니다. 애니메이션 대사 원문은 저장소에
복제하지 않습니다.

## 작업 순서

현재 제1~5화 초안은 다음 명령으로 원문·용어사전 기준의 `newTranslation`과 검수용
오버레이를 다시 생성할 수 있습니다. 기존 `translation` 필드는 비교용으로 보존됩니다.

```powershell
python scripts/retranslate_first_scenario.py
```

```powershell
python -X utf8 scripts/retranslate_second_scenario.py
```

```powershell
python -X utf8 scripts/retranslate_third_scenario.py
```

```powershell
python -X utf8 scripts/retranslate_fourth_scenario.py
```

```powershell
python -X utf8 scripts/retranslate_fifth_scenario.py
```

다섯 스크립트 모두 일본어 원문을 키로 한 `OVERRIDES` 사전으로 문장을 지정하고,
전각 공백·쉼표 뒤 공백·고정 표기만 `normalize()`로 정리합니다. 3화는 같은 원문이
장면에 따라 달라져야 하는 행만 `ENTRY_OVERRIDES`에 `entryId`로 따로 둡니다.
3·4화에서 `ENTRY_OVERRIDES`에 들어간 행은 같은 원문이 화자에 따라 다른 말투가
되어야 하는 경우입니다(예: 「了解！」가 소스케는 「알겠다!」, 후배는 「알겠습니다!」).
5화는 화자명 392행을 1~4화 확정 표기에서 자동으로 채우고, 새로 등장한 11명만
사람이 표기를 정했습니다.

```powershell
python -m siok_patch.cli retranslate export --scope stage --asset STG0001a
python -m siok_patch.cli retranslate publish --task work/retranslation/tasks/stage/STG0001a.json
python -m siok_patch.cli retranslate check
python -m siok_patch.cli retranslate apply
```

1. `export`는 일본어 원문과 기존 번역이 들어 있는 로컬 전용 작업 JSON을
   `work/retranslation/tasks/`에 만듭니다. 이 폴더는 Git에서 제외됩니다. 이미 있는
   작업 파일은 기본적으로 덮어쓰지 않으며, 다시 만들 때만 `--force`를 지정합니다.
2. 새 번역은 일본어 원문을 기준으로 작성하고 기존 번역은 애매한 고유명사나 문맥을
   확인하는 참고 자료로만 사용합니다.
3. 작업 JSON에 `freshTranslation`, `translationStatus`, `translator`를 작성한 뒤
   `publish`로 원문과 기존 번역을 제거한 오버레이를 만듭니다.
4. 오버레이를 `progress.json`에 등록합니다. `check`는 등록된 모든 오버레이의 기준
   TSV 전체 해시·행 수·원문 결박·제어코드·파일 간 중복을 함께 검사합니다.
5. `apply`는 원본 TSV를 덮어쓰지 않고 `work/retranslation/merged/translations.tsv`를
   만듭니다. 별도 `reviewer`가 기록된 `reviewed` 행만 활성 번역으로 반영합니다.
   `draft`, `blocked`, 미작성 행은 기존 활성 번역과 치환문을 비우고 `blocked`로
   표시하므로 예전 번역이나 미검수 초안이 빌드에 섞이지 않습니다.

`check`와 `apply`는 기본적으로 `progress.json`에 등록된 모든 오버레이를 사용합니다.
일부 파일만 의도적으로 시험 병합할 때에만 `--overlay ... --allow-partial`을 함께
지정합니다.

오버레이에는 `draft`, `reviewed`, `blocked`만 사용합니다. 실제 `approved`는 문자표
치환, 바이트 길이, CPK 재추출 검증까지 끝난 뒤에만 부여합니다.
