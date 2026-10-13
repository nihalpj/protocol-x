from __future__ import annotations

import pytest


@pytest.fixture()
def tmp_env(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("PROTOX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("PROTOX_WORKSPACE", str(workspace))
    return {"data_dir": data_dir, "workspace": workspace}


@pytest.fixture()
def settings(tmp_env):
    from server.config import Settings

    return Settings()


@pytest.fixture()
def ctx(settings):
    from server.app import AppContext

    context = AppContext(settings)
    yield context
    context.store.close()


@pytest.fixture()
def sample_png(settings):
    from PIL import Image

    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    path = settings.workspace_root / "sample.png"
    Image.new("RGB", (4000, 3000), (120, 60, 200)).save(path)
    return "sample.png"
