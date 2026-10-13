"""P9 onboarding: setup wizard, config auto-load, full doctor (F-031/F-035).

AC-P9-01 wizard writes a working server.yaml · AC-P9-02 rerun is idempotent
(token preserved) · AC-P9-03 doctor full probes sandbox + transport ·
AC-P9-04 non-interactive shells get manual instructions, not a crash.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml


def test_settings_autoloads_data_dir_config(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "server.yaml").write_text(yaml.safe_dump({
        "security": {"tokens": ["tok-abc"], "policy_preset": "strict"},
        "models": {"api_key": "tf-key-123"},
    }))
    monkeypatch.setenv("PROTOX_DATA_DIR", str(data_dir))
    monkeypatch.delenv("PROTOX_TF_API_KEY", raising=False)
    from server.config import Settings

    s = Settings()
    assert s.tokens == ["tok-abc"]
    assert s.preset_name == "strict"
    assert s.tf_api_key == "tf-key-123"
    assert s.agent_features_enabled


def test_wizard_writes_config_and_is_idempotent(tmp_path, monkeypatch, capsys):
    import builtins

    from server.setup import run_setup

    env = {"PROTOX_DATA_DIR": str(tmp_path / "data")}
    answers = iter(["", "", ""])  # preset, workspace, tf key
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(answers))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    assert run_setup(env) == 0
    cfg_path = tmp_path / "data" / "server.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    assert cfg["security"]["policy_preset"] == "network-egress"
    token = cfg["security"]["tokens"][0]
    assert len(token) > 16

    # rerun: defaults from the existing file, token preserved (AC-P9-02)
    answers = iter(["", "", "", ""])  # + the regenerate? question
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(answers))
    assert run_setup(env) == 0
    cfg2 = yaml.safe_load(cfg_path.read_text())
    assert cfg2["security"]["tokens"][0] == token

    out = capsys.readouterr().out
    assert "degraded mode" in out  # no TF key -> E-20 hint shown


def test_wizard_non_interactive_prints_manual_path(tmp_path, capsys):
    from server.setup import run_setup

    env = {"PROTOX_DATA_DIR": str(tmp_path / "data")}
    assert run_setup(env) == 0  # pytest stdin is not a TTY
    assert "non-interactive" in capsys.readouterr().out


def test_doctor_full_checks(settings):
    from server.doctor import run_checks

    checks = {c.name: c for c in run_checks(settings, quick=False)}
    for name in ("sandbox-spawn", "worker-env-scrubbed", "transport",
                 "token-factory", "python>=3.12", "workspace-writable"):
        assert name in checks
    assert checks["sandbox-spawn"].status == "green"
    assert checks["transport"].status == "green"
    assert checks["token-factory"].status == "yellow"  # no key -> degraded


def test_doctor_exit_codes(settings):
    from server.doctor import run_doctor

    assert run_doctor(quick=True, settings=settings) == 0
    assert run_doctor(quick=False, settings=settings) == 0  # yellows don't fail


def test_install_script_syntax():
    script = Path(__file__).resolve().parents[2] / "scripts" / "install.sh"
    subprocess.run(["bash", "-n", str(script)], check=True)
    assert script.stat().st_mode & 0o111  # executable


def test_sample_png_bundled():
    sample = Path(__file__).resolve().parents[2] / "samples" / "sample.png"
    assert sample.exists()
    from PIL import Image

    with Image.open(sample) as img:
        img.verify()
