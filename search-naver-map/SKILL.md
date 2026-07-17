---
name: search-naver-map
description: Use public, read-only Naver Map, Place, review, and Booking capabilities when an agent needs to discover Korean places, inspect place data, analyze public reviews, or check public booking availability. The agent decides how to combine capabilities from the user's intent. Do not use for reservation submission, login, payment, review posting, access-control bypass, Naver Blog SERP tracking, or non-Naver services.
---

# Search Naver Map

This skill provides composable Naver Place capabilities. Interpret the user's request, inspect the available capability contracts, and select any useful combination. Do not force requests through a predefined workflow or limit use to the examples in this skill.

Resolve all paths relative to this `SKILL.md`.

## Runtime

Use the isolated runtime created by the bundled bootstrap:

```bash
SKILL_DIR="<directory containing this SKILL.md>"
cd "$SKILL_DIR"

if [ ! -x .venv/bin/python ]; then
  python3 scripts/bootstrap.py
fi

bin/naver-place capabilities --json
```

If bootstrap or dependency verification fails, report the exact failure. Do not install packages into the user's system Python as a workaround.

## Capability Catalog

`bin/naver-place capabilities --json` is the authoritative machine-readable catalog. The public capabilities are independent tools:

- `search`: search public Naver Map results and return ordered Place summaries, Place IDs, addresses, coordinates, ranks, and reservation URLs when exposed.
- `detail`: inspect a known Place ID or URL and return public profile, hours status, links, menus, media metadata, feeds, and blog-review metadata.
- `reviews`: inspect public visitor reviews with bounded cursor pagination and optional owner-reply filtering.
- `booking`: inspect public Naver Booking businesses, accommodation inventory, date prices, capacity, options, or time-booking slots.

The agent owns:

- interpreting natural-language intent;
- choosing tools and call order;
- deciding whether more evidence is needed;
- constructing generic place, item, option, date, time, or text filters;
- comparing candidates and explaining recommendations.

The skill does not encode domain-specific recommendation rules. A request may concern accommodation, food, products, local-business research, audience signals in reviews, or a use not anticipated by the examples.

## Result Contract

New commands emit a versioned JSON envelope with:

- `status`: `ok`, `empty`, `partial`, or `error`;
- `data`: usable normalized data, including partial data when available;
- `provenance`: which public surface or fixture produced each portion;
- `completeness`: whether the bounded request completed and why it stopped;
- `warnings` and typed `errors`;
- request-count and elapsed-time budget usage.

Always inspect `status`, `completeness`, `warnings`, and `errors` before presenting a conclusion. Never describe a partial result as exhaustive. Treat live public availability as observed evidence at `fetched_at`, not as a reservation guarantee. Fixture replay uses its recorded capture time when available; otherwise `fetched_at` is `unknown` and `replayed_at` records execution time.

Default output minimizes agent context. Use a fuller view only when the user's request needs descriptions, images, extended reviewer metadata, or option detail.

See [result contract](references/result-contract.md) and [capability reference](references/capabilities.md).

## Safety Boundary

Allowed:

- public, stateless, read-only Map/Place/Booking requests;
- bounded pagination and polite retry;
- parsing saved sanitized fixtures;
- local filtering and agent-authored summaries.

Never:

- log in or use a user's authenticated session;
- send cookies, Authorization headers, `.netrc` credentials, or environment proxy credentials;
- solve or bypass CAPTCHA, access controls, or blocks;
- rotate identities to evade rate limits;
- submit a reservation, payment, review, message, or other write action;
- claim uncertain inventory, capacity, price, or completeness as confirmed;
- commit raw captures containing cookies, tokens, tracking values, receipt URLs, or unnecessary reviewer identifiers.

When a public source rejects a request, return or report the typed error. Do not silently switch to browser automation or a hosted proxy.

## Examples Are Non-Normative

- [Accommodation discovery example](references/examples-accommodation.md)
- [Local-business uses](references/examples-business.md)

These show possible compositions only. They are not mandatory search sequences, routing rules, or limits on what the tools can do.

## Done When

- The requested evidence was collected or a typed reason explains why it could not be.
- Partial, empty, blocked, rate-limited, and changed-upstream states are distinguished.
- The answer cites the relevant Place/Booking links and observation time when useful.
- No write, login, payment, or access-control boundary was crossed.
