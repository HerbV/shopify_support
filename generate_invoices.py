#!/usr/bin/env python3
import os
import re
import glob
import time
import subprocess
from datetime import datetime
from jinja2 import Template
import qrcode
import requests


# Configurations
WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
SHOPIFY_API_VERSION = "2026-04"
TEMPLATE_PATH = os.path.join(WORKSPACE_DIR, "invoice_template.html")
OUTPUT_DIR = os.path.join(WORKSPACE_DIR, "Rechnungen_Erstellt")
SAMPLES_DIR = os.path.join(WORKSPACE_DIR, "Rechnungen")

# Max/Starting Sequences
START_BELEGNUMMER_SEQ = 100562
START_VORGANGSNUMMER_SEQ = 411700
START_KUNDENNUMMER_SEQ = 90001

def get_latest_sequences():
    """Finds the latest invoice and transaction numbers from existing sample PDFs."""
    max_beleg = START_BELEGNUMMER_SEQ - 1
    max_vorgang = START_VORGANGSNUMMER_SEQ - 1

    if os.path.isdir(SAMPLES_DIR):
        pdfs = glob.glob(os.path.join(SAMPLES_DIR, "Rechnung 2026-*.pdf"))
        beleg_numbers = []
        for pdf in pdfs:
            basename = os.path.basename(pdf)
            m = re.search(r"2026-(\d+)", basename)
            if m:
                beleg_numbers.append(int(m.group(1)))
        if beleg_numbers:
            max_beleg = max(beleg_numbers)

        # Let's inspect a few of the latest files to find their Vorgangsnummer
        latest_pdfs = sorted(pdfs, key=os.path.getmtime)[-5:]
        vorgang_numbers = []
        for pdf_path in latest_pdfs:
            try:
                import pdfplumber
                with pdfplumber.open(pdf_path) as pdf:
                    text = pdf.pages[0].extract_text()
                    m = re.search(r"Vorgangsnummer\s+(\d+)", text)
                    if m:
                        vorgang_numbers.append(int(m.group(1)))
            except Exception:
                pass
        if vorgang_numbers:
            max_vorgang = max(vorgang_numbers)

    return max_beleg + 1, max_vorgang + 1

def format_german_date(date_str):
    """Parses standard Shopify date string (e.g. 2026-05-18 16:54:22 +0200) to German format (dd.mm.yyyy)."""
    if not date_str:
        return ""
    m = re.search(r"^(\d{4})-(\d{2})-(\d{2})", str(date_str))
    if m:
        return f"{m.group(3)}.{m.group(2)}.{m.group(1)}"
    return str(date_str)

def format_money(value):
    """Formats float value as German currency representation (e.g. 1.234,56)."""
    if value is None:
        return "0,00"
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def extract_termin(item_name, order_date_str):
    """Attempts to extract a delivery/event date from product name, else returns order date."""
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(?:\d{2,4})?\b", item_name)
    if m:
        day = int(m.group(1))
        month = int(m.group(2))
        year = 2026
        return f"{day:02d}.{month:02d}.{year}"
    
    m2 = re.search(r"\b(\d{1,2})\.(\d{1,2})\.\b", item_name)
    if m2:
        day = int(m2.group(1))
        month = int(m2.group(2))
        year = 2026
        return f"{day:02d}.{month:02d}.{year}"
        
    return format_german_date(order_date_str)

def classify_product(item_name):
    """Maps a product title to an order category key, or None if irrelevant."""
    nl = (item_name or "").lower()
    if "umlarv" in nl:
        return "umlarv"
    if "auffahrt" in nl:
        return "auffahrt"
    if "königin" in nl or "koenigin" in nl:
        return "koenigin"
    return None

def umlarv_termin_label(item_name):
    """Short termin label for an Umlarvaktion product, e.g. '27.5. Linz'."""
    m = re.search(r"(\d{1,2}\.\d{1,2}\.)\s*in\s+([^\s(]+)", item_name)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    m2 = re.search(r"(\d{1,2}\.\d{1,2}\.)", item_name)
    return m2.group(1) if m2 else "Umlarvaktion"

def clean_zip(zip_val):
    """Cleans Excel single quote artifacts from zip codes."""
    if zip_val is None:
        return ""
    return str(zip_val).replace("'", "").replace('"', "").strip()

def map_product_to_artnr(item_name):
    """Maps product descriptions to corporate SAGE article numbers."""
    name_lower = item_name.lower()
    if "umlarv" in name_lower:
        return "109001"
    elif "wirtschaft" in name_lower:
        return "109002"
    elif "zac" in name_lower:
        return "109003"
    elif "offensee" in name_lower:
        return "109004"
    elif "unbegattete" in name_lower:
        return "109005"
    elif "belegstelle" in name_lower:
        return "109006"
    elif "frühbucher" in name_lower:
        return "109007"
    elif "versand" in name_lower:
        return "109008"
    return "109999"

def get_db_connection():
    # Try to load env file from current directory
    env_path = os.path.join(WORKSPACE_DIR, "env")
    if os.path.exists(env_path):
        import dotenv
        dotenv.load_dotenv(env_path)
    else:
        try:
            import dotenv
            dotenv.load_dotenv()
        except ImportError:
            pass
            
    server = os.getenv("SQL_SERVER")
    database = os.getenv("SQL_DATABASE")
    user = os.getenv("SQL_USER")
    password = os.getenv("SQL_PASSWORD")
    driver = os.getenv("SQL_DRIVER", "ODBC Driver 18 for SQL Server")
    
    if not server or not database:
        return None
        
    # Standardize driver wrapping for pyodbc
    if not driver.startswith("{"):
        driver_str = f"{{{driver}}}"
    else:
        driver_str = driver
        
    conn_str = f"DRIVER={driver_str};SERVER={server};DATABASE={database};UID={user};PWD={password};TrustServerCertificate=yes;"
    try:
        import pyodbc
        return pyodbc.connect(conn_str, timeout=3)
    except Exception as e:
        print(f"Datenbankverbindung fehlgeschlagen: {e}")
        return None

def check_membership(kdnr):
    """
    Checks if a customer number is an active member in the database.
    Returns (is_member, db_error).
    """
    if not kdnr:
        return False, False
        
    conn = get_db_connection()
    if not conn:
        return False, True
        
    try:
        cursor = conn.cursor()
        query = "SELECT kategorie FROM [BZV_Test].[dbo].[Mitglieder_alle_Daten] WHERE kundennummer = ?"
        cursor.execute(query, (kdnr,))
        row = cursor.fetchone()
        if row:
            kategorie = row[0]
            # Active categories: "Mitglied mit Bienen", "Mitglied ohne Bienen"
            if kategorie in ["Mitglied mit Bienen", "Mitglied ohne Bienen"]:
                return True, False
        return False, False
    except Exception as e:
        print(f"Fehler bei der Mitgliederdatenbank-Abfrage für {kdnr}: {e}")
        return False, True
    finally:
        try:
            conn.close()
        except Exception:
            pass

def find_member_in_db(email=None, b_name=None, b_city=None):
    """
    Tries to find a customer in the database by Email, or Name + City.
    Returns the resolved kundennummer, or None.
    """
    conn = get_db_connection()
    if not conn:
        return None
        
    try:
        cursor = conn.cursor()
        
        # 1. Try to match by exact Email address
        if email and email.strip():
            query = "SELECT kundennummer FROM [BZV_Test].[dbo].[Mitglieder_alle_Daten] WHERE email = ?"
            cursor.execute(query, (email.strip(),))
            row = cursor.fetchone()
            if row and row[0]:
                return str(row[0])
                
        # 2. Try to match by Name and City
        if b_name and b_name.strip():
            name_parts = b_name.strip().split()
            if len(name_parts) >= 2:
                first_name = name_parts[0]
                last_name = name_parts[-1] # take the last word as surname
                
                # Check with both name and city
                if b_city and b_city.strip():
                    query = """
                        SELECT kundennummer FROM [BZV_Test].[dbo].[Mitglieder_alle_Daten] 
                        WHERE vorname = ? AND nachname = ? AND ort = ?
                    """
                    cursor.execute(query, (first_name, last_name, b_city.strip()))
                    row = cursor.fetchone()
                    if row and row[0]:
                        return str(row[0])
                        
                # 3. Fallback: match by Name alone
                query = """
                    SELECT kundennummer FROM [BZV_Test].[dbo].[Mitglieder_alle_Daten] 
                    WHERE vorname = ? AND nachname = ?
                """
                cursor.execute(query, (first_name, last_name))
                row = cursor.fetchone()
                if row and row[0]:
                    return str(row[0])
                    
        return None
    except Exception as e:
        print(f"Fehler bei Mitgliedersuche in DB: {e}")
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def generate_single_invoice(order, output_dir, belegnummer_str, vorgangsnummer_str):
    """Generates a single PDF invoice from structured order data."""
    total_brutto = order["total_brutto"]
    financial_status = order["financial_status"]
    paid_at = order["paid_at"]
    created_at = order["created_at"]
    payment_method = order["payment_method"]
    kdnr = order["kdnr"]
    b_name = order["b_name"]
    
    payment_date = format_german_date(paid_at) if paid_at else format_german_date(created_at)
    pm_str = str(payment_method) if payment_method else "Shopify-Zahlung"
    
    # Generate SEPA QR-Code if unpaid and positive amount
    qr_path = None
    if total_brutto > 0.001 and financial_status != 'paid':
        qr_data = f"BCD\n002\n1\nSCT\nVKBLAT2L\nOÖ Landesverband für Bienenzucht\nAT191860000010021657\nEUR{total_brutto:.2f}\n\n\nReNr {belegnummer_str} KdNr {kdnr}\n"
        qr = qrcode.QRCode(box_size=10, border=1)
        qr.add_data(qr_data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        qr_path = os.path.join(output_dir, f"qr_{belegnummer_str}.png")
        img.save(qr_path)

    if financial_status == 'paid' or total_brutto <= 0.001:
        payment_text = f"Der Rechnungsbetrag von EUR {format_money(total_brutto)} wurde bereits am {payment_date} vollständig per {pm_str} bezahlt. Zahlung dankend erhalten."
    else:
        payment_text = f"Zahlungsvereinbarungen: Zahlung erfolgt sofort ohne Abzug. Bei eBanking bitte unbedingt anführen: ReNr {belegnummer_str} und KdNr {kdnr}."
        
    # Render HTML
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = Template(f.read())
        
    context = {
        "sender_line": "OÖ Landesverband für Bienenzucht - Pachmayrstraße 57 - 4040 Linz",
        "customer_address": order["cust_addr"],
        "logo_path": os.path.join(WORKSPACE_DIR, "logo.png"),
        "qr_path": qr_path,
        "title": "Rechnung",
        "vorgangsnummer": vorgangsnummer_str,
        "belegnummer": belegnummer_str,
        "datum": format_german_date(created_at),
        "kundennummer": kdnr,
        "bearbeiter": "Sabine Hochreiter",
        "versandart": order["shipping_method"] or "Bienenladen",
        "ust_id_uns": "ATU23004200",
        "lieferbedingung": "",
        "bezug": "",
        "ust_id_kunde": "",
        "ihr_zeichen": "",
        "ihr_beleg": "",
        "steuer_nr_uns": "",
        "items": order["items"],
        "zwischensumme_str": format_money(total_brutto),
        "taxes": order["taxes_list"],
        "endsumme_str": format_money(total_brutto),
        "notes": order["notes"],
        "payment_text": payment_text
    }
    
    html_content = template.render(context)
    
    # Save temporary HTML file
    temp_html_path = os.path.join(output_dir, f"temp_{belegnummer_str}.html")
    with open(temp_html_path, "w", encoding="utf-8") as f_html:
        f_html.write(html_content)
        
    # Compile PDF using Headless Chrome
    output_pdf_path = os.path.join(output_dir, f"Rechnung {belegnummer_str}.pdf")
    
    chrome_cmd = [
        "google-chrome-stable",
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--no-pdf-header-footer",
        f"--print-to-pdf={output_pdf_path}",
        temp_html_path
    ]
    
    try:
        subprocess.run(chrome_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        if os.path.exists(temp_html_path):
            os.remove(temp_html_path)
        if qr_path and os.path.exists(qr_path):
            os.remove(qr_path)
        return True, f"Erfolgreich erstellt: Rechnung {belegnummer_str}.pdf für {b_name} (KdNr: {kdnr}, Betrag: {format_money(total_brutto)} EUR)"
    except subprocess.CalledProcessError as e:
        if os.path.exists(temp_html_path):
            os.remove(temp_html_path)
        if qr_path and os.path.exists(qr_path):
            os.remove(qr_path)
        return False, f"Fehler: Konvertierung in PDF für {belegnummer_str} fehlgeschlagen! {e}"



def _load_shopify_env():
    """Reads the env file (KEY=VALUE) into os.environ without external deps."""
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


# Cached Shopify access token (client_credentials flow)
_shopify_token = None
_shopify_token_expires_at = 0


def get_shopify_token():
    """
    Obtains an Admin API access token via the client_credentials OAuth flow.
    Returns (token, error_message); token is cached until shortly before expiry.
    """
    global _shopify_token, _shopify_token_expires_at

    _load_shopify_env()
    shop = os.getenv("SHOPIFY_SHOP")
    client_id = os.getenv("SHOPIFY_CLIENT_ID")
    client_secret = os.getenv("SHOPIFY_CLIENT_SECRET")

    if not shop or not client_id or not client_secret:
        return None, ("Shopify-API-Fehler: Keine Zugangsdaten (SHOPIFY_SHOP / "
                      "SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET) in der env-Datei konfiguriert.")

    shop = shop.strip().rstrip('.').replace(".myshopify.com", "")

    if _shopify_token and time.time() < _shopify_token_expires_at - 60:
        return _shopify_token, None

    try:
        r = requests.post(
            f"https://{shop}.myshopify.com/admin/oauth/access_token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id.strip(),
                "client_secret": client_secret.strip(),
            },
            timeout=15,
        )
        if r.status_code != 200:
            return None, f"Shopify-Token-Fehler (Status {r.status_code}): {r.text}"
        d = r.json()
        _shopify_token = d["access_token"]
        _shopify_token_expires_at = time.time() + d.get("expires_in", 3600)
        return _shopify_token, None
    except Exception as e:
        return None, f"Verbindungsfehler bei der Shopify-Token-Anfrage: {str(e)}"


def fetch_shopify_orders_from_api(start_date, end_date, status="any"):
    """
    Queries the Shopify Admin API (REST) for orders in a specific date range,
    following pagination so all matching orders are returned.
    Returns (orders_json, error_message).
    """
    token, err = get_shopify_token()
    if err:
        return None, err

    shop = os.getenv("SHOPIFY_SHOP", "").strip().rstrip('.').replace(".myshopify.com", "")
    headers = {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
    }

    # Format dates to ISO-8601 with timezone (Shopify expects this)
    # E.g., 2026-05-01T00:00:00+02:00
    params = {
        "status": "any",
        "created_at_min": f"{start_date}T00:00:00+02:00",
        "created_at_max": f"{end_date}T23:59:59+02:00",
        "limit": 250,
    }
    # The UI sends payment states (paid/unpaid); those belong to financial_status.
    if status and status != "any":
        params["financial_status"] = status

    url = f"https://{shop}.myshopify.com/admin/api/{SHOPIFY_API_VERSION}/orders.json"
    all_orders = []

    try:
        while url:
            response = requests.get(url, headers=headers, params=params, timeout=30)
            if response.status_code != 200:
                return None, f"Shopify-API-Fehler (Status {response.status_code}): {response.text}"

            data = response.json()
            if "orders" not in data:
                return None, "Shopify-API-Fehler: Ungültige Antwortstruktur von Shopify."

            all_orders.extend(data["orders"])

            # Cursor-based pagination via the Link header (rel="next").
            url = None
            params = None
            link = response.headers.get("Link", "")
            for part in link.split(","):
                if 'rel="next"' in part:
                    m = re.search(r"<([^>]+)>", part)
                    if m:
                        url = m.group(1)
                    break

        return all_orders, None
    except Exception as e:
        return None, f"Verbindungsfehler zur Shopify-API: {str(e)}"


def parse_shopify_api_orders(api_orders):
    """
    Parses orders fetched from Shopify Admin API JSON and returns a structured list of orders.
    Replicates the parsing, pricing, DB-lookup, and tax-checking logic from parse_input_file.
    """
    parsed_orders = []
    assigned_knrs = {}
    
    # 1. Build a customer_knr_map from orders that have a KNr tag
    customer_knr_map = {}
    for order in api_orders:
        email = order.get("email")
        b_addr = order.get("billing_address") or {}
        b_name = b_addr.get("name")
        tags = order.get("tags")
        
        knr = None
        if tags:
            m = re.search(r"KNr:(\S+)", str(tags))
            if m:
                knr = m.group(1).rstrip(",")
        if knr:
            if email:
                customer_knr_map[email] = knr
            if b_name:
                customer_knr_map[b_name] = knr

    # 2. Sort orders by order number ascending
    def get_order_num(o):
        val = re.sub(r"\D", "", o["name"])
        return int(val) if val else 0
        
    sorted_orders = sorted(api_orders, key=get_order_num)

    # 3. Parse each order
    for order in sorted_orders:
        order_name = order["name"]
        email = order.get("email")
        created_at = order["created_at"]
        
        b_addr = order.get("billing_address") or {}
        b_name = b_addr.get("name")
        b_company = b_addr.get("company")
        b_street = b_addr.get("address1")
        if b_addr.get("address2"):
            b_street = f"{b_street} {b_addr.get('address2')}".strip()
        b_city = b_addr.get("city")
        b_zip = clean_zip(b_addr.get("zip"))
        b_country = b_addr.get("country_code")
        
        shipping_lines = order.get("shipping_lines", [])
        shipping_method = shipping_lines[0].get("title") if shipping_lines else None
        shipping_cost = float(shipping_lines[0].get("price", 0.0)) if shipping_lines else 0.0
        
        tags = order.get("tags")
        payment_gateway_names = order.get("payment_gateway_names", [])
        payment_method = ", ".join(payment_gateway_names) if payment_gateway_names else "Shopify"
        
        financial_status = order.get("financial_status")
        paid_at = order.get("processed_at") if financial_status == "paid" else None
        notes = order.get("note")
        
        # Resolve Kundennummer
        kdnr = None
        if tags:
            m = re.search(r"KNr:(\S+)", str(tags))
            if m:
                kdnr = m.group(1).rstrip(",")
        
        if not kdnr and email and email in customer_knr_map:
            kdnr = customer_knr_map[email]
        if not kdnr and b_name and b_name in customer_knr_map:
            kdnr = customer_knr_map[b_name]
            
        if not kdnr:
            lookup_key = email or b_name or order_name
            if lookup_key in assigned_knrs:
                kdnr = assigned_knrs[lookup_key]
            else:
                db_match_knr = find_member_in_db(email=email, b_name=b_name, b_city=b_city)
                if db_match_knr:
                    kdnr = db_match_knr
                else:
                    kdnr = "Neukunde"
                assigned_knrs[lookup_key] = kdnr

        # Format Customer Address
        cust_addr = []
        if b_company and str(b_company).strip() and str(b_company).strip() != str(b_name).strip():
            cust_addr.append(str(b_company).strip())
            cust_addr.append(f"z.Hd. {str(b_name).strip()}")
        else:
            cust_addr.append(str(b_name).strip() if b_name else "")
            
        cust_addr.append(str(b_street).strip() if b_street else "")
        cust_addr.append(f"{b_zip} {str(b_city).strip()}" if b_zip or b_city else "")
        if b_country and str(b_country).strip().upper() != 'AT':
            cust_addr.append(str(b_country).strip().upper())
            
        # Build line items
        items = []
        pos = 1
        brutto_total_13 = 0.0
        brutto_total_20 = 0.0
        brutto_total_0 = 0.0
        total_discount = 0.0
        order_categories = set()
        umlarv_termine = set()

        line_items = order.get("line_items", [])
        for item in line_items:
            item_name = item.get("title") or item.get("name")
            if not item_name:
                continue

            cat = classify_product(item_name)
            if cat:
                order_categories.add(cat)
                if cat == "umlarv":
                    umlarv_termine.add(umlarv_termin_label(item_name))

            qty = int(item.get("quantity", 1))
            price = float(item.get("price", 0.0))
            
            disc_amount = float(item.get("total_discount", 0.0))
            total_discount += disc_amount
            
            item_brutto_total = (qty * price) - disc_amount
            
            tax_rate = 0.13
            tax_code = "2"
            
            tax_lines = item.get("tax_lines", [])
            if tax_lines:
                try:
                    rate = float(tax_lines[0].get("rate", 0.13))
                    if abs(rate - 0.20) < 0.02:
                         tax_rate = 0.20
                         tax_code = "1"
                    elif abs(rate - 0.13) < 0.02:
                         tax_rate = 0.13
                         tax_code = "2"
                    elif rate < 0.01:
                         tax_rate = 0.00
                         tax_code = "0"
                except Exception:
                    pass
            else:
                item_name_lower = item_name.lower()
                if "umlarv" in item_name_lower:
                    tax_rate = 0.20
                    tax_code = "1"
                elif not item.get("taxable", True):
                    tax_rate = 0.00
                    tax_code = "0"
            
            if tax_code == "1":
                brutto_total_20 += item_brutto_total
            elif tax_code == "2":
                brutto_total_13 += item_brutto_total
            else:
                brutto_total_0 += item_brutto_total
                
            items.append({
                "pos": pos,
                "artikelnr": map_product_to_artnr(item_name),
                "name": item_name,
                "details": f"Rabatt: -{format_money(disc_amount)} EUR" if disc_amount > 0 else None,
                "termin": extract_termin(item_name, created_at),
                "variant": (item.get("variant_title") or "").replace("\t", " ").strip(),
                "category": cat,
                "umlarv_termin": umlarv_termin_label(item_name) if cat == "umlarv" else "",
                "menge_str": f"{qty}x",
                "menge": qty,
                "einzelpreis_str": format_money(price),
                "gesamtpreis_str": format_money(item_brutto_total),
                "sc": tax_code
            })
            pos += 1
            
        if shipping_cost > 0:
            shipping_tax_code = "1"
            brutto_total_20 += shipping_cost
            
            items.append({
                "pos": pos,
                "artikelnr": "109008",
                "name": f"Versandkosten ({shipping_method or 'Standard'})",
                "details": None,
                "termin": format_german_date(created_at),
                "menge_str": "1x",
                "einzelpreis_str": format_money(shipping_cost),
                "gesamtpreis_str": format_money(shipping_cost),
                "sc": shipping_tax_code
            })
            pos += 1

        total_brutto = brutto_total_0 + brutto_total_13 + brutto_total_20

        # Calculate Net Base and Tax Values
        taxes_list = []
        if brutto_total_20 > 0:
            net_base = round(brutto_total_20 / 1.20, 2)
            tax_val = round(brutto_total_20 - net_base, 2)
            taxes_list.append({
                "sc": "1",
                "rate_str": "20,00",
                "base_str": format_money(net_base),
                "value_str": format_money(tax_val)
            })
            
        if brutto_total_13 > 0:
            net_base = round(brutto_total_13 / 1.13, 2)
            tax_val = round(brutto_total_13 - net_base, 2)
            taxes_list.append({
                "sc": "2",
                "rate_str": "13,00",
                "base_str": format_money(net_base),
                "value_str": format_money(tax_val)
            })
            
        if brutto_total_0 > 0:
            taxes_list.append({
                "sc": "0",
                "rate_str": "0,00",
                "base_str": format_money(brutto_total_0),
                "value_str": format_money(0.0)
            })

        # Check membership status in BZV SQL database
        is_member, db_error = check_membership(kdnr)
        
        # A member must receive a discount. If they are a member and total discount is 0, highlight them!
        member_missing_discount = bool(is_member and total_discount <= 0.01)

        order_data = {
            "order_name": order_name,
            "email": email,
            "created_at": created_at,
            "b_name": b_name,
            "b_company": b_company,
            "b_street": b_street,
            "b_city": b_city,
            "b_zip": b_zip,
            "b_country": b_country,
            "shipping_method": shipping_method,
            "shipping_cost": shipping_cost,
            "tags": tags,
            "payment_method": payment_method,
            "paid_at": paid_at,
            "financial_status": financial_status,
            "notes": notes,
            "kdnr": kdnr,
            "cust_addr": cust_addr,
            "items": items,
            "total_brutto": total_brutto,
            "total_brutto_str": format_money(total_brutto),
            "taxes_list": taxes_list,
            "total_discount": total_discount,
            "total_discount_str": format_money(total_discount),
            "is_member": is_member,
            "member_missing_discount": member_missing_discount,
            "db_error": db_error,
            "categories": sorted(order_categories),
            "umlarv_termine": sorted(umlarv_termine),
        }
        parsed_orders.append(order_data)

    return parsed_orders

