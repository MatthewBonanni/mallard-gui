"""PBS and LSF: adapters of the SSH backend that differ from Slurm only in the job script, submission, the queue
and cancelling. Staging, the run log, control files and transfers are the SSH backend's.

PBS covers PBS Pro and OpenPBS (`qsub`, `qstat -x -f`, `qdel`); LSF covers IBM Spectrum LSF (`bsub`, `bjobs`,
`bkill`). A remote's defaults name the queue as `partition` (or `queue`), the project as `account`, and the
walltime as `time` (`HH:MM:SS`), as for Slurm.
"""

from __future__ import annotations

import re
import shlex

from .base import Job, JobState, RunSpec
from .ssh import RemoteError, SlurmBackend


def _launch(spec: RunSpec, mallard: str, launcher: str) -> str:
    cmd = f"{shlex.quote(mallard)} -i input.toml --kokkos-num-threads={spec.threads}"
    if spec.restart:
        cmd += f" --restart {shlex.quote(spec.restart)}"
    return f"{launcher} -n {spec.ranks} {cmd}" if spec.ranks > 1 else cmd


def _queue_name(options: dict[str, str]) -> str:
    return options.get("queue") or options.get("partition") or ""


def pbs_script(spec: RunSpec, mallard: str, module: str = "") -> str:
    o = spec.options
    select = f"select=1:ncpus={spec.ranks * spec.threads}:mpiprocs={spec.ranks}:ompthreads={spec.threads}"
    if o.get("gpus"):
        select += f":ngpus={o['gpus']}"
    lines = [
        "#!/bin/bash",
        f"#PBS -N {_pbs_name(spec.name)}",
        f"#PBS -l {select}",
        "#PBS -j oe",
        f"#PBS -o {shlex.quote(spec.run_dir)}/stdout.txt",
    ]
    if o.get("time"):
        lines.append(f"#PBS -l walltime={o['time']}")
    if _queue_name(o):
        lines.append(f"#PBS -q {shlex.quote(_queue_name(o))}")
    if o.get("account"):
        lines.append(f"#PBS -A {shlex.quote(o['account'])}")
    lines += ["", "set -euo pipefail", f"cd {shlex.quote(spec.run_dir)}"]
    if module:
        lines.append(module)
    lines += ["export OMP_PROC_BIND=spread OMP_PLACES=threads", _launch(spec, mallard, "mpiexec"), ""]
    return "\n".join(lines)


def _pbs_name(name: str) -> str:
    """PBS job names: at most 15 characters (older servers), starting with a letter, no spaces."""
    clean = re.sub(r"[^A-Za-z0-9_.-]", "_", name) or "mallard"
    return (clean if clean[0].isalpha() else "m" + clean)[:15]


_PBS_STATES = {
    "Q": JobState.PENDING,
    "H": JobState.PENDING,
    "W": JobState.PENDING,
    "T": JobState.PENDING,
    "S": JobState.RUNNING,
    "U": JobState.RUNNING,
    "R": JobState.RUNNING,
    "E": JobState.RUNNING,
    "B": JobState.RUNNING,
    "X": JobState.COMPLETED,
}


def parse_qstat_f(text: str) -> dict[str, str]:
    """`qstat -x -f JOB` -> its attributes (continuation lines joined)."""
    attrs: dict[str, str] = {}
    key = ""
    for line in text.splitlines():
        if " = " in line and line.startswith("    ") and not line.startswith("\t"):
            key, _, value = line.strip().partition(" = ")
            attrs[key] = value
        elif key and line.startswith("\t"):
            attrs[key] += line.strip()
    return attrs


class PbsBackend(SlurmBackend):
    kind = "pbs"
    job_script_name = "run.pbs"

    def job_script(self, spec: RunSpec, mallard: str, module: str) -> str:
        return pbs_script(spec, mallard, module)

    def submit_script(self, path: str) -> str:
        out = self.conn.run(["qsub", path]).text.strip()
        if not re.fullmatch(r"\d+(\[\])?(\.[\w.-]+)?", out):
            raise RemoteError(f"qsub: unexpected reply {out!r}")
        return out

    def state(self, job: Job) -> JobState:
        result = self.conn.run(["qstat", "-x", "-f", job.id], check=False)
        attrs = parse_qstat_f(result.text)
        st = attrs.get("job_state", "")
        if st == "F":
            code = attrs.get("Exit_status", "")
            if code == "0":
                return JobState.COMPLETED
            # qdel'd jobs end with a negative exit status (-1, or 271 = 256 + SIGTERM on some servers).
            return JobState.CANCELLED if code in ("-1", "271") else JobState.FAILED
        return _PBS_STATES.get(st, JobState.UNKNOWN)

    def queue(self, user: str | None = None) -> list[dict[str, str]]:
        """`qstat -u USER`: Job ID, Username, Queue, Jobname, SessID, NDS, TSK, Memory, Time, S, Time."""
        argv = ["sh", "-c", 'qstat -u "${1:-$USER}"', "sh", user or ""]
        rows = []
        started = False
        for line in self.conn.run(argv, check=False).text.splitlines():
            if line.startswith("---"):
                started = True
                continue
            parts = line.split()
            if started and len(parts) >= 11:
                rows.append(
                    {"id": parts[0], "name": parts[3], "state": _pbs_word(parts[9]), "time": parts[10], "reason": ""}
                )
        return rows

    def cancel(self, job: Job) -> None:
        self.conn.run(["qdel", job.id])


def _pbs_word(code: str) -> str:
    st = _PBS_STATES.get(code)
    return st.value.upper() if st else code


def lsf_time(time_limit: str) -> str:
    """LSF's `-W [HH:]MM` from `[D-]HH:MM:SS` (seconds rounded up to a minute)."""
    days = 0
    if "-" in time_limit:
        d, _, time_limit = time_limit.partition("-")
        days = int(d)
    parts = [int(p) for p in time_limit.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts[-3:]
    minutes = (days * 24 + h) * 60 + m + (1 if s else 0)
    return f"{minutes // 60}:{minutes % 60:02d}"


def lsf_script(spec: RunSpec, mallard: str, module: str = "") -> str:
    o = spec.options
    lines = [
        "#!/bin/bash",
        f"#BSUB -J {shlex.quote(spec.name)}",
        f"#BSUB -n {spec.ranks}",
        f'#BSUB -R "affinity[core({spec.threads})]"',
        f"#BSUB -cwd {shlex.quote(spec.run_dir)}",
        "#BSUB -o stdout-%J.txt",
    ]
    if o.get("time"):
        lines.append(f"#BSUB -W {lsf_time(o['time'])}")
    if _queue_name(o):
        lines.append(f"#BSUB -q {shlex.quote(_queue_name(o))}")
    if o.get("account"):
        lines.append(f"#BSUB -P {shlex.quote(o['account'])}")
    if o.get("gpus"):
        lines.append(f'#BSUB -gpu "num={o["gpus"]}"')
    lines += ["", "set -euo pipefail"]
    if module:
        lines.append(module)
    lines += ["export OMP_PROC_BIND=spread OMP_PLACES=threads", _launch(spec, mallard, "mpirun"), ""]
    return "\n".join(lines)


_LSF_STATES = {
    "PEND": JobState.PENDING,
    "PSUSP": JobState.PENDING,
    "WAIT": JobState.PENDING,
    "PROV": JobState.PENDING,
    "RUN": JobState.RUNNING,
    "USUSP": JobState.RUNNING,
    "SSUSP": JobState.RUNNING,
    "DONE": JobState.COMPLETED,
    "EXIT": JobState.FAILED,
}
_BSUB_ID = re.compile(r"Job <(\d+)> is submitted")


class LsfBackend(SlurmBackend):
    kind = "lsf"
    job_script_name = "run.lsf"

    def job_script(self, spec: RunSpec, mallard: str, module: str) -> str:
        return lsf_script(spec, mallard, module)

    def submit_script(self, path: str) -> str:
        # bsub reads the #BSUB lines only from standard input.
        out = self.conn.run(["sh", "-c", 'bsub < "$1"', "sh", path]).text
        m = _BSUB_ID.search(out)
        if not m:
            raise RemoteError(f"bsub: unexpected reply {out.strip()!r}")
        return m.group(1)

    def state(self, job: Job) -> JobState:
        out = self.conn.run(["bjobs", "-noheader", "-o", "stat exit_code", job.id], check=False).text.split()
        if not out:
            return JobState.UNKNOWN
        st = _LSF_STATES.get(out[0], JobState.UNKNOWN)
        # bkill ends a job with EXIT and exit code 130 (SIGINT) or 137/143 (SIGKILL/SIGTERM).
        if st == JobState.FAILED and len(out) > 1 and out[1] in ("130", "137", "143"):
            return JobState.CANCELLED
        return st

    def queue(self, user: str | None = None) -> list[dict[str, str]]:
        argv = ["bjobs", "-noheader", "-o", "jobid job_name stat run_time pend_reason delimiter='|'"]
        if user:
            argv += ["-u", user]
        rows = []
        for line in self.conn.run(argv, check=False).text.splitlines():
            parts = line.split("|")
            if len(parts) == 5 and parts[0].strip().isdigit():
                rid, name, st, run_time, reason = (p.strip() for p in parts)
                state = _LSF_STATES.get(st)
                rows.append(
                    {
                        "id": rid,
                        "name": name,
                        "state": state.value.upper() if state else st,
                        "time": run_time.removesuffix(" second(s)"),
                        "reason": "" if reason == "-" else reason,
                    }
                )
        return rows

    def cancel(self, job: Job) -> None:
        self.conn.run(["bkill", job.id])


__all__ = ["LsfBackend", "PbsBackend", "lsf_script", "pbs_script"]
