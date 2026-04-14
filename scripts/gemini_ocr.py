#!/usr/bin/env python3
"""Gemini OCR — extract text from images using Google's Gemini API.

Usage:
    python3 scripts/gemini_ocr.py <image_path> [--prompt "custom prompt"]
    python3 scripts/gemini_ocr.py photo.jpg
    python3 scripts/gemini_ocr.py scan.pdf --prompt "Extract all handwritten notes"

Requires:
    pip install google-genai

Environment:
    GEMINI_API_KEY — your Gemini API key (from https://aistudio.google.com/apikey)
    Also checks ~/.config/deus/.env if the env var is not set.
"""

import argparse
import os
import sys
from pathlib import Path

from google import genai
from google.genai import types

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MODELS = ["gemini-3-flash-preview", "gemini-2.5-flash"]

DEFAULT_PROMPT = """\
You are an OCR engine. Extract ALL text from this image exactly as written.
For mathematical formulas, use LaTeX notation (inline $...$ and display $$...$$).
Preserve the original layout, language, and line breaks.
Output ONLY the extracted content, no commentary or explanation."""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_api_key():
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key
    env_path = Path.home() / ".config" / "deus" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    print(
        "Error: GEMINI_API_KEY not set. "
        "Export it or add to ~/.config/deus/.env",
        file=sys.stderr,
    )
    sys.exit(1)


def _read_image_bytes(path: str) -> tuple[bytes, str]:
    p = Path(path)
    if not p.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        sys.exit(1)
    suffix = p.suffix.lower()
    mime_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".heic": "image/heic",
        ".gif": "image/gif",
        ".pdf": "application/pdf",
    }
    mime = mime_map.get(suffix, "image/png")
    return p.read_bytes(), mime


def _ocr(client, model: str, img_bytes: bytes, mime: str, prompt: str) -> str:
    response = client.models.generate_content(
        model=model,
        contents=[
            types.Part.from_bytes(data=img_bytes, mime_type=mime),
            prompt,
        ],
        config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            max_output_tokens=65536,
        ),
    )
    return response.text.strip()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Extract text from images using Gemini OCR"
    )
    parser.add_argument("image", help="Path to image or PDF file")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="Custom OCR prompt")
    args = parser.parse_args()

    api_key = _load_api_key()
    client = genai.Client(api_key=api_key)
    img_bytes, mime = _read_image_bytes(args.image)

    for i, model in enumerate(MODELS):
        try:
            result = _ocr(client, model, img_bytes, mime, args.prompt)
            print(result)
            return
        except Exception as e:
            if "429" in str(e) and i < len(MODELS) - 1:
                print(f"Rate limited on {model}, trying {MODELS[i+1]}...", file=sys.stderr)
                continue
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
