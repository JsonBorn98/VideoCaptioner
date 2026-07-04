"""Tests for subtitle font discovery and import helpers."""

import shutil
from pathlib import Path

import pytest

from videocaptioner.core.subtitle import font_utils


@pytest.fixture(autouse=True)
def reset_font_cache():
    font_utils.clear_font_cache()
    yield
    font_utils.clear_font_cache()


def _resource_font(name: str) -> Path:
    return Path(__file__).parents[2] / "resource" / "fonts" / name


def test_import_font_files_imports_multiple_fonts(monkeypatch, tmp_path):
    monkeypatch.setattr(font_utils, "FONTS_PATH", tmp_path)

    imported = font_utils.import_font_files(
        [
            _resource_font("LXGWWenKai-Regular.ttf"),
            _resource_font("NotoSansSC-Regular.ttf"),
        ]
    )

    imported_names = {font["name"] for font in imported}
    assert {"LXGW WenKai", "Noto Sans SC"} <= imported_names
    assert (tmp_path / "LXGWWenKai-Regular.ttf").exists()
    assert (tmp_path / "NotoSansSC-Regular.ttf").exists()

    discovered_names = {font["name"] for font in font_utils.get_builtin_fonts()}
    assert {"LXGW WenKai", "Noto Sans SC"} <= discovered_names


def test_import_font_files_suffixes_different_files_with_same_name(tmp_path):
    source_a_dir = tmp_path / "source-a"
    source_b_dir = tmp_path / "source-b"
    font_dir = tmp_path / "fonts"
    source_a_dir.mkdir()
    source_b_dir.mkdir()

    source_a = source_a_dir / "CustomFont.ttf"
    source_b = source_b_dir / "CustomFont.ttf"
    shutil.copy2(_resource_font("LXGWWenKai-Regular.ttf"), source_a)
    shutil.copy2(_resource_font("NotoSansSC-Regular.ttf"), source_b)

    imported = font_utils.import_font_files([source_a, source_b], font_dir=font_dir)

    assert {font["name"] for font in imported} == {"LXGW WenKai", "Noto Sans SC"}
    assert (font_dir / "CustomFont.ttf").exists()
    assert (font_dir / "CustomFont-1.ttf").exists()


def test_import_font_files_skips_unsupported_files(tmp_path):
    unsupported = tmp_path / "not-a-font.txt"
    unsupported.write_text("hello", encoding="utf-8")

    assert font_utils.import_font_files([unsupported], font_dir=tmp_path / "fonts") == []
