# Contributing

Thanks for looking. This is an early alpha extracted from a personal homelab
project, maintained by one person — so the most valuable contribution right now
is a **good bug report from an install that isn't mine**.

## Before you write code

**Open an issue first for anything non-trivial.** Some parts of this codebase
have invariants that are not obvious from reading it (see `CLAUDE.md` — the
Werkbank orchestrator, the vault/memory subsystem and the i18n mechanism all
have hard rules). A PR that violates one of them cannot be merged no matter how
good the code is, and finding that out after you wrote it is a waste of your
evening.

Typo fixes, obvious bugs and translation corrections need no issue — just send
the PR.

## Development setup

```bash
python3.12 -m venv .venv && source .venv/bin/activate   # 3.12–3.14
pip install --extra-index-url https://download.pytorch.org/whl/cpu -e ".[crawl,i18n,dev]"
playwright install chromium        # only for the [crawl] extra
cp .env.example .env               # APP_PATH = repo root, with trailing slash
python main.py                     # http://0.0.0.0:8080
```

On a GPU machine, drop `--extra-index-url` to get CUDA torch.

You need a reachable Paperless-ngx instance with a superuser token. For chat
work, an Ollama server is enough; ingestion additionally needs a vision model.

## Tests

```bash
pytest
```

CI runs the suite on every PR. Add a test when you fix a bug — most of the
existing tests exist because something broke once.

## House rules

- **Dependencies:** `pyproject.toml` is the single source of truth (direct deps
  only, range pins). Adding one needs a reason in the PR description. If you use
  an optional extra of a package, declare the extra — `markdown2[latex]`, not
  `markdown2`.
- **Match the surrounding code.** Comment density, naming and idiom vary between
  modules; follow the file you are in rather than a global style.
- **User-visible strings must be translatable.** Wrap them in `_()` from the
  per-page translator, never a module-level `_`, and never an f-string inside
  `_()`. Do not wrap log messages or internal keys. Full rules in `CLAUDE.md`.
- **Never commit credentials.** `.env` and `.deploy.env` are gitignored; keep it
  that way. Redact tokens from any log you paste.
- **NiceGUI patterns:** use `@ui.refreshable` for dynamic content in plain
  columns. `element.clear()` inside `QTabPanels` silently does nothing.

## Translations

English source strings are the `msgid`; there is no English catalog. To update
German or add a language:

```bash
pybabel extract -F babel.cfg -o locales/messages.pot .
pybabel update -i locales/messages.pot -d locales     # or: init -l <code> for a new language
# edit locales/<code>/LC_MESSAGES/messages.po
pybabel compile -d locales
```

Resolve every `fuzzy` flag before submitting — pybabel's guesses are frequently
wrong, and a shipped fuzzy entry is a visible bug. Commit the compiled `.mo`.

## Pull requests

- One topic per PR.
- Say what you tested and on what (Docker or bare metal, which Paperless-ngx
  version, which LLM backend).
- Screenshots for UI changes.
- Expect review to be slow-ish. It is a side project.

## Reporting bugs

Use the issue templates. Include the version (`config/version.py` or image tag),
your deployment type, and `docker compose logs` — with tokens redacted. Most
reports that stall do so for lack of a log.

For **security** problems, do not open an issue — see [SECURITY.md](SECURITY.md).
