"""The suite must not reach the real internet, and must say so when it tries.

Three tests were found calling Groq and Gemini for real in one afternoon. Each
was billed, each was slow, and each passed or failed on somebody else's
uptime. The provider stubs did not catch them because they name call sites,
and those tests used call sites added later. This covers the connection
instead, so it holds for call sites nobody has written yet.
"""

from __future__ import annotations

import socket

import pytest


def test_connecting_out_fails_and_names_the_address():
    with pytest.raises(RuntimeError) as refused:
        socket.socket().connect(("142.250.185.0", 443))
    assert "142.250.185.0" in str(refused.value)
    assert "must not use the network" in str(refused.value)


def test_a_resolved_hostname_is_still_blocked_at_the_connection():
    """The lookup is allowed; the connection it was for is not."""
    where = socket.getaddrinfo("api.groq.com", 443, proto=socket.IPPROTO_TCP)
    assert where, "expected the name to resolve"
    family, _type, _proto, _canon, address = where[0]
    with pytest.raises(RuntimeError):
        socket.socket(family, socket.SOCK_STREAM).connect(address)


def test_looking_up_an_address_is_allowed():
    """The SSRF guards resolve on purpose, to refuse what points inward."""
    assert socket.getaddrinfo("169.254.169.254", 443, proto=socket.IPPROTO_TCP)


def test_loopback_still_works():
    """The event loop's own self-pipe is loopback; blocking it breaks asyncio."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.socket()
    client.connect(server.getsockname())  # must not raise
    client.close()
    server.close()


@pytest.mark.allow_network
def test_a_test_that_says_so_may_still_reach_out():
    """Opting out leaves the real socket in place.

    Asserted by identity rather than by connecting, so this does not need the
    internet to be up to prove the opt-out works.
    """
    assert socket.socket.connect.__name__ != "guarded_connect"
