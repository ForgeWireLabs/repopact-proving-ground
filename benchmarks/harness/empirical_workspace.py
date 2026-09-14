"""Secure disposable workspaces for live empirical model turns.

Windows Codex sandbox commands run under a restricted user token.  The token
does not reliably exercise inherited/group-only file ACEs, so live empirical
workspaces use a disposable, protected DACL with an explicit ACE for the
sandbox user SID and the host user SID.  This module never changes a global
ACL and never invokes a model.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


WORKSPACE_SECURITY_VERSION = "repopact.windows-empirical-workspace.v1"
SANDBOX_COMMAND = "codex sandbox -- <command>"
_SID_RE = re.compile(r"S-\d-\d+(?:-\d+)+")
_SYSTEM_SID = "S-1-5-18"
_ADMINISTRATORS_SID = "S-1-5-32-544"


class EmpiricalWorkspaceError(RuntimeError):
    """A live empirical workspace failed its security contract."""


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_windows() -> bool:
    return sys.platform == "win32"


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _run_checked(command: list[str], *, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EmpiricalWorkspaceError(f"workspace security command failed: {exc}") from exc
    return result


def _extract_sid(output: str) -> str:
    matches = _SID_RE.findall(output)
    if not matches:
        raise EmpiricalWorkspaceError("workspace security command did not expose a SID")
    return matches[-1]


def _codex_executable() -> str:
    candidate = shutil.which("codex")
    if candidate:
        return candidate
    raise EmpiricalWorkspaceError("codex CLI is required for the Windows sandbox preflight")


def _powershell_executable() -> str:
    candidate = shutil.which("pwsh") or shutil.which("powershell")
    if candidate:
        return candidate
    raise EmpiricalWorkspaceError("PowerShell is required for Windows ACL validation")


def _host_sid() -> str:
    result = _run_checked(["whoami", "/user"])
    if result.returncode:
        raise EmpiricalWorkspaceError(f"host identity probe failed: {result.stderr.strip()}")
    return _extract_sid(result.stdout + result.stderr)


def _sandbox_sid() -> str:
    result = _run_checked([_codex_executable(), "sandbox", "--", "whoami", "/user"])
    if result.returncode:
        detail = (result.stdout + result.stderr).strip()
        raise EmpiricalWorkspaceError(f"direct sandbox identity probe failed: {detail}")
    return _extract_sid(result.stdout + result.stderr)


def _reject_reparse(path: Path) -> None:
    if path.is_symlink():
        raise EmpiricalWorkspaceError(f"workspace path is a symlink: {path}")
    if not _is_windows():
        return
    snapshot = _acl_snapshot(path)
    if snapshot.get("is_reparse_point") is True:
        raise EmpiricalWorkspaceError(f"workspace path is a reparse point: {path}")


def _absolute_without_resolving(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _reject_reparse_components(path: Path) -> None:
    """Reject symlink/junction components before any canonical resolution."""
    if not _is_windows():
        if path.is_symlink():
            raise EmpiricalWorkspaceError(f"workspace path is a symlink: {path}")
        return
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if not current.exists() and not current.is_symlink():
            continue
        _reject_reparse(current)


def _powershell_json(script: str, *, timeout: float = 30.0) -> dict[str, Any]:
    encoded = __import__("base64").b64encode(script.encode("utf-16-le")).decode("ascii")
    result = _run_checked(
        [_powershell_executable(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        timeout=timeout,
    )
    if result.returncode:
        raise EmpiricalWorkspaceError(f"PowerShell workspace security probe failed: {(result.stdout + result.stderr).strip()}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise EmpiricalWorkspaceError(f"PowerShell workspace security probe returned invalid JSON: {result.stdout!r}") from exc
    if not isinstance(value, dict):
        raise EmpiricalWorkspaceError("PowerShell workspace security probe returned a non-object")
    return value


def _acl_snapshot(path: Path) -> dict[str, Any]:
    if not _is_windows():
        mode = stat.S_IMODE(path.stat().st_mode)
        return {
            "path": str(path),
            "kind": "directory" if path.is_dir() else "file",
            "attributes": "",
            "is_reparse_point": path.is_symlink(),
            "owner_sid": None,
            "are_access_rules_protected": False,
            "are_access_rules_canonical": True,
            "integrity_sddl": None,
            "sddl": f"mode:{mode:o}",
            "access": [],
        }
    script = f"""
$ErrorActionPreference = 'Stop'
$path = {_ps_quote(str(path))}
$item = Get-Item -LiteralPath $path -Force
$acl = Get-Acl -LiteralPath $path
$ownerSid = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
$sddl = $acl.GetSecurityDescriptorSddlForm([System.Security.AccessControl.AccessControlSections]::All)
$rules = @($acl.Access | ForEach-Object {{
    $sid = $null
    try {{ $sid = $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value }} catch {{ $sid = [string]$_.IdentityReference }}
    [pscustomobject]@{{
        sid = $sid
        access = [string]$_.FileSystemRights
        control = [string]$_.AccessControlType
        is_inherited = [bool]$_.IsInherited
        inheritance = [string]$_.InheritanceFlags
        propagation = [string]$_.PropagationFlags
    }}
}})
$integrity = $null
if ($sddl -match 'S:(.*)$') {{ $integrity = $Matches[1] }}
[pscustomobject]@{{
    path = $path
    kind = if ($item.PSIsContainer) {{ 'directory' }} else {{ 'file' }}
    attributes = [string]$item.Attributes
    is_reparse_point = [bool](($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)
    owner_sid = $ownerSid
    are_access_rules_protected = [bool]$acl.AreAccessRulesProtected
    are_access_rules_canonical = [bool]$acl.AreAccessRulesCanonical
    integrity_sddl = $integrity
    sddl = $sddl
    access = $rules
}} | ConvertTo-Json -Depth 12 -Compress
"""
    return _powershell_json(script)


def _normalized_acl(snapshot: dict[str, Any]) -> dict[str, Any]:
    rules = snapshot.get("access") or []
    if isinstance(rules, dict):
        rules = [rules]
    normalized_rules = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        normalized_rules.append({
            "sid": rule.get("sid"),
            "access": rule.get("access"),
            "control": rule.get("control"),
            "is_inherited": bool(rule.get("is_inherited")),
            "inheritance": rule.get("inheritance"),
            "propagation": rule.get("propagation"),
        })
    normalized_rules.sort(key=lambda item: json.dumps(item, sort_keys=True))
    return {
        "owner_sid": snapshot.get("owner_sid"),
        "are_access_rules_protected": bool(snapshot.get("are_access_rules_protected")),
        "are_access_rules_canonical": bool(snapshot.get("are_access_rules_canonical")),
        "integrity_sddl": snapshot.get("integrity_sddl"),
        "access": normalized_rules,
    }


def _acl_digest(snapshot: dict[str, Any]) -> str:
    return _digest(_normalized_acl(snapshot))


def _apply_windows_acl(path: Path, host_sid: str, sandbox_sid: str, *, recursive: bool) -> None:
    if not _is_windows():
        return
    if recursive:
        items = "@(Get-Item -LiteralPath $path -Force) + @(Get-ChildItem -LiteralPath $path -Force -Recurse)"
    else:
        items = "@(Get-Item -LiteralPath $path -Force)"
    script = f"""
$ErrorActionPreference = 'Stop'
$path = {_ps_quote(str(path))}
$hostSid = [System.Security.Principal.SecurityIdentifier]::new({_ps_quote(host_sid)})
$sandboxSid = [System.Security.Principal.SecurityIdentifier]::new({_ps_quote(sandbox_sid)})
$systemSid = [System.Security.Principal.SecurityIdentifier]::new({_ps_quote(_SYSTEM_SID)})
$administratorsSid = [System.Security.Principal.SecurityIdentifier]::new({_ps_quote(_ADMINISTRATORS_SID)})
$items = {items}
foreach ($item in $items) {{
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {{ throw \"reparse point in empirical workspace\" }}
    $acl = Get-Acl -LiteralPath $item.FullName
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($existing in @($acl.Access)) {{ [void]$acl.RemoveAccessRule($existing) }}
    $inheritance = [System.Security.AccessControl.InheritanceFlags]::None
    if ($item.PSIsContainer) {{
        $inheritance = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
    }}
    foreach ($sid in @($systemSid, $administratorsSid, $hostSid, $sandboxSid)) {{
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $sid,
            [System.Security.AccessControl.FileSystemRights]::FullControl,
            $inheritance,
            [System.Security.AccessControl.PropagationFlags]::None,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        [void]$acl.AddAccessRule($rule)
    }}
    Set-Acl -LiteralPath $item.FullName -AclObject $acl
}}
[pscustomobject]@{{ ok = $true }} | ConvertTo-Json -Compress
"""
    _powershell_json(script, timeout=120.0)


def _validate_acl(path: Path, host_sid: str, sandbox_sid: str) -> dict[str, Any]:
    snapshot = _acl_snapshot(path)
    if snapshot.get("is_reparse_point") is True:
        raise EmpiricalWorkspaceError("empirical workspace ACL validation rejected a reparse point")
    rules = snapshot.get("access") or []
    if isinstance(rules, dict):
        rules = [rules]
    deny_rules = [rule for rule in rules if isinstance(rule, dict) and rule.get("control") == "Deny"]
    if deny_rules:
        raise EmpiricalWorkspaceError("empirical workspace ACL validation rejected a deny ACE")
    required = {host_sid, sandbox_sid}
    allowed = {
        str(rule.get("sid"))
        for rule in rules
        if isinstance(rule, dict)
        and rule.get("control") == "Allow"
        and "FullControl" in str(rule.get("access"))
    }
    if not required.issubset(allowed):
        missing = sorted(required - allowed)
        raise EmpiricalWorkspaceError(f"empirical workspace ACL is missing explicit full-control principals: {missing}")
    normalized = _normalized_acl(snapshot)
    return {
        "owner_sid": snapshot.get("owner_sid"),
        "dacl_protected": bool(snapshot.get("are_access_rules_protected")),
        "dacl_canonical": bool(snapshot.get("are_access_rules_canonical")),
        "deny_ace_count": len(deny_rules),
        "allow_principal_roles": [
            {"role": "host_user", "sid": host_sid},
            {"role": "codex_sandbox_user", "sid": sandbox_sid},
        ],
        "normalized_acl_digest": _digest(normalized),
    }


def _sandbox_echo(path: Path, value: str) -> None:
    path_literal = str(path).replace("'", "''")
    value_literal = value.replace("'", "''")
    command = f"Set-Content -LiteralPath '{path_literal}' -Value '{value_literal}' -NoNewline -Encoding utf8"
    result = _run_checked(
        [_codex_executable(), "sandbox", "--", "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        timeout=60.0,
    )
    if result.returncode:
        detail = (result.stdout + result.stderr).strip()
        raise EmpiricalWorkspaceError(f"direct sandbox file probe failed: {detail}")


def _host_exact_read(path: Path, expected: bytes) -> None:
    try:
        actual = path.read_bytes()
    except OSError as exc:
        raise EmpiricalWorkspaceError(f"host could not read empirical workspace probe: {exc}") from exc
    if actual != expected:
        raise EmpiricalWorkspaceError("host empirical workspace probe returned unexpected bytes")


def _preflight(path: Path, host_sid: str, sandbox_sid: str) -> dict[str, Any]:
    probe_id = uuid.uuid4().hex
    host_probe = path / f".repopact-host-probe-{probe_id}.txt"
    sandbox_probe = path / f".repopact-sandbox-probe-{probe_id}.txt"
    host_payload = b"repopact-host-probe-v1\r\n"
    sandbox_payload = b"\xef\xbb\xbf" + b"repopact-sandbox-probe-v1"
    modified_payload = b"\xef\xbb\xbf" + b"repopact-sandbox-modified-v1"
    checks = {
        "host_write_seed": False,
        "host_read_seed": False,
        "sandbox_create_file": False,
        "host_read_sandbox_file": False,
        "sandbox_modify_host_file": False,
        "host_read_modified_file": False,
    }
    try:
        host_probe.write_bytes(host_payload)
        checks["host_write_seed"] = True
        _host_exact_read(host_probe, host_payload)
        checks["host_read_seed"] = True
        _sandbox_echo(sandbox_probe, "repopact-sandbox-probe-v1")
        checks["sandbox_create_file"] = True
        _host_exact_read(sandbox_probe, sandbox_payload)
        checks["host_read_sandbox_file"] = True
        _sandbox_echo(host_probe, "repopact-sandbox-modified-v1")
        checks["sandbox_modify_host_file"] = True
        _host_exact_read(host_probe, modified_payload)
        checks["host_read_modified_file"] = True
    finally:
        for probe in (host_probe, sandbox_probe):
            try:
                probe.unlink(missing_ok=True)
            except OSError as exc:
                raise EmpiricalWorkspaceError(f"empirical workspace probe cleanup failed: {exc}") from exc
    if not all(checks.values()):
        raise EmpiricalWorkspaceError(f"empirical workspace preflight failed: {checks}")
    return checks


def _configured_root(repo_root: str | Path | None, configured_root: str | Path | None = None) -> Path:
    configured = os.environ.get("REPOPACT_BENCH_WORK_ROOT") or (str(configured_root) if configured_root is not None else None)
    if configured:
        root = Path(configured)
        if not root.is_absolute():
            raise EmpiricalWorkspaceError("REPOPACT_BENCH_WORK_ROOT must be absolute")
    else:
        base = Path(repo_root) if repo_root is not None else Path.cwd()
        root = base.resolve() / ".repopact-bench-workspaces"
    root = _absolute_without_resolving(root)
    if root == Path(root.anchor):
        raise EmpiricalWorkspaceError("empirical work root may not be a filesystem root")
    root.mkdir(parents=True, exist_ok=True)
    _reject_reparse_components(root)
    return root.resolve()


def _inferred_root_for(path: Path) -> Path | None:
    """Find the default allocator root without depending on the caller's CWD."""
    candidate = _absolute_without_resolving(path)
    for ancestor in (candidate, *candidate.parents):
        if ancestor.name == ".repopact-bench-workspaces":
            return ancestor
    return None


def _inside(candidate: Path, root: Path) -> bool:
    candidate = _absolute_without_resolving(candidate)
    root = _absolute_without_resolving(root)
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _security_record(path: Path, *, root: Path, host_sid: str | None, sandbox_sid: str | None, preflight: dict[str, Any], acl: dict[str, Any]) -> dict[str, Any]:
    core = {
        "version": WORKSPACE_SECURITY_VERSION,
        "sandbox_mode": SANDBOX_COMMAND if _is_windows() else "not-applicable",
        "host_principal": {"role": "host_user", "sid": host_sid},
        "sandbox_principal": {"role": "codex_sandbox_user", "sid": sandbox_sid},
        "acl": acl,
        "preflight": preflight,
        "scope": "configured empirical work root",
    }
    return {**core, "fingerprint": _digest(core)}


def _validate_and_probe(path: Path, root: Path, host_sid: str | None, sandbox_sid: str | None) -> dict[str, Any]:
    _reject_reparse(path)
    if not path.is_dir():
        raise EmpiricalWorkspaceError("empirical workspace is not a directory")
    if _is_windows():
        assert host_sid is not None and sandbox_sid is not None
        acl = _validate_acl(path, host_sid, sandbox_sid)
        preflight = _preflight(path, host_sid, sandbox_sid)
    else:
        acl = {
            "owner_sid": None,
            "dacl_protected": False,
            "dacl_canonical": True,
            "deny_ace_count": 0,
            "allow_principal_roles": [],
            "normalized_acl_digest": _acl_digest(_acl_snapshot(path)),
        }
        probe = path / f".repopact-host-probe-{uuid.uuid4().hex}.txt"
        expected = b"repopact-host-probe-v1\n"
        probe.write_bytes(expected)
        _host_exact_read(probe, expected)
        probe.unlink()
        preflight = {"host_write_seed": True, "host_read_seed": True}
    return _security_record(path, root=root, host_sid=host_sid, sandbox_sid=sandbox_sid, preflight=preflight, acl=acl)


@dataclass
class EmpiricalWorkspace:
    """One isolated workspace whose security contract was proven pre-inference."""

    path: Path
    configured_root: Path
    security: dict[str, Any]
    _closed: bool = False

    @classmethod
    def allocate(cls, *, repo_root: str | Path | None = None, prefix: str = "empirical") -> "EmpiricalWorkspace":
        root = _configured_root(repo_root)
        host_sid = _host_sid() if _is_windows() else None
        sandbox_sid = _sandbox_sid() if _is_windows() else None
        path = root / f"{prefix}-{uuid.uuid4().hex}"
        if path.exists():
            raise EmpiricalWorkspaceError("generated empirical workspace path already exists")
        path.mkdir()
        try:
            if _is_windows():
                assert host_sid is not None and sandbox_sid is not None
                _apply_windows_acl(path, host_sid, sandbox_sid, recursive=False)
            security = _validate_and_probe(path, root, host_sid, sandbox_sid)
        except Exception:
            shutil.rmtree(path, ignore_errors=True)
            raise
        return cls(path=path, configured_root=root, security=security)

    @classmethod
    def validate_for_inference(cls, path: str | Path, *, repo_root: str | Path | None = None, configured_root: str | Path | None = None) -> dict[str, Any]:
        inferred = None if os.environ.get("REPOPACT_BENCH_WORK_ROOT") or repo_root is not None or configured_root is not None else _inferred_root_for(Path(path))
        root = inferred or _configured_root(repo_root, configured_root)
        candidate = _absolute_without_resolving(path)
        if not _inside(candidate, root):
            raise EmpiricalWorkspaceError("empirical workspace is outside the configured empirical work root")
        _reject_reparse_components(candidate)
        candidate = candidate.resolve()
        host_sid = _host_sid() if _is_windows() else None
        sandbox_sid = _sandbox_sid() if _is_windows() else None
        return _validate_and_probe(candidate, root, host_sid, sandbox_sid)

    @classmethod
    def prepare_existing(cls, path: str | Path, *, repo_root: str | Path | None = None) -> Path:
        root = _configured_root(repo_root)
        candidate = _absolute_without_resolving(path)
        if not _inside(candidate, root):
            raise EmpiricalWorkspaceError("existing empirical workspace is outside the configured empirical work root")
        _reject_reparse_components(candidate)
        candidate = candidate.resolve()
        _reject_reparse(candidate)
        if not candidate.is_dir():
            raise EmpiricalWorkspaceError("existing empirical workspace is not a directory")
        if _is_windows():
            _apply_windows_acl(candidate, _host_sid(), _sandbox_sid(), recursive=True)
        return candidate

    def child(self, name: str) -> Path:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 1:
            raise EmpiricalWorkspaceError("empirical workspace child name must be one safe relative path component")
        child = self.path / relative
        if not _inside(child, self.path):
            raise EmpiricalWorkspaceError("empirical workspace child escapes its root")
        return child

    def prepare_existing_child(self, path: str | Path) -> Path:
        """Apply the same ACL profile to an already-copied child workspace."""
        candidate = _absolute_without_resolving(path)
        if not _inside(candidate, self.path):
            raise EmpiricalWorkspaceError("existing child workspace escapes its allocator root")
        _reject_reparse_components(candidate)
        candidate = candidate.resolve()
        _reject_reparse(candidate)
        if not candidate.is_dir():
            raise EmpiricalWorkspaceError("existing child workspace is not a directory")
        if _is_windows():
            _apply_windows_acl(candidate, _host_sid(), _sandbox_sid(), recursive=True)
        return candidate

    def close(self) -> None:
        if self._closed:
            return
        if not _inside(self.path, self.configured_root) or self.path == self.configured_root:
            raise EmpiricalWorkspaceError("refusing to clean an invalid empirical workspace path")
        try:
            shutil.rmtree(self.path)
        except OSError as exc:
            raise EmpiricalWorkspaceError(f"empirical workspace cleanup failed: {exc}") from exc
        self._closed = True

    def __enter__(self) -> "EmpiricalWorkspace":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
