from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from bibliotheque_mtg.v2 import card_generation


def _image(path: Path, size=(744, 1039), color=(30, 50, 80)) -> Path:
    Image.new("RGB", size, color).save(path, format="PNG")
    return path


def test_generate_card_candidate_uses_french_text_and_optional_previous_reference(
        tmp_path, monkeypatch):
    account_home = tmp_path / "account"
    account_home.mkdir()
    (account_home / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(account_home))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-forwarded")
    visual = _image(tmp_path / "english.png", color=(80, 40, 20))
    text = _image(tmp_path / "french.png", color=(20, 80, 40))
    previous = _image(tmp_path / "previous.png", color=(40, 20, 80))
    observed = {}

    def fake_run(args, **kwargs):
        observed["args"] = args
        observed["input"] = kwargs["input"]
        observed["env"] = kwargs["env"]
        output = Path(kwargs["env"]["CODEX_HOME"]) / "generated_images" / "result.png"
        _image(output)
        return subprocess.CompletedProcess(args, 0, "{}", "")

    monkeypatch.setattr(card_generation.subprocess, "run", fake_run)
    result = card_generation.generate_card_candidate(
        visual, text, tmp_path / "candidates", "Corrige le nom français", previous_path=previous,
        executable="codex",
    )

    assert result["reference_count"] == 3
    assert Path(result["path"]).is_file()
    assert result["width"] == 744 and result["height"] == 1039
    assert "Corrige le nom français" in observed["input"]
    assert observed["args"].count("--image") == 3
    assert observed["args"][-1] == "-"
    assert "OPENAI_API_KEY" not in observed["env"]
    assert observed["env"]["CODEX_HOME"] != str(account_home)


def test_generate_card_candidate_uses_verified_french_text_without_text_image(
        tmp_path, monkeypatch):
    account_home = tmp_path / "account"
    account_home.mkdir()
    (account_home / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(account_home))
    visual = _image(tmp_path / "visual.png")
    observed = {}

    def fake_run(args, **kwargs):
        observed["args"] = args
        observed["input"] = kwargs["input"]
        output = Path(kwargs["env"]["CODEX_HOME"]) / "generated_images" / "result.png"
        _image(output)
        return subprocess.CompletedProcess(args, 0, "{}", "")

    monkeypatch.setattr(card_generation.subprocess, "run", fake_run)
    fields = {"name": "Carte", "type_line": "Créature", "oracle_text": "Vol"}
    result = card_generation.generate_card_candidate(
        visual, None, tmp_path / "candidates", french_text=fields, executable="codex")
    assert Path(result["path"]).is_file()
    assert observed["args"].count("--image") == 1
    assert '"oracle_text": "Vol"' in observed["input"]
    assert "sole authority" in observed["input"]


def test_generate_card_candidate_rejects_missing_codex_output(tmp_path, monkeypatch):
    account_home = tmp_path / "account"
    account_home.mkdir()
    (account_home / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(account_home))
    visual = _image(tmp_path / "english.png")
    text = _image(tmp_path / "french.png")
    monkeypatch.setattr(card_generation.subprocess, "run", lambda args, **kwargs:
                       subprocess.CompletedProcess(args, 0, "{}", ""))

    with pytest.raises(card_generation.CardGenerationError, match="aucun PNG"):
        card_generation.generate_card_candidate(
            visual, text, tmp_path / "candidates", executable="codex")


def test_small_french_jpeg_reference_keeps_full_generated_resolution(tmp_path, monkeypatch):
    account_home = tmp_path / "account"
    account_home.mkdir()
    (account_home / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(account_home))
    visual = _image(tmp_path / "english.png", size=(745, 1040))
    french = tmp_path / "french.jpg"
    Image.new("RGB", (146, 204), (50, 70, 90)).save(french, format="JPEG")

    def fake_run(args, **kwargs):
        output = Path(kwargs["env"]["CODEX_HOME"]) / "generated_images" / "result.png"
        _image(output, size=(1024, 1430))
        return subprocess.CompletedProcess(args, 0, "{}", "")

    monkeypatch.setattr(card_generation.subprocess, "run", fake_run)
    result = card_generation.generate_card_candidate(
        visual, french, tmp_path / "candidates", executable="codex")
    assert (result["width"], result["height"]) == (1024, 1430)
    with Image.open(result["path"]) as generated:
        assert generated.size == (1024, 1430)


@pytest.mark.parametrize(("diagnostic", "expected"), [
    ("image_generation failed: usage_limit_reached; token=secret", "quota de génération d’images"),
    ("image_generation error: content_policy_violation prompt=private", "modération"),
    ("Unknown tool: image_generation; /Users/private/config", "indisponible ou non pris en charge"),
    ("image_generation failed: http 429 Too Many Requests", "limite temporairement"),
    ("image_generation failed: permission_error", "accès au service d’images Codex est refusé"),
    ("The model 'gpt-image-2' does not exist.", "modèle nécessaire à la génération"),
])
def test_generate_card_candidate_reports_safe_cli_failure_categories(
        tmp_path, monkeypatch, diagnostic, expected):
    account_home = tmp_path / "account"
    account_home.mkdir()
    (account_home / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(account_home))
    visual = _image(tmp_path / "english.png")
    text = _image(tmp_path / "french.png")
    monkeypatch.setattr(card_generation.subprocess, "run", lambda args, **kwargs:
                       subprocess.CompletedProcess(args, 1, "", diagnostic))

    with pytest.raises(card_generation.CardGenerationError) as error:
        card_generation.generate_card_candidate(
            visual, text, tmp_path / "candidates", executable="codex")

    assert expected in str(error.value)
    assert "secret" not in str(error.value)
    assert "private" not in str(error.value)


def test_generate_card_candidate_recovers_inline_png_payload(tmp_path, monkeypatch):
    account_home = tmp_path / "account"
    account_home.mkdir()
    (account_home / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(account_home))
    visual = _image(tmp_path / "english.png")
    text = _image(tmp_path / "french.png")

    def fake_run(args, **kwargs):
        encoded = base64.b64encode((tmp_path / "french.png").read_bytes()).decode("ascii")
        return subprocess.CompletedProcess(
            args, 0, json.dumps({"type": "image_generation", "result": encoded}), "")

    monkeypatch.setattr(card_generation.subprocess, "run", fake_run)
    result = card_generation.generate_card_candidate(
        visual, text, tmp_path / "candidates", executable="codex")

    assert Path(result["path"]).is_file()
    assert result["width"] == 744 and result["height"] == 1039


def test_generate_card_candidate_uses_last_of_multiple_pngs(tmp_path, monkeypatch):
    account_home = tmp_path / "account"
    account_home.mkdir()
    (account_home / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(account_home))
    visual = _image(tmp_path / "english.png")
    french = _image(tmp_path / "french.png")

    def fake_run(args, **kwargs):
        generated = Path(kwargs["env"]["CODEX_HOME"]) / "generated_images"
        _image(generated / "first.png", color=(10, 20, 30))
        _image(generated / "last.png", color=(90, 80, 70))
        output = "\n".join(json.dumps({"saved_path": str(generated / name)})
                           for name in ("first.png", "last.png"))
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(card_generation.subprocess, "run", fake_run)
    result = card_generation.generate_card_candidate(
        visual, french, tmp_path / "candidates", executable="codex")
    with Image.open(result["path"]) as candidate:
        assert candidate.getpixel((0, 0)) == (90, 80, 70)


def test_reported_png_paths_are_limited_to_private_invocation(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    reported = private / "nested" / "result.png"
    outside = tmp_path / "outside.png"
    output = "\n".join((
        json.dumps({"type": "image_generation", "saved_path": str(reported)}),
        json.dumps({"path": str(outside)}),
    ))

    assert card_generation._reported_pngs(output, private) == [reported.resolve()]


def test_generate_card_candidate_classifies_quota_from_timeout_partial_output(tmp_path, monkeypatch):
    account_home = tmp_path / "account"
    account_home.mkdir()
    (account_home / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(account_home))
    visual = _image(tmp_path / "english.png")
    text = _image(tmp_path / "french.png")

    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 1, output=b"image_generation usage_limit_reached secret")

    monkeypatch.setattr(card_generation.subprocess, "run", fake_run)
    with pytest.raises(card_generation.CardGenerationError, match="quota de génération d’images") as error:
        card_generation.generate_card_candidate(
            visual, text, tmp_path / "candidates", executable="codex")
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(("diagnostic", "returncode", "expected"), [
    ("usage_limit_reached account=private token=secret", 0, "quota de génération d’images"),
    ("HTTP 429 rate_limit_exceeded secret", 0, "limite temporairement"),
    ("HTTP 400 moderation_blocked category=self-harm prompt=private", 0, "modération"),
    ("HTTP 401 unauthorized /Users/private/auth.json", 0, "accès au service d’images"),
    ("error: invalid JSON in config /Users/private/config.toml secret", 1,
     "réponse ou la configuration JSON"),
    ('{"type":"turn.completed","usage":{}}', 0, "sans appeler l’outil image_generation"),
    ('{"type":"item.completed","item":{"type":"image_generation_call"}}', 0,
     "sans produire de PNG exploitable"),
])
def test_missing_png_diagnostic_distinguishes_safe_failure_classes(
        tmp_path, monkeypatch, diagnostic, returncode, expected):
    account_home = tmp_path / "account"
    account_home.mkdir()
    (account_home / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(account_home))
    visual = _image(tmp_path / "english.png")
    text = _image(tmp_path / "french.png")
    monkeypatch.setattr(card_generation.subprocess, "run", lambda args, **kwargs:
                       subprocess.CompletedProcess(args, returncode, diagnostic, ""))

    with pytest.raises(card_generation.CardGenerationError) as error:
        card_generation.generate_card_candidate(
            visual, text, tmp_path / "candidates", executable="codex")

    message = str(error.value)
    assert expected in message
    assert "secret" not in message
    assert "private" not in message
    assert "self-harm" not in message


def test_cli_imagegen_event_count_only_counts_jsonl_records():
    output = "\n".join((
        '{"type":"item.completed","item":{"type":"image_generation_call"}}',
        '{"type":"turn.completed","text":"image_generation was not called"}',
        "not-json image_generation",
    ))
    assert card_generation._cli_imagegen_event_count(output) == 2
