# Hindi Story Shorts Pipeline

Turns a one-line story idea into a 50-60 second vertical (1080x1920) animated short:
Hindi voiceover, Hinglish karaoke captions, AI-generated art, and a funny twist or
a moral at the end. Built for Akbar-Birbal, Tenali Raman and Vikram-Betaal style tales.

```
python -m src.main "Akbar asks Birbal to bring him a flower that never wilts"
```

## How it works

```
idea
 └─> Module 1  script_engine    Gemini writes the story as structured JSON:
 │                              Devanagari narration (for TTS) + word-for-word
 │                              Hinglish (for captions) + image prompts
 └─> Module 2  audio_engine     edge-tts narrates each scene separately and
 │                              reports per-word timings
 └─> Module 3a visual_engine    Cloudflare Workers AI generates one still per scene
 └─> Module 3b motion_engine    the 4 highest-impact scenes become real video
 │                              clips; everything else gets a Ken Burns move
 └─> Module 4a caption_engine   word-by-word highlighted captions as ASS
 └─> Module 4b composer         ffmpeg assembles it all into the final MP4
 └─> Module 5  publish_engine   optional: YouTube Shorts
```

Each stage writes to `work/<slug>/` and is skipped on a re-run if its inputs have
not changed, so an interrupted run resumes instead of starting over — and never
re-pays for an image-to-video clip it already has.

## Setup

### 1. ffmpeg

**Use `ffmpeg-full`, not `ffmpeg`.** Homebrew's default `ffmpeg` formula is a
minimal build with no libass and no libfreetype, so it cannot burn captions at
all — the `ass`, `subtitles` and `drawtext` filters are simply absent.

```bash
brew install ffmpeg-full
```

It installs alongside any existing ffmpeg without relinking it. The pipeline
finds a capable binary on its own; set `FFMPEG_BINARY` to override.

### 2. Python

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3. Keys

```bash
cp .env.example .env
```

| Key                                               | Needed for           | Cost                                                                          |
| ------------------------------------------------- | -------------------- | ----------------------------------------------------------------------------- |
| `GEMINI_API_KEY`                                  | Module 1             | Free tier, ~1,500 requests/day. [Get one](https://aistudio.google.com/apikey) |
| `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN`  | Module 3a            | Free 10k neurons/day. [Dashboard](https://dash.cloudflare.com/)               |
| `FAL_KEY`                                         | Module 3b only       | ~$0.40-1.00 per video. [Get one](https://fal.ai/dashboard/keys)               |
| `POLLINATIONS_TOKEN`                              | Module 3a fallback   | Optional; only if `images.provider: pollinations`                             |
| YouTube OAuth (`secrets/` + `tools.auth_youtube`) | Module 5 `--publish` | Free (quota limits apply)                                                     |

Run with `--no-motion` and you never need `FAL_KEY` at all. Skip `--publish`
and you never need YouTube credentials.

**Images default to Cloudflare Workers AI** (`flux-2-klein-4b` at 768x1344,
scaled to 1080x1920 on compose). That stays inside the free daily neuron
allowance for a few shorts per day.

### 4. Music (optional)

Drop royalty-free tracks into `assets/music/`. One is picked at random per
video and ducked under the narration. With the folder empty, the video renders
with voice only.

## Usage

```bash
# one story
python -m src.main "Tenali Raman outwits a greedy merchant"

# free run, no image-to-video spend
python -m src.main "..." --no-motion

# review the stills before any money is spent
python -m src.main "..." --review

# ten shorts in one go
python -m src.main --batch ideas.txt

# render then upload to YouTube Shorts
python -m src.main "..." --publish

# ignore cached script and narration
python -m src.main "..." --force
```

Output lands in `output/<slug>.mp4`.

## Publish (YouTube Shorts)

Publishing is opt-in via `--publish`. Without that flag, nothing is uploaded.
Do the one-time YouTube setup below, then verify with `python -m src.main --check`.

### YouTube Data API (one-time)

You need a Google account that owns (or can upload to) the YouTube channel.

1. Open [Google Cloud Console](https://console.cloud.google.com/) → create a
   project (e.g. `hindi-stories-publish`).
2. **APIs & Services → Library** → search **YouTube Data API v3** → **Enable**.
3. **APIs & Services → OAuth consent screen**:
   - User type: **External** (fine for personal use).
   - App name / support email: anything you like.
   - Scopes: add `https://www.googleapis.com/auth/youtube.upload`
     (or leave empty here — the auth script requests it).
   - Test users: add **your Google account email** (required while the app is
     in Testing mode).
   - Publish the app only if you need other people to authorize; for yourself,
     Testing + your email as a test user is enough.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Desktop app**.
   - Name: e.g. `hindi-stories-desktop`.
   - Download the JSON.
5. Save the downloaded file as:

```text
secrets/youtube_client_secret.json
```

(The filename must match `YOUTUBE_CLIENT_SECRETS` in `.env`, which defaults
to that path.) 6. From the project root, authorize once in a browser:

```bash
.venv/bin/python -m tools.auth_youtube
```

Sign in with the Google account that owns the channel, accept upload access.
This writes `secrets/youtube_token.json` (gitignored). Re-run if you revoke
access or delete the token file. 7. Confirm:

```bash
.venv/bin/python -m src.main --check
```

`youtube auth` should show `ok`. Then try:

```bash
.venv/bin/python -m src.main "Akbar asks Birbal for an honest answer" --publish
```

### Tuning

`publish` in `config.yaml` controls brand name and YouTube privacy/tags.
Metadata is generated by Gemini and cached at `work/<slug>/publish.json`.
Successful upload ids are stored in `work/<slug>/publish_result.json` so a
re-run skips videos that already posted (use `--force` to republish).

### Verify

```bash
.venv/bin/python -m src.main --check
```

You want `youtube auth` `ok` before using `--publish`. Missing publish secrets
only `warn` — they do not block local renders.

```bash
.venv/bin/python -m src.main "..." --publish
```

## Daily GitHub Action (1pm JST)

Workflow: `.github/workflows/daily-post.yml`

- Runs every day at **13:00 JST** (`cron: 0 4 * * *` UTC)
- Also runnable manually via **Actions → Daily Short → Run workflow**
- Gemini invents a fresh idea each run (avoids anything already in
  `ideas_used.txt`), renders one short, uploads to YouTube, then records the
  idea in `ideas_used.txt` (committed back to the repo)
- Pass an idea in the workflow input, or use `--from-queue` locally to take the
  next unused line from `ideas.txt` instead
- Defaults to `--no-motion` (free Ken Burns). Uncheck that on a manual run
  only if `FAL_KEY` is set and you want paid clips

## Sunday long-form (2pm JST)

Workflow: `.github/workflows/sunday-long.yml`

- Runs every **Sunday at 14:00 JST** (`cron: 0 5 * * 0` UTC)
- 16:9 landscape (`1920x1080`), **at least 6 minutes** (`config.long.yaml`)
- Invents a fresh idea by default (tracked in `ideas_long_used.txt`); optional
  backlog in `ideas_long.txt` via `--from-queue`
- Publishes as a normal YouTube video (no `#Shorts`)
- Manual run: **Actions → Sunday Long → Run workflow**

Local equivalents:

```bash
# daily short
.venv/bin/python -m tools.daily_post --no-motion

# sunday long-form
.venv/bin/python -m tools.daily_post --profile long --no-motion

# one-off long render without uploading
.venv/bin/python -m src.main "..." --profile long --no-motion
```

### Secrets to add

Repo → **Settings → Secrets and variables → Actions**. Paste values from your
local `.env` / `secrets/` files:

| Secret                       | Required for       | Notes                                                                                  |
| ---------------------------- | ------------------ | -------------------------------------------------------------------------------------- |
| `GEMINI_API_KEY`             | always             | script + publish metadata                                                              |
| `CLOUDFLARE_ACCOUNT_ID`      | images             | Workers AI                                                                             |
| `CLOUDFLARE_API_TOKEN`       | images             | Workers AI                                                                             |
| `YOUTUBE_TOKEN_JSON`         | YouTube            | **full contents** of `secrets/youtube_token.json` after `python -m tools.auth_youtube` |
| `YOUTUBE_CLIENT_SECRET_JSON` | YouTube (optional) | full contents of `secrets/youtube_client_secret.json`                                  |
| `FAL_KEY`                    | motion only        | optional; leave unset for free Ken Burns                                               |

Quick local check of the same entrypoint:

```bash
.venv/bin/python -m tools.daily_post --no-motion
.venv/bin/python -m tools.daily_post --profile long --no-motion
```

## Tuning

Everything lives in `config.yaml` (Shorts) or `config.long.yaml` (`--profile long`).

- **Length** — `story.min_duration_s` / `max_duration_s`. The script is
  regenerated if the narrated audio misses the window.
- **Music** — `music.volume_db` (default **-25**). Tracks in `assets/music/`
  loop for the full video length and duck under narration.
- **Voice** — `audio.voice`. Default is `hi-IN-SwaraNeural` (female storyteller).
  Use `hi-IN-MadhurNeural` for male.
- **Spend** — `motion.hero_scene_count` and `motion.max_cost_usd`. The cap is
  checked before each call, and blowing it degrades to Ken Burns rather than
  failing the run.
- **Art style** — `story.art_style`. Put style words early; image models weight
  leading tokens far more heavily than trailing ones.
- **Captions** — `captions.font_size`, `vertical_position` (0.56 for Shorts UI;
  0.88 for landscape), and `max_words_per_group`.

## Testing

```bash
# whole render path, synthetic assets, no API calls, no spend
.venv/bin/python -m tools.smoke_test

# compare image models and resolutions
.venv/bin/python -m tools.probe_images
```

## Notes from building this

Things that are true now and were not obvious:

- **Gemini cannot generate the images.** Every Nano Banana image model shows
  "Free Tier: Not available". Gemini text is still free, which is why it writes
  the script and nothing else.
- **edge-tts defaults to `SentenceBoundary`.** Left alone it returns one timing
  event for a whole sentence, which silently collapses captions onto evenly
  spaced timings. `boundary="WordBoundary"` restores per-word timings.
- **`zoompan` needs `d=1`.** The intuitive `d=<total frames>` makes it emit a
  whole clip per _input_ frame, and with `-loop 1` feeding it endlessly that is
  about 50x redundant work: 154 seconds versus 3 for one 4-second segment. Its
  `fps` also defaults to 25 and must be set, or segments drift out of sync.
- **Captions are Hinglish, deliberately.** Burning Devanagari needs a
  HarfBuzz-enabled libass and `shaping=complex`; get it wrong and matras land
  in the wrong places silently. Roman script sidesteps that entirely, and
  Hinglish is what this audience reads faster anyway.

## Layout

```
config.yaml          Shorts tuning (9:16, ~60s)
config.long.yaml     Sunday long-form (16:9, ≥6 min)
prompts/             short + long story system prompts
src/                 the five modules + orchestrator
tools/               smoke test, probes, YouTube OAuth, daily_post
secrets/             YouTube OAuth client + token (gitignored)
assets/fonts/        caption font
assets/music/        background tracks (looped under voice)
work/<slug>/         intermediates, resumable
output/<slug>.mp4    the finished video
ideas.txt            daily Shorts queue
ideas_long.txt       Sunday long-form queue
```
