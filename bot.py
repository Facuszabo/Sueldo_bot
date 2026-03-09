"""
Flujo Bot — Control de gastos por Telegram
"""
import os
import json
import re
import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, JobQueue
)
from telegram.constants import ParseMode

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
DB_PATH = Path(__file__).parent / "flujo.db"

# ─── EMOJIS DE CATEGORÍAS ────────────────────────────────────────────────────
CAT_INGRESO = {
    "sueldo": "💼", "salario": "💼", "freelance": "💰", "honorario": "💰",
    "alquiler": "🏠", "inversion": "📈", "inversión": "📈", "bono": "🎁",
    "venta": "🛍️", "otro": "🎁"
}
CAT_COMPROMETIDO = {
    "alquiler": "🏠", "luz": "💡", "gas": "🔥", "agua": "💧",
    "internet": "🌐", "telefono": "📱", "teléfono": "📱", "celular": "📱",
    "credito": "💳", "crédito": "💳", "cuota": "💳", "prestamo": "💳",
    "préstamo": "💳", "prepaga": "🏥", "salud": "🏥", "seguro": "🛡️",
    "educacion": "📚", "educación": "📚", "suscripcion": "🔁", "suscripción": "🔁",
    "netflix": "🎬", "spotify": "🎵", "gym": "🏋️", "otro": "📦"
}
CAT_INCURRIDO = {
    "super": "🛒", "supermercado": "🛒", "mercado": "🛒", "verduleria": "🥦",
    "verdulería": "🥦", "carniceria": "🥩", "carnicería": "🥩",
    "comida": "🍽️", "restaurant": "🍽️", "restaurante": "🍽️",
    "cafe": "☕", "café": "☕", "delivery": "🛵",
    "ropa": "👕", "indumentaria": "👕", "zapatos": "👟",
    "farmacia": "💊", "medicamento": "💊",
    "taxi": "🚕", "uber": "🚕", "remis": "🚕", "combustible": "⛽", "nafta": "⛽",
    "peaje": "🛣️", "estacionamiento": "🅿️",
    "cine": "🎬", "teatro": "🎭", "entretenimiento": "🎮",
    "deporte": "⚽", "gym": "🏋️",
    "tecnologia": "💻", "tecnología": "💻", "electronica": "📱", "electrónica": "📱",
    "regalo": "🎁", "otro": "📦"
}

def detect_emoji(concepto: str, tipo: str) -> str:
    """Detecta emoji según el concepto."""
    c = concepto.lower()
    cat_map = {"ingreso": CAT_INGRESO, "comprometido": CAT_COMPROMETIDO, "incurrido": CAT_INCURRIDO}
    cats = cat_map.get(tipo, CAT_INCURRIDO)
    for key, emoji in cats.items():
        if key in c:
            return emoji
    return "💼" if tipo == "ingreso" else "🔒" if tipo == "comprometido" else "📝"

# ─── BASE DE DATOS ───────────────────────────────────────────────────────────
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS ingresos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            fecha DATE NOT NULL,
            monto REAL NOT NULL,
            concepto TEXT NOT NULL,
            categoria TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS comprometidos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            concepto TEXT NOT NULL,
            monto REAL NOT NULL,
            vencimiento DATE NOT NULL,
            estado TEXT DEFAULT 'pendiente',
            recurrente INTEGER DEFAULT 0,
            categoria TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS incurridos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            fecha DATE NOT NULL,
            monto REAL NOT NULL,
            concepto TEXT NOT NULL,
            categoria TEXT,
            medio TEXT DEFAULT 'efectivo',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            moneda TEXT DEFAULT '$',
            recordatorio_dias INTEGER DEFAULT 3,
            notif_hora TEXT DEFAULT '09:00',
            nombre TEXT
        );
        """)

def get_db():
    return sqlite3.connect(DB_PATH)

def fmt(monto: float, user_id: int = None) -> str:
    return f"${monto:,.0f}".replace(",", ".")

def get_mes_actual():
    return date.today().replace(day=1)

# ─── PARSEO INTELIGENTE DE MENSAJES ─────────────────────────────────────────
def parse_message(text: str) -> dict | None:
    """
    Interpreta mensajes en lenguaje natural:
    - "ingreso 50000 sueldo"
    - "gasto 1500 supermercado"
    - "pago 25000 alquiler vence 15"
    - "cobré 80000 proyecto"
    - "gasté 3000 en nafta"
    - "debo pagar 8000 prepaga el 10"
    """
    text = text.strip()
    result = {}

    # Normalizar
    t = text.lower()
    t = re.sub(r'\s+', ' ', t)

    # ── Detectar TIPO ────────────────────────────────────────────────────────
    palabras_ingreso = r'\b(ingres[oa]|cobr[eéó]|recibi[oó]|sueldo|salario|honorario|pagaron|me\s+pagaron|cobré)\b'
    palabras_comprometido = r'\b(vence|vencimiento|pago\s+fijo|debo\s+pagar|hay\s+que\s+pagar|compromiso|cuota|el\s+d[ií]a|el\s+\d+)\b'
    palabras_incurrido = r'\b(gast[eéó]|compré|pagué|compr[eéó]|sali[oó]|gasto|comida|super|taxi|uber|farmacia)\b'

    if re.search(palabras_ingreso, t):
        result['tipo'] = 'ingreso'
    elif re.search(palabras_comprometido, t):
        result['tipo'] = 'comprometido'
    elif re.search(palabras_incurrido, t):
        result['tipo'] = 'incurrido'
    else:
        # fallback: si empieza con "pago" sin fecha → comprometido, sino incurrido
        if t.startswith('pago') or t.startswith('pagar'):
            result['tipo'] = 'comprometido'
        else:
            result['tipo'] = 'incurrido'

    # ── Detectar MONTO ───────────────────────────────────────────────────────
    monto_match = re.search(r'\$?\s*(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d+)?)[kK]?', text)
    if not monto_match:
        return None
    monto_str = monto_match.group(1).replace('.', '').replace(',', '.')
    monto = float(monto_str)
    if text[monto_match.start():monto_match.end()].lower().endswith('k'):
        monto *= 1000
    result['monto'] = monto

    # ── Detectar FECHA DE VENCIMIENTO (para comprometidos) ───────────────────
    dia_match = re.search(r'\b(?:el\s+)?(?:d[ií]a\s+)?(\d{1,2})(?:\s+de\s+\w+)?\b', t)
    if dia_match and result['tipo'] == 'comprometido':
        dia = int(dia_match.group(1))
        if 1 <= dia <= 31:
            hoy = date.today()
            try:
                venc = hoy.replace(day=dia)
                if venc < hoy:
                    # Siguiente mes
                    if hoy.month == 12:
                        venc = venc.replace(year=hoy.year + 1, month=1)
                    else:
                        venc = venc.replace(month=hoy.month + 1)
                result['vencimiento'] = venc.isoformat()
            except ValueError:
                pass

    # ── Detectar si es RECURRENTE ─────────────────────────────────────────────
    result['recurrente'] = bool(re.search(r'\b(mensual|siempre|todos\s+los\s+meses|fijo|recurrente)\b', t))

    # ── Detectar CONCEPTO ────────────────────────────────────────────────────
    # Eliminar palabras clave y el monto para quedarse con el concepto
    concepto = text
    concepto = re.sub(r'\$?\s*\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?\s*[kK]?', '', concepto)
    concepto = re.sub(r'\b(ingresé|ingreso|cobré|cobro|recibí|recibo|gasté|gasto|pagué|pago|compré|compro|salió)\b', '', concepto, flags=re.I)
    concepto = re.sub(r'\b(en|de|por|el|la|un|una|del|al|fijo|mensual|recurrente)\b', '', concepto, flags=re.I)
    concepto = re.sub(r'\b(vence|vencimiento|el\s+d[ií]a|debo\s+pagar|hay\s+que\s+pagar)\b', '', concepto, flags=re.I)
    concepto = re.sub(r'\b\d{1,2}\b', '', concepto)
    concepto = re.sub(r'\s+', ' ', concepto).strip()

    if not concepto:
        concepto = "Sin concepto"

    # Capitalizar
    concepto = concepto.strip('.- ').capitalize()
    result['concepto'] = concepto

    return result

# ─── KEYBOARDS ───────────────────────────────────────────────────────────────
def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💵 Ingresos", callback_data="menu_ingresos"),
         InlineKeyboardButton("🔒 Comprometidos", callback_data="menu_comprometidos")],
        [InlineKeyboardButton("📝 Incurridos", callback_data="menu_incurridos"),
         InlineKeyboardButton("📊 Resumen", callback_data="menu_resumen")],
        [InlineKeyboardButton("⏰ Vencimientos", callback_data="menu_vencimientos"),
         InlineKeyboardButton("⚙️ Config", callback_data="menu_config")],
    ])

def kb_tipo():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💵 Ingreso", callback_data="tipo_ingreso"),
         InlineKeyboardButton("🔒 Comprometido", callback_data="tipo_comprometido"),
         InlineKeyboardButton("📝 Incurrido", callback_data="tipo_incurrido")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")],
    ])

def kb_confirmar(data: dict):
    import urllib.parse
    d = urllib.parse.urlencode(data)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirmar", callback_data=f"confirmar|{json.dumps(data)}"),
         InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")],
    ])

# ─── HANDLERS ────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_settings(user_id, nombre) VALUES(?,?)",
            (uid, user.first_name)
        )
    msg = (
        f"👋 *¡Hola {user.first_name}!* Soy *Flujo*, tu bot de control de gastos.\n\n"
        "Podés escribirme en lenguaje natural y entiendo todo:\n\n"
        "💵 *Ingresos:*\n"
        "`cobré 150000 sueldo`\n"
        "`ingresé 30000 freelance`\n\n"
        "🔒 *Gastos comprometidos:*\n"
        "`pago 25000 alquiler vence el 10`\n"
        "`debo pagar 3500 luz el 20`\n\n"
        "📝 *Gastos incurridos:*\n"
        "`gasté 5200 supermercado`\n"
        "`pagué 1800 almuerzo`\n\n"
        "📊 Usá los botones para ver resúmenes y vencimientos."
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())

async def cmd_resumen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    texto = build_resumen(uid)
    await update.message.reply_text(texto, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())

async def cmd_vencimientos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    texto = build_vencimientos(uid)
    await update.message.reply_text(texto, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())

async def cmd_ayuda(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📖 *Guía rápida de Flujo*\n\n"
        "*¿Cómo cargar datos?*\n"
        "Escribí en lenguaje natural. Ejemplos:\n\n"
        "💵 `cobré 80000 sueldo`\n"
        "💵 `ingresé 20000 por proyecto`\n"
        "🔒 `pago alquiler 30000 el dia 5`\n"
        "🔒 `debo pagar luz 2500 el 15 mensual`\n"
        "📝 `gasté 4500 en super`\n"
        "📝 `pagué 2000 nafta`\n"
        "📝 `comida 1500`\n\n"
        "*Comandos:*\n"
        "/start — Menú principal\n"
        "/resumen — Resumen del mes\n"
        "/vencimientos — Próximos pagos\n"
        "/ingresos — Ver ingresos del mes\n"
        "/comprometidos — Ver comprometidos\n"
        "/incurridos — Ver gastos realizados\n"
        "/borrar — Borrar último registro\n"
        "/ayuda — Esta ayuda\n\n"
        "*Tip:* Al cargar un comprometido, podés agregar 'mensual' o 'fijo' para que se repita automáticamente."
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_ingresos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    texto = build_lista(uid, 'ingreso')
    await update.message.reply_text(texto, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())

async def cmd_comprometidos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    texto = build_lista(uid, 'comprometido')
    await update.message.reply_text(texto, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())

async def cmd_incurridos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    texto = build_lista(uid, 'incurrido')
    await update.message.reply_text(texto, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())

async def cmd_borrar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    with get_db() as conn:
        # Buscar el último registro en cualquier tabla
        rows = []
        for tabla in ['ingresos', 'incurridos']:
            r = conn.execute(
                f"SELECT id, '{tabla}' as tabla, concepto, monto, created_at FROM {tabla} WHERE user_id=? ORDER BY created_at DESC LIMIT 1",
                (uid,)
            ).fetchone()
            if r:
                rows.append(r)
        r2 = conn.execute(
            "SELECT id, 'comprometidos' as tabla, concepto, monto, created_at FROM comprometidos WHERE user_id=? ORDER BY created_at DESC LIMIT 1",
            (uid,)
        ).fetchone()
        if r2:
            rows.append(r2)

        if not rows:
            await update.message.reply_text("No hay registros para borrar.")
            return

        ultimo = max(rows, key=lambda x: x[4])
        rid, tabla, concepto, monto, _ = ultimo

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Sí, borrar", callback_data=f"borrar|{tabla}|{rid}"),
            InlineKeyboardButton("❌ Cancelar", callback_data="cancelar"),
        ]])
        await update.message.reply_text(
            f"¿Borrar el último registro?\n*{concepto}* — {fmt(monto)}",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb
        )

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handler principal: interpreta mensajes de texto libre."""
    text = update.message.text.strip()
    uid = update.effective_user.id

    # Ignorar comandos
    if text.startswith('/'):
        return

    parsed = parse_message(text)

    if not parsed or not parsed.get('monto'):
        await update.message.reply_text(
            "🤔 No entendí bien. Probá así:\n"
            "• `gasté 5000 supermercado`\n"
            "• `cobré 100000 sueldo`\n"
            "• `pago 25000 alquiler el 10`\n\n"
            "O usá /ayuda para más ejemplos.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_main()
        )
        return

    # Si no hay fecha de vencimiento para comprometido, preguntar
    if parsed['tipo'] == 'comprometido' and not parsed.get('vencimiento'):
        ctx.user_data['pending'] = parsed
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{i}", callback_data=f"dia|{i}") for i in range(1, 8)],
            [InlineKeyboardButton(f"{i}", callback_data=f"dia|{i}") for i in range(8, 15)],
            [InlineKeyboardButton(f"{i}", callback_data=f"dia|{i}") for i in range(15, 22)],
            [InlineKeyboardButton(f"{i}", callback_data=f"dia|{i}") for i in range(22, 29)],
            [InlineKeyboardButton("❌ Sin fecha fija", callback_data="dia|1")],
        ])
        await update.message.reply_text(
            f"🔒 *{parsed['concepto']}* — {fmt(parsed['monto'])}\n"
            "¿Qué día del mes vence?",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb
        )
        return

    # Mostrar confirmación
    await show_confirm(update, ctx, parsed, uid)

async def show_confirm(update_or_query, ctx, parsed, uid):
    """Muestra mensaje de confirmación antes de guardar."""
    tipo = parsed['tipo']
    emoji = detect_emoji(parsed['concepto'], tipo)
    monto_fmt = fmt(parsed['monto'])

    lines = [f"{emoji} *{parsed['concepto']}* — *{monto_fmt}*"]

    if tipo == 'ingreso':
        lines.append(f"📅 Fecha: {date.today().strftime('%d/%m/%Y')}")
        lines.append("Tipo: 💵 Ingreso")
    elif tipo == 'comprometido':
        venc = parsed.get('vencimiento', date.today().isoformat())
        lines.append(f"📅 Vence: {datetime.fromisoformat(venc).strftime('%d/%m/%Y')}")
        lines.append("Tipo: 🔒 Comprometido")
        if parsed.get('recurrente'):
            lines.append("🔁 Se repite mensualmente")
    else:
        lines.append(f"📅 Fecha: {date.today().strftime('%d/%m/%Y')}")
        lines.append("Tipo: 📝 Gasto incurrido")

    lines.append("\n¿Lo registro?")
    text = '\n'.join(lines)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Guardar", callback_data=f"guardar|{json.dumps(parsed)}"),
         InlineKeyboardButton("✏️ Cambiar tipo", callback_data=f"cambio_tipo|{json.dumps(parsed)}")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")],
    ])

    if hasattr(update_or_query, 'message') and update_or_query.message:
        await update_or_query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await update_or_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    data = query.data

    # ── Menú principal ────────────────────────────────────────────────────────
    if data == "menu_resumen":
        await query.edit_message_text(build_resumen(uid), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())
        return
    if data == "menu_vencimientos":
        await query.edit_message_text(build_vencimientos(uid), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())
        return
    if data == "menu_ingresos":
        await query.edit_message_text(build_lista(uid, 'ingreso'), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())
        return
    if data == "menu_comprometidos":
        await query.edit_message_text(build_lista(uid, 'comprometido'), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())
        return
    if data == "menu_incurridos":
        await query.edit_message_text(build_lista(uid, 'incurrido'), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())
        return
    if data == "menu_config":
        await query.edit_message_text(build_config(uid), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())
        return

    # ── Selección de día para comprometido ───────────────────────────────────
    if data.startswith("dia|"):
        dia = int(data.split("|")[1])
        pending = ctx.user_data.get('pending', {})
        hoy = date.today()
        try:
            venc = hoy.replace(day=dia)
            if venc < hoy:
                if hoy.month == 12:
                    venc = venc.replace(year=hoy.year + 1, month=1)
                else:
                    venc = venc.replace(month=hoy.month + 1)
        except ValueError:
            venc = hoy
        pending['vencimiento'] = venc.isoformat()
        ctx.user_data['pending'] = pending
        await show_confirm(query, ctx, pending, uid)
        return

    # ── Guardar ───────────────────────────────────────────────────────────────
    if data.startswith("guardar|"):
        parsed = json.loads(data[8:])
        guardar_registro(uid, parsed)
        tipo = parsed['tipo']
        emoji = detect_emoji(parsed['concepto'], tipo)
        tipo_label = {"ingreso": "Ingreso", "comprometido": "Comprometido", "incurrido": "Gasto"}[tipo]
        msg = (
            f"✅ *{tipo_label} registrado*\n"
            f"{emoji} {parsed['concepto']} — *{fmt(parsed['monto'])}*\n\n"
            f"{build_mini_resumen(uid)}"
        )
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())
        return

    # ── Cambiar tipo ──────────────────────────────────────────────────────────
    if data.startswith("cambio_tipo|"):
        parsed = json.loads(data[12:])
        ctx.user_data['pending_change'] = parsed
        tipo_actual = parsed['tipo']
        tipos = ["ingreso", "comprometido", "incurrido"]
        tipos.remove(tipo_actual)
        botones = [[
            InlineKeyboardButton(
                f"{'💵' if t=='ingreso' else '🔒' if t=='comprometido' else '📝'} {t.capitalize()}",
                callback_data=f"set_tipo|{t}|{json.dumps(parsed)}"
            ) for t in tipos
        ], [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")]]
        await query.edit_message_text(
            "¿Cuál es el tipo correcto?",
            reply_markup=InlineKeyboardMarkup(botones)
        )
        return

    if data.startswith("set_tipo|"):
        parts = data.split("|", 2)
        nuevo_tipo = parts[1]
        parsed = json.loads(parts[2])
        parsed['tipo'] = nuevo_tipo
        if nuevo_tipo == 'comprometido' and not parsed.get('vencimiento'):
            ctx.user_data['pending'] = parsed
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{i}", callback_data=f"dia|{i}") for i in range(1, 8)],
                [InlineKeyboardButton(f"{i}", callback_data=f"dia|{i}") for i in range(8, 15)],
                [InlineKeyboardButton(f"{i}", callback_data=f"dia|{i}") for i in range(15, 22)],
                [InlineKeyboardButton(f"{i}", callback_data=f"dia|{i}") for i in range(22, 29)],
            ])
            await query.edit_message_text("¿Qué día del mes vence?", reply_markup=kb)
        else:
            await show_confirm(query, ctx, parsed, uid)
        return

    # ── Borrar ────────────────────────────────────────────────────────────────
    if data.startswith("borrar|"):
        _, tabla, rid = data.split("|")
        with get_db() as conn:
            conn.execute(f"DELETE FROM {tabla} WHERE id=? AND user_id=?", (rid, uid))
        await query.edit_message_text("🗑️ Registro eliminado.", reply_markup=kb_main())
        return

    # ── Marcar comprometido como pagado ──────────────────────────────────────
    if data.startswith("pagar|"):
        cid = data.split("|")[1]
        with get_db() as conn:
            conn.execute("UPDATE comprometidos SET estado='pagado' WHERE id=? AND user_id=?", (cid, uid))
        await query.edit_message_text(
            "✅ Marcado como pagado.\n\n" + build_vencimientos(uid),
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main()
        )
        return

    if data == "cancelar":
        await query.edit_message_text("❌ Cancelado.", reply_markup=kb_main())
        return

# ─── GUARDAR REGISTRO ────────────────────────────────────────────────────────
def guardar_registro(uid: int, parsed: dict):
    tipo = parsed['tipo']
    emoji = detect_emoji(parsed['concepto'], tipo)
    with get_db() as conn:
        if tipo == 'ingreso':
            conn.execute(
                "INSERT INTO ingresos(user_id, fecha, monto, concepto, categoria) VALUES(?,?,?,?,?)",
                (uid, date.today().isoformat(), parsed['monto'], parsed['concepto'], emoji)
            )
        elif tipo == 'comprometido':
            venc = parsed.get('vencimiento', date.today().isoformat())
            conn.execute(
                "INSERT INTO comprometidos(user_id, concepto, monto, vencimiento, recurrente, categoria) VALUES(?,?,?,?,?,?)",
                (uid, parsed['concepto'], parsed['monto'], venc, int(parsed.get('recurrente', False)), emoji)
            )
            # Si es recurrente, crear próximos 11 meses
            if parsed.get('recurrente'):
                venc_date = date.fromisoformat(venc)
                for i in range(1, 12):
                    m = venc_date.month + i
                    y = venc_date.year + (m - 1) // 12
                    m = ((m - 1) % 12) + 1
                    try:
                        nueva = venc_date.replace(year=y, month=m)
                        conn.execute(
                            "INSERT OR IGNORE INTO comprometidos(user_id, concepto, monto, vencimiento, recurrente, categoria) VALUES(?,?,?,?,?,?)",
                            (uid, parsed['concepto'], parsed['monto'], nueva.isoformat(), 1, emoji)
                        )
                    except ValueError:
                        pass
        else:  # incurrido
            conn.execute(
                "INSERT INTO incurridos(user_id, fecha, monto, concepto, categoria) VALUES(?,?,?,?,?)",
                (uid, date.today().isoformat(), parsed['monto'], parsed['concepto'], emoji)
            )

# ─── BUILDERS DE TEXTO ──────────────────────────────────────────────────────
def build_resumen(uid: int) -> str:
    hoy = date.today()
    mes = hoy.replace(day=1)
    mes_sig = (mes.replace(month=mes.month % 12 + 1) if mes.month < 12 else mes.replace(year=mes.year + 1, month=1))
    mes_label = hoy.strftime("%B %Y").capitalize()

    with get_db() as conn:
        total_ing = conn.execute(
            "SELECT COALESCE(SUM(monto),0) FROM ingresos WHERE user_id=? AND fecha>=? AND fecha<?",
            (uid, mes.isoformat(), mes_sig.isoformat())
        ).fetchone()[0]
        total_comp = conn.execute(
            "SELECT COALESCE(SUM(monto),0) FROM comprometidos WHERE user_id=? AND vencimiento>=? AND vencimiento<?",
            (uid, mes.isoformat(), mes_sig.isoformat())
        ).fetchone()[0]
        total_comp_pend = conn.execute(
            "SELECT COALESCE(SUM(monto),0) FROM comprometidos WHERE user_id=? AND vencimiento>=? AND vencimiento<? AND estado='pendiente'",
            (uid, mes.isoformat(), mes_sig.isoformat())
        ).fetchone()[0]
        total_inc = conn.execute(
            "SELECT COALESCE(SUM(monto),0) FROM incurridos WHERE user_id=? AND fecha>=? AND fecha<?",
            (uid, mes.isoformat(), mes_sig.isoformat())
        ).fetchone()[0]

    disponible = total_ing - total_inc
    proyeccion = disponible - total_comp_pend
    ahorro = total_ing - total_comp - total_inc
    pct_ahorro = (ahorro / total_ing * 100) if total_ing > 0 else 0

    def barra(val, total, largo=10):
        if total <= 0:
            return "░" * largo
        n = min(largo, int(val / total * largo))
        return "█" * n + "░" * (largo - n)

    pct_disp = (disponible / total_ing) if total_ing > 0 else 0
    estado_disp = "🟢" if pct_disp > 0.5 else "🟡" if pct_disp > 0.2 else "🔴"

    lines = [
        f"📊 *Resumen — {mes_label}*\n",
        f"💵 *Ingresos:* {fmt(total_ing)}",
        f"🔒 *Comprometidos:* {fmt(total_comp)}",
        f"📝 *Incurridos:* {fmt(total_inc)}\n",
        f"{'─'*28}",
        f"{estado_disp} *Disponible ahora:* {fmt(disponible)}",
        f"   `{barra(disponible, total_ing)}` {int(pct_disp*100)}%\n",
        f"🔮 *Post-comprometidos:* {fmt(proyeccion)}",
        f"   ({'✅ Positivo' if proyeccion >= 0 else '❌ Negativo'})\n",
        f"💰 *Ahorro estimado:* {fmt(ahorro)} ({pct_ahorro:.0f}%)",
    ]
    return '\n'.join(lines)

def build_mini_resumen(uid: int) -> str:
    """Resumen compacto que aparece tras guardar."""
    hoy = date.today()
    mes = hoy.replace(day=1)
    mes_sig = (mes.replace(month=mes.month % 12 + 1) if mes.month < 12 else mes.replace(year=mes.year + 1, month=1))
    with get_db() as conn:
        ing = conn.execute("SELECT COALESCE(SUM(monto),0) FROM ingresos WHERE user_id=? AND fecha>=? AND fecha<?", (uid, mes.isoformat(), mes_sig.isoformat())).fetchone()[0]
        comp_pend = conn.execute("SELECT COALESCE(SUM(monto),0) FROM comprometidos WHERE user_id=? AND vencimiento>=? AND vencimiento<? AND estado='pendiente'", (uid, mes.isoformat(), mes_sig.isoformat())).fetchone()[0]
        inc = conn.execute("SELECT COALESCE(SUM(monto),0) FROM incurridos WHERE user_id=? AND fecha>=? AND fecha<?", (uid, mes.isoformat(), mes_sig.isoformat())).fetchone()[0]
    disp = ing - inc
    proy = disp - comp_pend
    return (
        f"💵 Ingresos: {fmt(ing)} | 📝 Gastado: {fmt(inc)}\n"
        f"✅ Disponible: *{fmt(disp)}* | 🔮 Post-pagos: *{fmt(proy)}*"
    )

def build_vencimientos(uid: int) -> str:
    hoy = date.today()
    limite = hoy + timedelta(days=30)
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, concepto, monto, vencimiento, estado, categoria
               FROM comprometidos
               WHERE user_id=? AND vencimiento>=? AND vencimiento<=? AND estado='pendiente'
               ORDER BY vencimiento ASC""",
            (uid, hoy.isoformat(), limite.isoformat())
        ).fetchall()

    if not rows:
        return "✅ *Sin vencimientos próximos* en los próximos 30 días.\n\nUsá /ayuda para registrar comprometidos."

    lines = ["⏰ *Vencimientos próximos (30 días)*\n"]
    total = 0
    for rid, concepto, monto, venc_str, estado, emoji in rows:
        venc = date.fromisoformat(venc_str)
        dias = (venc - hoy).days
        if dias < 0:
            tag = "🔴 VENCIDO"
        elif dias == 0:
            tag = "🚨 HOY"
        elif dias <= 3:
            tag = f"🟠 en {dias}d"
        elif dias <= 7:
            tag = f"🟡 en {dias}d"
        else:
            tag = f"🟢 en {dias}d"
        lines.append(f"{emoji or '📦'} *{concepto}* — {fmt(monto)}\n   📅 {venc.strftime('%d/%m')} {tag}")
        total += monto

    lines.append(f"\n{'─'*28}")
    lines.append(f"💳 *Total pendiente: {fmt(total)}*")
    return '\n'.join(lines)

def build_lista(uid: int, tipo: str) -> str:
    hoy = date.today()
    mes = hoy.replace(day=1)
    mes_sig = (mes.replace(month=mes.month % 12 + 1) if mes.month < 12 else mes.replace(year=mes.year + 1, month=1))

    if tipo == 'ingreso':
        with get_db() as conn:
            rows = conn.execute(
                "SELECT concepto, monto, fecha, categoria FROM ingresos WHERE user_id=? AND fecha>=? AND fecha<? ORDER BY fecha DESC",
                (uid, mes.isoformat(), mes_sig.isoformat())
            ).fetchall()
        if not rows:
            return "💵 No hay ingresos registrados este mes.\n\nEjemplo: `cobré 80000 sueldo`"
        total = sum(r[1] for r in rows)
        lines = [f"💵 *Ingresos — {hoy.strftime('%B %Y').capitalize()}*\n"]
        for concepto, monto, fecha, emoji in rows:
            d = date.fromisoformat(fecha).strftime('%d/%m')
            lines.append(f"{emoji or '💼'} {concepto} — *{fmt(monto)}* _{d}_")
        lines.append(f"\n*Total: {fmt(total)}*")
        return '\n'.join(lines)

    elif tipo == 'comprometido':
        with get_db() as conn:
            rows = conn.execute(
                "SELECT id, concepto, monto, vencimiento, estado, categoria FROM comprometidos WHERE user_id=? AND vencimiento>=? AND vencimiento<? ORDER BY vencimiento ASC",
                (uid, mes.isoformat(), mes_sig.isoformat())
            ).fetchall()
        if not rows:
            return "🔒 No hay comprometidos este mes.\n\nEjemplo: `pago alquiler 25000 el 10 mensual`"
        total = sum(r[2] for r in rows)
        pend = sum(r[2] for r in rows if r[4] == 'pendiente')
        lines = [f"🔒 *Comprometidos — {hoy.strftime('%B %Y').capitalize()}*\n"]
        for rid, concepto, monto, venc_str, estado, emoji in rows:
            venc = date.fromisoformat(venc_str)
            dias = (venc - hoy).days
            est = "✅" if estado == 'pagado' else ("🔴" if dias < 0 else "🟡" if dias <= 3 else "⏳")
            lines.append(f"{est} {emoji or '📦'} {concepto} — *{fmt(monto)}* _{venc.strftime('%d/%m')}_")
        lines.append(f"\n*Total: {fmt(total)}* | Pendiente: *{fmt(pend)}*")
        return '\n'.join(lines)

    else:  # incurrido
        with get_db() as conn:
            rows = conn.execute(
                "SELECT concepto, monto, fecha, categoria FROM incurridos WHERE user_id=? AND fecha>=? AND fecha<? ORDER BY fecha DESC",
                (uid, mes.isoformat(), mes_sig.isoformat())
            ).fetchall()
        if not rows:
            return "📝 No hay gastos incurridos este mes.\n\nEjemplo: `gasté 5000 supermercado`"
        total = sum(r[1] for r in rows)
        lines = [f"📝 *Gastos incurridos — {hoy.strftime('%B %Y').capitalize()}*\n"]
        for concepto, monto, fecha, emoji in rows:
            d = date.fromisoformat(fecha).strftime('%d/%m')
            lines.append(f"{emoji or '📝'} {concepto} — *{fmt(monto)}* _{d}_")
        lines.append(f"\n*Total gastado: {fmt(total)}*")
        return '\n'.join(lines)

def build_config(uid: int) -> str:
    with get_db() as conn:
        row = conn.execute("SELECT moneda, recordatorio_dias, notif_hora FROM user_settings WHERE user_id=?", (uid,)).fetchone()
    if not row:
        return "⚙️ Configuración no encontrada."
    moneda, dias, hora = row
    return (
        f"⚙️ *Configuración*\n\n"
        f"💱 Moneda: `{moneda}`\n"
        f"⏰ Recordatorios: `{dias}` días antes del vencimiento\n"
        f"🕐 Hora de notificación: `{hora}`\n\n"
        f"Para cambiar, escribí:\n"
        f"`config recordatorio 5` — cambia los días\n"
        f"`config hora 08:00` — cambia la hora"
    )

# ─── JOB: RECORDATORIOS DIARIOS ──────────────────────────────────────────────
async def job_recordatorios(ctx: ContextTypes.DEFAULT_TYPE):
    """Envía recordatorios de vencimientos próximos."""
    hoy = date.today()
    with get_db() as conn:
        settings = conn.execute("SELECT user_id, recordatorio_dias FROM user_settings").fetchall()
        for uid, dias_aviso in settings:
            limite = hoy + timedelta(days=dias_aviso)
            rows = conn.execute(
                """SELECT concepto, monto, vencimiento, categoria
                   FROM comprometidos
                   WHERE user_id=? AND vencimiento>=? AND vencimiento<=? AND estado='pendiente'
                   ORDER BY vencimiento ASC""",
                (uid, hoy.isoformat(), limite.isoformat())
            ).fetchall()
            if rows:
                lines = [f"⏰ *Recordatorio de vencimientos*\n"]
                for concepto, monto, venc_str, emoji in rows:
                    venc = date.fromisoformat(venc_str)
                    dias = (venc - hoy).days
                    tag = "🚨 HOY" if dias == 0 else f"en {dias} día{'s' if dias != 1 else ''}"
                    lines.append(f"{emoji or '📦'} *{concepto}* — {fmt(monto)} ({tag})")
                lines.append(f"\nUsá /vencimientos para más detalle.")
                try:
                    await ctx.bot.send_message(
                        chat_id=uid,
                        text='\n'.join(lines),
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=kb_main()
                    )
                except Exception:
                    pass

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    if not TOKEN:
        print("❌ ERROR: Falta TELEGRAM_TOKEN en variables de entorno.")
        print("   Exportá: export TELEGRAM_TOKEN='tu_token_aqui'")
        return

    init_db()
    print("✅ Base de datos inicializada")

    app = Application.builder().token(TOKEN).build()

    # Comandos
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("resumen", cmd_resumen))
    app.add_handler(CommandHandler("vencimientos", cmd_vencimientos))
    app.add_handler(CommandHandler("ingresos", cmd_ingresos))
    app.add_handler(CommandHandler("comprometidos", cmd_comprometidos))
    app.add_handler(CommandHandler("incurridos", cmd_incurridos))
    app.add_handler(CommandHandler("borrar", cmd_borrar))
    app.add_handler(CommandHandler("ayuda", cmd_ayuda))

    # Callbacks
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Mensajes de texto libre
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Job de recordatorios: todos los días a las 9:00 AM
    app.job_queue.run_daily(
        job_recordatorios,
        time=datetime.strptime("09:00", "%H:%M").time()
    )

    print("🚀 Flujo Bot iniciado. Esperando mensajes...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()