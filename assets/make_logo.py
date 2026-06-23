#!/usr/bin/env python3
"""
$Vault logo generator.

Emits the brand mark as scalable SVG (the master asset), then rasterizes to PNG
via macOS Quick Look (qlmanage) — no third-party image libs required.

Produces:
  assets/vault-logo.svg   full mark  (gear + VAULT + 3 chevrons)
  assets/vault-mark.svg   compact mark (gear + 3 chevrons only — for the header / favicon)
  assets/vault-logo.png   1024px raster of the full mark
  assets/vault-mark.png   256px raster of the compact mark

Run:  python3 assets/make_logo.py
"""
import math
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Phosphor green — matches the dashboard --g so the brand is cohesive.
GREEN = "#00ff41"
DIM   = "#00aa2b"
BLACK = "#000d02"

CX = CY = 100.0


def polar(r, deg):
    """deg measured clockwise from 'up' (top = 0, right = 90) for intuitive layout."""
    rad = math.radians(deg - 90.0)
    return CX + r * math.cos(rad), CY + r * math.sin(rad)


def fmt(x):
    return f"{x:.2f}"


def gear_teeth(n=12, r_base=80.0, r_tip=94.0, half_base=8.5, half_tip=5.5):
    out = []
    for k in range(n):
        c = 360.0 * k / n
        pts = [
            polar(r_base, c - half_base),
            polar(r_tip,  c - half_tip),
            polar(r_tip,  c + half_tip),
            polar(r_base, c + half_base),
        ]
        p = " ".join(f"{fmt(x)},{fmt(y)}" for x, y in pts)
        out.append(f'<polygon points="{p}" fill="{GREEN}"/>')
    return "\n  ".join(out)


def screws(n=8, r=76.5, start=0.0):
    out = []
    for k in range(n):
        c = start + 360.0 * k / n
        x, y = polar(r, c)
        # slot oriented along the spoke
        sx1, sy1 = x - 2.4 * math.cos(math.radians(c)), y - 2.4 * math.sin(math.radians(c))
        sx2, sy2 = x + 2.4 * math.cos(math.radians(c)), y + 2.4 * math.sin(math.radians(c))
        out.append(
            f'<circle cx="{fmt(x)}" cy="{fmt(y)}" r="3.6" fill="{BLACK}" stroke="{GREEN}" stroke-width="0.9"/>'
            f'<line x1="{fmt(sx1)}" y1="{fmt(sy1)}" x2="{fmt(sx2)}" y2="{fmt(sy2)}" '
            f'stroke="{GREEN}" stroke-width="1.1" stroke-linecap="round"/>'
        )
    return "\n  ".join(out)


def gauge_ticks(r0=66.0, r1=70.5, span=210.0, count=36):
    """Speedometer-style ticks arcing across the top; the bottom stays clear."""
    out = []
    half = span / 2.0
    for k in range(count + 1):
        c = -half + span * k / count            # centered on top (0 deg)
        x0, y0 = polar(r0, c)
        x1, y1 = polar(r1, c)
        # ticks past the lower flanks fade out (the dim arc in the source art)
        dim = abs(c) > 130.0
        col = DIM if dim else GREEN
        w = 1.0 if dim else 1.4
        out.append(
            f'<line x1="{fmt(x0)}" y1="{fmt(y0)}" x2="{fmt(x1)}" y2="{fmt(y1)}" '
            f'stroke="{col}" stroke-width="{w}"/>'
        )
    return "\n  ".join(out)


def chevrons(cys=(86.0, 104.0, 122.0), x_l=56.0, x_r=141.0, notch=12.0, h=8.0):
    out = []
    for cy in cys:
        tip_x = x_r
        body_r = x_r - notch
        pts = [
            (x_l, cy - h),
            (body_r, cy - h),
            (tip_x, cy),
            (body_r, cy + h),
            (x_l, cy + h),
            (x_l + notch, cy),
        ]
        p = " ".join(f"{fmt(x)},{fmt(y)}" for x, y in pts)
        # screw dots + a slot line for the riveted-plate look
        d1x, d2x = x_l + notch + 8, x_r - notch - 6
        out.append(
            f'<polygon points="{p}" fill="{GREEN}"/>'
            f'<line x1="{fmt(x_l+notch+4)}" y1="{fmt(cy)}" x2="{fmt(x_r-notch-2)}" y2="{fmt(cy)}" '
            f'stroke="{BLACK}" stroke-width="1.6"/>'
            f'<circle cx="{fmt(d1x)}" cy="{fmt(cy)}" r="1.5" fill="{BLACK}"/>'
            f'<circle cx="{fmt(d2x)}" cy="{fmt(cy)}" r="1.5" fill="{BLACK}"/>'
        )
    return "\n  ".join(out)


def gear_body():
    return (
        f'<circle cx="{CX}" cy="{CY}" r="82" fill="{GREEN}"/>\n  '
        f'<circle cx="{CX}" cy="{CY}" r="72" fill="{BLACK}"/>\n  '
        f'<circle cx="{CX}" cy="{CY}" r="64" fill="none" stroke="{GREEN}" stroke-width="1.4"/>'
    )


def svg(full=True):
    # Full opaque square — NO rounded corners. macOS Quick Look composites any
    # transparent pixel onto WHITE, so rounded/transparent corners bake into white
    # squares in the PNG. A solid black square has no transparent pixels and stays
    # invisible against the dark dashboard / boot screen.
    bg = f'<rect width="200" height="200" fill="#000600"/>'
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200" '
        f'width="200" height="200" role="img" aria-label="$Vault">',
        bg,
        gear_teeth(),
        gear_body(),
        screws(),
        gauge_ticks(),
        chevrons(),
    ]
    if full:
        parts += [
            f'<text x="100" y="29" text-anchor="middle" fill="{GREEN}" '
            f'font-family="Impact,\'Arial Narrow\',sans-serif" font-size="7" '
            f'letter-spacing="1.5">PWR</text>',
            f'<text x="100" y="66" text-anchor="middle" fill="{GREEN}" '
            f'font-family="Impact,\'Arial Black\',sans-serif" font-size="27" '
            f'letter-spacing="1">VAULT</text>',
        ]
    parts.append("</svg>")
    return "\n  ".join(parts)


def rasterize(svg_path, png_path, size):
    """SVG -> PNG via macOS Quick Look. Falls back silently if qlmanage is absent.

    Quick Look fits the SVG's intrinsic width/height into the -s box top-left, so we
    write a temp copy whose width/height equal the target px (viewBox unchanged) to
    guarantee a full-canvas, edge-to-edge raster.
    """
    if not shutil.which("qlmanage"):
        print(f"  [skip] qlmanage not found — {png_path} not rasterized")
        return False
    with open(svg_path) as fh:
        src = fh.read()
    src = src.replace('width="200" height="200"', f'width="{size}" height="{size}"', 1)
    tmp_svg = svg_path + ".tmp.svg"
    with open(tmp_svg, "w") as fh:
        fh.write(src)
    tmp_png = tmp_svg + ".png"
    subprocess.run(
        ["qlmanage", "-t", "-s", str(size), "-o", HERE, tmp_svg],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    os.remove(tmp_svg)
    if os.path.exists(tmp_png):
        os.replace(tmp_png, png_path)
        return True
    print(f"  [warn] qlmanage produced no output for {svg_path}")
    return False


def write(path, content):
    with open(path, "w") as fh:
        fh.write(content)
    print("  wrote", os.path.relpath(path))


def main():
    full_svg = os.path.join(HERE, "vault-logo.svg")
    mark_svg = os.path.join(HERE, "vault-mark.svg")
    write(full_svg, svg(full=True))
    write(mark_svg, svg(full=False))
    rasterize(full_svg, os.path.join(HERE, "vault-logo.png"), 1024)
    rasterize(mark_svg, os.path.join(HERE, "vault-mark.png"), 256)
    print("done.")


if __name__ == "__main__":
    sys.exit(main())
