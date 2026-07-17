# Search Naver Map

네이버 지도 검색, Place 상세 정보, 방문자 리뷰, 공개 예약 정보를 읽는 CLI와 Agent Skill 지침입니다.

## 설치 확인

이 폴더를 에이전트의 스킬 경로에 복사하거나 연결한 뒤 실행 환경을 준비합니다.

```bash
python3 scripts/bootstrap.py
bin/naver-place capabilities --json
```

Python 3.10 이상이 필요합니다. 의존성은 `.venv`에만 설치되며 환경이 이미 최신이면 다시 설치하지 않습니다.

Git 저장소부터 내려받아 확인하려면 다음 명령을 사용합니다.

```bash
git clone https://github.com/jseook11/Naver-Codex-SKILL.git
cd Naver-Codex-SKILL/search-naver-map
python3 scripts/bootstrap.py
bin/naver-place capabilities --json
```

에이전트에게 설치를 맡길 때는 저장소 주소와 함께 아래처럼 요청하면 됩니다.

> `search-naver-map` 스킬을 설치하고 전용 실행 환경을 준비한 뒤, 명령 목록이 정상적으로 출력되는지 확인해줘.

## 빠른 실행

```bash
# 장소 검색
bin/naver-place search --query "성수 떡볶이" --limit 5

# Place 상세
bin/naver-place detail --place 1234567890

# 방문자 리뷰
bin/naver-place reviews --place 1234567890 --limit 20

# 숙박 예약 정보
bin/naver-place booking \
  --query "제주 게스트하우스" \
  --check-in 2026-07-20 \
  --check-out 2026-07-22 \
  --guests 2
```

명령은 JSON을 출력합니다. `status`가 `partial`이면 확인된 데이터가 남아 있어도 일부 조회가 끝나지 않은 상태입니다. `errors`, `warnings`, `completeness`를 함께 확인하세요.

조회·출력 범위는 `--view compact|standard|full`로 조절합니다. `compact`와 `standard`는 긴 설명과 이미지 배열을 줄이고 리뷰어 ID, 영수증 URL, 프로필 이미지 등을 제외합니다. `booking --view full`은 설명·이미지·옵션을 위해 추가 조회를 할 수 있습니다.

## 문서 안내

- 처음 설치하거나 경로를 바꿀 때: [installation.md](references/installation.md)
- 명령 인수와 출력 필드를 확인할 때: [capabilities.md](references/capabilities.md)
- 상태·오류·종료 코드를 처리할 때: [result-contract.md](references/result-contract.md)
- 코드 구조와 데이터 흐름을 볼 때: [architecture.md](references/architecture.md)
- 자연어 요청 예시가 필요할 때: [usage-examples.md](references/usage-examples.md)

## 개발

```bash
python3 scripts/bootstrap.py --dev
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider
.venv/bin/python -m compileall -q naver_place scripts
```

저장된 테스트 데이터는 네트워크 없이 파서와 결과 형식을 검증하는 용도입니다. 실제 서비스 응답은 시점과 화면 변경에 따라 달라질 수 있습니다.

## 지원하지 않는 작업

로그인, CAPTCHA 우회, 예약 신청, 결제, 리뷰 게시, 매장 정보 수정은 지원하지 않습니다. API 키나 사용자 계정 정보를 입력받지 않습니다.
