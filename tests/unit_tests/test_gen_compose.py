import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import gen_compose


def reload_gen_compose(
    monkeypatch,
    protocol: str | None = None,
    host: str | None = "syslog.example",
):
    monkeypatch.setenv("CLIMATECLAW_DEV", "0")
    if host is None:
        monkeypatch.delenv("CLIMATECLAW_SYSLOG_HOST", raising=False)
    else:
        monkeypatch.setenv("CLIMATECLAW_SYSLOG_HOST", host)
    monkeypatch.setenv("CLIMATECLAW_SYSLOG_PORT", "5514")
    if protocol is None:
        monkeypatch.delenv("CLIMATECLAW_SYSLOG_PROTOCOL", raising=False)
    else:
        monkeypatch.setenv("CLIMATECLAW_SYSLOG_PROTOCOL", protocol)
    return importlib.reload(gen_compose)


def test_get_syslog_target_accepts_tcp_and_udp(monkeypatch):
    module = reload_gen_compose(monkeypatch)
    assert module.get_syslog_target() == "tcp@syslog.example:5514"

    module = reload_gen_compose(monkeypatch, protocol="udp")
    assert module.get_syslog_target() == "udp@syslog.example:5514"


def test_get_syslog_target_warns_for_invalid_protocol(monkeypatch):
    module = reload_gen_compose(monkeypatch, protocol="tpc")

    with pytest.warns(UserWarning, match="remote syslog disabled"):
        assert module.get_syslog_target() is None


def test_get_syslog_target_warns_when_host_is_missing(monkeypatch):
    module = reload_gen_compose(monkeypatch, host=None)

    with pytest.warns(UserWarning, match="remote syslog disabled"):
        assert module.get_syslog_target() is None


def test_generate_haproxy_continues_without_remote_syslog(monkeypatch):
    module = reload_gen_compose(monkeypatch, host=None)

    with pytest.warns(UserWarning, match="remote syslog disabled"):
        config = module.generate_haproxy(
            services={
                "climateclaw": {},
                "litellm": {},
                "ollama": {},
            },
            backend_n=1,
            backend_port="8502",
            litellm_n=1,
            ollama_n=1,
            server_list=[],
            replica_dict={},
            port_dict={},
            timeout=600,
        )

    assert "log stdout format raw local0 info" in config
    assert "stats socket /var/run/haproxy.sock mode 660 level admin" in config
    assert "log tcp@" not in config
    assert "log udp@" not in config


def test_add_litellm_syslog_logging_when_host_is_set(monkeypatch):
    module = reload_gen_compose(monkeypatch)
    services = {"litellm": {}, "litellm-2": {}, "climateclaw": {}}

    module.add_litellm_syslog_logging(services)

    expected_logging = {
        "driver": "syslog",
        "options": {
            "syslog-address": "tcp://syslog.example:5514",
            "tag": "litellm-${CLIMATECLAW_INSTANCE_NAME}",
        },
    }
    assert services["litellm"]["logging"] == expected_logging
    assert services["litellm-2"]["logging"] == {
        "driver": "syslog",
        "options": {
            "syslog-address": "tcp://syslog.example:5514",
            "tag": "litellm-2-${CLIMATECLAW_INSTANCE_NAME}",
        },
    }
    assert "logging" not in services["climateclaw"]


def test_add_litellm_syslog_logging_continues_without_host(monkeypatch):
    module = reload_gen_compose(monkeypatch, host=None)
    services = {"litellm": {}}

    with pytest.warns(UserWarning, match="remote syslog disabled"):
        module.add_litellm_syslog_logging(services)

    assert "logging" not in services["litellm"]
