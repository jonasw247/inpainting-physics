"""Render the website "Local geometry editing" figure with the *L-MAE prediction*
on the deformed geometry (state 2), not the ground-truth CFD field.

Six different Aneumo arteries, each a source -> locally-deformed-target pair from
the example_deformation triplets. For each pair we:
  1. flow-transfer the source's GT velocity onto the target geometry, mask the
     grown ("deformed") region, let L-MAE predict the masked tokens, and the AE
     decode back to a full predicted velocity field on the target  (== the
     paper's L-MAE inpainting pipeline, plot_inpaint_overlay_v2.py);
  2. render two geometry PNGs per state (plain grey + the smooth red mask overlay
     used by the paper figure), and dump subsampled glyphs:
        state 1 (original)  -> the GT source velocity field   (this *is* the
                               context the model conditions on)
        state 2 (deformed)  -> the L-MAE-predicted velocity field
  3. record the full-sample nMSE of that prediction vs the reference solver.

Outputs: website/static/images/deform_demo/<a>_<b>_{a,a_mask,b,b_mask}.png and
website/static/data/fm_deform.{json,js} (window.FM_DEFORM = {pairs:[...]}).

Needs a GPU + the L-MAE (lilac-night-22) / v3_tmp-AE checkpoints. Run on the
cluster, e.g.:
  srun --gres=gpu:1 --exclude=helios -p universe,asteroids \
       ~/.conda/envs/ffm/bin/python website/tools/render_deform_mae.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import torch

REPO = Path(__file__).resolve().parents[2]            # .../fluid-flow-matching
PKG = REPO / "fluid-flow-matching"                    # the inner package dir
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(PKG / "paper_figures/example_deformation"))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # website/tools

from src.data.dataset import FlowDataset                                    # noqa: E402
from plot_inpaint_overlay import (                                          # noqa: E402
    compute_mask_v2, _run_inpaint_v2, _wall_distance, _load_ae_mae,
)
# pure-numpy helpers shared with the GT-only exporter
from export_deform_demo import (                                            # noqa: E402
    reference_rotation, rot3, rot3_faces, load_stl, load_internal,
    render_mesh, mask_face_weights, footprint_mask, glyphs_px,
    VIEW_PAD_FRAC, N_ARROWS, ARROW_REF_LEN_PX, VREF_PCTL,
    IMG_DIR, JSON_OUT, JS_OUT,
)

ANEUMO_ROOT = "/vol/miltank/datasets/cfd-blood-flow/Aneumo/extracted_files"
ORIENT_SPEED = "0.0035"
FLOW_SPEED = "0.003"
RNG_SEED = 7

# source -> deformed-target pairs, each a *different* artery.
PAIRS = [("19", "22"), ("272", "292"), ("587", "604"), ("746", "752")]
# extra in-plane rotation of the rendered view per pair, in degrees. Positive =
# rotate the artery counter-clockwise on screen ("to the left"). 0 unless the
# default projection looks awkward.
EXTRA_ROT_DEG = {("746", "752"): 90.0}


def _ds_index(ds, sid: str) -> int:
    sids = [str(s) for s in ds.shape_ids]
    return sids.index(str(sid))


def _extra_rot(deg: float) -> np.ndarray:
    """3x3 matrix that, post-multiplied onto the projection, rotates the
    rendered XY view by `deg` CCW on screen. (The render maps world-Y downward,
    so this is a math-CW rotation in world (x, y).)"""
    c, s = np.cos(np.deg2rad(-deg)), np.sin(np.deg2rad(-deg))
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]], dtype=np.float32)


def process_pair(sid_a: str, sid_b: str, ds, ae, mae, cfg, center_at_com, device, rng) -> dict:
    print(f"\n=== pair {sid_a} -> {sid_b} ===")
    ia, ib = _ds_index(ds, sid_a), _ds_index(ds, sid_b)
    ds.fixed_subpath = f"npy/m={FLOW_SPEED}"
    d_a, d_b = ds[ia], ds[ib]
    pos_a = d_a.pos.numpy().astype(np.float32)
    pos_b = d_b.pos.numpy().astype(np.float32)
    vel_a = d_a.x[:, 1:4].numpy().astype(np.float32)        # GT source flow (the context)
    vel_b_gt = d_b.x[:, 1:4].numpy().astype(np.float32)     # GT target flow (for the nMSE only)

    # mask = the grown region on the target (NN-distance based, paper's compute_mask_v2)
    mask_b, core_b, mean_dist = compute_mask_v2(pos_a, pos_b)
    print(f"  mask_b = {mask_b.sum()}/{len(pos_b)} ({100 * mask_b.mean():.1f}%), core {core_b.sum()}, mean_dist {mean_dist:.2e}")

    # L-MAE inpainting: source flow transferred onto target, deformed region masked
    wall_thr = cfg["dataset"].get("wall_velocity_threshold", 1e-4)
    wall_dist_t = torch.from_numpy(_wall_distance(pos_b, vel_b_gt, wall_thr))
    vel_b_pred, tok_mask, _, _ = _run_inpaint_v2(
        ae, mae, cfg, d_a, d_b, mask_b,
        wall_distance_full=wall_dist_t, device=device, center_at_com=center_at_com,
    )
    vel_b_pred = vel_b_pred.astype(np.float32)
    nmse_full = float(np.sum((vel_b_pred - vel_b_gt) ** 2) / np.sum(vel_b_gt ** 2))
    m = mask_b
    nmse_mask = float(np.sum((vel_b_pred[m] - vel_b_gt[m]) ** 2) / max(np.sum(vel_b_gt[m] ** 2), 1e-12)) if m.any() else float("nan")
    print(f"  L-MAE nMSE  full={100 * nmse_full:.2f}%  masked={100 * nmse_mask:.2f}%  (tok_mask {tok_mask.sum()})")

    # one shared projection (from the source, at the orientation speed), plus an
    # optional in-plane view rotation for this pair
    pos_ao, vel_ao = load_internal(sid_a, ORIENT_SPEED)
    R = reference_rotation(pos_ao, vel_ao, sid_a, ORIENT_SPEED)
    extra_deg = EXTRA_ROT_DEG.get((sid_a, sid_b), 0.0)
    if extra_deg:
        R = (_extra_rot(extra_deg) @ R).astype(np.float32)
        print(f"  extra view rotation: {extra_deg:+.0f}deg CCW on screen")
    pos_ar = rot3(pos_a, R); vel_ar = rot3(vel_a, R)
    pos_br = rot3(pos_b, R); vel_br_pred = rot3(vel_b_pred, R)
    faces_a, norm_a = load_stl(sid_a); faces_ar = rot3_faces(faces_a, R); norm_ar = rot3(norm_a, R)
    faces_b, norm_b = load_stl(sid_b); faces_br = rot3_faces(faces_b, R); norm_br = rot3(norm_b, R)

    flat = np.concatenate([faces_ar.reshape(-1, 3), faces_br.reshape(-1, 3)], axis=0)
    x0, x1 = float(flat[:, 0].min()), float(flat[:, 0].max())
    y0, y1 = float(flat[:, 1].min()), float(flat[:, 1].max())
    pad = VIEW_PAD_FRAC * max(x1 - x0, y1 - y0)
    xlim = (x0 - pad, x1 + pad); ylim = (y0 - pad, y1 + pad)
    ext_w, ext_h = xlim[1] - xlim[0], ylim[1] - ylim[0]

    # source-side red region = the edit footprint mapped back onto the original vessel
    mean_d = float(mean_dist)
    m_a = footprint_mask(pos_a.astype(np.float64), pos_b.astype(np.float64)[core_b], mean_d)
    fw_a = mask_face_weights(faces_ar, pos_ar, m_a, mean_d)
    fw_b = mask_face_weights(faces_br, pos_br, mask_b, mean_d)

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

    vref = float(np.percentile(np.concatenate(
        [np.linalg.norm(vel_ar, axis=1), np.linalg.norm(vel_br_pred, axis=1)]), VREF_PCTL))
    g_a = glyphs_px(pos_ar[:, :2], vel_ar, vref, to_px, rng, N_ARROWS)            # GT source flow
    g_b = glyphs_px(pos_br[:, :2], vel_br_pred, vref, to_px, rng, N_ARROWS)       # L-MAE prediction

    rel = "static/images/deform_demo/"
    return {
        "shapeA": sid_a, "shapeB": sid_b,
        "w": w_px, "h": h_px,
        "L0": ARROW_REF_LEN_PX, "vref": round(vref, 6),
        "geomA": rel + png_a.name, "geomMaskA": rel + png_am.name,
        "geomB": rel + png_b.name, "geomMaskB": rel + png_bm.name,
        "maskFracB": round(float(mask_b.mean()), 4),
        "nmseFull": round(100.0 * nmse_full, 2), "nmseMask": round(100.0 * nmse_mask, 2),
        "glyphsA": g_a, "glyphsB": g_b,
    }


def main():
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"device: {device}")
    rng = np.random.default_rng(RNG_SEED)

    print("building FlowDataset …")
    ds = FlowDataset(
        root_dir=ANEUMO_ROOT, file_prefix="array_internal",
        fixed_subpath=[f"npy/m={ORIENT_SPEED}", f"npy/m={FLOW_SPEED}"],
        normalization="none", preload_data=False, enable_caching=False,
        use_fixed_dataset_statistics=True,
    )
    ds.fixed_subpath = f"npy/m={FLOW_SPEED}"

    ae, mae, cfg, center_at_com = _load_ae_mae(device)
    print(f"center_at_com={center_at_com}")

    pairs = []
    for sid_a, sid_b in PAIRS:
        try:
            pairs.append(process_pair(sid_a, sid_b, ds, ae, mae, cfg, center_at_com, device, rng))
        except Exception as e:
            import traceback
            print(f"  ERROR on pair {sid_a}->{sid_b}: {e}")
            traceback.print_exc()
    if not pairs:
        raise SystemExit("no pairs succeeded")

    payload = {"flow_speed": FLOW_SPEED, "state2": "L-MAE prediction", "pairs": pairs}
    blob = json.dumps(payload, separators=(",", ":"))
    JSON_OUT.write_text(blob)
    JS_OUT.write_text("window.FM_DEFORM=" + blob + ";\n")
    print(f"\nwrote {JSON_OUT} + {JS_OUT}  ({len(blob) / 1024:.0f} KB)  "
          f"{len(pairs)} pairs; nMSE full = {[p['nmseFull'] for p in pairs]}")


if __name__ == "__main__":
    main()
