# KI-Werkbank — Architektur-Spezifikation

Autonomes Agenten-Modul für PaperSage. Nimmt ein Ziel ("Goal") entgegen, zerlegt
es selbstständig in Teilaufgaben, arbeitet diese mit lokalen oder API-Modellen ab
und legt das Ergebnis nach Freigabe als PDF in Paperless ab.

Dieses Dokument ist die Quelle der Wahrheit für die Implementierung. Es hält die
**Entscheidungen und ihre Begründungen** fest — besonders die, die man aus dem
Code allein nicht ablesen kann.

---

## 1. Zweck und bewusste Abgrenzung

**Ist:** Ein Auftragssystem, das mehrstufige Recherche-/Analyse-Aufgaben über die
vorhandene Dokumentenbasis (Paperless/Chroma), das Gedächtnis (brain-Collection)
und das Web (SearXNG/Crawl4AI als Tools) autonom abarbeitet und ein reviewbares
Markdown-/PDF-Ergebnis produziert.

**Ist ausdrücklich NICHT:** Eine zweite Wahrheitsquelle, die Web-Recherche-Ergebnisse
ungeprüft in den `documents`-Index kippt. Abgeleitete Inhalte landen erst nach
**manueller Freigabe** in Paperless (und damit ggf. später im RAG-Index). Bis dahin
sind sie Quarantäne-Material. Das schützt die Verlässlichkeit des Dokumenten-Gehirns.

---

## 2. Kernprinzipien (nicht verhandelbar)

1. **Der Orchestrator ist deterministischer Python-Code, kein LLM.** Er steuert
   Reihenfolge, Status, Retries, Fehlerbehandlung. Die kognitive Arbeit passiert
   nur *innerhalb* der Rollen. Das LLM dirigiert sich niemals selbst.

2. **Scoped Prompts + Tool-Subsets pro Rolle.** Kein Agent bekommt alle Tools.
   Tool-Überladung degradiert lokale Modelle. Jeder Worker erhält nur die Tools
   seines Archetyps.

3. **Isolierte Kontexte + Kompaktierung.** Jeder Sub-Task läuft in frischem, kurzem
   Kontext. Nachgelagerte Tasks sehen nur die *kompaktierten Summaries* der
   Abhängigkeiten, nie deren rohe Tool-Outputs. Das ist der Hebel, der lokale
   Modelle über lange Ketten trägt.

4. **Review-vor-Persist.** Das Ergebnis ist erst `COMPLETED`, wenn der Nutzer es
   freigegeben hat. Davor: `AWAITING_REVIEW`.

5. **User-Scoping auf jeder Query.** User A darf nie Daten von User B sehen.
   Jede Tabelle trägt `user_id`, jede Repository-Query filtert darauf.

---

## 3. Die Pipeline

```
Goal (User)
  └─ Planner (LLM, System-Rolle) ──► umformulierter Goal
       └─ TRIAGE: User reviewt/editiert den Text ──► GO
            └─ Splitter (LLM, System-Rolle) ──► Sub-Tasks (DAG)
                 └─ Orchestrator (Python) — Ready-Set-Walk:
                      für jeden ausführbaren Sub-Task:
                        Worker (LLM, Archetyp) ──► raw result
                          └─ Python-Prechecks  (deterministisch)
                          └─ Critic (LLM, System-Rolle)  ──► ok / redo
                          └─ Kompaktierung (LLM)  ──► compacted result
                 └─ Synthesizer (LLM, System-Rolle) ──► finales .md
                      └─ AWAITING_REVIEW: User reviewt/editiert .md ──► Freigabe
                           └─ Export: .md → PDF → Paperless (Tags aus Settings)
                                └─ COMPLETED
```

---

## 4. Rollen vs. Archetypen (wichtige Trennung)

**System-Rollen** — feste Pipeline-Bestandteile. Ihre Prompts liegen in den
**Settings** (admin-tunebar), nicht im Archetypen-CRUD:
- **Planner** — formuliert den Nutzer-Goal in einen präziseren Arbeitsauftrag um.
- **Splitter** — zerlegt den Goal in Sub-Tasks; weist jedem einen Archetyp zu.
  Bekommt dafür die Liste der verfügbaren Archetypen (Name + Kurzbeschreibung).
- **Critic** — prüft ein Sub-Task-Ergebnis gegen seine `success_criteria`. Nur als
  Groundedness-/Vollständigkeits-Gate verstehen, nicht als Geschmacks-TÜV. Ein
  gleichstarkes Modell ist ein schwacher Gutachter — entsprechend gewichten.
- **Synthesizer** — verdichtet alle (auch fehlgeschlagene) Teilergebnisse zum
  finalen Markdown. Bekommt typischerweise KEINE Tools.

**Worker-Archetypen** — das ist der Nutzer-CRUD. In SQLite gespeichert, pro User.
Felder: Name, Kurzbeschreibung, Soul-Text (System-Prompt), aktivierte Tools
(Subset der vorhandenen Chat-Agent-Tools).
- **Defaults (in Code, beim ersten Laden in die User-Rows geseedet):**
  - `retriever` — durchsucht Dokumentenbasis + Gedächtnis (rag_search, brain_search, get_doc_details)
  - `researcher` — recherchiert im Web (web_search, fetch_content)
- Nutzer kann Defaults editieren und weitere anlegen → alles als SQLite-Rows.

---

## 5. Nebenläufigkeitsmodell

Die **lokale Ollama-Instanz ist die serialisierende Ressource**, nicht "ein Task
global". Tasks mit eigenem Claude-API-Key laufen gegen Anthropic und blockieren die
GPU nicht.

**Lanes über asyncio-Semaphoren, eine pro Backend:**
- lokal: `Semaphore(1)` — nur einer berührt Ollama gleichzeitig
- api: `Semaphore(N)` — mehrere parallel (Anthropic-Limits beachten)

Jeder LLM-Call `await`et die Semaphore seines Backends. Der Orchestrator weiß von
Nebenläufigkeit nichts — die Lane steckt im injizierten LLM-Callable.

- **v1:** Nebenläufigkeit nur *zwischen* Tasks. Sub-Tasks eines Tasks laufen seriell
  in topologischer Reihenfolge.
- **v2 (später):** unabhängige DAG-Zweige eines API-Tasks parallel.

Das UI muss den Wartezustand ehrlich zeigen ("Position 2 in der Warteschlange"),
wenn ein lokaler Task läuft und weitere lokale Tasks warten.

---

## 6. Status-Lebenszyklus

```
DRAFT ─(User: Start)─► TRIAGE ─(User: bestätigt Text)─► QUEUED
  ─► RUNNING ⇄ PAUSED
  ─► AWAITING_REVIEW ─(User: Freigabe)─► COMPLETED
                                       ╲─► FAILED
```

- **DRAFT** — angelegt, nicht gestartet (Default; Nutzer muss aktiv starten).
- **TRIAGE** — Planner hat umformuliert; Nutzer kann den Auftragstext anpassen.
- **QUEUED** — bestätigt, wartet auf den Worker.
- **RUNNING** — in Bearbeitung.
- **PAUSED** — durch Stop pausiert (nach aktuellem Sub-Task; resumebar).
- **AWAITING_REVIEW** — Synthese fertig, .md liegt vor, wartet auf Freigabe.
- **COMPLETED** — freigegeben und in Paperless abgelegt.
- **FAILED** — Abbruch (z.B. Splitter nach Retries kaputt, oder harter Fehler).

---

## 7. Persistenz — SQLite (WAL-Modus)

Begründung: Kanban-Daten sind strukturierte relationale Daten ohne Ähnlichkeits-
bezug. Chroma (Vektorstore) ist dafür das falsche Werkzeug. Chroma bleibt für
Dokument-/Brain-Vektoren. SQLite im WAL-Modus erlaubt gleichzeitigen Reader (UI)
und Writer (Worker).

```sql
-- Worker-Archetypen (pro User, Defaults werden geseedet)
agent_archetypes(
  id, user_id, name, description, soul_text,
  enabled_tools JSON,            -- Liste von Tool-Namen aus der Tool-Registry
  created_at, updated_at
)

-- Aufträge
agent_tasks(
  id, user_id, original_request, refined_request,
  status,                        -- siehe Lebenszyklus
  model,                         -- gewähltes Modell (mappt auf Backend/Lane)
  result_md, paperless_id, paperless_url,
  created_at, updated_at
)

-- Teilaufgaben
agent_subtasks(
  id, task_id FK, archetype_id FK, user_id,   -- user_id denormalisiert: Defense-in-Depth
  instruction, success_criteria,
  status,                        -- TODO / RUNNING / DONE / FAILED
  depends_on JSON,               -- Liste von Sub-Task-IDs (DAG-Kanten)
  order_index,                   -- stabile Anzeigereihenfolge gleichrangiger Tasks
  result_raw, result_compacted,
  critic_verdict, retry_count,
  created_at, updated_at,
  started_at, finished_at        -- getrennt von updated_at: Fortschritt + Debugging
)
```

`user_id` auf `agent_subtasks` ist bewusst denormalisiert (statt nur per Join über
`agent_tasks`): zweiter Schutzwall gegen Datenlecks + schlankere Polling-Queries.

---

## 8. Splitter-Vertrag (heikelste Schnittstelle)

### Ausgabe-Schema

```json
{
  "subtasks": [
    {
      "ref": "s1",
      "instruction": "Selbstständige, konkrete Anweisung ...",
      "archetype": "retriever",
      "success_criteria": "Kurzes prüfbares Kriterium.",
      "depends_on": []
    },
    {
      "ref": "s2",
      "instruction": "...",
      "archetype": "synthesizer",
      "success_criteria": "...",
      "depends_on": ["s1"]
    }
  ]
}
```

- **`ref`** sind temporäre lokale IDs. Der Splitter kennt keine DB-IDs. `depends_on`
  referenziert nur `ref`s. Der Code remappt nach dem Insert auf echte DB-IDs.
- `order_index` wird aus der Array-Position abgeleitet, nicht vom LLM verlangt.

### Generierungs-Constraint (größter Robustheits-Hebel)

Bei lokalem Modell: **Ollamas `format`-Parameter mit JSON-Schema** zwingt strukturell
valide Ausgabe. Bei Claude: Tool-Use mit erzwungenem Schema. Das verhindert die
meisten Parse-Fehler, *bevor* sie entstehen. Die Validierung bleibt trotzdem nötig
(erzwingt nur Syntax, nicht Semantik).

### Deterministische Validierung (in dieser Reihenfolge)

1. JSON extrahieren (Code-Fences strippen — lokale Modelle wrappen gern in ```json).
2. `json.loads` mit try/except.
3. `subtasks` ist nicht-leere Liste, Länge ≤ `MAX_SUBTASKS` (z.B. 15).
4. Pro Sub-Task: Pflichtfelder vorhanden + Typen korrekt; `instruction` nicht leer.
5. `ref`-Eindeutigkeit.
6. Archetyp existiert in der User-Liste → sonst Fallback auf `retriever` + Warnung.
7. Referenzielle Integrität: jede `depends_on`-Ref existiert; keine Selbst-Abhängigkeit.
8. Azyklizität: topologischer Sort muss durchlaufen (sonst Zyklus → invalid).

### Repair- und Fallback-Strategie

- **Retry mit Feedback** (Cap 2): kaputter Output + Fehlermeldung zurück an den Splitter.
- **Degradierungs-Fallback**: Wenn weiter kaputt, den umformulierten Goal als *einen*
  Sub-Task (Archetyp `researcher`) behandeln. Einstufige Bearbeitung ist besser als FAILED.

### Remapping (Temp-Ref → DB-ID)

In topologischer Reihenfolge inserten (Eltern zuerst), `ref→id`-Map bauen, zweiter
Durchlauf schreibt `depends_on` von Refs auf echte IDs um.

---

## 9. Ausführungs-Mechanik

### Ready-Set-Scheduling (DAG, subsummiert lineare Kette)

Ein Sub-Task ist *ready*, wenn alle seine `depends_on` auf `DONE` stehen. Der
Orchestrator-Loop: nimm ready Sub-Tasks → führe aus → wiederhole, bis alle
`DONE`/`FAILED`. Bei reiner Kette = serielle Reihenfolge. Kinder warten auf die
kompaktierten Ergebnisse ihrer Eltern.

### Pro Sub-Task

```
Worker.run(instruction, dep_results) ──► raw
  └─ prechecks(raw)                       # deterministisch: nicht leer? Quelle? Länge?
       ├─ fail ─► retry (bis Cap) ─► immer noch fail ─► FAILED (Teilinfo-Policy)
       └─ pass ─► Critic.run(task, raw)   # ok / redo
                    ├─ redo (bis Cap)
                    └─ ok ─► compact(raw) ─► result_compacted ─► DONE
```

### Failed-Policy

Scheitert ein Sub-Task nach Retries: als `FAILED` markieren, Platzhalter-Ergebnis
("keine belastbaren Daten ermittelt") setzen, **weitermachen**. Der Synthesizer
bekommt die Lücke mit und vermerkt sie im finalen Dokument. Kein Gesamt-Abbruch.

### Stop / Resume (geschenkt durch Persistenz)

"Stop" = pausieren *nach* dem aktuellen Sub-Task (kein hartes Kill mitten im
LLM-Call). "Resume" = beim ersten Sub-Task mit Status ≠ `DONE` weitermachen. Möglich,
weil jedes Sub-Task-Ergebnis in SQLite persistiert wird. Übersteht auch Neustarts.

---

## 10. Modulstruktur

```
werkbank/
├── __init__.py
├── models.py           # dataclasses + Status-Enum, KEINE Logik
├── repository.py        # einzige SQLite-Schicht (CRUD, WAL, immer user-scoped)
├── schema.sql
├── roles/               # je eine stateless Klasse mit async def run(...)
│   ├── planner.py
│   ├── splitter.py      # + Validierung, oder validation.py separat
│   ├── worker.py
│   ├── critic.py
│   └── synthesizer.py
├── orchestrator.py      # deterministische State-Machine für EINEN Task
├── scheduler.py         # Worker-Loop: zieht QUEUED Tasks, managt Lanes
├── llm_lane.py          # Backend-Semaphoren (lokal=1, api=N)
├── compaction.py
├── prechecks.py
├── archetypes.py        # Default-Archetypen + Auflösung/Seeding
├── export.py            # .md-Assembly + MD→PDF + Paperless-Upload
└── ui/
    ├── module_page.py     # Header-Modul: user-gescopte Tabelle + Buttons
    ├── task_dialog.py     # Board (Triage → To-Do → In Bearbeitung → Abgeschlossen);
    │                       # weicht der Ergebnisansicht bei AWAITING_REVIEW
    └── archetype_dialog.py # Archetypen-CRUD (Name, Beschreibung, Soul-Text, Tool-Toggles)
```

Designregeln:
- Rollen sind stateless, bekommen alles injiziert (LLM-Callable, Tool-Subset,
  Eingaben) → isoliert testbar.
- Orchestrator ruft nie selbst ein LLM, nur Rollen.
- Repository ist die einzige Stelle mit SQL.
- `models.py` enthält nur Daten, keine Logik.

---

## 11. Wiederverwendung bestehender PaperSage-Komponenten

NICHT neu bauen — anbinden:
- **LLM-Client** (Ollama + Anthropic) → von den Rollen über `llm_lane` genutzt.
- **Tool-Registry** → Archetypen referenzieren Tool-Subsets per Namen. Tools müssen
  eine stabile, benannte Liste sein.
- **Chroma-Zugriff** (rag_search, brain_search) → über die Tools.
- **Paperless-Client** → in `export.py` für den Upload.
- **Embedding-Service** (intfloat/multilingual-e5-large-instruct, **mit Instruct-
  Prefixes!**) → falls Sub-Tasks Retrieval machen.
- **Auth/User-Kontext** → `user_id` für das Scoping.
- **Settings** → System-Rollen-Prompts, Default-Tags (Posteingang + ai-generated),
  verfügbare Modelle, per-User verschlüsselter Claude-API-Key (vorhandene
  verschlüsselte Datei pro User).

---

## 12. Sicherheit

- `user_id`-Filter auf JEDER Repository-Query.
- Claude-API-Key bleibt in der vorhandenen per-User-verschlüsselten Datei, nie
  Klartext in SQLite.
- Web-/abgeleitete Inhalte erst nach Freigabe nach Paperless; nie automatisch in
  den `documents`-Index.

---

## 13. UI-Verhalten

- **Modulseite:** user-gescopte Tabelle (Hauptaufgabe, Datum, Status, Paperless-ID +
  Link). Buttons "Neue Aufgabe" und "Agenten" (Archetypen-Dialog).
- **Task-Dialog:** horizontales Board. Steuerung oben: Start, Stop (Default Stop),
  Löschen, Modellauswahl (aus Settings). Triage-Spalte hält den umformulierten Goal
  (editierbar); Sub-Task-Spalten To-Do → In Bearbeitung → Abgeschlossen.
- Bei `AWAITING_REVIEW`: Board weicht der **Ergebnisansicht** mit editierbarem
  Markdown + "Freigeben & nach Paperless" / "Verwerfen".
- Refresh per `ui.timer` (2–3 s) aus dem Repository. Fortschritt zeigen
  ("Sub-Task 3 von 8"), da Läufe lange dauern (15–25 min lokal).
