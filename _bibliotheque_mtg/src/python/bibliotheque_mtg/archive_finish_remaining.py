"""Place all remaining historical images flat in Impossible with an audit index."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .archive_finalize import ROOT, IMPOSSIBLE, LOG, RESOLVED, _write

EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def main() -> None:
    IMPOSSIBLE.mkdir(parents=True, exist_ok=True)
    index_path = IMPOSSIBLE / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.is_file() else {"entries": {}}
    log = json.loads(LOG.read_text(encoding="utf-8")) if LOG.is_file() else {"entries": {}}
    resolved = json.loads(RESOLVED.read_text(encoding="utf-8")) if RESOLVED.is_file() else {"entries": {}}
    moved = 0
    for source in sorted(ROOT.rglob("*")):
        if not source.is_file() or source.suffix.casefold() not in EXTENSIONS or IMPOSSIBLE in source.parents:
            continue
        relative = source.relative_to(ROOT).as_posix()
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        destination = IMPOSSIBLE / source.name
        if destination.exists():
            destination = IMPOSSIBLE / f"{source.stem}__{digest[:12]}{source.suffix}"
        if destination.exists():
            raise FileExistsError(destination)
        entry = {"original_path": relative, "filename": destination.name, "sha256": digest,
                 "reason": "aucune identité unique après les protocoles automatiques"}
        index["entries"][relative] = entry
        _write(index_path, index)
        os.replace(source, destination)
        log["entries"][relative] = {"status": "impossible", "sha256": digest,
            "destination": destination.relative_to(ROOT).as_posix(), "reason": entry["reason"]}
        resolved["entries"][relative] = {"sha256": digest, "status": "impossible"}
        moved += 1
    _write(LOG, log)
    _write(RESOLVED, resolved)
    print(json.dumps({"impossible_moved": moved, "index": str(index_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
