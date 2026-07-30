# The Abomination — a worked example

![preview](img/abomination_preview.gif)

**Full video (with cursed audio):** [the_abomination.mp4](the_abomination.mp4)

This is what FileMixer produces when you commit to the bit. Made from five
files: the HEYYEYAA meme video, *Go Kitty Go!*, a random JPEG, *Swanee* (an
mp3), and a novelty TERMS OF SERVICE PDF.

## Exactly how it was made

Three passes, each output feeding the next. All commands run from the repo
folder with the source files in a `pile/` folder.

### Pass 1 — remix: splice real content into the video

```bash
python3 filemixer.py pile/HEYYEYAA.webm "pile/Go Kitty Go !.webm" \
    pile/1083521.jpg pile/Swanee.mp3 "pile/TERMS OF SERVICE.pdf" \
    -m remix -i 85 -o stage1.mp4 --no-play
```

`remix` decodes everything and splices it into file 1, one file at a time:

- chunks of the *Go Kitty Go!* video flash into the frame at random times
  and positions
- the JPEG does the same
- the mp3 blasts in as gated audio bursts
- the PDF can't be decoded as media at all, so its **literal raw bytes**
  are interpreted as 8-bit audio and mixed in as bursts of digital noise

(Historical note: the original render predates a fix, so the mp3's embedded
album *cover art* got flash-overlaid as if it were a video. Today's code
treats an mp3 as audio — to reproduce the cover-art flashes, extract the art
and pass it as its own image file.)

### Pass 2 — sprinkle: raw byte shrapnel

```bash
python3 filemixer.py stage1.mp4 pile/1083521.jpg \
    -m sprinkle -i 2 --seed 7 -o stage2.mp4 --no-play
```

`sprinkle` keeps the file's exact size and layout but overwrites 2% of its
2 KB chunks with the JPEG's raw bytes. The dose is deliberately small: big
enough that the video decoder hallucinates (wrong colors, smeared blocks),
small enough that it keeps decoding.

### Pass 3 — datamosh: the bad-wifi smear

```bash
python3 filemixer.py stage2.mp4 "pile/Go Kitty Go !.webm" \
    -m datamosh -i 90 --seed 13 -o the_abomination.mp4 --no-play
```

`datamosh` first **re-encodes** the video — this is the secret ingredient:
the decoder's hallucinations from pass 2 get baked into real, permanent
frames. Then it deletes almost every keyframe packet from the fresh stream
(intensity 90 ≈ 93% of keyframes die), so the player smears whatever is on
screen forward with the next motion vectors. Everything melts into
everything. The file still plays start to finish.

## The one-command version

This whole ritual is now a built-in mode:

```bash
python3 filemixer.py pile/HEYYEYAA.webm "pile/Go Kitty Go !.webm" \
    pile/1083521.jpg pile/Swanee.mp3 "pile/TERMS OF SERVICE.pdf" \
    -m curse -i 85
```

or in the GUI: pile up the files, pick `curse`, crank Intensity, SMASH.
