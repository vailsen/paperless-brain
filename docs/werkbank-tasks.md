# KI-Werkbank v2 — Tasks

Begleitdokument zu `werkbank-architecture.md`. Dort steht das *Warum*, hier das
*Was in welcher Reihenfolge*.

**Arbeitsweise:** Eine Phase pro Session. `/clear` zwischen den Phasen. Plan Mode
vor jeder Phase. Keine Phase gilt als fertig, bevor ihre Akzeptanzkriterien
verifiziert sind — nicht "sollte funktionieren", sondern ausgeführt und gezeigt.

**Pfade unten sind Vorschläge** und an die bestehende Werkbank-Struktur
anzupassen. Falls bereits ein `werkbank/`-Modul existiert: v2 als
Parallelstruktur unter `werkbank/v2/` aufbauen, v1 bleibt lauffähig, bis Phase 7
durch ist.

---

## Phase 0 — Bestandsaufnahme ✅ erledigt

Ergebnis: `werkbank-v2-findings.md` — Wiederverwendungs-Matrix, vollständiges
Tool-Inventar mit Quotierbarkeit, Tool-Zuordnung auf die fünf Archetypen und
die Analyse des AirEx-Fehlschlags.

Reine Diagnose, kein Code.

1. Bestehendes Werkbank-Modul lesen: Rollen, Persistenzschema, Scheduler,
   UI-Anbindung.
2. Dokumentieren, welche Teile wiederverwendbar sind (vermutlich:
   SQLite-Setup, NiceGUI-Board, Ollama-Client) und welche ersetzt werden
   (Rollen-Prompts, Result-Handling, alles was Prosa als Subtask-Output annimmt).
3. Prüfen, welche Tool-Wrapper schon existieren und welche Signatur sie haben:
   Paperless semantisch, Paperless Filter, Vault-Suche, SearXNG + Fetch,
   IMAP, CalDAV, calculate.
4. **Kritisch:** Für jeden Tool-Wrapper prüfen, ob er den **Rohtext des
   Ergebnisses** zurückgibt oder nur eine aufbereitete Zusammenfassung. Ohne
   Rohtext ist Check D2 (Quote-Matching) nicht durchführbar. Fehlende
   Rohtext-Rückgabe ist ein Blocker für Phase 4 und muss hier auf die Liste.

**Ergebnis:** Kurzes Findings-Dokument. Keine Änderungen am Code.

---

## Phase 1 — Datenmodell und deterministische Checks ✅ erledigt

Geliefert: `werkbank/v2/models.py`, `werkbank/v2/checks.py`,
`werkbank/v2/store.py` und `tests/test_werkbank_v2_{models,checks,store}.py`.

Zwei bewusste Abweichungen vom Text unten:

- **Kein `rapidfuzz`.** Der Quote-Abgleich läuft auf der Standardbibliothek
  (`difflib` plus Normalisierung). Die eigentliche Frage ist „steht dieser Satz
  im abgerufenen Text", und die beantwortet Normalisierung plus Teilstring-Test
  exakt; unscharf verglichen wird nur für OCR-Rauschen. Ein Austausch beträfe
  genau `partial_ratio()`.
- **Zusätzlich ein Token-Test** (`unsupported_tokens`). Ein Ähnlichkeitsmaß
  allein erkennt den gefährlichsten Fall nicht: ein getauschtes Wort oder eine
  getauschte Zahl („drei" → „sechs", „1.240" → „1.420") lässt zwei Sätze zu
  über 90 % übereinstimmen und dreht die Aussage trotzdem um. Zahlen müssen
  deshalb exakt vorkommen, lange Wörter nahezu vollständig.

Kein LLM in dieser Phase. Das ist Absicht: die Checks müssen ohne Modell testbar
sein.

### 1.1 Pydantic-Modelle

`werkbank/v2/models.py`

- `Brief`, `Subtask`, `SubtaskResult`, `Fact`, `Source`, `Gap`,
  `CriticVerdict`, `RunState`
- Enums: `Evidence`, `SourceTrust`, `FactKind`, `SubtaskStatus`,
  `DepthBudget`, `GapReason`, `CriticDecision`
- `DEPTH_BUDGETS: dict[DepthBudget, BudgetConfig]` mit `max_subtasks`,
  `max_revisions`, `run_plan_critic`

Feldschema exakt nach `werkbank-architecture.md` §3.

Validatoren direkt im Modell:
- `Fact.id` muss dem Muster `st<n>.f<n>` entsprechen
- `evidence == "quote"` → mindestens eine Source mit nicht-leerem `quote`
- `evidence == "derived"` → `derived_from` nicht leer
- `narrative` darf nur Marker der Form `[st<n>.f<n>]` enthalten

### 1.2 Persistenz

`werkbank/v2/store.py`

SQLite (WAL), Tabellen: `runs`, `subtasks`, `facts`, `sources`,
`critic_verdicts`, `tool_calls`.

- `tool_calls` speichert pro Subtask den **abgerufenen Rohtext** (nötig für D2).
  Retention: mit dem Run löschbar, nicht dauerhaft.
- Run muss nach Neustart fortsetzbar sein → `subtasks.status` und `revision`
  persistent, abgeschlossene Subtasks werden nicht erneut ausgeführt.

### 1.3 Deterministische Checks

`werkbank/v2/checks.py` — reine Funktionen, keine Seiteneffekte, kein LLM.

D1–D9 nach §4 des Architekturdokuments. Signatur einheitlich:

```python
def check_d2_quote_grounding(result: SubtaskResult, raw_texts: dict[str, str]) -> CheckReport
```

`CheckReport` enthält: `passed`, `rejected_fact_ids`, `flags`, `messages`.

Für D2: Fuzzy-Match via `rapidfuzz.partial_ratio`, Schwelle 90. Vorher
normalisieren (Whitespace, Zeilenumbrüche, typografische Anführungszeichen).

Für D3 Arithmetik: `expression` mit einem **restriktiven** Parser auswerten
(nur Zahlen und `+ - * / ( )`), kein `eval`.

### Akzeptanzkriterien Phase 1

Unit-Tests mit handgeschriebenen Fixtures, alle grün und vorgeführt:

- Fact mit manipuliertem `quote` (Wort geändert) → D2 verwirft ihn
- Fact mit korrektem Zitat, anders formatiert (Umbrüche) → D2 akzeptiert
- `evidence: computed` ohne `expression` und ohne `query` → D3 verwirft
- `expression: "1240 + 890 + 2100"` mit falschem Claim-Wert → D3 verwirft
- `derived_from: ["st9.f1"]` bei nicht existierendem st9 → D4 verwirft
- `tool_calls == 0` bei verfügbaren Tools → D5 erzwingt Revision
- `narrative` mit Marker `[st3.f99]` → D6 strippt und flaggt
- alle Facts verworfen, `gaps` leer → D8 setzt `unresolvable`
- Run abbrechen und neu laden → abgeschlossene Subtasks bleiben abgeschlossen

**Nicht** in dieser Phase: Prompts, LLM-Calls, UI.

---

## Phase 2 — BRIEFER + Bestätigungs-UI ✅ erledigt

Geliefert: `werkbank/v2/llm.py`, `werkbank/v2/briefer.py`,
`werkbank/v2/prompts/briefer.md`, `werkbank/v2/ui/brief_dialog.py`,
`tests/test_werkbank_v2_briefer.py`.

Ergänzungen gegenüber dem Text unten:

- **Die Verb-Regel steht im Prompt, geprüft wird im Code etwas anderes.**
  „Beginnt mit einem Verb" ist über Deutsch und Englisch hinweg ohne Parser
  nicht zuverlässig entscheidbar, und ein fälschlich abgelehntes gutes
  Kriterium ist schlimmer als ein durchgerutschtes schwaches. Entscheidbar ist
  das *Vokabular der Vagheit* — `criterion_problem()` lehnt „umfassend",
  „gut", „alle relevanten Aspekte", Fragen und Ein-Wort-Kriterien ab.
- **Definitions-Kriterium bei Vergleichsaufgaben** (`missing_definition_criterion`).
  Enthält die Anfrage Wettbewerbs-/Vergleichsvokabular, muss ein Kriterium
  festlegen, was als vergleichbar gilt — sonst vergleicht der Lauf Namen statt
  Funktionen. Das ist der AirEx-Fehlschlag, im Code adressiert.
- **`original_request` wird nach dem Call aus der Eingabe zurückgeschrieben**,
  nicht aus der Antwort übernommen.
- **`current_date` injiziert `llm.py` in jeden Prompt**, nicht jede Rolle für
  sich — so ist es eine Invariante und keine Konvention.
- **Prompt-Log** als JSONL pro Run (`data/werkbank_v2_logs/<run_id>.jsonl`),
  Voraussetzung für den Nachweis in Phase 4.

Offen: die Akzeptanzkriterien „drei reale Aufgaben ergeben valide Briefs" und
„jedes Kriterium beginnt mit einem Verb" brauchen ein echtes Modell und sind
manuell zu prüfen. Alles andere ist mit Stub-Modell getestet.

### 2.1 Prompt

`prompts/briefer.md`

Muss enthalten:
- `current_date` (Datum, Wochentag, KW) wird injiziert
- Output ausschließlich JSON nach `Brief`-Schema, kein Preamble, keine Backticks
- `original_request` wird **wörtlich unverändert** übernommen
- `depth_budget` nur aus dem Enum, mit erklärter Semantik der drei Stufen
- `acceptance_criteria`: jedes beginnt mit einem Verb und benennt ein prüfbares
  Artefakt. Negativbeispiele explizit im Prompt ("umfassende Antwort",
  "gute Analyse" → nicht zulässig)
- `assumptions`: jede Auslegung oder Verengung der Aufgabe muss hier stehen

### 2.2 Runner-Infrastruktur

`werkbank/v2/llm.py` — dünner Wrapper um den bestehenden Ollama/Cloud-Client:
JSON-Mode, Retry bei Schema-Fehler (max. 2), Temperatur pro Rolle konfigurierbar,
vollständiges Prompt-Logging pro Call (wird in Phase 4 zur Verifikation
gebraucht).

### 2.3 UI

NiceGUI-Dialog nach dem BRIEFER-Call:
- `goal`, `deliverable_format` als editierbare Textfelder
- `assumptions` **prominent**, als Liste, jede einzeln löschbar
- `acceptance_criteria` editierbar, hinzufügbar, löschbar
- `out_of_scope` editierbar
- `depth_budget` als Dropdown mit sichtbaren Auswirkungen ("max. 8 Subtasks,
  1 Revision")
- Buttons: Bestätigen / Abbrechen

### Akzeptanzkriterien Phase 2

- Drei reale, unterschiedlich komplexe Aufgaben ergeben schema-valide Briefs
- Jedes erzeugte `acceptance_criterion` beginnt mit einem Verb (manuell geprüft
  und im Ergebnis gezeigt)
- `depth_budget` ist immer ein gültiger Enum-Wert
- `original_request` ist im gespeicherten State byte-identisch mit der Eingabe
- User-Änderungen im Dialog landen im persistierten Brief

---

## Phase 3 — Registry + PLANNER + PLAN_CRITIC ✅ erledigt

Geliefert: `config/agents.yaml`, `werkbank/v2/registry.py`,
`werkbank/v2/plan_checks.py`, `werkbank/v2/planner.py`,
`werkbank/v2/prompts/{planner,plan_critic}.md`,
`tests/test_werkbank_v2_planner.py`.

Ergänzungen gegenüber dem Text unten:

- **`forbidden_tools` in der Registry.** Schreibende Tools, Dialog-Tools und
  `create_kanban_task` sind für *jeden* Archetyp gesperrt — auch für
  nutzerdefinierte. Ein Rechercheauftrag verändert keine Nutzerdaten, und das
  ist keine Einstellung.
- **Trust hängt am Tool, feiner als in §5.** `get_document_page_text` ist
  `authoritative`, `get_document_details` ist `derived` (die Sidecar-Summary ist
  die Paraphrase eines Modells). Begründung in den Findings.
- **PLANNER-Regel 7** (Definitions-Subtask bei Vergleichen), im Prompt und über
  den Brief bereits in Phase 2 abgesichert.
- **`ensure_contradiction_checker()` hängt den Pass an und korrigiert seine
  Abhängigkeiten auf die Blätter** — auch wenn der Planer ihn schon gesetzt hat,
  aber nur an einen Teil gehängt hat.
- **Ein `covered`-Verdict ohne genannte Subtask-ID wird auf `partial`
  abgestuft.** Ein Urteil ohne Beleg ist eine Meinung.
- **Registry-Pfad:** `config/agents.yaml` im Repo, zur Laufzeit unter
  `APP_PATH/config/agents.yaml` überschreibbar.

### 3.1 Registry

`config/agents.yaml` nach §6.5. Loader in `werkbank/v2/registry.py`:

```python
def available_agents(user_config: UserConfig) -> list[AgentSpec]
```

Filtert nach `requires` gegen die tatsächlich konfigurierten Tools des Users.
Ein Agent, dessen Pflicht-Tools fehlen, existiert für diesen Run nicht.

### 3.2 PLANNER

`prompts/planner.md`

Input: Brief, **gefilterte** Registry (ID, Label, Tools, kurze Eignung),
`current_date`.
Output: JSON mit Subtask-Liste. Pro Subtask: `subtask_id`, `question`,
`agent`, `acceptance_criteria`, `covers_criteria`, `depends_on`,
`sources_restrict`.

Zuweisungsregeln 1–6 aus §2.3 **wörtlich** in den Prompt.

### 3.3 Plan-Validierung (Code, kein LLM)

`werkbank/v2/plan_checks.py`:

- Agent existiert in der gefilterten Registry
- DAG zyklenfrei (topologische Sortierung)
- `depends_on` referenziert existierende Subtasks
- Anzahl Subtasks ≤ `max_subtasks` des Budgets
- Jedes Brief-Kriterium wird von mindestens einem Subtask abgedeckt
- `synthesizer`/`contradiction_checker` haben `depends_on` nicht leer
- Alle anderen Agenten haben einen zugewiesenen Agenten
- `contradiction_checker` kommt genau einmal vor und hängt von allen
  Blatt-Subtasks ab (wird bei Bedarf **automatisch angehängt**, nicht
  vom LLM erwartet)

Verstöße → Replanning-Call mit konkreter Mängelliste, max. 1 Runde, dann
Abbruch mit sichtbarer Fehlermeldung.

### 3.4 PLAN_CRITIC

`prompts/plan_critic.md`

Läuft **nach** der Code-Validierung und nur bei `run_plan_critic == True`.
Genau eine Frage: Welches Abnahmekriterium wird durch die zugewiesenen Agenten
nicht hinreichend abgedeckt?

Output: pro Brief-Kriterium `covered` / `partial` / `uncovered` + Subtask-IDs
als Beleg. Kein Freitext-Gesamturteil.

Bei `uncovered` → ein Replanning-Durchlauf. Danach läuft der Plan wie er ist;
die Schwäche wird im RunState vermerkt und landet später in der Reflexion.

### Akzeptanzkriterien Phase 3

- Mit deaktiviertem Mail-Tool taucht `comms_researcher` weder in der Registry
  noch im Plan auf
- Eine nur per Mail beantwortbare Frage erzeugt bei deaktiviertem Mail-Tool
  einen `gaps`-Eintrag mit `reason: "source_unavailable"` — und **keinen**
  Ersatz-Subtask über `web_researcher` oder Parameterwissen
- Ein konstruierter zyklischer Plan wird von der Code-Validierung abgelehmt
- Ein Plan mit unabgedecktem Brief-Kriterium löst genau einen Replanning-Lauf aus
- `contradiction_checker` ist im finalen Plan genau einmal enthalten
- `quick`-Budget überspringt den PLAN_CRITIC nachweislich (Log)

---

## Phase 4 — Erster Runner (`doc_researcher`) + FACT_CRITIC + Loop ✅ erledigt

Geliefert: `werkbank/v2/tools.py`, `werkbank/v2/agent_loop.py`,
`werkbank/v2/runner.py`, `werkbank/v2/critic.py`,
`werkbank/v2/prompts/agents/doc_researcher.md`,
`werkbank/v2/prompts/fact_critic.md`, `tests/test_werkbank_v2_runner.py`.

Der in Phase 0 gemeldete Blocker ist damit erledigt: `tools.ToolBelt` ruft die
Tools selbst auf, hält den Rohtext pro Call fest und setzt `trust` — die
einzige Stelle, an der das passieren kann.

Ergänzungen gegenüber dem Text unten:

- **Zwei Calls pro Subtask statt einem.** Erst der Tool-Loop (Sammeln), dann
  ein zweiter, schema-gebundener Call (Berichten) mit dem Katalog der
  abgerufenen Quellen davor. Vermischt man beides, kommt Prosa mit
  angetacktertem JSON zurück — und „erfinde keine Source-ID" bleibt eine Bitte
  statt einer prüfbaren Aussage.
- **`renumber_facts()` repariert Fact-IDs am Parse-Rand.** Ein Modell, das seine
  Facts in Subtask `st1` mit `st7.f1` beschriftet, hat sich verbucht, nicht
  halluziniert — das Modell-Layer lehnt die Inkonsistenz aber ab, also wird vor
  der Validierung umnummeriert (inkl. `derived_from` und Narrative-Marker).
- **Die Checks überstimmen den Critic.** Ein `accept` auf einem Subtask, dessen
  Facts D1–D9 verworfen haben, wird zu `revise` heruntergestuft.
- **`agent_loop.py` dupliziert die Backend-Formatierung aus
  `roles/worker.py`.** Bewusst: v1 läuft bis Phase 7, und ein Refactoring des
  Loops, von dem v1 abhängt, riskiert das laufende Modul ohne Gewinn. Fällt mit
  v1 weg.

Offen für die manuelle Prüfung mit echtem Modell: die beiden Negativtests gegen
echte Paperless-Daten. Die Mechanik dahinter (D5 bei `tool_calls == 0`,
`unresolvable` mit `gaps` statt erfundener Facts, Revisions-Cap,
Critic ohne `narrative`) ist mit Stub-Modell getestet.

Die inhaltlich schwierigste Phase. Nur **ein** Agent, damit der Loop sauber
verifizierbar ist.

### 4.1 Runner-Prompt

`prompts/agents/doc_researcher.md`

- Aufgabe, `acceptance_criteria`, `current_date`, Facts der Vorgänger
  (**nur Facts, nie deren `narrative`**)
- Tool-Beschreibungen: Paperless semantisch, Paperless Filter, Notizen,
  Vault-Suche, calculate
- Vault-Suche wird bei jeder Dokumentensuche automatisch mitgefeuert
  (im Tool-Wrapper, nicht als Modellentscheidung)
- Output-Schema `SubtaskResult`
- Explizit: Facts dürfen lang sein und Markdown-Tabellen enthalten. Maßstab ist
  nicht Kürze, sondern: *als Ganzes akzeptierbar oder verwerfbar*
- Explizit: Wenn eine Teilfrage nicht beantwortbar ist → `gaps`-Eintrag. Ein
  leeres Suchergebnis ist ein legitimes Ergebnis, kein Anlass für Vermutungen
- Bei Metadatenfiltern: `query` und `hits` sind Pflicht, es gibt kein Belegzitat

### 4.2 Trust-Zuweisung

**Im Tool-Wrapper, nicht im Prompt.** Jedes Suchergebnis wird beim Zurückgeben
mit `source.trust` nach der Tabelle in `agents.yaml` versehen. Das Modell kann
`trust` weder setzen noch überschreiben — beim Deserialisieren wird der Wert aus
dem Tool-Log überschrieben, falls das Modell etwas anderes schreibt.

### 4.3 FACT_CRITIC

`prompts/fact_critic.md`

Reihenfolge zwingend: **D1–D9 zuerst, dann LLM.** Was die Checks verworfen
haben, sieht der Critic gar nicht mehr.

Critic-Input strikt begrenzt auf:
- `question`, `acceptance_criteria`, `original_request`
- Fact-Liste (ohne `confidence`)
- Rohsnippets der Quellen
- **kein `narrative`**

Prompt-Haltung:
- Pro Fact: *"Welcher Teil des Belegzitats stützt diese Aussage nicht?"*
  (Präsupposition auf Mangel, nicht auf Korrektheit)
- Pro Abnahmekriterium: `met` / `partial` / `unmet` + Fact-IDs als Beleg
- Kriterium ohne referenzierte Fact-ID → im Code automatisch auf `unmet` gesetzt
- Bei `kind != "statement"`: stichprobenartige Prüfung einzelner Zellen/Zeilen
- `temperature: 0.1`

Erlaubte Decisions: `accept` / `revise` (+ Mängelliste) / `unresolvable`.
**Kein Tool-Zugriff, keine eigenen Facts.** Der Critic-Prompt enthält keine
Tool-Definitionen.

### 4.4 Revisions-Loop

`werkbank/v2/executor.py`

- Bei `revise`: Runner erneut, mit Mängelliste und den bisherigen Facts als
  Kontext, `revision += 1`
- Cap aus `depth_budget.max_revisions`. Erreicht → `unresolvable`,
  `RunState.capped_subtasks` vermerken
- Jede Revision wird persistiert, nicht überschrieben (Nachvollziehbarkeit)

### Akzeptanzkriterien Phase 4

- Ein realer Subtask gegen echte Paperless-Daten erzeugt schema-valide Facts,
  deren Zitate D2 bestehen
- **Negativtest:** "Welche Kündigungsfrist steht in Dokument
  ›Mietvertrag Musterstraße 99‹?" (existiert nicht) → `status: unresolvable`,
  `gaps` gefüllt, **null erfundene Facts**
- **Negativtest:** Subtask, den das Modell aus Parameterwissen beantworten
  könnte → D5 greift bei `tool_calls == 0` und erzwingt Revision
- Prompt-Log belegt: der FACT_CRITIC hat das `narrative` nicht im Kontext
- Ein Fact mit Markdown-Tabelle aus einer Quelle wird korrekt als
  `kind: "table"` verarbeitet
- Ein Vault-Treffer bekommt `trust: user_asserted`, auch wenn das Modell im JSON
  etwas anderes behauptet
- Revisions-Cap greift und erzeugt `unresolvable` statt Endlosschleife

---

## Phase 5 — Scheduler + restliche Runner ✅ erledigt

Geliefert: `werkbank/v2/scheduler.py`, die Prompts für `web_researcher`,
`comms_researcher`, `synthesizer` und `contradiction_checker`,
`tests/test_werkbank_v2_scheduler.py`.

Ergänzungen gegenüber dem Text unten:

- **Failed-Policy im Scheduler.** Ein abgestürzter Subtask wird zu einem
  Platzhalter mit `gaps`-Eintrag, der Lauf geht weiter. Auch der Fall
  „Agent zwischen Planung und Ausführung verschwunden" (Zugangsdaten entfernt)
  landet dort statt in einem Traceback.
- **`unresolvable` ist ein Ergebnis, kein Fehlschlag.** Beim Resume wird es
  nicht erneut versucht — sonst ist der Revisions-Cap wirkungslos, weil ein
  Neustart die Schleife von vorn beginnt.
- **Nur akzeptierte Facts werden vererbt.** Facts eines `unresolvable`
  Vorgängers werden *nicht* weitergereicht: sie würden eine Lücke als Quelle
  ausgeben.

### 5.1 Scheduler

`werkbank/v2/scheduler.py`

- Level-weise Abarbeitung: alle Subtasks mit erfüllten `depends_on`
- `asyncio.Semaphore` mit `CONCURRENCY[backend]` (`local: 2`, `cloud: 6`)
- Abhängige Subtasks bekommen **nur die akzeptierten Facts** der Vorgänger
- Fortsetzbarkeit nach Neustart aus dem persistierten State

### 5.2 Weitere Runner-Prompts

`prompts/agents/web_researcher.md`
- Zitate ausschließlich aus gefetchtem Volltext, **nie aus SearXNG-Snippets**
  (im Tool-Wrapper erzwingen: Snippet-Text wird gar nicht erst als zitierbarer
  Rohtext registriert)
- `retrieved_at` Pflicht

`prompts/agents/comms_researcher.md`
- Gmail: SPECIAL-USE `\All`-Flag zur Ordner-Discovery, modified UTF-7 beachten,
  `SEARCH X-GM-RAW` bei `X-GM-EXT-1`
- Serientermine und Zeitzonen vor der Fact-Erzeugung auflösen
- Alle Facts `trust: user_asserted`

`prompts/agents/synthesizer.md`
- **Keine Tool-Definitionen im Prompt.** Input sind ausschließlich Facts
- Jeder erzeugte Fact braucht `evidence: "derived"` und vollständiges
  `derived_from`
- Explizit: keine neuen Aussagen, nur Verdichtung des Vorhandenen

### Akzeptanzkriterien Phase 5

- Ein Plan mit zwei parallelen und einem abhängigen Subtask läuft korrekt durch;
  Log zeigt die parallele Ausführung
- Concurrency-Limit wird eingehalten (bei `local: 2` nie mehr als zwei
  gleichzeitige LLM-Calls)
- Ein abhängiger Subtask hat nachweislich nur Facts, kein `narrative`, im Kontext
- Run wird mitten in der Ausführung abgebrochen und korrekt fortgesetzt
- `web_researcher` erzeugt keinen Fact, dessen Zitat nur im Snippet stand
- `synthesizer` mit erfundenem `derived_from` scheitert an D4

---

## Phase 6 — CONTRADICTION_CHECKER + WRITER ✅ erledigt

Geliefert: `werkbank/v2/contradictions.py`, `werkbank/v2/reflection.py`,
`werkbank/v2/writer.py`, `werkbank/v2/prompts/writer.md`,
`tests/test_werkbank_v2_writer.py`.

Ergänzungen gegenüber dem Text unten:

- **Der Reflexionsblock trägt Marker** (`<!-- reflection:begin/end -->`). Ohne
  eindeutige Grenzen ist „unverändert übernommen" nicht prüfbar; mit ihnen ist
  der Vergleich exakt und die Wiederherstellung eindeutig.
- **D9-Flags fließen zurück in den Block.** Ein Absatz ohne Beleg wird nicht nur
  vermerkt, sondern in *demselben* Dokument benannt, in dem er steht — der Block
  wird nach der Prüfung neu gebaut und ersetzt.
- **Das Quellenverzeichnis wird aus den Facts gebaut, nicht vom Modell
  erfragt.** Ein Modell, das seine eigenen Quellen auflistet, listet die guten
  auf; gebraucht werden gerade die schwachen.
- **`contradictions.apply_pairs()` schreibt beide Richtungen.** Eine nur
  einseitig vermerkte Widersprüchlichkeit erscheint im Bericht je nach zitiertem
  Fact — oder gar nicht.
- **`trust_conflicts()`** hebt das Paar `authoritative` ↔ `user_asserted`
  gesondert heraus.

### 6.1 CONTRADICTION_CHECKER

`prompts/contradiction_checker.md`

Input: alle akzeptierten Facts aller Subtasks, jeweils mit `trust`. Kein Tool.
Output: Liste von Paaren `{fact_a, fact_b, nature, note}`.

Prompt-Fokus: Widersprüche **benennen, nicht auflösen**. Besonders zwischen
`authoritative` und `user_asserted`.

Code setzt anschließend die `contradicts`-Referenzen in beiden Facts.

### 6.2 Reflexions-Generator (Code, kein LLM)

`werkbank/v2/reflection.py`

Baut aus dem RunState:
- `unresolvable`-Subtasks mit ihrer Frage
- offene `gaps` mit `reason`
- alle `contradicts`-Paare
- Facts mit `evidence: "model_knowledge"`
- Subtasks am Revisions-Cap
- Kriterien mit Verdict `partial` / `unmet`
- Verteilung der Facts nach `source_trust`
- geflaggte Absätze aus D9

Ausgabe: fertiger Markdown-Block. **Der Writer darf ihn nicht verändern.**

### 6.3 WRITER

`prompts/writer.md`

Input: Brief, alle akzeptierten Facts, Subtask-Übersicht, generierter
Reflexionsblock.

Regeln im Prompt:
- Jeder Absatz trägt mindestens einen Fact-Marker `[st3.f1]`
- Keine Aussage, die nicht durch einen Fact gedeckt ist
- Der Reflexionsblock wird **unverändert** eingesetzt; Kommentierung davor oder
  danach ist erlaubt, Kürzung nicht

Struktur: Titel · Auftraggeber · Modell · Datum · Dauer → Inhaltsverzeichnis →
Aufgabenstellung (Original + Brief) → Subtasks mit Agenten → Bericht →
Selbstreflexion → Quellenverzeichnis mit `trust`-Kennzeichnung.

Nachbearbeitung im Code: D6 und D9 anwenden, Reflexionsblock gegen das Original
diffen und bei Abweichung durch das Original ersetzen.

### Akzeptanzkriterien Phase 6

- Konstruierter Fall: Vault-Notiz ("Kündigungsfrist 3 Monate") widerspricht
  Paperless-Dokument ("6 Wochen") → `contradicts`-Eintrag, im Bericht sichtbar,
  beide Trust-Stufen genannt
- Jeder Absatz des Berichts trägt einen Fact-Marker; ein absichtlich
  eingeschmuggelter markerlos generierter Absatz wird geflaggt
- Ein Run mit einem `unresolvable`-Subtask: die Reflexion nennt ihn namentlich,
  auch wenn der Bericht ansonsten vollständig wirkt
- Manipulationstest: Writer-Output mit gekürztem Reflexionsblock wird im Code
  erkannt und ersetzt
- Quellenverzeichnis unterscheidet sichtbar `authoritative` / `user_asserted` /
  `external`

---

## Phase 7 — UI-Integration ✅ erledigt (v2 ist live)

Geliefert: `werkbank/v2/ui/page.py` (die Seite `/werkbank`),
`werkbank/v2/pipeline.py`, `werkbank/v2/prompts.py`, Board und Stil aus dem
ersten Teil dieser Phase, Restore-Defaults in `werkbank/archetypes.py` +
`werkbank/ui/archetype_dialog.py`, `tests/test_werkbank_v2_ui.py`,
`tests/test_werkbank_v2_pipeline.py`, `tests/test_werkbank_v2_prompts.py`.

Erledigt:

- **Board** pro Subtask: Agent, Status, Revision, Critic-Verdikt je Kriterium,
  Lücken, Revisions-Cap-Hinweis, Fortschrittsbalken.
- **Fact-Marker im Bericht sind klickbar** und öffnen den Fact mit Evidenz,
  Trust-Stufe, wörtlichem Beleg und Widerspruchs-Referenz. Umgesetzt als
  HTML-Nachbearbeitung mit *einem* delegierten Listener — ein Element pro
  Textstück macht aus einem Absatz einen Stapel Blöcke, und der Fließtext hört
  auf, Fließtext zu sein.
- **Reflexionsblock** wird optisch abgesetzt (`.wb-reflection`), damit er als
  Befund gelesen wird und nicht als Schlusswort.
- **„Auf Standard zurücksetzen"** pro Archetyp (nur sichtbar, wenn er
  tatsächlich abweicht) und global. Funktioniert auch, wenn der Archetyp
  *gelöscht* wurde — das ist der Fehler, den Leute wirklich machen.
- **Stil:** Farbe kodiert Zustand statt Identität (neutral / accent für „läuft" /
  warn für „du musst ran"), Theme-Tokens statt Hex, Kleinschreibung statt
  Versalien, kein Querscrollen bei 390 px (gemessen, nicht gelesen).
- **Seite `/werkbank` ist v2.** Lauf-Liste → Auftrag → Bestätigungsdialog →
  Hintergrundlauf → Board + Bericht. `main.py` importiert `werkbank.v2.ui.page`.
- **Fortschritt kommt aus dem Store, nicht aus einem Callback.** Die Seite pollt
  alle 4 s. Das ist kein Kompromiss, sondern die Konsequenz daraus, dass ein Lauf
  den Tab überlebt: der `progress`-Callback des Schedulers erreicht nur die
  Sitzung, die ihn gestartet hat, der Store erreicht jede.
- **Nach einem Neustart ist ein Lauf fortsetzbar, nie automatisch fortgesetzt.**
  `store.reset_stale_runs()` setzt hängengebliebene `running`-Zeilen beim Start
  auf `planned`; jeder Subtask kostet Modellaufrufe, also entscheidet der Nutzer.
- **Export:** Bericht als Markdown herunterladen, oder als PDF nach Paperless
  ablegen (`werkbank.export.upload_pdf`) — auf Knopfdruck, nie automatisch.
  Review-vor-Persist bleibt gültig.
- **Rollen-Prompts sind editierbar** (`werkbank/v2/prompts.py`, Einstellungen →
  KI-Tiefenrecherche), inklusive Token-Limit pro Rolle und Reset auf den
  ausgelieferten Text. Ein Override ändert, was eine Rolle *gefragt* wird — was
  *geprüft* wird, liegt in `checks.py` und ist von dort nicht erreichbar. Ein
  Test pinnt genau diese Grenze.
- **Chat-Übergabe** (`create_kanban_task`) startet einen v2-Lauf.

Zum v1-Pfad:

Der v1-Ausführungspfad (`orchestrator.py`, `scheduler.py`, `roles/`,
`compaction.py`, `prechecks.py`, `ui/module_page.py`, `ui/task_dialog.py`) ist
aus **jedem** Live-Pfad entfernt und mit einem `DEPRECATED`-Header versehen,
liegt aber noch eine Release lang auf der Platte. Grund: diese Phase macht die
Entfernung davon abhängig, dass v2 einen vollständigen Lauf auf echten Daten
bestanden hat — und dieser Lauf ist erst der erste echte. Scheitert er, bringen
zwei Import-Zeilen in `main.py` v1 zurück. `tests/test_werkbank_v2_prompts.py`
hält die Live-Pfade frei von v1-Importen, damit sich in der Zwischenzeit keiner
zurückschleicht.

Weiterhin nur mit echtem Modell prüfbar (nicht automatisierbar):

- Vollständiger Run über eine echte, komplexe Aufgabe von Brief bis Bericht
- Jeder Fact-Marker im Bericht führt zur richtigen Quelle
- Run mit deaktiviertem Mail- und Kalender-Tool läuft sauber durch und
  dokumentiert die Lücken

**Danach:** v1-Module löschen.

---

## Querschnitt: Verbote

Gilt in **allen** Phasen. Verstöße sind Bugs, keine Stilfragen.

- LLM setzt nie: `source_trust`, `self_check`, `confidence`, `hits`
- Keine numerische Confidence vom Modell abfragen
- FACT_CRITIC bekommt keine Tools und schreibt keine Facts
- FACT_CRITIC bekommt nie das `narrative` des Runners
- Kein Agent kombiniert zwei externe Quellen — dafür `synthesizer`
- Fehlende Quelle → `gap`, niemals Fallback auf Parameterwissen
- Kein Retry ohne Cap
- Reflexionsblock wird nicht vom LLM umgeschrieben
- Keine thema-basierten Agenten ("Jurist", "Analyst")
- Deterministische Checks laufen **vor** dem LLM-Critic und sind nicht
  überstimmbar
