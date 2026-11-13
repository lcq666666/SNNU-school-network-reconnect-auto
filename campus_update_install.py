"""Out-of-process executable replacement, receipt verification and rollback."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess

from campus_install import atomic_json


UPDATE_SCRIPT = r'''
param([string]$Manifest)
$ErrorActionPreference = 'Stop'
$taskConfig = Get-Content -LiteralPath $Manifest -Raw | ConvertFrom-Json
$target = [IO.Path]::GetFullPath($taskConfig.target)
$staged = [IO.Path]::GetFullPath($taskConfig.staged)
$backup = $target + '.previous'
$incoming = $target + '.incoming.exe'
$startupReport = $taskConfig.receipt + '.preflight'
$updateLog = $taskConfig.error + '.log'
$replaced = $false
$started = $null
function Write-UpdateLog([string]$Message) {
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -LiteralPath $updateLog -Value ("$stamp $Message") -Encoding UTF8
}
function Get-TargetProcessIds {
    $ids = @(Get-CimInstance -ClassName Win32_Process | Where-Object {
        $_.ExecutablePath -and
        [IO.Path]::GetFullPath($_.ExecutablePath).Equals(
            $target, [StringComparison]::OrdinalIgnoreCase)
    } | ForEach-Object { [int]$_.ProcessId })
    return $ids
}
function Get-FileSha256([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        return [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
    } finally {
        $stream.Dispose()
        $hasher.Dispose()
    }
}
try {
    Remove-Item -LiteralPath $updateLog -Force -ErrorAction SilentlyContinue
    Write-UpdateLog 'Checking downloaded update checksum.'
    if ((Get-FileSha256 $staged) -ne $taskConfig.sha256) {
        throw 'Staged update SHA256 mismatch'
    }
    Remove-Item -LiteralPath $startupReport -Force -ErrorAction SilentlyContinue
    Write-UpdateLog 'Preflighting downloaded update.'
    $check = Start-Process -FilePath $staged `
        -ArgumentList @('--load-check', ('"' + $startupReport + '"')) `
        -Wait -PassThru -WindowStyle Hidden
    if ($check.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $startupReport) `
        -or (Get-Content -LiteralPath $startupReport -Raw).Trim() -ne 'ok') {
        throw 'Staged update startup check failed'
    }
    Remove-Item -LiteralPath $startupReport -Force -ErrorAction SilentlyContinue
    Write-UpdateLog 'Waiting for every old process at the target path to exit.'
    $deadline = (Get-Date).AddSeconds(60)
    do {
        $oldIds = @(Get-TargetProcessIds)
        if ($oldIds.Count -eq 0) { break }
        if ((Get-Date) -ge $deadline) {
            throw ('Old application processes did not exit: ' + ($oldIds -join ', '))
        }
        Start-Sleep -Milliseconds 500
    } while ($true)
    Write-UpdateLog 'Copying update beside the target executable.'
    Copy-Item -LiteralPath $staged -Destination $incoming -Force
    if ((Get-FileSha256 $incoming) -ne $taskConfig.sha256) {
        throw 'Copied update SHA256 mismatch'
    }
    Remove-Item -LiteralPath $startupReport -Force -ErrorAction SilentlyContinue
    Write-UpdateLog 'Preflighting the copied executable before replacement.'
    $check = Start-Process -FilePath $incoming `
        -ArgumentList @('--load-check', ('"' + $startupReport + '"')) `
        -Wait -PassThru -WindowStyle Hidden
    if ($check.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $startupReport) `
        -or (Get-Content -LiteralPath $startupReport -Raw).Trim() -ne 'ok') {
        throw ('Copied update startup check failed with exit code ' + $check.ExitCode)
    }
    Remove-Item -LiteralPath $startupReport -Force -ErrorAction SilentlyContinue
    $ready = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try {
            if (Test-Path -LiteralPath $target) {
                [IO.File]::Replace($incoming, $target, $backup)
            } else {
                Move-Item -LiteralPath $incoming -Destination $target
            }
            $ready = $true
            $replaced = $true
            break
        } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $ready) { throw 'Executable replacement timed out' }
    if ((Get-FileSha256 $target) -ne $taskConfig.sha256) {
        throw 'Replaced executable SHA256 mismatch'
    }
    Write-UpdateLog 'Replacement verified; starting updated application.'
    $arguments = @('--background', '--update-receipt', ('"' + $taskConfig.receipt + '"'))
    $started = Start-Process -FilePath $target -ArgumentList $arguments -PassThru -WindowStyle Hidden
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        if (Test-Path -LiteralPath $taskConfig.receipt) {
            $receipt = Get-Content -LiteralPath $taskConfig.receipt -Raw | ConvertFrom-Json
            if ($receipt.version -eq $taskConfig.version) {
                Write-UpdateLog ('Updated application startup confirmed; version ' + $receipt.version + '.')
                exit 0
            }
        }
        if ($started.HasExited) {
            throw ('Updated application exited before startup receipt; exit code ' + $started.ExitCode)
        }
        Start-Sleep -Seconds 1
    }
    throw 'Updated application startup was not confirmed'
} catch {
    Write-UpdateLog ('Update failed: ' + $_.Exception.Message)
    $_.Exception.Message | Out-File -LiteralPath $taskConfig.error -Encoding utf8
    if ($replaced -and (Test-Path -LiteralPath $backup)) {
        if ($started -and (-not $started.HasExited)) {
            $savedPreference = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            try { & taskkill.exe /PID $started.Id /T /F 2>$null | Out-Null }
            finally { $ErrorActionPreference = $savedPreference }
        }
        Start-Sleep -Seconds 2
        $restored = $false
        for ($attempt = 0; $attempt -lt 20; $attempt++) {
            try {
                Copy-Item -LiteralPath $backup -Destination $target -Force
                $restored = $true
                break
            } catch { Start-Sleep -Seconds 1 }
        }
        if (-not $restored) {
            'Rollback failed. The previous executable is preserved in the .previous file.' |
                Out-File -LiteralPath $taskConfig.error -Encoding utf8 -Append
            Write-UpdateLog 'Rollback failed; previous executable remains in the .previous file.'
            exit 1
        }
        Write-UpdateLog 'Previous executable restored after failed update.'
    }
    if (@(Get-TargetProcessIds).Count -eq 0) {
        Start-Process -FilePath $target -ArgumentList '--background' -WindowStyle Hidden
        Write-UpdateLog 'Started the restored application.'
    } else {
        Write-UpdateLog 'Restored application is already running.'
    }
    exit 1
}
'''


def preflight_staged_executable(staged: Path, directory: Path) -> None:
    """Verify the frozen executable loads before the current app exits."""
    directory.mkdir(parents=True, exist_ok=True)
    report = directory / "update-preflight.txt"
    report.unlink(missing_ok=True)
    try:
        result = subprocess.run(
            [str(staged), "--load-check", str(report)],
            check=False, timeout=30,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("更新文件启动自检失败：%s" % type(exc).__name__) from exc
    detail = report.read_text(encoding="utf-8").strip() if report.is_file() else ""
    report.unlink(missing_ok=True)
    if result.returncode != 0 or detail != "ok":
        raise RuntimeError("更新文件启动自检失败（退出码 %s）" % result.returncode)


def launch_installer(staged: Path, target: Path, version: str, directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    preflight_staged_executable(staged, directory)
    receipt = directory / "update-receipt.json"
    receipt.unlink(missing_ok=True)
    error = directory / "install-error.txt"
    error.unlink(missing_ok=True)
    manifest = directory / "install.json"
    digest = hashlib.sha256()
    with staged.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    atomic_json(manifest, {
        "staged": str(staged.resolve()), "target": str(target.resolve()),
        "pid": os.getpid(), "version": version.lstrip("vV"),
        "sha256": digest.hexdigest(),
        "receipt": str(receipt.resolve()), "error": str(error.resolve()),
    })
    script = directory / "install-update.ps1"
    script.write_text(UPDATE_SCRIPT, encoding="utf-8-sig")
    environment = os.environ.copy()
    environment.pop("PSModulePath", None)
    subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                      "Bypass", "-File", str(script), "-Manifest", str(manifest)],
                     env=environment,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True)


def launch_setup(setup: Path) -> None:
    """Start a verified Inno Setup package and let it replace the install."""
    setup = Path(setup).resolve()
    if not setup.is_file() or setup.suffix.lower() != ".exe":
        raise OSError("安装包文件不存在")
    subprocess.Popen(
        [str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True)
