# Result Contract

Every new CLI capability returns a versioned JSON envelope.

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

## Status

- `ok`: the bounded operation returned usable data.
- `empty`: a valid collection request completed with no matches.
- `partial`: usable data exists, or a bounded source page is known to be non-exhaustive. Inspect both warnings and errors.
- `error`: no usable result satisfies the request.

An empty search is not `not_found`. `not_found` is reserved for a directly supplied Place or Booking identifier that does not resolve.

## Important error codes

- `invalid_input`
- `dependency_missing`
- `network_error`
- `rate_limited`
- `blocked`
- `upstream_rejected`
- `upstream_changed`
- `secondary_not_found`
- `not_found`
- `request_budget_exhausted`
- `time_budget_exhausted`
- `internal_error`

Partial results exit successfully so the agent can use `data`. The agent must still inspect `status`, `completeness`, and `errors`.

CLI exit codes are `0` for `ok`, `empty`, and usable `partial`; `2` for invalid input; `3` for missing runtime dependencies; `10` for network, block, rate-limit, upstream rejection, or budget failures; `11` for schema drift; `12` for a directly requested resource that was not found; and `1` for an unexpected internal failure.

## Views

- `compact`: decision-critical fields only.
- `standard`: useful default evidence without large descriptions, media arrays, reviewer IDs, or receipt URLs.
- `full`: extended normalized public fields.

Selecting a smaller view does not make collection incomplete.

## Provenance time

Live provenance uses `fetched_at` as the observation time. Offline fixtures use their recorded capture date/time when supplied. If no capture metadata is available, `fetched_at` is `unknown`; `provenance[].detail.replayed_at` records when the fixture was replayed. Replay time must not be presented as fresh availability evidence.

## Stateless requests

Public requests do not carry cookies, Authorization headers, `.netrc` credentials, or environment proxy credentials. Redirects are followed only through the bounded transport, count against the request budget, and cannot downgrade HTTPS or leave allowed Naver origins.
