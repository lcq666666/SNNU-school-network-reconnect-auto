"""Static and offline checks for the standard Windows installer path."""
import pytest


def test_inno_recipe_preserves_user_data_and_registers_startup(project_root):
    text = (project_root / "installer" / "CampusReconnect.iss").read_text(encoding="utf-8")
    assert "{localappdata}\\Programs\\CampusReconnect" in text
    assert "CampusReconnectSetup" in text
    assert "CampusReconnectTray" in text
    assert "UninstallDelete" not in text
    assert "Flags: nowait postinstall skipifsilent" not in text


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
