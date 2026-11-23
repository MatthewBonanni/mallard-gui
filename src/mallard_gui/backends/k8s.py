"""Runs on Kubernetes: the user's kubeconfig context and namespace, a helper pod for every remote command, and a
`Job` per run, all on the remote's PersistentVolumeClaim.

Kubernetes has no login node, so commands (listing, hashing, reading the run log, writing control files, the
transfer engine's streams) run in a helper pod of the Mallard image with the volume mounted, created on demand
and kept while the GUI uses it. It exits on its own after an idle period and at its `activeDeadlineSeconds`,
whether or not the GUI is still open. Nothing cluster-wide is installed; everything runs with what the user's
kubeconfig allows.

A remote of this backend names its kube context as `host`, the Mallard image as its install's `path`, and its
`k8s` settings: `namespace`, `pvc`, `mount` (where the volume is mounted, default /data), `prefix` of the GUI's
object names, `node_selector` ("key=value,key=value"), `run_as_user`, `idle` and `deadline` seconds of the helper
pod. Job resources come from its defaults: `cpu`, `memory`, `gpu`, `gpu_resource`, `time`.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import PurePosixPath
from typing import Any

from ..config import Remote
from .base import Backend, Job, JobState, RunSpec, check_request
from .ssh import RemoteError, Result, Runner, subprocess_runner
from .staging import run_input

MANAGED_BY = "mallard-gui"
LABEL = "app.kubernetes.io/managed-by"
K8S_DEFAULTS = {"mount": "/data", "prefix": "mallard-gui", "idle": "120", "deadline": "14400"}
ACTIVE_FILE = "/tmp/.mallard-gui-active"
JOB_FILE = "job.json"
_DNS = re.compile(r"[^a-z0-9-]+")


def k8s_settings(remote: Remote) -> dict[str, str]:
    s = {**K8S_DEFAULTS, **remote.k8s}
    for key in ("namespace", "pvc"):
        if not s.get(key):
            raise ValueError(f"remote {remote.name}: k8s.{key} is not set")
    return s


def dns_name(*parts: str, limit: int = 63) -> str:
    """A DNS-1123 label from `parts`, shortened with a hash when too long."""
    name = _DNS.sub("-", "-".join(parts).lower()).strip("-")
    if len(name) > limit:
        digest = hashlib.sha256(name.encode()).hexdigest()[:6]
        name = name[: limit - 7].rstrip("-") + "-" + digest
    return name


def node_selector(text: str) -> dict[str, str]:
    """`key=value,key=value` -> a nodeSelector."""
    out = {}
    for part in text.split(","):
        key, sep, value = part.strip().partition("=")
        if key:
            if not sep:
                raise ValueError(f"node selector {part!r}: key=value")
            out[key.strip()] = value.strip()
    return out


class KubeConnection:
    """`kubectl --context CONTEXT -n NAMESPACE ...`, the user's own kubeconfig and credentials."""

    def __init__(
        self, context: str, namespace: str, runner: Runner = subprocess_runner, kubectl: str | None = None
    ) -> None:
        if not context or context.startswith("-"):
            raise ValueError(f"context {context!r}: a context of your kubeconfig")
        if not namespace or namespace.startswith("-"):
            raise ValueError(f"namespace {namespace!r}")
        self.context = context
        self.namespace = namespace
        self.runner = runner
        # $MALLARD_GUI_KUBECTL names another client (the tests' stand-in).
        self.kubectl = kubectl or os.environ.get("MALLARD_GUI_KUBECTL") or "kubectl"

    def args(self, argv: Sequence[str]) -> list[str]:
        return [self.kubectl, "--context", self.context, "-n", self.namespace, *argv]

    def run(self, argv: Sequence[str], stdin: bytes | None = None, check: bool = True) -> Result:
        result = self.runner(self.args(argv), stdin)
        if check and result.returncode != 0:
            err = result.stderr.decode("utf-8", "replace").strip()
            raise RemoteError(f"kubectl {shlex.join(argv)}: exit {result.returncode}: {err}")
        return result

    def get(self, kind: str, name: str = "", selector: str = "") -> dict[str, Any] | None:
        """An object (None if it does not exist), or a list of them by label selector."""
        argv = ["get", kind, *([name] if name else []), *(["-l", selector] if selector else []), "-o", "json"]
        result = self.run(argv, check=False)
        if result.returncode != 0:
            if b"NotFound" in result.stderr or b"not found" in result.stderr:
                return None
            raise RemoteError(f"kubectl get {kind} {name}: {result.stderr.decode('utf-8', 'replace').strip()}")
        data: dict[str, Any] = json.loads(result.stdout)
        return data

    def apply(self, manifest: dict[str, Any]) -> None:
        self.run(["apply", "-f", "-"], stdin=json.dumps(manifest).encode())

    def delete(self, kind: str, name: str) -> None:
        self.run(["delete", kind, name, "--ignore-not-found", "--wait=false"], check=False)

    def can_i(self, verb: str, resource: str) -> bool:
        return self.run(["auth", "can-i", verb, resource], check=False).text.strip() == "yes"

    def popen(
        self, argv: Sequence[str], stdin: int | None = None, stdout: int | None = None
    ) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            self.args(argv),
            stdin=stdin if stdin is not None else subprocess.DEVNULL,
            stdout=stdout if stdout is not None else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )


def security_context(s: dict[str, str]) -> dict[str, Any]:
    """The restricted profile: what a namespace with Pod Security "restricted" (or OpenShift's) admits."""
    ctx: dict[str, Any] = {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "runAsNonRoot": True,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    if s.get("run_as_user"):
        ctx["runAsUser"] = int(s["run_as_user"])
    return ctx


def pod_spec(
    remote: Remote,
    container: dict[str, Any],
    deadline: int | None = None,
    restart: str = "Never",
) -> dict[str, Any]:
    """A pod spec with the remote's volume mounted, its node selector and the restricted security profile."""
    s = k8s_settings(remote)
    container = {
        "securityContext": security_context(s),
        "volumeMounts": [{"name": "data", "mountPath": s["mount"]}],
        **container,
    }
    spec: dict[str, Any] = {
        "restartPolicy": restart,
        "containers": [container],
        "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": s["pvc"]}}],
        "enableServiceLinks": False,
        "automountServiceAccountToken": False,
    }
    if deadline:
        spec["activeDeadlineSeconds"] = deadline
    if s.get("node_selector"):
        spec["nodeSelector"] = node_selector(s["node_selector"])
    return spec


def labels(remote: Remote, role: str) -> dict[str, str]:
    return {LABEL: MANAGED_BY, "mallard-gui/role": role, "mallard-gui/remote": dns_name(remote.name)}


def image_of(remote: Remote) -> str:
    if not remote.installs or not remote.installs[0].path:
        raise ValueError(f"remote {remote.name}: no Mallard image recorded (its install's path)")
    return remote.installs[0].path


def pod_ready(pod: dict[str, Any] | None) -> bool:
    if not pod or pod.get("status", {}).get("phase") != "Running":
        return False
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in pod["status"].get("conditions", []))


_STUCK = ("ErrImagePull", "ImagePullBackOff", "CreateContainerConfigError", "InvalidImageName")


def pod_problem(pod: dict[str, Any] | None) -> str:
    """Why a pod is not starting (an image that cannot be pulled, a bad configuration), or ''."""
    if not pod:
        return ""
    status = pod.get("status", {})
    if status.get("phase") in ("Failed", "Succeeded"):
        why = status.get("reason") or status.get("message", "")
        return f"pod {pod['metadata']['name']} {status['phase'].lower()}: {why}"
    for c in status.get("containerStatuses", []):
        waiting = c.get("state", {}).get("waiting") or {}
        if waiting.get("reason") in (
            "ErrImagePull",
            "ImagePullBackOff",
            "CreateContainerConfigError",
            "InvalidImageName",
        ):
            return f"{waiting['reason']}: {waiting.get('message', '')}"
    return ""


def wait_ready(
    conn: KubeConnection,
    name: str,
    timeout: float,
    on_wait: Callable[[float], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    start = time.monotonic()
    while True:
        pod = conn.get("pod", name)
        if pod_ready(pod):
            assert pod is not None
            return pod
        problem = pod_problem(pod)
        if problem:
            raise RemoteError(problem)
        waited = time.monotonic() - start
        if waited > timeout:
            phase = pod.get("status", {}).get("phase") if pod else "missing"
            raise RemoteError(f"pod {name}: not ready after {timeout:.0f} s ({phase})")
        if on_wait:
            on_wait(waited)
        sleep(1.0)


# The helper's main process: alive while some command touched the active file within the idle period.
_HELPER_MAIN = f"""
touch {ACTIVE_FILE}
trap 'exit 0' TERM
while [ $(( $(date +%s) - $(stat -c %Y {ACTIVE_FILE}) )) -lt "$1" ]; do sleep 5 & wait $!; done
"""

# Every command run in the helper keeps it active while it runs (a long transfer included).
_EXEC = f"""
touch {ACTIVE_FILE}
( while sleep 20; do touch {ACTIVE_FILE}; done ) </dev/null >/dev/null 2>&1 &
keep=$!
"$@"
status=$?
kill $keep 2>/dev/null
exit $status
"""


class HelperPod:
    """The helper pod: the shell of a Kubernetes location (run and popen, as an SSH connection has)."""

    def __init__(
        self,
        remote: Remote,
        conn: KubeConnection | None = None,
        start_timeout: float = 300.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        s = k8s_settings(remote)
        self.remote = remote
        self.settings = s
        self.conn = conn or KubeConnection(remote.host, s["namespace"])
        self.name = dns_name(s["prefix"], "helper", remote.name)
        self.start_timeout = start_timeout
        self._sleep = sleep
        self._ready = False

    def manifest(self) -> dict[str, Any]:
        s = self.settings
        container = {
            "name": "helper",
            "image": image_of(self.remote),
            "command": ["sh", "-c", _HELPER_MAIN, "sh", s["idle"]],
            "workingDir": s["mount"],
            "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "512Mi"}},
        }
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": self.name, "labels": labels(self.remote, "helper")},
            "spec": pod_spec(self.remote, container, deadline=int(s["deadline"])),
        }

    def ensure(self, on_wait: Callable[[float], None] | None = None) -> None:
        """Start the helper unless it runs; replace one that ended (idle or at its deadline)."""
        if self._ready:
            return
        pod = self.conn.get("pod", self.name)
        if pod is not None and pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            self.conn.run(["delete", "pod", self.name, "--ignore-not-found", "--wait=true", "--timeout=60s"])
            pod = None
        if pod is None:
            self.conn.apply(self.manifest())
        wait_ready(self.conn, self.name, self.start_timeout, on_wait, self._sleep)
        self._ready = True

    def _exec_args(self, argv: Sequence[str], interactive: bool) -> list[str]:
        return [
            "exec",
            *(["-i"] if interactive else []),
            self.name,
            "-c",
            "helper",
            "--",
            "sh",
            "-c",
            _EXEC,
            "sh",
            *argv,
        ]

    def run(self, argv: Sequence[str], stdin: bytes | None = None, check: bool = True) -> Result:
        self.ensure()
        result = self.conn.run(self._exec_args(argv, stdin is not None), stdin=stdin, check=False)
        if result.returncode != 0 and _gone(result.stderr):
            # The helper exited idle between two commands: start another and try once more.
            self._ready = False
            self.ensure()
            result = self.conn.run(self._exec_args(argv, stdin is not None), stdin=stdin, check=False)
        if check and result.returncode != 0:
            err = result.stderr.decode("utf-8", "replace").strip()
            raise RemoteError(f"{self.remote.name}: {shlex.join(argv)}: exit {result.returncode}: {err}")
        return result

    def popen(
        self, argv: Sequence[str], stdin: int | None = None, stdout: int | None = None, forward_agent: bool = False
    ) -> subprocess.Popen[bytes]:
        if forward_agent:
            raise ValueError("Kubernetes pods get no forwarded SSH agent")
        self.ensure()
        return self.conn.popen(self._exec_args(argv, stdin is not None), stdin=stdin, stdout=stdout)

    def write_file(self, path: str, data: bytes) -> None:
        self.run(["sh", "-c", 'mkdir -p "$(dirname "$1")" && cat > "$1"', "sh", path], stdin=data)

    def close(self) -> None:
        """Delete the helper (it would also exit on its own once idle)."""
        self.conn.delete("pod", self.name)
        self._ready = False


def _gone(stderr: bytes) -> bool:
    text = stderr.decode("utf-8", "replace")
    return "NotFound" in text or "not found" in text or "container not found" in text or "is not running" in text


_JOB_STATES = {"Complete": JobState.COMPLETED, "Failed": JobState.FAILED, "SuccessCriteriaMet": JobState.COMPLETED}


def seconds_of(time_limit: str) -> int | None:
    """`[D-]HH:MM:SS`, `MM:SS` or seconds -> seconds."""
    if not time_limit:
        return None
    days = 0
    if "-" in time_limit:
        d, _, time_limit = time_limit.partition("-")
        days = int(d)
    parts = [int(p) for p in time_limit.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts[-3:]
    return ((days * 24 + h) * 60 + m) * 60 + sec


class K8sBackend(Backend):
    kind = "k8s"

    def __init__(self, remote: Remote, conn: KubeConnection | None = None, helper: HelperPod | None = None) -> None:
        if remote.backend != "k8s":
            raise ValueError(f"remote {remote.name}: backend {remote.backend}, not k8s")
        self.remote = remote
        self.settings = k8s_settings(remote)
        self.image = image_of(remote)
        self.conn = conn or KubeConnection(remote.host, self.settings["namespace"])
        self.helper = helper or HelperPod(remote, self.conn)

    def job_manifest(self, spec: RunSpec, name: str) -> dict[str, Any]:
        o = {**self.remote.defaults, **spec.options}
        cpu = o.get("cpu") or str(max(1, spec.threads) * max(1, spec.ranks))
        memory = o.get("memory") or "2Gi"
        resources: dict[str, dict[str, str]] = {
            "requests": {"cpu": cpu, "memory": memory},
            "limits": {"memory": memory},
        }
        if o.get("gpu"):
            gpu = o.get("gpu_resource") or "nvidia.com/gpu"
            resources["requests"][gpu] = resources["limits"][gpu] = o["gpu"]
        mallard = self.settings.get("mallard", "mallard")
        cmd = [mallard, "-i", "input.toml", f"--kokkos-num-threads={spec.threads}"]
        if spec.restart:
            cmd += ["--restart", spec.restart]
        if spec.ranks > 1:
            cmd = ["mpirun", "-n", str(spec.ranks), *cmd]
        container = {
            "name": "mallard",
            "image": self.image,
            "command": cmd,
            "workingDir": spec.run_dir,
            "env": [{"name": "OMP_PROC_BIND", "value": "spread"}, {"name": "OMP_PLACES", "value": "threads"}],
            "resources": resources,
        }
        meta_labels = {**labels(self.remote, "run"), "mallard-gui/run": dns_name(spec.name)}
        template_spec = pod_spec(self.remote, container)
        job_spec: dict[str, Any] = {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 7 * 86400,
            "template": {"metadata": {"labels": meta_labels}, "spec": template_spec},
        }
        deadline = seconds_of(o.get("time", ""))
        if deadline:
            job_spec["activeDeadlineSeconds"] = deadline
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": name,
                "labels": meta_labels,
                "annotations": {"mallard-gui/run-dir": spec.run_dir, "mallard-gui/run-name": spec.name},
            },
            "spec": job_spec,
        }

    def submit(self, spec: RunSpec) -> Job:
        run_dir = PurePosixPath(spec.run_dir)
        if not run_dir.is_absolute():
            raise ValueError(f"run directory {spec.run_dir!r}: absolute on the volume")
        mount = PurePosixPath(self.settings["mount"])
        if mount not in run_dir.parents:
            raise ValueError(f"run directory {spec.run_dir}: not under the volume's mount {mount}")
        if not spec.resume:
            input_text = run_input(spec.input_file, absolute_paths=False)
            self.helper.write_file(str(run_dir / "input.toml"), input_text.encode())
        name = dns_name(self.settings["prefix"], spec.name, os.urandom(2).hex())
        manifest = self.job_manifest(spec, name)
        # Written into the run directory, so the job is visible and can be resubmitted without the GUI.
        self.helper.write_file(str(run_dir / JOB_FILE), json.dumps(manifest, indent=2).encode() + b"\n")
        self.conn.apply(manifest)
        return Job(id=name, run_dir=str(run_dir), backend=self.kind)

    def state(self, job: Job) -> JobState:
        data = self.conn.get("job", job.id)
        if data is None:
            return JobState.UNKNOWN
        return job_state(data, self._pods(job.id))

    def _pods(self, name: str) -> list[dict[str, Any]]:
        listing = self.conn.get("pods", selector=f"job-name={name}")
        return list(listing.get("items", [])) if listing else []

    def queue(self) -> list[dict[str, str]]:
        """The GUI's jobs in the namespace: id, name, state, time, reason (as the Slurm queue's rows)."""
        listing = self.conn.get("jobs", selector=f"{LABEL}={MANAGED_BY}")
        rows = []
        for item in (listing or {}).get("items", []):
            st = job_state(item, [])
            status = item.get("status", {})
            rows.append(
                {
                    "id": item["metadata"]["name"],
                    "name": item["metadata"].get("annotations", {}).get("mallard-gui/run-name", ""),
                    "state": st.value.upper(),
                    "time": elapsed(status.get("startTime", ""), status.get("completionTime", "")),
                    "reason": next((c.get("message", "") for c in status.get("conditions", [])), ""),
                }
            )
        return rows

    def read_file(self, job: Job, path: str, offset: int) -> bytes:
        full = str(PurePosixPath(job.run_dir) / path)
        script = 'test -f "$1" && tail -c +"$2" "$1" || true'
        return self.helper.run(["sh", "-c", script, "sh", full, str(offset + 1)]).stdout

    def control(self, job: Job, request: str) -> None:
        self.helper.run(["touch", str(PurePosixPath(job.run_dir) / check_request(request))])

    def cancel(self, job: Job) -> None:
        self.conn.run(["delete", "job", job.id, "--ignore-not-found", "--wait=false", "--cascade=background"])

    def logs(self, job: Job, tail: int = 200) -> str:
        """The container's output (`kubectl logs`): what the run printed, the run log being on the volume."""
        return self.conn.run(["logs", f"job/{job.id}", f"--tail={tail}"], check=False).text

    def close(self) -> None:
        self.helper.close()


def elapsed(start: str, end: str = "", now: datetime.datetime | None = None) -> str:
    """A Job's run time as `[d-]h:mm:ss` from its RFC 3339 start and end (or now), as Slurm shows it."""
    if not start:
        return ""
    t0 = datetime.datetime.fromisoformat(start)
    t1 = datetime.datetime.fromisoformat(end) if end else now or datetime.datetime.now(datetime.UTC)
    s = max(0, int((t1 - t0).total_seconds()))
    d, s = divmod(s, 86400)
    text = f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{d}-{text}" if d else text


def job_state(job: dict[str, Any], pods: list[dict[str, Any]]) -> JobState:
    status = job.get("status", {})
    for c in status.get("conditions", []):
        if c.get("status") == "True" and c.get("type") in _JOB_STATES:
            return _JOB_STATES[c["type"]]
    if status.get("succeeded"):
        return JobState.COMPLETED
    if status.get("failed"):
        return JobState.FAILED
    if any(p.get("status", {}).get("phase") == "Running" for p in pods):
        return JobState.RUNNING
    if status.get("active") and not pods:
        return JobState.RUNNING if status.get("ready") else JobState.PENDING
    return JobState.PENDING


def probe_k8s(remote: Remote, conn: KubeConnection | None = None) -> dict[str, Any]:
    """The connection test of a Kubernetes remote: the context answers, what the user may do in the namespace,
    the volume, and the GPU types of the nodes (from their labels, where the user may list nodes)."""
    s = k8s_settings(remote)
    conn = conn or KubeConnection(remote.host, s["namespace"])
    t0 = time.monotonic()
    try:
        conn.run(["get", "pvc", s["pvc"], "-o", "name"])
    except RemoteError as e:
        return {"ok": False, "error": str(e), "connect_s": time.monotonic() - t0}
    connect_s = time.monotonic() - t0
    can = {
        what: conn.can_i(verb, resource)
        for what, (verb, resource) in {
            "jobs": ("create", "jobs"),
            "pods": ("create", "pods"),
            "exec": ("create", "pods/exec"),
            "port-forward": ("create", "pods/portforward"),
        }.items()
    }
    gpus: dict[str, int] = {}
    with contextlib.suppress(RemoteError, json.JSONDecodeError):
        nodes = conn.run(["get", "nodes", "-o", "json"], check=False)
        if nodes.returncode == 0:
            for node in json.loads(nodes.stdout).get("items", []):
                product = node["metadata"].get("labels", {}).get("nvidia.com/gpu.product")
                if product:
                    gpus[product] = gpus.get(product, 0) + 1
    return {"ok": all(can.values()), "connect_s": connect_s, "can": can, "gpus": gpus, "error": ""}


__all__ = ["HelperPod", "K8sBackend", "KubeConnection", "job_state", "k8s_settings", "pod_spec", "probe_k8s"]
