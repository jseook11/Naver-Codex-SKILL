# JSON 결과와 오류 코드

`search`, `detail`, `reviews`, `booking`은 같은 JSON 틀을 사용합니다.

```json
{
  "schema_version": "1",
  "status": "ok",
  "capability": "map.search",
  "request": {},
  "data": {},
  "provenance": [],
  "completeness": {
    "complete": true,
    "stop_reason": "requested_limit"
  },
  "budget": {
    "requests_used": 1,
    "request_limit": 40,
    "elapsed_seconds": 0.42,
    "elapsed_limit_seconds": 120
  },
  "warnings": [],
  "errors": []
}
```

## 공통 필드

| 필드 | 의미 |
| --- | --- |
| `schema_version` | JSON 형식 버전 |
| `status` | 정상, 빈 결과, 일부 결과, 실패 구분 |
| `capability` | 실행한 조회 종류 |
| `request` | 명령이 해석한 입력값. 입력 오류가 나면 잘못된 값도 포함될 수 있음 |
| `data` | 실제로 확인한 데이터. 일부 결과도 여기에 남음 |
| `provenance` | 출처, 조회 작업, 시각, 실시간 여부 |
| `completeness` | 요청한 범위를 끝까지 확인했는지와 중단 이유 |
| `budget` | 사용한 요청 수와 실행 시간 |
| `warnings` | 데이터는 쓸 수 있지만 주의할 내용 |
| `errors` | 원인별 오류 코드와 세부 정보 |

## `status`

| 값 | 의미 |
| --- | --- |
| `ok` | 요청한 범위에서 사용할 수 있는 결과를 정상적으로 반환 |
| `empty` | 요청은 정상적으로 끝났지만 일치하는 항목이 없음 |
| `partial` | 사용할 수 있는 데이터는 있으나 일부 조회가 끝나지 않았거나 원본 화면의 범위가 제한됨 |
| `error` | 요청을 만족하는 데이터를 반환하지 못함 |

`partial`은 종료 코드 `0`을 사용할 수 있습니다. 프로세스 성공 여부만 보지 말고 `status`, `completeness`, `warnings`, `errors`를 읽어야 합니다.

검색 결과 0건은 `empty`입니다. 사용자가 직접 지정한 Place ID나 Booking ID를 찾지 못했을 때만 `not_found`를 사용합니다.

리뷰에서 공개 추천순·최신순 snapshot 두 개를 정상 처리했다면, 표본의 `total_available`이 반환 수보다 커도 `ok`일 수 있습니다. 전체 리뷰를 순회하는 명령이 아니므로 원본 총수와의 차이만으로 `partial`이 되지 않습니다. 한 snapshot만 처리하고 다른 하나가 실패했다면 사용할 수 있는 표본을 남긴 `partial`입니다.

## `completeness`

`complete`가 `false`이면 `stop_reason`을 확인합니다. 명령에 따라 아래 정보가 함께 들어갑니다.

- `pages_fetched`: 확인한 페이지 수
- `requested_count`: 명령이 처리하려던 대상 수
- `returned_count`: 명령이 반환하거나 처리를 마친 대상 수

두 필드의 정확한 대상은 명령마다 다릅니다. 예를 들어 `booking`에서는 선택된 사업 후보 수와 처리를 마친 후보 수를 뜻할 수 있습니다.

예를 들어 지도 검색 화면에 더 많은 결과가 있다고 표시됐지만 현재 화면에서 요청 수를 채우지 못하면 `source_page_limit`으로 끝납니다.

`reviews`의 완료 범위는 전체 upstream 리뷰가 아니라 추천순·최신순 HTML snapshot 두 개입니다. `limit`은 `0..10` 범위에서 `latest`, `recommended`, `recommended_keyword_only` 각 표본에 적용됩니다. 두 snapshot과 세 root를 정상 처리하면 `complete: true`, `stop_reason: snapshot_complete`입니다. `total_available`은 표본별 문맥 정보이지 완료 목표가 아니므로 `total_available > returned_count`만으로 `source_page_limit`을 사용하지 않습니다. `pages_fetched`는 처리한 source snapshot 수입니다. `requested_count`와 `returned_count`는 세 표본의 슬롯을 합산하므로 여러 표본에 중복 노출된 리뷰도 각각 계산합니다. 반면 `data.returned_count`는 `data.reviews`의 중복 제거된 항목 수입니다.

## 오류 코드

| 코드 | 의미 |
| --- | --- |
| `invalid_input` | 필수 입력 누락, 잘못된 값이나 파일 |
| `dependency_missing` | 실행 환경이나 의존성이 준비되지 않음 |
| `network_error` | 연결 실패, 시간 초과, 서버 오류 |
| `rate_limited` | 네이버가 요청 수를 제한함 |
| `blocked` | 로그인·CAPTCHA·접근 제한 화면을 만남 |
| `upstream_rejected` | 공개 요청이 거부되거나 지원하지 않는 응답을 받음 |
| `upstream_changed` | 예상하던 공개 응답 구조가 바뀜 |
| `secondary_not_found` | 기본 정보는 있으나 선택한 보조 정보가 없음 |
| `not_found` | 직접 지정한 Place 또는 Booking 대상을 찾지 못함 |
| `request_budget_exhausted` | 최대 요청 수를 모두 사용함 |
| `time_budget_exhausted` | 전체 실행 시간 제한을 넘김 |
| `internal_error` | 예상하지 못한 프로그램 오류 |

오류 항목에는 작업 이름, HTTP 상태, 재시도 가능 여부, 필요한 세부 정보가 포함될 수 있습니다.

## 종료 코드

| 코드 | 의미 |
| --- | --- |
| `0` | `ok`, `empty`, 사용할 수 있는 `partial` |
| `2` | 잘못된 입력 |
| `3` | 의존성 누락 |
| `10` | 네트워크, 접근 제한, 요청 제한, 거부, 실행 한도 초과 |
| `11` | 네이버 응답 구조 변경 |
| `12` | 직접 지정한 대상을 찾지 못함 |
| `1` | 예상하지 못한 내부 오류 |

## 출력 범위

| 값 | 용도 |
| --- | --- |
| `compact` | 장소 선택에 필요한 핵심 필드 중심 |
| `standard` | 긴 설명과 큰 이미지 배열을 줄인 기본 출력 |
| `full` | 정규화한 공개 필드를 더 넓게 포함 |

`search`, `detail`, `reviews`에서는 주로 출력 필드를 줄입니다. `booking --view full`은 설명, 이미지, 옵션을 위해 추가 조회를 할 수 있으므로 요청 수와 `status`, `completeness`가 달라질 수 있습니다.

`standard`는 리뷰어 ID, 영수증 URL, 프로필 이미지 등을 줄이지만 공개 닉네임과 리뷰 본문은 남을 수 있습니다.

`full`은 공개 응답에 포함된 리뷰어·영수증 관련 메타데이터를 더 많이 담을 수 있습니다. 어떤 출력 범위도 완전한 익명화를 보장하지 않습니다.

리뷰의 출력 범위는 표본 수, 원본 순서, root 선택을 바꾸지 않습니다. `data.samples.latest`와 `recommended`의 `review_ids`는 본문이 있는 리뷰만 가리키고, `recommended_keyword_only.review_ids`는 별도 공개 키워드 신호를 가리킵니다. 실제 정규화 항목은 중복 제거된 `data.reviews`에 있으며 `sample_sources`로 유형을 구분합니다. `full`도 추가 리뷰 화면을 요청하지 않습니다.

## 출처와 시각

실시간 조회의 `fetched_at`은 정보를 관찰한 시각입니다. 저장된 테스트 데이터에는 기록 당시 시각이 있으면 그 값을 사용합니다.

기록 시각이 없으면 `fetched_at`은 `unknown`이고 `provenance[].detail.replayed_at`에 재현 실행 시각을 남깁니다. `replayed_at`은 최신 예약·가격을 확인한 시각이 아닙니다.

실시간 리뷰 provenance는 추천순과 최신순 공개 HTML snapshot을 각각 가리킵니다. 저장된 두 HTML로 재생하면 둘 다 `live: false`이며, 재생 시각을 실제 리뷰 관찰 시각으로 해석하지 않습니다.

## 요청 상태와 인증

공개 요청은 쿠키, 인증 헤더, `.netrc`, 환경 프록시의 인증 정보를 사용하지 않습니다. 응답이 설정한 쿠키도 다음 요청으로 가져가지 않습니다.

`search`와 `detail`은 HTTPS 네이버 주소로 향하는 리다이렉트를 최대 5번 따르며 각 이동도 요청 수에 포함합니다. 알려진 로그인·CAPTCHA 화면은 `blocked`로 분류합니다.

`booking`은 리다이렉트를 따르지 않습니다. `search`, `detail`, `reviews`는 HTTPS·네이버 호스트·인증정보 제거 조건을 만족하는 제한된 리다이렉트만 따릅니다. 어느 명령도 로그인이나 접근 제한을 우회하지 않습니다.
