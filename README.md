# shrink

Turn an MP4 or MP3 into a compressed MP3 that fits under a size cap.

The default cap is **25 MB** — the upload limit for Whisper and most transcription APIs. Point it
at a 500 MB screen recording, get back an MP3 small enough to upload.

```
$ python shrink.py meeting.mp4
meeting.mp4 -> meeting.mp3  487.2 MB -> 21.6 MB  (48 kbps mono 44.1 kHz)
```

## Install

Needs Python 3.10+. ffmpeg comes from pip, so there is nothing to install system-wide and no
PATH to configure.

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
```

If `venv` won't build (some redirected Windows folders break `ensurepip`), install into a
`vendor/` directory beside the script instead — `shrink.py` looks there automatically:

```bash
python -m pip install --target vendor -r requirements.txt
```

If you already have ffmpeg on your PATH, `shrink` uses that one and ignores the bundled copy.

## Usage

```
python shrink.py INPUT [INPUT ...] [options]

  -o, --out-dir DIR      where to write (default: alongside each input)
      --target-size SIZE cap per output file (default: 25MB)
      --bitrate RATE     force a bitrate (e.g. 64k) and skip target sizing
      --keep-stereo      never downmix to mono, only lower the bitrate
      --force            overwrite an existing output file
      --quiet            suppress progress output
```

```bash
python shrink.py lecture.mp4                          # default 25 MB cap
python shrink.py *.mp4 -o compressed --target-size 10MB
python shrink.py podcast.mp3 --bitrate 64k            # ignore the cap, fix the bitrate
python shrink.py concert.mp4 --keep-stereo            # music: don't collapse to mono
```

Sizes are **decimal** by default: `25MB` means 25,000,000 bytes, which stays safely under a
25 MiB limit. Write `25MiB` if you want binary units.

## How it picks a bitrate

Duration and the cap give an ideal bitrate, which snaps **down** to the nearest rate LAME will
actually encode:

```
bitrate = (cap × 0.97 × 8) ÷ (duration_seconds × 1000)   → rounded down to a real LAME rung
```

The 0.97 leaves room for ID3 tags and frame padding. Two limits apply on top:

- **Never above 128 kbps.** A three-minute clip under a 25 MB cap could technically use 320 kbps;
  that just wastes space for no audible gain on speech.
- **Never above the source bitrate.** A 64 kbps podcast is not improved by re-encoding it at 128.

Then the audio shape degrades with the bitrate, so a small cap stays listenable:

| Bitrate | Channels | Sample rate |
|---|---|---|
| ≥ 64 kbps | stereo | 44.1 kHz |
| 48–63 kbps | mono | 44.1 kHz |
| < 48 kbps | mono | 22.05 kHz |

`--keep-stereo` skips the downmix. It cannot skip the sample-rate drop: MPEG-1 at 44.1 kHz will
not encode below 32 kbps, and LAME quietly raises the bitrate rather than failing — which would
silently blow the size budget.

After encoding, the output is measured. If it somehow came out over the cap, `shrink` retries one
rung lower. If it still doesn't fit, it says so and exits non-zero rather than handing back a file
that won't upload.

**An MP3 that already fits is copied through, not re-encoded** — no second generation of
lossy-to-lossy damage.

## Behaviour worth knowing

- Output is `<name>.mp3`. Converting an MP3 in place writes `<name>.compressed.mp3`; the source is
  never overwritten.
- Encoding goes to a `.part` file and is renamed on success, so an interrupted run never leaves a
  truncated MP3 behind.
- In a batch, a bad file prints an error and the rest continue.
- Exit codes: `0` all succeeded, `2` some succeeded, `1` none did.
- A file too long to fit its cap even at 8 kbps is encoded anyway, with a warning and a non-zero
  exit — you get the file and the honest news, not a refusal.

## Tests

```bash
.venv/Scripts/python -m pytest test_shrink.py -q
```

49 tests covering the bitrate math, shape thresholds, the MPEG rate constraint, passthrough,
argument parsing and output naming. They're pure — no ffmpeg or media files needed.
