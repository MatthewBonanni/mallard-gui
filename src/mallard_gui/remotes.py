"""Remotes: the connection test and Mallard install discovery (installing is `installs`).

Every probe is one shell script per round trip on the login node; nothing is installed or left running.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .backends.ssh import RemoteError, SshConnection
from .config import Install
from .mallard import parse_version

RELEASES = "https://github.com/MatthewBonanni/mallard/releases/download"
IMAGE = "ghcr.io/matthewbonanni/mallard"

# Each line is "key<TAB>value[<TAB>value...]". `module` is a shell function that non-interactive
# shells often lack, so its init script is sourced when the environment names one.
_PROBE = r"""
echo "os	$(uname -s)"
echo "home	$HOME"
echo "arch	$(uname -m)"
echo "glibc	$(ldd --version 2>&1 | head -n 1)"
for c in sbatch squeue sinfo qsub bsub apptainer singularity spack curl wget sha256sum; do
  command -v "$c" >/dev/null 2>&1 && echo "has	$c"
done
if ! command -v module >/dev/null 2>&1 && [ -n "$MODULESHOME" ] && [ -f "$MODULESHOME/init/sh" ]; then
  . "$MODULESHOME/init/sh" >/dev/null 2>&1
fi
command -v module >/dev/null 2>&1 && echo "has	module"
command -v sinfo >/dev/null 2>&1 && sinfo -h -o '%P	%G	%D' 2>/dev/null | sed 's/^/part	/'
version() { "$1" --version 2>&1 | head -n 1; }
p=$(command -v mallard 2>/dev/null) && echo "cand	PATH	$p	$(version "$p")"
for f in "$HOME"/mallard/*/install.toml; do
  [ -f "$f" ] || continue
  d=$(dirname "$f")
  [ -x "$d/bin/mallard" ] && echo "cand	$d/install.toml	$d/bin/mallard	$(version "$d/bin/mallard")"
done
for p in "$@"; do
  [ -x "$p" ] && echo "cand	recorded	$p	$(version "$p")" || echo "missing	$p"
done
if command -v module >/dev/null 2>&1; then
  for m in $(module -t avail mallard 2>&1 | grep -i '^mallard'); do
    ( module load "$m" >/dev/null 2>&1 && p=$(command -v mallard) && echo "cand	module:$m	$p	$(version "$p")" )
  done
fi
"""


@dataclass
class Candidate:
    """A Mallard found on a remote: how it was found, its path, and `--version`'s answer."""

    source: str
    path: str
    version: str
    text: str

    @property
    def module(self) -> str:
        return f"module load {self.source.removeprefix('module:')}" if self.source.startswith("module:") else ""

    def install(self) -> Install:
        return Install(self.path, module=self.module, version=self.version)


@dataclass
class Probe:
    ok: bool
    error: str = ""
    connect_s: float = 0.0
    latency_s: float = 0.0
    os: str = ""
    arch: str = ""
    glibc: str = ""
    home: str = ""
    tools: set[str] = field(default_factory=set)
    partitions: list[dict[str, str]] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def scheduler(self) -> str:
        for tool, name in (("sbatch", "slurm"), ("qsub", "pbs"), ("bsub", "lsf")):
            if tool in self.tools:
                return name
        return ""

    @property
    def container(self) -> str:
        return next((c for c in ("apptainer", "singularity") if c in self.tools), "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error": self.error,
            "connect_s": self.connect_s,
            "latency_s": self.latency_s,
            "os": self.os,
            "arch": self.arch,
            "glibc": self.glibc,
            "home": self.home,
            "scheduler": self.scheduler,
            "container": self.container,
            "tools": sorted(self.tools),
            "partitions": self.partitions,
            "candidates": [vars(c) | {"module": c.module} for c in self.candidates],
            "missing": self.missing,
        }


def probe(conn: SshConnection, recorded: list[str] | None = None) -> Probe:
    """Connect (MFA is answered here, once per ControlMaster), time a round trip, and run the probe."""
    t0 = time.monotonic()
    try:
        conn.run(["true"])
    except RemoteError as e:
        return Probe(ok=False, error=str(e), connect_s=time.monotonic() - t0)
    t1 = time.monotonic()
    conn.run(["true"])
    t2 = time.monotonic()
    try:
        out = conn.run(["sh", "-c", _PROBE, "sh", *(recorded or [])]).text
    except RemoteError as e:
        return Probe(ok=False, error=str(e), connect_s=t1 - t0, latency_s=t2 - t1)
    return parse_probe(out, connect_s=t1 - t0, latency_s=t2 - t1)


def parse_probe(text: str, connect_s: float = 0.0, latency_s: float = 0.0) -> Probe:
    p = Probe(ok=True, connect_s=connect_s, latency_s=latency_s)
    seen: set[str] = set()
    for line in text.splitlines():
        key, _, rest = line.partition("\t")
        fields = rest.split("\t")
        if key in ("os", "arch", "glibc", "home"):
            setattr(p, key, _glibc(rest) if key == "glibc" else rest.strip())
        elif key == "has":
            p.tools.add(rest.strip())
        elif key == "part" and len(fields) >= 1:
            gres = fields[1] if len(fields) > 1 and fields[1] != "(null)" else ""
            p.partitions.append({"name": fields[0], "gres": gres, "nodes": fields[2] if len(fields) > 2 else ""})
        elif key == "cand" and len(fields) >= 2 and fields[1] not in seen:
            seen.add(fields[1])
            text_ = fields[2] if len(fields) > 2 else ""
            p.candidates.append(Candidate(fields[0], fields[1], parse_version(text_) if text_ else "", text_))
        elif key == "missing":
            p.missing.append(rest)
    return p


def _glibc(line: str) -> str:
    """`ldd (GNU libc) 2.28` -> `2.28`; musl and others keep their first line."""
    words = line.split()
    if "GNU" in line and words and words[-1][:1].isdigit():
        return words[-1]
    return line.strip()


def release_of(version: str) -> str:
    """The release a build belongs to: `0.10.0-147-g1fa6e0b05` -> `0.10.0`."""
    return version.lstrip("v").split("-", 1)[0]
