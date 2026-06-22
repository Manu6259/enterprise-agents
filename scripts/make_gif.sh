#!/usr/bin/env bash
# Convert a screen recording (.mov/.mp4) into an optimized, looping GIF
# suitable for embedding in the README (GitHub renders inline, <100MB but
# aim for <5MB so it loads fast).
#
# Usage:
#   scripts/make_gif.sh <input.mov> [output.gif] [fps] [width]
#
# Examples:
#   scripts/make_gif.sh ~/Desktop/demo.mov
#   scripts/make_gif.sh ~/Desktop/demo.mov docs/demo.gif 12 1000
#
# Trim before converting (optional, keeps the GIF tight):
#   ffmpeg -i raw.mov -ss 2 -to 24 -c copy trimmed.mov
#   scripts/make_gif.sh trimmed.mov
#
# Uses ffmpeg's two-pass palette method (palettegen + paletteuse) for far
# better quality/size than a naive single-pass GIF.

set -euo pipefail

IN="${1:?Usage: make_gif.sh <input.mov> [output.gif] [fps] [width]}"
OUT="${2:-${IN%.*}.gif}"
FPS="${3:-12}"
WIDTH="${4:-1000}"
PALETTE="$(mktemp -t palette).png"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg not found. Install with: brew install ffmpeg" >&2
  exit 1
fi

echo "→ Pass 1/2: generating color palette ($FPS fps, ${WIDTH}px wide)…"
ffmpeg -loglevel error -i "$IN" \
  -vf "fps=${FPS},scale=${WIDTH}:-1:flags=lanczos,palettegen=stats_mode=diff" \
  -y "$PALETTE"

echo "→ Pass 2/2: encoding GIF…"
ffmpeg -loglevel error -i "$IN" -i "$PALETTE" \
  -lavfi "fps=${FPS},scale=${WIDTH}:-1:flags=lanczos,paletteuse=dither=bayer:bayer_scale=3" \
  -y "$OUT"

rm -f "$PALETTE"
SIZE=$(du -h "$OUT" | cut -f1)
echo "✓ Wrote $OUT ($SIZE)"
[ "$(du -k "$OUT" | cut -f1)" -gt 5120 ] && \
  echo "  ⚠ Over 5MB — re-run with lower fps/width, e.g.: make_gif.sh \"$IN\" \"$OUT\" 10 880"
echo "  (loops automatically; embed in README with: ![demo](${OUT}))"
