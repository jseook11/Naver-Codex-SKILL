"""Unified public CLI for independent Naver Place capabilities."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping

from .capabilities import (
    get_booking_availability,
    get_place_detail,
    get_reviews,
    search_places,
)
from .contracts import (
    CapabilityError,
    CapabilityResult,
    Completeness,
    ErrorCode,
    RequestBudget,
    RequestPolicy,
    Status,
)
from .serializers import V1Serializer
from .transport import Transport


CAPABILITY_INFO = {
    "search": {
        "capability": "map.search",
        "description": "Search ordered public Naver Map Place summaries.",
    },
    "detail": {
        "capability": "place.detail",
        "description": "Inspect a public Naver Place profile and optional secondary sources.",
    },
    "reviews": {
        "capability": "place.reviews",
        "description": "Collect bounded latest and recommended public review snapshots.",
    },
    "booking": {
        "capability": "booking.availability",
        "description": "Inspect public accommodation or time-booking availability.",
    },
}
READ_ONLY_BOUNDARY = (
    "Public read-only discovery only; never logs in, bypasses access controls, "
    "or submits reservations, payments, reviews, or messages."
)


class CliUsageError(ValueError):
    pass


class CapabilityArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliUsageError(message)


def _append_argument(
    parser: argparse.ArgumentParser,
    flag: str,
    *,
    help: str,
) -> None:
    parser.add_argument(flag, action="append", default=[], help=help)


def _add_common_arguments(
    parser: argparse.ArgumentParser, *, include_connect_timeout: bool = True
) -> None:
    parser.add_argument(
        "--view",
        choices=("compact", "standard", "full"),
        default="standard",
        help="Agent-facing output size and privacy view.",
    )
    parser.add_argument(
        "--request-budget",
        type=int,
        default=40,
        help="Maximum outbound requests for this invocation (1-100).",
    )
    parser.add_argument(
        "--time-budget",
        dest="max_elapsed_seconds",
        type=float,
        default=120,
        help="Whole-invocation elapsed-time limit in seconds.",
    )
    if include_connect_timeout:
        parser.add_argument(
            "--connect-timeout",
            type=float,
            default=15,
            help="Connection timeout in seconds.",
        )
    parser.add_argument(
        "--read-timeout",
        type=float,
        default=30,
        help="Response read timeout in seconds.",
    )
    parser.add_argument("--output", help="Write the versioned JSON envelope to this path.")


def build_parser() -> CapabilityArgumentParser:
    parser = CapabilityArgumentParser(
        prog="naver-place",
        description="Composable public, read-only Naver Place capabilities for agents.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    catalog = subparsers.add_parser("capabilities", help="Describe available tools and arguments.")
    catalog.add_argument("--json", action="store_true", help="Emit the machine-readable catalog.")

    search = subparsers.add_parser("search", help=CAPABILITY_INFO["search"]["description"])
    search.add_argument("--query", required=True, help="Naver Map search query.")
    search.add_argument("--limit", type=int, default=20, help="Maximum returned places (1-100).")
    search.add_argument(
        "--sort", choices=("", "relativity", "distance"), default="", help="Upstream map sort."
    )
    _append_argument(search, "--include-text", help="Require text in the Place summary; repeatable.")
    _append_argument(search, "--exclude-text", help="Reject text in the Place summary; repeatable.")
    search.add_argument("--target-id", help="Optionally report whether this Place ID is ranked.")
    search.add_argument("--target-name", help="Optionally report whether this Place name is ranked.")
    search.add_argument("--html", help="Replay a saved sanitized map HTML fixture without network.")
    search.add_argument("--user-agent", help="Override the public-request User-Agent.")
    _add_common_arguments(search)

    detail = subparsers.add_parser("detail", help=CAPABILITY_INFO["detail"]["description"])
    detail.add_argument("--place", required=True, help="Numeric Place ID or Naver Place URL.")
    detail.add_argument("--no-feed", action="store_true", help="Do not inspect the optional feed source.")
    detail.add_argument("--no-hours", action="store_true", help="Do not inspect the optional hours source.")
    detail.add_argument("--html", help="Replay saved sanitized Place home HTML.")
    detail.add_argument("--feed-html", help="Replay saved sanitized Place feed HTML.")
    detail.add_argument("--hours-json", help="Replay a saved sanitized hours JSON response.")
    detail.add_argument(
        "--offline",
        action="store_true",
        help="Forbid network; missing enabled fixture sources become typed errors.",
    )
    detail.add_argument("--user-agent", help="Override the public-request User-Agent.")
    _add_common_arguments(detail)

    reviews = subparsers.add_parser("reviews", help=CAPABILITY_INFO["reviews"]["description"])
    reviews.add_argument("--place", required=True, help="Numeric Place ID or Naver Place URL.")
    reviews.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum reviews from each public snapshot group (0-10).",
    )
    reviews.add_argument(
        "--owner-reply",
        choices=("all", "exclude_replied", "only_replied"),
        default="all",
        help="Filter on the public owner-reply signal.",
    )
    reviews.add_argument(
        "--latest-html",
        help="Replay saved sanitized latest-sort visitor-review HTML.",
    )
    reviews.add_argument(
        "--recommended-html",
        help="Replay saved sanitized recommended-sort visitor-review HTML.",
    )
    reviews.add_argument("--user-agent", help="Override the public-request User-Agent.")
    _add_common_arguments(reviews)

    booking = subparsers.add_parser("booking", help=CAPABILITY_INFO["booking"]["description"])
    source = booking.add_mutually_exclusive_group(required=True)
    source.add_argument("--query", help="Map query used to resolve public reservation URLs.")
    source.add_argument("--booking-url", help="Public Naver Booking URL.")
    source.add_argument("--business-id", help="Naver Booking business ID.")
    booking.add_argument("--business-type-id", type=int, help="Required with --business-id.")
    booking.add_argument("--check-in", help="Accommodation arrival date, YYYY-MM-DD.")
    booking.add_argument("--check-out", help="Accommodation departure date, YYYY-MM-DD.")
    booking.add_argument("--date", dest="booking_date", help="Time-booking date, YYYY-MM-DD.")
    booking.add_argument("--guests", type=int, default=1, help="People requiring capacity.")
    booking.add_argument("--units", type=int, default=1, help="Rooms or booking units required.")
    booking.add_argument("--time-from", help="Earliest time slot, HH:MM.")
    booking.add_argument("--time-to", help="Latest time slot, HH:MM.")
    booking.add_argument("--available-only", action="store_true", help="Keep only confirmed available items.")
    for scope in ("", "place-", "item-", "option-"):
        label = scope[:-1] or "item"
        _append_argument(
            booking,
            f"--{scope}include-text",
            help=f"Require text in {label} evidence; repeatable.",
        )
        _append_argument(
            booking,
            f"--{scope}exclude-text",
            help=f"Reject text in {label} evidence; repeatable.",
        )
    booking.add_argument("--limit", type=int, default=20, help="Map candidates considered (1-100).")
    booking.add_argument(
        "--max-businesses", type=int, default=10, help="Booking businesses inspected (1-20)."
    )
    booking.add_argument(
        "--query-mode",
        choices=("auto", "broad", "specific"),
        default="auto",
        help="Map candidate resolution breadth.",
    )
    booking.add_argument(
        "--detail-mode",
        choices=("minimal", "full"),
        default="minimal",
        help="Whether surviving items require extended upstream detail.",
    )
    booking.add_argument(
        "--sort", choices=("", "relativity", "distance"), default="", help="Map discovery sort."
    )
    booking.add_argument("--raw-dir", help="Replay sanitized Booking GraphQL responses.")
    booking.add_argument("--user-agent", help="Override the public-request User-Agent.")
    _add_common_arguments(booking, include_connect_timeout=False)
    return parser


def _action_payload(action: argparse.Action) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "flags": list(action.option_strings) or [action.dest],
        "name": action.dest,
        "required": bool(action.required),
        "help": action.help,
    }
    if action.default is not argparse.SUPPRESS and action.default is not None:
        payload["default"] = action.default
    if action.choices is not None:
        payload["choices"] = list(action.choices)
    if action.type is not None:
        payload["type"] = getattr(action.type, "__name__", str(action.type))
    if action.__class__.__name__ == "_AppendAction":
        payload["repeatable"] = True
    return payload


def capability_catalog(parser: argparse.ArgumentParser) -> dict[str, Any]:
    subparser_action = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    capabilities: list[dict[str, Any]] = []
    arguments_by_command: dict[str, list[dict[str, Any]]] = {}
    for command, info in CAPABILITY_INFO.items():
        command_parser = subparser_action.choices[command]
        arguments_by_command[command] = [
            _action_payload(action)
            for action in command_parser._actions
            if action.dest != "help"
        ]
        required_groups = [
            [action.dest for action in group._group_actions]
            for group in command_parser._mutually_exclusive_groups
            if group.required
        ]
        capabilities.append(
            {
                "command": command,
                **info,
                "arguments": arguments_by_command[command],
                **({"required_one_of": required_groups} if required_groups else {}),
            }
        )

    # Common execution controls are defined once instead of consuming agent
    # context four times. Exact payload equality keeps this derived from the
    # parsers rather than from a second hand-maintained argument list.
    common_names = set.intersection(
        *(set(argument["name"] for argument in values) for values in arguments_by_command.values())
    )
    common_arguments: list[dict[str, Any]] = []
    for name in sorted(common_names):
        matches = [
            next(argument for argument in arguments_by_command[command] if argument["name"] == name)
            for command in CAPABILITY_INFO
        ]
        if all(match == matches[0] for match in matches[1:]):
            common_arguments.append(matches[0])
            for capability in capabilities:
                capability["arguments"] = [
                    argument
                    for argument in capability["arguments"]
                    if argument["name"] != name
                ]
    return {
        "schema_version": "1",
        "tool": "naver-place",
        "safety_boundary": READ_ONLY_BOUNDARY,
        "read_only": True,
        "output_views": ["compact", "standard", "full"],
        "common_arguments": common_arguments,
        "capabilities": capabilities,
        "error_codes": [code.value for code in ErrorCode],
    }


def _catalog_text(catalog: Mapping[str, Any]) -> str:
    lines = ["naver-place capabilities", READ_ONLY_BOUNDARY, ""]
    for capability in catalog["capabilities"]:
        lines.append(
            f"{capability['command']}: {capability['capability']} — {capability['description']}"
        )
    return "\n".join(lines)


def _headers(user_agent: str | None) -> dict[str, str] | None:
    value = (user_agent or os.environ.get("NAVER_MAP_USER_AGENT") or "").strip()
    return {"User-Agent": value} if value else None


def _read_text(path: str | None) -> str | None:
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        label = Path(path).name or "supplied file"
        raise ValueError(f"could not read supplied file: {label}") from None


def _read_json(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    value = json.loads(_read_text(path) or "")
    if not isinstance(value, dict):
        raise ValueError(f"{Path(path).name or 'supplied file'} must contain a JSON object")
    return value


def _budget(args: argparse.Namespace) -> RequestBudget:
    if not math.isfinite(args.read_timeout) or args.read_timeout <= 0:
        raise ValueError("read_timeout must be greater than zero")
    if hasattr(args, "connect_timeout") and (
        not math.isfinite(args.connect_timeout) or args.connect_timeout <= 0
    ):
        raise ValueError("connect_timeout must be greater than zero")
    return RequestBudget(
        max_requests=args.request_budget,
        max_elapsed_seconds=args.max_elapsed_seconds,
    )


def _transport(args: argparse.Namespace, budget: RequestBudget) -> Transport:
    policy = RequestPolicy(
        connect_timeout_seconds=args.connect_timeout,
        read_timeout_seconds=args.read_timeout,
    )
    return Transport(policy=policy, budget=budget)


def _run_capability(args: argparse.Namespace) -> CapabilityResult:
    budget = _budget(args)
    if args.command == "search":
        html = _read_text(args.html)
        return search_places(
            args.query,
            limit=args.limit,
            sort=args.sort,
            target_place_id=args.target_id,
            target_name=args.target_name,
            include_text=args.include_text,
            exclude_text=args.exclude_text,
            transport=None if html is not None else _transport(args, budget),
            budget=budget,
            html=html,
            source_url=f"fixture://{Path(args.html).name}" if args.html else None,
            headers=_headers(args.user_agent),
        )
    if args.command == "detail":
        home_html = _read_text(args.html)
        feed_html = _read_text(args.feed_html)
        hours = _read_json(args.hours_json)
        has_all_enabled_fixtures = home_html is not None and (
            (args.no_feed or feed_html is not None) and (args.no_hours or hours is not None)
        )
        return get_place_detail(
            args.place,
            include_feed=not args.no_feed,
            include_hours=not args.no_hours,
            transport=None
            if args.offline or has_all_enabled_fixtures
            else _transport(args, budget),
            budget=budget,
            home_html=home_html,
            feed_html=feed_html,
            business_hours_payload=hours,
            offline=args.offline,
            headers=_headers(args.user_agent),
        )
    if args.command == "reviews":
        latest_html = _read_text(args.latest_html)
        recommended_html = _read_text(args.recommended_html)
        offline = latest_html is not None or recommended_html is not None
        return get_reviews(
            args.place,
            limit=args.limit,
            owner_reply=args.owner_reply,
            view=args.view,
            request_budget=args.request_budget,
            max_elapsed_seconds=args.max_elapsed_seconds,
            latest_html=latest_html,
            recommended_html=recommended_html,
            latest_source_url=(
                f"fixture://{Path(args.latest_html).name}"
                if args.latest_html
                else None
            ),
            recommended_source_url=(
                f"fixture://{Path(args.recommended_html).name}"
                if args.recommended_html
                else None
            ),
            transport=None if offline else _transport(args, budget),
            budget=budget,
            user_agent=args.user_agent,
        )
    if args.command == "booking":
        return get_booking_availability(
            query=args.query,
            booking_url=args.booking_url,
            business_id=args.business_id,
            business_type_id=args.business_type_id,
            check_in=args.check_in,
            check_out=args.check_out,
            booking_date=args.booking_date,
            guests=args.guests,
            units=args.units,
            available_only=args.available_only,
            include_text=args.include_text,
            exclude_text=args.exclude_text,
            place_include_text=args.place_include_text,
            place_exclude_text=args.place_exclude_text,
            item_include_text=args.item_include_text,
            item_exclude_text=args.item_exclude_text,
            option_include_text=args.option_include_text,
            option_exclude_text=args.option_exclude_text,
            limit=args.limit,
            max_businesses=args.max_businesses,
            query_mode=args.query_mode,
            detail_mode=args.detail_mode,
            time_from=args.time_from,
            time_to=args.time_to,
            view=args.view,
            request_budget=args.request_budget,
            max_elapsed_seconds=args.max_elapsed_seconds,
            raw_dir=args.raw_dir,
            budget=budget,
            timeout=args.read_timeout,
            user_agent=args.user_agent,
            sort=args.sort,
        )
    raise CliUsageError(f"unsupported command: {args.command}")


def _error_result(message: str, *, code: ErrorCode, operation: str = "cli") -> CapabilityResult:
    return CapabilityResult(
        capability="cli",
        request={},
        data={},
        status=Status.ERROR,
        errors=(CapabilityError(code=code, message=message, operation=operation),),
        completeness=Completeness(complete=False, stop_reason=code.value),
    )


def _emit(payload: Mapping[str, Any], output: str | None = None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except CliUsageError as exc:
        result = _error_result(str(exc), code=ErrorCode.INVALID_INPUT)
        _emit(V1Serializer().serialize(result))
        return result.exit_code

    if args.command == "capabilities":
        catalog = capability_catalog(parser)
        if args.json:
            _emit(catalog)
        else:
            print(_catalog_text(catalog))
        return 0

    try:
        result = _run_capability(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = _error_result(str(exc), code=ErrorCode.INVALID_INPUT, operation=args.command)
    except Exception as exc:  # Keep the public CLI envelope stable at the bug boundary.
        result = CapabilityResult(
            capability=CAPABILITY_INFO.get(args.command, {}).get("capability", "cli"),
            request={},
            data={},
            status=Status.ERROR,
            errors=(
                CapabilityError(
                    code=ErrorCode.INTERNAL_ERROR,
                    message="unexpected internal failure",
                    operation=args.command,
                    detail={"exception": exc.__class__.__name__},
                ),
            ),
            completeness=Completeness(complete=False, stop_reason="internal_error"),
        )
    serializer = V1Serializer(view=args.view)
    try:
        _emit(serializer.serialize(result), args.output)
    except (OSError, ValueError):
        result = _error_result(
            "could not write the requested output file",
            code=ErrorCode.INVALID_INPUT,
            operation=args.command,
        )
        _emit(serializer.serialize(result))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
