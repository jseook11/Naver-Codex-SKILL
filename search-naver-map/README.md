# Search Naver Map

에이전트가 자연어 요청을 해석해 네이버 지도·플레이스·리뷰·예약 도구를 자유롭게 조합하는 Agent Skill입니다.

## 에이전트에게 설치시키기

Agent Skills를 지원하는 에이전트에게 저장소 URL과 함께 이렇게 요청합니다.

> https://github.com/jseook11/naver-map-serp.git 저장소의 스킬을 설치해줘. `SKILL.md`와 설치 문서를 읽고 전용 실행환경까지 준비한 다음 `capabilities --json`으로 정상 설치를 확인해줘.

에이전트는 스킬을 복사하거나 연결하고 다음 bootstrap을 실행합니다.

```bash
python3 scripts/bootstrap.py
bin/naver-place capabilities --json
```

시스템 Python에는 패키지를 설치하지 않습니다. 스킬 폴더의 `.venv`만 사용합니다.

직접 검증하려면:

```bash
git clone https://github.com/jseook11/naver-map-serp.git
cd naver-map-serp/search-naver-map
python3 scripts/bootstrap.py
bin/naver-place capabilities --json
```

## 대표 사용 장면

설치 후 사용자는 스크립트나 옵션을 배울 필요 없이 자연어로 요청합니다.

> 제주 게스트하우스 중 7월 20일부터 22일까지 남자 2명이 묵을 수 있고, 서귀포가 아니며, 실제 예약 가능한 객실이 있는 곳을 찾아줘.

숙소 찾기는 대표 데모일 뿐 제품 범위가 아닙니다. 같은 capability를 에이전트가 다르게 조합할 수 있습니다.

- 떡볶이를 파는 장소 후보와 메뉴 근거 찾기
- 특정 키워드에서 한 매장의 지도 노출 순위 확인
- 리뷰 신호를 이용한 장소 비교
- 자영업자의 매장·경쟁점 공개 정보 조사
- 식당의 특정 날짜·시간대 예약 가능 여부 확인

## 철학

이 저장소는 검색 workflow를 코드에 고정하지 않습니다.

```text
사용자 의도
    ↓
에이전트의 추론
    ↓
search / detail / reviews / booking 자유 조합
    ↓
출처·완전성·오류가 명시된 결과
```

스킬은 도구의 능력, 입력, 출력, 오류, 안전 경계를 설명합니다. 어떤 순서로 무엇을 찾고 어떻게 추천할지는 에이전트가 사용자 요청에 맞춰 판단합니다.

## 안전 범위

공개된 읽기 전용 정보만 조회합니다. 로그인, CAPTCHA 우회, 예약 제출, 결제, 리뷰 작성은 지원하지 않습니다. 예약 가능 여부와 가격은 조회 시점의 관찰값이며 확정 예약을 보장하지 않습니다.

자세한 내용:

- [설치](references/installation.md)
- [capability 목록](references/capabilities.md)
- [결과와 오류 계약](references/result-contract.md)
- [숙소 탐색 예시](references/examples-accommodation.md)
- [자영업자 활용 예시](references/examples-business.md)
