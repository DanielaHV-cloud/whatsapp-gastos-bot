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
    Si no se menciona, regresa vacío.
    """
    t = texto.lower()
    if "lui" in t or "luisa" in t:
        return "Lui"
    if "dani" in t or "daniela" in t:
        return "Dani"
    return ""


# ===================== FALLBACK (PLAN B) =====================

def extraer_monto_regex(texto: str) -> float:
    """
    Busca números tipo: 500, 500.50, 1,200
    """
    t = texto.replace(",", "")
    m = re.search(r"\b(\d+(?:\.\d+)?)\b", t)
    if not m:
        return 0.0
    try:
        return float(m.group(1))
    except Exception:
        return 0.0

def extraer_merchant_regex(texto: str) -> str:
    """
    Intenta:
    - 'en Walmart' -> Walmart
    - 'Walmart' al final -> Walmart
    - 'Uber con tarjeta' -> Uber
    """
    t = texto.strip()

    # "en X"
    m = re.search(r"\ben\s+([A-Za-zÁÉÍÓÚÑáéíóúñ0-9&\-\._ ]+)", t, flags=re.IGNORECASE)
    if m:
        candidate = m.group(1)
        candidate = re.split(r"\b(con|por|para|el|la|los|las)\b", candidate, flags=re.IGNORECASE)[0]
        candidate = candidate.strip(" .,-")
        return limpiar_descripcion(candidate)

    # "X con tarjeta"
    m = re.search(r"\b([A-Za-zÁÉÍÓÚÑáéíóúñ0-9&\-\._ ]+)\s+con\s+tarjeta\b", t, flags=re.IGNORECASE)
    if m:
        candidate = m.group(1).strip(" .,-")
        # quitar posibles palabras al inicio
        candidate = re.sub(r"^(lui|luisa|dani|daniela)\b", "", candidate, flags=re.IGNORECASE).strip()
        return limpiar_descripcion(candidate)

    # último token razonable
    words = re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñ0-9&\-\._]+", t)
    if words:
        return limpiar_descripcion(words[-1])

    return ""


# ===================== IA =====================

def interpretar_gasto(texto: str) -> dict:
    # ✅ AQUÍ estaba el bug: no estábamos metiendo el mensaje del usuario.
    prompt = f"""
Eres un asistente que extrae información de gastos personales desde un mensaje en español.

Devuelve ÚNICAMENTE un JSON válido con esta estructura:
{{
  "fecha": "YYYY-MM-DD o vacío",
  "descripcion": "SOLO la marca o merchant (ej: Walmart, Uber, Oxxo)",
  "monto": 0,
  "metodo": "efectivo|tarjeta",
  "tarjeta": "nombre o vacío"
}}

Reglas:
- SOLO llena "fecha" si el usuario menciona fecha; si NO, devuelve "fecha": "".
- "descripcion" debe ser SOLO la marca/merchant. NO agregues palabras como compra/pago/gasto.
- "monto" es numérico.
- Si no se menciona tarjeta, "tarjeta" debe ser "".
- "metodo" solo "efectivo" o "tarjeta".

Mensaje del usuario:
\"\"\"{texto}\"\"\"
"""

    response = client_ai.responses.create(
        model="gpt-5-mini",
        input=prompt
    )

    raw = getattr(response, "output_text", None)
    if not raw:
        # fallback por si el SDK devuelve otra estructura
        raw = response.output[0].content[0].text

    # extraer json
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1:
        raw_json = raw[start:end + 1]
    else:
        raw_json = raw

    data = json.loads(raw_json)

    # ---- Normalización base ----
    data["descripcion"] = limpiar_descripcion(data.get("descripcion", ""))
    data["metodo"] = (data.get("metodo") or "tarjeta").lower().strip()
    data["tarjeta"] = (data.get("tarjeta") or "").strip()

    try:
        data["monto"] = float(data.get("monto") or 0)
    except Exception:
        data["monto"] = 0.0

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
                if data.get("fecha"):
                    datetime.fromisoformat(data["fecha"])
                else:
                    data["fecha"] = hoy
            except Exception:
                data["fecha"] = hoy

    # ---- Pagado por (vacío si no se menciona) ----
    data["pagado_por"] = detectar_pagado_por(texto)

    # ---- FALLBACK si OpenAI regresó vacío ----
    if not data["descripcion"]:
        data["descripcion"] = extraer_merchant_regex(texto)

    if not data["monto"] or data["monto"] == 0.0:
        data["monto"] = extraer_monto_regex(texto)

    # ---- Catálogo ----
    desc_norm = normalizar_desc(data["descripcion"])
    categoria, tipo = "otros", "otros"
    if desc_norm in catalogo_gastos:
        categoria, tipo = catalogo_gastos[desc_norm]

    data["categoria"] = categoria
    data["tipo"] = tipo

    # Defaults de método/tarjeta si no vinieron
    if "tarjeta" in texto.lower() and not data["tarjeta"]:
        # si el texto dice tarjeta pero no especifica cuál, al menos deja vacío
        data["tarjeta"] = ""

    if data["metodo"] not in ["efectivo", "tarjeta"]:
        data["metodo"] = "tarjeta"

    return data


def registrar_gasto(texto: str) -> dict:
    if sheet_gastos is None:
        raise RuntimeError("No hay conexión a Google Sheets (sheet_gastos = None).")

    data = interpretar_gasto(texto)

    # IMPORTANTE: en tu Google Sheet, agrega columna "pagado_por"
    # Orden recomendado:
    # A fecha, B descripcion, C categoria, D tipo, E pagado_por, F monto, G metodo, H tarjeta
    fila = [
        data["fecha"],
        data["descripcion"],
        data["categoria"],
        data["tipo"],
        data["pagado_por"],   # 👈 NUEVO (vacío si no se menciona)
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

        if datos.get("pagado_por"):
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
