# AGENTS.md

Guidance for AI agents working in this repo.

## What this is

A single-file CLI (`shrink.py`) that converts MP4/MP3 into an MP3 under a size cap. Default cap
is 25 MB — the Whisper/transcription upload limit, which is the reason this tool exists. Keep
that use case in mind: speech in, uploadable file out.

Stdlib plus `imageio-ffmpeg`. No framework, no package layout. Resist adding either.

## Setup

`vendor/` is gitignored and will not exist in a fresh clone. Nothing runs until:

```bash
python -m pip install --target vendor -r requirements.txt
```

Then prefix commands with `PYTHONPATH=vendor`, or use a venv (`shrink.py` finds either — see
`_load_imageio_ffmpeg`).

```bash
PYTHONPATH=vendor python -m pytest test_shrink.py -q
PYTHONPATH=vendor python shrink.py input.mp4 --target-size 10MB
```

## Constraints that are easy to break

These were each found by testing, not by reading docs. Breaking them tends to fail *silently* —
you get a plausible-looking MP3 that is the wrong size.

**1. MPEG-1 has a 32 kbps floor at 44.1 kHz.** Ask LAME for 16 kbps at 44.1 kHz and it does not
error — it quietly encodes at 32 kbps, doubling your output size. Anything below 32 kbps must
drop to 22.05 kHz. This applies even under `--keep-stereo`. See the clamp at the end of
`plan_encode` and its regression tests.

**2. There is no ffprobe.** `imageio-ffmpeg` ships ffmpeg only. Duration and stream details come
from parsing `ffmpeg -i` stderr in `probe()`. That command exits non-zero by design. Do not
"fix" it by adding an ffprobe dependency without checking the bundled wheel actually has one.

**3. The temp file needs an explicit `-f mp3`.** Output is written to `<name>.mp3.part` and
renamed on success. ffmpeg cannot infer a container from `.part`.

**4. ffmpeg's stderr goes to a temp file, not a pipe.** We drain stdout (progress) to completion
before reading stderr; on a long encode a stderr pipe would fill and deadlock. Don't switch it
back to `subprocess.PIPE`.

**5. MP3 is mono or stereo only.** A 5.1 source must fold to ≤2 channels, `--keep-stereo`
included.

## Where the logic lives

`plan_encode()` is the only function with real decision-making, and it is pure — no I/O, no
ffmpeg. All 49 tests target it plus the parsing helpers. **New sizing behaviour belongs there,
with a test.** If you find yourself making size decisions inside `encode()` or `process()`,
that's the wrong place.

The bitrate rule, in short: duration and cap give an ideal bitrate; it snaps *down* to a real
LAME rung, capped at 128 kbps and never above the source bitrate. Then channels and sample rate
degrade as the bitrate falls. `HEADROOM = 0.97` covers ID3 tags and frame padding.

## Testing

Unit tests need no media and run in under a second. Run them for any change to sizing.

No fixtures are committed (media is gitignored). Generate them:

```bash
FF=$(PYTHONPATH=vendor python -c "import shrink;print(shrink.find_ffmpeg())")
"$FF" -f lavfi -i "sine=frequency=440:duration=600" \
      -f lavfi -i "testsrc=size=640x480:rate=24:duration=600" \
      -c:v libx264 -preset ultrafast -c:a aac -b:a 192k -shortest sample.mp4
```

For anything touching the encode path, check the output **size against the cap** and confirm the
audio survived — file size alone doesn't prove it isn't silence:

```bash
"$FF" -i out.mp3 -af volumedetect -f null - 2>&1 | grep mean_volume
```

Worth exercising by hand after encode changes: an MP3 already under the cap (should be
stream-copied, not re-encoded), a cap too small to be satisfiable (warns, encodes anyway, exits
non-zero), and a batch containing a bad file (exit code 2).

## Conventions

- Exit codes are part of the interface: `0` all succeeded, `2` some, `1` none.
- Errors on one file print and let the batch continue; only argument and ffmpeg-discovery
  problems are fatal.
- Never overwrite the input. Converting an MP3 in place produces `<name>.compressed.mp3`.
- Report real numbers to the user. When output exceeds the cap, say so and exit non-zero rather
  than presenting it as success.
