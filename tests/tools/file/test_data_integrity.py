from pathlib import Path

import pytest

from openminion.modules.tool.errors import ToolRuntimeError
from openminion.tools.file.backends import (
    EditOperation,
    InMemoryStorageBackend,
    LocalStorageBackend,
)


@pytest.fixture(params=["local", "memory"])
def backend(request, tmp_path):
    if request.param == "local":
        return LocalStorageBackend(tmp_path)
    return InMemoryStorageBackend(root=tmp_path)


@pytest.mark.parametrize("append", [False, True])
def test_invalid_write_preserves_original(backend, tmp_path, append):
    path = str(tmp_path / "original.txt")
    backend.write(path, "original\n")
    with pytest.raises(ToolRuntimeError) as error:
        backend.write(path, "bad\ud800", append=append)
    assert error.value.code == "INVALID_ARGUMENT"
    assert backend.read(path).content == "original\n"


def test_invalid_write_does_not_create_parents(backend, tmp_path):
    path = str(tmp_path / "new" / "child.txt")
    with pytest.raises(ToolRuntimeError):
        backend.write(path, "\ud800")
    assert not backend.exists(path)
    assert not backend.exists(str(Path(path).parent))


@pytest.mark.parametrize("dry_run", [False, True])
def test_invalid_edit_preserves_original(backend, tmp_path, dry_run):
    path = str(tmp_path / "original.txt")
    backend.write(path, "original\n")
    with pytest.raises(ToolRuntimeError) as error:
        backend.edit(
            path, [EditOperation("replace", "original", "\ud800")], dry_run=dry_run
        )
    assert error.value.code == "INVALID_ARGUMENT"
    assert backend.read(path).content == "original\n"


@pytest.mark.parametrize("newline", ["\r\n", "\n", "\r"])
@pytest.mark.parametrize("operation", ["replace", "insert_before", "insert_after"])
def test_edit_preserves_newline_bytes(backend, tmp_path, newline, operation):
    path = str(tmp_path / "lines.txt")
    original = newline.join(["first", "second", "third", ""])
    backend.write(path, original)
    inserted = {
        "replace": "changed",
        "insert_before": "changed" + newline + "second",
        "insert_after": "second" + newline + "changed",
    }[operation]
    backend.edit(path, [EditOperation(operation, "second", "changed")])
    expected = original.replace("second", inserted)
    if isinstance(backend, LocalStorageBackend):
        assert Path(path).read_bytes() == expected.encode()
    else:
        assert backend.read(path).content == expected


@pytest.mark.parametrize(
    "matches,limit,truncated", [(3, 1, True), (1, 1, False), (0, 1, False)]
)
def test_search_limit_is_truthful(backend, tmp_path, matches, limit, truncated):
    backend.write(str(tmp_path / "a.txt"), "needle\n" * matches)
    result = backend.search(str(tmp_path), query="needle", max_matches=limit)
    assert result.count == min(matches, limit)
    assert result.truncated is truncated


@pytest.mark.parametrize("pattern", ["src/*.py", "*.py", "**/*.py"])
def test_search_relative_glob(backend, tmp_path, pattern):
    backend.write(str(tmp_path / "src" / "example.py"), "needle\n")
    backend.write(str(tmp_path / "other" / "example.txt"), "needle\n")
    result = backend.search(str(tmp_path), query="needle", file_glob=pattern)
    assert result.count == 1
    assert result.matches[0].path == str(tmp_path / "src" / "example.py")


def test_range_beyond_prefix_limit(backend, tmp_path):
    path = str(tmp_path / "large.txt")
    backend.write(path, "".join(f"{i:05d} " + "x" * 93 + "\n" for i in range(1, 3001)))
    result = backend.read_range(path, start_line=2500, end_line=2502, max_chars=200000)
    assert result.total_lines == 3000
    assert result.content.startswith("2500: 02500 ")
    assert result.end_line == 2502
    assert not result.truncated


@pytest.mark.parametrize(
    "text,start,end,total,expected",
    [
        ("", 1, 2, 0, ""),
        ("a\nb", 2, 5, 2, "2: b"),
        ("a\r\nb\r\n", 1, 2, 2, "1: a\n2: b"),
    ],
)
def test_range_boundaries(backend, tmp_path, text, start, end, total, expected):
    path = str(tmp_path / "range.txt")
    backend.write(path, text)
    result = backend.read_range(path, start_line=start, end_line=end, max_chars=200000)
    assert result.total_lines == total
    assert result.content == expected


def test_range_rejects_beyond_eof_and_caps_output(backend, tmp_path):
    path = str(tmp_path / "range.txt")
    backend.write(path, "a" * 100 + "\nb\n")
    with pytest.raises(ToolRuntimeError):
        backend.read_range(path, start_line=3, end_line=4, max_chars=20)
    result = backend.read_range(path, start_line=1, end_line=2, max_chars=20)
    assert result.total_lines == 2
    assert len(result.content) == 20
    assert result.truncated


def test_memory_trash_preserves_unsupported_content(tmp_path):
    backend = InMemoryStorageBackend(root=tmp_path)
    path = str(tmp_path / "important.txt")
    backend.write(path, "original")
    with pytest.raises(ToolRuntimeError):
        backend.trash(path)
    assert backend.read(path).content == "original"


@pytest.mark.parametrize(
    "failure", [PermissionError("denied"), OSError("trash unavailable")]
)
def test_native_trash_failure_preserves_original(tmp_path, monkeypatch, failure):
    import send2trash

    path = tmp_path / "original.txt"
    path.write_bytes(b"original")

    def reject(_path):
        raise failure

    monkeypatch.setattr(send2trash, "send2trash", reject)
    with pytest.raises(ToolRuntimeError):
        LocalStorageBackend(tmp_path).trash(str(path))
    assert path.read_bytes() == b"original"


def test_edit_mixed_newlines_and_dry_run(backend, tmp_path):
    path = str(tmp_path / "mixed.txt")
    original = "first\nsecond\r\nthird"
    backend.write(path, original)
    result = backend.edit(
        path, [EditOperation("insert_after", "second", "added")], dry_run=True
    )
    assert result.preview == "first\nsecond\r\nadded\r\nthird"
    if isinstance(backend, LocalStorageBackend):
        assert Path(path).read_bytes() == original.encode()
    else:
        assert backend.read(path).content == original


def test_search_scan_ceiling_is_truthful(backend, tmp_path, monkeypatch):
    from openminion.tools.file.backends import local, memory

    monkeypatch.setattr(local, "FILE_SEARCH_MAX_SCANNED_FILES", 1)
    monkeypatch.setattr(memory, "FILE_SEARCH_MAX_SCANNED_FILES", 1)
    backend.write(str(tmp_path / "a.txt"), "needle\n")
    backend.write(str(tmp_path / "b.txt"), "needle\n")
    result = backend.search(str(tmp_path), query="needle")
    assert result.count == 1
    assert result.truncated


def test_search_excludes_denied_descendants(backend, tmp_path):
    backend.write(str(tmp_path / "src" / "public.py"), "needle\n")
    backend.write(str(tmp_path / "src" / "private.py"), "needle\n")
    result = backend.search(
        str(tmp_path),
        query="needle",
        file_glob="src/*.py",
        path_filter=lambda path: not path.endswith("private.py"),
    )
    assert result.count == 1
    assert result.matches[0].path.endswith("public.py")
