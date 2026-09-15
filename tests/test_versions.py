import pytest

from az_artifacts import PackageVersion, VersionNotFoundError
from az_artifacts._versions import resolve_version, validate_name, version_number, version_pattern


@pytest.mark.parametrize("name", ["package", "my-package_1.2", "123"])
def test_valid_names(name):
    validate_name(name)


@pytest.mark.parametrize("name", ["UPPER", "a..b", "-a", "a-", "a+b", "a/b", ""])
def test_invalid_names(name):
    with pytest.raises(ValueError):
        validate_name(name)


@pytest.mark.parametrize(
    ("version", "prefix"), [("*", ()), ("1.*", (1,)), ("1.2.*", (1, 2)), ("0.*", (0,))]
)
def test_version_patterns(version, prefix):
    assert version_pattern(version) == prefix


@pytest.mark.parametrize("version", ["1.2.3", "0.0.0", "1.2.3-rc.1", "1.2.3-0", "1.2.3-01a"])
def test_exact_versions(version):
    assert version_pattern(version) is None


@pytest.mark.parametrize(
    "version",
    [
        "1",
        "1.2",
        "1.2.03",
        "1.2.3+build",
        "1.2.3-RC",
        "1.2.3-01",
        "1.*.3",
        "01.*",
        "1.2.3.*",
        "*-rc",
    ],
)
def test_invalid_versions(version):
    with pytest.raises(ValueError):
        version_pattern(version)


def test_stable_semver_uses_numeric_order():
    assert version_number("1.10.0") > version_number("1.9.0")
    assert version_number("1.2.3-rc.1", stable_only=True) is None


@pytest.mark.parametrize(
    ("versions", "message"),
    [(None, "was not found"), ((), "No released version")],
)
def test_catalog_absence_preserves_wildcard_errors(versions, message):
    with pytest.raises(VersionNotFoundError, match=message):
        resolve_version("package", (), versions)


def test_wildcard_selection_uses_typed_normalized_catalog_versions():
    versions = (
        PackageVersion("1.9.0"),
        PackageVersion("1.10.0"),
        PackageVersion("1.12.0", is_deleted=True),
        PackageVersion("1.11.0-rc.1"),
    )
    assert resolve_version("package", (1,), versions) == "1.10.0"
