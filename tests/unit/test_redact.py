import pytest

from feedlab.redact import redact_line, redact_text

TS19 = "1762858123456789012"


def line(ts=TS19, host="debian-vm", lat="4210"):
    return f"QUOTE sym=BTCUSDT u=101 bid=67000.10 bid_qty=0.5 ask=67000.20 ask_qty=1.2 proc_ts={ts} host={host} latency_ns={lat}"


def test_variable_fields_are_masked():
    assert redact_line(line()) == (
        "QUOTE sym=BTCUSDT u=101 bid=67000.10 bid_qty=0.5 ask=67000.20 ask_qty=1.2 "
        "proc_ts=<TS> host=<HOST> latency_ns=<LAT>"
    )


@pytest.mark.parametrize("ts", ["176285812345678901", "17628581234567890123", "", "abc"])
def test_damaged_timestamp_stays_visible(ts):
    assert f"proc_ts={ts} " in redact_line(line(ts=ts))


@pytest.mark.parametrize("kept", ["sym=BTCUSDT", "u=101", "bid=67000.10", "bid_qty=0.5", "ask=67000.20", "ask_qty=1.2"])
def test_business_fields_are_never_masked(kept):
    assert kept in redact_line(line())


def test_price_with_19_digits_is_not_a_timestamp():
    price = "1234567890123456789"
    out = redact_line(f"QUOTE sym=X u=1 bid={price} bid_qty=1 ask={price}1 ask_qty=1 proc_ts={TS19} host=h latency_ns=1")
    assert f"bid={price} " in out


def test_sequence_number_with_19_digits_is_not_masked():
    out = redact_line(f"GAP sym=X expected={TS19} received={TS19} proc_ts={TS19} host=h latency_ns=1")
    assert out == f"GAP sym=X expected={TS19} received={TS19} proc_ts=<TS> host=<HOST> latency_ns=<LAT>"


@pytest.mark.parametrize("host", ["a", "ip-10-0-0-1.eu-west-3.compute.internal", "HOST_42"])
def test_any_host_is_masked(host):
    assert "host=<HOST>" in redact_line(line(host=host))


def test_field_name_must_be_whole_word():
    assert redact_line(f"x my_proc_ts={TS19} myhost=a") == f"x my_proc_ts={TS19} myhost=a"


def test_redaction_is_idempotent():
    once = redact_line(line())
    assert redact_line(once) == once


def test_redact_text_keeps_line_structure():
    text = line() + "\n" + line(ts="1") + "\n"
    out = redact_text(text)
    assert out.count("\n") == 2
    assert "proc_ts=1 " in out
