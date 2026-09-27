#!/usr/bin/env python3
"""Redeem one Kingshot gift code for players listed in a CSV.

The official web form is used so its own JavaScript creates any request fields
required by the site. This script does not call the site's private API.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO

from playwright.sync_api import Browser, Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


GIFT_CENTER_URL = "https://ks-giftcode.centurygame.com/"
REQUIRED_COLUMNS = {"player_id", "kingdom_id"}
LABEL_COLUMNS = ("label", "name", "comment")
CHALLENGE_MARKERS = (
    "captcha",
    "verify you are human",
    "security verification",
    "verification required",
)
RATE_LIMIT_MARKERS = ("too frequent", "rate limit", "try again later")
INVALID_CODE_MARKERS = (
    "gift code not found",
    "code not found",
    "please check the capitalization",
    "code has expired",
    "gift code expired",
)
SUCCESS_MARKERS = ("success", "redeemed", "received", "claim successful")
FAILURE_MARKERS = (
    "invalid",
    "not found",
    "already used",
    "already claimed",
    "expired",
    "failed",
    "error",
)


@dataclass(frozen=True)
class Player:
    player_id: str
    kingdom_id: str
    label: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Submit a Kingshot gift code through the official Gift Code Center."
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


def visible_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=5_000)
    except PlaywrightTimeoutError:
        return ""


def has_marker(text: str, markers: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in markers)


def input_fields(page: Page, timeout_seconds: int = 45):
    """Wait for the JavaScript-rendered form, then return its three text inputs."""
    fields = page.locator("input:visible")
    deadline = time.monotonic() + timeout_seconds
    count = fields.count()
    while count != 3 and count < 3 and time.monotonic() < deadline:
        page.wait_for_timeout(250)
        count = fields.count()

    if count != 3:
        try:
            title = page.title()
        except Exception:
            title = "(unavailable)"
        excerpt = re.sub(r"\s+", " ", visible_text(page)).strip()[:240]
        raise RuntimeError(
            f"The Gift Code Center did not show its three form fields within {timeout_seconds} seconds "
            f"(found {count}; page title: {title!r}; page text: {excerpt!r}). "
            "The site may still be loading or may have changed; stopping before submission."
        )
    return fields


def click_confirm(page: Page) -> None:
    buttons = page.get_by_role("button", name="Confirm", exact=True).all()
    visible_buttons = [button for button in buttons if button.is_visible()]
    if visible_buttons:
        visible_buttons[-1].click(timeout=10_000)
        return

    text_controls = page.get_by_text("Confirm", exact=True).all()
    visible_controls = [control for control in text_controls if control.is_visible()]
    if not visible_controls:
        raise RuntimeError("Could not identify the form's Confirm control; stopping.")
    # The site renders response dialogs after a submission. Their Confirm
    # control is appended after the form's control, so choose the last visible
    # one when dismissing the dialog.
    visible_controls[-1].click(timeout=10_000)


def dismiss_result_dialog(page: Page) -> None:
    controls = [
        control
        for control in page.get_by_text("Confirm", exact=True).all()
        if control.is_visible()
    ]
    if len(controls) <= 1:
        return

    previous_count = len(controls)
    controls[-1].click(timeout=10_000)
    page.wait_for_timeout(400)
    remaining = [
        control
        for control in page.get_by_text("Confirm", exact=True).all()
        if control.is_visible()
    ]
    if len(remaining) >= previous_count:
        raise RuntimeError(
            "The site's response dialog did not close; stopping before the next player."
        )


def wait_for_outcome(page: Page, before: str, timeout_seconds: int = 45) -> tuple[str, str]:
    """Wait for visible response text, pausing for any human verification."""
    deadline = time.monotonic() + timeout_seconds
    last_text = before
    changed_at: float | None = None

    while time.monotonic() < deadline:
        current = visible_text(page)
        if has_marker(current, CHALLENGE_MARKERS):
            print("\nA verification challenge is displayed in the browser.")
            input("Complete it in the browser, then press Enter here to continue waiting: ")
            # The visible result might be unchanged until the challenge is completed.
            last_text = visible_text(page)
            changed_at = None
            continue

        if current != before:
            if current != last_text:
                last_text = current
                changed_at = time.monotonic()
            elif changed_at is not None and time.monotonic() - changed_at >= 0.8:
                return "", current

        time.sleep(0.25)

    if last_text != before:
        return "", last_text
    return "UNCERTAIN", "No clear response appeared before the 45-second timeout. Do not retry automatically."


def classify_outcome(response: str) -> str:
    if has_marker(response, RATE_LIMIT_MARKERS):
        return "RATE_LIMITED"
    if has_marker(response, INVALID_CODE_MARKERS):
        return "INVALID_CODE"
    if has_marker(response, SUCCESS_MARKERS):
        return "SUCCESS"
    if has_marker(response, FAILURE_MARKERS):
        return "REJECTED"
    return "REVIEW"


def default_results_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(f"gift_code_results_{stamp}.csv")


def write_result(writer: csv.DictWriter, output: TextIO, player: Player, code: str,
                 status: str, response: str) -> None:
    writer.writerow(
        {
            "player_id": player.player_id,
            "kingdom_id": player.kingdom_id,
            "label": player.label,
            "gift_code": code,
            "status": status,
            "response": response.strip(),
            "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    )
    output.flush()


def write_not_attempted(writer: csv.DictWriter, output: TextIO, players: list[Player],
                        code: str, reason: str) -> None:
    for player in players:
        write_result(writer, output, player, code, "NOT_ATTEMPTED", reason)


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
            "player_id", "kingdom_id", "label", "gift_code", "status", "response", "recorded_at"
        ]
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        output.flush()

        with sync_playwright() as playwright:
            browser: Browser | None = None
            next_index = 0
            active_player: Player | None = None
            submit_may_have_started = False
            try:
                browser = playwright.chromium.launch(headless=False)
                page = browser.new_page()
                page.set_default_timeout(15_000)
                page.goto(GIFT_CENTER_URL, wait_until="domcontentloaded", timeout=45_000)
                input_fields(page)
                print("\nThe official Gift Code Center is open in the browser.")
                print("If the page asks you to complete a human check, do it in the browser.")

                for index, player in enumerate(players):
                    active_player = player
                    submit_may_have_started = False
                    print(f"[{index + 1}/{len(players)}] {player.label or '(no label)'} — "
                          f"player {player.player_id}, kingdom {player.kingdom_id}")
                    fields = input_fields(page)
                    fields.nth(0).fill(player.player_id)
                    fields.nth(1).fill(player.kingdom_id)
                    fields.nth(2).fill(code)
                    before = visible_text(page)
                    # If Playwright is interrupted during the click, the server may
                    # already have received it; record uncertainty and never retry.
                    submit_may_have_started = True
                    click_confirm(page)
                    status, response = wait_for_outcome(page, before)
                    if status != "UNCERTAIN":
                        status = classify_outcome(response)

                    write_result(writer, output, player, code, status, response)
                    next_index = index + 1
                    active_player = None
                    submit_may_have_started = False
                    print(f"    {status}: {response[-300:].replace(chr(10), ' ')}")

                    if status == "RATE_LIMITED":
                        print("Rate limiting was reported. Remaining rows were not submitted.")
                        write_not_attempted(
                            writer, output, players[next_index:], code,
                            "Not submitted because the site reported rate limiting.",
                        )
                        next_index = len(players)
                        break

                    if status == "INVALID_CODE":
                        print("The gift code was rejected. Remaining rows were not submitted.")
                        write_not_attempted(
                            writer, output, players[next_index:], code,
                            "Not submitted because the gift code was rejected.",
                        )
                        next_index = len(players)
                        break

                    if next_index < len(players):
                        dismiss_result_dialog(page)
                        # Let the page finish any result transition before the next row.
                        page.wait_for_timeout(1_000)
            except KeyboardInterrupt:
                print("\nStopped by user. Results for completed rows have been saved.")
                if active_player is not None and submit_may_have_started:
                    write_result(
                        writer, output, active_player, code, "UNCERTAIN",
                        "Interrupted during submission; check in-game before retrying.",
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
                write_not_attempted(writer, output, players[next_index:], code, str(error))
            finally:
                if browser is not None:
                    browser.close()

    print(f"\nResults saved to: {results_path.resolve()}")
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
    print("\nSubmitting to the official Gift Code Center.")

    results_path = args.results or default_results_path()
    return submit_players(players, code, results_path)


if __name__ == "__main__":
    raise SystemExit(main())
