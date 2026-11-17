#!/usr/bin/env python3
"""Basic, deterministic checks for one SVG output file.

Usage: python3 validate_output.py <file.svg>
Prints a single JSON object on stdout. Never fails hard: every check degrades
to a recorded value so the runner can always write a sidecar.
"""
import hashlib
import json
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

SVG_NS = '{http://www.w3.org/2000/svg}'
ANIM_TAGS = {'animate', 'animateTransform', 'animateMotion', 'set'}
SHAPE_TAGS = {'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon',
              'path', 'text', 'g', 'use', 'image', 'foreignObject'}
EXTERNAL_RE = re.compile(
    r'''(?:xlink:href|href)\s*=\s*["']https?://(?!www\.w3\.org/2000)[^"']*["']|'''
    r'''url\(\s*["']?https?://(?!www\.w3\.org/2000)[^"')]*["']?\s*\)|'''
    r'''@import''', re.I | re.X)


def local(tag):
    return tag.rsplit('}', 1)[-1] if '}' in tag else tag


def main():
    path = sys.argv[1]
    out = {
        'file': path,
        'exists': False,
        'size_bytes': 0,
        'sha256': None,
        'well_formed_xml': False,
        'xml_error': None,
        'root_is_svg': False,
        'has_xmlns': False,
        'has_viewbox_or_size': False,
        'viewBox': None,
        'elements_total': 0,
        'element_counts': {},
        'gradient_defs': 0,
        'animation_elements': 0,
        'keyframes_blocks': 0,
        'script_blocks': 0,
        'external_refs': 0,
        'rendered': None,   # True/False/None (None = no rasterizer available)
        'render_tool': None,
        'render_error': None,
    }
    try:
        raw = open(path, 'rb').read()
    except OSError as e:
        out['xml_error'] = str(e)
        print(json.dumps(out, indent=2))
        return
    out['exists'] = True
    out['size_bytes'] = len(raw)
    out['sha256'] = hashlib.sha256(raw).hexdigest()
    text = raw.decode('utf-8', errors='replace')

    out['keyframes_blocks'] = len(re.findall(r'@keyframes', text, re.I))
    out['script_blocks'] = len(re.findall(r'<script\b', text, re.I))
    out['external_refs'] = len(EXTERNAL_RE.findall(text))

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        out['xml_error'] = str(e)
        print(json.dumps(out, indent=2))
        return
    out['well_formed_xml'] = True
    out['root_is_svg'] = local(root.tag) == 'svg'
    out['has_xmlns'] = (root.tag or '').startswith(SVG_NS) or root.get('xmlns') == 'http://www.w3.org/2000/svg'
    out['viewBox'] = root.get('viewBox')
    out['has_viewbox_or_size'] = bool(root.get('viewBox') or (root.get('width') and root.get('height')))

    counts = {}
    grads = 0
    anims = 0
    total = 0
    for el in root.iter():
        name = local(el.tag)
        if not name or name == '{':
            continue
        total += 1
        counts[name] = counts.get(name, 0) + 1
        if name in ANIM_TAGS:
            anims += 1
        if name in ('linearGradient', 'radialGradient', 'pattern'):
            grads += 1
    out['elements_total'] = total
    out['element_counts'] = dict(sorted(counts.items()))
    out['gradient_defs'] = grads
    out['animation_elements'] = anims + out['keyframes_blocks']

    # ── canvas-level transform animations ──────────────────────────────────
    # A transform animation attached to the root <svg> (instead of to the
    # group it was meant for) moves the ENTIRE scene. The canvas then slides
    # out of the viewBox and the artwork renders blank or cropped, even
    # though the file is perfectly valid XML.
    out['root_transform_animation'] = False
    out['canvas_offcanvas'] = False
    out['root_transform_note'] = None
    vb = out.get('viewBox')
    vb_w = vb_h = None
    if vb:
        parts = re.split(r'[,\s]+', vb.strip())
        if len(parts) == 4:
            try:
                vb_w = abs(float(parts[2]))
                vb_h = abs(float(parts[3]))
            except ValueError:
                vb_w = vb_h = None
    for el in list(root):
        name = local(el.tag)
        if name not in ANIM_TAGS:
            continue
        attr = el.get('attributeName')
        if attr not in ('transform', 'x', 'y'):
            continue
        out['root_transform_animation'] = True
        note = f'root <svg> has a {name} on "{attr}"'
        if attr == 'transform' and (el.get('type') or 'translate') == 'translate':
            def _xy(val):
                parts = re.split(r'[,\s]+', (val or '').strip())
                try:
                    return (float(parts[0]), float(parts[1]) if len(parts) > 1 else 0.0)
                except (ValueError, IndexError):
                    return None
            x0 = _xy(el.get('from'))
            x1 = _xy(el.get('to'))
            if x0 is not None and vb_w and vb_h:
                if x1 is None:
                    x1 = x0

                def _visible(tx, ty):
                    """Fraction of the canvas still showing scene content."""
                    vx = (min(vb_w, tx + vb_w) - max(0.0, tx)) / vb_w
                    vy = (min(vb_h, ty + vb_h) - max(0.0, ty)) / vb_h
                    return max(0.0, vx) * max(0.0, vy)

                # The scene is painted from (0,0) and the transform moves all
                # of it. Sample the whole range: the start pose is what a
                # thumbnail and the judge's first frames show, the best pose
                # is what the animation eventually reaches.
                start = _visible(*x0)
                best = 0.0
                steps = 24
                for i in range(steps + 1):
                    f = i / steps
                    best = max(best, _visible(x0[0] + (x1[0] - x0[0]) * f,
                                               x0[1] + (x1[1] - x0[1]) * f))
                if start < 0.95 or best < 0.95:
                    out['canvas_offcanvas'] = True
                    note += (f' translating the whole scene from ({x0[0]:g},{x0[1]:g}) '
                             f'to ({x1[0]:g},{x1[1]:g}) — only {start * 100:.0f}% of the '
                             f'{vb_w:g}×{vb_h:g} canvas shows artwork at the start'
                             + (f', {best * 100:.0f}% at best' if best >= 0.95 else
                                f', {best * 100:.0f}% at best'))
                else:
                    note += (f' translating the whole scene from ({x0[0]:g},{x0[1]:g}) '
                             f'to ({x1[0]:g},{x1[1]:g})')
        out['root_transform_note'] = note
        break

    # Best effort rasterisation check — only if a tool is already installed.
    for tool, cmd in (
        ('rsvg-convert', ['rsvg-convert', '-o', '/dev/null', path]),
        ('inkscape', ['inkscape', path, '--export-type=png', '--export-filename=/dev/null']),
    ):
        if shutil.which(tool):
            try:
                p = subprocess.run(cmd, capture_output=True, timeout=30)
                out['rendered'] = (p.returncode == 0)
                out['render_tool'] = tool
                if p.returncode != 0:
                    out['render_error'] = (p.stderr or b'').decode('utf-8', 'replace')[:400]
            except Exception as e:  # noqa: BLE001 - record, don't crash
                out['rendered'] = False
                out['render_error'] = str(e)
            break
    else:
        try:
            import cairosvg  # type: ignore
            try:
                cairosvg.svg2png(bytestring=raw, write_to='/dev/null')
                out['rendered'] = True
                out['render_tool'] = 'cairosvg'
            except Exception as e:  # noqa: BLE001
                out['rendered'] = False
                out['render_error'] = str(e)[:400]
        except ImportError:
            out['rendered'] = None

    print(json.dumps(out, indent=2))


if __name__ == '__main__':
    main()
