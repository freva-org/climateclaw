import pytest

from climateclaw.core import available_chatbots as chatbots


def _write_config(path, model_name):
    path.write_text(
        f"model_list:\n  - model_name: {model_name}\n",
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _refresh_chatbot_cache():
    chatbots.refresh_cache()
    yield
    chatbots.refresh_cache()


def test_available_chatbots_uses_dev_config_in_dev_mode(tmp_path, monkeypatch):
    _write_config(tmp_path / "litellm_config.yaml", "deployment-model")
    _write_config(tmp_path / "litellm_config.dev.yaml", "development-model")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LITELLM_CONFIG", raising=False)
    monkeypatch.setenv("CLIMATECLAW_DEV", "1")

    assert chatbots.available_chatbots() == ["development-model"]


def test_available_chatbots_uses_deployment_config_outside_dev_mode(
    tmp_path, monkeypatch
):
    _write_config(tmp_path / "litellm_config.yaml", "deployment-model")
    _write_config(tmp_path / "litellm_config.dev.yaml", "development-model")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LITELLM_CONFIG", raising=False)
    monkeypatch.setenv("CLIMATECLAW_DEV", "0")

    assert chatbots.available_chatbots() == ["deployment-model"]
