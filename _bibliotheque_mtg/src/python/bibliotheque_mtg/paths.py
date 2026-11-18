"""Storage paths shared by the server and maintenance commands."""
from pathlib import Path


def catalogue_root(ops_root: Path) -> Path:
    preferred = ops_root / "v3-staging"
    legacy = ops_root / "work" / "v3-staging"
    if not (preferred / "current.json").exists() and (legacy / "current.json").exists():
        return legacy
    return preferred


def card_candidate_root(ops_root: Path) -> Path:
    preferred = ops_root / "state" / "ai-card-candidates"
    legacy = ops_root / "work" / "ai-card-candidates"
    if not preferred.exists() and legacy.is_dir():
        return legacy
    return preferred
