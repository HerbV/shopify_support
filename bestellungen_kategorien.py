#!/usr/bin/env python3
"""Teilt Shopify-Bestellungen in drei Kategorien auf:

  1. Königinnen-Bestellungen   (Produkttitel enthält "Königin")
  2. Umlarvaktionen            (Produkttitel enthält "Umlarvaktion")
  3. Belegstellen-Auffahrten   (Produkttitel enthält "Auffahrt")

Eine Bestellung kann in mehreren Kategorien auftauchen, wenn sie
entsprechende Positionen enthält.
"""
import re
import sys
import csv

import read_orders as shop

CREATED_MIN = "2026-01-01T00:00:00+02:00"

CATEGORIES = [
    ("Königinnen",            "königin"),
    ("Umlarvaktionen",        "umlarvaktion"),
    ("Belegstellen-Auffahrt", "auffahrt"),
]


def fetch_all_orders():
    """Lädt alle Bestellungen ab CREATED_MIN (mit Paginierung)."""
    import requests
    tok = shop.get_token()
    url = f"https://{shop.SHOP}.myshopify.com/admin/api/{shop.API_VERSION}/orders.json"
    params = {"status": "any", "limit": 250, "created_at_min": CREATED_MIN}
    orders = []
    while url:
        resp = requests.get(url, headers={"X-Shopify-Access-Token": tok},
                            params=params, timeout=30)
        resp.raise_for_status()
        orders.extend(resp.json().get("orders", []))
        url, params = None, None
        for part in resp.headers.get("Link", "").split(","):
            if 'rel="next"' in part:
                m = re.search(r"<([^>]+)>", part)
                url = m.group(1) if m else None
                break
    return orders


def categorize(orders):
    """Liefert {Kategorie: [Zeilen-Dicts]}."""
    result = {name: [] for name, _ in CATEGORIES}
    for o in orders:
        b = o.get("billing_address") or {}
        customer = b.get("name") or o.get("email") or "—"
        date = (o.get("created_at") or "")[:10]
        for cat_name, keyword in CATEGORIES:
            matched = [li for li in o.get("line_items", [])
                       if keyword in (li.get("title") or "").lower()]
            if not matched:
                continue
            qty = sum(li.get("quantity", 0) for li in matched)
            products = "; ".join(sorted({li.get("title") for li in matched}))
            result[cat_name].append({
                "order": o.get("name"),
                "date": date,
                "customer": customer,
                "qty": qty,
                "products": products,
            })
    # Nach Bestellnummer sortieren
    for rows in result.values():
        rows.sort(key=lambda r: int(re.sub(r"\D", "", r["order"]) or 0))
    return result


def print_report(result):
    for cat_name, _ in CATEGORIES:
        rows = result[cat_name]
        total_qty = sum(r["qty"] for r in rows)
        print(f"\n{'='*78}\n{cat_name}  —  {len(rows)} Bestellung(en), {total_qty} Stück\n{'='*78}")
        for r in rows:
            print(f"  {r['order']:<8} {r['date']}  {str(r['qty']).rjust(3)}×  "
                  f"{r['customer'][:28].ljust(28)}  {r['products']}")


def write_csv(result):
    for cat_name, _ in CATEGORIES:
        fname = f"liste_{cat_name.lower().replace(' ', '_').replace('-', '_')}.csv"
        with open(fname, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow(["Bestellnr", "Datum", "Kunde", "Menge", "Produkte"])
            for r in result[cat_name]:
                w.writerow([r["order"], r["date"], r["customer"], r["qty"], r["products"]])
        print(f"  geschrieben: {fname} ({len(result[cat_name])} Zeilen)")


if __name__ == "__main__":
    orders = fetch_all_orders()
    result = categorize(orders)
    print_report(result)
    if "--csv" in sys.argv:
        print(f"\n{'='*78}\nCSV-Export\n{'='*78}")
        write_csv(result)
