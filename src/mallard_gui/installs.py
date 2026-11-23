"""Mallard installs on a remote: choosing what to install from the release, installing it beside the
others, verifying it with `mallard install verify`, upgrading and removing.

What a release offers comes from its `release-manifest.json` (an archive per architecture and backend,
images by digest), read here; the login node downloads the chosen artifact itself and checks it against
the SHA-256 the manifest names, so a corrupt or substituted download never lands. Every install goes into
`<root>/mallard/<version>-<variant>/` with an `install.toml`; versions live side by side and nothing points
at "current": the remote's recorded installs name the paths, the first being its default.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .backends.ssh import RemoteError, SshConnection
from .config import Install, Remote
from .mallard import USAGE_ERROR, environment_check_args, install_verify_args, install_verify_reply, json_reply
from .remotes import IMAGE, RELEASES, Probe, release_of

MANIFEST = "release-manifest.json"
MANIFEST_SCHEMA = "mallard-release-manifest"
METHODS = ("archive", "apptainer", "spack")
VARIANTS = ("cpu", "cuda12", "cuda13", "rocm")

Line = Callable[[str], None]


class InstallError(RuntimeError):
    pass


def releases_url() -> str:
    """Where releases are downloaded from: GitHub, or `$MALLARD_GUI_RELEASES` (a mirror, or a local directory)."""
    return os.environ.get("MALLARD_GUI_RELEASES", RELEASES).rstrip("/")


@dataclass
class Artifact:
    kind: str  # archive, image, sif, wheel
    name: str
    variant: str
    platform: str = ""  # linux-x86_64, linux-aarch64; images: every platform they hold
    url: str = ""
    sha256: str = ""
    size: int = 0
    digest: str = ""  # images: sha256:..., pulled by it
    glibc: str = ""  # archives: the oldest glibc they run on

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Artifact:
        return cls(
            kind=str(d.get("kind", "")),
            name=str(d.get("name", "")),
            variant=str(d.get("variant", "")),
            platform=str(d.get("platform", "")),
            url=str(d.get("url", "")),
            sha256=str(d.get("sha256", "")),
            size=int(d.get("size", 0) or 0),
            digest=str(d.get("digest", "")),
            glibc=str(d.get("glibc", "")),
        )


@dataclass
class ReleaseManifest:
    version: str
    artifacts: list[Artifact]
    source: str = ""  # where it was read; "" when built from the naming conventions

    @classmethod
    def from_json(cls, data: dict[str, Any], source: str = "") -> ReleaseManifest:
        if data.get("schema") != MANIFEST_SCHEMA:
            raise InstallError(f"{source or 'manifest'}: not a {MANIFEST_SCHEMA} (schema {data.get('schema')!r})")
        if int(data.get("version", 0)) > 1:
            raise InstallError(f"{source}: manifest version {data.get('version')} is newer than this GUI reads (1)")
        return cls(str(data["mallard"]), [Artifact.from_dict(a) for a in data.get("artifacts", [])], source)

    def find(self, kind: str, variant: str, platform: str = "") -> Artifact | None:
        for a in self.artifacts:
            if a.kind == kind and a.variant == variant and (not platform or not a.platform or a.platform == platform):
                return a
        return None


def _read_url(url: str, timeout: float = 30.0) -> bytes:
    if not url.startswith(("https://", "http://", "file://")):
        raise InstallError(f"{url}: not an http(s) or file URL")
    with urllib.request.urlopen(url, timeout=timeout) as r:
        data: bytes = r.read()
    return data


def fetch_manifest(version: str, base: str | None = None) -> ReleaseManifest:
    """The release's manifest; a release without one (before 0.12) gets the artifacts its names imply,
    each archive's SHA-256 read from its `.sha256` file here, so the remote still checks it."""
    base = base or releases_url()
    v = release_of(version)
    url = f"{base}/v{v}/{MANIFEST}"
    try:
        return ReleaseManifest.from_json(json.loads(_read_url(url)), url)
    except (urllib.error.URLError, OSError, ValueError) as e:
        if isinstance(e, urllib.error.HTTPError) and e.code != 404:
            raise InstallError(f"{url}: {e}") from e
    return conventional_manifest(v, base)


def conventional_manifest(version: str, base: str) -> ReleaseManifest:
    """TODO(#1322): releases before the manifest; drop once every supported release has one."""
    artifacts = [Artifact("image", f"{IMAGE}:{version}", "cpu"), Artifact("image", f"{IMAGE}:{version}-cuda", "cuda13")]
    for arch in ("x86_64", "aarch64"):
        for variant in VARIANTS:
            name = archive_name(version, arch, variant)
            url = f"{base}/v{version}/{name}"
            try:
                sha = _read_url(url + ".sha256", timeout=10).decode().split()[0]
            except (urllib.error.URLError, OSError, IndexError, UnicodeDecodeError):
                continue
            artifacts.append(Artifact("archive", name, variant, f"linux-{arch}", url, sha))
    return ReleaseManifest(version, artifacts)


def archive_name(version: str, arch: str, variant: str) -> str:
    return f"mallard-{version}-linux-{arch}-{variant}.tar.gz"


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", text)[:3])


@dataclass
class InstallPlan:
    method: str
    version: str
    variant: str
    directory: str
    script: str
    note: str
    artifact: str = ""
    sha256: str = ""
    size: int = 0
    job: bool = False  # runs as a batch job (Spack builds), not on the login node
    skipped: list[str] = field(default_factory=list)  # methods passed over, each with why

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))


def plan(
    probe: Probe,
    manifest: ReleaseManifest,
    variant: str = "cpu",
    method: str = "",
    root: str = "",
) -> InstallPlan:
    """What to install, in the design's order: the release's relocatable archive for this architecture and
    variant (its SHA-256 checked), the release image through Apptainer, a Spack build as a batch job."""
    if probe.os and probe.os != "Linux":
        raise InstallError(f"{probe.os}: Mallard's remote installs are Linux only")
    if variant not in VARIANTS:
        raise InstallError(f"variant {variant!r}: one of {', '.join(VARIANTS)}")
    if method and method not in METHODS:
        raise InstallError(f"method {method!r}: one of {', '.join(METHODS)}")
    root = (root or probe.home or "$HOME").rstrip("/")
    v = manifest.version
    directory = f"{root}/mallard/{v}-{variant}"
    skipped: list[str] = []
    for m in [method] if method else list(METHODS):
        why = ""
        if m == "archive":
            why = _archive_unusable(probe, manifest, variant)
            if not why:
                a = manifest.find("archive", variant, f"linux-{probe.arch or 'x86_64'}")
                assert a is not None
                return InstallPlan(
                    "archive",
                    v,
                    variant,
                    directory,
                    archive_script(directory, a, probe, v),
                    f"the release's relocatable archive for {probe.arch or 'x86_64'}, its SHA-256 checked; built "
                    "against the MPICH ABI, so multi-node runs load the site's MPICH-compatible MPI module",
                    a.name,
                    a.sha256,
                    a.size,
                    skipped=skipped,
                )
        elif m == "apptainer":
            a = manifest.find("image", variant)
            if not probe.container:
                why = "no apptainer or singularity on the login node"
            elif a is None:
                why = f"release {v} has no {variant} image"
            else:
                d = f"{directory}-{probe.container}"
                return InstallPlan(
                    "apptainer",
                    v,
                    variant,
                    d,
                    apptainer_script(d, a, probe.container, v, variant),
                    f"the release image through {probe.container}, pulled by digest: single-node runs",
                    a.name,
                    a.digest,
                    skipped=skipped,
                )
        elif m == "spack":
            if "spack" not in probe.tools:
                why = "no spack on the login node"
            else:
                d = f"{directory}-spack"
                return InstallPlan(
                    "spack",
                    v,
                    variant,
                    d,
                    spack_script(d, v, variant),
                    "a Spack build in your space with the site's compilers and MPI as externals, as a batch job: slow",
                    f"mallard@{v}",
                    job=probe.scheduler == "slurm",
                    skipped=skipped,
                )
        skipped.append(f"{m}: {why}")
    raise InstallError("no way to install here: " + "; ".join(skipped))


def _archive_unusable(probe: Probe, manifest: ReleaseManifest, variant: str) -> str:
    arch = probe.arch or "x86_64"
    a = manifest.find("archive", variant, f"linux-{arch}")
    if a is None:
        return f"release {manifest.version} has no {variant} archive for {arch}"
    if not a.sha256:
        return f"{a.name}: no SHA-256 to check it against"
    if (
        a.glibc
        and probe.glibc
        and _version_tuple(probe.glibc)
        and _version_tuple(probe.glibc) < _version_tuple(a.glibc)
    ):
        return f"{a.name} needs glibc {a.glibc}; the login node has {probe.glibc}"
    if probe.tools and not {"curl", "wget"} & probe.tools:
        return "neither curl nor wget on the login node"
    return ""


def _header(directory: str) -> list[str]:
    """Every script installs into DIR.partial and renames it to DIR only once it works: an install either
    exists whole or not at all, and an existing one is never overwritten."""
    return [
        "set -eu",
        "exec 2>&1",
        f"d={shlex.quote(directory)}",
        't="$d.partial"',
        '[ -e "$d/install.toml" ] && { echo "$d exists: installs are never overwritten"; exit 3; }',
        'rm -rf "$t" && mkdir -p "$t" && cd "$t"',
    ]


def _install_toml(fields: dict[str, str]) -> list[str]:
    body = "".join(f"{k} = {json.dumps(v)}\n" for k, v in fields.items())
    return ["cat > install.toml <<'EOF'", body.rstrip("\n"), "EOF"]


def _footer() -> list[str]:
    return [
        '"$t/bin/mallard" --version',
        'mv "$t" "$d"',
        'echo "installed $d"',
    ]


def archive_script(directory: str, a: Artifact, probe: Probe, version: str) -> str:
    fetch = 'curl -fsSL --retry 3 -o "$f" "$u"' if not probe.tools or "curl" in probe.tools else 'wget -q -O "$f" "$u"'
    check = (
        'echo "$s  $f" | sha256sum -c -'
        if not probe.tools or "sha256sum" in probe.tools
        else 'echo "$s  $f" | shasum -a 256 -c -'
    )
    lines = [
        *_header(directory),
        f"u={shlex.quote(a.url)}",
        f"f={shlex.quote(a.name)}",
        f"s={shlex.quote(a.sha256)}",
        'echo "downloading $u"',
        fetch,
        check,
        'tar -xzf "$f" --strip-components=1 && rm "$f"',
        *_install_toml(
            {
                "version": version,
                "variant": a.variant,
                "method": "archive",
                "artifact": a.name,
                "sha256": a.sha256,
                "url": a.url,
                "installed_by": "mallard-gui",
            }
        ),
        *_footer(),
    ]
    return "\n".join(lines) + "\n"


def apptainer_script(directory: str, a: Artifact, runtime: str, version: str, variant: str) -> str:
    ref = a.name.rsplit(":", 1)[0] + "@" + a.digest if a.digest else a.name
    nv = " --nv" if variant.startswith("cuda") else " --rocm" if variant == "rocm" else ""
    wrapper = f'#!/bin/sh\nexec {runtime} exec{nv} "$(dirname "$0")/../mallard.sif" mallard "$@"\n'
    lines = [
        *_header(directory),
        "mkdir -p bin",
        f'echo "pulling {ref}"',
        f"{runtime} pull mallard.sif docker://{ref}",
        f"printf '%s' {shlex.quote(wrapper)} > bin/mallard",
        "chmod +x bin/mallard",
        *_install_toml(
            {
                "version": version,
                "variant": variant,
                "method": runtime,
                "artifact": a.name,
                "digest": a.digest,
                "installed_by": "mallard-gui",
            }
        ),
        *_footer(),
    ]
    return "\n".join(lines) + "\n"


def spack_script(directory: str, version: str, variant: str) -> str:
    spec = f"mallard@{version}" + {"cpu": "", "rocm": " +rocm", "cuda12": " +cuda", "cuda13": " +cuda"}[variant]
    lines = [
        "#!/bin/bash",
        *_header(directory),
        "mkdir -p bin env",
        "spack compiler find >/dev/null",
        "spack external find --not-buildable mpich openmpi cray-mpich mvapich cuda hip >/dev/null 2>&1 || true",
        "cat > env/spack.yaml <<EOF",
        "spack:",
        f'  specs: ["{spec}"]',
        "  view: false",
        "  config: {install_tree: {root: $t/opt}}",
        "EOF",
        "spack -e env concretize -f",
        "spack -e env install --fail-fast",
        "p=$(spack -e env location -i mallard)",
        'printf \'#!/bin/sh\\nexec "%s/bin/mallard" "$@"\\n\' "$p" > bin/mallard',
        "chmod +x bin/mallard",
        *_install_toml(
            {"version": version, "variant": variant, "method": "spack", "artifact": spec, "installed_by": "mallard-gui"}
        ),
        *_footer(),
    ]
    return "\n".join(lines) + "\n"


# Running an install


@dataclass
class Outcome:
    ok: bool
    install: Install | None = None
    error: str = ""
    log: str = ""


def run_install(conn: SshConnection, p: InstallPlan, on_line: Line | None = None, poll_s: float = 10.0) -> Outcome:
    """Run the plan's script: on the login node with its output streamed, or (Spack) as a batch job whose
    log is followed. The result is the install to record, once its directory exists whole."""
    lines: list[str] = []

    def emit(line: str) -> None:
        lines.append(line)
        if on_line:
            on_line(line)

    if p.job:
        rc = _run_job(conn, p, emit, poll_s)
    else:
        proc = conn.popen(["sh", "-c", p.script], stdout=subprocess.PIPE)
        assert proc.stdout is not None
        for raw in proc.stdout:
            emit(raw.decode("utf-8", "replace").rstrip("\n"))
        rc = proc.wait()
        if proc.stderr:
            for err in proc.stderr.read().decode("utf-8", "replace").splitlines():
                emit(err)
    log = "\n".join(lines)
    if rc != 0:
        return Outcome(False, error=f"exit {rc}: " + (lines[-1] if lines else "no output"), log=log)
    install = Install(
        f"{p.directory}/bin/mallard",
        version=p.version,
        variant=p.variant,
        method=p.method,
        artifact=p.artifact,
        sha256=p.sha256,
        installed=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    return Outcome(True, install, log=log)


def _run_job(conn: SshConnection, p: InstallPlan, emit: Line, poll_s: float) -> int:
    script = f"{p.directory}.install.sh"
    log = f"{p.directory}.install.log"
    conn.write_file(script, p.script.encode())
    out = conn.run(
        ["sbatch", "--parsable", "--job-name=mallard-install", f"--output={log}", "--time=04:00:00", "-c", "8", script]
    ).text.strip()
    job = out.split(";", 1)[0]
    emit(f"submitted the build as job {job}; its log is {log}")
    offset = 0
    while True:
        chunk = conn.run(["sh", "-c", 'test -f "$1" && tail -c +"$2" "$1" || true', "sh", log, str(offset + 1)]).stdout
        offset += len(chunk)
        for line in chunk.decode("utf-8", "replace").splitlines():
            emit(line)
        state = conn.run(["squeue", "-h", "-j", job, "-o", "%T"], check=False).text.strip()
        if not state:
            break
        time.sleep(poll_s)
    done = conn.run(["test", "-f", f"{p.directory}/install.toml"], check=False).returncode == 0
    return 0 if done else 1


# Verification


@dataclass
class Verification:
    passed: bool
    summary: str
    checks: list[dict[str, Any]] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    log: str = ""


def verify_command(remote: Remote, install: Install, partial: bool = False) -> list[str]:
    """`mallard install verify --format json` as a short job on the remote's partition (srun, which waits
    for it), or on the login node where the remote has no Slurm or its viewer settings say `login`."""
    load = f"{install.module} >/dev/null 2>&1; " if install.module else ""
    mallard = shlex.quote(install.path)
    args = shlex.join(environment_check_args() if partial else install_verify_args())
    inner = f"{load}{mallard} {args} --kokkos-num-threads=2"
    if remote.backend == "slurm" and remote.viewer.get("launch", "srun") != "login":
        srun = ["srun", "--ntasks=1", "--cpus-per-task=2", "--time=00:15:00", "--job-name=mallard-verify"]
        for key in ("partition", "account"):
            value = remote.viewer.get(key) or remote.defaults.get(key)
            if value:
                srun.append(f"--{key}={value}")
        return [*srun, "sh", "-c", inner]
    return ["sh", "-c", inner]


def verify(conn: SshConnection, remote: Remote, install: Install) -> Verification:
    """Run the install's verification and record its verdict on `install`. A Mallard older than
    `install verify` (0.12) gets the environment check alone, said so in the summary."""
    try:
        result = conn.run(verify_command(remote, install), check=False)
    except (RemoteError, OSError) as e:
        return _record(install, Verification(False, str(e)))
    text = result.text
    data = install_verify_reply(text)
    # TODO(#1320): a Mallard without `install verify` gets the environment check alone
    if data is None and result.returncode == USAGE_ERROR:
        partial = conn.run(verify_command(remote, install, partial=True), check=False)
        env = json_reply(partial.text)
        ok = env is not None and partial.returncode == 0 and not env.get("errors")
        summary = "environment " + ("ok" if ok else "failed") + "; this Mallard predates install verify (no quick run)"
        return _record(install, Verification(ok, summary, data=env or {}, log=partial.text))
    if data is None:
        err = result.stderr.decode("utf-8", "replace").strip() or text.strip()
        return _record(install, Verification(False, f"exit {result.returncode}: {err[-300:]}", log=text))
    checks = list(data.get("checks", []))
    passed = bool(data.get("passed")) and result.returncode == 0
    failed = [c.get("name", "?") + (f" {c['dim']}D" if c.get("dim") else "") for c in checks if not c.get("passed")]
    seconds = float(data.get("seconds", 0.0))
    summary = (
        f"passed: {len(checks)} checks in {seconds:.1f} s" if passed else "failed: " + (", ".join(failed) or "see log")
    )
    return _record(install, Verification(passed, summary, checks, data, text))


def _record(install: Install, v: Verification) -> Verification:
    install.verified = ("passed" if v.passed else "failed") + " " + datetime.now(UTC).strftime("%Y-%m-%d")
    install.verify_summary = v.summary
    return v


# Upgrades and removal


def install_directory(install: Install) -> str:
    """The install's own directory (`.../mallard/<version>-<variant>`), from its executable's path."""
    return install.path.removesuffix("/bin/mallard")


def upgrade_target(remote: Remote, version: str) -> str:
    """The release to offer as an upgrade: `version` when no recorded install has it and it is newer
    than the default; "" otherwise."""
    v = release_of(version)
    if not v or not remote.installs:
        return ""
    if any(release_of(i.version) == v for i in remote.installs):
        return ""
    current = _version_tuple(release_of(remote.installs[0].version))
    return v if not current or _version_tuple(v) > current else ""


def make_default(remote: Remote, path: str) -> None:
    remote.installs.sort(key=lambda i: i.path != path)


def remove(conn: SshConnection, install: Install) -> None:
    """Delete an install's directory on the remote, only one this GUI's layout made: it must hold an
    `install.toml` and sit in a `mallard/` directory."""
    d = install_directory(install)
    if not d.startswith("/") or "/mallard/" not in d or d.endswith("/mallard"):
        raise InstallError(f"{d}: not an install directory of the form <root>/mallard/<version>-<variant>")
    conn.run(
        [
            "sh",
            "-c",
            'test -f "$1/install.toml" || { echo "$1 has no install.toml" >&2; exit 1; }; rm -rf "$1"',
            "sh",
            d,
        ]
    )
