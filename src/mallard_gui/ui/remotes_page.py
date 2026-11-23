"""The remotes page: the global remotes.toml, a connection test with install discovery, Mallard installs
(`installs_panel`), and the user's jobs in each remote's queue."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from trame.widgets import html

from ..backends.batch import LsfBackend, PbsBackend
from ..backends.k8s import K8sBackend, probe_k8s
from ..backends.ssh import RemoteError, SshConnection, slurm_queue
from ..config import Install, Remote, RemoteStore
from ..remotes import Probe, probe
from .fmt import number
from .installs_panel import InstallsPanel
from .panes import list_splitter, list_style, splitter

if TYPE_CHECKING:
    from .app import App

SSH_BACKENDS = ("slurm", "pbs", "lsf")
BACKENDS = (*SSH_BACKENDS, "k8s")
JOB_KEYS = ("partition", "account", "time", "gres", "cpu", "memory", "gpu")
K8S_KEYS = ("namespace", "pvc", "mount", "prefix", "node_selector", "run_as_user")
FIELDS = ("name", "host", "backend", "run_root", "install_root", "image", *JOB_KEYS, *K8S_KEYS)
SSH_ONLY = "rm_backend !== 'k8s'"
QUEUE_COMMAND = {"slurm": "squeue --me", "pbs": "qstat -u $USER", "lsf": "bjobs", "k8s": "jobs of the GUI"}
FORM = "[{" + ", ".join(f"{k}: rm_{k}" for k in FIELDS) + "}]"


class RemotesPage:
    def __init__(self, app: App, connect: Callable[[Remote], SshConnection] | None = None) -> None:
        self.app = app
        self.state = app.state
        self._connect = connect or (lambda r: SshConnection(r.host))
        self._probes: dict[str, Probe] = {}
        self._kube: dict[str, dict[str, Any]] = {}
        s = self.state
        s.rm_list = []
        s.rm_sel = ""
        s.rm_new = False
        for f in FIELDS:
            s[f"rm_{f}"] = ""
        s.rm_backend = "slurm"
        s.rm_error = ""
        s.rm_probe = {}
        s.rm_testing = False
        s.rm_queue = []
        s.rm_queue_error = ""
        s.rm_queue_busy = False
        s.rm_path = ""
        self.installs = InstallsPanel(self)
        s.queue_cmd = QUEUE_COMMAND

    @property
    def store(self) -> RemoteStore:
        return self.app.session.data.remotes

    # Actions

    def open(self) -> None:
        self.state.rm_path = str(self.store.path).replace(str(Path.home()), "~", 1)
        self._list()
        if not self.state.rm_sel and not self.state.rm_new and self.state.rm_list:
            self.select(self.state.rm_list[0]["name"])

    def _list(self) -> None:
        self.state.rm_list = [
            {
                "name": r.name,
                "host": r.host,
                "backend": r.backend,
                "status": self._status(r.name),
            }
            for r in sorted(self.store.remotes.values(), key=lambda r: r.name)
        ]

    def _status(self, name: str) -> str:
        if name in self._kube:
            return "ok" if self._kube[name]["ok"] else "err"
        return ("ok" if self._probes[name].ok else "err") if name in self._probes else ""

    def select(self, name: str) -> None:
        r = self.store.remotes[name]
        s = self.state
        s.rm_sel, s.rm_new, s.rm_error = name, False, ""
        s.rm_name, s.rm_host, s.rm_backend = r.name, r.host, r.backend
        s.rm_run_root = r.storage.get("run_root", "")
        s.rm_install_root = r.storage.get("install_root", "")
        s.rm_image = r.installs[0].path if r.backend == "k8s" and r.installs else ""
        for key in JOB_KEYS:
            s[f"rm_{key}"] = r.defaults.get(key, "")
        for key in K8S_KEYS:
            s[f"rm_{key}"] = r.k8s.get(key, "")
        self._show_installs(r)
        self._show_probe(name)
        s.rm_queue, s.rm_queue_error = [], ""

    def new(self) -> None:
        s = self.state
        s.rm_sel, s.rm_new, s.rm_error = "", True, ""
        for f in FIELDS:
            s[f"rm_{f}"] = ""
        s.rm_backend = "slurm"
        s.rm_probe, s.rm_queue, s.rm_queue_error = {}, [], ""
        self.installs.show(None)

    def save(self, form: dict[str, str]) -> None:
        """`form` comes with the click, since the fields' own state can reach the server after it."""
        s = self.state
        old = self.store.remotes.get(s.rm_sel)
        f = {k: str(form.get(k) or "").strip() for k in FIELDS}
        kube = f["backend"] == "k8s"
        try:
            remote = Remote(
                name=f["name"],
                host=f["host"],
                backend=f["backend"] or "slurm",
                installs=[Install(f["image"])] if kube and f["image"] else list(old.installs) if old else [],
                defaults={k: f[k] for k in JOB_KEYS if f[k]},
                k8s={k: f[k] for k in K8S_KEYS if f[k]} if kube else {},
                storage={**(old.storage if old else {}), "run_root": f["run_root"], "install_root": f["install_root"]},
                reach=dict(old.reach) if old else {},
                viewer=dict(old.viewer) if old else {},
            )
            if not remote.name:
                raise ValueError("a remote needs a name")
            if kube and not (remote.k8s.get("namespace") and remote.k8s.get("pvc") and remote.installs):
                raise ValueError("a Kubernetes remote needs a namespace, a volume claim and an image")
            for key in ("run_root", "install_root"):
                if not remote.storage[key]:
                    del remote.storage[key]
            if old is None:
                self.store.add(remote)
            else:
                self.store.update(s.rm_sel, remote)
            self.store.save()
        except (ValueError, OSError) as e:
            s.rm_error = str(e)
            return
        if old is not None and old.name != remote.name:
            for found in (self._probes, self._kube):
                if old.name in found:
                    found[remote.name] = found.pop(old.name)  # type: ignore[assignment]
        self.app.session.data.forget(old.name if old else remote.name)
        self._list()
        self.select(remote.name)

    def revert(self) -> None:
        if self.state.rm_sel:
            self.select(self.state.rm_sel)
        else:
            self.new()

    def delete(self) -> None:
        name = self.state.rm_sel
        if not name:
            return
        self.store.remove(name)
        self.store.save()
        self._probes.pop(name, None)
        self.app.session.data.forget(name)
        self.state.rm_sel = ""
        self._list()
        if self.state.rm_list:
            self.select(self.state.rm_list[0]["name"])
        else:
            self.new()

    def test(self) -> None:
        if self.state.rm_sel and not self.state.rm_testing:
            self._schedule(self._test(self.state.rm_sel))

    def refresh_queue(self) -> None:
        if self.state.rm_sel and not self.state.rm_queue_busy:
            self._schedule(self._queue(self.state.rm_sel))

    def _schedule(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self.app._tasks.add(task)
        task.add_done_callback(self.app._tasks.discard)

    async def _test(self, name: str) -> None:
        remote = self.store.remotes[name]
        with self.state:
            self.state.rm_testing = True
        if remote.backend == "k8s":
            try:
                self._kube[name] = await asyncio.to_thread(probe_k8s, remote)
            except (ValueError, OSError) as e:
                self._kube[name] = {"ok": False, "error": str(e)}
        else:
            conn = self._connect(remote)
            self._probes[name] = await asyncio.to_thread(probe, conn, [i.path for i in remote.installs])
        with self.state:
            self.state.rm_testing = False
            self._list()
            if self.state.rm_sel == name:
                self._show_probe(name)
                self._show_installs(remote)

    async def _queue(self, name: str) -> None:
        remote = self.store.remotes[name]
        with self.state:
            self.state.rm_queue_busy = True
        rows: list[dict[str, str]] = []
        error = ""
        try:
            rows = await asyncio.to_thread(self._queue_rows, remote)
        except (RemoteError, ValueError, OSError) as e:
            error = str(e)
        with self.state:
            self.state.rm_queue_busy = False
            if self.state.rm_sel == name:
                self.state.rm_queue = rows
                self.state.rm_queue_error = error or ("" if rows else "no jobs of yours in the queue")

    def _queue_rows(self, remote: Remote) -> list[dict[str, str]]:
        if remote.backend == "k8s":
            return K8sBackend(remote).queue()
        if remote.backend == "slurm":
            return slurm_queue(self._connect(remote))
        kind = {"pbs": PbsBackend, "lsf": LsfBackend}[remote.backend]
        return kind(remote, self._connect(remote)).queue()

    def _show_installs(self, remote: Remote) -> None:
        self.installs.show(remote)

    def _show_probe(self, name: str) -> None:
        if name in self._kube:
            k = self._kube[name]
            can = k.get("can", {})
            self.state.rm_probe = {
                "ok": k["ok"],
                "kube": True,
                "error": k.get("error", "")
                or ("" if k["ok"] else "not allowed: " + ", ".join(w for w, ok in can.items() if not ok)),
                "connect": f"{number(1000 * k.get('connect_s', 0))} ms",
                "can": ", ".join(w for w, ok in can.items() if ok) or "–",
                "gpus": ", ".join(f"{g} ({n})" for g, n in sorted(k.get("gpus", {}).items())) or "–",
            }
            self.state.rm_install = {}
            return
        found = self._probes.get(name)
        if found is None:
            self.state.rm_probe = {}
            return
        d = found.to_dict()
        d["connect"] = f"{number(1000 * found.connect_s)} ms"
        d["latency"] = f"{number(1000 * found.latency_s)} ms"
        d["parts"] = ", ".join(p["name"] + (f" ({p['gres']})" if p["gres"] else "") for p in found.partitions) or "–"
        self.state.rm_probe = d

    # Layout

    def layout(self) -> None:
        with html.Div(classes="panel"):
            splitter("remotes-left", "the remote list", minimum=180, maximum=640)
            with html.Div(classes="panel-head"):
                html.Span("Remotes")
                html.Button(
                    v_html="icons.plus",
                    classes="icon-btn",
                    title="Add a remote",
                    click=self.new,
                    style="margin-left: auto",
                )
            with html.Div(classes="scroll"):
                with html.Div(
                    v_for="r in rm_list",
                    key="r.name",
                    classes=("['loc-row', 'rm-row', {sel: r.name === rm_sel}]",),
                    click=(self.select, "[r.name]"),
                ):
                    html.Span(v_html="icons.server", classes="ticon")
                    with html.Div(classes="loc-main"):
                        with html.Div():
                            html.Span("{{ r.name }}", classes="mono", style="font-weight: 600")
                            html.Span("{{ r.backend }}", classes="tag")
                        html.Div("{{ r.host }}", classes="loc-root mono")
                    html.Span(
                        classes=("['dot', r.status]",),
                        v_if="r.status",
                        title=("r.status === 'ok' ? 'reachable' : 'unreachable'",),
                    )
                with html.Div(v_if="!rm_list.length && !rm_new", classes="notice"):
                    html.Span("No remotes yet: Add one for each cluster you use, by its ~/.ssh/config alias.")
            with html.Div(classes="textbar mono rm-path"):
                html.Span("\u200e{{ rm_path }}\u200e", classes="muted", title=("rm_path",))
        with html.Div(classes="center"):
            with html.Div(classes="run-head"):
                html.Span(v_html="icons.server", classes="ticon")
                html.Span("{{ rm_new ? 'New remote' : rm_sel }}", classes="name")
                html.Span("{{ rm_host }}", classes="mono muted where", v_if="!rm_new")
                html.Div(classes="grow")
                with html.Button(
                    classes="btn",
                    click=self.test,
                    disabled=("rm_testing || !rm_sel",),
                    title="ssh to the host, time a round trip, and look for schedulers and Mallard installs",
                ):
                    html.Span(v_html="icons.plug")
                    html.Span("{{ rm_testing ? 'Testing ...' : 'Test connection' }}")
                with html.Button(
                    classes="btn danger", click=self.delete, disabled=("!rm_sel",), title="Remove from remotes.toml"
                ):
                    html.Span(v_html="icons.trash")
                    html.Span("Remove")
            with html.Div(classes="scroll"):
                with html.Div(classes="panel-head"):
                    html.Span("Connection")
                    html.Span(
                        "{{ rm_backend === 'k8s' ? 'kubeconfig context' : 'ssh alias' }}, no secrets stored",
                        classes="mono",
                    )
                self._field("name", "name", "hpc", req=True)
                with html.Div(classes="prow"):
                    html.Div("backend", classes="plabel")
                    with html.Div(classes="pval"), html.Select(v_model=("rm_backend",), aria_label="backend"):
                        for b in BACKENDS:
                            html.Option(b, value=b)
                self._field("host", "host", "an alias of ~/.ssh/config", req=True, v_if="rm_backend !== 'k8s'")
                self._field("host", "context", "a context of ~/.kube/config", req=True, v_if="rm_backend === 'k8s'")
                with html.Div(v_if="rm_backend === 'k8s'"):
                    self._field("namespace", "namespace", "your namespace", req=True)
                    self._field("pvc", "volume claim", "a ReadWriteOnce PVC in it", req=True)
                    self._field("mount", "mount", "/data")
                    self._field("image", "image", "ghcr.io/matthewbonanni/mallard:<version>-cpu-amd64", req=True)
                    self._field("prefix", "pod prefix", "mallard-gui")
                    self._field("node_selector", "node selector", "key=value,key=value")
                    self._field("run_as_user", "run as user", "the namespace's UID, where it needs one")
                self._field("run_root", "run root", "/scratch/$USER/mallard-runs (on the volume for k8s)")
                self._field(
                    "install_root",
                    "install root",
                    "your home: installs go to <root>/mallard/",
                    v_if="rm_backend !== 'k8s'",
                )
                with html.Div(classes="panel-head"):
                    html.Span("Job defaults")
                    html.Span(
                        "{{ rm_backend === 'k8s' ? 'resources of every Job' : 'directives of every job' }}",
                        classes="mono",
                    )
                with html.Div(v_if="rm_backend !== 'k8s'"):
                    self._field("partition", "partition", "the site's default", v_if="rm_backend === 'slurm'")
                    self._field("partition", "queue", "the site's default", v_if="rm_backend !== 'slurm'")
                    self._field("account", "account", "the site's default")
                    self._field("gres", "gres", "gpu:a100:1", v_if="rm_backend === 'slurm'")
                with html.Div(v_if="rm_backend === 'k8s'"):
                    self._field("cpu", "cpu", "1")
                    self._field("memory", "memory", "2Gi")
                    self._field("gpu", "gpu", "0")
                self._field("time", "time", "01:00:00")
                html.Div("{{ rm_error }}", classes="prop-foot", style="color: var(--m-danger)", v_if="rm_error")
                with html.Div(classes="actions"):
                    html.Div(classes="grow")
                    with html.Button(classes="btn", click=self.revert):
                        html.Span("Revert")
                    with html.Button(classes="btn primary", click=(self.save, FORM), title="Write remotes.toml"):
                        html.Span(v_html="icons.check")
                        html.Span("{{ rm_new ? 'Add remote' : 'Save' }}")
                self.installs.layout()
        with html.Div(classes="panel right"):
            splitter("remotes-right", "the connection panel", edge="start", minimum=240, maximum=760)
            with html.Div(classes="panel-head"):
                html.Span("Connection test")
                html.Span(
                    "{{ rm_probe.ok === undefined ? '' : rm_probe.ok ? 'reachable' : 'failed' }}",
                    classes=("rm_probe.ok ? 'mono okc' : 'mono err'",),
                )
            with html.Div(classes="scroll", style=list_style("remotes-probe", "55%")):
                html.Div(
                    "{{ rm_backend === 'k8s' ? 'Test connection: the context answers, the volume claim exists, and "
                    "what you may do in the namespace.' : 'Test connection: one ssh connection (MFA once), a round "
                    "trip, and a probe of the login node.' }}",
                    classes="notice",
                    v_if="rm_probe.ok === undefined",
                )
                html.Div("{{ rm_probe.error }}", classes="prop-foot mono err", v_if="rm_probe.error")
                with html.Div(v_if="rm_probe.kube && rm_probe.connect"):
                    for label, expr in (
                        ("connect", "rm_probe.connect"),
                        ("allowed", "rm_probe.can"),
                        ("GPU nodes", "rm_probe.gpus"),
                    ):
                        with html.Div(classes="prow"):
                            html.Div(label, classes="plabel")
                            html.Div(f"{{{{ {expr} }}}}", classes="pval ro", title=(expr,))
                with html.Div(v_if="rm_probe.ok && !rm_probe.kube"):
                    for label, expr in (
                        ("connect", "rm_probe.connect"),
                        ("round trip", "rm_probe.latency"),
                        ("system", "rm_probe.os + ' ' + rm_probe.arch"),
                        ("glibc", "rm_probe.glibc || '–'"),
                        ("scheduler", "rm_probe.scheduler || 'none found'"),
                        ("partitions", "rm_probe.parts"),
                        ("containers", "rm_probe.container || 'none'"),
                        ("modules", "rm_probe.tools.includes('module') ? 'yes' : 'no'"),
                        ("spack", "rm_probe.tools.includes('spack') ? 'yes' : 'no'"),
                        ("mallard", "rm_probe.candidates.length + ' found'"),
                    ):
                        with html.Div(classes="prow"):
                            html.Div(label, classes="plabel")
                            html.Div(f"{{{{ {expr} }}}}", classes="pval ro", title=(expr,))
            list_splitter("remotes-probe", "the connection test")
            with html.Div(classes="panel-head"):
                html.Span("Queue")
                html.Span("{{ queue_cmd[rm_backend] || '' }}", classes="mono")
                html.Button(
                    v_html="icons.refresh",
                    classes="icon-btn",
                    title="Refresh the queue",
                    click=self.refresh_queue,
                    disabled=("rm_queue_busy || !rm_sel",),
                    style="margin-left: auto",
                )
            with html.Div(classes="scroll"):
                with html.Table(classes="ftable queue", v_if="rm_queue.length"):
                    with html.Thead(), html.Tr():
                        html.Th("job")
                        html.Th("name", classes="q-name")
                        html.Th("state", classes="q-state")
                        html.Th("time", classes="num q-time")
                    with html.Tbody():
                        with html.Tr(v_for="j in rm_queue", key="j.id", title=("j.reason || j.id",)):
                            html.Td("{{ j.id }}", classes="mono")
                            html.Td("{{ j.name }}", classes="mono")
                            html.Td("{{ j.state.toLowerCase() }}", classes=("['mono', 'q-' + j.state.toLowerCase()]",))
                            html.Td("{{ j.time }}", classes="num mono muted")
                html.Div(
                    "{{ rm_queue_error || (rm_queue_busy ? 'reading the queue ...' : 'Refresh to read the queue.') }}",
                    classes="notice",
                    v_if="!rm_queue.length",
                )

    def _field(self, key: str, label: str, placeholder: str, req: bool = False, v_if: str | None = None) -> None:
        with html.Div(classes="prow", **({"v_if": v_if} if v_if else {})):
            html.Div(label, classes="plabel req" if req else "plabel")
            with html.Div(classes="pval"):
                html.Input(v_model=(f"rm_{key}",), placeholder=placeholder, aria_label=label, classes="mono")
