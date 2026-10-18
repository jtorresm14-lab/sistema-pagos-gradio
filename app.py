import gradio as gr
import pandas as pd
import sqlite3
import re
import shutil
from datetime import datetime, date
import random
import os
import json
import urllib.request
import urllib.error
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

# ---------------------------------------------------------
# CONSTANTES DEL SISTEMA
# ---------------------------------------------------------
CUENTA_EMPRESA = "30506843261071"      # TODOS los pagos se acreditan a esta cuenta
CORREO_NOTIFICACION = "juanjosetorresmelgarejo@gmail.com"   # destino de TODAS las notificaciones de pago
CATEGORIAS = ["Servicios", "Impuestos (SUNAT)", "Préstamos Bancarios", "Proveedores", "Planilla"]
METODOS_TODOS = ["Billetera digital", "Tarjeta de débito", "Efectivo"]
METODOS_SERVICIOS = ["Efectivo"]       # Los servicios SOLO se pagan en efectivo
MONTOS_PERMITIDOS = ["1", "2", "3", "4", "5"]   # Simulación: S/ 1 a S/ 5
EXT_EVIDENCIA = (".png", ".jpg", ".jpeg", ".pdf")
MAX_EVIDENCIA_MB = 5
EVID_DIR = "evidencias"
COMP_DIR = "comprobantes"
os.makedirs(EVID_DIR, exist_ok=True)
os.makedirs(COMP_DIR, exist_ok=True)

MESES = {1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril", 5: "Mayo", 6: "Junio",
         7: "Julio", 8: "Agosto", 9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"}

# Rangos de los registros pedidos
REG_PASADO = ("2026-07-01", "2026-09-30")   # Julio, Agosto, Septiembre 2026
REG_FUTURO = ("2026-11-01", "2027-01-31")   # Noviembre, Diciembre 2026 y Enero 2027

# Servicios que se pagan todos los meses (misma fecha de vencimiento: día 10)
SERVICIOS_BASE = [
    ("Luz del Sur", "Recibo de Luz", 4),
    ("Sedapal", "Servicio de Agua", 2),
    ("Claro Empresas", "Internet Fibra Óptica", 5),
]
DIA_VENC = 10

# ---------------------------------------------------------
# BASE DE DATOS SQLITE CON DATOS PRECARGADOS
# ---------------------------------------------------------
conn = sqlite3.connect("pagos_empresa_v2.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute('''
    CREATE TABLE IF NOT EXISTS pagos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        proveedor TEXT NOT NULL,
        concepto TEXT NOT NULL,
        monto REAL NOT NULL,
        fecha_vencimiento DATE NOT NULL,
        categoria TEXT NOT NULL,
        cuenta_destino TEXT NOT NULL,
        metodo_pago TEXT,
        estado TEXT NOT NULL DEFAULT 'Pendiente',
        comprobante TEXT,
        evidencia TEXT
    )
''')
cursor.execute("CREATE TABLE IF NOT EXISTS meta (clave TEXT PRIMARY KEY, valor TEXT)")
conn.commit()

def _insertar_servicios(anio, mes, estado):
    """Inserta luz, agua e internet de un mes. Los meses pasados quedan pagados en efectivo."""
    nombre = MESES[mes]
    fecha = f"{anio}-{mes:02d}-{DIA_VENC:02d}"
    filas = []
    for i, (prov, conc, monto) in enumerate(SERVICIOS_BASE, start=1):
        concepto = f"{conc} - {nombre} {anio}"
        if estado == "Pagado":
            filas.append((prov, concepto, monto, fecha, "Servicios", CUENTA_EMPRESA,
                          "Pagado", "Efectivo", f"EFE-{anio % 100:02d}{mes:02d}{i:02d}", None))
        else:
            filas.append((prov, concepto, monto, fecha, "Servicios", CUENTA_EMPRESA,
                          "Pendiente", None, None, None))
    cursor.executemany('''
        INSERT INTO pagos (proveedor, concepto, monto, fecha_vencimiento, categoria, cuenta_destino,
                           estado, metodo_pago, comprobante, evidencia)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', filas)
    conn.commit()

def sembrar_datos():
    cursor.execute("SELECT COUNT(*) FROM pagos")
    if cursor.fetchone()[0] != 0:
        return

    for anio, mes in [(2026, 7), (2026, 8), (2026, 9)]:                 # meses pasados: pagados
        _insertar_servicios(anio, mes, "Pagado")
    for anio, mes in [(2026, 10), (2026, 11), (2026, 12), (2027, 1)]:   # mes actual y futuros: pendientes
        _insertar_servicios(anio, mes, "Pendiente")

    otros = [
        ("SUNAT", "Pago IGV / Renta Periodo Setiembre", 3, "2026-10-20", "Impuestos (SUNAT)", "Pendiente", None, None),
        ("Banco de Crédito BCP", "Cuota 12/24 Préstamo Laptops", 5, "2026-10-30", "Préstamos Bancarios", "Pendiente", None, None),
        ("Distribuidora Tech Perú", "Factura F001-892 Suministros", 2, "2026-11-05", "Proveedores", "Pendiente", None, None),
        ("Planilla", "Planilla Personal Administrativo", 4, "2026-11-15", "Planilla", "Pendiente", None, None),
        ("Distribuidora Tech Perú", "Factura F001-851 Suministros", 2, "2026-08-20", "Proveedores", "Pagado", "Billetera digital", "BIL-102938"),
        ("BBVA Continental", "Pagaré Comercial Mype", 3, "2026-09-25", "Préstamos Bancarios", "Pagado", "Tarjeta de débito", "TAR-849201"),
    ]
    cursor.executemany('''
        INSERT INTO pagos (proveedor, concepto, monto, fecha_vencimiento, categoria, cuenta_destino,
                           estado, metodo_pago, comprobante, evidencia)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', [(p, c, m, f, cat, CUENTA_EMPRESA, est, met, comp, None) for p, c, m, f, cat, est, met, comp in otros])
    conn.commit()

def migrar_octubre():
    """Bases creadas antes de este cambio no tenían luz/agua/internet de octubre: se agregan una sola vez."""
    cursor.execute("SELECT valor FROM meta WHERE clave = 'servicios_octubre'")
    if cursor.fetchone():
        return
    cursor.execute("SELECT COUNT(*) FROM pagos WHERE categoria = 'Servicios' AND concepto LIKE '%Octubre 2026%'")
    if cursor.fetchone()[0] == 0:
        _insertar_servicios(2026, 10, "Pendiente")
    cursor.execute("INSERT OR REPLACE INTO meta (clave, valor) VALUES ('servicios_octubre', '1')")
    conn.commit()

sembrar_datos()
migrar_octubre()

# ---------------------------------------------------------
# PERMISOS POR ROL
# ---------------------------------------------------------
PERMISOS = {
    "gerente": {"registrar", "editar", "eliminar", "pagar", "ver_registro"},
    "asistente": {"pagar"},
}

def tiene_permiso(rol, accion):
    return accion in PERMISOS.get(rol, set())

# ---------------------------------------------------------
# GENERACIÓN DE PDF Y CORREO
# ---------------------------------------------------------
def ruta_comprobante(pago_id):
    return os.path.join(COMP_DIR, f"comprobante_pago_{pago_id}.pdf")

def generar_pdf_comprobante(pago_id, proveedor, concepto, monto, fecha_venc, metodo, comprobante, evidencia=None):
    filename = ruta_comprobante(pago_id)
    doc = SimpleDocTemplate(filename, pagesize=letter)
    styles = getSampleStyleSheet()
    story = []

    title_style = ParagraphStyle('TitleStyle', parent=styles['Heading1'], fontSize=18, textColor=colors.HexColor("#1E3A8A"), alignment=1)
    sub_style = ParagraphStyle('SubStyle', parent=styles['Normal'], fontSize=10, textColor=colors.gray, alignment=1)

    story.append(Paragraph("<b>SISTEMA DE GESTIÓN DE PAGOS S.A.C.</b>", title_style))
    story.append(Paragraph("Comprobante Oficial de Liquidación de Pago", sub_style))
    story.append(Spacer(1, 15))

    data = [
        ["ID Transacción:", f"# {pago_id}"],
        ["Fecha de Emisión:", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
        ["Entidad / Beneficiario:", f"{proveedor}"],
        ["Concepto:", f"{concepto}"],
        ["Monto Pagado:", f"S/ {float(monto):,.2f}"],
        ["Fecha Vencimiento Original:", f"{fecha_venc}"],
        ["Cuenta de Destino:", CUENTA_EMPRESA],
        ["Medio de Pago Utilizado:", f"{metodo}"],
        ["N° Operación / Comprobante:", f"{comprobante}"],
    ]
    if evidencia:
        data.append(["Evidencia adjunta:", os.path.basename(evidencia)])
    data.append(["Estado Transacción:", "PAGADO / ACREDITADO EN SISTEMA"])

    t = Table(data, colWidths=[200, 280])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor("#EFF6FF")),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#BFDBFE")),
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('TEXTCOLOR', (0, 0), (-1, -1), colors.HexColor("#1E293B")),
        ('PADDING', (0, 0), (-1, -1), 8),
    ]))

    story.append(t)
    story.append(Spacer(1, 20))
    story.append(Paragraph("<font size=9 color=gray>Comprobante generado automáticamente por la Pasarela de Pagos (demostración).</font>", styles['Normal']))

    doc.build(story)
    return filename

def enviar_correo(pdf_path, correo_destino, proveedor, monto, comprobante):
    remitente_email = os.getenv("SMTP_USER", CORREO_NOTIFICACION)
    remitente_pass = os.getenv("SMTP_PASS", "abcd efgh ijkl mnop")

    if not correo_destino:
        return "⚠️ Sin correo de destino asignado."

    try:
        msg = MIMEMultipart()
        msg['From'] = remitente_email
        msg['To'] = correo_destino
        msg['Subject'] = f"🟢 NOTIFICACIÓN DE PAGO: {proveedor} - S/ {monto:,.2f}"

        body = f"Se ha registrado el pago exitosamente.\nEntidad: {proveedor}\nMonto: S/ {monto:,.2f}\nComprobante: {comprobante}"
        msg.attach(MIMEText(body, 'plain'))

        if os.path.exists(pdf_path):
            with open(pdf_path, "rb") as attachment:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(attachment.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename= {os.path.basename(pdf_path)}")
            msg.attach(part)

        if remitente_pass != "abcd efgh ijkl mnop":
            server = smtplib.SMTP("smtp.gmail.com", 587)
            server.starttls()
            server.login(remitente_email, remitente_pass)
            server.sendmail(remitente_email, correo_destino, msg.as_string())
            server.quit()
            return f"✅ Correo enviado con éxito a {correo_destino}"
        else:
            return (f"ℹ️ Correo en modo simulación (NO se envió a {correo_destino}): falta configurar SMTP_PASS "
                    f"(contraseña de aplicación de Gmail). La constancia PDF sí se generó.")
    except Exception as e:
        return f"❌ Error al enviar correo: {str(e)}"

def asegurar_comprobante(pago_id):
    """Devuelve la ruta del PDF de un pago ya liquidado; lo genera si todavía no existe."""
    ruta = ruta_comprobante(pago_id)
    if os.path.exists(ruta):
        return ruta
    cursor.execute(
        "SELECT id, proveedor, concepto, monto, fecha_vencimiento, metodo_pago, comprobante, evidencia "
        "FROM pagos WHERE id = ? AND estado = 'Pagado'", (pago_id,))
    r = cursor.fetchone()
    if not r:
        return None
    return generar_pdf_comprobante(r[0], r[1], r[2], r[3], r[4], r[5] or "-", r[6] or "-", r[7])

# ---------------------------------------------------------
# CONSULTAS
# ---------------------------------------------------------
COLS_MES = ['id', 'categoria', 'proveedor', 'concepto', 'monto', 'fecha_vencimiento', 'Estado']

def mes_actual_txt():
    hoy = date.today()
    return f"{MESES[hoy.month]} {hoy.year}"

def rango_mes_actual():
    hoy = date.today()
    inicio = hoy.replace(day=1)
    fin = date(inicio.year + (1 if inicio.month == 12 else 0), inicio.month % 12 + 1, 1)
    return str(inicio), str(fin)

def obtener_tabla_mes():
    """Obligaciones del mes actual, con su estado (pendiente con alerta, o PAGADO)."""
    inicio, fin = rango_mes_actual()
    df = pd.read_sql_query(
        "SELECT * FROM pagos WHERE fecha_vencimiento >= ? AND fecha_vencimiento < ? ORDER BY fecha_vencimiento, id",
        conn, params=(inicio, fin))
    if df.empty:
        return pd.DataFrame(columns=COLS_MES)

    def estado(row):
        if row['estado'] == 'Pagado':
            return '🟢 PAGADO'
        try:
            venc = datetime.strptime(row['fecha_vencimiento'], '%Y-%m-%d').date()
            dias = (venc - date.today()).days
            if dias < 0:
                return '🔴 VENCIDO'
            elif dias <= 7:
                return '🟡 PRÓXIMO'
            return '🔵 A TIEMPO'
        except Exception:
            return '⚪ PENDIENTE'

    df['Estado'] = df.apply(estado, axis=1)
    return df[COLS_MES]

def opciones_pendientes():
    """IDs pendientes del mes actual (servicios, impuestos, préstamos, proveedores, planilla)."""
    inicio, fin = rango_mes_actual()
    cursor.execute(
        "SELECT id, categoria, proveedor, concepto, monto FROM pagos "
        "WHERE estado = 'Pendiente' AND fecha_vencimiento >= ? AND fecha_vencimiento < ? "
        "ORDER BY fecha_vencimiento, id", (inicio, fin))
    return [(f"#{i} · {cat} · {prov} · {conc} · S/ {monto:.2f}", i)
            for i, cat, prov, conc, monto in cursor.fetchall()]

def opciones_todas():
    """Todas las obligaciones (pendientes y pagadas) para el editor del gerente."""
    cursor.execute("SELECT id, estado, categoria, proveedor, concepto, monto, fecha_vencimiento FROM pagos ORDER BY fecha_vencimiento, id")
    return [(f"#{i} · {est} · {cat} · {prov} · {conc} · S/ {monto:.2f} · vence {f}", i)
            for i, est, cat, prov, conc, monto, f in cursor.fetchall()]

def datos_pago(pago_id):
    if pago_id in (None, ""):
        return None
    try:
        pid = int(pago_id)
    except (TypeError, ValueError):
        return None
    cursor.execute(
        "SELECT id, proveedor, concepto, monto, fecha_vencimiento, categoria, estado, cuenta_destino FROM pagos WHERE id = ?",
        (pid,))
    return cursor.fetchone()

def cargar_registro(rol):
    """Registro Jul-Sep (con comprobantes PDF para descargar) y futuros pagos Nov-Ene."""
    cols = ['id', 'categoria', 'proveedor', 'concepto', 'monto', 'fecha_vencimiento', 'estado', 'metodo_pago', 'comprobante']
    vacio = pd.DataFrame(columns=cols + ['comprobante_pdf'])
    if not tiene_permiso(rol, "ver_registro"):
        return vacio, pd.DataFrame(columns=cols), None, None
    df = pd.read_sql_query(f"SELECT {', '.join(cols)} FROM pagos ORDER BY fecha_vencimiento, id", conn)
    pasado = df[(df['fecha_vencimiento'] >= REG_PASADO[0]) & (df['fecha_vencimiento'] <= REG_PASADO[1])].copy()
    futuro = df[(df['fecha_vencimiento'] >= REG_FUTURO[0]) & (df['fecha_vencimiento'] <= REG_FUTURO[1])].copy()

    archivos, nombres = [], []
    for _, r in pasado.iterrows():
        ruta = asegurar_comprobante(int(r['id'])) if r['estado'] == 'Pagado' else None
        if ruta:
            archivos.append(ruta)
            nombres.append(os.path.basename(ruta))
        else:
            nombres.append("—")
    pasado['comprobante_pdf'] = nombres
    return pasado, futuro, (archivos or None), None

def seleccionar_comprobante(rol, df, evt: gr.SelectData):
    """Al hacer clic en una fila del registro, muestra su comprobante para descargarlo."""
    if not tiene_permiso(rol, "ver_registro"):
        return None
    try:
        fila = getattr(evt, "row_value", None)
        if fila:
            pid = int(fila[0])
        else:
            idx = evt.index
            pid = int(df.iloc[idx[0] if isinstance(idx, (list, tuple)) else idx]['id'])
    except Exception:
        return None
    return asegurar_comprobante(pid)

def refrescar_tablas():
    t = obtener_tabla_mes()
    ops = gr.update(choices=opciones_pendientes(), value=None)
    return t, t, ops, ops

# ---------------------------------------------------------
# REGISTRO Y EDICIÓN DE OBLIGACIONES (SOLO GERENTE)
# ---------------------------------------------------------
def validar_obligacion(categoria, proveedor, concepto, monto, fecha):
    if not proveedor or not concepto or not str(proveedor).strip() or not str(concepto).strip():
        return "⚠️ Complete la entidad y el concepto."
    if str(monto).strip() not in MONTOS_PERMITIDOS:
        return "⚠️ El monto debe ser un número entero del 1 al 5 (sin símbolos)."
    try:
        datetime.strptime(str(fecha).strip(), "%Y-%m-%d")
    except ValueError:
        return "⚠️ La fecha debe tener el formato YYYY-MM-DD."
    if categoria not in CATEGORIAS:
        return "⚠️ Categoría inválida."
    return None

def registrar_pago(rol, categoria, proveedor, concepto, monto, fecha_venc):
    def salida(msg, ok=False):
        t = obtener_tabla_mes()
        if ok:
            ops = gr.update(choices=opciones_pendientes(), value=None)
            return msg, t, t, ops, ops
        return msg, t, t, gr.update(), gr.update()

    if not tiene_permiso(rol, "registrar"):
        return salida("⛔ No tiene permisos para registrar obligaciones.")

    error = validar_obligacion(categoria, proveedor, concepto, monto, fecha_venc)
    if error:
        return salida(error)

    fecha = str(fecha_venc).strip()
    cursor.execute(
        """INSERT INTO pagos (proveedor, concepto, monto, fecha_vencimiento, categoria, cuenta_destino, estado)
           VALUES (?, ?, ?, ?, ?, ?, 'Pendiente')""",
        (proveedor.strip(), concepto.strip(), float(int(str(monto).strip())), fecha, categoria, CUENTA_EMPRESA)
    )
    conn.commit()

    msg = "✅ Obligación registrada con éxito en el sistema."
    inicio, fin = rango_mes_actual()
    if not (inicio <= fecha < fin):
        msg += " ℹ️ Su vencimiento no es del mes actual: no aparece en esta tabla, pero sí en la lista de pagos."
    return salida(msg, ok=True)

def cargar_para_editar(rol, pago_id):
    # Salida: categoría, entidad, concepto, monto, fecha, info
    vacio = ("Servicios", "", "", "1", "", "Seleccione una obligación para editarla.")
    if not tiene_permiso(rol, "editar"):
        return vacio
    try:
        pid = int(pago_id)
    except (TypeError, ValueError):
        return vacio
    cursor.execute(
        "SELECT categoria, proveedor, concepto, monto, fecha_vencimiento, estado, metodo_pago, comprobante FROM pagos WHERE id = ?",
        (pid,))
    r = cursor.fetchone()
    if not r:
        return vacio
    cat, prov, con, monto, fecha, estado, metodo, comp = r
    monto_txt = str(int(monto)) if str(int(monto)) in MONTOS_PERMITIDOS else "1"
    info = f"Editando la obligación **#{pid}** · estado actual: **{estado}**"
    if estado == "Pagado":
        info += f" · método: {metodo} · operación: {comp}"
    return (cat if cat in CATEGORIAS else "Servicios", prov, con, monto_txt, fecha, info)

def guardar_edicion(rol, pago_id, categoria, proveedor, concepto, monto, fecha):
    # Salida: mensaje, tabla_a, tabla_g, dd_pago_a, dd_pago_g, dd_editar
    def salida(msg, ok=False, pid=None):
        t = obtener_tabla_mes()
        if ok:
            ops = gr.update(choices=opciones_pendientes(), value=None)
            return msg, t, t, ops, ops, gr.update(choices=opciones_todas(), value=pid)
        return msg, t, t, gr.update(), gr.update(), gr.update()

    if not tiene_permiso(rol, "editar"):
        return salida("⛔ No tiene permisos para editar servicios.")

    try:
        pid = int(pago_id)
    except (TypeError, ValueError):
        return salida("⚠️ Seleccione primero el servicio que desea editar.")

    cursor.execute("SELECT estado FROM pagos WHERE id = ?", (pid,))
    fila = cursor.fetchone()
    if not fila:
        return salida("⚠️ El servicio seleccionado ya no existe.")
    estado_actual = fila[0]

    error = validar_obligacion(categoria, proveedor, concepto, monto, fecha)
    if error:
        return salida(error)

    valores = (proveedor.strip(), concepto.strip(), float(int(str(monto).strip())), str(fecha).strip(), categoria)

    cursor.execute(
        "UPDATE pagos SET proveedor = ?, concepto = ?, monto = ?, fecha_vencimiento = ?, categoria = ? WHERE id = ?",
        valores + (pid,))
    conn.commit()

    if estado_actual == "Pagado":
        # El comprobante debe reflejar los datos editados
        cursor.execute(
            "SELECT id, proveedor, concepto, monto, fecha_vencimiento, metodo_pago, comprobante, evidencia FROM pagos WHERE id = ?",
            (pid,))
        r = cursor.fetchone()
        generar_pdf_comprobante(r[0], r[1], r[2], r[3], r[4], r[5] or "-", r[6] or "-", r[7])

    return salida(f"✅ Obligación #{pid} actualizada correctamente.", ok=True, pid=pid)

def refrescar_edicion(rol):
    if not tiene_permiso(rol, "editar"):
        return gr.update()
    return gr.update(choices=opciones_todas(), value=None)

# --- Eliminar obligación (con confirmación Sí / No) ---
def pedir_eliminar(rol, pago_id):
    # Salida: caja de confirmación, texto, id a eliminar (State), mensaje
    if not tiene_permiso(rol, "eliminar"):
        return gr.update(visible=False), "", None, "⛔ No tiene permisos para eliminar obligaciones."
    p = datos_pago(pago_id)
    if not p:
        return gr.update(visible=False), "", None, "⚠️ Seleccione primero la obligación que desea eliminar."
    pid, prov, conc, monto, vence, cat, estado, _ = p
    aviso = " Está **pagada**: también se borrará su comprobante." if estado == "Pagado" else ""
    texto = (f"### ⚠️ ¿Eliminar la obligación #{pid}?\n"
             f"**{prov}** · {conc} · S/ {monto:.2f} · vence {vence}.{aviso}\n\n"
             f"Esta acción no se puede deshacer.")
    return gr.update(visible=True), texto, pid, ""

def cancelar_eliminar():
    return gr.update(visible=False), None, "↩️ Eliminación cancelada: no se borró nada."

def ocultar_confirmacion():
    return gr.update(visible=False), None

def eliminar_obligacion(rol, pago_id):
    # Salida: mensaje, tabla_a, tabla_g, dd_pago_a, dd_pago_g, dd_editar, caja de confirmación, id (State)
    def salida(msg, ok=False):
        t = obtener_tabla_mes()
        oculto = gr.update(visible=False)
        if ok:
            ops = gr.update(choices=opciones_pendientes(), value=None)
            return msg, t, t, ops, ops, gr.update(choices=opciones_todas(), value=None), oculto, None
        return msg, t, t, gr.update(), gr.update(), gr.update(), oculto, None

    if not tiene_permiso(rol, "eliminar"):
        return salida("⛔ No tiene permisos para eliminar obligaciones.")
    try:
        pid = int(pago_id)
    except (TypeError, ValueError):
        return salida("⚠️ No hay ninguna eliminación pendiente de confirmar.")

    cursor.execute("SELECT evidencia FROM pagos WHERE id = ?", (pid,))
    fila = cursor.fetchone()
    if not fila:
        return salida("⚠️ La obligación ya no existe.")

    cursor.execute("DELETE FROM pagos WHERE id = ?", (pid,))
    conn.commit()
    for ruta in (ruta_comprobante(pid), fila[0]):
        if ruta and os.path.exists(ruta):
            os.remove(ruta)
    return salida(f"🗑️ Obligación #{pid} eliminada correctamente.", ok=True)

# ---------------------------------------------------------
# PASARELA DE PAGO (ASISTENTE Y GERENTE)
# ---------------------------------------------------------
def _vis(metodo):
    # billetera, tarjeta, evidencia (se limpia al cambiar), botones Enviar/Cancelar (ocultos)
    return (
        gr.update(visible=(metodo == "Billetera digital")),
        gr.update(visible=(metodo == "Tarjeta de débito")),
        gr.update(visible=(metodo == "Efectivo"), value=None),
        gr.update(visible=False),
    )

def vista_por_id(pago_id):
    """Al elegir un ID: muestra el detalle, restringe métodos y ajusta la pasarela."""
    p = datos_pago(pago_id)
    if not p or p[6] != "Pendiente":
        g1, g2, g3, g4 = _vis(None)
        return ("Seleccione una obligación para ver el detalle.",
                gr.update(choices=METODOS_TODOS, value=None),
                g1, g2, g3, g4,
                gr.update(value="Yapear"), gr.update(value="Pagar"))

    _, proveedor, concepto, monto, vence, categoria, _, _ = p
    es_servicio = (categoria == "Servicios")
    metodos = METODOS_SERVICIOS if es_servicio else METODOS_TODOS
    metodo = "Efectivo" if es_servicio else "Billetera digital"

    info = (f"**Entidad:** {proveedor} &nbsp;|&nbsp; **Concepto:** {concepto} &nbsp;|&nbsp; "
            f"**Categoría:** {categoria}\n\n"
            f"**Monto exacto a pagar:** S/ {monto:.2f} &nbsp;|&nbsp; **Vence:** {vence} &nbsp;|&nbsp; "
            f"**Cuenta destino:** `{CUENTA_EMPRESA}`")
    if es_servicio:
        info += ("\n\nℹ️ Los **servicios** solo se pagan en **efectivo**: suba la evidencia (imagen o PDF) "
                 "y confirme con **Enviar documento**.")

    g1, g2, g3, g4 = _vis(metodo)
    return (info,
            gr.update(choices=metodos, value=metodo),
            g1, g2, g3, g4,
            gr.update(value=f"Yapear S/ {monto:.2f}"),
            gr.update(value=f"Pagar S/ {monto:.2f}"))

def vista_por_metodo(metodo):
    return _vis(metodo)

def mostrar_acciones(archivo, metodo):
    """Enviar documento / Cancelar solo aparecen cuando hay un archivo cargado en efectivo."""
    return gr.update(visible=(archivo is not None and metodo == "Efectivo"))

def cancelar_envio():
    return gr.update(value=None), gr.update(visible=False), "↩️ Envío cancelado: no se registró ningún pago."

def guardar_evidencia(archivo, pago_id):
    ruta = archivo if isinstance(archivo, str) else getattr(archivo, "name", None)
    if not ruta or not os.path.exists(ruta):
        return None, "⚠️ Suba la evidencia del pago en efectivo (imagen o PDF)."
    ext = os.path.splitext(ruta)[1].lower()
    if ext not in EXT_EVIDENCIA:
        return None, "⚠️ Formato no permitido. Suba una imagen (PNG/JPG) o un PDF."
    if os.path.getsize(ruta) > MAX_EVIDENCIA_MB * 1024 * 1024:
        return None, f"⚠️ El archivo supera los {MAX_EVIDENCIA_MB} MB."
    destino = os.path.join(EVID_DIR, f"evidencia_pago_{pago_id}_{datetime.now():%Y%m%d%H%M%S}{ext}")
    shutil.copy(ruta, destino)
    return destino, None

# ---------------------------------------------------------
# CULQI: pasarela real en MODO PRUEBAS (billetera digital y tarjeta)
# ---------------------------------------------------------
# Las llaves se leen de variables de entorno (nunca se escriben en el código):
#   CULQI_PUBLIC_KEY = pk_test_...    CULQI_SECRET_KEY = sk_test_...
# Solo se aceptan llaves de INTEGRACIÓN (pruebas): con ellas no se cobra dinero real.
CULQI_URL_TOKENS = "https://secure.culqi.com/v2"
CULQI_URL_CARGOS = "https://api.culqi.com/v2"
CULQI_TIMEOUT = 25

class CulqiError(Exception):
    pass

def modo_culqi():
    """'pruebas' (llaves de integración), 'simulacion' (sin llaves) o 'bloqueado' (llaves inválidas o de producción)."""
    pk = os.getenv("CULQI_PUBLIC_KEY", "").strip()
    sk = os.getenv("CULQI_SECRET_KEY", "").strip()
    if not pk and not sk:
        return "simulacion"
    if pk.startswith("pk_test_") and sk.startswith("sk_test_"):
        return "pruebas"
    return "bloqueado"

def _culqi_post(base, ruta, llave, payload):
    req = urllib.request.Request(
        f"{base}{ruta}", data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {llave}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=CULQI_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8"))
            texto = err.get("user_message") or err.get("merchant_message") or err.get("code") or f"HTTP {e.code}"
        except Exception:
            texto = f"HTTP {e.code}"
        raise CulqiError(texto)
    except (urllib.error.URLError, TimeoutError, OSError):
        raise CulqiError("No se pudo conectar con Culqi (revise la conexión a internet).")
    except ValueError:
        raise CulqiError("Culqi devolvió una respuesta inválida.")

def _culqi_cargo(token_id, monto, descripcion, pid):
    cargo = _culqi_post(CULQI_URL_CARGOS, "/charges", os.getenv("CULQI_SECRET_KEY", "").strip(), {
        "amount": int(round(float(monto) * 100)),     # Culqi trabaja en céntimos
        "currency_code": "PEN",
        "email": CORREO_NOTIFICACION,
        "source_id": token_id,
        "description": descripcion[:80],
        "metadata": {"obligacion_id": str(pid)},
    })
    if cargo.get("object") == "error":
        raise CulqiError(cargo.get("user_message") or cargo.get("merchant_message") or "Cargo rechazado.")
    if cargo.get("object") != "charge" or not cargo.get("id"):
        raise CulqiError("Culqi no confirmó el cargo.")
    outcome = cargo.get("outcome") or {}
    if outcome.get("type") and outcome.get("type") != "venta_exitosa":
        raise CulqiError(outcome.get("user_message") or "El cargo no fue aprobado.")
    return cargo["id"]

def culqi_cobrar_yape(celular, otp, monto, descripcion, pid):
    tok = _culqi_post(CULQI_URL_TOKENS, "/tokens/yape", os.getenv("CULQI_PUBLIC_KEY", "").strip(), {
        "amount": int(round(float(monto) * 100)),
        "otp": otp,
        "number_phone": celular,
    })
    if tok.get("object") == "error" or not tok.get("id"):
        raise CulqiError(tok.get("user_message") or tok.get("merchant_message") or "Culqi no aceptó el pago con billetera digital.")
    return _culqi_cargo(tok["id"], monto, descripcion, pid)

def culqi_cobrar_tarjeta(numero, cvv, mes, anio, monto, descripcion, pid):
    tok = _culqi_post(CULQI_URL_TOKENS, "/tokens", os.getenv("CULQI_PUBLIC_KEY", "").strip(), {
        "card_number": numero,
        "cvv": cvv,
        "expiration_month": mes,
        "expiration_year": anio,
        "email": CORREO_NOTIFICACION,
    })
    if tok.get("object") == "error" or not tok.get("id"):
        raise CulqiError(tok.get("user_message") or tok.get("merchant_message") or "Culqi no aceptó la tarjeta.")
    return _culqi_cargo(tok["id"], monto, descripcion, pid)

def banner_pasarela():
    modo = modo_culqi()
    if modo == "pruebas":
        return ("🧪 **Pasarela Culqi activa (ambiente de pruebas):** los pagos con billetera digital y tarjeta se procesan "
                "en Culqi, sin cobrar dinero real.\n\n"
                "Datos de prueba → **Yape:** celular `900000001` y cualquier código de 6 dígitos · "
                "**Tarjeta:** `4111 1111 1111 1111`, vence `09/30`, CVV `123`.")
    if modo == "bloqueado":
        return ("⛔ **Llaves de Culqi no válidas.** Esta demostración solo acepta llaves de prueba "
                "(`pk_test_…` y `sk_test_…`). Los pagos con billetera y tarjeta están deshabilitados.")
    return ("ℹ️ **Modo simulación:** los pagos con billetera digital y tarjeta no se envían a ningún banco. "
            "Para usar la pasarela real de pruebas, defina `CULQI_PUBLIC_KEY` y `CULQI_SECRET_KEY`.")

def procesar_pago(rol, pago_id, metodo, cel, cod, tit, num, exp, cvv, evidencia):
    # Salida: status, pdf, tabla_a, tabla_g, dd_a, dd_g, cel, cod, tit, num, exp, cvv, evidencia
    def fallo(msg, limpiar_evidencia=False):
        t = obtener_tabla_mes()
        ev = gr.update(value=None) if limpiar_evidencia else gr.update()
        return (msg, None, t, t, gr.update(), gr.update(),
                gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), ev)

    if not tiene_permiso(rol, "pagar"):
        return fallo("⛔ No tiene permisos para ejecutar pagos.")

    p = datos_pago(pago_id)
    if not p:
        return fallo("⚠️ Seleccione primero una obligación de la lista.")

    pid, proveedor, concepto, monto, vence, categoria, estado, _ = p
    if estado == "Pagado":
        return fallo(f"ℹ️ El pago #{pid} ya se encuentra liquidado.", limpiar_evidencia=True)

    inicio, fin = rango_mes_actual()
    if not (inicio <= vence < fin):
        return fallo(f"⚠️ Solo se pueden pagar obligaciones del mes actual ({mes_actual_txt()}).")

    if metodo not in METODOS_TODOS:
        return fallo("⚠️ Seleccione un método de pago.")

    if categoria == "Servicios" and metodo != "Efectivo":
        return fallo("🔴 PAGO RECHAZADO: los servicios solo se pueden pagar en efectivo.")

    detalle_metodo = metodo
    evidencia_ruta = None
    tarjeta = None
    origen = "Simulación (no se envió a ningún banco)"

    if metodo == "Billetera digital":
        if not re.fullmatch(r"9\d{8}", (cel or "").strip()):
            return fallo("⚠️ Ingrese un celular válido de 9 dígitos (empieza con 9).")
        if not re.fullmatch(r"\d{6}", (cod or "").strip()):
            return fallo("⚠️ El código de aprobación debe tener 6 dígitos.")

    elif metodo == "Tarjeta de débito":
        if len((tit or "").strip()) < 3:
            return fallo("⚠️ Ingrese el titular de la tarjeta.")
        digitos = re.sub(r"[\s-]", "", num or "")
        if not re.fullmatch(r"\d{13,19}", digitos):
            return fallo("⚠️ Número de tarjeta inválido.")
        m = re.fullmatch(r"(0[1-9]|1[0-2])/(\d{2})", (exp or "").strip())
        if not m:
            return fallo("⚠️ La fecha de expiración debe tener el formato MM/AA.")
        hoy = date.today()
        if (2000 + int(m.group(2)), int(m.group(1))) < (hoy.year, hoy.month):
            return fallo("🔴 PAGO RECHAZADO: la tarjeta está vencida.")
        if not re.fullmatch(r"\d{3,4}", (cvv or "").strip()):
            return fallo("⚠️ CVV inválido.")
        tarjeta = (digitos, cvv.strip(), m.group(1), str(2000 + int(m.group(2))))

    else:  # Efectivo
        evidencia_ruta, error = guardar_evidencia(evidencia, pid)
        if error:
            return fallo(error, limpiar_evidencia=True)
        origen = "Pago en efectivo con evidencia adjunta"

    # Billetera digital y tarjeta: cobro en Culqi (ambiente de pruebas)
    nro_op = None
    if metodo in ("Billetera digital", "Tarjeta de débito"):
        modo = modo_culqi()
        if modo == "bloqueado":
            return fallo("⛔ Llaves de Culqi no válidas: esta demostración solo acepta llaves de prueba "
                         "(pk_test_… y sk_test_…). Revise CULQI_PUBLIC_KEY y CULQI_SECRET_KEY.")
        if modo == "pruebas":
            descripcion = f"Pago #{pid} {proveedor} - {concepto}"
            try:
                if metodo == "Billetera digital":
                    nro_op = culqi_cobrar_yape(cel.strip(), cod.strip(), monto, descripcion, pid)
                else:
                    nro_op = culqi_cobrar_tarjeta(*tarjeta, monto, descripcion, pid)
            except CulqiError as e:
                return fallo(f"🔴 PAGO RECHAZADO por Culqi: {e}")
            origen = "Culqi (ambiente de pruebas: no se cobró dinero real)"

    if not nro_op:
        prefijo = {"Billetera digital": "BIL", "Tarjeta de débito": "TAR", "Efectivo": "EFE"}[metodo]
        nro_op = f"{prefijo}-{random.randint(100000, 999999)}"

    # Nunca se guardan datos de tarjeta, celular ni códigos: solo el resultado del pago
    cursor.execute(
        "UPDATE pagos SET estado = 'Pagado', comprobante = ?, metodo_pago = ?, evidencia = ?, cuenta_destino = ? WHERE id = ?",
        (nro_op, detalle_metodo, evidencia_ruta, CUENTA_EMPRESA, pid))
    conn.commit()

    pdf_path = generar_pdf_comprobante(pid, proveedor, concepto, monto, vence, detalle_metodo, nro_op, evidencia_ruta)
    res_email = enviar_correo(pdf_path, CORREO_NOTIFICACION, proveedor, monto, nro_op)

    msg = (
        f"🟢 ¡PAGO APROBADO CON ÉXITO!\n"
        f"• Estado del servicio: PAGADO\n"
        f"• Beneficiario: {proveedor}\n"
        f"• Cuenta destino: {CUENTA_EMPRESA}\n"
        f"• Monto: S/ {monto:,.2f}\n"
        f"• Método: {detalle_metodo}\n"
        f"• Procesado por: {origen}\n"
        f"• N° Operación: {nro_op}\n"
        f"• {res_email}"
    )
    t = obtener_tabla_mes()
    ops = gr.update(choices=opciones_pendientes(), value=None)
    return (msg, pdf_path, t, t, ops, ops, "", "", "", "", "", "", None)

# ---------------------------------------------------------
# AUTENTICACIÓN
# ---------------------------------------------------------
USUARIOS = {
    "gerente": "admin123",
    "asistente": "pago123"
}

def iniciar_sesion_internal(user, pwd):
    # Salidas: box_login, box_asistente, box_gerente, lbl_usuario, msg_login,
    #          rol_state, tabla_a, tabla_g, dd_a, dd_g
    if user in USUARIOS and USUARIOS[user] == pwd:
        pend = obtener_tabla_mes()
        ops = gr.update(choices=opciones_pendientes(), value=None)
        if user == "asistente":
            return (gr.update(visible=False), gr.update(visible=True), gr.update(visible=False),
                    f"👤 Sesión Activa: ASISTENTE ({user}) — solo puede realizar pagos",
                    "", "asistente", pend, pend, ops, ops)
        return (gr.update(visible=False), gr.update(visible=False), gr.update(visible=True),
                f"👤 Sesión Activa: GERENTE / ADMINISTRADOR ({user})",
                "", "gerente", pend, pend, ops, ops)
    return (gr.update(visible=True), gr.update(visible=False), gr.update(visible=False),
            "", "❌ Credenciales incorrectas. Verifique usuario y contraseña.", "",
            gr.update(), gr.update(), gr.update(), gr.update())

def cerrar_sesion_internal():
    return (gr.update(visible=True), gr.update(visible=False), gr.update(visible=False),
            "", "🔒 Sesión cerrada con éxito. Ingrese nuevamente.", "",
            gr.update(), gr.update(), gr.update(), gr.update())

# ---------------------------------------------------------
# ESTILOS Y ESTRUCTURA DE LA INTERFAZ
# ---------------------------------------------------------
custom_theme = gr.themes.Soft(
    primary_hue="blue",
    secondary_hue="emerald",
    neutral_hue="slate"
).set(
    button_primary_background_fill="linear-gradient(90deg, #2563EB, #1D4ED8)",
    button_primary_background_fill_hover="linear-gradient(90deg, #1D4ED8, #1E40AF)",
    button_primary_text_color="white",
    block_title_text_color="#0F172A",
    block_label_background_fill="#F8FAFC"
)

def construir_pasarela():
    """Pasarela de pago reutilizable (asistente y gerente)."""
    c = {}
    gr.Markdown("### ⚡ Pasarela de Pago")
    gr.Markdown(f"Todos los pagos se acreditan en la cuenta de la empresa: **{CUENTA_EMPRESA}**")
    gr.Markdown(banner_pasarela())

    c["dd"] = gr.Dropdown(choices=opciones_pendientes(), value=None,
                          label="1. ID de la Obligación a Pagar (todas las categorías)")
    c["info"] = gr.Markdown("Seleccione una obligación para ver el detalle.")

    with gr.Row():
        c["metodo"] = gr.Radio(METODOS_TODOS, value=None, label="2. Método de Pago")
        with gr.Column():
            c["evidencia"] = gr.File(label="Subir evidencia del pago en efectivo: imagen o PDF",
                                     file_types=list(EXT_EVIDENCIA), visible=False)
            with gr.Row(visible=False) as acciones:
                c["btn_enviar"] = gr.Button("📤 Enviar documento", variant="primary")
                c["btn_cancelar"] = gr.Button("✖ Cancelar", variant="stop")
            c["acciones"] = acciones

    # --- Billetera digital (estilo Yape) ---
    with gr.Column(visible=False) as g_bil:
        gr.Markdown("#### 📱 Pago con billetera digital")
        c["cel"] = gr.Textbox(label="Ingresa tu celular Yape", placeholder="9XXXXXXXX")
        c["cod"] = gr.Textbox(label="Ingresa el código de aprobación",
                              info='Encuéntralo en tu Yape, opción "Aprobar compras"', placeholder="6 dígitos")
        c["btn_bil"] = gr.Button("Yapear", variant="primary")
    c["g_bil"] = g_bil

    # --- Tarjeta de débito ---
    with gr.Column(visible=False) as g_tar:
        gr.Markdown("#### 💳 Pago con tarjeta de débito")
        c["tit"] = gr.Textbox(label="Titular de la tarjeta *", placeholder="TITULAR")
        c["num"] = gr.Textbox(label="Numero de tarjeta *", placeholder="Número de Tarjeta")
        with gr.Row():
            c["exp"] = gr.Textbox(label="Fecha de expiracion *", placeholder="MM/AA")
            c["cvv"] = gr.Textbox(label="CVV *", placeholder="CVV", type="password")
        c["btn_tar"] = gr.Button("Pagar", variant="primary")
    c["g_tar"] = g_tar

    gr.Markdown(f"📧 Las notificaciones de pago se envían a: **{CORREO_NOTIFICACION}**")

    with gr.Row():
        c["status"] = gr.Textbox(label="Respuesta y Resultado de la Operación", lines=8)
        c["pdf"] = gr.File(label="📥 Descargar Constancia PDF Oficial")
    return c

with gr.Blocks(theme=custom_theme, title="Sistema de Gestión de Pagos") as demo:

    rol_state = gr.State("")
    id_eliminar = gr.State(None)   # obligación pendiente de confirmar para eliminar

    gr.HTML("""
        <div style='background: linear-gradient(90deg, #0F172A, #1E3A8A, #2563EB); padding: 22px; border-radius: 12px; color: white; text-align: center; margin-bottom: 15px;'>
            <h1 style='margin: 0; font-size: 28px; font-weight: 700; color: #FFFFFF;'>💳 Sistema de Control y Pasarela de Pagos</h1>
            <p style='margin: 6px 0 0 0; opacity: 0.9; font-size: 15px;'>Módulo Operativo de Liquidación con Validación en Tiempo Real</p>
        </div>
    """)

    with gr.Row():
        lbl_usuario_actual = gr.Markdown("")

    # ---------------- LOGIN ----------------
    with gr.Column(visible=True) as box_login:
        gr.Markdown("## 🔑 Iniciar Sesión en el Sistema")
        txt_user = gr.Textbox(label="Usuario", placeholder="Ej: gerente o asistente", value="asistente")
        txt_pass = gr.Textbox(label="Contraseña", type="password", value="pago123")
        btn_login = gr.Button("🚀 Ingresar al Sistema", variant="primary")
        out_login_msg = gr.Textbox(label="Estado de Acceso", interactive=False)

        gr.HTML("""
        <div style='background-color: #F1F5F9; border-left: 5px solid #64748B; padding: 10px; border-radius: 6px; margin-top: 15px;'>
            <strong>Credenciales de Prueba:</strong><br>
            • <b>Asistente (solo paga):</b> usuario: <code>asistente</code> | clave: <code>pago123</code><br>
            • <b>Gerente (administrador):</b> usuario: <code>gerente</code> | clave: <code>admin123</code>
        </div>
        """)

    # ---------------- PANEL ASISTENTE: SOLO PAGAR ----------------
    with gr.Column(visible=False) as box_asistente:
        gr.Markdown("## 📋 Panel de Asistente — Realizar Pagos")
        btn_logout_asistente = gr.Button("🔴 Cerrar Sesión", variant="stop")

        gr.Markdown(f"### 📅 Obligaciones del mes actual ({mes_actual_txt()})")
        tabla_pend_a = gr.DataFrame(value=obtener_tabla_mes(), label="Obligaciones y su estado", interactive=False)
        btn_refrescar_a = gr.Button("🔄 Actualizar Tabla", variant="secondary")

        gr.Markdown("---")
        PA = construir_pasarela()

    # ---------------- PANEL GERENTE: TODO ----------------
    with gr.Column(visible=False) as box_gerente:
        gr.Markdown("## 📊 Panel de Gerencia / Administrador")
        btn_logout_gerente = gr.Button("🔴 Cerrar Sesión", variant="stop")

        with gr.Tabs():
            with gr.Tab("➕ Registrar Obligaciones"):
                with gr.Row():
                    with gr.Column(scale=1):
                        gr.Markdown("### Registrar Nueva Obligación")
                        cat = gr.Dropdown(CATEGORIAS, label="Categoría", value="Servicios")
                        prov = gr.Textbox(label="Entidad / Beneficiario", placeholder="Ej: Luz del Sur")
                        con = gr.Textbox(label="Concepto / N° Recibo", placeholder="Ej: Recibo N° 8812")
                        mon = gr.Dropdown(MONTOS_PERMITIDOS, value="1",
                                          label="Monto Deuda Exacta (S/) — solo del 1 al 5")
                        f_venc = gr.Textbox(label="Fecha Vencimiento (YYYY-MM-DD)", value=str(date.today()))
                        gr.Markdown(f"Cuenta de destino (fija para todos los pagos): **{CUENTA_EMPRESA}**")
                        btn_reg = gr.Button("💾 Guardar Registro", variant="primary")
                        out_reg = gr.Textbox(label="Estado del Registro")
                    with gr.Column(scale=2):
                        gr.Markdown(f"### 📅 Obligaciones del mes actual ({mes_actual_txt()})")
                        tabla_pend_g = gr.DataFrame(value=obtener_tabla_mes(), label="Obligaciones y su estado", interactive=False)
                        btn_refrescar_g = gr.Button("🔄 Actualizar Tabla", variant="secondary")

            with gr.Tab("✏️ Editar Obligación") as tab_editar:
                gr.Markdown("### Editar o eliminar una obligación (monto, fecha, entidad, categoría…)")
                dd_edit = gr.Dropdown(choices=opciones_todas(), value=None, label="Seleccione la obligación")
                ed_info = gr.Markdown("Seleccione una obligación para editarla.")
                with gr.Row():
                    ed_cat = gr.Dropdown(CATEGORIAS, label="Categoría", value="Servicios")
                    ed_mon = gr.Dropdown(MONTOS_PERMITIDOS, value="1", label="Monto Deuda Exacta (S/) — solo del 1 al 5")
                with gr.Row():
                    ed_prov = gr.Textbox(label="Entidad / Beneficiario")
                    ed_con = gr.Textbox(label="Concepto / N° Recibo")
                ed_fecha = gr.Textbox(label="Fecha Vencimiento (YYYY-MM-DD)")
                with gr.Row():
                    btn_guardar_edit = gr.Button("💾 Guardar cambios", variant="primary")
                    btn_eliminar = gr.Button("🗑️ Eliminar obligación", variant="stop")
                with gr.Column(visible=False) as caja_confirmar:
                    txt_confirmar = gr.Markdown("")
                    with gr.Row():
                        btn_si = gr.Button("✅ Sí, eliminar", variant="stop")
                        btn_no = gr.Button("❌ No", variant="secondary")
                out_edit = gr.Textbox(label="Resultado")

            with gr.Tab("⚡ Realizar Pagos"):
                PG = construir_pasarela()

            with gr.Tab("📅 Registro y Futuros Pagos") as tab_registro:
                gr.Markdown("### 🗂️ Registro de los primeros 3 meses: Julio, Agosto y Septiembre 2026")
                with gr.Row():
                    with gr.Column(scale=3):
                        tabla_reg_pasado = gr.DataFrame(
                            label="Julio – Septiembre 2026 (haga clic en una fila para descargar su comprobante)",
                            interactive=False)
                    with gr.Column(scale=1):
                        file_sel = gr.File(label="📥 Comprobante de la fila seleccionada", interactive=False)
                        file_todos = gr.File(label="📦 Todos los comprobantes (Jul – Sep)",
                                             file_count="multiple", interactive=False)
                gr.Markdown("### 🔮 Futuros pagos de los 3 meses siguientes: Noviembre, Diciembre 2026 y Enero 2027")
                tabla_reg_futuro = gr.DataFrame(label="Noviembre 2026 – Enero 2027", interactive=False)
                btn_reg_cargar = gr.Button("🔄 Actualizar Registro", variant="secondary")

    # ---------------- EVENTOS ----------------
    salidas_sesion = [box_login, box_asistente, box_gerente, lbl_usuario_actual, out_login_msg,
                      rol_state, tabla_pend_a, tabla_pend_g, PA["dd"], PG["dd"]]

    btn_login.click(iniciar_sesion_internal, inputs=[txt_user, txt_pass], outputs=salidas_sesion)
    for b in [btn_logout_asistente, btn_logout_gerente]:
        b.click(cerrar_sesion_internal, outputs=salidas_sesion)

    # Registro de obligaciones (solo gerente, validado también en el servidor)
    btn_reg.click(
        registrar_pago,
        inputs=[rol_state, cat, prov, con, mon, f_venc],
        outputs=[out_reg, tabla_pend_a, tabla_pend_g, PA["dd"], PG["dd"]]
    )

    # Edición de obligaciones (solo gerente)
    dd_edit.change(
        cargar_para_editar, inputs=[rol_state, dd_edit],
        outputs=[ed_cat, ed_prov, ed_con, ed_mon, ed_fecha, ed_info]
    )
    dd_edit.change(ocultar_confirmacion, outputs=[caja_confirmar, id_eliminar])
    btn_guardar_edit.click(
        guardar_edicion,
        inputs=[rol_state, dd_edit, ed_cat, ed_prov, ed_con, ed_mon, ed_fecha],
        outputs=[out_edit, tabla_pend_a, tabla_pend_g, PA["dd"], PG["dd"], dd_edit]
    )
    # Eliminar: botón -> confirmación Sí / No
    btn_eliminar.click(pedir_eliminar, inputs=[rol_state, dd_edit],
                       outputs=[caja_confirmar, txt_confirmar, id_eliminar, out_edit])
    btn_no.click(cancelar_eliminar, outputs=[caja_confirmar, id_eliminar, out_edit])
    btn_si.click(
        eliminar_obligacion, inputs=[rol_state, id_eliminar],
        outputs=[out_edit, tabla_pend_a, tabla_pend_g, PA["dd"], PG["dd"], dd_edit, caja_confirmar, id_eliminar]
    )
    tab_editar.select(refrescar_edicion, inputs=[rol_state], outputs=[dd_edit])

    # Refrescar tablas del mes
    refrescar_out = [tabla_pend_a, tabla_pend_g, PA["dd"], PG["dd"]]
    btn_refrescar_a.click(refrescar_tablas, outputs=refrescar_out)
    btn_refrescar_g.click(refrescar_tablas, outputs=refrescar_out)

    # Registro / futuros pagos / comprobantes
    salidas_registro = [tabla_reg_pasado, tabla_reg_futuro, file_todos, file_sel]
    tab_registro.select(cargar_registro, inputs=[rol_state], outputs=salidas_registro)
    btn_reg_cargar.click(cargar_registro, inputs=[rol_state], outputs=salidas_registro)
    tabla_reg_pasado.select(seleccionar_comprobante, inputs=[rol_state, tabla_reg_pasado], outputs=[file_sel])

    # Pasarela (misma lógica para asistente y gerente)
    def conectar_pasarela(P):
        P["dd"].change(
            vista_por_id, inputs=[P["dd"]],
            outputs=[P["info"], P["metodo"], P["g_bil"], P["g_tar"], P["evidencia"], P["acciones"],
                     P["btn_bil"], P["btn_tar"]]
        )
        P["metodo"].change(
            vista_por_metodo, inputs=[P["metodo"]],
            outputs=[P["g_bil"], P["g_tar"], P["evidencia"], P["acciones"]]
        )
        # Efectivo: al cargar el archivo aparecen "Enviar documento" y "Cancelar"
        P["evidencia"].change(mostrar_acciones, inputs=[P["evidencia"], P["metodo"]], outputs=[P["acciones"]])
        P["btn_cancelar"].click(cancelar_envio, outputs=[P["evidencia"], P["acciones"], P["status"]])

        entradas = [rol_state, P["dd"], P["metodo"], P["cel"], P["cod"], P["tit"], P["num"],
                    P["exp"], P["cvv"], P["evidencia"]]
        salidas = [P["status"], P["pdf"], tabla_pend_a, tabla_pend_g, PA["dd"], PG["dd"],
                   P["cel"], P["cod"], P["tit"], P["num"], P["exp"], P["cvv"], P["evidencia"]]
        P["btn_bil"].click(procesar_pago, inputs=entradas, outputs=salidas)
        P["btn_tar"].click(procesar_pago, inputs=entradas, outputs=salidas)
        P["btn_enviar"].click(procesar_pago, inputs=entradas, outputs=salidas)

    conectar_pasarela(PA)
    conectar_pasarela(PG)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    demo.launch(
        server_name="0.0.0.0",
        server_port=port,
        allowed_paths=[os.path.abspath(COMP_DIR), os.path.abspath(EVID_DIR)]
    )
         
    
