"""
ManaPool Price Updater
----------------------
Runs on a schedule. Calls the ManaPool bulk price API to update all singles
inventory using the market_low_percentage strategy.

Environment variables (set in Portainer):
  MANAPOOL_API_KEY     — your ManaPool API key
  MANAPOOL_EMAIL       — your ManaPool email
  PRICE_UPDATE_INTERVAL — seconds between updates (default: 3600 = 1 hour)
  PRICE_MODIFIER       — percentage modifier relative to market low (default: -1)
  MIN_CONFIDENCE       — minimum market confidence 0-1 (default: 0.5)
  ROUND_TO             — round to nearest cents value (default: 1)
"""

# pylint: disable=duplicate-code

import os
import time
from datetime import datetime

import requests

# ─── CONFIGURATION ────────────────────────────────────────────────────────────

API_KEY = os.environ.get("MANAPOOL_API_KEY", "")
API_EMAIL = os.environ.get("MANAPOOL_EMAIL", "")
API_BASE = "https://manapool.com/api/v1"

UPDATE_INTERVAL = int(os.environ.get("PRICE_UPDATE_INTERVAL", "14400"))
PRICE_MODIFIER = float(os.environ.get("PRICE_MODIFIER", "-1"))
MIN_CONFIDENCE = float(os.environ.get("MIN_CONFIDENCE", "0"))
ROUND_TO = int(os.environ.get("ROUND_TO", "1"))
MAX_REDUCTION_PCT = float(os.environ.get("MAX_REDUCTION_PCT", "15"))

# ─── HELPERS ──────────────────────────────────────────────────────────────────


def log(msg: str):
    """Print a timestamped log message."""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def api_headers() -> dict:
    """Return the authentication headers for ManaPool API requests."""
    return {
        "X-ManaPool-Email": API_EMAIL,
        "X-ManaPool-Access-Token": API_KEY,
        "Content-Type": "application/json",
    }


def build_payload() -> dict:
    """Build the bulk price request payload from the configured settings."""
    return {
        "filters": {
            "productFilters": {"productType": "mtg_single"},
            "inventoryFilters": {"minQuantity": 1},
        },
        "pricing": {
            "strategy": "market_low_percentage",
            "modifier": PRICE_MODIFIER,
            "threshold": MAX_REDUCTION_PCT,
        },
        "excludeLetterShippingDisabledSellers": False,
    }


def run_preview() -> bool:
    """Run a preview to show skip-reason breakdown before the real update.

    Returns True if the update should proceed, False if there's nothing to do.
    """
    payload = build_payload()
    try:
        resp = requests.post(
            f"{API_BASE}/inventory/bulk-price/preview",
            headers=api_headers(),
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        stats = data.get("statistics", {})
        counts = stats.get("counts", {})
        metrics = stats.get("metrics", {})

        log("  ── Preview ──")
        log(f"    Total items     : {counts.get('totalItems', '?')}")
        log(f"    Would update    : {counts.get('successfulItems', '?')}")
        log(f"    No change       : {counts.get('skippedNoChange', '?')}")
        log(f"    At minimum      : {counts.get('skippedAtMinimum', '?')}")
        log(f"    Below minimum   : {counts.get('skippedReducedToMinimum', '?')}")
        log(f"    No market data  : {counts.get('skippedNoMarketData', '?')}")
        log(f"    No confidence   : {counts.get('skippedNoConfidenceData', '?')}")
        log(f"    Low confidence  : {counts.get('skippedBelowMinConfidence', '?')}")
        log(f"    Zero price      : {counts.get('skippedZeroPrice', '?')}")
        log(f"    Exceeds thresh  : {counts.get('skippedExceedsThreshold', '?')}")

        avg_old = metrics.get("avgOldPriceCents")
        avg_new = metrics.get("avgNewPriceCents")
        value_change = metrics.get("totalValueChangeCents")
        log(
            f"    Avg old price   : {avg_old:.1f}¢"
            if avg_old
            else "    Avg old price   : ?"
        )
        log(
            f"    Avg new price   : {avg_new:.1f}¢"
            if avg_new
            else "    Avg new price   : ?"
        )
        log(
            f"    Value change    : {value_change}¢"
            if value_change is not None
            else "    Value change    : ?"
        )
        if data.get("warning"):
            log(f"    ⚠ {data['warning']}")

        successful = counts.get("successfulItems", 0)
        if successful == 0:
            log("  ⏭ Nothing to update")
            return False

        return True
    except requests.exceptions.RequestException as e:
        log(f"  ✗ Preview failed: {e}")
        return True


def update_prices() -> str | None:
    """Submit a bulk price update job. Returns the job ID or None."""
    payload = build_payload()

    try:
        resp = requests.post(
            f"{API_BASE}/inventory/bulk-price",
            headers=api_headers(),
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        job_id = data.get("jobId", "unknown")
        log(f"  ✓ Bulk price job created: {job_id}")
        log(f"    {data.get('message', '')}")
        return job_id
    except requests.exceptions.HTTPError as e:
        log(f"  ✗ API error: {e}")
        if e.response is not None:
            log(f"    {e.response.text[:500]}")
        return None
    except requests.exceptions.RequestException as e:
        log(f"  ✗ Failed: {e}")
        return None


def poll_job(job_id: str):
    """Poll the job until it completes or times out."""
    for _ in range(60):  # up to 5 minutes (60 × 5s)
        time.sleep(5)
        try:
            resp = requests.get(
                f"{API_BASE}/inventory/bulk-price/jobs/{job_id}",
                headers=api_headers(),
                timeout=15,
            )
            resp.raise_for_status()
            job = resp.json().get("job", {})
            status = job.get("status", "unknown")
            processed = job.get("processed_items", 0)
            total = job.get("total_items", "?")
            successful = job.get("successful_items", 0)
            skipped = job.get("skipped_items", 0)
            failed = job.get("failed_items", 0)

            if status in ("completed", "failed"):
                log(
                    f"  Job {status}: {successful} updated, "
                    f"{skipped} skipped, {failed} failed (of {total})"
                )
                return
            log(f"  Job {status}: {processed}/{total} processed...")
        except requests.exceptions.RequestException as e:
            log(f"  ✗ Poll error: {e}")

    log("  Job still running after 5 minutes — will check next cycle")


# ─── MAIN LOOP ────────────────────────────────────────────────────────────────


def main():
    """Run the price-update loop on the configured interval."""
    log("=" * 50)
    log("  ManaPool Price Updater")
    log("  Strategy : market_low_percentage")
    log(f"  Modifier : {PRICE_MODIFIER}%")
    log(f"  Max drop : {MAX_REDUCTION_PCT}%")
    log(f"  Interval : every {UPDATE_INTERVAL}s")
    log("=" * 50)

    while True:
        log("Starting price update...")
        should_proceed = run_preview()
        if not should_proceed:
            log(f"Next update in {UPDATE_INTERVAL}s")
            time.sleep(UPDATE_INTERVAL)
            continue
        job_id = update_prices()
        if job_id:
            poll_job(job_id)
        log(f"Next update in {UPDATE_INTERVAL}s")
        time.sleep(UPDATE_INTERVAL)


if __name__ == "__main__":
    main()
