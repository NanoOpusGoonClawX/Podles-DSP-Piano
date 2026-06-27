#!/usr/bin/env python3
"""
synth_piano.py - Self-sourced realistic piano audio for the virtual venue simulation.

No external audio download required. Generates piano-timbre notes via additive
synthesis: multiple harmonics, per-partial decay, slight inharmonicity (real piano
strings are inharmonic), and an ADSR amplitude envelope. Output is 16 kHz int16
mono WAV - the exact format the ESP32 firmware would stream.

Usage:
    python tools/synth_piano.py --song fur_elise --out fixtures/venue_piano.wav
    python tools/synth_piano.py --song cmaj --out fixtures/cmaj_synth.wav
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

SAMPLE_RATE = 16000


def midi_to_hz(midi: int) -> float:
    return 440.0 * (2.0 ** ((midi - 69) / 12.0))


def piano_note(midi: int, dur_s: float, velocity: float = 0.8) -> np.ndarray:
    """One piano-timbre note via additive synthesis with inharmonic partials + ADSR."""
    n = int(dur_s * SAMPLE_RATE)
    t = np.arange(n) / SAMPLE_RATE
    f0 = midi_to_hz(midi)

    # Inharmonicity coefficient (real piano strings: higher partials sharp)
    B = 0.0004
    sig = np.zeros(n, dtype=np.float64)

    # Fundamental-dominant spectrum: the f0 carries the bulk of the energy so a
    # pitch model locks onto the correct octave instead of a harmonic. Upper
    # partials are present (for piano timbre) but steeply attenuated.
    n_partials = 5
    partial_gain = {1: 1.0, 2: 0.18, 3: 0.10, 4: 0.05, 5: 0.03}
    for k in range(1, n_partials + 1):
        # inharmonic partial frequency
        fk = f0 * k * np.sqrt(1.0 + B * k * k)
        if fk >= SAMPLE_RATE / 2:  # below Nyquist only
            break
        amp = partial_gain.get(k, 0.02)
        # higher partials decay faster (energy loss)
        partial_decay = np.exp(-t * (2.0 + 0.8 * k))
        sig += amp * partial_decay * np.sin(2.0 * np.pi * fk * t)

    # ADSR envelope: sharp attack (hammer strike), exponential decay, short release
    env = np.ones(n)
    attack = int(0.005 * SAMPLE_RATE)
    release = int(0.04 * SAMPLE_RATE)
    if attack > 0:
        env[:attack] = np.linspace(0.0, 1.0, attack)
    body_decay = np.exp(-t * 1.8)
    env *= body_decay
    if release > 0 and n > release:
        env[-release:] *= np.linspace(1.0, 0.0, release)

    note = sig * env * velocity
    return note.astype(np.float64)


# Fur Elise opening (right hand), MIDI note numbers and durations in beats.
# E5 D#5 E5 D#5 E5 B4 D5 C5 A4 ...
FUR_ELISE = [
    (76, 0.4), (75, 0.4), (76, 0.4), (75, 0.4), (76, 0.4), (71, 0.4),
    (74, 0.4), (72, 0.4), (69, 0.8),
    (57, 0.4), (60, 0.4), (64, 0.4), (69, 0.8),
    (71, 0.4), (52, 0.4), (56, 0.4), (64, 0.4), (72, 0.4), (71, 0.8),
]

# C major chord arpeggio + block chord (simple, unambiguous for verification)
CMAJ = [
    (60, 0.5), (64, 0.5), (67, 0.5),   # C E G arpeggio
    (60, 1.0),                          # C again
]

# Ode to Joy page-1 right-hand melody — MUST match the Node mock score
# (E4 E4 F4 G4 G4 F4 E4 D4 C4 C4 D4 E4 E4 D4 D4) so the score tracker advances
# and a PAGE_TURN fires. MIDI: E4=64 F4=65 G4=67 D4=62 C4=60.
ODE_TO_JOY = [
    (64, 0.5), (64, 0.5), (65, 0.5), (67, 0.5),
    (67, 0.5), (65, 0.5), (64, 0.5), (62, 0.5),
    (60, 0.5), (60, 0.5), (62, 0.5), (64, 0.5),
    (64, 0.75), (62, 0.25), (62, 1.0),
]

_SONGS = {"fur_elise": FUR_ELISE, "cmaj": CMAJ, "ode_to_joy": ODE_TO_JOY}


def build_song(name: str) -> tuple[np.ndarray, list[tuple[int, float, float]]]:
    """Returns (audio, events) where events = [(midi, onset_s, dur_s), ...]."""
    seq = _SONGS.get(name, FUR_ELISE)
    beat_s = 0.5  # 120 BPM
    gap_s = 0.06  # small gap between notes so onsets are distinct
    pieces: list[np.ndarray] = []
    events: list[tuple[int, float, float]] = []
    cursor = 0.0
    for midi, beats in seq:
        dur = beats * beat_s
        note = piano_note(midi, dur)
        events.append((midi, cursor, dur))
        pieces.append(note)
        # silence gap
        pieces.append(np.zeros(int(gap_s * SAMPLE_RATE)))
        cursor += dur + gap_s
    audio = np.concatenate(pieces)
    # normalize to -3 dBFS
    peak = float(np.max(np.abs(audio))) or 1.0
    audio = audio / peak * 0.707
    return audio.astype(np.float32), events


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Synthesize realistic piano audio")
    _ = ap.add_argument("--song", choices=("fur_elise", "cmaj", "ode_to_joy"), default="fur_elise")
    _ = ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    audio, events = build_song(str(args.song))
    out = Path(str(args.out))
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out), audio, SAMPLE_RATE, subtype="PCM_16")

    dur = len(audio) / SAMPLE_RATE
    print(f"[synth] wrote {out} | {dur:.2f}s | {len(events)} notes | {SAMPLE_RATE} Hz int16 mono")
    print("[synth] note timeline (midi @ onset_s):")
    for midi, onset, d in events:
        print(f"        midi={midi:3d}  onset={onset:5.2f}s  dur={d:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
