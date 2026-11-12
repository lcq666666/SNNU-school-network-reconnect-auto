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
$incoming = $target + '.incoming'
$replaced = $false
$started = $null
try {
    $stream = [IO.File]::OpenRead($staged)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
    } finally {
        $stream.Dispose()
        $hasher.Dispose()
    }
    if ($digest -ne $taskConfig.sha256) {
        throw 'Staged update SHA256 mismatch'
    }
    $old = Get-Process -Id $taskConfig.pid -ErrorAction SilentlyContinue
    if ($old) { Wait-Process -Id $old.Id -Timeout 60 -ErrorAction SilentlyContinue }
    Copy-Item -LiteralPath $staged -Destination $incoming -Force
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
    $arguments = @('--background', '--update-receipt', ('"' + $taskConfig.receipt + '"'))
    $started = Start-Process -FilePath $target -ArgumentList $arguments -PassThru -WindowStyle Hidden
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        if (Test-Path -LiteralPath $taskConfig.receipt) {
            $receipt = Get-Content -LiteralPath $taskConfig.receipt -Raw | ConvertFrom-Json
            if ($receipt.version -eq $taskConfig.version) { exit 0 }
        }
        Start-Sleep -Seconds 1
    }
    throw 'Updated application startup was not confirmed'
} catch {
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
            exit 1
        }
    }
    Start-Process -FilePath $target -ArgumentList '--background' -WindowStyle Hidden
    exit 1
}
'''


def launch_installer(staged: Path, target: Path, version: str, directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
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
