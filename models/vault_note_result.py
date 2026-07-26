# models/vault_note_result.py
from dataclasses import dataclass, field


@dataclass
class VaultNoteResult:
    pbrain_id: str
    path: str        # relative path in vault (for display + disk access)
    title: str       # filename without .md
    snippet: str     # matched chunk text preview
    distance: float = 1.0
    heading_path: str = ""
    user: str = ""
