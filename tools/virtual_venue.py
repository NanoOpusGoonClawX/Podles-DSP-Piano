#!/usr/bin/env python3
"""
virtual_venue.py - Full hardware-free simulation of the Podles DSP Piano pipeline.

Replaces the ESP32 + piezo + physical piano with synthesized piano audio, then
drives the REAL software stack exactly as a venue test would:

    [synth piano WAV]  (stands in for piezo+ESP32 capture)
          |
          v  audio_stream_v1 frames over websocket  (identical to ESP32 output)
    [Python server :8000]  AudioIngest -> BasicPitch (ONNX/CPU) -> note_events
          |
          v  3-byte MIDI [0x90, note, vel]  (NodeJsBridge)
    [Node.js server :8080]  FullScoreProgressTracker -> PAGE_TURN / SCORE_COMPLETED
          |
          v  broadcast JSON
    [this script's ws client]  records every event the mobile app would see

It launches both servers, loads a mock score into the Node tracker, opens a ws
listener, streams the synth WAV, and prints every note/page event observed.

Usage:
    python tools/virtual_venue.py --wav fixtures/venue_piano.wav
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY_PORT = 8000
NODE_PORT = 8080


def _post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _upload_dummy_pdf(url: str, filename: str) -> dict:
    """POST a tiny multipart 'PDF' so the Node mock score keys off the filename.
    The Node /transcribe endpoint fabricates the score from the filename (it does
    not parse PDF bytes), so a minimal valid-enough body is sufficient."""
    boundary = "----podlesvenue123"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
        "%PDF-1.4 podles virtual venue dummy\r\n"
        f"\r\n--{boundary}--\r\n"
    ).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _wait_http(url: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False


async def _wait_ws(uri: str, timeout: float) -> bool:
    import websockets
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            async with websockets.connect(uri):
                return True
        except Exception:
            await asyncio.sleep(0.5)
    return False


async def _listen_node_events(stop: asyncio.Event, sink: list) -> None:
    """Connect to the Node.js ws and record every broadcast (NOTE_PLAYED / PAGE_TURN / ...)."""
    import websockets
    uri = f"ws://localhost:{NODE_PORT}"
    try:
        async with websockets.connect(uri) as ws:
            while not stop.is_set():
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                if isinstance(msg, bytes):
                    if len(msg) == 3:
                        sink.append({"kind": "binary_midi", "status": msg[0], "note": msg[1], "vel": msg[2]})
                    continue
                try:
                    data = json.loads(msg)
                    sink.append({"kind": "json", **data})
                    print(f"  [node-event] {data}")
                except Exception:
                    pass
    except Exception as exc:
        print(f"  [listener] closed: {exc}")


async def run(wav: Path) -> int:
    procs: list[subprocess.Popen] = []
    print("=" * 64)
    print("VIRTUAL VENUE - hardware-free end-to-end simulation")
    print("=" * 64)

    # 1. Node.js score-tracker server (:8080)
    print(f"[1/6] starting Node.js score server on :{NODE_PORT} ...")
    node_env = {"PORT": str(NODE_PORT)}
    import os
    env = {**os.environ, **node_env}
    procs.append(subprocess.Popen(["node", str(ROOT / "server" / "index.js")], cwd=str(ROOT / "server"), env=env))
    if not _wait_http(f"http://localhost:{NODE_PORT}/", 20):
        # Node server has no GET / health; probe ws instead
        if not await _wait_ws(f"ws://localhost:{NODE_PORT}", 10):
            print("  ERROR: Node server did not come up")
            for p in procs: p.terminate()
            return 1
    print("  Node server up.")

    # 2. Python transcription server (:8000) with ONNX BasicPitch + bridge to Node
    print(f"[2/6] starting Python server on :{PY_PORT} (BasicPitch ONNX/CPU) -> bridge :{NODE_PORT} ...")
    pyenv = {**os.environ, "TF_CPP_MIN_LOG_LEVEL": "3", "TF_ENABLE_ONEDNN_OPTS": "0"}
    procs.append(subprocess.Popen(
        [sys.executable, str(ROOT / "server" / "app.py"),
         "--port", str(PY_PORT), "--transcriber", "basic_pitch",
         "--nodejs-uri", f"ws://localhost:{NODE_PORT}"],
        cwd=str(ROOT), env=pyenv))
    if not _wait_http(f"http://localhost:{PY_PORT}/health", 90):
        print("  ERROR: Python server did not come up (model load can take ~30s)")
        for p in procs: p.terminate()
        return 1
    print("  Python server up.")

    # 3. Load a mock score into the Node tracker (simulates a parsed sheet PDF)
    print("[3/6] uploading matching score PDF + loading tracker ...")
    try:
        # Upload a PDF whose name triggers the Ode-to-Joy mock score, so the
        # streamed Ode audio matches the loaded score and PAGE_TURN can fire.
        _upload_dummy_pdf(f"http://localhost:{NODE_PORT}/upload-pdf", "ode_to_joy.pdf")
    except Exception as exc:
        print(f"  WARN: upload-pdf failed ({exc})")
    try:
        score = _post_json(f"http://localhost:{NODE_PORT}/transcribe", {"pageNumber": 1})
        print(f"  score loaded: scoreId={score.get('scoreId')} notes={len(score.get('notes', []))}")
    except Exception as exc:
        print(f"  WARN: /transcribe failed ({exc}); tracker may be empty, notes still flow")

    # 4. Open a listener on the Node ws (this is what the mobile app sees)
    print("[4/6] opening event listener on Node ws ...")
    stop = asyncio.Event()
    sink: list = []
    listener = asyncio.create_task(_listen_node_events(stop, sink))
    await asyncio.sleep(1.0)

    # 5. Stream the synthesized piano to the Python server (stands in for the ESP32)
    print(f"[5/6] streaming synth piano -> Python /stream (realtime) : {wav.name}")
    streamer = subprocess.Popen(
        [sys.executable, str(ROOT / "tools" / "stream_synthetic.py"),
         "--server", f"ws://localhost:{PY_PORT}/stream",
         "--wav", str(wav), "--realtime"],
        cwd=str(ROOT))
    streamer.wait()
    print("  stream complete; waiting for final transcription flush ...")
    await asyncio.sleep(4.0)

    # 6. Stop + report
    stop.set()
    await listener
    print("[6/6] tearing down servers ...")
    for p in procs:
        p.terminate()

    notes = [e for e in sink if e.get("kind") == "json" and e.get("type") == "NOTE_PLAYED"]
    midi_bin = [e for e in sink if e.get("kind") == "binary_midi"]
    page_turns = [e for e in sink if e.get("kind") == "json" and e.get("type") == "PAGE_TURN"]
    completed = [e for e in sink if e.get("kind") == "json" and e.get("type") == "SCORE_COMPLETED"]

    print("\n" + "=" * 64)
    print("VIRTUAL VENUE RESULT")
    print("=" * 64)
    print(f"  events observed on Node ws : {len(sink)}")
    print(f"  NOTE_PLAYED json events    : {len(notes)}")
    print(f"  binary MIDI packets        : {len(midi_bin)}")
    print(f"  PAGE_TURN events           : {len(page_turns)}")
    print(f"  SCORE_COMPLETED events     : {len(completed)}")
    ok = (len(notes) + len(midi_bin)) > 0
    print(f"\n  END-TO-END NOTE FLOW: {'PASS - notes reached the score tracker' if ok else 'FAIL - no notes observed'}")

    out = ROOT / "fixtures" / "virtual_venue_result.json"
    out.write_text(json.dumps({"events": sink, "summary": {
        "note_played": len(notes), "binary_midi": len(midi_bin),
        "page_turns": len(page_turns), "completed": len(completed)}}, indent=2))
    print(f"  full event log -> {out}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Hardware-free end-to-end venue simulation")
    _ = ap.add_argument("--wav", type=Path, default=ROOT / "fixtures" / "venue_piano.wav")
    args = ap.parse_args(argv)
    wav = Path(args.wav)
    if not wav.exists():
        print(f"ERROR: {wav} not found. Run: python tools/synth_piano.py --song fur_elise --out {wav}")
        return 1
    return asyncio.run(run(wav))


if __name__ == "__main__":
    sys.exit(main())
