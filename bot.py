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
    "super": "🛒", "supermercado": "🛒", "mercado": "🛒", "verduleria": "🥦",
    "verdulería": "🥦", "carniceria": "🥩", "carnicería": "🥩",
    "comida": "🍽️", "restaurant": "🍽️", "restaurante": "🍽️",
    "cafe": "☕", "café": "☕", "delivery": "🛵",
    "ropa": "👕", "farmacia": "💊", "medicamento": "💊",
    "taxi": "🚕", "uber": "🚕", "remis": "🚕", "nafta": "⛽",
    "cine": "🎬", "teatro": "🎭", "regalo": "🎁", "otro": "📦"
}

def detect_emoji(concepto, tipo):
    c = concepto.lower()
    cats = {"ingreso": CAT_INGRESO, "comprometido": CAT_COMPROMETIDO, "incurrido": CAT_INCURRIDO}.get(tipo, CAT_INCURRIDO)
    for key, emoji in cats.items():
        if key in c:
            return emoji
    return "💼" if tipo == "ingreso" else "🔒" if tipo == "comprometido" else "📝"

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS ingresos (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, fecha DATE NOT NULL, monto REAL NOT NULL, concepto TEXT NOT NULL, categoria TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS comprometidos (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, concepto TEXT NOT NULL, monto REAL NOT NULL, vencimiento DATE NOT NULL, estado TEXT DEFAULT 'pendiente', recurrente INTEGER DEFAULT 0, categoria TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS incurridos (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, fecha DATE NOT NULL, monto REAL NOT NULL, concepto TEXT NOT NULL, categoria TEXT, medio TEXT DEFAULT 'efectivo', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS user_settings (user_id INTEGER PRIMARY KEY, moneda TEXT DEFAULT '$', recordatorio_dias INTEGER DEFAULT 3, notif_hora TEXT DEFAULT '09:00', nombre TEXT);
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
    if re.search(r'\b(ingres[oa]|cobr[eéó]|recibi[oó]|sueldo|salario|honorario|pagaron)\b', t):
        result['tipo'] = 'ingreso'
    elif re.search(r'\b(vence|vencimiento|debo\s+pagar|cuota|el\s+d[ií]a|el\s+\d+)\b', t):
        result['tipo'] = 'comprometido'
    elif re.search(r'\b(gast[eéó]|compré|pagué|gasto|comida|super|taxi|uber|farmacia)\b', t):
        result['tipo'] = 'incurrido'
    else:
        result['tipo'] = 'comprometido' if t.startswith('pago') or t.startswith('pagar') else 'incurrido'
    monto_match = re.search(r'\$?\s*(\d[\d.,]*)', text)
    if not monto_match:
        return None
    try:
        monto = float(monto_match.group(1).replace('.', '').replace(',', '.'))
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
                    venc = venc.replace(month=hoy.month + 1) if hoy.month < 12 else venc.replace(year=hoy.year + 1, month=1)
                result['vencimiento'] = venc.isoformat()
            except:
                pass
    result['recurrente'] = bool(re.search(r'\b(mensual|siempre|todos\s+los\s+meses|fijo|recurrente)\b', t))
    concepto = re.sub(r'\$?\s*\d[\d.,]*', '', text)
    concepto = re.sub(r'\b(ingresé|ingreso|cobré|cobro|recibí|gasté|gasto|pagué|pago|compré|compro)\b', '', concepto, flags=re.I)
    concepto = re.sub(r'\b(en|de|por|el|la|un|una|del|al|fijo|mensual|recurrente|vence|debo|pagar)\b', '', concepto, flags=re.I)
    concepto = re.sub(r'\s+', ' ', concepto).strip().strip('.- ').capitalize() or "Sin concepto"
    result['concepto'] = concepto
    return result

def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💵 Ingresos", callback_data="menu_ingresos"), InlineKeyboardButton("🔒 Comprometidos", callback_data="menu_comprometidos")],
        [InlineKeyboardButton("📝 Incurridos", callback_data="menu_incurridos"), InlineKeyboardButton("📊 Resumen", callback_data="menu_resumen")],
        [InlineKeyboardButton("⏰ Vencimientos", callback_data="menu_vencimientos"), InlineKeyboardButton("⚙️ Config", callback_data="menu_config")],
    ])

async def cmd_start(update, ctx):
    user = update.effective_user
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO user_settings(user_id, nombre) VALUES(?,?)", (user.id, user.first_name))
    await update.message.reply_text(f"👋 *¡Hola {user.first_name}!* Soy *Flujo*.\n\n💵 `cobré 150000 sueldo`\n🔒 `pago 25000 alquiler el 10`\n📝 `gasté 5200 supermercado`", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())

async def cmd_resumen(update, ctx):
    await update.message.reply_text(build_resumen(update.effective_user.id), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())

async def cmd_vencimientos(update, ctx):
    await update.message.reply_text(build_vencimientos(update.effective_user.id), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())

async def cmd_ingresos(update, ctx):
    await update.message.reply_text(build_lista(update.effective_user.id, 'ingreso'), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())

async def cmd_comprometidos(update, ctx):
    await update.message.reply_text(build_lista(update.effective_user.id, 'comprometido'), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())

async def cmd_incurridos(update, ctx):
    await update.message.reply_text(build_lista(update.effective_user.id, 'incurrido'), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())

async def cmd_ayuda(update, ctx):
    await update.message.reply_text("📖 *Ayuda*\n\n💵 `cobré 80000 sueldo`\n🔒 `pago alquiler 30000 el 5 mensual`\n📝 `gasté 4500 super`\n\n/resumen /vencimientos /ingresos /comprometidos /incurridos /borrar", parse_mode=ParseMode.MARKDOWN)

async def cmd_borrar(update, ctx):
    uid = update.effective_user.id
    with get_db() as conn:
        rows = []
        for tabla in ['ingresos', 'incurridos']:
            r = conn.execute(f"SELECT id, '{tabla}', concepto, monto, created_at FROM {tabla} WHERE user_id=? ORDER BY created_at DESC LIMIT 1", (uid,)).fetchone()
            if r: rows.append(r)
        r2 = conn.execute("SELECT id, 'comprometidos', concepto, monto, created_at FROM comprometidos WHERE user_id=? ORDER BY created_at DESC LIMIT 1", (uid,)).fetchone()
        if r2: rows.append(r2)
        if not rows:
            await update.message.reply_text("No hay registros para borrar.")
            return
        rid, tabla, concepto, monto, _ = max(rows, key=lambda x: x[4])
        await update.message.reply_text(f"¿Borrar?\n*{concepto}* — {fmt(monto)}", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Sí", callback_data=f"borrar|{tabla}|{rid}"), InlineKeyboardButton("❌ No", callback_data="cancelar")]]))
async def handle_message(update, ctx):
    text = update.message.text.strip()
    uid = update.effective_user.id
    if text.startswith('/'): return
    parsed = parse_message(text)
    if not parsed or not parsed.get('monto'):
        await update.message.reply_text("🤔 No entendí. Probá:\n• `gasté 5000 super`\n• `cobré 100000 sueldo`\n• `pago 25000 alquiler el 10`", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())
        return
    if parsed['tipo'] == 'comprometido' and not parsed.get('vencimiento'):
        ctx.user_data['pending'] = parsed
        await update.message.reply_text(f"🔒 *{parsed['concepto']}* — {fmt(parsed['monto'])}\n¿Qué día vence?", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{i}", callback_data=f"dia|{i}") for i in range(1,8)],[InlineKeyboardButton(f"{i}", callback_data=f"dia|{i}") for i in range(8,15)],[InlineKeyboardButton(f"{i}", callback_data=f"dia|{i}") for i in range(15,22)],[InlineKeyboardButton(f"{i}", callback_data=f"dia|{i}") for i in range(22,29)],[InlineKeyboardButton("❌ Sin fecha", callback_data="dia|1")]]))
        return
    await show_confirm(update, ctx, parsed, uid)

async def show_confirm(upd, ctx, parsed, uid):
    tipo = parsed['tipo']
    emoji = detect_emoji(parsed['concepto'], tipo)
    venc = parsed.get('vencimiento', date.today().isoformat())
    info = f"📅 Vence: {datetime.fromisoformat(venc).strftime('%d/%m/%Y')}\n🔒 Comprometido" if tipo == 'comprometido' else f"📅 {date.today().strftime('%d/%m/%Y')}\n{'💵 Ingreso' if tipo=='ingreso' else '📝 Gasto'}"
    if parsed.get('recurrente'): info += "\n🔁 Mensual"
    text = f"{emoji} *{parsed['concepto']}* — *{fmt(parsed['monto'])}*\n{info}\n\n¿Lo registro?"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Guardar", callback_data=f"guardar|{json.dumps(parsed)}"), InlineKeyboardButton("✏️ Cambiar tipo", callback_data=f"cambio_tipo|{json.dumps(parsed)}")],[InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")]])
    if hasattr(upd, 'message') and upd.message:
        await upd.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await upd.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def handle_callback(update, ctx):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    data = query.data
    if data == "menu_resumen": await query.edit_message_text(build_resumen(uid), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main()); return
    if data == "menu_vencimientos": await query.edit_message_text(build_vencimientos(uid), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main()); return
    if data == "menu_ingresos": await query.edit_message_text(build_lista(uid,'ingreso'), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main()); return
    if data == "menu_comprometidos": await query.edit_message_text(build_lista(uid,'comprometido'), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main()); return
    if data == "menu_incurridos": await query.edit_message_text(build_lista(uid,'incurrido'), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main()); return
    if data == "menu_config": await query.edit_message_text(build_config(uid), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main()); return
    if data.startswith("dia|"):
        dia = int(data.split("|")[1])
        pending = ctx.user_data.get('pending', {})
        hoy = date.today()
        try:
            venc = hoy.replace(day=dia)
            if venc < hoy: venc = venc.replace(month=hoy.month+1) if hoy.month < 12 else venc.replace(year=hoy.year+1, month=1)
        except: venc = hoy
        pending['vencimiento'] = venc.isoformat()
        ctx.user_data['pending'] = pending
        await show_confirm(query, ctx, pending, uid); return
    if data.startswith("guardar|"):
        parsed = json.loads(data[8:])
        guardar_registro(uid, parsed)
        tipo_label = {"ingreso":"Ingreso","comprometido":"Comprometido","incurrido":"Gasto"}[parsed['tipo']]
        await query.edit_message_text(f"✅ *{tipo_label} registrado*\n{detect_emoji(parsed['concepto'],parsed['tipo'])} {parsed['concepto']} — *{fmt(parsed['monto'])}*\n\n{build_mini_resumen(uid)}", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main()); return
    if data.startswith("cambio_tipo|"):
        parsed = json.loads(data[12:])
        tipos = [t for t in ["ingreso","comprometido","incurrido"] if t != parsed['tipo']]
        await query.edit_message_text("¿Cuál es el tipo correcto?", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{'💵' if t=='ingreso' else '🔒' if t=='comprometido' else '📝'} {t.capitalize()}", callback_data=f"set_tipo|{t}|{json.dumps(parsed)}") for t in tipos],[InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")]])); return
    if data.startswith("set_tipo|"):
        parts = data.split("|", 2); parsed = json.loads(parts[2]); parsed['tipo'] = parts[1]
        if parts[1] == 'comprometido' and not parsed.get('vencimiento'):
            ctx.user_data['pending'] = parsed
            await query.edit_message_text("¿Qué día vence?", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{i}", callback_data=f"dia|{i}") for i in range(1,8)],[InlineKeyboardButton(f"{i}", callback_data=f"dia|{i}") for i in range(8,15)],[InlineKeyboardButton(f"{i}", callback_data=f"dia|{i}") for i in range(15,22)],[InlineKeyboardButton(f"{i}", callback_data=f"dia|{i}") for i in range(22,29)]]))
        else: await show_confirm(query, ctx, parsed, uid); return
    if data.startswith("borrar|"):
        _, tabla, rid = data.split("|")
        with get_db() as conn: conn.execute(f"DELETE FROM {tabla} WHERE id=? AND user_id=?", (rid, uid))
        await query.edit_message_text("🗑️ Eliminado.", reply_markup=kb_main()); return
    if data == "cancelar": await query.edit_message_text("❌ Cancelado.", reply_markup=kb_main()); return

def guardar_registro(uid, parsed):
    tipo = parsed['tipo']
    emoji = detect_emoji(parsed['concepto'], tipo)
    with get_db() as conn:
        if tipo == 'ingreso':
            conn.execute("INSERT INTO ingresos(user_id,fecha,monto,concepto,categoria) VALUES(?,?,?,?,?)", (uid, date.today().isoformat(), parsed['monto'], parsed['concepto'], emoji))
        elif tipo == 'comprometido':
            venc = parsed.get('vencimiento', date.today().isoformat())
            conn.execute("INSERT INTO comprometidos(user_id,concepto,monto,vencimiento,recurrente,categoria) VALUES(?,?,?,?,?,?)", (uid, parsed['concepto'], parsed['monto'], venc, int(parsed.get('recurrente',False)), emoji))
            if parsed.get('recurrente'):
                venc_date = date.fromisoformat(venc)
                for i in range(1, 12):
                    m = venc_date.month + i; y = venc_date.year + (m-1)//12; m = ((m-1)%12)+1
                    try: conn.execute("INSERT OR IGNORE INTO comprometidos(user_id,concepto,monto,vencimiento,recurrente,categoria) VALUES(?,?,?,?,?,?)", (uid, parsed['concepto'], parsed['monto'], venc_date.replace(year=y,month=m).isoformat(), 1, emoji))
                    except: pass
        else:
            conn.execute("INSERT INTO incurridos(user_id,fecha,monto,concepto,categoria) VALUES(?,?,?,?,?)", (uid, date.today().isoformat(), parsed['monto'], parsed['concepto'], emoji))

def build_resumen(uid):
    hoy = date.today(); mes = hoy.replace(day=1)
    mes_sig = mes.replace(month=mes.month%12+1) if mes.month < 12 else mes.replace(year=mes.year+1,month=1)
    with get_db() as conn:
        ing = conn.execute("SELECT COALESCE(SUM(monto),0) FROM ingresos WHERE user_id=? AND fecha>=? AND fecha<?", (uid,mes.isoformat(),mes_sig.isoformat())).fetchone()[0]
        comp = conn.execute("SELECT COALESCE(SUM(monto),0) FROM comprometidos WHERE user_id=? AND vencimiento>=? AND vencimiento<?", (uid,mes.isoformat(),mes_sig.isoformat())).fetchone()[0]
        comp_pend = conn.execute("SELECT COALESCE(SUM(monto),0) FROM comprometidos WHERE user_id=? AND vencimiento>=? AND vencimiento<? AND estado='pendiente'", (uid,mes.isoformat(),mes_sig.isoformat())).fetchone()[0]
        inc = conn.execute("SELECT COALESCE(SUM(monto),0) FROM incurridos WHERE user_id=? AND fecha>=? AND fecha<?", (uid,mes.isoformat(),mes_sig.isoformat())).fetchone()[0]
    disp = ing - inc; proy = disp - comp_pend; ahorro = ing - comp - inc
    pct = int(disp/ing*100) if ing > 0 else 0
    estado = "🟢" if pct > 50 else "🟡" if pct > 20 else "🔴"
    return f"📊 *Resumen — {hoy.strftime('%B %Y').capitalize()}*\n\n💵 Ingresos: *{fmt(ing)}*\n🔒 Comprometidos: *{fmt(comp)}*\n📝 Incurridos: *{fmt(inc)}*\n\n{estado} Disponible: *{fmt(disp)}* ({pct}%)\n🔮 Post-comprometidos: *{fmt(proy)}*\n💰 Ahorro: *{fmt(ahorro)}*"

def build_mini_resumen(uid):
    hoy = date.today(); mes = hoy.replace(day=1)
    mes_sig = mes.replace(month=mes.month%12+1) if mes.month < 12 else mes.replace(year=mes.year+1,month=1)
    with get_db() as conn:
        ing = conn.execute("SELECT COALESCE(SUM(monto),0) FROM ingresos WHERE user_id=? AND fecha>=? AND fecha<?", (uid,mes.isoformat(),mes_sig.isoformat())).fetchone()[0]
        cp = conn.execute("SELECT COALESCE(SUM(monto),0) FROM comprometidos WHERE user_id=? AND vencimiento>=? AND vencimiento<? AND estado='pendiente'", (uid,mes.isoformat(),mes_sig.isoformat())).fetchone()[0]
        inc = conn.execute("SELECT COALESCE(SUM(monto),0) FROM incurridos WHERE user_id=? AND fecha>=? AND fecha<?", (uid,mes.isoformat(),mes_sig.isoformat())).fetchone()[0]
    disp = ing - inc
    return f"💵 {fmt(ing)} | 📝 {fmt(inc)}\n✅ Disponible: *{fmt(disp)}* | 🔮 Post-pagos: *{fmt(disp-cp)}*"

def build_vencimientos(uid):
    hoy = date.today()
    with get_db() as conn:
        rows = conn.execute("SELECT id,concepto,monto,vencimiento,estado,categoria FROM comprometidos WHERE user_id=? AND vencimiento>=? AND vencimiento<=? AND estado='pendiente' ORDER BY vencimiento ASC", (uid,hoy.isoformat(),(hoy+timedelta(days=30)).isoformat())).fetchall()
    if not rows: return "✅ *Sin vencimientos* en los próximos 30 días."
    lines = ["⏰ *Vencimientos próximos*\n"]; total = 0
    for rid,concepto,monto,venc_str,estado,emoji in rows:
        venc = date.fromisoformat(venc_str); dias = (venc-hoy).days
        tag = "🔴 VENCIDO" if dias<0 else "🚨 HOY" if dias==0 else f"🟠 {dias}d" if dias<=3 else f"🟡 {dias}d" if dias<=7 else f"🟢 {dias}d"
        lines.append(f"{emoji or '📦'} *{concepto}* — {fmt(monto)} {tag}"); total += monto
    lines.append(f"\n💳 *Total: {fmt(total)}*")
    return '\n'.join(lines)

def build_lista(uid, tipo):
    hoy = date.today(); mes = hoy.replace(day=1)
    mes_sig = mes.replace(month=mes.month%12+1) if mes.month < 12 else mes.replace(year=mes.year+1,month=1)
    if tipo == 'ingreso':
        with get_db() as conn:
            rows = conn.execute("SELECT concepto,monto,fecha,categoria FROM ingresos WHERE user_id=? AND fecha>=? AND fecha<? ORDER BY fecha DESC", (uid,mes.isoformat(),mes_sig.isoformat())).fetchall()
        if not rows: return "💵 Sin ingresos este mes.\n\nEjemplo: `cobré 80000 sueldo`"
        lines = [f"💵 *Ingresos — {hoy.strftime('%B %Y').capitalize()}*\n"] + [f"{e or '💼'} {c} — *{fmt(m)}* _{date.fromisoformat(f).strftime('%d/%m')}_" for c,m,f,e in rows]
        lines.append(f"\n*Total: {fmt(sum(r[1] for r in rows))}*"); return '\n'.join(lines)
    elif tipo == 'comprometido':
        with get_db() as conn:
            rows = conn.execute("SELECT id,concepto,monto,vencimiento,estado,categoria FROM comprometidos WHERE user_id=? AND vencimiento>=? AND vencimiento<? ORDER BY vencimiento ASC", (uid,mes.isoformat(),mes_sig.isoformat())).fetchall()
        if not rows: return "🔒 Sin comprometidos este mes.\n\nEjemplo: `pago alquiler 25000 el 10 mensual`"
        lines = [f"🔒 *Comprometidos — {hoy.strftime('%B %Y').capitalize()}*\n"]
        for rid,c,m,v,est,e in rows:
            d=(date.fromisoformat(v)-hoy).days; s="✅" if est=='pagado' else "🔴" if d<0 else "🟡" if d<=3 else "⏳"
            lines.append(f"{s} {e or '📦'} {c} — *{fmt(m)}* _{date.fromisoformat(v).strftime('%d/%m')}_")
        lines.append(f"\n*Total: {fmt(sum(r[2] for r in rows))}* | Pendiente: *{fmt(sum(r[2] for r in rows if r[4]=='pendiente'))}*"); return '\n'.join(lines)
    else:
        with get_db() as conn:
            rows = conn.execute("SELECT concepto,monto,fecha,categoria FROM incurridos WHERE user_id=? AND fecha>=? AND fecha<? ORDER BY fecha DESC", (uid,mes.isoformat(),mes_sig.isoformat())).fetchall()
        if not rows: return "📝 Sin gastos este mes.\n\nEjemplo: `gasté 5000 super`"
        lines = [f"📝 *Gastos — {hoy.strftime('%B %Y').capitalize()}*\n"] + [f"{e or '📝'} {c} — *{fmt(m)}* _{date.fromisoformat(f).strftime('%d/%m')}_" for c,m,f,e in rows]
        lines.append(f"\n*Total: {fmt(sum(r[1] for r in rows))}*"); return '\n'.join(lines)

def build_config(uid):
    with get_db() as conn:
        row = conn.execute("SELECT moneda,recordatorio_dias,notif_hora FROM user_settings WHERE user_id=?", (uid,)).fetchone()
    if not row: return "⚙️ Config no encontrada."
    return f"⚙️ *Configuración*\n\n💱 Moneda: `{row[0]}`\n⏰ Recordatorios: `{row[1]}` días antes\n🕐 Hora: `{row[2]}`"

async def job_recordatorios(ctx):
    hoy = date.today()
    with get_db() as conn:
        for uid, dias_aviso in conn.execute("SELECT user_id,recordatorio_dias FROM user_settings").fetchall():
            rows = conn.execute("SELECT concepto,monto,vencimiento,categoria FROM comprometidos WHERE user_id=? AND vencimiento>=? AND vencimiento<=? AND estado='pendiente' ORDER BY vencimiento ASC", (uid,hoy.isoformat(),(hoy+timedelta(days=dias_aviso)).isoformat())).fetchall()
            if rows:
                lines = ["⏰ *Recordatorio*\n"] + [f"{e or '📦'} *{c}* — {fmt(m)} ({'🚨 HOY' if (date.fromisoformat(v)-hoy).days==0 else f'en {(date.fromisoformat(v)-hoy).days}d'})" for c,m,v,e in rows]
                try: await ctx.bot.send_message(chat_id=uid, text='\n'.join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())
                except: pass

def main():
    if not TOKEN: print("❌ Falta TELEGRAM_TOKEN"); return
    init_db(); print("✅ DB lista")
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
    app.job_queue.run_daily(job_recordatorios, time=datetime.strptime("09:00","%H:%M").time())
    print("🚀 Bot iniciado")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
