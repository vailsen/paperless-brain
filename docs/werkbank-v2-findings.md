# Werkbank v2 — Phase 0: Bestandsaufnahme

Diagnose vor dem Umbau. Kein Code geändert. Begleitet
`docs/werkbank-architecture.md` (Warum) und `docs/werkbank-tasks.md` (Was).

---

## 1. Bestehendes Modul

4 631 Zeilen in `werkbank/`.

| Datei | Zeilen | v2 |
|---|---|---|
| `llm_lane.py` | 677 | **wiederverwenden** — Lanes/Semaphore, `create_llm`, `complete_structured` (JSON-Schema-Zwang) sind genau das, was v2 braucht |
| `repository.py` | 431 | **Muster wiederverwenden**, Schema ersetzen (SQLite+WAL, User-Scoping auf jeder Query) |
| `ui/task_dialog.py` | 829 | **erweitern** — Board, Live-Refresh, Review-vor-Persist bleiben |
| `ui/archetype_dialog.py` | 181 | **behalten** — CRUD über Archetypen bleibt Nutzerfunktion |
| `ui/module_page.py` | 202 | behalten |
| `scheduler.py` | 110 | **ersetzen** — v2 arbeitet Level-weise über einen DAG |
| `orchestrator.py` | 332 | **ersetzen** — kennt nur Prosa-Ergebnisse |
| `roles/splitter.py` | 466 | **ersetzen** durch PLANNER + Code-Validierung |
| `roles/worker.py` | 191 | **ersetzen** durch RUNNER mit `SubtaskResult` |
| `roles/critic.py` | 129 | **ersetzen** durch FACT_CRITIC (sieht das `narrative` nicht) |
| `roles/synthesizer.py` | 78 | **ersetzen** durch WRITER + Reflexions-Generator |
| `models.py` | 87 | **ersetzen** — Dataclasses ohne Facts |
| `compaction.py` | 28 | **entfällt** — v2 gibt Facts weiter, nicht komprimierte Prosa |
| `prechecks.py` | 90 | prüfen, ob in die deterministischen Checks aufgeht |
| `export.py` | 182 | behalten (Paperless-Ablage) |
| `settings_store.py` | 251 | behalten, um Rollen-Prompts erweitern |

Der Bruch verläuft sauber entlang der Rollen: **Infrastruktur bleibt, Kognition
wird ersetzt.** v2 entsteht als `werkbank/v2/`, v1 bleibt bis Phase 7 lauffähig.

---

## 2. Tool-Inventar

25 Tools in `TOOL_DEFINITIONS`. Das Architekturdokument entstand vor diesem
Stand, deshalb hier die vollständige Zuordnung.

### 2.1 Quotierbarkeit (entscheidend für D2)

D2 vergleicht das Belegzitat gegen den **tatsächlich abgerufenen Text**. Das
setzt voraus, dass es diesen Text gibt.

| Tool | Rückgabe | Zitierbar? |
|---|---|---|
| `get_document_page_text` | Seitentext aus der Vision-Extraktion | **ja** — die beste Quelle im System |
| `web_fetch_page` | Volltext via trafilatura/Crawl4AI | **ja** |
| `vault_search` | Chunk-Text der Notiz | **ja** |
| `search` / `search_exact` | Trefferliste + `matched_chunks` | **ja, nur die Chunks** — die Kopfzeilen sind generiert |
| `get_document_table` | Tabelle aus dem Sidecar | ja, als `kind: "table"` |
| `search_emails` | Betreff + Textausschnitt | ja |
| `search_calendar` | Termin-Metadaten | nein → `computed` |
| `get_document_details` | **LLM-Zusammenfassung**, auf 500 Zeichen gekürzt | **nein** (siehe 2.2) |
| `get_actions` | aus dem Sidecar extrahierte Fristen | nein → `derived` |
| `calculate` | Rechenergebnis | nein → `computed` |

### 2.2 Befund: nicht jedes Paperless-Ergebnis ist `authoritative`

`get_document_details` liefert `full_summary_summarized` — eine
**Vision-LLM-Zusammenfassung des Dokuments**, gekürzt auf 500 Zeichen
(`services/chat_service.py:1524`). Ein Zitat daraus ist ein Zitat aus der
Paraphrase eines Modells, nicht aus dem Dokument.

Das Architekturdokument (§5) setzt "Paperless-Dokumente → `authoritative`"
pauschal. Das ist zu grob. Vorschlag für v2:

| Quelle | Trust |
|---|---|
| `get_document_page_text`, `search`-Chunks, `get_document_table` | `authoritative` |
| `get_document_details` (Summary), `get_actions` | `derived` — Extraktion, kein Wortlaut |
| Paperless-Notizen | `user_asserted` |

Damit steht im Bericht sichtbar, ob eine Zahl im Dokument stand oder ob ein
Modell sie beim Ingest herausgelesen hat.

### 2.3 Blocker für D2

`execute_tool()` gibt `(text_for_llm, docs, extras)` zurück
(`services/chat_service.py:937`) — eine **für das Modell formatierte
Zeichenkette**, keinen Rohtext. Für D2 fehlt die Ebene darunter.

**Konsequenz:** v2 ruft die Tools nicht über `execute_tool`, sondern über eine
eigene dünne Schicht (`werkbank/v2/tools.py`), die pro Call ablegt:
`tool`, `args`, `raw_text`, `trust`, `retrieved_at`, `ref`, `hits`. Das ist
gleichzeitig die Stelle, an der `trust` gesetzt wird (§4.2 der Tasks: im
Wrapper, nicht im Prompt) — und die einzige Stelle, die dafür in Frage kommt.

Kein Blocker für Phase 1, zwingend vor Phase 4.

### 2.4 Tool-Verteilung auf die Archetypen

Auswahlregel: **Quelle und epistemischer Modus, nie Thema** (§6).

| Agent | Tools |
|---|---|
| `doc_researcher` | `search`, `search_exact`, `get_document_page_text`, `get_document_table`, `get_document_details`, `get_actions`, `vault_search`, `search_memory`, `calculate`, `get_current_date` |
| `web_researcher` | `web_search`, `web_fetch_page`, `calculate`, `get_current_date` |
| `comms_researcher` | `search_emails`, `search_calendar`, `calculate`, `get_current_date` |
| `synthesizer` | `calculate`, `get_current_date` |
| `contradiction_checker` | `get_current_date` |

**Aus allen Agenten ausgeschlossen, mit Begründung:**

- `create_note`, `create_deadline`, `remember_fact`, `update_brain_fact`,
  `delete_brain_fact` — **schreiben**. Ein Rechercheauftrag darf die Daten des
  Nutzers nicht verändern; Review-vor-Persist wäre ausgehebelt.
- `trigger_docx_generation`, `create_email`, `generate_chat_pdf` — erwarten
  einen Browser-Dialog, den es in einem Hintergrundlauf nicht gibt.
- `create_kanban_task` — ein Werkbank-Lauf, der Werkbank-Läufe erzeugt.
- `download_document` — Browser-Download ohne Nutzen für den Agenten.
- `view_document_page` — Vision-Call pro Seite, teuer; `get_document_page_text`
  liefert denselben Inhalt als Text. Kandidat für ein späteres Opt-in.

---

## 3. Warum der AirEx-Lauf falsche Produkte fand

Der gemeldete Fehlschlag (Wettbewerbsprodukte zu Argo-Hytos AirEx → Treffer mit
anderem Zweck) ist kein Modellversagen, sondern hat drei benennbare Ursachen im
Ablauf. Alle drei adressiert v2:

1. **Kein vorregistrierter Maßstab.** Ohne `acceptance_criteria` *vor* der
   Recherche erfindet der Critic den Maßstab hinterher und bestätigt, was da
   ist. Ein Kriterium wie „nennt nur Produkte, die dieselbe Funktion erfüllen
   (Be- und Entlüftung von Hydrauliktanks)" hätte den Lauf gekippt.
   → BRIEFER + PLAN_CRITIC (Phase 2/3).
2. **Zitat aus dem Suchsnippet.** Wer nur die SearXNG-Zeile sieht, sieht den
   Produktnamen, nicht den Verwendungszweck. → `web_researcher` zitiert nur aus
   gefetchtem Volltext, im Wrapper erzwungen (Phase 5).
3. **Keine Definitionsstufe.** Ein Mensch klärt erst, was „Wettbewerbsprodukt"
   heißt, und sucht dann. → Zusätzliche PLANNER-Regel für Vergleichsaufgaben:
   *Ein Vergleich braucht einen ersten Subtask, der den Vergleichsgegenstand aus
   einer Quelle bestimmt; die Suche hängt davon ab.* Ohne das vergleicht der
   Lauf Namen statt Funktionen.

Punkt 3 steht so noch nicht im Architekturdokument und wird in Phase 3
ergänzt.

---

## 4. Archetypen bleiben nutzerdefinierbar

Bestand: `werkbank/archetypes.py` (Defaults + Seeding), Tabelle
`agent_archetypes`, Dialog `ui/archetype_dialog.py` (CRUD).

Für v2 bleibt das erhalten, mit zwei Ergänzungen:

- Die fünf v2-Archetypen sind die neuen Defaults (Seeding wie bisher).
- **„Auf Standard zurücksetzen"** pro Archetyp und global — die Defaults sind
  in `config/agents.yaml` versioniert, der Dialog kann jederzeit dorthin
  zurückfallen. Heute gibt es das nicht; wer einen Default-Prompt kaputt
  editiert, kommt nicht zurück.

Nutzerdefinierte Archetypen bekommen kein `requires` und erben `trust` aus der
Tool-Tabelle — Trust bleibt an das Tool gebunden, nie an den Archetyp.

---

## 5. Offene Punkte für die nächsten Phasen

- **`werkbank/v2/tools.py` ist Voraussetzung für Phase 4** (2.3).
- Trust-Feinstufung aus 2.2 in `config/agents.yaml` übernehmen.
- PLANNER-Regel 7 (Definitionsstufe bei Vergleichen) in Phase 3.
- `prechecks.py` gegen D1–D9 abgleichen: was dort schon geprüft wird, wandert
  in `checks.py`, der Rest entfällt.
