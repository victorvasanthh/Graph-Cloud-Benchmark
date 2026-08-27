#!/usr/bin/env python3
"""Find out *where* a connection to a target fails.

    python scripts/diagnose_target.py cognodb-cloud

`ConnectionResetError` from the driver is a symptom with many causes: wrong
port, no TLS, wrong TLS, a proxy with no healthy backend, an IP allowlist, or a
stopped instance. The driver cannot tell them apart, so it reports the one
thing it knows and the reader guesses.

This walks the stack one layer at a time and reports the first layer that
fails, which is usually enough to name the cause:

    DNS  ->  TCP  ->  TLS  ->  Bolt handshake  ->  driver connect  ->  query

Credentials come from .env through the normal config loader and are never
printed, logged, or included in any error message this script produces. The
password is passed to the driver and nowhere else.
"""

from __future__ import annotations

import argparse
import socket
import ssl
import struct
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from benchmark.core.config import load_config  # noqa: E402

#: Bolt's magic preamble, then four version proposals. A Bolt server answers
#: with four bytes naming the version it agreed to. Anything else is not Bolt.
BOLT_MAGIC = bytes.fromhex("6060B017")
BOLT_PROPOSALS = struct.pack(">4I", 0x0004_0405, 0x0000_0104, 0x0000_0004, 0x0000_0003)

OK, FAIL, SKIP = "ok  ", "FAIL", "skip"


def line(status: str, label: str, detail: str = "") -> None:
    print(f"  [{status}] {label}{(' - ' + detail) if detail else ''}")


def endpoint_of(uri: str) -> tuple[str, int, bool]:
    """(host, port, tls_expected) from a Bolt URI, without touching credentials."""
    parsed = urlparse(uri)
    tls = parsed.scheme.endswith(("+s", "+ssc"))
    return parsed.hostname or "", parsed.port or 7687, tls


def check_dns(host: str) -> list[str]:
    try:
        addresses = sorted({info[4][0] for info in socket.getaddrinfo(host, None)})
        line(OK, "DNS", f"{host} -> {', '.join(addresses)}")
        return addresses
    except socket.gaierror as exc:
        line(FAIL, "DNS", f"{host} does not resolve ({exc.strerror or exc})")
        return []


def check_tcp(host: str, port: int, timeout: float) -> bool:
    started = time.monotonic()
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        line(OK, "TCP", f"{host}:{port} accepted in {(time.monotonic() - started) * 1000:.0f} ms")
        return True
    except Exception as exc:
        line(FAIL, "TCP", f"{host}:{port} {type(exc).__name__}: {exc}")
        return False


def check_tls(host: str, port: int, timeout: float) -> bool:
    context = ssl.create_default_context()
    try:
        with (
            socket.create_connection((host, port), timeout=timeout) as raw,
            context.wrap_socket(raw, server_hostname=host) as tls,
        ):
            cert = tls.getpeercert() or {}
            subject = dict(x[0] for x in cert.get("subject", ()))
            line(OK, "TLS", f"{tls.version()}, cert CN={subject.get('commonName', '?')}")
            return True
    except ssl.SSLError as exc:
        line(FAIL, "TLS", f"handshake rejected: {exc}")
    except Exception as exc:
        # A reset here, with TCP already proven open, means the listener does
        # not speak TLS at all - or never gets far enough to try.
        line(FAIL, "TLS", f"{type(exc).__name__}: {exc}")
    return False


def check_bolt(host: str, port: int, timeout: float, tls: bool) -> bool:
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
        if tls:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            raw = context.wrap_socket(raw, server_hostname=host)
        with raw as sock:
            sock.sendall(BOLT_MAGIC + BOLT_PROPOSALS)
            sock.settimeout(timeout)
            reply = sock.recv(4)
        if len(reply) != 4:
            line(FAIL, "Bolt", f"expected a 4-byte version reply, got {len(reply)} bytes")
            return False
        if reply == b"\x00\x00\x00\x00":
            line(FAIL, "Bolt", "server is Bolt but agreed none of our versions")
            return False
        line(OK, "Bolt", f"server agreed Bolt {reply[3]}.{reply[2]}")
        return True
    except Exception as exc:
        line(FAIL, "Bolt", f"{type(exc).__name__}: {exc}")
        return False


def check_driver(target) -> bool:
    try:
        from neo4j import GraphDatabase
    except ImportError:
        line(SKIP, "driver", "neo4j package not installed")
        return False

    uri = target.settings.get("uri", "")
    username = target.settings.get("username", "")
    password = target.settings.get("password", "")
    auth = (username, password) if username else None
    try:
        driver = GraphDatabase.driver(uri, auth=auth)
        try:
            driver.verify_connectivity()
            line(OK, "driver", "verify_connectivity succeeded")
            with driver.session() as session:
                value = session.run("RETURN 1 AS ok").single()[0]
            line(OK, "query", f"RETURN 1 -> {value}")
            return True
        finally:
            driver.close()
    except Exception as exc:
        # The message can echo the URI but never the password: the driver does
        # not put credentials in exception text, and nothing here adds them.
        line(FAIL, "driver", f"{type(exc).__name__}: {exc}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", help="target name from config/databases.yaml")
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--config-dir", type=Path, default=REPO_ROOT / "config")
    args = parser.parse_args()

    config = load_config(config_dir=args.config_dir)
    target = next((t for t in config.targets if t.name == args.target), None)
    if target is None:
        print(f"unknown target {args.target!r}", file=sys.stderr)
        return 2
    if not target.available:
        print(f"{args.target} is not configured ({', '.join(target.missing)})", file=sys.stderr)
        return 2

    uri = target.settings.get("uri") or target.settings.get("url") or ""
    host, port, tls_expected = endpoint_of(uri)
    print(f"diagnosing {args.target}")
    print(f"  endpoint {host}:{port}  TLS expected: {tls_expected}")
    print()

    if not check_dns(host):
        print("\nfirst failure: DNS. The hostname does not exist.")
        return 1
    if not check_tcp(host, port, args.timeout):
        print("\nfirst failure: TCP. Nothing is listening, or a firewall drops it.")
        return 1

    tls_ok = check_tls(host, port, args.timeout) if tls_expected else SKIP
    if tls_expected and not tls_ok:
        print(
            "\nfirst failure: TLS, with TCP already open.\n"
            "  Something accepts the connection but will not complete a TLS\n"
            "  handshake. Either the endpoint is not serving TLS on this port\n"
            "  (try the bolt:// scheme), or a proxy is accepting connections\n"
            "  with no healthy backend behind it."
        )
        # Still worth knowing whether it speaks plaintext Bolt.
        check_bolt(host, port, args.timeout, tls=False)
        return 1

    if not check_bolt(host, port, args.timeout, tls=tls_expected):
        print("\nfirst failure: Bolt handshake. The port is open and TLS works,")
        print("  but the service behind it is not speaking Bolt.")
        return 1

    if not check_driver(target):
        print("\nfirst failure: driver. The transport is fine, so this is")
        print("  authentication or an unsupported protocol version.")
        return 1

    print("\nall layers passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
