#!/usr/bin/env python3
"""FileMixer — literally smash the raw bytes of two OR MORE files together.

The mixing functions are generators that take a LIST of file paths and yield
chunks of the output file, so the GUI can watch the bytes stream in real time.

Source files are only ever opened read-only. Output is always a new file.
"""

import argparse
import os
import random
import shutil
import subprocess
import sys

import numpy as np

DEFAULT_CHUNK = 4096
DEFAULT_MAX_SIZE = 512 * 1024 * 1024  # refuse to write more than this

# every real byte-mashing mode; PAIRWISE ones combine overlapping bytes with
# math, the rest rearrange whole chunks. All of them take 2+ files.
MODES = ("interleave", "zipper", "splice", "shuffle", "stutter", "reverse",
         "sprinkle", "waltz", "drunk", "yeet", "sortstorm", "scream",
         "xor", "add", "subtract", "and", "or", "rotate", "blend")
PAIRWISE = {"xor", "add", "subtract", "and", "or", "rotate", "blend"}


# ---------------------------------------------------------------------------
# byte sources
# ---------------------------------------------------------------------------

class _Reader:
    """Read-only byte source; can loop back to the start at EOF."""

    def __init__(self, path, loop=False):
        self.f = open(path, "rb")
        self.loop = loop
        self.size = os.fstat(self.f.fileno()).st_size

    def read(self, n):
        data = self.f.read(n)
        if self.loop and self.size > 0:
            while len(data) < n:
                self.f.seek(0)
                more = self.f.read(n - len(data))
                if not more:
                    break
                data += more
        return data

    def close(self):
        self.f.close()


def _open_all(paths, pad):
    """Open every file; in loop mode all but the longest wrap around.
    Returns (readers, primary_index): the primary's exhaustion ends the mix."""
    sizes = [os.path.getsize(p) for p in paths]
    primary = sizes.index(max(sizes))
    readers = [_Reader(p, loop=(pad == "loop" and i != primary))
               for i, p in enumerate(paths)]
    return readers, primary


def _close_all(readers):
    for r in readers:
        r.close()


def expected_size(mode, paths, pad="stop"):
    """Predicted output size, for progress bars. Rough is fine."""
    sizes = [os.path.getsize(p) for p in paths]
    total, first, biggest = sum(sizes), sizes[0], max(sizes)
    if mode == "sprinkle":
        return first
    if mode == "stutter":
        return 2 * first
    if mode == "yeet":
        return int(total * 0.75)
    if mode == "drunk":
        return total
    if mode == "waltz":
        return 2 * first + (total - first)
    if mode in PAIRWISE:
        return biggest
    if pad == "loop":
        return len(paths) * biggest
    return total


# ---------------------------------------------------------------------------
# smash modes — generators over a list of paths, yielding output chunks
# ---------------------------------------------------------------------------

def _round_robin(readers, primary, pad, sizes_fn):
    """Shared loop: each turn, read one piece from every reader in order."""
    while True:
        chunks = [r.read(n) for r, n in zip(readers, sizes_fn())]
        if not any(chunks):
            return
        if pad == "loop" and not chunks[primary]:
            return
        for c in chunks:
            if c:
                yield c


def mix_interleave(paths, chunk=DEFAULT_CHUNK, pad="stop", **_):
    """A bite of each file in turn, round and round."""
    readers, primary = _open_all(paths, pad)
    try:
        yield from _round_robin(readers, primary, pad,
                                lambda: [chunk] * len(readers))
    finally:
        _close_all(readers)


def mix_splice(paths, pad="stop", seed=0, **_):
    """Randomly-sized bites of each file. Same seed, same mix."""
    rng = random.Random(seed)
    readers, primary = _open_all(paths, pad)
    try:
        yield from _round_robin(readers, primary, pad,
                                lambda: [rng.randint(256, 32768)
                                         for _ in readers])
    finally:
        _close_all(readers)


def mix_waltz(paths, chunk=DEFAULT_CHUNK, pad="stop", **_):
    """3/4 time: ONE two three, ONE two three — file 1 leads the dance with
    a double-length step, the others follow with quick little steps."""
    readers, primary = _open_all(paths, pad)
    try:
        yield from _round_robin(readers, primary, pad,
                                lambda: [chunk * 2] + [chunk] * (len(readers) - 1))
    finally:
        _close_all(readers)


def mix_zipper(paths, pad="stop", **_):
    """Alternate single bytes across every file: a b c a b c ..."""
    readers, primary = _open_all(paths, pad)
    block = 32768
    try:
        while True:
            chunks = [r.read(block) for r in readers]
            if not any(chunks):
                return
            if pad == "loop" and not chunks[primary]:
                return
            live = [c for c in chunks if c]
            n = len(live)
            m = min(len(c) for c in live)
            if m:
                out = bytearray(n * m)
                for i, c in enumerate(live):
                    out[i::n] = c[:m]
                yield bytes(out)
            for c in live:  # survivors past the shortest pass through
                if len(c) > m:
                    yield c[m:]
    finally:
        _close_all(readers)


def mix_shuffle(paths, chunk=DEFAULT_CHUNK, seed=0, **_):
    """Every file diced into chunks, ALL shuffled together, dealt back out."""
    pieces = []
    for p in paths:
        size = os.path.getsize(p)
        pieces += [(p, off, min(chunk, size - off)) for off in range(0, size, chunk)]
    random.Random(seed).shuffle(pieces)
    handles = {p: open(p, "rb") for p in paths}
    try:
        for p, off, ln in pieces:
            handles[p].seek(off)
            yield handles[p].read(ln)
    finally:
        for f in handles.values():
            f.close()


def mix_stutter(paths, chunk=DEFAULT_CHUNK, seed=0, **_):
    """File 1 plays through but chunks randomly st-st-stutter, while the
    other files barge in uninvited."""
    rng = random.Random(seed)
    ra = _Reader(paths[0])
    others = [_Reader(p, loop=True) for p in paths[1:]]
    try:
        while True:
            ca = ra.read(chunk)
            if not ca:
                return
            for _ in range(1 if rng.random() > 0.3 else rng.randint(2, 5)):
                yield ca
            if others and rng.random() < 0.15:
                cb = rng.choice(others).read(chunk)
                if cb:
                    yield cb
    finally:
        ra.close()
        _close_all(others)


def mix_reverse(paths, chunk=DEFAULT_CHUNK, **_):
    """File 1 plays forward while every other file is fed in BACKWARDS."""
    ra = _Reader(paths[0])
    backs = [(open(p, "rb"), os.path.getsize(p)) for p in paths[1:]]
    positions = [s for _, s in backs]
    try:
        while True:
            out = []
            ca = ra.read(chunk)
            if ca:
                out.append(ca)
            for i, (f, _) in enumerate(backs):
                take = min(chunk, positions[i])
                if take:
                    f.seek(positions[i] - take)
                    out.append(f.read(take)[::-1])
                    positions[i] -= take
            if not out:
                return
            yield from out
    finally:
        ra.close()
        for f, _ in backs:
            f.close()


def mix_sprinkle(paths, chunk=2048, intensity=5.0, seed=0, **_):
    """Keep file 1's size and layout, but overwrite a percentage of its
    chunks with bytes from the other files. Low intensity (1-5%) usually
    leaves media playable but glitchy."""
    rng = random.Random(seed)
    p = max(0.0, min(100.0, intensity)) / 100.0
    total = max(1, -(-os.path.getsize(paths[0]) // chunk))
    k = min(total, max(1, round(total * p))) if p > 0 else 0
    hits = set(rng.sample(range(total), k))
    ra = _Reader(paths[0])
    others = [_Reader(pth, loop=True) for pth in paths[1:]]
    try:
        for i in range(total):
            ca = ra.read(chunk)
            if not ca:
                return
            if i in hits and others:
                cb = others[i % len(others)].read(len(ca))
                yield cb if len(cb) == len(ca) else ca
            else:
                yield ca
    finally:
        ra.close()
        _close_all(others)


def mix_drunk(paths, chunk=DEFAULT_CHUNK, seed=0, **_):
    """Stumbles around ALL the files, grabbing random gulps from random
    spots until it has drunk roughly everything once."""
    rng = random.Random(seed)
    handles = [(open(p, "rb"), os.path.getsize(p)) for p in paths]
    budget = sum(s for _, s in handles)
    drunk = 0
    try:
        while drunk < budget:
            f, size = rng.choice(handles)
            if size == 0:
                drunk += 1
                continue
            gulp = rng.randint(chunk // 4, chunk * 4)
            f.seek(rng.randint(0, max(size - 1, 0)))
            data = f.read(gulp)
            if data:
                drunk += len(data)
                yield data
    finally:
        for f, _ in handles:
            f.close()


def mix_yeet(paths, chunk=DEFAULT_CHUNK, pad="stop", seed=0, **_):
    """Interleaves everything but randomly YEETS ~25% of the chunks into
    the void. What falls out is what you get."""
    rng = random.Random(seed)
    for c in mix_interleave(paths, chunk=chunk, pad=pad):
        if rng.random() < 0.25:
            continue  # yeet
        yield c


def mix_sortstorm(paths, chunk=DEFAULT_CHUNK, pad="stop", **_):
    """Takes each round of chunks and SORTS the bytes in it. Data becomes
    eerily tidy rainbow gradients. The most organized destruction possible."""
    buf = bytearray()
    for c in mix_interleave(paths, chunk=chunk, pad=pad):
        buf += c
        if len(buf) >= chunk * 4:
            yield np.sort(np.frombuffer(bytes(buf), dtype=np.uint8)).tobytes()
            buf.clear()
    if buf:
        yield np.sort(np.frombuffer(bytes(buf), dtype=np.uint8)).tobytes()


def mix_scream(paths, chunk=DEFAULT_CHUNK, pad="stop", **_):
    """Every byte pushed to its absolute extreme: 0 or 255, nothing in
    between. As sound it screams. As pixels it strobes. You were warned."""
    for c in mix_interleave(paths, chunk=chunk, pad=pad):
        a = np.frombuffer(c, dtype=np.uint8)
        yield np.where(a >= 128, 255, 0).astype(np.uint8).tobytes()


def _rotate_op(x, y):
    s = y % 8
    return ((x << s) | (x >> (8 - s))) & 0xFF


_PAIR_OPS = {
    "xor": lambda x, y: x ^ y,
    "add": lambda x, y: (x + y) & 0xFF,
    "subtract": lambda x, y: (x - y) & 0xFF,
    "and": lambda x, y: x & y,
    "or": lambda x, y: x | y,
    "rotate": _rotate_op,  # rotate bits left by the other file's byte
    "blend": lambda x, y: (x + y) // 2,  # smooth 50/50 byte smoothie
}


def _make_pairwise(op):
    """Fold the op across ALL files' overlapping bytes; when streams run
    out, the survivors pass through."""
    def mixer(paths, pad="stop", **_):
        readers, primary = _open_all(paths, pad)
        block = 65536
        bufs = [b"" for _ in readers]
        try:
            while True:
                for i, r in enumerate(readers):
                    if len(bufs[i]) < block:
                        bufs[i] += r.read(block - len(bufs[i]))
                live = [b for b in bufs if b]
                if not live:
                    return
                if pad == "loop" and not bufs[primary]:
                    return
                m = min(len(b) for b in live)
                arrs = [np.frombuffer(b[:m], dtype=np.uint8).astype(np.uint16)
                        for b in live]
                acc = arrs[0]
                for a in arrs[1:]:
                    acc = op(acc, a)
                yield acc.astype(np.uint8).tobytes()
                bufs = [b[m:] if b else b for b in bufs]
        finally:
            _close_all(readers)
    return mixer


MIXERS = {
    "interleave": mix_interleave,
    "zipper": mix_zipper,
    "splice": mix_splice,
    "shuffle": mix_shuffle,
    "stutter": mix_stutter,
    "reverse": mix_reverse,
    "sprinkle": mix_sprinkle,
    "waltz": mix_waltz,
    "drunk": mix_drunk,
    "yeet": mix_yeet,
    "sortstorm": mix_sortstorm,
    "scream": mix_scream,
    **{name: _make_pairwise(op) for name, op in _PAIR_OPS.items()},
}


def smash(paths, mode="interleave", chunk=DEFAULT_CHUNK, pad="stop",
          seed=0, keep_header=0, intensity=5.0, max_size=DEFAULT_MAX_SIZE):
    """Yield the chunks of the smashed file, capped at max_size bytes."""
    written = 0
    if keep_header > 0:
        with open(paths[0], "rb") as f:
            head = f.read(keep_header)
        written += len(head)
        yield head
    if mode == "sprinkle":
        chunk = min(chunk, 2048)  # finer grain = more scattered glitches
    for c in MIXERS[mode](paths, chunk=chunk, pad=pad, seed=seed,
                          intensity=intensity):
        if written + len(c) > max_size:
            c = c[: max_size - written]
            if c:
                yield c
            print(f"note: output capped at {max_size} bytes", file=sys.stderr)
            return
        written += len(c)
        yield c


def mp4_safe(out_path, first_file):
    """Video renders are written as H.264/AAC, so if the first file is a
    video, steer the output name to .mp4 no matter what A's extension was
    (a 'mosh_x.webm' would be an invalid container for them)."""
    if _probe_kind(first_file) == "video":
        return os.path.splitext(out_path)[0] + ".mp4"
    return out_path


def default_output_name(paths):
    names = [os.path.splitext(os.path.basename(p))[0] for p in paths[:3]]
    if len(paths) > 3:
        names.append(f"plus{len(paths) - 3}")
    ext = os.path.splitext(paths[0])[1] or ".bin"
    return "mashed_" + "_".join(names) + ext


def write_smash(paths, out_path, **kwargs):
    """Write the smashed file to disk. Returns bytes written."""
    written = 0
    with open(out_path, "wb") as out:
        for c in smash(paths, **kwargs):
            out.write(c)
            written += len(c)
    return written


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------

def _probe_kind(path):
    """'video', 'image', 'audio', or None — what ffmpeg sees in the file."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    kinds = [l.split(",")[0] for l in out.strip().splitlines() if l]
    if "video" in kinds and "audio" in kinds:
        # an mp3/flac with embedded cover art reports a video stream too —
        # if the only video is an attached picture, this is an audio file
        try:
            disp = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v",
                 "-show_entries", "stream_disposition=attached_pic",
                 "-of", "csv=p=0", path],
                capture_output=True, text=True, timeout=15).stdout
            if disp.strip() and all(x.strip() == "1"
                                    for x in disp.strip().splitlines()):
                return "audio"
        except (OSError, subprocess.TimeoutExpired):
            pass
    if "video" in kinds:
        # ffprobe calls still images "video" too (a PNG reports 25 fps!),
        # so use the container duration to tell them apart
        dur = _probe_duration(path)
        return "video" if dur and dur > 0.04 else "image"
    if "audio" in kinds:
        return "audio"
    return None


def probe_playable(path, timeout=10):
    """Quick health check: can ffmpeg actually open and decode this?
    Returns False for byte soup so players never get a chance to freeze."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return False
        # also decode a little to catch files with valid headers, dead bodies
        r = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-t", "2", "-i", path,
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _probe_duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path], capture_output=True, text=True, timeout=15).stdout
        return float(out.strip())
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def _probe_dims(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=15).stdout
        w, h = out.strip().split(",")[:2]
        return int(w), int(h)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def _ffmpeg(args, on_progress=None, duration=None):
    """Run ffmpeg quietly; raise RuntimeError with its stderr tail on failure.

    If on_progress and duration are given, ffmpeg's own progress feed is
    parsed and on_progress(fraction 0..1) is called as the render advances.
    """
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error"]
    if on_progress and duration:
        cmd += ["-progress", "pipe:1", "-nostats"]
    cmd += args
    if not (on_progress and duration):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            tail = (r.stderr or "").strip().splitlines()
            raise RuntimeError(tail[-1] if tail else f"ffmpeg exit code {r.returncode}")
        return
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    for line in proc.stdout:
        if line.startswith("out_time_us="):
            try:
                on_progress(min(int(line.split("=")[1]) / 1e6 / duration, 1.0))
            except ValueError:
                pass
        elif line.startswith("progress=end"):
            on_progress(1.0)
    proc.wait()
    if proc.returncode != 0:
        tail = (proc.stderr.read() or "").strip().splitlines()
        raise RuntimeError(tail[-1] if tail else f"ffmpeg exit code {proc.returncode}")


# ---------------------------------------------------------------------------
# FAKE ZONE — no byte smashing at all. ffmpeg re-renders file 1 with eerie
# effects whose strength/seed come from the other files' bytes, so the
# output always plays. Clearly labeled fake; real modes always available.
# ---------------------------------------------------------------------------

FAKE_PRESETS = {
    "backrooms": lambda i, s: (
        f"colorchannelmixer=rr=1.15:gg=1.05:bb=0.72,"
        f"eq=saturation=0.65:gamma=1.12:brightness=-0.06,"
        f"noise=alls={int(8 + i * 0.3)}:allf=t:all_seed={s},"
        f"vignette=PI/4,tmix=frames=3",
        f"lowpass=f=1100,aecho=0.8:0.7:60:0.4,vibrato=f=0.5:d=0.3"),
    "poolrooms": lambda i, s: (
        f"colorchannelmixer=rr=0.75:gg=1.0:bb=1.25,"
        f"eq=saturation=0.8:brightness=0.04,"
        f"noise=alls={int(5 + i * 0.2)}:allf=t:all_seed={s},"
        f"vignette=PI/5,tmix=frames=2",
        f"aecho=0.9:0.85:300|600:0.5|0.3,lowpass=f=1600,vibrato=f=0.8:d=0.35"),
    "vhs_nightmare": lambda i, s: (
        f"noise=alls={int(20 + i * 0.6)}:allf=t+u:all_seed={s},"
        f"rgbashift=rh={4 + int(i / 8)}:bv={3 + int(i / 10)},"
        f"eq=contrast=1.2:saturation=1.5,il=l=i:c=i",
        f"vibrato=f=6:d={min(0.4 + i / 150, 1):.2f},"
        f"acrusher=bits=6:mode=log:mix={min(0.3 + i / 150, 1):.2f}"),
    "datamosh_dream": lambda i, s: (
        f"tmix=frames={min(4 + int(i / 15), 12)},tblend=all_mode=grainextract,"
        f"eq=saturation={1.2 + i / 80:.2f}",
        f"aecho=0.9:0.9:500|1000:0.5|0.3"),
    "static_ghost": lambda i, s: (
        f"noise=alls={int(35 + i * 0.5)}:allf=t+u:all_seed={s},"
        f"eq=saturation=0.15:contrast=1.5,vignette",
        f"acrusher=bits=3:mode=log:mix={min(0.4 + i / 200, 1):.2f},highpass=f=700"),
    "corridor_echo": lambda i, s: (
        f"eq=brightness=-0.16:saturation=0.45,vignette=PI/3.5,tmix=frames=2",
        f"aecho=0.9:0.8:250|500|1000:0.6|0.4|0.2,lowpass=f=900,vibrato=f=1:d=0.4"),
    "the_void": lambda i, s: (
        f"eq=brightness=-0.3:saturation=0.1:contrast=1.3,vignette=PI/3,"
        f"noise=alls={int(4 + i * 0.15)}:allf=t:all_seed={s},tmix=frames=4",
        f"lowpass=f=400,aecho=0.95:0.9:800|1600:0.7|0.4,volume=0.8"),
}


def fake_glitch(paths, out_path, intensity=30.0, preset="backrooms",
                on_progress=None):
    """Render an eerie-looking (but fully playable) copy of the first file.
    The other files' bytes pick the flavour. Raises on failure."""
    path_a, extras = paths[0], paths[1:]
    seed = 1
    for p in extras:
        with open(p, "rb") as f:
            seed += sum(f.read(65536))
    seed %= 1000
    i = max(1.0, min(100.0, intensity))

    kind = _probe_kind(path_a)
    if kind is None:
        raise ValueError(
            "fake mode needs the first file to be a video, image or audio "
            "file (ffmpeg couldn't decode it)")

    vf, af = FAKE_PRESETS[preset](i, seed)

    if kind == "video":
        dur = _probe_duration(path_a)
        try:
            _ffmpeg(["-i", path_a, "-vf", vf, "-af", af,
                     "-c:v", "libx264", "-c:a", "aac",
                     "-preset", "veryfast", out_path],
                    on_progress=on_progress, duration=dur)
        except RuntimeError:  # e.g. video with no audio stream
            _ffmpeg(["-i", path_a, "-vf", vf, "-an", "-c:v", "libx264",
                     "-preset", "veryfast", out_path],
                    on_progress=on_progress, duration=dur)
    elif kind == "image":
        # temporal filters make no sense on a single frame
        vf = ",".join(t for t in vf.split(",")
                      if not t.startswith(("tmix", "tblend", "il=")))
        _ffmpeg(["-i", path_a, "-vf", vf, "-frames:v", "1", out_path])
    else:  # audio
        _ffmpeg(["-i", path_a, "-af", af, out_path])


# ---------------------------------------------------------------------------
# DATAMOSH — the "bad wifi" smear. Video codecs send full keyframes with only
# motion data in between; this re-encodes file 1 with regular keyframes, then
# DROPS the actual keyframe packets from the real stream (seeded by the other
# files' bytes). The decoder smears old frames forward with new motion.
# Real corruption of the real stream — and it still plays.
# ---------------------------------------------------------------------------

def datamosh(paths, out_path, intensity=60.0, seed=0, on_progress=None):
    a = paths[0]
    if _probe_kind(a) != "video":
        raise ValueError("datamosh needs the first file to be a video")
    for p in paths[1:]:
        with open(p, "rb") as f:
            seed += sum(f.read(65536))
    i = max(1.0, min(100.0, intensity))
    dur = _probe_duration(a)

    # more intensity = more frequent keyframes to drop = more smearing
    gop = max(8, int(48 - i / 3))
    p_drop = 0.35 + i / 155          # chance each later keyframe gets dropped
    tmp = out_path + ".gop.mp4"
    try:
        # step 1: re-encode with regular keyframes and no scene-cut extras
        _ffmpeg(["-i", a, "-c:v", "libx264", "-preset", "veryfast",
                 "-g", str(gop), "-sc_threshold", "0",
                 "-c:a", "aac", tmp],
                on_progress=(lambda f: on_progress(f * 0.8)) if on_progress else None,
                duration=dur)
        # step 2: yeet the keyframes (keep the very first so playback starts)
        expr = f"gt(n\\,0)*key*lt(random({seed % 1000})\\,{p_drop:.2f})"
        _ffmpeg(["-i", tmp, "-c", "copy", "-bsf:v", f"noise=drop={expr}",
                 out_path])
        if on_progress:
            on_progress(1.0)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# CURSE — the full ritual in one call: remix real content together, sprinkle
# raw byte shrapnel over it, then datamosh the survivors so the decoder's
# hallucinations get baked into real frames and smeared. Playable at the end.
# ---------------------------------------------------------------------------

def curse(paths, out_path, intensity=70.0, seed=0, on_progress=None):
    if _probe_kind(paths[0]) != "video":
        raise ValueError("the curse needs the first file to be a video")
    t1 = out_path + ".c1.mp4"
    t2 = out_path + ".c2.mp4"
    part = (lambda lo, hi: (lambda f: on_progress(lo + f * (hi - lo))))         if on_progress else (lambda lo, hi: None)
    try:
        # stage 1: remix everything into the video (real decoded content)
        remix(paths, t1, intensity=intensity, seed=seed,
              on_progress=part(0.0, 0.55))
        # stage 2: raw byte shrapnel, low dose so the decoder survives
        shrapnel = max(0.5, min(intensity / 25, 4.0))
        write_smash([t1] + paths[1:], t2, mode="sprinkle",
                    intensity=shrapnel, seed=seed)
        src = t2 if probe_playable(t2) else t1
        if on_progress:
            on_progress(0.6)
        # stage 3: datamosh — re-encoding bakes the shrapnel hallucinations
        # into real frames, then the keyframe drops smear them around
        try:
            datamosh([src] + paths[1:], out_path, intensity=intensity,
                     seed=seed, on_progress=part(0.6, 1.0))
        except RuntimeError:
            if src == t1:
                raise
            datamosh([t1] + paths[1:], out_path, intensity=intensity,
                     seed=seed, on_progress=part(0.6, 1.0))
    finally:
        for t in (t1, t2):
            try:
                os.remove(t)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# REMIX mode — real content from every file, but decoded first so the result
# always plays: chunks of the other files flash into file 1's video, patches
# paste onto its image, bursts blast into its audio.
# ---------------------------------------------------------------------------

def remix(paths, out_path, intensity=20.0, seed=0, on_progress=None):
    """Splice decoded pieces of every other file into file 1, one at a
    time (A+B -> tmp, tmp+C -> tmp2, ...). Always playable. Raises on failure."""
    if len(paths) < 2:
        raise ValueError("remix needs at least two files")
    current = paths[0]
    temps = []
    try:
        total = len(paths) - 1
        for n, other in enumerate(paths[1:]):
            is_last = n == total - 1
            target = out_path if is_last else out_path + f".step{n}" + \
                os.path.splitext(out_path)[1]
            cb = (lambda frac, n=n: on_progress((n + frac) / total)) \
                if on_progress else None
            _remix_pair(current, other, target,
                        intensity=intensity, seed=seed + n, on_progress=cb)
            if not is_last:
                temps.append(target)
            current = target
        if on_progress:
            on_progress(1.0)
    finally:
        for t in temps:
            try:
                os.remove(t)
            except OSError:
                pass


def _remix_pair(path_a, path_b, out_path, intensity, seed, on_progress=None):
    ka, kb = _probe_kind(path_a), _probe_kind(path_b)
    if ka is None:
        raise ValueError("remix needs file 1 to be a video, image or audio file")
    rng = random.Random(seed)
    i = max(1.0, min(100.0, intensity))

    if ka == "video" and kb in ("video", "image"):
        _remix_video_visual(path_a, path_b, kb, out_path, i, rng, on_progress)
    elif ka == "video" and kb == "audio":
        _remix_audio_bursts(path_a, path_b, "audio", out_path, i, rng, video=True)
    elif ka == "audio":
        bkind = "audio" if kb == "audio" else "rawbytes"
        _remix_audio_bursts(path_a, path_b, bkind, out_path, i, rng, video=False)
    elif ka == "image" and kb in ("video", "image"):
        _remix_image_patches(path_a, path_b, kb, out_path, i, rng)
    elif ka == "video" and kb is None:
        # undecodable partner: its raw bytes become audio bursts in the video
        _remix_audio_bursts(path_a, path_b, "rawbytes", out_path, i, rng, video=True)
    else:
        raise ValueError(f"remix can't put {kb or 'that file'} into an image "
                         "(tip: reorder the files, or use a real smash mode)")


def _remix_video_visual(a, b, kb, out, i, rng, on_progress=None):
    """Random chunks of B (photo or video) flash into A's video."""
    dur = _probe_duration(a)
    dims = _probe_dims(a)
    if not dur or not dims:
        raise ValueError("couldn't read the video's duration/size")
    aw, ah = dims
    flashes = min(36, max(2, int(2 + i / 4)))
    # few overlay branches, many time-windows each: 16 parallel branches of
    # a looped video buffer enough frames to get ffmpeg OOM-killed
    n = min(4, flashes)
    b_input = ["-loop", "1", "-i", b] if kb == "image" else ["-stream_loop", "-1", "-i", b]

    parts = [f"[1:v]split={n}" + "".join(f"[b{k}]" for k in range(n))]
    cur = "[0:v]"
    for k in range(n):
        frac = 0.25 + rng.random() * (0.35 + i / 150)
        sw = max(32, int(aw * frac)) // 2 * 2
        x = rng.randint(0, max(aw - sw, 1))
        y = rng.randint(0, max(ah // 3, 1))
        wins = []
        for _ in range(flashes // n + (1 if k < flashes % n else 0)):
            st = rng.random() * max(dur - 0.7, 0.1)
            d = 0.12 + rng.random() * (0.15 + i / 200)
            wins.append(f"between(t,{st:.2f},{st + d:.2f})")
        parts.append(f"[b{k}]scale={sw}:-2[s{k}]")
        parts.append(f"{cur}[s{k}]overlay={x}:{y}:eof_action=pass"
                     f":enable='{'+'.join(wins)}'[v{k}]")
        cur = f"[v{k}]"
    parts.append(f"{cur}format=yuv420p[vout]")
    args = (["-i", a] + b_input +
            ["-filter_complex", ";".join(parts), "-map", "[vout]",
             "-map", "0:a?", "-c:v", "libx264", "-c:a", "copy",
             "-preset", "veryfast", "-t", f"{dur:.3f}", out])
    try:
        _ffmpeg(args, on_progress=on_progress, duration=dur)
    except RuntimeError:  # some containers refuse audio copy — re-encode it
        args[args.index("copy")] = "aac"
        _ffmpeg(args, on_progress=on_progress, duration=dur)


def _remix_audio_bursts(a, b, bkind, out, i, rng, video):
    """Bursts of B's sound (or B's literal bytes as sound) blast into A."""
    dur = _probe_duration(a)
    if not dur:
        raise ValueError("couldn't read the file's duration")
    if bkind == "audio":
        b_input = ["-stream_loop", "-1", "-i", b]
    else:  # interpret B's raw bytes as 8-bit audio — literal data as sound
        b_input = ["-f", "u8", "-ar", "44100", "-stream_loop", "-1", "-i", b]
    period = max(0.6, dur / (2 + i / 5))
    burst = min(period * 0.8, 0.15 + i / 80)
    phase = rng.random() * period
    gate = (f"[1:a]volume='if(lt(mod(t+{phase:.2f},{period:.2f}),{burst:.2f}),"
            f"{0.5 + i / 120:.2f},0)':eval=frame[g]")
    mix = f"{gate};[0:a][g]amix=inputs=2:duration=first:normalize=0[aout]"
    if video:
        args = (["-i", a] + b_input +
                ["-filter_complex", mix, "-map", "0:v", "-c:v", "copy",
                 "-map", "[aout]", "-t", f"{dur:.3f}", out])
        try:
            _ffmpeg(args)
        except RuntimeError:  # A has no audio track — B's bursts become the track
            _ffmpeg(["-i", a] + b_input +
                    ["-filter_complex", gate, "-map", "0:v", "-c:v", "copy",
                     "-map", "[g]", "-t", f"{dur:.3f}", out])
    else:
        _ffmpeg(["-i", a] + b_input +
                ["-filter_complex", mix, "-map", "[aout]", "-t", f"{dur:.3f}", out])


def _remix_image_patches(a, b, kb, out, i, rng):
    """Random rectangles of B pasted all over A's image."""
    da, db = _probe_dims(a), _probe_dims(b)
    if not da or not db:
        raise ValueError("couldn't read image sizes")
    aw, ah = da
    bw, bh = db
    n = min(20, max(2, int(2 + i / 5)))
    b_input = ["-i", b] if kb == "image" else \
              ["-ss", f"{rng.random() * (_probe_duration(b) or 1) * 0.9:.2f}", "-i", b]
    parts = [f"[1:v]split={n}" + "".join(f"[b{k}]" for k in range(n))]
    cur = "[0:v]"
    for k in range(n):
        cw = max(8, int(bw * (0.1 + rng.random() * 0.35)))
        ch = max(8, int(bh * (0.1 + rng.random() * 0.35)))
        cx, cy = rng.randint(0, bw - cw), rng.randint(0, bh - ch)
        x, y = rng.randint(0, max(aw - cw, 1)), rng.randint(0, max(ah - ch, 1))
        parts.append(f"[b{k}]crop={cw}:{ch}:{cx}:{cy}[c{k}]")
        parts.append(f"{cur}[c{k}]overlay={x}:{y}[v{k}]")
        cur = f"[v{k}]"
    parts[-1] = parts[-1].replace(f"[v{n-1}]", "[vout]")
    _ffmpeg(["-i", a] + b_input +
            ["-filter_complex", ";".join(parts), "-map", "[vout]",
             "-frames:v", "1", out])


# ---------------------------------------------------------------------------
# playback
# ---------------------------------------------------------------------------

def play_raw_audio(path, rate=8000):
    """Interpret every byte as an 8-bit audio sample. Any file becomes sound."""
    return subprocess.call(
        ["ffplay", "-hide_banner", "-loglevel", "error", "-autoexit",
         "-f", "u8", "-ar", str(rate), "-ch_layout", "mono", path])


def play_raw_video(path, size="640x360"):
    """Interpret bytes as raw RGB pixels — the visualiser-in-reverse.

    Small files can't fill a big frame (one 640x360 RGB frame is ~690 KB),
    so step down until the file holds at least a couple of frames.
    """
    fsize = os.path.getsize(path)
    for candidate in (size, "320x180", "160x90", "80x45", "40x22"):
        w, h = (int(v) for v in candidate.split("x"))
        if fsize >= 2 * w * h * 3:
            size = candidate
            break
    else:
        size = "40x22"
    return subprocess.call(
        ["ffplay", "-hide_banner", "-loglevel", "error", "-autoexit",
         "-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", size,
         "-framerate", "10", path])


def force_audio(path, out_path, rate=8000):
    """Save the raw-bytes-as-sound interpretation as a real WAV file —
    exactly what play_raw_audio plays, but on disk forever."""
    _ffmpeg(["-f", "u8", "-ar", str(rate), "-i", path, out_path])
    return out_path


def force_video(path, out_path, seconds=30):
    """FORCE any byte soup to become a real, playable video: the file's
    bytes are read as raw RGB pixels for the picture AND as raw 8-bit
    samples for the sound, then encoded into a normal mp4."""
    fsize = os.path.getsize(path)
    # heights must stay even — libx264 refuses odd dimensions
    for candidate in ("480x270", "320x180", "160x90", "80x44", "40x22"):
        w, h = (int(v) for v in candidate.split("x"))
        if fsize >= 12 * w * h * 3:
            size = candidate
            break
    else:
        size = "40x22"
    _ffmpeg(["-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", size,
             "-framerate", "12", "-i", path,
             "-f", "u8", "-ar", "8000", "-i", path,
             "-t", str(seconds), "-shortest", "-pix_fmt", "yuv420p", out_path])
    return out_path


def play_auto(path, rate=8000):
    """Health-check first so a frozen player can never happen: if the file
    isn't cleanly decodable, FORCE it to become a video and play that."""
    if probe_playable(path) and shutil.which("mpv"):
        rc = subprocess.call(["mpv", "--really-quiet", path])
        if rc == 0:
            return 0
    print("too smashed to trust a player with it - "
          "FORCING it to become a video...")
    forced = path + ".forced.mp4"
    force_video(path, forced)
    return subprocess.call(["mpv", "--really-quiet", forced])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(
        description="Smash the raw bytes of two or more files together and "
                    "play the wreckage.")
    p.add_argument("files", nargs="+", help="two or more files to smash")
    p.add_argument("-m", "--mode", choices=MODES + ("remix", "datamosh", "curse"),
                   default="interleave",
                   help="remix = decode the files and splice the others' content "
                        "into the first (always playable); everything else is a "
                        "real byte smash")
    p.add_argument("-o", "--output", help="output file (default: mashed_<names>.<ext>)")
    p.add_argument("--chunk", type=int, default=DEFAULT_CHUNK,
                   help="chunk size for chunk-based modes (default 4096)")
    p.add_argument("--pad", choices=("stop", "loop"), default="stop",
                   help="when a file runs out: stop mixing it, or loop it")
    p.add_argument("--seed", type=int, default=0, help="seed for the random modes")
    p.add_argument("-i", "--intensity", type=float, default=5.0, metavar="PCT",
                   help="sprinkle/remix/fake: how hard to hit, 0-100%% "
                        "(default 5; try 1 for a still-playable video)")
    p.add_argument("--fake", action="store_true",
                   help="FAKE ZONE: no byte smashing - ffmpeg re-renders the first "
                        "file with eerie effects seeded by the others; always plays")
    p.add_argument("--preset", choices=tuple(FAKE_PRESETS), default="backrooms",
                   help="which FAKE ZONE preset to use (default backrooms)")
    p.add_argument("--keep-header", type=int, default=0, metavar="N",
                   help="cheat: copy the first file's first N bytes untouched so "
                        "players recognise the format (default 0 = pure smash)")
    p.add_argument("--max-size", type=int, default=DEFAULT_MAX_SIZE)
    p.add_argument("--play", choices=("auto", "raw-audio", "raw-video",
                                      "force-video", "none"), default="auto")
    p.add_argument("--no-play", dest="play", action="store_const", const="none")
    p.add_argument("--rate", type=int, default=8000, help="raw-audio sample rate")
    p.add_argument("--size", default="640x360", help="raw-video frame size WxH")
    p.add_argument("-f", "--force", action="store_true", help="overwrite output file")
    args = p.parse_args(argv)

    if len(args.files) < 2:
        p.error("need at least two files to smash together")
    for f in args.files:
        if not os.path.isfile(f):
            p.error(f"not a file: {f}")

    out = args.output or default_output_name(args.files)
    if not args.output and (args.fake or args.mode in ("remix", "datamosh")):
        out = mp4_safe(out, args.files[0])
    if os.path.exists(out) and not args.force:
        p.error(f"{out} already exists (use --force to overwrite)")

    if args.fake:
        try:
            fake_glitch(args.files, out, intensity=args.intensity,
                        preset=args.preset)
        except (ValueError, RuntimeError) as e:
            print(f"FAKE ZONE failed: {e}", file=sys.stderr)
            return 1
        print(f"FAKE '{args.preset}' render of {args.files[0]} -> {out}  "
              f"({os.path.getsize(out):,} bytes - always playable, no real smashing)")
    elif args.mode == "curse":
        try:
            curse(args.files, out, intensity=args.intensity, seed=args.seed)
        except (ValueError, RuntimeError) as e:
            print(f"the curse failed: {e}", file=sys.stderr)
            return 1
        print(f"CURSED {args.files[0]} with {len(args.files) - 1} offering(s) -> {out}  "
              f"({os.path.getsize(out):,} bytes - remixed, shrapneled, moshed)")
    elif args.mode == "datamosh":
        try:
            datamosh(args.files, out, intensity=args.intensity, seed=args.seed)
        except (ValueError, RuntimeError) as e:
            print(f"datamosh failed: {e}", file=sys.stderr)
            return 1
        print(f"datamoshed {args.files[0]} -> {out}  "
              f"({os.path.getsize(out):,} bytes - keyframes yeeted, smears ahead)")
    elif args.mode == "remix":
        try:
            remix(args.files, out, intensity=args.intensity, seed=args.seed)
        except (ValueError, RuntimeError) as e:
            print(f"remix failed: {e}", file=sys.stderr)
            return 1
        print(f"remixed {len(args.files) - 1} file(s) INTO {args.files[0]} -> {out}  "
              f"({os.path.getsize(out):,} bytes - real content, always playable)")
    else:
        n = write_smash(args.files, out,
                        mode=args.mode, chunk=args.chunk, pad=args.pad,
                        seed=args.seed, keep_header=args.keep_header,
                        intensity=args.intensity, max_size=args.max_size)
        print(f"smashed {' + '.join(args.files)} -> {out}  "
              f"({n:,} bytes, mode={args.mode})")

    if args.play == "auto":
        play_auto(out, args.rate)
    elif args.play == "raw-audio":
        play_raw_audio(out, args.rate)
    elif args.play == "raw-video":
        play_raw_video(out, args.size)
    elif args.play == "force-video":
        forced = force_video(out, out + ".forced.mp4")
        subprocess.call(["mpv", "--really-quiet", forced])
    return 0


if __name__ == "__main__":
    sys.exit(main())
