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


# --------------------------------------------------------------------------
# The asyncio loop, not only the socket
# --------------------------------------------------------------------------
# Guarding socket.connect alone is not enough on Windows: the Proactor event
# loop opens its connections through overlapped I/O and never calls it, so
# every async httpx request went straight past the guard. It looked installed,
# and was - and live Groq calls were still being made and answered inside the
# suite. Found when a scope check reached the real model and failed a test
# that passes when it cannot. The test above is synchronous, which is exactly
# why it never noticed.


def _every_cause(error: BaseException):
    """Flatten an exception and anything it wraps, groups included.

    httpx reports the refusal as "unhandled errors in a TaskGroup", so the
    guard's own words are only found by walking in.
    """
    yield error
    for nested in getattr(error, "exceptions", ()) or ():
        yield from _every_cause(nested)
    for nested in (error.__cause__, error.__context__):
        if nested is not None:
            yield from _every_cause(nested)


@pytest.mark.asyncio
async def test_an_async_client_cannot_reach_a_provider():
    import httpx

    with pytest.raises(Exception) as refused:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post("https://api.groq.com/openai/v1/chat/completions", json={})

    # The address, not the name: httpx resolves before it asks the loop to
    # connect, so by here the host is an IP. The refusal is the point.
    said = " ".join(str(e) for e in _every_cause(refused.value))
    assert "must not use the network" in said, said[:400]


@pytest.mark.asyncio
async def test_the_model_helpers_come_back_empty_rather_than_answered():
    """What the suite actually depends on: no provider answers a test.

    The prompt has to be one a provider would really answer with an object.
    A vague one comes back as prose, so `structured` returns None whether the
    guard is holding or not - a test that passes either way, which is the
    failure this whole file exists to catch.
    """
    from app.services import understanding

    prompt = (
        "A customer wrote: 'I need a roof replacement in Seattle.'\n"
        'Return ONLY this JSON: {"job": "<the work in a few words>", "fits": true | false}'
    )
    assert await understanding.structured(prompt, 6) is None
