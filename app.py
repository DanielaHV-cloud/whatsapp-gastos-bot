import os
import json
import re
from datetime import datetime, timedelta

from flask import Flask, request, Response
from openai import OpenAI
from twilio.twiml.messaging_response import MessagingResponse

import gspread
from google.oauth2.service_account import Credentials


# ===================== CONFIGURACIONES =====================

client_ai = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

CREDS_FILE = "service_account.json"  # debe existir en el proyecto en Render

SPREADSHEET_NAME = "Financial Planner ADHV"
HOJA_GASTOS = "Gastos AI"
HOJA_CATALOGO = "CatalogoGastos"

app = Flask(__name__)


# ===================== GOOGLE SHEETS INIT =====================

try:
    creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
    client_gs = gspread.authorize(creds)
    spreadsheet = client_gs.open(SPREADSHEET_NAME)
    sheet_gastos = spreadsheet.worksheet(HOJA_GASTOS)
    print("[INIT] Conexión con Google Sheets OK")
except Exception as e:
    print(f"[INIT ERROR] No se pudo conectar a Google Sheets: {e}")
    spreadsheet = None
    sheet_gastos = None


# ===================== CATÁLOGO =====================

catalogo_gastos = {}  # desc_norm -> (categoria, tipo)

def normalizar_desc(s: str) -> str:
    return " ".join((s or "").lower().split())

def cargar_catalogo():
    global catalogo_gastos

    if spreadsheet is None:
        print("[CATALOGO] No hay conexión a spreadsheet.")
        catalogo_gastos = {}
        return

    try:
        hoja_catalogo = spreadsheet.worksheet(HOJA_CATALOGO)
        filas = hoja_catalogo.get_all_values()

        # Saltar encabezado si parece encabezado
        if filas and len(filas) > 0:
            header = " ".join([c.lower() for c in filas[0]])
            if "descripcion" in header or "descripcion_base" in header:
                filas = filas[1:]

        tmp = {}
        for fila in filas:
            if len(fila) < 3:
                continue
            desc = normalizar_desc(fila[0])
            cat = (fila[1] or "").strip()
            tipo = (fila[2] or "").strip()
            if desc:
                tmp[desc] = (cat, tipo)

        catalogo_gastos = tmp
        print(f"[CATALOGO] Se cargaron {len(catalogo_gastos)} registros.")
    except Exception as e:
        catalogo_gastos = {}
        print(f"[CATALOGO] Error al cargar catálogo: {e}")

cargar_catalogo()


# ===================== FECHA: DETECCIÓN & CÁLCULO =====================

MESES_ES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "setiembre", "octubre", "noviembre", "diciembre"
]

def texto_menciona_fecha(texto: str) -> bool:
    t = (texto or "").lower()

    if any(m in t for m in MESES_ES):
        return True

    if re.search(r"\b\d{4}-\d{1,2}-\d{1,2}\b", t):
        return True

    if re.search(r"\b\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?\b", t):
        return True

    if re.search(r"\b(hoy|ayer|antier|antes de ayer|mañana|pasado mañana)\b", t):
        return True

    return False


def fecha_relativa_si_aplica(texto: str) -> str | None:
    t = (texto or "").lower()
    today = datetime.now().date()

    if "antes de ayer" in t or "antier" in t:
        return (today - timedelta(days=2)).isoformat()
    if "ayer" in t:
        return (today - timedelta(days=1)).isoformat()
    if "pasado mañana" in t:
        return (today + timedelta(days=2)).isoformat()
    if "mañana" in t:
        return (today + timedelta(days=1)).isoformat()
    if "hoy" in t:
        return today.isoformat()

    return None


# ===================== LIMPIEZA DE DESCRIPCIÓN =====================

def limpiar_descripcion(desc: str) -> str:
    """
    Convierte cosas como:
      "Compra en Walmart" -> "Walmart"
      "Pago a Telcel" -> "Telcel"
    y quita palabras extra antes de buscar en catálogo.
    """
    d = normalizar_desc(desc)

    prefijos = [
        "compra en ",
        "gasto en ",
        "pago en ",
        "pago a ",
        "pagué en ",
        "pague en ",
        "servicio de ",
        "servicio ",
        "suscripción a ",
        "suscripcion a ",
        "recarga ",
        "recarga a ",
    ]

    for pref in prefijos:
        if d.startswith(pref):
            d = d[len(pref):].strip()

    # también si viene tipo "en walmart"
    if d.startswith("en "):
        d = d[3:].strip()

    # quitar artículos muy comunes al inicio
    for pref in ["el ", "la ", "los ", "las ", "un ", "una "]:
        if d.startswith(pref):
            d = d[len(pref):].strip()

    # Regresar en "Title Case" (opcional)
    return " ".join([w.capitalize() for w in d.split()])


# ===================== LÓGICA DE IA =====================

def interpretar_gasto(texto: str) -> dict:
    prompt = f"""
Eres un asistente que extrae información de gastos personales desde un mensaje en español.

Devuelve ÚNICAMENTE un JSON válido con esta estructura:
{{
  "fecha": "YYYY-MM-DD o vacío",
  "descripcion": "SOLO la descripcion_base (marca/merchant) sin palabras extra",
  "monto": 0,
  "metodo": "efectivo|tarjeta",
  "tarjeta": "nombre o vacío"
}}

Reglas IMPORTANTES:
- SOLO llena "fecha" si el usuario menciona explícitamente una fecha (ej. "el 22 de noviembre", "13/12/2025", "hoy", "ayer").
- Si el usuario NO menciona fecha, devuelve: "fecha": "" (cadena vacía).
- "metodo" solo puede ser "efectivo" o "tarjeta".
- Si no menciona tarjeta, deja "tarjeta" como "".
- "monto" es numérico (sin símbolo de moneda).

Reglas EXTRA para "descripcion":
- Devuelve SOLO la marca/merchant (ej: "Walmart", "Uber", "Oxxo", "Telcel").
- NO agregues palabras como "compra en", "pago de", "gasto en", "servicio", etc.
- Si el texto contiene "en Walmart", la descripcion debe ser exactamente "Walmart".
- Si el texto menciona un merchant claro, usa ese merchant como descripcion.

Mensaje del usuario:
\"\"\"{texto}\"\"\"
"""

    response = client_ai.responses.create(
        model="gpt-5-mini",
        input=prompt
    )

    # lectura defensiva
    raw = ""
    try:
        raw = response.output_text
    except Exception:
        raw = response.output[0].content[0].text

    # extraer json
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1:
        raw = raw[start:end + 1]

    data = json.loads(raw)

    # ---------- Normalizar campos base ----------
    data["descripcion"] = (data.get("descripcion") or "").strip()
    data["metodo"] = (data.get("metodo") or "").strip().lower() or "tarjeta"
    data["tarjeta"] = (data.get("tarjeta") or "").strip()
    data["monto"] = float(data.get("monto") or 0)

    # ---------- FECHA: regla fuerte ----------
    hoy = datetime.now().date().isoformat()

    if not texto_menciona_fecha(texto):
        data["fecha"] = hoy
    else:
        rel = fecha_relativa_si_aplica(texto)
        if rel:
            data["fecha"] = rel
        else:
            try:
                if data.get("fecha"):
                    _ = datetime.fromisoformat(data["fecha"])
                else:
                    data["fecha"] = hoy
            except Exception:
                data["fecha"] = hoy

    # ---------- LIMPIAR DESCRIPCIÓN ----------
    data["descripcion"] = limpiar_descripcion(data["descripcion"])

    # ---------- CATEGORÍA & TIPO (catálogo) ----------
    desc_norm = normalizar_desc(data["descripcion"])
    categoria = "otros"
    tipo = "otros"

    if desc_norm in catalogo_gastos:
        cat, tp = catalogo_gastos[desc_norm]
        if cat:
            categoria = cat
        if tp:
            tipo = tp

    data["categoria"] = categoria
    data["tipo"] = tipo

    return data


def registrar_gasto(texto: str) -> dict:
    if sheet_gastos is None:
        raise RuntimeError("No hay conexión a Google Sheets (sheet_gastos = None).")

    data = interpretar_gasto(texto)

    fila = [
        data["fecha"],
        data["descripcion"],
        data["categoria"],
        data["tipo"],
        data["monto"],
        data["metodo"],
        data["tarjeta"],
    ]

    sheet_gastos.append_row(fila, value_input_option="USER_ENTERED")
    return data


# ===================== ENDPOINTS =====================

@app.route("/", methods=["GET"])
def health():
    return "Bot de gastos WhatsApp OK", 200


@app.route("/webhook-whatsapp", methods=["POST"])
def webhook_whatsapp():
    resp = MessagingResponse()

    try:
        body = request.form.get("Body", "")
        from_number = request.form.get("From", "")

        print(f"[WHATSAPP] Mensaje recibido de {from_number}: {body}")

        if not body.strip():
            resp.message(
                "❌ No entendí el mensaje. Ejemplos:\n"
                "- Gasté 250 en Uber con tarjeta BBVA\n"
                "- Ayer gasté 500 en Walmart con tarjeta AMEX\n"
                "- El 13 de diciembre 2025 gasté 300 en luz con tarjeta BBVA"
            )
            return Response(str(resp), mimetype="application/xml")

        datos_gasto = registrar_gasto(body)

        msg = (
            "✅ Gasto registrado:\n"
            f"• Fecha: {datos_gasto['fecha']}\n"
            f"• Descripción: {datos_gasto['descripcion']}\n"
            f"• Concepto: {datos_gasto['categoria']}\n"
            f"• Tipo: {datos_gasto['tipo']}\n"
            f"• Monto: {datos_gasto['monto']}\n"
            f"• Método: {datos_gasto['metodo']}\n"
            f"• Tarjeta: {datos_gasto['tarjeta'] or 'N/A'}"
        )
        resp.message(msg)

    except Exception as e:
        print(f"[ERROR WEBHOOK] {e}")
        resp.message(
            "❌ Ocurrió un error al registrar tu gasto.\n"
            "Revisa el formato o intenta de nuevo."
        )

    return Response(str(resp), mimetype="application/xml")
