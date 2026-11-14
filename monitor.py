"""Vigila la web de prácticas y envía un correo cuando aparecen clases nuevas."""
import html
import json
import os
import re
import smtplib
import sys
import tempfile
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

URL = os.environ.get("TARGET_URL", "https://fke12wzwjp.zite.so")
STATE = Path("state.json")
SUBJECT = "🚗 ¡Nuevas prácticas de coche disponibles!"
SUBJECT_SIN_CAMBIOS = "✅ Sin novedades en las prácticas"

# True = también te llega un correo cuando no hay novedades.
# Para dejar de recibirlos, cambia True por False.
AVISAR_SIN_CAMBIOS = True
# Cada cuántos minutos como mucho se envía el correo de "Sin novedades"
AVISAR_CADA_MIN = float(os.environ.get("AVISAR_CADA_MIN", "5"))
HEARTBEAT_FILE = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())) / "ultimo_aviso_sin_cambios"

DATE = r"\d{1,2}[-/.]\d{1,2}[-/.]\d{4}"
TIME = r"\d{1,2}:\d{2}\s*[-–—]\s*\d{1,2}:\d{2}"
CLASS_RE = re.compile(rf"({DATE})\s+({TIME})")


# ---------- lectura de la web ----------
def fetch_page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(3000)  # margen para que cargue el contenido dinámico
        text = page.inner_text("body")
        browser.close()
    return text


def parse_classes(text):
    """Busca filas tipo: fecha, hora, coche, profesor. Devuelve {clave: fila}."""
    matches = list(CLASS_RE.finditer(text))
    found = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        tokens = [t.strip() for t in re.split(r"[\t\n]+", text[m.end():end]) if t.strip()]
        fecha = m.group(1)
        hora = re.sub(r"\s*[-–—]\s*", "-", m.group(2))
        coche = tokens[0] if tokens else "—"
        profe = tokens[1] if len(tokens) > 1 else "—"
        found[f"{fecha}|{hora}|{coche}|{profe}"] = (fecha, hora, coche, profe)

    def sort_key(row):
        d, mth, y = re.split(r"[-/.]", row[0])
        return (int(y), int(mth), int(d), row[1])

    return dict(sorted(found.items(), key=lambda kv: sort_key(kv[1])))


# ---------- correo ----------
def build_html(rows, now):
    n = len(rows)
    th = "text-align:left;padding:16px 12px;font-weight:700;"
    td = "padding:16px 12px;"
    head = "".join(f'<th style="{th}">{h}</th>' for h in ("Fecha", "Hora", "Coche", "Profesor/a"))
    body = "".join(
        "<tr>" + "".join(f'<td style="{td}">{html.escape(c)}</td>' for c in r) + "</tr>"
        for r in rows
    )
    intro = (
        f"Hemos encontrado <b>{n} {'práctica nueva' if n == 1 else 'prácticas nuevas'}</b> "
        "disponible" + ("" if n == 1 else "s") + "."
    )
    return f"""\
<!DOCTYPE html>
<html lang="es"><body style="margin:0;padding:0;background:#ffffff;">
<div style="font-family:Arial,Helvetica,sans-serif;color:#1a1a1a;max-width:640px;
 margin:0 auto;padding:20px 16px;font-size:18px;line-height:1.5;">
 <p style="margin:0 0 24px;">{intro}</p>
 <table width="100%" cellpadding="0" cellspacing="0"
  style="border-collapse:collapse;font-size:17px;">
  <tr style="background:#f2f2f2;">{head}</tr>
  {body}
 </table>
 <p style="margin:28px 0 0;font-size:16px;">
  <a href="{html.escape(URL)}" style="color:#1a56db;">Abrir la web de prácticas</a></p>
 <p style="margin:20px 0 0;font-size:13px;color:#8a8a8a;">Detectado el {now}</p>
</div></body></html>"""


def build_text(rows, now):
    out = [SUBJECT, ""]
    out += ["• " + " · ".join(r) for r in rows]
    out += ["", f"Web: {URL}", f"Detectado el {now}"]
    return "\n".join(out)


def build_html_sin_cambios(total, now):
    if total:
        detalle = (
            f"Ahora mismo hay <b>{total} {'práctica' if total == 1 else 'prácticas'}</b> "
            "en la web, pero ninguna nueva desde la última comprobación."
        )
    else:
        detalle = "No hay prácticas nuevas desde la última comprobación."
    return f"""\
<!DOCTYPE html>
<html lang="es"><body style="margin:0;padding:0;background:#ffffff;">
<div style="font-family:Arial,Helvetica,sans-serif;color:#1a1a1a;max-width:640px;
 margin:0 auto;padding:20px 16px;font-size:18px;line-height:1.5;">
 <p style="margin:0 0 12px;"><b>Sin novedades.</b></p>
 <p style="margin:0 0 24px;">{detalle}</p>
 <p style="margin:0;font-size:16px;">
  <a href="{html.escape(URL)}" style="color:#1a56db;">Abrir la web de prácticas</a></p>
 <p style="margin:20px 0 0;font-size:13px;color:#8a8a8a;">Comprobado el {now}</p>
</div></body></html>"""


def mask(addr):
    user, _, domain = addr.partition("@")
    return f"{user[:2]}***@{domain}"


def deliver(subject, text, html_body):
    recipients = [r for r in re.split(r"[,;\s]+", os.environ["MAIL_TO"].strip()) if r]
    sender = os.environ["SMTP_USER"].strip()

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")

    print(f"Enviando desde {mask(sender)} a {', '.join(mask(r) for r in recipients)}")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(sender, os.environ["SMTP_PASS"].strip())
        s.send_message(msg, to_addrs=recipients)


def now_madrid():
    return datetime.now(ZoneInfo("Europe/Madrid")).strftime("%d/%m/%Y %H:%M")


def send_email(rows):
    now = now_madrid()
    deliver(SUBJECT, build_text(rows, now), build_html(rows, now))


def toca_aviso_sin_cambios():
    if not AVISAR_SIN_CAMBIOS:
        return False
    try:
        ultimo = float(HEARTBEAT_FILE.read_text())
    except Exception:
        ultimo = 0.0
    return time.time() - ultimo >= AVISAR_CADA_MIN * 60 - 30  # margen por la duración de cada vuelta


def send_sin_cambios(total):
    now = now_madrid()
    texto = f"Sin novedades en las prácticas.\nComprobado el {now}\nWeb: {URL}"
    deliver(SUBJECT_SIN_CAMBIOS, texto, build_html_sin_cambios(total, now))


# ---------- estado ----------
def load_state():
    if not STATE.exists():
        return None
    data = json.loads(STATE.read_text())
    # Formatos antiguos: se vuelve a guardar sin avisar
    if isinstance(data, dict) and data.get("mode") == "classes":
        return data
    return {"mode": "legacy", "items": []}


def save_state(current):
    STATE.write_text(json.dumps({"mode": "classes", "items": list(current)}, ensure_ascii=False, indent=2))


def main():
    text = fetch_page()
    if len(text.strip()) < 20:
        print("No se pudo leer la web; no toco el estado.")
        sys.exit(1)

    current = parse_classes(text)
    print(f"{len(current)} prácticas detectadas en la web")
    if not current and re.search(TIME, text):
        print("AVISO: hay horas en la web pero no reconozco el formato de las prácticas.")

    state = load_state()
    if state is None or state["mode"] == "legacy":
        save_state(current)
        print("Primera ejecución con este formato: estado guardado, sin enviar correo.")
        return

    previous = set(state["items"])
    new_keys = [k for k in current if k not in previous]

    if new_keys:
        print(f"{len(new_keys)} prácticas nuevas, enviando correo.")
        send_email([current[k] for k in new_keys])
    else:
        print("Sin cambios.")
        if toca_aviso_sin_cambios():
            send_sin_cambios(len(current))
            HEARTBEAT_FILE.write_text(str(time.time()))

    if set(current) != previous:
        save_state(current)


if __name__ == "__main__":
    main()
