import os
import re
from datetime import datetime

import pdfplumber
import pyodbc
from dotenv import load_dotenv
from tabulate import tabulate

load_dotenv(os.path.join(os.path.dirname(__file__), "env"))

RECHNUNGEN_DIR = os.path.join(os.path.dirname(__file__), "Rechnungen")
SQL_DIR = os.path.join(os.path.dirname(__file__), "create_tables.sql")


def parse_rechnung(pdf_path):
    """Extrahiert die relevanten Daten aus einer Rechnungs-PDF."""
    with pdfplumber.open(pdf_path) as pdf:
        text = pdf.pages[0].extract_text()

    if not text:
        return None

    def extract(pattern, default=""):
        m = re.search(pattern, text)
        return m.group(1).strip() if m else default

    # Kopfdaten
    vorgangsnummer = extract(r"Vorgangsnummer\s+(\d+)")
    belegnummer = extract(r"Belegnummer\s+([\d\-]+)")
    datum = extract(r"Datum\s+([\d.]+)")
    kundennummer = extract(r"Kundennummer\s+(\S+)")
    bearbeiter = extract(r"Bearbeiter(?:\*in)?\s+(.+?)(?:\n|$)")

    # Kundenname: erste Zeile nach der Absenderzeile (vor "Rechnung")
    kunde = ""
    match_kunde = re.search(
        r"4040 Linz\n(.+?)\s+Rechnung", text
    )
    if match_kunde:
        kunde = match_kunde.group(1).strip()

    # Endsumme
    endsumme = extract(r"Endsumme\s+EUR\s+([\d.,]+)")

    # MwSt-Satz und Betrag
    mwst_match = re.search(
        r"MwSt\.\s+mit\s+Steuercode\s+\d+\s+([\d.,]+)\s+%\s+von\s+([\d.,]+)\s+([\d.,]+)",
        text,
    )
    mwst_satz = mwst_match.group(1) if mwst_match else "0,00"
    mwst_betrag = mwst_match.group(3) if mwst_match else "0,00"

    # Positionen aus der Tabelle extrahieren
    # Format: "1 102858 Bezeichnung 14.11.2025 1x 75,50 75,50 0"
    # oder:   "1 102859 Bezeichnung 30.01.2026 1 St 80,00 80,00 1"
    positionen = []
    pos_pattern = re.findall(
        r"^(\d+)\s+(\d{5,6})\s+(.+?)\s+(\d{2}\.\d{2}\.\d{4})\s+(\d+\s*\w+)\s+([\d.,]+)\s+([\d.,]+)\s+\d+",
        text,
        re.MULTILINE,
    )
    for p in pos_pattern:
        bezeichnung = re.sub(r"\s*€\s*$", "", p[2].strip())
        positionen.append({
            "Pos": p[0],
            "Artikelnr": p[1],
            "Bezeichnung": bezeichnung,
            "Termin": p[3],
            "Menge": p[4].strip(),
            "Einzelpreis": p[5],
            "Gesamtpreis": p[6],
        })

    return {
        "Datei": os.path.basename(pdf_path),
        "Pfad": pdf_path,
        "Vorgangsnummer": vorgangsnummer,
        "Belegnummer": belegnummer,
        "Datum": datum,
        "Kundennummer": kundennummer,
        "Kunde": kunde,
        "Bearbeiter": bearbeiter,
        "MwSt-Satz": f"{mwst_satz} %",
        "MwSt-Betrag": f"{mwst_betrag} EUR",
        "Endsumme": f"{endsumme} EUR",
        "Positionen": positionen,
    }


def get_sql_connection():
    """Stellt eine Verbindung zur SQL-Server-Datenbank her."""
    driver = os.getenv("SQL_DRIVER", "ODBC Driver 17 for SQL Server")
    if not driver.startswith("{"):
        driver = f"{{{driver}}}"
    conn_str = (
        f"DRIVER={driver};"
        f"SERVER={os.getenv('SQL_SERVER')},{os.getenv('SQL_PORT')};"
        f"DATABASE={os.getenv('SQL_DATABASE')};"
        f"UID={os.getenv('SQL_USER')};"
        f"PWD={os.getenv('SQL_PASSWORD')};"
        f"TrustServerCertificate=yes;"
    )
    return pyodbc.connect(conn_str)


def parse_german_decimal(value):
    """Wandelt '75,50' oder '1.234,56' in einen float um."""
    return float(value.replace(".", "").replace(",", "."))


def parse_german_date(value):
    """Wandelt '20.01.2026' in ein datetime.date um."""
    return datetime.strptime(value, "%d.%m.%Y").date()


def create_tables(conn):
    """Erstellt die Tabellen falls sie noch nicht existieren."""
    with open(SQL_DIR, "r") as f:
        sql = f.read()
    cursor = conn.cursor()
    # SQL-Skript enthält mehrere Batches (IF...BEGIN...END)
    for batch in sql.split("END;"):
        batch = batch.strip()
        if batch:
            cursor.execute(batch + " END;")
    conn.commit()
    print("Tabellen geprüft/erstellt.")


def save_to_database(conn, rechnungen):
    """Speichert die geparsten Rechnungsdaten in die Datenbank."""
    cursor = conn.cursor()
    eingefuegt = 0
    uebersprungen = 0

    for r in rechnungen:
        # Prüfen ob Rechnung bereits existiert
        cursor.execute(
            "SELECT COUNT(*) FROM [dbo].[Rechnungen_Import] WHERE [Belegnummer] = ?",
            r["Belegnummer"],
        )
        if cursor.fetchone()[0] > 0:
            uebersprungen += 1
            continue

        # PDF-Datei als Binärdaten lesen
        with open(r["Pfad"], "rb") as f:
            pdf_daten = f.read()

        # Rechnungskopf einfügen
        endsumme = parse_german_decimal(r["Endsumme"].replace(" EUR", ""))
        mwst_satz = parse_german_decimal(r["MwSt-Satz"].replace(" %", ""))
        mwst_betrag = parse_german_decimal(r["MwSt-Betrag"].replace(" EUR", ""))
        datum = parse_german_date(r["Datum"])

        cursor.execute(
            """INSERT INTO [dbo].[Rechnungen_Import]
               ([Belegnummer], [Vorgangsnummer], [Datum], [Kundennummer],
                [Kunde], [Bearbeiter], [MwStSatz], [MwStBetrag], [Endsumme], [Dateiname], [PDFDaten])
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            r["Belegnummer"],
            r["Vorgangsnummer"],
            datum,
            r["Kundennummer"],
            r["Kunde"],
            r["Bearbeiter"],
            mwst_satz,
            mwst_betrag,
            endsumme,
            r["Datei"],
            pyodbc.Binary(pdf_daten),
        )

        # Positionen einfügen
        for pos in r["Positionen"]:
            termin = parse_german_date(pos["Termin"]) if pos["Termin"] else None
            cursor.execute(
                """INSERT INTO [dbo].[Rechnungen_Import_Positionen]
                   ([Belegnummer], [Position], [Artikelnummer], [Bezeichnung],
                    [Termin], [Menge], [Einzelpreis], [Gesamtpreis])
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                r["Belegnummer"],
                int(pos["Pos"]),
                pos["Artikelnr"],
                pos["Bezeichnung"],
                termin,
                pos["Menge"],
                parse_german_decimal(pos["Einzelpreis"]),
                parse_german_decimal(pos["Gesamtpreis"]),
            )

        eingefuegt += 1

    conn.commit()
    print(f"\nDatenbank: {eingefuegt} Rechnungen eingefügt, {uebersprungen} bereits vorhanden.")


def main():
    if not os.path.isdir(RECHNUNGEN_DIR):
        print(f"Verzeichnis '{RECHNUNGEN_DIR}' nicht gefunden.")
        return

    pdf_files = sorted(
        f for f in os.listdir(RECHNUNGEN_DIR) if f.lower().endswith(".pdf")
    )

    if not pdf_files:
        print("Keine PDF-Dateien im Rechnungen-Verzeichnis gefunden.")
        return

    rechnungen = []
    alle_positionen = []

    for filename in pdf_files:
        pfad = os.path.join(RECHNUNGEN_DIR, filename)
        daten = parse_rechnung(pfad)
        if daten:
            rechnungen.append(daten)
            for pos in daten["Positionen"]:
                alle_positionen.append({
                    "Belegnr.": daten["Belegnummer"],
                    **pos,
                })

    # Übersichtstabelle
    uebersicht = []
    for r in rechnungen:
        uebersicht.append([
            r["Belegnummer"],
            r["Datum"],
            r["Kundennummer"],
            r["Kunde"],
            r["Bearbeiter"],
            r["MwSt-Satz"],
            r["MwSt-Betrag"],
            r["Endsumme"],
        ])

    print("=" * 120)
    print("RECHNUNGSÜBERSICHT")
    print("=" * 120)
    print(tabulate(
        uebersicht,
        headers=[
            "Belegnummer", "Datum", "KdNr", "Kunde",
            "Bearbeiter*in", "MwSt-Satz", "MwSt-Betrag", "Endsumme",
        ],
        tablefmt="grid",
    ))

    # Gesamtsumme
    gesamt = sum(
        float(r["Endsumme"].replace(" EUR", "").replace(".", "").replace(",", "."))
        for r in rechnungen
    )
    print(f"\nGesamtsumme aller Rechnungen: {gesamt:,.2f} EUR".replace(",", "X").replace(".", ",").replace("X", "."))

    # Positionstabelle
    print("\n" + "=" * 120)
    print("POSITIONEN (DETAIL)")
    print("=" * 120)
    pos_rows = []
    for p in alle_positionen:
        pos_rows.append([
            p["Belegnr."],
            p["Pos"],
            p["Artikelnr"],
            p["Bezeichnung"],
            p["Termin"],
            p["Menge"],
            p["Einzelpreis"],
            p["Gesamtpreis"],
        ])
    print(tabulate(
        pos_rows,
        headers=[
            "Belegnr.", "Pos", "Artikelnr.", "Bezeichnung",
            "Termin", "Menge", "Einzelpreis", "Gesamtpreis",
        ],
        tablefmt="grid",
    ))

    print(f"\nInsgesamt {len(rechnungen)} Rechnungen mit {len(alle_positionen)} Positionen analysiert.")

    # In Datenbank speichern
    print("\n" + "=" * 120)
    print("DATENBANK-IMPORT")
    print("=" * 120)
    try:
        conn = get_sql_connection()
        create_tables(conn)
        save_to_database(conn, rechnungen)
        conn.close()
    except pyodbc.Error as e:
        print(f"Datenbankfehler: {e}")
    except Exception as e:
        print(f"Fehler: {e}")


if __name__ == "__main__":
    main()
