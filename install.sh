#!/bin/sh
# Installs mallard-gui with uv, installing uv first where it is missing:
#
#   sh install.sh [--solver]
#
# --solver adds Mallard itself (the mallard-python wheel's `mallard` executables, CPU), which the GUI
# then finds in its own environment, for cases run on this machine. Without it the GUI uses the mallard
# on PATH or $MALLARD, and remotes bring their own.
#
# MALLARD_GUI_SPEC (default mallard-gui) is what to install: a version (mallard-gui==0.2.0), a wheel or
# a directory. MALLARD_GUI_FIND_LINKS is a directory of wheels to install from as well as the index.
set -eu

solver=""
for arg in "$@"; do
    case "$arg" in
        --solver) solver=1 ;;
        -h | --help) sed -n '2,12s/^# \{0,1\}//p' "$0"; exit 0 ;;
        *) echo "install.sh: unknown option $arg" >&2; exit 2 ;;
    esac
done

if ! command -v uv > /dev/null 2>&1; then
    for d in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
        [ -x "$d/uv" ] && PATH="$d:$PATH"
    done
fi
if ! command -v uv > /dev/null 2>&1; then
    echo "install.sh: installing uv (https://docs.astral.sh/uv/)"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$PATH"
fi

# Bytecode compiled now rather than by the first launch, which would otherwise compile ~500 modules.
set -- tool install --upgrade --compile-bytecode "${MALLARD_GUI_SPEC:-mallard-gui}"
# TODO(#1321): `mallard-gui[solver]` once the mallard-python wheels are published
[ -n "$solver" ] && set -- "$@" --with mallard-python
[ -n "${MALLARD_GUI_FIND_LINKS:-}" ] && set -- "$@" --find-links "$MALLARD_GUI_FIND_LINKS"
uv "$@"

bin=$(uv tool dir --bin)
case ":$PATH:" in
    *":$bin:"*) echo "install.sh: run mallard-gui in a case's directory" ;;
    *) echo "install.sh: $bin is not on PATH: run \`uv tool update-shell\`, open a new terminal, then mallard-gui" ;;
esac
