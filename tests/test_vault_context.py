"""The context header embedded alongside every chunk (vault/context.py).

A chunk saying "change the brake pads" is ambiguous alone and unambiguous under
`To-Dos/Car.md`, so the folder has to reach the vector — but in one short line,
or it drowns out the chunk it is supposed to disambiguate.
"""

from pathlib import Path

from vault.context import (
    EMBEDDING_SCHEMA_VERSION,
    embed_context,
    embed_text,
    note_folder,
    note_title,
    path_metadata,
)


def test_header_carries_folder_title_and_heading():
    assert embed_context(Path("To-Dos/Auto.md"), "Bremsen") == "[To-Dos] Auto › Bremsen"


def test_nested_folders_stay_slash_separated():
    assert embed_context(Path("Projekte/Haus/Sanierung.md")) == "[Projekte/Haus] Sanierung"


def test_root_note_has_no_bracket():
    assert embed_context(Path("Notizen.md")) == "Notizen"


def test_header_is_a_single_line():
    """More than one line and it starts outweighing a short chunk."""
    assert "\n" not in embed_context(Path("a/b/C.md"), "H1 > H2")


def test_embed_text_prepends_header_to_body():
    assert embed_text("Bremsbeläge wechseln", Path("To-Dos/Auto.md")) == (
        "[To-Dos] Auto\n\nBremsbeläge wechseln"
    )


def test_embed_text_of_root_note_has_no_blank_prefix():
    assert embed_text("Text", Path("Notiz.md")).startswith("Notiz\n\nText")


def test_strip_prefix_removes_the_constant_brain_folder():
    """Every brain fact lives there, so keeping it discriminates nothing."""
    header = embed_context(
        Path("PaperSage Memory/Drehmomentschlüssel.md"),
        strip_prefix="PaperSage Memory",
    )
    assert header == "Drehmomentschlüssel"


def test_strip_prefix_keeps_subfolders_below_it():
    assert note_folder(
        Path("PaperSage Memory/Fristen/Steuer.md"), strip_prefix="PaperSage Memory"
    ) == "Fristen"


def test_strip_prefix_ignores_a_similarly_named_folder():
    assert note_folder(Path("PaperSage Memories/X.md"), strip_prefix="PaperSage Memory") == (
        "PaperSage Memories"
    )


def test_note_title_drops_the_suffix():
    assert note_title("To-Dos/Auto.md") == "Auto"


def test_path_metadata_fields():
    meta = path_metadata(Path("To-Dos/Auto.md"))
    assert meta == {
        "rel_path": "To-Dos/Auto.md",
        "folder": "To-Dos",
        "filename": "Auto.md",
        "title": "Auto",
        "schema_version": EMBEDDING_SCHEMA_VERSION,
    }


def test_path_metadata_is_chroma_safe():
    """Chroma metadata values must be scalars, not lists or dicts."""
    for value in path_metadata(Path("a/b/C.md")).values():
        assert isinstance(value, (str, int, float, bool))
