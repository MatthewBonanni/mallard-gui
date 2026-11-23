"""The frozen `mallard-gui-server`: `mallard-gui`, and `-m MODULE ARGS` for the processes the GUI starts with
`sys.executable -m` (the local viewer, a server for another project)."""

import runpy
import sys

if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "-m":
        module = sys.argv[2]
        sys.argv = [sys.argv[0], *sys.argv[3:]]
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    else:
        from mallard_gui import main

        sys.exit(main())
