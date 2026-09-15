from pathlib import Path

import pytest

from az_artifacts._paths import prepare_destination, relative_path, select_files
from az_artifacts.errors import NoMatchingFilesError, UnsafePathError
from az_artifacts.models import BlobRef, ManifestItem


def items(*paths):
    return tuple(ManifestItem(path, BlobRef("00" * 32 + "01", 0)) for path in paths)


def test_manifest_leading_slash_is_package_relative():
    assert str(relative_path("/dir/file.txt")) == "dir/file.txt"


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/",
        "//file",
        "/../file",
        "dir/../file",
        "./file",
        "dir//file",
        "C:/file",
        "dir\\file",
        "a\0b",
    ],
)
def test_invalid_paths_are_rejected(path):
    with pytest.raises(UnsafePathError):
        relative_path(path)


@pytest.mark.parametrize(
    "paths",
    [("/a", "a"), ("a", "a/b"), ("a/b", "a")],
)
def test_duplicate_or_overlapping_paths_are_rejected(paths):
    with pytest.raises(UnsafePathError):
        select_files(items(*paths), None)


def test_filters_use_package_relative_paths():
    selected = select_files(items("/root.txt", "/dir/file.txt", "/dir/file.bin"), "**/*.txt")
    assert [path for _, path in selected] == [Path("root.txt"), Path("dir/file.txt")]


def test_star_does_not_cross_directories():
    selected = select_files(items("root.txt", "dir/file.txt"), "*.txt")
    assert [path for _, path in selected] == [Path("root.txt")]


def test_ordered_exclusion_and_reinclusion():
    selected = select_files(
        items("a.txt", "b.txt", "dir/c.txt"),
        ["**", "!**/*.txt", "dir/*.txt"],
    )
    assert [path for _, path in selected] == [Path("dir/c.txt")]


def test_extended_glob_is_not_an_exclusion():
    selected = select_files(items("a.txt", "b.bin"), "!(*.bin)")
    assert [path for _, path in selected] == [Path("a.txt")]


def test_no_matches():
    with pytest.raises(NoMatchingFilesError):
        select_files(items("a.txt"), "*.bin")


def test_hidden_files_are_included():
    assert len(select_files(items(".hidden"), "*")) == 1


def test_empty_manifest():
    assert select_files((), None) == ()


def test_existing_file_requires_overwrite(tmp_path):
    (tmp_path / "file").write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        prepare_destination(tmp_path, Path("file"), overwrite=False)
    assert (tmp_path / "file").read_bytes() == b"existing"


def test_symlink_destinations_are_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this platform")
    with pytest.raises(UnsafePathError):
        prepare_destination(tmp_path, Path("link/file"), overwrite=True)


def test_symlink_files_are_rejected(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"existing")
    link = tmp_path / "link"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this platform")
    with pytest.raises(UnsafePathError):
        prepare_destination(tmp_path, Path("link"), overwrite=True)
    assert target.read_bytes() == b"existing"


def test_resolved_parent_must_stay_inside_output(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == root / "redirected":
            return root.parent / "outside"
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(UnsafePathError, match="outside"):
        prepare_destination(root, Path("redirected/file"), overwrite=True)
    assert not (root / "redirected").exists()
