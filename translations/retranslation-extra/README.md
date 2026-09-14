# 추가 자산 재번역

이 폴더는 본편·DLC 대사 오버레이와 분리된 추가 자산 재번역 트리입니다.

이 자료는 현재 공개 릴리스 패치의 입력이 아니라 원문 결박형 번역·검수 초안입니다.
실제로 적용 완료된 메뉴·기체명 자산은 `config/release-patch.json`의
`rpw_data.cpk`, `AIDDataPack.cpk`, `KDataVITA.cpk` 델타를 사용하며, 아래 초안의
`MtZkn_KW/Pt/Rt` 등은 사람 검수와 CPK 재빌드가 끝날 때까지 통합 실행기가 건너뜁니다.

대상은 다음과 같습니다.

- `eboot.bin`의 Shift-JIS/UTF-8 문자열
- `AIDDataPack.cpk`
- `rpw_data.cpk`
- `SRVC.BIN`
- `KDataVITA.cpk`
- `MtZkn_KW`, `MtZkn_Pt`, `MtZkn_Rt` 키워드 CPK

원본 XLSX의 일본어 문자열을 새 번역 기준으로 사용하며, 기존 번역 열은
참조 데이터로만 보존합니다. 원본 CPK/BIN과 게임 ROM은 이 저장소에 넣지
않습니다.

## 재현 순서

```powershell
$env:PYTHONPATH = 'src'
python scripts/import_extra_retranslation.py `
  --archive-root '<로컬 아카이브 경로> Ho Lee\Desktop\Z\PCSG00264'

python scripts/auto_translate_retranslation.py `
  --source work/extra-normalized/translations.tsv `
  --task-root work/retranslation-extra/tasks `
  --output-root translations/retranslation-extra `
  --workers 3
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
python scripts/check_retranslation_bundle.py `
  --source work/extra-normalized/translations.tsv `
  --output-root translations/retranslation-extra `
  --report work/retranslation-extra/bundle-report.json
```

모든 결과는 자동 번역 초안(`draft`)으로 생성됩니다. 적용 전 제어 토큰,
CP932/UTF-8 바이트 길이, 고정 오프셋과 실제 CPK/BIN 재빌드 결과를 별도로
검수해야 합니다. 현재 조각 보정 후 가나 잔류 248행(408자)은 고유명사·문자표
시험 문자열로 남아 있으므로 사람 검수에서 확정해야 합니다. 전체 구조와 ID
검사 결과는 `work/retranslation-extra/bundle-report.json`에 기록합니다.

완료된 릴리스 전체를 자기 원본에 적용하려면 이 폴더의 초안을 직접 바이너리에
덮어쓰지 말고 [통합 패치 실행](../../docs/통합_패치_실행.md)의 검증된 델타 경로를
사용하세요.
