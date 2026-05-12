"""
SSH helpers using subprocess.

Keeps things simple: no paramiko, no asyncio, just ssh(1).
BatchMode=yes means it will fail cleanly if keys aren't set up.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class SSHResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class SSHClient:
    def __init__(self, host: str, user: str, port: int = 22, identity_file: Optional[str] = None, shell_init: Optional[str] = None):
        self.host = host
        self.user = user
        self.port = port
        self.identity_file = identity_file
        self.shell_init = shell_init  # prepended to every command, e.g. "source /etc/profile"

    def _base_args(self) -> List[str]:
        args = [
            "ssh",
            "-p", str(self.port),
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new",
        ]
        if self.identity_file:
            args += ["-i", os.path.expanduser(self.identity_file)]
        args.append(f"{self.user}@{self.host}")
        return args

    def run(self, command: str, timeout: Optional[int] = 60) -> SSHResult:
        """Run a single shell command on the remote host."""
        if self.shell_init:
            command = f"{self.shell_init} && {command}"
        cmd = self._base_args() + [command]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return SSHResult(
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        except subprocess.TimeoutExpired:
            return SSHResult(exit_code=-1, stdout="", stderr=f"Command timed out after {timeout}s")
        except FileNotFoundError:
            return SSHResult(exit_code=-1, stdout="", stderr="ssh not found on PATH")

    def write_file(self, remote_path: str, content: str, timeout: int = 30) -> SSHResult:
        """Write content to a remote file by piping via stdin — avoids arg length limits."""
        cmd = self._base_args() + [f"cat > {remote_path}"]
        try:
            result = subprocess.run(
                cmd,
                input=content,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return SSHResult(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)
        except subprocess.TimeoutExpired:
            return SSHResult(exit_code=-1, stdout="", stderr=f"Command timed out after {timeout}s")

    def ping(self) -> bool:
        """Return True if we can reach the host and run a basic command."""
        result = self.run("echo pong", timeout=10)
        return result.ok and "pong" in result.stdout
