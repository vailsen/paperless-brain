#!/usr/bin/env python3
"""Run one email search against a real account, with the IMAP traffic visible.

The email tool has no other way to be checked end to end: the credentials are
encrypted per user, the server is remote, and the interesting failures (charset,
folder encoding, a term that only Gmail's index can match) are invisible from
the outside. This prints what actually went over the wire.

    python scripts/imap_debug.py alice <paperless-token> "Drehmomentschlüssel"
    python scripts/imap_debug.py alice <token> --folders
    python scripts/imap_debug.py alice <token> "invoice" --raw 12345

The token is the user's Paperless token — it is the key the credential store is
encrypted with, so nothing here can read credentials the user has not handed
over deliberately.

`--raw <uid>` dumps one message as it arrives from the server, which is the way
to settle "is the word even in this mail?": grep the dump for the term, then
grep the extracted snippet the model gets. If it is in the first and not in the
second, extraction lost it.
"""

import argparse
import asyncio
import imaplib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.credential_store import load_credentials  # noqa: E402
from services.imap_service import (  # noqa: E402
    _body_snippet,
    folder_display,
    search_emails,
)


def _imap_config(username: str, token: str) -> dict:
    cfg = load_credentials(username, token).get("imap", {})
    if not cfg.get("host"):
        sys.exit(f"no IMAP credentials stored for {username}")
    return cfg


def _dump_raw(cfg: dict, uid: str) -> None:
    """Fetch one message untouched and show what extraction makes of it."""
    import email as email_mod

    conn_class = imaplib.IMAP4_SSL if cfg.get("use_ssl", True) else imaplib.IMAP4
    conn = conn_class(cfg["host"], int(cfg.get("port", 993)))
    conn.login(cfg["username"], cfg["password"])
    try:
        for raw in conn.list()[1] or []:
            name = raw.decode() if isinstance(raw, bytes) else str(raw)
            quoted = name.split('"')
            readable = folder_display(quoted[-2] if len(quoted) > 2 else name)
            print(f"  folder: {readable}")
        target = input("\nfolder to select [INBOX]: ").strip() or "INBOX"
        conn.select(f'"{target}"', readonly=True)
        _, data = conn.fetch(uid, "(BODY.PEEK[])")
        if not data or not isinstance(data[0], tuple):
            sys.exit(f"message {uid} not found in {target}")
        raw_bytes = data[0][1]
        out = Path(f"/tmp/imap_{uid}.eml")
        out.write_bytes(raw_bytes)
        print(f"\nraw message written to {out} ({len(raw_bytes)} bytes)")
        snippet = _body_snippet(email_mod.message_from_bytes(raw_bytes), max_chars=40_000)
        snippet_file = Path(f"/tmp/imap_{uid}.extracted.txt")
        snippet_file.write_text(snippet, encoding="utf-8")
        print(f"extracted text written to {snippet_file} ({len(snippet)} chars)")
        print("\nNow compare, e.g.:")
        print(f"  grep -ic drehmoment {out} {snippet_file}")
    finally:
        conn.logout()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username")
    parser.add_argument("token", help="the user's Paperless token")
    parser.add_argument("query", nargs="?", default="", help="full-text search term")
    parser.add_argument("--folders", action="store_true", help="list folders and exit")
    parser.add_argument("--from-addr", default="", help="restrict by sender")
    parser.add_argument("--folder", default="", help="restrict to one folder (plain text)")
    parser.add_argument("--max-results", type=int, default=10)
    parser.add_argument("--raw", metavar="UID", help="dump one message and its extraction")
    parser.add_argument("--quiet", action="store_true", help="no IMAP protocol trace")
    args = parser.parse_args()

    cfg = _imap_config(args.username, args.token)
    if not args.quiet:
        imaplib.Debug = 4

    if args.raw:
        _dump_raw(cfg, args.raw)
        return

    inputs: dict = {
        "query": args.query,
        "from_addr": args.from_addr,
        "folder": args.folder,
        "list_folders_only": args.folders,
    }
    result = asyncio.run(
        search_emails(
            host=cfg["host"],
            port=int(cfg.get("port", 993)),
            username=cfg["username"],
            password=cfg["password"],
            inputs=inputs,
            use_ssl=bool(cfg.get("use_ssl", True)),
            max_results=args.max_results,
        )
    )

    print("\n" + "=" * 70)
    if args.folders:
        for name in result.get("all_folders", []):
            print(f"  {name}")
        return

    # Says which folders were searched, how many hits each had, and whether the
    # prefix fallback had to step in — the three things a failed search needs.
    print(result.get("folder", ""))
    print(f"{len(result.get('results', []))} of {result.get('total', 0)} shown\n")
    for i, r in enumerate(result.get("results", []), 1):
        print(f"{i}. {r.get('date', '')} | {r.get('from', '')}")
        print(f"   {r.get('subject', '')}")
        if snippet := r.get("snippet"):
            print(f"   {snippet[:200]}")
        print()


if __name__ == "__main__":
    main()
