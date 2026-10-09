import json
import socket

import pytest


@pytest.fixture
def engine_cfg(srv):
    srv._config.update(server_active=True, response_active=True)
    return srv


def test_dispatch_unknown_op(voiced):
    r = voiced._dispatch({"op": "bogus"})
    assert r["ok"] is False and "bogus" in r["error"]


def test_dispatch_status(voiced, engine_cfg):
    r = voiced._dispatch({"op": "STATUS"})
    assert r["ok"] and r["result"]["daemon"] is True
    assert r["result"]["server_active"] is True


def test_dispatch_speak_empty(voiced, engine_cfg):
    assert voiced._dispatch({"op": "speak", "text": "  "})["result"] == "Nothing to say."


def test_dispatch_speak_when_stopped_or_muted(voiced, srv):
    assert voiced._dispatch({"op": "speak", "text": "hi"})["result"] == "Server is stopped."
    srv._config.update(server_active=True, response_active=False)
    assert voiced._dispatch({"op": "speak", "text": "hi"})["result"] == "Voice response is off."


def test_dispatch_speak_routes_local_for_short_text(voiced, engine_cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(engine_cfg, "_speak_background", lambda *a: calls.append(a))
    monkeypatch.setattr(engine_cfg, "_write_last_spoken", lambda t: None)
    engine_cfg._config["gemini_api_key"] = "k"
    r = voiced._dispatch({"op": "speak", "text": "short"})
    assert r["result"] == "Speaking (daemon)."
    import time; time.sleep(0.2)
    assert calls and calls[0][0] == "short" and calls[0][1] is False  # use_cloud False


def test_dispatch_confirm_requires_active_server(voiced, srv):
    assert voiced._dispatch({"op": "confirm"})["error"] == "Server inactive."


def test_dispatch_confirm_clamps_window_and_parses(voiced, engine_cfg, monkeypatch):
    seen = {}
    def fake_record(window, silence):
        seen["window"] = window
        return "yes please"
    monkeypatch.setattr(engine_cfg, "_record_audio", fake_record)
    r = voiced._dispatch({"op": "confirm", "timeout": 999})
    assert seen["window"] == 15.0
    assert r["result"] == {"decision": "yes", "transcript": "yes please"}


def test_dispatch_listen_no_speech(voiced, engine_cfg, monkeypatch):
    monkeypatch.setattr(engine_cfg, "_record_audio", lambda w, s: "")
    assert voiced._dispatch({"op": "listen"})["result"] == "(no speech detected)"


def test_dispatch_listen_busy_mic(voiced, engine_cfg):
    assert voiced._capture_lock.acquire(blocking=False)
    try:
        assert "Already listening" in voiced._dispatch({"op": "listen"})["error"]
    finally:
        voiced._capture_lock.release()


def test_dispatch_toggle_routes_to_engine(voiced, srv):
    r = voiced._dispatch({"op": "toggle", "action": "mode_full"})
    assert r["ok"] and "full" in r["result"]


def test_handle_roundtrip_over_socketpair(voiced, monkeypatch):
    # socket.connect is blocked by the no_network fixture; socketpair needs none.
    a, b = socket.socketpair()
    a.sendall(b'{"op": "bogus"}\n')
    voiced._handle(b)
    reply = json.loads(a.makefile().readline())
    assert reply["ok"] is False
    a.close()


def test_handle_bad_json_returns_error(voiced):
    a, b = socket.socketpair()
    a.sendall(b"not json\n")
    voiced._handle(b)
    assert json.loads(a.makefile().readline())["ok"] is False
    a.close()


def test_shim_tools_registered(shim):
    import asyncio
    names = {t.name for t in asyncio.run(shim.mcp.list_tools())}
    assert names == {"voice_speak", "voice_confirm", "voice_listen", "voice_toggle"}


def test_shim_toggle_maps_status_op(shim, monkeypatch):
    import asyncio
    sent = []
    monkeypatch.setattr(shim, "_call", lambda req, timeout=90.0: sent.append(req) or {"ok": True, "result": {"a": 1}})
    assert asyncio.run(shim.voice_toggle(" Status ")) == '{"a": 1}'
    asyncio.run(shim.voice_toggle("stop"))
    assert [r["op"] for r in sent] == ["status", "toggle"]


def test_shim_confirm_error_is_unclear(shim, monkeypatch):
    import asyncio
    monkeypatch.setattr(shim, "_call", lambda req, timeout=90.0: {"ok": False, "error": "down"})
    out = json.loads(asyncio.run(shim.voice_confirm()))
    assert out["decision"] == "unclear" and out["error"] == "down"


def test_shim_call_returns_error_when_daemon_unavailable(shim, monkeypatch):
    monkeypatch.setattr(shim, "_ensure_daemon", lambda: None)
    monkeypatch.setattr(shim, "_try_connect", lambda: None)
    assert shim._call({"op": "status"}) == {"ok": False, "error": "voice daemon unavailable"}
