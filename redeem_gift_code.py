#!/usr/bin/env python3
"""Redeem one Kingshot gift code for players listed in a CSV.

Uses the signed API request made by the official Gift Code Center. Every
response is saved to CSV, with an optional Webex completion summary.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


GIFT_CODE_API_URL = "https://kingshot-giftcode.centurygame.com/api/gift_code"
# Public client constant shipped by the official Gift Code Center.
CLIENT_KEY = "mN4!pQs6JrYwV9"
URL_SAFE_CHARS = "~()*!.'-"
REQUEST_TIMEOUT_SECONDS = 30
PLAYER_DELAY_SECONDS = 1
RATE_LIMIT_RETRY_DELAY_SECONDS = 1
REQUIRED_COLUMNS = {"player_id", "kingdom_id"}
LABEL_COLUMNS = ("label", "name", "comment")


@dataclass(frozen=True)
class Player:
    player_id: str
    kingdom_id: str
    label: str = ""


@dataclass(frozen=True)
class RedemptionResult:
    status: str
    response: str
    http_status: int | None = None
    api_code: object = None
    err_code: object = None
    message: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Submit a Kingshot gift code through the official Gift Code Center API."
    )
    parser.add_argument("csv_file", type=Path, help="CSV with player_id and kingdom_id columns")
    parser.add_argument("--code", help="Gift code; if omitted, the script prompts for it")
    parser.add_argument(
        "--results",
        type=Path,
        help="Results CSV path (default: gift_code_results_<timestamp>.csv)",
    )
    return parser.parse_args()


def read_players(csv_path: Path) -> tuple[list[Player], list[str]]:
    if not csv_path.is_file():
        raise ValueError(f"CSV file does not exist: {csv_path}")

    players: list[Player] = []
    warnings: list[str] = []
    seen: set[tuple[str, str]] = set()
    with csv_path.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        headers = {header.strip().lower() for header in (reader.fieldnames or []) if header}
        missing = REQUIRED_COLUMNS - headers
        if missing:
            raise ValueError(
                "CSV is missing required column(s): " + ", ".join(sorted(missing))
            )
        label_column = next((name for name in LABEL_COLUMNS if name in headers), None)
        for line_number, row in enumerate(reader, start=2):
            normalized = {
                key.strip().lower(): (value or "").strip()
                for key, value in row.items()
                if key is not None
            }
            player_id = normalized.get("player_id", "")
            kingdom_id = normalized.get("kingdom_id", "")
            label = normalized.get(label_column, "") if label_column else ""

            if not player_id and not kingdom_id and not label:
                continue
            if not player_id or not kingdom_id:
                warnings.append(f"Row {line_number}: missing player_id or kingdom_id; skipped")
                continue
            if not player_id.isdigit() or not kingdom_id.isdigit():
                warnings.append(f"Row {line_number}: IDs must contain digits only; skipped")
                continue

            key = (player_id, kingdom_id)
            if key in seen:
                warnings.append(
                    f"Row {line_number}: duplicate player/kingdom pair {player_id}/{kingdom_id}; skipped"
                )
                continue
            seen.add(key)
            players.append(Player(player_id, kingdom_id, label))

    if not players:
        raise ValueError("No valid player rows found in the CSV")
    return players, warnings


def signed_payload(player: Player, code: str) -> dict[str, str | int]:
    payload: dict[str, str | int] = {
        "fid": player.player_id,
        "kid": player.kingdom_id,
        "cdk": code,
        "time": int(time.time()),
    }
    canonical = "&".join(
        f"{key}={quote(str(payload[key]), safe=URL_SAFE_CHARS)}"
        for key in sorted(payload)
    )
    payload["sign"] = hashlib.md5((canonical + CLIENT_KEY).encode("utf-8")).hexdigest()
    return payload


def classify_outcome(response: object) -> str:
    """Use explicit API codes, never message fragments such as 'RECEIVED.'."""
    if not isinstance(response, dict):
        return "REVIEW"
    err_code = str(response.get("err_code", ""))
    if err_code == "40008":
        return "ALREADY_CLAIMED"
    if err_code in {"40014", "40007"}:
        return "INVALID_CODE"
    # Confirmed live response: code=0, msg=SUCCESS, err_code=20000.
    if err_code == "20000" and str(response.get("code", "")) == "0":
        return "SUCCESS"
    if err_code in {"40002", "40019", "40100"}:
        return "RATE_LIMITED"
    message = str(response.get("msg", "")).lower()
    if err_code in {"40102", "40103"} or any(
        marker in message for marker in ("captcha", "verification code")
    ):
        return "VERIFICATION_REQUIRED"
    if err_code in {
        "40001", "40003", "40005", "40006", "40009", "40011",
        "40012", "40017", "40020",
    }:
        return "REJECTED"
    return "REVIEW"


def redeem_player(player: Player, code: str) -> RedemptionResult:
    request = Request(
        GIFT_CODE_API_URL,
        data=urlencode(signed_payload(player, code)).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                http_status = response.status
                raw = response.read().decode("utf-8", errors="replace")
        except HTTPError as error:
            http_status = error.code
            try:
                raw = error.read().decode("utf-8", errors="replace")
            finally:
                error.close()
    except (URLError, TimeoutError, OSError) as error:
        # A lost response does not tell us whether redemption happened.
        return RedemptionResult("UNCERTAIN", f"Request failed: {error}; check in-game before retrying.")

    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    if http_status == 429:
        status = "RATE_LIMITED"
    elif not 200 <= http_status < 300:
        status = "UNCERTAIN"
    else:
        status = classify_outcome(body)
    fields = body if isinstance(body, dict) else {}
    return RedemptionResult(
        status, raw, http_status, fields.get("code"), fields.get("err_code"),
        str(fields.get("msg", "")),
    )


def default_results_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(f"gift_code_results_{stamp}.csv")


def write_result(writer: csv.DictWriter, output: TextIO, player: Player, code: str,
                 status: str, response: str, result: RedemptionResult | None = None) -> None:
    writer.writerow(
        {
            "player_id": player.player_id,
            "kingdom_id": player.kingdom_id,
            "label": player.label,
            "gift_code": code,
            "status": status,
            "response": response.strip(),
            "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "http_status": result.http_status if result else None,
            "api_code": result.api_code if result else None,
            "err_code": result.err_code if result else None,
            "message": result.message if result else "",
        }
    )
    output.flush()


def write_not_attempted(writer: csv.DictWriter, output: TextIO, players: list[Player],
                        code: str, reason: str) -> None:
    for player in players:
        write_result(writer, output, player, code, "NOT_ATTEMPTED", reason)


def send_webex_summary(results_path: Path) -> None:
    webhook_url = os.environ.get("WEBEX_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("Webex notification skipped: set WEBEX_WEBHOOK_URL to enable it.")
        return

    counts: dict[str, int] = {}
    with results_path.open("r", newline="", encoding="utf-8") as source:
        for row in csv.DictReader(source):
            status = row.get("status", "REVIEW")
            counts[status] = counts.get(status, 0) + 1

    total = sum(counts.values())
    succeeded = counts.get("SUCCESS", 0)
    already_claimed = counts.get("ALREADY_CLAIMED", 0)
    other_rejections = counts.get("REJECTED", 0) + counts.get("INVALID_CODE", 0)
    needs_review = counts.get("UNCERTAIN", 0) + counts.get("REVIEW", 0)
    not_submitted = counts.get("NOT_ATTEMPTED", 0)
    rate_limited = counts.get("RATE_LIMITED", 0)
    verification_required = counts.get("VERIFICATION_REQUIRED", 0)

    if total > 0 and already_claimed == total:
        emoji, outcome = "🟢", "Already claimed"
    elif total > 0 and succeeded + already_claimed == total:
        emoji, outcome = "🟢", "Succeeded"
    elif succeeded > 0:
        emoji, outcome = "🟠", "Partially succeeded"
    elif needs_review or verification_required or rate_limited:
        emoji, outcome = "🟠", "Needs attention"
    else:
        emoji, outcome = "🔴", "Unsuccessful"

    markdown = (
        f"# Kingshot Gift Code Run — {emoji} {outcome}\n\n"
        f"**Summary:** {succeeded} of {total} players succeeded.\n\n"
        f"- Already claimed: {already_claimed}\n"
        f"- Other rejections: {other_rejections}\n"
        f"- Needs review: {needs_review}\n"
        f"- Rate limited: {rate_limited}\n"
        f"- Verification required: {verification_required}\n"
        f"- Not submitted: {not_submitted}"
    )
    request = Request(
        webhook_url,
        data=json.dumps({"markdown": markdown}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=20) as response:
            if response.status not in (200, 201, 202, 204):
                print(f"Webex notification returned HTTP {response.status}.", file=sys.stderr)
                return
    except HTTPError as error:
        print(f"Webex notification failed with HTTP {error.code}.", file=sys.stderr)
        return
    except (URLError, TimeoutError, OSError) as error:
        print(f"Webex notification failed: {error}", file=sys.stderr)
        return

    print("Webex status update sent.")


def submit_players(players: list[Player], code: str, results_path: Path) -> int:
    results_path.parent.mkdir(parents=True, exist_ok=True)
    if results_path.exists():
        print(f"Results file already exists; choose another path: {results_path}", file=sys.stderr)
        return 2

    try:
        output_context = results_path.open("x", newline="", encoding="utf-8")
    except OSError as error:
        print(f"Could not create results file: {error}", file=sys.stderr)
        return 2

    with output_context as output:
        columns = [
            "player_id", "kingdom_id", "label", "gift_code", "status", "response", "recorded_at",
            "http_status", "api_code", "err_code", "message",
        ]
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        output.flush()

        next_index = 0
        active_player: Player | None = None
        active_result: RedemptionResult | None = None
        submit_may_have_started = False
        try:
            for index, player in enumerate(players):
                active_player = player
                active_result = None
                submit_may_have_started = False
                print(f"[{index + 1}/{len(players)}] {player.label or '(no label)'} — "
                      f"player {player.player_id}, kingdom {player.kingdom_id}")
                for attempt in range(2):
                    submit_may_have_started = True
                    result = redeem_player(player, code)
                    active_result = result
                    # An explicit rejection means the request is finished. This
                    # matters if interrupted while waiting for a rate-limit retry.
                    submit_may_have_started = result.status in {"UNCERTAIN", "REVIEW"}
                    if result.status != "RATE_LIMITED" or attempt == 1:
                        break
                    print("Rate limit reported. Waiting one second, then retrying this player once.")
                    time.sleep(RATE_LIMIT_RETRY_DELAY_SECONDS)

                write_result(writer, output, player, code, result.status, result.response, result)
                next_index = index + 1
                active_player = None
                active_result = None
                submit_may_have_started = False
                print(f"    {result.status}: {result.response[-300:].replace(chr(10), ' ')}")

                if result.status in {
                    "RATE_LIMITED", "INVALID_CODE", "VERIFICATION_REQUIRED", "UNCERTAIN", "REVIEW",
                }:
                    reason = f"Not submitted because the preceding request returned {result.status}."
                    print(f"Batch stopped: {result.status}. Remaining rows were not submitted.")
                    write_not_attempted(writer, output, players[next_index:], code, reason)
                    next_index = len(players)
                    break
                if next_index < len(players):
                    time.sleep(PLAYER_DELAY_SECONDS)
        except KeyboardInterrupt:
            print("\nStopped by user. Results for completed rows have been saved.")
            if active_player is not None and submit_may_have_started:
                write_result(
                    writer, output, active_player, code, "UNCERTAIN",
                    "Interrupted during submission; check in-game before retrying.",
                )
                next_index += 1
            elif active_player is not None and active_result is not None:
                write_result(
                    writer, output, active_player, code, active_result.status,
                    active_result.response, active_result,
                )
                next_index += 1
            write_not_attempted(
                writer, output, players[next_index:], code, "Not submitted; run was interrupted."
            )
        except Exception as error:
            print(f"\nStopped safely: {error}", file=sys.stderr)
            if active_player is not None and submit_may_have_started:
                write_result(
                    writer, output, active_player, code, "UNCERTAIN",
                    f"The run stopped during submission ({error}); check in-game before retrying.",
                )
                next_index += 1
            elif active_player is not None and active_result is not None:
                write_result(
                    writer, output, active_player, code, active_result.status,
                    active_result.response, active_result,
                )
                next_index += 1
            write_not_attempted(writer, output, players[next_index:], code, str(error))

    print(f"\nResults saved to: {results_path.resolve()}")
    send_webex_summary(results_path)
    return 0


def main() -> int:
    args = parse_args()
    code = (args.code or input("Gift code (case-sensitive): ")).strip()
    if not code:
        print("Gift code cannot be empty.", file=sys.stderr)
        return 2
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", code):
        print("Gift code contains unexpected characters; check it and try again.", file=sys.stderr)
        return 2

    try:
        players, warnings = read_players(args.csv_file)
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 2

    for warning in warnings:
        print(f"Warning: {warning}")

    print(f"\nGift code: {code}")
    print(f"Players to submit: {len(players)}")
    for player in players:
        print(f"  {player.player_id} / kingdom {player.kingdom_id} — {player.label or '(no label)'}")
    print("\nSubmitting to the official Gift Code Center API.")

    results_path = args.results or default_results_path()
    return submit_players(players, code, results_path)


if __name__ == "__main__":
    raise SystemExit(main())
