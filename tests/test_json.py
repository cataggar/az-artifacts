import pytest

from az_artifacts._json import blob_id, manifest, package_metadata
from az_artifacts.errors import ProtocolError

CHUNK = "ab" * 32 + "01"
NODE = "cd" * 32 + "02"


def test_metadata():
    metadata = package_metadata(
        {"version": "1.0.0", "manifestId": CHUNK, "superRootId": NODE, "packageSize": 1}
    )
    assert metadata.manifest_id == CHUNK.upper()
    assert metadata.super_root_id == NODE.upper()


@pytest.mark.parametrize("value", [None, "", "ABC01", "ff" * 32 + "03", "not a blob"])
def test_invalid_blob_id(value):
    with pytest.raises(ProtocolError):
        blob_id(value)


@pytest.mark.parametrize("value", [None, [], {"items": None}, {"items": [{}]}])
def test_invalid_manifest(value):
    with pytest.raises(ProtocolError):
        manifest(value)


@pytest.mark.parametrize("value", [-1, True, "123", None, 1.5])
def test_invalid_sizes(value):
    with pytest.raises(ProtocolError):
        manifest({"items": [{"path": "/file", "blob": {"id": CHUNK, "size": value}}]})


def test_manifest_size_and_id():
    (item,) = manifest({"items": [{"path": "/file", "blob": {"id": CHUNK, "size": 0}}]})
    assert item.blob.id == CHUNK.upper()
    assert item.blob.size == 0
