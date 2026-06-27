# RUN_VIRTUAL_DEMO — AI-Agent Runbook

> **Audience:** an AI coding agent (or human) with a shell. Follow top to bottom.
> **Goal:** run the entire Podles DSP Piano pipeline virtually — no ESP32, no piezo,
> no physical piano — and show a live GUI where detected piano notes light up keys.
>
> **What this proves:** server↔server comms, app↔server comms, audio→notes
> transcription, and an interactive browser GUI. All on one machine.

---

## 0. Architecture (what you are about to run)

```
[synth piano WAV]            <- stands in for piezo + ESP32 capture
      |  audio_stream_v1 frames over websocket (identical to real ESP32 output)
      v
[Python server  :8000]       AudioIngest -> BasicPitch (ONNX, CPU) -> note_events
      |  3-byte MIDI [0x90, note, vel]  (NodeJsBridge)
      v
[Node.js server :8080]       FullScoreProgressTracker -> PAGE_TURN / SCORE_COMPLETED
      |  broadcast JSON + binary MIDI
      v
[Browser GUI]                server/static/showcase.html lights up piano keys live
```

Key facts an agent must respect:
- **No CUDA.** Transcription uses **ONNX Runtime on CPU** (`CPUExecutionProvider`).
  The machine has an AMD GPU; do NOT attempt TensorFlow-GPU or CUDA paths.
- **Two servers, two ports.** Node = 8080 (score tracker + GUI events), Python =
  8000 (audio ingest + transcription + GUI static files).
- **The GUI is served by the Python server** at `/static/showcase.html` but connects
  to the **Node** server at `ws://<host>:8080` for note events.

---

## 1. Prerequisites (verify, do not assume)

```bash
# Python 3.11+ with these importable (install if missing):
python -c "import numpy, soundfile, scipy, fastapi, uvicorn, websockets, onnxruntime, basic_pitch; print('py deps OK')"
# If basic_pitch model load fails on TensorFlow, that is EXPECTED — the code
# auto-falls back to the bundled ONNX model (nmp.onnx). Do not 'fix' it.

# Node.js (for the score-tracker server):
node --version

# Install Node deps once:
cd server && npm install && cd ..
```

If `onnxruntime` is missing: `pip install onnxruntime`. It is CPU-only and correct
for this machine. Never install `onnxruntime-gpu` here.

---

## 2. Generate the piano audio (self-sourced, no download)

The synth is **fundamental-dominant** so the pitch model locks onto the correct
octave (do not add more harmonics — it causes octave/harmonic misreads).

```bash
# Ode to Joy (C major) — matches the Node mock score note-for-note:
python tools/synth_piano.py --song ode_to_joy --out fixtures/ode_to_joy.wav
# Optional alternates: --song fur_elise  | --song cmaj
```

Expected: a ~4.9s, 16 kHz, int16 mono WAV. Verify the model reads it correctly:
detected MIDI must equal `64 64 65 67 67 65 64 62 60 60 62 64 64 62 62`.

---

## 3. Option A — One-command end-to-end (headless, no browser)

Best for an AI agent that just needs to PROVE the pipeline works and capture results.

```bash
python tools/virtual_venue.py --wav fixtures/ode_to_joy.wav
```

This launches both servers, uploads a matching score, streams the audio, records
every event the GUI would see, then tears down. Success criteria printed at the end:
- `END-TO-END NOTE FLOW: PASS` and a non-zero MIDI packet count.
- Full event log saved to `fixtures/virtual_venue_result.json`.

(Known limitation: `PAGE_TURN` may be 0 — note flow is proven regardless; the
page-turn cursor logic in the Node tracker is a separate work item.)

---

## 4. Option B — Live GUI demo (what a human watches)

Run the servers **detached** so they survive your shell, then open the GUI.

### 4.1 Start Node score server (port 8080)
```bash
# Windows PowerShell:
Start-Process node -ArgumentList "index.js" -WorkingDirectory "server" -WindowStyle Hidden \
  -RedirectStandardOutput ".node_out.log" -RedirectStandardError ".node_err.log"
# macOS/Linux:
# (cd server && PORT=8080 node index.js > ../.node_out.log 2>&1 &)
```

### 4.2 Start Python server (port 8000), bridged to Node
```bash
# Windows PowerShell:
$env:TF_CPP_MIN_LOG_LEVEL="3"; $env:TF_ENABLE_ONEDNN_OPTS="0"
Start-Process python -ArgumentList "server\app.py","--port","8000","--transcriber","basic_pitch","--nodejs-uri","ws://localhost:8080" \
  -WindowStyle Hidden -RedirectStandardOutput ".py_out.log" -RedirectStandardError ".py_err.log"
# macOS/Linux:
# TF_CPP_MIN_LOG_LEVEL=3 python server/app.py --port 8000 --transcriber basic_pitch --nodejs-uri ws://localhost:8080 > .py_out.log 2>&1 &
```

### 4.3 Wait for readiness (model load ~30s) — poll, do NOT block forever
```bash
# Python is ready when /health returns status: ready
curl -s http://localhost:8000/health
# Expect: {"role":"laptop-server","status":"ready","transcriber":"basic_pitch",...}
# Node↔Python bridge is confirmed in .py_err.log: "NodeJsBridge connected to ws://localhost:8080"
```

### 4.4 Open the GUI (real browser)
```bash
# Windows:
Start-Process "http://localhost:8000/static/showcase.html"
# macOS:  open "http://localhost:8000/static/showcase.html"
# Linux:  xdg-open "http://localhost:8000/static/showcase.html"
```
GUI connects to `ws://localhost:8080`. Badge should read **connected**.

### 4.5 Load a score (so PAGE_TURN can fire) + drive notes
```bash
# Upload a PDF whose name contains "ode" -> Node loads the matching mock score:
curl -s -F "file=@OdetoJoy_13-14_Piano_vocal.pdf;filename=ode_to_joy.pdf" http://localhost:8080/upload-pdf
curl -s -X POST -H "Content-Type: application/json" -d '{"pageNumber":1}' http://localhost:8080/transcribe

# INTERACTION TEST — inject a single note, GUI lights the key instantly:
curl -s -X POST -H "Content-Type: application/json" -d '{"note":"E4","velocity":110}' http://localhost:8080/sim-note

# FULL MELODY — stream the synth piano through the whole chain:
python tools/stream_synthetic.py --server ws://localhost:8000/stream --wav fixtures/ode_to_joy.wav --realtime
```

Watch the browser: keys light **crimson** as each note is detected, "now playing"
updates, and the counters increment.

---

## 5. Teardown

```bash
# Windows PowerShell:
Get-NetTCPConnection -LocalPort 8000,8080 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
# macOS/Linux:
# lsof -ti :8000 -ti :8080 | xargs kill
```

---

## 6. Troubleshooting (deterministic answers)

| Symptom | Cause | Fix |
|---|---|---|
| `Failed to resolve component 'esp_websocket_client'` | only relevant to ESP firmware build, not this demo | ignore for virtual demo |
| basic_pitch TensorFlow load error | TF version mismatch | EXPECTED — code falls back to ONNX automatically |
| `onnxruntime-gpu` / CUDA errors | wrong package | uninstall gpu variant, `pip install onnxruntime` (CPU) |
| GUI badge says "disconnected" | Node server not up on 8080 | check `.node_err.log`, restart step 4.1 |
| `/health` never returns ready | model still loading or crashed | wait 30s; if still failing read `.py_err.log` |
| notes detected an octave too high (e.g. 93 instead of 64) | synth too harmonic | already fixed; regenerate WAV with current `synth_piano.py` |
| PAGE_TURN stays 0 | Node tracker cursor logic | known open item; note flow still passes |

---

## 7. File map (what each piece is)

- `tools/synth_piano.py` — generates fundamental-dominant piano WAVs (no download).
- `tools/stream_synthetic.py` — virtual ESP32: streams a WAV as `audio_stream_v1` frames.
- `tools/virtual_venue.py` — one-command orchestrator (servers + stream + capture).
- `server/app.py` — Python transcription server (ONNX BasicPitch, `/stream` `/notes` `/health`, NodeJsBridge).
- `server/index.js` — Node score-tracker server (`/upload-pdf` `/transcribe` `/sim-note`, ws :8080).
- `server/static/showcase.html` — the live GUI (cream + crimson theme).
- `final_bar_matching/` — the JS score-following engine the Node server uses.
- `fixtures/ode_to_joy.wav` — pre-generated demo audio.
