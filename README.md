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
 └─> Module 5  publish_engine   optional: YouTube Shorts + Instagram Reels
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

| Key | Needed for | Cost |
|---|---|---|
| `GEMINI_API_KEY` | Module 1 | Free tier, ~1,500 requests/day. [Get one](https://aistudio.google.com/apikey) |
| `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN` | Module 3a | Free 10k neurons/day. [Dashboard](https://dash.cloudflare.com/) |
| `FAL_KEY` | Module 3b only | ~$0.40-1.00 per video. [Get one](https://fal.ai/dashboard/keys) |
| `POLLINATIONS_TOKEN` | Module 3a fallback | Optional; only if `images.provider: pollinations` |
| YouTube OAuth (`secrets/` + `tools.auth_youtube`) | Module 5 `--publish youtube` | Free (quota limits apply) |
| `META_ACCESS_TOKEN` + `INSTAGRAM_USER_ID` | Module 5 `--publish instagram` | Free (rate limits apply) |
| R2 keys + public base URL | Module 5 Instagram hosting | R2 free tier is usually enough |

Run with `--no-motion` and you never need `FAL_KEY` at all. Skip `--publish`
and you never need YouTube/Instagram/R2 credentials.

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

# render then upload to YouTube Shorts + Instagram Reels
python -m src.main "..." --publish

# one platform only
python -m src.main "..." --publish youtube
python -m src.main "..." --publish instagram

# ignore cached script and narration
python -m src.main "..." --force
```

Output lands in `output/<slug>.mp4`.

## Publish (YouTube Shorts + Instagram Reels)

Publishing is opt-in via `--publish`. Without that flag, nothing is uploaded.
Do the three one-time setups below, then verify with `python -m src.main --check`.

### 1. YouTube Data API (one-time)

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
   to that path.)
6. From the project root, authorize once in a browser:

```bash
.venv/bin/python -m tools.auth_youtube
```

   Sign in with the Google account that owns the channel, accept upload access.
   This writes `secrets/youtube_token.json` (gitignored). Re-run if you revoke
   access or delete the token file.
7. Confirm:

```bash
.venv/bin/python -m src.main --check
```

   `youtube auth` should show `ok`. Then try:

```bash
.venv/bin/python -m src.main "Akbar asks Birbal for an honest answer" --publish youtube
```

### 2. Instagram Graph API (one-time)

Personal Instagram accounts **cannot** publish via API. You need:

- Instagram **Creator** or **Business**
- A **Facebook Page** linked to that Instagram account
- A Meta developer app with a long-lived Page token

#### A. Convert Instagram + link a Page

1. In the Instagram app: **Settings → Account type and tools → Switch to
   professional account** → pick **Creator** or **Business**.
2. On Facebook, create a Page if you do not have one
   ([facebook.com/pages/create](https://www.facebook.com/pages/create)).
3. Link them: Instagram **Settings → Account → Sharing to other apps →
   Facebook** (or Facebook Page settings → **Linked accounts → Instagram**).
   Confirm the Page shows your IG handle as connected.

#### B. Create a Meta app

1. Open [Meta for Developers](https://developers.facebook.com/) → **My Apps →
   Create App**.
2. Choose a use case that includes Instagram / Business (or start with
   **Other → Business** if the UI offers it).
3. In the app dashboard, add the **Instagram Graph API** product (and
   **Facebook Login for Business** if prompted).
4. Under **App roles / Roles**, add yourself as Admin.
5. For permissions you will need (granted when generating the token):

   - `instagram_basic`
   - `instagram_content_publish`
   - `pages_show_list`
   - `pages_read_engagement`

   While the app is in Development mode, only app roles (you) can use it —
   that is enough to post to your own linked account.

#### C. Get `INSTAGRAM_USER_ID` and a long-lived `META_ACCESS_TOKEN`

Easiest path for a solo channel: [Graph API Explorer](https://developers.facebook.com/tools/explorer/).

1. Select your Meta app in the explorer.
2. **Generate Access Token** → grant the permissions listed above, and pick
   the Facebook Page that is linked to Instagram when asked.
3. Call:

```text
GET /me/accounts
```

   Copy the Page `id` and the Page `access_token` from the response.
4. Call (replace `{page-id}`):

```text
GET /{page-id}?fields=instagram_business_account
```

   Copy `instagram_business_account.id` — that is `INSTAGRAM_USER_ID`.
5. Exchange the short-lived Page token for a long-lived one
   ([Access Token Tool](https://developers.facebook.com/tools/debug/accesstoken/)
   or the documented `fb_exchange_token` endpoint with your app id + secret).
   Save the long-lived Page token as `META_ACCESS_TOKEN`.
6. Put both in `.env`:

```bash
META_ACCESS_TOKEN=EAAB...your_long_lived_page_token
INSTAGRAM_USER_ID=17841...your_ig_user_id
```

Do **not** use a User token alone for publishing Reels — the Page token tied
to the linked IG account is what Content Publishing expects.

### 3. Cloudflare R2 (required for Instagram)

Instagram’s publish API will not accept a local file. It needs a public HTTPS
`video_url`. The pipeline uploads the MP4 to R2 first, then hands Meta that URL.

1. Open [Cloudflare Dashboard](https://dash.cloudflare.com/) → **R2 Object
   Storage**.
2. Note your **Account ID** (R2 overview sidebar) → that is `R2_ACCOUNT_ID`
   (often the same as `CLOUDFLARE_ACCOUNT_ID` you already use for Workers AI).
3. **Create bucket**, e.g. `hindi-stories-shorts`.
4. Make objects publicly readable:
   - Bucket **Settings → Public access** → allow **R2.dev subdomain**, **or**
   - Attach a **custom domain** you control.
   - Copy the public base URL, e.g. `https://pub-xxxxx.r2.dev` (no trailing
     slash, no object key). That is `R2_PUBLIC_BASE_URL`.
5. **Manage R2 API Tokens → Create API token**:
   - Permissions: **Object Read & Write** (scoped to this bucket is best).
   - Copy **Access Key ID** → `R2_ACCESS_KEY_ID`
   - Copy **Secret Access Key** → `R2_SECRET_ACCESS_KEY`
6. Add to `.env`:

```bash
R2_ACCOUNT_ID=your_cloudflare_account_id
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET=hindi-stories-shorts
R2_PUBLIC_BASE_URL=https://pub-xxxxx.r2.dev
```

7. Sanity-check: after a test upload (or after the first `--publish instagram`
   run), open
   `{R2_PUBLIC_BASE_URL}/shorts/<object>.mp4` in a private browser window —
   it must play **without** signing in. If it 404s or asks for auth, Instagram
   container creation will fail.

### Tuning

`publish` in `config.yaml` controls brand name, YouTube privacy/tags, Instagram
caption length, and R2 key prefix. Metadata (titles/captions) is generated by
Gemini and cached at `work/<slug>/publish.json`. Successful platform ids are
stored in `work/<slug>/publish_result.json` so a re-run skips platforms that
already posted (use `--force` to republish).

### Verify

```bash
.venv/bin/python -m src.main --check
```

You want `youtube auth`, `instagram auth`, and `R2 hosting` all `ok` before
using `--publish`. Missing publish secrets only `warn` — they do not block
local renders.

```bash
# both platforms
.venv/bin/python -m src.main "..." --publish

# one at a time while testing setup
.venv/bin/python -m src.main "..." --publish youtube
.venv/bin/python -m src.main "..." --publish instagram
```

## Daily GitHub Action (1pm JST)

Workflow: `.github/workflows/daily-post.yml`

- Runs every day at **13:00 JST** (`cron: 0 4 * * *` UTC)
- Also runnable manually via **Actions → Daily Short → Run workflow**
- Takes the first line from `ideas.txt`, renders one short, publishes, then
  moves that line to `ideas_used.txt` (committed back to the repo)
- If `ideas.txt` is empty, Gemini invents a fresh idea
- Defaults to `--no-motion` (free Ken Burns). Uncheck that on a manual run
  only if `FAL_KEY` is set and you want paid clips

### Secrets to add

Repo → **Settings → Secrets and variables → Actions**. Paste values from your
local `.env` / `secrets/` files:

| Secret | Required for | Notes |
|---|---|---|
| `GEMINI_API_KEY` | always | script + publish metadata |
| `CLOUDFLARE_ACCOUNT_ID` | images | Workers AI |
| `CLOUDFLARE_API_TOKEN` | images | Workers AI |
| `YOUTUBE_TOKEN_JSON` | YouTube | **full contents** of `secrets/youtube_token.json` after `python -m tools.auth_youtube` |
| `YOUTUBE_CLIENT_SECRET_JSON` | YouTube (optional) | full contents of `secrets/youtube_client_secret.json` |
| `META_ACCESS_TOKEN` | Instagram | long-lived Page token |
| `INSTAGRAM_USER_ID` | Instagram | IG professional account id |
| `R2_ACCOUNT_ID` | Instagram | Cloudflare account id |
| `R2_ACCESS_KEY_ID` | Instagram | R2 API token |
| `R2_SECRET_ACCESS_KEY` | Instagram | R2 API token |
| `R2_BUCKET` | Instagram | e.g. `hindi-stories-shorts` |
| `R2_PUBLIC_BASE_URL` | Instagram | e.g. `https://pub-xxxxx.r2.dev` |
| `FAL_KEY` | motion only | optional; leave unset for free Ken Burns |

YouTube-only is fine: set the Gemini + Cloudflare + `YOUTUBE_TOKEN_JSON`
secrets and skip the Instagram/R2 ones. The job posts to whichever platforms
have credentials.

Quick local check of the same entrypoint:

```bash
.venv/bin/python -m tools.daily_post --no-motion --publish youtube
```

## Tuning

Everything lives in `config.yaml`.

- **Length** — `story.min_duration_s` / `max_duration_s`. The script is
  regenerated if the narrated audio misses the window.
- **Voice** — `audio.voice`. Default is `hi-IN-SwaraNeural` (female storyteller)
  at `+16%`. Use `hi-IN-MadhurNeural` for male.
- **Spend** — `motion.hero_scene_count` and `motion.max_cost_usd`. The cap is
  checked before each call, and blowing it degrades to Ken Burns rather than
  failing the run.
- **Art style** — `story.art_style`. Put style words early; image models weight
  leading tokens far more heavily than trailing ones.
- **Captions** — `captions.font_size`, `vertical_position` (0.56 keeps text
  clear of the Shorts UI), and `max_words_per_group`.

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
  whole clip per *input* frame, and with `-loop 1` feeding it endlessly that is
  about 50x redundant work: 154 seconds versus 3 for one 4-second segment. Its
  `fps` also defaults to 25 and must be set, or segments drift out of sync.
- **Captions are Hinglish, deliberately.** Burning Devanagari needs a
  HarfBuzz-enabled libass and `shaping=complex`; get it wrong and matras land
  in the wrong places silently. Roman script sidesteps that entirely, and
  Hinglish is what this audience reads faster anyway.

## Layout

```
config.yaml          all tuning
prompts/             the story system prompt
src/                 the five modules + orchestrator
tools/               smoke test, probes, YouTube OAuth
secrets/             YouTube OAuth client + token (gitignored)
assets/fonts/        caption font
assets/music/        background tracks (yours)
work/<slug>/         intermediates, resumable
output/<slug>.mp4    the finished short
```
