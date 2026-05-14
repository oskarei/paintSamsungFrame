#!/usr/bin/env python3
"""
daily_cat_art.py — Generates a new painting of the pet every day and pushes it
to a Samsung Frame TV. Uses the Gemini API for everything.

Flow:
  1. recent_scenes() reads the last 14 archived scenes (environment + activity
     only — the pet itself is never part of this)
  2. Gemini (text) invents a fresh random environment + activity + art direction,
     explicitly told to differ from those recent scenes
  3. The scene is combined with the petDescription file into a painting prompt
  4. Gemini (Nano Banana Pro) generates the image natively at 4K, 16:9
  5. The image is normalized to a clean 3840x2160 JPEG for the Frame
  6. (optional) The painting is uploaded to Contentstack as an asset
  7. samsung-tv-ws-api uploads it to the TV and switches to it
  8. The painting is archived, the archive is pruned to the most recent
     ARCHIVE_KEEP days, and yesterday's Frame image is deleted

Both Gemini calls retry with backoff on transient server errors (5xx / 429).

Intended to run daily via cron.

Requires: pip install google-genai pillow requests   (Python 3.11+)
The pet is described in a plain-text file named 'petDescription' in this folder.
"""

import os
import io
import sys
import json
import time
import base64
import logging
import datetime
from pathlib import Path

import requests
from google import genai
from google.genai import types
from google.genai import errors as genai_errors
from PIL import Image
from samsungtvws import SamsungTVWS

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
TV_IP          = os.environ.get("FRAME_TV_IP", "192.168.1.50")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")     # must be set
TEXT_MODEL     = "gemini-2.5-flash"
IMAGE_MODEL    = "gemini-3-pro-image-preview"   # Nano Banana Pro — supports 4K
ASPECT_RATIO   = "16:9"
IMAGE_SIZE     = "4K"                            # uppercase K is required
RECENT_COUNT   = 14                              # scenes to look back on / avoid
ARCHIVE_KEEP   = 90                              # days of paintings to keep on disk

# Optional: also upload each painting to Contentstack as an asset.
# Set this to False to skip the Contentstack step entirely.
uploadToContentstack = True
CS_API_KEY           = os.environ.get("CS_API_KEY")            # Contentstack API key
CS_MANAGEMENT_TOKEN  = os.environ.get("CS_MANAGEMENT_TOKEN")   # Management token
CS_API_BASE          = "https://eu-api.contentstack.com"      # EU region CMA
csFolder             = "blt3b4e39bb29abe0a0"                  # Contentstack folder uid
csTag                = "jessethecat"                          # tag applied to each asset
CS_DESCRIPTION_MAX   = 1000                                   # CS asset description char limit

BASE_DIR      = Path(__file__).resolve().parent
ARCHIVE       = BASE_DIR / "archive"
STATE_FILE    = BASE_DIR / "state.json"
LOG_FILE      = BASE_DIR / "daily_cat_art.log"
TOKEN_FILE    = BASE_DIR / "token.txt"
PET_DESC_FILE = BASE_DIR / "petDescription"      # plain-text pet description

# HTTP status codes worth retrying — transient server / rate-limit errors.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# ----------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger("pet-art")

client = genai.Client(api_key=GEMINI_API_KEY)


# ----------------------------------------------------------------------
# Retry helper for Gemini calls — survives transient 5xx / 429 errors.
# Non-retryable errors (auth, bad request) are raised immediately.
# ----------------------------------------------------------------------
def call_with_retry(fn, *, what: str, retries: int = 5, delay: int = 20):
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except genai_errors.APIError as e:
            code = getattr(e, "code", None)
            if code not in RETRYABLE_STATUS:
                raise
            last_err = e
            log.warning("%s: transient error %s (attempt %d/%d)",
                        what, code, attempt, retries)
            if attempt < retries:
                time.sleep(delay * attempt)      # linear backoff: 20s, 40s, ...
    raise RuntimeError(f"{what}: gave up after {retries} attempts: {last_err}")


# ----------------------------------------------------------------------
# Inputs: pet description + recent scene history
# ----------------------------------------------------------------------
def load_pet_description() -> str:
    if not PET_DESC_FILE.exists():
        raise FileNotFoundError(f"Pet description file not found: {PET_DESC_FILE}")
    text = PET_DESC_FILE.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"Pet description file is empty: {PET_DESC_FILE}")
    return text


def recent_scenes(n: int = RECENT_COUNT) -> list[dict]:
    """Return the last n scenes as {environment, activity} — pet is skipped."""
    scenes = []
    for f in sorted(ARCHIVE.glob("*.json"))[-n:]:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            scene = data.get("scene", {})
            if scene.get("environment") and scene.get("activity"):
                scenes.append({
                    "environment": scene["environment"],
                    "activity": scene["activity"],
                })
        except Exception as e:
            log.warning("Could not read archive file %s: %s", f.name, e)
    return scenes


def format_recent(recent: list[dict]) -> str:
    if not recent:
        return "(none yet -- this is one of the first paintings)"
    return "\n".join(
        f'{i}. Setting: {s["environment"]} | Pet was: {s["activity"]}'
        for i, s in enumerate(recent, 1)
    )


def prune_archive(keep: int = ARCHIVE_KEEP) -> None:
    """Keep only the most recent `keep` archive files, delete the rest.

    Filenames are ISO dates, so a plain sort is chronological. Images (.jpg)
    are the disk hogs and use `keep` directly. The tiny .json files are needed
    by recent_scenes() for de-duplication, so they are never pruned below
    RECENT_COUNT, regardless of how low `keep` is set.
    """
    plans = [("*.jpg", keep), ("*.json", max(keep, RECENT_COUNT))]
    for pattern, limit in plans:
        files = sorted(ARCHIVE.glob(pattern))
        for f in files[:-limit] if len(files) > limit else []:
            try:
                f.unlink()
                log.info("Pruned old archive file: %s", f.name)
            except Exception as e:
                log.warning("Could not prune %s: %s", f.name, e)


# ----------------------------------------------------------------------
# Stage 1 — Gemini invents a random environment + activity + art direction.
# The pet is deliberately NOT mentioned here, so the scene history stays
# reusable even if the pet changes later.
# ----------------------------------------------------------------------
def generate_scene(recent: list[dict]) -> dict:
    prompt = f"""You are an art director planning a daily series of paintings of
a domestic pet. Each day you invent a fresh SCENE for the painting -- but you
never describe the pet itself, only its surroundings and what it is doing.

Invent ONE scene for today. It must be clearly distinct from the recent scenes
listed below: a different environment, a different activity, a different time
of day and overall feel.

Recent scenes to avoid repeating:
{format_recent(recent)}

Vary widely. Scenes may be mundane (a sunlit kitchen floor) or fantastical
(the deck of an airship above the clouds). Choose a tasteful, gallery-worthy
art style and vary that too -- e.g. Studio Ghibli watercolour, Hammershoi
interior, Hokusai woodblock, Sargent oil portrait, Klimt gold leaf, Kjarval
Icelandic landscape, art nouveau poster. Avoid generic 'digital art'.

Respond with a JSON object containing exactly these keys:
  "environment" : one vivid sentence describing the setting/surroundings
  "activity"    : one short phrase describing what the pet is doing
  "art_style"   : the specific artistic style and medium
  "mood"        : two or three mood words
  "palette"     : a short colour-palette description
"""
    response = call_with_retry(
        lambda: client.models.generate_content(
            model=TEXT_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
            ),
        ),
        what="Scene generation",
    )
    scene = json.loads(response.text)
    log.info("Scene: %s", scene)
    return scene


# ----------------------------------------------------------------------
# Stage 2 — pure string assembly: combine the scene with the pet description.
# No second API call needed.
# ----------------------------------------------------------------------
def build_image_prompt(scene: dict, pet_description: str) -> str:
    return f"""{scene['art_style']} painting of a domestic pet.

The pet (keep faithful to this exact description):
{pet_description}

Setting: {scene['environment']}
The pet is: {scene['activity']}
Mood: {scene['mood']}.
Colour palette: {scene['palette']}.

Composition: gallery-quality, 16:9 landscape orientation, the pet as the clear
focal point, balanced negative space. No text, no captions, no signatures,
no watermark, no border or frame within the image.
""".strip()


# ----------------------------------------------------------------------
# Gemini image (Nano Banana Pro): generate the image at native 4K
# ----------------------------------------------------------------------
def generate_image(prompt: str) -> Image.Image:
    response = call_with_retry(
        lambda: client.models.generate_content(
            model=IMAGE_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_modalities=["TEXT", "IMAGE"],
                image_config=types.ImageConfig(
                    aspect_ratio=ASPECT_RATIO,
                    image_size=IMAGE_SIZE,
                ),
            ),
        ),
        what="Image generation",
    )
    for part in response.parts:
        inline = getattr(part, "inline_data", None)
        if inline is not None and inline.data:
            data = inline.data
            if isinstance(data, str):          # some SDK versions return base64
                data = base64.b64decode(data)
            return Image.open(io.BytesIO(data))
    raise RuntimeError("Gemini returned no image")


# ----------------------------------------------------------------------
# Normalize for the Frame: clean RGB JPEG at 3840x2160 (no cropping)
# ----------------------------------------------------------------------
def normalize_for_frame(img: Image.Image) -> bytes:
    img = img.convert("RGB")
    # 4K at 16:9 comes out around 4096x2304; this is a clean 16:9 -> 16:9
    # downscale to the Frame's native resolution, never a crop.
    if img.size != (3840, 2160):
        img = img.resize((3840, 2160), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


# ----------------------------------------------------------------------
# Optional: upload the painting to Contentstack as an asset.
# Mirrors the proven createAsset() form from the contentstack-cloner:
# no Content-Type header (requests sets the multipart boundary), and tags
# passed as a list so requests repeats the field, which is what CS expects.
# ----------------------------------------------------------------------
def trim_for_cs(text: str, limit: int = CS_DESCRIPTION_MAX) -> str:
    """Trim text to fit Contentstack's asset description limit, on a word boundary."""
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip()


def upload_to_contentstack(img_bytes: bytes, prompt: str, today: str) -> None:
    if not CS_API_KEY or not CS_MANAGEMENT_TOKEN:
        log.warning("Contentstack upload skipped: CS_API_KEY / "
                    "CS_MANAGEMENT_TOKEN not set in the environment.")
        return

    url = f"{CS_API_BASE}/v3/assets"
    headers = {
        "api_key": CS_API_KEY,
        "authorization": CS_MANAGEMENT_TOKEN,
        # no Content-Type — requests sets the multipart boundary itself
    }
    files = {
        "asset[upload]": (f"{today}.jpg", img_bytes, "image/jpeg"),
    }
    data = {
        "asset[parent_uid]": csFolder,                  # place it in the folder
        "asset[title]": f"Painting {today}",
        "asset[description]": trim_for_cs(prompt),      # trimmed to <= 1000 chars
        "asset[tags]": [csTag],                         # list -> repeated field, as CS expects
    }
    resp = requests.post(url, headers=headers, files=files, data=data, timeout=60)
    if not resp.ok:
        # surface Contentstack's actual error message, not just the status code
        raise RuntimeError(f"Contentstack {resp.status_code}: {resp.text}")
    asset = resp.json().get("asset", {})
    log.info("Uploaded to Contentstack -- asset uid: %s", asset.get("uid"))


# ----------------------------------------------------------------------
# Connect to the Frame (with retry — art mode may need time to wake)
# ----------------------------------------------------------------------
def connect_art(retries: int = 5, delay: int = 15):
    last_err = None
    for i in range(1, retries + 1):
        try:
            log.info("Connecting to Frame (attempt %d/%d)...", i, retries)
            tv = SamsungTVWS(host=TV_IP, port=8002,
                             token_file=str(TOKEN_FILE), timeout=30)
            art = tv.art()
            art.get_current()      # a real art call confirms the connection
            log.info("Connection to Frame confirmed.")
            return art
        except Exception as e:
            last_err = e
            log.warning("Not connected yet: %s", e)
            if i < retries:
                time.sleep(delay)
    raise RuntimeError(f"Could not connect to Frame: {last_err}")


# ----------------------------------------------------------------------
# Upload + switch to it + delete yesterday's image
# ----------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def upload_and_show(art, img_bytes: bytes) -> None:
    new_id = art.upload(img_bytes, file_type="JPEG", matte="none")
    log.info("Uploaded -- content id: %s", new_id)
    time.sleep(2)
    art.select_image(new_id, show=True)
    log.info("Switched to the new image.")

    state = load_state()
    prev_id = state.get("previous_id")
    if prev_id:
        try:
            art.delete(prev_id)
            log.info("Deleted yesterday's image: %s", prev_id)
        except Exception as e:
            log.warning("Could not delete %s: %s", prev_id, e)

    state["previous_id"] = new_id
    state["updated"] = datetime.datetime.now().isoformat()
    save_state(state)


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------
def main() -> int:
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY is not set in the environment.")
        return 1

    ARCHIVE.mkdir(exist_ok=True)
    today = datetime.date.today().isoformat()

    try:
        pet_description = load_pet_description()
        recent = recent_scenes()
        log.info("Loaded pet description and %d recent scene(s).", len(recent))

        scene = generate_scene(recent)
        prompt = build_image_prompt(scene, pet_description)

        log.info("Generating image...")
        img = generate_image(prompt)
        framed = normalize_for_frame(img)

        # archive — store the scene so recent_scenes() can read it back,
        # plus the full prompt for reproducibility
        (ARCHIVE / f"{today}.jpg").write_bytes(framed)
        (ARCHIVE / f"{today}.json").write_text(
            json.dumps({"date": today, "scene": scene, "prompt": prompt},
                       indent=2, ensure_ascii=False)
        )
        log.info("Saved to archive/%s.jpg", today)

        # keep the archive folder from growing forever
        prune_archive()

        # optional Contentstack upload — non-fatal, must not block the TV update
        if uploadToContentstack:
            try:
                upload_to_contentstack(framed, prompt, today)
            except Exception as e:
                log.warning("Contentstack upload failed (non-fatal): %s", e)
        else:
            log.info("Contentstack upload disabled (uploadToContentstack = False).")

        art = connect_art()
        upload_and_show(art, framed)
        log.info("Done -- today's painting is on the TV.")
        return 0

    except Exception as e:
        log.exception("Run failed: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
