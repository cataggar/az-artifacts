import ntpath
import posixpath
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace

import pytest

from az_artifacts import _paths
from az_artifacts._paths import (
    filter_files,
    manifest_files,
    package_path,
    prepare_destination,
    relative_path,
    select_files,
    validate_file_filter,
)
from az_artifacts.errors import NoMatchingFilesError, UnsafePathError
from az_artifacts.models import BlobRef, ManifestItem


def items(*paths):
    return manifest_files(tuple(ManifestItem(path, BlobRef("00" * 32 + "01", 0)) for path in paths))


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


@pytest.mark.parametrize(
    "path",
    ["CON", "dir/aux.txt", "trail.", "trail ", "q?", "star*", "dir/name:stream", "<x>", 'a"b'],
)
def test_portable_files_ignore_windows_names_but_download_rejects_all_entries(monkeypatch, path):
    monkeypatch.setattr(_paths, "os", SimpleNamespace(name="nt", path=ntpath))
    files = items("safe", path)
    assert files[1].path == PurePosixPath(path)
    assert filter_files(files, None) == files
    with pytest.raises(UnsafePathError):
        select_files(files, "safe")


@pytest.mark.parametrize("paths", [("FILE", "file"), ("DIR", "dir/file")])
def test_windows_collisions_do_not_affect_logical_inspection(monkeypatch, paths):
    monkeypatch.setattr(_paths, "os", SimpleNamespace(name="nt", path=ntpath))
    files = items(*paths)
    assert len(filter_files(files, None)) == 2
    with pytest.raises(UnsafePathError):
        select_files(files, "unmatched")


@pytest.mark.parametrize("paths", [("é", "e\u0301"), ("É", "e\u0301/file")])
def test_macos_unicode_collisions_do_not_affect_logical_inspection(monkeypatch, paths):
    monkeypatch.setattr(_paths, "os", SimpleNamespace(name="posix", path=posixpath))
    monkeypatch.setattr(_paths, "sys", SimpleNamespace(platform="darwin"))
    files = items(*paths)
    assert len(filter_files(files, None)) == 2
    with pytest.raises(UnsafePathError):
        select_files(files, "unmatched")


def test_download_preserves_colon_restriction_on_posix(monkeypatch):
    monkeypatch.setattr(_paths, "os", SimpleNamespace(name="posix", path=posixpath))
    with pytest.raises(UnsafePathError):
        select_files(items("dir/name:stream"), None)


@pytest.mark.parametrize("path", ["/file", "//file", "", ".", "a//b", "a/./b", "a/../b"])
def test_caller_path_is_explicitly_relative(path):
    with pytest.raises(UnsafePathError):
        package_path(path)


@pytest.mark.parametrize("path", [Path("file"), PureWindowsPath("file"), None, 1])
def test_caller_path_rejects_host_path_and_invalid_types(path):
    with pytest.raises(ValueError):
        package_path(path)


def test_caller_pure_posix_path_uses_its_existing_normalization():
    assert package_path(PurePosixPath("dir//./file")) == PurePosixPath("dir/file")


@pytest.mark.parametrize("patterns", [[], "", "!", ["ok", "!"], [None], b"*", {"*"}, 4])
def test_invalid_filters_are_rejected_even_without_files(patterns):
    with pytest.raises(ValueError):
        validate_file_filter(patterns)


def test_portable_filter_is_case_sensitive_and_preserves_manifest_order():
    files = items("z.TXT", "a.txt", "c.md", "b.txt")
    assert [file.path.as_posix() for file in filter_files(files, ("*.{txt,md}",))] == [
        "a.txt",
        "c.md",
        "b.txt",
    ]


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
