"""`python -m mallard_gui.viewer --stdio|--listen PORT --root DIR [--idle SECONDS]`: the viewer job's entry
point; `--batch SPEC` renders views to files instead (the fallback without a live view), `--build-lod` makes
a snapshot's surface levels of detail."""

from __future__ import annotations

import argparse
import sys

from .protocol import main_listen, main_stdio


def main() -> int:
    p = argparse.ArgumentParser(prog="mallard-viewer", description="Render Mallard's snapshots next to the data.")
    how = p.add_mutually_exclusive_group(required=True)
    how.add_argument("--stdio", action="store_true", help="the protocol on stdin and stdout")
    how.add_argument("--listen", type=int, metavar="PORT", help="the protocol on 127.0.0.1:PORT (a viewer pod)")
    how.add_argument("--batch", metavar="SPEC", help="render the views of a JSON file to PNG files, then exit")
    how.add_argument(
        "--build-lod",
        nargs=3,
        metavar=("RUN", "SERIES", "INDEX"),
        help="make a snapshot's surface levels of detail next to it (a job), then exit",
    )
    p.add_argument("--root", required=True, help="the directory runs are opened under")
    p.add_argument("--idle", type=float, default=900.0, help="exit after this many seconds without a request")
    args = p.parse_args()
    if args.build_lod:
        import json
        from pathlib import Path

        from .engine import ResultScene

        scene = ResultScene(Path(args.root))
        run, series, index = args.build_lod
        scene.open(run)
        print(json.dumps(scene.build_lod(series, int(index))))
        return 0
    if args.batch:
        from .batch import main_batch

        return main_batch(args.batch, args.root)
    if args.listen:
        return main_listen(args.root, args.idle, args.listen)
    return main_stdio(args.root, args.idle)


if __name__ == "__main__":
    sys.exit(main())
