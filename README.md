# paintSamsungFrame

> A Raspberry Pi paints a new portrait of my cat every night, then hangs it on a Samsung Frame TV before I wake up. Yes, a literal pet project.

The Samsung Frame is a TV that pretends to be a painting when you're not watching it. You can load static images, but they get stale fast. So why not let a generative model paint a fresh one every day? That's what this is.

If you've got a Frame, an always-on computer (Raspberry Pi, NAS, an old laptop, whatever), a Gemini API key, and a pet to immortalise — you can run this too.

---

## What it does, end to end

Once a day (cron, on the Pi):

1. **Pick an art style.** Random pick from `artStyles` — a curated list of ~60 signature styles. Hokusai woodblock one day, Sargent oil portrait the next, Hopper urban realism after that.
2. **Invent a scene.** Gemini Flash (`gemini-2.5-flash`) writes an environment + activity + mood + palette that suits the chosen style. It's explicitly told to differ from the last 14 paintings so the gallery doesn't loop.
3. **Build the prompt.** Plain Python templating — your pet description gets stitched into the scene. No second LLM call here; that would just be slow and expensive.
4. **Paint it.** Gemini 3 Pro Image (a.k.a. Nano Banana Pro, `gemini-3-pro-image-preview`) renders the image *natively at 4K, 16:9*. No upscaling.
5. **Normalise.** Convert to a clean 3840×2160 RGB JPEG.
6. **Archive.** Save the JPEG + a JSON sidecar (chosen style, scene, full prompt) to `archive/`. Prune to the last 90 days.
7. **Optional: upload to Contentstack** as an asset, with tags. Failure here is non-fatal — the TV still updates.
8. **Push to the Frame.** Connect over the local network (with retry, because art mode wakes slowly), upload, switch to the new image, delete yesterday's so the TV's storage doesn't fill up.

Roughly one minute end to end. By breakfast, there's a fresh painting on the wall.

---

## The flow, at a glance

```
        artStyles ──┐
                    │
         (random)   │            ┌─────────────────────────┐
                    └─► today's ─► Gemini Flash (scene) ───┤
                        style       env/activity/mood/      │
                                    palette                 │
                                                            ▼
                              petDescription ─► prompt template ─► prompt
                                                            │
                                                            ▼
                                     Gemini 3 Pro Image (Nano Banana Pro)
                                              native 4K, 16:9
                                                            │
                                                            ▼
                                                    normalise to JPEG
                                                            │
                                           ┌────────────────┼────────────────┐
                                           ▼                ▼                ▼
                                  Samsung Frame TV     archive/         Contentstack
                                  (display)            (last 90 days)   (optional)
```

A deliberate quirk worth flagging: the **pet is excluded from step 2**. Gemini only invents the surroundings; the pet is added in step 3 by string assembly. That way the dedup history stays usable even if you swap pets, and the same style/scene library works for anyone's cat/dog/axolotl.

---

## What you need

- A **Samsung Frame TV** on the local network. The Frame's art-mode API is reachable when the screen is in art mode (the "off-but-painting" state). Deep standby (fully off) is not reachable.
- An **always-on machine** with Python 3.11+. A Raspberry Pi works perfectly. You'll need SSH access and the ability to run cron.
- A **Gemini API key** ([ai.google.dev](https://ai.google.dev/)). Free tier covers Flash easily; Nano Banana Pro 4K image generation has a cost per image — small, but real. Budget a few cents to a few tens of cents per day, depending on your tier.
- **Optional:** a Contentstack account if you want every painting archived as a web-addressable asset.

---

## Setup, step by step

### 1. Clone & install

On your always-on machine:

```bash
git clone https://github.com/<you>/paintSamsungFrame.git
cd paintSamsungFrame
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

### 2. Environment variables

Create a `.env` in the project root:

```bash
GEMINI_API_KEY=your-gemini-api-key
FRAME_TV_IP=192.168.x.x        # your Frame's local IP

# Optional — only if you want Contentstack archiving:
CS_API_KEY=your-cs-api-key
CS_MANAGEMENT_TOKEN=your-cs-management-token
```

`.env` is gitignored. Keep it that way.

If you don't want Contentstack archiving, flip `uploadToContentstack = False` at the top of `paint.py` and skip the CS variables.

### 3. Describe your pet

Create a `petDescription` file (no extension, plain text) in the project root. This is what Gemini sees every day. The more specific, the more consistent your paintings will be:

```
A small ginger tabby cat, very fluffy, white socks on all four paws,
bright green eyes, a slightly grumpy expression that hides a very
soft soul. Tail with a faint dark tip.
```

This file is gitignored — your pet stays personal.

Text only is deliberate. Passing a reference photo makes Gemini drop the photo into the painting verbatim instead of reinterpreting the pet in the chosen art style. Words leave room for the style to do its job.

### 4. Curate art styles (optional)

`artStyles` is committed with ~60 signature styles. One per line, lines starting with `#` are comments. Add, remove, retire as you like.

The rule: **keep entries signature-like** ("Sumi-e ink wash", "Klimt gold leaf"), not bloated qualifiers ("...emphasising ethereal light and pastoral calm..."). Gemini fills in the mood and palette itself based on the style name.

### 5. Tweak the prompt template (optional)

`paintingPrompt` is the actual prompt sent to the image model. You can edit it freely. Placeholders that get filled in each day:

- `{art_style}` — today's chosen style
- `{pet_description}` — contents of your `petDescription`
- `{environment}` — Gemini's scene
- `{activity}` — what the pet is doing
- `{mood}` — mood words
- `{palette}` — colour palette

Lines starting with `#` are stripped at load — handy for leaving notes for future-you. Blank lines are preserved.

### 6. Pair with the Frame

First run will trigger a "Allow this device to control your TV?" prompt on the screen. Press OK on the remote. A `token.txt` is saved locally and reused on every future run.

If you can't see the prompt — your Frame is probably in deep standby. Turn it on first, then re-run.

### 7. Test run

Manually source your `.env` and run `paint.py`:

```bash
set -a && source .env && set +a
.venv/bin/python paint.py
```

It logs to `daily_cat_art.log` and writes the JPEG to `archive/<today>.jpg`. Watch the log for any complaints (rate limits, missing files, TV unreachable). The whole run usually takes 30–90 seconds — Gemini does the heavy lifting.

### 8. Schedule it

Pop a line in your crontab — `crontab -e` and add:

```cron
0 3 * * * cd /home/<you>/paintSamsungFrame && set -a && . ./.env && set +a && /home/<you>/paintSamsungFrame/.venv/bin/python /home/<you>/paintSamsungFrame/paint.py >> /home/<you>/paintSamsungFrame/daily_cat_art.log 2>&1
```

A few notes on this line:

- **Absolute paths everywhere.** Cron has a minimal `PATH` and won't find `python` from a venv.
- **`set -a; . ./.env; set +a`.** Cron does not load your shell profile, so the script can't see `GEMINI_API_KEY` etc. unless you source them explicitly. This pattern marks them all as exported.
- **`/bin/sh` (POSIX).** The line uses `.` (the POSIX equivalent of `source`) so it works in dash, which is what `/bin/sh` is on Raspberry Pi OS.
- **03:00** is just a pick. Choose a time when the TV is in art mode, not deep standby.

---

## Two scripts

### `paint.py` — the daily flow

Cron's friend. Generates a new painting from scratch and pushes it. Runs on the always-on machine.

### `push.py` — manual override

For when you find a specific image you'd rather have on the TV today than whatever Gemini would have come up with. Run it from your laptop:

```bash
.venv/bin/python push.py /path/to/image.jpg
.venv/bin/python push.py https://example.com/some-painting.jpg
```

It centre-crops to 16:9 if your image isn't already, then downscales to 3840×2160. If the source is too small to fill 3840×2160 without upscaling, it refuses — better a clean error than a blurry painting.

`push.py` keeps its own `manual_state.json` so it doesn't interfere with `paint.py`'s cleanup logic. Each script deletes only the previous image *it* uploaded.

---

## Files in the repo

| File | Purpose | In git? |
| --- | --- | --- |
| `paint.py` | Daily generation flow (cron) | yes |
| `push.py` | Manual override push | yes |
| `paintingPrompt` | Image-prompt template, editable | yes |
| `artStyles` | Curated art-style list | yes |
| `requirements.txt` | Python deps | yes |
| `CLAUDE.md` | Project context for AI coding assistants | yes |
| `petDescription` | Your pet description | no (personal) |
| `.env` | API keys, TV IP | no (secret) |
| `token.txt` | Frame pairing token | no (auto-generated) |
| `state.json` | Tracks last cron-uploaded asset for cleanup | no |
| `manual_state.json` | Same, but for `push.py` | no |
| `archive/` | Past JPEGs + scene JSON sidecars | no |
| `daily_cat_art.log` | Cron run log | no |

---

## Customisation ideas

- **Different pet.** Edit `petDescription`. Cat → dog → axolotl → tortoise. The rest of the pipeline is pet-agnostic.
- **Narrow the style range.** Want only oil paintings? Delete every non-oil line in `artStyles`. Want only watercolours? Same idea.
- **Different mood for the prompt.** Edit `paintingPrompt`. Want black-and-white only? Add a constraint. Want a different framing? Rewrite the composition block.
- **Different schedule.** Move the cron earlier, later, weekly, on Sundays only. Cron is your oyster.
- **Skip Contentstack.** Set `uploadToContentstack = False` at the top of `paint.py`. The CS step is fully optional.

---

## Lessons learned (the hard way)

Things that each cost a debugging round at some point. Future-me, future-you: don't relearn these.

- **`IMAGE_SIZE` must be `"4K"` with a capital K.** Lowercase is silently rejected by Gemini and you get a 1024px image instead of 4K with no warning.
- **Gemini's `part.as_image()` returns a `types.Image`, not a PIL image.** Pull raw bytes from `part.inline_data.data` and open *those* with PIL.
- **Gemini's 5xx and 429 errors are common during peak hours.** Both calls retry with linear backoff (20s, 40s, …). Non-retryable errors raise immediately.
- **The Frame's art-mode API wakes slowly.** First connection often fails; subsequent retries succeed. Build in a retry loop.
- **The Frame in deep standby is unreachable.** No retry will help. Wake-on-LAN would, but that's not wired up here.
- **Contentstack asset `description` caps at 1000 characters** — anything longer returns a 422 (error_code 142). The `trim_for_cs()` helper handles it.
- **Contentstack `asset[tags]` must be a list, not a string** — also a 422 if you pass a string. Pass a list and `requests` repeats the field, which is what the API expects.
- **Surface Contentstack's response body on error,** not just the HTTP status — `resp.text` is where the actual reason lives.
- **Cron doesn't load your shell profile.** Source the `.env` inline in the cron line, or env-vars-on-the-cron-line will haunt you.

---

## Known limitations

- **Deep standby is a wall.** If the TV is fully off when cron fires, the image still gets generated and archived, but the upload step fails after retries. The painting is still recoverable from `archive/`; just push it manually later with `push.py`.

---

## Roadmap

- A small public gallery view of the archive (web page, RSS feed, something).
- Optional: scheduled cron variants per art-style category, in case you want only oil paintings on Sundays.

---

## License

MIT — see [LICENSE](LICENSE).

---

## Acknowledgements

- xchwarze's [`samsung-tv-ws-api`](https://github.com/xchwarze/samsung-tv-ws-api) for talking to the Frame.
- Google's Gemini API for the actual painting.
- One very specific cat for being the reason any of this exists.
