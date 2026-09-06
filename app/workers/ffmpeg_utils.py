"""ffmpeg/ffprobe subprocess wrappers — the actual transcoding engine.

Mirrors the exact subprocess pattern already proven in
backend/kis/apps/broadcasts/media_utils.py and views.py's
_probe_video_duration/_create_thumbnail: plain subprocess.run with
check=True, captured/discarded output, and every failure funneled through
a narrow, typed exception rather than letting a raw CalledProcessError (or
worse, a silent wrong-looking success) reach the Celery task. Those two
existing functions only needed a duration float and a single JPEG frame;
this file does the same style of call for the additional things a real
transcode needs — full probe (duration + resolution), an HLS rendition,
and a master playlist — none of which existed anywhere in this codebase
before, so there was nothing further to mirror for those beyond the
subprocess-invocation *pattern* itself.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass


class FfmpegError(Exception):
    """Raised for any ffmpeg/ffprobe failure — callers (the Celery tasks)
    catch this specifically to set TranscodeJob.status = 'failed' with a
    real error_message, rather than letting a bare CalledProcessError with
    a truncated/binary stderr blob become that message."""


@dataclass(frozen=True)
class ProbeResult:
    duration_seconds: float
    width: int
    height: int


@dataclass(frozen=True)
class Rendition:
    height: int
    bitrate_kbps: int
    # ffmpeg's -maxrate/-bufsize convention: video bitrate target, audio
    # fixed separately below. Values are the same rough ladder YouTube/Mux
    # use for these resolutions — not derived from anything in this
    # codebase (nothing here transcoded video before), chosen as reasonable
    # defaults rather than guessed low/high.
    playlist_filename: str


# Ordered highest-to-lowest so `renditions_for_source` can stop at the
# first one <= the source height without scanning the whole list.
RENDITION_LADDER: list[Rendition] = [
    Rendition(height=1080, bitrate_kbps=5000, playlist_filename="1080p.m3u8"),
    Rendition(height=720, bitrate_kbps=2800, playlist_filename="720p.m3u8"),
    Rendition(height=480, bitrate_kbps=1400, playlist_filename="480p.m3u8"),
    Rendition(height=360, bitrate_kbps=800, playlist_filename="360p.m3u8"),
]

# Fixed per ARCHITECTURE.md's HLS segment format decision — fMP4 (CMAF)
# rather than legacy .ts segments. Modern HLS players (including the
# kistube-website HlsVideo.tsx player this feeds) support CMAF natively,
# and a single fMP4 segment format is also what would be needed if this
# service ever serves DASH from the same renditions later — .ts would be a
# dead end for that, CMAF isn't.
HLS_SEGMENT_DURATION_SECONDS = 6


def probe(source_path: str) -> ProbeResult:
    """Runs a single ffprobe call for duration + resolution together
    (format=duration and stream=width,height in one invocation, not two
    separate probes like the Django code's split
    _probe_video_duration/nothing-for-resolution — resolution was never
    needed there since that codebase never produced renditions)."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height:format=duration",
        "-of",
        "json",
        source_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise FfmpegError(f"ffprobe timed out after {exc.timeout}s") from exc
    except subprocess.CalledProcessError as exc:
        raise FfmpegError(f"ffprobe failed: {exc.stderr.strip()[:500]}") from exc

    try:
        data = json.loads(result.stdout)
        stream = (data.get("streams") or [{}])[0]
        width = int(stream["width"])
        height = int(stream["height"])
        duration = float(data["format"]["duration"])
    except (KeyError, ValueError, IndexError, json.JSONDecodeError) as exc:
        raise FfmpegError(f"ffprobe returned unexpected output: {result.stdout[:500]}") from exc

    return ProbeResult(duration_seconds=duration, width=width, height=height)


def renditions_for_source(source_height: int) -> list[Rendition]:
    """Only renditions <= source resolution — never upscale a low-res
    source into a fake higher-quality rendition. A source at exactly one
    of the ladder heights (e.g. a real 720p upload) includes that height
    and everything below it, not just the ones strictly smaller."""
    matches = [r for r in RENDITION_LADDER if r.height <= source_height]
    # A source smaller than the entire ladder's lowest rung (e.g. an old
    # 240p upload) still needs at least one playable rendition — transcode
    # it at its own native height rather than producing nothing.
    if not matches:
        return [Rendition(height=source_height, bitrate_kbps=600, playlist_filename="source.m3u8")]
    return matches


def transcode_rendition(source_path: str, output_dir: str, rendition: Rendition) -> str:
    """Produces one HLS rendition (its own .m3u8 + fMP4 segments) inside
    output_dir. Returns the absolute path to that rendition's .m3u8.

    -2 in scale=-2:height keeps ffmpeg's own even-dimension requirement for
    H.264 (odd widths fail to encode) while deriving width from the
    source's aspect ratio automatically, rather than hardcoding a 16:9
    width per rendition height that would letterbox/distort any source
    shot in a different aspect ratio (portrait phone video, etc.).
    """
    os.makedirs(output_dir, exist_ok=True)
    playlist_path = os.path.join(output_dir, rendition.playlist_filename)
    segment_pattern = os.path.join(output_dir, f"{rendition.height}p_%04d.m4s")
    init_segment_path = os.path.join(output_dir, f"{rendition.height}p_init.mp4")

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        source_path,
        "-vf",
        f"scale=-2:{rendition.height}",
        "-c:v",
        # libx264 is the real encoder name — "h264" is not a valid ffmpeg
        # encoder identifier (it's the codec *name*, which ffmpeg exposes
        # for decoding/probing, but -c:v needs one of the concrete encoder
        # implementations: libx264 in software, or a hardware-specific
        # variant like h264_nvenc/h264_qsv/h264_vaapi — none of which exist
        # on a plain GPU-less VM such as the Lightsail box this runs on.
        # Verified directly against that server's real ffmpeg build, not
        # assumed — every job would otherwise fail immediately with
        # "Unknown encoder 'h264'" (loudly, not silently: caught below as a
        # CalledProcessError -> FfmpegError -> job marked 'failed' with a
        # real message, so this was never a data-corruption risk — just a
        # 100% job-failure rate until fixed).
        "libx264",
        # Explicit rather than relying on libx264's own default (medium) -
        # bitrate is already fixed by -b:v/-maxrate/-bufsize below (correct
        # for an HLS bitrate ladder, where each rung needs a predictable
        # bitrate for the player's ABR switching logic - -crf would produce
        # variable, unpredictable output size and defeat that), so a slower
        # preset here only spends more CPU/wall-clock time for a marginal
        # quality gain AT that same fixed bitrate - not worth it for a
        # background queue job on a plain, GPU-less VM.
        "-preset",
        "veryfast",
        "-b:v",
        f"{rendition.bitrate_kbps}k",
        "-maxrate",
        f"{int(rendition.bitrate_kbps * 1.07)}k",
        "-bufsize",
        f"{rendition.bitrate_kbps * 2}k",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ac",
        "2",
        "-f",
        "hls",
        "-hls_time",
        str(HLS_SEGMENT_DURATION_SECONDS),
        "-hls_playlist_type",
        "vod",
        "-hls_segment_type",
        "fmp4",
        "-hls_fmp4_init_filename",
        os.path.basename(init_segment_path),
        "-hls_segment_filename",
        segment_pattern,
        playlist_path,
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=3600)
    except subprocess.TimeoutExpired as exc:
        raise FfmpegError(f"ffmpeg timed out transcoding {rendition.height}p after {exc.timeout}s") from exc
    except subprocess.CalledProcessError as exc:
        raise FfmpegError(f"ffmpeg failed transcoding {rendition.height}p: {exc.stderr.strip()[:1000]}") from exc

    if not os.path.exists(playlist_path):
        raise FfmpegError(f"ffmpeg reported success but {playlist_path} was never created")
    return playlist_path


def write_master_playlist(output_dir: str, renditions: list[Rendition]) -> str:
    """Writes the top-level .m3u8 referencing every rendition — standard
    HLS master-playlist format (RFC 8216), the same shape any HLS player
    (including kistube-website's HlsVideo.tsx, which already plays real
    Mux-produced master playlists today) already knows how to read. No
    Mux-specific or custom fields — deliberately the plainest possible
    valid master playlist, since "the client needs zero changes" is the
    entire point per ARCHITECTURE.md.

    BANDWIDTH is required by the spec and is what a player uses to pick a
    starting rendition — derived from each rendition's own video+audio
    bitrate target (128k audio, fixed above) converted to bits/sec, not a
    real measured value (nothing here measures actual encoded bitrate,
    which can legitimately vary a little from the -b:v target) — close
    enough for ABR selection, which is inherently approximate and
    re-adjusts during playback anyway.
    """
    lines = ["#EXTM3U", "#EXT-X-VERSION:7"]
    for rendition in sorted(renditions, key=lambda r: r.height, reverse=True):
        bandwidth_bps = (rendition.bitrate_kbps + 128) * 1000
        # Standard 16:9 signaling for RESOLUTION — a real width isn't known
        # at this point without re-probing the rendition's own output, and
        # RESOLUTION here is informational for the player's own rendition
        # picker, not something played content is validated against.
        width = int(rendition.height * 16 / 9)
        lines.append(
            f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth_bps},RESOLUTION={width}x{rendition.height}'
        )
        lines.append(rendition.playlist_filename)

    master_path = os.path.join(output_dir, "master.m3u8")
    with open(master_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return master_path


def create_thumbnail(source_path: str, dest_path: str) -> bool:
    """Identical to media_utils.py's _create_thumbnail — same command,
    same behavior (grabs the frame at 1s, scales to 320px wide, returns
    False + cleans up a partial file on any failure instead of raising).
    Kept as a bool-returning function rather than raising FfmpegError like
    the functions above, matching that file's own convention exactly,
    since a missing thumbnail is a degraded-but-still-usable outcome
    (Asset.thumbnail_url is nullable) — not a reason to fail the whole job
    the way a missing rendition would be.
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        source_path,
        "-ss",
        "00:00:01",
        "-frames:v",
        "1",
        "-vf",
        "scale=320:-1",
        dest_path,
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        return os.path.exists(dest_path)
    except Exception:
        if os.path.exists(dest_path):
            os.remove(dest_path)
        return False
