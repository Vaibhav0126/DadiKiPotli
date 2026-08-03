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

Run with `--no-motion` and you never need `FAL_KEY` at all.

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

# ignore cached script and narration
python -m src.main "..." --force
```

Output lands in `output/<slug>.mp4`.

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
src/                 the four modules + orchestrator
tools/               smoke test and probes
assets/fonts/        caption font
assets/music/        background tracks (yours)
work/<slug>/         intermediates, resumable
output/<slug>.mp4    the finished short
```
