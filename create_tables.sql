-- =====================================================
-- Tabellen für Rechnungsdaten aus PDF-Import
-- Datenbank: BZV
-- =====================================================

-- Rechnungskopf: eine Zeile pro Rechnung
-- Kundennummer als Index für schnelle Zuordnung
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'[dbo].[Rechnungen_Import]') AND type = 'U')
BEGIN
    CREATE TABLE [dbo].[Rechnungen_Import] (
        [ID]              INT IDENTITY(1,1) PRIMARY KEY,
        [Belegnummer]     NVARCHAR(20)   NOT NULL UNIQUE,
        [Vorgangsnummer]  NVARCHAR(20)   NOT NULL,
        [Datum]           DATE           NOT NULL,
        [Kundennummer]    NVARCHAR(20)   NOT NULL,
        [Kunde]           NVARCHAR(200)  NULL,
        [Bearbeiter]      NVARCHAR(100)  NULL,
        [MwStSatz]        DECIMAL(5,2)   NOT NULL DEFAULT 0,
        [MwStBetrag]      DECIMAL(10,2)  NOT NULL DEFAULT 0,
        [Endsumme]        DECIMAL(10,2)  NOT NULL,
        [Dateiname]       NVARCHAR(255)  NULL,
        [PDFDaten]        VARBINARY(MAX) NULL,
        [ImportDatum]     DATETIME       NOT NULL DEFAULT GETDATE()
    );

    CREATE INDEX IX_Rechnungen_Import_Kundennummer
        ON [dbo].[Rechnungen_Import] ([Kundennummer]);

    CREATE INDEX IX_Rechnungen_Import_Datum
        ON [dbo].[Rechnungen_Import] ([Datum]);
END;

-- Rechnungspositionen: mehrere Zeilen pro Rechnung
IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id = OBJECT_ID(N'[dbo].[Rechnungen_Import_Positionen]') AND type = 'U')
BEGIN
    CREATE TABLE [dbo].[Rechnungen_Import_Positionen] (
        [ID]              INT IDENTITY(1,1) PRIMARY KEY,
        [Belegnummer]     NVARCHAR(20)   NOT NULL,
        [Position]        INT            NOT NULL,
        [Artikelnummer]   NVARCHAR(20)   NOT NULL,
        [Bezeichnung]     NVARCHAR(500)  NOT NULL,
        [Termin]          DATE           NULL,
        [Menge]           NVARCHAR(20)   NOT NULL,
        [Einzelpreis]     DECIMAL(10,2)  NOT NULL,
        [Gesamtpreis]     DECIMAL(10,2)  NOT NULL,

        CONSTRAINT FK_Positionen_Rechnung
            FOREIGN KEY ([Belegnummer])
            REFERENCES [dbo].[Rechnungen_Import] ([Belegnummer])
            ON DELETE CASCADE
    );

    CREATE INDEX IX_Positionen_Belegnummer
        ON [dbo].[Rechnungen_Import_Positionen] ([Belegnummer]);
END;

-- Migration: PDFDaten-Spalte nachträglich hinzufügen (falls Tabelle schon existiert)
IF NOT EXISTS (SELECT * FROM sys.columns WHERE object_id = OBJECT_ID(N'[dbo].[Rechnungen_Import]') AND name = 'PDFDaten')
BEGIN
    ALTER TABLE [dbo].[Rechnungen_Import]
        ADD [PDFDaten] VARBINARY(MAX) NULL;
END;
