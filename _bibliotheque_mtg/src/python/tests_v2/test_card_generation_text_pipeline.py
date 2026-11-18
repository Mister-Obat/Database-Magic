import json
import sqlite3
import subprocess
import threading
import urllib.request

import pytest
from PIL import Image

from bibliotheque_mtg import local_app
from bibliotheque_mtg.v2 import card_generation


def _reference(path):
    Image.new("RGB", (531, 738), "white").save(path)


def test_multiple_compatible_official_texts_choose_stable_first(tmp_path, monkeypatch):
    active_db = tmp_path / "active.sqlite"
    with sqlite3.connect(active_db) as conn:
        conn.executescript("""
            CREATE TABLE editions (set_id TEXT, code TEXT);
            CREATE TABLE printings (printing_id TEXT, set_id TEXT, collector_number TEXT,
                                    layout TEXT, oracle_id TEXT);
            CREATE TABLE source_records (provider_card_id TEXT, printing_id TEXT,
                                         lang TEXT, raw_json TEXT);
            CREATE TABLE image_slots (printing_id TEXT, face_index INTEGER);
        """)
        conn.execute("INSERT INTO editions VALUES ('target-set', 'TGT')")
        conn.execute("INSERT INTO printings VALUES ('target-print', 'target-set', '12', 'normal', 'oracle')")
        conn.execute("INSERT INTO source_records VALUES (?, ?, ?, ?)",
                     ("target", "target-print", "en", json.dumps({
                         "name": "Example", "oracle_id": "oracle", "layout": "normal"})))
        conn.execute("INSERT INTO image_slots VALUES ('target-print', 0)")
    with sqlite3.connect(tmp_path / "catalogue.sqlite") as conn:
        conn.execute("CREATE TABLE cards (id TEXT, oracle_id TEXT, lang TEXT, set_id TEXT, "
                     "set_code TEXT, collector_number TEXT, layout TEXT, raw_json TEXT)")
        for code, wording in (("AAA", "Premier texte"), ("ZZZ", "Autre texte")):
            conn.execute("INSERT INTO cards VALUES (?, ?, 'fr', ?, ?, '1', 'normal', ?)",
                         (f"fr-{code.lower()}", "oracle", code, code, json.dumps({
                             "oracle_id": "oracle", "layout": "normal", "printed_name": "Exemple",
                             "printed_type_line": "Créature", "printed_text": wording})))
    monkeypatch.setattr(local_app, "OPS_ROOT", tmp_path)

    result = local_app._lookup_local_card_text(active_db, "target", 0)

    assert result["fields"]["oracle_text"] == "Premier texte"
    assert result["available_text_variants"] == 2
    assert result["source_card_ids"] == ["fr-aaa"]

    with sqlite3.connect(active_db) as conn:
        conn.execute("INSERT INTO source_records VALUES (?, ?, ?, ?)",
                     ("fr-exact", "target-print", "fr", json.dumps({
                         "oracle_id": "oracle", "layout": "normal", "printed_name": "Exemple",
                         "printed_type_line": "Créature", "printed_text": "Texte exact"})))
    exact = local_app._lookup_local_card_text(active_db, "target", 0)
    assert exact["fields"]["oracle_text"] == "Texte exact"
    assert exact["available_text_variants"] == 1


@pytest.mark.parametrize("translate", [False, True])
def test_text_reference_is_transcribed_without_image_generation(tmp_path, monkeypatch, translate):
    account = tmp_path / "account"
    account.mkdir()
    (account / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(account))
    reference = tmp_path / "text.png"
    _reference(reference)
    seen = {}

    def codex_text_call(args, **kwargs):
        seen["args"] = args
        seen["prompt"] = kwargs["input"]
        output = args[args.index("--output-last-message") + 1]
        fields = {key: "" for key in card_generation.TEXT_REFERENCE_FIELDS}
        fields.update(name="Exemple", type_line="Créature", oracle_text="Texte français")
        with open(output, "w", encoding="utf-8") as stream:
            json.dump(fields, stream)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(card_generation.subprocess, "run", codex_text_call)
    result = card_generation.extract_card_text_reference(
        reference, translate_to_french=translate, executable="codex")

    assert result["oracle_text"] == "Texte français"
    assert seen["args"].count("--image") == 1
    assert "image_generation" not in seen["args"]
    assert "transcribe" in seen["prompt"].lower()
    assert ("Translate only" in seen["prompt"]) == translate


def test_image_generation_never_receives_text_reference_image(tmp_path, monkeypatch):
    class PromptCaptured(Exception):
        pass

    account = tmp_path / "account"
    account.mkdir()
    (account / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(account))
    visual, text_image = tmp_path / "visual.png", tmp_path / "text.png"
    _reference(visual)
    _reference(text_image)
    monkeypatch.setattr(card_generation, "extract_card_text_reference", lambda *_args, **_kwargs: {
        "name": "Exemple", "type_line": "Créature", "oracle_text": "Texte français"})

    def image_call(args, **kwargs):
        assert args.count("--image") == 1
        assert "image_generation" in args
        assert '"oracle_text": "Texte français"' in kwargs["input"]
        raise PromptCaptured

    monkeypatch.setattr(card_generation.subprocess, "run", image_call)
    with pytest.raises(PromptCaptured):
        card_generation.generate_card_candidate(visual, text_image, tmp_path / "candidates",
                                                executable="codex")


def test_image_reference_is_transcribed_before_server_starts_imagegen(tmp_path, monkeypatch):
    visual = {"provider_card_id": "visual", "printing_id": "print", "face_index": 0,
              "language": "en", "source_data": b"visual"}
    french = {"provider_card_id": "french", "printing_id": "print", "face_index": 0,
              "language": "fr", "source_data": b"french"}
    monkeypatch.setattr(local_app, "_image_candidate", lambda _id: [visual, french])
    monkeypatch.setattr(local_app.LibraryHandler, "db", property(lambda self: tmp_path / "active.sqlite"))
    captured = {}

    def start_task(_kind, _label, work, **_kwargs):
        captured["work"] = work
        return {"task_id": "task", "status": "queued"}

    monkeypatch.setattr(local_app, "_start_image_task", start_task)
    text_calls = []

    def extract(*_args, **_kwargs):
        text_calls.append(True)
        return {"name": "Exemple", "type_line": "Créature", "oracle_text": "Texte français"}

    monkeypatch.setattr(local_app, "extract_card_text_reference", extract)

    def generate(_visual_path, text_path, _root, _correction, **kwargs):
        assert text_path is None
        assert kwargs["french_text"]["oracle_text"] == "Texte français"
        return {"candidate_id": "candidate", "path": str(tmp_path / "candidate.png")}

    monkeypatch.setattr(local_app, "generate_card_candidate", generate)
    server = local_app.LocalServer(("127.0.0.1", 0), local_app.LibraryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        request = urllib.request.Request(base + "/api/v2/image/ai/generate",
            data=json.dumps({"candidate_id": "source", "visual_index": 0,
                             "text_index": 1, "text_mode": "image"}).encode(), method="POST",
            headers={"Content-Type": "application/json", "Origin": base})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 202
        result = captured["work"](threading.Event())
        assert result["french_text"]["oracle_text"] == "Texte français"
        assert result["text_provenance"]["source"] == "codex_text_reference"
        retry = urllib.request.Request(base + "/api/v2/image/ai/generate",
            data=json.dumps({"candidate_id": "source", "visual_index": 0, "text_index": 1,
                             "text_mode": "image", "previous_id": "candidate"}).encode(), method="POST",
            headers={"Content-Type": "application/json", "Origin": base})
        with urllib.request.urlopen(retry, timeout=5) as response:
            assert response.status == 202
        captured["work"](threading.Event())
        assert len(text_calls) == 1
    finally:
        local_app._AI_CANDIDATES.pop("candidate", None)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
