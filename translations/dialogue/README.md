# 공통 대사 검수 폴더

이 폴더의 JSON은 시나리오·전투·사전 대사를 사람이 확인하고 수정하기
위한 공통 형식입니다. 파일 종류가 달라도 모두
`format: "siok.dialogue-review"`, `entries[]`, `sourceText`, `translation`,
`controls`, `sourcePayload`, `appliedPayload`, `location` 구조를 사용합니다.

현재 구성:

- `scenario_*.json`: 본편·DLC 시나리오 174개 자산, 170,746행
- `battle_SRVC.json`: 전투 대사 25,758개 마스터행과 슬롯 위치
- `dictionary_MtZkn_KW.json`: 사전 대사 564개 태그

## 검수할 때 수정하는 필드

각 행에서 사람이 검토하고 수정할 값은 `translation`입니다. `sourceText`,
`entryId`, `location`, `sourcePayload`는 원문 결박과 패치 위치이므로
수정하지 않습니다. 번역을 수정한 뒤에는 `translationTextSha256`을 함께
갱신해야 하며, 페이로드가 있는 전투·사전 행은 `appliedPayload`도 전용
인코더로 다시 만들어야 합니다. 해시나 제어 토큰을 임의로 지우면 패치기가
안전하게 중단합니다.

공통 형식 검사:

```powershell
python scripts/check_dialogue_review_bundle.py
```

원문 TSV와 기존 재번역 오버레이에서 이 폴더를 다시 만들 때:

```powershell
python scripts/build_dialogue_review_bundle.py
```

이 명령은 게임 원본·완성 파일을 읽거나 저장하지 않습니다. `work/`의
정규화 TSV가 없는 새 환경에서는 먼저 본인 소유 원본을 사용한 기존 추출·
정규화 절차를 실행해야 합니다. GitHub에는 이미 생성된 검수 JSON이 있으므로
검수만 할 때는 TSV가 필요하지 않습니다.

공통 JSON을 편집한 뒤에는 유형별 입력으로 다시 반영합니다. 시나리오는 기존
재번역 오버레이와 `dialogue-build`의 CPK 검사를 거치고, 전투는
`scripts/import_battle_dialogue.py`로 원본 XLSX·문자표를 다시 읽으며, 사전은
`scripts/translate_dictionary.py`와 `scripts/apply_dictionary_patch.py`를
사용합니다. 각 유형의 새 결과를 만든 뒤 `build_dialogue_review_bundle.py`로
공통 폴더를 갱신합니다. 전투·사전 실행기는 `appliedPayload`와 전체 결과
SHA-256을 확인하므로, 문자표·원본 파일을 사용한 페이로드 재생성 없이
바이너리를 덮어쓰지 않습니다.

원본 일본어와 게임 데이터의 권리는 각 권리자에게 있습니다. 본인이 합법적으로
보유·덤프한 게임에만 사용하며, 이 폴더에는 게임 바이너리를 저장하지 않습니다.
