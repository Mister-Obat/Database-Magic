from pathlib import Path


def find_ops_root(test_file: str) -> Path:
    """Locate operations data from either the work copy or the installed Magic tree."""
    for parent in Path(test_file).resolve().parents:
        if (parent / "schemas" / "config.schema.json").is_file() and (parent / "config.example.json").is_file():
            return parent
        nested = parent / "_bibliotheque_mtg"
        if (nested / "schemas" / "config.schema.json").is_file() and (nested / "config.example.json").is_file():
            return nested
    raise RuntimeError(f"Cannot locate _bibliotheque_mtg operations root from {test_file}")
