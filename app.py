import os
import json
from datetime import datetime

from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

import gspread
from google.oauth2.service_account import Credentials
from openai import OpenAI

app = Flask(__name__)

# ========= OpenAI =========
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("Falta la variable de entorno OPENAI_API_KEY")

client_ai = OpenAI(api_key=OPENAI_API_KEY)

# ========= Google Sheets =========
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

creds = Credentials.from_service_account_file(
    "service_account.json",
    scopes=SCOPES,
)

client_gs = gspread.authorize(creds)
spreadsheet = client_gs.open("Financial Planner ADHV")
sheet = spreadsheet.worksheet("Gastos AI")


def extraer_json_desde_texto(texto: str) -> dict:
    """Toma la respuesta del modelo y extrae el JSON entre { ... }."""
    start = texto.find("{")
    end = texto.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No se encontró JSON en el texto:\n{texto}")
    json_str = texto[start : end + 1]
    return json.loads(json_str)


def interpretar_gasto(texto: str) -> dict:
    """Llama a OpenAI para convertir texto libre en un JSON de gasto."""
    hoy = datetime.now().strftime("%Y-%m-%d")

    prompt = f"""
Devuelve SOLO un JSON válido con estas llaves:
fecha, descripcion, concepto, monto, metodo_pago, tipo_tarjeta.

Reglas:
- Si no hay fecha, usa {hoy}.
- Si dice "ayer", ajusta la fecha.
- Método de pago: efectivo, tarjeta, transferencia u otro.
- Conceptos: comida, transporte, casa, salud, entretenimiento, otros.

Ejemplo:
{{
  "fecha": "2025-11-22",
  "descripcion": "uber",
  "concepto": "transporte",
  "monto": 230,
  "metodo_pago": "tarjeta",
  "tipo_tarjeta": "NU"
}}

Texto del usuario:
"{texto}"
"""

    resp = client_ai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "Eres un asistente que estructura gastos y SIEMPRE "
                    "respondes solo JSON válido."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    )

    contenido = resp.choices[0].message.content
    datos = extraer_json_desde_texto(contenido)
    return datos


def registrar_gasto_en_sheet(texto_usuario: str) -> dict:
    """Interpreta el gasto y lo guarda en Google Sheets."""
    datos = interpretar_gasto(texto_usuario)

    sheet.append_row(
        [
            datos.get("fecha"),
            datos.get("descripcion"),
            datos.get("concepto"),
            datos.get("monto"),
            datos.get("metodo_pago"),
            datos.get("tipo_tarjeta"),
            "whatsapp_bot",
        ]
    )

    return datos


@app.route("/", methods=["GET"])
def health():
    return "Bot de gastos WhatsApp OK", 200


@app.route("/webhook-whatsapp", methods=["POST"])
def webhook_whatsapp():
    """Endpoint que Twilio llamará cada vez que llegue un WhatsApp."""
    body = request.form.get("Body", "")
    from_number = request.form.get("From", "")

    print(f"Mensaje recibido de {from_number}: {body}")

    try:
        datos = registrar_gasto_en_sheet(body)
        resp_text = (
            "✅ Gasto registrado:\n"
            f"- Fecha: {datos.get('fecha')}\n"
            f"- Descripción: {datos.get('descripcion')}\n"
            f"- Concepto: {datos.get('concepto')}\n"
            f"- Monto: {datos.get('monto')}\n"
            f"- Método: {datos.get('metodo_pago')}\n"
            f"- Tarjeta: {datos.get('tipo_tarjeta')}"
        )
    except Exception as e:
        print("Error procesando gasto:", e)
        resp_text = (
            "❌ Ocurrió un error al registrar tu gasto.\n"
            "Revisa el formato o intenta de nuevo."
        )

    tw_resp = MessagingResponse()
    tw_resp.message(resp_text)
    return str(tw_resp)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

