"""FFmpeg assembly: swap in Hindi narration, then stitch shots into one video.

Every segment is re-encoded with identical parameters so the final concat can
run as a pure stream copy.
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


class AssemblyError(RuntimeError):
    pass


def ensure_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise AssemblyError(
                f"{tool} not found on PATH. Install it with 'brew install ffmpeg' "
                f"(macOS) or 'sudo apt-get install -y ffmpeg' (Linux/CI)."
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
    clip: Path, narration: Path, destination: Path, aspect_ratio: str
) -> Path:
    """Replace a Veo clip's own audio with Hindi narration, matching durations."""
    width, height = TARGET_SIZE.get(aspect_ratio, TARGET_SIZE["16:9"])
    clip_seconds = probe_duration(clip)
    narration_seconds = probe_duration(narration)

    audio_chain = []
    if narration_seconds > clip_seconds:
        # Gently compress overlong narration rather than freezing the picture
        # for seconds or cutting the sentence off mid-word.
        tempo = min(MAX_SPEEDUP, narration_seconds / clip_seconds)
        audio_chain.append(f"atempo={tempo:.4f}")
        narration_seconds /= tempo
    audio_chain.append("apad")  # pad the tail with silence

    segment_seconds = max(clip_seconds, narration_seconds)
    pad_seconds = max(0.0, segment_seconds - clip_seconds)

    video_chain = [
        f"scale={width}:{height}:force_original_aspect_ratio=increase",
        f"crop={width}:{height}",
    ]
    if pad_seconds > 0.05:
        video_chain.append(f"tpad=stop_mode=clone:stop_duration={pad_seconds:.3f}")

    filter_complex = (
        f"[0:v]{','.join(video_chain)}[v];[1:a]{','.join(audio_chain)}[a]"
    )

    destination.parent.mkdir(parents=True, exist_ok=True)
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


def normalize(source: Path, destination: Path, aspect_ratio: str) -> Path:
    """Re-encode an external clip (e.g. the HeyGen mascot) to match segment settings."""
    width, height = TARGET_SIZE.get(aspect_ratio, TARGET_SIZE["16:9"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg", "-y",
            "-i", str(source),
            "-vf", (
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height}"
            ),
            "-af", "aresample=async=1",
            *VIDEO_ARGS,
            str(destination),
        ]
    )
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
