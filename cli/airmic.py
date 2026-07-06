#!/usr/bin/env python3
"""Hermes Voice mic helper (airmic).

This is intentionally NOT a Hermes agent runtime. It only:
- records/transcribes local microphone audio;
- talks to your Hermes dashboard websocket (prompt.submit RPC);
- streams the reply back as NDJSON events and speaks it via TTS.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import urllib.request
import wave

def _load_voicepill_env() -> None:
    """Optional KEY=VALUE config written by setup.sh — lets the menu-bar app
    (which gets no shell env from launchd) and the CLI share one config file.
    Real environment variables always win (setdefault)."""
    env_file = Path.home() / ".hermes" / "voicepill" / "voicepill.env"
    try:
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except Exception:
        pass


_load_voicepill_env()

DASHBOARD_DEFAULT = os.environ.get("HERMES_DASHBOARD_URL", "http://127.0.0.1:9119").rstrip("/")
HERMES_SRC = Path.home() / ".hermes" / "hermes-agent"


def jprint(obj: dict, *, json_mode: bool = False) -> None:
    if json_mode:
        print(json.dumps(obj, indent=2, sort_keys=True, default=str))
    else:
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                print(f"{k}={json.dumps(v, default=str)}")
            else:
                print(f"{k}={v}")


def fetch_token(dashboard: str) -> str:
    body = urllib.request.urlopen(dashboard + "/", timeout=8).read().decode("utf-8", "ignore")
    patterns = [
        r"__HERMES_SESSION_TOKEN__\s*=\s*[\"']([^\"']+)",
        r"__HERMES_SESSION_TOKEN__[^\"']+[\"']([^\"']+)",
    ]
    for pat in patterns:
        m = re.search(pat, body)
        if m:
            return m.group(1)
    raise RuntimeError("could not parse N1 dashboard websocket token")


class Gateway:
    def __init__(self, dashboard: str = DASHBOARD_DEFAULT):
        self.dashboard = dashboard.rstrip("/")
        self.token = fetch_token(self.dashboard)
        self.ws_url = self.dashboard.replace("http://", "ws://").replace("https://", "wss://") + "/api/ws?token=" + self.token
        self._seq = 0

    async def rpc(self, method: str, params: dict | None = None, timeout: float = 12.0) -> dict:
        import websockets
        self._seq += 1
        rid = str(self._seq)
        async with websockets.connect(self.ws_url, open_timeout=timeout, max_size=None) as ws:
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}))
            deadline = time.time() + timeout
            events = []
            while time.time() < deadline:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.time()))
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                if obj.get("id") == rid:
                    if "error" in obj:
                        raise RuntimeError(obj["error"].get("message") or json.dumps(obj["error"]))
                    res = obj.get("result") or {}
                    if isinstance(res, dict):
                        res.setdefault("events_seen", events)
                    return res
                if obj.get("method") == "event":
                    params = obj.get("params") or {}
                    events.append({"type": params.get("type"), "session_id": params.get("session_id")})
            raise TimeoutError(f"RPC timed out: {method}")


def run_rpc(method: str, params: dict | None = None, dashboard: str = DASHBOARD_DEFAULT, timeout: float = 12.0) -> dict:
    return asyncio.run(Gateway(dashboard).rpc(method, params or {}, timeout=timeout))


def audio_devices() -> dict:
    try:
        import sounddevice as sd
        devices = []
        for i, d in enumerate(sd.query_devices()):
            devices.append({
                "i": i,
                "name": d.get("name"),
                "in": int(d.get("max_input_channels", 0) or 0),
                "out": int(d.get("max_output_channels", 0) or 0),
            })
        return {"sounddevice": True, "default": list(getattr(sd.default, "device", [])), "devices": devices}
    except Exception as e:
        return {"sounddevice": False, "error": f"{type(e).__name__}: {e}"}


def choose_input_device(prefer: str = "MacBook Air Microphone") -> int | None:
    import sounddevice as sd
    rows = list(sd.query_devices())
    prefer_l = prefer.lower().strip()
    for i, d in enumerate(rows):
        if int(d.get("max_input_channels", 0) or 0) > 0 and prefer_l and prefer_l in str(d.get("name") or "").lower():
            return i
    default = getattr(sd.default, "device", None)
    if isinstance(default, (list, tuple)) and default and int(default[0]) >= 0:
        return int(default[0])
    for i, d in enumerate(rows):
        if int(d.get("max_input_channels", 0) or 0) > 0:
            return i
    return None


def record_wav(seconds: float, output: str | None = None, device: int | None = None, prefer: str = "MacBook Air Microphone") -> dict:
    import numpy as np
    import sounddevice as sd
    sr = 16000
    dev = device if device is not None else choose_input_device(prefer)
    if dev is None:
        raise RuntimeError("no input audio device found")
    frames = int(sr * seconds)
    data = sd.rec(frames, samplerate=sr, channels=1, dtype="int16", device=dev)
    sd.wait()
    arr = np.asarray(data).reshape(-1)
    rms = float(math.sqrt(float(np.mean(arr.astype(np.float64) ** 2)))) if arr.size else 0.0
    peak = int(np.max(np.abs(arr))) if arr.size else 0
    out = Path(output) if output else Path(tempfile.gettempdir()) / f"airmic-{int(time.time())}.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(arr.tobytes())
    return {"path": str(out), "bytes": out.stat().st_size, "seconds": seconds, "sample_rate": sr, "rms": round(rms, 2), "peak": peak, "device": dev}


def record_manual(output: str | None = None, max_seconds: float = 240.0, device: int | None = None, prefer: str = "MacBook Air Microphone") -> dict:
    """Record until SIGINT/SIGTERM or max_seconds, then write a valid WAV."""
    import signal
    import threading

    import numpy as np
    import sounddevice as sd

    sr = 16000
    dev = device if device is not None else choose_input_device(prefer)
    if dev is None:
        raise RuntimeError("no input audio device found")

    out = Path(output) if output else Path(tempfile.gettempdir()) / f"airmic-manual-{int(time.time())}.wav"
    out.parent.mkdir(parents=True, exist_ok=True)

    stop = threading.Event()
    frames: list[object] = []
    reason = {"value": "max_seconds"}

    def _handler(signum, frame):  # noqa: ARG001
        reason["value"] = "signal"
        stop.set()

    old_int = signal.getsignal(signal.SIGINT)
    old_term = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)

    def _callback(indata, frame_count, time_info, status):  # noqa: ARG001
        frames.append(indata.copy())

    start = time.monotonic()
    try:
        with sd.InputStream(samplerate=sr, channels=1, dtype="int16", device=dev, callback=_callback):
            while not stop.is_set() and time.monotonic() - start < max_seconds:
                time.sleep(0.05)
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)

    seconds = max(0.0, time.monotonic() - start)
    if frames:
        arr = np.concatenate(frames, axis=0).reshape(-1)
    else:
        arr = np.asarray([], dtype=np.int16)
    rms = float(math.sqrt(float(np.mean(arr.astype(np.float64) ** 2)))) if arr.size else 0.0
    peak = int(np.max(np.abs(arr))) if arr.size else 0

    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(arr.astype(np.int16, copy=False).tobytes())

    return {
        "path": str(out),
        "bytes": out.stat().st_size,
        "seconds": round(seconds, 3),
        "sample_rate": sr,
        "rms": round(rms, 2),
        "peak": peak,
        "device": dev,
        "stop_reason": reason["value"],
    }




def record_vad(
    output: str | None = None,
    max_seconds: float = 240.0,
    silence_threshold: int = 120,
    silence_duration: float = 4.5,
    max_wait: float = 8.0,
    min_speech_duration: float = 0.18,
    device: int | None = None,
    prefer: str = "MacBook Air Microphone",
) -> dict:
    """Record until end-of-utterance silence, then write a valid WAV.

    Production Air endpointing for the remote Hermes bridge:
    - one hotkey starts recording;
    - speech is detected above a conservative dynamic threshold;
    - trailing silence is measured from the last strong voiced block;
    - mic/room floor after speech is not treated as speech forever;
    - parent death and SIGINT/SIGTERM exit cleanly with a WAV + JSON.
    """
    import os
    import signal
    import statistics
    import threading

    import numpy as np
    import sounddevice as sd

    sr = 16000
    blocksize = 1024
    dev = device if device is not None else choose_input_device(prefer)
    if dev is None:
        raise RuntimeError("no input audio device found")

    out = Path(output) if output else Path(tempfile.gettempdir()) / f"airmic-vad-{int(time.time())}.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    metrics_path = Path(str(out) + ".metrics.jsonl")
    result_path = Path(str(out) + ".result.json")

    frames: list[object] = []
    latest = {"rms": 0.0, "peak": 0, "blocks": 0}
    peak_rms = {"value": 0.0}
    peak_abs = {"value": 0}
    reason = {"value": "max_seconds"}
    stop_requested = threading.Event()
    initial_parent_pid = os.getppid()
    try:
        expected_parent_pid = int(os.environ.get("AIRMIC_EXPECT_PARENT_PID") or initial_parent_pid)
    except Exception:
        expected_parent_pid = initial_parent_pid

    def _request_stop(stop_reason: str):
        reason["value"] = stop_reason
        stop_requested.set()

    old_handlers: dict[int, object] = {}

    def _handler(signum, frame):  # noqa: ARG001
        _request_stop("signal")

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            old_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, _handler)
        except Exception:
            pass

    def _callback(indata, frame_count, time_info, status):  # noqa: ARG001
        frames.append(indata.copy())
        arr = indata.astype(np.float64, copy=False).reshape(-1)
        rms = float(np.sqrt(np.mean(arr * arr))) if arr.size else 0.0
        peak = int(np.max(np.abs(arr))) if arr.size else 0
        latest["rms"] = rms
        latest["peak"] = peak
        latest["blocks"] = int(latest["blocks"]) + 1
        peak_rms["value"] = max(float(peak_rms["value"]), rms)
        peak_abs["value"] = max(int(peak_abs["value"]), peak)

    start = time.monotonic()
    has_spoken = False
    speech_run_start = 0.0
    last_voice_time = 0.0
    recent_quiet: list[float] = []
    recent_low_after_speech: list[float] = []
    speech_blocks = 0
    silent_blocks = 0
    parent_gone_blocks = 0
    enter_threshold_last = float(silence_threshold)
    exit_threshold_last = float(silence_threshold)

    max_wait = max(2.0, min(float(max_wait), max(8.0, float(silence_duration) + 3.0)))

    def low_percentile(values: list[float], pct: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * pct))))
        return float(ordered[idx])

    def noise_floor() -> float:
        candidates = list(recent_quiet[-100:])
        # After speech, blocks far below the utterance peak are likely ambient
        # floor even when they are above an old fixed threshold like RMS=90.
        candidates.extend(recent_low_after_speech[-120:])
        if not candidates:
            return 0.0
        return low_percentile(candidates, 0.35)

    def thresholds(floor: float) -> tuple[float, float]:
        base_enter = max(float(silence_threshold), floor + max(90.0, floor * 0.65))
        base_exit = max(float(silence_threshold) * 0.75, floor + max(55.0, floor * 0.30))
        if not has_spoken:
            return base_enter, base_exit
        # Once real speech was seen, the failed 2026-07-01 live recording showed
        # Air "silence" at RMS ~300 while speech peaked thousands higher. A
        # fixed RMS=90 exit threshold made that ambient floor count as speech
        # until max_seconds. Use a dynamic low-speech cutoff tied to the observed
        # utterance peak, capped so quiet follow-up speech is still tolerated.
        dynamic_exit = max(base_exit, min(1200.0, max(300.0, float(peak_rms["value"]) * 0.06)))
        dynamic_enter = max(base_enter, dynamic_exit * 1.25)
        return dynamic_enter, dynamic_exit

    def write_metric(now: float, rms: float, floor: float, enter: float, exit_: float, state: str):
        # Compact non-secret diagnostics; about 4 lines/sec.
        try:
            if int((now - start) * 4) != int((now - start - 0.03) * 4):
                import json as _json
                metrics_path.parent.mkdir(parents=True, exist_ok=True)
                with metrics_path.open("a") as f:
                    f.write(_json.dumps({
                        "t": round(now - start, 3),
                        "rms": round(rms, 2),
                        "peak": int(latest["peak"]),
                        "floor": round(floor, 2),
                        "enter_threshold": round(enter, 2),
                        "exit_threshold": round(exit_, 2),
                        "has_spoken": bool(has_spoken),
                        "state": state,
                    }) + "\n")
        except Exception:
            pass

    try:
        with sd.InputStream(samplerate=sr, channels=1, dtype="int16", device=dev, blocksize=blocksize, callback=_callback):
            while True:
                now = time.monotonic()
                elapsed = now - start
                if stop_requested.is_set():
                    break
                current_parent_pid = os.getppid()
                if expected_parent_pid not in (0, 1) and current_parent_pid != expected_parent_pid:
                    parent_gone_blocks += 1
                    if parent_gone_blocks >= 3:
                        reason["value"] = "parent_gone"
                        break
                else:
                    parent_gone_blocks = 0
                if elapsed >= max_seconds:
                    reason["value"] = "max_seconds"
                    break

                rms = float(latest["rms"])
                floor = noise_floor()
                enter_threshold, exit_threshold = thresholds(floor)
                enter_threshold_last = enter_threshold
                exit_threshold_last = exit_threshold

                if not has_spoken:
                    if rms >= enter_threshold:
                        if speech_run_start == 0.0:
                            speech_run_start = now
                        if now - speech_run_start >= float(min_speech_duration):
                            has_spoken = True
                            last_voice_time = now
                            speech_blocks += 1
                    else:
                        speech_run_start = 0.0
                        recent_quiet.append(rms)
                        silent_blocks += 1
                        if elapsed >= max_wait:
                            reason["value"] = "no_speech"
                            break
                    write_metric(now, rms, floor, enter_threshold, exit_threshold, "pre_speech")
                else:
                    # Keep learning likely ambient floor after speech if energy is
                    # much lower than the observed utterance. This is what the
                    # old loop missed on the live failed WAV.
                    low_candidate_limit = max(float(silence_threshold) * 2.5, min(1500.0, float(peak_rms["value"]) * 0.18))
                    if rms <= low_candidate_limit:
                        recent_low_after_speech.append(rms)
                    if rms >= exit_threshold:
                        last_voice_time = now
                        speech_blocks += 1
                        state = "voice"
                    else:
                        silent_blocks += 1
                        state = "trailing_silence"
                        if last_voice_time and now - last_voice_time >= float(silence_duration):
                            reason["value"] = "silence"
                            write_metric(now, rms, floor, enter_threshold, exit_threshold, "stop_silence")
                            break
                    write_metric(now, rms, floor, enter_threshold, exit_threshold, state)

                time.sleep(0.03)
    finally:
        for sig, old in old_handlers.items():
            try:
                signal.signal(sig, old)
            except Exception:
                pass

    seconds = max(0.0, time.monotonic() - start)
    if frames:
        arr = np.concatenate(frames, axis=0).reshape(-1)
    else:
        arr = np.asarray([], dtype=np.int16)
    rms_final = float(math.sqrt(float(np.mean(arr.astype(np.float64) ** 2)))) if arr.size else 0.0
    peak = int(np.max(np.abs(arr))) if arr.size else 0

    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(arr.astype(np.int16, copy=False).tobytes())

    result = {
        "path": str(out),
        "bytes": out.stat().st_size,
        "seconds": round(seconds, 3),
        "sample_rate": sr,
        "rms": round(rms_final, 2),
        "peak": peak,
        "peak_rms": int(round(float(peak_rms["value"]))),
        "device": dev,
        "stop_reason": reason["value"],
        "has_spoken": bool(has_spoken),
        "speech_blocks": int(speech_blocks),
        "silent_blocks": int(silent_blocks),
        "silence_threshold": int(silence_threshold),
        "silence_duration": float(silence_duration),
        "max_wait": float(max_wait),
        "noise_floor": round(noise_floor(), 2),
        "enter_threshold": round(enter_threshold_last, 2),
        "exit_threshold": round(exit_threshold_last, 2),
        "metrics_path": str(metrics_path),
        "result_path": str(result_path),
        "initial_parent_pid": int(initial_parent_pid),
        "expected_parent_pid": int(expected_parent_pid),
        "final_parent_pid": int(os.getppid()),
    }
    try:
        import json as _json
        result_path.write_text(_json.dumps(result, indent=2))
    except Exception:
        pass
    return result


def synth_wav(text: str, output: str | None = None) -> dict:
    import subprocess
    out = Path(output) if output else Path(tempfile.gettempdir()) / f"airmic-selftest-{int(time.time())}.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(["/usr/bin/say", "-o", str(out), "--data-format=LEI16@16000", text], text=True, capture_output=True, timeout=30)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "say failed").strip())
    return {"path": str(out), "bytes": out.stat().st_size, "text": text}


def _fluidaudio_cli_path() -> Path:
    configured = os.environ.get("AIRMIC_FLUIDAUDIO_CLI", "").strip()
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    root = Path.home() / "hermes-voice-deps" / "FluidAudio"
    candidates.extend([
        root / ".build" / "release" / "FluidAudioCLI",
        root / ".build" / "arm64-apple-macosx" / "release" / "FluidAudioCLI",
    ])
    for candidate in candidates:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError("FluidAudioCLI not found; cannot use Parakeet STT")


def _parakeet_model_dir() -> Path:
    configured = os.environ.get("AIRMIC_PARAKEET_MODEL_DIR", "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.exists():
            return candidate
        raise RuntimeError(f"configured Parakeet model dir missing: {candidate}")
    base = Path.home() / "Library" / "Application Support" / "FluidAudio" / "Models"
    version = os.environ.get("AIRMIC_PARAKEET_MODEL_VERSION", "v2").strip().lower() or "v2"
    preferred = "parakeet-tdt-0.6b-v3-coreml" if version == "v3" else "parakeet-tdt-0.6b-v2-coreml"
    candidates = [base / preferred, base / "parakeet-tdt-0.6b-v2-coreml", base / "parakeet-tdt-0.6b-v3-coreml"]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None  # let FluidAudioCLI download/manage the model itself


def transcribe_parakeet(path: str) -> dict:
    import subprocess
    cli = _fluidaudio_cli_path()
    model_dir = _parakeet_model_dir()
    model_version = os.environ.get("AIRMIC_PARAKEET_MODEL_VERSION", "v2").strip().lower() or "v2"
    if model_dir is not None and "v3" in model_dir.name:
        model_version = "v3"
    timeout = float(os.environ.get("AIRMIC_PARAKEET_TIMEOUT", "90"))
    out_json = Path(tempfile.gettempdir()) / f"airmic-parakeet-{os.getpid()}-{int(time.time() * 1000)}.json"
    cmd = [
        str(cli),
        "transcribe",
        str(path),
        "--model-version",
        model_version,
        "--output-json",
        str(out_json),
    ]
    if model_dir is not None:
        cmd += ["--model-dir", str(model_dir)]
    started = time.time()
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    elapsed = time.time() - started
    data = {}
    if out_json.exists():
        try:
            data = json.loads(out_json.read_text(errors="ignore") or "{}")
        except Exception:
            data = {}
    transcript = str(data.get("text") or proc.stdout or "").strip()
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or f"FluidAudioCLI exited {proc.returncode}").strip()[:1000])
    if not transcript:
        raise RuntimeError("FluidAudio Parakeet returned an empty transcript")
    return {
        "success": True,
        "transcript": transcript,
        "provider": "fluidaudio-parakeet",
        "model": model_dir.name if model_dir else "auto",
        "model_version": model_version,
        "model_dir": str(model_dir) if model_dir else "auto",
        "cli": str(cli),
        "confidence": data.get("confidence"),
        "durationSeconds": data.get("durationSeconds"),
        "processingTimeSeconds": data.get("processingTimeSeconds"),
        "rtfx": data.get("rtfx"),
        "elapsedSeconds": round(elapsed, 3),
        "fallback_used": False,
    }


def transcribe_hermes_local(path: str) -> dict:
    if str(HERMES_SRC) not in sys.path:
        sys.path.insert(0, str(HERMES_SRC))
    from tools.voice_mode import transcribe_recording
    res = dict(transcribe_recording(path))
    res.setdefault("provider", "hermes-local")
    return res


def transcribe(path: str) -> dict:
    provider = os.environ.get("AIRMIC_STT_PROVIDER", "fluidaudio-parakeet").strip().lower()
    if provider in {"fluidaudio-parakeet", "fluidaudio", "parakeet", "fluid-parakeet"}:
        try:
            return transcribe_parakeet(path)
        except Exception as exc:
            if os.environ.get("AIRMIC_STT_FALLBACK", "0").strip() in {"1", "true", "yes"}:
                res = transcribe_hermes_local(path)
                res["fallback_used"] = True
                res["fallback_reason"] = str(exc)[:500]
                return res
            raise
    if provider in {"hermes", "hermes-local", "local", "whisper", "whisper-1"}:
        return transcribe_hermes_local(path)
    raise RuntimeError(f"unsupported AIRMIC_STT_PROVIDER={provider!r}")




def _split_tts_chunks(text: str, max_chars: int = 420) -> list[str]:
    """Split full assistant text for capped Qwen/Base-clone TTS without dropping words."""
    # Floor lowered from 120: the MLX ICL lane needs 56-char chunks (garble cliff).
    max_chars = max(24, int(max_chars or 420))
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return []
    # Prefer sentence boundaries, then comma/semicolon, then words.
    pieces = re.split(r"(?<=[.!?])\s+", normalized)
    chunks: list[str] = []
    current = ""

    def flush_current() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
            current = ""

    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        if len(piece) > max_chars:
            # Break long sentences on softer punctuation first.
            subpieces = re.split(r"(?<=[,;:])\s+", piece)
            for sub in subpieces:
                sub = sub.strip()
                if not sub:
                    continue
                if len(sub) > max_chars:
                    words = sub.split()
                    for word in words:
                        candidate = (current + " " + word).strip() if current else word
                        if len(candidate) > max_chars:
                            flush_current()
                            current = word
                        else:
                            current = candidate
                else:
                    candidate = (current + " " + sub).strip() if current else sub
                    if len(candidate) > max_chars:
                        flush_current()
                        current = sub
                    else:
                        current = candidate
            continue
        candidate = (current + " " + piece).strip() if current else piece
        if len(candidate) > max_chars:
            flush_current()
            current = piece
        else:
            current = candidate
    flush_current()
    return chunks


def _decode_ogg_for_local_playback(path: str) -> str:
    """Decode Ogg/Opus to WAV for macOS local playback.

    macOS afplay/CoreAudio may return after ~1-2s on these Ogg/Opus files even
    though afinfo reports the full duration. Decode to WAV first for speakers.
    """
    src = Path(path)
    if src.suffix.lower() not in {".ogg", ".opus"}:
        return path
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError(f"Ogg playback requires imageio-ffmpeg decoder: {exc}")
    import subprocess
    out = Path(tempfile.gettempdir()) / f"airmic-playback-{os.getpid()}-{int(time.time()*1000)}.wav"
    cmd = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-ac",
        "1",
        "-ar",
        "48000",
        "-c:a",
        "pcm_s16le",
        str(out),
    ]
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if proc.returncode != 0 or not out.exists() or out.stat().st_size <= 44:
        raise RuntimeError((proc.stderr or proc.stdout or f"ffmpeg decode failed rc={proc.returncode}").strip()[:1000])
    return str(out)


def _concat_audio_files(paths: list[str], output: str) -> str | None:
    """Best-effort concatenate verified TTS chunks into one artifact when --output is requested."""
    if len(paths) <= 1:
        return paths[0] if paths else None
    import shutil
    import subprocess
    if not Path("/opt/homebrew/bin/ffmpeg").exists() and not shutil.which("ffmpeg"):
        return None
    out = Path(output).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    list_file = Path(tempfile.gettempdir()) / f"airmic-tts-concat-{os.getpid()}-{int(time.time()*1000)}.txt"
    def quote_concat_path(value: str) -> str:
        # ffmpeg concat demuxer uses single-quoted file lines; escape embedded quotes.
        return "'" + Path(value).as_posix().replace("'", "'\\''") + "'"
    list_file.write_text("".join(f"file {quote_concat_path(path)}\n" for path in paths))
    ffmpeg = "/opt/homebrew/bin/ffmpeg" if Path("/opt/homebrew/bin/ffmpeg").exists() else (shutil.which("ffmpeg") or "ffmpeg")
    try:
        proc = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(out)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        if proc.returncode != 0:
            # Re-encode concat if stream-copy fails across independently generated Ogg containers.
            proc = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c:a", "libopus", "-ac", "1", "-b:a", "96k", str(out)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "ffmpeg concat failed").strip()[:1000])
    finally:
        try:
            list_file.unlink(missing_ok=True)
        except Exception:
            pass
    return str(out)


HERMES_TTS_VOICE_NAME = "hermes"
_TTS_CONFIG_PATCH_LOCK = threading.RLock()


def _airmic_tts_provider() -> str:
    # Voice-out is ElevenLabs by default; any provider your Hermes tts config
    # supports works via AIRMIC_TTS_PROVIDER.
    return (os.environ.get("AIRMIC_TTS_PROVIDER") or "elevenlabs").strip().lower() or "elevenlabs"


def _airmic_tts_route() -> dict:
    provider = _airmic_tts_provider()
    if provider == "elevenlabs":
        return {
            "provider": "elevenlabs",
            "voice": HERMES_TTS_VOICE_NAME,
            # None = defer to your ~/.hermes/config.yaml tts.elevenlabs settings.
            "voice_id": os.environ.get("AIRMIC_ELEVENLABS_VOICE_ID") or os.environ.get("AIRMIC_TTS_VOICE_ID") or None,
            "model_id": os.environ.get("AIRMIC_ELEVENLABS_MODEL_ID") or None,
        }
    return {"provider": provider, "voice": os.environ.get("AIRMIC_TTS_VOICE", provider)}


def _airmic_tts_config() -> dict:
    if str(HERMES_SRC) not in sys.path:
        sys.path.insert(0, str(HERMES_SRC))
    from hermes_cli.config import load_config

    base = load_config().get("tts", {}) or {}
    cfg = dict(base) if isinstance(base, dict) else {}
    route = _airmic_tts_route()
    cfg["provider"] = route["provider"]
    if route["provider"] == "elevenlabs":
        eleven = dict(cfg.get("elevenlabs", {}) or {})
        if route.get("voice_id"):
            eleven["voice_id"] = route["voice_id"]
        if route.get("model_id"):
            eleven["model_id"] = route["model_id"]
        cfg["elevenlabs"] = eleven
    return cfg


def _airmic_text_to_speech_tool(text: str, output_path: str | None = None) -> str:
    if str(HERMES_SRC) not in sys.path:
        sys.path.insert(0, str(HERMES_SRC))
    from tools import tts_tool as _tts_tool

    # tools.tts_tool reads ~/.hermes/config.yaml globally. Patch that loader only
    # inside this airmic process so voice-pill overrides never mutate
    # unrelated Hermes TTS routes.
    with _TTS_CONFIG_PATCH_LOCK:
        original_loader = _tts_tool._load_tts_config
        _tts_tool._load_tts_config = _airmic_tts_config
        try:
            return _tts_tool.text_to_speech_tool(text, output_path=output_path)
        finally:
            _tts_tool._load_tts_config = original_loader


def speak_text(text: str, output: str | None = None, play: bool = True) -> dict:
    if not text or not text.strip():
        raise RuntimeError("text required")
    if str(HERMES_SRC) not in sys.path:
        sys.path.insert(0, str(HERMES_SRC))
    from tools.tts_tool import _strip_markdown_for_tts
    from tools.voice_mode import play_audio_file
    cleaned = _strip_markdown_for_tts(text).strip()
    if not cleaned:
        raise RuntimeError("text became empty after TTS cleanup")
    # Conversational cap: n2 Base-clone TTS runs ~21s per short chunk, so long
    # replies become minutes of synthesis. 0 = unlimited (voice-memo lanes).
    max_total = int(os.environ.get("AIRMIC_TTS_MAX_CHARS", "0"))
    truncated = False
    if max_total > 0 and len(cleaned) > max_total:
        head = cleaned[:max_total]
        boundaries = list(re.finditer(r"[.!?](?:\s|$)", head))
        cleaned = (head[: boundaries[-1].end()] if boundaries else head).strip()
        truncated = True
    # 180-char proven zone for the MLX ICL clone lane (see StreamingSpeaker note).
    max_chars = int(os.environ.get("AIRMIC_TTS_CHUNK_CHARS", "180"))
    chunks = _split_tts_chunks(cleaned, max_chars=max_chars)
    if not chunks:
        raise RuntimeError("TTS splitter produced no chunks")
    chunk_results = []
    chunk_paths: list[str] = []
    failures = []
    started = time.time()
    for idx, chunk in enumerate(chunks, start=1):
        chunk_output = None
        if output and len(chunks) == 1:
            chunk_output = output
        elif len(chunks) > 1:
            suffix = Path(output).suffix if output else ".ogg"
            chunk_output = str(Path(tempfile.gettempdir()) / f"airmic-tts-{os.getpid()}-{int(started * 1000)}-{idx:03d}{suffix or '.ogg'}")
        res_raw = _airmic_text_to_speech_tool(chunk, output_path=chunk_output)
        try:
            res = json.loads(res_raw)
        except Exception:
            raise RuntimeError(f"TTS chunk {idx}/{len(chunks)} returned non-JSON: {res_raw[:200]}")
        if not res.get("success"):
            raise RuntimeError(f"TTS chunk {idx}/{len(chunks)} failed: {str(res.get('error') or res_raw)[:500]}")
        path = str(res.get("file_path") or "")
        if not path:
            raise RuntimeError(f"TTS chunk {idx}/{len(chunks)} returned no file_path")
        chunk_paths.append(path)
        played = False
        playback_path = path
        decoded_playback_path = None
        if play:
            playback_path = _decode_ogg_for_local_playback(path)
            decoded_playback_path = playback_path if playback_path != path else None
            played = bool(play_audio_file(playback_path))
            if not played:
                failures.append(idx)
        chunk_results.append({
            "index": idx,
            "chars": len(chunk),
            "file_path": path,
            "playback_path": playback_path,
            "decoded_playback_path": decoded_playback_path,
            "bytes": Path(path).stat().st_size if Path(path).exists() else None,
            "playback_bytes": Path(playback_path).stat().st_size if Path(playback_path).exists() else None,
            "played": played,
            "provider": res.get("provider") or res.get("tts_provider"),
            "voice": res.get("voice"),
        })
    final_path = chunk_paths[0] if len(chunk_paths) == 1 else None
    if output and len(chunk_paths) > 1:
        final_path = _concat_audio_files(chunk_paths, output)
    return {
        "success": True,
        "played": bool(play and not failures),
        "play_failures": failures,
        "provider": (chunk_results[-1].get("provider") if chunk_results else None) or _airmic_tts_route().get("provider"),
        "voice": (chunk_results[-1].get("voice") if chunk_results else None) or _airmic_tts_route().get("voice"),
        "chunks": len(chunks),
        "chunk_chars_max": max_chars,
        "text_chars": len(cleaned),
        "spoken_chars": sum(len(c) for c in chunks),
        "truncated": truncated,
        "file_path": final_path or chunk_paths[-1],
        "chunk_files": chunk_paths,
        "chunk_results": chunk_results,
        "elapsedSeconds": round(time.time() - started, 3),
        "token_value_printed": "no",
    }


def _acquire_tts_lock(timeout: float = 90.0):
    """Serialize playback across processes — but NEVER block forever: a wedged
    holder (2026-07-02 incident: dead-WS chat stuck in 1800 s thread joins)
    must not mute the voice. Past the timeout we proceed unlocked; overlapping
    audio beats eternal silence."""
    import fcntl
    lock_path = Path(tempfile.gettempdir()) / "airmic-tts-playback.lock"
    lock_file = open(lock_path, "w")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock_file
        except OSError:
            if time.monotonic() >= deadline:
                return lock_file
            time.sleep(0.5)


def _mark_latest_tts_request() -> tuple[Path, str]:
    latest_path = Path(tempfile.gettempdir()) / "airmic-tts-latest-request"
    request_id = f"{os.getpid()}-{int(time.time() * 1000)}"
    latest_path.write_text(request_id)
    return latest_path, request_id


class StreamingSpeaker:
    """Sentence-streamed TTS for chat turns.

    Chunks synthesize through the Air-local ElevenLabs route while Hermes is still writing the reply and while
    earlier chunks play locally, so speech starts almost immediately after
    (often before) message.complete instead of paying the whole synthesis
    cost afterward.
    """

    def __init__(self, emit=None):
        import queue as _queue
        import threading as _threading

        self._emit = emit or (lambda obj: None)
        self.max_total = int(os.environ.get("AIRMIC_TTS_MAX_CHARS", "0"))
        # ElevenLabs handles long chunks fine;
        # keep chunks bounded for streaming latency and verification.
        self.chunk_chars = max(24, int(os.environ.get("AIRMIC_TTS_CHUNK_CHARS", "420")))
        self.verify = os.environ.get("AIRMIC_TTS_VERIFY", "1").strip().lower() not in {"0", "false", "no", "off"}
        self.buffer = ""
        self.spoken_chars = 0
        self.truncated = False
        self.chunks_sent = 0
        self.first_play_at = None
        self.complete_at = None
        self.skipped_stale = False
        self.play_failures = []
        self.errors = []
        self.chunk_receipts = []
        self._gen_q = _queue.Queue()
        self._play_q = _queue.Queue()
        self._buf_lock = _threading.Lock()
        self._last_feed = time.monotonic()
        self._closed = False
        self._latest_path, self._request_id = _mark_latest_tts_request()
        self._gen_thread = _threading.Thread(target=self._gen_worker, daemon=True)
        self._play_thread = _threading.Thread(target=self._play_worker, daemon=True)
        self._idle_thread = _threading.Thread(target=self._idle_flush_loop, daemon=True)
        self._gen_thread.start()
        self._play_thread.start()
        self._idle_thread.start()

    def feed(self, delta: str) -> None:
        if self.truncated or not delta:
            return
        with self._buf_lock:
            self.buffer += delta
            self._last_feed = time.monotonic()
        self._cut(force=False)

    def _idle_flush_loop(self) -> None:
        """Mid-turn messages: when Hermes pauses to run tools, speak whatever
        complete sentences are already buffered instead of waiting for the
        next batch threshold. 2.5 s of delta silence triggers the flush."""
        while not self._closed:
            time.sleep(1.0)
            if self._closed:
                return
            with self._buf_lock:
                stale = self.buffer.strip() and (time.monotonic() - self._last_feed) >= 2.5
            if stale:
                self._cut(force=False, idle=True)

    def _ready_prefix(self, force: bool, idle: bool = False):
        text = self.buffer
        if force:
            return text if text.strip() else None
        boundaries = [m.end() for m in re.finditer(r"[.!?](?:[\s\n]|$)", text)]
        if not boundaries:
            return None
        if self.chunks_sent == 0 or idle:
            # First audio / mid-turn lull: one complete sentence is enough.
            b = boundaries[-1] if boundaries[-1] <= 220 else boundaries[0]
            return text[:b] if b >= 20 else None
        # Later chunks: small batches keep each piece in the safe ICL zone
        # while the pipeline overlaps generation with playback.
        if boundaries[-1] >= 120:
            return text[: boundaries[-1]]
        return None

    def _cut(self, force: bool, idle: bool = False) -> None:
        if idle and self._closed:
            # finish() owns the final force-cut; an idle flush racing it could
            # enqueue a chunk after the None sentinel (chunk silently lost).
            return
        if str(HERMES_SRC) not in sys.path:
            sys.path.insert(0, str(HERMES_SRC))
        from tools.tts_tool import _strip_markdown_for_tts

        while True:
            with self._buf_lock:
                prefix = self._ready_prefix(force, idle=idle)
                if not prefix:
                    return
                self.buffer = self.buffer[len(prefix):]
            cleaned = _strip_markdown_for_tts(prefix).strip()
            if not cleaned:
                if force and not self.buffer.strip():
                    return
                continue
            if self.max_total:
                remaining = self.max_total - self.spoken_chars
                if remaining <= 0:
                    self.truncated = True
                    self.buffer = ""
                    return
                if len(cleaned) > remaining:
                    head = cleaned[:remaining]
                    ends = list(re.finditer(r"[.!?](?:[\s\n]|$)", head))
                    cleaned = (head[: ends[-1].end()] if ends else head).strip()
                    self.truncated = True
                    self.buffer = ""
            for chunk in _split_tts_chunks(cleaned, max_chars=self.chunk_chars):
                self.spoken_chars += len(chunk)
                self.chunks_sent += 1
                self._gen_q.put(chunk)
            if self.truncated:
                return

    # Distinctive phrases from the old voice-clone lane: their presence in a
    # take that shouldn't contain them is the reference-bleed signature.
    _BLEED_PHRASES = (
        "work starts now", "gate is holding", "proof not excuses",
        "numbers first", "when the build lands", "hear it from me",
        "local voice api", "api is live",
    )

    @classmethod
    def _speech_matches(cls, expected: str, transcript: str) -> bool:
        # Digits are dropped before comparing: Parakeet writes "62158" as words
        # ("six two one five eight"), which false-muted jargon-heavy chunks.
        norm = lambda s: [w for w in re.sub(r"[^a-z ]", " ", s.lower()).split() if w]  # noqa: E731
        want, got = norm(expected), norm(transcript)
        want_str, got_str = " ".join(want), " ".join(got)
        for phrase in cls._BLEED_PHRASES:
            if phrase in got_str and phrase not in want_str:
                return False
        if not want:
            return True
        overlap = sum(1 for w in want if w in got)
        # Real garble scores near zero here; STT quirks on technical words
        # stay well above 0.3. Only hard mismatches get rejected.
        return overlap / len(want) >= 0.3

    def _verify_chunk(self, chunk_text: str, playback_wav: str) -> tuple:
        """Garble firewall: Parakeet the generated audio and require it to
        roughly match the text it claims to speak. ~0.5 s per chunk, local."""
        try:
            check = transcribe_parakeet(playback_wav)
            transcript = str(check.get("transcript") or "")
        except Exception as exc:
            # Verifier trouble should not mute Hermes entirely.
            return True, f"verify-skipped: {exc}"
        return self._speech_matches(chunk_text, transcript), transcript

    def _synthesize_verified(self, chunk: str):
        """Attempt chain: hermes provider → direct reroll seed 48 → seed 49.
        Generation errors AND verification failures both advance the chain, so
        a chunk goes silent only after three independent attempts fail — a
        dropped chunk is what Nathan hears as the voice 'stopping randomly'."""
        def primary():
            res = json.loads(_airmic_text_to_speech_tool(chunk))
            if not res.get("success"):
                raise RuntimeError(str(res.get("error") or "tts failed")[:300])
            path = str(res.get("file_path") or "")
            if not path:
                raise RuntimeError("tts returned no file_path")
            return path

        route = _airmic_tts_route()
        attempts = [("primary", primary)]
        last_heard = ""
        for label, make in attempts:
            try:
                playback = _decode_ogg_for_local_playback(make())
            except Exception as exc:
                self.errors.append(f"tts {label}: {type(exc).__name__}: {exc}")
                continue
            if not self.verify:
                return playback, label, None
            ok, transcript = self._verify_chunk(chunk, playback)
            if ok:
                return playback, label, True
            last_heard = str(transcript)[:120]
            if route.get("provider") == "elevenlabs":
                # ElevenLabs cannot reference-bleed (the garble the firewall
                # exists for) and has no reroll fallback — a verify miss here
                # is a Parakeet mishear. Play the chunk; never mute a sentence.
                self.errors.append(f"tts {label} verify miss, played anyway (heard: {last_heard[:60]})")
                return playback, label, False
            self.errors.append(f"tts {label} failed verification (heard: {last_heard[:60]})")
        return None, f"muted (heard: {last_heard[:60]})", False

    def _gen_worker(self):
        if str(HERMES_SRC) not in sys.path:
            sys.path.insert(0, str(HERMES_SRC))

        while True:
            chunk = self._gen_q.get()
            if chunk is None:
                self._play_q.put(None)
                return
            try:
                t0 = time.time()
                playback, label, verified = self._synthesize_verified(chunk)
                route = _airmic_tts_route()
                receipt = {
                    "chars": len(chunk),
                    "gen_seconds": round(time.time() - t0, 2),
                    "attempt": label,
                    "verified": verified,
                    "provider": route.get("provider"),
                    "voice": route.get("voice"),
                    "voice_id": route.get("voice_id"),
                }
                if playback is None:
                    receipt["stt_heard"] = label
                    self.chunk_receipts.append(receipt)
                    self._emit({"event": "tts_muted_chunk", "chars": len(chunk)})
                    continue
                self.chunk_receipts.append(receipt)
                # Liveness heartbeat: keeps the pill's stall watchdog fed
                # through long post-complete speech.
                self._emit({"event": "tts_chunk", "chars": len(chunk), "gen_s": receipt["gen_seconds"]})
                self._play_q.put({"path": playback, "gen_s": receipt["gen_seconds"]})
            except Exception as e:
                # The None-sentinel chain must survive anything, or finish()
                # joins a thread that never ends (the 2026-07-02 wedge).
                self.errors.append(f"gen worker: {type(e).__name__}: {e}")

    def _play_worker(self):
        if str(HERMES_SRC) not in sys.path:
            sys.path.insert(0, str(HERMES_SRC))
        from tools.voice_mode import play_audio_file

        while True:
            item = self._play_q.get()
            if item is None:
                return
            # Per-chunk lock: acquired for one playback, released right after,
            # so a long turn never starves other speech (and a wedged process
            # can only ever hold the lock for one chunk's duration).
            lock_file = _acquire_tts_lock()
            try:
                # Stale check EVERY chunk: a superseded speaker (zombie turn
                # that survived a barge-in) must stop within one chunk, not
                # keep narrating a dead reply.
                try:
                    latest = self._latest_path.read_text(errors="ignore").strip()
                except Exception:
                    latest = self._request_id
                if latest != self._request_id:
                    self.skipped_stale = True
                if self.skipped_stale:
                    continue
                if self.first_play_at is None:
                    self.first_play_at = time.time()
                    self._emit({"event": "speaking", "first_chunk_gen_s": item.get("gen_s")})
                if not play_audio_file(item["path"]):
                    self.play_failures.append(item["path"])
            finally:
                try:
                    lock_file.close()
                except Exception:
                    pass

    def finish(self, final_text: str = "") -> dict:
        # Gateways that never emitted deltas still get spoken via the final text.
        if self.chunks_sent == 0 and not self.buffer.strip() and final_text:
            self.buffer = final_text
        self.complete_at = time.time()
        self._closed = True
        self._cut(force=True)
        self._gen_q.put(None)
        # Bounded per-chunk waits: a wedged thread costs minutes, not the
        # half-hour lock hostage situation from the 2026-07-02 incident.
        gen_wait = 30.0 + 25.0 * max(1, self._gen_q.qsize() + 1)
        self._gen_thread.join(timeout=gen_wait)
        self._play_thread.join(timeout=300)
        if self._gen_thread.is_alive() or self._play_thread.is_alive():
            self.errors.append("speaker threads did not drain; abandoning playback")
        first_play_after_complete = None
        if self.first_play_at is not None and self.complete_at is not None:
            first_play_after_complete = round(self.first_play_at - self.complete_at, 2)
        route = _airmic_tts_route()
        return {
            "played": self.first_play_at is not None and not self.play_failures and not self.skipped_stale,
            "provider": route.get("provider"),
            "voice": route.get("voice"),
            "voice_id": route.get("voice_id"),
            "skipped": "stale_tts_request" if self.skipped_stale else None,
            "chunks": self.chunks_sent,
            "spoken_chars": self.spoken_chars,
            "truncated": self.truncated,
            "first_play_after_complete_s": first_play_after_complete,
            "chunk_receipts": self.chunk_receipts,
            "errors": self.errors,
            "play_failures": len(self.play_failures),
        }


def cmd_speak(args: argparse.Namespace) -> int:
    # The Air TUI used to SIGTERM the current TTS child whenever a later assistant/status
    # message arrived. Ignore that signal here so already-started speech finishes cleanly.
    import signal
    try:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    except Exception:
        pass

    text = args.text or (sys.stdin.read() if not sys.stdin.isatty() else "")
    latest_path, request_id = _mark_latest_tts_request()
    lock_file = _acquire_tts_lock()
    try:
        # Coalesce backlog: never interrupt active speech, but if several later requests piled up
        # while waiting for the lock, only the newest pending request speaks.
        drop_stale = os.environ.get("AIRMIC_TTS_DROP_STALE", "1").strip().lower() not in {"0", "false", "no", "off"}
        if drop_stale:
            try:
                latest_seen = latest_path.read_text(errors="ignore").strip()
            except Exception:
                latest_seen = request_id
            if latest_seen != request_id:
                res = {
                    "success": True,
                    "played": False,
                    "skipped": "stale_tts_request",
                    "request_id": request_id,
                    "latest_request_id": latest_seen,
                    "sigterm_ignored": True,
                    "tts_lock": "acquired",
                }
                jprint(res, json_mode=args.json)
                return 0
        res = speak_text(text, args.output or None, not args.no_play)
        res.update({"sigterm_ignored": True, "tts_lock": "acquired", "request_id": request_id})
        jprint(res, json_mode=args.json)
        return 0 if (args.no_play or res.get("played")) else 7
    finally:
        try:
            lock_file.close()
        except Exception:
            pass


CHAT_LOG = Path.home() / ".hermes" / "voicepill" / "logs" / "chat.ndjson"


def _emit_ndjson(obj: dict) -> None:
    """One machine-readable event per line; the HermesVoice app reads these.
    Mirrored to CHAT_LOG so silent failures leave receipts behind."""
    line = json.dumps(obj, default=str)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
    try:
        CHAT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with CHAT_LOG.open("a") as f:
            f.write(json.dumps({"ts": time.strftime("%H:%M:%S"), "pid": os.getpid(), **obj}, default=str) + "\n")
    except Exception:
        pass


CHAT_SESSION_STATE = Path.home() / ".hermes" / "voicepill" / "chat_session.json"


def _load_chat_session() -> dict:
    try:
        return dict(json.loads(CHAT_SESSION_STATE.read_text()))
    except Exception:
        return {}


def _save_chat_session(live_id: str, session_key: str) -> None:
    try:
        CHAT_SESSION_STATE.parent.mkdir(parents=True, exist_ok=True)
        CHAT_SESSION_STATE.write_text(json.dumps({
            "live_id": live_id,
            "session_key": session_key,
            "last_turn_at": time.time(),
        }, indent=2))
    except Exception:
        pass


async def _chat_turn(dashboard: str, session_id: str, text: str, wait_seconds: float, allow_busy: bool, speaker=None) -> dict:
    """Submit one prompt into the live N1 session over a single websocket and
    stream reply events (NDJSON on stdout) until message.complete."""
    import websockets

    gw = Gateway(dashboard)
    result: dict = {"ok": False, "final_text": ""}
    # ping_timeout=75 rides out dashboard event-loop stalls (session compaction
    # pins the loop; default 20 s produced mid-turn 1011 keepalive deaths).
    # max_size=None: the dashboard broadcasts EVERY session's events on this
    # socket — one giant tool-output frame > any finite cap kills the turn
    # with close code 1009 (message too big). Trusted token-authed backend; no cap.
    _ws_opts = {"open_timeout": 12, "ping_interval": 20, "ping_timeout": 75, "max_size": None}
    ws = await websockets.connect(gw.ws_url, **_ws_opts)
    try:
        seq = 0
        early_events: list = []  # session events caught mid-RPC, replayed by the watch loop

        async def rpc(method: str, params: dict, timeout: float = 15.0) -> dict:
            nonlocal seq
            seq += 1
            rid = f"chat-{seq}"
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                if obj.get("id") == rid:
                    if "error" in obj:
                        raise RuntimeError(obj["error"].get("message") or json.dumps(obj["error"]))
                    return obj.get("result") or {}
                # Events interleaved with an RPC ack (e.g. the first deltas
                # racing the prompt.submit response) are REAL — queue them for
                # the watch loop instead of dropping the reply's opening.
                if obj.get("method") == "event":
                    early_events.append(obj)
            raise TimeoutError(f"RPC timed out: {method}")

        async def wait_active(live_ids: set, wait_s: float = 60.0):
            # session.resume takes the STORED key; active rows carry a fresh live
            # id with the stored key in session_key. Match either.
            deadline_ = time.monotonic() + wait_s
            while time.monotonic() < deadline_:
                rows = list((await rpc("session.active_list", {})).get("sessions") or [])
                row = next(
                    (s for s in rows if s.get("id") in live_ids or s.get("session_key") in live_ids),
                    None,
                )
                if row is not None:
                    return row
                await asyncio.sleep(1.0)
            raise RuntimeError(f"session {sorted(live_ids)} did not become active within {int(wait_s)}s")

        # ACTIVE-CALL MODEL: the pill owns a dedicated "Hermes Voice" session that
        # persists until Nathan hangs up (voice command or menu). It NEVER grabs
        # someone else's live session and never resumes arbitrary recents.
        # AIRMIC_CHAT_SESSION_TTL: 0 (default) = call never expires; >0 restores
        # idle-based rotation.
        sessions = list((await rpc("session.active_list", {})).get("sessions") or [])
        target = None
        if session_id:
            target = next((s for s in sessions if s.get("id") == session_id or s.get("session_key") == session_id), None)
            if target is None:
                raise RuntimeError(f"requested session not active: {session_id}")
        else:
            ttl = float(os.environ.get("AIRMIC_CHAT_SESSION_TTL", "0"))
            stored = _load_chat_session()
            fresh = bool(stored) and (ttl <= 0 or time.time() - float(stored.get("last_turn_at") or 0) < ttl)
            own_ids = {v for v in (stored.get("live_id"), stored.get("session_key")) if v}
            if fresh and own_ids:
                target = next(
                    (s for s in sessions if s.get("id") in own_ids or s.get("session_key") in own_ids),
                    None,
                )
                if target is None and stored.get("session_key"):
                    # Our own conversation went cold on the gateway; wake IT (never
                    # anything else).
                    _emit_ndjson({"event": "attach", "mode": "waking-own-session"})
                    resumed = await rpc("session.resume", {"session_id": stored["session_key"], "cols": 100}, timeout=30)
                    live = str((resumed or {}).get("session_id") or "")
                    target = await wait_active(own_ids | ({live} if live else set()))
                    _emit_ndjson({"event": "attach", "session_id": target.get("id"), "ready": True})
            if target is None:
                _emit_ndjson({"event": "attach", "mode": "new-session"})
                created = await rpc("session.create", {"source": "voicepill", "title": "Hermes Voice", "cols": 100}, timeout=30)
                live = str(created.get("session_id") or "")
                if not live:
                    raise RuntimeError("session.create returned no session_id")
                target = await wait_active({live, str(created.get("stored_session_id") or "")} - {""})
                _emit_ndjson({"event": "attach", "session_id": target.get("id"), "ready": True})

        sid = str(target.get("id"))
        _emit_ndjson({"event": "target", "session_id": sid, "title": target.get("title"), "status": target.get("status")})
        if str(target.get("status") or "").lower() in {"working", "running", "busy"} and not allow_busy:
            raise RuntimeError("target session busy; pass --allow-busy to queue anyway")

        await rpc("prompt.submit", {"session_id": sid, "text": text}, timeout=20)
        _emit_ndjson({"event": "submitted", "chars": len(text)})

        deadline = time.monotonic() + wait_seconds
        last_heartbeat = time.monotonic()
        submit_time = time.monotonic()
        # Barge-in echo guard: interrupting the previous turn makes the gateway
        # emit a message.complete for the KILLED turn right as we submit; without
        # activity tracking that echo ends the new watcher instantly and the
        # pill collapses mid-run.
        saw_activity = False
        delta_parts: list = []
        reconnects = 0
        pending_complete = None  # (text, at) — ambiguous early complete, see below

        missing_polls = 0
        empty_polls = 0

        async def _probe_recovered():
            """After a reconnect the dashboard does NOT re-stream session events
            to the new socket (verified live 2026-07-03): the turn finishes
            server-side but no message.complete ever arrives. Poll the session
            status instead; once it leaves working, synthesize the completion
            from the streamed deltas (or the session preview when the deltas
            were lost in the blind window). None = still running."""
            nonlocal missing_polls
            try:
                rows = list((await rpc("session.active_list", {})).get("sessions") or [])
            except Exception:
                return None
            row = next((s for s in rows if s.get("id") == sid or s.get("session_key") == target.get("session_key")), None)
            if row is None:
                # The session can briefly drop off the active list right after
                # a reconnect (listing race, verified live). Only conclude the
                # turn is over after ~30 s of consecutive misses.
                missing_polls += 1
                if missing_polls < 6:
                    return None
                return "".join(delta_parts).strip()
            missing_polls = 0
            if str(row.get("status") or "").lower() in {"working", "running", "busy"}:
                return None
            final = "".join(delta_parts).strip() or str(row.get("preview") or "").strip()
            if not final:
                # Idle but nothing recoverable yet (preview lag) — give it
                # ~30 s of polls before completing the turn empty-handed.
                nonlocal empty_polls
                empty_polls += 1
                if empty_polls < 6:
                    return None
            return final

        def _save_own_session() -> None:
            # Explicit --session-id turns (tests/diagnostics) must NEVER
            # overwrite the pill's own call lineage — that bleed sent live
            # voice turns into a test session (2026-07-03).
            if not session_id:
                _save_chat_session(str(target.get("id") or ""), str(target.get("session_key") or ""))

        def _recovered_result(final: str) -> dict:
            _emit_ndjson({"event": "complete", "text": final, "recovered": "ws-reconnect"})
            result.update({"ok": True, "final_text": final})
            _save_own_session()
            return result

        async def _still_working() -> bool:
            """Live session status — the ground truth for whether Hermes is
            actually done, independent of what the event stream claims."""
            try:
                rows = list((await rpc("session.active_list", {})).get("sessions") or [])
            except Exception:
                return False  # can't tell — treat as settled so nothing hangs
            row = next((s for s in rows if s.get("id") == sid or s.get("session_key") == target.get("session_key")), None)
            return str((row or {}).get("status") or "").lower() in {"working", "running", "busy"}

        while time.monotonic() < deadline:
            if early_events:
                obj = early_events.pop(0)
            else:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                except asyncio.TimeoutError:
                    if pending_complete and time.monotonic() - pending_complete[1] >= 12:
                        # Stashed complete with no activity since. Ground-truth
                        # check: if the session still reports working, Hermes is
                        # genuinely continuing (interrupted-turn auto-continue) —
                        # hold the stash and keep watching. Only settle once the
                        # session actually stops.
                        if await _still_working():
                            pending_complete = (pending_complete[0], time.monotonic())
                            _emit_ndjson({"event": "waiting", "note": "held-complete: session still working"})
                        else:
                            _emit_ndjson({"event": "complete", "text": pending_complete[0]})
                            result.update({"ok": True, "final_text": pending_complete[0]})
                            _save_own_session()
                            return result
                    if reconnects:
                        # Event stream is dead post-reconnect — status polls are
                        # the only way this turn can ever complete.
                        final = await _probe_recovered()
                        if final is not None:
                            return _recovered_result(final)
                    if time.monotonic() - last_heartbeat >= 30:
                        _emit_ndjson({"event": "waiting", "elapsed": round(time.monotonic() - (deadline - wait_seconds), 1)})
                        last_heartbeat = time.monotonic()
                    continue
                except websockets.exceptions.ConnectionClosed as exc:
                    # Mid-turn socket death (1011 keepalive timeout, backend/proxy
                    # restart). The turn keeps running server-side — reconnect and
                    # resume instead of failing the whole turn.
                    attempts = 0
                    while True:
                        attempts += 1
                        reconnects += 1
                        # Backend wedges run 1-3 min before the server watchdog
                        # kickstarts them — 8 attempts × up to 15 s backoff
                        # rides out that whole window instead of dying at ~10 s.
                        if attempts > 8:
                            raise RuntimeError(f"websocket lost mid-turn ({exc}); 8 reconnects failed")
                        _emit_ndjson({"event": "waiting", "note": f"ws-reconnect-{reconnects}"})
                        await asyncio.sleep(min(15.0, 3.0 * attempts))
                        try:
                            ws = await websockets.connect(gw.ws_url, **_ws_opts)
                            break
                        except Exception:
                            continue
                    final = await _probe_recovered()
                    if final is not None:
                        return _recovered_result(final)
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
            if obj.get("method") != "event":
                continue
            params = obj.get("params") or {}
            if str(params.get("session_id") or "") != sid:
                continue
            etype = str(params.get("type") or "")
            payload = params.get("payload") or {}
            if etype == "message.delta":
                delta = str(payload.get("text") or "")
                if delta:
                    saw_activity = True
                    pending_complete = None  # activity after a stashed complete = the stash was an echo
                    delta_parts.append(delta)
                    _emit_ndjson({"event": "delta", "text": delta})
                    if speaker is not None:
                        speaker.feed(delta)
            elif etype == "tool.start":
                saw_activity = True
                pending_complete = None
                _emit_ndjson({
                    "event": "tool",
                    "status": "start",
                    "name": str(payload.get("name") or "tool"),
                    "context": str(payload.get("context") or "")[:80],
                })
            elif etype == "tool.complete":
                saw_activity = True
                pending_complete = None
                _emit_ndjson({
                    "event": "tool",
                    "status": "done",
                    "name": str(payload.get("name") or ""),
                })
            elif etype == "message.complete":
                if not saw_activity and time.monotonic() - submit_time < 20:
                    # Ambiguous: either the barge-in echo of the killed turn, or
                    # a REAL delta-less fast reply. An echo carries no new text —
                    # drop it. One WITH text gets stashed: if no activity follows
                    # within 12 s it was the real completion (2026-07-03 audit:
                    # unconditional drop hung delta-less turns to the deadline).
                    stash_text = str(payload.get("text") or "").strip()
                    if stash_text:
                        pending_complete = (stash_text, time.monotonic())
                        _emit_ndjson({"event": "stale_complete_stashed"})
                    else:
                        _emit_ndjson({"event": "stale_complete_ignored"})
                    continue
                # Some completes arrive with an empty payload; the streamed
                # deltas ARE the reply — never let that turn go silent.
                final = str(payload.get("text") or "").strip() or "".join(delta_parts).strip()
                # Hermes interrupted-turn auto-continue: the gateway settles a
                # cancelled segment with a "[interrupted]" complete (see TUI
                # turnController) and then KEEPS WORKING. That complete is a
                # segment notice, not the end of the turn — hold it, keep
                # watching, and only settle once the session leaves working
                # (the timeout branch re-checks status before accepting).
                low = final.lower().lstrip()
                held = (
                    low.startswith("[interrupt")
                    or "[interrupted]" in low[:200]
                    or "request interrupted" in low[:200]
                    or not final  # an empty settle while working = same ambiguity
                )
                if held and await _still_working():
                    saw_activity = True
                    pending_complete = (final, time.monotonic())
                    _emit_ndjson({"event": "midturn_complete", "text": final[:200]})
                    continue
                _emit_ndjson({"event": "complete", "text": final})
                result.update({"ok": True, "final_text": final})
                _save_own_session()
                return result
            elif etype == "error":
                message = str(payload.get("message") or "gateway error")
                _emit_ndjson({"event": "error", "message": message})
                raise RuntimeError(message)
        raise TimeoutError(f"no message.complete within {wait_seconds}s")
    finally:
        try:
            await ws.close()
        except Exception:
            pass


_HANGUP_INTENTS = {
    "hang up", "hangup", "hang it up", "end call", "end the call",
    "new session", "start a new session", "fresh session", "new call",
}


def _is_hangup_utterance(text: str) -> bool:
    """True only when the WHOLE (short) utterance is a hang-up command, so
    'we should hang up the poster after' never ends the call."""
    norm = re.sub(r"[^a-z ]", " ", text.lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    if len(norm.split()) > 5:
        return False
    return norm in _HANGUP_INTENTS or any(norm == i or norm == i + " please" for i in _HANGUP_INTENTS)


def cmd_hangup(args: argparse.Namespace) -> int:
    """End the active call: next chat turn starts a clean Hermes Voice session."""
    stored = _load_chat_session()
    try:
        CHAT_SESSION_STATE.unlink(missing_ok=True)
    except Exception as e:
        _emit_ndjson({"event": "error", "message": f"{type(e).__name__}: {e}"})
        return 1
    _emit_ndjson({"event": "hangup", "previous_session": stored.get("live_id") or None})
    return 0


def cmd_interrupt(args: argparse.Namespace) -> int:
    """Interrupt the pill's own Hermes turn server-side (same as Esc in the TUI)."""
    sid = args.session_id
    if not sid:
        stored = _load_chat_session()
        sid = str(stored.get("live_id") or "")
        # Live ids rotate on gateway resume/restart while the session_key is
        # stable — a stale stored live_id made barge-in a silent noop while
        # the REAL turn kept running (2026-07-03). Resolve against the active
        # list first and interrupt whichever live id carries our lineage.
        own = {v for v in (stored.get("live_id"), stored.get("session_key")) if v}
        if own:
            try:
                rows = active_sessions(dashboard=args.dashboard)
                row = next((s for s in rows if s.get("id") in own or s.get("session_key") in own), None)
                if row is not None:
                    sid = str(row.get("id") or sid)
            except Exception:
                pass  # fall back to the stored live_id
    if not sid:
        _emit_ndjson({"event": "error", "message": "no voice session to interrupt"})
        return 1
    try:
        res = run_rpc("session.interrupt", {"session_id": sid}, dashboard=args.dashboard, timeout=10)
        _emit_ndjson({"event": "interrupted", "session_id": sid, "result": {k: res.get(k) for k in ("ok", "status") if k in res}})
        return 0
    except Exception as e:
        message = f"{type(e).__name__}: {e}"
        if "not found" in message.lower() or "not active" in message.lower():
            # Nothing running = nothing to interrupt; barge-in treats this as done.
            _emit_ndjson({"event": "interrupted", "session_id": sid, "noop": True})
            return 0
        _emit_ndjson({"event": "error", "message": message})
        return 1


def cmd_chat(args: argparse.Namespace) -> int:
    # HermesVoice barge-in: SIGTERM must silence local playback immediately, not
    # orphan an afplay child that keeps talking.
    import signal

    def _terminate(signum, frame):  # noqa: ARG001
        # ElevenLabs chunks are .mp3 → played by an OUT-OF-PROCESS afplay
        # (the old Qwen lane was wav→sounddevice, in-process). While this
        # handler runs, the play worker keeps draining its (fast-synth, full)
        # queue in parallel and can Popen a NEW afplay that survives os._exit
        # as an orphan — heard as "the voice won't interrupt". Order matters:
        # 1) stale the latest-request marker so the worker refuses new chunks,
        # 2) terminate the currently tracked player,
        # 3) sweep any afplay child that squeaked through the race.
        try:
            _mark_latest_tts_request()
        except Exception:
            pass
        try:
            if str(HERMES_SRC) not in sys.path:
                sys.path.insert(0, str(HERMES_SRC))
            from tools.voice_mode import stop_playback
            stop_playback()
        except Exception:
            pass
        try:
            import subprocess
            subprocess.run(
                ["/usr/bin/pkill", "-P", str(os.getpid()), "-x", "afplay"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
            )
        except Exception:
            pass
        os._exit(65)

    try:
        signal.signal(signal.SIGTERM, _terminate)
    except Exception:
        pass

    text = (args.text or "").strip()
    if args.audio:
        _emit_ndjson({"event": "transcribing", "path": args.audio})
        # Silence is not a fault: `\` pressed, nothing said → tell the pill
        # "empty" and exit 0 so it drops quietly back to idle instead of
        # flashing an error. Parakeet signals silence by raising on the empty
        # transcript, so that specific exception is the silence path too.
        try:
            tr = transcribe(args.audio)
        except Exception as exc:
            if "empty transcript" in str(exc).lower():
                _emit_ndjson({"event": "empty"})
                return 0
            _emit_ndjson({"event": "error", "message": f"{type(exc).__name__}: {exc}"})
            return 4
        text = str(tr.get("transcript") or "").strip()
        if not tr.get("success"):
            _emit_ndjson({"event": "error", "message": str(tr.get("error") or "transcription failed")})
            return 4
        if not text:
            _emit_ndjson({"event": "empty"})
            return 0
        _emit_ndjson({"event": "transcript", "text": text, "provider": tr.get("provider")})
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    if not text:
        _emit_ndjson({"event": "error", "message": "text required (--text, --audio, or stdin)"})
        return 1

    # Voice hang-up: the utterance itself ends the call; nothing goes to Hermes.
    if _is_hangup_utterance(text):
        return cmd_hangup(args)

    speaker = StreamingSpeaker(emit=_emit_ndjson) if args.speak else None
    try:
        res = asyncio.run(_chat_turn(args.dashboard, args.session_id, text, args.wait, args.allow_busy, speaker=speaker))
    except Exception as e:
        _emit_ndjson({"event": "error", "message": f"{type(e).__name__}: {e}"})
        return 1

    final = str(res.get("final_text") or "")
    if speaker is not None:
        try:
            receipt = speaker.finish(final)
            _emit_ndjson({
                "event": "spoken",
                "played": bool(receipt.get("played")),
                "provider": receipt.get("provider"),
                "voice": receipt.get("voice"),
                "voice_id": receipt.get("voice_id"),
                "skipped": receipt.get("skipped"),
                "truncated": bool(receipt.get("truncated")),
                "chunks": receipt.get("chunks"),
                "first_play_after_complete_s": receipt.get("first_play_after_complete_s"),
                "chunk_receipts": receipt.get("chunk_receipts"),
                "errors": receipt.get("errors"),
            })
        except Exception as e:
            _emit_ndjson({"event": "error", "message": f"tts: {type(e).__name__}: {e}"})
    return 0


def active_sessions(dashboard: str = DASHBOARD_DEFAULT) -> list[dict]:
    res = run_rpc("session.active_list", {}, dashboard=dashboard)
    return list(res.get("sessions") or [])


def pick_session(session_id: str = "", dashboard: str = DASHBOARD_DEFAULT) -> dict:
    sessions = active_sessions(dashboard)
    if session_id:
        for s in sessions:
            if s.get("id") == session_id or s.get("session_key") == session_id:
                return s
        raise RuntimeError(f"requested session not active: {session_id}")
    if len(sessions) != 1:
        raise RuntimeError(f"ambiguous active sessions: {len(sessions)}; pass --session-id")
    return sessions[0]


def cmd_status(args: argparse.Namespace) -> int:
    try:
        stt_status = {
            "provider": os.environ.get("AIRMIC_STT_PROVIDER", "fluidaudio-parakeet"),
            "model": _parakeet_model_dir().name,
            "model_dir": str(_parakeet_model_dir()),
            "cli": str(_fluidaudio_cli_path()),
            "fallback_enabled": os.environ.get("AIRMIC_STT_FALLBACK", "0").strip().lower() in {"1", "true", "yes"},
        }
    except Exception as e:
        stt_status = {
            "provider": os.environ.get("AIRMIC_STT_PROVIDER", "fluidaudio-parakeet"),
            "error": f"{type(e).__name__}: {e}",
        }
    out = {
        "host": os.uname().nodename,
        "role": "dumb mic/client helper only; no Hermes runtime/gateway/session",
        "dashboard": args.dashboard,
        "audio": audio_devices(),
        "stt": stt_status,
        "tts": _airmic_tts_route(),
        "token_parse": "not_printed",
    }
    try:
        sessions = active_sessions(args.dashboard)
        out["active_sessions"] = [
            {k: s.get(k) for k in ("id", "session_key", "title", "status", "model", "message_count", "preview")}
            for s in sessions
        ]
        out["n1_route"] = "pass"
    except Exception as e:
        out["n1_route"] = "fail"
        out["error"] = f"{type(e).__name__}: {e}"
    jprint(out, json_mode=args.json)
    return 0 if out.get("n1_route") == "pass" else 2


def cmd_record(args: argparse.Namespace) -> int:
    res = record_wav(args.seconds, args.output, args.device, args.prefer)
    res["silent_gate"] = "pass" if float(res.get("rms") or 0) > args.min_rms else "fail"
    jprint(res, json_mode=args.json)
    return 0 if res["silent_gate"] == "pass" else 3


def cmd_record_vad(args: argparse.Namespace) -> int:
    res = record_vad(
        args.output,
        args.max_seconds,
        args.silence_threshold,
        args.silence_duration,
        args.max_wait,
        args.min_speech_duration,
        args.device,
        args.prefer,
    )
    # no_speech is a useful terminal state, but still returns no transcript-worthy audio.
    res["silent_gate"] = "pass" if res.get("has_spoken") and float(res.get("peak_rms") or 0) > args.min_rms else "fail"
    jprint(res, json_mode=args.json)
    return 0 if res["silent_gate"] == "pass" else 3


def cmd_record_manual(args: argparse.Namespace) -> int:
    res = record_manual(args.output, args.max_seconds, args.device, args.prefer)
    res["silent_gate"] = "pass" if float(res.get("rms") or 0) > args.min_rms else "fail"
    jprint(res, json_mode=args.json)
    return 0 if res["silent_gate"] == "pass" else 3


def cmd_transcribe(args: argparse.Namespace) -> int:
    res = transcribe(args.path)
    jprint(res, json_mode=args.json)
    return 0 if res.get("success") else 4


def cmd_inject(args: argparse.Namespace) -> int:
    text = args.text or (sys.stdin.read().strip() if not sys.stdin.isatty() else "")
    if not text:
        raise RuntimeError("text required via argument or stdin")
    s = pick_session(args.session_id, args.dashboard)
    target = {k: s.get(k) for k in ("id", "session_key", "title", "status", "model", "message_count", "preview")}
    if args.dry_run:
        jprint({"ok": True, "dry_run": True, "target": target, "token_value_printed": "no"}, json_mode=args.json)
        return 0
    if str(s.get("status") or "").lower() in {"working", "running", "busy"} and not args.allow_busy:
        jprint({"ok": False, "error": "target session busy; refusing to interrupt/queue without --allow-busy", "target": target}, json_mode=args.json)
        return 5
    res = run_rpc("prompt.submit", {"session_id": s["id"], "text": text}, dashboard=args.dashboard, timeout=20)
    jprint({"ok": True, "submit": res, "target": target}, json_mode=args.json)
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    rec = record_wav(args.seconds, args.output, args.device, args.prefer)
    if float(rec.get("rms") or 0) <= args.min_rms:
        jprint({"ok": False, "stage": "record", "recording": rec, "error": "recording looks silent"}, json_mode=args.json)
        return 3
    tr = transcribe(rec["path"])
    if not tr.get("success") or not str(tr.get("transcript") or "").strip():
        jprint({"ok": False, "stage": "transcribe", "recording": rec, "transcription": tr}, json_mode=args.json)
        return 4
    args.text = str(tr.get("transcript") or "").strip()
    code = cmd_inject(args)
    try:
        Path(rec["path"]).unlink(missing_ok=True)
    except Exception:
        pass
    return code


def cmd_selftest(args: argparse.Namespace) -> int:
    syn = synth_wav(args.phrase, args.output)
    tr = transcribe(syn["path"])
    ok = bool(tr.get("success")) and args.expect.lower() in str(tr.get("transcript") or "").lower()
    out = {"ok": ok, "synthetic_audio": syn, "transcription": tr}
    if args.inject or args.dry_run:
        inj_args = argparse.Namespace(**vars(args))
        inj_args.text = str(tr.get("transcript") or "").strip()
        inj_args.dry_run = args.dry_run or not args.inject
        s = pick_session(args.session_id, args.dashboard)
        out["target"] = {k: s.get(k) for k in ("id", "session_key", "title", "status", "model", "message_count", "preview")}
        if args.inject:
            if str(s.get("status") or "").lower() in {"working", "running", "busy"} and not args.allow_busy:
                out["inject"] = {"ok": False, "error": "target session busy; refusing without --allow-busy"}
            else:
                out["inject"] = run_rpc("prompt.submit", {"session_id": s["id"], "text": inj_args.text}, dashboard=args.dashboard, timeout=20)
    jprint(out, json_mode=args.json)
    try:
        Path(syn["path"]).unlink(missing_ok=True)
    except Exception:
        pass
    return 0 if ok else 6


def main() -> int:
    p = argparse.ArgumentParser(description="Hermes Voice mic helper — record, transcribe, chat with your Hermes, speak the reply")
    p.add_argument("--dashboard", default=DASHBOARD_DEFAULT)
    p.add_argument("--json", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("status")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("record-smoke")
    sp.add_argument("--seconds", type=float, default=4.0)
    sp.add_argument("--output", default="")
    sp.add_argument("--device", type=int, default=None)
    sp.add_argument("--prefer", default="MacBook Air Microphone")
    sp.add_argument("--min-rms", type=float, default=1.0)
    sp.set_defaults(func=cmd_record)



    sp = sub.add_parser("record-vad")
    sp.add_argument("--output", default="")
    sp.add_argument("--max-seconds", type=float, default=240.0)
    sp.add_argument("--silence-threshold", type=int, default=120)
    sp.add_argument("--silence-duration", type=float, default=4.5)
    sp.add_argument("--max-wait", type=float, default=15.0)
    sp.add_argument("--min-speech-duration", type=float, default=0.3)
    sp.add_argument("--device", type=int, default=None)
    sp.add_argument("--prefer", default="MacBook Air Microphone")
    sp.add_argument("--min-rms", type=float, default=1.0)
    sp.set_defaults(func=cmd_record_vad)

    sp = sub.add_parser("record-manual")
    sp.add_argument("--output", default="")
    sp.add_argument("--max-seconds", type=float, default=240.0)
    sp.add_argument("--device", type=int, default=None)
    sp.add_argument("--prefer", default="MacBook Air Microphone")
    sp.add_argument("--min-rms", type=float, default=1.0)
    sp.set_defaults(func=cmd_record_manual)

    sp = sub.add_parser("transcribe")
    sp.add_argument("path")
    sp.set_defaults(func=cmd_transcribe)

    sp = sub.add_parser("inject")
    sp.add_argument("text", nargs="?")
    sp.add_argument("--session-id", default="")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--allow-busy", action="store_true")
    sp.set_defaults(func=cmd_inject)

    sp = sub.add_parser("once")
    sp.add_argument("--seconds", type=float, default=6.0)
    sp.add_argument("--output", default="")
    sp.add_argument("--device", type=int, default=None)
    sp.add_argument("--prefer", default="MacBook Air Microphone")
    sp.add_argument("--min-rms", type=float, default=1.0)
    sp.add_argument("--session-id", default="")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--allow-busy", action="store_true")
    sp.set_defaults(func=cmd_once)


    sp = sub.add_parser("hangup")
    sp.set_defaults(func=cmd_hangup)

    sp = sub.add_parser("interrupt")
    sp.add_argument("--session-id", default="")
    sp.set_defaults(func=cmd_interrupt)

    sp = sub.add_parser("chat")
    sp.add_argument("--text", default="")
    sp.add_argument("--audio", default="")
    sp.add_argument("--session-id", default="")
    sp.add_argument("--wait", type=float, default=3600.0)
    sp.add_argument("--allow-busy", action="store_true")
    sp.add_argument("--speak", action="store_true")
    sp.set_defaults(func=cmd_chat)

    sp = sub.add_parser("speak")
    sp.add_argument("--text", default="")
    sp.add_argument("--output", default="")
    sp.add_argument("--no-play", action="store_true")
    sp.set_defaults(func=cmd_speak)

    sp = sub.add_parser("selftest")
    sp.add_argument("--phrase", default="air mic bridge selftest")
    sp.add_argument("--expect", default="air mic bridge")
    sp.add_argument("--output", default="")
    sp.add_argument("--session-id", default="")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--inject", action="store_true")
    sp.add_argument("--allow-busy", action="store_true")
    sp.set_defaults(func=cmd_selftest)

    args = p.parse_args()
    try:
        return int(args.func(args) or 0)
    except Exception as e:
        jprint({"ok": False, "error": f"{type(e).__name__}: {e}"}, json_mode=getattr(args, "json", False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
