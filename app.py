import os
import json
from datetime import datetime

from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

import gspread
from google.oauth2.service_account import Credentials

from openai import OpenAI

# =========================
# CONFIGURACIÓN INICIAL
# =========================

# OpenAI
client_ai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Google Sheets
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# En Render subimos el secret file como `service_account.json`
SERVICE_ACCOUNT_FILE = os.getenv(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/etc/secrets/service_account.json"
)

creds = Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE,
    scopes=SCOPES
)
gc = gspread.authorize(creds)

SPREADSHEET_NAME = "Financial Planner ADHV"
SHEET_REGISTROS = "Gastos AI"
SHEET_CATALOGO = "CatalogoGastos"

sh = gc.open(SPREADSHEET_NAME)
sheet_registros = sh.worksheet(SHEET_REGISTROS)
sheet_catalogo = sh.worksheet(SHEET_CATALOGO)

# =========================
# CARGAR CATÁLOGO DE GASTOS
# =========================

def cargar_catalogo_desde_sheet():
    """
    Lee CatalogoGastos y arma un dict:
    {
        "luz": {"categoria": "...", "tipo": "..."},
        "uber": {"categoria": "...", "tipo": "..."},
        ...
    }
    Normalizamos descripción a minúsculas, sin espacios alrededor.
    """
    registros = sheet_catalogo.get_all_records()
    catalogo = {}

    for fila in registros:
        # Tratamos de admitir varias variantes de nombres de columna
        desc = (
            fila.get("descripcion")
            or fila.get("Descripción")
            or fila.get("DESCRIPCION")
            or fila.get("DESCRIPCIÓN")
            or fila.get("Description")
            or ""
        )
        categoria = (
            fila.get("categoria")
            or fila.get("Categoría")
            or fila.get("CATEGORIA")
            or ""
        )
        tipo = (
            fila.get("tipo")
            or fila.get("Tipo")
            or fila.get("TIPO")
            or ""
        )

        desc_norm = str(desc).strip().lower()
        if desc_norm:
            catalogo[desc_norm] = {
                "categoria": str(categoria).strip(),
                "tipo": str(tipo).strip(),
            }

    return catalogo


CATALOGO_GASTOS = cargar_catalogo_desde_sheet()

# =========================
# FUNCIÓN: INTERPRETAR GASTO
# =========================

def interpretar_gasto(texto):
    """
    Llama a OpenAI para transformar el mensaje en un JSON con:
    fecha, descripcion, monto, metodo_pago, tarjeta.

    Luego completa concepto y tipo usando el catálogo.
    """
    hoy = datetime.now().strftime("%Y-%m-%d")

    prompt = f"""
Eres un asistente que extrae información de gastos a partir de mensajes en español.

Del siguiente texto de entrada, identifica:
- fecha del gasto (si no se menciona, usa la fecha de hoy: {hoy})
- descripción del gasto (por ejemplo: "uber", "luz", "super", "gasolina")
- monto en pesos mexicanos
- método de pago (efectivo, tarjeta, transferencia, etc.)
- tarjeta o banco si se menciona (por ejemplo: BBVA, AMEX, Banorte)

Devuelve SOLO un JSON válido con esta estructura (sin texto extra):

{{
  "fecha": "YYYY-MM-DD",
  "descripcion": "texto corto",
  "monto": 123.45,
  "metodo_pago": "tarjeta | efectivo | transferencia | otro",
  "tarjeta": "nombre de banco/tarjeta o vacío si no se menciona"
}}

Texto de entrada:
\"\"\"{texto}\"\"\""""

    response = client_ai.responses.create(
        model="gpt-5-mini",
        input=prompt,
    )

    # Extraemos el texto de la respuesta
    raw = response.output[0].content[0].text
    texto_modelo = raw.value if hasattr(raw, "value") else str(raw)

    # Nos quedamos solo con el bloque JSON
    start = texto_modelo.find("{")
    end = texto_modelo.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError(f"No se encontró JSON en la respuesta del modelo: {texto_modelo}")

    json_str = texto_modelo[start:end]
    data = json.loads(json_str)

    # Normalizamos algunos campos básicos
    fecha = data.get("fecha", hoy)
    descripcion = str(data.get("descripcion", "")).strip()
    monto = float(data.get("monto", 0))
    metodo_pago = str(data.get("metodo_pago", "")).strip().lower()
    tarjeta = str(data.get("tarjeta", "")).strip()

    # =========================
    # COMPLETAR CON CATÁLOGO
    # =========================
    desc_norm = descripcion.lower()

    info_cat = CATALOGO_GASTOS.get(desc_norm)
    if info_cat:
        concepto = info_cat.get("categoria") or "otros"
        tipo = info_cat.get("tipo") or "otros"
    else:
        # Si no encontramos la descripción en el catálogo,
        # igual registramos el gasto con valores genéricos.
        concepto = "otros"
        tipo = "otros"

    return {
        "fecha": fecha,
        "descripcion": descripcion,
        "monto": monto,
        "metodo_pago": metodo_pago,
        "tarjeta": tarjeta,
        "concepto": concepto,
        "tipo": tipo,
    }

# =========================
# FUNCIÓN: REGISTRAR GASTO
# =========================

def registrar_gasto(texto_original):
    """
    Interpreta el mensaje, escribe una fila en Google Sheets
    y devuelve el texto de respuesta para WhatsApp.
    """
    datos = interpretar_gasto(texto_original)

    fecha = datos["fecha"]
    descripcion = datos["descripcion"]
    monto = datos["monto"]
    metodo_pago = datos["metodo_pago"]
    tarjeta = datos["tarjeta"]
    concepto = datos["concepto"]
    tipo = datos["tipo"]

    # Por si quieres guardar hora exacta y fuente
    ts_registro = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    origen = "WhatsApp"

    # Ajusta el orden de columnas a como está tu pestaña "Gastos AI"
    fila = [
        fecha,
        descripcion,
        concepto,
        tipo,
        monto,
        metodo_pago,
        tarjeta,
        ts_registro,
        origen,
    ]

    sheet_registros.append_row(fila, value_input_option="USER_ENTERED")

    respuesta = (
        "✅ Gasto registrado:\n"
        f"• Fecha: {fecha}\n"
        f"• Descripción: {descripcion}\n"
        f"• Concepto: {concepto}\n"
        f"• Tipo: {tipo}\n"
        f"• Monto: {monto}\n"
        f"• Método: {metodo_pago}\n"
        f"• Tarjeta: {tarjeta or '-'}"
    )
    return respuesta

# =========================
# FLASK + TWILIO
# =========================

app = Flask(__name__)

@app.route("/", methods=["GET"])
def health():
    return "Bot de gastos WhatsApp OK", 200


@app.route("/webhook-whatsapp", methods=["POST"])
def webhook_whatsapp():
    """
    Endpoint que Twilio llama cada vez que llega un WhatsApp.
    """
    body = request.form.get("Body", "")
    from_number = request.form.get("From", "")

    print(f"Mensaje recibido de {from_number}: {body}")

    resp = MessagingResponse()

    try:
        texto_respuesta = registrar_gasto(body)
        resp.message(texto_respuesta)
    except Exception as e:
        # Log detallado para que puedas ver el error en Render logs
        print(f"Error procesando mensaje: {e}")
        resp.message("❌ Ocurrió un error al registrar tu gasto.\nRevisa el formato o intenta de nuevo.")

    return str(resp)


if __name__ == "__main__":
    # Para pruebas locales
    app.run(host="0.0.0.0", port=5000, debug=True)
