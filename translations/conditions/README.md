# 스테이지 조건 번역

`scenario_<asset>.json`은 각 스테이지 CPK의 `ID00001/OPERATE_TBL.str_tbl`을 원문·한국어 쌍으로 기록한 파일입니다.

- `conditionTypes`: `win`(승리), `defeat`(패배), `sr`(SR 포인트), `other`
- `sourceId`: 원본 `str_tbl` ID
- `translation`: 수정할 한국어 문자열

JSON을 수정한 뒤 저장소 루트에서 다음을 실행하면 같은 asset의 시나리오 대사와 함께 CPK에 자동 반영됩니다.

```cmd
set PYTHONPATH=src
python scripts\build_all_scenario_cpks.py
```

게임 원본 CPK와 `cpkmakec.exe`는 본인 소유의 로컬 파일을 사용하며 저장소에는 포함하지 않습니다. 상세한 원본 구조와 조건 JSON 재생성 방법은 [스테이지 조건 번역 문서](../../docs/스테이지_조건_번역.md)를 참고하세요.
