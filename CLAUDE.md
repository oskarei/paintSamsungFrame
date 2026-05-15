# CLAUDE.md

Context for working on this project with Claude Code. Keep this file tight and
high-signal — it loads into context every session.

## What this is

A daily art generator. Once a day (via cron on a Raspberry Pi) `paint.py`
generates a fresh painting of a pet, pushes it to a Samsung Frame TV as the
displayed artwork, and optionally archives it to Contentstack. The whole
thing runs unattended.

`push.py` is a small companion script (laptop-side, ad-hoc) for pushing an
override image to the same Frame TV from a local path or URL. It centre-crops
to 16:9 if needed and rejects sources too small to fill 3840x2160 without
upscaling. It tracks its own `manual_state.json` so it doesn't trample
`paint.py`'s `state.json`.

No package structure — both scripts stand alone by design.

## Daily flow

1. **Load inputs** — read `petDescription` (plain-text pet description),
   `artStyles` (one curated art style per line), `paintingPrompt` (the
   image-prompt template), and the last 14 archived scenes from
   `archive/*.json`.
2. **Generate scene** — Python picks today's art style at random from
   `artStyles`, then Gemini (`gemini-2.5-flash`) invents an environment +
   activity + mood + palette that suit it, explicitly told to differ from
   the recent 14.
3. **Build prompt** — pure string assembly: the `paintingPrompt` template is
   filled in with the scene values and the pet description. No second LLM
   call here.
4. **Generate image** — Gemini Nano Banana Pro (`gemini-3-pro-image-preview`)
   renders the painting natively at 4K, 16:9.
5. **Normalize** — convert to a clean 3840x2160 RGB JPEG for the Frame.
6. **Archive + prune** — save the JPEG and a JSON sidecar (scene + full prompt),
   then prune the archive to the most recent `ARCHIVE_KEEP` days.
7. **Contentstack (optional)** — if `uploadToContentstack` is True, upload the
   JPEG as an asset. Non-fatal: failure here must not block the TV update.
8. **Frame** — connect to the TV (with retry), upload, switch to the new image,
   and delete yesterday's image.

## Key design decisions — do not "fix" these

- **The pet is described in text, not by reference photo.** Gemini takes a
  reference image too literally — it drops the exact reference into the
  painting instead of reinterpreting the pet in the chosen art style. A text
  description gives the model room to stylize.
- **The pet is deliberately excluded from scene generation (step 2).** Gemini
  invents only the environment and activity; the pet is added later in step 3.
  This keeps the archived scene history reusable even if the pet changes, and
  keeps the dedup history pet-agnostic. Do not merge these steps.
- **Step 3 is intentionally NOT an LLM call.** It is plain templating. A second
  Gemini call there would add cost and latency for nothing.
- **The script is regenerated whole, not patched.** When making changes,
  produce the complete file rather than diffs/snippets.
- **Contentstack upload is intentionally non-fatal.** It runs inside its own
  try/except so the TV still updates even if Contentstack is down.

## Environment

- Runs in a `.venv` on a Raspberry Pi. Python 3.11.
- Dependencies: `google-genai`, `pillow`, `requests`, `samsungtvws`.
- Required env vars (names only — never commit values):
  - `GEMINI_API_KEY` — Gemini API key
  - `FRAME_TV_IP` — local IP of the Samsung Frame TV
  - `CS_API_KEY` — Contentstack API key
  - `CS_MANAGEMENT_TOKEN` — Contentstack management token
- Required local files:
  - `petDescription` — plain-text description of the pet (not in git)
  - `token.txt` — Samsung TV pairing token, created on first run (not in git)
  - `artStyles` — curated art-style list, one per line; committed to git
  - `paintingPrompt` — image-prompt template with `{art_style}`, `{pet_description}`,
    `{environment}`, `{activity}`, `{mood}`, `{palette}` placeholders. Lines
    starting with `#` are stripped at load; blank lines preserved. Committed
    to git so the default is restorable from history.
- cron does not load the shell profile — set env vars inline in the crontab
  entry or source them explicitly.

## Gotchas (each of these cost a debugging round — don't relearn them)

- **Frame art mode wakes slowly.** The TV has three power states: on, art mode,
  and deep standby. Connecting while in art mode often needs a few seconds and
  a couple of retries before the Art API responds — this is why `connect_art()`
  has retry logic. In deep standby it is unreachable entirely (would need
  Wake-on-LAN).
- **`IMAGE_SIZE` must be `"4K"` with a capital K.** Lowercase is rejected.
- **Gemini's `part.as_image()` returns a `types.Image`, not a PIL image.** Pull
  raw bytes from `part.inline_data.data` and open those with PIL instead.
- **Contentstack asset `description` caps at 1000 characters.** Longer values
  return a 422 (error_code 142). `trim_for_cs()` handles this.
- **Contentstack `asset[tags]` must be a list, not a string.** A bare string
  returns a 422. Passing a list makes `requests` repeat the field, which is
  what the API expects.
- **Always surface Contentstack's response body on error**, not just the HTTP
  status — `resp.text` contains the actual reason.
- **Cloud MCP servers are not reachable from Claude Code / the API** — only
  from the Claude.ai app. Not relevant to this script directly, but worth
  knowing if MCP integration ever comes up here.
- Gemini calls can return transient `503` / `429`. `call_with_retry()` wraps
  both Gemini calls with linear backoff; non-retryable errors raise immediately.

## Config knobs (top of paint.py)

- `uploadToContentstack` — set False to skip the Contentstack step entirely.
- `RECENT_COUNT` (14) — how many past scenes are fed back in for dedup.
- `ARCHIVE_KEEP` (90) — days of paintings kept on disk. JSON sidecars are never
  pruned below `RECENT_COUNT` even if this is set lower.
- `csFolder`, `csTag` — Contentstack folder uid and asset tag.

## Current state

Working: end-to-end daily flow — scene generation with dedup, 4K image
generation, Frame upload, archive + prune, Contentstack upload.

## Open / next

- Possible later: a public gallery view of the archive.
