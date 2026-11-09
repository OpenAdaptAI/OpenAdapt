from __future__ import annotations

from pathlib import Path

import pytest

from scripts.alias_lowercase_x11_sonames import (
    X11_ALIASES,
    alias_path,
    install_alias,
    locate_real_library,
    main,
)


def test_alias_pairs_preserve_published_flow_134_probe_names() -> None:
    assert X11_ALIASES == (
        ("xcomposite", "Xcomposite"),
        ("xdamage", "Xdamage"),
        ("xfixes", "Xfixes"),
        ("xrandr", "Xrandr"),
    )


def test_install_alias_creates_lowercase_soname_beside_the_real_library(tmp_path: Path) -> None:
    real = tmp_path / "libXcomposite.so.1"
    real.write_bytes(b"x11")

    dest = install_alias(real, "xcomposite")

    assert dest == tmp_path / "libxcomposite.so"
    assert dest.is_symlink()
    assert dest.resolve() == real.resolve()
    assert locate_real_library("Xcomposite", (tmp_path,)) == real
    assert alias_path(real, "xcomposite") == dest
    # Idempotent: a second call leaves the existing alias.
    assert install_alias(real, "xcomposite") == dest


def test_main_is_a_noop_off_linux(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("scripts.alias_lowercase_x11_sonames.sys.platform", "darwin")
    assert main(["--lib-dir", str(tmp_path)]) == 0
    assert list(tmp_path.iterdir()) == []


def test_main_refuses_a_host_missing_the_real_x11_libraries(tmp_path: Path) -> None:
    if __import__("sys").platform != "linux":
        pytest.skip("Linux host aliases only")
    with pytest.raises(FileNotFoundError, match="missing real X11 library"):
        locate_real_library("Xcomposite", (tmp_path,))
