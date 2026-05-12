/* ---------------------------------------------------------------------------
   Project page interactions:
     · BibTeX copy
     · deformation slider                (interactive figure 1)
     · mask-ratio slider                 (interactive figure 2)
     · before / after wipe               (interactive figure 3)
     · flow-matching inpainting canvas   (interactive figure 4)
     · iterative-MAE inpainting canvas   (interactive figure 5)
     · "Local geometry editing" curtain comparison    (#dfm-grid):
       state 1 = original geometry + GT flow, state 2 = deformed geometry +
       the inpainting prediction; data baked by render_deform_mae.py

   The animated figures (1, 2, 4, 5) start *paused* - the animation runs only
   after you press a figure's play button; the page coordinator still makes sure
   only one auto-player runs at a time (whichever is most in view).

   The "Scroll-through animation" toggle (above figure 1) applies to figure 1
   only: while on, the figure pins to the screen and your scrolling steps through
   its deformation frames, then releases so you can scroll past it. (Falls back to
   a non-pinned scroll-linked animation if the figure is taller than the viewport.)
   --------------------------------------------------------------------------- */

document.addEventListener('DOMContentLoaded', function () {

  var ICON_PAUSE = '❚❚';
  var ICON_PLAY  = '▶';

  function clamp(x, a, b) { return Math.max(a, Math.min(b, x)); }
  function fmt(x) { return x.toFixed(1) + '%'; }

  /* ---- copy BibTeX ----------------------------------------------------- */
  var copyBtn = document.getElementById('copy-bibtex');
  if (copyBtn) {
    copyBtn.addEventListener('click', function () {
      navigator.clipboard.writeText(document.getElementById('bibtex-code').innerText).then(function () {
        var old = copyBtn.textContent;
        copyBtn.textContent = 'Copied!';
        setTimeout(function () { copyBtn.textContent = old; }, 1600);
      });
    });
  }

  /* ---- coordinator: which figures are "active" (allowed to animate) ----- *
   * Players register { el, setActive(bool), independent? }. Among the *shared*
   * players at most one is active at a time: whichever one a user most recently
   * *claimed* by pressing its play button, else the one with the largest visible
   * fraction (ties broken by document order). `independent` players (e.g. the
   * side-by-side FM / MAE pair, which are meant to run in parallel) don't compete
   * — each is simply active while it's at all visible. registerPlayer returns a
   * `claim` fn; calling it on an explicit play makes that shared figure the
   * active one.                                                                */
  var players = [], claimed = null;
  function reconcile() {
    var shared = players.filter(function (p) { return !p.independent; });
    var winner = (claimed && claimed.ratio > 0) ? claimed : null;
    if (!winner) shared.forEach(function (p) {
      if (p.ratio > 0 && (!winner || p.ratio > winner.ratio)) winner = p;
    });
    players.forEach(function (p) {
      p.setActive(p.independent ? (p.ratio > 0) : (p === winner));
    });
  }
  var haveIO = 'IntersectionObserver' in window;
  var io = haveIO ? new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      var p = players.find(function (q) { return q.el === e.target; });
      if (p) p.ratio = e.isIntersecting ? (e.intersectionRatio || 0.0001) : 0;
    });
    reconcile();
  }, { threshold: [0, 0.05, 0.15, 0.3, 0.5, 0.75, 1] }) : null;
  function registerPlayer(el, setActive, independent) {
    if (!el) { setActive(true); return function () {}; }
    var p = { el: el, setActive: setActive, ratio: 0, independent: !!independent };
    players.push(p);
    if (haveIO) io.observe(el); else { p.ratio = 1; }
    reconcile();
    return function () { if (!p.independent) { claimed = p; reconcile(); } };   // claim the active slot
  }

  /* ---- scroll-through registry: only the opening (deformation) figure
     registers here, so the "Scroll-through animation" toggle pins+scrubs it.   */
  var scrollyPlayers = [];   // { el, steps, vmin, vmax, render, setAuto, scrolly, mode, h, scrub, top }
  function registerScrolly(cfg) { cfg.scrolly = null; scrollyPlayers.push(cfg); }

  /* ---- interactive 1: deform the artery (continuous, ping-pong) -------- */
  var dSlider = document.getElementById('deform-slider');
  if (dSlider) {
    var dN = 5;
    var maeNMSE = [0.90, 5.18, 10.44, 3.64, 2.66];
    var supNMSE = [19.30, 23.80, 23.68, 26.50, 27.55];
    var dCells = {
      geo: document.getElementById('dc-geo'),
      gt:  document.getElementById('dc-gt'),
      mae: document.getElementById('dc-mae'),
      sup: document.getElementById('dc-sup')
    };
    var maeErrEl = document.getElementById('err-mae');
    var supErrEl = document.getElementById('err-sup');
    var dReadout = document.getElementById('deform-readout');

    function dRender(v) {
      v = clamp(v, 1, dN);
      var i = clamp(Math.floor(v), 1, dN);
      var j = Math.min(i + 1, dN);
      var f = clamp(v - i, 0, 1);
      Object.keys(dCells).forEach(function (k) {
        dCells[k].querySelectorAll('img').forEach(function (img, idx) {
          var lvl = idx + 1;
          img.style.opacity = (lvl === i) ? (1 - f) : (lvl === j ? f : 0);
        });
      });
      var lvl = clamp(Math.round(v), 1, dN);
      if (maeErrEl) maeErrEl.textContent = 'nMSE ' + fmt(maeNMSE[lvl - 1]);
      if (supErrEl) supErrEl.textContent = 'nMSE ' + fmt(supNMSE[lvl - 1]);
      if (dReadout) dReadout.innerHTML =
        'Deformation step <b>' + lvl + '</b> of ' + dN +
        ' &nbsp;·&nbsp; nMSE &nbsp; L-MAE <b>' + fmt(maeNMSE[lvl - 1]) +
        '</b> &nbsp;vs&nbsp; supervised <b>' + fmt(supNMSE[lvl - 1]) + '</b>';
      dSlider.value = v;
      dSlider.style.setProperty('--fill', ((v - 1) / (dN - 1) * 100) + '%');
    }

    var dv = 1, dDir = 1, dSpeed = 1.25;        // units / second
    var dWantPlay = false, dActive = false;     // starts paused: animation begins only on a play click
    var dRaf = null, dLast = null;
    var dBtn = document.getElementById('deform-play');

    function dRunning() { return dWantPlay && dActive; }
    function dSyncBtn() { if (dBtn) dBtn.innerHTML = dRunning() ? ICON_PAUSE : ICON_PLAY; }
    function dStep(ts) {
      if (!dRunning()) { dRaf = null; dLast = null; return; }
      if (dLast == null) dLast = ts;
      dv += dDir * dSpeed * (ts - dLast) / 1000; dLast = ts;
      if (dv >= dN) { dv = dN; dDir = -1; }
      if (dv <= 1)  { dv = 1;  dDir = 1;  }
      dRender(dv);
      dRaf = requestAnimationFrame(dStep);
    }
    function dKick() { if (dRunning() && dRaf == null) { dLast = null; dRaf = requestAnimationFrame(dStep); } }
    function dStop() { if (dRaf) cancelAnimationFrame(dRaf); dRaf = null; dLast = null; }
    var dClaim = function () {};
    function dPlay()    { dClaim(); dWantPlay = true;  dSyncBtn(); dKick(); }
    function dPause()   { dWantPlay = false; dSyncBtn(); dStop(); }
    // scroll-through mode: while on, the toggle drives dRender straight from the
    // scroll position, so the rAF loop is parked; turning it off leaves the
    // figure paused (it resumes only on a play click).
    function dSetAuto() { dWantPlay = false; dSyncBtn(); dStop(); }

    if (dBtn) dBtn.addEventListener('click', function () { dRunning() ? dPause() : dPlay(); });
    dSlider.addEventListener('pointerdown', dPause);
    dSlider.addEventListener('input', function () { dPause(); dv = parseFloat(dSlider.value); dRender(dv); });

    dRender(dv);
    var dEl = dSlider.closest('.interactive');
    // registerPlayer enforces "one active player at a time" - but a figure only
    // *starts* once you press its play button (which claims the active slot).
    dClaim = registerPlayer(dEl, function (on) { dActive = on; dSyncBtn(); on ? dKick() : dStop(); });
    registerScrolly({ el: dEl, steps: dN, vmin: 1, vmax: dN, render: dRender, setAuto: dSetAuto });
  }

  /* ---- interactive 2: masking ratio (discrete, looping) --------------- */
  var mSlider = document.getElementById('mr-slider');
  if (mSlider) {
    var fracs = ['20%', '60%', '99%'];
    var maeMR = [3.9, 16.8, 43.0];
    var fmMR  = [18.7, 200.7, 78.1];
    var mCells = [document.getElementById('mc-in'), document.getElementById('mc-mae'), document.getElementById('mc-fm')];
    var mFracEl = document.getElementById('mr-frac');
    var mMaeEl  = document.getElementById('mr-err-mae');
    var mFmEl   = document.getElementById('mr-err-fm');
    var mReadout = document.getElementById('mr-readout');

    function mRender(kk) {
      var k = clamp(Math.round(kk), 0, 2);
      mCells.forEach(function (cell) {
        cell.querySelectorAll('img').forEach(function (img, idx) { img.style.opacity = (idx === k) ? 1 : 0; });
      });
      if (mFracEl) mFracEl.textContent = fracs[k] + ' masked';
      if (mMaeEl)  mMaeEl.textContent  = 'nMSE ' + fmt(maeMR[k]);
      if (mFmEl)   mFmEl.textContent   = 'nMSE ' + fmt(fmMR[k]);
      if (mReadout) mReadout.innerHTML =
        'Mask fraction <b>' + fracs[k] + '</b> &nbsp;·&nbsp; nMSE &nbsp; L-MAE <b>' + fmt(maeMR[k]) +
        '</b> &nbsp;vs&nbsp; L-FM-Physics <b>' + fmt(fmMR[k]) + '</b>';
      mSlider.value = k;
      mSlider.style.setProperty('--fill', (k / 2 * 100) + '%');
    }

    var mk = 0, MR_DWELL = 2200;
    var mWantPlay = false, mActive = false, mTimer = null;   // starts paused
    var mBtn = document.getElementById('mr-play');

    function mRunning() { return mWantPlay && mActive; }
    function mSyncBtn() { if (mBtn) mBtn.innerHTML = mRunning() ? ICON_PAUSE : ICON_PLAY; }
    function mTick() {
      mTimer = null;
      if (!mRunning()) return;
      mk = (mk + 1) % 3; mRender(mk);
      mTimer = setTimeout(mTick, MR_DWELL);
    }
    function mKick() { if (mRunning() && mTimer == null) mTimer = setTimeout(mTick, MR_DWELL); }
    function mStop() { if (mTimer) clearTimeout(mTimer); mTimer = null; }
    var mClaim = function () {};
    function mPlay()    { mClaim(); mWantPlay = true;  mSyncBtn(); mKick(); }
    function mPause()   { mWantPlay = false; mSyncBtn(); mStop(); }

    if (mBtn) mBtn.addEventListener('click', function () { mRunning() ? mPause() : mPlay(); });
    mSlider.addEventListener('pointerdown', mPause);
    mSlider.addEventListener('input', function () { mPause(); mk = Math.round(parseFloat(mSlider.value)); mRender(mk); });

    mRender(mk);
    var mEl = mSlider.closest('.interactive');
    mClaim = registerPlayer(mEl, function (on) { mActive = on; mSyncBtn(); on ? mKick() : mStop(); });
  }

  /* ---- interactive 4 & 5: inpaint a masked region of a real CFD field ----- *
   * Real ground-truth aneurysm velocity field (an Aneumo shape at one flow
   * speed; data baked by website/tools/export_inpaint_demo.py): a shaded
   * geometry PNG + GT velocity glyphs. A ball region of the lumen is the mask
   * (coral outline); the rest of the field + the geometry are fixed context,
   * pre-rendered once to a shared offscreen canvas. Two *independent* figures
   * (the page coordinator runs only one auto-player at a time, so they never
   * animate simultaneously):
   *   fig 4 / #fm-canvas  -- flow matching: every in-mask glyph starts at a
   *      random orientation + random magnitude and, over the integration time t,
   *      rotates into its true CFD direction while its length lerps to truth.
   *   fig 5 / #mae-canvas -- iterative masked autoencoder: the masked glyphs are
   *      revealed outside-in in `nShells` discrete steps; the mask outline stays
   *      drawn the whole time.                                                 */
  var fmCanvas = document.getElementById('fm-canvas');
  var maeCanvas = document.getElementById('mae-canvas');
  if ((fmCanvas || maeCanvas) && window.FM_INPAINT &&
      ((fmCanvas && fmCanvas.getContext) || (maeCanvas && maeCanvas.getContext))) {
    var FM = window.FM_INPAINT;
    var FW = FM.w, FH = FM.h, L0 = FM.L0, L0SQ = L0 * L0;
    var SHELLS = FM.shells || [];
    var NS = FM.nShells || (SHELLS.length || 5);
    var MASK_OUTLINE = SHELLS[0] || [];
    var ML = FM.maskLabel || null;
    var BC = (FM.fixedBC || []).filter(function (p) { return p && p.length === 2; });   // inlet/outlet anchors (px)
    var SHAFT_W = 2.2, HALO_W = 3.8, OUTLINE_W = 5.0, HEAD_MIN = 5.0;
    var CORAL = '#a52a2a', SLATE = '#2e3d4f';   // coral = the mask; slate = the fixed boundary conditions
    var LBL_FONT = Math.max(15, Math.round(0.024 * FW));   // annotation text size (px in canvas space)

    function lerpAngle(a, b, t) { var d = ((b - a) % 6.283185307 + 9.42477796) % 6.283185307 - 3.141592654; return a + d * t; }

    /* matplotlib viridis (19 stops) */
    var VIRIDIS = [[68,1,84],[71,18,101],[72,38,119],[69,55,129],[64,71,136],[57,85,140],
      [50,99,141],[44,113,142],[39,125,142],[34,138,141],[32,151,139],[33,165,133],
      [42,178,125],[64,191,112],[94,201,97],[133,209,78],[174,213,57],[216,217,36],[253,231,37]];
    function viridis(t) {
      t = t < 0 ? 0 : t > 1 ? 1 : t;
      var f = t * (VIRIDIS.length - 1), i = f | 0, g = f - i;
      var a = VIRIDIS[i], b = VIRIDIS[i + 1 < VIRIDIS.length ? i + 1 : i];
      return 'rgb(' + ((a[0]+(b[0]-a[0])*g)|0) + ',' + ((a[1]+(b[1]-a[1])*g)|0) + ',' + ((a[2]+(b[2]-a[2])*g)|0) + ')';
    }
    var NB = 22, BUCKET = [];
    for (var bi = 0; bi < NB; bi++) BUCKET.push(viridis(bi / (NB - 1)));
    function bucketOf(vx, vy) {                          // (|v|/L0)^2 == speed/vref (sqrt-scaled glyphs)
      var c = (vx*vx + vy*vy) / L0SQ; if (c > 1) c = 1;
      var k = (c * (NB - 1) + 0.5) | 0; return k < 0 ? 0 : k >= NB ? NB - 1 : k;
    }

    function arrowPath(ctx, tx, ty, vx, vy) {
      var m = Math.sqrt(vx*vx + vy*vy), hx = tx + vx, hy = ty + vy;
      ctx.moveTo(tx, ty); ctx.lineTo(hx, hy);
      if (m > HEAD_MIN) {
        var hl = m < 22 ? m * 0.36 : 7.9 + m * 0.13, ang = Math.atan2(vy, vx);
        ctx.moveTo(hx, hy); ctx.lineTo(hx - Math.cos(ang - 0.44) * hl, hy - Math.sin(ang - 0.44) * hl);
        ctx.moveTo(hx, hy); ctx.lineTo(hx - Math.cos(ang + 0.44) * hl, hy - Math.sin(ang + 0.44) * hl);
      }
    }
    /* white-halo pass + NB colour-bucket passes; `keep` (Uint8 or null) filters glyphs */
    function strokeArrows(ctx, n, px, py, vx, vy, cb, keep, haloA, shaftA, haloW, shaftW) {
      ctx.lineCap = 'round'; ctx.lineJoin = 'round';
      var i;
      ctx.beginPath();
      for (i = 0; i < n; i++) { if (keep && !keep[i]) continue; arrowPath(ctx, px[i], py[i], vx[i], vy[i]); }
      ctx.lineWidth = haloW; ctx.strokeStyle = 'rgba(255,255,255,' + haloA + ')'; ctx.stroke();
      ctx.globalAlpha = shaftA;
      for (var bk = 0; bk < NB; bk++) {
        ctx.beginPath(); var any = false;
        for (i = 0; i < n; i++) { if ((keep && !keep[i]) || cb[i] !== bk) continue; arrowPath(ctx, px[i], py[i], vx[i], vy[i]); any = true; }
        if (any) { ctx.lineWidth = shaftW; ctx.strokeStyle = BUCKET[bk]; ctx.stroke(); }
      }
      ctx.globalAlpha = 1;
    }
    function tracePoly(ctx, poly) {
      ctx.moveTo(poly[0][0], poly[0][1]);
      for (var j = 1; j < poly.length; j++) ctx.lineTo(poly[j][0], poly[j][1]);
      ctx.closePath();
    }
    function strokePoly(ctx, poly, w, color) {
      if (!poly || poly.length < 3) return;
      ctx.lineJoin = 'round'; ctx.lineCap = 'round';
      ctx.beginPath(); tracePoly(ctx, poly); ctx.lineWidth = w; ctx.strokeStyle = color; ctx.stroke();
    }
    /* plain colour-coded text labels - no leader lines / dots; positioned in the
       white space near (but not on top of) the feature they name.
       `cx`/`cy` (fractions of W/H) = wanted *centre* of the text box; clamped.   */
    function drawAnnotation(ctx, txt, color, cxFrac, cyFrac) {
      ctx.save();
      ctx.font = '700 ' + LBL_FONT + 'px ' + (getComputedStyle(document.body).fontFamily || 'sans-serif');
      ctx.fillStyle = color; var w = ctx.measureText(txt).width, m = 8;
      var x = clamp(cxFrac * FW, m + w / 2, FW - m - w / 2);
      var y = clamp(cyFrac * FH, m + LBL_FONT / 2, FH - m - LBL_FONT / 2);
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(txt, x, y);
      ctx.restore();
    }
    /* "masked region" - top-right white band (above/right of the sac);
       "fixed boundaries" - bottom-left white band (between the inlet & left stub). */
    function drawLabels(ctx) {
      if (ML) drawAnnotation(ctx, 'masked region', CORAL, 0.84, 0.045);
      if (BC.length) drawAnnotation(ctx, 'fixed boundaries', SLATE, 0.27, 0.955);
    }

    /* ---- parse the glyph payload ----------------------------------------- */
    var C = FM.context, cN = C.length;
    var cPx = new Float32Array(cN), cPy = new Float32Array(cN), cVx = new Float32Array(cN), cVy = new Float32Array(cN), cCb = new Uint8Array(cN);
    for (var ci = 0; ci < cN; ci++) { cPx[ci]=C[ci][0]; cPy[ci]=C[ci][1]; cVx[ci]=C[ci][2]; cVy[ci]=C[ci][3]; cCb[ci]=bucketOf(cVx[ci],cVy[ci]); }
    var Mk = FM.masked, mN = Mk.length;
    var mPx = new Float32Array(mN), mPy = new Float32Array(mN);
    var mGx = new Float32Array(mN), mGy = new Float32Array(mN), mCbGT = new Uint8Array(mN);            // GT vector + colour
    var mShell = new Uint8Array(mN);                                                                  // iterative-reveal shell
    var mAng1 = new Float32Array(mN), mLen1 = new Float32Array(mN);                                   // FM: GT angle / length
    var mAng0 = new Float32Array(mN), mLen0 = new Float32Array(mN), mSeed = new Float32Array(mN);     // FM: random init
    var mVx = new Float32Array(mN), mVy = new Float32Array(mN), mCb = new Uint8Array(mN), mKeep = new Uint8Array(mN);
    (function () {
      for (var i = 0; i < mN; i++) {
        var gx = Mk[i][2], gy = Mk[i][3];
        mPx[i]=Mk[i][0]; mPy[i]=Mk[i][1]; mGx[i]=gx; mGy[i]=gy; mCbGT[i]=bucketOf(gx,gy);
        mShell[i] = Mk[i].length > 4 ? Mk[i][4] : 0;
        mAng1[i] = Math.atan2(gy, gx); mLen1[i] = Math.sqrt(gx*gx + gy*gy);
        mAng0[i] = Math.random() * 6.283185307;
        mLen0[i] = L0 * Math.sqrt(Math.random());     // |v|^2/L0^2 ~ U(0,1) -> full colour spread at t=0
        mSeed[i] = Math.random() * 6.283185307;
      }
    })();

    /* ---- shared offscreen: geometry + static GT context glyphs ----------- */
    var fmReady = false;
    var fmOff = document.createElement('canvas'); fmOff.width = FW; fmOff.height = FH;
    var fmOffCtx = fmOff.getContext('2d');
    fmOffCtx.fillStyle = '#f3f4f6'; fmOffCtx.fillRect(0, 0, FW, FH);
    var fmGeom = new Image();
    fmGeom.onload = function () {
      fmOffCtx.fillStyle = '#ffffff'; fmOffCtx.fillRect(0, 0, FW, FH);
      fmOffCtx.drawImage(fmGeom, 0, 0, FW, FH);
      strokeArrows(fmOffCtx, cN, cPx, cPy, cVx, cVy, cCb, null, 0.30, 0.90, HALO_W, SHAFT_W);
      fmReady = true; if (renderFM) renderFM(fmT); if (renderMAE) renderMAE(maeStep);
      if (fmKick) fmKick(); if (maeKick) maeKick();
    };
    fmGeom.onerror = function () { /* leave the placeholder grey fill */ };
    fmGeom.src = FM.geom;

    /* a play controller for the FM / MAE columns: each starts *paused* (runs only
       after a play click) and each runs independently — they're registered as
       `independent` players, so playing one never pauses the other. `setActive`
       follows column visibility (true while at all on-screen). */
    function makeController(syncBtn, kickFn, stopFn) {
      var active = false, wantPlay = false;          // starts paused
      function running() { return active && wantPlay; }
      function sync() { syncBtn(running() ? ICON_PAUSE : ICON_PLAY); }
      function apply() { sync(); running() ? kickFn() : stopFn(); }
      return {
        running: running,
        setActive: function (on) { active = on; apply(); },
        toggle:    function ()   { wantPlay = !wantPlay; apply(); },
        pause:     function ()   { wantPlay = false; sync(); stopFn(); }
      };
    }

    var renderFM = null, renderMAE = null, fmKick = null, maeKick = null;

    /* ===================== figure 4: flow matching (continuous t) ========= */
    if (fmCanvas && fmCanvas.getContext) {
      var fmCtx = fmCanvas.getContext('2d');
      fmCanvas.width = FW; fmCanvas.height = FH;
      fmCtx.fillStyle = '#f3f4f6'; fmCtx.fillRect(0, 0, FW, FH);
      var fmSlider = document.getElementById('fm-slider'), fmReadout = document.getElementById('fm-readout');
      var fmBtn = document.getElementById('fm-play');
      var fmT = 0, fmPhase = 0;
      function fmMeta(t) {
        fmT = clamp(t, 0, 1);
        if (fmSlider) { fmSlider.value = fmT; fmSlider.style.setProperty('--fill', (fmT * 100) + '%'); }
        if (fmReadout) fmReadout.innerHTML = 'Integration time <b>t = ' + fmT.toFixed(2) + '</b>';
      }
      renderFM = function (t) {
        fmMeta(t);
        if (!fmReady) return;
        var now = performance.now(), e = fmT, inv = 1 - e, wob = inv * 0.5;   // rad of angular jiggle at t=0
        for (var i = 0; i < mN; i++) {
          var ang = lerpAngle(mAng0[i], mAng1[i], e) + wob * Math.sin(mSeed[i] + now * 0.0040);
          var len = mLen0[i] + (mLen1[i] - mLen0[i]) * e;
          mVx[i] = Math.cos(ang) * len; mVy[i] = Math.sin(ang) * len; mCb[i] = bucketOf(mVx[i], mVy[i]);
        }
        fmCtx.drawImage(fmOff, 0, 0);
        strokeArrows(fmCtx, mN, mPx, mPy, mVx, mVy, mCb, null, 0.35, 0.93, HALO_W, SHAFT_W);
        strokePoly(fmCtx, MASK_OUTLINE, OUTLINE_W, CORAL);
        drawLabels(fmCtx);
      };

      var fmRaf = null, fmLast = null, fmHold = 0, FM_UP = 5.0, FM_HOLD = 1.6, FM_DOWN = 0.9;
      function fmLoop(ts) {
        if (!fmC.running()) { fmRaf = null; fmLast = null; return; }
        if (fmLast == null) fmLast = ts;
        var dt = (ts - fmLast) / 1000; fmLast = ts;
        if (!fmReady) { fmRaf = requestAnimationFrame(fmLoop); return; }
        if (fmPhase === 0)      { fmT += dt / FM_UP;   if (fmT >= 1) { fmT = 1; fmPhase = 1; fmHold = 0; } }
        else if (fmPhase === 1) { fmHold += dt;        if (fmHold >= FM_HOLD) fmPhase = 2; }
        else                    { fmT -= dt / FM_DOWN; if (fmT <= 0) { fmT = 0; fmPhase = 0; } }
        renderFM(fmT);
        fmRaf = requestAnimationFrame(fmLoop);
      }
      fmKick = function () { if (fmC.running() && fmRaf == null) { fmLast = null; fmRaf = requestAnimationFrame(fmLoop); } };
      function fmStop() { if (fmRaf) cancelAnimationFrame(fmRaf); fmRaf = null; fmLast = null; }
      var fmC = makeController(function (ic) { if (fmBtn) fmBtn.innerHTML = ic; }, fmKick, fmStop);
      if (fmBtn) fmBtn.addEventListener('click', fmC.toggle);
      if (fmSlider) {
        fmSlider.addEventListener('pointerdown', fmC.pause);
        fmSlider.addEventListener('input', function () { fmC.pause(); fmPhase = 0; renderFM(parseFloat(fmSlider.value)); });
      }
      fmMeta(0);
      // independent player observing this column (the two methods share one card
      // but are meant to run in parallel, so they don't compete for the slot)
      registerPlayer(fmCanvas.closest('.fm-col') || fmCanvas.closest('.interactive'), fmC.setActive, true);
    }

    /* ===================== figure 5: iterative MAE (NS discrete steps) ==== */
    if (maeCanvas && maeCanvas.getContext) {
      var maeCtx = maeCanvas.getContext('2d');
      maeCanvas.width = FW; maeCanvas.height = FH;
      maeCtx.fillStyle = '#f3f4f6'; maeCtx.fillRect(0, 0, FW, FH);
      var maeSlider = document.getElementById('mae-slider'), maeReadout = document.getElementById('mae-readout');
      var maeBtn = document.getElementById('mae-play');
      var maeStep = 0;
      function maeMeta(k) {
        maeStep = clamp(Math.round(k), 0, NS);
        if (maeSlider) { maeSlider.value = maeStep; maeSlider.style.setProperty('--fill', (maeStep / NS * 100) + '%'); }
        if (maeReadout) maeReadout.innerHTML = 'Step <b>' + maeStep + ' of ' + NS + '</b>';
      }
      renderMAE = function (k) {
        maeMeta(k);
        if (!fmReady) return;
        var step = maeStep, i;
        for (i = 0; i < mN; i++) mKeep[i] = mShell[i] < step ? 1 : 0;          // shells revealed so far
        maeCtx.drawImage(fmOff, 0, 0);
        strokeArrows(maeCtx, mN, mPx, mPy, mGx, mGy, mCbGT, mKeep, 0.35, 0.93, HALO_W, SHAFT_W);
        strokePoly(maeCtx, MASK_OUTLINE, OUTLINE_W, CORAL);              // the mask outline stays for all steps
        drawLabels(maeCtx);
      };

      var maeTimer = null, MAE_DWELL = 900, MAE_HOLD = 1600;
      function maeTick() {
        maeTimer = null;
        if (!maeC.running()) return;
        maeStep = maeStep >= NS ? 0 : maeStep + 1;
        renderMAE(maeStep);
        maeTimer = setTimeout(maeTick, maeStep >= NS ? MAE_HOLD : MAE_DWELL);
      }
      maeKick = function () { if (maeC.running() && maeTimer == null && fmReady) maeTimer = setTimeout(maeTick, MAE_DWELL); };
      function maeStop() { if (maeTimer) clearTimeout(maeTimer); maeTimer = null; }
      var maeC = makeController(function (ic) { if (maeBtn) maeBtn.innerHTML = ic; }, maeKick, maeStop);
      if (maeBtn) maeBtn.addEventListener('click', maeC.toggle);
      if (maeSlider) {
        maeSlider.addEventListener('pointerdown', maeC.pause);
        maeSlider.addEventListener('input', function () { maeC.pause(); maeStep = clamp(Math.round(parseFloat(maeSlider.value)), 0, NS); renderMAE(maeStep); });
      }
      maeMeta(0);
      registerPlayer(maeCanvas.closest('.fm-col') || maeCanvas.closest('.interactive'), maeC.setActive, true);
    }
  }

  /* ---- "Local geometry editing": curtain comparison of two states ------- *
   * N different arteries; per artery two panels (geometry | velocity), and one
   * draggable curtain (like the before/after wipe lower on the page) that wipes
   * all 2N panels at the same x-fraction:
   *   left of the curtain  = state 1: the original vessel + its GT CFD field
   *   right of the curtain = state 2: the same vessel with an aneurysm grown
   *                          locally + the inpainting prediction of the field
   * Data baked by website/tools/render_deform_mae.py (one GPU run; the results
   * — static/data/fm_deform.{json,js} + the geometry PNGs — are stored, so this
   * never re-runs at page load; re-run only if the pairs/checkpoints change).
   * The geometry panel wipes two stacked <img> (clip-path on the state-2 one);
   * the velocity panel pre-renders each state to an offscreen canvas once, then
   * composites base + clipped-overlay on every drag (cheap → smooth).           */
  (function () {
    var grid = document.getElementById('dfm-grid');
    if (!grid) return;
    if (!window.FM_DEFORM || !Array.isArray(window.FM_DEFORM.pairs) || !window.FM_DEFORM.pairs.length) {
      var fb = document.getElementById('dfm-fallback'); if (fb) fb.hidden = false;
      var dfmInt = grid.closest('.interactive'), dfmCtl = dfmInt && dfmInt.querySelector('.dfm-curtain-cap, .dfm-toggle-row, .slider-row');
      if (dfmCtl) dfmCtl.style.display = 'none';
      return;
    }
    var PAIRS = window.FM_DEFORM.pairs;
    var SHAFT_W = 1.7, HEAD_MIN = 5.0;

    /* matplotlib viridis (same 19 stops as the inpainting figure) */
    var VIRIDIS = [[68,1,84],[71,18,101],[72,38,119],[69,55,129],[64,71,136],[57,85,140],
      [50,99,141],[44,113,142],[39,125,142],[34,138,141],[32,151,139],[33,165,133],
      [42,178,125],[64,191,112],[94,201,97],[133,209,78],[174,213,57],[216,217,36],[253,231,37]];
    function viridis(t) {
      t = t < 0 ? 0 : t > 1 ? 1 : t;
      var f = t * (VIRIDIS.length - 1), i = f | 0, g = f - i;
      var a = VIRIDIS[i], b = VIRIDIS[i + 1 < VIRIDIS.length ? i + 1 : i];
      return 'rgb(' + ((a[0]+(b[0]-a[0])*g)|0) + ',' + ((a[1]+(b[1]-a[1])*g)|0) + ',' + ((a[2]+(b[2]-a[2])*g)|0) + ')';
    }
    var NB = 22, BUCKET = [];
    for (var bi = 0; bi < NB; bi++) BUCKET.push(viridis(bi / (NB - 1)));

    function arrowPath(ctx, tx, ty, vx, vy) {
      var m = Math.sqrt(vx*vx + vy*vy), hx = tx + vx, hy = ty + vy;
      ctx.moveTo(tx, ty); ctx.lineTo(hx, hy);
      if (m > HEAD_MIN) {
        var hl = m < 22 ? m * 0.36 : 7.9 + m * 0.13, ang = Math.atan2(vy, vx);
        ctx.moveTo(hx, hy); ctx.lineTo(hx - Math.cos(ang - 0.44) * hl, hy - Math.sin(ang - 0.44) * hl);
        ctx.moveTo(hx, hy); ctx.lineTo(hx - Math.cos(ang + 0.44) * hl, hy - Math.sin(ang + 0.44) * hl);
      }
    }
    function strokeGlyphs(ctx, g) {
      if (!g || !g.n) return;
      ctx.lineCap = 'round'; ctx.lineJoin = 'round';
      var i, n = g.n;
      ctx.beginPath();
      for (i = 0; i < n; i++) arrowPath(ctx, g.px[i], g.py[i], g.vx[i], g.vy[i]);
      ctx.globalAlpha = 0.30; ctx.lineWidth = SHAFT_W + 1.6; ctx.strokeStyle = '#fff'; ctx.stroke();
      ctx.globalAlpha = 1;
      for (var bk = 0; bk < NB; bk++) {
        ctx.beginPath(); var any = false;
        for (i = 0; i < n; i++) { if (g.cb[i] !== bk) continue; arrowPath(ctx, g.px[i], g.py[i], g.vx[i], g.vy[i]); any = true; }
        if (any) { ctx.lineWidth = SHAFT_W; ctx.strokeStyle = BUCKET[bk]; ctx.stroke(); }
      }
    }

    var FRAC = 0.5;                                         // shared curtain position [0..1]

    function parseG(rows, L0SQ) {
      rows = rows || [];
      var n = rows.length, px = new Float32Array(n), py = new Float32Array(n),
          vx = new Float32Array(n), vy = new Float32Array(n), cb = new Uint8Array(n);
      for (var i = 0; i < n; i++) {
        px[i] = rows[i][0]; py[i] = rows[i][1]; vx[i] = rows[i][2]; vy[i] = rows[i][3];
        var c = (vx[i]*vx[i] + vy[i]*vy[i]) / L0SQ; if (c > 1) c = 1;
        var k = (c * (NB - 1) + 0.5) | 0; cb[i] = k < 0 ? 0 : k >= NB ? NB - 1 : k;
      }
      return { n: n, px: px, py: py, vx: vx, vy: vy, cb: cb };
    }
    function offscreen(w, h) { var c = document.createElement('canvas'); c.width = w; c.height = h; return c; }
    function bakeState(w, h, mesh, glyphs) {                // white + mesh PNG + glyphs -> offscreen canvas
      var oc = offscreen(w, h), x = oc.getContext('2d');
      x.fillStyle = '#fff'; x.fillRect(0, 0, w, h);
      if (mesh) x.drawImage(mesh, 0, 0, w, h);
      strokeGlyphs(x, glyphs);
      return oc;
    }
    function frameHandle() {
      var hd = document.createElement('div'); hd.className = 'dfm-handle'; hd.setAttribute('aria-hidden', 'true');
      return hd;
    }

    /* ---- build DOM + parse each pair ----------------------------------- */
    var arts = PAIRS.map(function (p, idx) {
      var L0SQ = (p.L0 || 36) * (p.L0 || 36);
      var gA = parseG(p.glyphsA, L0SQ), gB = parseG(p.glyphsB, L0SQ);

      var wrap = document.createElement('div'); wrap.className = 'dfm-artery';
      var lbl = document.createElement('div'); lbl.className = 'lbl'; lbl.textContent = 'Artery ' + (idx + 1);
      var row = document.createElement('div'); row.className = 'dfm-row';

      // geometry panel: state-1 PNG as the base, state-2 PNG clipped on top
      var gcell = document.createElement('div'); gcell.className = 'dfm-cell';
      var gcap = document.createElement('div'); gcap.className = 'cap'; gcap.textContent = 'geometry';
      var gframe = document.createElement('div'); gframe.className = 'dfm-frame'; gframe.style.aspectRatio = p.w + ' / ' + p.h;
      var imgA = new Image(), imgB = new Image();
      imgA.alt = 'artery ' + (idx + 1) + ', original geometry, edited region in red';
      imgB.alt = 'artery ' + (idx + 1) + ', deformed geometry, grown region in red';
      imgA.decoding = imgB.decoding = 'async'; imgA.loading = imgB.loading = 'lazy';
      imgA.className = 'dfm-img'; imgB.className = 'dfm-img dfm-img-top';
      var gHandle = frameHandle();
      gframe.appendChild(imgA); gframe.appendChild(imgB); gframe.appendChild(gHandle);
      gcell.appendChild(gcap); gcell.appendChild(gframe); row.appendChild(gcell);
      imgA.src = p.geomMaskA; imgB.src = p.geomMaskB;

      // velocity panel: a canvas (composites the two pre-baked states each drag)
      var fcell = document.createElement('div'); fcell.className = 'dfm-cell';
      var fcap = document.createElement('div'); fcap.className = 'cap';
      fcap.innerHTML = 'velocity &nbsp;<span class="dfm-side l">GT</span>&nbsp;|&nbsp;<span class="dfm-side r">prediction</span>';
      var fframe = document.createElement('div'); fframe.className = 'dfm-frame'; fframe.style.aspectRatio = p.w + ' / ' + p.h;
      var cv = document.createElement('canvas'); cv.width = p.w; cv.height = p.h;
      cv.setAttribute('aria-label', 'artery ' + (idx + 1) + ' velocity field: ground-truth CFD on the original geometry (left of the divider) vs. the inpainting prediction on the locally deformed geometry (right of the divider)');
      var fHandle = frameHandle();
      fframe.appendChild(cv); fframe.appendChild(fHandle); fcell.appendChild(fcap); fcell.appendChild(fframe); row.appendChild(fcell);

      wrap.appendChild(lbl); wrap.appendChild(row); grid.appendChild(wrap);

      var meshA = new Image(), meshB = new Image();
      var st = { p: p, gA: gA, gB: gB, imgB: imgB, gHandle: gHandle, fHandle: fHandle,
                 ctx: cv.getContext('2d'), meshA: meshA, meshB: meshB,
                 offA: null, offB: null, readyA: false, readyB: false };
      function bakeIfReady() {
        if (st.readyA && st.readyB && !st.offA) {
          st.offA = bakeState(p.w, p.h, st.meshA, st.gA);
          st.offB = bakeState(p.w, p.h, st.meshB, st.gB);
          drawOne(st);
        }
      }
      meshA.onload = function () { st.readyA = true; bakeIfReady(); };
      meshB.onload = function () { st.readyB = true; bakeIfReady(); };
      meshA.onerror = meshB.onerror = function () {};
      meshA.src = p.geomA; meshB.src = p.geomB;
      // anywhere on a frame drags the curtain
      [gframe, fframe].forEach(function (fr) {
        fr.addEventListener('pointerdown', function (e) {
          e.preventDefault(); fr.setPointerCapture(e.pointerId); dragFrom(fr, e.clientX);
        });
        fr.addEventListener('pointermove', function (e) { if (fr.hasPointerCapture(e.pointerId)) dragFrom(fr, e.clientX); });
        var rel = function (e) { if (fr.hasPointerCapture && fr.hasPointerCapture(e.pointerId)) fr.releasePointerCapture(e.pointerId); };
        fr.addEventListener('pointerup', rel); fr.addEventListener('pointercancel', rel);
      });
      return st;
    });

    function drawOne(st) {
      var p = st.p, w = p.w, h = p.h, x = Math.round(FRAC * w);
      st.gHandle.style.left = (FRAC * 100) + '%';
      st.fHandle.style.left = (FRAC * 100) + '%';
      if (st.imgB) st.imgB.style.clipPath = 'inset(0 0 0 ' + (FRAC * 100) + '%)';   // state 2 shows only right of the curtain
      var f = st.ctx;
      if (!st.offA) { f.clearRect(0, 0, w, h); f.fillStyle = '#fff'; f.fillRect(0, 0, w, h); return; }
      f.drawImage(st.offA, 0, 0);                            // base = state 1 (GT)
      if (x < w) { f.save(); f.beginPath(); f.rect(x, 0, w - x, h); f.clip(); f.drawImage(st.offB, 0, 0); f.restore(); }
    }
    function drawAll() { for (var i = 0; i < arts.length; i++) drawOne(arts[i]); }

    /* ---- shared curtain drag ------------------------------------------- */
    var raf = null;
    function setFrac(v) {
      v = clamp(v, 0, 1);
      if (v === FRAC) return;
      FRAC = v;
      if (raf == null) raf = requestAnimationFrame(function () { raf = null; drawAll(); });
    }
    function dragFrom(frameEl, clientX) {
      var r = frameEl.getBoundingClientRect();
      setFrac((clientX - r.left) / r.width);
    }
    drawAll();
  })();

  /* ---- scroll-through animation mode (deformation figure only) --------- *
   * The "Scroll-through animation" toggle pins the deformation figure and
   * scrubs its frames with the page scroll, then releases it. Only figures
   * that called registerScrolly() participate - currently just the
   * deformation figure (interactive figure 3, in the #interactive-more
   * section near the bottom of the page).                                   */
  (function () {
    var btn = document.getElementById('scrollmode-toggle');
    if (!btn) return;
    if (!scrollyPlayers.length) { btn.style.display = 'none'; return; }

    var KEY = 'ffm-scroll-through';
    var on = false, raf = null;

    function setBtn() {
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
      var b = btn.querySelector('b'); if (b) b.textContent = on ? 'on' : 'off';
    }

    /* keep whatever's near the top of the viewport visually stable across the
       layout change (the wrappers we add/remove change the page height). */
    function pickAnchor() {
      var el = document.elementFromPoint(Math.round(window.innerWidth / 2), 12);
      while (el && scrollyPlayers.some(function (p) { return p.el.contains(el); })) el = el.parentElement;
      return el || document.getElementById('interactive');
    }
    function withAnchor(mutate) {
      var html = document.documentElement;
      var prevBehavior = html.style.scrollBehavior;
      html.style.scrollBehavior = 'auto';
      var anchor = pickAnchor();
      var before = anchor ? anchor.getBoundingClientRect().top : null;
      mutate();
      if (anchor && before != null) window.scrollBy(0, anchor.getBoundingClientRect().top - before);
      html.style.scrollBehavior = prevBehavior;
    }

    function layout() {
      var vh = window.innerHeight;
      scrollyPlayers.forEach(function (p) {
        if (!p.scrolly) return;
        p.el.style.position = ''; p.el.style.top = ''; p.scrolly.style.height = '';
        var h = p.el.offsetHeight;
        if (h > vh - 64) {                 // too tall to pin -> scroll-linked, no pinning
          p.mode = 'flow'; p.h = h;
        } else {
          var scrub = Math.round(vh * 0.6 * Math.max(1, p.steps - 1));
          var top = Math.round((vh - h) / 2);
          p.scrolly.style.height = (h + scrub) + 'px';
          p.el.style.position = 'sticky';
          p.el.style.top = top + 'px';
          p.mode = 'pin'; p.h = h; p.scrub = scrub; p.top = top;
        }
      });
      tick();
    }

    function rawProgress(p) {
      if (p.mode === 'pin') {
        var r = p.scrolly.getBoundingClientRect();
        return (p.top - r.top) / p.scrub;            // 0..1 over the pinned range
      }
      var rr = p.el.getBoundingClientRect();          // flow fallback: enter at bottom, leave at top
      var vh = window.innerHeight;
      return (vh - rr.top) / (vh + p.h);
    }

    function tick() {
      if (!on) return;
      var pinned = false;
      scrollyPlayers.forEach(function (p) {
        if (!p.scrolly) return;
        var raw = rawProgress(p);
        p.render(p.vmin + clamp(raw, 0, 1) * (p.vmax - p.vmin));
        if (p.mode === 'pin' && raw > 0 && raw < 1) pinned = true;
      });
      document.body.classList.toggle('scroll-pinned', pinned);
    }
    function onScroll() { if (raf == null) raf = requestAnimationFrame(function () { raf = null; tick(); }); }

    function enable() {
      if (on) return; on = true; setBtn();
      document.body.classList.add('scroll-mode');
      withAnchor(function () {
        scrollyPlayers.forEach(function (p) {
          p.setAuto(false);
          var w = document.createElement('div');
          w.className = 'scrolly';
          p.el.parentNode.insertBefore(w, p.el);
          w.appendChild(p.el);
          p.scrolly = w;
        });
        layout();
      });
      window.addEventListener('scroll', onScroll, { passive: true });
      window.addEventListener('resize', layout);
      try { localStorage.setItem(KEY, '1'); } catch (e) {}
    }

    function disable() {
      if (!on) return; on = false; setBtn();
      window.removeEventListener('scroll', onScroll);
      window.removeEventListener('resize', layout);
      document.body.classList.remove('scroll-mode', 'scroll-pinned');
      withAnchor(function () {
        scrollyPlayers.forEach(function (p) {
          if (p.scrolly) {
            p.scrolly.parentNode.insertBefore(p.el, p.scrolly);
            p.scrolly.remove(); p.scrolly = null;
          }
          p.el.style.position = ''; p.el.style.top = '';
          p.setAuto(true);
        });
      });
      try { localStorage.removeItem(KEY); } catch (e) {}
    }

    btn.addEventListener('click', function () { on ? disable() : enable(); });
    setBtn();

    var saved = false;
    try { saved = localStorage.getItem(KEY) === '1'; } catch (e) {}
    if (saved) enable();

    window.addEventListener('load', function () { if (on) layout(); });
    setTimeout(function () { if (on) layout(); }, 600);   // re-measure after fonts settle
  })();

  /* ---- before/after wipe ---------------------------------------------- */
  document.querySelectorAll('.wipe').forEach(function (w) {
    var topEl = w.querySelector('.top');
    var handle = w.querySelector('.handle');
    var dragging = false;
    function setPos(clientX) {
      var r = w.getBoundingClientRect();
      var pct = (clamp((clientX - r.left) / r.width, 0, 1) * 100).toFixed(2);
      topEl.style.clipPath = 'inset(0 0 0 ' + pct + '%)';
      handle.style.left = pct + '%';
    }
    w.addEventListener('pointerdown', function (e) { dragging = true; w.setPointerCapture(e.pointerId); setPos(e.clientX); });
    w.addEventListener('pointermove', function (e) { if (dragging) setPos(e.clientX); });
    w.addEventListener('pointerup', function () { dragging = false; });
    w.addEventListener('pointercancel', function () { dragging = false; });
    topEl.style.clipPath = 'inset(0 0 0 50%)';
    handle.style.left = '50%';
  });

  /* ---- splash: fade the velocity field in over the geometry on scroll -- */
  (function () {
    var splash = document.querySelector('.splash');
    var flow = document.querySelector('.splash-flow');
    if (!splash || !flow) return;
    // flow opacity ramps 0 -> 1 over this slice of the splash's scroll-scrub range
    var FADE_START = 0.05, FADE_END = 0.55;
    function update() {
      var rect = splash.getBoundingClientRect();
      var range = splash.offsetHeight - window.innerHeight;   // px of pinned scrolling
      var p = range > 0 ? clamp(-rect.top / range, 0, 1) : 1;
      var t = clamp((p - FADE_START) / (FADE_END - FADE_START), 0, 1);
      t = t * t * (3 - 2 * t);                                 // smoothstep
      flow.style.opacity = t.toFixed(3);
    }
    window.addEventListener('scroll', update, { passive: true });
    window.addEventListener('resize', update);
    update();
  })();

  /* ---- topnav: dark while it floats over the black splash, light after --- */
  (function () {
    var nav = document.querySelector('.topnav');
    var splash = document.querySelector('.splash');
    if (!nav || !splash) return;
    function update() {
      // dark as long as the black splash still reaches past the bar's bottom edge
      var overSplash = splash.getBoundingClientRect().bottom > nav.offsetHeight - 1;
      nav.classList.toggle('on-dark', overSplash);
    }
    window.addEventListener('scroll', update, { passive: true });
    window.addEventListener('resize', update);
    update();
  })();

});
