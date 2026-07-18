import unittest
from unittest import mock

from home_location_endpoint import reality_probe


class _FakeTlsSocket:
    def __init__(self, version="TLSv1.3", alpn="h2"):
        self._version = version
        self._alpn = alpn

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def version(self):
        return self._version

    def selected_alpn_protocol(self):
        return self._alpn


class _FakeRawSocket:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakeContext:
    def __init__(self, tls_socket):
        self.minimum_version = None
        self.alpn_protocols = None
        self.tls_socket = tls_socket
        self.server_hostname = None

    def set_alpn_protocols(self, protocols):
        self.alpn_protocols = protocols

    def wrap_socket(self, _socket, *, server_hostname):
        self.server_hostname = server_hostname
        return self.tls_socket


class RealityProbeTests(unittest.TestCase):
    @mock.patch("home_location_endpoint.reality_probe.socket.create_connection")
    @mock.patch("home_location_endpoint.reality_probe.ssl.create_default_context")
    def test_probe_requires_tls13_and_h2(self, create_context, create_connection):
        context = _FakeContext(_FakeTlsSocket())
        create_context.return_value = context
        create_connection.return_value = _FakeRawSocket()

        result = reality_probe.probe_reality_target(
            "www.example.com", "origin.example.com:443"
        )

        self.assertEqual(result["tls"], "TLSv1.3")
        self.assertEqual(result["alpn"], "h2")
        self.assertEqual(context.minimum_version, reality_probe.ssl.TLSVersion.TLSv1_3)
        self.assertEqual(context.alpn_protocols, ["h2"])
        self.assertEqual(context.server_hostname, "www.example.com")
        create_connection.assert_called_once_with(("origin.example.com", 443), timeout=10.0)

    @mock.patch("home_location_endpoint.reality_probe.socket.create_connection")
    @mock.patch("home_location_endpoint.reality_probe.ssl.create_default_context")
    def test_probe_rejects_missing_h2(self, create_context, create_connection):
        create_context.return_value = _FakeContext(_FakeTlsSocket(alpn=None))
        create_connection.return_value = _FakeRawSocket()

        with self.assertRaisesRegex(ValueError, "HTTP/2"):
            reality_probe.probe_reality_target(
                "www.example.com", "origin.example.com:443"
            )


if __name__ == "__main__":
    unittest.main()
