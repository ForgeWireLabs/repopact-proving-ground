$ErrorActionPreference = 'Stop'

function Get-SidValue([object]$identityReference) {
    try { return $identityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value }
    catch { return [string]$identityReference }
}

function Get-AclSnapshot([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force
    $acl = Get-Acl -LiteralPath $Path
    $ownerSid = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
    $sddl = $acl.GetSecurityDescriptorSddlForm([System.Security.AccessControl.AccessControlSections]::All)
    $rules = @(
        $acl.Access | ForEach-Object {
            [pscustomobject]@{
                sid = Get-SidValue $_.IdentityReference
                access = [string]$_.FileSystemRights
                control = [string]$_.AccessControlType
                is_inherited = [bool]$_.IsInherited
                inheritance = [string]$_.InheritanceFlags
                propagation = [string]$_.PropagationFlags
            }
        }
    )
    $integritySddl = $null
    if ($sddl -match 'S:(.*)$') { $integritySddl = $Matches[1] }
    [pscustomobject]@{
        path = $Path
        kind = if ($item.PSIsContainer) { 'directory' } else { 'file' }
        attributes = [string]$item.Attributes
        is_reparse_point = [bool](($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)
        owner_sid = $ownerSid
        are_access_rules_protected = [bool]$acl.AreAccessRulesProtected
        are_access_rules_canonical = [bool]$acl.AreAccessRulesCanonical
        integrity_sddl = $integritySddl
        sddl = $sddl
        access = $rules
    }
}

function Get-IdentitySnapshot {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [System.Security.Principal.WindowsPrincipal]::new($identity)
    $account = $null
    try { $account = $identity.User.Translate([System.Security.Principal.NTAccount]).Value } catch {}
    [pscustomobject]@{
        principal_sid = $identity.User.Value
        principal_account = $account
        is_elevated = $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
        integrity_and_groups = (& whoami /groups 2>&1 | Out-String).Trim()
        groups = @(
            $identity.Groups | ForEach-Object {
                $sid = $_.Value
                $name = $null
                try { $name = $_.Translate([System.Security.Principal.NTAccount]).Value } catch {}
                [pscustomobject]@{ sid = $sid; account = $name }
            }
        )
        cwd = (Get-Location).Path
        temp = $env:TEMP
        tmp = $env:TMP
        user_profile = $env:USERPROFILE
        volume = (Get-Location).Drive.Root
    }
}

function Invoke-SandboxAction([string]$Action, [string]$Path, [string]$Root, [string]$ErrorPath) {
    $actionLiteral = $Action | ConvertTo-Json -Compress
    $pathLiteral = $Path | ConvertTo-Json -Compress
    $rootLiteral = $Root | ConvertTo-Json -Compress
    $command = @'
$ErrorActionPreference = 'Stop'
$Action = __ACTION__
$Path = __PATH__
$Root = __ROOT__
if ($Path -notlike "$Root\*") { throw 'path escapes expected root' }
if ($Action -eq 'create') {
    Set-Content -LiteralPath $Path -Value "repopact-sandbox-$Action" -NoNewline
} else {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw 'host seed is missing' }
    Add-Content -LiteralPath $Path -Value '|sandbox-modified' -NoNewline
}
$hash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
[pscustomobject]@{
    action = $Action
    identity = ((whoami /user 2>&1 | Out-String).Trim())
    path_inside_root = $true
    exists = (Test-Path -LiteralPath $Path -PathType Leaf)
    length = (Get-Item -LiteralPath $Path -Force).Length
    sha256 = $hash
} | ConvertTo-Json -Compress
'@
    $command = $command.Replace('__ACTION__', $actionLiteral).Replace('__PATH__', $pathLiteral).Replace('__ROOT__', $rootLiteral)
    $encodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    $oldPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $stdout = (& codex sandbox -- powershell.exe -NoProfile -NonInteractive -EncodedCommand $encodedCommand 2> $ErrorPath | Out-String).Trim()
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $oldPreference
    }
    $stderr = if (Test-Path -LiteralPath $ErrorPath) { (Get-Content -LiteralPath $ErrorPath -Raw).Trim() } else { '' }
    $payload = $null
    if ($stdout) {
        try { $payload = $stdout | ConvertFrom-Json } catch {}
    }
    [pscustomobject]@{
        action = $Action
        exit_code = $exitCode
        payload = $payload
        stdout = $stdout
        stderr = $stderr
    }
}

$repoRoot = (Get-Location).Path
$runId = [guid]::NewGuid().ToString('N')
$repoMatrixBase = Join-Path $repoRoot '.wi022-acl-matrix'
$tempBase = [System.IO.Path]::GetTempPath().TrimEnd('\')
$locations = @(
    [pscustomobject]@{ label = 'A_python_default_temp'; kind = 'python_default_temp'; parent = $tempBase; rootName = "repopact-wi022-acl-$runId-A" }
    [pscustomobject]@{ label = 'B_explicit_percent_TEMP'; kind = 'explicit_percent_TEMP'; parent = $env:TEMP; rootName = "repopact-wi022-acl-$runId-B" }
    [pscustomobject]@{ label = 'C_dedicated_repo_project_volume'; kind = 'dedicated_repo_project_volume'; parent = $repoMatrixBase; rootName = 'C' }
    [pscustomobject]@{ label = 'D_dedicated_benchmark_workspace_candidate'; kind = 'dedicated_benchmark_workspace_candidate'; parent = $repoMatrixBase; rootName = 'D' }
)
$results = @()
$errorPath = Join-Path $repoMatrixBase "sandbox-$runId.err.txt"
New-Item -ItemType Directory -Path $repoMatrixBase -Force | Out-Null
try {
    foreach ($location in $locations) {
        $root = Join-Path $location.parent $location.rootName
        $result = [ordered]@{
            label = $location.label
            kind = $location.kind
            base = $location.parent
            root = $root
            root_before = $null
            host_seed_before = $null
            sandbox_modify = $null
            host_read_after_modify = $null
            host_seed_after_modify = $null
            sandbox_create = $null
            host_read_sandbox_created = $null
            sandbox_created_after = $null
            error = $null
            cleanup_error = $null
        }
        try {
            New-Item -ItemType Directory -Path $root -Force | Out-Null
            $result.root_before = Get-AclSnapshot $root
            $hostSeed = Join-Path $root 'host-seed.txt'
            $sandboxCreated = Join-Path $root 'sandbox-created.txt'
            [System.IO.File]::WriteAllText($hostSeed, 'repopact-host-seed', [System.Text.Encoding]::UTF8)
            $result.host_seed_before = Get-AclSnapshot $hostSeed
            $modify = Invoke-SandboxAction 'modify' $hostSeed $root $errorPath
            $result.sandbox_modify = $modify
            $readAfterModify = $null
            try { $readAfterModify = [System.IO.File]::ReadAllText($hostSeed, [System.Text.Encoding]::UTF8) } catch { $readAfterModify = "READ_ERROR:$($_.Exception.GetType().FullName):$($_.Exception.Message)" }
            $result.host_read_after_modify = $readAfterModify
            $result.host_seed_after_modify = Get-AclSnapshot $hostSeed
            $create = Invoke-SandboxAction 'create' $sandboxCreated $root $errorPath
            $result.sandbox_create = $create
            $readSandboxCreated = $null
            try { $readSandboxCreated = [System.IO.File]::ReadAllText($sandboxCreated, [System.Text.Encoding]::UTF8) } catch { $readSandboxCreated = "READ_ERROR:$($_.Exception.GetType().FullName):$($_.Exception.Message)" }
            $result.host_read_sandbox_created = $readSandboxCreated
            if (Test-Path -LiteralPath $sandboxCreated) { $result.sandbox_created_after = Get-AclSnapshot $sandboxCreated }
        } catch {
            $result.error = "ERROR:$($_.Exception.GetType().FullName):$($_.Exception.Message)"
        } finally {
            try {
                if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction Stop }
            } catch { $result.cleanup_error = "CLEANUP_ERROR:$($_.Exception.GetType().FullName):$($_.Exception.Message)" }
        }
        $results += [pscustomobject]$result
    }
} finally {
    try { if (Test-Path -LiteralPath $errorPath) { Remove-Item -LiteralPath $errorPath -Force -ErrorAction SilentlyContinue } } catch {}
    try { if (Test-Path -LiteralPath $repoMatrixBase) { Remove-Item -LiteralPath $repoMatrixBase -Recurse -Force -ErrorAction SilentlyContinue } } catch {}
}
[pscustomobject]@{
    cli_syntax = 'codex sandbox -- <command>'
    generated_at_utc = [DateTime]::UtcNow.ToString('o')
    host = Get-IdentitySnapshot
    locations = $results
} | ConvertTo-Json -Depth 20
