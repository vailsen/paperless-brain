import uuid
from pathlib import Path

import yaml

from vault.paths import align_vault_perms

# Stable per-file identity key in frontmatter (and Chroma metadata). `psage_id`
# is the pre-rebrand legacy name, still accepted on read and migrated on write.
ID_KEY = "pbrain_id"
LEGACY_ID_KEY = "psage_id"


def get_id(meta: dict) -> str:
    """Return the file's stable id from frontmatter, accepting the legacy key."""
    return str(meta.get(ID_KEY) or meta.get(LEGACY_ID_KEY) or "")


def sanitize_tags(tags: list) -> list[str]:
    """Normalize tags into Obsidian-valid strings.

    - strip, collapse spaces to hyphens, drop empties
    - Obsidian rejects tags containing ONLY digits (e.g. "2025"); a tag must
      hold at least one non-numeric char. Prefix purely-numeric tags with "_"
      so the value (the year etc.) is preserved while staying valid.
    """
    out: list[str] = []
    for t in tags or []:
        s = str(t).strip().replace(" ", "-")
        if not s:
            continue
        if s.isdigit():
            s = f"_{s}"
        out.append(s)
    return out


def read(path: Path) -> tuple[dict, str]:
    """Parse YAML frontmatter + body from a .md file.

    Returns (meta_dict, body_text). meta_dict is {} if no frontmatter present.
    """
    text = path.read_text(encoding="utf-8")
    if text.startswith("---\n") or text.startswith("---\r\n"):
        rest = text[4:]
        idx = rest.find("\n---")
        if idx != -1:
            yaml_str = rest[:idx]
            body = rest[idx + 4:].lstrip("\n")
            try:
                meta = yaml.safe_load(yaml_str) or {}
                if not isinstance(meta, dict):
                    meta = {}
            except Exception:
                meta = {}
            return meta, body
    return {}, text


def write(path: Path, meta: dict, body: str) -> None:
    """Atomically write frontmatter + body (temp file → rename within same dir)."""
    yaml_str = yaml.dump(
        meta,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    content = f"---\n{yaml_str}---\n{body}"
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    # rename replaces the inode, so ownership/mode must be set on the temp
    # file — preserve the original file's owner when overwriting
    align_vault_perms(tmp, ref=path if path.exists() else None)
    tmp.rename(path)


def ensure_pbrain_id(path: Path, meta: dict) -> str:
    """Return the pbrain_id for this file.

    If meta already carries the current key, return it (no I/O). If it only
    carries the legacy `psage_id`, migrate in place: rewrite frontmatter under
    the new key, drop the old one, and return the (unchanged) UUID value.
    Otherwise generate a UUID4 and persist it. Idempotent.
    """
    if existing := meta.get(ID_KEY):
        return str(existing)
    # Re-read current state before writing back to avoid overwriting concurrent changes
    current_meta, body = read(path)
    if legacy := current_meta.pop(LEGACY_ID_KEY, None):
        # Migrate: preserve the UUID, move it under the new key (top of dict).
        pbrain_id = str(legacy)
        current_meta = {ID_KEY: pbrain_id, **current_meta}
        write(path, current_meta, body)
        return pbrain_id
    pbrain_id = str(uuid.uuid4())
    current_meta = {ID_KEY: pbrain_id, **current_meta}
    write(path, current_meta, body)
    return pbrain_id
