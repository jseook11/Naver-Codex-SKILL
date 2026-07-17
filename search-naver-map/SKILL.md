---
name: search-naver-map
description: Use public Naver Map, Place, visitor-review, and Booking data to find Korean places, inspect place details, compare public reviews, or check observed booking availability. Use only for public read-only research. Do not use for login, reservation submission, payment, review posting, access-control bypass, Naver Blog SERP tracking, or non-Naver services.
---

# Search Naver Map

Use the bundled CLI to collect public Naver place evidence for the user's request. Resolve every path relative to this file.

## Setup

```bash
SKILL_DIR="<directory containing this SKILL.md>"
cd "$SKILL_DIR"

if [ ! -x .venv/bin/python ]; then
  python3 scripts/bootstrap.py
fi

bin/naver-place capabilities --json
```

If setup fails, report the command and error. Do not install dependencies into the system Python.

## Commands

Read `capabilities --json` before assuming an argument or enum.

- `search`: find ordered places for a Map query and return IDs, addresses, coordinates, observed positions, and links.
- `detail`: read the public profile, hours, menus, links, feeds, and blog-review metadata for a Place ID or URL.
- `reviews`: read a bounded number of public visitor reviews and optionally filter by owner-reply state.
- `booking`: read public accommodation inventory, date prices, capacity, options, or time slots from a query, Booking URL, or business ID.

Choose the commands needed for the evidence requested. Do not apply a fixed search sequence or add industry-specific recommendation rules.

## Read every result before answering

- Check `status`, `completeness`, `warnings`, and `errors` even when the process exits with code `0`.
- Keep usable `partial` data, but state what could not be checked and why.
- Treat `is_available: null` as unknown, never as available.
- Treat prices, inventory, hours, and observed search positions as point-in-time evidence from `fetched_at`.
- Do not present fixture replay time as live observation time.
- Use `compact` or `standard` unless the request needs descriptions, media, options, or extended public reviewer fields.

Include relevant Place or Booking links in the response. If the visible source page is incomplete, do not describe the result as an exhaustive ranking or collection.

## Safety

Use only public, read-only requests and local fixture replay.

Never:

- use a logged-in browser or authenticated session;
- send cookies, authorization headers, `.netrc` credentials, or environment proxy credentials;
- solve or bypass CAPTCHA, access controls, blocks, or rate limits;
- submit reservations, payments, reviews, messages, or profile changes;
- switch to browser automation or a hosted proxy after a public request is rejected;
- claim uncertain price, capacity, inventory, identity, or completeness as confirmed.

## References

- Read [installation](references/installation.md) for supported skill paths and runtime setup.
- Read [commands](references/capabilities.md) for exact inputs, outputs, and offline examples.
- Read [result contract](references/result-contract.md) for status, errors, exit codes, and views.
- Read [usage examples](references/usage-examples.md) for realistic requests and evidence limits.

## Response requirements

- Answer from returned evidence, not assumptions.
- Distinguish empty, partial, blocked, rate-limited, `upstream_changed`, and not-found cases.
- State the observation time and source links when they affect the conclusion.
- Stop at the read-only boundary.
