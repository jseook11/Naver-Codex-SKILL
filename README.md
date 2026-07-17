# Naver Map SERP Agent Skill

에이전트가 네이버 지도·플레이스·방문자리뷰·예약 정보를 탐색할 때 사용하는 공개 읽기 전용 Agent Skill입니다.

핵심 구현은 [`search-naver-map`](search-naver-map/)에 있습니다. 검색 순서나 업종별 추천 규칙을 코드에 고정하지 않고, 에이전트가 사용자의 자연어 요청에 맞춰 `search`, `detail`, `reviews`, `booking` capability를 자율적으로 조합합니다.

> 이 저장소는 NAVER와 제휴하거나 NAVER가 보증하는 공식 프로젝트가 아닙니다.

## Git 링크로 설치시키기

Codex, Claude Code처럼 Agent Skills를 지원하는 에이전트에게 다음처럼 요청할 수 있습니다.

> https://github.com/jseook11/naver-map-serp.git 저장소의 `search-naver-map` 스킬을 설치해줘. `SKILL.md`와 설치 문서를 읽고 전용 실행환경을 준비한 뒤 `capabilities --json`으로 정상 설치까지 확인해줘.

직접 검증하려면:

```bash
git clone https://github.com/jseook11/naver-map-serp.git
cd naver-map-serp/search-naver-map
python3 scripts/bootstrap.py
bin/naver-place capabilities --json
```

bootstrap은 시스템 Python을 변경하지 않고 스킬 폴더의 `.venv`에 고정된 의존성을 설치합니다.

## 활용 예

- 특정 날짜에 실제 예약 가능한 숙소 후보 찾기
- 메뉴 근거와 함께 떡볶이를 파는 매장 찾기
- 공개 리뷰 신호를 이용해 장소 비교하기
- 특정 키워드에서 매장의 네이버 지도 노출 순위 확인하기
- 날짜·시간대별 음식점 예약 가능 여부 확인하기

숙소나 음식점은 예시일 뿐 제품 범위가 아닙니다. capability의 능력·입출력·오류·안전 경계만 제공하며, 무엇을 어떤 순서로 탐색할지는 에이전트가 판단합니다.

## 설계 원칙

- 공개 정보만 사용하는 stateless read-only 요청
- `ok`, `empty`, `partial`, `error`를 구분하는 버전된 결과 계약
- provenance, completeness, typed error와 요청 예산 제공
- 기본 출력에서 긴 설명·이미지·리뷰어 식별정보 최소화
- 로그인·쿠키·환경 프록시·`.netrc` 자격증명 미사용
- CAPTCHA, 접근제어, rate limit 우회 금지

로그인, 예약 제출, 결제, 리뷰 게시 같은 쓰기 작업은 지원하지 않습니다. 예약 가능 여부·가격·검색 순위는 조회 시점의 관찰값이며 실제 예약이나 노출을 보장하지 않습니다.

## 개발과 검증

```bash
cd search-naver-map
python3 scripts/bootstrap.py --dev
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider
.venv/bin/python -m compileall -q naver_place scripts
```

자세한 사용법은 [`search-naver-map/SKILL.md`](search-naver-map/SKILL.md), [capability 목록](search-naver-map/references/capabilities.md), [결과 계약](search-naver-map/references/result-contract.md)을 참고하세요.

## License

[MIT License](LICENSE)
