"""Vigila la web de prácticas y envía un correo cuando aparecen clases nuevas."""
import html
import json
import os
import re
import smtplib
import sys
import tempfile
import time
from datetime import date, datetime
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

DAY_RE = re.compile(
    r"^(lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)\s+(\d{1,2})\s+de\s+([a-záéíóú]+)$",
    re.IGNORECASE,
)
TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
MESES = {m: i for i, m in enumerate(
    "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre".split(), 1)}
COLUMNAS = ("Fecha", "Hora", "Coche", "Profesor/a", "Lugar", "Precio")


# ---------- lectura de la web ----------
def fetch_page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle", timeout=60000)
        try:
            page.wait_for_selector("text=Disponibles", timeout=30000)
        except Exception:
            pass
        page.wait_for_timeout(2000)  # margen para que carguen las tarjetas
        # Asegura que se lee la pestaña "Disponibles" (y no "Todas" o "Mis reservas")
        for sel in ('button:text-is("Disponibles")', '[role="tab"]:text-is("Disponibles")'):
            try:
                page.locator(sel).first.click(timeout=3000)
                break
            except Exception:
                continue
        page.wait_for_timeout(1500)
        text = page.inner_text("body")
        browser.close()
    return text


def orden_fecha(fecha_txt, hora):
    m = DAY_RE.match(fecha_txt)
    try:
        hoy = datetime.now(ZoneInfo("Europe/Madrid")).date()
        d = date(hoy.year, MESES[m.group(3).lower()], int(m.group(2)))
        if (hoy - d).days > 60:  # la web no muestra el año: si ya pasó, es del año siguiente
            d = d.replace(year=hoy.year + 1)
        return (d.year, d.month, d.day, hora)
    except Exception:
        return (9999, 0, 0, hora)


LUGAR_RE = re.compile(r"\b(zona|parking|renfe|aparcamiento)\b", re.IGNORECASE)
TIPO_RE = re.compile(r"^(autom[aá]tic[oa]|manual)$", re.IGNORECASE)
RESERVADA_RE = re.compile(r"^reservada$", re.IGNORECASE)
ETIQUETA_RE = re.compile(r"^(nueva|nuevo|disponible|ocupada|cancelada)$", re.IGNORECASE)


def parse_classes(text):
    """Lee las tarjetas sin depender del orden: día (título), horas, tipo, profesor, coche, lugar y precio."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    found = {}
    fecha = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if DAY_RE.match(line):
            fecha = line
            i += 1
            continue
        if fecha and TIME_RE.match(line):
            inicio, fin, j = line, "", i + 1
            if j < len(lines) and TIME_RE.match(lines[j]):
                fin, j = lines[j], j + 1
            tokens = []
            while (j < len(lines) and not DAY_RE.match(lines[j]) and not TIME_RE.match(lines[j])
                   and not lines[j].lower().startswith("made with")):
                tokens.append(lines[j])
                j += 1

            precio, tipo, lugar, resto, reservada = "", "", "", [], False
            for t in tokens:
                if RESERVADA_RE.match(t):
                    reservada = True
                elif ETIQUETA_RE.match(t):
                    continue  # etiquetas sueltas que no son datos de la práctica
                elif "€" in t:
                    precio = precio or t
                elif TIPO_RE.match(t) and not tipo:
                    tipo = t
                elif LUGAR_RE.search(t) and not lugar:
                    lugar = t
                else:
                    resto.append(t)
            if reservada:  # ya reservada: no es una práctica disponible
                i = j
                continue
            profe = resto[0] if resto else "—"
            coche = resto[1] if len(resto) > 1 else "—"
            if not lugar and len(resto) > 2:
                lugar = resto[2]
            if not lugar:
                lugar = "—"
                print(f"DEBUG tarjeta sin lugar: {fecha} {inicio} -> {tokens[:8]}")

            hora = f"{inicio}-{fin}" if fin else inicio
            clave = f"{fecha}|{hora}|{profe}|{coche}|{lugar}"
            coche_txt = f"{coche} ({tipo})" if tipo else coche
            found[clave] = (fecha, hora, coche_txt, profe, lugar, precio or "—")
            i = j
            continue
        i += 1
    return dict(sorted(found.items(), key=lambda kv: orden_fecha(kv[1][0], kv[1][1])))


# ---------- correo ----------
def tabla_html(rows):
    th = "text-align:left;padding:14px 8px;font-weight:700;"
    td = "padding:14px 8px;"
    head = "".join(f'<th style="{th}">{h}</th>' for h in COLUMNAS)
    body = "".join(
        "<tr>" + "".join(f'<td style="{td}">{html.escape(c)}</td>' for c in r) + "</tr>"
        for r in rows
    )
    return (
        '<table width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;font-size:16px;">'
        f'<tr style="background:#f2f2f2;">{head}</tr>{body}</table>'
    )


def envoltorio(contenido):
    return f"""\
<!DOCTYPE html>
<html lang="es"><body style="margin:0;padding:0;background:#ffffff;">
<div style="font-family:Arial,Helvetica,sans-serif;color:#1a1a1a;max-width:720px;
 margin:0 auto;padding:20px 16px;font-size:18px;line-height:1.5;">
{contenido}
</div></body></html>"""


def enlace_web():
    return (f'<p style="margin:28px 0 0;font-size:16px;">'
            f'<a href="{html.escape(URL)}" style="color:#1a56db;">Abrir la web de prácticas</a></p>')


def build_html(rows, now):
    n = len(rows)
    intro = (
        f"Hemos encontrado <b>{n} {'práctica nueva' if n == 1 else 'prácticas nuevas'}</b> "
        "disponible" + ("" if n == 1 else "s") + "."
    )
    return envoltorio(
        f'<p style="margin:0 0 24px;">{intro}</p>{tabla_html(rows)}{enlace_web()}'
        f'<p style="margin:20px 0 0;font-size:13px;color:#8a8a8a;">Detectado el {now}</p>'
    )


def build_text(rows, now):
    out = [SUBJECT, ""]
    out += ["• " + " · ".join(r) for r in rows]
    out += ["", f"Web: {URL}", f"Detectado el {now}"]
    return "\n".join(out)


def build_html_sin_cambios(rows, now):
    n = len(rows)
    if n:
        extra = (
            f'<p style="margin:24px 0 12px;">Disponibles ahora en la web '
            f'(<b>{n}</b>), ya conocidas:</p>{tabla_html(rows)}'
        )
    else:
        extra = '<p style="margin:0 0 12px;">Ahora mismo no hay prácticas disponibles.</p>'
    return envoltorio(
        '<p style="margin:0 0 12px;"><b>Sin novedades.</b> '
        'No hay prácticas nuevas desde la última comprobación.</p>'
        f'{extra}{enlace_web()}'
        f'<p style="margin:20px 0 0;font-size:13px;color:#8a8a8a;">Comprobado el {now}</p>'
    )


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


def send_sin_cambios(rows):
    now = now_madrid()
    texto = f"Sin novedades en las prácticas ({len(rows)} disponibles ya conocidas).\nComprobado el {now}\nWeb: {URL}"
    deliver(SUBJECT_SIN_CAMBIOS, texto, build_html_sin_cambios(rows, now))


# ---------- estado ----------
def load_state():
    if not STATE.exists():
        return None
    data = json.loads(STATE.read_text())
    if isinstance(data, dict) and data.get("mode") == "classes":
        return data
    return {"mode": "legacy", "items": []}  # formatos antiguos: se reinicia sin avisar


def save_state(current):
    STATE.write_text(json.dumps({"mode": "classes", "items": list(current)}, ensure_ascii=False, indent=2))


def main():
    text = fetch_page()
    if len(text.strip()) < 20:
        print("No se pudo leer la web; no toco el estado.")
        sys.exit(1)

    current = parse_classes(text)
    print(f"{len(current)} prácticas detectadas en la web")
    m = re.search(r"(\d+)\s*\n\s*Disponibles", text)
    if m and int(m.group(1)) != len(current):
        print(f"AVISO: la web dice {m.group(1)} disponibles pero he leído {len(current)}.")

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
            send_sin_cambios(list(current.values()))
            HEARTBEAT_FILE.write_text(str(time.time()))

    if set(current) != previous:
        save_state(current)


if __name__ == "__main__":
    main()
