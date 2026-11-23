"""mallard-gui: a graphical front end for the Mallard solver (docs/design/gui.md in Mallard)."""

__version__ = "0.1.0"


def main() -> int:
    from .cli import main as cli_main

    return cli_main()
