"""Offline test harness: no audio hardware, TTS/STT engines, or network.

HOME is redirected to a temp dir *before* any Levity module is imported, since
they create ~/.levity-voice (logs, pid files) at import time. Hardware-only
third-party modules are replaced with stubs.
"""
import importlib.util
import os
import socket
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_HOME = tempfile.mkdtemp(prefix="levity-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
os.environ.pop("GEMINI_API_KEY", None)

for _name in ("sounddevice", "whisper", "rumps"):
    if _name not in sys.modules:
        sys.modules[_name] = types.ModuleType(_name)

sys.path.insert(0, str(ROOT))

import server  # noqa: E402  (must come after HOME redirect)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def srv():
    return server


@pytest.fixture(scope="session")
def voiced():
    # levity_voiced does `import server`, which is already loaded above.
    return _load("levity_voiced", ROOT / "multi-host" / "levity_voiced.py")


@pytest.fixture(scope="session")
def shim():
    return _load("levity_shim", ROOT / "multi-host" / "levity_shim.py")


@pytest.fixture(scope="session")
def hook():
    return _load("claude_code_speak_hook", ROOT / "hooks" / "claude_code_speak_hook.py")


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Point server config at tmp_path and reset in-memory state per test."""
    monkeypatch.setattr(server, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(server, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(server, "_config_mtime_ns", 0)
    monkeypatch.setattr(server, "_kill_active_tts", lambda: None)
    monkeypatch.setattr(server, "_maybe_launch_menubar", lambda: None)
    monkeypatch.setattr(server, "_config", server._validate_config({}))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    yield


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def _blocked(*a, **k):
        raise RuntimeError("network access is disabled in tests")
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(server, "urlopen", _blocked)
