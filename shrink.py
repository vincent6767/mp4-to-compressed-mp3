#!/usr/bin/env python3
"""
shrink - turn an MP4 or MP3 into a compressed MP3 that fits under a size cap.

The default cap is 25 MB, the upload limit for Whisper and most transcription
APIs. Give it a file, get back an MP3 small enough to upload.
"""
from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------- constants

# Bitrates LAME will actually encode, highest first. Below 48 kbps these are
# MPEG-2 LSF rates, which require a sample rate of 24 kHz or lower - the shape
# rules in plan_encode() keep those two facts in sync.
LADDER = (320, 256, 224, 192, 160, 128, 112, 96, 80, 64, 56, 48, 40, 32, 24, 16, 8)

# A short file does not need 320 kbps just to use up its budget.
MAX_AUTO_KBPS = 128

# Leave room for ID3 tags and frame padding so we land under the cap, not on it.
HEADROOM = 0.97

DEFAULT_CAP = "25MB"

_UNITS = {
    "": 1, "b": 1,
    "k": 1000, "kb": 1000, "m": 1000**2, "mb": 1000**2, "g": 1000**3, "gb": 1000**3,
    "kib": 1024, "mib": 1024**2, "gib": 1024**3,
}

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_AUDIO_RE = re.compile(r"Stream #\d+:\d+.*?:\s*Audio:\s*([A-Za-z0-9_]+)")
_HZ_RE = re.compile(r"(\d+)\s*Hz")
_KBPS_RE = re.compile(r"(\d+)\s*kb/s")
_OVERALL_KBPS_RE = re.compile(r"Duration:.*?bitrate:\s*(\d+)\s*kb/s")


# ------------------------------------------------------------------ ffmpeg

def _load_imageio_ffmpeg():
    """Import imageio_ffmpeg, falling back to a vendor/ dir beside this script."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg
    except ImportError:
        pass
    vendor = Path(__file__).resolve().parent / "vendor"
    if vendor.is_dir():
        sys.path.insert(0, str(vendor))
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg
        except ImportError:
            pass
    return None


def find_ffmpeg() -> str:
    """Prefer a system ffmpeg; otherwise use the one bundled with imageio-ffmpeg."""
    system = shutil.which("ffmpeg")
    if system:
        return system
    module = _load_imageio_ffmpeg()
    if module is not None:
        try:
            return module.get_ffmpeg_exe()
        except Exception as exc:  # download failure, unsupported platform
            raise SystemExit("shrink: could not obtain ffmpeg ({})".format(exc))
    raise SystemExit(
        "shrink: ffmpeg not found.\n"
        "  Install the bundled build:  pip install imageio-ffmpeg\n"
        "  Or install ffmpeg itself:   winget install Gyan.FFmpeg"
    )


def pick_encoder(ffmpeg: str) -> str:
    """libmp3lame if available, else the built-in mp3 encoder."""
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    if "libmp3lame" in out:
        return "libmp3lame"
    if re.search(r"^\s*A\S*\s+mp3\s", out, re.M):
        return "mp3"
    raise SystemExit(
        "shrink: this ffmpeg build has no MP3 encoder.\n"
        "  Try:  winget install Gyan.FFmpeg"
    )


# ------------------------------------------------------------------- model

@dataclass
class SourceInfo:
    path: Path
    size_bytes: int
    duration_s: float
    codec: str | None = None
    channels: int = 2
    sample_rate: int = 44100
    bitrate_kbps: int | None = None

    @property
    def has_audio(self) -> bool:
        return self.codec is not None


@dataclass
class EncodePlan:
    bitrate_kbps: int
    channels: int
    sample_rate: int
    copy: bool = False
    warning: str | None = None


# ------------------------------------------------------------------ probing

def parse_size(text: str) -> int:
    """'25MB' -> 25_000_000. Decimal by default; use MiB/KiB/GiB for binary."""
    match = re.fullmatch(r"\s*([\d.]+)\s*([A-Za-z]*)\s*", text)
    if not match:
        raise argparse.ArgumentTypeError("cannot parse size {!r}".format(text))
    number, unit = match.group(1), match.group(2).lower()
    if unit not in _UNITS:
        raise argparse.ArgumentTypeError("unknown size unit {!r}".format(match.group(2)))
    try:
        value = float(number)
    except ValueError:
        raise argparse.ArgumentTypeError("cannot parse size {!r}".format(text))
    size = int(value * _UNITS[unit])
    if size <= 0:
        raise argparse.ArgumentTypeError("size must be positive")
    return size


def parse_bitrate(text: str) -> int:
    """'64k' or '64' -> 64 (kbps)."""
    match = re.fullmatch(r"\s*(\d+)\s*([Kk]?)(?:b(?:ps)?)?\s*", text)
    if not match:
        raise argparse.ArgumentTypeError("cannot parse bitrate {!r}".format(text))
    value = int(match.group(1))
    if value > 1000:  # given in bits per second
        value //= 1000
    if not 8 <= value <= 320:
        raise argparse.ArgumentTypeError("bitrate must be between 8 and 320 kbps")
    return value


def _parse_channels(line: str) -> int:
    if re.search(r"\bmono\b", line):
        return 1
    if re.search(r"\bstereo\b", line):
        return 2
    layout = re.search(r"\b(\d+)(?:\.(\d+))? channels?\b", line)
    if layout:
        return int(layout.group(1)) + (int(layout.group(2)) if layout.group(2) else 0)
    if re.search(r"\b5\.1\b", line):
        return 6
    if re.search(r"\b7\.1\b", line):
        return 8
    return 2


def probe(ffmpeg: str, path: Path) -> SourceInfo:
    """Read duration and audio stream details out of `ffmpeg -i` stderr.

    imageio-ffmpeg ships ffmpeg but not ffprobe, so we parse the banner ffmpeg
    prints when asked to read a file with no output. It exits non-zero doing
    this, which is expected.
    """
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostdin", "-i", str(path)],
        capture_output=True, text=True, errors="replace",
    )
    text = result.stderr

    duration_match = _DURATION_RE.search(text)
    if not duration_match:
        raise ValueError("could not read a duration (not a media file?)")
    hours, minutes, seconds = duration_match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    if duration <= 0:
        raise ValueError("file reports zero duration")

    info = SourceInfo(path=path, size_bytes=path.stat().st_size, duration_s=duration)

    for line in text.splitlines():
        audio_match = _AUDIO_RE.search(line)
        if not audio_match:
            continue
        info.codec = audio_match.group(1)
        hz = _HZ_RE.search(line)
        if hz:
            info.sample_rate = int(hz.group(1))
        info.channels = _parse_channels(line)
        kbps = _KBPS_RE.search(line)
        if kbps:
            info.bitrate_kbps = int(kbps.group(1))
        break

    if not info.has_audio:
        raise ValueError("no audio stream found")

    if info.bitrate_kbps is None:
        overall = _OVERALL_KBPS_RE.search(text)
        if overall and info.codec == "mp3":
            info.bitrate_kbps = int(overall.group(1))

    return info


# ------------------------------------------------------------------ sizing

def plan_encode(
    source: SourceInfo,
    cap_bytes: int,
    keep_stereo: bool = False,
    forced_kbps: int | None = None,
) -> EncodePlan:
    """Choose bitrate, channel count and sample rate. Pure - the tests live here."""
    warning = None

    if forced_kbps is None and source.codec == "mp3" and source.size_bytes <= cap_bytes:
        # Already an MP3 that fits. Re-encoding would only add a generation of
        # lossy-to-lossy damage, so copy the stream through untouched.
        return EncodePlan(
            bitrate_kbps=source.bitrate_kbps or 0,
            channels=source.channels,
            sample_rate=source.sample_rate,
            copy=True,
        )

    if forced_kbps is not None:
        bitrate = forced_kbps
    else:
        budget = cap_bytes * HEADROOM
        ideal = (budget * 8) / (source.duration_s * 1000)

        ceiling = MAX_AUTO_KBPS
        if source.bitrate_kbps:
            ceiling = min(ceiling, source.bitrate_kbps)

        allowed = [b for b in LADDER if b <= ceiling] or [LADDER[-1]]
        fits = [b for b in allowed if b <= ideal]
        if fits:
            bitrate = max(fits)
        else:
            bitrate = min(allowed)
            if ideal < bitrate:
                warning = (
                    "{:.0f} min at the lowest usable bitrate ({} kbps) will not fit "
                    "under the cap; encoding anyway".format(
                        math.ceil(source.duration_s / 60), bitrate
                    )
                )

    if keep_stereo:
        # MP3 is mono or stereo only, so a 5.1 source still has to fold down to 2.
        channels = min(source.channels, 2)
        sample_rate = min(source.sample_rate, 44100)
    elif bitrate >= 64:
        channels = min(source.channels, 2)
        sample_rate = min(source.sample_rate, 44100)
    elif bitrate >= 48:
        channels = 1
        sample_rate = min(source.sample_rate, 44100)
    else:
        channels = 1
        sample_rate = min(source.sample_rate, 22050)

    # MPEG-1 (32/44.1/48 kHz) will not encode below 32 kbps - LAME silently bumps
    # the bitrate back up, blowing the size budget. Below that the stream has to
    # drop to MPEG-2 rates, even when the caller asked to keep stereo.
    if bitrate < 32:
        sample_rate = min(sample_rate, 22050)

    if bitrate <= 32 and warning is None:
        warning = "{} kbps is very low; speech stays intelligible, music will not".format(
            bitrate
        )

    return EncodePlan(bitrate, channels, sample_rate, warning=warning)


def next_lower(bitrate: int) -> int | None:
    """The next rung down the ladder, or None at the bottom."""
    lower = [b for b in LADDER if b < bitrate]
    return max(lower) if lower else None


# ----------------------------------------------------------------- encoding

def _format_time(seconds: float) -> str:
    seconds = int(seconds)
    return "{}:{:02d}".format(seconds // 60, seconds % 60)


def _run_with_progress(cmd: list[str], duration_s: float, label: str, quiet: bool) -> None:
    """Run ffmpeg, streaming a one-line progress readout."""
    # ffmpeg's stderr goes to a temp file rather than a pipe: we only drain it
    # after stdout is exhausted, and on a long encode a full pipe would deadlock.
    with tempfile.TemporaryFile(mode="w+", errors="replace") as errfile:
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=errfile,
            text=True, errors="replace", bufsize=1,
        )
        interactive = sys.stderr.isatty()
        last_pct = -10
        for line in process.stdout:
            if quiet or not line.startswith("out_time_us="):
                continue
            raw = line.split("=", 1)[1].strip()
            if not raw.isdigit():
                continue
            done = int(raw) / 1_000_000
            pct = min(100, int(done / duration_s * 100)) if duration_s else 0
            if interactive:
                print(
                    "\r  {}  {} / {}  ({}%)".format(
                        label, _format_time(done), _format_time(duration_s), pct
                    ),
                    end="", file=sys.stderr, flush=True,
                )
            elif pct >= last_pct + 10:
                last_pct = pct
                print("  {}  {}%".format(label, pct), file=sys.stderr, flush=True)
        process.wait()
        errfile.seek(0)
        stderr = errfile.read()
    if not quiet and interactive:
        print("\r" + " " * 70 + "\r", end="", file=sys.stderr, flush=True)
    if process.returncode != 0:
        tail = "\n".join(stderr.strip().splitlines()[-4:])
        raise RuntimeError("ffmpeg failed:\n" + tail)


def encode(
    ffmpeg: str, encoder: str, source: SourceInfo, plan: EncodePlan,
    destination: Path, quiet: bool,
) -> None:
    """Encode to a .part file and rename on success, so an interrupted run
    never leaves a truncated MP3 behind."""
    partial = destination.with_suffix(destination.suffix + ".part")
    cmd = [
        ffmpeg, "-hide_banner", "-nostdin", "-y",
        "-i", str(source.path),
        "-vn", "-map", "0:a:0",
    ]
    if plan.copy:
        cmd += ["-c:a", "copy"]
    else:
        cmd += [
            "-c:a", encoder,
            "-b:a", "{}k".format(plan.bitrate_kbps),
            "-ac", str(plan.channels),
            "-ar", str(plan.sample_rate),
        ]
    cmd += [
        "-map_metadata", "0", "-id3v2_version", "3",
        # The temp file ends in .part, so ffmpeg cannot infer the container.
        "-f", "mp3",
        "-progress", "pipe:1", "-nostats",
        str(partial),
    ]
    try:
        _run_with_progress(cmd, source.duration_s, source.path.name, quiet)
        os.replace(partial, destination)
    finally:
        if partial.exists():
            try:
                partial.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------- CLI

def output_path(source: Path, out_dir: Path | None) -> Path:
    directory = out_dir if out_dir is not None else source.parent
    candidate = directory / (source.stem + ".mp3")
    try:
        collides = candidate.resolve() == source.resolve()
    except OSError:
        collides = False
    if collides:
        # Would overwrite the input; never clobber the source.
        candidate = directory / (source.stem + ".compressed.mp3")
    return candidate


def human(size: int) -> str:
    if size >= 1000**2:
        return "{:.1f} MB".format(size / 1000**2)
    if size >= 1000:
        return "{:.0f} KB".format(size / 1000)
    return "{} B".format(size)


def process(ffmpeg: str, encoder: str, path: Path, args, cap_bytes: int) -> bool:
    if not path.exists():
        print("shrink: {}: no such file".format(path), file=sys.stderr)
        return False
    if path.is_dir():
        print("shrink: {}: is a directory".format(path), file=sys.stderr)
        return False

    try:
        source = probe(ffmpeg, path)
    except ValueError as exc:
        print("shrink: {}: {}".format(path.name, exc), file=sys.stderr)
        return False

    destination = output_path(path, args.out_dir)
    if destination.exists() and not args.force:
        print(
            "shrink: {} already exists (use --force)".format(destination.name),
            file=sys.stderr,
        )
        return False

    plan = plan_encode(source, cap_bytes, args.keep_stereo, args.bitrate)
    if plan.warning and not args.quiet:
        print("  note: {}".format(plan.warning), file=sys.stderr)

    try:
        encode(ffmpeg, encoder, source, plan, destination, args.quiet)
    except RuntimeError as exc:
        print("shrink: {}: {}".format(path.name, exc), file=sys.stderr)
        return False

    size = destination.stat().st_size

    # One retry a rung lower if we overshot despite the headroom.
    if size > cap_bytes and not plan.copy and args.bitrate is None:
        lower = next_lower(plan.bitrate_kbps)
        if lower is not None:
            if not args.quiet:
                print(
                    "  {} still over the cap, retrying at {} kbps".format(
                        human(size), lower
                    ),
                    file=sys.stderr,
                )
            retry = plan_encode(source, cap_bytes, args.keep_stereo, forced_kbps=lower)
            try:
                encode(ffmpeg, encoder, source, retry, destination, args.quiet)
            except RuntimeError as exc:
                print("shrink: {}: {}".format(path.name, exc), file=sys.stderr)
                return False
            plan, size = retry, destination.stat().st_size

    if plan.copy:
        shape = "copied, already under the cap"
    else:
        shape = "{} kbps {} {:g} kHz".format(
            plan.bitrate_kbps,
            "mono" if plan.channels == 1 else "stereo",
            plan.sample_rate / 1000,
        )
    print(
        "{} -> {}  {} -> {}  ({})".format(
            path.name, destination.name, human(source.size_bytes), human(size), shape
        )
    )

    if size > cap_bytes:
        print(
            "shrink: {} is {}, over the {} cap".format(
                destination.name, human(size), human(cap_bytes)
            ),
            file=sys.stderr,
        )
        return False
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shrink",
        description="Convert MP4/MP3 files into compressed MP3s that fit under a size cap.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "sizes are decimal by default (25MB = 25,000,000 bytes);\n"
            "use MiB/KiB/GiB if you want binary units\n"
            "\n"
            "examples:\n"
            "  shrink.py meeting.mp4\n"
            "  shrink.py *.mp4 -o compressed --target-size 10MB\n"
            "  shrink.py podcast.mp3 --bitrate 64k\n"
        ),
    )
    parser.add_argument("inputs", nargs="+", type=Path, metavar="INPUT")
    parser.add_argument("-o", "--out-dir", type=Path, default=None,
                        help="where to write (default: alongside each input)")
    parser.add_argument("--target-size", type=parse_size, default=parse_size(DEFAULT_CAP),
                        metavar="SIZE",
                        help="cap per output file (default: {})".format(DEFAULT_CAP))
    parser.add_argument("--bitrate", type=parse_bitrate, default=None, metavar="RATE",
                        help="force a bitrate (e.g. 64k) and skip target sizing")
    parser.add_argument("--keep-stereo", action="store_true",
                        help="never downmix to mono, only lower the bitrate")
    parser.add_argument("--force", action="store_true", help="overwrite existing output")
    parser.add_argument("--quiet", action="store_true", help="suppress progress output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.out_dir is not None:
        try:
            args.out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SystemExit(
                "shrink: cannot use {} as an output directory: {}".format(
                    args.out_dir, exc.strerror or exc
                )
            )

    ffmpeg = find_ffmpeg()
    encoder = pick_encoder(ffmpeg)

    results = [process(ffmpeg, encoder, p, args, args.target_size) for p in args.inputs]
    if all(results):
        return 0
    if any(results):
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
