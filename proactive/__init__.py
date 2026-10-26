"""Proactive system: notifications, deadline reminders, triage, dossiers.

Spec: `docs/proactive-architecture.md`, tasks: `docs/proactive-tasks.md`,
invariants: the "Proactive system" section of `CLAUDE.md`.

Two processes use this package: the NiceGUI app (settings, findings page,
reaction endpoint — registered only when enabled, `proactive.app`) and the
worker (`paperlessbrain-worker`, `proactive.worker`). The worker must never
import `services.clients`: that loads the embedding model and opens Chroma,
which is not meant to be shared between processes. Anything needing Chroma or
the chat tools goes through the app's internal API (`proactive.internal_api`).
"""
