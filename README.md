# ManaPool Automations — Portainer Setup Guide

This stack runs three services on your TrueNAS server via Portainer:

- **manapool-cups** — Network print server (CUPS) that receives print jobs from the
  printer container and sends them to your physical printer.
- **manapool-printer** — Polls the ManaPool API for new orders and prints a
  packing slip for each one via CUPS.
- **manapool-pricer** — Reprices all singles in your inventory every 4 hours
  using the market low percentage strategy.

No webhooks, no open ports, no tunnel — the automation services only make
outbound API calls.

---

## Files — copy all to your server

Put these in a directory on your server (e.g. a TrueNAS configs dataset).
Update the absolute paths in `docker-compose.yml` to match your chosen location:

```
/mnt/tank/configs/manapool-automations/
  ├── Dockerfile            # Image for manapool-printer (Python + Playwright + CUPS)
  ├── Dockerfile.pricer     # Image for manapool-pricer  (Python + requests)
  ├── docker-compose.yml
  ├── cupsd.conf            # Custom CUPS config (allows unauthenticated printing)
  ├── requirements.txt
  ├── print_server.py
  ├── packing_slip.html
  └── price_updater.py
```

---

## Step 1 — Build the images

SSH into your server and build both images:

```bash
cd /mnt/tank/configs/manapool-automations
sudo docker build -t manapool-printer .
sudo docker build -t manapool-pricer -f Dockerfile.pricer .
```

---

## Step 2 — Deploy the stack in Portainer

1. Log into Portainer
2. Select your Docker environment
3. Go to **Stacks → Add stack**
4. Name it `manapool-automations`
5. Select **Web editor**
6. Paste the contents of `docker-compose.yml` into the editor
7. Scroll down to **Environment variables** and add:

| Variable                | Value                                   |
| ----------------------- | --------------------------------------- |
| `MANAPOOL_API_KEY`      | Your ManaPool API access token          |
| `MANAPOOL_EMAIL`        | Your ManaPool account email             |
| `PRINTER_NAME`          | Your CUPS printer name                  |
| `POLL_INTERVAL`         | `3600` (seconds between order checks)   |
| `SELLER_NAME`           | Your store name (shown on packing slip) |
| `PRICE_UPDATE_INTERVAL` | `14400` (seconds between reprices)      |
| `PRICE_MODIFIER`        | `-1` (% relative to market low)         |
| `MIN_CONFIDENCE`        | `0.5` (0-1, minimum market confidence)  |
| `MAX_REDUCTION_PCT`     | `15` (max % drop per card for pricer)   |
| `TZ`                    | `America/New_York` (container timezone) |

8. Click **Deploy the stack**

---

## Step 3 — Add your network printer to the CUPS container

Once the stack is running, open the CUPS web interface at:

```
http://YOUR-SERVER-IP:631
```

The custom `cupsd.conf` disables authentication for printing and disables HTTPS
redirects, so the web UI should be accessible without SSL.

**Add the printer via CLI (recommended):**

```bash
docker exec manapool-cups lpadmin -p <printer_name> -E -v socket://PRINTER-IP:9100 -m raw
docker exec manapool-cups lpoptions -d <printer_name>
```

Replace `PRINTER-IP` with your printer's IP and `<printer_name>` with your
desired printer name (must match the `PRINTER_NAME` env var).

**Or via the web UI:**

1. Go to **Administration → Add Printer**
2. Select **AppSocket/HP JetDirect** and enter `socket://PRINTER-IP:9100`
3. Name it to match your `PRINTER_NAME` env var

> **Note:** If CUPS sub-pages don't load, the `cups_config` volume may have
> stale data. Delete the volume in Portainer and redeploy the stack.

---

## Step 4 — Verify it's working

### Printer container

In Portainer, open the **manapool-printer** container logs.
You should see it checking for orders every 3600 seconds:

```
[2026-05-15 10:00:00] ManaPool Auto-Print — polling mode
[2026-05-15 10:00:00]   Printer : <printer_name>
[2026-05-15 10:00:00]   Interval: every 3600s
[2026-05-15 10:00:00] Loaded 0 previously printed order(s)
[2026-05-15 10:00:00] Checking for new orders...
[2026-05-15 10:00:01] No new orders.
```

When an order comes in:

```
[2026-05-15 10:01:00] Checking for new orders...
[2026-05-15 10:01:00] Found 1 new order(s)!
[2026-05-15 10:01:00]   Fetching detail for order abc-123...
[2026-05-15 10:01:02]   ✓ Printed order abc-123
```

### Pricer container

Open the **manapool-pricer** container logs:

```
[2026-05-15 17:28:50] ==================================================
[2026-05-15 17:28:50]   ManaPool Price Updater
[2026-05-15 17:28:50]   Strategy : market_low_percentage
[2026-05-15 17:28:50]   Modifier : -1.0%
[2026-05-15 17:28:50]   Interval : every 14400s
[2026-05-15 17:28:50] ==================================================
[2026-05-15 17:28:50] Starting price update...
[2026-05-15 17:28:50]   ── Preview ──
[2026-05-15 17:28:50]     Total items     : 1939
[2026-05-15 17:28:50]     Would update    : 8
...
[2026-05-15 17:28:56]   Job completed: 8 updated, 1931 skipped, 0 failed
```

---

## Updating the app

If you edit any Python or HTML files:

1. SCP the changed file(s) to `/mnt/tank/configs/manapool-automations/` on your server
2. SSH in and rebuild the affected image:
   ```bash
   # For print_server.py or packing_slip.html changes:
   sudo docker build -t manapool-printer .
   # For price_updater.py changes:
   sudo docker build -t manapool-pricer -f Dockerfile.pricer .
   ```
3. In Portainer: open the stack → click **Update the stack**
   - Leave **Re-pull image** **unchecked** (these are local images)

---

## Troubleshooting

| Symptom                                         | Fix                                                                                                   |
| ----------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| Container keeps restarting                      | Check logs in Portainer for Python errors                                                             |
| "lp: Unauthorized" in logs                      | Delete the `cups_config` volume and redeploy — the custom `cupsd.conf` needs a fresh volume           |
| "lp failed" in logs                             | Run `docker exec manapool-cups lpstat -a` — confirm printer name matches exactly                      |
| CUPS web UI sub-pages don't load                | Delete `cups_config` volume and redeploy; ensure `cupsd.conf` has `DefaultEncryption Never`           |
| Orders not being detected                       | Check API key and email are correct in Portainer env vars                                             |
| Already-printed orders reprinting after restart | The `/data` volume persists the seen list — check the bind mount exists                               |
| Failed orders not retrying                      | They will retry on the next poll cycle automatically                                                  |
| Pricer skipping all items                       | Most cheap singles lack market confidence data — only cards with market data will be repriced         |
| Portainer 500 error on stack update             | Make sure **Re-pull image** is unchecked — these are locally-built images; remove `build:` directives |
