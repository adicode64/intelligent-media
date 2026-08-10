#!/usr/bin/env python3
"""
Seed / demo script.

Generates a small set of synthetic vehicle images that deliberately trip each
detector, uploads them to a running API, then polls until every job settles and
prints a compact verdict table.

Usage:
    python scripts/seed.py                     # against http://localhost:8000
    API_BASE=http://host:8000 python scripts/seed.py
"""

from __future__ import annotations

import io
import os
import sys
import time

import httpx
from PIL import Image, ImageDraw, ImageFilter

API_BASE = os.environ.get("API_BASE", "http://localhost:8000").rstrip("/")
POLL_TIMEOUT_SECONDS = 120


def _plate_image(text: str, size: tuple[int, int] = (900, 600), bg: int = 210) -> Image.Image:
    """A crude 'vehicle photo': flat body colour with a legible plate panel."""
    img = Image.new("RGB", size, (bg, bg, bg))
    draw = ImageDraw.Draw(img)
    # Body shape, so the frame is not a uniform field.
    draw.rectangle([60, 120, size[0] - 60, size[1] - 120], fill=(140, 150, 165))
    # Plate panel.
    px0, py0 = size[0] // 2 - 220, size[1] - 250
    draw.rectangle([px0, py0, px0 + 440, py0 + 120], fill=(255, 255, 255), outline=(0, 0, 0), width=5)
    draw.text((px0 + 30, py0 + 45), text, fill=(0, 0, 0))
    return img


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def build_samples() -> list[tuple[str, bytes]]:
    """Each sample targets a specific check so the results are self-explanatory."""
    clean = _plate_image("MH12AB1234")
    samples: list[tuple[str, bytes]] = [
        ("01_clean_valid_plate.png", _png(clean)),
        # Duplicate: byte-identical content, different filename -> pHash distance 0.
        ("02_duplicate_of_01.png", _png(clean)),
        # Blur: heavy Gaussian collapses Laplacian variance.
        ("03_blurry.png", _png(clean.filter(ImageFilter.GaussianBlur(radius=9)))),
        # Low light: dark body and dark background pull the mean intensity down.
        ("04_low_light.png", _png(_plate_image("MH12AB1234", bg=18))),
        # Invalid plate format: right shape, wrong grammar for an Indian plate.
        ("05_invalid_plate_format.png", _png(_plate_image("XX-999-ZZZ"))),
        # Dimension check: below the configured minimum.
        ("06_too_small.png", _png(_plate_image("MH12AB1234", size=(120, 90)))),
    ]
    return samples


def upload(client: httpx.Client, name: str, data: bytes) -> str | None:
    resp = client.post(
        "/images/upload", files={"file": (name, data, "image/png")}, timeout=60.0
    )
    if resp.status_code != 202:
        print(f"  ! {name}: upload failed ({resp.status_code}) {resp.text[:200]}")
        return None
    image_id = resp.json()["id"]
    print(f"  + {name} -> {image_id}")
    return image_id


def wait_for(client: httpx.Client, ids: list[str]) -> dict[str, dict]:
    """Poll status until each job reaches a terminal state or we hit the timeout."""
    pending = set(ids)
    settled: dict[str, dict] = {}
    deadline = time.time() + POLL_TIMEOUT_SECONDS

    while pending and time.time() < deadline:
        for image_id in list(pending):
            status = client.get(f"/images/{image_id}/status", timeout=30.0).json()
            if status["status"] in {"completed", "failed"}:
                settled[image_id] = client.get(
                    f"/images/{image_id}/results", timeout=30.0
                ).json()
                pending.discard(image_id)
        if pending:
            time.sleep(1.5)

    for image_id in pending:
        settled[image_id] = {"status": "timeout", "summary": None}
    return settled


def print_table(names: dict[str, str], results: dict[str, dict]) -> None:
    print(f"\n{'file':<32} {'status':<10} {'verdict':<12} {'conf':<6} issues")
    print("-" * 96)
    for image_id, result in results.items():
        summary = result.get("summary") or {}
        issues = ", ".join(
            c["name"] for c in (result.get("checks") or []) if c.get("status") != "pass"
        )
        print(
            f"{names.get(image_id, image_id)[:32]:<32} "
            f"{result.get('status', '?'):<10} "
            f"{str(summary.get('overall_status', '-')):<12} "
            f"{str(summary.get('confidence', '-')):<6} "
            f"{issues or '-'}"
        )


def main() -> int:
    client = httpx.Client(base_url=API_BASE)
    try:
        health = client.get("/health", timeout=10.0)
        if health.status_code != 200:
            print(f"API at {API_BASE} is unhealthy: {health.text[:200]}")
            return 1
    except httpx.HTTPError as exc:
        print(f"Cannot reach the API at {API_BASE}: {exc}")
        print("Start the stack first:  docker compose up --build")
        return 1

    print(f"Seeding {API_BASE} with synthetic samples:")
    names: dict[str, str] = {}
    for name, data in build_samples():
        image_id = upload(client, name, data)
        if image_id:
            names[image_id] = name

    if not names:
        print("Nothing was uploaded successfully.")
        return 1

    print("\nWaiting for the worker to finish...")
    results = wait_for(client, list(names))
    print_table(names, results)
    print("\nDone. Inspect any single job with:")
    print(f"  curl {API_BASE}/images/<id>/results")
    return 0


if __name__ == "__main__":
    sys.exit(main())
