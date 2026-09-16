"""Read-only Microsoft-tool download helper; never exposes raw tool output."""

import argparse
import json
import os
import subprocess
from pathlib import Path

if __package__:
    from .reference_tool import HOOK
else:
    from reference_tool import HOOK


def download_package(
    *, organization, project, feed, name, version, path, token, tool, capture_path=None
):
    """Download only; callers must compare files against their reviewed hashes."""
    destination = Path(path).resolve()
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError("Microsoft reference downloads require an empty destination")
    tool = Path(tool).resolve()
    if not token or not tool.is_file():
        raise RuntimeError("An existing Microsoft tool and process-local credential are required")
    env = os.environ.copy()
    env["AZ_ARTIFACTS_REFERENCE_TOKEN"] = token
    env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
    env.pop("DOTNET_STARTUP_HOOKS", None)
    env.pop("AZ_ARTIFACTS_CAPTURE_PATH", None)
    env.pop("AZ_ARTIFACTS_CAPTURE_PACKAGE_NAME", None)
    if capture_path is not None:
        capture = Path(capture_path).resolve()
        if capture.exists() or not HOOK.is_file():
            raise RuntimeError("Capture must be new and the sanitized hook must be built")
        if capture.is_relative_to(destination):
            raise ValueError("Capture must be outside the package destination")
        env["DOTNET_STARTUP_HOOKS"] = str(HOOK)
        env["AZ_ARTIFACTS_CAPTURE_PATH"] = str(capture)
        env["AZ_ARTIFACTS_CAPTURE_PACKAGE_NAME"] = name
    result = subprocess.run(
        [
            str(tool),
            "universal",
            "download",
            "--service",
            organization,
            "--project",
            project,
            "--feed",
            feed,
            "--package-name",
            name,
            "--package-version",
            version,
            "--path",
            str(destination),
            "--patvar",
            "AZ_ARTIFACTS_REFERENCE_TOKEN",
        ],
        env=env,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Microsoft reference download failed (exit {result.returncode})")
    return {"download": "completed", "path": str(destination), "exit_code": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("organization", "project", "feed", "name", "version", "path", "tool"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--capture-path")
    args = vars(parser.parse_args())
    args["token"] = os.environ.get("AZ_ARTIFACTS_REFERENCE_TOKEN")
    try:
        result = download_package(**args)
    except (OSError, RuntimeError, ValueError):
        print("Microsoft reference download failed; raw tool output was withheld.")
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
