import os
import json
from datetime import datetime

from flask import Flask, request, Response
from openai import OpenAI
from twilio.twiml.messaging_response import MessagingResponse

import gspread
from google.oauth2.service_account import Credentials

# ===================== CONFIGURACIONES =====================

# OpenAI
client_ai = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# Google Sheets
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

CREDS_FILE = "service_account.json"  # archivo subido a Render

SPREADSHEET_NAME = "Financial Planner ADHV"
HOJA_GASTOS = "Gastos AI"
HOJA_CATALOGO = "CatalogoGastos"

# --- Autenticación Google Sheets ---
creds = Credentials.from_service_account_file(
    CREDS_FILE,
    scopes=SCOPES
)
client_gs = gspread.authorize(creds)
spreadsheet = client_gs.open(SPREADSHEET_NAME)
sheet_gastos = spreadsheet.worksheet(HOJA_GASTOS)

print("[INIT] Conexión con Google Sheets OK")

# ===================== CARGAR CATÁLOGO =====================

# Diccionario descripcion_normalizada -> (categoria, tipo)
catalogo_gastos = {}

def cargar_catalogo():
    """
    Lee la pestaña CatalogoGastos y construye un diccionario:
    descripcion (col A) -> (categoria (col B), tipo (col C))
    Todo en minúsculas y sin espacios extra.
    """
    global catalogo_gastos
    try:
        hoja_catalogo = spreadsheet.worksheet(HOJA_CATALOGO)
        filas = hoja_catalogo.get_all_values()[1:]  # salta encabezado

        tmp = {}
        for fila in filas:
            if len(fila) < 3:
                continue
            descripcion = (fila[0] or "").strip().lower()
            categoria = (fila[1] or "").strip()
            tipo = (fila[2] or "").strip()
            if descripcion:
                # normalizamos espacios internos
                desc_norm = " ".join(descripcion.split())
                tmp[desc_norm] = (categoria, tipo)

        catalogo_gastos = tmp
        print(f"[CATALOGO] Se cargaron {len(catalogo_gastos)} registros.")
    except Exception as e:
        catalogo_gastos = {}
        print(f"[CATALOGO] Error al cargar catálogo: {e}")

cargar_catalogo()

# ===================== LÓGICA DE IA =====================

def _parse_json_seguro(raw_text):
    """
    Intenta extraer JSON de un texto.
    Si falla, lanza excepción para que el caller decida qué hacer.
    """
    # Intentar localizar JSON entre { ... }
    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start != -1 and end != -1:
        json_str = raw_text[start:end+1]
    else:
        json_str = raw_text

    return json.loads(json_str)

def interpretar_gasto(texto):
    """
    Usa OpenAI para extraer la información del gasto a partir del texto libre.
    1) Pide a OpenAI una estructura JSON básica (fecha, descripción, monto, método, tarjeta).
    2) Con la descripción, busca en el catálogo la categoría y el tipo.
    """

    prompt = f"""
Eres un asistente que extrae información de gastos personales desde un mensaje en español.

Devuelve UNICAMENTE un JSON válido con esta estructura:
{{
  "fecha": "YYYY-MM-DD",
  "descripcion": "texto corto",
  "monto": 0,
  "metodo": "efectivo|tarjeta",
  "tarjeta": "nombre o vacío"
}}

Reglas:
- Si el usuario dice una fecha como "el 22 de noviembre", conviértela a formato YYYY-MM-DD (año actual).
- "metodo" solo puede ser "efectivo" o "tarjeta".
- Si no menciona tarjeta, deja "tarjeta" como cadena vacía "".
- "monto" es numérico (sin símbolo de moneda).
- Usa el año actual si no se especifica.

Mensaje del usuario:
\"\"\"{texto}\"\"\""""

    # 1) Llamar a OpenAI
    try:
        response = client_ai.responses.create(
            model="gpt-5-mini",
            input=prompt
        )
        raw = response.output[0].content[0].text
        print(f"[OPENAI RAW] {raw}")
    except Exception as e:
        print(f"[OPENAI ERROR] {e}")
        raise

    # 2) Intentar parsear JSON
    try:
        data = _parse_json_seguro(raw)
    except Exception as e:
        print(f"[JSON ERROR] {e}")
        # Plan B súper simple: si falla el JSON, hacemos un objeto básico
        # para no romper todo el flujo
        hoy = datetime.now().date().isoformat()
        data = {
            "fecha": hoy,
            "descripcion": texto[:50],
            "monto": 0,
            "metodo": "tarjeta",
            "tarjeta": ""
        }

    # 3) Normalizar fecha
    try:
        if data.get("fecha"):
            _ = datetime.fromisoformat(data["fecha"])
        else:
            raise ValueError("Fecha vacía")
    except Exception:
        hoy = datetime.now().date().isoformat()
        data["fecha"] = hoy

    # 4) Normalizar otros campos
    data["descripcion"] = (data.get("descripcion") or "").strip()
    data["metodo"] = (data.get("metodo") or "").strip().lower() or "tarjeta"
    data["tarjeta"] = (data.get("tarjeta") or "").strip()

    try:
        data["monto"] = float(data.get("monto", 0) or 0)
    except Exception:
        data["monto"] = 0.0

    # ================= CATEGORÍA Y TIPO DESDE CATÁLOGO =================
    desc_norm = " ".join(data["descripcion"].lower().split())
    categoria = "otros"
    tipo = "otros"

    if desc_norm in catalogo_gastos:
        cat_catalogo, tipo_catalogo = catalogo_gastos[desc_norm]
        if cat_catalogo:
            categoria = cat_catalogo
        if tipo_catalogo:
            tipo = tipo_catalogo

    data["categoria"] = categoria
    data["tipo"] = tipo

    print(f"[INTERPRETADO] {data}")
    return data

def registrar_gasto(texto):
    """
    Llama a OpenAI para interpretar el gasto,
    luego lo registra en la pestaña 'Gastos AI' de Google Sheets.
    """
    data = interpretar_gasto(texto)

    fila = [
        data["fecha"],
        data["descripcion"],
        data["categoria"],
        data["tipo"],
        data["monto"],
        data["metodo"],
        data["tarjeta"]
    ]

    print(f"[APPEND ROW] {fila}")
    sheet_gastos.append_row(fila, value_input_option="USER_ENTERED")
    return data

# ===================== FLASK APP =====================

app = Flask(__name__)

@app.route("/", methods=["GET"])
def health():
    return "Bot de gastos WhatsApp OK", 200

@app.route("/webhook-whatsapp", methods=["POST"])
def webhook_whatsapp():
    """Endpoint que Twilio llamará cada vez que llegue un WhatsApp."""
    resp = MessagingResponse()

    try:
        body = request.form.get("Body", "")
        from_number = request.form.get("From", "")

        print(f"[WHATSAPP] Mensaje recibido de {from_number}: {body}")

        if not body.strip():
            resp.message(
                "❌ No entendí el mensaje. Envía algo como:\n"
                "'Gasté 250 en Uber con tarjeta BBVA'."
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
