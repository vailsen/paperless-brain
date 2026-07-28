"""iCal date handling — the three time forms RFC 5545 allows.

An event at 17:00 Berlin time is exported by Google as 15:00Z. The parser used
to strip the trailing Z and print the number as-is, so every timed event from a
UTC feed showed up two hours early in summer and one in winter. TZID feeds had
the opposite problem: the parameter was discarded before the parser saw it, so a
foreign zone was rendered as if it were already local.
"""

import pytest

from config.settings import settings
from services.caldav_service import _parse_ical_date, _parse_ical_date_raw, _parse_ical_text


@pytest.fixture(autouse=True)
def berlin():
    """Pin the display timezone so the assertions do not depend on the host."""
    before = settings.tz
    settings.tz = "Europe/Berlin"
    yield
    settings.tz = before


def test_utc_is_converted_not_stripped():
    """The reported bug: 15:00Z is 17:00 in Berlin during CEST."""
    assert _parse_ical_date("", "20260728T150000Z") == "28.07.2026 17:00"


def test_utc_in_winter_uses_the_other_offset():
    """CET, not CEST — a fixed +2 would be wrong half the year."""
    assert _parse_ical_date("", "20260128T150000Z") == "28.01.2026 16:00"


def test_utc_conversion_can_change_the_date():
    assert _parse_ical_date("", "20260728T230000Z") == "29.07.2026 01:00"


def test_tzid_is_honoured():
    """New York 09:00 is 15:00 in Berlin."""
    got = _parse_ical_date("TZID=America/New_York", "20260728T090000")
    assert got == "28.07.2026 15:00"


def test_floating_time_is_left_alone():
    """No Z, no TZID — already local by definition."""
    assert _parse_ical_date("", "20260728T170000") == "28.07.2026 17:00"


def test_all_day_events_are_not_shifted():
    """A date has no time to convert; treating it as midnight UTC would move it."""
    assert _parse_ical_date("", "20260728") == "28.07.2026"


def test_unknown_tzid_falls_back_to_local():
    """Outlook emits Windows zone names that zoneinfo cannot resolve."""
    got = _parse_ical_date('TZID="W. Europe Standard Time"', "20260728T170000")
    assert got == "28.07.2026 17:00"


def test_unparseable_value_is_passed_through():
    assert _parse_ical_date("", "not-a-date") == "not-a-date"


def test_sort_key_is_local_and_comparable_across_forms():
    """_filter_and_sort compares this key against local YYYY-MM-DD bounds, so the
    same instant must produce the same key however the feed expressed it."""
    from_utc = _parse_ical_date_raw("", "20260728T150000Z")
    from_tzid = _parse_ical_date_raw("TZID=Europe/Berlin", "20260728T170000")
    assert from_utc == from_tzid == "20260728T170000"


def test_sort_key_orders_across_a_utc_midnight():
    """23:00Z on the 28th is the 29th locally and must sort after 22:00 local."""
    late = _parse_ical_date_raw("", "20260728T230000Z")
    earlier = _parse_ical_date_raw("", "20260728T200000")
    assert earlier < late


def test_parser_reads_tzid_off_the_property_line():
    """End to end: the TZID lives on the key side of the colon, and the parser
    has to hand it to the date conversion."""
    ical = (
        "BEGIN:VCALENDAR\r\n"
        "BEGIN:VEVENT\r\n"
        "SUMMARY:Zahnarzt\r\n"
        "DTSTART;TZID=America/New_York:20260728T090000\r\n"
        "DTEND;TZID=America/New_York:20260728T100000\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    (event,) = _parse_ical_text(ical)
    assert event["dtstart"] == "28.07.2026 15:00"
    assert event["dtend"] == "28.07.2026 16:00"


def test_parser_converts_a_google_style_utc_event():
    ical = (
        "BEGIN:VCALENDAR\r\n"
        "BEGIN:VEVENT\r\n"
        "SUMMARY:Termin\r\n"
        "DTSTART:20260728T150000Z\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    (event,) = _parse_ical_text(ical)
    assert event["dtstart"] == "28.07.2026 17:00"
    assert event["dtstart_raw"] == "20260728T170000"
