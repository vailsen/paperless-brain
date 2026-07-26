from pathlib import Path


def is_brain_path(rel_path: Path, brain_subfolder: str) -> bool:
    """True if rel_path (relative to vault root) lives under brain_subfolder."""
    return bool(rel_path.parts) and rel_path.parts[0] == brain_subfolder
