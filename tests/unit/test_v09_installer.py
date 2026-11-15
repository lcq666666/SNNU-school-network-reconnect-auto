"""Static and offline checks for the standard Windows installer path."""
from pathlib import Path

import pytest


def test_inno_recipe_preserves_user_data_and_registers_startup(project_root):
    text = (project_root / "installer" / "CampusReconnect.iss").read_text(encoding="utf-8")
    assert "{localappdata}\\Programs\\CampusReconnect" in text
    assert "CampusReconnectSetup" in text
    assert "CampusReconnectTray" in text
    assert "UninstallDelete" not in text
    assert "Flags: nowait postinstall skipifsilent" not in text


def test_installer_uses_onedir_runtime_outside_temp_and_requires_windows_10(project_root):
    text = (project_root / "installer" / "CampusReconnect.iss").read_text(encoding="utf-8")
    assert "MinVersion=10.0" in text
    assert r"..\build\onedir-dist\CampusReconnect\*" in text
    assert "recursesubdirs createallsubdirs" in text
    assert r"..\dist\{#MyAppExeName}" not in text


def test_build_finds_per_user_inno_setup_installation(tmp_path, monkeypatch):
    from tools import build_installer

    compiler = tmp_path / "Programs" / "Inno Setup 6" / "ISCC.exe"
    compiler.parent.mkdir(parents=True)
    compiler.write_bytes(b"MZ")
    monkeypatch.setattr(build_installer.shutil, "which", lambda _: None)
    monkeypatch.setattr(Path, "is_file", lambda path: path == compiler)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert build_installer.find_iscc() == str(compiler)


def test_packaging_spec_builds_onedir_without_upx_compression(project_root):
    text = (project_root / "CampusReconnect.spec").read_text(encoding="utf-8")
    assert "CAMPUSRECONNECT_ONEDIR" in text
    assert "exclude_binaries=is_onedir" in text
    assert "COLLECT(" in text
    assert "upx=False" in text


def test_installed_update_launches_visible_installer_and_propagates_start_failure(
        tmp_path, monkeypatch):
    import campus_update_install as update_install

    setup = tmp_path / "CampusReconnectSetup.exe"
    setup.write_bytes(b"MZ")
    launched = []

    def popen(command, **kwargs):
        launched.append((command, kwargs))
        return object()

    monkeypatch.setattr(update_install.subprocess, "Popen", popen)
    process = update_install.launch_setup(setup)

    assert process is not None
    assert launched[0][0] == [str(setup.resolve())]
    assert "creationflags" not in launched[0][1]

    def fail_to_start(command, **kwargs):
        raise OSError("process creation failed")

    monkeypatch.setattr(update_install.subprocess, "Popen", fail_to_start)
    with pytest.raises(OSError, match="process creation failed"):
        update_install.launch_setup(setup)


def test_installer_asset_download_uses_its_own_checksum(tmp_path):
    from campus_update import UpdateInfo, download_installer

    payload = b"MZ" + b"x" * 10
    import hashlib
    digest = hashlib.sha256(payload).hexdigest()

    class Response:
        def __init__(self, text="", chunks=None):
            self.status_code = 200
            self.text = text
            self.headers = {"Content-Length": str(len(payload))}
            self._chunks = chunks
        def iter_content(self, chunk_size):
            yield payload
        def close(self):
            pass

    class Session:
        def get(self, url, **kwargs):
            if url.endswith(".sha256"):
                return Response("%s  CampusReconnectSetup.exe\nsize=%d\n" %
                                (digest, len(payload)))
            return Response(chunks=True)

    target = download_installer(Session(), UpdateInfo("v9.0.0", "test", "https://x"),
                                tmp_path)
    assert target.name == "CampusReconnectSetup.exe"
    assert target.read_bytes() == payload
