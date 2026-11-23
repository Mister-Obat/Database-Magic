import base64
import json
import runpy
from hashlib import sha256
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image


@pytest.fixture
def preview_tool(tmp_path):
    script = Path(__file__).resolve().parents[3] / "tools/generate_deck_box_previews.py"
    refresh = runpy.run_path(str(script))["refresh_previews"]
    tool = refresh.__globals__
    assets = tmp_path / "assets"
    assets.mkdir()
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps({"canopy": sha256(b"shipped").hexdigest()}), encoding="utf-8"
    )
    tool.update(
        UI_ROOT=tmp_path,
        BASELINE_PATH=baseline,
        STATE_PATH=tmp_path / "state" / "fingerprints.json",
    )
    return tool, assets


def test_unchanged_panorama_preserves_edited_preview_without_browser(preview_tool, monkeypatch):
    tool, assets = preview_tool
    (assets / "deck-box-canopy.webp").write_bytes(b"shipped")
    preview = assets / "deck-box-preview-canopy.webp"
    preview.write_bytes(b"manually edited preview")
    monkeypatch.setitem(tool, "find_browser", lambda: pytest.fail("Unexpected browser lookup"))

    assert tool["refresh_previews"]({"canopy"})
    assert preview.read_bytes() == b"manually edited preview"
    assert not tool["STATE_PATH"].exists()


def test_refresh_keeps_failed_preview_and_retries_only_failed_design(preview_tool, monkeypatch):
    tool, assets = preview_tool
    designs = {"canopy", "glacier"}
    for design in designs:
        (assets / f"deck-box-{design}.webp").write_bytes(design.encode())
        (assets / f"deck-box-preview-{design}.webp").write_bytes(b"old preview")
    stream = BytesIO()
    Image.new("RGB", (256, 320), color="white").save(stream, format="WEBP")
    webp = stream.getvalue()
    result = {"data": base64.b64encode(webp).decode(), "ms": 1}
    browser = Path("chromium")
    seen = []

    def render(path, requested, textures):
        assert path == browser
        seen.append(requested)
        assert set(textures) == {f"/deck-box-{design}.webp" for design in requested}
        images = dict.fromkeys(requested, result)
        if len(seen) == 1:
            images["glacier"] = {"data": base64.b64encode(b"broken").decode(), "ms": 1}
        return {"images": images, "errors": {}}

    monkeypatch.setitem(tool, "render_previews", render)
    assert not tool["refresh_previews"](designs, browser)
    assert (assets / "deck-box-preview-canopy.webp").read_bytes() == webp
    assert (assets / "deck-box-preview-glacier.webp").read_bytes() == b"old preview"

    assert tool["refresh_previews"](designs, browser)
    assert (assets / "deck-box-preview-glacier.webp").read_bytes() == webp
    assert tool["refresh_previews"](designs, browser)
    assert seen == [["canopy", "glacier"], ["glacier"]]
