from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from az_artifacts import (
    LimitedPackageMetadata,
    LimitedPackageMetadataListResponse,
    PackageMetadata,
    PackagePushMetadata,
    PackageVersionDeletionState,
)
from az_artifacts._json import (
    blob_id,
    limited_package_metadata,
    limited_package_metadata_list_response,
    manifest,
    package_metadata,
    package_push_metadata,
    package_version_deletion_state,
)
from az_artifacts.errors import ProtocolError

CHUNK = "ab" * 32 + "01"
NODE = "cd" * 32 + "02"
METADATA = {"version": "1.0.0", "manifestId": CHUNK, "superRootId": NODE, "packageSize": 1}
PUSH_METADATA = {"manifestId": CHUNK, "superRootId": NODE, "proofNodes": ["opaque proof", ""]}
DELETION_STATE = {"name": "package", "version": "1.0.0"}


def test_metadata():
    metadata = package_metadata(METADATA)
    assert metadata == PackageMetadata("1.0.0", CHUNK.upper(), NODE.upper(), 1)
    assert metadata.description is None


@pytest.mark.parametrize(
    ("decoder", "wire"),
    [
        (package_metadata, METADATA),
        (limited_package_metadata, {"version": "1.0.0-rc.1"}),
        (package_push_metadata, PUSH_METADATA),
    ],
)
@pytest.mark.parametrize("description", [None, "", "Package description"])
def test_optional_descriptions(decoder, wire, description):
    assert decoder(wire).description is None
    assert decoder(wire | {"description": description}).description == description


@pytest.mark.parametrize(
    ("decoder", "wire"),
    [
        (package_metadata, METADATA),
        (limited_package_metadata, {"version": "1.0.0"}),
        (package_push_metadata, PUSH_METADATA),
    ],
)
@pytest.mark.parametrize("description", [False, 3, 1.5, [], {}])
def test_invalid_descriptions(decoder, wire, description):
    with pytest.raises(ProtocolError, match="description"):
        decoder(wire | {"description": description})


@pytest.mark.parametrize("count", [0, 1, 7])
def test_limited_metadata_preserves_count_and_service_order(count):
    response = limited_package_metadata_list_response(
        {
            "count": count,
            "value": [
                {"version": "2.0.0-rc.1", "description": ""},
                {"version": "1.0.0"},
            ],
        }
    )
    assert response == LimitedPackageMetadataListResponse(
        count, (LimitedPackageMetadata("2.0.0-rc.1", ""), LimitedPackageMetadata("1.0.0"))
    )
    assert isinstance(response.value, tuple)


def test_empty_limited_metadata_list():
    assert limited_package_metadata_list_response(
        {"count": 0, "value": []}
    ) == LimitedPackageMetadataListResponse(0, ())


@pytest.mark.parametrize(
    "wire",
    [
        None,
        [],
        {},
        {"count": 0},
        {"count": 0, "value": None},
        {"count": 0, "value": {}},
        {"value": []},
        {"count": -1, "value": []},
        {"count": True, "value": []},
        {"count": "0", "value": []},
        {"count": 1.5, "value": []},
        {"count": 1, "value": [None]},
        {"count": 1, "value": [{}]},
        {"count": 1, "value": [{"version": ""}]},
        {"count": 1, "value": [{"version": True}]},
        {"count": 1, "value": [{"version": "1.0.0", "description": []}]},
    ],
)
def test_invalid_limited_metadata_list(wire):
    with pytest.raises(ProtocolError):
        limited_package_metadata_list_response(wire)


@pytest.mark.parametrize("field", ["continuationToken", "nextLink", "@odata.nextLink"])
def test_limited_metadata_continuation_is_not_silently_ignored(field):
    with pytest.raises(ProtocolError, match="continuation"):
        limited_package_metadata_list_response({"count": 0, "value": [], field: "next"})


def test_push_metadata_keeps_opaque_proof_strings():
    result = package_push_metadata(PUSH_METADATA)
    assert result == PackagePushMetadata(CHUNK.upper(), NODE.upper(), ("opaque proof", ""))
    assert isinstance(result.proof_nodes, tuple)
    assert package_push_metadata(PUSH_METADATA | {"proofNodes": []}).proof_nodes == ()


@pytest.mark.parametrize("proof_nodes", [None, "proof", {}, [None], [123], [True], [[]]])
def test_invalid_proof_nodes(proof_nodes):
    with pytest.raises(ProtocolError, match="proof"):
        package_push_metadata(PUSH_METADATA | {"proofNodes": proof_nodes})


@pytest.mark.parametrize(
    ("wire_date", "expected"),
    [
        (None, None),
        ("2026-09-15T23:06:16Z", datetime(2026, 9, 15, 23, 6, 16, tzinfo=UTC)),
        ("2026-09-15T17:06:16-06:00", datetime(2026, 9, 15, 23, 6, 16, tzinfo=UTC)),
        ("2026-09-16T01:06:16+02:00", datetime(2026, 9, 15, 23, 6, 16, tzinfo=UTC)),
        (
            "2026-09-15T23:06:16.1234567+00:00",
            datetime(2026, 9, 15, 23, 6, 16, 123456, tzinfo=UTC),
        ),
    ],
)
def test_deletion_date_is_optional_and_normalized_to_utc(wire_date, expected):
    assert package_version_deletion_state(DELETION_STATE).deleted_date is None
    result = package_version_deletion_state(DELETION_STATE | {"deletedDate": wire_date})
    assert result == PackageVersionDeletionState("package", "1.0.0", expected)
    if result.deleted_date is not None:
        assert result.deleted_date.tzinfo is UTC


@pytest.mark.parametrize(
    "wire_date",
    [
        "",
        False,
        123,
        [],
        {},
        "not a date",
        "2026-09-15",
        "2026-09-15T23:06:16",
        "2026-02-30T00:00:00Z",
        "0001-01-01T00:00:00+01:00",
    ],
)
def test_invalid_deletion_dates(wire_date):
    with pytest.raises(ProtocolError, match="deletion date"):
        package_version_deletion_state(DELETION_STATE | {"deletedDate": wire_date})


@pytest.mark.parametrize(
    ("decoder", "wire", "field"),
    [
        (package_metadata, METADATA, "version"),
        (package_metadata, METADATA, "manifestId"),
        (package_metadata, METADATA, "superRootId"),
        (package_metadata, METADATA, "packageSize"),
        (limited_package_metadata, {"version": "1.0.0"}, "version"),
        (package_push_metadata, PUSH_METADATA, "manifestId"),
        (package_push_metadata, PUSH_METADATA, "superRootId"),
        (package_push_metadata, PUSH_METADATA, "proofNodes"),
        (package_version_deletion_state, DELETION_STATE, "name"),
        (package_version_deletion_state, DELETION_STATE, "version"),
    ],
)
def test_required_model_fields(decoder, wire, field):
    with pytest.raises(ProtocolError):
        decoder({key: value for key, value in wire.items() if key != field})
    with pytest.raises(ProtocolError):
        decoder(wire | {field: False})


@pytest.mark.parametrize(
    "decoder",
    [
        package_metadata,
        limited_package_metadata,
        limited_package_metadata_list_response,
        package_push_metadata,
        package_version_deletion_state,
    ],
)
@pytest.mark.parametrize("wire", [None, [], "metadata", {0: "value"}])
def test_model_decoders_require_objects(decoder, wire):
    with pytest.raises(ProtocolError, match="JSON object"):
        decoder(wire)


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (PackageMetadata("1.0.0", CHUNK, NODE, 1), "version"),
        (LimitedPackageMetadata("1.0.0"), "description"),
        (LimitedPackageMetadataListResponse(0, ()), "value"),
        (PackagePushMetadata(CHUNK, NODE, ()), "proof_nodes"),
        (PackageVersionDeletionState("package", "1.0.0"), "deleted_date"),
    ],
)
def test_public_metadata_models_are_frozen(model, field):
    with pytest.raises(FrozenInstanceError):
        setattr(model, field, None)


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
