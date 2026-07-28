# Security policy

## Supported versions

PaperlessBrain is an early alpha. Only the **latest release** receives fixes —
there are no maintenance branches. Report against the newest tag before
assuming a bug is still present.

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Use GitHub's private vulnerability reporting: **Security → Report a
vulnerability** on this repository. That opens a private advisory only you and
the maintainer can see.

Please include:

- what an attacker can do, and what they need first (network access? a valid
  Paperless-ngx login? another user's session?)
- affected version (`config/version.py` or the image tag)
- deployment: Docker image, `docker compose` with host networking, or bare metal
- reproduction steps, and logs with tokens redacted

Expect a first response within a week. This is a one-person hobby project, not a
vendor with an on-call rotation — if the issue is being actively exploited
against you, say so in the first line.

## What this project handles

Read this before deploying it anywhere reachable from the internet.

| Data | Where it lives | Protection |
|---|---|---|
| Paperless-ngx session token | `app.storage.user` (server-side) | encrypted with `STORAGE_SECRET` |
| Per-user LLM API keys, IMAP and CalDAV credentials | `data/credentials/<username>.enc` | AES-256-GCM, key derived per user via HKDF-SHA256 from that user's Paperless token |
| Document text, extractions, embeddings | `data/` (sidecars, ChromaDB) | filesystem permissions only — **not encrypted at rest** |
| Vault notes and agent memory | the mounted vault directory | filesystem permissions only — plain Markdown, plus a git history |
| `PAPERLESS_SUPERUSER_TOKEN` | `.env` | plaintext, as with any env config |

Consequences worth stating plainly:

- **The `.env` superuser token is full access to your Paperless-ngx archive.**
  Treat the `.env` file and any backup of it as a credential.
- **`STORAGE_SECRET` protects every session.** Generate a random one; never
  reuse the example value.
- **Encrypted credentials are only as strong as the Paperless token they are
  keyed from.** If a user's Paperless token is compromised, so are the IMAP,
  CalDAV and API keys that user stored.
- **`data/` and the vault are readable by anyone with host filesystem access**,
  including the contents of your documents. Full-disk encryption is your job.
- **The settings export string is plaintext.** With "include passwords and API
  keys" enabled it is a credential dump the moment it leaves the app. It is off
  by default for that reason.

## Multi-user isolation

Document access is delegated to Paperless-ngx, not reimplemented here. Every
document a tool returns is fetched through the logged-in user's own Paperless
token, so Paperless' object-level permissions decide what that user sees. Vector
hits, sidecar extractions and deadlines are all filtered through that check
before any of their text is shown.

Two limits are worth knowing before you rely on this:

- **The vector index itself is not partitioned.** ChromaDB is populated by the
  superuser sync and holds chunks of every document. It is queried unscoped and
  the permission filter is applied to the results — so no foreign text is
  displayed, but foreign documents do occupy ranking slots and can push your own
  hits out of a result page.
- **Paperless-ngx documents have no owner by default**, which makes them visible
  to every user. If you want separation between users, you must set owners and
  permissions in Paperless. PaperlessBrain inherits whatever you configure there
  and cannot add isolation that Paperless is not enforcing.

Per-user data that PaperlessBrain owns itself — brain facts, vault notes, stored
credentials, model registry — is scoped by username and does not depend on the
Paperless configuration.

## Deployment expectations

This project assumes a trusted network — a homelab, a LAN, or behind a VPN.
There is no rate limiting, no brute-force lockout and no CSRF hardening beyond
what NiceGUI provides. Authentication is delegated entirely to Paperless-ngx.

If you expose it to the internet, put it behind a reverse proxy with TLS and
your own authentication layer. Reports that amount to "it is insecure when
published directly to the internet without a proxy" are known and documented
here rather than fixed.

## Out of scope

- Vulnerabilities in Paperless-ngx, Ollama, ChromaDB or other dependencies —
  report those upstream (do tell us if PaperlessBrain makes them exploitable in a
  way upstream would not be).
- Anything requiring host root or physical access to the machine.
- LLM prompt injection through document contents. It is real: a document in your
  archive can contain text aimed at the assistant reading it. The mitigations
  here are architectural — tools are scoped per role, and nothing derived from
  web or generated content is written into the document index without manual
  review. A concrete injection chain that reaches a destructive tool call or
  exfiltrates data **is** in scope; "an LLM can be talked into saying things" is
  not.
