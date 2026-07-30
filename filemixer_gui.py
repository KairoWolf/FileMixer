#!/usr/bin/env python3
"""FileMixer GUI - the byte collider.

Left panel  = file A's bytes as colored pixels
Right panel = file B's bytes
Center      = the output painting itself while the mixer runs, with a
              bright write-head sweeping ahead like a disk head

Two sections:
  REAL BYTE MASHING - every real smash mode; actual bytes, zero tricks
  FAKE ZONE         - backrooms-flavoured ffmpeg renders; always plays

Pure tkinter, no installs. The mixing lives in filemixer.py so the GUI
and the terminal command always behave the same.
"""

import mmap
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import filemixer as fm

# ---------------------------------------------------------------------------
# byte -> color palette (classic binary-visualization scheme):
#   0x00 near-black | control bytes blue | printable ASCII green
#   high bytes magenta->orange | 0xFF white
# ---------------------------------------------------------------------------

def _build_palette():
    pal = []
    for v in range(256):
        if v == 0:
            pal.append("#0b0b10")
        elif v == 255:
            pal.append("#ffffff")
        elif v < 32:
            t = v / 32
            pal.append("#%02x%02x%02x" % (int(20 + 40 * t), int(60 + 80 * t), int(120 + 135 * t)))
        elif v < 127:
            t = (v - 32) / 95
            pal.append("#%02x%02x%02x" % (int(30 + 60 * t), int(140 + 115 * t), int(60 + 40 * t)))
        else:
            t = (v - 127) / 128
            pal.append("#%02x%02x%02x" % (int(180 + 75 * t), int(40 + 120 * t), int(160 - 120 * t)))
    return pal

PALETTE = _build_palette()

BG = "#101016"
PANEL = "#1e1e28"
FG = "#e8e8f0"
ACCENT = "#ff5f87"
HACK = "#4fee6f"                 # hacker green
BR_BG = "#242012"                # backrooms mustard-dark
BR_FG = "#d8c358"                # backrooms yellow

SIDE_W, SIDE_H = 148, 280
MID_W, MID_H = 296, 280
HEAD_COLOR = "#7dffa0"           # write-head sweep


def render_file_to_image(img, path, w, h):
    """Paint a file into a PhotoImage by sampling w*h bytes evenly across it."""
    size = os.path.getsize(path)
    if size == 0:
        img.put("#000000", to=(0, 0, w, h))
        return
    rows = []
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        total = w * h
        for y in range(h):
            row = []
            for x in range(w):
                i = (y * w + x) * (size - 1) // max(total - 1, 1)
                row.append(PALETTE[mm[i]])
            rows.append("{" + " ".join(row) + "}")
    img.put(" ".join(rows), to=(0, 0))


class LiveMixView:
    """Paints the output into the center panel as bytes stream in, with a
    glowing write-head row sweeping just ahead of the data."""

    def __init__(self, img, w, h, expected_total):
        self.img = img
        self.w, self.h = w, h
        self.step = max(1, expected_total // (w * h))
        self.next_pos = 0
        self.seen = 0
        self.pixel = 0
        self.row = []

    def feed(self, chunk):
        end = self.seen + len(chunk)
        while self.next_pos < end and self.pixel < self.w * self.h:
            b = chunk[self.next_pos - self.seen]
            self.row.append(PALETTE[b])
            self.pixel += 1
            self.next_pos += self.step
            if len(self.row) == self.w:
                y = self.pixel // self.w - 1
                self.img.put("{" + " ".join(self.row) + "}", to=(0, y))
                if y + 1 < self.h:  # the write-head: a bright scanning line
                    self.img.put(HEAD_COLOR, to=(0, y + 1, self.w, min(y + 2, self.h)))
                self.row = []
        self.seen = end

    def flush(self):
        if self.row:
            y = self.pixel // self.w
            pad = self.row + [self.row[-1]] * (self.w - len(self.row))
            self.img.put("{" + " ".join(pad) + "}", to=(0, y))
            self.row = []


class FileMixerApp:
    def __init__(self, root):
        self.root = root
        root.title("FileMixer :: the byte collider")
        root.configure(bg=BG)
        root.resizable(False, False)

        self.path_a = None
        self.path_b = None
        self.extra_paths = []    # files C, D, E ... for 3+ file smashing
        self.out_path = None
        self.chunk_queue = None
        self.live = None
        self.expected = 1
        self.busy = False
        self.done = False
        self.render_t0 = 0.0
        self._speed = 60.0       # plain float: safe to read from worker thread
        self._hexoff = 0

        self._build_ui()
        self._flicker()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=FG, fieldbackground=PANEL)
        s.configure("TButton", background=PANEL, foreground=FG, padding=6)
        s.map("TButton", background=[("active", "#2c2c3a")])
        s.configure("Smash.TButton", font=("Sans", 13, "bold"),
                    foreground=ACCENT, padding=8)
        s.configure("Fake.TButton", font=("Sans", 11, "bold"),
                    background=BR_BG, foreground=BR_FG, padding=8)
        s.map("Fake.TButton", background=[("active", "#3a3420")])
        s.configure("TCombobox", fieldbackground=PANEL, background=PANEL, foreground=FG)
        s.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL)
        s.configure("Horizontal.TScale", troughcolor=PANEL)

        # -- top: file pickers ------------------------------------------
        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=10, pady=(8, 4))
        self.btn_a = ttk.Button(top, text="[ Choose File A ]", command=lambda: self.pick("a"))
        self.btn_a.pack(side="left", expand=True, fill="x", padx=(0, 5))
        self.btn_b = ttk.Button(top, text="[ Choose File B ]", command=lambda: self.pick("b"))
        self.btn_b.pack(side="left", expand=True, fill="x", padx=(5, 0))

        extra = tk.Frame(self.root, bg=BG)
        extra.pack(fill="x", padx=10)
        ttk.Button(extra, text="+ add MORE files to the pile",
                   command=self.pick_extra).pack(side="left")
        ttk.Button(extra, text="x clear extras",
                   command=self.clear_extras).pack(side="left", padx=4)
        self.extra_var = tk.StringVar(value="")
        tk.Label(extra, textvariable=self.extra_var, bg=BG, fg="#9a8fd0",
                 font=("Sans", 8), anchor="w").pack(side="left", fill="x", padx=6)

        # -- the three byte panels ---------------------------------------
        mid = tk.Frame(self.root, bg=BG)
        mid.pack(padx=10, pady=4)

        def panel(parent, title, w, h, fg=FG):
            f = tk.Frame(parent, bg=BG)
            tk.Label(f, text=title, bg=BG, fg=fg, font=("Monospace", 10, "bold")).pack()
            img = tk.PhotoImage(width=w, height=h)
            img.put("#15151d", to=(0, 0, w, h))
            lbl = tk.Label(f, image=img, bg=PANEL, bd=1, relief="solid")
            lbl.img = img
            lbl.pack(padx=3)
            return f, img

        fa, self.img_a = panel(mid, "FILE A", SIDE_W, SIDE_H)
        fa.pack(side="left", padx=3)
        fc, self.img_mix = panel(mid, ">>> BYTE COLLIDER <<<", MID_W, MID_H, fg=HACK)
        fc.pack(side="left", padx=3)
        fb, self.img_b = panel(mid, "FILE B", SIDE_W, SIDE_H)
        fb.pack(side="left", padx=3)

        # -- hacker hex console -------------------------------------------
        self.console = tk.Text(self.root, height=5, bg="#060a06", fg=HACK,
                               font=("Monospace", 9), bd=0, highlightthickness=1,
                               highlightbackground="#1c3a24", state="disabled",
                               insertwidth=0)
        self.console.pack(fill="x", padx=10, pady=(4, 2))
        self._console_write("filemixer v2 :: byte collider online :: awaiting targets _")

        # -- two sections: REAL | FAKE ZONE --------------------------------
        secs = tk.Frame(self.root, bg=BG)
        secs.pack(fill="x", padx=10, pady=4)

        # REAL BYTE MASHING
        real = tk.LabelFrame(secs, text=" REAL BYTE MASHING ", bg=BG, fg=ACCENT,
                             font=("Monospace", 10, "bold"), bd=1, relief="solid",
                             labelanchor="n")
        real.pack(side="left", expand=True, fill="both", padx=(0, 5))
        r1 = tk.Frame(real, bg=BG); r1.pack(fill="x", padx=8, pady=(6, 2))
        tk.Label(r1, text="Mode:", bg=BG, fg=FG).pack(side="left")
        self.mode_var = tk.StringVar(value="interleave")
        ttk.Combobox(r1, textvariable=self.mode_var, state="readonly", width=13,
                     values=list(fm.MODES) + ["remix", "datamosh"]).pack(side="left", padx=4)
        self.mode_hint = tk.Label(real, text="", bg=BG, fg="#888", font=("Sans", 8),
                                  anchor="w", justify="left", wraplength=340)
        self.mode_hint.pack(fill="x", padx=8)
        self.mode_var.trace_add("write", lambda *a: self._update_hint())
        r2 = tk.Frame(real, bg=BG); r2.pack(fill="x", padx=8, pady=2)
        tk.Label(r2, text="Speed:", bg=BG, fg=FG).pack(side="left")
        self.speed_var = tk.DoubleVar(value=60)
        ttk.Scale(r2, from_=0, to=100, variable=self.speed_var, length=110,
                  command=lambda v: setattr(self, "_speed", float(v))).pack(side="left", padx=4)
        tk.Label(r2, text="slo-mo <-> instant", bg=BG, fg="#777",
                 font=("Sans", 8)).pack(side="left")
        r3 = tk.Frame(real, bg=BG); r3.pack(fill="x", padx=8, pady=2)
        tk.Label(r3, text="Intensity:", bg=BG, fg=FG).pack(side="left")
        self.intensity_var = tk.DoubleVar(value=5)
        ttk.Scale(r3, from_=1, to=100, variable=self.intensity_var,
                  length=110).pack(side="left", padx=4)
        self.int_lbl = tk.Label(r3, text="5%", bg=BG, fg=FG, width=4)
        self.int_lbl.pack(side="left")
        self.intensity_var.trace_add("write", lambda *a: self.int_lbl.config(
            text=f"{self.intensity_var.get():.0f}%"))
        self.smash_btn = ttk.Button(real, text="* SMASH THE BYTES *", style="Smash.TButton",
                                    command=self.start_smash, state="disabled")
        self.smash_btn.pack(fill="x", padx=8, pady=(4, 8))

        # FAKE ZONE (backrooms)
        fake = tk.LabelFrame(secs, text=" :: FAKE ZONE :: ", bg=BR_BG, fg=BR_FG,
                             font=("Monospace", 10, "bold"), bd=1, relief="solid",
                             labelanchor="n")
        fake.pack(side="left", expand=True, fill="both", padx=(5, 0))
        self.fake_frame = fake
        tk.Label(fake, text="level 0 - nothing here is real.\n"
                            "effects only. the output ALWAYS plays.",
                 bg=BR_BG, fg="#8a7d3a", font=("Monospace", 8), justify="left",
                 anchor="w").pack(fill="x", padx=8, pady=(6, 2))
        f1 = tk.Frame(fake, bg=BR_BG); f1.pack(fill="x", padx=8, pady=2)
        tk.Label(f1, text="Preset:", bg=BR_BG, fg=BR_FG).pack(side="left")
        self.preset_var = tk.StringVar(value="backrooms")
        ttk.Combobox(f1, textvariable=self.preset_var, state="readonly", width=15,
                     values=list(fm.FAKE_PRESETS)).pack(side="left", padx=4)
        tk.Label(fake, text="(uses the same Intensity dial)", bg=BR_BG, fg="#8a7d3a",
                 font=("Sans", 8), anchor="w").pack(fill="x", padx=8)
        self.fake_btn = ttk.Button(fake, text="~ ENTER THE FAKE ZONE ~", style="Fake.TButton",
                                   command=self.start_fake, state="disabled")
        self.fake_btn.pack(fill="x", padx=8, pady=(6, 8))

        self.progress = ttk.Progressbar(self.root, maximum=1.0)
        self.progress.pack(fill="x", padx=10, pady=(2, 0))

        # -- playback row ---------------------------------------------------
        play = tk.Frame(self.root, bg=BG)
        play.pack(fill="x", padx=10, pady=6)
        self.play_btns = []
        for text, cmd in (("Play result", self.play_result),
                          ("FORCE VIDEO!", self.play_force_video),
                          ("Raw sound", self.play_raw_audio),
                          ("Raw pixels", self.play_raw_video),
                          ("Open folder", self.open_folder)):
            b = ttk.Button(play, text=text, command=cmd, state="disabled")
            b.pack(side="left", expand=True, fill="x", padx=2)
            self.play_btns.append(b)

        self.status_var = tk.StringVar(
            value="Pick two files - any files at all - then smash (real) or render (fake).")
        tk.Label(self.root, textvariable=self.status_var, bg=BG, fg="#aaa",
                 anchor="w", padx=10, pady=4, wraplength=640,
                 justify="left").pack(fill="x")
        self._update_hint()

    HINTS = {
        "interleave": "a bite of A, a bite of B, repeat - the classic smash",
        "zipper": "single bytes alternating: a b a b a b",
        "splice": "randomly-sized bites of each - seeded chaos",
        "shuffle": "both files diced into chunks and shuffled together",
        "stutter": "A st-st-stutters while B barges in",
        "reverse": "A forwards + B backwards at the same time",
        "sprinkle": "keeps A playable-ish: Intensity % of chunks become B",
        "xor": "A XOR B - maximum destruction",
        "add": "bytes added together (mod 256)",
        "subtract": "B subtracted from A byte by byte",
        "and": "bitwise AND - darkness wins",
        "or": "bitwise OR - brightness wins",
        "rotate": "A's bits spun around by B's bytes",
        "blend": "every byte averaged - a smooth 50/50 byte smoothie",
        "waltz": "ONE two three: A leads with double steps, the rest follow",
        "drunk": "stumbles around ALL files grabbing random gulps",
        "yeet": "interleaves everything but YEETS 25% of chunks into the void",
        "sortstorm": "sorts the bytes into eerily tidy rainbow gradients",
        "scream": "every byte forced to 0 or 255. it screams. you were warned",
        "remix": "DECODES both files and splices B's content into A - real "
                 "content, always playable (photo chunks flashing in a video!)",
        "datamosh": "the BAD WIFI smear: real keyframe packets yeeted from the "
                    "stream so frames melt into each other - and it still plays",
    }

    def _update_hint(self):
        self.mode_hint.config(text=self.HINTS.get(self.mode_var.get(), ""))

    # ---------------------------------------------------------- console
    def _console_write(self, line):
        self.console.config(state="normal")
        self.console.insert("end", "\n" + line)
        lines = int(self.console.index("end-1c").split(".")[0])
        if lines > 200:
            self.console.delete("1.0", f"{lines - 200}.0")
        self.console.see("end")
        self.console.config(state="disabled")

    def _console_hex(self, chunk):
        piece = chunk[:24]
        hx = " ".join(f"{b:02X}" for b in piece)
        txt = "".join(chr(b) if 32 <= b < 127 else "." for b in piece)
        self._console_write(f"0x{self._hexoff:08X}  {hx:<71}  |{txt}|")
        self._hexoff += len(chunk)

    def _flicker(self):
        # the FAKE ZONE label buzzes like a dying fluorescent tube
        t = time.monotonic()
        bright = (int(t * 7) % 11) not in (3, 7)
        self.fake_frame.config(fg=BR_FG if bright else "#5a5228")
        self.root.after(140, self._flicker)

    # ------------------------------------------------------------- actions
    def pick(self, which):
        path = filedialog.askopenfilename(title=f"Choose file {which.upper()}")
        if not path:
            return
        size = os.path.getsize(path)
        label = f"{os.path.basename(path)}  ({size:,} B)"
        if which == "a":
            self.path_a = path
            self.btn_a.config(text=label)
            render_file_to_image(self.img_a, path, SIDE_W, SIDE_H)
        else:
            self.path_b = path
            self.btn_b.config(text=label)
            render_file_to_image(self.img_b, path, SIDE_W, SIDE_H)
        self._console_write(f"target {which.upper()} locked: {path} [{size:,} bytes]")
        if self.path_a and self.path_b:
            self.smash_btn.config(state="normal")
            self.fake_btn.config(state="normal")
            self.status_var.set("Ready. SMASH for real corruption, FAKE ZONE for safe spookiness.")

    def pick_extra(self):
        path = filedialog.askopenfilename(title="Add another file to the pile")
        if not path:
            return
        self.extra_paths.append(path)
        self._console_write(f"extra target locked: {path} "
                            f"[{os.path.getsize(path):,} bytes]")
        self._show_extras()

    def clear_extras(self):
        if self.extra_paths:
            self.extra_paths = []
            self._console_write("extra targets released")
            self._show_extras()

    def _show_extras(self):
        names = [os.path.basename(p) for p in self.extra_paths]
        self.extra_var.set(f"+ {len(names)} more: {', '.join(names)}" if names else "")

    def paths(self):
        return [self.path_a, self.path_b] + self.extra_paths

    def _output_path(self, prefix):
        name = fm.default_output_name(self.paths())
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            prefix + name[len("mashed_"):] if prefix else name)

    def _confirm_overwrite(self, out):
        if os.path.exists(out):
            return messagebox.askyesno(
                "Overwrite?", f"{os.path.basename(out)} already exists. Overwrite it?")
        return True

    def _lock_ui(self):
        self.busy = True
        self.smash_btn.config(state="disabled")
        self.fake_btn.config(state="disabled")
        for b in self.play_btns:
            b.config(state="disabled")

    def _unlock_ui(self, enable_play):
        self.busy = False
        self.smash_btn.config(state="normal")
        self.fake_btn.config(state="normal")
        if enable_play:
            for b in self.play_btns:
                b.config(state="normal")

    # ------------------------------------------------------- real smashing
    def start_smash(self):
        if self.busy:
            return
        mode = self.mode_var.get()
        if mode in ("remix", "datamosh"):
            self._start_render(mode)
            return
        out = self._output_path("")
        if not self._confirm_overwrite(out):
            return
        self.out_path = out
        self.expected = min(fm.expected_size(mode, self.paths()),
                            fm.DEFAULT_MAX_SIZE)
        self.img_mix.put("#15151d", to=(0, 0, MID_W, MID_H))
        self.live = LiveMixView(self.img_mix, MID_W, MID_H, self.expected)
        self.chunk_queue = queue.Queue(maxsize=256)
        self.done = False
        self._hexoff = 0
        self._lock_ui()
        self._console_write(f">> SMASH INITIATED :: {len(self.paths())} files "
                            f":: mode={mode} intensity={self.intensity_var.get():.0f}%")
        self.status_var.set(f"Smashing with mode '{mode}' ...")
        threading.Thread(target=self._mix_worker,
                         args=(mode, out, self.intensity_var.get()),
                         daemon=True).start()
        self.root.after(33, self._drain_queue)

    def _mix_worker(self, mode, out, intensity):
        try:
            with open(out, "wb") as f:
                for chunk in fm.smash(self.paths(), mode=mode,
                                      chunk=fm.DEFAULT_CHUNK, intensity=intensity):
                    f.write(chunk)
                    self.chunk_queue.put(chunk)
                    if self._speed < 99:
                        time.sleep(((100 - self._speed) / 100) ** 2 * 0.35)
            self.chunk_queue.put(None)
        except Exception as e:
            self.chunk_queue.put(e)

    def _drain_queue(self):
        wrote_hex = False
        try:
            for _ in range(64):
                item = self.chunk_queue.get_nowait()
                if item is None:
                    self._finish()
                    return
                if isinstance(item, Exception):
                    self.status_var.set(f"Error: {item}")
                    self._console_write(f"!! ERROR: {item}")
                    self._unlock_ui(enable_play=False)
                    return
                self.live.feed(item)
                if not wrote_hex:  # one console line per frame, not per chunk
                    self._console_hex(item)
                    wrote_hex = True
                else:
                    self._hexoff += len(item)
                self.progress["value"] = min(self.live.seen / self.expected, 1.0)
        except queue.Empty:
            pass
        self.root.after(33, self._drain_queue)

    def _finish(self):
        self.live.flush()
        self.progress["value"] = 1.0
        size = os.path.getsize(self.out_path)
        self._console_write(f">> COLLISION COMPLETE :: {size:,} bytes written "
                            f":: sources untouched")
        self.status_var.set(
            f"Done! Wrote {os.path.basename(self.out_path)} ({size:,} bytes). "
            "Originals untouched. Play the wreckage ->")
        self._unlock_ui(enable_play=True)
        self.done = True

    # --------------------------------------------- ffmpeg renders (fake/remix)
    def start_fake(self):
        if not self.busy:
            self._start_render("fake")

    def _start_render(self, kind):
        prefix = {"fake": "fake_", "remix": "remix_", "datamosh": "mosh_"}[kind]
        out = fm.mp4_safe(self._output_path(prefix), self.path_a)
        if not self._confirm_overwrite(out):
            return
        self.out_path = out
        self._lock_ui()
        self.progress["value"] = 0.0
        self.render_t0 = time.monotonic()
        prog = lambda frac: self.root.after(
            0, lambda: self.progress.configure(value=frac))
        if kind == "fake":
            preset = self.preset_var.get()
            self._console_write(f">> entering the fake zone :: preset={preset} :: "
                                "no bytes were harmed")
            desc = f"FAKE ZONE '{preset}'"
            work = lambda: fm.fake_glitch(self.paths(), out,
                                          intensity=self.intensity_var.get(),
                                          preset=preset, on_progress=prog)
        elif kind == "datamosh":
            self._console_write(">> DATAMOSH :: yeeting keyframes from the real "
                                "stream :: prepare to smear")
            desc = "datamosh"
            work = lambda: fm.datamosh(self.paths(), out,
                                       intensity=self.intensity_var.get(),
                                       on_progress=prog)
        else:
            self._console_write(">> REMIX :: decoding all files, splicing them into A")
            desc = "remix"
            work = lambda: fm.remix(self.paths(), out,
                                    intensity=self.intensity_var.get(),
                                    on_progress=prog)
        self._render_desc = desc
        self._render_running = True
        self._tick_render()
        threading.Thread(target=self._render_worker, args=(work, out), daemon=True).start()

    def _tick_render(self):
        if not getattr(self, "_render_running", False):
            return
        el = time.monotonic() - self.render_t0
        self.status_var.set(f"{self._render_desc}: ffmpeg is rendering ... "
                            f"({el:.0f}s) - big files can take a while, it's not stuck!")
        self.root.after(500, self._tick_render)

    def _render_worker(self, work, out):
        try:
            work()
            err = None
        except Exception as e:
            err = str(e)
        self.root.after(0, lambda: self._render_done(out, err))

    def _render_done(self, out, err):
        self._render_running = False
        if err:
            self.progress["value"] = 0
            self._console_write(f"!! render failed: {err}")
            self.status_var.set(f"{self._render_desc} failed: {err}")
            self._unlock_ui(enable_play=False)
            return
        self.progress["value"] = 1.0
        render_file_to_image(self.img_mix, out, MID_W, MID_H)
        size = os.path.getsize(out)
        self._console_write(f">> render complete :: {size:,} bytes :: fully playable")
        self.status_var.set(f"Done! Wrote {os.path.basename(out)} ({size:,} bytes) "
                            "- fully playable. Hit Play result!")
        self._unlock_ui(enable_play=True)
        self.done = True

    # ------------------------------------------------------------ playback
    def play_result(self):
        self.status_var.set("Health-checking the wreckage before trusting a "
                            "player with it ...")
        threading.Thread(target=self._play_auto_worker, daemon=True).start()

    def _play_auto_worker(self):
        # never hand byte soup to a player - that's how freezes happen
        if fm.probe_playable(self.out_path):
            self.root.after(0, lambda: self.status_var.set("Decodable! Playing it."))
            rc = subprocess.call(["mpv", "--really-quiet", self.out_path])
            if rc == 0:
                return
        self.root.after(0, lambda: self.status_var.set(
            "Too smashed to play safely - FORCING it to become a video "
            "(bytes as pixels + bytes as sound)..."))
        self._force_video_worker()

    def play_force_video(self):
        self.status_var.set("FORCING the bytes to become a real video: "
                            "pixels from the bytes, sound from the same bytes...")
        threading.Thread(target=self._force_video_worker, daemon=True).start()

    def _force_video_worker(self):
        forced = self.out_path + ".forced.mp4"
        try:
            fm.force_video(self.out_path, forced)
        except Exception as e:
            self.root.after(0, lambda: self.status_var.set(f"force-video failed: {e}"))
            return
        self.root.after(0, lambda: self.status_var.set(
            f"Forced! Playing {os.path.basename(forced)} - crazy video, as ordered."))
        subprocess.call(["mpv", "--really-quiet", forced])

    def play_raw_audio(self):
        self.status_var.set("Playing every byte as an audio sample (ffplay).")
        threading.Thread(target=self._raw_audio_worker, daemon=True).start()

    def _raw_audio_worker(self):
        # save the raw-sound take as a real WAV first, then play that
        saved = os.path.splitext(self.out_path)[0] + ".rawsound.wav"
        try:
            fm.force_audio(self.out_path, saved)
            self.root.after(0, lambda: (
                self._console_write(f">> raw sound saved as {os.path.basename(saved)}"),
                self.status_var.set(f"Raw sound SAVED as {os.path.basename(saved)} "
                                    "- playing it now.")))
        except Exception:
            saved = self.out_path  # can't save? still play the bytes directly
        fm.play_raw_audio(saved) if saved == self.out_path else             subprocess.call(["ffplay", "-hide_banner", "-loglevel", "error",
                             "-autoexit", saved])

    def play_raw_video(self):
        self.status_var.set("Playing the bytes as raw RGB pixels (ffplay).")
        threading.Thread(target=fm.play_raw_video, args=(self.out_path,), daemon=True).start()

    def open_folder(self):
        subprocess.Popen(["xdg-open", os.path.dirname(self.out_path)])


def main():
    root = tk.Tk()
    FileMixerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
