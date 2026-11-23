# ruff: noqa: E501  (vendored Lucide path data)
"""The GUI's one icon set: Lucide (ISC, `assets/LUCIDE-LICENSE.txt`), outline glyphs on a 24-unit grid, drawn
at 16 px with a 1.75 px stroke. The domain glyphs Lucide lacks (regions, partial, slice, les, sponges, sources,
particles) are drawn in its style.
"""

from __future__ import annotations

_SHAPES = {
    "case": (  # file-sliders
        '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/>',
        '<path d="M14 2v4a2 2 0 0 0 2 2h4"/>',
        '<path d="M8 12h8"/>',
        '<path d="M10 11v2"/>',
        '<path d="M8 17h8"/>',
        '<path d="M14 16v2"/>',
    ),
    "runs": (  # circle-play
        '<circle cx="12" cy="12" r="10"/>',
        '<polygon points="10 8 16 12 10 16 10 8"/>',
    ),
    "data": (  # database
        '<ellipse cx="12" cy="5" rx="9" ry="3"/>',
        '<path d="M3 5V19A9 3 0 0 0 21 19V5"/>',
        '<path d="M3 12A9 3 0 0 0 21 12"/>',
    ),
    "analysis": (  # chart-spline
        '<path d="M3 3v16a2 2 0 0 0 2 2h16"/>',
        '<path d="M7 16c.5-2 1.5-7 4-7 2 0 2 3 4 3 2.5 0 4.5-5 5-7"/>',
    ),
    "results": (  # box
        '<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/>',
        '<path d="m3.3 7 8.7 5 8.7-5"/>',
        '<path d="M12 22V12"/>',
    ),
    "remotes": (  # server
        '<rect width="20" height="8" x="2" y="2" rx="2" ry="2"/>',
        '<rect width="20" height="8" x="2" y="14" rx="2" ry="2"/>',
        '<line x1="6" x2="6.01" y1="6" y2="6"/>',
        '<line x1="6" x2="6.01" y1="18" y2="18"/>',
    ),
    "settings": (  # settings
        '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/>',
        '<circle cx="12" cy="12" r="3"/>',
    ),
    "search": (  # search
        '<circle cx="11" cy="11" r="8"/>',
        '<path d="m21 21-4.3-4.3"/>',
    ),
    "mesh": (  # grid-3x3
        '<rect width="18" height="18" x="3" y="3" rx="2"/>',
        '<path d="M3 9h18"/>',
        '<path d="M3 15h18"/>',
        '<path d="M9 3v18"/>',
        '<path d="M15 3v18"/>',
    ),
    "physics": (  # waves
        '<path d="M2 6c.6.5 1.2 1 2.5 1C7 7 7 5 9.5 5c2.6 0 2.4 2 5 2 2.5 0 2.5-2 5-2 1.3 0 1.9.5 2.5 1"/>',
        '<path d="M2 12c.6.5 1.2 1 2.5 1 2.5 0 2.5-2 5-2 2.6 0 2.4 2 5 2 2.5 0 2.5-2 5-2 1.3 0 1.9.5 2.5 1"/>',
        '<path d="M2 18c.6.5 1.2 1 2.5 1 2.5 0 2.5-2 5-2 2.6 0 2.4 2 5 2 2.5 0 2.5-2 5-2 1.3 0 1.9.5 2.5 1"/>',
    ),
    "boundaries": (  # square-dashed
        '<path d="M5 3a2 2 0 0 0-2 2"/>',
        '<path d="M19 3a2 2 0 0 1 2 2"/>',
        '<path d="M21 19a2 2 0 0 1-2 2"/>',
        '<path d="M5 21a2 2 0 0 1-2-2"/>',
        '<path d="M9 3h1"/>',
        '<path d="M9 21h1"/>',
        '<path d="M14 3h1"/>',
        '<path d="M14 21h1"/>',
        '<path d="M3 9v1"/>',
        '<path d="M21 9v1"/>',
        '<path d="M3 14v1"/>',
        '<path d="M21 14v1"/>',
    ),
    "initialize": (  # crosshair
        '<circle cx="12" cy="12" r="10"/>',
        '<line x1="22" x2="18" y1="12" y2="12"/>',
        '<line x1="6" x2="2" y1="12" y2="12"/>',
        '<line x1="12" x2="12" y1="6" y2="2"/>',
        '<line x1="12" x2="12" y1="22" y2="18"/>',
    ),
    "numerics": (  # sigma
        '<path d="M18 7V5a1 1 0 0 0-1-1H6.5a.5.5 0 0 0-.4.8l4.5 6a2 2 0 0 1 0 2.4l-4.5 6a.5.5 0 0 0 .4.8H17a1 1 0 0 0 1-1v-2"/>',
    ),
    "regions": (
        '<path d="M4 4h10v10H4z"/>',
        '<path d="M10 10h10v10H10z"/>',
    ),
    "probes": (  # locate-fixed
        '<line x1="2" x2="5" y1="12" y2="12"/>',
        '<line x1="19" x2="22" y1="12" y2="12"/>',
        '<line x1="12" x2="12" y1="2" y2="5"/>',
        '<line x1="12" x2="12" y1="19" y2="22"/>',
        '<circle cx="12" cy="12" r="7"/>',
        '<circle cx="12" cy="12" r="3"/>',
    ),
    "output": (  # download
        '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>',
        '<polyline points="7 10 12 15 17 10"/>',
        '<line x1="12" x2="12" y1="15" y2="3"/>',
    ),
    "statistics": (  # chart-column
        '<path d="M3 3v16a2 2 0 0 0 2 2h16"/>',
        '<path d="M18 17V9"/>',
        '<path d="M13 17V5"/>',
        '<path d="M8 17v-3"/>',
    ),
    "run": (  # clock
        '<circle cx="12" cy="12" r="10"/>',
        '<polyline points="12 6 12 12 16 14"/>',
    ),
    "chemistry": (  # flame
        '<path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z"/>',
    ),
    "les": (  # a large eddy and a small one (drawn in Lucide's style)
        '<path d="M10 14a1.5 1.5 0 0 1 3 0 3 3 0 0 1-6 0 4.5 4.5 0 0 1 9 0 6 6 0 0 1-12 0"/>',
        '<path d="M19 5.5a1 1 0 0 1 2 0 2 2 0 0 1-4 0 3 3 0 0 1 4.5-2.6"/>',
    ),
    "radiation": (  # sun
        '<circle cx="12" cy="12" r="4"/>',
        '<path d="M12 2v2"/>',
        '<path d="M12 20v2"/>',
        '<path d="m4.93 4.93 1.41 1.41"/>',
        '<path d="m17.66 17.66 1.41 1.41"/>',
        '<path d="M2 12h2"/>',
        '<path d="M20 12h2"/>',
        '<path d="m6.34 17.66-1.41 1.41"/>',
        '<path d="m19.07 4.93-1.41 1.41"/>',
    ),
    "sponges": (  # a wave damped to rest against a wall (drawn in Lucide's style)
        '<path d="M2 12c1-9 3-9 4 0s2.5 6 3.5 0 2-3.5 3 0 1.5 1.5 2.5 0"/>',
        '<rect width="4" height="16" x="18" y="4" rx="1"/>',
    ),
    "sources": (  # a plus in a circle, sending out (drawn in Lucide's style)
        '<circle cx="12" cy="12" r="3.5"/>',
        '<path d="M12 10.5v3"/>',
        '<path d="M10.5 12h3"/>',
        '<path d="m15.5 8.5 4.5-4.5"/>',
        '<path d="M17 4h3v3"/>',
        '<path d="m8.5 15.5-4.5 4.5"/>',
        '<path d="M7 20H4v-3"/>',
        '<path d="m8.5 8.5-4.5-4.5"/>',
        '<path d="M4 7V4h3"/>',
        '<path d="m15.5 15.5 4.5 4.5"/>',
        '<path d="M20 17v3h-3"/>',
    ),
    "triggers": (  # zap
        '<path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/>',
    ),
    "parallel": (  # cpu
        '<rect width="16" height="16" x="4" y="4" rx="2"/>',
        '<rect width="6" height="6" x="9" y="9" rx="1"/>',
        '<path d="M15 2v2"/>',
        '<path d="M15 20v2"/>',
        '<path d="M2 15h2"/>',
        '<path d="M2 9h2"/>',
        '<path d="M20 15h2"/>',
        '<path d="M20 9h2"/>',
        '<path d="M9 2v2"/>',
        '<path d="M9 20v2"/>',
    ),
    "particles": (  # scattered particles and one's path (drawn in Lucide's style)
        '<path d="M3 20c3-1 5-4 7.5-7S16 8 21 6" stroke-dasharray="2 3"/>',
        '<circle cx="21" cy="6" r="1.5"/>',
        '<circle cx="6" cy="6" r="1.5"/>',
        '<circle cx="11" cy="4" r="1"/>',
        '<circle cx="17" cy="17" r="1.5"/>',
        '<circle cx="13" cy="20" r="1"/>',
    ),
    "periodic": (  # repeat
        '<path d="m17 2 4 4-4 4"/>',
        '<path d="M3 11v-1a4 4 0 0 1 4-4h14"/>',
        '<path d="m7 22-4-4 4-4"/>',
        '<path d="M21 13v1a4 4 0 0 1-4 4H3"/>',
    ),
    "other": (  # list
        '<path d="M3 12h.01"/>',
        '<path d="M3 18h.01"/>',
        '<path d="M3 6h.01"/>',
        '<path d="M8 12h13"/>',
        '<path d="M8 18h13"/>',
        '<path d="M8 6h13"/>',
    ),
    "eye": (  # eye
        '<path d="M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0"/>',
        '<circle cx="12" cy="12" r="3"/>',
    ),
    "fit": (  # expand: arrows out to the corners
        '<path d="m15 15 6 6"/>',
        '<path d="m15 9 6-6"/>',
        '<path d="M21 16v5h-5"/>',
        '<path d="M21 8V3h-5"/>',
        '<path d="M3 16v5h5"/>',
        '<path d="m3 21 6-6"/>',
        '<path d="M3 8V3h5"/>',
        '<path d="M9 9 3 3"/>',
    ),
    "frame": (  # scan around a solid square: the selection, framed (drawn in Lucide's style)
        '<path d="M3 7V5a2 2 0 0 1 2-2h2"/>',
        '<path d="M17 3h2a2 2 0 0 1 2 2v2"/>',
        '<path d="M21 17v2a2 2 0 0 1-2 2h-2"/>',
        '<path d="M7 21H5a2 2 0 0 1-2-2v-2"/>',
        '<rect x="8" y="8" width="8" height="8" rx="1" fill="currentColor"/>',
    ),
    "wind": (  # wind
        '<path d="M12.8 19.6A2 2 0 1 0 14 16H2"/>',
        '<path d="M17.5 8a2.5 2.5 0 1 1 2 4H2"/>',
        '<path d="M9.8 4.4A2 2 0 1 1 11 8H2"/>',
    ),
    "camera": (  # camera
        '<path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/>',
        '<circle cx="12" cy="13" r="3"/>',
    ),
    "clip": (  # layers
        '<path d="M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83z"/>',
        '<path d="M2 12a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 0 0 0 22 12"/>',
        '<path d="M2 17a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 0 0 0 22 17"/>',
    ),
    "box": (  # box
        '<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/>',
        '<path d="m3.3 7 8.7 5 8.7-5"/>',
        '<path d="M12 22V12"/>',
    ),
    "xray": (  # scan-eye
        '<path d="M3 7V5a2 2 0 0 1 2-2h2"/>',
        '<path d="M17 3h2a2 2 0 0 1 2 2v2"/>',
        '<path d="M21 17v2a2 2 0 0 1-2 2h-2"/>',
        '<path d="M7 21H5a2 2 0 0 1-2-2v-2"/>',
        '<circle cx="12" cy="12" r="1"/>',
        '<path d="M18.944 12.33a1 1 0 0 0 0-.66 7.5 7.5 0 0 0-13.888 0 1 1 0 0 0 0 .66 7.5 7.5 0 0 0 13.888 0"/>',
    ),
    "isolate": (  # square-dot
        '<rect width="18" height="18" x="3" y="3" rx="2"/>',
        '<circle cx="12" cy="12" r="1"/>',
    ),
    "filter": (  # funnel
        '<path d="M10 20a1 1 0 0 0 .553.895l2 1A1 1 0 0 0 14 21v-7a2 2 0 0 1 .517-1.341L21.74 4.67A1 1 0 0 0 21 3H3a1 1 0 0 0-.742 1.67l7.225 7.989A2 2 0 0 1 10 14z"/>',
    ),
    "pick": (  # mouse-pointer-2
        '<path d="M4.037 4.688a.495.495 0 0 1 .651-.651l16 6.5a.5.5 0 0 1-.063.947l-6.124 1.58a2 2 0 0 0-1.438 1.435l-1.579 6.126a.5.5 0 0 1-.947.063z"/>',
    ),
    "check": (  # check
        '<path d="M20 6 9 17l-5-5"/>',
    ),
    "play": (  # play
        '<polygon points="6 3 20 12 6 21 6 3"/>',
    ),
    "toml": (  # file-code
        '<path d="M10 12.5 8 15l2 2.5"/>',
        '<path d="m14 12.5 2 2.5-2 2.5"/>',
        '<path d="M14 2v4a2 2 0 0 0 2 2h4"/>',
        '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7z"/>',
    ),
    "stop": (  # square
        '<rect width="18" height="18" x="3" y="3" rx="2"/>',
    ),
    "bookmark": (  # bookmark
        '<path d="m19 21-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v16z"/>',
    ),
    "sliders": (  # sliders-horizontal
        '<line x1="21" x2="14" y1="4" y2="4"/>',
        '<line x1="10" x2="3" y1="4" y2="4"/>',
        '<line x1="21" x2="12" y1="12" y2="12"/>',
        '<line x1="8" x2="3" y1="12" y2="12"/>',
        '<line x1="21" x2="16" y1="20" y2="20"/>',
        '<line x1="12" x2="3" y1="20" y2="20"/>',
        '<line x1="14" x2="14" y1="2" y2="6"/>',
        '<line x1="8" x2="8" y1="10" y2="14"/>',
        '<line x1="16" x2="16" y1="18" y2="22"/>',
    ),
    "local": (  # monitor
        '<rect width="20" height="14" x="2" y="3" rx="2"/>',
        '<line x1="8" x2="16" y1="21" y2="21"/>',
        '<line x1="12" x2="12" y1="17" y2="21"/>',
    ),
    "error": (  # circle-x
        '<circle cx="12" cy="12" r="10"/>',
        '<path d="m15 9-6 6"/>',
        '<path d="m9 9 6 6"/>',
    ),
    "warning": (  # triangle-alert
        '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/>',
        '<path d="M12 9v4"/>',
        '<path d="M12 17h.01"/>',
    ),
    "ok": (  # check
        '<path d="M20 6 9 17l-5-5"/>',
    ),
    "link": (  # link
        '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/>',
        '<path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
    ),
    "home": (  # house
        '<path d="M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8"/>',
        '<path d="M3 10a2 2 0 0 1 .709-1.528l7-5.999a2 2 0 0 1 2.582 0l7 5.999A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    ),
    "server": (  # server
        '<rect width="20" height="8" x="2" y="2" rx="2" ry="2"/>',
        '<rect width="20" height="8" x="2" y="14" rx="2" ry="2"/>',
        '<line x1="6" x2="6.01" y1="6" y2="6"/>',
        '<line x1="6" x2="6.01" y1="18" y2="18"/>',
    ),
    "present": (  # circle-check
        '<circle cx="12" cy="12" r="10"/>',
        '<path d="m9 12 2 2 4-4"/>',
    ),
    "partial": (
        '<circle cx="12" cy="12" r="9"/>',
        '<path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor" stroke="none"/>',
    ),
    "prefix": (  # circle-ellipsis
        '<circle cx="12" cy="12" r="10"/>',
        '<path d="M17 12h.01"/>',
        '<path d="M12 12h.01"/>',
        '<path d="M7 12h.01"/>',
    ),
    "absent": (  # circle-minus
        '<circle cx="12" cy="12" r="10"/>',
        '<path d="M8 12h8"/>',
    ),
    "differs": (  # circle-alert
        '<circle cx="12" cy="12" r="10"/>',
        '<line x1="12" x2="12" y1="8" y2="12"/>',
        '<line x1="12" x2="12.01" y1="16" y2="16"/>',
    ),
    "transfer": (  # arrow-right-left
        '<path d="m16 3 4 4-4 4"/>',
        '<path d="M20 7H4"/>',
        '<path d="m8 21-4-4 4-4"/>',
        '<path d="M4 17h16"/>',
    ),
    "refresh": (  # refresh-cw
        '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/>',
        '<path d="M21 3v5h-5"/>',
        '<path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/>',
        '<path d="M8 16H3v5"/>',
    ),
    "plus": (  # plus
        '<path d="M5 12h14"/>',
        '<path d="M12 5v14"/>',
    ),
    "edit": (  # pencil
        '<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"/>',
        '<path d="m15 5 4 4"/>',
    ),
    "plug": (  # plug
        '<path d="M12 22v-5"/>',
        '<path d="M9 8V2"/>',
        '<path d="M15 8V2"/>',
        '<path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"/>',
    ),
    "terminal": (  # square-terminal
        '<path d="m7 11 2-2-2-2"/>',
        '<path d="M11 13h4"/>',
        '<rect width="18" height="18" x="3" y="3" rx="2" ry="2"/>',
    ),
    "copy": (  # copy
        '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/>',
        '<path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
    ),
    "queue": (  # list-ordered
        '<path d="M10 12h11"/>',
        '<path d="M10 18h11"/>',
        '<path d="M10 6h11"/>',
        '<path d="M4 10h2"/>',
        '<path d="M4 6h1v4"/>',
        '<path d="M6 18H4c0-1 2-2 2-3s-1-1.5-2-1"/>',
    ),
    "trash": (  # trash-2
        '<path d="M3 6h18"/>',
        '<path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/>',
        '<path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>',
        '<line x1="10" x2="10" y1="11" y2="17"/>',
        '<line x1="14" x2="14" y1="11" y2="17"/>',
    ),
    "restart": (  # history
        '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/>',
        '<path d="M3 3v5h5"/>',
        '<path d="M12 7v5l4 2"/>',
    ),
    "theme": (  # sun-moon
        '<path d="M12 8a2.83 2.83 0 0 0 4 4 4 4 0 1 1-4-4"/>',
        '<path d="M12 2v2"/>',
        '<path d="M12 20v2"/>',
        '<path d="m4.9 4.9 1.4 1.4"/>',
        '<path d="m17.7 17.7 1.4 1.4"/>',
        '<path d="M2 12h2"/>',
        '<path d="M20 12h2"/>',
        '<path d="m6.3 17.7-1.4 1.4"/>',
        '<path d="m19.1 4.9-1.4 1.4"/>',
    ),
    "slice": (
        '<path d="M3 15l7-5h11l-7 5z"/>',
        '<path d="M12 3v6"/>',
        '<path d="M12 16v5"/>',
    ),
    "probe": (  # map-pin
        '<path d="M20 10c0 4.993-5.539 10.193-7.399 11.799a1 1 0 0 1-1.202 0C9.539 20.193 4 14.993 4 10a8 8 0 0 1 16 0"/>',
        '<circle cx="12" cy="10" r="3"/>',
    ),
    "line": (  # spline
        '<circle cx="19" cy="5" r="2"/>',
        '<circle cx="5" cy="19" r="2"/>',
        '<path d="M5 17A12 12 0 0 1 17 5"/>',
    ),
    "first": (  # chevron-first
        '<path d="m17 18-6-6 6-6"/>',
        '<path d="M7 6v12"/>',
    ),
    "prev": (  # chevron-left
        '<path d="m15 18-6-6 6-6"/>',
    ),
    "next": (  # chevron-right
        '<path d="m9 18 6-6-6-6"/>',
    ),
    "down": (  # chevron-down
        '<path d="m6 9 6 6 6-6"/>',
    ),
    "folder": (  # folder
        '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
    ),
    "last": (  # chevron-last
        '<path d="m7 18 6-6-6-6"/>',
        '<path d="M17 6v12"/>',
    ),
    "bell": (  # bell
        '<path d="M10.268 21a2 2 0 0 0 3.464 0"/>',
        '<path d="M3.262 15.326A1 1 0 0 0 4 17h16a1 1 0 0 0 .74-1.673C19.41 13.956 18 12.499 18 8A6 6 0 0 0 6 8c0 4.499-1.411 5.956-2.738 7.326"/>',
    ),
    "compare": (  # columns-2
        '<rect width="18" height="18" x="3" y="3" rx="2"/>',
        '<path d="M12 3v18"/>',
    ),
    "external": (  # external-link
        '<path d="M15 3h6v6"/>',
        '<path d="M10 14 21 3"/>',
        '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    ),
    "film": (  # film
        '<rect width="18" height="18" x="3" y="3" rx="2"/>',
        '<path d="M7 3v18"/>',
        '<path d="M3 7.5h4"/>',
        '<path d="M3 12h18"/>',
        '<path d="M3 16.5h4"/>',
        '<path d="M17 3v18"/>',
        '<path d="M17 7.5h4"/>',
        '<path d="M17 16.5h4"/>',
    ),
    "close": (  # x
        '<path d="M18 6 6 18"/>',
        '<path d="m6 6 12 12"/>',
    ),
    "lock": (  # lock
        '<rect width="18" height="11" x="3" y="11" rx="2" ry="2"/>',
        '<path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    ),
    "download": (  # download
        '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>',
        '<polyline points="7 10 12 15 17 10"/>',
        '<line x1="12" x2="12" y1="15" y2="3"/>',
    ),
    "x": (  # x
        '<path d="M18 6 6 18"/>',
        '<path d="m6 6 12 12"/>',
    ),
    "reset": (  # rotate-ccw
        '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/>',
        '<path d="M3 3v5h5"/>',
    ),
}


def icon(name: str, size: int = 16) -> str:
    shapes = "".join(_SHAPES.get(name, _SHAPES["other"]))
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
        f'stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" '
        f'aria-hidden="true">{shapes}</svg>'
    )


ICONS = {name: icon(name) for name in _SHAPES}
