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
    "super": "🛒", "supermercado": "🛒", "mercado": "🛒",
    "comida": "🍽️", "restaurant": "🍽️", "restaurante": "🍽️",
    "cafe": "☕", "café": "☕", "delivery": "🛵",
    "ropa": "👕", "farmacia": "💊", "medicamento": "💊",
    "taxi": "🚕", "uber": "🚕", "remis": "🚕", "nafta": "⛽",
    "cine": "🎬", "teatro": "🎭", "regalo": "🎁", "otro": "📦"
}

def detect_emoji(concepto, tipo):
    c = concepto.lower()
    cat_map = {"ingreso": CAT_INGRESO, "comprometido": CAT_COMPROMETIDO, "incurrido": CAT_INCURRIDO}
    cats = cat_map.get(tipo, CAT_INCURRIDO)
    for key, emoji in cats.items():
        if key in c:
            return emoji
    return "💼" if tipo == "ingreso" else "🔒" if tipo == "comprometido" else "📝"

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

def fmt(monto, user_id=None):
    return f"${monto:,.0f}".replace(",", ".")
def parse_message(text):
    text = text.strip()
    result = {}
    t = text.lower()
    t = re.sub(r'\s+', ' ', t)

    palabras_ingreso = r'\b(ingres[oa]|cobr[eéó]|recibi[oó]|sueldo|salario|honorario|pagaron|me\s+pagaron)\b'
    palabras_comprometido = r'\b(vence|vencimiento|debo\s+pagar|hay\s+que\s+pagar|cuota|el\s+d[ií]a|el\s+\d+)\b'
    palabras_incurrido = r'\b(gast[eéó]|compré|pagué|compr[eéó]|gasto|comida|super|taxi|uber|farmacia)\b'

    if re.search(palabras_ingreso, t):
        result['tipo'] = 'ingreso'
    elif re.search(palabras_comprometido, t):
        result['tipo'] = 'comprometido'
    elif re.search(palabras_incurrido, t):
        result['tipo'] = 'incurrido'
    else:
        if t.startswith('pago') or t.startswith('pagar'):
            result['tipo'] = 'comprometido'
        else:
            result['tipo'] = 'incurrido'

    monto_match = re.search(r'\$?\s*(\d[\d.,]*)', text)
    if not monto_match:
        return None
    monto_str = monto_match.group(1).replace('.', '').replace(',', '.')
    try:
        monto = float(monto_str)
    except:
        return None
    result['monto'] = monto

    dia_match = re.search(r'\b(?:el\s+)?(?:d[ií]a\s+)?(\d{1,2})\b', t)
    if dia_match and result['tipo'] == 'comprometido':
        dia = int(dia_match.group(1))
        if 1 <= dia <= 31:
            hoy = date.today()
            try:
                venc = hoy.replace(day=dia)
                if venc < hoy:
                    if hoy.month == 12:
                        venc = venc.replace(year=hoy.year + 1, month=1)
                    else:
                        venc = venc.replace(month=hoy.month + 1)
                result['vencimiento'] = venc.isoformat()
            except:
                pass

    result['recurrente'] = bool(re.search(r'\b(mensual|siempre|todos\s+los\s+meses|fijo|recurrente)\b', t))

    concepto = text
    concepto = re.sub(r'\$?\s*\d[\d.,]*', '', concepto)
    concepto = re.sub(r'\b(ingresé|ingreso|cobré|cobro|recibí|recibo|gasté|gasto|pagué|pago|compré|compro)\b', '', concepto, flags=re.I)
    concepto = re.sub(r'\b(en|de|por|el|la|un|una|del|al|fijo|mensual|recurrente|vence|debo|pagar)\b', '', concepto, flags=re.I)
    concepto = re.sub(r'\s+', ' ', concepto).strip().strip('.- ')
    if not concepto:
        concepto = "Sin concepto"
    concepto = concepto.capitalize()
    result['concepto'] = concepto
    return result

def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💵 Ingresos", callback_data="menu_ingresos"),
         InlineKeyboardButton("🔒 Comprometidos", callback_data="menu_comprometidos")],
        [InlineKeyboardButton("📝 Incurridos", callback_data="menu_incurridos"),
         InlineKeyboardButton("📊 Resumen", callback_data="menu_resumen")],
        [InlineKeyboardButton("⏰ Vencimientos", callback_data="menu_vencimientos"),
         InlineKeyboardButton("⚙️ Config", callback_data="menu_config")],
    ])
def parse_message(text):
    text = text.strip()
    result = {}
    t = text.lower()
    t = re.sub(r'\s+', ' ', t)

    palabras_ingreso = r'\b(ingres[oa]|cobr[eéó]|recibi[oó]|sueldo|salario|honorario|pagaron|me\s+pagaron)\b'
    palabras_comprometido = r'\b(vence|vencimiento|debo\s+pagar|hay\s+que\s+pagar|cuota|el\s+d[ií]a|el\s+\d+)\b'
    palabras_incurrido = r'\b(gast[eéó]|compré|pagué|compr[eéó]|gasto|comida|super|taxi|uber|farmacia)\b'

    if re.search(palabras_ingreso, t):
        result['tipo'] = 'ingreso'
    elif re.search(palabras_comprometido, t):
        result['tipo'] = 'comprometido'
    elif re.search(palabras_incurrido, t):
        result['tipo'] = 'incurrido'
    else:
        if t.startswith('pago') or t.startswith('pagar'):
            result['tipo'] = 'comprometido'
        else:
            result['tipo'] = 'incurrido'

    monto_match = re.search(r'\$?\s*(\d[\d.,]*)', text)
    if not monto_match:
        return None
    monto_str = monto_match.group(1).replace('.', '').replace(',', '.')
    try:
        monto = float(monto_str)
    except:
        return None
    result['monto'] = monto

    dia_match = re.search(r'\b(?:el\s+)?(?:d[ií]a\s+)?(\d{1,2})\b', t)
    if dia_match and result['tipo'] == 'comprometido':
        dia = int(dia_match.group(1))
        if 1 <= dia <= 31:
            hoy = date.today()
            try:
                venc = hoy.replace(day=dia)
                if venc < hoy:
                    if hoy.month == 12:
                        venc = venc.replace(year=hoy.year + 1, month=1)
                    else:
                        venc = venc.replace(month=hoy.month + 1)
                result['vencimiento'] = venc.isoformat()
            except:
                pass

    result['recurrente'] = bool(re.search(r'\b(mensual|siempre|todos\s+los\s+meses|fijo|recurrente)\b', t))

    concepto = text
    concepto = re.sub(r'\$?\s*\d[\d.,]*', '', concepto)
    concepto = re.sub(r'\b(ingresé|ingreso|cobré|cobro|recibí|recibo|gasté|gasto|pagué|pago|compré|compro)\b', '', concepto, flags=re.I)
    concepto = re.sub(r'\b(en|de|por|el|la|un|una|del|al|fijo|mensual|recurrente|vence|debo|pagar)\b', '', concepto, flags=re.I)
    concepto = re.sub(r'\s+', ' ', concepto).strip().strip('.- ')
    if not concepto:
        concepto = "Sin concepto"
    concepto = concepto.capitalize()
    result['concepto'] = concepto
    return result

def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💵 Ingresos", callback_data="menu_ingresos"),
         InlineKeyboardButton("🔒 Comprometidos", callback_data="menu_comprometidos")],
        [InlineKeyboardButton("📝 Incurridos", callback_data="menu_incurridos"),
         InlineKeyboardButton("📊 Resumen", callback_data="menu_resumen")],
        [InlineKeyboardButton("⏰ Vencimientos", callback_data="menu_vencimientos"),
         InlineKeyboardButton("⚙️ Config", callback_data="menu_config")],
    ])
async def handle_callback(update, ctx):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    data = query.data

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
        except:
            venc = hoy
        pending['vencimiento'] = venc.isoformat()
        ctx.user_data['pending'] = pending
        await show_confirm(query, ctx, pending, uid)
        return
    if data.startswith("guardar|"):
        parsed = json.loads(data[8:])
        guardar_registro(uid, parsed)
        tipo = parsed['tipo']
        emoji = detect_emoji(parsed['concepto'], tipo)
        tipo_label = {"ingreso": "Ingreso", "comprometido": "Comprometido", "incurrido": "Gasto"}[tipo]
        msg = f"✅ *{tipo_label} registrado*\n{emoji} {parsed['concepto']} — *{fmt(parsed['monto'])}*\n\n{build_mini_resumen(uid)}"
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())
        return
    if data.startswith("cambio_tipo|"):
        parsed = json.loads(data[12:])
        tipo_actual = parsed['tipo']
        tipos = ["ingreso", "comprometido", "incurrido"]
        tipos.remove(tipo_actual)
        botones = [[InlineKeyboardButton(f"{'💵' if t=='ingreso' else '🔒' if t=='comprometido' else '📝'} {t.capitalize()}", callback_data=f"set_tipo|{t}|{json.dumps(parsed)}") for t in tipos], [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")]]
        await query.edit_message_text("¿Cuál es el tipo correcto?", reply_markup=InlineKeyboardMarkup(botones))
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
            await query.edit_message_text("¿Qué día vence?", reply_markup=kb)
        else:
            await show_confirm(query, ctx, parsed, uid)
        return
    if data.startswith("borrar|"):
        _, tabla, rid = data.split("|")
        with get_db() as conn:
            conn.execute(f"DELETE FROM {tabla} WHERE id=? AND user_id=?", (rid, uid))
        await query.edit_message_text("🗑️ Eliminado.", reply_markup=kb_main())
        return
    if data == "cancelar":
        await query.edit_message_text("❌ Cancelado.", reply_markup=kb_main())
        return

def guardar_registro(uid, parsed):
    tipo = parsed['tipo']
    emoji = detect_emoji(parsed['concepto'], tipo)
    with get_db() as conn:
        if tipo == 'ingreso':
            conn.execute("INSERT INTO ingresos(user_id, fecha, monto, concepto, categoria) VALUES(?,?,?,?,?)", (uid, date.today().isoformat(), parsed['monto'], parsed['concepto'], emoji))
        elif tipo == 'comprometido':
            venc = parsed.get('vencimiento', date.today().isoformat())
            conn.execute("INSERT INTO comprometidos(user_id, concepto, monto, vencimiento, recurrente, categoria) VALUES(?,?,?,?,?,?)", (uid, parsed['concepto'], parsed['monto'], venc, int(parsed.get('recurrente', False)), emoji))
            if parsed.get('recurrente'):
                venc_date = date.fromisoformat(venc)
                for i in range(1, 12):
                    m = venc_date.month + i
                    y = venc_date.year + (m - 1) // 12
                    m = ((m - 1) % 12) + 1
                    try:
                        nueva = venc_date.replace(year=y, month=m)
                        conn.execute("INSERT OR IGNORE INTO comprometidos(user_id, concepto, monto, vencimiento, recurrente, categoria) VALUES(?,?,?,?,?,?)", (uid, parsed['concepto'], parsed['monto'], nueva.isoformat(), 1, emoji))
                    except:
                        pass
        else:
            conn.execute("INSERT INTO incurridos(user_id, fecha, monto, concepto, categoria) VALUES(?,?,?,?,?)", (uid, date.today().isoformat(), parsed['monto'], parsed['concepto'], emoji))

def build_resumen(uid):
    hoy = date.today()
    mes = hoy.replace(day=1)
    mes_sig = mes.replace(month=mes.month % 12 + 1) if mes.month < 12 else mes.replace(year=mes.year + 1, month=1)
    with get_db() as conn:
        ing = conn.execute("SELECT COALESCE(SUM(monto),0) FROM ingresos WHERE user_id=? AND fecha>=? AND fecha<?", (uid, mes.isoformat(), mes_sig.isoformat())).fetchone()[0]
        comp = conn.execute("SELECT COALESCE(SUM(monto),0) FROM comprometidos WHERE user_id=? AND vencimiento>=? AND vencimiento<?", (uid, mes.isoformat(), mes_sig.isoformat())).fetchone()[0]
        comp_pend = conn.execute("SELECT COALESCE(SUM(monto),0) FROM comprometidos WHERE user_id=? AND vencimiento>=? AND vencimiento<? AND estado='pendiente'", (uid, mes.isoformat(), mes_sig.isoformat())).fetchone()[0]
        inc = conn.execute("SELECT COALESCE(SUM(monto),0) FROM incurridos WHERE user_id=? AND fecha>=? AND fecha<?", (uid, mes.isoformat(), mes_sig.isoformat())).fetchone()[0]
    disp = ing - inc
    proy = disp - comp_pend
    ahorro = ing - comp - inc
    pct = int(disp / ing * 100) if ing > 0 else 0
    estado = "🟢" if pct > 50 else "🟡" if pct > 20 else "🔴"
    return (
        f"📊 *Resumen — {hoy.strftime('%B %Y').capitalize()}*\n\n"
        f"💵 Ingresos: *{fmt(ing)}*\n"
        f"🔒 Comprometidos: *{fmt(comp)}*\n"
        f"📝 Incurridos: *{fmt(inc)}*\n\n"
        f"{estado} Disponible ahora: *{fmt(disp)}* ({pct}%)\n"
        f"🔮 Post-comprometidos: *{fmt(proy)}*\n"
        f"💰 Ahorro estimado: *{fmt(ahorro)}*"
    )

def build_mini_resumen(uid):
    hoy = date.today()
    mes = hoy.replace(day=1)
    mes_sig = mes.replace(month=mes.month % 12 + 1) if mes.month < 12 else mes.replace(year=mes.year + 1, month=1)
    with get_db() as conn:
        ing = conn.execute("SELECT COALESCE(SUM(monto),0) FROM ingresos WHERE user_id=? AND fecha>=? AND fecha<?", (uid, mes.isoformat(), mes_sig.isoformat())).fetchone()[0]
        comp_pend = conn.execute("SELECT COALESCE(SUM(monto),0) FROM comprometidos WHERE user_id=? AND vencimiento>=? AND vencimiento<? AND estado='pendiente'", (uid, mes.isoformat(), mes_sig.isoformat())).fetchone()[0]
        inc = conn.execute("SELECT COALESCE(SUM(monto),0) FROM incurridos WHERE user_id=? AND fecha>=? AND fecha<?", (uid, mes.isoformat(), mes_sig.isoformat())).fetchone()[0]
    disp = ing - inc
    return f"💵 {fmt(ing)} | 📝 {fmt(inc)}\n✅ Disponible: *{fmt(disp)}* | 🔮 Post-pagos: *{fmt(disp - comp_pend)}*"

def build_vencimientos(uid):
    hoy = date.today()
    limite = hoy + timedelta(days=30)
    with get_db() as conn:
        rows = conn.execute("SELECT id, concepto, monto, vencimiento, estado, categoria FROM comprometidos WHERE user_id=? AND vencimiento>=? AND vencimiento<=? AND estado='pendiente' ORDER BY vencimiento ASC", (uid, hoy.isoformat(), limite.isoformat())).fetchall()
    if not rows:
        return "✅ *Sin vencimientos* en los próximos 30 días."
    lines = ["⏰ *Vencimientos próximos*\n"]
    total = 0
    for rid, concepto, monto, venc_str, estado, emoji in rows:
        venc = date.fromisoformat(venc_str)
        dias = (venc - hoy).days
        tag = "🔴 VENCIDO" if dias < 0 else "🚨 HOY" if dias == 0 else f"🟠 {dias}d" if dias <= 3 else f"🟡 {dias}d" if dias <= 7 else f"🟢 {dias}d"
        lines.append(f"{emoji or '📦'} *{concepto}* — {fmt(monto)} {tag}")
        total += monto
    lines.append(f"\n💳 *Total: {fmt(total)}*")
    return '\n'.join(lines)

def build_lista(uid, tipo):
    hoy = date.today()
    mes = hoy.replace(day=1)
    mes_sig = mes.replace(month=mes.month % 12 + 1) if mes.month < 12 else mes.replace(year=mes.year + 1, month=1)
    if tipo == 'ingreso':
        with get_db() as conn:
            rows = conn.execute("SELECT concepto, monto, fecha, categoria FROM ingresos WHERE user_id=? AND fecha>=? AND fecha<? ORDER BY fecha DESC", (uid, mes.isoformat(), mes_sig.isoformat())).fetchall()
        if not rows:
            return "💵 Sin ingresos este mes.\n\nEjemplo: `cobré 80000 sueldo`"
        total = sum(r[1] for r in rows)
        lines = [f"💵 *Ingresos — {hoy.strftime('%B %Y').capitalize()}*\n"]
        for concepto, monto, fecha, emoji in rows:
            lines.append(f"{emoji or '💼'} {concepto} — *{fmt(monto)}* _{date.fromisoformat(fecha).strftime('%d/%m')}_")
        lines.append(f"\n*Total: {fmt(total)}*")
        return '\n'.join(lines)
    elif tipo == 'comprometido':
        with get_db() as conn:
            rows = conn.execute("SELECT id, concepto, monto, vencimiento, estado, categoria FROM comprometidos WHERE user_id=? AND vencimiento>=? AND vencimiento<? ORDER BY vencimiento ASC", (uid, mes.isoformat(), mes_sig.isoformat())).fetchall()
        if not rows:
            return "🔒 Sin comprometidos este mes.\n\nEjemplo: `pago alquiler 25000 el 10 mensual`"
        total = sum(r[2] for r in rows)
        pend = sum(r[2] for r in rows if r[4] == 'pendiente')
        lines = [f"🔒 *Comprometidos — {hoy.strftime('%B %Y').capitalize()}*\n"]
        for rid, concepto, monto, venc_str, estado, emoji in rows:
            venc = date.fromisoformat(venc_str)
            est = "✅" if estado == 'pagado' else ("🔴" if (venc - hoy).days < 0 else "🟡" if (venc - hoy).days <= 3 else "⏳")
            lines.append(f"{est} {emoji or '📦'} {concepto} — *{fmt(monto)}* _{venc.strftime('%d/%m')}_")
        lines.append(f"\n*Total: {fmt(total)}* | Pendiente: *{fmt(pend)}*")
        return '\n'.join(lines)
    else:
        with get_db() as conn:
            rows = conn.execute("SELECT concepto, monto, fecha, categoria FROM incurridos WHERE user_id=? AND fecha>=? AND fecha<? ORDER BY fecha DESC", (uid, mes.isoformat(), mes_sig.isoformat())).fetchall()
        if not rows:
            return "📝 Sin gastos este mes.\n\nEjemplo: `gasté 5000 supermercado`"
        total = sum(r[1] for r in rows)
        lines = [f"📝 *Gastos — {hoy.strftime('%B %Y').capitalize()}*\n"]
        for concepto, monto, fecha, emoji in rows:
            lines.append(f"{emoji or '📝'} {concepto} — *{fmt(monto)}* _{date.fromisoformat(fecha).strftime('%d/%m')}_")
        lines.append(f"\n*Total: {fmt(total)}*")
        return '\n'.join(lines)

def build_config(uid):
    with get_db() as conn:
        row = conn.execute("SELECT moneda, recordatorio_dias, notif_hora FROM user_settings WHERE user_id=?", (uid,)).fetchone()
    if not row:
        return "⚙️ Configuración no encontrada."
    moneda, dias, hora = row
    return f"⚙️ *Configuración*\n\n💱 Moneda: `{moneda}`\n⏰ Recordatorios: `{dias}` días antes\n🕐 Hora: `{hora}`"

async def job_recordatorios(ctx):
    hoy = date.today()
    with get_db() as conn:
        settings = conn.execute("SELECT user_id, recordatorio_dias FROM user_settings").fetchall()
        for uid, dias_aviso in settings:
            limite = hoy + timedelta(days=dias_aviso)
            rows = conn.execute("SELECT concepto, monto, vencimiento, categoria FROM comprometidos WHERE user_id=? AND vencimiento>=? AND vencimiento<=? AND estado='pendiente' ORDER BY vencimiento ASC", (uid, hoy.isoformat(), limite.isoformat())).fetchall()
            if rows:
                lines = ["⏰ *Recordatorio de vencimientos*\n"]
                for concepto, monto, venc_str, emoji in rows:
                    venc = date.fromisoformat(venc_str)
                    dias = (venc - hoy).days
                    tag = "🚨 HOY" if dias == 0 else f"en {dias}d"
                    lines.append(f"{emoji or '📦'} *{concepto}* — {fmt(monto)} ({tag})")
                try:
                    await ctx.bot.send_message(chat_id=uid, text='\n'.join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())
                except:
                    pass

def main():
    if not TOKEN:
        print("❌ Falta TELEGRAM_TOKEN")
        return
    init_db()
    print("✅ DB lista")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("resumen", cmd_resumen))
    app.add_handler(CommandHandler("vencimientos", cmd_vencimientos))
    app.add_handler(CommandHandler("ingresos", cmd_ingresos))
    app.add_handler(CommandHandler("comprometidos", cmd_comprometidos))
    app.add_handler(CommandHandler("incurridos", cmd_incurridos))
    app.add_handler(CommandHandler("borrar", cmd_borrar))
    app.add_handler(CommandHandler("ayuda", cmd_ayuda))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.job_queue.run_daily(job_recordatorios, time=datetime.strptime("09:00", "%H:%M").time())
    print("🚀 Bot iniciado")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
