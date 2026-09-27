#!/usr/bin/env python3
"""Monitor kingshot.net for newly published gift codes.

The monitor is intentionally one-shot: schedule this script with cron or
launchd.  The future claim script is called once per new code as:

    python redeem_gift_code.py players.csv --code CODE

Only the standard library is required.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator, Sequence


DEFAULT_URL = "https://kingshot.net/gift-codes"
DEFAULT_USER_AGENT = "Kingshot-Gift-Code-Monitor/1.0"
DEFAULT_WEBEX_WEBHOOK = "https://webexapis.com/v1/webhooks/incoming/Y2lzY29zcGFyazovL3VzL1dFQkhPT0svMWE3MTFkMDktMWFmMC00MjIwLTllYzgtODk0ZTFmZGRiZWZh"
CODE_PATTERN = re.compile(r"[A-Z0-9][A-Z0-9_-]{5,31}\Z")


class GiftCodePageParser(HTMLParser):
    """Collect readable page text while preserving useful block boundaries."""

    BLOCK_TAGS = {"article", "br", "div", "h1", "h2", "h3", "h4", "li", "p", "section"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def lines(self) -> list[str]:
        text = html.unescape("".join(self.parts))
        return [line for line in (re.sub(r"\s+", " ", item).strip() for item in text.splitlines()) if line]


def normalize_code(value: str) -> str:
    return re.sub(r"\s+", "", html.unescape(value)).upper()


def extract_active_codes(document: str) -> list[str]:
    """Extract validated codes between the active and expired sections."""
    parser = GiftCodePageParser()
    parser.feed(document)
    lines = parser.lines()

    try:
        active_index = next(i for i, line in enumerate(lines) if line.casefold() == "active gift codes")
    except StopIteration as exc:
        raise ValueError("Could not locate the Active Gift Codes section") from exc

    expired_index = next(
        (
            i
            for i, line in enumerate(lines[active_index + 1 :], active_index + 1)
            if line.casefold().startswith("expired")
        ),
        None,
    )
    if expired_index is None:
        raise ValueError("Could not locate the expired-codes boundary")

    ignored = {"ACTIVE", "COPYCODE", "SIGNINTOREDEEM", "SHARELINK"}
    codes: set[str] = set()
    for line in lines[active_index + 1 : expired_index]:
        candidate = normalize_code(line)
        if candidate not in ignored and not candidate.startswith("COPYCODE") and CODE_PATTERN.fullmatch(candidate):
            codes.add(candidate)

    if not codes:
        raise ValueError("No active gift codes were extracted")
    return sorted(codes)


def load_state(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read state file {path}: {exc}") from exc
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RuntimeError(f"State file {path} must contain a JSON array of strings")
    return {normalize_code(item) for item in value}


def save_state(path: Path, codes: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(sorted({normalize_code(code) for code in codes}), indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


@contextmanager
def single_run_lock(path: Path) -> Iterator[None]:
    """Prevent overlapping runs on Unix-like systems."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - the target is macOS/Linux.
        yield
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Another monitor run is active ({path})") from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def fetch_page(url: str, timeout: float) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise RuntimeError(f"Kingshot page returned HTTP {response.status}")
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not fetch {url}: {exc}") from exc


@dataclass(frozen=True)
class ClaimResult:
    code: str
    returncode: int


def invoke_claim_script(
    claim_script: Path, players_file: Path, code: str, python_executable: str
) -> ClaimResult:
    command = [python_executable, str(claim_script), str(players_file), "--code", code]
    print(f"Claiming {code} with {claim_script}")
    completed = subprocess.run(command, check=False)
    print(f"Claim script exit status for {code}: {completed.returncode}")
    return ClaimResult(code, completed.returncode)


def send_webex_update(webhook_url: str, new_codes: Sequence[str], checked_at: str, timeout: float) -> None:
    code_list = ", ".join(f"`{code}`" for code in new_codes)
    plural = "s" if len(new_codes) != 1 else ""
    message = (
        f"**New Kingshot gift code{plural} detected:** {code_list}\n\n"
        f"The code{plural} appeared in the active list at [Kingshot.net]({DEFAULT_URL}).\n\n"
        f"Checked: {checked_at}"
    )
    payload = json.dumps({"markdown": message}).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(f"Webex webhook returned HTTP {response.status}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not send Webex update: {exc}") from exc
    print(f"Webex update sent for: {', '.join(new_codes)}")


def run(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser()
    lock_path = Path(args.lock_file).expanduser() if args.lock_file else state_path.with_suffix(state_path.suffix + ".lock")
    claim_script = Path(args.claim_script).expanduser() if args.claim_script else None

    with single_run_lock(lock_path):
        previous = load_state(state_path)
        current = set(extract_active_codes(fetch_page(args.url, args.timeout)))
        first_run = not previous
        new_codes = [] if first_run else sorted(current - previous)
        checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        if first_run:
            print(f"Baseline established with {len(current)} active code(s) at {checked_at}; no claims made.")
            save_state(state_path, current)
            return 0

        if not new_codes:
            print(f"No new active gift codes at {checked_at}.")
            save_state(state_path, current)
            return 0

        print(f"New active gift code(s): {', '.join(new_codes)}")
        results: list[ClaimResult] = []
        players_file: Path | None = None
        if claim_script:
            if not claim_script.is_file():
                raise RuntimeError(f"Claim script does not exist: {claim_script}")
            players_file = (
                Path(args.players_file).expanduser()
                if args.players_file
                else claim_script.parent / "players.csv"
            )
            if not players_file.is_file():
                raise RuntimeError(f"Players CSV does not exist: {players_file}")
        send_webex_update(args.webex_webhook, new_codes, checked_at, args.timeout)
        if claim_script:
            for code in new_codes:
                try:
                    result = invoke_claim_script(claim_script, players_file, code, args.python)
                except OSError as exc:
                    raise RuntimeError(f"Could not launch claim script for {code}: {exc}") from exc
                results.append(result)

        # Webex notification succeeded, so these codes should not generate a
        # duplicate notification on the next hourly run. Claiming is optional.
        save_state(state_path, current)

        return 0 if all(result.returncode == 0 for result in results) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claim-script", help="Optional path to redeem_gift_code.py")
    parser.add_argument(
        "--players-file",
        help="Player CSV passed to the claim script; defaults to players.csv beside that script",
    )
    parser.add_argument("--webex-webhook", default=DEFAULT_WEBEX_WEBHOOK, help="Webex incoming-webhook URL")
    parser.add_argument("--state-file", default="kingshot_gift_codes.json", help="JSON state file path")
    parser.add_argument("--lock-file", help="Lock-file path; defaults beside --state-file")
    parser.add_argument("--url", default=DEFAULT_URL, help="Gift-code page URL")
    parser.add_argument("--timeout", type=float, default=45.0, help="HTTP timeout in seconds")
    parser.add_argument("--python", default=sys.executable, help="Python executable for the claim script")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
