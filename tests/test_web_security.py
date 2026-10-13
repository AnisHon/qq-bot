from __future__ import annotations

import asyncio
import ipaddress
import os
import shutil
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpcore
import httpx

from app.tools.url_policy import (
    ApprovedTarget,
    FetchLimitError,
    PinnedAsyncNetworkBackend,
    SafeHTTPFetcher,
    SafeHTTPResponse,
    URLPolicyError,
    create_pinned_transport,
    normalize_url,
    redact_url,
    resolve_approved_target,
)
from app.tools import external


class AsyncBytes(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


def streamed_response(
    status_code: int,
    request: httpx.Request,
    content: bytes = b"",
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return httpx.Response(
        status_code,
        headers=headers,
        stream=AsyncBytes([content]),
        request=request,
    )


class FakeResolver:
    def __init__(self, answers: dict[str, object]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, int]] = []

    async def resolve(self, hostname: str, port: int):
        self.calls.append((hostname, port))
        answer = self.answers[hostname]
        if callable(answer):
            answer = answer(len([call for call in self.calls if call[0] == hostname]))
        return answer


class URLPolicyTest(unittest.IsolatedAsyncioTestCase):
    def test_rejects_non_public_literals_and_legacy_ip_spellings(self) -> None:
        cases = (
            "http://127.0.0.1/",
            "http://10.1.2.3/",
            "http://169.254.169.254/",
            "http://[::1]/",
            "http://[fe80::1]/",
            "http://[fd00::1]/",
            "http://[::ffff:127.0.0.1]/",
            "http://127.1/",
            "http://0177.0.0.1/",
            "http://2130706433/",
            "http://0x7f000001/",
            "http://192.0.2.10/",
            "http://192.0.0.9/",
            "http://198.18.0.1/",
            "http://[2001::1]/",
            "http://[2002::1]/",
            "http://[64:ff9b::808:808]/",
        )
        for value in cases:
            with self.subTest(url=value), self.assertRaises(URLPolicyError):
                normalize_url(value)

    def test_rejects_encoded_authority_userinfo_and_nonstandard_ports(self) -> None:
        cases = (
            "http://%31%32%37.0.0.1/",
            "http://user:pass@example.com/",
            "http://example.com:8080/",
            "http://example.com:0/",
            "file:///etc/passwd",
            "http://example.com\\@127.0.0.1/",
        )
        for value in cases:
            with self.subTest(url=value), self.assertRaises(URLPolicyError):
                normalize_url(value)

    def test_redacts_common_secret_query_keys_and_userinfo(self) -> None:
        redacted = redact_url(
            "https://user:synthetic@example.com/?x-api-key=key-value&accessToken=bearer-value&view=full"
        )
        self.assertNotIn("synthetic", redacted)
        self.assertNotIn("key-value", redacted)
        self.assertNotIn("bearer-value", redacted)
        self.assertIn("view=full", redacted)

    async def test_idna_hostname_is_normalized_before_resolution(self) -> None:
        normalized = normalize_url("https://例子.测试/manual#fragment")
        expected = "例子.测试".encode("idna").decode("ascii")
        self.assertEqual(normalized.host, expected)
        self.assertEqual(normalized.url, f"https://{expected}/manual")

        resolver = FakeResolver({expected: ["8.8.8.8"]})
        target = await resolve_approved_target(normalized.url, resolver)
        self.assertEqual(target.address, ipaddress.ip_address("8.8.8.8"))
        self.assertEqual(resolver.calls, [(expected, 443)])

    async def test_mixed_public_and_private_dns_answers_fail_before_request(self) -> None:
        resolver = FakeResolver({"mixed.example": ["8.8.8.8", "10.0.0.8"]})
        with self.assertRaises(URLPolicyError):
            await resolve_approved_target("https://mixed.example/", resolver)
        self.assertEqual(resolver.calls, [("mixed.example", 443)])

    async def test_mock_transport_reads_bounded_identity_response(self) -> None:
        resolver = FakeResolver({"docs.example": ["93.184.216.34"]})
        requests: list[httpx.Request] = []

        def transport_factory(_target: ApprovedTarget) -> httpx.MockTransport:
            def handler(request: httpx.Request) -> httpx.Response:
                requests.append(request)
                return streamed_response(
                    200,
                    request=request,
                    headers={"Content-Type": "text/plain; charset=utf-8"},
                    content=b"readable page",
                )

            return httpx.MockTransport(handler)

        with patch.dict(
            os.environ,
            {
                "HTTP_PROXY": "http://127.0.0.1:9",
                "HTTPS_PROXY": "http://127.0.0.1:9",
            },
        ):
            with patch("app.tools.url_policy.httpx.AsyncClient", wraps=httpx.AsyncClient) as factory:
                response = await SafeHTTPFetcher(
                    resolver=resolver,
                    transport_factory=transport_factory,
                ).get("https://docs.example/path", timeout=2)
        self.assertFalse(factory.call_args.kwargs["trust_env"])
        self.assertFalse(factory.call_args.kwargs["follow_redirects"])

        self.assertEqual(response.body, b"readable page")
        self.assertEqual(response.text, "readable page")
        self.assertEqual(requests[0].url.host, "docs.example")
        self.assertEqual(requests[0].headers["host"], "docs.example")
        self.assertEqual(requests[0].headers["accept-encoding"], "identity")

    async def test_redirect_revalidates_destination_before_second_request(self) -> None:
        resolver = FakeResolver(
            {
                "public.example": ["8.8.8.8"],
                "internal.example": ["127.0.0.1"],
            }
        )
        dispatched: list[ApprovedTarget] = []

        def transport_factory(target: ApprovedTarget) -> httpx.MockTransport:
            dispatched.append(target)
            return httpx.MockTransport(
                lambda request: httpx.Response(
                    302,
                    headers={"Location": "http://internal.example/private"},
                    request=request,
                )
            )

        fetcher = SafeHTTPFetcher(resolver=resolver, transport_factory=transport_factory)
        with self.assertRaises(URLPolicyError):
            await fetcher.get("https://public.example/start", timeout=2)

        self.assertEqual(len(dispatched), 1)
        self.assertEqual(len(resolver.calls), 2)
        self.assertEqual(resolver.calls[-1][0], "internal.example")

    async def test_jina_reader_compatibility_shim_never_fetches_remote_target(self) -> None:
        with patch.object(external, "_bing_client", side_effect=AssertionError("network call")):
            results = await external._fetch_via_jina(
                "http://127.0.0.1/private?token=synthetic",
                timeout=1,
                reason="blocked",
            )
        self.assertEqual(len(results), 1)
        self.assertIn("已禁用", results[0].summary)
        self.assertNotIn("synthetic", results[0].url)

    async def test_short_page_does_not_use_reader_fallback(self) -> None:
        fetcher = AsyncMock()
        fetcher.get.return_value = SafeHTTPResponse(
            status_code=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=b"<html><title>short</title><body>x</body></html>",
            encoding="utf-8",
            url="https://docs.example/",
        )
        with (
            patch.object(external, "_SAFE_WEB_FETCHER", fetcher),
            patch.object(external, "_fetch_via_jina", new_callable=AsyncMock) as reader,
        ):
            results = await external.fetch_web_page("https://docs.example/")
        self.assertEqual(len(results), 1)
        reader.assert_not_awaited()

    def test_private_transport_contract_fails_closed_on_dependency_upgrade(self) -> None:
        target = ApprovedTarget(
            url="https://docs.example/",
            scheme="https",
            host="docs.example",
            port=443,
            address=ipaddress.ip_address("8.8.8.8"),
        )
        with patch("app.tools.url_policy.httpx.__version__", "0.29.0"):
            with self.assertRaisesRegex(RuntimeError, "reviewed HTTPX/httpcore versions"):
                create_pinned_transport(target)

    async def test_dns_rebinding_between_redirect_hops_is_blocked(self) -> None:
        answers = {"rebind.example": lambda count: ["8.8.8.8"] if count == 1 else ["192.168.1.10"]}
        resolver = FakeResolver(answers)
        dispatch_count = 0

        def transport_factory(_target: ApprovedTarget) -> httpx.MockTransport:
            nonlocal dispatch_count
            dispatch_count += 1
            return httpx.MockTransport(
                lambda request: httpx.Response(
                    302,
                    headers={"Location": "/again"},
                    request=request,
                )
            )

        with self.assertRaises(URLPolicyError):
            await SafeHTTPFetcher(
                resolver=resolver,
                transport_factory=transport_factory,
            ).get("https://rebind.example/start", timeout=2)

        self.assertEqual(dispatch_count, 1)
        self.assertEqual(len(resolver.calls), 2)

    async def test_redirect_cycles_and_limits_are_rejected(self) -> None:
        resolver = FakeResolver({"docs.example": ["8.8.8.8"]})
        fetcher = SafeHTTPFetcher(
            resolver=resolver,
            transport_factory=lambda _target: httpx.MockTransport(
                lambda request: httpx.Response(302, headers={"Location": "/start"}, request=request)
            ),
        )
        with self.assertRaises(URLPolicyError):
            await fetcher.get("https://docs.example/start", timeout=2)

        requests = 0

        def redirect_factory(_target: ApprovedTarget) -> httpx.MockTransport:
            def handler(request: httpx.Request) -> httpx.Response:
                nonlocal requests
                requests += 1
                return httpx.Response(
                    302,
                    headers={"Location": f"/hop-{requests}"},
                    request=request,
                )

            return httpx.MockTransport(handler)

        limited = SafeHTTPFetcher(
            resolver=resolver,
            transport_factory=redirect_factory,
            max_redirects=2,
        )
        with self.assertRaises(URLPolicyError):
            await limited.get("https://docs.example/start", timeout=2)
        self.assertEqual(requests, 3)

    async def test_total_timeout_includes_dns_resolution(self) -> None:
        class SlowResolver:
            async def resolve(self, _hostname: str, _port: int):
                await asyncio.sleep(0.05)
                return ["8.8.8.8"]

        fetcher = SafeHTTPFetcher(
            resolver=SlowResolver(),
            transport_factory=lambda _target: httpx.MockTransport(
                lambda request: httpx.Response(200, request=request)
            ),
        )
        with self.assertRaises(TimeoutError):
            await fetcher.get("https://docs.example/", timeout=0.005)

    async def test_compressed_or_oversized_response_is_rejected(self) -> None:
        resolver = FakeResolver({"docs.example": ["8.8.8.8"]})

        compressed = SafeHTTPFetcher(
            resolver=resolver,
            transport_factory=lambda _target: httpx.MockTransport(
                lambda request: streamed_response(
                    200,
                    request=request,
                    headers={"Content-Encoding": "gzip"},
                    content=b"compressed",
                )
            ),
        )
        with self.assertRaises(FetchLimitError):
            await compressed.get("https://docs.example/", timeout=2)

        oversized = SafeHTTPFetcher(
            resolver=resolver,
            transport_factory=lambda _target: httpx.MockTransport(
                lambda request: streamed_response(200, request=request, content=b"x" * 17)
            ),
            max_response_bytes=16,
        )
        with self.assertRaises(FetchLimitError):
            await oversized.get("https://docs.example/", timeout=2)


class PinnedConnectorTest(unittest.IsolatedAsyncioTestCase):
    async def test_connector_dials_only_the_pinned_peer(self) -> None:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.read(1)
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        backend = PinnedAsyncNetworkBackend(
            "fetch.example",
            port,
            ipaddress.ip_address("127.0.0.1"),
        )
        try:
            stream = await backend.connect_tcp("fetch.example", port, timeout=2)
            peer = stream.get_extra_info("server_addr")
            self.assertEqual(ipaddress.ip_address(peer[0]), ipaddress.ip_address("127.0.0.1"))
            await stream.aclose()
        finally:
            server.close()
            await server.wait_closed()

    async def test_connector_rejects_mismatched_actual_peer(self) -> None:
        class WrongPeerStream:
            closed = False

            def get_extra_info(self, _name: str):
                return ("8.8.8.8", 443)

            async def aclose(self) -> None:
                self.closed = True

        class WrongPeerBackend(httpcore.AsyncNetworkBackend):
            def __init__(self) -> None:
                self.stream = WrongPeerStream()

            async def connect_tcp(self, *_args, **_kwargs):
                return self.stream

        inner = WrongPeerBackend()
        backend = PinnedAsyncNetworkBackend(
            "fetch.example",
            443,
            ipaddress.ip_address("93.184.216.34"),
            backend=inner,
        )
        with self.assertRaises(httpcore.ConnectError):
            await backend.connect_tcp("fetch.example", 443)
        self.assertTrue(inner.stream.closed)

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL is required for the local TLS contract test")
    async def test_https_keeps_original_host_sni_host_header_and_certificate_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cert_path = Path(temp_dir) / "cert.pem"
            key_path = Path(temp_dir) / "key.pem"
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-keyout",
                    str(key_path),
                    "-out",
                    str(cert_path),
                    "-days",
                    "1",
                    "-subj",
                    "/CN=fetch.example",
                    "-addext",
                    "subjectAltName=DNS:fetch.example",
                    "-addext",
                    "basicConstraints=critical,CA:TRUE",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(certfile=cert_path, keyfile=key_path)
            server_names: list[str | None] = []
            received_headers: list[bytes] = []
            server_context.set_servername_callback(
                lambda _ssl, server_name, _context: server_names.append(server_name)
            )

            async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                received_headers.append(await reader.readuntil(b"\r\n\r\n"))
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                    b"Content-Length: 2\r\nConnection: close\r\n\r\nok"
                )
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_server(
                handle,
                "127.0.0.1",
                0,
                ssl=server_context,
            )
            port = server.sockets[0].getsockname()[1]
            target = ApprovedTarget(
                url=f"https://fetch.example:{port}/page",
                scheme="https",
                host="fetch.example",
                port=port,
                address=ipaddress.ip_address("127.0.0.1"),
            )
            client_context = ssl.create_default_context(cafile=str(cert_path))
            client = httpx.AsyncClient(
                transport=create_pinned_transport(target, verify=client_context),
                trust_env=False,
                follow_redirects=False,
                timeout=2,
            )
            wrong_target = ApprovedTarget(
                url=f"https://wrong.example:{port}/page",
                scheme="https",
                host="wrong.example",
                port=port,
                address=ipaddress.ip_address("127.0.0.1"),
            )
            wrong_client = httpx.AsyncClient(
                transport=create_pinned_transport(wrong_target, verify=client_context),
                trust_env=False,
                follow_redirects=False,
                timeout=2,
            )
            try:
                response = await client.get(target.url)
                self.assertEqual(response.text, "ok")
                self.assertEqual(server_names[0], "fetch.example")
                self.assertIn(b"host: fetch.example:", received_headers[0].lower())
                with patch.object(asyncio.base_events.logger, "error"):
                    with self.assertRaises(httpx.ConnectError):
                        await wrong_client.get(wrong_target.url)
                    await asyncio.sleep(0.01)
                self.assertEqual(server_names[-1], "wrong.example")
            finally:
                await client.aclose()
                await wrong_client.aclose()
                server.close()
                await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
