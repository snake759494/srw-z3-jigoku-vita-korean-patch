# 정규 번역 원고

대사 CPK 빌드는 기존 XLSX를 직접 조립기에 넘기지 않고 정규 JSON으로 변환한 뒤 사용합니다. 구조 예시는 `dialogue-manifest.example.json`, 정식 계약은 `../config/dialogue-manifest.schema.json`을 참고하세요. 예시는 합성 문자열과 가짜 해시만 포함하며 실제 게임 데이터가 아닙니다.

원본 일본어 대사는 `work/normalized`에만 두며 Git에서 제외합니다. Git 게시용 번역은 `scripts/export_public_translations.py`로 일본어 원문을 SHA-256으로 치환하고 `corpus/data/` 아래 자산별 TSV로 분리합니다.

새로 번역하는 대사는 `retranslation/`에 원문 해시와 새 한국어만 기록합니다. 기존
번역은 로컬 작업 JSON의 참조 열로만 유지하며 새 번역을 자동으로 채우는 데 쓰지
않습니다. 자세한 절차는 `retranslation/README.md`를 참고하세요.

번역 corpus에는 번역 후보·통합 번역·게임용 치환문이 들어 있지만, `blocked` 행과 미완성 번역도 포함된 작업 스냅숏입니다. 원본 XLSX와 게임 바이너리는 포함하지 않습니다.

`dictionary/MtZkn_KW.json`은 별도 사전 대사 계약입니다. 게임 CPK에서 직접
추출한 141개 멤버·564개 태그의 일본어 원문과 새 한국어 번역, 원문·적용
페이로드 해시를 함께 보관합니다. 현재 564개 행은 `draft` 280개와
`overflow-repacked` 284개로 표시되어 사람 검수 전 상태입니다.
`scripts/apply_dictionary_patch.py`가 자기
소유 `MtZkn_KW.cpk`를 검증한 뒤 태그 길이와 CPK ITOC를 갱신해 리팩하며,
원본 CPK는 저장소에 없습니다. 추출·번역 재현 방법은
`../docs/사전_대사_추출과패치.md`를 참고하세요.

사람이 세 종류 대사를 한 형식으로 검수할 때는 `dialogue/`를 사용합니다.
`scenario_*.json`, `battle_SRVC.json`, `dictionary_MtZkn_KW.json`이 모두
`siok.dialogue-review` 형식이며, 일본어 `sourceText`와 한국어 `translation`을
같은 행에서 확인할 수 있습니다. 공통 형식의 필드 설명과 해시·제어 토큰
검사는 `dialogue/README.md`와 `scripts/check_dialogue_review_bundle.py`에
있습니다.

가져오기 명령:

```powershell
python -m siok_patch.cli import-legacy --project-root .
```
