# config/extraction_rules/de.py
"""German extraction profile — the original default profile.

Keys must match the document_type names in your Paperless-ngx instance.
Targets the German legal/administrative domain.
"""

from config.extraction_rules.base import BASE_INSTRUCTIONS

RULES: dict[str, dict] = {
    # === RECHNUNGEN & BELEGE ===
    "Beitragsrechnung": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Beitragsrechnung (z.B. Versicherungsbeitrag, Mitgliedsbeitrag).
Achte besonders auf:
- Beitragszeitraum und Fälligkeitsdatum
- Beitragshöhe, Aufschlüsselung einzelner Posten
- Zahlungsempfänger und Bankverbindung
- Vertragsnummer / Versicherungsscheinnummer
- Änderungen gegenüber vorherigen Beiträgen
""",
    },
    "Rechnung": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Rechnung (z.B. Arztrechnung, Handwerkerrechnung, Händlerrechnung).
Achte besonders auf:
- Einzelpositionen mit Menge, Einzelpreis, Gesamtpreis
- GOZ/GOÄ-Ziffern bei ärztlichen/zahnärztlichen Rechnungen
- Steigerungsfaktoren bei Heilbehandlungen
- Rechnungsnummer und Rechnungsdatum
- Zahlungsfrist und Bankverbindung — Zahlungsfrist als Aktion mit Deadline erfassen
- Umsatzsteuer / MwSt wenn ausgewiesen
""",
    },
    "Kostenübersicht": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Kostenübersicht / Kostenaufstellung.
Achte besonders auf:
- Gegenstand und Zeitraum der Aufstellung
- Einzelposten vollständig als Tabelle erfassen
- Zwischensummen und Gesamtsumme
- Vergleichswerte oder Vorjahresangaben wenn vorhanden
""",
    },
    "Belege": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Beleg / Quittung.
Achte besonders auf:
- Betrag und Datum der Zahlung
- Zahlungsart (bar, Überweisung, Lastschrift)
- Verwendungszweck
- Zugehörige Rechnungs- oder Vorgangsnummer
""",
    },
    "Nachforderung": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Nachforderung / Nachberechnung.
Achte besonders auf:
- Ursprüngliche Rechnung / Vorgang auf den sich die Nachforderung bezieht
- Differenzbetrag und Begründung
- Zahlungsfrist (oft kurz!) — als Aktion mit Deadline erfassen
- Konsequenzen bei Nichtzahlung (Mahngebühren, Verzugszinsen)
""",
    },
    # === BESCHEIDE ===
    "Steuerbescheid": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Steuerbescheid (Einkommensteuer, Grundsteuer, etc.).
Achte besonders auf:
- Steuerart und Veranlagungszeitraum
- Festgesetzte Steuer, Nachzahlung oder Erstattung
- Abweichungen von der Steuererklärung mit Begründung
- Einspruchsfrist (normalerweise 1 Monat) — als Aktion mit Deadline erfassen
- Steuernummer und Aktenzeichen
- Bankverbindung für Nachzahlungen
""",
    },
    "Grundsteuerbescheid": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Grundsteuerbescheid.
Achte besonders auf:
- Grundstücksdaten (Flurstück, Adresse, Einheitswert)
- Festgesetzter Grundsteuerbetrag und Fälligkeitstermine
- Hebesatz der Gemeinde
- Einspruchsfrist — als Aktion mit Deadline erfassen
- Aktenzeichen / Steuernummer
""",
    },
    "Bescheid": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Allgemeiner Bescheid (Behörde, Versicherung, etc.).
Achte besonders auf:
- Bescheidgegenstand und Entscheidung
- Begründung der Entscheidung
- Rechtsmittelbelehrung: Widerspruchsfrist — als Aktion mit Deadline erfassen
- Aktenzeichen und Ansprechpartner
""",
    },
    "Kraftfahrzeugsteuerbescheid": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Kraftfahrzeugsteuerbescheid.
Achte besonders auf:
- Kennzeichen und Fahrzeugdaten (Hubraum, Emissionswerte)
- Festgesetzte Jahressteuer und Fälligkeit
- Einzugsermächtigung / Zahlungsweise
- Einspruchsfrist — als Aktion mit Deadline erfassen
- Aktenzeichen / Steuernummer
""",
    },
    # === KONTOAUSZÜGE & KREDIT ===
    "Kontoauszug": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Kontoauszug.
Achte besonders auf:
- Kontonummer / IBAN und Auszugsnummer
- Zeitraum des Auszugs
- Anfangs- und Endsaldo
- Einzelne Buchungen: Datum, Empfänger/Auftraggeber, Betrag, Verwendungszweck
- Auffällige oder ungewöhnlich hohe Posten
""",
    },
    "Kreditauszug": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Kreditauszug / Darlehensübersicht.
Achte besonders auf:
- Darlehensnummer und Kreditgeber
- Restschuld und Tilgungsstand
- Zinssatz und Zinsberechnung
- Nächste Ratenfälligkeit — als Aktion mit Deadline erfassen
- Sondertilgungsmöglichkeiten
""",
    },
    # === VERTRÄGE ===
    "Vertrag": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Vertrag (allgemein, z.B. Dienstleistungs-, Handy-, Streaming-Vertrag).
Achte besonders auf:
- Vertragsparteien und Vertragsgegenstand
- Vertragsnummer / Kundennummer
- Laufzeit, Verlängerungsklauseln und Kündigungsfristen
- Kosten, Zahlungsmodalitäten und Preisanpassungsklauseln
- Widerrufsrecht und -frist — als Aktion mit Deadline erfassen
- Besondere Vereinbarungen und Unterschriftsdaten
""",
    },
    "Arbeitsvertrag": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Arbeitsvertrag.
Achte besonders auf:
- Arbeitgeber und Arbeitnehmer
- Position / Tätigkeitsbeschreibung und Arbeitsort
- Beginn, Befristung und Probezeit
- Vergütung (Grundgehalt, Zulagen, Sonderzahlungen)
- Arbeitszeit und Urlaubsanspruch
- Kündigungsfristen und Nebentätigkeitsklauseln
""",
    },
    "Kreditvertrag": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Kreditvertrag / Darlehensvertrag.
Achte besonders auf:
- Darlehenssumme, Zinssatz (fest/variabel), Laufzeit
- Tilgungsmodalitäten und Ratenhöhe
- Sondertilgungsrechte und Vorfälligkeitsentschädigung
- Sicherheiten (Grundschuld, Bürgschaft)
- Widerrufsrecht und -frist — als Aktion mit Deadline erfassen
- Vertragspartner und Unterschriftsdaten
""",
    },
    "Kaufvertrag": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Kaufvertrag.
Achte besonders auf:
- Vertragsgegenstand (Beschreibung, Grundbuchdaten bei Immobilien)
- Kaufpreis und Zahlungsmodalitäten
- Übergabetermin — als Aktion mit Deadline erfassen
- Gewährleistung / Haftungsausschlüsse
- Rücktrittsrechte und Bedingungen
- Notarielle Beurkundung wenn vorhanden
""",
    },
    "Mietvertrag": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Mietvertrag.
Achte besonders auf:
- Mietobjekt (Adresse, Größe, Zimmeranzahl)
- Kaltmiete, Nebenkosten, Gesamtmiete
- Mietbeginn und Vertragslaufzeit / Kündigungsfristen
- Kaution (Höhe und Zahlungsmodalität)
- Schönheitsreparaturen und Instandhaltungspflichten
- Besondere Vereinbarungen
""",
    },
    # === VERSICHERUNGEN & GESUNDHEIT ===
    "Versicherungsschein": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Versicherungsschein / Police.
Achte besonders auf:
- Versicherer und Versicherungsscheinnummer
- Versicherungssparte (Haftpflicht, Hausrat, KFZ, Leben, Kranken, etc.)
- Versicherte Personen / Objekte / Risiken
- Versicherungsbeginn, Ablauf und Zahlweise
- Beitragshöhe und Fälligkeiten
- Deckungssummen, Leistungsumfang und Selbstbeteiligung
""",
    },
    "Gutachten": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Gutachten (ärztlich, technisch, Wertgutachten).
Achte besonders auf:
- Gutachtenart und Auftraggeber
- Untersuchungsgegenstand / Patient
- Befund und Diagnose
- Empfehlungen und Schlussfolgerungen
- Referenzen auf andere Gutachten oder Aktenzeichen
""",
    },
    "Untersuchungsbericht": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Untersuchungsbericht (ärztlich, Labor, technisch).
Achte besonders auf:
- Patient / Untersuchungsgegenstand und Untersuchungsdatum
- Art der Untersuchung und durchführende Stelle
- Messwerte und Laborwerte VOLLSTÄNDIG als Tabelle erfassen —
  inklusive Referenzbereichen und Einheiten
- Befunde und Diagnosen
- Empfehlungen und Folgetermine — konkrete Termine als Aktion mit Deadline erfassen
""",
    },
    "Meldebescheinigung zur Sozialversicherung": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Meldebescheinigung zur Sozialversicherung.
Achte besonders auf:
- Versicherungsnummer und Arbeitgeber
- Meldezeitraum und Meldegrund
- Beitragsgrundlage (Bruttolohn)
- Beitragsgruppen (KV, RV, AV, PV)
""",
    },
    # === KÜNDIGUNGEN ===
    "Kündigung": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Kündigung (ein- oder ausgehend).
Achte besonders auf:
- Wer kündigt was (Vertragsnummer, Vertragsgegenstand)
- Kündigungsdatum und gewünschtes Vertragsende
- Kündigungsgrund wenn angegeben
- Forderung nach Bestätigung — als Aktion erfassen
- Hinweise auf Kündigungsfristen
""",
    },
    "Kündigungsbestätigung": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Kündigungsbestätigung.
Achte besonders auf:
- Bestätigtes Vertragsende
- Vertragsnummer und Vertragsgegenstand
- Restforderungen oder Guthaben
- Rückgabepflichten — als Aktion mit Deadline erfassen
""",
    },
    # === STEUERN & SPENDEN ===
    "Steuererklärung": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Steuererklärung (inkl. Anlagen).
Achte besonders auf:
- Steuerjahr und Steuerart
- Steuernummer / Steuer-ID der Steuerpflichtigen
- Welche Anlagen enthalten sind (Anlage N, KAP, V, Kind, etc.)
- Erklärte Einkünfte je Einkunftsart
- Werbungskosten, Sonderausgaben, außergewöhnliche Belastungen
- Formularfelder mit Zeilennummern wörtlich als "Zeile N: Wert" erfassen
""",
    },
    "Steuerbescheinigung": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Steuerbescheinigung (z.B. Jahressteuerbescheinigung der Bank).
Achte besonders auf:
- Ausstellendes Institut und Depot-/Kontonummer
- Steuerjahr
- Höhe der Kapitalerträge und deren Aufschlüsselung
- Einbehaltene Kapitalertragsteuer, Solidaritätszuschlag, Kirchensteuer
- In Anspruch genommener Sparer-Pauschbetrag / Freistellungsauftrag
- Verlustverrechnungstöpfe wenn ausgewiesen
""",
    },
    "Lohnsteuerbescheinigung": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Lohnsteuerbescheinigung.
Achte besonders auf:
- Arbeitgeber und Kalenderjahr / Beschäftigungszeitraum
- Steuer-ID / eTIN und Steuerklasse
- Bruttoarbeitslohn (Zeile 3) und alle nummerierten Zeilen wörtlich
  als "Zeile N: Wert" erfassen
- Einbehaltene Lohnsteuer, Solidaritätszuschlag, Kirchensteuer
- Arbeitgeber- und Arbeitnehmeranteile zur Sozialversicherung
""",
    },
    "Spendenbescheinigung": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Spendenbescheinigung / Zuwendungsbestätigung.
Achte besonders auf:
- Empfängerorganisation und Steuernummer des Empfängers
- Spendenbetrag und -datum
- Art der Zuwendung (Geldzuwendung / Sachzuwendung)
- Hinweis auf steuerliche Absetzbarkeit
""",
    },
    # === WOHNEN ===
    "Nebenkostenabrechnung": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Nebenkostenabrechnung / Betriebskostenabrechnung.
Achte besonders auf:
- Abrechnungszeitraum und Mietobjekt
- Einzelne Kostenarten mit Gesamtkosten und Verteilerschlüssel
- Vorauszahlungen vs. tatsächliche Kosten
- Nachzahlung oder Guthaben — Betrag und Fälligkeit als Aktion mit Deadline
- Einspruchsfrist (12 Monate) — als Aktion mit Deadline erfassen
""",
    },
    "Wohnungsübergabeprotokoll": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Wohnungsübergabeprotokoll.
Achte besonders auf:
- Mietobjekt und Übergabedatum
- Anwesende Parteien (Mieter, Vermieter, Zeugen)
- Zählerstände (Strom, Gas, Wasser, Heizung) als Tabelle erfassen
- Zustand und Mängel je Raum
- Anzahl übergebener Schlüssel
- Vereinbarte Nacharbeiten — als Aktion erfassen wenn mit Frist
""",
    },
    "Grundbuchauszug": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Grundbuchauszug.
Achte besonders auf:
- Amtsgericht, Grundbuch von, Blattnummer
- Bestandsverzeichnis: Gemarkung, Flur, Flurstück, Größe, Lage
- Abteilung I: Eigentümer und Erwerbsgrund
- Abteilung II: Lasten und Beschränkungen (Wegerechte, Wohnrechte, Vormerkungen)
- Abteilung III: Grundschulden / Hypotheken mit Beträgen und Gläubigern
- Eintragungs- und Löschungsdaten
""",
    },
    "Grundschuldbestellung": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Grundschuldbestellung (notarielle Urkunde).
Achte besonders auf:
- Grundschuldbetrag und Grundschuldzinsen
- Gläubiger (Bank) und Eigentümer / Besteller
- Belastetes Grundstück (Grundbuch, Flurstück)
- Notar und Urkundenrollennummer
- Unterwerfung unter die sofortige Zwangsvollstreckung
- Zweckerklärung / gesicherte Forderung
""",
    },
    # === FAHRZEUG ===
    "Fahrzeugbrief": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Fahrzeugbrief / Zulassungsbescheinigung Teil II.
Achte besonders auf:
- Fahrzeug-Identifizierungsnummer (FIN)
- Dokumentnummer der Zulassungsbescheinigung
- Hersteller, Typ und Handelsbezeichnung
- Aktueller Halter und frühere Halter
- Erstzulassungsdatum
- Alle Feldcodes wörtlich als "Feldcode: Wert" erfassen
""",
    },
    "Fahrzeugschein": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Fahrzeugschein / Zulassungsbescheinigung Teil I.
Achte besonders auf:
- Kennzeichen und Fahrzeug-Identifizierungsnummer (FIN)
- Halter (Name, Anschrift)
- Technische Daten mit Feldcodes wörtlich als "Feldcode: Wert" erfassen
  (z.B. "P.2: 110 kW", "F.1: 2280 kg")
- Erstzulassung und Zulassungsdatum
- Nächster HU-Termin — als Aktion mit Deadline erfassen
""",
    },
    # === ARBEIT & GEHALT ===
    "Gehaltsinformation": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Gehaltsinformation / Gehaltsabrechnung.
Achte besonders auf:
- Arbeitgeber und Abrechnungsmonat / -zeitraum
- Bruttobezüge mit allen Einzelpositionen (Grundgehalt, Zulagen, Sonderzahlungen)
- Abzüge: Lohnsteuer, Solidaritätszuschlag, Kirchensteuer, SV-Beiträge
- Nettobetrag und Auszahlungsbetrag
- Steuerklasse und SV-Nummer
""",
    },
    "Zwischenzeugnis": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Zwischenzeugnis / Arbeitszeugnis.
Achte besonders auf:
- Arbeitgeber und Position des Arbeitnehmers
- Beschäftigungszeitraum und Tätigkeitsbeschreibung
- Leistungs- und Verhaltensbeurteilung WÖRTLICH übernehmen
  (Zeugnissprache nicht umformulieren oder interpretieren)
- Anlass der Ausstellung
- Ausstellungsdatum und Unterzeichner
""",
    },
    "Dienstreise": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Dienstreise (Reisekostenabrechnung, Genehmigung).
Achte besonders auf:
- Reisender, Reiseziel und Reisezweck
- Reisezeitraum (Beginn und Ende mit Uhrzeiten)
- Kostenpositionen (Fahrt, Übernachtung, Verpflegungspauschalen) als Tabelle
- Erstattungsbetrag und Genehmigungen
""",
    },
    # === SONSTIGES ===
    "Bescheinigung": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Bescheinigung (allgemein).
Achte besonders auf:
- Ausstellende Stelle und Ausstellungsdatum
- Bescheinigter Sachverhalt und betroffene Person
- Geltungszeitraum / Gültigkeitsdauer
- Zweck der Bescheinigung (z.B. zur Vorlage bei ...)
- Referenz- und Aktenzeichen
""",
    },
    "Vollmacht": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Vollmacht.
Achte besonders auf:
- Vollmachtgeber und Bevollmächtigter
- Art und Umfang (General-, Vorsorge-, Bank-, Einzelvollmacht)
- Geltungsbeginn, Befristung und Widerrufsregelung
- Notarielle Beurkundung / Beglaubigung wenn vorhanden
- Datum und Unterschriften
""",
    },
    "Eintragungsbekanntmachung": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Eintragungsbekanntmachung (Patent- / Markenregister).
Achte besonders auf:
- Art des Schutzrechts (Patent, Gebrauchsmuster, Marke)
- Aktenzeichen / Registernummer / Patentnummer
- Inhaber und Erfinder
- Titel / Bezeichnung und Klassifikation
- Anmelde-, Eintragungs- und Veröffentlichungstag
- Laufende Fristen (Jahresgebühren) — als Aktion mit Deadline erfassen
""",
    },
    "Information": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Informationsschreiben (Versicherung, Bank, Behörde, etc.).
Achte besonders auf:
- Kernaussage / Mitteilung des Schreibens
- Änderungen die den Empfänger betreffen (Beitragsanpassung, Vertragsänderung, etc.)
- Handlungsaufforderungen und Fristen — als Aktionen mit Deadlines erfassen
- Verweise auf andere Schriftstücke
""",
    },
    "Berechnung": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Berechnung / Abrechnung.
Achte besonders auf:
- Berechnungsgegenstand und -zeitraum
- Einzelne Berechnungsschritte und -positionen
- Endergebnis / Summe
- Vergleich mit Vorperioden wenn vorhanden
""",
    },
    "Bericht": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Bericht.
Achte besonders auf:
- Berichtsgegenstand und -zeitraum
- Zentrale Ergebnisse und Empfehlungen
- Daten, Tabellen und Grafiken besonders sorgfältig in Prosa umwandeln
""",
    },
    "Formular": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Formular (ausgefüllt oder leer).
Achte besonders auf:
- Art des Formulars und ausstellende Stelle
- Ausgefüllte Felder: Feldname und eingetragener Wert als Paare erfassen
- Unterschriften und Datumsangaben
- Ankreuzfelder: Welche Optionen sind ausgewählt?
""",
    },
    "Patentinformation": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Patentinformation / gewerblicher Rechtsschutz.
Achte besonders auf:
- Patentnummer / Aktenzeichen / Anmeldenummer
- Anmelder und Erfinder
- Titel und Klassifikation der Erfindung
- Fristen (Jahresgebühren, Einspruchsfristen) — als Aktionen mit Deadlines erfassen
- Verweise auf verwandte Patente oder Entgegenhaltungen
""",
    },
    "Plan": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: Plan (Bauplan, Versicherungsplan, Tarif-/Leistungsplan).
Achte besonders auf:
- Art des Plans und Geltungszeitraum
- Enthaltene Leistungen, Konditionen oder Maßnahmen
- Grafiken und Diagramme besonders sorgfältig beschreiben
""",
    },
    "SEPA-Lastschriftmandat": {
        "prompt": BASE_INSTRUCTIONS
        + """
Dokumententyp: SEPA-Lastschriftmandat.
Achte besonders auf:
- Mandatsreferenz und Gläubiger-ID
- IBAN und Kontoinhaber
- Zahlungsempfänger
- Art des Mandats (einmalig / wiederkehrend)
- Datum der Erteilung
""",
    },
    # === DEFAULT ===
    "_default": {
        "prompt": BASE_INSTRUCTIONS
        + """
Der Dokumententyp ist nicht vorklassifiziert. Bestimme den Dokumententyp
anhand des Inhalts und trage ihn im Feld "document_type" ein.
Achte allgemein auf:
- Absender, Empfänger, Datum
- Jegliche Fristen und Handlungsaufforderungen
- Vertrags- und Aktenzeichen
- Finanzielle Beträge und Zahlungsmodalitäten
""",
    },
}
