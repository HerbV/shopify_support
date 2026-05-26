#!/usr/bin/env python3
"""Liest Bestellungen aus der Shopify Admin API aus (GraphQL).

Nutzt den client_credentials-Flow mit SHOPIFY_SHOP / SHOPIFY_CLIENT_ID /
SHOPIFY_CLIENT_SECRET aus der env-Datei.
"""
import os
import time
import sys
import requests

WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
API_VERSION = "2026-04"


def load_env():
    """Liest die env-Datei (Format KEY=VALUE) ohne externe Abhängigkeit ein."""
    env_path = os.path.join(WORKSPACE_DIR, "env")
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


load_env()

SHOP = os.environ["SHOPIFY_SHOP"]          # nur Subdomain, ohne .myshopify.com
CLIENT_ID = os.environ["SHOPIFY_CLIENT_ID"]
CLIENT_SECRET = os.environ["SHOPIFY_CLIENT_SECRET"]

_token, _expires_at = None, 0


def get_token():
    global _token, _expires_at
    if _token and time.time() < _expires_at - 60:
        return _token
    r = requests.post(
        f"https://{SHOP}.myshopify.com/admin/oauth/access_token",
        data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        timeout=15,
    )
    r.raise_for_status()
    d = r.json()
    _token, _expires_at = d["access_token"], time.time() + d.get("expires_in", 3600)
    return _token


def graphql(query, variables=None):
    r = requests.post(
        f"https://{SHOP}.myshopify.com/admin/api/{API_VERSION}/graphql.json",
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": get_token(),
        },
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


ORDERS_QUERY = """
query Orders($first: Int!, $after: String) {
  orders(first: $first, after: $after, sortKey: CREATED_AT, reverse: true) {
    pageInfo { hasNextPage endCursor }
    nodes {
      name
      createdAt
      displayFinancialStatus
      displayFulfillmentStatus
      email
      currentTotalPriceSet { shopMoney { amount currencyCode } }
      customer { displayName }
    }
  }
}
"""


def fetch_orders(limit=25):
    result = graphql(ORDERS_QUERY, {"first": limit, "after": None})
    if "errors" in result:
        print("GraphQL-Fehler:", result["errors"], file=sys.stderr)
        return []
    return result["data"]["orders"]["nodes"]


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    print(f"Shop: {SHOP}.myshopify.com  |  API {API_VERSION}")
    orders = fetch_orders(n)
    print(f"\n{len(orders)} Bestellung(en):\n")
    for o in orders:
        money = o["currentTotalPriceSet"]["shopMoney"]
        cust = (o.get("customer") or {}).get("displayName") or o.get("email") or "—"
        print(
            f"  {o['name']:<10} {o['createdAt'][:10]}  "
            f"{money['amount']:>10} {money['currencyCode']}  "
            f"{o['displayFinancialStatus']:<12} {o['displayFulfillmentStatus']:<12} {cust}"
        )
