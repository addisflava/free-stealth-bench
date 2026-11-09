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
