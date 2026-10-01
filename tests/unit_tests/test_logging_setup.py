import socket

from climateclaw.core.logging_setup import _syslog_socket_type


def test_syslog_socket_type_maps_tcp_and_udp():
    assert _syslog_socket_type("tcp") == socket.SOCK_STREAM
    assert _syslog_socket_type("udp") == socket.SOCK_DGRAM


def test_syslog_socket_type_rejects_invalid_protocol():
    assert _syslog_socket_type("tpc") is None
