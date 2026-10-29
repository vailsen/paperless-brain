# Proactive reminders & insights

PaperlessBrain can work while you are not using it: remind you of deadlines in
your documents, point out contracts, insurance or savings worth a look, and keep a
running file on long matters (a building society savings plan, a dispute). It is
off by default. Design and invariants: [proactive-architecture.md](proactive-architecture.md).

## What it does

- **Deadline reminders.** Every document gets a short text-only analysis of the
  text ingestion already extracted (no new vision run). Dates you have to act on
  — pay, cancel, object, apply — become reminders at 30, 7 and 1 day before (your
  choice). Dates that have already passed never produce a reminder. The dashboard
  shows the same deadlines, so there is one source for both.
- **Insights.** A model looks at each new document for you: is there money to
  save, a contract to review, a change to notice? If so you get a short message
  with **Interesting / Later / Never again**. "Interesting" starts a deep-research
  run (the Werkbank) on it; the result arrives as a second message ("Research
  done: …"). Its summary goes on top of the finding; the full report is a note
  in `PaperlessBrain Research/` in your vault, linked from the finding (and
  from the topic's dossier), and opens as a PDF from the findings page. It
  stays when the run is deleted.
  Letters from the same sender about the same matter are **one** finding: the
  twelfth invoice announcing the same price change joins the first finding
  (its document and quote are added) instead of sending a twelfth message. A
  finding you dismissed stays dismissed when another such letter arrives —
  unless it turns critical.
- **Learning what matters.** Reactions are counted per topic in
  `PaperlessBrain Findings/_interests.md` in your vault. Topics you like are sent
  more readily, topics you mute are held back — except critical ones.
- **Dossiers.** For a matter that spans years, a file in
  `PaperlessBrain Findings/Dossiers/` with the current state, a timeline (each
  entry linked to its document) and open questions, updated as letters arrive.
- **Quiet weeks.** If nothing was sent for a while, one digest of what was held back.

Everything is a Markdown file in your vault (`PaperlessBrain Findings/`). You can
change a finding's `status` in Obsidian or the note editor; the app follows it.

## What it never does

- It never writes to Paperless-ngx.
- It acts with your own Paperless access, so it sees exactly your documents —
  and only if you allowed it (Settings > Proactive > Background access).
- A recommendation needs a quote from your document and a web page actually read
  in the same run, or it is not shown; recommendations carry a disclaimer.
- Web searches never contain your name, IBAN, contract or policy numbers,
  amounts or dates — code refuses such a query before it is sent.
- Messages contain a title, the urgency and a link by default. Push is end-to-end
  encrypted; the full text by email only if you switch it on.
- Its findings are never quoted as evidence in chat answers; the chat cites the
  documents they point to.

## Setting it up (operator)

1. In `.env`: `PROACTIVE_ENABLED=true` and `PROACTIVE_APP_BASE_URL` (the address
   users open the app at — used for links in messages). All other keys are in
   `.env.example.full`, section *Proactive system*.
2. Start the worker — a second process from the same image:
   - Docker: the `paperless-brain-worker` service in `docker-compose.yml`
     (`docker compose up -d`). It needs the same `./data` and vault mounts as the
     app.
   - Bare metal / LXC: `deploy/paperlessbrain-worker.service` (systemd).
3. Channels — offered to users only when configured:
   - **Push (the app itself):** notifications on Android, iPhone (app added to
     the home screen) and desktop browsers, end-to-end encrypted. Generate keys
     once with `python -m proactive.notify.webpush` and put both lines in
     `.env`, plus `PUSH_CONTACT=mailto:…`. No buttons: tapping opens the finding.
   - **Email:** an SMTP account to send from — `SMTP_*`. Send-only: a message
     is a title, the urgency and a link to the finding; nothing reads the
     mailbox, so it can be your normal one.
4. Models: by default each user's proactive jobs use the model they pick in
   Settings > Proactive (or their chat model). `PROACTIVE_MODEL_FIXED` forces one
   model for everyone; `PROACTIVE_MODEL_ALLOWLIST` limits the choice;
   cloud models only with `PROACTIVE_CLOUD_ALLOWED=true`, within a daily cap.
5. Work on the existing archive runs only inside `PROACTIVE_SWEEP_WINDOWS`
   (default 01:00–05:00, at most 200 documents a night, shared fairly between
   users). With `PROACTIVE_WOL_AUTO=true` the worker wakes the GPU host through the
   same Wake-on-LAN path as the dashboard button — **only inside a sweep window**,
   or for something a user asked for in the last 30 minutes ("Analyse now", the
   closer look after "Interesting"). Outside the windows everything else (a new
   document, its triage, a dossier update) waits until the host is on anyway or
   the next window opens. The idle rule shuts a worker-woken host down again.

## Setting it up (user)

Settings > Proactive:

1. **Allow background access** — the app keeps your Paperless token encrypted on
   the server so the worker can act as you. Withdraw it any time.
2. Switch on **Proactive reminders and insights**.
3. Turn on **push** on each device (phone, computer) and/or link **email**
   (a code is mailed to you), choose where messages go, and send a test message.
4. Optional: reminder days, quiet hours (default 22:00–07:00 —
   what is found then is sent when they end, as **one** message per channel
   listing everything, most urgent first), the language findings are written
   in, and the model. Each push device shows what the push service said to the
   last message (accepted / refused) — "accepted" but nothing on the phone
   means the phone held it back (battery saver, notification settings).
5. **Analyse the archive now** works through as many of your existing
   documents as you choose right away, instead of waiting for the night. A chat
   always goes first.

Findings are listed under **Findings** in the navigation.

## The findings list

Filter by status and topic, sort by relevance (urgency × how much the topic
interests you; a deadline within a week counts as critical), urgency,
deadline or date, and group by topic, by status, or by topic and then status.
Groups are headlines to scroll past, nothing to open or close — for a shorter
list pick one topic. A coloured left edge marks what presses: high or critical
urgency, or a deadline within a week. Done and dismissed findings are greyed.

Each status has an icon:

| Status | Means |
|---|---|
| New | just found; within minutes it is sent or held back |
| Notified | sent to you, waiting for your reaction |
| Held back | not sent: low priority or a muted topic (the quiet-weeks digest may still bring it) |
| Later | you pressed "Later": it comes back in 7 days |
| Interesting | you want a closer look: the research is queued or running |
| Researched | the research is done; its report is linked at the top |
| Done / Dismissed | closed; "Restore" takes it back |
