# [chore] tests/test_analysis_queue.py 의 import 정렬

- 브랜치: chore/test-import-order (main 위)
- 작업일: 2026-11-07

## 수행한 변경

- `tests/test_analysis_queue.py` 의 import 순서를 `ruff` 규칙(I001)에 맞게 고쳤다 (`from krx_screener.analysis_queue import …` 를 첫 번째 그룹에서 `krx_screener` 그룹으로). 동작 변경 없음.

## 왜

#12 의 PR(#52)로 머지된 `main` 에서 `ruff check src tests` 가 이 한 건으로 실패한다. #12 의 기록과 PR 본문에는 "ruff 통과"라고 적었는데, 머지된 상태에서는 사실이 아니었다 (정정).
CLAUDE.md 는 커밋 전에 `ruff check src tests` 통과를 요구하므로 바로 고친다. 이 수정이 #30(문서) 브랜치에 섞이지 않도록 따로 낸다.

## 검증 결과

- `ruff check src tests`: 수정 전 1건(I001) 실패 → 수정 뒤 통과.
- `pytest`: 테스트 DB 연결 시 655 passed (변경 없음).
