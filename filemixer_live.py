#!/usr/bin/env python3
"""FileMixer LIVE - realtime corruption console.

Two windows: the CONSOLE (controls + telemetry) and the PREVIEW (the
live video frame) - drag the preview to a second monitor if you have
one; strikes land wherever you click it.

A video plays forever while you destroy it IN REAL TIME:

  click the video   -> chuck a random file's bytes AT that exact spot
  drag across it    -> glitch-brush: smear and rip whatever you touch
  SPACE / big button-> huge chuck with impact animation
  U or Ctrl+Z       -> UNDO the last atrocity
  Chaos slider      -> the reactor also damages things on its own
  Smear slider      -> live datamosh trails

The audio is corrupted live too: chucks blast the soundtrack with the
chucked file's bytes, and high chaos bitcrushes and stutters it.

IT IS COMPLETELY SAFE. Every file is opened read-only. All damage
happens to decoded pixels and samples in memory, on their way to your
screen and speakers. Close the window and nothing ever happened.
The only file this program can create is a Snapshot PNG.
"""

import ctypes
import os
import random
import shutil
import signal
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
WARN = "#ffd75f"

VIEW_W = 512
UNDO_DEPTH = 12


QUIPS = [
    "this is fine.",
    "checksum has left the building",
    "ERR_0xC0DEC: reality misaligned (non-fatal)",
    "the codec is aware and has chosen violence",
    "integrity report: physically fine, emotionally corrupted",
    "motion vectors are now just vibes",
    "packet arrived. from where? unclear.",
    "warranty status: still valid (nothing was written)",
    "the frame remembers what you did",
    "entropy budget exceeded, borrowing from tomorrow",
]


def _dies_with_us():
    """preexec for child processes: SIGKILL them the instant this program
    dies, however it dies. No more immortal songs haunting the desktop."""
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG


def popen(cmd, **kw):
    return subprocess.Popen(cmd, preexec_fn=_dies_with_us, **kw)

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
        self.proc = popen(
            ["ffmpeg", "-nostdin", "-v", "error", "-stream_loop", "-1",
             "-i", path, "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-vf", f"scale={self.w}:{self.h},fps={self.fps}", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.frame_bytes = self.w * self.h * 3
        self.latest = None
        self.alive = True
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        # decoding happens here so a starving pipe can never freeze the UI
        while self.alive:
            data = self.proc.stdout.read(self.frame_bytes)
            if not data or len(data) < self.frame_bytes:
                time.sleep(0.05)
                continue
            self.latest = np.frombuffer(data, dtype=np.uint8).reshape(
                self.h, self.w, 3)

    def read(self):
        return self.latest

    def stop(self):
        self.alive = False
        self.proc.kill()


class AudioVizDecoder:
    """For audio files: renders a live scrolling visual from the decoded
    samples (waveform over byte-colored history) - which then gets
    corrupted exactly like video. Same interface as Decoder."""

    def __init__(self, path, width):
        self.w, self.h = width, 288
        self.fps = 20
        self.spf = 22050 // self.fps          # samples per frame
        self.proc = popen(
            ["ffmpeg", "-nostdin", "-v", "quiet", "-stream_loop", "-1",
             "-i", path, "-vn", "-f", "s16le", "-ar", "22050", "-ac", "1",
             "pipe:1"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.buf = np.zeros((self.h, self.w, 3), dtype=np.uint8)
        self.latest = None
        self.alive = True
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while self.alive:
            t0 = time.monotonic()
            raw = self.proc.stdout.read(self.spf * 2)
            if not raw or len(raw) < self.spf * 2:
                time.sleep(0.05)
                continue
            smp = np.frombuffer(raw, dtype=np.int16)
            self.buf = np.roll(self.buf, -3, axis=0)
            # bottom rows: the samples' raw bytes as color history
            bytes_row = np.frombuffer(raw, dtype=np.uint8)[: self.w * 3]
            if len(bytes_row) < self.w * 3:
                bytes_row = np.pad(bytes_row, (0, self.w * 3 - len(bytes_row)))
            self.buf[-3:] = bytes_row.reshape(1, self.w, 3)
            frame = self.buf.copy()
            # the waveform itself
            xs = np.linspace(0, len(smp) - 1, self.w).astype(int)
            ys = (self.h // 2 + (smp[xs].astype(np.int32) * (self.h // 3)
                                 // 32768)).clip(1, self.h - 2)
            frame[ys, np.arange(self.w)] = (127, 255, 160)
            frame[ys - 1, np.arange(self.w)] = (40, 160, 90)
            self.latest = frame
            time.sleep(max(0.0, 1 / self.fps - (time.monotonic() - t0)))

    def read(self):
        return self.latest

    def stop(self):
        self.alive = False
        self.proc.kill()


class FuelPile:
    """Read-only grab-bag of random byte slices from a folder's files."""

    def __init__(self, folder):
        self.files = []
        for n in os.listdir(folder):
            p = os.path.join(folder, n)
            try:
                if os.path.isfile(p) and os.path.getsize(p) > 4096:
                    self.files.append(p)
            except OSError:
                pass
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


class AudioMangler(threading.Thread):
    """Decodes the video's audio and corrupts it live on the way to the
    speakers: chuck blasts, chaos bitcrush, stutters. Read-only, of course."""

    CHUNK = 8192  # bytes of s16 stereo ~ 46ms

    def __init__(self, path, app):
        super().__init__(daemon=True)
        self.app = app
        self.alive = True
        self.dec = popen(
            ["ffmpeg", "-nostdin", "-v", "quiet", "-stream_loop", "-1",
             "-i", path, "-vn", "-f", "s16le", "-ar", "44100", "-ac", "2",
             "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if shutil.which("aplay"):
            sink_cmd = ["aplay", "-q", "-f", "S16_LE", "-r", "44100", "-c", "2"]
        else:
            sink_cmd = ["ffplay", "-nodisp", "-loglevel", "quiet", "-f", "s16le",
                        "-ar", "44100", "-ac", "2", "-i", "pipe:0"]
        self.sink = popen(sink_cmd, stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        self.prev = None
        self.rng = random.Random()

    def run(self):
        while self.alive:
            raw = self.dec.stdout.read(self.CHUNK)
            if not raw or len(raw) < self.CHUNK:
                break
            arr = np.frombuffer(raw, dtype=np.int16).copy()
            chaos = self.app.chaos.get() / 100
            try:
                if time.monotonic() < self.app.audio_blast_until and self.app.fuel:
                    # a chuck is landing: the file's bytes hit the speakers
                    _, fb = self.app.fuel.grab(len(arr), self.rng)
                    blast = (fb.astype(np.int16) - 128) * 90  # 8-bit scream
                    arr = (arr // 2 + blast // 2).astype(np.int16)
                elif chaos > 0.05 and self.rng.random() < chaos * 0.4:
                    shift = 4 + int(chaos * 7)               # bitcrush
                    arr = ((arr >> shift) << shift).astype(np.int16)
                if self.prev is not None and self.rng.random() < chaos * 0.2:
                    arr = self.prev                          # st-st-stutter
            except Exception:
                pass
            self.prev = arr
            try:
                self.sink.stdin.write(arr.tobytes())
            except (BrokenPipeError, OSError):
                break

    def stop(self):
        self.alive = False
        self.dec.kill()
        self.sink.kill()


class LiveReactor:
    def __init__(self, root):
        self.root = root
        root.title("FileMixer LIVE :: realtime corruption console")
        root.configure(bg=BG)
        root.resizable(False, False)

        self.rng = random.Random()
        self.decoder = None
        self.fuel = None
        self.smear = None
        self.photo = None
        self.audio = None
        self.video_path = None
        self.frames = 0
        self.running = False
        self.audio_blast_until = 0.0

        # session telemetry
        self.undo_stack = []
        self.score_mb = 0.0
        self.events = 0
        self.shake = 0

        self._build_ui()

    # ------------------------------------------------------------------ UI
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

        # telemetry bar
        hud = tk.Frame(self.root, bg=BG)
        hud.pack(fill="x", padx=10)
        self.hud_score = tk.Label(hud, text="injected 0.0 MB | 0 events", bg=BG,
                                  fg="#9aa0b8", font=("Monospace", 9))
        self.hud_score.pack(side="left")
        tk.Label(hud, text="source integrity: 100% (read-only)", bg=BG, fg=HACK,
                 font=("Monospace", 9)).pack(side="right")

        # the preview is its own window - drag it to any monitor
        self.preview = tk.Toplevel(self.root)
        self.preview.title("FileMixer LIVE :: preview")
        self.preview.configure(bg="#000000")
        self.preview.resizable(False, False)
        self.preview.protocol("WM_DELETE_WINDOW", self.root.destroy)
        self.canvas = tk.Canvas(self.preview, width=VIEW_W, height=288,
                                bg="#000000", highlightthickness=0)
        self.canvas.pack()
        self.img_item = self.canvas.create_image(0, 0, anchor="nw")
        self.canvas.bind("<Button-1>", self.click_chuck)
        self.canvas.bind("<B1-Motion>", self.drag_brush)
        for win in (self.root, self.preview):
            win.bind("<space>", lambda e: self.chuck_big())
            win.bind("<u>", lambda e: self.undo())
            win.bind("<Control-z>", lambda e: self.undo())
        # park the preview beside the console to start with
        self.root.update_idletasks()
        self.preview.geometry(f"+{self.root.winfo_x() + 420}+{self.root.winfo_y()}")
        self._drag_gate = 0

        ctl = tk.Frame(self.root, bg=BG)
        ctl.pack(fill="x", padx=10, pady=2)
        tk.Label(ctl, text="Chaos:", bg=BG, fg=FG).pack(side="left")
        self.chaos = tk.DoubleVar(value=25)
        ttk.Scale(ctl, from_=0, to=100, variable=self.chaos, length=120).pack(side="left", padx=4)
        tk.Label(ctl, text="Smear:", bg=BG, fg=FG).pack(side="left", padx=(10, 0))
        self.smear_amt = tk.DoubleVar(value=40)
        ttk.Scale(ctl, from_=0, to=95, variable=self.smear_amt, length=120).pack(side="left", padx=4)
        self.audio_var = tk.BooleanVar(value=True)
        tk.Checkbutton(ctl, text="audio", variable=self.audio_var, bg=BG, fg=FG,
                       selectcolor=PANEL, activebackground=BG,
                       command=self._restart_audio).pack(side="right")

        row = tk.Frame(self.root, bg=BG)
        row.pack(fill="x", padx=10, pady=4)
        self.chuck_btn = ttk.Button(row, text="INJECT FILE  (space)",
                                    style="Big.TButton", command=self.chuck_big,
                                    state="disabled")
        self.chuck_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))
        ttk.Button(row, text="UNDO (u)", command=self.undo).pack(side="left", padx=(0, 4))
        ttk.Button(row, text="Snapshot", command=self.snapshot).pack(side="left")

        self.status = tk.StringVar(
            value="Load a video and a fuel folder, then click the PREVIEW window "
                  "to inject at that position. Drag it to a second monitor if "
                  "you like. All sources read-only.")
        tk.Label(self.root, textvariable=self.status, bg=BG, fg=HACK,
                 font=("Monospace", 9), anchor="w", padx=10, pady=4,
                 wraplength=520, justify="left").pack(fill="x")

    # ------------------------------------------------------------- setup
    def pick_video(self):
        path = filedialog.askopenfilename(title="Choose a video to torment")
        if not path:
            return
        kind = fm._probe_kind(path)
        try:
            if kind == "audio":
                dec = AudioVizDecoder(path, VIEW_W)
            elif kind == "video":
                dec = Decoder(path, VIEW_W)
            else:
                self.status.set("need a video or audio file (images and byte "
                                "soup can't drive the preview - yet)")
                return
        except (ValueError, OSError) as e:
            self.status.set(f"can't use that: {e}")
            return
        if self.decoder:
            self.decoder.stop()
        self.video_path = path
        self.decoder = dec
        self.smear = None
        self.undo_stack.clear()
        self.canvas.config(width=dec.w, height=dec.h)
        self.vid_btn.config(text=os.path.basename(path))
        self.status.set(f"loaded {os.path.basename(path)} ({dec.w}x{dec.h} @ "
                        f"{dec.fps}fps, looping) - click the frame to inject")
        self._restart_audio()
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
        self.status.set(f"fuel loaded: {len(self.fuel.files)} files, opened "
                        "read-only - originals guaranteed intact")
        self._maybe_arm()

    def _maybe_arm(self):
        if self.decoder and self.fuel:
            self.chuck_btn.config(state="normal")

    def _restart_audio(self):
        if self.audio:
            self.audio.stop()
            self.audio = None
        if self.audio_var.get() and self.video_path:
            try:
                self.audio = AudioMangler(self.video_path, self)
                self.audio.start()
            except OSError:
                self.status.set("audio device unavailable - running silent")

    # -------------------------------------------------------- game logic
    def _push_undo(self):
        if self.smear is not None:
            self.undo_stack.append(self.smear.copy())
            if len(self.undo_stack) > UNDO_DEPTH:
                self.undo_stack.pop(0)

    def undo(self):
        if not self.undo_stack:
            self.status.set("undo history empty - no modifications to revert")
            return
        self.smear = self.undo_stack.pop()
        self.status.set(f"reverted last injection "
                        f"({len(self.undo_stack)} undo levels remaining)")

    def _hit(self, mb):
        self.score_mb += mb
        self.events += 1
        self.shake = 3
        self.audio_blast_until = time.monotonic() + 0.4
        self.hud_score.config(
            text=f"injected {self.score_mb:.1f} MB | {self.events} events")

    def _blockmosh(self, out, x0, y0, rw, rh, raw):
        """Codec-style damage: the region is rebuilt from a displaced copy
        of the frame itself (motion vectors gone wrong), the fuel's bytes
        bleed through it, 8px strips get scrambled, and sometimes the
        decoder-panic green washes over. Looks like real corruption
        because it's built from the frame's own content."""
        h, w, _ = out.shape
        rng = self.rng
        sy = min(max(y0 + rng.randint(-h // 4, h // 4), 0), h - rh)
        sx = min(max(x0 + rng.randint(-w // 4, w // 4), 0), w - rw)
        src = out[sy:sy + rh, sx:sx + rw].astype(np.float32)
        fuel = raw.reshape(rh, rw, 3).astype(np.float32)
        region = src * 0.6 + fuel * 0.4
        # scramble vertical 8px strips like shattered macroblocks
        strips = [region[:, i:i + 8] for i in range(0, rw - 7, 8)]
        if len(strips) > 1:
            rng.shuffle(strips)
            region[:, :len(strips) * 8] = np.concatenate(strips, axis=1)
        # chroma bleed: green/pink decoder panic
        if rng.random() < 0.3:
            region[..., 1] = np.minimum(region[..., 1] * 1.6 + 40, 255)
        elif rng.random() < 0.3:
            region[..., 0] = np.minimum(region[..., 0] * 1.5 + 30, 255)
        out[y0:y0 + rh, x0:x0 + rw] = region.astype(np.uint8)

    # ------------------------------------------------------ interactions
    def click_chuck(self, event):
        if not (self.fuel and self.smear is not None):
            return
        self._push_undo()
        h, w, _ = self.smear.shape
        x = min(max(event.x, 0), w - 1)
        y = min(max(event.y, 0), h - 1)
        rw, rh = self.rng.randint(w // 6, w // 3), self.rng.randint(h // 6, h // 3)
        x0, y0 = max(0, x - rw // 2), max(0, y - rh // 2)
        rw, rh = min(rw, w - x0), min(rh, h - y0)
        name, raw = self.fuel.grab(rw * rh * 3, self.rng)
        buf = self.smear.astype(np.uint8)
        self._blockmosh(buf, x0, y0, rw, rh, raw)
        self.smear = buf.astype(np.float32)
        self._hit(rw * rh * 3 / 1e6)
        self._impact_anim(x, y, name, big=False)
        self.status.set(f"injected {name} at ({x},{y}) - {self.rng.choice(QUIPS)}")

    def drag_brush(self, event):
        if self.smear is None:
            return
        self._drag_gate += 1
        if self._drag_gate % 2:   # every other motion event is plenty
            return
        h, w, _ = self.smear.shape
        y = min(max(event.y, 12), h - 13)
        band = self.smear[y - 12:y + 12]
        self.smear[y - 12:y + 12] = np.roll(band, self.rng.randint(-60, 60), axis=1)
        if self.fuel and self.rng.random() < 0.3:
            _, raw = self.fuel.grab(w * 2 * 3, self.rng)
            self.smear[y:y + 2] = raw.reshape(2, w, 3).astype(np.float32)
            self.score_mb += w * 2 * 3 / 1e6

    def chuck_big(self):
        if not (self.fuel and self.smear is not None):
            return
        self._push_undo()
        h, w, _ = self.smear.shape
        buf = self.smear.astype(np.uint8)
        name = "?"
        for _ in range(3):
            rh = h // 5
            y = self.rng.randint(0, h - rh)
            name, raw = self.fuel.grab(w * rh * 3, self.rng)
            self._blockmosh(buf, 0, y, w, rh, raw)
        self.smear = buf.astype(np.float32)
        self._hit(w * (h // 2) * 3 / 1e6)
        self._impact_anim(w // 2, h // 2, name, big=True)
        self.status.set(f"bulk injection: {name} - {self.rng.choice(QUIPS)}")

    # -------------------------------------------------------- animations
    def _impact_anim(self, x, y, name, big):
        c = self.canvas
        label = c.create_text(x, y - 12, text=name, fill="#d8d8e8",
                              font=("Monospace", 9 if big else 8))
        rings = []

        def ring(step=0):
            if step > (5 if big else 4):
                for r in rings:
                    c.delete(r)
                return
            r = c.create_oval(x - step * 12, y - step * 12,
                              x + step * 12, y + step * 12,
                              outline=ACCENT, width=1)
            rings.append(r)
            if len(rings) > 1:
                c.delete(rings.pop(0))
            self.root.after(45, lambda: ring(step + 1))
        ring()

        def floatup(step=0):
            if step > 12:
                c.delete(label)
                return
            c.move(label, 0, -1.5)
            self.root.after(50, lambda: floatup(step + 1))
        floatup()

    # ------------------------------------------------------- the reactor
    def _corrupt(self, frame):
        f = frame.astype(np.float32)
        h, w, _ = frame.shape
        chaos = self.chaos.get() / 100
        rng = self.rng

        alpha = 1.0 - self.smear_amt.get() / 100
        if self.smear is None:
            self.smear = f.copy()
        self.smear = self.smear * (1 - alpha) + f * alpha
        out = self.smear.astype(np.uint8).copy()

        if self.fuel and rng.random() < chaos * 0.6:
            rw = max(16, rng.randint(w // 10, w // 3) // 8 * 8)
            rh = rng.randint(h // 12, h // 4)
            x, y = rng.randint(0, w - rw), rng.randint(0, h - rh)
            name, raw = self.fuel.grab(rw * rh * 3, rng)
            self._blockmosh(out, x, y, rw, rh, raw)
            self.score_mb += rw * rh * 3 / 2e6  # ambient damage half-scores

        if rng.random() < chaos * 0.7:
            y = rng.randint(0, h - h // 6)
            band = slice(y, y + h // 6)
            out[band, :, 0] = np.roll(out[band, :, 0], rng.randint(4, 40), axis=1)
        if rng.random() < chaos * 0.5:
            y = rng.randint(0, h - h // 8)
            out[y:y + h // 8] = np.roll(out[y:y + h // 8],
                                        rng.randint(-w // 3, w // 3), axis=1)

        self.smear = self.smear * 0.7 + out.astype(np.float32) * 0.3
        return out

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
            self.canvas.itemconfig(self.img_item, image=self.photo)
            if self.shake > 0:  # impact screen shake
                self.canvas.coords(self.img_item,
                                   self.rng.randint(-3, 3), self.rng.randint(-3, 3))
                self.shake -= 1
            else:
                self.canvas.coords(self.img_item, 0, 0)
            self.frames += 1
        delay = max(1, int((1 / self.decoder.fps - (time.monotonic() - t0)) * 1000)) \
            if self.decoder else 50
        self.root.after(delay, self._tick)

    def shutdown(self):
        self.running = False
        if self.decoder:
            self.decoder.stop()
        if self.audio:
            self.audio.stop()


def main():
    root = tk.Tk()
    app = LiveReactor(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.shutdown(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()
