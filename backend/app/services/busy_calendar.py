"""The owner's own calendar, read for when they are busy.

A shop's diary in PingPulse only knows what PingPulse booked. Somebody selling
software books demos around a day that is already full - a dentist, a school
run, a call with an investor - and a demo offered on top of any of those is a
promise they cannot keep.

Google Calendar, Outlook and Apple all publish a calendar at a private
address in iCalendar format ("Secret address in iCal format" in Google's
settings). That address is read here, and every event on it is a time the
agent will not offer. No OAuth and no Google account are involved: the owner
pastes one link, and removing it stops everything.

Three rules:

* **Only busy is read.** Titles, guests and notes are never stored or shown;
  an event is a start and an end. Events marked free or cancelled don't count.
* **Our own events are ignored.** A calendar that has the PingPulse feed or
  our invites in it would otherwise report every booking back as busy, and a
  moved appointment would still block its old time.
* **Unreadable means unknown, never free.** If the calendar can't be read and
  there is no recent copy, no time is offered - saying a time is free that
  might not be is the thing this module exists to stop.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time as clock
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

CONFIG_KEY = "busy_calendar_url"

# A fresh read is at most this old. Short, because a meeting the owner adds by
# hand should stop being offered within minutes.
FRESH_SECONDS = 120
# How long an old copy may stand in when the calendar can't be reached. An
# hour-old picture of a calendar is still a better answer than none; a day-old
# one is not.
STALE_SECONDS = 3600
FETCH_TIMEOUT = 6
MAX_BYTES = 5_000_000
# How far ahead events are expanded. Bookings are only taken this far out.
HORIZON_DAYS = 190

OWN_UID_SUFFIX = "@pingpulse"


class Unreadable(Exception):
    """The owner's calendar is set but could not be read."""


@dataclass(frozen=True)
class Busy:
    """A span the owner is taken. Shaped like an appointment for clash checks."""

    starts_at: datetime
    ends_at: datetime
    id: None = None


@dataclass
class _Copy:
    busy: list
    read_at: float
    events: int


_cache: dict[str, _Copy] = {}
_locks: dict[str, asyncio.Lock] = {}


def url_of(organization) -> str:
    config = (getattr(organization, "agent_config", None) or {}).get("appointments") or {}
    return str(config.get(CONFIG_KEY) or "").strip()


def normalise(url: str) -> str:
    """The address as it will be fetched: webcal:// is https:// by another name."""
    url = (url or "").strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    return url


def problem_with(url: str) -> str | None:
    """Why this address can't be used, in words for the owner, or None."""
    parsed = urlparse(normalise(url))
    if parsed.scheme != "https" or not parsed.hostname:
        return "Use the calendar's private https:// (or webcal://) address."
    return None


def _public(host: str) -> bool:
    """Whether every address this host resolves to is on the public internet.

    The server fetches whatever is pasted, so an address inside the machine's
    own network - a metadata service, a database - must not be fetchable
    through this box.
    """
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            return False
    return bool(infos)


async def _download(url: str) -> bytes:
    host = urlparse(url).hostname or ""
    if not await asyncio.to_thread(_public, host):
        raise Unreadable("that address is not on the public internet")
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=False) as client:
        for _ in range(4):
            async with client.stream("GET", url, headers={"Accept": "text/calendar"}) as response:
                if response.is_redirect:
                    target = response.headers.get("location", "")
                    url = str(httpx.URL(url).join(target))
                    if problem_with(url) or not await asyncio.to_thread(
                        _public, urlparse(url).hostname or ""
                    ):
                        raise Unreadable("the calendar redirected somewhere it may not be read from")
                    continue
                if response.status_code != 200:
                    raise Unreadable(f"the calendar answered {response.status_code}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise Unreadable("the calendar is too large to read")
                return bytes(body)
    raise Unreadable("the calendar redirected too many times")


def _as_utc(value, zone) -> datetime:
    """A DTSTART/DTEND value as an aware UTC moment. Dates are whole local days."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=zone)
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=zone).astimezone(timezone.utc)
    raise ValueError("not a time")


def parse(body: bytes, zone, now: datetime | None = None) -> tuple[list[Busy], int]:
    """Busy spans from an iCalendar document, recurring events expanded."""
    import icalendar
    import recurring_ical_events

    try:
        calendar = icalendar.Calendar.from_ical(body)
    except Exception as exc:  # noqa: BLE001 - any parse failure means unreadable
        raise Unreadable("that address did not return a calendar") from exc

    moment = now or datetime.now(timezone.utc)
    start = moment - timedelta(days=1)
    end = moment + timedelta(days=HORIZON_DAYS)
    busy: list[Busy] = []
    events = 0
    for event in recurring_ical_events.of(calendar, skip_bad_series=True).between(start, end):
        events += 1
        uid = str(event.get("UID") or "")
        if uid.endswith(OWN_UID_SUFFIX):
            continue
        if str(event.get("STATUS") or "").upper() == "CANCELLED":
            continue
        if str(event.get("TRANSP") or "").upper() == "TRANSPARENT":
            continue
        try:
            begins = _as_utc(event.decoded("DTSTART"), zone)
            if event.get("DTEND") is not None:
                ends = _as_utc(event.decoded("DTEND"), zone)
            elif event.get("DURATION") is not None:
                ends = begins + event.decoded("DURATION")
            elif not isinstance(event.decoded("DTSTART"), datetime):
                ends = begins + timedelta(days=1)
            else:
                ends = begins
        except (KeyError, ValueError, TypeError):
            continue
        if ends > begins:
            busy.append(Busy(begins, ends))
    busy.sort(key=lambda span: span.starts_at)
    return busy, events


async def read(organization, *, fresh: bool = False) -> _Copy | None:
    """The owner's busy times, or None when no calendar is set. Raises Unreadable."""
    url = normalise(url_of(organization))
    if not url:
        return None
    if problem_with(url):
        raise Unreadable(problem_with(url))

    from app.services import agent_config

    zone = agent_config.zone_of(organization)
    key = f"{getattr(organization, 'id', '')}:{url}"
    copy = _cache.get(key)
    if copy and not fresh and clock.monotonic() - copy.read_at < FRESH_SECONDS:
        return copy

    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        copy = _cache.get(key)
        if copy and not fresh and clock.monotonic() - copy.read_at < FRESH_SECONDS:
            return copy
        try:
            body = await _download(url)
            busy, events = parse(body, zone)
        except (Unreadable, httpx.HTTPError, OSError) as exc:
            reason = str(exc) or type(exc).__name__
            if copy and clock.monotonic() - copy.read_at < STALE_SECONDS:
                logger.warning("owner calendar unreadable (%s); using a copy from earlier", reason)
                return copy
            logger.warning("owner calendar unreadable for %s: %s", getattr(organization, "id", "?"), reason)
            raise Unreadable(reason) from exc
        copy = _Copy(busy=busy, read_at=clock.monotonic(), events=events)
        _cache[key] = copy
        return copy


async def busy_between(organization, since: datetime, until: datetime) -> list[Busy]:
    """Busy spans overlapping this window. Empty when no calendar is set."""
    copy = await read(organization)
    if copy is None:
        return []
    return [span for span in copy.busy if span.ends_at > since and span.starts_at < until]


def forget(organization) -> None:
    """Drop cached copies, after the address changes."""
    prefix = f"{getattr(organization, 'id', '')}:"
    for key in [key for key in _cache if key.startswith(prefix)]:
        _cache.pop(key, None)
