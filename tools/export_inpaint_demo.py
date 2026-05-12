"""Export real CFD data for the website "flow matching as inpainting" figure.

Picks one Aneumo aneurysm, loads its ground-truth velocity field at one flow
speed, orients it the same way the paper figures do (dominant flow -> +X, inlet
pointing down, slight outlet tilt toward the camera), renders the flat-shaded
geometry mesh to a PNG, subsamples velocity glyphs, picks a contiguous ball
mask deep in the aneurysm sac, and writes:

    website/static/images/fm_inpaint_geom.png   -- shaded mesh, opaque white bg
    website/static/data/fm_inpaint.json         -- {w, h, geom, L0, vref, nShells,
                                                     maskLabel, fixedBC:[[x,y],..],
                                                     shells:[poly..],
                                                     context:[[x,y,vx,vy],..],
                                                     masked:[[x,y,vx,vy,shell],..]}

(x, y) are pixel coords in the PNG; (vx, vy) are the GT velocity vectors scaled
to pixels such that |v| == L0 px at the colour-scale reference speed `vmax`
(percentile of the speed distribution) and y points downward.

The website draws the mesh, renders the `context` glyphs statically (GT flow
outside the mask), and uses the masked glyphs two ways: (fig 4 / flow matching)
each in-mask glyph starts as a random vector and over the integration time t its
angle slerps + its length lerps to the GT value, recoloured by |v(t)| each frame;
(fig 5 / iterative MAE) the masked glyphs are revealed outside-in by `shell`
index over `nShells` discrete steps. The coral `shells[0]` outline + `maskLabel`
anchor place the "masked region" annotation on both figures.

No GPU / no model needed -- pure numpy + matplotlib. Run with the `ffm` env:
    /u/home/wejo/.conda/envs/ffm/bin/python website/tools/export_inpaint_demo.py
"""
from __future__ import annotations

import argparse
import base64
import json
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import PolyCollection
from PIL import Image
from stl import mesh as stl_mesh
from scipy.ndimage import gaussian_filter

# ------------------------------------------------------------------ config
ANEUMO_ROOT = Path("/vol/miltank/datasets/cfd-blood-flow/Aneumo/extracted_files")
STL_SCALE = 1e-3                         # STL is mm, .npy is m
MESH_GRAY = np.array([0.80, 0.81, 0.84], dtype=np.float32)
MESH_AMBIENT, MESH_DIFFUSE = 0.42, 0.58
INLET_DOWN_TILT_DEG = 35.0
OUTLET_TILT_DEG = 20.0

ARROW_REF_LEN_PX = 36.0                  # |v| (px) at the speed-scale reference `vref`
VREF_PCTL = 90.0                         # length / colour reference = this speed pctl
SPEED_CAP_RATIO = 16.0                   # clip (speed/vref) here -> longest arrow ~4*L0
NOISE_STD_PX = 14.0                       # (unused by the site now -- kept for reference)
N_ARROWS = 5400                          # total subsampled glyphs
MASK_FRAC = 0.60                         # fraction of points inside the mask
MASK_UP_BIAS = 0.05                      # nudge the mask centre up by this much of the view height
MASK_UP_STRETCH = 0.78                   # <1: mask reaches ~1/x further toward the top of the view
N_SHELLS = 5                             # iterative-MAE reveal: this many outside-in shells
FIG_H_IN = 5.6                           # render height (inches)
DPI = 200                                # -> ~1100 px tall PNG
OUTLINE_RES = 300
OUTLINE_SIGMA_CELLS = 5.5                # smaller -> the outline hugs the masked points more tightly
OUTLINE_LEVEL_FRAC = 0.13                # higher contour level -> tighter outline
OUTLINE_MAX_PTS = 190                    # decimate the contour polygon to ~this many
RNG_SEED = 7

WEB_ROOT = Path(__file__).resolve().parents[1]
PNG_OUT = WEB_ROOT / "static" / "images" / "fm_inpaint_geom.png"
JSON_OUT = WEB_ROOT / "static" / "data" / "fm_inpaint.json"
JS_OUT = WEB_ROOT / "static" / "data" / "fm_inpaint.js"   # window.FM_INPAINT = {...}


# ------------------------------------------------------- geometry helpers
def load_internal(sid: str, speed: str):
    a = np.load(ANEUMO_ROOT / sid / "npy" / f"m={speed}" / f"array_internal_{sid}.npy")
    return a[:, :3].astype(np.float32), a[:, 4:7].astype(np.float32)   # pos, vel


def opening_centroid(sid: str, speed: str, which: str):
    p = ANEUMO_ROOT / sid / "npy" / f"m={speed}" / f"array_{which}_{sid}.npy"
    return np.load(p)[:, :3].astype(np.float32).mean(0) if p.exists() else None


def load_stl(sid: str):
    m = stl_mesh.Mesh.from_file(str(ANEUMO_ROOT / sid / "Stl" / f"{sid}.stl"))
    faces = m.vectors.astype(np.float32) * STL_SCALE
    normals = m.normals.astype(np.float32)
    return faces, normals / (np.linalg.norm(normals, axis=1, keepdims=True) + 1e-12)


def _parse_vtp_points(path: Path):
    text = Path(path).read_text()
    da = ET.fromstring(text).find(".//Piece/Points/DataArray")
    n_pts = int(ET.fromstring(text).find(".//Piece").attrib["NumberOfPoints"])
    raw = base64.b64decode(da.text.strip())
    n_bytes = struct.unpack("<Q", raw[:8])[0]
    return np.frombuffer(raw[8:8 + n_bytes], dtype=np.float32).reshape(n_pts, 3)


def inlet_outward_normal(sid: str, speed: str, interior_centroid):
    path = ANEUMO_ROOT / sid / "VTK" / f"m={speed}" / "inlet.vtp"
    if not path.exists():
        return None
    pts = _parse_vtp_points(path)
    c = pts.mean(0)
    _, _, Vt = np.linalg.svd(pts - c, full_matrices=False)
    n = Vt[2].astype(np.float32)
    if np.dot(n, interior_centroid - c) > 0:
        n = -n
    return n / max(np.linalg.norm(n), 1e-12)


# ------------------------------------------------------ orientation matrix
def reference_rotation(pos, vel, sid, speed):
    """LR (flow -> +X)  o  X-down (inlet -> -Y)  o  extra X tilt  o  Y outlet tilt."""
    speed_b = np.linalg.norm(vel, axis=1)
    sel = speed_b >= np.quantile(speed_b, 0.75)
    mv = vel[sel, :2].mean(0)
    theta = -float(np.arctan2(mv[1], mv[0])) if np.hypot(*mv) > 1e-12 else 0.0
    c, s = np.cos(theta), np.sin(theta)
    R_xy = np.eye(3, dtype=np.float32)
    R_xy[:2, :2] = [[c, -s], [s, c]]

    n_in = inlet_outward_normal(sid, speed, pos.mean(0))
    if n_in is None:
        raise RuntimeError(f"missing inlet.vtp for sid={sid} m={speed}")
    n_lr = R_xy @ n_in
    if n_lr[1] ** 2 + n_lr[2] ** 2 < 1e-10:
        R_x, phi_x = np.eye(3, dtype=np.float32), 0.0
    else:
        phi_x = float(np.arctan2(n_lr[2], -n_lr[1]))
        c, s = np.cos(phi_x), np.sin(phi_x)
        R_x = np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=np.float32)

    phi_e = np.deg2rad(INLET_DOWN_TILT_DEG) * (1.0 if phi_x >= 0 else -1.0)
    c, s = np.cos(phi_e), np.sin(phi_e)
    R_xe = np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=np.float32)

    psi = np.deg2rad(-OUTLET_TILT_DEG)
    c, s = np.cos(psi), np.sin(psi)
    R_y = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=np.float32)

    return (R_y @ R_xe @ R_x @ R_xy).astype(np.float32)


def rot3(arr, R):
    return arr @ R.T


def rot3_faces(faces, R):
    return (faces.reshape(-1, 3) @ R.T).reshape(faces.shape).astype(np.float32)


# ---------------------------------------------------------------- render
def render_mesh(faces_r, normals_r, xlim, ylim, out_path):
    aspect = (xlim[1] - xlim[0]) / (ylim[1] - ylim[0])
    fig, ax = plt.subplots(figsize=(FIG_H_IN * aspect, FIG_H_IN))
    ax.set_position([0, 0, 1, 1])
    ax.set_xlim(*xlim); ax.set_ylim(*ylim); ax.axis("off")
    depth = faces_r[:, :, 2].mean(1)
    shade = MESH_AMBIENT + MESH_DIFFUSE * np.abs(normals_r[:, 2])
    rgb = (np.broadcast_to(MESH_GRAY, (faces_r.shape[0], 3)) * shade[:, None]).clip(0, 1)
    order = np.argsort(depth)
    pc = PolyCollection(faces_r[order, :, :2], facecolors=rgb[order],
                        edgecolors=rgb[order], antialiased=True, linewidths=0.5)
    ax.add_collection(pc)
    fig.savefig(out_path, dpi=DPI, facecolor="white")     # opaque white bg -> small RGB PNG
    plt.close(fig)
    # quantise the smooth shading to keep the file light (it's drawn at ~half size)
    im = Image.open(out_path).convert("RGB").quantize(colors=192, dither=Image.NONE)
    im.save(out_path, optimize=True)
    return im.size                                        # true (w, h) of the saved PNG


def outline_polygon(p2d_mask, xlim, ylim):
    """Loose Gaussian-smoothed-density contour of the masked points (data coords)."""
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
    if not segs:
        return []
    return max(segs, key=len)   # (M, 2) data coords


# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shape_id", default="302")
    ap.add_argument("--speed", default="0.003")
    ap.add_argument("--orient_speed", default="0.0035")
    args = ap.parse_args()
    sid = str(args.shape_id)
    rng = np.random.default_rng(RNG_SEED)

    print(f"shape {sid}  flow m={args.speed}  (orientation from m={args.orient_speed})")
    pos, vel = load_internal(sid, args.speed)
    pos_o, vel_o = load_internal(sid, args.orient_speed)
    R = reference_rotation(pos_o, vel_o, sid, args.orient_speed)

    pos_r = rot3(pos, R)
    vel_r = rot3(vel, R)
    faces, normals = load_stl(sid)
    faces_r = rot3_faces(faces, R)
    normals_r = rot3(normals, R)

    # mesh bbox + a span used only to size the up-bias (the final view is computed
    # below after we know how far the mask reaches, so the round mask fits unflattened)
    flat = faces_r.reshape(-1, 3)
    xmin, xmax = float(flat[:, 0].min()), float(flat[:, 0].max())
    ymin, ymax = float(flat[:, 1].min()), float(flat[:, 1].max())
    mesh_span = max(xmax - xmin, ymax - ymin)

    # ---- mask: a ball over the widest part of the lumen (anchor = max wall-distance
    # interior point), nudged up + stretched toward the top of the view so it covers
    # more of the aneurysm sac, then take the closest MASK_FRAC of points.
    from scipy.spatial import cKDTree
    wall_p = ANEUMO_ROOT / sid / "npy" / f"m={args.speed}" / f"array_wall_{sid}.npy"
    if wall_p.exists():
        wall_pts = np.load(wall_p)[:, :3].astype(np.float32)
        wall_dist, _ = cKDTree(wall_pts).query(pos, k=1)
        a_r = pos_r[int(np.argmax(wall_dist))].astype(np.float64).copy()
        how = "max wall-distance"
    else:
        c_in = opening_centroid(sid, args.speed, "inlet")
        c_out = opening_centroid(sid, args.speed, "outlet")
        if c_in is not None and c_out is not None:
            a_r = pos_r[int(np.argmax(np.minimum(np.linalg.norm(pos - c_in, axis=1),
                                                 np.linalg.norm(pos - c_out, axis=1))))].astype(np.float64).copy()
            how = "deepest from openings"
        else:
            a_r = pos_r[rng.integers(len(pos))].astype(np.float64).copy(); how = "random"
    a_r[1] += MASK_UP_BIAS * mesh_span                   # nudge the centre toward the top of the view
    dxyz = pos_r.astype(np.float64) - a_r
    up = dxyz[:, 1] > 0                                  # points above the anchor (toward image top)
    dxyz[up, 1] *= MASK_UP_STRETCH                       # ...count as nearer -> the ball bulges upward
    d_anchor = np.linalg.norm(dxyz, axis=1)
    pt_mask = d_anchor <= np.quantile(d_anchor, MASK_FRAC)
    print(f"  mask ({how}, up-bias {MASK_UP_BIAS}, stretch {MASK_UP_STRETCH}): {pt_mask.sum()}/{len(pos)} pts")

    # ---- view bbox = (mesh bbox) ∪ (masked-points bbox), then a comfortable pad
    # so the full round mask fits inside the rendered image (it isn't flattened at
    # the top where the ball pokes past the geometry).
    mp_r = pos_r[pt_mask][:, :2]
    bx0 = min(xmin, float(mp_r[:, 0].min())); bx1 = max(xmax, float(mp_r[:, 0].max()))
    by0 = min(ymin, float(mp_r[:, 1].min())); by1 = max(ymax, float(mp_r[:, 1].max()))
    pad = 0.06 * max(bx1 - bx0, by1 - by0)
    xlim = (bx0 - pad, bx1 + pad); ylim = (by0 - pad, by1 + pad)
    ext_w, ext_h = xlim[1] - xlim[0], ylim[1] - ylim[0]
    aspect = ext_w / ext_h
    w_px, h_px = render_mesh(faces_r, normals_r, xlim, ylim, PNG_OUT)   # true PNG dims
    print(f"  wrote {PNG_OUT}  ({w_px} x {h_px} px)")

    # ---- iterative-MAE reveal order: assign each masked point an equal-count
    # "shell" by distance to the anchor -- shell 0 = outermost (revealed first),
    # shell N-1 = innermost (revealed last).
    idx_mask = np.where(pt_mask)[0]
    dm = d_anchor[idx_mask]
    ecdf = np.empty(len(dm)); ecdf[np.argsort(dm)] = np.arange(len(dm)) / max(len(dm), 1)
    shell_masked = np.clip(((1.0 - ecdf) * N_SHELLS).astype(int), 0, N_SHELLS - 1)
    shell_full = np.full(len(pos), -1, dtype=int); shell_full[idx_mask] = shell_masked

    # ---- pixel mapping
    def to_px(p2):
        return np.stack([(p2[:, 0] - xlim[0]) / ext_w * w_px,
                         (1.0 - (p2[:, 1] - ylim[0]) / ext_h) * h_px], axis=1)

    # GT velocity glyphs: length encodes speed (sqrt-compressed dynamic range, like
    # the paper's quiver), direction = true projected flow direction, y points down.
    speed = np.linalg.norm(vel_r, axis=1)
    vref = float(np.percentile(speed, VREF_PCTL))
    ratio = np.clip(speed / max(vref, 1e-12), 0.0, SPEED_CAP_RATIO)
    mag_px = ARROW_REF_LEN_PX * np.sqrt(ratio)       # |arrow| in px; == L0 at speed==vref
    dir2 = vel_r[:, :2] / np.maximum(speed[:, None], 1e-12)
    vel_px = dir2 * mag_px[:, None]
    vel_px[:, 1] *= -1.0
    # colour in the website: norm = min(1, (|arrow_px| / L0)**2)  ==  min(1, speed/vref)

    pos_px = to_px(pos_r[:, :2])

    # ---- subsample, keeping the mask reasonably dense
    n_total = min(N_ARROWS, len(pos))
    n_mask = int(round(n_total * max(0.42, pt_mask.mean())))
    idx_mask = np.where(pt_mask)[0]
    idx_ctx = np.where(~pt_mask)[0]
    sel_mask = rng.choice(idx_mask, min(n_mask, len(idx_mask)), replace=False)
    sel_ctx = rng.choice(idx_ctx, min(n_total - len(sel_mask), len(idx_ctx)), replace=False)

    def pack(sel, shell_of=None):
        out = []
        for i in sel:
            row = [round(float(pos_px[i, 0]), 1), round(float(pos_px[i, 1]), 1),
                   round(float(vel_px[i, 0]), 2), round(float(vel_px[i, 1]), 2)]
            if shell_of is not None:
                row.append(int(shell_of[i]))            # iterative-reveal shell index
            out.append(row)
        return out

    # ---- nested mask contours (data -> px, longest segment, decimated):
    # shellPoly[k] bounds the region still masked after step k (= {shell >= k}).
    def outline_to_px(p2d):
        d = outline_polygon(np.asarray(p2d), xlim, ylim) if len(p2d) >= 6 else []
        if not len(d):
            return []
        op = to_px(np.asarray(d))
        if len(op) > OUTLINE_MAX_PTS:
            op = op[np.linspace(0, len(op) - 1, OUTLINE_MAX_PTS).round().astype(int)]
        return [[round(float(x), 1), round(float(y), 1)] for x, y in op]

    p2d_masked = pos_r[idx_mask][:, :2]
    shells_px = [outline_to_px(p2d_masked[shell_masked >= k]) for k in range(N_SHELLS)]

    # ---- mask label anchor: bbox + centroid of the outer outline (px); the site
    # uses it to place a "masked region" label with a short leader to the outline.
    outer = np.asarray(shells_px[0], dtype=np.float64) if len(shells_px[0]) >= 3 else None
    if outer is None:
        mp = pos_px[idx_mask]
        x0, y0, x1, y1 = float(mp[:, 0].min()), float(mp[:, 1].min()), float(mp[:, 0].max()), float(mp[:, 1].max())
        cx, cy = float(mp[:, 0].mean()), float(mp[:, 1].mean())
    else:
        x0, y0, x1, y1 = float(outer[:, 0].min()), float(outer[:, 1].min()), float(outer[:, 0].max()), float(outer[:, 1].max())
        cx, cy = float(outer[:, 0].mean()), float(outer[:, 1].mean())
    mask_label = {"cx": round(cx, 1), "cy": round(cy, 1),
                  "x0": round(x0, 1), "y0": round(y0, 1), "x1": round(x1, 1), "y1": round(y1, 1)}

    # ---- fixed-boundary anchors: the inlet & outlet openings (their centroids,
    # rotated + projected to pixels). The site draws a "fixed boundaries" label
    # with a leader to each. Drop any opening whose projection lands *inside* the
    # mask outline (e.g. a stub pointing toward/away from the camera projects onto
    # the body) -- a "fixed boundary" dot inside the masked region reads wrong.
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
    outer_px = shells_px[0] if len(shells_px[0]) >= 3 else None

    def _add_bc(xy_list, p2, tag):
        xy = [round(float(p2[0]), 1), round(float(p2[1]), 1)]
        if _in_poly(xy, outer_px):
            print(f"  fixed-BC: {tag} {xy} lands inside the mask -> skipped"); return
        for q in xy_list:
            if abs(q[0] - xy[0]) < 0.02 * ext_w and abs(q[1] - xy[1]) < 0.02 * ext_h:
                return                                   # too close to an existing anchor
        xy_list.append(xy)

    fixed_bc = []
    for which in ("inlet", "outlet"):                    # inlet/outlet opening centroids
        c3 = opening_centroid(sid, args.speed, which)
        if c3 is not None:
            _add_bc(fixed_bc, to_px(((np.asarray(c3, np.float64) @ R.T)[:2])[None, :])[0], which)
    # plus the geometry's left / right stub openings (their visible rims project to
    # the mesh's x-extremes) -- these are the other fixed boundaries
    flat_xy = faces_r.reshape(-1, 3)[:, :2]
    for j in (int(np.argmin(flat_xy[:, 0])), int(np.argmax(flat_xy[:, 0]))):
        _add_bc(fixed_bc, to_px(flat_xy[j][None, :])[0], "stub-tip")
    print(f"  fixed-BC anchors: {fixed_bc}")

    payload = {
        "shape_id": sid,
        "flow_speed": args.speed,
        "w": w_px, "h": h_px,
        "geom": "static/images/fm_inpaint_geom.png",
        "L0": ARROW_REF_LEN_PX,            # arrow px length at speed == vref
        "vref": round(vref, 6),            # reference speed (length & colour scale)
        "nShells": N_SHELLS,               # iterative-MAE: # of outside-in reveal shells
        "maskLabel": mask_label,           # bbox + centroid of the mask outline (px)
        "fixedBC": fixed_bc,               # [[x,y],…] inlet/outlet centroids in px
        "n_context": len(sel_ctx), "n_masked": len(sel_mask),
        "shells": shells_px,               # shells[k] = still-masked boundary after step k
        "context": pack(sel_ctx),          # [x, y, vx, vy]
        "masked": pack(sel_mask, shell_full),  # [x, y, vx, vy, shell]
    }
    blob = json.dumps(payload, separators=(",", ":"))
    JSON_OUT.write_text(blob)
    JS_OUT.write_text("window.FM_INPAINT=" + blob + ";\n")
    sz = len(blob) / 1024
    print(f"  wrote {JSON_OUT} + {JS_OUT}  ({sz:.0f} KB)  "
          f"{len(sel_ctx)} ctx + {len(sel_mask)} masked glyphs, "
          f"shells {[len(s) for s in shells_px]} pts")


if __name__ == "__main__":
    main()
