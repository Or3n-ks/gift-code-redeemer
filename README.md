# Kingshot Gift Code CSV Redeemer

Redeem a Kingshot gift code for players in a CSV using the backend called by the official [Gift Code Center](https://ks-giftcode.centurygame.com/). Each run saves per-player results to CSV and can send a Webex completion summary.

The redemption call is `POST https://kingshot-giftcode.centurygame.com/api/gift_code`. Requests contain `fid` (player ID), `kid` (kingdom), `cdk` (gift code), a Unix timestamp in seconds, and a signature matching the website's client. This is an undocumented website API and can change. This project is independent of Century Games.

## Requirements

Python 3.10 or newer. Both scripts use only the Python standard library. No packages, browser, API key, or player password are required. `requirements.txt` is retained for tooling compatibility and contains no dependencies.

## Player CSV

Copy `players.example.csv` to your own local roster, such as `players.csv`, and replace the illustrative rows with the accounts you intend to process:

```csv
player_id,kingdom_id,label
123456789,1234,Example player A
987654321,5678,Example player B
```

`player_id` and `kingdom_id` are required and must contain digits only. An optional `label`, `name`, or `comment` identifies players in the preview and report. Duplicate player/kingdom pairs and malformed rows are skipped with a warning.

## Run

From this directory:

```sh
python3 redeem_gift_code.py players.csv --code YOURCODE
```

Omit `--code` to be prompted. Codes are case-sensitive. After displaying the roster, the script submits automatically, one player at a time. To choose a report location:

```sh
python3 redeem_gift_code.py players.csv --code YOURCODE --results results/run.csv
```

The default output is `gift_code_results_<timestamp>.csv` in the current directory. Existing output files are never overwritten.

## Optional Webex notifications

Set your own incoming webhook URL in the environment before starting either script:

```sh
export WEBEX_WEBHOOK_URL='YOUR_WEBEX_INCOMING_WEBHOOK_URL'
```

Without this setting, both scripts run normally and skip Webex notifications. No webhook URL is bundled. The scripts read the process environment; they do not load `.env` files automatically. Scheduled runs must receive this environment setting too.

The redeemer's completion message counts new successes, already-claimed codes, other rejections, responses needing review, rate limits, verification requests, and rows not submitted. It omits player IDs, labels, and the gift code. Notification failures are reported without discarding the CSV.

## Results and response handling

The report columns are `player_id`, `kingdom_id`, `label`, `gift_code`, `status`, `response`, `recorded_at`, `http_status`, `api_code`, `err_code`, and `message`. `response` retains the raw response body. Rows are flushed after each player.

| API response | Report status | Batch behavior |
| --- | --- | --- |
| `code=0`, `err_code=20000`, usually `msg=SUCCESS` | `SUCCESS` | Continue |
| `err_code=40008`, usually `msg=RECEIVED.` | `ALREADY_CLAIMED` | Continue; count separately from new claims |
| `err_code=40014`, usually `msg=CDK NOT FOUND.` | `INVALID_CODE` | Stop |
| `err_code=40007`, usually `msg=TIME ERROR.` | `INVALID_CODE` | Stop; outside the code's redemption period |
| Known account-specific rejection | `REJECTED` | Continue |
| Explicit API rate limit or HTTP 429 | `RATE_LIMITED` | Retry once after one second; stop if it persists |
| CAPTCHA or verification request | `VERIFICATION_REQUIRED` | Stop |
| Unknown or malformed response | `REVIEW` | Stop |
| Timeout, connection failure, or other HTTP error | `UNCERTAIN` | Stop without retrying |

There is a one-second pause between players and a 30-second request timeout. Rate-limit retries generate a fresh timestamp and signature. Message text alone never establishes success. Verification is not automated.

When a batch stops or is interrupted, remaining rows are recorded as `NOT_ATTEMPTED`. A request interrupted while in flight is `UNCERTAIN`: check in-game before retrying because the server may already have processed it.

## Optional gift-code monitor

The monitor checks the community-maintained [Kingshot.net gift-code list](https://kingshot.net/gift-codes). It runs once per invocation; schedule repeated checks with your preferred scheduler.

```sh
python3 kingshot_gift_code_monitor.py
```

The first run establishes a baseline and makes no claims or notifications. Later runs report newly listed codes and optionally notify Webex. To redeem new codes for a local roster too:

```sh
python3 kingshot_gift_code_monitor.py --claim-script redeem_gift_code.py --players-file players.csv
```

The monitor invokes the redeemer once per new code. Without `--players-file`, it uses `players.csv` beside the claim script. Use `--state-file path.json` to choose where the locally generated baseline is saved. With Webex configured, a failed new-code notification stops processing before claims and leaves the baseline unchanged for a later retry.

The monitor depends on the source page's active/expired section format and stops if those boundaries cannot be found. View all options with `--help`.

## Keeping local data private

Only illustrative player rows are included. The `.gitignore` excludes CSVs other than `players.example.csv`, generated JSON state, locks, logs, local environment files, and `gift_code_config.py`. Keep real player data, output reports, webhook URLs, and local paths out of source files and examples. Console output includes the roster and response details, so treat captured logs as private too.

Ignore rules do not remove files or credentials already committed to Git history. Review staged changes before publishing, and replace any webhook that was previously committed. Use environment settings for credentials.

The signing constant in the redeemer is distributed by the official website's public JavaScript; it is not a personal credential.

## Tests

```sh
python3 -m unittest discover -p 'test_*.py' -v
```

Tests use synthetic player data and simulated API/Webex responses. They make no live redemptions or send messages.
