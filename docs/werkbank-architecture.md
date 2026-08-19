# KI-Werkbank v2 — Architektur & Umsetzung

> **Maßgebliches Dokument.** Umsetzungsstand und Reihenfolge:
> `werkbank-tasks.md`. Bestandsaufnahme, Tool-Inventar und die Zuordnung der
> Tools auf die Archetypen: `werkbank-v2-findings.md`. Das laufende v1 ist in
> `werkbank-v1-legacy.md` beschrieben und bleibt bis Phase 7 in Betrieb.
>
> Zwei Punkte sind gegenüber dem Erstentwurf präzisiert, beide begründet in den
> Findings: die Trust-Stufe von Paperless-Ergebnissen hängt vom *Tool* ab (eine
> Sidecar-Zusammenfassung ist `derived`, kein Wortlaut), und der PLANNER
> bekommt eine siebte Regel für Vergleichsaufgaben.

Überarbeitung des Multi-Agent-Workflows. Ziel ist nicht mehr Durchsatz, sondern
**belegbare Wahrhaftigkeit**: jede Aussage im Endbericht ist entweder auf eine
abgerufene Quelle rückführbar, aus anderen Facts abgeleitet, oder explizit als
Lücke markiert.

Leitsatz für alle folgenden Entscheidungen:

> Ehrlichkeit entsteht nicht durch Prompt-Anweisungen, sondern dadurch, dass das
> Schema einen Platz für "weiß ich nicht" hat und deterministischer Code prüft,
> was das LLM behauptet.

---

## 1. Pipeline

```
USER
 │
 ├─ BRIEFER ──────────────► Brief (goal, criteria, depth_budget, assumptions)
 │
 ├─ USER CONFIRM ─────────► Bestätigung / Korrektur (UI)
 │
 ├─ PLANNER ──────────────► DAG aus Subtasks (Agent + Abhängigkeiten)
 │
 ├─ PLAN_CRITIC ──────────► accept | revise (max. 1 Revision)
 │
 ├─ SCHEDULER ────────────► Level-weise Ausführung gegen Semaphore
 │   │
 │   └─ pro Subtask:
 │        RUNNER ─────────► SubtaskResult (facts[], gaps[], narrative)
 │        DET_CHECKS ─────► deterministisch, nicht überstimmbar
 │        FACT_CRITIC ────► accept | revise | unresolvable
 │        └─ bei revise: zurück an RUNNER (max. depth_budget.max_revisions)
 │
 ├─ CONTRADICTION_CHECK ──► einmalig über alle Facts
 │
 └─ WRITER ───────────────► Bericht (Claims nur aus Facts, Reflexion aus Daten)
```

Reformulierer und Auftragsplaner der alten Version sind zu **BRIEFER**
zusammengefasst; Auftragsplaner und Zuweiser zu **PLANNER**. Begründung: jede
Umformulierungsstufe ist eine Gelegenheit für semantische Drift ohne
Erkennungsmechanismus. Wer Aspekte identifiziert, muss ohnehin wissen, womit sie
beantwortet würden.

**Invariante:** Der Originalwortlaut des Users wird verbatim durch die gesamte
Pipeline getragen (`original_request`) und liegt im Kontext beider Critics. Der
Brief ist additiv, nicht ersetzend.

---

## 2. Rollen

### 2.1 BRIEFER

Ein Call. Erzeugt den `Brief` und ersetzt die reine Prosa-Reformulierung.

```json
{
  "original_request": "wörtlich, unverändert",
  "goal": "Was am Ende beantwortet sein muss",
  "out_of_scope": ["explizit nicht Teil der Aufgabe"],
  "deliverable_format": "Bericht | Tabelle | Liste | Zusammenfassung",
  "assumptions": ["Annahme, die die Aufgabe verengt oder auslegt"],
  "acceptance_criteria": [
    "nennt alle Fristen mit Datum und Quelldokument",
    "unterscheidet vertraglich fixierte von gesetzlichen Fristen"
  ],
  "depth_budget": "standard"
}
```

**`acceptance_criteria` sind der wichtigste Hebel des gesamten Systems.** Ohne
vorregistrierte Kriterien erfindet der FactCritic den Maßstab post-hoc und
besteht immer — das ist Standardverhalten von LLM-Self-Evaluation, kein
Randfall.

Constraint im Prompt: jedes Kriterium beginnt mit einem Verb und benennt ein
prüfbares Artefakt. Nicht-prüfbare Kriterien ("umfassende Antwort") lehnt der
PLAN_CRITIC ab.

**`depth_budget` ist ein Enum, kein Freitext** — das LLM hat kein Gefühl für
Laufzeit- und Tokenkosten. Im Bestätigungsdialog vom User änderbar:

| Wert | max_subtasks | max_revisions | plan_critic |
|---|---|---|---|
| `quick` | 3 | 0 | übersprungen |
| `standard` | 8 | 1 | ja |
| `deep` | 20 | 2 | zwingend |

Damit ist das Complexity-Gate erledigt; ein separater Mechanismus entfällt.

### 2.2 USER CONFIRM

UI-Schritt, kein LLM-Call. Prominent darzustellen sind `assumptions` und
`acceptance_criteria` — die Reformulierung verengt Aufgaben still, das ist die
häufigste Fehlerquelle. `depth_budget` als Dropdown editierbar.

### 2.3 PLANNER

Erzeugt den vollständigen DAG in einem Call: Subtasks, Agent-Zuweisung,
Abhängigkeiten.

**Zuweisungsregeln — wörtlich in den Prompt:**

1. **Genau ein Agent pro Subtask.** Braucht eine Frage zwei Agenten, wird sie
   gesplittet und ein `synthesizer` nachgeschaltet.
2. **Kein Subtask ohne Agent.** Ausnahme: `synthesizer` und
   `contradiction_checker`, deren Input Facts sind.
3. **Nicht verfügbare Tools existieren nicht.** Die Registry wird zur Laufzeit
   nach User-Konfiguration gefiltert. Ist eine Frage nur mit einem fehlenden
   Tool beantwortbar → `gaps`-Eintrag mit `reason: "source_unavailable"`. Kein
   Ausweichen auf Parameterwissen.
4. **Jeder Subtask trägt eigene `acceptance_criteria`**, abgeleitet aus dem
   Brief. Ein Subtask ohne Kriterium ist nicht planbar.
5. **`contradiction_checker` läuft genau einmal am Ende**, nie pro Subtask.
6. Jedes Brief-Kriterium muss von mindestens einem Subtask adressiert werden
   (`covers_criteria: [0, 2]`).

### 2.4 PLAN_CRITIC

Regeln 1–6 sind **mechanisch verifizierbar und laufen als Code**, nicht als
Prompt. Dem LLM bleibt genau eine Frage, die echtes Urteil braucht:

> Welches Abnahmekriterium wird durch die zugewiesenen Agenten **nicht**
> hinreichend abgedeckt?

Erzwungenes Verdict pro Kriterium: `covered` / `partial` / `uncovered` + die
Subtask-IDs als Beleg. Kein Freitext-Gesamturteil. Max. 1 Replanning-Runde,
danach läuft der Plan wie er ist (die Schwäche landet in der Reflexion).

### 2.5 RUNNER

Führt einen Subtask mit genau einem Agenten aus. Priorität: Richtigkeit,
Halluzinationsverbot, Benennung von Unbeantwortbarem.

Bei `depends_on` bekommt der Runner **nur die `facts` der Vorgänger**, nie deren
`narrative` oder Rohtranskripte. Nebeneffekt: der Kontextverbrauch bleibt auch
bei tiefen DAGs beherrschbar.

### 2.6 FACT_CRITIC

Läuft auf demselben Modell wie der Runner (nur ein Modell verfügbar). Die
Gegenmaßnahmen gegen Self-Approval-Bias tragen deshalb die volle Last:

- **Deterministische Checks laufen zuerst und sind nicht überstimmbar** (§4).
- **Der Critic sieht das `narrative` des Runners nicht.** Input ist
  ausschließlich: Frage, `acceptance_criteria`, Fact-Liste, Rohsnippets der
  Quellen. Die Begründungskette des Runners ist der Haupttransportweg für
  "klingt überzeugend, also stimmt es".
- **Frage auf Mangel präsupponiert.** Nicht "ist das korrekt?", sondern pro
  Fact: *"Welcher Teil des Belegzitats stützt diese Aussage nicht?"* Bei
  identischem Modell ist das der wirksamste Einzelhebel.
- `temperature: 0.1` (Runner darf höher).
- Erzwungenes Verdict pro Abnahmekriterium: `met` / `partial` / `unmet` + Fact-IDs.
  **Ein Kriterium ohne referenzierte Fact-ID ist automatisch `unmet`.**
- Bei `kind != "statement"` (Tabellen, Listen): stichprobenartige Prüfung
  einzelner Zellen gegen die Quellen.

**Der FactCritic recherchiert nicht selbst.** Sonst bewertet er in der nächsten
Runde seine eigene Arbeit und die Provenienz geht verloren. Erlaubte Outputs:

- `accept`
- `revise` + konkrete, prüfbare Mängelliste → zurück an den Runner
- `unresolvable` → Subtask endet, landet sichtbar im Bericht

**Revisions-Cap aus `depth_budget`.** Ohne Cap gibt es Ping-Pong, und der
Bericht kann nie sagen "nicht beantwortbar" — was die gewünschte Ehrlichkeit
technisch unmöglich machen würde.

### 2.7 CONTRADICTION_CHECKER

Pflicht-Pass am Ende über alle akzeptierten Facts. Kein Tool-Zugriff. Setzt
`contradicts`-Referenzen. Separat vom Synthesizer, weil seine Prompt-Haltung
adversarisch ist und er nicht gleichzeitig konsolidieren soll.

Besonders relevant: `authoritative` vs. `user_asserted` (§5) — veraltete eigene
Notizen gegen echte Dokumente. Das ist praktisch der wertvollste Output des
gesamten Systems.

### 2.8 WRITER

Zwei harte Regeln:

1. **Keine neuen Claims.** Jeder Absatz trägt Fact-Marker `[st3.f1]`. Absätze
   ohne Marker werden automatisch geflaggt.
2. **Die Selbstreflexion wird aus Daten generiert, nicht vom LLM erfunden.**

Der Reflexionsabschnitt wird von Code aus dem Run-State gebaut:

- Anzahl `unresolvable`-Subtasks (mit Frage)
- alle offenen `gaps` mit `reason`
- alle `contradicts`-Paare
- Facts mit `evidence: "model_knowledge"`
- Subtasks am Revisions-Cap
- Kriterien mit Verdict `partial` / `unmet`
- Verteilung nach `source_trust`

Das LLM darf diesen Block kommentieren, aber nicht kürzen oder umschreiben.
Deterministische Ehrlichkeit schlägt performte Ehrlichkeit — sonst schreibt das
Modell drei wohlklingende Sätze über "Limitationen dieser Analyse" und
verschweigt, dass Subtask 4 nichts gefunden hat.

**Berichtsstruktur:** Titel · Auftraggeber · Modell · Datum · Dauer →
Inhaltsverzeichnis → Aufgabenstellung (Original + Brief) → Subtask-Übersicht mit
Agenten → Bericht → **Selbstreflexion (generiert)** → Quellenverzeichnis mit
`source_trust`-Kennzeichnung.

---

## 3. Datenmodell

Prosa bleibt erhalten, aber **abgeleitet**: Facts sind das zitierfähige
Substrat, `narrative` darf ausschließlich Fact-IDs referenzieren. Sonst schreibt
der Writer Prosa aus Prosa aus Prosa.

### 3.1 SubtaskResult

```json
{
  "subtask_id": "st3",
  "revision": 2,
  "status": "ok | partial | unresolvable",
  "question": "Welche Fristen ergeben sich aus dem Mietvertrag?",
  "acceptance_criteria": ["nennt jede Frist mit Datum und Quelldokument"],
  "covers_criteria": [0, 2],
  "depends_on": ["st1"],
  "agent": "doc_researcher",
  "sources_restrict": null,
  "model": "qwen3:32b",
  "started_at": "2026-08-17T09:12:03Z",
  "finished_at": "2026-08-17T09:13:05Z",
  "duration_s": 62,

  "facts": [ /* siehe 3.2 */ ],

  "gaps": [
    {
      "question": "Gibt es eine Nebenabrede zur Frist?",
      "reason": "not_found | source_unavailable | ambiguous | conflicting",
      "suggested_source": "comms"
    }
  ],

  "narrative": "Fließtext, darf nur [st3.f1]-Referenzen enthalten",

  "self_check": {
    "claims_without_source": 0,
    "sources_fetched": 4,
    "tool_calls": 6
  }
}
```

`self_check` wird **von Code befüllt, nie vom LLM.** Ein Runner, dessen Facts
alle `model_knowledge` ohne Quelle sind, obwohl Tools verfügbar waren, wird
deterministisch abgelehnt — das ist der Minimax-Bug (Antwort ohne Tool-Nutzung),
hier strukturell erschlagen statt per Prompt bekämpft.

### 3.2 Fact

```json
{
  "id": "st3.f1",
  "kind": "statement | table | list | excerpt | figure",
  "claim": "Aussage. Darf Markdown enthalten, auch Tabellen.",
  "evidence": "quote | computed | derived | model_knowledge | none",
  "expression": "1240 + 890 + 2100",
  "sources": [
    {
      "id": "s7",
      "type": "paperless | vault | web | email | calendar | note",
      "trust": "authoritative | user_asserted | external | computed | model",
      "ref": "doc:1423#p2",
      "retrieved_at": "2026-08-17T09:12:40Z",
      "quote": "wörtliches Belegzitat aus dem abgerufenen Text",
      "query": "correspondent=Stadtwerke&created__year=2025",
      "hits": 17
    }
  ],
  "derived_from": ["st1.f4"],
  "contradicts": ["st2.f9"],
  "confidence": "high | medium | low"
}
```

**Was gegenüber dem ursprünglichen Entwurf geändert wurde und warum:**

- **`certainty: 0.9` → `evidence` + abgeleitete `confidence`.** Numerische
  LLM-Confidence ist nicht kalibriert; praktisch alles landet zwischen 0.85 und
  0.95 und ist nicht prüfbar. `evidence` ist an etwas Überprüfbares gebunden.
  **`confidence` wird im Code aus `evidence` und `source_trust` abgeleitet — das
  LLM wird nicht danach gefragt.**
- **`source: "…"` → Objekt mit `quote`.** Das wörtliche Belegzitat ist der
  einzige *deterministische* Anti-Halluzinations-Check, den es gibt:
  Fuzzy-Match gegen den tatsächlich abgerufenen Text. Kein Match → Fact wird
  programmatisch verworfen, ohne LLM-Urteil.
- **`fact_id` global eindeutig** (`st3.f1` statt `1`) — sonst kann der Writer
  nicht subtask-übergreifend referenzieren.
- **`gaps` als First-Class-Feld.** Zentral: gibt es keinen gebahnten Weg für
  "weiß ich nicht", nimmt das Modell den Weg zu "ja".
- **`contradicts`** — ohne das wählt der Writer bei widersprüchlichen Quellen
  still eine aus.
- **`kind`** — Facts dürfen lang sein (Markdown-Tabellen etc.).

### 3.3 Atomarität

Die Regel lautet nicht mehr "Einzeiler", sondern:

> Ein Fact ist die kleinste Einheit, die **als Ganzes** akzeptiert oder verworfen
> werden kann.

Eine Tabelle aus einer Quelle = ein Fact. Eine Tabelle aus fünf Quellen = ein
`derived` Fact mit vollständiger `derived_from`-Liste, erzeugt vom
`synthesizer`, nicht vom Researcher.

Bei `kind != "statement"` greift Quote-Matching pro Zelle nicht sinnvoll — dann
ist `evidence: "derived"` oder `"computed"` Pflicht und die Quellenliste muss
vollständig sein.

### 3.4 Berechnete Werte

Jedes `calculate`-Ergebnis erzeugt einen Fact mit `evidence: "computed"`,
gefülltem `expression` und `derived_from` auf die Eingangswerte. Damit ist im
Bericht nachvollziehbar, welche Zahl gemessen und welche gerechnet wurde — und
der Check kann die Arithmetik deterministisch nachrechnen, ganz ohne LLM.

Analog bei Paperless-Filterabfragen: kein Belegzitat möglich, daher sind
`query` und `hits` Pflicht.

---

## 4. Deterministische Checks

Laufen **vor** dem FactCritic und sind von ihm nicht überstimmbar. Sie
erschlagen genau die Fehlerklasse, bei der Selbstbewertung am unzuverlässigsten
ist.

| # | Check | Fehlverhalten |
|---|---|---|
| D1 | JSON-Schema-Validierung | Retry (max. 2), dann `unresolvable` |
| D2 | `evidence: "quote"` → Fuzzy-Match ≥ 0.90 gegen abgerufenen Text | Fact verwerfen |
| D3 | `evidence: "computed"` → `expression` vorhanden **und** nachrechenbar, **oder** `query` + `hits` vorhanden | Fact verwerfen |
| D4 | `evidence: "derived"` → `derived_from` nicht leer, alle IDs existieren | Fact verwerfen |
| D5 | Tools waren verfügbar, aber `tool_calls == 0` | Subtask ablehnen, Revision erzwingen |
| D6 | `narrative` referenziert nur existierende Fact-IDs | unbekannte Marker strippen, Absatz flaggen |
| D7 | `sources_restrict` verletzt | Fact verwerfen |
| D8 | Alle Facts verworfen und `gaps` leer | Subtask → `unresolvable` |
| D9 | Writer-Absatz ohne Fact-Marker | flaggen, in Reflexion listen |

Der abgerufene Rohtext jedes Tool-Calls wird pro Subtask zwischengespeichert,
sonst ist D2 nicht durchführbar.

---

## 5. Quellen-Vertrauensstufen

`source_trust` wird **vom aufrufenden Code gesetzt**, abgeleitet aus dem Tool,
das den Treffer geliefert hat. Nie vom LLM, und vom Agenten nicht
überschreibbar.

| Stufe | Bedeutung | Quellen |
|---|---|---|
| `authoritative` | echtes Dokument, unabhängig vom User entstanden | Paperless-Dokumente |
| `user_asserted` | vom User oder Umfeld behauptet, unverifiziert | Vault-Notizen, eigene Mails, Kalendereinträge, Paperless-Notizen |
| `external` | Dritte, Aktualität und Bias unklar | Websuche |
| `computed` | aus Metadaten oder Arithmetik berechnet | Paperless-Filter, calculate |
| `model` | Parameterwissen ohne Beleg | LLM selbst |

Der Unterschied zwischen `authoritative` und `user_asserted` ist der, den Nutzer
am ehesten übersehen: eine Vault-Notiz "Kündigungsfrist 3 Monate" ist kein
Beleg, sondern eine Erinnerung an eine Vermutung. Ein Bericht, der beides gleich
behandelt, ist unehrlich — auch wenn jeder Einzelschritt sauber war.

**Regel:** Ein `authoritative`-Fact und ein widersprechender
`user_asserted`-Fact ergeben zwingend einen `contradicts`-Eintrag, keinen
stillen Vorrang.

---

## 6. Agenten-Registry

Archetypen definieren sich über **Quelle und epistemischen Modus, niemals über
Thema.** Kein "Jurist", kein "Finanzanalyst" — das Modell kann seine eigene
Fachkompetenz nicht einschätzen, und der Planer würde nach Themenwörtern statt
nach Informationsbedarf zuweisen. Fachlichkeit kommt über den Subtask-Prompt.

`current_date` (inkl. Wochentag und KW) wird in **jeden** Prompt injiziert und
ist kein Agent.

`calculate` ist ein **Tool für alle Agenten**, kein eigener Agent. Ein
Rechner-Agent würde nur bedeuten, dass Zahlen den Kontext wechseln — das erzeugt
Fehler, statt sie zu verhindern.

| ID | Tools | Trust | Kernrisiko |
|---|---|---|---|
| `doc_researcher` | Paperless semantisch + Metadatenfilter, Notizen, Vault-Suche, calculate | `authoritative` / `user_asserted` / `computed` | Vault-Notiz wird als Faktum behandelt |
| `web_researcher` | SearXNG + Fetch (trafilatura/Crawl4AI), calculate | `external` | zitiert aus Snippet statt Volltext |
| `comms_researcher` | IMAP/Gmail, CalDAV, calculate | `user_asserted` | Locale-Ordner → falsches "nichts gefunden" |
| `synthesizer` | keine externen — nur Facts, calculate | `derived` | schmuggelt neue Claims ein |
| `contradiction_checker` | keine — nur Facts | `derived` | glättet Widersprüche |

### 6.1 doc_researcher

Semantische Suche und Metadatenfilter sind zwei Modi desselben Tools. Ein Agent
kann damit auch das Naheliegende: erst filtern, dann semantisch innerhalb der
Treffermenge suchen. Getrennte Agenten hätten das nie zusammengebaut.

Bei jeder Dokumentensuche wird **automatisch eine semantische Vault-Suche
mitgefeuert** und in die Ergebnisse gespielt — genau dort entstehen die
Widersprüche, die sichtbar werden sollen.

Der Unterschied zwischen Paperless und Vault liegt nicht im Agenten, sondern in
`source_trust` (§5), gesetzt vom Code.

Subtask-Flag `sources_restrict: ["paperless"]` schaltet die Vault-Beimischung
ab — für rechtlich oder finanziell heikle Fragen.

### 6.2 web_researcher

Zwei Pflichtregeln:

- Zitate ausschließlich aus **gefetchtem Volltext**, nie aus SearXNG-Snippets.
- `retrieved_at` ist Pflicht. Zwingt den Writer zu korrekten Aktualitätsangaben
  statt "laut aktuellen Informationen".

### 6.3 comms_researcher

E-Mail und Kalender sind beide zeitlich verankerte persönliche Streams, und die
typischen Fragen brauchen sie gemeinsam ("Wann haben wir den Termin vereinbart,
und steht er im Kalender?"). Getrennt hätte jede solche Frage einen
Synthesizer-Subtask erzwungen.

Prompt-Hinweise (keine Architekturfragen, aber bekannte Fehlerquellen):

- Gmail-Ordner sind locale-abhängig (`[Google Mail]/Alle Nachrichten`) →
  SPECIAL-USE `\All`-Flag zur Discovery, modified UTF-7 beachten.
- Bei `X-GM-EXT-1` in CAPABILITY: `SEARCH X-GM-RAW`.
- Serientermine und Zeitzonen explizit auflösen, bevor ein Fact entsteht.

### 6.4 synthesizer / contradiction_checker

Kein Zugriff auf externe Quellen, nur auf Facts vorheriger Subtasks. Dadurch
deterministisch prüfbar (D4). Sie sind das Ventil für Fragen, die mehrere
Quellen bräuchten: statt eines Multi-Source-Agenten (Provenienz wird matschig,
Quote-Matching unmöglich) baut der Planer zwei Researcher plus einen
Synthesizer.

### 6.5 Registry-Format

**YAML mit Laufzeit-Filterung**, nicht als Python-Konstante — bei ~40 Nutzern
mit je eigener Tool-Verfügbarkeit (Mail, Kalender, Web optional) muss die
Registry pro User gefiltert werden, und die Prompt-Fragmente sollen ohne Deploy
editierbar sein.

```yaml
# config/agents.yaml
agents:
  doc_researcher:
    label: "Dokumenten-Recherche"
    tools: [paperless_semantic, paperless_filter, paperless_notes, vault_search, calculate]
    requires: [paperless]          # Agent entfällt, wenn nicht konfiguriert
    default_trust:
      paperless_semantic: authoritative
      paperless_filter: computed
      paperless_notes: user_asserted
      vault_search: user_asserted
    prompt_file: prompts/agents/doc_researcher.md
```

Der PLANNER bekommt die **gefilterte** Registry als Teil seines Prompts.

---

## 7. Scheduler

Ein Modell für alles, entweder lokal oder Cloud — kein Routing im Plan,
`backend` existiert nicht im Subtask-Schema.

```python
CONCURRENCY = {"local": 2, "cloud": 6}   # local: VRAM-gebunden (RTX 5090)
```

Level-weise Abarbeitung des DAG: alle Subtasks mit erfüllten `depends_on` gehen
gegen eine `asyncio.Semaphore`. Bei lokalem Backend faktisch serielle
Abarbeitung mit Doppelspur — bei komplexen Aufgaben akzeptabel.

Persistenz in SQLite (WAL), analog zur bestehenden Werkbank: Run, Subtask,
Fact, CriticVerdict. Der Run muss nach Neustart fortsetzbar sein; abgeschlossene
Subtasks werden nicht erneut ausgeführt.

---

## 8. Umsetzungsphasen

Jede Phase ist end-to-end verifizierbar. `/clear` zwischen den Phasen.

### Phase 1 — Datenmodell und deterministische Checks

Kein LLM. Pydantic-Modelle für `Brief`, `Subtask`, `SubtaskResult`, `Fact`,
`Source`, `Gap`, `CriticVerdict`. Checks D1–D9 als reine Funktionen.
SQLite-Schema + Migration.

*Akzeptanz:* Unit-Tests mit handgeschriebenen Fixtures — ein Fact mit
manipuliertem Quote wird von D2 verworfen; `computed` ohne `expression` und ohne
`query` wird von D3 verworfen; Arithmetik in `expression` wird nachgerechnet;
`derived_from` mit unbekannter ID scheitert an D4.

### Phase 2 — BRIEFER + Bestätigungs-UI

*Akzeptanz:* Drei reale Aufgaben ergeben valide Briefs; `depth_budget` ist ein
Enum-Wert; jedes `acceptance_criterion` beginnt mit einem Verb; die UI zeigt
`assumptions` prominent und erlaubt Korrektur; `original_request` bleibt
unverändert im State.

### Phase 3 — Registry + PLANNER + PLAN_CRITIC

*Akzeptanz:* Bei deaktiviertem Mail-Tool taucht `comms_researcher` weder in der
Registry noch im Plan auf, und eine nur per Mail beantwortbare Frage erzeugt
`gaps.reason = "source_unavailable"`; Regeln 1–6 werden als Code geprüft, nicht
als Prompt; der DAG ist zyklenfrei; jedes Brief-Kriterium ist von mindestens
einem Subtask abgedeckt.

### Phase 4 — Ein Runner (`doc_researcher`) + FACT_CRITIC + Loop

*Akzeptanz:* Ein Subtask erzeugt schema-valide Facts mit echten Belegzitaten,
die D2 bestehen; ein absichtlich unbeantwortbarer Subtask ("Was steht im nicht
existierenden Dokument X?") endet als `unresolvable` mit `gaps`-Eintrag statt
mit erfundenem Inhalt; der Revisions-Cap greift; der Critic sieht das
`narrative` nachweislich nicht (Prompt-Log prüfen).

### Phase 5 — Scheduler + restliche Runner

*Akzeptanz:* Ein Plan mit paralleler und serieller Abhängigkeit läuft korrekt;
Concurrency-Limit wird eingehalten; ein abhängiger Subtask erhält nur Facts,
kein `narrative`; Run überlebt einen Neustart.

### Phase 6 — CONTRADICTION_CHECKER + WRITER

*Akzeptanz:* Ein konstruierter Fall (Vault-Notiz widerspricht Paperless-Dokument)
erzeugt einen `contradicts`-Eintrag und erscheint im Bericht; jeder Absatz des
Berichts trägt Fact-Marker; die Selbstreflexion listet nachweislich alle
`unresolvable`-Subtasks und offenen `gaps`; das Quellenverzeichnis kennzeichnet
`source_trust`.

### Phase 7 — UI-Integration

Kanban-Board zeigt Subtask-Status, Revisionen, Critic-Verdicts. Fact-Marker im
Bericht sind auf die Quelle klickbar.

---

## 9. Do not

- Keine numerische Confidence vom LLM abfragen.
- Kein Agent, der zwei externe Quellen kombiniert — dafür gibt es den
  `synthesizer`.
- Der FactCritic recherchiert nicht und schreibt keine Facts.
- `source_trust`, `self_check` und `confidence` werden nie vom LLM gesetzt.
- Kein Fallback auf Parameterwissen, wenn eine Quelle fehlt — das ist ein `gap`.
- Die generierte Selbstreflexion wird vom Writer nicht gekürzt oder umformuliert.
- Kein Thema-basierter Agent-Archetyp.
- Kein Retry ohne Cap.
