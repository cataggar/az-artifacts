"""Generate the next synthetic approval payload locally; performs no network I/O."""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / ".reference-proposal" / "0.0.2-feasibility.20000101"
SEED = b"az-artifacts reference multi-chunk v1\0"
PAYLOAD_SIZE = 104857600


def main() -> None:
    if SOURCE.exists():
        raise RuntimeError("Proposal directory already exists; do not overwrite reviewed bytes")
    (SOURCE / "nested").mkdir(parents=True)
    digest = hashlib.sha256()
    with (SOURCE / "payload.bin").open("xb") as output:
        for start in range(0, PAYLOAD_SIZE // 32, 32768):
            block = b"".join(
                hashlib.sha256(SEED + i.to_bytes(8, "little")).digest()
                for i in range(start, start + 32768)
            )
            output.write(block)
            digest.update(block)
    (SOURCE / "empty.txt").write_bytes(b"")
    (SOURCE / "nested" / "repeated.bin").write_bytes(bytes(131072))
    files = []
    for relative in ("payload.bin", "empty.txt", "nested/repeated.bin"):
        path = SOURCE / relative
        with path.open("rb") as stream:
            sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
        files.append({"path": relative, "size": path.stat().st_size, "sha256": sha256})
    if files[0]["sha256"] != digest.hexdigest():
        raise RuntimeError("Generated payload readback hash mismatch")
    print(
        json.dumps(
            {
                "source_directory": str(SOURCE),
                "total_bytes": sum(item["size"] for item in files),
                "files": files,
                "external_writes": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
