#!/usr/bin/env python3
import os
import zipfile
import io
import shutil
from flask import Flask, render_template, request, jsonify, send_from_directory, send_file
from datetime import datetime

# Import refactored functions from generate_invoices.py
from generate_invoices import (
    generate_single_invoice,
    get_latest_sequences,
    fetch_shopify_orders_from_api,
    parse_shopify_api_orders,
    OUTPUT_DIR,
    WORKSPACE_DIR
)

app = Flask(__name__)
app.secret_key = "bee_invoice_secret_key"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# Ensure static folder exists and contains logo.png
STATIC_FOLDER = os.path.join(WORKSPACE_DIR, "static")
os.makedirs(STATIC_FOLDER, exist_ok=True)
if os.path.exists(os.path.join(WORKSPACE_DIR, "logo.png")):
    shutil.copy(os.path.join(WORKSPACE_DIR, "logo.png"), os.path.join(STATIC_FOLDER, "logo.png"))

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

@app.route("/api/shopify/fetch", methods=["POST"])
def api_shopify_fetch():
    """Fetches orders directly from Shopify via Admin API."""
    data = request.get_json() or {}
    start_date = data.get("start_date")
    end_date = data.get("end_date")
    status = data.get("status", "any")
    
    if not start_date or not end_date:
        return jsonify({"error": "Startdatum und Enddatum sind erforderlich."}), 400
        
    api_orders, err = fetch_shopify_orders_from_api(start_date, end_date, status)
    if err:
        return jsonify({"error": err}), 400
        
    try:
        parsed_orders = parse_shopify_api_orders(api_orders)
        return jsonify({
            "orders": parsed_orders,
            "count": len(parsed_orders)
        })
    except Exception as e:
        return jsonify({"error": f"Fehler beim Verarbeiten der Shopify-Daten: {str(e)}"}), 500

@app.route("/api/generate_single", methods=["POST"])
def api_generate_single():
    """Generates a single PDF invoice."""
    data = request.get_json()
    if not data or "order" not in data or "next_beleg" not in data or "next_vorgang" not in data:
        return jsonify({"error": "Ungültige Parameter"}), 400
        
    order = data["order"]
    belegnummer_str = f"2026-{data['next_beleg']}"
    vorgangsnummer_str = str(data["next_vorgang"])
    
    success, msg = generate_single_invoice(order, OUTPUT_DIR, belegnummer_str, vorgangsnummer_str)
    
    return jsonify({
        "success": success,
        "msg": msg
    })

@app.route("/archiv")
def archive():
    """Renders the generated invoices list."""
    invoices = []
    if os.path.exists(OUTPUT_DIR):
        for fname in sorted(os.listdir(OUTPUT_DIR)):
            if fname.lower().endswith(".pdf") and fname.startswith("Rechnung"):
                path = os.path.join(OUTPUT_DIR, fname)
                size_kb = round(os.path.getsize(path) / 1024, 1)
                mtime = os.path.getmtime(path)
                created_str = datetime.fromtimestamp(mtime).strftime("%d.%m.%Y %H:%M")
                
                # Parse Belegnummer from filename e.g. "Rechnung 2026-100562.pdf" -> "2026-100562"
                beleg = fname.replace("Rechnung ", "").replace(".pdf", "")
                
                invoices.append({
                    "name": fname,
                    "beleg": beleg,
                    "size": size_kb,
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
    print("=" * 80)
    print(f"RECHNUNGS-VERWALTUNG LAUFT AUF {host}:5000")
    print(f"Oeffne http://127.0.0.1:5000 oder http://<server-ip>:5050 in deinem Webbrowser.")
    print("=" * 80)
    app.run(host=host, port=5000, debug=True)
