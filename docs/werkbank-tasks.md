# KI-Werkbank — Implementierungsplan

Schrittweiser Bauplan für Claude Code. Architektur-Details und Begründungen:
siehe `werkbank-architecture.md`. Phasen aufeinander aufbauend; jede Phase hat eine
"Definition of Done" (DoD). Innerhalb einer Phase die Reihenfolge einhalten.

**Vor Beginn:** Die bestehenden PaperSage-Interfaces kennen, die angebunden werden —
LLM-Client (Ollama + Anthropic), Tool-Registry (Namen der Chat-Agent-Tools),
Paperless-Client, Embedding-Service, Settings-Zugriff, Auth/User-Kontext. Diese
NICHT neu bauen.

---

## Phase 0 — Gerüst & Datenschicht

- [ ] Modulverzeichnis `werkbank/` anlegen (Struktur siehe Architektur §10).
- [ ] `models.py`: dataclasses `Task`, `SubTask`, `Archetype` + `Status`-Enum
      (DRAFT, TRIAGE, QUEUED, RUNNING, PAUSED, AWAITING_REVIEW, COMPLETED, FAILED)
      und `SubTaskStatus`-Enum (TODO, RUNNING, DONE, FAILED). Keine Logik.
- [ ] `schema.sql`: drei Tabellen gemäß Architektur §7. `user_id` auf allen dreien.
      `started_at`/`finished_at` auf `agent_subtasks`.
- [ ] `repository.py`: einzige SQLite-Schicht. WAL-Modus aktivieren. CRUD für alle
      drei Tabellen. **Jede Lese-/Schreibmethode nimmt `user_id` und filtert darauf.**
      Methoden u.a.: `create_task`, `update_task_status`, `get_tasks_for_user`,
      `insert_subtask`, `update_depends_on`, `get_subtasks`, `set_subtask_result`,
      `next_queued_task`, Archetyp-CRUD.

**DoD:** Tabellen werden bei App-Start angelegt; ein Task + Sub-Tasks lassen sich
schreiben und user-gescopt wieder lesen. Unit-Test mit zwei Usern beweist Isolation.

---

## Phase 1 — Archetypen

- [ ] `archetypes.py`: Code-Defaults `retriever` und `researcher` (Name, Beschreibung,
      Soul-Text, enabled_tools als Tool-Namen aus der vorhandenen Registry).
- [ ] Seeding-Logik: beim ersten Laden eines Users ohne Archetyp-Rows die Defaults
      in seine SQLite-Rows schreiben.
- [ ] Auflösungs-Helper: Archetyp-Name → (Soul-Text, Tool-Subset). Tool-Namen gegen
      die echte Registry validieren.

**DoD:** Neuer User bekommt automatisch die zwei Default-Archetypen; Tool-Namen
mappen korrekt auf reale Tools.

---

## Phase 2 — LLM-Lanes & Rollen-Gerüst

- [ ] `llm_lane.py`: per-Backend `asyncio.Semaphore` (lokal=1, api=N). Funktion/Closure,
      die ein LLM-Callable so wrappt, dass es vor dem Call die passende Semaphore
      `await`et. Backend wird aus dem gewählten Modell abgeleitet.
- [ ] `roles/`: für jede Rolle eine stateless Klasse mit `async def run(...)`.
      Alles injiziert (LLM-Callable, ggf. Tool-Subset, Eingaben). Vorerst Gerüste mit
      klaren Signaturen:
  - `planner.py` — `run(original_request) -> refined_request`
  - `splitter.py` — `run(refined_request, available_archetypes) -> list[SubTaskSpec]`
  - `worker.py` — `run(instruction, archetype, dep_results) -> raw_result`
  - `critic.py` — `run(instruction, success_criteria, raw_result) -> verdict`
  - `synthesizer.py` — `run(task, subtask_results) -> markdown`

**DoD:** Zwei lokale Dummy-Tasks treffen nie gleichzeitig auf das LLM-Callable
(Semaphore-Test); ein API-Task läuft parallel zu einem lokalen.

---

## Phase 3 — Splitter-Vertrag & Validierung

- [ ] JSON-Schema des Splitter-Outputs festlegen (Architektur §8).
- [ ] Generierungs-Constraint: bei Ollama `format=<schema>`, bei Claude Tool-Use mit
      erzwungenem Schema.
- [ ] `validation.py` (oder in `splitter.py`): die 8-stufige Validierungs-Pipeline
      (Architektur §8). `SplitterParseError` mit aussagekräftiger Meldung.
- [ ] Retry-mit-Feedback (Cap 2) + Degradierungs-Fallback (Goal als ein einzelner
      `researcher`-Sub-Task).
- [ ] Remapping Temp-`ref` → DB-ID (topologisch inserten, dann `depends_on` umschreiben).

**DoD:** Valides Schema parst korrekt; absichtlich kaputte Outputs (kein JSON,
Zyklus, unbekannter Archetyp, dangling ref, leere instruction) werden alle gefangen;
Fallback erzeugt einen lauffähigen Einzel-Sub-Task.

---

## Phase 4 — Orchestrator & Scheduler

- [ ] `prechecks.py`: deterministische Checks (nicht leer, mind. eine Quelle falls
      Retrieval-Archetyp, Längengrenzen). Laufen VOR dem Critic.
- [ ] `compaction.py`: LLM-Call ohne Tools, verdichtet `raw` → `compacted` bezogen
      auf den Goal.
- [ ] `orchestrator.py`: deterministische State-Machine für EINEN Task.
      Ready-Set-Walk über den DAG. Pro Sub-Task: Worker → prechecks → (retry) →
      Critic → (redo) → compact → DONE. Failed-Policy: nach Retry-Cap FAILED +
      Platzhalter, weitermachen. Status nach JEDEM Schritt in SQLite persistieren.
      Pause-Flag respektieren (nach aktuellem Sub-Task halten).
- [ ] `scheduler.py`: langlebige In-Process-async-Coroutine. Pollt
      `next_queued_task`, startet pro Task eine Orchestrator-Ausführung als
      Coroutine. Cross-Task-Nebenläufigkeit über die Lanes (v1: Sub-Tasks innerhalb
      eines Tasks seriell).

**DoD:** Ein mehrstufiger Task läuft end-to-end durch bis `AWAITING_REVIEW`; Stop
pausiert nach dem aktuellen Sub-Task; Resume macht korrekt weiter; ein bewusst
fehlschlagender Sub-Task bricht den Lauf nicht ab und wird im Ergebnis vermerkt.

---

## Phase 5 — Export nach Paperless

- [ ] `export.py`: finales Markdown zusammensetzen (Metadaten + Ursprungsaufgabe +
      Synthese-Ergebnis). MD → PDF (z.B. weasyprint). Upload via vorhandenen
      Paperless-Client mit Tags aus Settings (Posteingang + ai-generated).
      Rückgabe-Doc-ID + Link in `agent_tasks` speichern.

**DoD:** Nach Freigabe liegt ein sauberes PDF mit beiden Tags in Paperless; ID und
Link sind in der Task-Row gespeichert und in der UI verlinkt.

---

## Phase 6 — UI

- [ ] `ui/module_page.py`: Eintrag im Header neben den anderen Modulen
      ("KI-Werkbank"). User-gescopte Tabelle (Hauptaufgabe, Datum, Status,
      Paperless-ID + Link). Buttons "Neue Aufgabe", "Agenten".
- [ ] `ui/archetype_dialog.py`: CRUD-Dialog (Name, Kurzbeschreibung, Soul-Text,
      Tool-Toggles aus der realen Tool-Registry).
- [ ] `ui/task_dialog.py`: Board (Triage → To-Do → In Bearbeitung → Abgeschlossen),
      Steuerung oben (Start, Stop=Default, Löschen, Modellauswahl). Bei
      `AWAITING_REVIEW` weicht das Board der Ergebnisansicht mit editierbarem
      Markdown + "Freigeben & nach Paperless" / "Verwerfen".
- [ ] Triage-Flow: Start → Planner umformuliert → TRIAGE; Nutzer editiert Text →
      Bestätigung → QUEUED.
- [ ] Live-Refresh per `ui.timer` (2–3 s) aus dem Repository. Fortschritt anzeigen
      ("Sub-Task 3 von 8") und Warteposition bei belegter lokaler Lane.

**DoD:** Kompletter Durchlauf per UI: Aufgabe anlegen → starten → Triage editieren →
bestätigen → Board füllt sich → Ergebnis reviewen → freigeben → PDF in Paperless.
Nur eigene Aufgaben sichtbar.

---

## Phase 7 — Settings-Integration

- [ ] System-Rollen-Prompts (Planner, Splitter, Critic, Synthesizer) in Settings.
- [ ] Default-Tags (Posteingang, ai-generated) in Settings.
- [ ] Verfügbare Modelle in Settings (Mapping Modell → Backend lokal/api).

**DoD:** Prompts und Tags ohne Code-Änderung anpassbar; Modellauswahl im Task-Dialog
zieht aus den Settings.

---

## Querschnitt (in jeder Phase beachten)

- **User-Scoping** auf jeder Query — nicht nachträglich.
- **Status nach jedem Schritt persistieren** — ermöglicht Pause/Resume und Crash-Recovery.
- **Rollen isoliert testbar halten** — keine versteckten Abhängigkeiten.
- **Keine Web-/abgeleiteten Inhalte automatisch in den `documents`-Index** —
  ausschließlich über den Freigabe-Pfad nach Paperless.
