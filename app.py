#!/usr/bin/env python3
import os
import re
import csv
import json
import zipfile
import io
import shutil
from flask import (
    Flask, render_template, request, jsonify, send_from_directory, send_file,
    session, redirect, url_for, flash,
)
from werkzeug.security import check_password_hash
from datetime import datetime, timedelta

# Import refactored functions from generate_invoices.py
from generate_invoices import (
    generate_single_invoice,
    get_latest_sequences,
    fetch_shopify_orders_from_api,
    fetch_shopify_orders_params,
    fetch_order_transactions,
    parse_shopify_api_orders,
    get_db_connection,
    qualified_table,
    OUTPUT_DIR,
    WORKSPACE_DIR
)

app = Flask(__name__)
# Session-Schlüssel aus der Umgebung (FLASK_SECRET_KEY); Fallback nur für lokale Entwicklung.
app.secret_key = os.getenv("FLASK_SECRET_KEY", "bee_invoice_secret_key")

# --- Login gegen die Mitgliederverwaltung ----------------------------------
# Authentifizierung gegen dieselbe Benutzertabelle wie die Vereinsverwaltung
# (dbo.User_Accounts in der konfigurierten DB BZV_2026, werkzeug-Passwort-Hashes).
# Es werden hier KEINE Benutzer angelegt – die Verwaltung erfolgt in der Vereinsverwaltung.

# Nur Landesverbands-Verwaltung darf in diese Anwendung (kein Mitglied/OV-Admin).
VERWALTUNG_ROLES = {"admin_lv", "mitarbeiter_lv"}

# Endpunkte, die ohne Anmeldung erreichbar sind.
PUBLIC_ENDPOINTS = {"login", "static"}


def get_user_by_username(username):
    """Holt einen aktiven Benutzer aus User_Accounts. None bei Fehler/nicht gefunden."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT * FROM {qualified_table('User_Accounts')} WHERE username = ? AND active = 1",
            (username,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        columns = [c[0] for c in cursor.description]
        return dict(zip(columns, row))
    except Exception as e:
        print(f"Fehler bei Benutzerabfrage: {e}")
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


@app.before_request
def require_login():
    """Schützt alle Routen; nicht angemeldete Nutzer*innen landen beim Login."""
    if request.endpoint in PUBLIC_ENDPOINTS:
        return
    if "user_id" not in session:
        # API-Aufrufe bekommen 401 statt eines HTML-Redirects.
        if request.path.startswith("/api/"):
            return jsonify({"error": "Nicht angemeldet"}), 401
        return redirect(url_for("login"))


@app.context_processor
def inject_current_user():
    """Stellt den angemeldeten Benutzer in allen Templates bereit."""
    return {
        "current_user": session.get("username"),
        "current_role": session.get("role"),
    }


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = get_user_by_username(username)
        if user and check_password_hash(user["password_hash"], password):
            # role kann mehrere kommaseparierte Rollen enthalten (z. B. "admin_lv,mitarbeiter_lv").
            roles = {r.strip() for r in (user["role"] or "").split(",") if r.strip()}
            if not (roles & VERWALTUNG_ROLES):
                flash("Kein Zugriff – diese Anwendung ist nur für die Verwaltung.", "error")
                return render_template("login.html")
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            # Bearbeiter*in-Name für Rechnungen: Vorname + Nachname; falls nicht
            # hinterlegt, der Verband statt des Login-Namens (E-Mail).
            vn = (user.get("vorname") or "").strip()
            nn = (user.get("nachname") or "").strip()
            session["bearbeiter"] = f"{vn} {nn}".strip() or "OÖ Landesverband für Bienenzucht"
            flash(f"Willkommen, {username}!", "success")
            return redirect(url_for("index"))
        flash("Benutzername oder Passwort ungültig.", "error")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# Belegnummer-Format: <Prefix><Jahr>-<laufende Nummer>, z. B. "SF2026-100598"
BELEG_PREFIX = "SF"
BELEG_YEAR = "2026"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# Ensure static folder exists and contains logo.png
STATIC_FOLDER = os.path.join(WORKSPACE_DIR, "static")
os.makedirs(STATIC_FOLDER, exist_ok=True)
if os.path.exists(os.path.join(WORKSPACE_DIR, "logo.png")):
    shutil.copy(os.path.join(WORKSPACE_DIR, "logo.png"), os.path.join(STATIC_FOLDER, "logo.png"))

# --- Lokaler Bestelldaten-Speicher -----------------------------------------
# Rohe Shopify-Bestellungen werden lokal persistiert; "Aktualisieren" holt nur
# neue/geänderte (updated_at) und führt sie in den Bestand. Beide Seiten lesen
# daraus, ohne bei jedem Laden Shopify abzufragen.
DATA_DIR = os.path.join(WORKSPACE_DIR, "data")
ORDERS_FILE = os.path.join(DATA_DIR, "orders.json")
# Zuordnung Belegnummer -> Bestellnummer (für die Anzeige im Archiv)
INVOICE_ORDERS_FILE = os.path.join(DATA_DIR, "invoice_orders.json")


def load_invoice_orders():
    """Lädt die Zuordnung {belegnummer: bestellnummer}."""
    if not os.path.exists(INVOICE_ORDERS_FILE):
        return {}
    try:
        with open(INVOICE_ORDERS_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}


def _write_invoice_orders(mapping):
    """Schreibt die Belegnummer->Bestellnummer-Zuordnung atomar."""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = INVOICE_ORDERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(mapping, fh, ensure_ascii=False)
    os.replace(tmp, INVOICE_ORDERS_FILE)


def save_invoice_order(belegnummer, bestellnummer):
    """Merkt sich die Bestellnummer zu einer erzeugten Belegnummer (atomar)."""
    if not bestellnummer:
        return
    mapping = load_invoice_orders()
    mapping[belegnummer] = bestellnummer
    _write_invoice_orders(mapping)


def remove_invoice_orders_for(bestellnummer, keep=None):
    """Löscht erzeugte PDFs + Zuordnungen einer Bestellung (außer `keep`).

    Wird von der Einzel-Neuerstellung genutzt, um die zuletzt erzeugte Rechnung
    der Bestellung zu ersetzen. Liefert die Liste der entfernten Belegnummern."""
    mapping = load_invoice_orders()
    removed = []
    for beleg, name in list(mapping.items()):
        if name == bestellnummer and beleg != keep:
            pdf = os.path.join(OUTPUT_DIR, f"Rechnung {beleg}.pdf")
            try:
                if os.path.exists(pdf):
                    os.remove(pdf)
            except OSError:
                pass
            mapping.pop(beleg, None)
            removed.append(beleg)
    if removed:
        _write_invoice_orders(mapping)
    return removed


def next_beleg_seq():
    """Nächste freie laufende Belegnummer.

    Berücksichtigt die Referenz-PDFs (get_latest_sequences) UND die bereits
    erzeugten Rechnungen in OUTPUT_DIR, damit keine Nummer doppelt vergeben wird."""
    next_beleg, _ = get_latest_sequences()
    max_seq = next_beleg - 1
    if os.path.isdir(OUTPUT_DIR):
        for fname in os.listdir(OUTPUT_DIR):
            if fname.lower().endswith(".pdf"):
                m = re.search(r"(\d+)\.pdf$", fname)
                if m:
                    max_seq = max(max_seq, int(m.group(1)))
    return max_seq + 1


def load_store():
    """Lädt den lokalen Bestand. Struktur: {last_update, initial_start, orders:{id:order}}."""
    if not os.path.exists(ORDERS_FILE):
        return {"last_update": None, "initial_start": None, "orders": {}, "flags": {}}
    try:
        with open(ORDERS_FILE, encoding="utf-8") as fh:
            store = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {"last_update": None, "initial_start": None, "orders": {}, "flags": {}}
    store.setdefault("orders", {})
    store.setdefault("last_update", None)
    store.setdefault("initial_start", None)
    store.setdefault("flags", {})  # manuelle Status-Marker je order_name (überleben Updates)
    return store


def save_store(store):
    """Schreibt den Bestand atomar (tmp + replace)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = ORDERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(store, fh, ensure_ascii=False)
    os.replace(tmp, ORDERS_FILE)


def store_orders_list(store):
    """Bestellungen als Liste (für Parsen/Kategorisieren)."""
    return list(store.get("orders", {}).values())

# --- Imkerei: Bestell-Kategorien -------------------------------------------
# Spiegelt die Logik aus bestellungen_kategorien.py, arbeitet aber direkt auf
# den REST-Orders von fetch_shopify_orders_from_api (keine eigene Abrufkette).
IMKEREI_CATEGORIES = [
    ("Königinnen", "königin"),
    ("Umlarvaktionen", "umlarvaktion"),
    ("Belegstellen-Auffahrt", "auffahrt"),
]


_GERMAN_MONTHS = {
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5,
    "juni": 6, "juli": 7, "august": 8, "september": 9, "oktober": 10,
    "november": 11, "dezember": 12,
}


def lieferdatum_sortkey(variant_title):
    """Sortier-Schlüssel für ein Königinnen-Lieferdatum aus dem variant_title.
    Erkennt 'TT.MM.JJ(JJ)' und deutsche Formate wie '3. Juni'. Positionen ohne
    erkennbares Datum werden ans Ende sortiert. Liefert (hat_datum, datum)."""
    s = variant_title or ""
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{2,4})?\b", s)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = m.group(3)
        if year:
            year = int(year)
            if year < 100:
                year += 2000
        else:
            year = 2026
        try:
            return (0, datetime(year, month, day))
        except ValueError:
            pass
    m2 = re.search(r"\b(\d{1,2})\.\s*([A-Za-zäöüÄÖÜ]+)", s)
    if m2 and m2.group(2).lower() in _GERMAN_MONTHS:
        day = int(m2.group(1))
        month = _GERMAN_MONTHS[m2.group(2).lower()]
        try:
            return (0, datetime(2026, month, day))
        except ValueError:
            pass
    return (1, datetime.max)  # ohne Termin -> ans Ende


def categorize_orders(api_orders):
    """Teilt Shopify-REST-Bestellungen anhand der Produkttitel in Kategorien auf.
    Eine Bestellung kann in mehreren Kategorien erscheinen.

    Liefert eine Liste von Kategorie-Dicts mit Feld "mode":
      - "flat" (Belegstellen-Auffahrt): eine Zeile je Bestellung,
        rows:[{order,date,customer,qty,products}].
      - "grouped" (Umlarvaktionen): nach Produkt (Aktion) gruppiert, je Aktion
        die Buchungen nach Uhrzeit (variant_title) sortiert,
        groups:[{product,count,total_qty,slots:[{time,customer,order,qty}]}].
      - "delivery" (Königinnen): eine Zeile je Position mit Lieferdatum
        (variant_title), chronologisch sortiert,
        rows:[{lieferdatum,order,customer,qty,sorte}].
    """
    flat = {}      # Andere flache Kategorien
    umlarv = {}    # Umlarvaktionen: Produkttitel (Aktion) -> Slot-Dicts
    koenig = []    # Königinnen: eine Zeile je Position
    auffahrt_rows = []  # Belegstellen-Auffahrt: eine Zeile je Position mit Liefertermin

    for o in api_orders:
        b = o.get("billing_address") or {}
        customer = b.get("name") or o.get("email") or "—"
        date = (o.get("created_at") or "")[:10]
        order_name = o.get("name")
        paid = o.get("financial_status") == "paid"
        line_items = o.get("line_items", [])

        for cat_name, keyword in IMKEREI_CATEGORIES:
            matched = [li for li in line_items
                       if keyword in (li.get("title") or "").lower()]
            if not matched:
                continue

            if cat_name == "Umlarvaktionen":
                # Pro Buchung (Line-Item) ein Slot, gruppiert nach Aktion (title)
                for li in matched:
                    product = li.get("title") or "—"
                    umlarv.setdefault(product, []).append({
                        "time": li.get("variant_title") or "—",
                        "customer": customer,
                        "order": order_name,
                        "qty": li.get("quantity", 0),
                        "paid": paid,
                    })
            elif cat_name == "Königinnen":
                # Pro Position eine Zeile; Lieferdatum steckt im variant_title
                for li in matched:
                    koenig.append({
                        "lieferdatum": (li.get("variant_title") or "").strip(),
                        "order": order_name,
                        "customer": customer,
                        "qty": li.get("quantity", 0),
                        "sorte": li.get("title") or "—",
                        "paid": paid,
                    })
            elif cat_name == "Belegstellen-Auffahrt":
                # Pro Position eine Zeile; Liefertermin steckt im variant_title
                for li in matched:
                    auffahrt_rows.append({
                        "lieferdatum": (li.get("variant_title") or "").strip(),
                        "order": order_name,
                        "customer": customer,
                        "qty": li.get("quantity", 0),
                        "sorte": li.get("title") or "—",
                        "paid": paid,
                    })
            else:
                qty = sum(li.get("quantity", 0) for li in matched)
                products = "; ".join(sorted({li.get("title") for li in matched}))
                flat.setdefault(cat_name, []).append({
                    "order": order_name,
                    "date": date,
                    "customer": customer,
                    "qty": qty,
                    "products": products,
                    "paid": paid,
                })

    result = []
    for cat_name, _ in IMKEREI_CATEGORIES:
        if cat_name == "Umlarvaktionen":
            groups = []
            for product in sorted(umlarv):
                slots = sorted(umlarv[product], key=lambda s: s["time"])
                groups.append({
                    "product": product,
                    "count": len(slots),
                    "total_qty": sum(s["qty"] for s in slots),
                    "slots": slots,
                })
            result.append({
                "name": cat_name,
                "mode": "grouped",
                "count": sum(g["count"] for g in groups),
                "total_qty": sum(g["total_qty"] for g in groups),
                "groups": groups,
            })
        elif cat_name == "Königinnen":
            koenig.sort(key=lambda r: lieferdatum_sortkey(r["lieferdatum"]))
            result.append({
                "name": cat_name,
                "mode": "delivery",
                "count": len(koenig),
                "total_qty": sum(r["qty"] for r in koenig),
                "rows": koenig,
            })
        elif cat_name == "Belegstellen-Auffahrt":
            auffahrt_rows.sort(key=lambda r: lieferdatum_sortkey(r["lieferdatum"]))
            result.append({
                "name": cat_name,
                "mode": "delivery",
                "count": len(auffahrt_rows),
                "total_qty": sum(r["qty"] for r in auffahrt_rows),
                "rows": auffahrt_rows,
            })
        else:
            rows = flat.get(cat_name, [])
            rows.sort(key=lambda r: int(re.sub(r"\D", "", r["order"] or "") or 0))
            result.append({
                "name": cat_name,
                "mode": "flat",
                "count": len(rows),
                "total_qty": sum(r["qty"] for r in rows),
                "rows": rows,
            })
    return result


@app.route("/")
def index():
    """Renders the main upload and generation screen."""
    return render_template("index.html")

@app.route("/api/sequences")
def api_sequences():
    """Returns the next invoice and transaction sequences."""
    next_beleg, next_vorgang = get_latest_sequences()
    return jsonify({
        "next_beleg": next_beleg,
        "next_vorgang": next_vorgang
    })

def kdnr_missing(kdnr):
    """True, wenn keine echte Kundennummer vorliegt (leer oder 'Neukunde')."""
    return str(kdnr or "").strip() in ("", "Neukunde")


@app.route("/api/orders")
def api_orders():
    """Returns the locally stored orders, parsed for the invoice screen."""
    store = load_store()
    raw = store_orders_list(store)
    try:
        parsed_orders = parse_shopify_api_orders(raw)
    except Exception as e:
        return jsonify({"error": f"Fehler beim Verarbeiten der lokalen Daten: {str(e)}"}), 500

    # Neueste zuerst (nach Erstelldatum absteigend)
    parsed_orders.sort(key=lambda o: o.get("created_at") or "", reverse=True)

    # Erstellte Rechnungen je Bestellung (Umkehrung der Belegnummer-Zuordnung).
    # Nur Belege mit tatsächlich vorhandener PDF-Datei zählen (keine toten Links/
    # veralteten Einträge), damit auch der Filter "Ohne Rechnung" verlässlich ist.
    invoice_orders = load_invoice_orders()
    rechnungen_by_order = {}
    for beleg, bestellnr in invoice_orders.items():
        if os.path.exists(os.path.join(OUTPUT_DIR, f"Rechnung {beleg}.pdf")):
            rechnungen_by_order.setdefault(bestellnr, []).append(beleg)

    # Manuelle Status-Marker je Bestellung anhängen
    flags = store.get("flags", {})
    for o in parsed_orders:
        f = flags.get(o.get("order_name"), {})
        o["nicht_verrechnen"] = bool(f.get("nicht_verrechnen"))
        o["rueckueberwiesen"] = bool(f.get("rueckueberwiesen"))
        # Manuell erfasste Kundennummer (überschreibt die automatische Zuordnung)
        manual_kdnr = (f.get("kdnr") or "").strip()
        if manual_kdnr:
            o["kdnr"] = manual_kdnr
        o["kdnr_missing"] = kdnr_missing(o.get("kdnr"))
        o["rechnungen"] = sorted(rechnungen_by_order.get(o.get("order_name"), []))
        # Manuell erfasste Positions-Hinweise (je Line-Item-ID) anhängen
        line_notes = f.get("line_notes", {})
        for it in o.get("items", []):
            it["hinweis"] = line_notes.get(str(it.get("line_id")), "")

    return jsonify({
        "orders": parsed_orders,
        "count": len(parsed_orders),
        "last_update": store.get("last_update"),
        "initial_start": store.get("initial_start"),
    })


@app.route("/api/orders/flags", methods=["POST"])
def api_orders_flags():
    """Persists a manual status flag (nicht_verrechnen / rueckueberwiesen) for an order."""
    data = request.get_json() or {}
    name = data.get("order_name")
    if not name:
        return jsonify({"error": "order_name fehlt"}), 400

    store = load_store()
    entry = store.setdefault("flags", {}).setdefault(name, {})
    if "nicht_verrechnen" in data:
        entry["nicht_verrechnen"] = bool(data["nicht_verrechnen"])
    if "rueckueberwiesen" in data:
        entry["rueckueberwiesen"] = bool(data["rueckueberwiesen"])
    if "kdnr" in data:
        val = (data.get("kdnr") or "").strip()
        if val:
            entry["kdnr"] = val
        else:
            entry.pop("kdnr", None)

    # Leere Einträge wieder entfernen, damit die Datei schlank bleibt
    if not any(entry.values()):
        store["flags"].pop(name, None)

    save_store(store)
    return jsonify({"success": True, "order_name": name, "flags": entry})


@app.route("/api/orders/line_note", methods=["POST"])
def api_orders_line_note():
    """Persists a free-text note for a single product line (by Shopify line-item id).

    Notes live under flags[order_name]["line_notes"][line_id] so they survive
    incremental Shopify updates (the line-item id is stable)."""
    data = request.get_json() or {}
    name = data.get("order_name")
    line_id = data.get("line_id")
    if not name or line_id in (None, "", "None"):
        return jsonify({"error": "order_name oder line_id fehlt"}), 400

    text = (data.get("hinweis") or "").strip()
    store = load_store()
    entry = store.setdefault("flags", {}).setdefault(name, {})
    notes = entry.setdefault("line_notes", {})
    if text:
        notes[str(line_id)] = text
    else:
        notes.pop(str(line_id), None)

    # Leere Strukturen wieder entfernen, damit die Datei schlank bleibt
    if not notes:
        entry.pop("line_notes", None)
    if not any(entry.values()):
        store["flags"].pop(name, None)

    save_store(store)
    return jsonify({"success": True, "order_name": name, "line_id": str(line_id), "hinweis": text})


@app.route("/api/orders/update", methods=["POST"])
def api_orders_update():
    """Incrementally syncs the local store from Shopify.

    First run (empty store) needs a start_date for the initial import; afterwards
    only orders updated since the last sync (updated_at_min, with overlap) are
    fetched and merged by order id."""
    data = request.get_json() or {}
    store = load_store()

    if store.get("last_update"):
        # 5 Min Überlappung, damit am Zeitfenster-Rand nichts verloren geht
        try:
            since = datetime.fromisoformat(store["last_update"]) - timedelta(minutes=5)
            updated_min = since.isoformat()
        except ValueError:
            updated_min = store["last_update"]
        extra = {"updated_at_min": updated_min}
    else:
        start_date = data.get("start_date")
        if not start_date:
            return jsonify({
                "error": "Erstbefüllung: bitte ein Startdatum angeben.",
                "needs_initial": True,
            }), 400
        extra = {"created_at_min": f"{start_date}T00:00:00+02:00"}
        store["initial_start"] = start_date

    api_orders, err = fetch_shopify_orders_params(extra)
    if err:
        return jsonify({"error": err}), 400

    new_count = 0
    upd_count = 0
    for o in api_orders:
        oid = str(o.get("id"))
        if not oid or oid == "None":
            continue
        if oid in store["orders"]:
            upd_count += 1
        else:
            new_count += 1
        # Transaktionen für die genaue Zahlungsart mitladen und einbetten.
        o["transactions"] = fetch_order_transactions(oid)
        store["orders"][oid] = o

    # Sync-Zeitpunkt in lokaler Zeit (+02:00, konsistent zur übrigen Datumslogik)
    store["last_update"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+02:00")
    save_store(store)

    return jsonify({
        "new": new_count,
        "updated": upd_count,
        "fetched": len(api_orders),
        "total": len(store["orders"]),
        "last_update": store["last_update"],
    })

@app.route("/imkerei")
def imkerei():
    """Renders the beekeeping order-category / delivery-list screen."""
    return render_template("imkerei.html")


@app.route("/api/imkerei/categories")
def api_imkerei_categories():
    """Splits the locally stored orders into beekeeping categories."""
    store = load_store()
    raw = store_orders_list(store)
    try:
        categories = categorize_orders(raw)
        return jsonify({
            "categories": categories,
            "order_count": len(raw),
            "last_update": store.get("last_update"),
        })
    except Exception as e:
        return jsonify({"error": f"Fehler beim Kategorisieren: {str(e)}"}), 500


@app.route("/api/imkerei/export")
def api_imkerei_export():
    """Builds a ZIP of one CSV per category from the locally stored orders."""
    store = load_store()
    raw = store_orders_list(store)
    categories = categorize_orders(raw)

    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, "w", zipfile.ZIP_DEFLATED) as zipf:
        for cat in categories:
            slug = cat["name"].lower().replace(" ", "_").replace("-", "_")
            buf = io.StringIO()
            # utf-8-sig BOM so Excel opens the umlauts correctly
            buf.write("﻿")
            writer = csv.writer(buf, delimiter=";")
            paid_str = lambda p: "Ja" if p else "Nein"
            if cat.get("mode") == "grouped":
                # Umlarvaktionen: nach Aktion gruppiert, je Aktion nach Uhrzeit
                writer.writerow(["Aktion", "Uhrzeit", "Kunde", "Bestellnr", "Menge", "Bezahlt"])
                for grp in cat["groups"]:
                    for s in grp["slots"]:
                        writer.writerow([grp["product"], s["time"], s["customer"], s["order"], s["qty"], paid_str(s.get("paid"))])
            elif cat.get("mode") == "delivery":
                # Königinnen / Belegstellen: je Position mit Lieferdatum/Termin
                is_auffahrt = cat["name"] == "Belegstellen-Auffahrt"
                term_header = "Termin" if is_auffahrt else "Lieferdatum"
                prod_header = "Belegstelle / Produkt" if is_auffahrt else "Sorte"
                writer.writerow([term_header, "Bestellnr", "Kunde", "Menge", prod_header, "Bezahlt"])
                for r in cat["rows"]:
                    writer.writerow([r["lieferdatum"] or "ohne Termin", r["order"], r["customer"], r["qty"], r["sorte"], paid_str(r.get("paid"))])
            else:
                writer.writerow(["Bestellnr", "Datum", "Kunde", "Menge", "Produkte", "Bezahlt"])
                for r in cat["rows"]:
                    writer.writerow([r["order"], r["date"], r["customer"], r["qty"], r["products"], paid_str(r.get("paid"))])
            zipf.writestr(f"liste_{slug}.csv", buf.getvalue())

    memory_file.seek(0)
    date_str = datetime.now().strftime("%Y-%m-%d")
    return send_file(
        memory_file,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"Imkerei_Listen_{date_str}.zip",
    )


@app.route("/api/generate_single", methods=["POST"])
def api_generate_single():
    """Generates a single PDF invoice."""
    data = request.get_json()
    if not data or "order" not in data or "next_beleg" not in data or "next_vorgang" not in data:
        return jsonify({"error": "Ungültige Parameter"}), 400
        
    order = data["order"]
    if kdnr_missing(order.get("kdnr")):
        return jsonify({"success": False, "msg": "Kundennummer fehlt – bitte zuerst eine Kundennummer erfassen."}), 400

    belegnummer_str = f"{BELEG_PREFIX}{BELEG_YEAR}-{data['next_beleg']}"
    vorgangsnummer_str = str(data["next_vorgang"])

    success, msg = generate_single_invoice(
        order, OUTPUT_DIR, belegnummer_str, vorgangsnummer_str,
        bearbeiter=session.get("bearbeiter"),
    )

    if success:
        save_invoice_order(belegnummer_str, order.get("order_name"))

    return jsonify({
        "success": success,
        "msg": msg
    })


@app.route("/api/regenerate_single", methods=["POST"])
def api_regenerate_single():
    """(Neu-)Erstellt die Rechnung für genau eine Bestellung.

    Vergibt die nächste freie Belegnummer und ersetzt – bei Erfolg – eine
    eventuell bereits vorhandene Rechnung derselben Bestellung (altes PDF wird
    gelöscht), damit das Archiv keine Duplikate enthält."""
    data = request.get_json() or {}
    order = data.get("order")
    if not order:
        return jsonify({"error": "Ungültige Parameter"}), 400
    if kdnr_missing(order.get("kdnr")):
        return jsonify({"success": False, "msg": "Kundennummer fehlt – bitte zuerst eine Kundennummer erfassen."}), 400

    name = order.get("order_name")
    belegnummer_str = f"{BELEG_PREFIX}{BELEG_YEAR}-{next_beleg_seq()}"
    _, next_vorgang = get_latest_sequences()
    vorgangsnummer_str = str(next_vorgang)

    success, msg = generate_single_invoice(
        order, OUTPUT_DIR, belegnummer_str, vorgangsnummer_str,
        bearbeiter=session.get("bearbeiter"),
    )
    if not success:
        # Fehlgeschlagen: vorhandene Rechnung bleibt unangetastet
        return jsonify({"success": False, "msg": msg})

    replaced = remove_invoice_orders_for(name, keep=belegnummer_str)
    save_invoice_order(belegnummer_str, name)

    return jsonify({
        "success": True,
        "belegnummer": belegnummer_str,
        "replaced": replaced,
        "msg": msg,
    })

@app.route("/archiv")
def archive():
    """Renders the generated invoices list."""
    invoices = []
    invoice_orders = load_invoice_orders()
    if os.path.exists(OUTPUT_DIR):
        for fname in sorted(os.listdir(OUTPUT_DIR)):
            if fname.lower().endswith(".pdf") and fname.startswith("Rechnung"):
                path = os.path.join(OUTPUT_DIR, fname)
                mtime = os.path.getmtime(path)
                created_str = datetime.fromtimestamp(mtime).strftime("%d.%m.%Y %H:%M")

                # Parse Belegnummer from filename e.g. "Rechnung 2026-100562.pdf" -> "2026-100562"
                beleg = fname.replace("Rechnung ", "").replace(".pdf", "")

                invoices.append({
                    "name": fname,
                    "beleg": beleg,
                    "bestellnr": invoice_orders.get(beleg, ""),
                    "created": created_str,
                    "mtime": mtime
                })
                
    # Sort by creation date descending
    invoices.sort(key=lambda x: x["mtime"], reverse=True)
    return render_template("archive.html", invoices=invoices)

@app.route("/api/invoices/<filename>")
def api_invoice_file(filename):
    """Serves a single generated PDF."""
    return send_from_directory(OUTPUT_DIR, filename)

@app.route("/api/delete_invoice/<filename>", methods=["POST"])
def api_delete_invoice(filename):
    """Deletes a single generated PDF."""
    path = os.path.join(OUTPUT_DIR, filename)
    if os.path.exists(path):
        try:
            os.remove(path)
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"error": "Datei nicht gefunden"}), 404

@app.route("/api/clear_archive", methods=["POST"])
def api_clear_archive():
    """Deletes all generated PDFs in the archive."""
    try:
        count = 0
        for fname in os.listdir(OUTPUT_DIR):
            if fname.lower().endswith(".pdf"):
                os.remove(os.path.join(OUTPUT_DIR, fname))
                count += 1
        return jsonify({"success": True, "count": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/download-zip")
def api_download_zip():
    """Compiles all generated PDFs into a single ZIP archive and serves it."""
    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for fname in os.listdir(OUTPUT_DIR):
            if fname.lower().endswith(".pdf"):
                path = os.path.join(OUTPUT_DIR, fname)
                zipf.write(path, arcname=fname)
                
    memory_file.seek(0)
    date_str = datetime.now().strftime("%Y-%m-%d")
    return send_file(
        memory_file,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f"Rechnungen_Export_{date_str}.zip"
    )

if __name__ == "__main__":
    import os
    # Bind to 0.0.0.0 to make the app reachable inside Docker and the network
    host = os.getenv("FLASK_HOST", "0.0.0.0")
    # Debug nur außerhalb der Produktion (der Werkzeug-Debugger erlaubt sonst
    # Remote-Code-Ausführung). docker-compose setzt FLASK_ENV=production -> debug aus.
    debug = os.getenv("FLASK_ENV", "").lower() != "production"
    print("=" * 80)
    print(f"RECHNUNGS-VERWALTUNG LAUFT AUF {host}:5000 (debug={debug})")
    print(f"Oeffne http://127.0.0.1:5000 oder http://<server-ip>:5050 in deinem Webbrowser.")
    print("=" * 80)
    app.run(host=host, port=5000, debug=debug)
