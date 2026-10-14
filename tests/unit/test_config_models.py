"""Settings: agent testing model (models.test / PROTOX_TF_TEST_MODEL)."""

from server.config import Settings


def test_test_model_default(tmp_path):
    cfg = tmp_path / "server.yaml"
    cfg.write_text("{}")
    s = Settings(env={}, config_file=str(cfg))
    assert s.tf_test_model == "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"


def test_test_model_env_override(tmp_path):
    cfg = tmp_path / "server.yaml"
    cfg.write_text("models:\n  test: nvidia/yaml-test\n")
    s = Settings(env={"PROTOX_TF_TEST_MODEL": "nvidia/env-test"},
                 config_file=str(cfg))
    assert s.tf_test_model == "nvidia/env-test"  # env wins over file


def test_test_model_yaml_key(tmp_path):
    cfg = tmp_path / "server.yaml"
    cfg.write_text("models:\n  test: nvidia/yaml-test\n")
    s = Settings(env={}, config_file=str(cfg))
    assert s.tf_test_model == "nvidia/yaml-test"


# -- testing: section (adapter tool-testing stage) ------------------------------


def test_testing_defaults(tmp_path):
    cfg = tmp_path / "server.yaml"
    cfg.write_text("{}")
    s = Settings(env={}, config_file=str(cfg))
    assert s.testing_enabled is True
    assert s.testing_max_cycles == 3


def test_testing_yaml_keys(tmp_path):
    cfg = tmp_path / "server.yaml"
    cfg.write_text("testing:\n  enabled: false\n  max_cycles: 5\n")
    s = Settings(env={}, config_file=str(cfg))
    assert s.testing_enabled is False
    assert s.testing_max_cycles == 5


def test_testing_env_overrides(tmp_path):
    cfg = tmp_path / "server.yaml"
    cfg.write_text("testing:\n  enabled: true\n  max_cycles: 5\n")
    s = Settings(
        env={"PROTOX_TESTING_ENABLED": "0", "PROTOX_TESTING_MAX_CYCLES": "2"},
        config_file=str(cfg),
    )
    assert s.testing_enabled is False
    assert s.testing_max_cycles == 2
