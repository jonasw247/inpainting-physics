"""Export the REAL-prediction velocity fields (from run_inpaint_predictions.py)
for the website "Inpainting methods (real predictions)" figure.

Unlike export_inpaint_demo.py (which animates the *ground truth* — FM stylised
noise->GT, MAE GT revealed outside-in), this bakes the **actual per-step model
outputs**: L-FM Euler snapshots and iterative-L-MAE reveal snapshots, decoded to
physical velocity by the trained AE. The website plays the true frames.

Reuses the geometry / orientation / mesh-render / quiver helpers from
export_inpaint_demo.py so the look matches the GT figure exactly. The mask here
is the model's inlet/outlet mask (a point is "inpainted" if mask_points[i]).

Reads   website/static/data/predictions/{fm_steps,mae_steps,gt,pos,mask_points}.npy + meta.json
Writes  website/static/images/fm_inpaint_real_geom.png
        website/static/data/fm_inpaint_real.js   (window.FM_INPAINT_REAL = {...})
        website/static/data/fm_inpaint_real.json

Payload (positions/vectors in PNG pixels, y down; |v| = L0 px at the colour-ref
speed vref; velocity per glyph is a flat [vx0,vy0, vx1,vy1, ...] over frames):
  {w,h, geom, L0, vref, fmFrames, maeFrames, maskLabel, fixedBC,
   context: [[x,y,vx,vy],...],          # fixed GT glyphs outside the mask
   masked:  [[x,y],...],                # masked glyph positions
   fm:  [[vx0,vy0,...],...],            # FM predicted vel per glyph per frame
   mae: [[vx0,vy0,...],...],            # MAE predicted vel per glyph per frame
   shells: [poly0]}                     # the mask outline (single boundary)

Run on CPU in the `ffm` env (no GPU / no model — pure numpy + matplotlib):
    /u/home/wejo/.conda/envs/ffm/bin/python website/tools/export_inpaint_predictions.py
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parent))   # so `export_inpaint_demo` imports from anywhere

import numpy as np

# reuse the GT exporter's helpers + styling constants
from export_inpaint_demo import (
    ANEUMO_ROOT, ARROW_REF_LEN_PX, VREF_PCTL, SPEED_CAP_RATIO,
    OUTLINE_MAX_PTS, WEB_ROOT,
    load_internal, load_stl, reference_rotation, rot3, rot3_faces,
    render_mesh, outline_polygon, opening_centroid,
)

PRED_DIR = WEB_ROOT / "static" / "data" / "predictions"
PNG_OUT = WEB_ROOT / "static" / "images" / "fm_inpaint_real_geom.png"
JSON_OUT = WEB_ROOT / "static" / "data" / "fm_inpaint_real.json"
JS_OUT = WEB_ROOT / "static" / "data" / "fm_inpaint_real.js"

N_ARROWS = 2600                  # total glyphs (kept modest: each masked glyph stores all frames)
MASK_UP_PAD = 0.06               # view-bbox pad (fraction of the larger extent)
OUTLINE_SIGMA_CELLS = 5.5
OUTLINE_LEVEL_FRAC = 0.13
OUTLINE_RES = 300
RNG_SEED = 7


def _outline(p2d_mask, xlim, ylim):
    """Gaussian-density contour of the masked points (data coords)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from scipy.ndimage import gaussian_filter
    xs = np.linspace(xlim[0], xlim[1], OUTLINE_RES + 1)
    ys = np.linspace(ylim[0], ylim[1], OUTLINE_RES + 1)
    H, _, _ = np.histogram2d(p2d_mask[:, 0], p2d_mask[:, 1], bins=[xs, ys])
    H = gaussian_filter(H, sigma=OUTLINE_SIGMA_CELLS)
    if H.max() <= 0:
        return []
    H /= H.max()
    xc, yc = 0.5 * (xs[:-1] + xs[1:]), 0.5 * (ys[:-1] + ys[1:])
    fig, ax = plt.subplots()
    cs = ax.contour(xc, yc, H.T, levels=[OUTLINE_LEVEL_FRAC])
    segs = [s for s in cs.allsegs[0] if len(s) > 3]
    plt.close(fig)
    return max(segs, key=len) if segs else []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shape_id", default="697")
    ap.add_argument("--speed", default="0.003")
    ap.add_argument("--orient_speed", default="0.0035")
    ap.add_argument("--pred_dir", default=str(PRED_DIR))
    ap.add_argument("--suffix", default="",
                    help="appended to output filenames + JS var, e.g. '_410' -> "
                         "fm_inpaint_real_410.js / window.FM_INPAINT_REAL_410")
    ap.add_argument("--roll_deg", type=float, default=0.0,
                    help="extra rotation (deg) about the main flow axis, applied "
                         "after reference_rotation (e.g. 90 to roll the view)")
    args = ap.parse_args()
    sid = str(args.shape_id)
    rng = np.random.default_rng(RNG_SEED)
    pred = Path(args.pred_dir)
    sfx = args.suffix
    png_out = WEB_ROOT / "static" / "images" / f"fm_inpaint_real{sfx}_geom.png"
    json_out = WEB_ROOT / "static" / "data" / f"fm_inpaint_real{sfx}.json"
    js_out = WEB_ROOT / "static" / "data" / f"fm_inpaint_real{sfx}.js"
    var_name = "window.FM_INPAINT_REAL" + sfx.upper()

    meta = json.loads((pred / "meta.json").read_text())
    pos_pred = np.load(pred / "pos.npy").astype(np.float64)          # (N,3) raw frame
    gt_v = np.load(pred / "gt.npy").astype(np.float64)              # (N,3)
    fm_steps = np.load(pred / "fm_steps.npy").astype(np.float64)    # (Sf+1,N,3)
    mae_steps = np.load(pred / "mae_steps.npy").astype(np.float64)  # (Sm+1,N,3)
    mask_pts = np.load(pred / "mask_points.npy").astype(bool)       # (N,)
    # per-point MAE reveal pass (1..maeFrames-1 for masked pts; 0 = context).
    # Used to hide a masked glyph in the MAE panel until its reveal pass.
    reveal_path = pred / "mae_reveal_points.npy"
    mae_reveal = (np.load(reveal_path).astype(np.int64) if reveal_path.exists()
                  else np.zeros(len(mask_pts), dtype=np.int64))
    print(f"shape {sid}  m={args.speed}  N={len(pos_pred)}  "
          f"FM frames={fm_steps.shape[0]}  MAE frames={mae_steps.shape[0]}  "
          f"masked pts={int(mask_pts.sum())}")

    # ---- orientation: same rotation the GT figure uses (from the raw file) ----
    pos_o, vel_o = load_internal(sid, args.orient_speed)
    R = reference_rotation(pos_o, vel_o, sid, args.orient_speed)
    # optional extra roll about the main flow axis (+X after reference_rotation).
    # premultiply so the roll happens in the flow-aligned frame.
    if abs(args.roll_deg) > 1e-9:
        a = np.deg2rad(args.roll_deg)
        c, s = np.cos(a), np.sin(a)
        R_roll = np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=np.float32)
        R = (R_roll @ R).astype(np.float32)
        print(f"  applied {args.roll_deg:g} deg roll about flow axis")

    # sanity: the prediction's pos should match array_internal xyz (same loader)
    pos_raw, _ = load_internal(sid, args.speed)
    if len(pos_raw) != len(pos_pred):
        print(f"  [warn] N mismatch raw={len(pos_raw)} pred={len(pos_pred)} "
              f"(using prediction pos)")

    pos_r = rot3(pos_pred.astype(np.float32), R).astype(np.float64)
    faces, normals = load_stl(sid)
    faces_r = rot3_faces(faces, R)
    normals_r = rot3(normals, R)

    # ---- view bbox = (mesh bbox) ∪ (masked-points bbox) + pad ----
    flat = faces_r.reshape(-1, 3)
    xmin, xmax = float(flat[:, 0].min()), float(flat[:, 0].max())
    ymin, ymax = float(flat[:, 1].min()), float(flat[:, 1].max())
    mp_r = pos_r[mask_pts][:, :2]
    bx0 = min(xmin, float(mp_r[:, 0].min())); bx1 = max(xmax, float(mp_r[:, 0].max()))
    by0 = min(ymin, float(mp_r[:, 1].min())); by1 = max(ymax, float(mp_r[:, 1].max()))
    pad = MASK_UP_PAD * max(bx1 - bx0, by1 - by0)
    xlim = (bx0 - pad, bx1 + pad); ylim = (by0 - pad, by1 + pad)
    ext_w, ext_h = xlim[1] - xlim[0], ylim[1] - ylim[0]
    w_px, h_px = render_mesh(faces_r, normals_r, xlim, ylim, png_out)
    print(f"  wrote {png_out}  ({w_px} x {h_px} px)")

    def to_px(p2):
        return np.stack([(p2[:, 0] - xlim[0]) / ext_w * w_px,
                         (1.0 - (p2[:, 1] - ylim[0]) / ext_h) * h_px], axis=1)

    pos_px = to_px(pos_r[:, :2])

    # ---- velocity -> pixel glyphs. Length scale fixed by the GT field (so all
    # three (GT / FM / MAE) frames share one |v|->px mapping). y points down. ----
    def vel_to_px(v3):
        vr = rot3(v3.astype(np.float32), R).astype(np.float64)      # (N,3) rotated
        speed = np.linalg.norm(vr, axis=1)
        ratio = np.clip(speed / max(vref, 1e-12), 0.0, SPEED_CAP_RATIO)
        mag = ARROW_REF_LEN_PX * np.sqrt(ratio)
        d2 = vr[:, :2] / np.maximum(speed[:, None], 1e-12)
        vp = d2 * mag[:, None]
        vp[:, 1] *= -1.0
        return vp

    vref = float(np.percentile(np.linalg.norm(rot3(gt_v.astype(np.float32), R), axis=1), VREF_PCTL))
    gt_px = vel_to_px(gt_v)
    fm_px = np.stack([vel_to_px(fm_steps[t]) for t in range(fm_steps.shape[0])], 0)   # (Sf+1,N,2)
    mae_px = np.stack([vel_to_px(mae_steps[t]) for t in range(mae_steps.shape[0])], 0)  # (Sm+1,N,2)

    # ---- subsample. Cap the masked share (each masked glyph stores ALL frames,
    # so it dominates the payload) and keep a visible band of context glyphs.   ----
    n_total = min(N_ARROWS, len(pos_px))
    idx_mask = np.where(mask_pts)[0]
    idx_ctx = np.where(~mask_pts)[0]
    n_mask = min(int(round(n_total * 0.80)), len(idx_mask))
    sel_mask = rng.choice(idx_mask, n_mask, replace=False)
    sel_ctx = rng.choice(idx_ctx, min(n_total - len(sel_mask), len(idx_ctx)), replace=False)
    print(f"  {len(sel_ctx)} ctx + {len(sel_mask)} masked glyphs")

    def r1(x): return round(float(x), 1)

    # context: GT velocity (fixed boundary / outside-mask field)
    context = [[r1(pos_px[i, 0]), r1(pos_px[i, 1]), r1(gt_px[i, 0]), r1(gt_px[i, 1])]
               for i in sel_ctx]

    # masked positions + per-frame velocity (flat [vx0,vy0,vx1,vy1,...], 1-dp px)
    masked_xy = [[r1(pos_px[i, 0]), r1(pos_px[i, 1])] for i in sel_mask]
    fm_glyph = [[r1(v) for t in range(fm_px.shape[0]) for v in fm_px[t, i]] for i in sel_mask]
    mae_glyph = [[r1(v) for t in range(mae_px.shape[0]) for v in mae_px[t, i]] for i in sel_mask]
    gt_glyph = [[r1(gt_px[i, 0]), r1(gt_px[i, 1])] for i in sel_mask]

    # MAE reveal pass per masked glyph (int): the glyph is hidden in the MAE
    # panel for steps < reveal, shown from `reveal` onward. (FM shows all.)
    mae_reveal_glyph = [int(mae_reveal[i]) for i in sel_mask]

    # ---- mask outline (single boundary) ----
    def outline_to_px(p2d):
        d = _outline(np.asarray(p2d), xlim, ylim) if len(p2d) >= 6 else []
        if not len(d):
            return []
        op = to_px(np.asarray(d))
        if len(op) > OUTLINE_MAX_PTS:
            op = op[np.linspace(0, len(op) - 1, OUTLINE_MAX_PTS).round().astype(int)]
        return [[r1(x), r1(y)] for x, y in op]

    shell0 = outline_to_px(pos_r[idx_mask][:, :2])

    # ---- mask label anchor (bbox of the outline) ----
    if len(shell0) >= 3:
        op = np.asarray(shell0, dtype=np.float64)
        x0, y0, x1, y1 = op[:, 0].min(), op[:, 1].min(), op[:, 0].max(), op[:, 1].max()
        cx, cy = op[:, 0].mean(), op[:, 1].mean()
    else:
        mp = pos_px[idx_mask]
        x0, y0, x1, y1 = mp[:, 0].min(), mp[:, 1].min(), mp[:, 0].max(), mp[:, 1].max()
        cx, cy = mp[:, 0].mean(), mp[:, 1].mean()
    mask_label = {"cx": r1(cx), "cy": r1(cy), "x0": r1(x0), "y0": r1(y0), "x1": r1(x1), "y1": r1(y1)}

    # ---- fixed-boundary anchors (inlet/outlet centroids + stub tips), dropping
    # any that land inside the mask outline — same logic as the GT exporter ----
    def _in_poly(pt, poly):
        if poly is None or len(poly) < 3:
            return False
        x, y = pt; inside = False; n = len(poly); j = n - 1
        for i in range(n):
            xi, yi = poly[i]; xj, yj = poly[j]
            if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
                inside = not inside
            j = i
        return inside

    def _add_bc(lst, p2, tag):
        xy = [r1(p2[0]), r1(p2[1])]
        if _in_poly(xy, shell0):
            print(f"  fixed-BC: {tag} {xy} inside mask -> skip"); return
        for q in lst:
            if abs(q[0] - xy[0]) < 0.02 * ext_w and abs(q[1] - xy[1]) < 0.02 * ext_h:
                return
        lst.append(xy)

    fixed_bc = []
    for which in ("inlet", "outlet"):
        c3 = opening_centroid(sid, args.speed, which)
        if c3 is not None:
            _add_bc(fixed_bc, to_px(((np.asarray(c3, np.float64) @ R.T)[:2])[None, :])[0], which)
    flat_xy = faces_r.reshape(-1, 3)[:, :2]
    for j in (int(np.argmin(flat_xy[:, 0])), int(np.argmax(flat_xy[:, 0]))):
        _add_bc(fixed_bc, to_px(flat_xy[j][None, :])[0], "stub-tip")
    print(f"  fixed-BC anchors: {fixed_bc}")

    payload = {
        "shape_id": sid, "flow_speed": args.speed,
        "fm_ckpt": meta.get("fm_ckpt"), "mae_ckpt": meta.get("mae_ckpt"),
        "w": w_px, "h": h_px,
        "geom": f"static/images/fm_inpaint_real{sfx}_geom.png",
        "L0": ARROW_REF_LEN_PX, "vref": round(vref, 6),
        "fmFrames": int(fm_px.shape[0]), "maeFrames": int(mae_px.shape[0]),
        "maskLabel": mask_label, "fixedBC": fixed_bc,
        "n_context": len(sel_ctx), "n_masked": len(sel_mask),
        "shells": [shell0],
        "context": context,
        "masked": masked_xy,
        "fm": fm_glyph,
        "mae": mae_glyph,
        "maeReveal": mae_reveal_glyph,
        "gt": gt_glyph,
    }
    blob = json.dumps(payload, separators=(",", ":"))
    json_out.write_text(blob)
    js_out.write_text(var_name + "=" + blob + ";\n")
    print(f"  wrote {json_out} + {js_out}  ({len(blob)/1024:.0f} KB)")


if __name__ == "__main__":
    main()
