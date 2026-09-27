# Kingshot Gift Code CSV Redeemer

This script submits one gift code for each row through the official [Gift Code Center](https://ks-giftcode.centurygame.com/). It uses the site's form so the page creates its own request fields, including its `sign` value. It does not call or reproduce the private API directly.

## CSV format

Required columns are `player_id` and `kingdom_id`. Add a `label`, `name`, or `comment` column to identify each player in the preview and results file. IDs should contain digits only.

```csv
player_id,kingdom_id,label
123456789,1234,Main account
987654321,5678,Friend account
```

An example file is included as `players.example.csv`.

## Run

Use the shared Python environment configured for this workspace:

```sh
python redeem_gift_code.py players.csv
```

The script prompts for the case-sensitive gift code, previews all valid rows, then submits automatically. You can also pass the code directly:

```sh
python redeem_gift_code.py players.csv --code YOURCODE
```

Playwright and its Chromium browser must be available. If Chromium is missing, install the browser once with:

```sh
python -m playwright install chromium
```

## Results and safeguards

- The browser stays visible and submits one player at a time after displaying the preview.
- The script waits up to 45 seconds for the page's JavaScript-rendered form to appear before submitting; if it does not load, the error includes the page title and visible text.
- Duplicate player/kingdom pairs and malformed rows are skipped with a warning.
- A verification challenge pauses the run so you can complete it in the browser.
- A timeout is recorded as `UNCERTAIN`; the script never automatically retries it.
- If the site reports rate limiting, the remaining players are left unsubmitted.
- If the site rejects the gift code itself (for example, “Gift Code not found! Please check the capitalization.”), the batch stops and remaining players are marked `NOT_ATTEMPTED`.
- After player-specific responses, the script closes the site's response dialog before continuing. If it cannot close the dialog, it stops before submitting the next player.
- Results are written as `gift_code_results_<timestamp>.csv` by default. Use `--results path.csv` to choose another location.
- If the form no longer has exactly three visible inputs or its Confirm control cannot be found, the script stops before submitting.

## Optional gift-code monitor

The adjacent `kingshot_gift_code_monitor.py` checks the active-code list hourly when scheduled with cron or launchd. On a new code it sends a Webex update. Claiming is optional; when enabled, the monitor invokes this redeemer once per code using the adjacent `players.csv`:

```sh
python kingshot_gift_code_monitor.py --claim-script redeem_gift_code.py
```

The resulting claim command is:

```sh
python redeem_gift_code.py players.csv --code NEWCODE
```

Run without `--claim-script` to receive Webex notifications without automatically redeeming codes. Use `--players-file path/to/players.csv` to select a different player list.
