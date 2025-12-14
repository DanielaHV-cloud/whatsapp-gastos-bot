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

CREDS_FILE = "service_account.json"

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
    print(f"[INIT ERROR] Google Sheets: {e}")
    spreadsheet = None
    sheet_gastos = None


# ===================== CATÁLOGO =====================

catalogo_gastos = {}  # descripcion_normalizada -> (categoria, tipo)

def normalizar_desc(s: str) -> str:
    return " ".join((s or "").lower().split())

def cargar_catalogo():
    global catalogo_gastos

    if spreadsheet is None:
        catalogo_gastos = {}
        return

    try:
        hoja = spreadsheet.worksheet(HOJA_CATALOGO)
        filas = hoja.get_all_values()

        # Saltar encabezado si existe
        if filas and "descripcion" in " ".join([c.lower() for c in filas[0]]):
            filas = filas[1:]

        tmp = {}
        for fila in filas:
            if len(fila) < 3:
                continue
            desc = normalizar_desc(fila[0])
            categoria = (fila[1] or "").strip()
            tipo = (fila[2] or "").strip()
            if desc:
                tmp[desc] = (categoria, tipo)

        catalogo_gastos = tmp
        print(f"[CATALOGO] {len(catalogo_gastos)} registros cargados")
    except Exception as e:
        catalogo_gastos = {}
        print(f"[CATALOGO ERROR] {e}")

cargar_catalogo()


# ===================== FECHA =====================

MESES_ES = [
    "enero","febrero","marzo","abril","mayo","junio",
    "julio","agosto","septiembre","setiembre","octubre","noviembre","diciembre"
]

def texto_menciona_fecha(texto: str) -> bool:
    t = texto.lower()
    if any(m in t for m in MESES_ES):
        return True
    if re.search(r"\b\d{4}-\d{1,2}-\d{1,2}\b", t):
        return True
    if re.search(r"\b\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?\b", t):
        return True
    if re.search(r"\b(hoy|ayer|antier|antes de ayer|mañana|pasado mañana)\b", t):
        return True
    return False

def fecha_relativa(texto: str):
    t = texto.lower()
    hoy = datetime.now().date()
    if "antes de ayer" in t or "antier" in t:
        return (hoy - timedelta(days=2)).isoformat()
    if "ayer" in t:
        return (hoy - timedelta(days=1)).isoformat()
    if "mañana" in t:
        return (hoy + timedelta(days=1)).isoformat()
    if "pasado mañana" in t:
        return (hoy + timedelta(days=2)).isoformat()
    if "hoy" in t:
        return hoy.isoformat()
    return None


# ===================== DESCRIPCIÓN =====================

def limpiar_descripcion(desc: str) -> str:
    d = normalizar_desc(desc)

    prefijos = [
        "compra en ", "gasto en ", "pago en ", "pago a ",
        "servicio de ", "servicio ", "suscripción a ", "suscripcion a ",
        "recarga ", "recarga a ", "en "
    ]

    for p in prefijos:
        if d.startswith(p):
            d = d[len(p):].strip()

    for p in ["el ", "la ", "los ", "las ", "un ", "una "]:
        if d.startswith(p):
            d = d[len(p):].strip()

    return " ".join(w.capitalize() for w in d.split())


# ===================== PAGADO POR =====================

def detectar_pagado_por(texto: str) -> str:
    """
    Si no se menciona, regresa vacío
    """
    t = texto.lower()
    if "lui" in t or "luisa" in t:
        return "Lui"
    if "dani" in t or "daniela" in t:
        return "Dani"
    return ""


# ===================== IA =====================

def interpretar_gasto(texto: str) -> dict:
    prompt = f"""
Eres un asistente que extrae información de gastos personales desde un mensaje en español.

Devuelve ÚNICAMENTE un JSON válido con esta estructura:
{{
  "fecha": "YYYY-MM-DD o vacío",
  "descripcion": "SOLO la marca o merchant",
  "monto": 0,
  "metodo": "efectivo|tarjeta",
  "tarjeta": "nombre o vacío"
}}

Reglas:
- SOLO llena "fecha" si el usuario menciona fecha.
- Si NO menciona fecha, devuelve "fecha": "".
- "descripcion" debe ser solo la marca (ej: Walmart, Uber).
- NO escribas palabras como compra, pago, gasto.
- "monto" es numérico.
"""

    response = client_ai.responses.create(
        model="gpt-5-mini",
        input=prompt
    )

    raw = getattr(response, "output_text", None)
    if not raw:
        raw = response.output[0].content[0].text

    raw = raw[raw.find("{"):raw.rfind("}") + 1]
    data = json.loads(raw)

    # ---- Normalización ----
    data["descripcion"] = limpiar_descripcion(data.get("descripcion", ""))
    data["metodo"] = (data.get("metodo") or "tarjeta").lower()
    data["tarjeta"] = (data.get("tarjeta") or "").strip()
    data["monto"] = float(data.get("monto") or 0)

    # ---- Fecha ----
    hoy = datetime.now().date().isoformat()
    if not texto_menciona_fecha(texto):
        data["fecha"] = hoy
    else:
        rel = fecha_relativa(texto)
        if rel:
            data["fecha"] = rel
        else:
            try:
                datetime.fromisoformat(data["fecha"])
            except Exception:
                data["fecha"] = hoy

    # ---- Catálogo ----
    desc_norm = normalizar_desc(data["descripcion"])
    categoria, tipo = "otros", "otros"
    if desc_norm in catalogo_gastos:
        categoria, tipo = catalogo_gastos[desc_norm]

    data["categoria"] = categoria
    data["tipo"] = tipo

    # ---- Pagado por ----
    data["pagado_por"] = detectar_pagado_por(texto)

    return data


def registrar_gasto(texto: str) -> dict:
    data = interpretar_gasto(texto)

    fila = [
        data["fecha"],
        data["descripcion"],
        data["categoria"],
        data["tipo"],
        data["pagado_por"],   # 👈 NUEVO
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
        datos = registrar_gasto(body)

        msg = (
            "✅ Gasto registrado:\n"
            f"• Fecha: {datos['fecha']}\n"
            f"• Descripción: {datos['descripcion']}\n"
            f"• Concepto: {datos['categoria']}\n"
            f"• Tipo: {datos['tipo']}\n"
        )

        if datos["pagado_por"]:
            msg += f"• Pagado por: {datos['pagado_por']}\n"

        msg += (
            f"• Monto: {datos['monto']}\n"
            f"• Método: {datos['metodo']}\n"
            f"• Tarjeta: {datos['tarjeta'] or 'N/A'}"
        )

        resp.message(msg)

    except Exception as e:
        print("[ERROR]", e)
        resp.message("❌ Ocurrió un error al registrar el gasto.")

    return Response(str(resp), mimetype="application/xml")
