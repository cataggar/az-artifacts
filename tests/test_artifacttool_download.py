"""Ensure the general reference helper can only download and never logs secrets."""

from types import SimpleNamespace

import pytest
from interop import artifacttool_download


@pytest.mark.parametrize("status", [0, 1])
def test_reference_helper_is_download_only_and_withholds_output(
    tmp_path, monkeypatch, capsys, status
):
    calls = []
    tool = tmp_path / "artifacttool.exe"
    tool.touch()
    monkeypatch.setenv("DOTNET_STARTUP_HOOKS", "inherited-hook-must-not-run")

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=status, stdout=b"never-record-signed-url", stderr=b"never-record-token"
        )

    monkeypatch.setattr(artifacttool_download.subprocess, "run", run)
    args = {
        "organization": "https://dev.azure.com/org",
        "project": "project",
        "feed": "feed",
        "name": "package",
        "version": "1.2.3",
        "path": tmp_path / "output",
        "token": "never-record-token",
        "tool": tool,
    }
    if status:
        with pytest.raises(RuntimeError) as caught:
            artifacttool_download.download_package(**args)
        assert "never-record" not in str(caught.value)
    else:
        assert artifacttool_download.download_package(**args)["exit_code"] == 0
    command, kwargs = calls[0]
    assert command[1:3] == ["universal", "download"]
    assert "publish" not in command and args["token"] not in command
    assert kwargs["capture_output"] is True
    assert kwargs["env"]["AZ_ARTIFACTS_REFERENCE_TOKEN"] == args["token"]
    assert "DOTNET_STARTUP_HOOKS" not in kwargs["env"]
    captured = capsys.readouterr()
    assert "never-record" not in captured.out + captured.err
