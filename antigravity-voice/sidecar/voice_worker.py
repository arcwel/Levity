#!/usr/bin/env python3
"""
Antigravity Voice — Python sidecar.

Captures microphone audio, transcribes with openai-whisper, and optionally
runs always-on wake-word detection via Picovoice Porcupine.

IPC contract (line-delimited JSON over stdin/stdout):
  Host  → sidecar (stdin):
    {"action": "listen"}                            start a new recording session
    {"action": "stop"}                              manually end the in-progress recording
    {"action": "start_wakeword", "keyword": "...",  begin always-on wake-word detection
     "access_key": "...",
     "custom_keyword_path": ""}
    {"action": "stop_wakeword"}                     stop wake-word detection
    {"action": "configure",                         update runtime settings
     "whisper_model": "base",
     "silence_duration": 10}
    {"action": "shutdown"}                          exit cleanly

  Sidecar → host (stdout):
    {"type": "ready"}                              Whisper loaded, accepting commands
    {"type": "status", "state": "listening|processing|idle|wakeword"}
    {"type": "transcription", "text": "..."}
    {"type": "wakeword_detected"}                  keyword was spoken
    {"type": "error", "message": "..."}

Stdout is reserved for JSON. All human-readable logging goes to stderr.
"""

import json
import signal
import sys
import threading
import time
import os

# 16 kHz mono float32 — what Whisper expects natively.
SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "float32"
BLOCK_SIZE = 512  # Porcupine needs 512-sample frames at 16kHz

# Voice-activity heuristics.
SILENCE_RMS_THRESHOLD = 0.01
SILENCE_DURATION_SEC = 10.0
MAX_RECORDING_SEC = 60.0
WHISPER_MODEL_NAME = "base"


def log(msg: str) -> None:
    print(f"[sidecar] {msg}", file=sys.stderr, flush=True)


def emit(payload: dict) -> None:
    print(json.dumps(payload), file=sys.stdout, flush=True)


def main() -> int:
    # Lazy imports for clean error reporting.
    try:
        import numpy as np
    except ImportError as e:
        emit({"type": "error", "message": f"numpy not installed: {e}. Run sidecar/setup.sh."})
        return 1

    try:
        import sounddevice as sd
    except (ImportError, OSError) as e:
        emit({"type": "error", "message": f"sounddevice unavailable: {e}. Run sidecar/setup.sh."})
        return 1

    try:
        import whisper
    except ImportError as e:
        emit({"type": "error", "message": f"openai-whisper not installed: {e}. Run sidecar/setup.sh."})
        return 1

    # Porcupine is optional — only needed for wake-word mode.
    porcupine_available = False
    try:
        import pvporcupine
        porcupine_available = True
    except ImportError:
        log("pvporcupine not installed — wake-word mode unavailable")

    # ---- Mutable config (can be updated via "configure" action) ----
    config = {
        "whisper_model": WHISPER_MODEL_NAME,
        "silence_duration": SILENCE_DURATION_SEC,
    }

    log(f"loading whisper model '{config['whisper_model']}' (first run downloads ~140MB)...")
    try:
        model_holder = [whisper.load_model(config["whisper_model"])]
    except Exception as e:
        emit({"type": "error", "message": f"Failed to load Whisper model: {e}"})
        return 1
    log("whisper ready")

    # ---- Shared state ----
    lock = threading.Lock()
    state = {
        "recording": False,
        "stop_requested": False,
        "has_spoken": False,
        "wakeword_active": False,
    }
    buffer: list = []
    last_voice_time = [0.0]
    record_start = [0.0]

    # ---- Porcupine state ----
    porcupine_handle = [None]  # mutable holder
    wakeword_thread_stop = threading.Event()

    def audio_callback(indata, frames, time_info, status):
        if status:
            log(f"stream status: {status}")
        with lock:
            if not state["recording"]:
                return
            buffer.append(indata.copy())
            rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
            if rms > SILENCE_RMS_THRESHOLD:
                last_voice_time[0] = time.time()
                state["has_spoken"] = True

    try:
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            blocksize=BLOCK_SIZE,
            callback=audio_callback,
        )
        stream.start()
    except Exception as e:
        emit({"type": "error", "message": f"Could not open microphone: {e}"})
        return 1

    def transcribe_and_emit():
        with lock:
            chunks = list(buffer)
            buffer.clear()
        if not chunks:
            emit({"type": "transcription", "text": ""})
            return
        audio = np.concatenate(chunks, axis=0).flatten().astype(np.float32)
        try:
            result = model_holder[0].transcribe(audio, fp16=False, language="en")
            emit({"type": "transcription", "text": (result.get("text") or "").strip()})
        except Exception as e:
            emit({"type": "error", "message": f"Transcription failed: {e}"})

    shutdown_event = threading.Event()

    def monitor_loop():
        while not shutdown_event.is_set():
            time.sleep(0.1)
            should_finalize = False
            with lock:
                if not state["recording"]:
                    continue
                now = time.time()
                total_elapsed = now - record_start[0]
                silence_elapsed = (now - last_voice_time[0]) if state["has_spoken"] else 0.0
                if (
                    state["stop_requested"]
                    or (state["has_spoken"] and silence_elapsed >= config["silence_duration"])
                    or total_elapsed >= MAX_RECORDING_SEC
                ):
                    state["recording"] = False
                    state["stop_requested"] = False
                    should_finalize = True
            if should_finalize:
                emit({"type": "status", "state": "processing"})
                transcribe_and_emit()
                # If wake-word mode is active, go back to wakeword state.
                with lock:
                    if state["wakeword_active"]:
                        emit({"type": "status", "state": "wakeword"})
                    else:
                        emit({"type": "status", "state": "idle"})

    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()

    # ---- Wake-word detection ----
    def start_wakeword(access_key: str, keyword: str, custom_path: str):
        if not porcupine_available:
            emit({"type": "error", "message": "pvporcupine not installed. Run sidecar/setup.sh."})
            return

        stop_wakeword()  # clean up any existing instance

        try:
            if custom_path and os.path.isfile(custom_path):
                porcupine_handle[0] = pvporcupine.create(
                    access_key=access_key,
                    keyword_paths=[custom_path],
                )
                log(f"porcupine loaded custom keyword: {custom_path}")
            else:
                porcupine_handle[0] = pvporcupine.create(
                    access_key=access_key,
                    keywords=[keyword],
                )
                log(f"porcupine loaded built-in keyword: {keyword}")
        except Exception as e:
            emit({"type": "error", "message": f"Porcupine init failed: {e}"})
            return

        wakeword_thread_stop.clear()
        with lock:
            state["wakeword_active"] = True

        def wakeword_loop():
            """
            Read raw int16 frames from a dedicated stream and feed them to
            Porcupine. When the keyword is detected, emit an event and
            auto-start recording.
            """
            porcupine = porcupine_handle[0]
            if porcupine is None:
                return

            frame_length = porcupine.frame_length  # typically 512

            try:
                ww_stream = sd.InputStream(
                    samplerate=porcupine.sample_rate,
                    channels=1,
                    dtype="int16",
                    blocksize=frame_length,
                )
                ww_stream.start()
            except Exception as e:
                emit({"type": "error", "message": f"Wake-word mic error: {e}"})
                return

            emit({"type": "status", "state": "wakeword"})
            log("wake-word detection active")

            try:
                while not wakeword_thread_stop.is_set():
                    data, overflowed = ww_stream.read(frame_length)
                    if overflowed:
                        log("wake-word stream overflow")
                    pcm = data.flatten()
                    keyword_index = porcupine.process(pcm)
                    if keyword_index >= 0:
                        log("wake word detected!")
                        emit({"type": "wakeword_detected"})

                        # Auto-start recording.
                        with lock:
                            if not state["recording"]:
                                buffer.clear()
                                state["recording"] = True
                                state["stop_requested"] = False
                                state["has_spoken"] = False
                                now = time.time()
                                record_start[0] = now
                                last_voice_time[0] = now
                        emit({"type": "status", "state": "listening"})
            except Exception as e:
                if not wakeword_thread_stop.is_set():
                    emit({"type": "error", "message": f"Wake-word loop error: {e}"})
            finally:
                try:
                    ww_stream.stop()
                    ww_stream.close()
                except Exception:
                    pass

        t = threading.Thread(target=wakeword_loop, daemon=True)
        t.start()

    def stop_wakeword():
        wakeword_thread_stop.set()
        with lock:
            state["wakeword_active"] = False
        if porcupine_handle[0] is not None:
            try:
                porcupine_handle[0].delete()
            except Exception:
                pass
            porcupine_handle[0] = None
        log("wake-word detection stopped")
        emit({"type": "status", "state": "idle"})

    # Translate SIGTERM (sent by the host's proc.kill() on shutdown/deactivate)
    # into a KeyboardInterrupt so the command loop unwinds through the `finally`
    # block below — closing the mic stream and Porcupine instead of leaving an
    # orphaned process holding the microphone.
    def _on_sigterm(_signum, _frame):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError):
        # Not on the main thread or unsupported platform — best effort only.
        pass

    # ---- Ready ----
    emit({"type": "ready"})

    # ---- Command loop ----
    try:
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except json.JSONDecodeError as e:
                emit({"type": "error", "message": f"Bad JSON from host: {e}"})
                continue

            action = cmd.get("action")

            if action == "listen":
                with lock:
                    if state["recording"]:
                        emit({"type": "error", "message": "Already recording"})
                        continue
                    buffer.clear()
                    state["recording"] = True
                    state["stop_requested"] = False
                    state["has_spoken"] = False
                    now = time.time()
                    record_start[0] = now
                    last_voice_time[0] = now
                emit({"type": "status", "state": "listening"})

            elif action == "stop":
                with lock:
                    if state["recording"]:
                        state["stop_requested"] = True

            elif action == "start_wakeword":
                access_key = cmd.get("access_key", "")
                keyword = cmd.get("keyword", "computer")
                custom_path = cmd.get("custom_keyword_path", "")
                if not access_key:
                    emit({"type": "error", "message": "Picovoice access key is required for wake-word mode."})
                else:
                    start_wakeword(access_key, keyword, custom_path)

            elif action == "stop_wakeword":
                stop_wakeword()

            elif action == "configure":
                new_model = cmd.get("whisper_model")
                if new_model and new_model != config["whisper_model"]:
                    log(f"switching whisper model to '{new_model}'...")
                    try:
                        model_holder[0] = whisper.load_model(new_model)
                        config["whisper_model"] = new_model
                        log(f"whisper model switched to '{new_model}'")
                    except Exception as e:
                        emit({"type": "error", "message": f"Failed to load model '{new_model}': {e}"})

                new_silence = cmd.get("silence_duration")
                if new_silence is not None:
                    config["silence_duration"] = float(new_silence)
                    log(f"silence duration set to {config['silence_duration']}s")

            elif action == "shutdown":
                break
            else:
                emit({"type": "error", "message": f"Unknown action: {action!r}"})

    except KeyboardInterrupt:
        pass
    finally:
        shutdown_event.set()
        stop_wakeword()
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
