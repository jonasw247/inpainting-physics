"""Export real CFD data for the website "geometry deformation" interactive figure.

Four *different* Aneumo arteries, each shown in two states: a source geometry
(state 1) and a locally deformed geometry (state 2, an aneurysm grown on the
same vessel). For each artery and each state we render two flat-shaded geometry
PNGs — one plain grey, one with the edited ("masked") region in a smoothly-faded
red overlay the way the paper figure marks it (plot_inpaint_overlay_v2.py) — and
dump subsampled GT-velocity glyphs. The website draws, per artery, two panels:

    · geometry only        — shaded mesh, edited region in a soft red gradient
    · geometry + quiver     — plain grey mesh + GT velocity glyphs (viridis by speed)

and a single global control cross-fades all four arteries between state 1 and
state 2 (a blend of the two geometry PNGs and the two glyph sets).

All panels of one artery share the *same* projection: the reference rotation is
computed once from the source shape, applied to source + target, and the view
bbox is the union of both meshes (+ a small pad), so the source and the deformed
geometry overlay pixel-for-pixel and the red region lines up across both states.

Outputs (per pair, in website/static/images/deform_demo/):
    <sid_a>_<sid_b>_a.png        -- source mesh, plain grey
    <sid_a>_<sid_b>_a_mask.png   -- source mesh, edit footprint in red
    <sid_a>_<sid_b>_b.png        -- deformed mesh, plain grey
    <sid_a>_<sid_b>_b_mask.png   -- deformed mesh, grown region in red
    website/static/data/fm_deform.json   -- {pairs:[{w,h,L0,vref, geomA,geomMaskA,
                                                     geomB,geomMaskB, shapeA,shapeB,
                                                     maskFracA,maskFracB,
                                                     glyphsA:[[x,y,vx,vy]..],
                                                     glyphsB:[..]}, ..]}
    website/static/data/fm_deform.js     -- window.FM_DEFORM = {...}

(x, y) are pixel coords in that pair's PNGs; (vx, vy) are GT velocity scaled to
pixels so |v| == L0 px at the colour-scale reference speed `vref` (a percentile
of the pair's speed distribution), y pointing downward — matching
export_inpaint_demo.py so the website's quiver renderer is reused as-is.

No GPU / no model needed -- pure numpy + matplotlib + scipy. Run with `ffm`:
    /u/home/wejo/.conda/envs/ffm/bin/python website/tools/export_deform_demo.py
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import PolyCollection
from PIL import Image
from scipy.spatial import cKDTree

# reuse the projection / mesh helpers from the inpainting export
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from export_inpaint_demo import (                     # noqa: E402
    MESH_GRAY, MESH_AMBIENT, MESH_DIFFUSE, ARROW_REF_LEN_PX, VREF_PCTL,
    SPEED_CAP_RATIO, DPI, FIG_H_IN, RNG_SEED,
    load_internal, load_stl, reference_rotation, rot3, rot3_faces,
)

# the paper figure's "deformed region" overlay: a brighter coral, blended onto
# the grey mesh with a smooth distance fade (see plot_inpaint_overlay_v2.py).
MASK_RED_BRIGHT = np.array([0.97, 0.45, 0.45], dtype=np.float32)

# ------------------------------------------------------------------ config
# 4 source -> deformed-target pairs, each a *different* artery (from the
# example_deformation triplets; chosen for diverse vessels + clear edits).
PAIRS = [("19", "22"), ("272", "292"), ("363", "369"), ("746", "752")]
FLOW_SPEED = "0.003"
ORIENT_SPEED = "0.0035"

N_ARROWS = 3000                # subsampled glyphs per state
VIEW_PAD_FRAC = 0.06           # extra pad around the union bbox of both meshes
MASK_DIST_MULT = 10.0          # core mask: NN dist to the other shape > mult * mean spacing
MASK_DILATE_MULT = 40.0        # dilate the core by this many mean-spacings
MASK_FADE_MULT = 30.0          # red overlay fades to grey over this many mean-spacings

WEB_ROOT = Path(__file__).resolve().parents[1]
IMG_DIR = WEB_ROOT / "static" / "images" / "deform_demo"
JSON_OUT = WEB_ROOT / "static" / "data" / "fm_deform.json"
JS_OUT = WEB_ROOT / "static" / "data" / "fm_deform.js"


def render_mesh(faces_r, normals_r, xlim, ylim, out_path, face_w=None):
    """Flat-shaded mesh PNG (opaque white bg). If `face_w` (per-face weight in
    [0,1]) is given, faces are blended grey->MASK_RED_BRIGHT by their weight —
    the paper figure's smoothly-faded "masked region" overlay."""
    aspect = (xlim[1] - xlim[0]) / (ylim[1] - ylim[0])
    fig, ax = plt.subplots(figsize=(FIG_H_IN * aspect, FIG_H_IN))
    ax.set_position([0, 0, 1, 1])
    ax.set_xlim(*xlim); ax.set_ylim(*ylim); ax.axis("off")
    depth = faces_r[:, :, 2].mean(1)
    shade = MESH_AMBIENT + MESH_DIFFUSE * np.abs(normals_r[:, 2])
    if face_w is None:
        base = np.broadcast_to(MESH_GRAY, (faces_r.shape[0], 3))
    else:
        w = np.asarray(face_w, dtype=np.float32).clip(0.0, 1.0)[:, None]
        base = MESH_GRAY[None, :] * (1.0 - w) + MASK_RED_BRIGHT[None, :] * w
    rgb = (base * shade[:, None]).clip(0, 1)
    order = np.argsort(depth)
    pc = PolyCollection(faces_r[order, :, :2], facecolors=rgb[order],
                        edgecolors=rgb[order], antialiased=True, linewidths=0.5)
    ax.add_collection(pc)
    fig.savefig(out_path, dpi=DPI, facecolor="white")
    plt.close(fig)
    im = Image.open(out_path).convert("RGB").quantize(colors=192, dither=Image.NONE)
    im.save(out_path, optimize=True)
    return im.size


def mask_face_weights(faces_3d, pos_cloud, mask_cloud, mean_d):
    """Per-face red weight: 1 inside the mask, fading to 0 over MASK_FADE_MULT
    mean-spacings away from the nearest masked point (matches the paper figure)."""
    centroids = faces_3d.mean(axis=1)
    masked_pts = pos_cloud[mask_cloud]
    if len(masked_pts) == 0:
        return np.zeros(len(faces_3d), dtype=np.float32)
    d_to_mask, _ = cKDTree(masked_pts).query(centroids, k=1, workers=-1)
    w = np.clip(1.0 - d_to_mask / (MASK_FADE_MULT * mean_d), 0.0, 1.0).astype(np.float32)
    # faces whose own centroid lands on a masked point get the full red
    _, idx = cKDTree(pos_cloud).query(centroids, k=1, workers=-1)
    return np.maximum(w, mask_cloud[idx].astype(np.float32))


def mean_spacing(pos: np.ndarray) -> float:
    """Average nearest-neighbour distance among `pos` (k=2 to skip self)."""
    d, _ = cKDTree(pos).query(pos, k=2)
    return float(d[:, 1].mean())


def deform_mask(pos_self: np.ndarray, pos_other: np.ndarray, mean_d: float):
    """Points of `pos_self` that the *other* geometry doesn't cover, dilated.

    core   = pts whose NN distance to `pos_other` > MASK_DIST_MULT * mean_d
    dilate = pts within MASK_DILATE_MULT * mean_d of any core pt
    Returns ``(full_mask, core_mask)``.
    """
    nn, _ = cKDTree(pos_other).query(pos_self, k=1, workers=-1)
    core = nn > MASK_DIST_MULT * mean_d
    if not core.any():
        return core, core.copy()
    nn_core, _ = cKDTree(pos_self[core]).query(pos_self, k=1, workers=-1)
    return core | (nn_core < MASK_DILATE_MULT * mean_d), core


def footprint_mask(pos_self: np.ndarray, mask_core_other: np.ndarray, mean_d: float) -> np.ndarray:
    """The edit's footprint on `pos_self`: pts within MASK_DILATE_MULT * mean_d
    of the *core* of the other shape's edited region (used for the source side
    of an additive edit, where the geometric difference is empty)."""
    if len(mask_core_other) == 0:
        return np.zeros(len(pos_self), dtype=bool)
    nn, _ = cKDTree(mask_core_other).query(pos_self, k=1, workers=-1)
    return nn < MASK_DILATE_MULT * mean_d


def glyphs_px(pos_r2, vel_r, vref, to_px, rng, n):
    """Subsample `n` glyphs; pack [x, y, vx, vy] in PNG-pixel space (y down,
    |arrow| == L0 px at speed == vref, sqrt-compressed dynamic range)."""
    speed = np.linalg.norm(vel_r, axis=1)
    ratio = np.clip(speed / max(vref, 1e-12), 0.0, SPEED_CAP_RATIO)
    mag = ARROW_REF_LEN_PX * np.sqrt(ratio)
    dir2 = vel_r[:, :2] / np.maximum(speed[:, None], 1e-12)
    vpx = dir2 * mag[:, None]
    vpx[:, 1] *= -1.0
    ppx = to_px(pos_r2)
    sel = rng.choice(len(pos_r2), min(n, len(pos_r2)), replace=False)
    return [[round(float(ppx[i, 0]), 1), round(float(ppx[i, 1]), 1),
             round(float(vpx[i, 0]), 2), round(float(vpx[i, 1]), 2)] for i in sel]


def process_pair(sid_a: str, sid_b: str, rng) -> dict:
    print(f"pair {sid_a} -> {sid_b}  (flow m={FLOW_SPEED}, orient m={ORIENT_SPEED})")
    pos_a, vel_a = load_internal(sid_a, FLOW_SPEED)
    pos_b, vel_b = load_internal(sid_b, FLOW_SPEED)
    pos_ao, vel_ao = load_internal(sid_a, ORIENT_SPEED)
    R = reference_rotation(pos_ao, vel_ao, sid_a, ORIENT_SPEED)   # one projection for both states

    pos_ar = rot3(pos_a, R); vel_ar = rot3(vel_a, R)
    pos_br = rot3(pos_b, R); vel_br = rot3(vel_b, R)
    faces_a, norm_a = load_stl(sid_a); faces_ar = rot3_faces(faces_a, R); norm_ar = rot3(norm_a, R)
    faces_b, norm_b = load_stl(sid_b); faces_br = rot3_faces(faces_b, R); norm_br = rot3(norm_b, R)

    # shared view bbox = union of both meshes (so the two states overlay)
    flat = np.concatenate([faces_ar.reshape(-1, 3), faces_br.reshape(-1, 3)], axis=0)
    x0, x1 = float(flat[:, 0].min()), float(flat[:, 0].max())
    y0, y1 = float(flat[:, 1].min()), float(flat[:, 1].max())
    pad = VIEW_PAD_FRAC * max(x1 - x0, y1 - y0)
    xlim = (x0 - pad, x1 + pad); ylim = (y0 - pad, y1 + pad)
    ext_w, ext_h = xlim[1] - xlim[0], ylim[1] - ylim[0]

    # deformation mask on each shape. The edit is (near-)additive: the target
    # carries a bulge the source lacks (= m_b), while the source has no region
    # the target is missing -> the source-side red region is that edit's
    # footprint mapped back onto the original vessel.
    pa64, pb64 = pos_a.astype(np.float64), pos_b.astype(np.float64)
    mean_d = mean_spacing(pa64)                          # source spacing as the reference scale
    m_b, core_b = deform_mask(pb64, pa64, mean_d)
    m_a, _ = deform_mask(pa64, pb64, mean_d)
    if not m_a.any():
        m_a = footprint_mask(pa64, pb64[core_b], mean_d)
    fw_a = mask_face_weights(faces_ar, pos_ar, m_a, mean_d)
    fw_b = mask_face_weights(faces_br, pos_br, m_b, mean_d)
    print(f"  mask A {m_a.sum()}/{len(pos_a)} ({100 * m_a.mean():.1f}%, w>0.5: {(fw_a > 0.5).sum()} faces), "
          f"mask B {m_b.sum()}/{len(pos_b)} ({100 * m_b.mean():.1f}%, w>0.5: {(fw_b > 0.5).sum()} faces)")

    IMG_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{sid_a}_{sid_b}"
    png_a = IMG_DIR / f"{stem}_a.png"; png_am = IMG_DIR / f"{stem}_a_mask.png"
    png_b = IMG_DIR / f"{stem}_b.png"; png_bm = IMG_DIR / f"{stem}_b_mask.png"
    w_px, h_px = render_mesh(faces_ar, norm_ar, xlim, ylim, png_a)
    wam, ham = render_mesh(faces_ar, norm_ar, xlim, ylim, png_am, face_w=fw_a)
    wb, hb = render_mesh(faces_br, norm_br, xlim, ylim, png_b)
    wbm, hbm = render_mesh(faces_br, norm_br, xlim, ylim, png_bm, face_w=fw_b)
    assert (w_px, h_px) == (wam, ham) == (wb, hb) == (wbm, hbm)
    print(f"  meshes -> {stem}_{{a,a_mask,b,b_mask}}.png  ({w_px} x {h_px} px)")

    def to_px(p2):
        p2 = np.atleast_2d(np.asarray(p2, dtype=np.float64))
        return np.stack([(p2[:, 0] - xlim[0]) / ext_w * w_px,
                         (1.0 - (p2[:, 1] - ylim[0]) / ext_h) * h_px], axis=1)

    # shared colour/length scale for the pair = 90th-pctl speed over both states
    vref = float(np.percentile(np.concatenate(
        [np.linalg.norm(vel_ar, axis=1), np.linalg.norm(vel_br, axis=1)]), VREF_PCTL))
    g_a = glyphs_px(pos_ar[:, :2], vel_ar, vref, to_px, rng, N_ARROWS)
    g_b = glyphs_px(pos_br[:, :2], vel_br, vref, to_px, rng, N_ARROWS)

    rel = "static/images/deform_demo/"
    return {
        "shapeA": sid_a, "shapeB": sid_b,
        "w": w_px, "h": h_px,
        "L0": ARROW_REF_LEN_PX, "vref": round(vref, 6),
        "geomA": rel + png_a.name, "geomMaskA": rel + png_am.name,
        "geomB": rel + png_b.name, "geomMaskB": rel + png_bm.name,
        "maskFracA": round(float(m_a.mean()), 4), "maskFracB": round(float(m_b.mean()), 4),
        "glyphsA": g_a, "glyphsB": g_b,
    }


def main():
    global FLOW_SPEED, ORIENT_SPEED
    ap = argparse.ArgumentParser()
    ap.add_argument("--flow_speed", default=FLOW_SPEED)
    ap.add_argument("--orient_speed", default=ORIENT_SPEED)
    args = ap.parse_args()
    FLOW_SPEED, ORIENT_SPEED = args.flow_speed, args.orient_speed

    rng = np.random.default_rng(RNG_SEED)
    pairs = [process_pair(a, b, rng) for a, b in PAIRS]
    payload = {"flow_speed": FLOW_SPEED, "pairs": pairs}
    blob = json.dumps(payload, separators=(",", ":"))
    JSON_OUT.write_text(blob)
    JS_OUT.write_text("window.FM_DEFORM=" + blob + ";\n")
    print(f"wrote {JSON_OUT} + {JS_OUT}  ({len(blob) / 1024:.0f} KB)  "
          f"{len(pairs)} pairs, glyphs {[len(p['glyphsA']) for p in pairs]}")


if __name__ == "__main__":
    main()
