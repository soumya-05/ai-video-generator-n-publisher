"""FFmpeg assembly: swap in the narration, burn in captions, then
stitch shots into one video.

Every segment is re-encoded with identical parameters so the final concat can
run as a pure stream copy. Subtitles are burned per segment rather than over the
finished film, because the segment encode is happening anyway and each shot's
exact duration is already known here.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

TARGET_SIZE = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080)}

VIDEO_ARGS = [
    "-c:v", "libx264",
    "-preset", "medium",
    "-crf", "20",
    "-pix_fmt", "yuv420p",
    "-r", "24",
    "-c:a", "aac",
    "-b:a", "192k",
    "-ar", "44100",
    "-ac", "2",
]

# Beyond this, speeding the narration up would sound unnatural.
MAX_SPEEDUP = 1.3

# A breath between one line and the next. Anything longer reads as a mistake:
# the sentence has ended, the picture is still going, and nothing is happening.
TAIL_SECONDS = 0.25


class AssemblyError(RuntimeError):
    pass


def ensure_ffmpeg(need_subtitles: bool = False) -> None:
    """Verify ffmpeg can do everything this run needs, before anything is paid for."""
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise AssemblyError(
                f"{tool} not found on PATH. Install it with 'brew install ffmpeg' "
                f"(macOS) or 'sudo apt-get install -y ffmpeg' (Linux/CI)."
            )
    if need_subtitles and not _has_filter("subtitles"):
        raise AssemblyError(
            "This ffmpeg was built without libass, so English subtitles cannot be "
            "burned in. Install a full build ('brew reinstall ffmpeg' on macOS, "
            "'sudo apt-get install -y ffmpeg' on Linux/CI) or set "
            "subtitles.enabled: false in config.yaml."
        )


def _has_filter(name: str) -> bool:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True
    )
    return any(
        line.split()[1:2] == [name]
        for line in result.stdout.splitlines()
        if len(line.split()) > 1
    )


def _run(args: List[str]) -> None:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-12:]
        raise AssemblyError(
            f"{args[0]} failed ({result.returncode}):\n" + "\n".join(tail)
        )


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssemblyError(f"ffprobe failed for {path}: {result.stderr[:200]}")
    return float(json.loads(result.stdout)["format"]["duration"])


def build_segment(
    clip: Path,
    narration: Path,
    destination: Path,
    aspect_ratio: str,
    subtitle: Optional[str] = None,
    subtitle_style: Optional[dict] = None,
) -> Path:
    """Replace a Veo clip's own audio with the narration, matching durations.

    The narration decides how long the shot is, not the clip. Veo always returns
    a full 8 seconds, but a spoken line is rarely exactly 8 seconds long, so
    holding every shot open for the clip's full length left a pocket of silence
    before each cut - eight of them in a one-minute video, which is what made
    the result feel broken. The clip is trimmed to the line instead.
    """
    width, height = TARGET_SIZE.get(aspect_ratio, TARGET_SIZE["16:9"])
    clip_seconds = probe_duration(clip)
    narration_seconds = probe_duration(narration)

    audio_chain = []
    if narration_seconds + TAIL_SECONDS > clip_seconds:
        # Gently compress overlong narration rather than freezing the picture
        # for seconds or cutting the sentence off mid-word.
        tempo = min(MAX_SPEEDUP, (narration_seconds + TAIL_SECONDS) / clip_seconds)
        audio_chain.append(f"atempo={tempo:.4f}")
        narration_seconds /= tempo
    audio_chain.append("apad")  # a beat of silence under the tail of the shot

    segment_seconds = narration_seconds + TAIL_SECONDS
    # Only needed when the line still outruns the clip at maximum speedup.
    pad_seconds = max(0.0, segment_seconds - clip_seconds)

    video_chain = [
        f"scale={width}:{height}:force_original_aspect_ratio=increase",
        f"crop={width}:{height}",
    ]
    if pad_seconds > 0.05:
        video_chain.append(f"tpad=stop_mode=clone:stop_duration={pad_seconds:.3f}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    if subtitle:
        # Burned in, not a sidecar track: Shorts do not reliably show captions.
        ass_path = destination.with_suffix(".ass")
        _write_ass(
            subtitle, segment_seconds, ass_path, width, height, subtitle_style or {}
        )
        video_chain.append(f"subtitles={_escape_filter_path(ass_path)}")

    filter_complex = (
        f"[0:v]{','.join(video_chain)}[v];[1:a]{','.join(audio_chain)}[a]"
    )

    _run(
        [
            "ffmpeg", "-y",
            "-i", str(clip),
            "-i", str(narration),
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "[a]",
            "-t", f"{segment_seconds:.3f}",
            *VIDEO_ARGS,
            str(destination),
        ]
    )
    return destination


def _escape_filter_path(path: Path) -> str:
    """Quote a path for use inside an ffmpeg filtergraph argument."""
    escaped = str(path).replace("\\", "\\\\").replace("'", r"\'").replace(":", r"\:")
    return f"'{escaped}'"


def _ass_time(seconds: float) -> str:
    hours, rest = divmod(max(0.0, seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{int(hours)}:{int(minutes):02d}:{secs:05.2f}"


def _split_cues(text: str, seconds: float, words_per_cue: int) -> List[tuple]:
    """Break one shot's narration into short cues spread across the clip.

    Showing a whole 8-second line at once is a wall of text. Words are a good
    enough proxy for speaking time here, so each cue is given a share of the
    clip proportional to its word count.
    """
    words = text.split()
    if not words:
        return []
    chunks = [
        " ".join(words[i : i + words_per_cue])
        for i in range(0, len(words), words_per_cue)
    ]
    cues, elapsed = [], 0.0
    for chunk in chunks:
        span = seconds * len(chunk.split()) / len(words)
        cues.append((elapsed, elapsed + span, chunk))
        elapsed += span
    # Absorb rounding into the last cue so it runs to the end of the clip.
    start, _, chunk = cues[-1]
    cues[-1] = (start, seconds, chunk)
    return cues


def _write_ass(
    text: str, seconds: float, destination: Path, width: int, height: int, style: dict
) -> Path:
    """Write an ASS subtitle file sized in real pixels.

    An SRT would be simpler, but ffmpeg gives SRT a fixed 384x288 canvas and
    then scales it, so the on-screen size depends on the video resolution in a
    way that is hard to reason about. Declaring PlayRes as the actual frame size
    means Fontsize and MarginV below are literal pixels.

    BorderStyle 1 draws an outline and shadow around the glyphs only. Style 3
    would paint an opaque box from BackColour, which covers a large part of a
    vertical frame.
    """
    cues = _split_cues(text, seconds, style.get("words_per_cue", 5))
    events = "\n".join(
        f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,"
        # \N is the ASS hard line break; libass wraps the rest on its own.
        + chunk.replace("\n", r"\N")
        for start, end, chunk in cues
    )
    content = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{style.get('font', 'DejaVu Sans')},{style.get('font_size', 52)},\
{style.get('primary_colour', '&H00FFFFFF')},&H000000FF,\
{style.get('outline_colour', '&H00000000')},{style.get('back_colour', '&HFF000000')},\
-1,0,0,0,100,100,0,0,{style.get('border_style', 1)},{style.get('outline', 3)},\
{style.get('shadow', 1)},2,\
{style.get('margin_h', 120)},{style.get('margin_h', 120)},{style.get('margin_v', 300)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
{events}
"""
    destination.write_text(content, encoding="utf-8")
    return destination


def concat(segments: List[Path], destination: Path) -> Path:
    if not segments:
        raise AssemblyError("Nothing to concatenate")

    destination.parent.mkdir(parents=True, exist_ok=True)
    listing = destination.parent / f"{destination.stem}_concat.txt"
    listing.write_text(
        "\n".join(f"file '{segment.resolve()}'" for segment in segments),
        encoding="utf-8",
    )
    _run(
        [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(listing),
            "-c", "copy",
            str(destination),
        ]
    )
    listing.unlink(missing_ok=True)
    return destination


def mix_background_music(
    video: Path, music: Path, destination: Path, music_volume: float = 0.12
) -> Path:
    """Duck a music bed under the existing narration."""
    _run(
        [
            "ffmpeg", "-y",
            "-i", str(video),
            "-stream_loop", "-1", "-i", str(music),
            "-filter_complex",
            f"[1:a]volume={music_volume}[m];[0:a][m]amix=inputs=2:duration=first[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            str(destination),
        ]
    )
    return destination


def last_frame(video: Path, destination: Path) -> Path:
    """Grab the final frame, to open the next shot on.

    -sseof seeks to one second before the end and -update overwrites the same
    file for every frame after it, so what survives is the last frame. Asking
    for an exact end timestamp instead tends to land past the final decodable
    frame and produce nothing.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg", "-y",
            "-sseof", "-1",
            "-i", str(video),
            "-update", "1",
            "-q:v", "2",
            str(destination),
        ]
    )
    return destination


def thumbnail(video: Path, destination: Path, at_seconds: float = 1.5) -> Optional[Path]:
    try:
        _run(
            [
                "ffmpeg", "-y",
                "-ss", str(at_seconds),
                "-i", str(video),
                "-frames:v", "1",
                "-q:v", "2",
                str(destination),
            ]
        )
        return destination
    except AssemblyError:
        return None
