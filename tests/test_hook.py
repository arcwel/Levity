import json
import time


def test_clean_for_speech_drops_code_fences(hook):
    text = "Intro line\n```python\nprint(1)\n```\nOutro"
    assert hook._clean_for_speech(text) == "Intro line Outro"


def test_clean_for_speech_truncates(hook):
    out = hook._clean_for_speech("word " * 500)
    assert out.endswith(", and more on screen.")
    assert len(out) < hook.MAX_SPEAK_CHARS + 40


def test_last_assistant_text_picks_final_text_block(hook, tmp_path):
    p = tmp_path / "t.jsonl"
    rows = [
        {"type": "assistant", "message": {"role": "assistant", "content": "old"}},
        {"type": "user", "message": {"role": "user", "content": "q"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use"}, {"type": "text", "text": "final"}]}},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\nGARBAGE\n")
    assert hook._last_assistant_text(str(p)) == "final"


def test_last_assistant_text_missing_file(hook, tmp_path):
    assert hook._last_assistant_text(str(tmp_path / "nope")) == ""


def test_already_spoken_recently(hook, tmp_path, monkeypatch):
    f = tmp_path / "last.json"
    monkeypatch.setattr(hook, "LAST_SPOKEN_FILE", f)
    assert hook._already_spoken_recently() is False
    f.write_text(json.dumps({"ts": time.time()}))
    assert hook._already_spoken_recently() is True
    f.write_text(json.dumps({"ts": time.time() - 1000}))
    assert hook._already_spoken_recently() is False
