# Git 게시용 번역 corpus

이 폴더는 로컬 `work/normalized/translations.tsv`에서 생성한 정제 번역 자료입니다.

- 일본어 원문 전문은 포함하지 않습니다.
- 원문 대응은 `source_text_sha256`과 원본 XLSX의 상대 이름·해시로 확인합니다.
- Google 번역, 기존 번역, 통합 번역과 게임용 치환문을 서로 다른 열로 보존합니다.
- `status=blocked`인 행은 자동 승인되지 않은 작업 중 자료입니다.
- 원본 XLSX, 게임 CPK·payload, 개인 절대경로는 포함하지 않습니다.

`data/manifest.json`에는 전체 입력 스냅숏과 자산별 TSV의 행 수·바이트 수·SHA-256이 기록됩니다. corpus를 다시 만들려면 프로젝트 루트에서 다음을 실행합니다.

```powershell
python scripts/export_public_translations.py
```

이 corpus는 비공식 팬 번역의 작업 자료입니다. 현재 번역 기여자의 재배포 동의와 별도 라이선스가 확정되지 않았으므로 공개 배포 권한을 의미하지 않습니다.
