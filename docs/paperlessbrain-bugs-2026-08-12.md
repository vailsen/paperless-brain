# PaperlessBrain — Bug- & Feature-Backlog (2026-08-12)

Arbeitsliste für die Dev-Session. Jeder Punkt: Symptom → Analyse/Hypothesen → Fix → Akzeptanzkriterien.
Reihenfolge = empfohlene Bearbeitungsreihenfolge (Impact/Aufwand).

---

## Status (Stand der Umsetzung)

Alle fünf Punkte sind umgesetzt. Was **nicht** verifizierbar war, weil es das
laufende Konto braucht, steht unten bei „Offen".

**#1 — Ursache gefunden, und sie ist banaler als jede Hypothese im Dokument:**
`imaplib` kodiert String-Argumente als **ASCII** (`IMAP4._encoding`). Jede Query
mit Umlaut warf damit `UnicodeEncodeError` *innerhalb* der Bibliothek — gefangen
vom umgebenden `except`, ununterscheidbar von „keine Mail gefunden". Das galt für
alle drei Pfade: X-GM-RAW, `SEARCH CHARSET UTF-8` und den ASCII-Fallback. Der
Gmail-Index war nie das Problem; die Query hat den Server nie erreicht.
Belegt in `tests/test_imap_search.py::test_umlaut_query_reaches_the_server`.

**#1 Nachtrag (erster Live-Lauf).** Die Query erreichte den Server, aber die
exakte Suche lieferte weiterhin 0 → der Prefix-Fallback sprang an und die
17 Treffer für „Drehmoment" waren Motorenmails von 2011. Zwei weitere Fehler,
beide gefixt:

- **Gmail dekodiert 8-Bit in Anführungszeichen nicht als UTF-8.** Es antwortet
  `OK` und matcht nichts. Nicht-ASCII muss als **IMAP-Literal** gesendet werden
  (`SEARCH CHARSET UTF-8 X-GM-RAW {23}\r\n<bytes>`) — die einzige Form, die
  RFC 3501 dafür kennt. `_search_literal()`, mit Fallback auf die Bytes-Variante.
- **Sortierung war umgekehrt.** `_fetch_messages` drehte in *beiden* Zweigen
  seines Ternärs die Liste um und machte damit die bereits sortierte Seite der
  Fast Paths („neueste 5") wieder zu den ältesten 5. Daher 2011 oben.
  Neu: `preserve_order=True` an den beiden Fast-Path-Aufrufen.
- Zusätzlich: Ranking lief **nach** der Paginierung, konnte einen Treffer auf
  Rang 12 also nie nach vorn holen. Im Fallback-Fall wird jetzt ein breiteres
  Fenster (20–40) geholt, gerankt und erst dann geschnitten.

**#2** — Kontext-Header (`[Ordner] Datei › Überschrift`) für *beide* Collections
in `vault/context.py`, plus `rel_path`/`folder`/`filename`/`title` als Metadata,
`EMBEDDING_SCHEMA_VERSION` mit automatischem Reindex bei Versionssprung und
manuellem Trigger in den Einstellungen (mit Fortschrittsanzeige).

**#3** — Abbrechen-Button + ESC, Reset beim Öffnen *und* bei jedem Schließen
(`on_value_change`, fängt auch Overlay-Klick), Schnellmemo als zweiter,
gleichwertiger Aufnahmeknopf mit App-Level-Task, Semaphore(1) und Notice-Queue.

**#4** — `tool_choice` wird jetzt explizit mitgeschickt, Guard mit genau einem
Retry, geschärfte Systemprompt-Regel, `supports_tools` /
`force_tool_first_turn` pro Modell, „no tools"-Markierung in der Modell-Liste.

**#5** — Option B: og:image-Nachladen für die Top-5 ohne Bild (gecacht, nur
`<head>`), Platzhalter liegt permanent unter dem Bild → kein Layout-Sprung und
kein kaputtes Bild-Icon mehr.

### Offen (braucht das echte Konto / einen Browser)

- Regressionslauf „Drehmomentschlüssel" gegen das echte Postfach →
  `python scripts/imap_debug.py <user> <token> "Drehmomentschlüssel"`.
  `--raw <uid>` legt Rohmail und extrahierten Text nebeneinander ab, falls die
  Mail trotzdem fehlt.
- Minimax M3 mit frischem Kontext gegenprüfen (Guard-Log: „Tool-use guard: …").
- Schnellmemo mit echtem Whisper + Seitenwechsel direkt nach dem Stopp.
- Optischer Check der neuen Bedienelemente auf dem Telefon (390 px).

Priorisierung:

1. **P1** — Email-Suche findet vorhandene Mails nicht (Kernfunktion kaputt)
2. **P1** — Vault-Embeddings ohne Pfad/Dateiname (Retrieval-Qualität, braucht Reembedding)
3. **P2** — Voice-Memo: Abbruch fehlt + State-Leak + Schnellmemo-Modus
4. **P2** — Modelle ignorieren offensichtlichen Tool-Bedarf (Minimax M3)
5. **P3** — Websearch-Vorschaubilder inkonsistent

---

## 1. [P1] Email-Tool: globale Suche findet vorhandene Mail nicht

### Symptom

Query: „Wie heißt der Drehmomentschlüssel, den ich gekauft habe?"

- Iter 1: `{"query": "Drehmomentschlüssel", "max_results": 10}` → **0 Treffer**, gesucht in `[Google Mail]/Alle Nachrichten`
- Mehrere Folgeversuche mit verwandten Begriffen → ebenfalls nichts
- `{"list_folders_only": true}` → liefert Ordnernamen **roh in modified UTF-7**: `Bestellvorg&AOQ-nge und Rechnungen`
- Agent nutzt diesen rohen String als `folder`-Parameter

Gegenprobe: manuelle Suche in der Gmail-App nach „Drehmomentschlüssel" → **3 Mails**.

**Entscheidender Punkt:** Das Wort steht in der Hauptmail **nirgends vollständig im sichtbaren Text**.

- Subject: `Bestellt: VANPO 3/8 Zoll…` — von Amazon abgeschnitten
- Body: `…Drehmomentschlüsse…` — ebenfalls von Amazon abgeschnitten, das `l` am Ende fehlt
- Dritte Mail (voelkner): Wort offenbar nur in einem Bild

Eine reine Substring-Suche nach `Drehmomentschlüssel` **kann** diese Mail also nicht finden. Insofern: ja, legitim. Die eigentliche Frage ist, warum Gmail sie trotzdem findet.

### Analyse

**Schritt 0 — erst messen: steht der volle String im Rohquelltext?**

Amazon-Bestellmails führen den vollständigen Produkttitel typischerweise noch an Stellen, die im gerenderten Text nicht auftauchen:

- Link-Slug (`amazon.de/Drehmomentschlüssel-VANPO-…/dp/…`, ggf. percent-encoded)
- `alt`-Attribut des Produktbilds
- versteckter Preheader-Div
- `X-`-Header oder JSON-LD-Block

Das ist die wahrscheinlichste Erklärung dafür, dass Gmail die Mail findet.

→ **Diagnose zuerst**: die konkrete Mail per `FETCH <uid> BODY.PEEK[]` roh ziehen, in Datei speichern, dann
`grep -ic drehmoment` auf (1) den Rohbytes, (2) den quoted-printable-dekodierten Bytes, (3) dem extrahierten Plaintext, den unser Tool an den Matcher gibt.
Das Ergebnis entscheidet, welcher Fix greift — **nicht raten**:

- Voll im Rohtext, aber nicht im extrahierten Plaintext → **(c)**, Extraktion wirft die Fundstelle weg
- Nur quoted-printable-kodiert vorhanden → **(c)**, Decoding fehlt
- Gar nicht vorhanden → **(b)**, nur ein Index mit Stemming/Prefix-Matching kann helfen

**(a) Umlaut / CHARSET in IMAP SEARCH — unabhängig davon prüfen**
IMAP `SEARCH` mit Nicht-ASCII-Suchbegriff erfordert die Form
`SEARCH CHARSET UTF-8 TEXT {23+}\r\nDrehmomentschlüssel` (Literal, byte-gezählt).
Ohne `CHARSET UTF-8` antwortet Gmail je nach Client-Lib mit `BAD` oder still mit leerer Trefferliste. `imaplib.IMAP4.search()` macht das **nicht** automatisch.
→ Test: Suche nach `Drehmoment` (reines ASCII, und garantiert im Text enthalten). Kommt auch das nicht zurück, liegt es zusätzlich am Charset-Handling. Kommt es zurück, ist Charset nicht das Problem und es geht um (b)/(c).

**(b) Gmail sucht anders als IMAP `TEXT`**
Googles Index macht Tokenisierung, Stemming, Kompositazerlegung und Prefix-Matching — `Drehmomentschlüsse` matcht dort gegen die Query `Drehmomentschlüssel`. Dazu OCR auf Bildern (erklärt die voelkner-Mail). IMAP `TEXT` ist dagegen ein stumpfer Substring-Match.
→ **Fix**: Wenn `CAPABILITY` das Flag `X-GM-EXT-1` meldet, `SEARCH X-GM-RAW "<query>"` verwenden. Damit läuft die Suche über Googles Index — identische Ergebnisse wie in der App. Das ist bei dieser Faktenlage nicht mehr „nice to have", sondern **der** Fix für Gmail-Accounts.

**(b2) Fallback für Nicht-Gmail: Prefix-Strategie für deutsche Komposita**
Ohne Server-Index bleibt nur, die Query selbst robuster zu machen. Deutsche Komposita werden in Betreffzeilen und Produkttiteln notorisch abgeschnitten.
→ Zweistufig suchen: erst der volle Term, bei 0 Treffern automatisch ein gekürzter Stamm. Regel: Terme über ~12 Zeichen auf einen Prefix von ~8–10 Zeichen kürzen (`Drehmomentschlüssel` → `Drehmoment`). Ergebnisse anschließend gegen den vollen Term ranken, damit die Trefferliste nicht verwässert.
→ Bewusst simpel halten: keine Morphologie-Library, keine Wortzerlegung. Prefix-Truncation deckt den Abschneide-Fall ab, und mehr braucht es hier nicht.

**(c) Extraktion und Transfer-Encoding**
Falls das Tool die IMAP-Treffer clientseitig nachfiltert:

- `email.message_from_bytes` → `get_payload(decode=True)` → charset aus Header dekodieren. Nie auf Rohbytes matchen (`Drehmomentschl=C3=BCssel`).
- Bei der HTML→Text-Extraktion **`alt`-Attribute und `href`-URLs erhalten**, nicht wegwerfen. Genau dort steht bei Bestellmails oft der volle Produkttitel. URLs vorher percent-decoden.
- Preheader-Divs (`display:none`) ebenfalls nicht strippen.

**(d) Ordnernamen in modified UTF-7 (RFC 3501 §5.1.3)**
`Bestellvorg&AOQ-nge und Rechnungen` = `Bestellvorgänge und Rechnungen`.
Der Agent bekommt unlesbare Strings zu sehen und rät. Umgekehrt muss ein vom Agent gelieferter Klartext-Ordnername beim `SELECT` wieder encodiert werden.
→ Decode/Encode-Layer an der Tool-Grenze: LLM sieht **immer** UTF-8-Klartext, IMAP sieht **immer** modified UTF-7. Python: `imapclient.imap_utf7.decode/encode` oder eigener Codec.

**(e) Ordner-Discovery ist lokalisierungsabhängig**
`[Google Mail]/Alle Nachrichten` vs. `[Gmail]/All Mail` — hängt von der Spracheinstellung des Accounts ab. Hartcodierte Namen brechen.
→ `LIST` mit SPECIAL-USE-Attributen auswerten und den Ordner mit `\All` als Default-Suchziel nehmen. Fallback: erster Ordner, dessen Name auf `All Mail|Alle Nachrichten` matcht.

### Fix — Aufgaben

- [ ] **Zuerst**: VANPO-Mail roh fetchen (`BODY.PEEK[]`), `grep -ic drehmoment` auf Rohbytes / dekodierten Bytes / extrahiertem Plaintext. Ergebnis notieren — es entscheidet, welche der folgenden Punkte überhaupt relevant sind.
- [ ] Reproduktions-Skript: gleiche Query, IMAP-Verkehr mit `imaplib.Debug = 4` mitloggen
- [ ] Kontrollsuche mit `Drehmoment` (ASCII, garantiert im Text) — trennt Charset-Problem von Matching-Problem
- [ ] Gmail-Pfad: `X-GM-RAW` verwenden, wenn `X-GM-EXT-1` in `CAPABILITY`
- [ ] Generischer Pfad: `SEARCH CHARSET UTF-8 TEXT` mit korrektem Literal-Encoding
- [ ] Fallback-Pfad: bei 0 Treffern automatisch mit gekürztem Prefix (>12 Zeichen → ~8–10) erneut suchen, Ergebnisse gegen den vollen Term ranken
- [ ] Ordnernamen: modified-UTF-7 Decode beim `LIST`, Encode beim `SELECT`
- [ ] Default-Suchordner über SPECIAL-USE `\All` ermitteln statt hartcodiert
- [ ] Falls clientseitige Nachfilterung existiert: MIME korrekt dekodieren (quoted-printable/base64, charset aus Header); bei HTML→Text `alt`, `href` und versteckte Preheader **erhalten**, URLs percent-decoden
- [ ] Tool-Description anpassen: dem Agent klarmachen, dass die Default-Suche bereits global über alle Nachrichten läuft und ordnerbezogene Suche nur eine Einschränkung ist — er soll nicht nach Fehlschlägen auf Ordner-Raten ausweichen
- [ ] Regressionstest mit dem konkreten Fall („Drehmomentschlüssel" → VANPO-Mail muss in Iter 1 kommen)

### Bewusst **nicht** im Scope

- Eigenes OCR auf Mail-Anhänge/inline-Bilder. Mit `X-GM-RAW` erledigt Google das für Gmail-Accounts; für andere Provider ist der Aufwand nicht gerechtfertigt.
- Eigener lokaler Mail-Index. Das wäre eine zusätzliche Persistenzschicht ohne Not — die Suche gehört dem Mailserver.

### Akzeptanzkriterien

- „Drehmomentschlüssel" liefert in **Iteration 1** die VANPO-Mail — trotz abgeschnittenem Wort im Body
- Umlaut-Queries liefern dieselben Treffer wie in der Gmail-App (Stichprobe: 3 Queries mit Umlaut)
- Der Prefix-Fallback greift nachweislich (Log zeigt zweiten Suchlauf) und liefert bei einem Nicht-Gmail-Testaccount ebenfalls einen Treffer
- `list_folders_only` gibt lesbare Klartext-Ordnernamen zurück
- Ein vom Agent gelieferter Klartext-Ordner wird korrekt selektiert

---

## 2. [P1] Vault-Embeddings: Ordnerpfad und Dateiname fehlen

### Symptom / Frage

Beim Embedding der Vault-Dateien: Fließen relativer Pfad und Dateiname in den embeddeten Text ein? Unterordner wie `To-Dos/` tragen eigene semantische Aussagekraft.

### Bewertung

Ja, das gehört rein — mit Einschränkung. `multilingual-e5-large-instruct` bildet den kompletten Input ab; ein kurzer Kontext-Header verbessert Retrieval bei kurzen Chunks deutlich (ein Chunk „Bremsbeläge wechseln" ohne Kontext ist mehrdeutig, mit `To-Dos/Auto.md` eindeutig). Zwei Regeln:

- **Header kompakt halten** (eine Zeile). Bei kurzen Chunks dominiert er sonst den Vektor und alles aus demselben Ordner wird künstlich ähnlich.
- **Pfad zusätzlich als Metadata** speichern (`rel_path`, `folder`, `filename`, `title`) — für Filterung und Zitierung, unabhängig vom Embedding.

Vorgeschlagenes Chunk-Format (e5-Prefix-Konvention beibehalten):

```
passage: [To-Dos/Auto] Bremsen — <chunk-text>
```

bzw. bei Root-Dateien ohne Ordner einfach `[Notizen]`. Ordnerpfad ohne `.md`-Endung, Unterordner mit `/` getrennt.

Gleiches Schema **konsistent für `vault` und `brain`** anwenden, sonst driften die beiden Collections auseinander.

### Fix — Aufgaben

- [ ] Kontext-Header in der Chunk-Erzeugung ergänzen (eine Stelle, beide Collections)
- [ ] Metadata erweitern: `rel_path`, `folder`, `filename`, `title` — `psage_id` bleibt Identität
- [ ] `embedding_schema_version` als Konstante im Code + in den Collection-Metadaten ablegen
- [ ] Beim Start: Version vergleichen. Mismatch → Full-Reindex triggern (Collection löschen, neu anlegen, alle Dateien einlesen), nicht inkrementell
- [ ] Git-basierte Change-Detection zurücksetzen: gespeicherten letzten Commit-Hash beim Reindex löschen, sonst hält er unveränderte Dateien für aktuell
- [ ] Manueller Trigger zusätzlich: CLI/Settings-Aktion „Vault neu indizieren (force)"
- [ ] Fortschrittsanzeige im UI beim Reindex — das dauert bei vollem Vault spürbar

### Akzeptanzkriterien

- Query „Was steht in meinen To-Dos zum Auto?" rankt Chunks aus `To-Dos/Auto.md` über gleichlautende Inhalte aus anderen Ordnern
- Nach Versionssprung läuft der Reindex automatisch genau einmal
- Nach dem Reindex sind `psage_id`s stabil (keine Duplikate, keine Waisen)

---

## 3. [P2] Voice-Memo: Abbruch fehlt, State bleibt kleben, Schnellmemo-Modus

Feature funktioniert grundsätzlich gut. Drei Punkte:

### 3a. Abbruch-Button fehlt

Aktuell führt „Aufnahme stoppen" zwangsläufig zur Ingestion. Es gibt keinen Weg, eine laufende Aufnahme zu verwerfen.

- [ ] Zweiter Button neben Stopp: **Abbrechen** (X-Icon). Beendet Aufnahme, verwirft Audio-Buffer, **keine** Transkription, alle Felder leeren, Dialog schließen.
- [ ] Optional zusätzlich: ESC-Taste als Shortcut.

### 3b. State-Leak nach „Discard"

„Discard" schließt nur den Dialog. Beim erneuten Öffnen steht der alte transkribierte Text wieder da.

- [ ] Ursache: Dialog-State liegt vermutlich in einer langlebigen Komponenten-/Session-Variable statt in lokalem Dialog-State. `discard` muss alle Felder aktiv zurücksetzen — Audio-Buffer, Transkript, AI-Rewrite, Metadaten.
- [ ] Zusätzlich beim **Öffnen** des Dialogs zurücksetzen (Gürtel und Hosenträger — schützt auch gegen andere Wege des Dialogschließens, z. B. Klick auf Overlay).

### 3c. Schnellmemo (Fire-and-Forget)

Der User soll die Wahl haben: mit Review oder komplett im Hintergrund.

**UI**: zwei gleichwertige Aufnahme-Buttons nebeneinander, kein Slider. Slider = versteckter Modus-State, den man vergisst; zwei Buttons machen die Entscheidung explizit bei jeder Aufnahme.

- Button 1: **Memo mit Prüfung** — bisheriger Flow (Transkript + Rewrite anzeigen, User bestätigt → Save)
- Button 2: **Schnellmemo** — nach Stopp schließt der Dialog sofort; Transkription, AI-Rewrite und Save laufen im Hintergrund

Umsetzung:

- [ ] Zwei Buttons mit klaren Labels/Icons, gleiche visuelle Gewichtung. Kurzer Hint-Text darunter, was der Unterschied ist.
- [ ] Hintergrundverarbeitung als Task auf **App-Ebene**, nicht client-gebunden — sonst stirbt der Task, wenn der User die Seite wechselt oder die PWA in den Hintergrund geht.
- [ ] Toast bei Erfolg mit Link zur gespeicherten Notiz.
- [ ] Fehlerfall: Toast mit Fehlermeldung, Audio **nicht** verwerfen — Retry oder Fallback in den Review-Dialog anbieten. Ein stumm verschlucktes Memo ist schlimmer als gar keines.
- [ ] Mehrere parallele Schnellmemos: sequenziell abarbeiten (Semaphore/Queue), damit die lokale Inferenz nicht überrannt wird.

### Akzeptanzkriterien

- Abbruch während laufender Aufnahme → kein Transkriptionsaufruf im Log, Felder leer
- Dialog nach Discard erneut öffnen → komplett leer
- Schnellmemo: Dialog schließt sofort nach Stopp; Notiz erscheint ohne weitere Interaktion im Vault; Toast bestätigt
- Seitenwechsel direkt nach Schnellmemo-Stopp → Memo wird trotzdem gespeichert

---

## 4. [P2] Modelle ignorieren offensichtlichen Tool-Bedarf (Minimax M3, Cloud)

### Symptom

Gleiche Frage nach dem Drehmomentschlüssel, frischer Kontext, keine Vorgeschichte. Minimax M3 antwortet **ohne jeden Tool-Call**: „Du hast den Pediro Pro 2.0 gekauft…" — frei erfunden.

### Analyse

Drei mögliche Ebenen, in dieser Reihenfolge prüfen:

1. **Wird `tools` überhaupt mitgeschickt?** Manche Gateways/Modelle brauchen explizit `tool_choice: "auto"`, sonst werden Tools ignoriert oder gar nicht durchgereicht. Raw-Request loggen und gegenprüfen.
2. **Unterstützt das Modell auf diesem Endpoint Function Calling zuverlässig?** Nicht jedes OpenAI-kompatible Deployment tut das, auch wenn das Modell es prinzipiell kann.
3. **Prompt-Ebene**: Das Modell hält die Frage für Allgemeinwissen. Systemprompt sagt vermutlich nicht hart genug, dass Aussagen über persönliche Daten des Users **ausschließlich** aus Tools stammen dürfen.

### Fix

- [ ] Raw-Request/Response für Minimax M3 loggen — erst klären, ob es Transport oder Verhalten ist
- [ ] Systemprompt-Regel schärfen, explizit und knapp: Aussagen über Dokumente, Mails, Käufe, Termine oder sonstige persönliche Fakten des Users **nur** auf Basis von Tool-Ergebnissen. Kein Tool-Ergebnis → sagen, dass nichts gefunden wurde. Niemals aus dem Modellwissen ergänzen.
- [ ] **Guard mit einem Retry**: Wenn die Anfrage einen persönlichen Bezug hat (First-Person-Possessiv + Vergangenheitsbezug: „ich habe gekauft", „meine Rechnung", „wann war mein…") und das Modell **null** Tool-Calls produziert hat → einmalig neu anfragen mit `tool_choice: "required"` (bzw. dem Äquivalent des Backends) und einem kurzen System-Reminder. Nur ein Retry, danach normal weiter.
- [ ] Per-Modell-Config im Universal-Adapter: `supports_tools`, `force_tool_first_turn`, `tool_choice_mode`. Modelle, die den Guard regelmäßig auslösen, bekommen `force_tool_first_turn: true` als Default.
- [ ] Modelle, die auch mit `required` nicht sauber callen: im Model-Picker als „eingeschränkt (kein zuverlässiges Tool-Use)" kennzeichnen, statt still schlechte Antworten zu liefern.

### Bewusst **nicht** im Scope

Kein generischer Halluzinations-Detector und kein LLM-Judge über jede Antwort. Der Guard ist eine billige Heuristik auf der Anfrage, kein zweites Modell im Pfad.

### Akzeptanzkriterien

- Minimax M3, frischer Kontext, „Wie heißt der Drehmomentschlüssel, den ich gekauft habe?" → mindestens ein Tool-Call
- Guard löst bei rein allgemeinen Fragen („Was ist ein Drehmomentschlüssel?") **nicht** aus
- Retry passiert maximal einmal pro Turn (Log prüfen)

---

## 5. [P3] Websearch: Vorschaubilder inkonsistent

### Symptom

Mal hat jedes Ergebnis ein Vorschaubild, mal keins, mal nur ein Teil — obwohl die Zielseiten passende Bilder haben.

### Analyse — vermutlich kein Bug, sondern fehlende Policy

SearXNG liefert `img_src` nur durch, wenn die **jeweilige Engine** es mitliefert. Das ist von Engine zu Engine verschieden und ändert sich je nach Query, welche Engines antworten. Daher das Muster „mal alle, mal keine, mal ein paar". Zusätzlich zu prüfen:

- `image_proxy` in der SearXNG-Config: ist der aktiv, kommen Bilder als proxied URLs über die SearXNG-Instanz. Nicht erreichbar/blockiert → Bild fehlt oder ist kaputt.
- Falls Vorschaubilder teilweise aus dem `trafilatura`-Fetch stammen (OG-Image), erklärt das die Teilbefüllung ebenfalls: nur die tatsächlich gefetchten Seiten haben ein Bild.

### Fix — eine Policy wählen, nicht beides halb

**Empfehlung: Option A.** Bilder sind hier Dekoration, keine Information; inkonsistente Karten sehen kaputter aus, als der Nutzen wert ist. Wenn Bilder gewünscht sind, dann konsistent über Option B.

- **Option A (empfohlen)**: Vorschaubilder in der Ergebnisliste weglassen. Einheitliches Layout, keine Extra-Requests, kein Leak an Drittseiten.
- **Option B (USERENTSCHEID)**: Für die Top-N Ergebnisse (N = 3–5) gezielt `og:image` per HEAD/kleinem GET nachladen und cachen. Nur Top-N, sonst kostet es Latenz. Bei Fehlschlag Platzhalter mit Favicon/Domain statt Lücke.

Aufgaben:

- [ ] `image_proxy`-Setting der SearXNG-Instanz prüfen und dokumentieren
- [ ] Klären, ob Bilder aus SearXNG, aus `trafilatura` oder aus beidem kommen
- [ ] Policy festlegen und einheitlich implementieren -> Option B
- [ ] Fallback-Rendering ohne Layout-Sprung (fixe Kachelgröße, Platzhalter)

### Akzeptanzkriterien

- Ergebnisliste sieht bei jeder Query gleich aus — entweder durchgehend mit Bild/Platzhalter oder durchgehend ohne
- Keine kaputten Bild-Icons mehr

---

## Reihenfolge für die Session

1. **#1 Email-Suche** — erst reproduzieren und loggen, dann `X-GM-RAW` + Charset + UTF-7-Decode. Größter Impact.
2. **#2 Vault-Embeddings** — Schema-Änderung + Reindex-Mechanik. Sauber in einem Rutsch, weil das Reembedding einmalig durchlaufen muss.
3. **#3 Voice-Memo** — 3a und 3b sind klein und unabhängig, 3c danach separat.
4. **#4 Tool-Use-Guard** — erst Transport klären, dann Prompt, dann Guard.
5. **#5 Vorschaubilder** — Policy-Entscheidung, dann kleine Umsetzung.

Phasen einzeln fahren, `/clear` dazwischen. #1 und #2 jeweils mit Regressionstest abschließen, bevor der nächste Punkt startet.
