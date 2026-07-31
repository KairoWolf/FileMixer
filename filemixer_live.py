#!/usr/bin/env python3
"""FileMixer LIVE - the live reactor, game edition.

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

import os
import random
import shutil
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

RANKS = [(0, "unpaid intern"), (5, "reactor technician"), (20, "shift supervisor"),
         (60, "safety inspector (fired)"), (150, "THE INCIDENT"),
         (400, "walking exclusion zone"), (1000, "elephant's foot")]


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
        self.dec = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-v", "quiet", "-stream_loop", "-1",
             "-i", path, "-vn", "-f", "s16le", "-ar", "44100", "-ac", "2",
             "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if shutil.which("aplay"):
            sink_cmd = ["aplay", "-q", "-f", "S16_LE", "-r", "44100", "-c", "2"]
        else:
            sink_cmd = ["ffplay", "-nodisp", "-loglevel", "quiet", "-f", "s16le",
                        "-ar", "44100", "-ac", "2", "-i", "pipe:0"]
        self.sink = subprocess.Popen(sink_cmd, stdin=subprocess.PIPE,
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
        root.title("FileMixer LIVE :: reactor game")
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

        # game state
        self.undo_stack = []
        self.score_mb = 0.0
        self.combo = 0
        self.last_hit = 0.0
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

        # HUD
        hud = tk.Frame(self.root, bg=BG)
        hud.pack(fill="x", padx=10)
        self.hud_score = tk.Label(hud, text="DESTRUCTION: 0.0 MB", bg=BG, fg=ACCENT,
                                  font=("Monospace", 10, "bold"))
        self.hud_score.pack(side="left")
        self.hud_combo = tk.Label(hud, text="", bg=BG, fg=WARN,
                                  font=("Monospace", 10, "bold"))
        self.hud_combo.pack(side="left", padx=12)
        self.hud_rank = tk.Label(hud, text="rank: unpaid intern", bg=BG, fg=HACK,
                                 font=("Monospace", 9))
        self.hud_rank.pack(side="right")

        self.canvas = tk.Canvas(self.root, width=VIEW_W, height=288, bg="#000000",
                                highlightthickness=1, highlightbackground=PANEL)
        self.canvas.pack(padx=10, pady=4)
        self.img_item = self.canvas.create_image(0, 0, anchor="nw")
        self.canvas.bind("<Button-1>", self.click_chuck)
        self.canvas.bind("<B1-Motion>", self.drag_brush)
        self.root.bind("<space>", lambda e: self.chuck_big())
        self.root.bind("<u>", lambda e: self.undo())
        self.root.bind("<Control-z>", lambda e: self.undo())
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
        self.chuck_btn = ttk.Button(row, text="!! CHUCK A FILE IN !! (space)",
                                    style="Big.TButton", command=self.chuck_big,
                                    state="disabled")
        self.chuck_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))
        ttk.Button(row, text="UNDO (u)", command=self.undo).pack(side="left", padx=(0, 4))
        ttk.Button(row, text="Snapshot", command=self.snapshot).pack(side="left")

        self.status = tk.StringVar(
            value="Pick a video + fuel folder, then CLICK THE VIDEO to strike it. "
                  "Files harmed so far: 0 (this number cannot go up)")
        tk.Label(self.root, textvariable=self.status, bg=BG, fg=HACK,
                 font=("Monospace", 9), anchor="w", padx=10, pady=4,
                 wraplength=520, justify="left").pack(fill="x")

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
        self.video_path = path
        self.decoder = dec
        self.smear = None
        self.undo_stack.clear()
        self.canvas.config(width=dec.w, height=dec.h)
        self.vid_btn.config(text=os.path.basename(path))
        self.status.set(f"reactor loaded: {os.path.basename(path)} - "
                        "now CLICK IT. drag across it. show it no mercy.")
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
        self.status.set(f"fuel pile armed: {len(self.fuel.files)} files (read-only, "
                        "they will all survive)")
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
            self.status.set("nothing to undo - the reactor is at peace")
            return
        self.smear = self.undo_stack.pop()
        self.status.set("UNDONE. like it never happened. "
                        f"({len(self.undo_stack)} more undos stacked)")

    def _hit(self, mb):
        now = time.monotonic()
        self.combo = self.combo + 1 if now - self.last_hit < 2.5 else 1
        self.last_hit = now
        self.score_mb += mb
        self.shake = 6
        self.audio_blast_until = now + 0.4
        self.hud_score.config(text=f"DESTRUCTION: {self.score_mb:.1f} MB")
        self.hud_combo.config(text=f"COMBO x{self.combo}!" if self.combo > 1 else "")
        rank = [r for m, r in RANKS if self.score_mb >= m][-1]
        self.hud_rank.config(text=f"rank: {rank}")

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
        self.smear[y0:y0 + rh, x0:x0 + rw] = \
            raw.reshape(rh, rw, 3).astype(np.float32)
        self._hit(rw * rh * 3 / 1e6)
        self._impact_anim(x, y, name, big=False)

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
        name, raw = self.fuel.grab(w * (h // 2) * 3, self.rng)
        y = self.rng.randint(0, h - h // 2)
        self.smear[y:y + h // 2] = raw.reshape(h // 2, w, 3).astype(np.float32)
        self._hit(w * (h // 2) * 3 / 1e6)
        self._impact_anim(w // 2, y + h // 4, name, big=True)

    # -------------------------------------------------------- animations
    def _impact_anim(self, x, y, name, big):
        c = self.canvas
        edge = self.rng.choice([(0, self.rng.randint(0, c.winfo_height())),
                                (c.winfo_width(), self.rng.randint(0, c.winfo_height())),
                                (self.rng.randint(0, c.winfo_width()), 0)])
        streak = c.create_line(*edge, x, y, fill=WARN, width=3 if big else 2)
        label = c.create_text(x, y - 14, text=f">> {name} <<", fill="#ffffff",
                              font=("Monospace", 11 if big else 9, "bold"))
        rings = []
        self.root.after(120, lambda: c.delete(streak))

        def ring(step=0):
            if step > (7 if big else 5):
                for r in rings:
                    c.delete(r)
                return
            r = c.create_oval(x - step * 14, y - step * 14,
                              x + step * 14, y + step * 14,
                              outline=ACCENT if step % 2 else WARN,
                              width=max(1, 4 - step // 2))
            rings.append(r)
            if len(rings) > 2:
                c.delete(rings.pop(0))
            self.root.after(40, lambda: ring(step + 1))
        ring()

        def floatup(step=0):
            if step > 16:
                c.delete(label)
                return
            c.move(label, 0, -2)
            self.root.after(45, lambda: floatup(step + 1))
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
            rw = rng.randint(w // 10, w // 3)
            rh = rng.randint(h // 12, h // 4)
            x, y = rng.randint(0, w - rw), rng.randint(0, h - rh)
            name, raw = self.fuel.grab(rw * rh * 3, rng)
            out[y:y + rh, x:x + rw] = raw.reshape(rh, rw, 3)
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
                                   self.rng.randint(-6, 6), self.rng.randint(-6, 6))
                self.shake -= 1
            else:
                self.canvas.coords(self.img_item, 0, 0)
            self.frames += 1
            self.hud_score.config(text=f"DESTRUCTION: {self.score_mb:.1f} MB")
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
