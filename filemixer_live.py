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
import struct
import tempfile
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
            ["ffmpeg", "-nostdin", "-v", "error", "-re", "-stream_loop", "-1",
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


def _find_mdat(path):
    """Locate the mp4 media-payload box. Everything outside it (ftyp,
    moov - the structure that makes the file playable) is off limits."""
    size_total = os.path.getsize(path)
    with open(path, "rb") as f:
        pos = 0
        while pos < size_total - 8:
            f.seek(pos)
            box_size, typ = struct.unpack(">I4s", f.read(8))
            if box_size == 1:
                box_size = struct.unpack(">Q", f.read(8))[0]
                if typ == b"mdat":
                    return pos + 16, box_size - 16
            elif typ == b"mdat":
                return pos + 8, box_size - 8
            if box_size < 8:
                break
            pos += box_size
    raise ValueError("no mdat box found")


class RealFeed:
    """REAL mode: a working COPY of the source (originals never touched)
    whose actual bytes on disk get corrupted - but only inside the mdat
    payload, never the container structure, so it can glitch but never
    turn to static. The preview decoder reads this genuinely damaged file."""

    def __init__(self, src, kind):
        fd, self.tmp = tempfile.mkstemp(
            suffix=".m4a" if kind == "audio" else ".mp4", prefix="fmlive_")
        os.close(fd)
        if kind == "audio":
            fm._ffmpeg(["-i", src, "-c:a", "aac", "-movflags", "+faststart",
                        self.tmp])
        else:
            fm._ffmpeg(["-i", src, "-c:v", "libx264", "-preset", "veryfast",
                        "-g", "30", "-sc_threshold", "0", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-movflags", "+faststart", self.tmp])
        self.mdat_start, self.mdat_size = _find_mdat(self.tmp)
        self.duration = fm._probe_duration(self.tmp) or 1.0
        self.patches = []       # one entry per strike, for undo

    def strike(self, frac, fuel, rng, spots=35):
        """Overwrite `spots` scattered runs of real bytes near the given
        playback fraction with a fuel file's bytes. Returns the fuel name."""
        guard = int(self.mdat_size * 0.03)   # first keyframe stays alive
        lo, hi = self.mdat_start + guard, self.mdat_start + self.mdat_size - 600
        center = self.mdat_start + int(self.mdat_size * min(max(frac, 0.04), 0.97))
        name = "?"
        applied = []
        with open(self.tmp, "r+b") as f:
            for _ in range(spots):
                off = min(max(center + rng.randint(0, int(self.mdat_size * 0.03)),
                              lo), hi)
                ln = rng.randint(64, 512)
                f.seek(off)
                orig = f.read(ln)
                name, raw = fuel.grab(ln, rng)
                f.seek(off)
                f.write(raw.tobytes())
                applied.append((off, orig))
        self.patches.append(applied)
        return name

    def undo(self):
        if not self.patches:
            return False
        with open(self.tmp, "r+b") as f:
            for off, orig in reversed(self.patches.pop()):
                f.seek(off)
                f.write(orig)
        return True

    def cleanup(self):
        try:
            os.remove(self.tmp)
        except OSError:
            pass


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
            chaos = 0 if self.app.real else self.app.chaos.get() / 100
            try:
                if (not self.app.real
                        and time.monotonic() < self.app.audio_blast_until):
                    # movie glitch: the sound machine-gun stutters and drops
                    if self.prev is not None:
                        piece = self.prev[: max(len(self.prev) // 4, 1)]
                        arr = np.tile(piece, 4)[: len(arr)].copy()
                    if self.rng.random() < 0.4:
                        arr[: len(arr) // 2] = 0          # dropout
                    shift = 6
                    arr = ((arr >> shift) << shift).astype(np.int16)
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
        self.src_kind = None
        self.real = None            # RealFeed when in real mode
        self.play_start = 0.0
        self.frames = 0
        self.running = False
        self.audio_blast_until = 0.0

        # cinematic burst engine
        self.burst = 0              # frames of glitch remaining
        self.burst_pos = (0, 0)
        self.frozen = None          # the stuck frame during a freeze

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
        tk.Label(ctl, text="Mode:", bg=BG, fg=FG).pack(side="left")
        self.mode_var = tk.StringVar(value="instant")
        mode_cb = ttk.Combobox(ctl, textvariable=self.mode_var, state="readonly",
                               width=7, values=["instant", "real"])
        mode_cb.pack(side="left", padx=(4, 10))
        mode_cb.bind("<<ComboboxSelected>>", lambda e: self._apply_mode())
        tk.Label(ctl, text="Auto-chaos:", bg=BG, fg=FG).pack(side="left")
        self.chaos = tk.DoubleVar(value=0)
        ttk.Scale(ctl, from_=0, to=100, variable=self.chaos, length=120).pack(side="left", padx=4)
        tk.Label(ctl, text="Smear:", bg=BG, fg=FG).pack(side="left", padx=(10, 0))
        self.smear_amt = tk.DoubleVar(value=0)
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
            value="Load a video + fuel folder. It plays CLEAN until you strike "
                  "it: click the preview to inject there. Auto-chaos makes the "
                  "reactor damage things by itself; Smear adds melt trails.")
        tk.Label(self.root, textvariable=self.status, bg=BG, fg=HACK,
                 font=("Monospace", 9), anchor="w", padx=10, pady=4,
                 wraplength=520, justify="left").pack(fill="x")

    # ------------------------------------------------------------- setup
    def pick_video(self):
        path = filedialog.askopenfilename(title="Choose a video to torment")
        if not path:
            return
        kind = fm._probe_kind(path)
        if kind not in ("audio", "video"):
            self.status.set("need a video or audio file (images and byte "
                            "soup can't drive the preview - yet)")
            return
        self.video_path = path
        self.src_kind = kind
        if self.real:
            self.real.cleanup()
            self.real = None
        self.undo_stack.clear()
        try:
            if self.mode_var.get() == "real":
                self._apply_mode()      # builds the working copy, then swaps
            else:
                self._swap_source(path)
        except (ValueError, OSError) as e:
            self.status.set(f"can't use that: {e}")
            return
        self.vid_btn.config(text=os.path.basename(path))
        if self.mode_var.get() != "real":
            self.status.set(f"loaded {os.path.basename(path)} - playing clean. "
                            "every glitch from here on is one you caused.")
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

    def _apply_mode(self):
        if not self.video_path:
            return
        if self.mode_var.get() == "real":
            self.status.set("REAL mode: re-encoding a working copy (originals "
                            "untouched) - the decoder will read genuinely "
                            "damaged bytes ...")
            threading.Thread(target=self._prepare_real, daemon=True).start()
        else:
            if self.real:
                self.real.cleanup()
                self.real = None
            self._swap_source(self.video_path)
            self.status.set("instant mode: damage is painted in memory, "
                            "zero latency")

    def _prepare_real(self):
        try:
            real = RealFeed(self.video_path, self.src_kind)
        except (ValueError, RuntimeError, OSError) as e:
            self.root.after(0, lambda: (self.mode_var.set("instant"),
                                        self.status.set(f"real mode failed: {e}")))
            return
        self.real = real
        self.root.after(0, lambda: (
            self._swap_source(real.tmp),
            self.status.set("REAL mode armed: this is a real decoder reading a "
                            "real file whose bytes you are about to really "
                            "damage. (container is protected - it can glitch "
                            "but never die)")))

    def _swap_source(self, path):
        if self.decoder:
            self.decoder.stop()
        kind = self.src_kind
        self.decoder = AudioVizDecoder(path, VIEW_W) if kind == "audio"             else Decoder(path, VIEW_W)
        self.canvas.config(width=self.decoder.w, height=self.decoder.h)
        self.smear = None
        self.play_start = time.monotonic()
        self._feed_path = path
        self._restart_audio(path)

    def _play_frac(self):
        dur = self.real.duration if self.real else 1.0
        return ((time.monotonic() - self.play_start) % dur) / dur

    def _maybe_arm(self):
        if self.decoder and self.fuel:
            self.chuck_btn.config(state="normal")

    def _restart_audio(self, path=None):
        if self.audio:
            self.audio.stop()
            self.audio = None
        path = path or getattr(self, "_feed_path", self.video_path)
        if self.audio_var.get() and path:
            try:
                self.audio = AudioMangler(path, self)
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
        if self.real:
            if self.real.undo():
                self.status.set("restored the original bytes of the last strike "
                                "- future loops play that part clean again")
            else:
                self.status.set("no byte damage left to undo")
            return
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
        self.burst = self.rng.randint(8, 14)
        self.frozen = None
        self.audio_blast_until = time.monotonic() + 0.5
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
        if self.real:
            frac = self._play_frac() + 1.2 / self.real.duration
            name = self.real.strike(frac, self.fuel, self.rng)
            self._hit(0.01)
            self._impact_anim(event.x, event.y, name, big=False)
            self.status.set(f"REAL bytes of the working copy damaged with "
                            f"{name} - arriving on screen in about a second. "
                            f"{self.rng.choice(QUIPS)}")
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
        self.burst_pos = (x, y)
        self._hit(rw * rh * 3 / 1e6)
        self._impact_anim(x, y, name, big=False)
        self.status.set(f"injected {name} at ({x},{y}) - {self.rng.choice(QUIPS)}")

    def drag_brush(self, event):
        if self.smear is None:
            return
        self._drag_gate += 1
        if self.real:
            if self.fuel and self._drag_gate % 6 == 0:
                self.real.strike(self._play_frac() + 1.0 / self.real.duration,
                                 self.fuel, self.rng, spots=6)
            return
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
        if self.real:
            frac = self._play_frac() + 1.2 / self.real.duration
            name = self.real.strike(frac, self.fuel, self.rng, spots=140)
            self._hit(0.05)
            self._impact_anim(self.decoder.w // 2, self.decoder.h // 2, name, big=True)
            self.status.set(f"bulk REAL damage: 140 byte-runs of {name} written "
                            f"into the copy. {self.rng.choice(QUIPS)}")
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

        if self.real:
            return out          # real mode: what the decoder says, you see

        # auto-chaos occasionally sparks a small burst of its own
        if self.burst == 0 and chaos > 0 and rng.random() < chaos * 0.03:
            self.burst = rng.randint(4, 8)
            self.burst_pos = (rng.randint(0, w), rng.randint(0, h))
            self.audio_blast_until = time.monotonic() + 0.3

        # ------- the burst: movie-style glitch, then snap back clean -------
        if self.burst > 0:
            self.burst -= 1
            bx, by = self.burst_pos
            # sometimes the picture STICKS on a frame mid-burst
            if self.frozen is not None and rng.random() < 0.6:
                out = self.frozen.copy()
            elif rng.random() < 0.35:
                self.frozen = out.copy()
            # RGB channels rip apart
            dx = rng.randint(4, 18)
            out[..., 0] = np.roll(out[..., 0], dx, axis=1)
            out[..., 2] = np.roll(out[..., 2], -dx, axis=1)
            # horizontal slices tear sideways, worst near the impact point
            for _ in range(rng.randint(3, 7)):
                sy = min(max(int(rng.gauss(by, h / 5)), 0), h - 8)
                sh = rng.randint(2, 10)
                out[sy:sy + sh] = np.roll(out[sy:sy + sh],
                                          rng.randint(-w // 4, w // 4), axis=1)
            # a couple of shattered blocks made of the frame's own content
            if self.fuel and rng.random() < 0.7:
                rw = max(24, min(w // 4, 96)) // 8 * 8
                rh = rng.randint(12, h // 5)
                x = min(max(bx + rng.randint(-w // 5, w // 5), 0), w - rw)
                y = min(max(by + rng.randint(-h // 5, h // 5), 0), h - rh)
                _, raw = self.fuel.grab(rw * rh * 3, rng)
                self._blockmosh(out, x, y, rw, rh, raw)
            # exposure flicker + the occasional thin white scanline
            out = (out.astype(np.float32) *
                   (0.75 if rng.random() < 0.5 else 1.25)).clip(0, 255).astype(np.uint8)
            if rng.random() < 0.5:
                sy = rng.randint(0, h - 2)
                out[sy:sy + 1] = 235
            if self.burst == 0:
                self.frozen = None      # snap back to clean playback

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
        if (self.real and self.fuel and self.chaos.get() > 0
                and self.rng.random() < self.chaos.get() / 100 * 0.02):
            self.real.strike(self._play_frac() + 1.5 / self.real.duration,
                             self.fuel, self.rng, spots=8)
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
        if self.real:
            self.real.cleanup()


def main():
    root = tk.Tk()
    app = LiveReactor(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.shutdown(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()
