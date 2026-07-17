# Example: Accommodation Discovery

Example user request:

> 제주 게스트하우스 중 7월 20일부터 22일까지 남자 2명이 묵을 수 있고, 서귀포가 아니며, 실제 예약 가능한 객실이 있는 곳을 찾아줘.

An agent can infer a Map query, dates, guest count, and generic place/item/option filters, then choose whichever capabilities are needed. It should preserve partial results, show observed prices and inventory with timestamps, explain exclusions from returned evidence, and provide Booking links.

This is an illustrative composition, not a mandatory workflow. The capability implementation must not contain guesthouse-, region-, or gender-specific recommendation rules.
