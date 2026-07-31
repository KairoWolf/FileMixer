#!/usr/bin/env python3
"""FileMixer LIVE - the live reactor.

Plays a video and chucks random files into it IN REAL TIME: raw bytes of
fuel files splatter into the decoded frames, glitch bands rip across,
and a smear buffer melts everything datamosh-style - all live, all in
memory. Nothing is ever written to or read destructively from disk: the
video is decoded read-only, fuel files are read read-only, and the
corruption only ever exists in the pixels on screen.

Snapshot button saves the current frame as a PNG if you catch something
beautiful. That's the only file this program ever creates.
"""

import os
import queue
import random
import struct
import subprocess
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk

import numpy as np

import filemixer as fm

BG = "#101016"
PANEL = "#1e1e28"
FG = "#e8e8f0"
ACCENT = "#ff5f87"
HACK = "#4fee6f"

VIEW_W = 512          # preview width; height follows the video's aspect


class Decoder:
    """Loops a video forever through ffmpeg, yielding raw RGB frames."""

    def __init__(self, path, width):
        dims = fm._probe_dims(path)
        if not dims:
            raise ValueError("that file doesn't decode as video")
        w, h = dims
        self.w = width
        self.h = max(2, int(width * h / w)) // 2 * 2
        self.fps = 20
        self.proc = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-v", "error", "-stream_loop", "-1",
             "-i", path, "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-vf", f"scale={self.w}:{self.h},fps={self.fps}", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.frame_bytes = self.w * self.h * 3

    def read(self):
        data = self.proc.stdout.read(self.frame_bytes)
        if len(data) < self.frame_bytes:
            return None
        return np.frombuffer(data, dtype=np.uint8).reshape(self.h, self.w, 3)

    def stop(self):
        self.proc.kill()


class FuelPile:
    """Read-only grab-bag of random byte slices from a folder's files."""

    def __init__(self, folder):
        self.files = []
        for root, _, names in os.walk(folder):
            for n in names:
                p = os.path.join(root, n)
                try:
                    if os.path.getsize(p) > 4096:
                        self.files.append(p)
                except OSError:
                    pass
            break  # top level only - no deep crawling through people's stuff
        if not self.files:
            raise ValueError("no usable files in that folder")

    def grab(self, nbytes, rng):
        path = rng.choice(self.files)
        size = os.path.getsize(path)
        with open(path, "rb") as f:  # read-only, always
            f.seek(rng.randint(0, max(size - nbytes, 0)))
            data = f.read(nbytes)
        if len(data) < nbytes:
            data += data * (nbytes // max(len(data), 1) + 1)
        return os.path.basename(path), np.frombuffer(data[:nbytes], dtype=np.uint8)


class LiveReactor:
    def __init__(self, root):
        self.root = root
        root.title("FileMixer LIVE :: the live reactor")
        root.configure(bg=BG)
        root.resizable(False, False)

        self.rng = random.Random()
        self.decoder = None
        self.fuel = None
        self.smear = None          # persistent float buffer = fake datamosh
        self.photo = None
        self.audio_proc = None
        self.frames = 0
        self.running = False

        self._build_ui()

    def _build_ui(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=FG, fieldbackground=PANEL)
        s.configure("TButton", background=PANEL, foreground=FG, padding=6)
        s.map("TButton", background=[("active", "#2c2c3a")])
        s.configure("Big.TButton", font=("Sans", 12, "bold"), foreground=ACCENT)

        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=10, pady=(8, 4))
        self.vid_btn = ttk.Button(top, text="[ Choose video ]", command=self.pick_video)
        self.vid_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.fuel_btn = ttk.Button(top, text="[ Choose fuel folder ]", command=self.pick_fuel)
        self.fuel_btn.pack(side="left", expand=True, fill="x", padx=(4, 0))

        self.view = tk.Label(self.root, bg="#000000", bd=1, relief="solid")
        self.view.pack(padx=10, pady=4)
        self._blank_view()

        ctl = tk.Frame(self.root, bg=BG)
        ctl.pack(fill="x", padx=10, pady=2)
        tk.Label(ctl, text="Chaos:", bg=BG, fg=FG).pack(side="left")
        self.chaos = tk.DoubleVar(value=35)
        ttk.Scale(ctl, from_=0, to=100, variable=self.chaos,
                  length=130).pack(side="left", padx=4)
        tk.Label(ctl, text="Smear:", bg=BG, fg=FG).pack(side="left", padx=(10, 0))
        self.smear_amt = tk.DoubleVar(value=40)
        ttk.Scale(ctl, from_=0, to=95, variable=self.smear_amt,
                  length=130).pack(side="left", padx=4)
        self.audio_var = tk.BooleanVar(value=True)
        tk.Checkbutton(ctl, text="audio", variable=self.audio_var, bg=BG, fg=FG,
                       selectcolor=PANEL, activebackground=BG,
                       command=self._toggle_audio).pack(side="right")

        row = tk.Frame(self.root, bg=BG)
        row.pack(fill="x", padx=10, pady=4)
        self.chuck_btn = ttk.Button(row, text="!! CHUCK A FILE IN NOW !!",
                                    style="Big.TButton", command=self.chuck_big,
                                    state="disabled")
        self.chuck_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))
        ttk.Button(row, text="Snapshot", command=self.snapshot).pack(side="left")

        self.status = tk.StringVar(
            value="Pick a video and a fuel folder. Everything stays read-only.")
        tk.Label(self.root, textvariable=self.status, bg=BG, fg=HACK,
                 font=("Monospace", 9), anchor="w", padx=10, pady=4,
                 wraplength=520, justify="left").pack(fill="x")

    def _blank_view(self):
        img = tk.PhotoImage(width=VIEW_W, height=288)
        img.put("#000000", to=(0, 0, VIEW_W, 288))
        self.view.config(image=img)
        self.view.img = img

    # ------------------------------------------------------------- setup
    def pick_video(self):
        path = filedialog.askopenfilename(title="Choose a video to torment")
        if not path:
            return
        try:
            dec = Decoder(path, VIEW_W)
        except (ValueError, OSError) as e:
            self.status.set(f"can't use that: {e}")
            return
        if self.decoder:
            self.decoder.stop()
        self._stop_audio()
        self.video_path = path
        self.decoder = dec
        self.smear = None
        self.vid_btn.config(text=os.path.basename(path))
        self.status.set(f"reactor loaded: {os.path.basename(path)} "
                        f"({dec.w}x{dec.h} @ {dec.fps}fps, looping forever)")
        self._toggle_audio()
        if not self.running:
            self.running = True
            self._tick()
        self._maybe_arm()

    def pick_fuel(self):
        folder = filedialog.askdirectory(title="Choose a folder of fuel files")
        if not folder:
            return
        try:
            self.fuel = FuelPile(folder)
        except ValueError as e:
            self.status.set(str(e))
            return
        self.fuel_btn.config(text=f"{os.path.basename(folder)} ({len(self.fuel.files)} files)")
        self.status.set(f"fuel pile armed: {len(self.fuel.files)} files, read-only")
        self._maybe_arm()

    def _maybe_arm(self):
        if self.decoder and self.fuel:
            self.chuck_btn.config(state="normal")

    def _toggle_audio(self):
        self._stop_audio()
        if self.audio_var.get() and self.decoder:
            self.audio_proc = subprocess.Popen(
                ["ffplay", "-nodisp", "-loglevel", "quiet", "-loop", "0",
                 self.video_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _stop_audio(self):
        if self.audio_proc:
            self.audio_proc.kill()
            self.audio_proc = None

    # ------------------------------------------------------- the reactor
    def _corrupt(self, frame):
        """All the damage happens HERE, to a copy of decoded pixels."""
        f = frame.astype(np.float32)
        h, w, _ = frame.shape
        chaos = self.chaos.get() / 100
        rng = self.rng

        # persistent smear = live fake datamosh
        alpha = 1.0 - self.smear_amt.get() / 100
        if self.smear is None:
            self.smear = f.copy()
        self.smear = self.smear * (1 - alpha) + f * alpha
        out = self.smear.astype(np.uint8).copy()

        # random byte splats from the fuel pile
        if self.fuel and rng.random() < chaos:
            for _ in range(1 + int(chaos * 3)):
                rw = rng.randint(w // 8, w // 2)
                rh = rng.randint(h // 10, h // 3)
                x, y = rng.randint(0, w - rw), rng.randint(0, h - rh)
                name, raw = self.fuel.grab(rw * rh * 3, rng)
                out[y:y + rh, x:x + rw] = raw.reshape(rh, rw, 3)
                self.last_chuck = name

        # channel-shift band
        if rng.random() < chaos * 0.7:
            y = rng.randint(0, h - h // 6)
            band = slice(y, y + h // 6)
            out[band, :, 0] = np.roll(out[band, :, 0], rng.randint(4, 40), axis=1)

        # row displacement rip
        if rng.random() < chaos * 0.5:
            y = rng.randint(0, h - h // 8)
            out[y:y + h // 8] = np.roll(out[y:y + h // 8],
                                        rng.randint(-w // 3, w // 3), axis=1)

        # keep the smear buffer contaminated so damage lingers and melts
        self.smear = self.smear * 0.7 + out.astype(np.float32) * 0.3
        return out

    def chuck_big(self):
        """The button: a huge splat right now."""
        if not (self.fuel and self.smear is not None):
            return
        h, w, _ = self.smear.shape
        name, raw = self.fuel.grab(w * (h // 2) * 3, self.rng)
        splat = raw.reshape(h // 2, w, 3).astype(np.float32)
        y = self.rng.randint(0, h - h // 2)
        self.smear[y:y + h // 2] = splat
        self.status.set(f">> CHUCKED: {name} (its bytes are now pixels. "
                        "the file itself is fine.)")

    def snapshot(self):
        if self.smear is None:
            return
        os.makedirs("snapshots", exist_ok=True)
        path = os.path.join("snapshots", f"reactor_{int(time.time())}.png")
        arr = self.smear.astype(np.uint8)
        h, w, _ = arr.shape
        ppm = b"P6\n%d %d\n255\n" % (w, h) + arr.tobytes()
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "quiet",
                        "-f", "image2pipe", "-i", "pipe:0", path], input=ppm)
        self.status.set(f"snapshot saved: {path}")

    def _tick(self):
        if not self.running:
            return
        t0 = time.monotonic()
        frame = self.decoder.read() if self.decoder else None
        if frame is not None:
            out = self._corrupt(frame)
            h, w, _ = out.shape
            ppm = b"P6\n%d %d\n255\n" % (w, h) + out.tobytes()
            self.photo = tk.PhotoImage(data=ppm)
            self.view.config(image=self.photo)
            self.frames += 1
        delay = max(1, int((1 / self.decoder.fps - (time.monotonic() - t0)) * 1000)) \
            if self.decoder else 50
        self.root.after(delay, self._tick)

    def shutdown(self):
        self.running = False
        if self.decoder:
            self.decoder.stop()
        self._stop_audio()


def main():
    root = tk.Tk()
    app = LiveReactor(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.shutdown(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()
