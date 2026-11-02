from pathlib import Path


def is_brain_path(rel_path: Path, brain_subfolder: str) -> bool:
    """True if rel_path (relative to vault root) lives under brain_subfolder."""
    return bool(rel_path.parts) and rel_path.parts[0] == brain_subfolder


def is_config_path(rel_path: Path, *subfolders: str) -> bool:
    """True for files under one of the configuration folders (experts,
    skills). Those are Markdown so the user owns and can edit them, but they
    are settings, not knowledge: never embedded, no `pbrain_id` minted."""
    return bool(rel_path.parts) and rel_path.parts[0] in {s for s in subfolders if s}
