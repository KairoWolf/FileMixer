#!/bin/bash
# Generate small, safe test media to smash: a test-pattern video,
# a sine-wave tone, and a picture.
set -e
cd "$(dirname "$0")/.."
mkdir -p testfiles
ffmpeg -y -loglevel error -f lavfi -i testsrc=duration=3:size=320x240:rate=15 \
       -f lavfi -i sine=frequency=440:duration=3 -shortest testfiles/test.mp4
ffmpeg -y -loglevel error -f lavfi -i sine=frequency=440:duration=3 testfiles/tone.wav
ffmpeg -y -loglevel error -f lavfi -i testsrc=duration=0.1:size=320x240:rate=1 \
       -frames:v 1 testfiles/pic.png
echo "testfiles/ ready: test.mp4, tone.wav, pic.png — go smash them"
