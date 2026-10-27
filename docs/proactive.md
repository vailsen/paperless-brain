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
  run (the Werkbank) on it; the result arrives as a second message.
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
- Messages contain a title, the urgency and a link by default. The full text only
  if you switch it on per channel (Telegram bot chats are not end-to-end encrypted).
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
   - **Telegram:** create a bot with @BotFather, put its token in
     `TELEGRAM_BOT_TOKEN`. Only the worker may poll it; do not use the token in
     any other bot.
   - **Email:** one system mailbox for sending and for replies — `SMTP_*` and
     `IMAP_*`. Replies "1", "2" or "3" (or "ja", "später", "nie") are understood.
4. Models: by default each user's proactive jobs use the model they pick in
   Settings > Proactive (or their chat model). `PROACTIVE_MODEL_FIXED` forces one
   model for everyone; `PROACTIVE_MODEL_ALLOWLIST` limits the choice;
   cloud models only with `PROACTIVE_CLOUD_ALLOWED=true`, within a daily cap.
5. Work on the existing archive runs only inside `PROACTIVE_SWEEP_WINDOWS`
   (default 01:00–05:00, at most 200 documents a night, shared fairly between
   users). With `PROACTIVE_WOL_AUTO=true` the worker wakes the GPU host through the
   same Wake-on-LAN path as the dashboard button; the existing idle rule shuts it
   down again.

## Setting it up (user)

Settings > Proactive:

1. **Allow background access** — the app keeps your Paperless token encrypted on
   the server so the worker can act as you. Withdraw it any time.
2. Switch on **Proactive reminders and insights**.
3. Turn on **push** on each device (phone, computer), and/or link **Telegram** (send the shown `/start CODE` to the bot) and/or **email**
   (a code is mailed to you), choose where messages go, and send a test message.
4. Optional: reminder days, your weekly message limit (critical deadline
   reminders always arrive, also with 0), and the model.

Findings are listed under **Findings** in the navigation.
