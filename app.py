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
    """
    Llama a OpenAI para convertir texto libre en un JSON de gasto,
    usando el catálogo de la pestaña 'CatalogoGastos'.
    """

    hoy = datetime.now().strftime("%Y-%m-%d")

    # 1) LEER EL CATALOGO DESDE GOOGLE SHEETS
    #    Pestaña: CatalogoGastos, columnas:
    #    A: descripcion_base, B: categoria, C: tipo
    sheet_catalogo = spreadsheet.worksheet("CatalogoGastos")
    catalogo_data = sheet_catalogo.get_all_values()[1:]  # omitimos la fila de encabezados

    catalogo_lineas = []
    for fila in catalogo_data:
        if len(fila) >= 3 and fila[0].strip():
            desc = fila[0].strip()
            categoria = fila[1].strip()
            tipo = fila[2].strip()
            catalogo_lineas.append(f"- {desc} -> categoria={categoria}, tipo={tipo}")

    if catalogo_lineas:
        catalogo_texto = "\n".join(catalogo_lineas)
    else:
        catalogo_texto = "(no hay filas en el catálogo)"

    # 2) ARMAR EL PROMPT PARA LA IA
    prompt = f"""
Eres un asistente que interpreta gastos personales y los clasifica
usando un CATALOGO OFICIAL.

CATÁLOGO OFICIAL (usa la opción más parecida):
{catalogo_texto}

INSTRUCCIONES:

1. A partir del texto del usuario, identifica:
   - fecha
   - descripcion (como la escriba la persona, limpio)
   - monto (solo número, sin símbolo)
   - metodo_pago (uno de: efectivo, tarjeta, transferencia, otro)
   - tipo_tarjeta (ej: BBVA, NU, HSBC, AMEX, etc. o null si no aplica)

2. Para CLASIFICAR:
   - Compara la descripcion del gasto con las "descripcion_base" del catálogo.
   - Elige la más parecida.
   - Usa EXACTAMENTE la categoria y tipo que aparezcan en el catálogo.
   - Si no encuentras nada razonable, usa:
     categoria = "Otros"
     tipo = "Otros"

3. Devuelve SOLO un JSON VÁLIDO con estas claves:
   fecha, descripcion, concepto, categoria, tipo, monto, metodo_pago, tipo_tarjeta

   - "concepto" puede ser una etiqueta corta (ej: transporte, comida, casa, etc.).
   - "categoria" y "tipo" deben venir del catálogo cuando haya coincidencia.

TEXTO DEL USUARIO:
"{texto}"
    """

    # 3) LLAMAR AL MODELO DE OPENAI
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

    # Reutilizamos tu función extraer_json_desde_texto para limpiar
    datos = extraer_json_desde_texto(contenido)
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

