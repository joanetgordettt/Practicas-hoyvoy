"""Vigila una web y envía un correo cuando aparece contenido nuevo."""
import json
import os
import re
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = os.environ.get("TARGET_URL", "https://fke12wzwjp.zite.so")
STATE = Path("state.json")
# Líneas a ignorar (regex separadas por "||"), p. ej. relojes o contadores
IGNORE = [re.compile(p) for p in os.environ.get("IGNORE_REGEX", "").split("||") if p]


def fetch_lines():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(3000)  # margen para que cargue el contenido dinámico
        text = page.inner_text("body")
        links = page.eval_on_selector_all(
            "a[href]", "els => els.map(e => (e.innerText.trim() + ' -> ' + e.href))"
        )
        browser.close()

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    lines += [l for l in links if l.strip(" ->")]
    lines = [l for l in lines if not any(r.search(l) for r in IGNORE)]
    return list(dict.fromkeys(lines))  # sin duplicados, conserva el orden


def send_email(new_lines):
    msg = EmailMessage()
    msg["Subject"] = f"Nuevas prácticas detectadas ({len(new_lines)} cambios)"
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["MAIL_TO"]
    body = "Se ha detectado contenido nuevo en la web:\n\n"
    body += "\n".join(f"• {l}" for l in new_lines)
    body += f"\n\nWeb: {URL}"
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)


def main():
    current = fetch_lines()
    if not current:
        print("No se pudo leer contenido; no toco el estado.")
        sys.exit(1)

    if not STATE.exists():
        STATE.write_text(json.dumps(current, ensure_ascii=False, indent=2))
        print(f"Primera ejecución: guardadas {len(current)} líneas, sin enviar correo.")
        return

    previous = set(json.loads(STATE.read_text()))
    new_lines = [l for l in current if l not in previous]

    if new_lines:
        print(f"{len(new_lines)} líneas nuevas, enviando correo.")
        send_email(new_lines)
        STATE.write_text(json.dumps(current, ensure_ascii=False, indent=2))
    else:
        print("Sin cambios.")


if __name__ == "__main__":
    main()
