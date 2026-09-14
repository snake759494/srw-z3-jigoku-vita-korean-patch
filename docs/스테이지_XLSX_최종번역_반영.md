# 스테이지 XLSX 최종 번역 반영

`01_스테이지 xlsx 모음`의 원고를 JSON에 다시 반영할 때는 다음 명령을 사용합니다.

```powershell
$xlsxRoot = (Get-ChildItem 'D:\Z\psvita\PCSG00264' -Directory |
  Where-Object { $_.Name -like '01_*xlsx*' }).FullName
$env:PYTHONPATH = 'src'
python scripts/apply_stage_xlsx_translations.py `
  --xlsx-root "$xlsxRoot" `
  --backup-dir 'work\xlsx-final-translation-backup'
```

스크립트는 `sourceArtifact`, 원본 행 번호, 일본어 원문을 동시에 확인한 뒤
번역을 교체합니다. 따라서 파일이나 행이 어긋나면 JSON을 일부만 덮어쓰지 않고
중단합니다.

번역 열 선택 우선순위는 다음과 같습니다.

1. `제미니번역` 또는 `제미니 번역`
2. 제미니 열이 없는 고정 슬롯 원고의 `번역`
3. 제미니 열이 없는 분기 원고의 `웹번역`
4. 마지막 호환용 `기존번역`

교체 시 `translationTextSha256`, 제어문자 서명, `references.importedTranslation`,
게임용 `replacementText`, 길이·고정 슬롯 크기도 함께 갱신합니다. CPK를 다시 만들기
전에는 다음 검사를 실행합니다.

CPK 빌드 시에만 게임 문자표에 없는 `퀜`과 `♡`를 원본 폰트가 사용하는 대응 글리프로
보정합니다. JSON의 `translation`에는 엑셀의 제미니번역 원문이 그대로 남으므로, 뷰어에서
번역을 확인하거나 다시 수정할 때 보정으로 내용이 바뀌지 않습니다.

```powershell
$env:PYTHONPATH = 'src'
python scripts/check_dialogue_review_bundle.py --root translations/dialogue
python -m pytest -q
```
