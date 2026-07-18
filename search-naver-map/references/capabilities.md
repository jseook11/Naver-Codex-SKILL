# 명령과 입출력

`bin/naver-place`는 로컬 CLI입니다. HTTP 서버나 REST API 주소를 제공하지 않습니다. 명령 이름과 인수, 선택값은 다음 명령에서 확인할 수 있습니다.

```bash
bin/naver-place capabilities --json
```

이 출력은 실제 CLI 정의에서 만들어집니다. 문서와 명령이 다를 때는 `capabilities --json`을 기준으로 삼습니다.

## 인증

API 키, OAuth, 로그인 설정이 없습니다. 쿠키와 인증 헤더는 거부하며 `.netrc`와 환경 프록시는 사용하지 않습니다. `--user-agent`는 공개 요청의 User-Agent만 바꾸며 인증 기능을 추가하지 않습니다.

## 공통 옵션

| 옵션 | 기본값 | 설명 |
| --- | --- | --- |
| `--view compact|standard|full` | `standard` | 조회·출력 범위. `booking`의 `full`은 추가 조회 가능 |
| `--request-budget N` | `40` | 한 번 실행할 때 허용할 최대 요청 수, 1~100 |
| `--time-budget SECONDS` | `120` | 전체 실행 시간 제한 |
| `--read-timeout SECONDS` | `30` | 응답 읽기 제한 시간 |
| `--output PATH` | 표준 출력 | JSON을 파일로 저장 |
| `--user-agent TEXT` | 명령별 기본값 | 공개 요청의 User-Agent 변경 |

`search`와 `detail`에는 연결 제한 시간을 정하는 `--connect-timeout`도 있습니다. 기본값은 15초입니다.

## `search`

네이버 지도 검색 결과 한 화면에서 장소 후보를 읽습니다.

```bash
bin/naver-place search \
  --query "성수 떡볶이" \
  --limit 10 \
  --include-text "떡볶이" \
  --exclude-text "폐업"
```

주요 입력:

- `--query`: 필수 검색어
- `--limit`: 반환할 장소 수, 1~100
- `--sort relativity|distance`: 네이버 검색 정렬
- `--include-text`, `--exclude-text`: 장소 요약에 적용할 반복 가능한 문자열 필터
- `--target-id`, `--target-name`: 특정 장소가 조회된 결과에 있는지 확인
- `--html`: 저장된 지도 HTML로 오프라인 실행

주요 결과:

- 장소 ID, 이름, 업종, 주소, 좌표, 전화번호
- 조회된 화면 안에서의 위치
- Place URL과 공개된 예약 URL
- 네이버가 표시한 전체 건수와 실제로 읽은 건수
- 대상 장소를 지정했다면 발견 여부와 위치

이 명령은 전체 검색 페이지를 순회하지 않습니다. 화면에 더 많은 결과가 있다고 표시되지만 요청한 수를 채우지 못하면 `partial`과 `source_page_limit`을 반환합니다.

## `detail`

Place ID 또는 안전한 네이버 Place URL에서 공개 상세 정보를 읽습니다.

```bash
bin/naver-place detail --place 1234567890
```

주요 입력:

- `--place`: 숫자 Place ID 또는 `https` 네이버 Place URL
- `--no-feed`: 공개 소식·피드 조회 생략
- `--no-hours`: 영업시간 조회 생략
- `--offline`: 네트워크 사용 금지
- `--html`, `--feed-html`, `--hours-json`: 해당 조회에 저장된 테스트 데이터 사용

주요 결과:

- 상호, 업종, 주소, 홈페이지와 공개 링크
- 방문자·블로그 리뷰 수
- 영업시간과 현재 영업 상태
- 메뉴, 공개 소식, 블로그 리뷰 메타데이터
- `--view full`일 때 Place·메뉴판·메뉴 항목·피드의 공개 이미지 URL
- Place URL과 조회 시각

기본 Place 화면은 필수 정보입니다. 영업시간이나 피드처럼 보조 정보만 실패했다면 기본 정보는 남기고 `partial`로 표시합니다.

구조화 메뉴가 메뉴 질문을 답하기에 부족하면 에이전트가 `full` 결과의 이미지 URL을 제한적으로 내려받아 비전으로 확인할 수 있습니다. 이는 CLI의 이미지 인식 기능이 아니며 [메뉴 이미지 fallback](menu-image-fallback.md)의 근거·안전 규칙을 따릅니다.

## `reviews`

Place의 공개 방문자 리뷰를 지정한 수만큼 읽습니다.

```bash
bin/naver-place reviews \
  --place 1234567890 \
  --limit 50 \
  --owner-reply exclude_replied
```

주요 입력:

- `--place`: 숫자 Place ID 또는 네이버 Place URL
- `--limit`: 반환할 리뷰 수, 0~500
- `--page-size`: 한 페이지에서 요청할 리뷰 수, 1~50
- `--owner-reply all|exclude_replied|only_replied`: 사장님 답글 상태 필터
- `--request-delay`: 다음 페이지 전 대기 시간, 기본 1.5초
- `--raw-dir`: 저장된 `page-*.json`으로 오프라인 실행

주요 결과:

- 리뷰 본문, 공개 닉네임, 방문·작성 시각, 태그와 이미지 수
- 사장님 답글 여부와 답글 작성 시각
- 네이버가 표시한 리뷰 수, 반환한 수, 확인한 페이지 수

커서가 반복되거나 사라진 경우, 표시된 총 건수보다 일찍 끝난 경우, 다음 페이지가 실패한 경우를 별도 오류로 남깁니다.

## `booking`

네이버 Booking에 공개된 숙박 상품 또는 시간 예약 정보를 읽습니다.

```bash
bin/naver-place booking \
  --query "제주 게스트하우스" \
  --check-in 2026-07-20 \
  --check-out 2026-07-22 \
  --guests 2 \
  --available-only
```

조회 대상을 아래 셋 중 하나로 지정합니다.

- `--query`: 지도 검색어로 예약 링크 후보 탐색
- `--booking-url`: 공개 Booking URL
- `--business-id`: Booking 사업 ID. 이 경우 `--business-type-id`도 필요

날짜 입력은 숙박과 시간 예약 중 하나를 사용합니다.

- 숙박: `--check-in`, `--check-out`
- 시간 예약: `--date`, 필요하면 `--time-from`, `--time-to`

추가 입력:

- `--guests`, `--units`: 인원과 필요한 객실·예약 단위 수
- `--available-only`: 확인된 예약 가능 항목만 남김
- `--include-text`, `--exclude-text`: 항목 근거 전체에 적용할 반복 가능한 문자열 필터
- `--place-include-text`, `--item-include-text`, `--option-include-text`: 범위별 반복 가능한 포함 필터
- `--place-exclude-text`, `--item-exclude-text`, `--option-exclude-text`: 범위별 반복 가능한 제외 필터
- `--limit`: 지도에서 확인할 후보 수, 기본 20, 최대 100
- `--max-businesses`: 검색어로 찾았을 때 확인할 사업 수, 기본 10, 최대 20
- `--query-mode auto|broad|specific`: 지도 후보를 고르는 범위
- `--detail-mode minimal|full`: 항목별 추가 상세 조회 여부
- `--sort relativity|distance`: 지도 후보 정렬
- `--raw-dir`: 저장된 Booking 응답 재생

주요 결과:

- 장소와 Booking 사업 식별자
- 객실·상품·시간대, 날짜별 가격, 공개 재고와 수용 인원
- 실제로 관찰한 항목 수와 필터를 통과한 항목 수
- `is_available: true|false|null`

`null`은 필요한 재고, 수용 인원, 일정 또는 옵션 정보가 부족하다는 뜻입니다. 정보가 없다는 이유만으로 예약 가능으로 판단하지 않습니다.

## 출력 파일과 오프라인 실행

`--output result.json`을 사용하면 표준 출력 대신 파일에 같은 JSON을 씁니다. 디렉터리가 없으면 생성합니다.

저장된 응답은 테스트와 재현에 사용할 수 있습니다. 명령마다 네트워크를 막는 범위가 다릅니다.

- `search --html`: 저장된 지도 HTML만 읽으며 네트워크를 사용하지 않음
- `detail --offline`: `--html`, `--feed-html`, `--hours-json`만 사용하고 네트워크를 금지함. 필요한 파일이 빠지면 오류로 기록
- `reviews --raw-dir`: 저장된 `page-*.json`만 읽으며 네트워크를 사용하지 않음
- `booking --raw-dir`: Booking 응답만 재생함. `--query`를 함께 쓰면 지도 후보 탐색에는 네트워크를 사용

Booking까지 네트워크 없이 재현하려면 `--query` 대신 저장된 응답에 맞는 `--booking-url` 또는 `--business-id`와 `--business-type-id`를 사용합니다.

저장된 응답의 기록 시각이 없으면 `fetched_at`은 `unknown`입니다. 오프라인 실행 시각을 최신 가격이나 재고를 확인한 시각으로 해석하면 안 됩니다.

응답 상태, 오류와 종료 코드는 [결과 형식](result-contract.md)을 참고하세요.
