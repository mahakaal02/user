// Tiny dependency-free SVG line/bar helpers — same math as the built-in
// dashboard, so no charting library is needed.
export function linePath(vals, w, h, lo, hi, pad = 4) {
  if (!vals || vals.length < 2) return "";
  const span = Math.max(hi - lo, 1e-9);
  const n = vals.length;
  return vals
    .map((v, i) => {
      const x = (i / (n - 1)) * (w - 2 * pad) + pad;
      const y = h - pad - ((v - lo) / span) * (h - 2 * pad);
      return (i ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1);
    })
    .join(" ");
}

export function bars(vals, w, h, hi) {
  if (!vals || !vals.length) return [];
  const n = vals.length;
  const bw = w / n;
  return vals.map((v, i) => ({
    x: i * bw,
    y: h - Math.min(h, (v / Math.max(hi, 1e-9)) * h),
    w: bw * 0.8,
    h: Math.min(h, (v / Math.max(hi, 1e-9)) * h),
  }));
}
