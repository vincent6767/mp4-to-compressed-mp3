"""Tests for the bitrate-selection math - the only part of shrink with real logic.

None of these touch ffmpeg; plan_encode is pure.
"""
import argparse
from pathlib import Path

import pytest

from shrink import (
    SourceInfo,
    next_lower,
    output_path,
    parse_bitrate,
    parse_size,
    plan_encode,
)

MB = 1000 * 1000
CAP_25 = 25 * MB


def source(duration_s, *, codec="aac", channels=2, sample_rate=44100,
           bitrate_kbps=192, size_bytes=400 * MB):
    return SourceInfo(
        path=Path("sample.mp4"),
        size_bytes=size_bytes,
        duration_s=duration_s,
        codec=codec,
        channels=channels,
        sample_rate=sample_rate,
        bitrate_kbps=bitrate_kbps,
    )


# ------------------------------------------------------------- bitrate math

def test_short_file_caps_at_128_rather_than_filling_the_budget():
    # 10 min under a 25 MB cap could afford ~320 kbps; we deliberately stop at 128.
    plan = plan_encode(source(10 * 60), CAP_25)
    assert plan.bitrate_kbps == 128
    assert plan.channels == 2
    assert plan.sample_rate == 44100


def test_hour_long_file_lands_on_a_rung_that_fits():
    plan = plan_encode(source(60 * 60), CAP_25)
    assert plan.bitrate_kbps == 48
    # 48 kbps for 3600 s must come in under the cap.
    assert plan.bitrate_kbps * 1000 / 8 * 3600 < CAP_25


def test_never_upscales_past_the_source_bitrate():
    # A 64 kbps podcast under a generous cap stays at 64, not 128.
    plan = plan_encode(source(10 * 60, bitrate_kbps=64), 100 * MB)
    assert plan.bitrate_kbps == 64


def test_chosen_bitrate_always_fits_when_one_exists():
    for minutes in (5, 20, 45, 90, 150):
        duration = minutes * 60
        plan = plan_encode(source(duration), CAP_25)
        predicted = plan.bitrate_kbps * 1000 / 8 * duration
        assert predicted <= CAP_25, (minutes, plan.bitrate_kbps)


def test_impossible_target_bottoms_out_and_warns_instead_of_failing():
    # 4 hours under 10 MB cannot fit even at 8 kbps.
    plan = plan_encode(source(4 * 60 * 60), 10 * MB)
    assert plan.bitrate_kbps == 8
    assert plan.warning is not None
    assert "not fit" in plan.warning


def test_low_bitrate_warns_about_music():
    plan = plan_encode(source(3 * 60 * 60), CAP_25)
    assert plan.bitrate_kbps <= 32
    assert "music" in plan.warning


# ------------------------------------------------------------- audio shape

@pytest.mark.parametrize("bitrate,channels,sample_rate", [
    (128, 2, 44100),
    (64, 2, 44100),
    (56, 1, 44100),
    (48, 1, 44100),
    (40, 1, 22050),
    (8, 1, 22050),
])
def test_shape_degrades_with_bitrate(bitrate, channels, sample_rate):
    plan = plan_encode(source(600), CAP_25, forced_kbps=bitrate)
    assert (plan.channels, plan.sample_rate) == (channels, sample_rate)


def test_keep_stereo_never_downmixes():
    plan = plan_encode(source(600), CAP_25, keep_stereo=True, forced_kbps=32)
    assert plan.channels == 2


def test_mono_source_stays_mono():
    plan = plan_encode(source(600, channels=1), CAP_25)
    assert plan.channels == 1


def test_never_upsamples_a_low_rate_source():
    plan = plan_encode(source(600, sample_rate=16000), CAP_25)
    assert plan.sample_rate == 16000


def test_high_rate_source_comes_down_to_44100():
    plan = plan_encode(source(600, sample_rate=48000), CAP_25)
    assert plan.sample_rate == 44100


# ------------------------------------------------------------- passthrough

def test_mp3_already_under_the_cap_is_copied():
    plan = plan_encode(
        source(600, codec="mp3", bitrate_kbps=128, size_bytes=9 * MB), CAP_25
    )
    assert plan.copy is True


def test_mp3_over_the_cap_is_re_encoded():
    plan = plan_encode(
        source(60 * 60, codec="mp3", bitrate_kbps=128, size_bytes=57 * MB), CAP_25
    )
    assert plan.copy is False
    assert plan.bitrate_kbps == 48


def test_explicit_bitrate_overrides_passthrough():
    plan = plan_encode(
        source(600, codec="mp3", bitrate_kbps=128, size_bytes=9 * MB),
        CAP_25, forced_kbps=64,
    )
    assert plan.copy is False
    assert plan.bitrate_kbps == 64


# ------------------------------------------------------------------ parsing

@pytest.mark.parametrize("text,expected", [
    ("25MB", 25_000_000),
    ("25mb", 25_000_000),
    ("25M", 25_000_000),
    ("10", 10),
    ("1.5GB", 1_500_000_000),
    ("25MiB", 26_214_400),
    ("500KB", 500_000),
])
def test_parse_size(text, expected):
    assert parse_size(text) == expected


@pytest.mark.parametrize("text", ["", "MB", "-5MB", "0", "25 furlongs"])
def test_parse_size_rejects_junk(text):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_size(text)


@pytest.mark.parametrize("text,expected", [
    ("64k", 64), ("64", 64), ("64kbps", 64), ("128000", 128), ("320k", 320),
])
def test_parse_bitrate(text, expected):
    assert parse_bitrate(text) == expected


@pytest.mark.parametrize("text", ["0", "500k", "abc", ""])
def test_parse_bitrate_rejects_junk(text):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_bitrate(text)


def test_next_lower():
    assert next_lower(128) == 112
    assert next_lower(48) == 40
    assert next_lower(8) is None


# -------------------------------------------------------------- file naming

def test_output_path_does_not_clobber_an_mp3_input(tmp_path):
    src = tmp_path / "podcast.mp3"
    src.write_bytes(b"x")
    assert output_path(src, None).name == "podcast.compressed.mp3"


def test_mp4_input_yields_plain_mp3_name(tmp_path):
    src = tmp_path / "meeting.mp4"
    src.write_bytes(b"x")
    assert output_path(src, None).name == "meeting.mp3"


def test_out_dir_avoids_the_collision(tmp_path):
    src = tmp_path / "podcast.mp3"
    src.write_bytes(b"x")
    out = tmp_path / "compressed"
    assert output_path(src, out) == out / "podcast.mp3"


# ------------------------------------------- MPEG-1 / MPEG-2 rate constraint

def test_sub_32kbps_forces_a_low_sample_rate_even_with_keep_stereo():
    # MPEG-1 at 44.1 kHz cannot go below 32 kbps; LAME would silently bump the
    # bitrate back up and blow the size budget.
    plan = plan_encode(source(600), CAP_25, keep_stereo=True, forced_kbps=16)
    assert plan.channels == 2
    assert plan.sample_rate <= 24000


@pytest.mark.parametrize("bitrate", [8, 16, 24])
def test_low_bitrates_never_pair_with_mpeg1_sample_rates(bitrate):
    for keep_stereo in (False, True):
        plan = plan_encode(source(600), CAP_25, keep_stereo=keep_stereo,
                           forced_kbps=bitrate)
        assert plan.sample_rate <= 24000, (bitrate, keep_stereo)


def test_surround_source_folds_to_stereo_because_mp3_has_no_5_1():
    for keep_stereo in (False, True):
        plan = plan_encode(source(600, channels=6), CAP_25, keep_stereo=keep_stereo)
        assert plan.channels <= 2, keep_stereo
