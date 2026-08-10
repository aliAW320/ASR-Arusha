#!/usr/bin/env python3
"""Enroll and identify speakers using pyannote speaker embeddings.

Install:
    pip install "pyannote.audio>=4,<5" "faster-whisper>=1.2,<2" numpy torch

Examples:
    # Enrollment audio must contain only Amir's voice.
    python speaker_detector.py enroll --name Amir amir_1.wav amir_2.wav

    python speaker_detector.py enroll --name Sara sara_1.wav sara_2.wav

    # output.json is the diarization/transcription JSON produced by test.ipynb.
    python speaker_detector.py identify \
        --audio meeting.wav \
        --segments output.json \
        --output named_output.json

The output keeps the original four-field format and replaces SPEAKER_XX with a
known name. Uncertain matches remain UNKNOWN_SPEAKER_XX.
"""

from __future__ import annotations

import argparse
import json
import os
import warnings
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import torch
from faster_whisper.audio import decode_audio

# This script always passes preloaded audio, so pyannote never uses torchcodec.
warnings.filterwarnings(
    "ignore",
    message=r"\s*torchcodec is not installed correctly.*",
    category=UserWarning,
)

from pyannote.audio import Inference, Model
from pyannote.core import Segment


EMBEDDING_MODEL_ID = "pyannote/wespeaker-voxceleb-resnet34-LM"
SAMPLE_RATE = 16_000
DEFAULT_DATABASE = Path("speakers.json")
MIN_SEGMENT_SECONDS = 1.0


def normalize(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))
    if norm < 1e-12:
        raise ValueError("Speaker embedding has zero norm.")
    return vector / norm


def load_audio(path: Path) -> dict[str, Any]:
    """Decode with PyAV and preload audio to avoid torchcodec/FFmpeg API issues."""
    if not path.is_file():
        raise FileNotFoundError(path)
    waveform = decode_audio(str(path), sampling_rate=SAMPLE_RATE)
    return {
        "waveform": torch.from_numpy(waveform).unsqueeze(0),
        "sample_rate": SAMPLE_RATE,
    }


def create_inference(token: str | None) -> Inference:
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = Model.from_pretrained(
        EMBEDDING_MODEL_ID,
        token=token,
        map_location=device,
    )
    if model is None:
        raise RuntimeError(f"Could not load embedding model: {EMBEDDING_MODEL_ID}")
    return Inference(model, window="whole", device=device)


def extract_whole_embedding(inference: Inference, audio_path: Path) -> np.ndarray:
    audio = load_audio(audio_path)
    embedding = inference(audio)
    return normalize(np.asarray(embedding))


def extract_crop_embedding(
    inference: Inference,
    audio: dict[str, Any],
    start: float,
    end: float,
) -> np.ndarray:
    embedding = inference.crop(audio, Segment(start, end))
    return normalize(np.asarray(embedding))


def empty_database() -> dict[str, Any]:
    return {
        "version": 1,
        "embedding_model": EMBEDDING_MODEL_ID,
        "speakers": {},
    }


def load_database(path: Path, allow_missing: bool = False) -> dict[str, Any]:
    if not path.exists():
        if allow_missing:
            return empty_database()
        raise FileNotFoundError(
            f"Speaker database does not exist: {path}. Run the enroll command first."
        )
    with path.open("r", encoding="utf-8") as file:
        database = json.load(file)
    if database.get("embedding_model") != EMBEDDING_MODEL_ID:
        raise ValueError("Database was created with a different embedding model.")
    return database


def save_database(path: Path, database: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(database, file, ensure_ascii=False, indent=2)
    temporary.replace(path)


def enroll(args: argparse.Namespace) -> None:
    inference = create_inference(args.token)
    embeddings = []
    for audio_path in args.audio:
        embedding = extract_whole_embedding(inference, audio_path)
        embeddings.append(embedding)
        print(f"embedded enrollment file: {audio_path}")

    new_centroid = normalize(np.mean(embeddings, axis=0))
    database = load_database(args.db, allow_missing=True)
    existing = database["speakers"].get(args.name)

    if existing and not args.replace:
        old_count = int(existing["sample_count"])
        old_centroid = normalize(np.asarray(existing["embedding"], dtype=np.float32))
        combined = old_centroid * old_count + new_centroid * len(embeddings)
        centroid = normalize(combined)
        sample_count = old_count + len(embeddings)
    else:
        centroid = new_centroid
        sample_count = len(embeddings)

    database["speakers"][args.name] = {
        "embedding": centroid.tolist(),
        "sample_count": sample_count,
    }
    save_database(args.db, database)
    print(f"enrolled {args.name!r} with {sample_count} sample(s) in {args.db}")


def read_segments(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as file:
        segments = json.load(file)
    if not isinstance(segments, list):
        raise ValueError("Segments JSON must be a list.")
    required = {"speaker", "start", "end", "text"}
    for index, segment in enumerate(segments):
        missing = required.difference(segment)
        if missing:
            raise ValueError(f"Segment {index} is missing fields: {sorted(missing)}")
    return segments


def aggregate_diarized_speakers(
    inference: Inference,
    audio: dict[str, Any],
    segments: list[dict[str, Any]],
) -> dict[str, np.ndarray]:
    grouped: dict[str, list[tuple[np.ndarray, float]]] = defaultdict(list)
    audio_duration = audio["waveform"].shape[-1] / SAMPLE_RATE

    for segment in segments:
        start = max(0.0, float(segment["start"]))
        end = min(audio_duration, float(segment["end"]))
        duration = end - start
        if duration < MIN_SEGMENT_SECONDS:
            continue
        embedding = extract_crop_embedding(inference, audio, start, end)
        grouped[str(segment["speaker"])].append((embedding, duration))

    centroids: dict[str, np.ndarray] = {}
    for anonymous_speaker, items in grouped.items():
        embeddings = np.stack([embedding for embedding, _ in items])
        weights = np.asarray([duration for _, duration in items], dtype=np.float32)
        centroids[anonymous_speaker] = normalize(
            np.average(embeddings, axis=0, weights=weights)
        )
    return centroids


def match_speaker(
    embedding: np.ndarray,
    known_speakers: dict[str, Any],
    threshold: float,
    min_margin: float,
) -> tuple[str | None, float, float]:
    scores = sorted(
        (
            float(np.dot(embedding, normalize(np.asarray(record["embedding"])))),
            name,
        )
        for name, record in known_speakers.items()
    )
    if not scores:
        return None, float("-inf"), 0.0

    best_score, best_name = scores[-1]
    second_score = scores[-2][0] if len(scores) > 1 else -1.0
    margin = best_score - second_score
    if best_score < threshold or margin < min_margin:
        return None, best_score, margin
    return best_name, best_score, margin


def identify(args: argparse.Namespace) -> None:
    database = load_database(args.db)
    if not database["speakers"]:
        raise RuntimeError("Speaker database is empty.")

    segments = read_segments(args.segments)
    audio = load_audio(args.audio)
    inference = create_inference(args.token)
    centroids = aggregate_diarized_speakers(inference, audio, segments)

    mapping: dict[str, str] = {}
    for anonymous_speaker in sorted({str(item["speaker"]) for item in segments}):
        embedding = centroids.get(anonymous_speaker)
        if embedding is None:
            mapping[anonymous_speaker] = f"UNKNOWN_{anonymous_speaker}"
            print(f"{anonymous_speaker} -> UNKNOWN (no segment >= {MIN_SEGMENT_SECONDS}s)")
            continue

        name, similarity, margin = match_speaker(
            embedding,
            database["speakers"],
            threshold=args.threshold,
            min_margin=args.min_margin,
        )
        mapped_name = name if name is not None else f"UNKNOWN_{anonymous_speaker}"
        mapping[anonymous_speaker] = mapped_name
        print(
            f"{anonymous_speaker} -> {mapped_name} "
            f"(cosine={similarity:.3f}, margin={margin:.3f})"
        )

    output = [
        {
            "speaker": mapping[str(segment["speaker"])],
            "start": float(segment["start"]),
            "end": float(segment["end"]),
            "text": str(segment["text"]),
        }
        for segment in segments
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as file:
        json.dump(output, file, ensure_ascii=False, indent=2)
    print(f"saved named transcript: {args.output}")


def list_speakers(args: argparse.Namespace) -> None:
    database = load_database(args.db)
    for name, record in sorted(database["speakers"].items()):
        print(f"{name}: {record['sample_count']} enrollment sample(s)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    enroll_parser = subparsers.add_parser("enroll", help="add a known speaker")
    enroll_parser.add_argument("--name", required=True, help="person name or stable ID")
    enroll_parser.add_argument(
        "audio", type=Path, nargs="+", help="clean single-speaker enrollment recordings"
    )
    enroll_parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    enroll_parser.add_argument("--replace", action="store_true")
    enroll_parser.add_argument("--token", default=os.environ.get("HF_TOKEN"))
    enroll_parser.set_defaults(handler=enroll)

    identify_parser = subparsers.add_parser(
        "identify", help="replace diarization labels with known names"
    )
    identify_parser.add_argument("--audio", type=Path, required=True)
    identify_parser.add_argument("--segments", type=Path, required=True)
    identify_parser.add_argument("--output", type=Path, default=Path("named_output.json"))
    identify_parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    identify_parser.add_argument(
        "--threshold", type=float, default=0.55,
        help="minimum cosine similarity; calibrate using your own recordings",
    )
    identify_parser.add_argument(
        "--min-margin", type=float, default=0.05,
        help="minimum top-1 minus top-2 similarity",
    )
    identify_parser.add_argument("--token", default=os.environ.get("HF_TOKEN"))
    identify_parser.set_defaults(handler=identify)

    list_parser = subparsers.add_parser("list", help="list enrolled speakers")
    list_parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    list_parser.set_defaults(handler=list_speakers)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
