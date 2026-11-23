"""Mallard's own answers through a resident `mallard session` (Mallard's docs/guide/session.md): the build's
capabilities (`initialize`), the units and their exact factors (`units.catalog`), what a file holds and whether
this build continues it (`file.info`), the files a case reads (`case.files`) and each boundary's values at its
faces (`case.boundary_values`).

What a build offers is read from `initialize`'s `methods`, never from its version: a build without a request
(or without `mallard session` at all) makes `has` false, and the caller keeps its older way."""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .session_client import SessionClient, SessionError, method_names

# A session that does not start in this long is taken as absent (a build older than `mallard session`).
START_TIMEOUT = 30.0


class MallardService:
    """One `mallard session` per executable, started on first use; requests are serialized."""

    def __init__(self, argv: Sequence[str], cwd: Path, env: Mapping[str, str] | None = None) -> None:
        self.argv = list(argv)
        self.cwd = cwd
        self.env = dict(env) if env is not None else None
        self._client: SessionClient | None = None
        self._failed = ""
        self._lock = threading.RLock()
        self.capabilities: dict[str, Any] = {}
        self.methods: set[str] = set()
        self._case: tuple[str, str] | None = None  # the open case: input path and the text sent

    def _start(self) -> SessionClient | None:
        if self._client is not None or self._failed:
            return self._client
        try:
            client = SessionClient.start(self.argv, cwd=str(self.cwd), timeout=START_TIMEOUT, env=self.env)
        except (SessionError, OSError, ValueError) as e:
            self._failed = str(e) or "mallard session did not start"
            return None
        client.timeout = 300.0
        self._client = client
        self.capabilities = client.capabilities
        self.methods = method_names(client.capabilities)
        return client

    @property
    def available(self) -> bool:
        with self._lock:
            return self._start() is not None

    @property
    def failure(self) -> str:
        return self._failed

    def has(self, method: str) -> bool:
        with self._lock:
            return self._start() is not None and method in self.methods

    def request(self, method: str, params: Mapping[str, Any] | None = None, timeout: float | None = None) -> Any:
        with self._lock:
            client = self._start()
            if client is None:
                raise SessionError(f"no mallard session: {self._failed}")
            try:
                return client.request(method, dict(params or {}), timeout)
            except SessionError:
                if client.proc.poll() is not None:
                    self._client, self._case = None, None  # the next request starts it again
                raise

    def open_case(self, input_path: Path, text: str | None = None) -> None:
        """The case open in the session, with the editor's text when it differs from the file."""
        with self._lock:
            key = (str(input_path), text if text is not None else "")
            if self._case == key:
                return
            if self._case is not None and self._case[0] == key[0] and text is not None:
                self.request("case.update", {"text": text})
            else:
                params: dict[str, Any] = {"input": str(input_path)}
                if text is not None:
                    params["text"] = text
                self.request("case.open", params)
            self._case = key

    def case_request(self, input_path: Path, text: str | None, method: str, params: Mapping[str, Any]) -> Any:
        with self._lock:
            self.open_case(input_path, text)
            return self.request(method, params)

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
            self._client = None
            self._case = None


def session_env(data_dir: Path | None) -> dict[str, str]:
    env = dict(os.environ)
    if data_dir is not None and "MALLARD_DATA_DIR" not in env:
        env["MALLARD_DATA_DIR"] = str(data_dir)
    return env
