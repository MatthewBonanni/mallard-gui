"""The data manager: a run's files with their state at every location, selections by what users think in,
and transfers between this machine and the remotes, or between remotes.

Nothing here is committed: copies are per-machine knowledge, in the cache index.
"""

from __future__ import annotations

import posixpath
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..backends.k8s import HelperPod
from ..backends.ssh import RemoteError, SshConnection
from ..config import Remote, RemoteStore
from ..project import Project, RunRecord
from .cache import CacheIndex
from .locations import LOCAL, LocalLocation, Location, Shell, ShellLocation, SshLocation, TransferError
from .manifest import DERIVED, LIVE, OBJECT, PARTIAL, STREAM, FileEntry, Manifest, build_manifest
from .transfer import REMOTE_ROUTES, Endpoint, Plan, Report, TransferEngine, default_route

SSH_BACKENDS = ("slurm", "pbs", "lsf")
REMOTE_BACKENDS = (*SSH_BACKENDS, "k8s")
SELECTIONS = {
    "snapshot": "One snapshot",
    "snapshots": "Time range",
    "probes": "Probes, forces, integrals",
    "restart": "Newest restart",
    "inputs": "Case inputs",
    "run": "Whole run",
}

# A file's state at a location.
HOME, PRESENT, PREFIX, PARTIAL_STATE, ABSENT, DIFFERS = "home", "present", "prefix", "partial", "absent", "differs"


@dataclass
class TransferState:
    running: bool = False
    done: int = 0
    total: int = 0
    current: str = ""
    report: Report | None = None
    plan: Plan | None = None
    error: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)


class DataManager:
    def __init__(
        self,
        project: Project,
        remotes: RemoteStore | None = None,
        cache: CacheIndex | None = None,
        connect: Callable[[Remote], SshConnection] | None = None,
        helper: Callable[[Remote], Shell] | None = None,
    ) -> None:
        self.project = project
        self.remotes = remotes or RemoteStore()
        self.cache = cache or CacheIndex()
        self.engine = TransferEngine(self.cache)
        self._connect = connect or (lambda r: SshConnection(r.host))
        self._helper = helper or HelperPod
        self._locations: dict[str, Location] = {LOCAL: LocalLocation()}
        self.manifests: dict[str, Manifest] = {}
        self.transfer = TransferState()
        self._thread: threading.Thread | None = None

    # Locations

    def location(self, name: str) -> Location:
        if name not in self._locations:
            remote = self.remotes.remotes.get(name)
            if remote is None:
                raise KeyError(f"no remote named {name!r} in {self.remotes.path}")
            if remote.backend == "k8s":
                self._locations[name] = ShellLocation(remote.name, remote.id, self._helper(remote))
            elif remote.backend in SSH_BACKENDS:
                self._locations[name] = SshLocation(remote.name, remote.id, self._connect(remote))
            else:
                raise ValueError(f"remote {name}: backend {remote.backend} holds no files")
        return self._locations[name]

    def forget(self, name: str) -> None:
        """Drop a cached connection (after a remote's entry changed)."""
        self._locations.pop(name, None)

    def close(self) -> None:
        """Delete the helper pods this session started (they would also exit once idle)."""
        for loc in self._locations.values():
            if isinstance(loc, ShellLocation) and isinstance(loc.conn, HelperPod):
                loc.conn.close()

    def location_names(self) -> list[str]:
        return [LOCAL] + [n for n, r in sorted(self.remotes.remotes.items()) if r.backend in REMOTE_BACKENDS]

    def home(self, record: RunRecord) -> Endpoint:
        """Where the run ran. A remote is found by name, else by its stable id (renamed remotes)."""
        name = record.home.get("remote", LOCAL)
        directory = record.home.get("directory", f"runs/{record.name}")
        if name == LOCAL:
            return Endpoint(self.location(LOCAL), str((self.project.root / directory).resolve()))
        remote = self.remotes.remotes.get(name)
        rid = record.home.get("remote_id")
        if (remote is None or (rid and remote.id != rid)) and rid:
            remote = self.remotes.by_id(rid)
        if remote is None:
            raise KeyError(f"run {record.name}: on {name} (not one of your remotes)")
        return Endpoint(self.location(remote.name), directory)

    def copy_root(self, record: RunRecord, name: str) -> str | None:
        """Where copies of a run go at a location: runs/<run> here; <run_root>/<run> on a remote."""
        home = self.home(record)
        if home.location.name == name:
            return home.root
        if name == LOCAL:
            return str((self.project.root / "runs" / record.name).resolve())
        remote = self.remotes.remotes[name]
        root = (self.project.remotes.get(name) or {}).get("run_root") or remote.storage.get("run_root")
        return posixpath.join(str(root), record.name) if root else None

    def endpoint(self, record: RunRecord, name: str) -> Endpoint | None:
        root = self.copy_root(record, name)
        return Endpoint(self.location(name), root) if root else None

    # Manifests and states

    def refresh(self, record: RunRecord, names: list[str] | None = None) -> dict[str, str]:
        """Rebuild the run's manifest at its home and what each location holds; errors by location."""
        errors: dict[str, str] = {}
        home = self.home(record)
        manifest = build_manifest(home.location, home.root)
        self.cache.record_manifest(manifest)
        self.manifests[record.name] = manifest
        for name in names if names is not None else self.location_names():
            if name == home.location.name:
                continue
            try:
                ep = self.endpoint(record, name)
                if ep is not None:
                    self.cache.record_manifest(build_manifest(ep.location, ep.root))
            except (RemoteError, TransferError, OSError, ValueError) as e:
                errors[name] = str(e).splitlines()[0][:200]
        return errors

    def manifest(self, record: RunRecord) -> Manifest:
        if record.name not in self.manifests:
            self.refresh(record, names=[])
        return self.manifests[record.name]

    def states(self, record: RunRecord, names: list[str] | None = None) -> list[dict[str, Any]]:
        """Each file of the run with, per location, its state and how much of it is there."""
        manifest = self.manifest(record)
        names = names if names is not None else self.location_names()
        home = self.home(record)
        held: dict[str, Any] = {}
        pending: dict[str, dict[str, Any]] = {}
        for name in names:
            root = self.copy_root(record, name)
            if root is None or name == home.location.name:
                continue
            loc = self.location(name)
            held[name] = (root, self.cache.copies_under(loc.id, root))
            pending[name] = {p.path: p for p in self.cache.pending(loc.id)}
        rows = []
        for e in manifest.entries():
            row: dict[str, Any] = {**e.to_dict(), "at": {}}
            for name in names:
                if name == home.location.name:
                    row["at"][name] = {"state": HOME, "have": e.size}
                elif name not in held:
                    row["at"][name] = {"state": "", "have": 0}
                else:
                    root, copies = held[name]
                    row["at"][name] = _state(e, copies.get(e.path), pending[name].get(posixpath.join(root, e.path)))
            rows.append(row)
        return rows

    # Selections

    def select(self, record: RunRecord, what: str, **params: Any) -> list[FileEntry]:
        """Files by what users think in: a snapshot, a time range, probes, the newest restart, inputs, all."""
        entries = [e for e in self.manifest(record).entries() if e.kind != PARTIAL]
        snaps = [e for e in entries if e.category in ("snapshot", "series")]
        if what == "snapshot":
            step = params.get("step")
            if step is None:
                last = max((e.step or 0 for e in snaps), default=None)
                step = last
            return [e for e in snaps if e.step == step]
        if what == "snapshots":
            lo, hi = float(params.get("t_min", float("-inf"))), float(params.get("t_max", float("inf")))
            return [e for e in snaps if e.t is not None and lo <= e.t <= hi]
        if what == "probes":
            return [e for e in entries if e.category in ("probe", "log")]
        if what == "restart":
            restarts = [e for e in entries if e.category == "restart"]
            newest = max(restarts, key=lambda e: (e.step or 0, e.path), default=None)
            chosen = params.get("path")
            return [e for e in restarts if e.path == chosen] if chosen else [newest] if newest else []
        if what == "inputs":
            return [e for e in entries if e.category == "input"]
        if what == "run":
            return [e for e in entries if e.kind != LIVE]
        raise ValueError(f"selection {what!r}: one of {', '.join(SELECTIONS)}")

    # Transfers

    def plan(
        self, record: RunRecord, entries: list[FileEntry], dst: str, route: str = "auto", probe: bool = True
    ) -> Plan:
        destination = self.endpoint(record, dst)
        if destination is None:
            raise ValueError(f"{dst}: no run root to copy into (set run_root for {dst} in Remotes)")
        home = self.home(record)
        sources = [home] + [
            ep
            for n in self.location_names()
            if n not in (home.location.name, dst) and (ep := self._safe_endpoint(record, n)) is not None
        ]
        src = home.location
        if route == "auto":
            route = self.choose_route(src.name, dst, probe)
        peer = ""
        if route == "direct":
            peer = self.reach(src.name, dst).get("host", "")
        elif route == "pull":
            peer = self.reach(dst, src.name).get("host", "")
        return self.engine.plan(entries, sources, destination, route, peer)

    def _safe_endpoint(self, record: RunRecord, name: str) -> Endpoint | None:
        try:
            return self.endpoint(record, name)
        except (KeyError, ValueError):
            return None

    # Routes between remotes

    def reach(self, src: str, dst: str) -> dict[str, Any]:
        """What the last probe found from `src` to `dst`: `ssh` (src reaches dst with the forwarded agent, as
        `host`), `sees` (dst's storage is mounted at src at the same path), `checked` (its date); {} if never."""
        a, b = self.remotes.remotes.get(src), self.remotes.remotes.get(dst)
        return dict(a.reach.get(b.id, {})) if a and b else {}

    def needs_probe(self, src: str, dst: str) -> bool:
        """Whether two SSH remotes have never been probed against each other."""
        a, b = self.location(src), self.location(dst)
        both = isinstance(a, SshLocation) and isinstance(b, SshLocation)
        return both and (not self.reach(src, dst) or not self.reach(dst, src))

    def choose_route(self, src: str, dst: str, probe: bool = True) -> str:
        """The cheapest route from `src` to `dst`, probing two SSH remotes the first time they meet (unless
        `probe` is false: then the funnel, until a probe ran)."""
        a, b = self.location(src), self.location(dst)
        if not (isinstance(a, SshLocation) and isinstance(b, SshLocation)):
            return default_route(a, b)
        if probe and self.needs_probe(src, dst):
            self.probe_routes(src, dst)
        return default_route(
            a,
            b,
            direct=bool(self.reach(src, dst).get("ssh")),
            pull=bool(self.reach(dst, src).get("ssh")),
            shared=bool(self.reach(dst, src).get("sees")),
        )

    def probe_routes(self, a: str, b: str) -> dict[str, dict[str, Any]]:
        """Probe two SSH remotes both ways: `ssh -A x ssh y true`, and a marker file written in one's storage
        root looked for at the other. Kept in remotes.toml with the date; rerun when a direct transfer fails."""
        import datetime

        la, lb = self.location(a), self.location(b)
        ra, rb = self.remotes.remotes[a], self.remotes.remotes[b]
        if not (isinstance(la, SshLocation) and isinstance(lb, SshLocation)):
            raise ValueError(f"{a} and {b}: routes are probed between two SSH remotes")
        today = datetime.date.today().isoformat()
        found: dict[str, dict[str, Any]] = {}
        for (x, lx, rx), (_, ly, ry) in (((a, la, ra), (b, lb, rb)), ((b, lb, rb), (a, la, ra))):
            entry = {"ssh": lx.can_reach(ry.host), "host": ry.host, "sees": self._sees(lx, ly, ry), "checked": today}
            rx.reach[ry.id] = entry
            found[x] = entry
        self.remotes.save()
        return found

    @staticmethod
    def _sees(here: SshLocation, there: SshLocation, remote: Remote) -> bool:
        """Whether `there`'s storage root is mounted `here` at the same path (a shared filesystem)."""
        root = remote.storage.get("run_root")
        if not root:
            return False
        import uuid

        marker = posixpath.join(root, f".mallard-gui-probe-{uuid.uuid4().hex}")
        try:
            there.conn.write_file(marker, b"mallard-gui route probe\n")
        except (RemoteError, OSError):
            return False
        try:
            return here.has_file(marker)
        finally:
            there.remove([marker])

    def start(self, plan: Plan, done: Callable[[], None] | None = None) -> None:
        """Run a plan on a worker thread; `transfer` holds its progress."""
        if self.transfer.running:
            raise RuntimeError("a transfer is running")
        state = TransferState(running=True, total=plan.bytes, plan=plan)
        self.transfer = state

        def progress(n: int, total: int, current: str) -> None:
            state.done, state.total, state.current = n, total, current

        def work() -> None:
            try:
                state.report = self.engine.run(plan, progress, state.cancel)
                if state.report.errors and plan.route in REMOTE_ROUTES and plan.items:
                    # A direct route that failed may be gone: ask again before the next transfer.
                    self.probe_routes(plan.items[0].source.location.name, plan.destination.location.name)
            except (RemoteError, TransferError, OSError, ValueError) as e:
                state.error = str(e)
            finally:
                state.running = False
                if done:
                    done()

        self._thread = threading.Thread(target=work, name="mallard-gui-transfer", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self.transfer.cancel.set()

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)


def _state(e: FileEntry, copy: Any, pending: Any) -> dict[str, Any]:
    if copy is not None and copy.sha256 == e.sha256 and copy.size == e.size:
        return {"state": PRESENT, "have": e.size}
    if e.kind == STREAM and copy is not None and copy.size < e.size:
        return {"state": PREFIX, "have": copy.size}
    if pending is not None and pending.sha256 == e.sha256:
        return {"state": PARTIAL_STATE, "have": pending.done}
    if copy is not None and e.kind in (OBJECT, DERIVED, STREAM):
        return {"state": DIFFERS, "have": copy.size}
    return {"state": ABSENT, "have": 0}
