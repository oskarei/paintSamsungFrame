#!/usr/bin/env python3
"""push.py -- push a local image or URL to the Samsung Frame TV.

Usage:
    python push.py <local-path-or-url>

The image is centre-cropped to 16:9 if needed, then downscaled to 3840x2160.
The source must be large enough to fill 3840x2160 after that 16:9 crop --
smaller sources are rejected rather than upscaled.

Companion to paint.py (which runs the daily cron on the Pi). This script is
laptop-side and Gemini-free; it just sends a ready image to the Frame.
"""

import os
import io
import sys
import json
import time
import datetime
from pathlib import Path

import requests
from PIL import Image
from samsungtvws import SamsungTVWS

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
TV_IP        = os.environ.get("FRAME_TV_IP", "192.168.1.50")
FRAME_W      = 3840
FRAME_H      = 2160
TARGET_AR    = FRAME_W / FRAME_H

BASE_DIR     = Path(__file__).resolve().parent
TOKEN_FILE   = BASE_DIR / "token.txt"
STATE_FILE   = BASE_DIR / "manual_state.json"   # separate from paint.py's state.json


# ----------------------------------------------------------------------
# Input: load an image from a local path or an http(s) URL
# ----------------------------------------------------------------------
def load_image(arg: str) -> Image.Image:
    if arg.startswith("http://") or arg.startswith("https://"):
        print(f"Downloading {arg} ...")
        resp = requests.get(arg, timeout=60)
        resp.raise_for_status()
        return Image.open(io.BytesIO(resp.content))
    path = Path(arg).expanduser()
    if not path.is_file():
        sys.exit(f"File not found: {arg}")
    return Image.open(path)


# ----------------------------------------------------------------------
# Size check: reject sources too small to fill 3840x2160 after a 16:9 crop
# ----------------------------------------------------------------------
def check_size(img: Image.Image) -> None:
    w, h = img.size
    ar = w / h
    if abs(ar - TARGET_AR) < 1e-3:
        if w < FRAME_W or h < FRAME_H:
            sys.exit(f"Source {w}x{h} too small: need at least "
                     f"{FRAME_W}x{FRAME_H} for the Frame.")
        return
    if ar > TARGET_AR:
        # wider than 16:9 -- after cropping the sides, height is the limit
        if h < FRAME_H:
            need_w = round(FRAME_H * TARGET_AR)
            sys.exit(f"Source {w}x{h} too small: need at least "
                     f"{need_w}x{FRAME_H} after a 16:9 crop "
                     f"(height is the limit when the source is wider than 16:9).")
    else:
        # narrower than 16:9 -- after cropping top/bottom, width is the limit
        if w < FRAME_W:
            need_h = round(FRAME_W / TARGET_AR)
            sys.exit(f"Source {w}x{h} too small: need at least "
                     f"{FRAME_W}x{need_h} after a 16:9 crop "
                     f"(width is the limit when the source is narrower than 16:9).")


# ----------------------------------------------------------------------
# Centre-crop to 16:9 (no-op if already 16:9)
# ----------------------------------------------------------------------
def crop_to_16_9(img: Image.Image) -> Image.Image:
    w, h = img.size
    ar = w / h
    if abs(ar - TARGET_AR) < 1e-3:
        return img
    if ar > TARGET_AR:
        new_w = round(h * TARGET_AR)
        left = (w - new_w) // 2
        return img.crop((left, 0, left + new_w, h))
    new_h = round(w / TARGET_AR)
    top = (h - new_h) // 2
    return img.crop((0, top, w, top + new_h))


# ----------------------------------------------------------------------
# Encode as the same clean RGB JPEG paint.py sends to the Frame
# ----------------------------------------------------------------------
def to_frame_jpeg(img: Image.Image) -> bytes:
    img = img.convert("RGB")
    if img.size != (FRAME_W, FRAME_H):
        img = img.resize((FRAME_W, FRAME_H), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


# ----------------------------------------------------------------------
# Connect to the Frame (with retry; art mode wakes slowly)
# ----------------------------------------------------------------------
def connect_art(retries: int = 5, delay: int = 15):
    last = None
    for i in range(1, retries + 1):
        try:
            print(f"Connecting to Frame at {TV_IP} (attempt {i}/{retries})...")
            tv = SamsungTVWS(host=TV_IP, port=8002,
                             token_file=str(TOKEN_FILE), timeout=30)
            art = tv.art()
            art.get_current()
            print("Connection confirmed.")
            return art
        except Exception as e:
            last = e
            print(f"Not connected yet: {e}")
            if i < retries:
                time.sleep(delay)
    raise RuntimeError(f"Could not connect to Frame: {last}")


# ----------------------------------------------------------------------
# Upload + select, then delete the previous *manual* upload.
# State lives in manual_state.json so paint.py's state.json is untouched.
# ----------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def upload_and_show(art, img_bytes: bytes) -> None:
    new_id = art.upload(img_bytes, file_type="JPEG", matte="none")
    print(f"Uploaded -- content id: {new_id}")
    time.sleep(2)
    art.select_image(new_id, show=True)
    print("Switched to the new image.")

    state = load_state()
    prev = state.get("previous_id")
    if prev:
        try:
            art.delete(prev)
            print(f"Deleted previous manual upload: {prev}")
        except Exception as e:
            print(f"Could not delete {prev}: {e}", file=sys.stderr)

    state["previous_id"] = new_id
    state["updated"] = datetime.datetime.now().isoformat()
    save_state(state)


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------
def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"Usage: {argv[0]} <local-path-or-url>", file=sys.stderr)
        return 1

    img = load_image(argv[1])
    print(f"Loaded source: {img.size[0]}x{img.size[1]} (mode={img.mode})")
    check_size(img)

    cropped = crop_to_16_9(img)
    if cropped.size != img.size:
        print(f"Centre-cropped to 16:9: {cropped.size[0]}x{cropped.size[1]}")

    framed = to_frame_jpeg(cropped)
    print(f"Encoded JPEG for Frame: {len(framed)} bytes")

    art = connect_art()
    upload_and_show(art, framed)
    print("Done -- new image is on the TV.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
