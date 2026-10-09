import asyncio
import json
import time

import pytest


def run(coro):
    return asyncio.run(coro)


# ---- config parsing -------------------------------------------------------

def test_validate_config_defaults(srv):
    cfg = srv._validate_config({})
    assert cfg["server_active"] is False
    assert cfg["response_active"] is True
    assert cfg["listen_mode"] == "quick"
    assert cfg["gemini_voice"] == srv.DEFAULT_GEMINI_VOICE


def test_validate_config_drops_unknown_and_bad_types(srv):
    cfg = srv._validate_config({"server_active": "yes", "bogus": 1, "local_voice": "Alex"})
    assert cfg["server_active"] is False  # wrong type -> default
    assert cfg["local_voice"] == "Alex"
    assert "bogus" not in cfg


def test_load_config_missing_and_corrupt_file(srv):
    assert srv._load_config() == srv._validate_config({})
    srv.CONFIG_FILE.write_text("{not json")
    assert srv._load_config()["response_active"] is True
    srv.CONFIG_FILE.write_text("[1, 2]")  # valid JSON, not a dict
    assert srv._load_config()["listen_mode"] == "quick"


def test_load_config_env_key_wins(srv, monkeypatch):
    srv.CONFIG_FILE.write_text(json.dumps({"gemini_api_key": "from-file"}))
    monkeypatch.setenv("GEMINI_API_KEY", " from-env ")
    assert srv._load_config()["gemini_api_key"] == "from-env"


def test_save_config_never_writes_api_key(srv):
    cfg = srv._validate_config({"gemini_api_key": "secret", "local_voice": "Alex"})
    srv._save_config(cfg)
    on_disk = json.loads(srv.CONFIG_FILE.read_text())
    assert "gemini_api_key" not in on_disk
    assert on_disk["local_voice"] == "Alex"


def test_reload_config_if_changed(srv):
    srv.CONFIG_FILE.write_text(json.dumps({"listen_mode": "full"}))
    srv._reload_config_if_changed()
    assert srv._get_config_snapshot()["listen_mode"] == "full"


def test_snapshot_is_a_copy(srv):
    snap = srv._get_config_snapshot()
    snap["server_active"] = True
    assert srv._get_config_snapshot()["server_active"] is False


# ---- tool schemas ---------------------------------------------------------

def test_mcp_tool_names_and_schemas(srv):
    tools = {t.name: t for t in run(srv.mcp.list_tools())}
    assert {"voice_speak", "voice_toggle", "voice_confirm", "voice_listen"} <= set(tools)
    speak = tools["voice_speak"].inputSchema
    assert "text" in speak["required"]
    assert {"force_local", "tone"} <= set(speak["properties"])
    assert tools["voice_toggle"].inputSchema["required"] == ["action"]


def test_tone_presets_include_default(srv):
    assert srv.DEFAULT_TONE in srv.TONE_PRESETS
    assert all(isinstance(v, str) and v for v in srv.TONE_PRESETS.values())


# ---- voice_toggle: actions, status, uptime --------------------------------

def test_toggle_start_stop_roundtrip(srv):
    assert run(srv.voice_toggle("start")) == "Voice server started."
    assert run(srv.voice_toggle(" START ")) == "Server is already active."
    assert json.loads(srv.CONFIG_FILE.read_text())["server_active"] is True
    assert run(srv.voice_toggle("stop")) == "Voice server stopped."
    assert srv._get_config_snapshot()["server_active"] is False


def test_toggle_response_and_mode(srv):
    run(srv.voice_toggle("response_off"))
    assert srv._get_config_snapshot()["response_active"] is False
    run(srv.voice_toggle("response_on"))
    assert srv._get_config_snapshot()["response_active"] is True
    assert "full" in run(srv.voice_toggle("mode_full"))
    assert srv._get_config_snapshot()["listen_mode"] == "full"


def test_toggle_unknown_action(srv):
    assert run(srv.voice_toggle("dance")).startswith("Unknown action: 'dance'")


def test_toggle_status_shape(srv):
    status = json.loads(run(srv.voice_toggle("status")))
    assert status["build"] == srv.BUILD
    assert status["platform"] == srv.PLATFORM
    assert status["has_gemini_key"] is False
    assert status["listen_mode"] in srv.LISTEN_MODES
    assert "gemini_api_key" not in status


@pytest.mark.parametrize("elapsed,expected", [
    (0, "0 days"),
    (0.5 * 86400, "0.5 days"),
    (1.0 * 86400, "1 day"),
    (1.6 * 86400, "1.5 days"),
    (3 * 86400, "3 days"),
])
def test_format_uptime(srv, monkeypatch, elapsed, expected):
    now = time.time()
    monkeypatch.setattr(srv, "_server_started_at", now - elapsed)
    monkeypatch.setattr(srv.time, "time", lambda: now)
    assert srv._format_uptime() == expected


# ---- confirmation parsing -------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Yes, go ahead", "yes"),
    ("nope", "no"),
    ("stop, cancel that", "no"),
    ("don't do it", "unclear"),  # conflicting words fail safe
    ("yes but wait", "unclear"),
    ("", "unclear"),
    (None, "unclear"),
    ("banana", "unclear"),
])
def test_parse_confirmation(srv, text, expected):
    assert srv._parse_confirmation(text) == expected


# ---- dotenv ---------------------------------------------------------------

def test_load_dotenv(srv, monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("# c\nexport LEVITY_T1='abc'  # note\nLEVITY_T2=\"x y\"\nbad line\n")
    monkeypatch.setattr(srv, "ENV_FILE", env)
    monkeypatch.delenv("LEVITY_T1", raising=False)
    monkeypatch.delenv("LEVITY_T2", raising=False)
    srv._load_dotenv()
    import os
    assert os.environ["LEVITY_T1"] == "abc"
    assert os.environ["LEVITY_T2"] == "x y"
    monkeypatch.delenv("LEVITY_T1")
    monkeypatch.delenv("LEVITY_T2")


@pytest.mark.hardware
def test_record_audio_real_microphone(srv):
    """Needs a real microphone and Whisper model."""
    assert isinstance(srv._record_audio(1.0, 0.5), str)
