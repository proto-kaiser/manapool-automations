"""Render a packing slip preview with dummy data and open in browser."""

import webbrowser
from pathlib import Path

from jinja2 import Template

template = Template(Path("packing_slip.html").read_text(encoding="utf-8"))

html = template.render(
    order_id="000000-0000000",
    order_label="000000-0000000",
    order_date="1/1/2025",
    seller_name="YourStoreName",
    buyer={"name": "Jane Doe", "username": "janedoe99"},
    shipping_address={
        "line1": "123 Main St",
        "line2": "Apt 1",
        "city": "Anytown",
        "state": "CA",
        "zip": "90210",
        "country": "US",
    },
    items=[
        {
            "name": "Sazacap's Brew",
            "set_code": "BLB",
            "condition": "NM",
            "foil": False,
            "language": "EN",
            "collector_number": "151",
            "quantity": 1,
            "price": 0.15,
        },
        {
            "name": "Unexpected Windfall",
            "set_code": "PLST",
            "condition": "NM",
            "foil": False,
            "language": "EN",
            "collector_number": "AFR-164",
            "quantity": 1,
            "price": 2.88,
        },
        {
            "name": "Crackle with Power",
            "variant": "SHOWCASE",
            "set_code": "SOA",
            "condition": "NM",
            "foil": False,
            "language": "JA",
            "collector_number": "107",
            "quantity": 1,
            "price": 11.62,
        },
    ],
    subtotal=14.65,
    shipping_total=1.30,
    total=15.95,
    printed_at="2026-05-15 14:30:00",
)

out = Path("preview.html")
out.write_text(html, encoding="utf-8")
webbrowser.open(out.resolve().as_uri())
print(f"Preview saved to {out.resolve()}")
