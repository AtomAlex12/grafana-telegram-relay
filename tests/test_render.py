import copy

import pytest
from jinja2.exceptions import SecurityError

from app.render import (DEFAULT_TEMPLATE, SAMPLE_PAYLOAD, TG_LIMIT, check_template, f_dt, f_duration,
                        render_messages)


def test_sample_grouped_into_one_message():
    msgs = render_messages(DEFAULT_TEMPLATE, SAMPLE_PAYLOAD, {"split": False})
    assert len(msgs) == 1
    text, all_resolved = msgs[0]
    assert "HighCPU" in text and "DiskFull" in text
    assert "──────────" in text
    assert all_resolved is False


def test_split_one_message_per_alert():
    msgs = render_messages(DEFAULT_TEMPLATE, SAMPLE_PAYLOAD, {"split": True})
    assert [r for _, r in msgs] == [False, True]
    firing, resolved = msgs[0][0], msgs[1][0]
    assert firing.startswith("🔥 <b>HighCPU</b>")
    assert "АВАРИЯ" in firing
    assert resolved.startswith("✅ <b>DiskFull</b>")
    assert "РЕШЕНО" in resolved
    assert "• mountpoint: <code>/mnt/data</code>" in resolved
    assert '<a href="http://grafana.local/d/xyz?viewPanel=2">' in firing


def test_hidden_labels_are_not_listed():
    text = render_messages(DEFAULT_TEMPLATE, SAMPLE_PAYLOAD, {"split": True})[0][0]
    assert "grafana_folder" not in text
    assert "• alertname" not in text


def test_label_values_are_html_escaped():
    payload = copy.deepcopy(SAMPLE_PAYLOAD)
    payload["alerts"][0]["labels"]["instance"] = "<script>alert(1)</script>"
    text = render_messages(DEFAULT_TEMPLATE, payload, {"split": True})[0][0]
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_non_grafana_payload_uses_message():
    assert render_messages(DEFAULT_TEMPLATE, {"message": "disk <full>"}) == [("disk &lt;full&gt;", False)]


def test_empty_payload_dumps_json():
    assert render_messages(DEFAULT_TEMPLATE, {}) == [("{}", False)]


def test_empty_render_produces_no_message():
    assert render_messages("{% if false %}x{% endif %}", SAMPLE_PAYLOAD) == []


def test_long_message_is_truncated():
    text = render_messages(DEFAULT_TEMPLATE, {"message": "x" * 10_000})[0][0]
    assert len(text) <= TG_LIMIT
    assert text.endswith("…(обрезано)")


def test_context_variables():
    tpl = "{{ status }}|{{ firing | length }}|{{ resolved | length }}|{{ route }}|{{ externalURL }}"
    assert render_messages(tpl, SAMPLE_PAYLOAD, {"name": "r1"})[0][0] == "firing|1|1|r1|http://grafana.local/"


def test_status_is_per_alert_when_split():
    msgs = render_messages("{{ status }}", SAMPLE_PAYLOAD, {"split": True})
    assert [t for t, _ in msgs] == ["firing", "resolved"]


def test_dt_filter():
    assert f_dt("2026-10-03T15:02:00Z", "%H:%M") == "15:02"  # TZ defaults to UTC in tests
    assert f_dt("2026-10-03T15:02:00.123456789Z", "%S") == "00"  # nanoseconds from Grafana
    assert f_dt("0001-01-01T00:00:00Z") == ""
    assert f_dt(None) == ""
    assert f_dt("garbage") == ""


def test_duration_filter():
    resolved = {"status": "resolved", "startsAt": "2026-10-03T13:40:00Z", "endsAt": "2026-10-03T14:55:00Z"}
    assert f_duration(resolved) == "1 ч 15 мин"
    short = {"status": "resolved", "startsAt": "2026-10-03T13:40:00Z", "endsAt": "2026-10-03T13:40:42Z"}
    assert f_duration(short) == "42 с"
    days = {"status": "resolved", "startsAt": "2026-10-01T10:00:00Z", "endsAt": "2026-10-03T12:30:00Z"}
    assert f_duration(days) == "2 д 2 ч 30 мин"
    assert f_duration({"status": "firing"}) == ""
    assert f_duration("nope") == ""


def test_sandbox_blocks_python_internals():
    with pytest.raises(SecurityError):
        check_template("{{ ''.__class__.__mro__[1].__subclasses__() }}")


def test_check_template_accepts_default():
    check_template(DEFAULT_TEMPLATE)
