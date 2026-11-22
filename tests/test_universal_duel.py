"""Automated test suite for Universal Duel, Deep Image Analysis (JPEG/PNG),
N-file Matrix, Cleanup Verification, Investigations Integration, and Exports.

Covers all 26 cases of the minimum test matrix plus APNG and diff-map endpoints.
"""

from __future__ import annotations

import io
import struct
import time
import zlib
from pathlib import Path

import pytest
from PIL import Image, ImageCms, PngImagePlugin
from pptx import Presentation

from orca.duel import compare_files
from orca.duel_engine import DuelBatchJob, build_duel_matrix
from orca.image_analysis import (
    IMAGE_ENGINE_VERSION,
    compare_images,
    extract_image_profile,
    generate_diff_map_png,
    inspect_jpeg_structure,
    inspect_png_structure,
)

from conftest import complete, upload


# ---------------------------------------------------------------------------
# Helpers to synthesize deterministic test fixtures
# ---------------------------------------------------------------------------

def _make_gradient_rgb(width: int = 32, height: int = 24) -> Image.Image:
    img = Image.new("RGB", (width, height))
    px = img.load()
    for y in range(height):
        for x in range(width):
            px[x, y] = ((x * 7) % 256, (y * 10) % 256, ((x + y) * 5) % 256)
    return img


def _save_jpeg_bytes(img: Image.Image, *, quality: int = 92, exif=None, icc_profile: bytes | None = None) -> bytes:
    buf = io.BytesIO()
    kwargs = {"format": "JPEG", "quality": quality}
    if exif is not None:
        kwargs["exif"] = exif
    if icc_profile is not None:
        kwargs["icc_profile"] = icc_profile
    img.save(buf, **kwargs)
    return buf.getvalue()


def _save_png_bytes(img: Image.Image, *, compress_level: int = 6, pnginfo=None, icc_profile: bytes | None = None) -> bytes:
    buf = io.BytesIO()
    kwargs = {"format": "PNG", "compress_level": compress_level}
    if pnginfo is not None:
        kwargs["pnginfo"] = pnginfo
    if icc_profile is not None:
        kwargs["icc_profile"] = icc_profile
    img.save(buf, **kwargs)
    return buf.getvalue()


def _strip_jpeg_app1_lossless(jpeg_bytes: bytes) -> bytes:
    """Remove APP1 segments from a JPEG without re-encoding entropy scan data."""
    assert jpeg_bytes[:2] == b"\xff\xd8"
    out = bytearray(b"\xff\xd8")
    pos = 2
    n = len(jpeg_bytes)
    while pos < n:
        if jpeg_bytes[pos] != 0xFF:
            out.extend(jpeg_bytes[pos:])
            break
        marker = jpeg_bytes[pos + 1]
        if marker == 0xDA:  # SOS -> copy rest verbatim
            out.extend(jpeg_bytes[pos:])
            break
        if marker in {0xD8, 0xD9}:
            out.extend(jpeg_bytes[pos:pos + 2])
            pos += 2
            continue
        seg_len = struct.unpack_from(">H", jpeg_bytes, pos + 2)[0]
        if marker != 0xE1:
            out.extend(jpeg_bytes[pos:pos + 2 + seg_len])
        pos += 2 + seg_len
    return bytes(out)


def _make_png_16bit_rgb(width: int, height: int, pixels_16: list[tuple[int, int, int]]) -> bytes:
    """Build a valid 16-bit RGB PNG (color_type=2, bit_depth=16) without 8-bit truncation."""
    def chunk(ctype: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 16, 2, 0, 0, 0)
    raw = bytearray()
    idx = 0
    for _ in range(height):
        raw.append(0)  # filter type 0 (None)
        for _ in range(width):
            r, g, b = pixels_16[idx]
            idx += 1
            raw.extend(struct.pack(">HHH", r, g, b))
    idat = zlib.compress(bytes(raw))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _make_apng_bytes() -> bytes:
    """Build a minimal structurally valid APNG containing acTL chunk."""
    base_png = _save_png_bytes(Image.new("RGBA", (8, 8), (10, 20, 30, 255)))
    # Insert acTL chunk right after IHDR (offset 8 + 25 = 33)
    actl_data = struct.pack(">II", 2, 0)  # 2 frames, infinite loop
    crc = zlib.crc32(b"acTL" + actl_data) & 0xFFFFFFFF
    actl_chunk = struct.pack(">I", 8) + b"acTL" + actl_data + struct.pack(">I", crc)
    return base_png[:33] + actl_chunk + base_png[33:]


def _make_pptx_bytes(title: str) -> bytes:
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[0])
    slide.shapes.title.text = title
    buf = io.BytesIO()
    deck.save(buf)
    return buf.getvalue()


def _duel_via_api(client, items: list[tuple[str, bytes, str]]):
    resp = upload(client, items)
    base, session = complete(client, resp)
    f0, f1 = session["files"][0]["id"], session["files"][1]["id"]
    duel = client.get(f"{base}/duel", params={"file_a": f0, "file_b": f1})
    assert duel.status_code == 200, duel.text
    return base, session, duel.json()


# ---------------------------------------------------------------------------
# Cases 01 - 10: Core Pairwise Universal Duel Scenarios
# ---------------------------------------------------------------------------

def test_case_01_two_pptx_preserves_document_duel(client):
    p1 = _make_pptx_bytes("Presentación Alfa")
    p2 = _make_pptx_bytes("Presentación Beta")
    _, _, res = _duel_via_api(client, [
        ("a.pptx", p1, "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        ("b.pptx", p2, "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ])
    assert res["capabilities"]["is_pptx_pair"] is True
    assert res["content_comparison"]["status"] == "analyzed"
    assert res["similarity"]["overall_available"] is True
    assert "pptx_content" in res["executed_comparators"]
    img_dim = next(d for d in res["dimensions"] if d["id"] == "image_pixels")
    assert img_dim["status"] == "not_applicable"


def test_case_02_two_identical_jpegs(client):
    jpg = _save_jpeg_bytes(_make_gradient_rgb())
    _, _, res = _duel_via_api(client, [("a.jpg", jpg, "image/jpeg"), ("b.jpg", jpg, "image/jpeg")])
    assert res["binary_identity"]["identical_bytes"] is True
    assert res["capabilities"]["is_image_pair"] is True
    ic = res["image_comparison"]
    assert ic["status"] == "analyzed"
    assert ic["decoded_samples"]["exact_match"] is True
    assert ic["visual_normalized"]["exact_match"] is True
    assert ic["visual_normalized"]["different_pixels"] == 0
    assert ic["structural_comparison"]["identical_structure"] is True


def test_case_03_two_identical_pngs(client):
    png = _save_png_bytes(_make_gradient_rgb())
    _, _, res = _duel_via_api(client, [("a.png", png, "image/png"), ("b.png", png, "image/png")])
    assert res["binary_identity"]["identical_bytes"] is True
    ic = res["image_comparison"]
    assert ic["decoded_samples"]["exact_match"] is True
    assert ic["visual_normalized"]["exact_match"] is True
    assert ic["structural_comparison"]["identical_structure"] is True


def test_case_04_jpeg_exif_stripped_without_reencode(client):
    exif = Image.Exif()
    exif[315] = "Autor Original"
    exif[305] = "Software Test"
    jpg_with_exif = _save_jpeg_bytes(_make_gradient_rgb(), exif=exif)
    jpg_stripped = _strip_jpeg_app1_lossless(jpg_with_exif)
    _, _, res = _duel_via_api(client, [("orig.jpg", jpg_with_exif, "image/jpeg"), ("strip.jpg", jpg_stripped, "image/jpeg")])
    assert res["binary_identity"]["identical_bytes"] is False
    ic = res["image_comparison"]
    assert ic["decoded_samples"]["exact_match"] is True
    assert ic["visual_normalized"]["exact_match"] is True
    assert ic["structural_comparison"]["metadata_segments_changed"] is True
    assert ic["structural_comparison"]["scan_data_changed"] is False


def test_case_05_png_metadata_removed(client):
    info = PngImagePlugin.PngInfo()
    info.add_text("Author", "Investigadora")
    info.add_text("Comment", "Nota de prueba")
    img = _make_gradient_rgb()
    png_meta = _save_png_bytes(img, pnginfo=info)
    png_clean = _save_png_bytes(img)
    _, _, res = _duel_via_api(client, [("meta.png", png_meta, "image/png"), ("clean.png", png_clean, "image/png")])
    assert res["binary_identity"]["identical_bytes"] is False
    ic = res["image_comparison"]
    assert ic["decoded_samples"]["exact_match"] is True
    assert ic["visual_normalized"]["exact_match"] is True
    assert ic["structural_comparison"]["metadata_chunks_changed"] is True


def test_case_06_png_recompressed_same_pixels(client):
    img = _make_gradient_rgb(48, 36)
    png_low = _save_png_bytes(img, compress_level=1)
    png_high = _save_png_bytes(img, compress_level=9)
    assert png_low != png_high
    _, _, res = _duel_via_api(client, [("low.png", png_low, "image/png"), ("high.png", png_high, "image/png")])
    assert res["binary_identity"]["identical_bytes"] is False
    ic = res["image_comparison"]
    assert ic["decoded_samples"]["exact_match"] is True
    assert ic["visual_normalized"]["exact_match"] is True
    assert ic["structural_comparison"]["idat_stream_changed"] is True


def test_case_07_jpeg_reencoded_detects_pixel_diffs_and_similarity(client):
    img = _make_gradient_rgb(64, 48)
    jpg_hi = _save_jpeg_bytes(img, quality=95)
    jpg_lo = _save_jpeg_bytes(img, quality=40)
    _, _, res = _duel_via_api(client, [("hi.jpg", jpg_hi, "image/jpeg"), ("lo.jpg", jpg_lo, "image/jpeg")])
    assert res["binary_identity"]["identical_bytes"] is False
    ic = res["image_comparison"]
    assert ic["visual_normalized"]["exact_match"] is False
    assert ic["visual_normalized"]["different_pixels"] > 0
    assert ic["perceptual"]["ssim"] is not None and ic["perceptual"]["ssim"] > 0.7
    assert ic["perceptual"]["phash_distance"] is not None
    assert ic["structural_comparison"]["coding_parameters_changed"] is True


def test_case_08_single_pixel_changed_and_diff_map(client):
    img_a = _make_gradient_rgb(32, 24)
    img_b = img_a.copy()
    r, g, b = img_b.getpixel((10, 10))
    img_b.putpixel((10, 10), ((r + 25) % 256, g, b))
    base, session, res = _duel_via_api(client, [
        ("a.png", _save_png_bytes(img_a), "image/png"),
        ("b.png", _save_png_bytes(img_b), "image/png"),
    ])
    ic = res["image_comparison"]
    assert ic["decoded_samples"]["exact_match"] is False
    assert ic["decoded_samples"]["different_pixels"] == 1
    assert ic["visual_normalized"]["different_pixels"] == 1
    assert ic["diff_map_available"] is True

    # Verify diff-map endpoint returns valid PNG & header metadata
    f0, f1 = session["files"][0]["id"], session["files"][1]["id"]
    dm = client.get(f"{base}/duel/diff-map", params={"file_a": f0, "file_b": f1, "mode": "normalized", "amplify": "true"})
    assert dm.status_code == 200
    assert dm.headers["content-type"] == "image/png"
    assert dm.headers["x-orca-diff-pixels"] == "1"
    assert dm.content.startswith(b"\x89PNG\r\n\x1a\n")

    # Verify preview endpoint works for image
    prev = client.get(f"{base}/files/{f0}/preview")
    assert prev.status_code == 200
    assert prev.headers["content-type"] == "image/png"


def test_case_09_jpeg_and_png_equivalent_pixels(client):
    # Solid color survives JPEG encoding identically so decoded RGB pixels match PNG
    img = Image.new("RGB", (24, 24), (64, 128, 192))
    jpg = _save_jpeg_bytes(img, quality=100)
    # Re-open JPEG to create a PNG with exact same decoded pixels
    with Image.open(io.BytesIO(jpg)) as decoded:
        png = _save_png_bytes(decoded)
    _, _, res = _duel_via_api(client, [("img.jpg", jpg, "image/jpeg"), ("img.png", png, "image/png")])
    assert res["binary_identity"]["identical_bytes"] is False
    ic = res["image_comparison"]
    assert ic["status"] == "analyzed"
    assert ic["visual_normalized"]["exact_match"] is True
    assert ic["structural_comparison"]["status"] == "not_applicable"
    assert ic["structural_comparison"]["individual_a"]["format"] == "JPEG"
    assert ic["structural_comparison"]["individual_b"]["format"] == "PNG"


def test_case_10_mixed_jpeg_and_pptx(client):
    jpg = _save_jpeg_bytes(_make_gradient_rgb())
    pptx = _make_pptx_bytes("Diapositiva")
    _, _, res = _duel_via_api(client, [
        ("foto.jpg", jpg, "image/jpeg"),
        ("doc.pptx", pptx, "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ])
    assert res["capabilities"]["is_mixed_formats"] is True
    assert res["similarity"]["overall_available"] is False
    assert res["image_comparison"] is None
    dims = {d["id"]: d["status"] for d in res["dimensions"]}
    assert dims["document_content"] == "not_applicable"
    assert dims["image_pixels"] == "not_applicable"
    assert dims["binary_identity"] == "different"


# ---------------------------------------------------------------------------
# Cases 11 - 13, 21, 22: N-File Matrix, Duplicates, Mixed Set, Partial & Cancel
# ---------------------------------------------------------------------------

def test_case_11_ten_images_matrix_and_reference_mode(client):
    files = []
    for i in range(10):
        img = _make_gradient_rgb(24, 24)
        img.putpixel((i, i), (255, 0, 0))
        files.append((f"img_{i:02d}.png", _save_png_bytes(img), "image/png"))
    resp = upload(client, files)
    base, session = complete(client, resp)
    assert len(session["files"]) == 10

    mx = client.get(f"{base}/duel/matrix")
    assert mx.status_code == 200
    data = mx.json()
    assert data["file_count"] == 10
    assert data["counts"]["total_pairs"] == 45  # 10 * 9 / 2
    assert len(data["inventory"]) == 10

    # Reference mode: 1 vs 9
    ref_id = session["files"][0]["id"]
    mx_ref = client.get(f"{base}/duel/matrix", params={"reference_id": ref_id})
    assert mx_ref.status_code == 200
    assert mx_ref.json()["mode"] == "reference"
    assert mx_ref.json()["counts"]["total_pairs"] == 9


def test_case_12_ten_duplicate_files_no_redundant_deep_work(client):
    jpg = _save_jpeg_bytes(_make_gradient_rgb())
    files = [(f"dup_{i:02d}.jpg", jpg, "image/jpeg") for i in range(10)]
    resp = upload(client, files)
    base, _ = complete(client, resp)
    data = client.get(f"{base}/duel/matrix").json()
    assert len(data["groups"]["binary_duplicates"]) == 1
    assert data["groups"]["binary_duplicates"][0]["count"] == 10
    assert data["counts"]["identical"] == 45
    assert data["counts"]["pending"] == 0


def test_case_13_mixed_set_automatic_capability_selection(client):
    files = [
        ("a.jpg", _save_jpeg_bytes(_make_gradient_rgb()), "image/jpeg"),
        ("b.png", _save_png_bytes(_make_gradient_rgb()), "image/png"),
        ("c.pptx", _make_pptx_bytes("Uno"), "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        ("d.pptx", _make_pptx_bytes("Dos"), "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ]
    resp = upload(client, files)
    base, _ = complete(client, resp)
    data = client.get(f"{base}/duel/matrix").json()
    assert data["counts"]["total_pairs"] == 6
    # 1 image-image, 1 pptx-pptx, 4 mixed (not_applicable)
    assert data["counts"]["not_applicable"] == 4


def test_case_21_partially_evaluated_matrix_shows_pending_states(client):
    files = [
        ("p1.pptx", _make_pptx_bytes("Alpha"), "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        ("p2.pptx", _make_pptx_bytes("Beta"), "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        ("p3.pptx", _make_pptx_bytes("Gamma"), "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ]
    resp = upload(client, files)
    base, session = complete(client, resp)
    # Evaluate only 1 of 3 pairs
    f0, f1 = session["files"][0]["id"], session["files"][1]["id"]
    assert client.get(f"{base}/duel", params={"file_a": f0, "file_b": f1}).status_code == 200

    mx = client.get(f"{base}/duel/matrix").json()
    assert mx["methodology"]["evaluated_deep_pairs"] == 1
    assert mx["methodology"]["pending_pairs"] == 2
    assert mx["counts"]["pending"] == 2


def test_case_22_cancel_batch_analysis_safe_state(client):
    files = [
        (f"p{i}.pptx", _make_pptx_bytes(f"Slide {i}"), "application/vnd.openxmlformats-officedocument.presentationml.presentation")
        for i in range(5)
    ]
    resp = upload(client, files)
    base, _ = complete(client, resp)
    started = client.post(f"{base}/duel/matrix/batch", json={"max_pairs": 10})
    assert started.status_code == 202
    cancelled = client.post(f"{base}/duel/matrix/cancel")
    assert cancelled.status_code == 200
    # Wait briefly for worker thread to settle cleanly
    for _ in range(40):
        st = client.get(f"{base}/duel/matrix/status").json()
        if st["state"] in {"cancelled", "complete"}:
            break
        time.sleep(0.03)
    assert st["state"] in {"cancelled", "complete"}
    # Matrix remains consistent after cancellation
    assert client.get(f"{base}/duel/matrix").status_code == 200


# ---------------------------------------------------------------------------
# Cases 14 - 20 + APNG: Edge Cases, Security, ICC, EXIF, Alpha, 16-bit, CRC
# ---------------------------------------------------------------------------

def test_case_14_corrupt_file_controlled_error_preserves_other_files(client):
    valid_a = _save_png_bytes(_make_gradient_rgb())
    valid_b = _save_png_bytes(Image.new("RGB", (16, 16), (1, 2, 3)))
    corrupt = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + b"broken-bytes-not-a-valid-png"
    resp = upload(client, [
        ("ok_a.png", valid_a, "image/png"),
        ("ok_b.png", valid_b, "image/png"),
        ("corrupt.png", corrupt, "image/png"),
    ])
    base, session = complete(client, resp)
    mx = client.get(f"{base}/duel/matrix").json()
    assert mx["counts"]["error"] >= 2  # pairs involving corrupt.png
    # Valid pair still works cleanly
    ok_ids = [f["id"] for f in session["files"] if f["name"].startswith("ok_")]
    res = client.get(f"{base}/duel", params={"file_a": ok_ids[0], "file_b": ok_ids[1]}).json()
    assert res["image_comparison"]["status"] == "analyzed"


def test_case_15_excessive_image_dimensions_safety_limit(tmp_path):
    # Craft a PNG header declaring 25000 x 25000 (625 MP > 40 MP limit)
    ihdr = struct.pack(">IIBBBBB", 25000, 25000, 8, 2, 0, 0, 0)
    crc = zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF
    iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
    huge_png = (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13) + b"IHDR" + ihdr + struct.pack(">I", crc)
        + struct.pack(">I", 0) + b"IEND" + struct.pack(">I", iend_crc)
    )
    p = tmp_path / "huge.png"
    p.write_bytes(huge_png)
    prof = extract_image_profile(p, expected_format="PNG")
    assert prof["status"] == "error"
    assert "límite" in prof["error"].lower() or "dimensiones" in prof["error"].lower()


def test_case_16_png_invalid_crc_detected(tmp_path):
    png = bytearray(_save_png_bytes(_make_gradient_rgb()))
    # Corrupt last byte of IHDR chunk CRC (offset 8 + 4 + 4 + 13 + 3 = 32)
    png[32] ^= 0xFF
    p = tmp_path / "bad_crc.png"
    p.write_bytes(bytes(png))
    st = inspect_png_structure(p)
    assert st["valid"] is False
    assert len(st["crc_errors"]) >= 1
    assert st["crc_errors"][0]["type"] == "IHDR"


def test_case_17_different_icc_profiles_distinguish_raw_vs_normalized(tmp_path):
    img = Image.new("RGB", (24, 24), (200, 80, 40))
    srgb_bytes = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    # Create a distinct ICC profile (Lab/XYZ or altered sRGB intent/profile)
    # Build a valid RGB ICC profile with different gamma/primaries by using built-in sRGB vs none,
    # and verify ICC difference detection & both levels explicitly.
    p_a = tmp_path / "srgb.png"
    p_b = tmp_path / "noicc.png"
    p_a.write_bytes(_save_png_bytes(img, icc_profile=srgb_bytes))
    p_b.write_bytes(_save_png_bytes(img))
    res = compare_images(p_a, p_b)
    assert res["status"] == "analyzed"
    assert res["decoded_samples"]["exact_match"] is True
    assert res["profile_a"]["icc"]["present"] is True
    assert res["profile_b"]["icc"]["present"] is False
    assert any("ICC" in n for n in res["notes"])
    assert res["structural_comparison"]["color_chunks_changed"] is True


def test_case_18_different_exif_orientation_coherent_both_policies(tmp_path):
    # Asymmetric 20x10 image; rotate 90° clockwise for B and tag with EXIF orientation 8 (270° CCW)
    # so raw samples differ (20x10 vs 10x20), while EXIF-normalized visual output matches!
    img_a = _make_gradient_rgb(20, 10)
    img_b_stored = img_a.rotate(-90, expand=True)  # 10x20 stored
    exif_b = Image.Exif()
    exif_b[274] = 8  # Rotate 270 CW (90 CCW) -> restores original 20x10 orientation
    p_a = tmp_path / "a.png"
    p_b = tmp_path / "b.png"
    p_a.write_bytes(_save_png_bytes(img_a))
    # Save B as lossless PNG with EXIF orientation
    buf = io.BytesIO()
    img_b_stored.save(buf, format="PNG", exif=exif_b)
    p_b.write_bytes(buf.getvalue())

    res = compare_images(p_a, p_b)
    assert res["status"] == "analyzed"
    # Decoded samples differ in dimensions (20x10 vs 10x20)
    assert res["decoded_samples"]["exact_match"] is False
    # Visual normalized applies EXIF orientation -> exact match!
    assert res["visual_normalized"]["exact_match"] is True
    assert any("orientación EXIF" in n for n in res["notes"])


def test_case_19_transparency_modified_detected(tmp_path):
    img_a = Image.new("RGBA", (20, 20), (50, 100, 150, 255))
    img_b = Image.new("RGBA", (20, 20), (50, 100, 150, 128))
    p_a = tmp_path / "opaque.png"
    p_b = tmp_path / "semi.png"
    p_a.write_bytes(_save_png_bytes(img_a))
    p_b.write_bytes(_save_png_bytes(img_b))

    res = compare_images(p_a, p_b)
    assert res["visual_normalized"]["exact_match"] is False
    assert res["visual_normalized"]["alpha_only_diff"] is True
    assert res["visual_normalized"]["max_channel_diff"]["A"] == 127
    assert res["visual_normalized"]["max_channel_diff"]["R"] == 0


def test_case_20_png_16bit_exact_comparison(tmp_path):
    # Two 16-bit PNGs that differ ONLY in the least-significant 8 bits (1000 vs 1001)!
    # An 8-bit truncating comparator would falsely call them equal; our 16-bit decoder catches it!
    px_a = [(1000, 2000, 3000)] * 16
    px_b = list(px_a)
    px_b[5] = (1001, 2000, 3000)
    p_a = tmp_path / "a16.png"
    p_b = tmp_path / "b16.png"
    p_a.write_bytes(_make_png_16bit_rgb(4, 4, px_a))
    p_b.write_bytes(_make_png_16bit_rgb(4, 4, px_b))

    res = compare_images(p_a, p_b)
    assert res["profile_a"]["bit_depth"] == 16
    assert res["decoded_samples"]["bit_depth"] == 16
    assert res["decoded_samples"]["exact_match"] is False
    assert res["decoded_samples"]["different_pixels"] == 1
    assert res["decoded_samples"]["max_channel_diff"]["R"] == 1
    assert res["visual_normalized"]["policy"]["lossy_conversion"] is True


def test_apng_explicitly_marked_unsupported(tmp_path):
    p_a = tmp_path / "anim.png"
    p_b = tmp_path / "static.png"
    p_a.write_bytes(_make_apng_bytes())
    p_b.write_bytes(_save_png_bytes(Image.new("RGBA", (8, 8), (10, 20, 30, 255))))
    res = compare_images(p_a, p_b)
    assert res["status"] == "unsupported"
    assert res["decoded_samples"]["status"] == "unsupported"
    assert res["visual_normalized"]["status"] == "unsupported"


# ---------------------------------------------------------------------------
# Cases 23 - 26: Cleanup Verification, Investigations, Exports, Regression
# ---------------------------------------------------------------------------

def test_case_23_cleanup_jpeg_and_png_runs_automatic_verification(client, samples):
    for kind in ("jpg", "png"):
        path = samples[kind]
        mime = "image/jpeg" if kind == "jpg" else "image/png"
        resp = upload(client, [(path.name, path.read_bytes(), mime)])
        base, session = complete(client, resp)
        fid = session["files"][0]["id"]
        clean_res = client.post(f"{base}/files/{fid}/clean")
        assert clean_res.status_code == 200, clean_res.text
        body = clean_res.json()
        ver = body["image_verification"]
        assert ver is not None
        assert ver["pixels_preserved"] is True
        assert ver["verdict_code"] in {"metadata_and_structure_only", "binary_changed_pixels_preserved"}


def test_case_24_investigation_persists_image_comparison(client):
    created = client.post("/api/investigations", json={
        "name": "Caso de imágenes", "responsible": "Dra. Ada Lovelace",
    })
    assert created.status_code == 201
    inv_uuid = created.json()["uuid"]

    img_a = _save_png_bytes(_make_gradient_rgb())
    img_b = _save_png_bytes(_make_gradient_rgb(), compress_level=1)
    added = client.post(
        f"/api/investigations/{inv_uuid}/files",
        files=[("files", ("a.png", img_a, "image/png")), ("files", ("b.png", img_b, "image/png"))],
    )
    assert added.status_code == 201
    fu0 = added.json()["files"][0]["uuid"]
    fu1 = added.json()["files"][1]["uuid"]

    comp = client.post(f"/api/investigations/{inv_uuid}/comparisons", json={
        "file_a_uuid": fu0, "file_b_uuid": fu1,
    })
    assert comp.status_code == 200
    payload = comp.json()
    assert payload["result"]["image_comparison"]["decoded_samples"]["exact_match"] is True
    assert any("image-profile" in alg for alg in payload["algorithms"])


def test_case_25_exports_json_and_html_reproducible_and_xss_safe(client):
    evil_name = '<script>alert("xss")</script>.png'
    png_a = _save_png_bytes(_make_gradient_rgb())
    png_b = _save_png_bytes(Image.new("RGB", (32, 24), (0, 0, 0)))
    png_c = _save_png_bytes(Image.new("RGB", (32, 24), (255, 255, 255)))
    resp = upload(client, [
        (evil_name, png_a, "image/png"),
        ("b.png", png_b, "image/png"),
        ("c.png", png_c, "image/png"),
    ])
    base, session = complete(client, resp)
    f0, f1 = session["files"][0]["id"], session["files"][1]["id"]

    # 2-file JSON & HTML export
    j2 = client.get(f"{base}/duel", params={"file_a": f0, "file_b": f1, "download": "true", "format": "json"})
    h2 = client.get(f"{base}/duel", params={"file_a": f0, "file_b": f1, "download": "true", "format": "html"})
    assert j2.status_code == 200 and h2.status_code == 200
    assert "<script>alert" not in h2.text
    assert "Análisis profundo de imágenes" in h2.text

    # N-file Matrix JSON & HTML export
    jm = client.get(f"{base}/duel/matrix", params={"download": "true", "format": "json"})
    hm = client.get(f"{base}/duel/matrix", params={"download": "true", "format": "html"})
    assert jm.status_code == 200 and hm.status_code == 200
    assert "<script>alert" not in hm.text
    assert "Informe de Duelo universal" in hm.text


def test_case_26_legacy_duel_contract_backwards_compatible(client, samples):
    pdf = samples["pdf"]
    docx = samples["docx"]
    _, _, res = _duel_via_api(client, [
        ("a.pdf", pdf.read_bytes(), "application/pdf"),
        ("b.docx", docx.read_bytes(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ])
    for required in ("schema_version", "session_id", "a", "b", "summary", "notes", "rows",
                     "binary_identity", "content_comparison", "metadata_comparison", "similarity"):
        assert required in res
