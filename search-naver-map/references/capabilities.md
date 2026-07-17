# Capabilities

Run `bin/naver-place capabilities --json` for the authoritative arguments and enums.

## `search`

Search Naver Map for ordered Place candidates. It can return IDs, names, categories, addresses, coordinates, ranks, Place links, and exposed reservation links. Target matching is optional in the new CLI.

```bash
bin/naver-place search --query "..." [--limit N] [--sort relativity|distance] \
  [--include-text TEXT] [--exclude-text TEXT] [--target-id ID] [--target-name NAME]
```

`--html` replays a sanitized Map fixture without network.

The public Map surface may report more results than one source page contains. If the requested limit is not satisfied, this is returned as `partial` with `stop_reason: source_page_limit`, not as an exhaustive result.

## `detail`

Inspect a known Place ID or URL. Base profile and optional hours/feed sources have separate provenance and errors, so a failed secondary source can return a useful partial result.

```bash
bin/naver-place detail --place ID_OR_URL [--no-feed] [--no-hours]
```

Use `--html`, `--feed-html`, `--hours-json`, and `--offline` for deterministic fixture replay.

## `reviews`

Collect bounded public visitor reviews using cursor pagination. Owner-reply filtering is optional. Standard output avoids reviewer IDs and receipt URLs.

```bash
bin/naver-place reviews --place ID_OR_URL [--limit N] [--page-size N] \
  [--owner-reply all|exclude_replied|only_replied]
```

`--raw-dir` replays sanitized `page-*.json` responses.

## `booking`

Inspect a known Booking URL/ID or discover booking businesses from a Map query. Supports accommodation date ranges and time-booking dates, guests, generic place/item/option text filters, availability, capacity, date prices, and stock when exposed.

```bash
bin/naver-place booking (--query TEXT | --booking-url URL | --business-id ID) \
  (--check-in YYYY-MM-DD --check-out YYYY-MM-DD | --date YYYY-MM-DD)
```

Text filters are repeatable at place, item, and option scope. `--raw-dir` replays sanitized Booking responses. The default broad-query ceiling is 10 businesses.

`is_available` is `null` when required capacity, inventory, schedule, or option facts are unknown. It is never promoted to `true` from missing data.

`observed_item_count` distinguishes a real business with locally filtered-out items from a directly requested business that returned no item evidence.

## Composition

Capabilities do not impose a global sequence. The agent may use one capability or combine several based on the evidence required by the user's request.

All four commands accept `--view compact|standard|full`, `--request-budget`, `--time-budget`, timeouts, and `--output`. Inspect `capabilities --json` rather than assuming an argument or enum.
