"""
ManaPool Auto-Print (polling edition)
--------------------------------------
Runs in a loop. Every 60 seconds it asks the ManaPool API for new orders.
Any order it hasn't seen before gets printed as a packing slip via CUPS.
No webhooks, no open ports, no tunnel needed.

Environment variables (set in Portainer):
  MANAPOOL_API_KEY  — your ManaPool API key
  PRINTER_NAME      — CUPS printer name (find with: lpstat -a)
  POLL_INTERVAL     — seconds between checks (default: 60)
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

API_KEY = os.environ.get("MANAPOOL_API_KEY", "YOUR_API_KEY_HERE")
API_EMAIL = os.environ.get("MANAPOOL_EMAIL", "YOUR_EMAIL_HERE")
PRINTER_NAME = os.environ.get("PRINTER_NAME", "YOUR_PRINTER_NAME")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "3600"))
CUPS_SERVER = os.environ.get("CUPS_SERVER", "")  # e.g. cups:631 for container
SELLER_NAME = os.environ.get("SELLER_NAME", "Seller")

API_BASE = "https://manapool.com/api/v1"
SEEN_FILE = "/data/printed_orders.txt"  # persisted via Docker volume

# ─── HELPERS ──────────────────────────────────────────────────────────────────


def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def api_headers() -> dict:
    return {
        "X-ManaPool-Email": API_EMAIL,
        "X-ManaPool-Access-Token": API_KEY,
        "Content-Type": "application/json",
    }


def load_seen() -> set:
    """Load the set of already-printed order IDs from disk."""
    path = Path(SEEN_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return set(path.read_text().splitlines())
    return set()


def save_seen(seen: set):
    Path(SEEN_FILE).write_text("\n".join(seen))


def fetch_new_orders(seen: set) -> list:
    """Fetch recent orders from ManaPool, return only ones we haven't printed."""
    try:
        resp = requests.get(
            f"{API_BASE}/seller/orders",
            headers=api_headers(),
            params={"is_fulfilled": "false"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        orders = data.get("orders", data if isinstance(data, list) else [])
        if not orders and isinstance(data, dict):
            # Try common wrapper keys
            for key in ["data", "results", "order_list"]:
                if key in data and isinstance(data[key], list):
                    orders = data[key]
                    break
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
    # Unwrap if the response nests the order under a key
    if isinstance(data, dict):
        for key in ["order", "data", "result"]:
            if key in data and isinstance(data[key], dict):
                data = data[key]
                break
    return data


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
    product = item.get("product", {})
    single = product.get("single") or {}
    sealed = product.get("sealed") or {}
    info = single or sealed
    finish_id = info.get("finish_id", "")
    condition_id = info.get("condition_id", "")
    return {
        "quantity": item.get("quantity", 1),
        "name": info.get("name", "Item"),
        "set_code": info.get("set", ""),
        "condition": CONDITION_LABELS.get(condition_id, condition_id),
        "finish": FINISH_LABELS.get(finish_id, finish_id),
        "foil": finish_id == "FO",
        "language": info.get("language_id", "EN"),
        "collector_number": info.get("number", ""),
        "price": item.get("price_cents", 0) / 100,
    }


def render_packing_slip(order: dict) -> str:
    template_path = Path(__file__).parent / "packing_slip.html"
    template = Template(template_path.read_text())

    # Format date as M/D/YYYY
    created = order.get("created_at", "")
    try:
        d = datetime.fromisoformat(created.replace("Z", "+00:00"))
        order_date = f"{d.month}/{d.day}/{d.year}"
    except Exception:
        order_date = created[:10] if created else ""

    raw_items = order.get("items", [])
    items = [flatten_item(i) for i in raw_items]

    payment = order.get("payment", {})
    subtotal = payment.get("subtotal_cents", 0) / 100
    if not subtotal:
        subtotal = sum(i["price"] * i["quantity"] for i in items)
    shipping_total = payment.get("shipping_cents", 0) / 100
    total = payment.get("total_cents", order.get("total_cents", 0)) / 100

    shipping_address = order.get("shipping_address", {})

    # Normalise postal code — API may use any of these field names
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
        page = browser.new_page()
        page.set_content(html_content, wait_until="networkidle")
        page.pdf(
            path=pdf_path,
            format="Letter",
            margin={
                "top": "0.5in",
                "bottom": "0.5in",
                "left": "0.5in",
                "right": "0.5in",
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

    seen = load_seen()
    log(f"Loaded {len(seen)} previously printed order(s)")

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
                    html = render_packing_slip(order)
                    print_html(html)
                    log(f"  ✓ Printed order {order_id}")
                    seen.add(order_id)
                    save_seen(seen)
                except Exception as e:
                    log(f"  ✗ Failed to print order {order_id}: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
