"""Rendering of Grafana webhook payloads into Telegram HTML messages."""
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from jinja2.sandbox import SandboxedEnvironment

try:
    TZ = ZoneInfo(os.environ.get("TZ") or "UTC")
except Exception:  # unknown TZ name
    TZ = timezone.utc

TG_LIMIT = 4096

DEFAULT_TEMPLATE = """\
{% for a in alerts %}
{% if a.status == 'firing' %}🔥{% else %}✅{% endif %} <b>{{ a.labels.alertname }}</b> · {{ 'АВАРИЯ' if a.status == 'firing' else 'РЕШЕНО' }}
{% if a.labels.severity %}Важность: <b>{{ a.labels.severity }}</b>
{% endif %}
{% if a.annotations.summary %}
{{ a.annotations.summary }}
{% endif %}
{% if a.annotations.description %}
<i>{{ a.annotations.description }}</i>
{% endif %}

{% for k, v in a.labels | dictsort %}
{% if k not in ['alertname', 'severity', 'grafana_folder', '__alert_rule_uid__'] %}
• {{ k }}: <code>{{ v }}</code>
{% endif %}
{% endfor %}
{% if a['values'] %}
📈 {% for k, v in a['values'] | dictsort %}{{ k }} = <code>{{ v }}</code>{% if not loop.last %}, {% endif %}{% endfor %}

{% endif %}
🕒 {{ a.startsAt | dt }}{% if a.status == 'resolved' %} → {{ a.endsAt | dt }}{% endif %} ({{ a | duration }})
{% if a.panelURL or a.dashboardURL or a.generatorURL %}
<a href="{{ a.panelURL or a.dashboardURL or a.generatorURL }}">Открыть в Grafana</a>
{% endif %}
{% if not loop.last %}

──────────

{% endif %}
{% endfor %}
{% if not alerts %}
{{ message or title or (payload | tojson) }}
{% endif %}
"""

SAMPLE_PAYLOAD = {
    "receiver": "telegram-relay",
    "status": "firing",
    "orgId": 1,
    "alerts": [
        {
            "status": "firing",
            "labels": {"alertname": "HighCPU", "severity": "critical", "instance": "web-01:9100",
                       "grafana_folder": "Infra"},
            "annotations": {"summary": "Загрузка CPU выше 90%",
                            "description": "Процессор на web-01 загружен на 95% последние 5 минут"},
            "startsAt": "2026-10-03T15:02:00Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": "http://grafana.local/alerting/grafana/abc/view",
            "fingerprint": "1a2b3c4d",
            "panelURL": "http://grafana.local/d/xyz?viewPanel=2",
            "values": {"A": 95.2},
            "valueString": "[ var='A' labels={instance=web-01:9100} value=95.2 ]",
        },
        {
            "status": "resolved",
            "labels": {"alertname": "DiskFull", "severity": "warning", "instance": "storage-01:9100",
                       "mountpoint": "/mnt/data"},
            "annotations": {"summary": "Диск заполнен более чем на 85%"},
            "startsAt": "2026-10-03T13:40:00Z",
            "endsAt": "2026-10-03T14:55:00Z",
            "generatorURL": "http://grafana.local/alerting/grafana/def/view",
            "fingerprint": "5e6f7a8b",
            "values": {"B": 71.4},
        },
    ],
    "groupLabels": {"alertname": "HighCPU"},
    "commonLabels": {},
    "commonAnnotations": {},
    "externalURL": "http://grafana.local/",
    "title": "[FIRING:1, RESOLVED:1]",
    "message": "Тестовое сообщение",
}

_FRACTION = re.compile(r"(\.\d{6})\d+")


def _parse_ts(ts) -> datetime | None:
    if not isinstance(ts, str) or not ts or ts.startswith("0001-"):
        return None
    try:
        return datetime.fromisoformat(_FRACTION.sub(r"\1", ts.replace("Z", "+00:00")))
    except ValueError:
        return None


def f_dt(ts, fmt: str = "%d.%m %H:%M:%S") -> str:
    d = _parse_ts(ts)
    return d.astimezone(TZ).strftime(fmt) if d else ""


def f_duration(alert) -> str:
    if not isinstance(alert, dict):
        return ""
    start = _parse_ts(alert.get("startsAt"))
    if not start:
        return ""
    end = _parse_ts(alert.get("endsAt")) if alert.get("status") == "resolved" else None
    sec = max(int(((end or datetime.now(timezone.utc)) - start).total_seconds()), 0)
    days, rem = divmod(sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = [f"{days} д" if days else "", f"{hours} ч" if hours else "", f"{minutes} мин" if minutes else ""]
    if not days and not hours and not minutes:
        parts.append(f"{seconds} с")
    return " ".join(p for p in parts if p)


env = SandboxedEnvironment(autoescape=True, trim_blocks=True, lstrip_blocks=True)
env.filters["dt"] = f_dt
env.filters["duration"] = f_duration


def _clean(text: str) -> str:
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > TG_LIMIT:
        text = text[: TG_LIMIT - 20].rstrip() + "\n…(обрезано)"
    return text


def check_template(source: str) -> None:
    """Raises jinja2 errors if the template does not compile or fails on the sample payload."""
    render_messages(source, SAMPLE_PAYLOAD, {"split": False, "name": "test"})


def render_messages(source: str, payload: dict, route: dict | None = None) -> list[tuple[str, bool]]:
    """Returns a list of (text, all_resolved) — one item per Telegram message."""
    tpl = env.from_string(source)
    raw_alerts = payload.get("alerts")
    alerts = [a for a in raw_alerts if isinstance(a, dict)] if isinstance(raw_alerts, list) else []
    split = bool(route and route.get("split")) and len(alerts) > 1
    groups = [[a] for a in alerts] if split else [alerts]

    out = []
    for group in groups:
        ctx = dict(payload)
        ctx.update(
            payload=payload,
            alerts=group,
            firing=[a for a in group if a.get("status") == "firing"],
            resolved=[a for a in group if a.get("status") == "resolved"],
            status=group[0].get("status") if split else payload.get("status"),
            route=(route or {}).get("name", ""),
        )
        text = _clean(tpl.render(**ctx))
        if text:
            all_resolved = bool(group) and all(a.get("status") == "resolved" for a in group)
            out.append((text, all_resolved))
    return out
