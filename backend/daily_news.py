# daily_news.py
import os
import json
import boto3
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta
import zoneinfo

sns = boto3.client('sns')
TOPIC_ARN = os.environ['TOPIC_ARN']
AR_TZ = zoneinfo.ZoneInfo("America/Argentina/Buenos_Aires")

def fetch_items_rss(url: str):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "DailyNewsBot/1.0"})
        with urllib.request.urlopen(req, timeout=12) as r:
            xml = r.read()
        root = ET.fromstring(xml)
        out = []

        # RSS 2.0
        for it in root.findall(".//item"):
            title = (it.findtext("title") or "").strip()
            link  = (it.findtext("link") or "").strip()
            pub   = it.findtext("pubDate")
            dt_pub = parsedate_to_datetime(pub) if pub else None
            if title and link:
                out.append({"title": title, "link": link, "date": dt_pub})

        # Atom (fallback)
        if not out:
            ns = {"a": "http://www.w3.org/2005/Atom"}
            for it in root.findall(".//a:entry", ns):
                title = (it.findtext("a:title", default="", namespaces=ns) or "").strip()
                link_el = it.find("a:link", ns)
                link = link_el.attrib.get("href", "") if link_el is not None else ""
                updated = it.findtext("a:updated", default="", namespaces=ns)
                dt_pub = parsedate_to_datetime(updated) if updated else None
                if title and link:
                    out.append({"title": title, "link": link, "date": dt_pub})

        return out
    except Exception as e:
        print(f"[WARN] RSS error {url}: {e}")
        return []

def handler(event, context):
    feeds = json.loads(os.environ['FEEDS'])
    now_ar = datetime.now(AR_TZ)
    cutoff = now_ar - timedelta(days=1)
    max_per = int(os.environ.get("MAX_PER_SOURCE", "5"))

    sections = []
    for src in feeds:
        items = fetch_items_rss(src["url"])
        todays = []
        for it in items:
            if not it["date"]:
                continue
            dt_ar = it["date"].astimezone(AR_TZ) if it["date"].tzinfo else it["date"].replace(tzinfo=AR_TZ)
            if dt_ar >= cutoff:
                todays.append((dt_ar, it))

        # Filtro "Dólar" solo para El Cronista cuando se pide
        if src.get("onlyDollar"):
            todays = [t for t in todays if "dólar" in t[1]["title"].lower() or "dolar" in t[1]["title"].lower()]

        todays.sort(reverse=True, key=lambda x: x[0])
        todays = [it for _, it in todays[:max_per]]

        lines = [f"• {it['title']} — {it['link']}" for it in todays] if todays else ["(Sin novedades relevantes en las últimas 24 h)"]
        sections.append(f"{src['name']}:\n" + "\n".join(lines))

    subject = os.environ.get("EMAIL_SUBJECT", f"DailyNewsBot — {now_ar.strftime('%Y-%m-%d')}")
    body = f"""DailyNewsBot — Resumen del día ({now_ar.strftime('%Y-%m-%d')}, {now_ar.strftime('%H:%M')} AR)

""" + "\n\n".join(sections) + "\n\n— Enviado automáticamente."

    sns.publish(TopicArn=TOPIC_ARN, Subject=subject, Message=body)
    return {"ok": True, "sources": [s["name"] for s in feeds]}
