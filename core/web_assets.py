"""Shared themes, inline font/emblem assets, JSON script escaping, and output-path guards for views and wiki (REL-D2)."""
from __future__ import annotations

import base64
import json
from pathlib import Path

from .paths import is_within

# Emerald Enterprise dark theme tokens (sage surfaces, emerald accent, mint completion).
DARK = (
    'color-scheme:dark;--bg:#002d2a;--bg-alt:#0c3835;--bg-hover:#194441;--fg:#f5f5f5;--muted:#a7b9b7;'
    '--border:#1b4340;--accent:#7ecf1c;--accent-soft:#23462b;--error:#ff8f8f;--mark:#5a4a14;'
    '--bg-sidebar:#002420;--bg-topbar:#002926;--bg-input:#002522;--bg-code:#001f1c;--bg-active:#214846;'
    '--border-soft:#103532;--border-strong:#426461;--border-lilac:#a7b9b7;--accent-strong:#3b6a12;'
    '--primary:#7ecf1c;--done:#62d6a2;--success:#62d6a2;--success-soft:#0e4436;--warn:#f2b54a;'
    '--warn-fg:#f7d65e;--warn-bg:#3a3218;--warn-border:#a88b2a;--sub:#c8d2d2;--chip-bg:#123d3a;'
    '--p3-bg:#0c3835;--ok-bg:#1b3a1c;--ok-border:#4f8a1a;--ok-fg:#b1ee2c;--next-bg:#214846;'
    '--next-border:#6f908d;--next-fg:#e3ecea;'
)

# Emerald Enterprise light theme tokens mapped to the same CSS custom properties.
LIGHT = (
    'color-scheme:light;--bg:#f5f5f5;--bg-alt:#ffffff;--bg-hover:#e9ecec;--fg:#002d2a;--muted:#426461;'
    '--border:#c8d2d2;--accent:#3b6a12;--accent-soft:#e3f2cf;--error:#b3261e;--mark:#fbe7a1;'
    '--bg-sidebar:#002d2a;--bg-topbar:#ffffff;--bg-input:#ffffff;--bg-code:#e9ecec;--bg-active:#dfe8e6;'
    '--border-soft:#dde4e4;--border-strong:#5e7f7c;--border-lilac:#426461;--accent-strong:#7ecf1c;'
    '--primary:#7ecf1c;--done:#16794c;--success:#16794c;--success-soft:#dcf3e8;--warn:#8a5700;'
    '--warn-fg:#6b4a00;--warn-bg:#fdf3d6;--warn-border:#d9a92e;--sub:#2b4a47;--chip-bg:#e9ecec;'
    '--p3-bg:#ffffff;--ok-bg:#e8f6d6;--ok-border:#7ecf1c;--ok-fg:#2f5a0c;--next-bg:#e9ecec;'
    '--next-border:#a7b9b7;--next-fg:#2b4a47;'
)

# Standalone Living Memory emblem (base64 SVG data URI: zero external URLs in rendered HTML).
_ALM_LOGO_BYTES = (
    b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 36 36' fill='none'>"
    b"<rect x='2' y='2' width='32' height='32' rx='8' fill='#0c3835' stroke='#7ecf1c' stroke-width='2'/>"
    b"<path d='M11 12h14M11 18h10M11 24h7' stroke='#f5f5f5' stroke-width='2.2' stroke-linecap='round'/>"
    b"<circle cx='25' cy='24' r='3' fill='#7ecf1c'/></svg>"
)
ALM_LOGO_SVG = 'data:image/svg+xml;base64,' + base64.b64encode(_ALM_LOGO_BYTES).decode('ascii')

_ASSETS = {}


def asset(name):
    """Read and cache a vendored base64 asset from assets/<name>."""
    if name not in _ASSETS:
        _ASSETS[name] = (Path(__file__).resolve().parent.parent / 'assets' / name).read_text(encoding='ascii').strip()
    return _ASSETS[name]


def font_face():
    """Inline @font-face rule for Plus Jakarta Sans Variable."""
    return (
        '@font-face{font-family:"Plus Jakarta Sans Variable";font-style:normal;font-display:swap;font-weight:200 800;'
        'src:url(data:font/woff2;base64,' + asset('plus-jakarta-sans-latin-wght-normal.woff2.b64') + ') format("woff2")}'
    )


def script_json(model):
    """Serialize `model` safely for embedding inside a <script type="application/json"> tag."""
    return (
        json.dumps(model, ensure_ascii=False)
        .replace('<', '\\u003c')
        .replace('>', '\\u003e')
        .replace('&', '\\u0026')
        .replace('\u2028', '\\u2028')
        .replace('\u2029', '\\u2029')
    )


def out_path(root, out):
    """Resolve an explicit --out path; refuse writing inside the store except under .views/."""
    path = Path(out).resolve()
    root_resolved = Path(root).resolve()
    if is_within(path, root_resolved) and not is_within(path, root_resolved / '.views'):
        raise ValueError('--out must be outside the memory store (or under its .views folder)')
    return path
