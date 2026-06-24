"""
ManaPool Auto-Print (polling edition)
--------------------------------------
Runs in a loop. Every POLL_INTERVAL seconds it asks the ManaPool API for
new orders that have no fulfillment yet. After printing, it creates a
fulfillment record via the API — no local file needed to track what's
been printed.

Environment variables (set in Portainer):
  MANAPOOL_API_KEY  — your ManaPool API key
  MANAPOOL_EMAIL    — your ManaPool account email
  PRINTER_NAME      — CUPS printer name (find with: lpstat -a)
  POLL_INTERVAL     — seconds between checks (default: 3600)
  CUPS_SERVER       — optional CUPS host:port for container networking
  SELLER_NAME       — your store name (shown on packing slip)
"""

import os
import time
import tempfile
import subprocess
from datetime import datetime
from pathlib import Path

import requests
from jinja2 import Template
from playwright.sync_api import sync_playwright

# ─── CONFIGURATION ────────────────────────────────────────────────────────────

API_KEY       = os.environ.get("MANAPOOL_API_KEY", "YOUR_API_KEY_HERE")
API_EMAIL     = os.environ.get("MANAPOOL_EMAIL", "YOUR_EMAIL_HERE")
PRINTER_NAME  = os.environ.get("PRINTER_NAME", "YOUR_PRINTER_NAME")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "3600"))
CUPS_SERVER   = os.environ.get("CUPS_SERVER", "")
SELLER_NAME   = os.environ.get("SELLER_NAME", "Seller")

API_BASE = "https://manapool.com/api/v1"

# ─── HELPERS ──────────────────────────────────────────────────────────────────


def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def api_headers() -> dict:
    return {
        "X-ManaPool-Email": API_EMAIL,
        "X-ManaPool-Access-Token": API_KEY,
        "Content-Type": "application/json",
    }


def fetch_new_orders(seen: set) -> list:
    """Fetch unfulfilled orders with no fulfillment record yet."""
    try:
        resp = requests.get(
            f"{API_BASE}/seller/orders",
            headers=api_headers(),
            params={"is_fulfilled": "false", "has_fulfillments": "false"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        orders = data.get("orders", data if isinstance(data, list) else [])
        if not orders and isinstance(data, dict):
            for key in ["data", "results", "order_list"]:
                if key in data and isinstance(data[key], list):
                    orders = data[key]
                    break
        # In-memory fallback: skip anything already handled this session
        return [o for o in orders if str(o.get("id")) not in seen]
    except Exception as e:
        log(f"✗ Failed to fetch orders: {e}")
        return []


def fetch_order_detail(order_id: str) -> dict:
    resp = requests.get(
        f"{API_BASE}/seller/orders/{order_id}", headers=api_headers(), timeout=15
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        for key in ["order", "data", "result"]:
            if key in data and isinstance(data[key], dict):
                data = data[key]
                break
    return data


def update_order_status(order_id: str, status: str = "processing"):
    """Create/update fulfillment via PUT /seller/orders/{id}/fulfillment."""
    resp = requests.put(
        f"{API_BASE}/seller/orders/{order_id}/fulfillment",
        headers=api_headers(),
        json={"fulfillment": {"status": status}},
        timeout=15,
    )
    if resp.status_code == 409:
        log(f"  ⚠ Order {order_id} fulfillment already exists — skipping status update")
        return
    resp.raise_for_status()
    log(f"  ✓ Order {order_id} marked as {status}")


FINISH_LABELS = {"NF": "Non-Foil", "FO": "Foil", "EF": "Etched Foil"}
CONDITION_LABELS = {
    "NM": "Near Mint",
    "LP": "Lightly Played",
    "MP": "Moderately Played",
    "HP": "Heavily Played",
    "DMG": "Damaged",
}


def flatten_item(item: dict) -> dict:
    """Flatten nested API item into the structure the packing slip template expects."""
    product      = item.get("product", {})
    single       = product.get("single") or {}
    sealed       = product.get("sealed") or {}
    info         = single or sealed
    finish_id    = info.get("finish_id", "")
    condition_id = info.get("condition_id", "")
    return {
        "quantity":         item.get("quantity", 1),
        "name":             info.get("name", "Item"),
        "set_code":         info.get("set", ""),
        "condition":        CONDITION_LABELS.get(condition_id, condition_id),
        "finish":           FINISH_LABELS.get(finish_id, finish_id),
        "foil":             finish_id == "FO",
        "language":         info.get("language_id", "EN"),
        "collector_number": info.get("number", ""),
        "price":            item.get("price_cents", 0) / 100,
    }


def render_packing_slip(order: dict) -> str:
    template_path = Path(__file__).parent / "packing_slip.html"
    template = Template(template_path.read_text())

    created = order.get("created_at", "")
    try:
        d = datetime.fromisoformat(created.replace("Z", "+00:00"))
        order_date = f"{d.month}/{d.day}/{d.year}"
    except Exception:
        order_date = created[:10] if created else ""

    raw_items = order.get("items", [])
    items     = [flatten_item(i) for i in raw_items]

    payment        = order.get("payment", {})
    subtotal       = payment.get("subtotal_cents", 0) / 100
    if not subtotal:
        subtotal   = sum(i["price"] * i["quantity"] for i in items)
    shipping_total = payment.get("shipping_cents", 0) / 100
    total          = payment.get("total_cents", order.get("total_cents", 0)) / 100

    shipping_address = order.get("shipping_address", {})
    if "postal_code" not in shipping_address:
        for key in ("zip", "zip_code", "zipcode", "postcode", "postalCode"):
            if key in shipping_address:
                shipping_address["postal_code"] = shipping_address[key]
                break

    return template.render(
        order_id=order.get("id", "N/A"),
        order_label=order.get("label", order.get("id", "N/A")),
        order_date=order_date,
        seller_name=SELLER_NAME,
        buyer={"name": shipping_address.get("name", "")},
        shipping_address=shipping_address,
        items=items,
        subtotal=subtotal,
        shipping_total=shipping_total,
        total=total,
        printed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


def print_html(html_content: str):
    """Render HTML → PDF via Playwright, send to CUPS printer."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False, dir="/tmp") as tmp:
        pdf_path = tmp.name

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page    = browser.new_page()
        page.set_content(html_content, wait_until="networkidle")
        page.pdf(
            path=pdf_path,
            format="Letter",
            margin={
                "top":    "0.5in",
                "bottom": "0.5in",
                "left":   "0.5in",
                "right":  "0.5in",
            },
            print_background=True,
        )
        browser.close()

    lp_cmd = ["lp", "-d", PRINTER_NAME]
    if CUPS_SERVER:
        lp_cmd += ["-h", CUPS_SERVER]
    lp_cmd.append(pdf_path)

    result = subprocess.run(lp_cmd, capture_output=True, text=True)
    os.unlink(pdf_path)

    if result.returncode != 0:
        raise RuntimeError(f"lp failed: {result.stderr.strip()}")


# ─── MAIN LOOP ────────────────────────────────────────────────────────────────


def main():
    log("=" * 50)
    log("  ManaPool Auto-Print — polling mode")
    log(f"  Printer : {PRINTER_NAME}")
    log(f"  Seller  : {SELLER_NAME!r}")
    log(f"  Interval: every {POLL_INTERVAL}s")
    log("=" * 50)

    # In-memory set — guards against double-prints within a single session
    # if a fulfillment update fails. Resets on container restart (intentional —
    # the API is now the source of truth via has_fulfillments=false).
    seen: set = set()

    while True:
        log("Checking for new orders...")
        new_orders = fetch_new_orders(seen)

        if not new_orders:
            log("No new orders.")
        else:
            log(f"Found {len(new_orders)} new order(s)!")
            for order_summary in new_orders:
                order_id = str(order_summary.get("id"))
                try:
                    log(f"  Fetching detail for order {order_id}...")
                    order = fetch_order_detail(order_id)
                    html  = render_packing_slip(order)
                    print_html(html)
                    log(f"  ✓ Printed order {order_id}")
                    seen.add(order_id)
                    update_order_status(order_id, "processing")
                except Exception as e:
                    log(f"  ✗ Failed on order {order_id}: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
