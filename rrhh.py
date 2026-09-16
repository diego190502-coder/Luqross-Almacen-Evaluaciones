from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List, Dict
import json, os, io
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor

app = FastAPI(title="LUQROSS | RRHH")

for d in ["static", "static/fotos", "static/incidencias"]:
    os.makedirs(d, exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")

FONT_FILE  = "Strasua.ttf"
LOGO_FILE  = "logo.png"

ESCALA = {0:"Crítico",1:"Insuficiente",2:"Necesita mejora",3:"En observación",4:"Satisfactorio",5:"Excelente"}
TIPOS_INC = ["Falta","Retardo","Permiso","Accidente","Conducta","Material Sucio","Material Incompleto","Mal Etiquetado","Otro"]
TIPOS_PUESTO = ["ALMACENISTA","CHOFER","BECARIA","Otro"]
ACTIVIDADES_HORAS = ["Hora de Conocimiento de Ruta","Hora de Entrega de Etiquetas",
                     "Hora de Término de Preparación","Hora de Entrega de Papeles",
                     "Hora de Salida","Hora de Llegada (Comida)"]
META_LOCAL = 180
META_PAQ   = 300
# Puntos porcentuales que se restan del promedio global de un colaborador por
# cada incidencia registrada en el mes que se está viendo/exportando.
PENALIZACION_POR_INCIDENCIA_PCT = 1.0

# ── Base de datos PostgreSQL ───────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")

def get_conn():
    url = DATABASE_URL
    # Railway requiere SSL
    if "sslmode" not in url:
        url += "?sslmode=require"
    return psycopg2.connect(url, cursor_factory=RealDictCursor)

def init_db():
    """Crear tablas si no existen"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS colaboradores (
            nombre TEXT PRIMARY KEY,
            puesto TEXT,
            actividades JSONB,
            activo BOOLEAN DEFAULT TRUE
        );
        CREATE TABLE IF NOT EXISTS evaluaciones (
            id SERIAL PRIMARY KEY,
            colaborador TEXT,
            anio INT,
            mes INT,
            dia INT,
            calificaciones JSONB,
            promedio FLOAT,
            pct FLOAT,
            guardado TEXT,
            UNIQUE(colaborador, anio, mes, dia)
        );
        CREATE TABLE IF NOT EXISTS incidencias (
            id SERIAL PRIMARY KEY,
            fecha TEXT,
            colaborador TEXT,
            tipo TEXT,
            responsable TEXT,
            ingresado_por TEXT,
            observaciones TEXT,
            estatus TEXT DEFAULT 'Abierta',
            solucion TEXT,
            resuelto_por TEXT,
            fecha_resolucion TEXT,
            foto TEXT
        );
        CREATE TABLE IF NOT EXISTS horas (
            id SERIAL PRIMARY KEY,
            colaborador TEXT,
            fecha TEXT,
            tipo_ruta TEXT,
            destino TEXT,
            horas JSONB,
            minutos_prep INT,
            minutos_ruta INT,
            meta_prep INT,
            efic_prep FLOAT,
            nivel_prep TEXT,
            cumplimientos JSONB,
            guardado TEXT,
            UNIQUE(colaborador, fecha)
        );
        CREATE TABLE IF NOT EXISTS tiempos (
            id SERIAL PRIMARY KEY,
            fecha TEXT,
            colaborador TEXT,
            tipo_ruta TEXT,
            modelo_tarea TEXT,
            minutos INT,
            meta_minutos INT,
            eficiencia FLOAT,
            nivel TEXT,
            observaciones TEXT
        );
    """)
    # Migración: agrega la columna 'activo' si la tabla ya existía de antes
    # (bases de datos creadas antes de este cambio no la tienen).
    cur.execute("ALTER TABLE colaboradores ADD COLUMN IF NOT EXISTS activo BOOLEAN DEFAULT TRUE;")
    cur.execute("UPDATE colaboradores SET activo=TRUE WHERE activo IS NULL;")
    conn.commit()
    cur.close()
    conn.close()

# Inicializar BD al arrancar
try:
    init_db()
except Exception as ex:
    print(f"⚠ Error iniciando BD: {ex}")

# ── Helpers BD ─────────────────────────────────────────────────────────────
def db_fetch(sql, params=()):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [dict(r) for r in rows]

def db_exec(sql, params=()):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(sql, params)
    result = None
    try: result = cur.fetchone()
    except: pass
    conn.commit()
    cur.close(); conn.close()
    return dict(result) if result else None

def parse_lista_json(raw):
    """Devuelve una lista a partir de un campo guardado como lista JSON
    (ej. '["ANA","LUIS"]') o, si es un texto plano de un solo valor guardado
    antes de este cambio, lo envuelve en una lista de un elemento, para no
    romper datos ya guardados."""
    if not raw:
        return []
    try:
        v = json.loads(raw)
        if isinstance(v, list):
            return [str(x) for x in v if str(x).strip()]
    except Exception:
        pass
    return [raw]

def parse_colabs(raw):
    """Devuelve la lista de colaboradores involucrados en una incidencia."""
    return parse_lista_json(raw)

def js_str(s):
    """Escapa un texto para insertarlo de forma segura dentro de comillas
    simples en JS embebido en el HTML (evita romper el onclick si el nombre
    trae un apóstrofe, comilla o backslash)."""
    return str(s).replace("\\", "\\\\").replace("'", "\\'")

def fecha_anio_mes(fecha_str):
    """Extrae (anio, mes) del texto de fecha 'DD/MM/YYYY HH:MM' que se usa
    en incidencias. Devuelve (None, None) si no se puede interpretar."""
    try:
        dt = datetime.strptime((fecha_str or "").strip()[:10], "%d/%m/%Y")
        return dt.year, dt.month
    except Exception:
        return None, None

# ── Modelos ────────────────────────────────────────────────────────────────
class Colaborador(BaseModel):
    nombre: str
    puestos: List[str]
    puesto_personalizado: Optional[str] = ""
    actividades: List[str]

class EvalDia(BaseModel):
    colaborador: str
    anio: int
    mes: int
    dia: int
    calificaciones: Dict[str, int]

class Incidencia(BaseModel):
    colaboradores: List[str]
    tipo: str
    tipo_personalizado: Optional[str] = ""
    responsable: str
    ingresado_por: Optional[str] = ""
    observaciones: Optional[str] = ""

class RegistroHoras(BaseModel):
    colaborador: str
    fecha: str
    tipo_ruta: str
    destino: Optional[str] = ""
    horas: Dict[str, str]

class Tiempo(BaseModel):
    colaborador: str
    tipo_ruta: str
    modelo_tarea: str
    minutos: int
    observaciones: Optional[str] = ""

# ── API colaboradores ──────────────────────────────────────────────────────
@app.post("/api/colaborador")
def add_colab(c: Colaborador):
    puestos = [p.strip() for p in c.puestos if p and p.strip()]
    if "Otro" in puestos:
        puestos = [p for p in puestos if p != "Otro"]
        extra = (c.puesto_personalizado or "").strip()
        if extra:
            puestos.append(extra)
    if not puestos:
        raise HTTPException(400, "Selecciona al menos un tipo de puesto.")
    try:
        # INSERT ... ON CONFLICT: si el nombre ya existía (por ejemplo,
        # estaba desactivado), lo reactiva y actualiza sus datos en vez de
        # fallar por el nombre duplicado.
        db_exec("""INSERT INTO colaboradores (nombre,puesto,actividades,activo)
                   VALUES (%s,%s,%s,TRUE)
                   ON CONFLICT (nombre) DO UPDATE
                   SET puesto=EXCLUDED.puesto, actividades=EXCLUDED.actividades, activo=TRUE""",
                (c.nombre.strip(), json.dumps(puestos), json.dumps(c.actividades)))
        return {"ok":True}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.delete("/api/colaborador/{nombre}")
def del_colab(nombre:str):
    """Desactiva al colaborador: deja de aparecer en las listas para
    registrar evaluaciones/incidencias/horas nuevas, pero su historial ya
    guardado se conserva tal cual."""
    db_exec("UPDATE colaboradores SET activo=FALSE WHERE nombre=%s", (nombre.strip(),))
    return {"ok":True}

@app.patch("/api/colaborador/{nombre}/activar")
def reactivar_colab(nombre:str):
    db_exec("UPDATE colaboradores SET activo=TRUE WHERE nombre=%s", (nombre.strip(),))
    return {"ok":True}

@app.post("/api/colaborador/{nombre}/foto")
async def subir_foto(nombre:str, file:UploadFile=File(...)):
    path=f"static/fotos/{nombre}.jpg"
    with open(path,"wb") as b: b.write(await file.read())
    return {"ok":True}

@app.put("/api/colaborador/{nombre}/actividades")
def update_actividades(nombre:str, body:dict):
    db_exec("UPDATE colaboradores SET actividades=%s WHERE nombre=%s",
            (json.dumps(body.get("actividades",[])), nombre))
    return {"ok":True}

# ── API evaluaciones diarias ──────────────────────────────────────────────
@app.post("/api/evaluacion")
def guardar_eval(e: EvalDia):
    total_vals = list(e.calificaciones.values())
    promedio = round(sum(total_vals)/len(total_vals),2) if total_vals else 0
    pct = round((promedio/5)*100,1)
    db_exec("""INSERT INTO evaluaciones (colaborador,anio,mes,dia,calificaciones,promedio,pct,guardado)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (colaborador,anio,mes,dia)
               DO UPDATE SET calificaciones=%s, promedio=%s, pct=%s, guardado=%s""",
            (e.colaborador, e.anio, e.mes, e.dia, json.dumps(e.calificaciones), promedio, pct,
             datetime.now().strftime("%d/%m/%Y %H:%M"),
             json.dumps(e.calificaciones), promedio, pct, datetime.now().strftime("%d/%m/%Y %H:%M")))
    return {"ok":True,"pct":pct}

@app.get("/api/exportar-evaluaciones")
def exportar_evaluaciones(anio: int, mes: int):
    import openpyxl
    from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    MESES_ES = ["Enero","Febrero","Marzo","Abril","Mayo","Junio",
                "Julio","Agosto","Septiembre","Octubre","Noviembre","Diciembre"]
    nombre_mes = MESES_ES[mes-1]

    colaboradores = db_fetch("SELECT * FROM colaboradores ORDER BY nombre")
    for c in colaboradores:
        if isinstance(c.get("actividades"), str):
            c["actividades"] = json.loads(c["actividades"])
        c["puestos"] = parse_lista_json(c.get("puesto"))

    evals = db_fetch("""SELECT * FROM evaluaciones WHERE anio=%s AND mes=%s ORDER BY colaborador, dia""",
                     (anio, mes))
    for e in evals:
        if isinstance(e.get("calificaciones"), str):
            e["calificaciones"] = json.loads(e["calificaciones"])

    # Conteo de incidencias por colaborador EN ESE MES (una incidencia puede
    # involucrar a varios). Se usa para restar del promedio global.
    inc_por_colab = {}
    for row in db_fetch("SELECT colaborador, fecha FROM incidencias"):
        a, m = fecha_anio_mes(row.get("fecha"))
        if a != anio or m != mes:
            continue
        for nombre in parse_colabs(row.get("colaborador")):
            inc_por_colab[nombre] = inc_por_colab.get(nombre, 0) + 1

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"Evaluaciones {nombre_mes} {anio}"

    # Estilos
    def estilo_header(fill_color="1a1a2e", font_color="FFFFFF", bold=True, size=11):
        return {
            "fill": PatternFill("solid", fgColor=fill_color),
            "font": Font(color=font_color, bold=bold, size=size, name="Calibri"),
            "alignment": Alignment(horizontal="center", vertical="center", wrap_text=True),
            "border": Border(
                left=Side(style="thin", color="888888"),
                right=Side(style="thin", color="888888"),
                top=Side(style="thin", color="888888"),
                bottom=Side(style="thin", color="888888")
            )
        }

    def aplicar_estilo(cell, **kwargs):
        for k, v in kwargs.items():
            setattr(cell, k, v)

    def color_pct(pct):
        if pct >= 80: return "1a7a4a"
        if pct >= 60: return "b45309"
        return "991b1b"

    fila = 1

    # ── TÍTULO ────────────────────────────────────────────────────────────
    ws.merge_cells(f"A{fila}:H{fila}")
    cell = ws.cell(fila, 1, f"LUQROSS AUTOMOTRIZ — EVALUACIONES {nombre_mes.upper()} {anio}")
    aplicar_estilo(cell, **estilo_header("0f172a", "f8fafc", bold=True, size=14))
    ws.row_dimensions[fila].height = 30
    fila += 2

    # ── SECCIÓN 1: RESUMEN DEL MES ────────────────────────────────────────
    ws.merge_cells(f"A{fila}:H{fila}")
    cell = ws.cell(fila, 1, f"RESUMEN DEL MES — {nombre_mes.upper()} {anio}")
    aplicar_estilo(cell, **estilo_header("1e3a5f", "93c5fd", bold=True, size=12))
    ws.row_dimensions[fila].height = 22
    fila += 1

    # Headers resumen
    headers_res = ["Colaborador","Puesto","Días Evaluados","Promedio Global %","Mejor Actividad","Actividad con menor pct","Incidencias"]
    for col, h in enumerate(headers_res, 1):
        cell = ws.cell(fila, col, h)
        aplicar_estilo(cell, **estilo_header("1e40af", "dbeafe"))
        ws.column_dimensions[get_column_letter(col)].width = 22
    ws.row_dimensions[fila].height = 18
    fila += 1

    for colab in colaboradores:
        evals_colab = [e for e in evals if e["colaborador"] == colab["nombre"]]
        if not evals_colab:
            continue
        dias = len(evals_colab)
        # Promedios por actividad
        act_map = {}
        for e in evals_colab:
            for act, val in (e.get("calificaciones") or {}).items():
                if act not in act_map: act_map[act] = []
                act_map[act].append(val)
        act_promedios = {a: round(sum(v)/len(v)/5*100,1) for a,v in act_map.items()}
        prom_global_bruto = round(sum(act_promedios.values())/len(act_promedios),1) if act_promedios else 0
        mejor = max(act_promedios, key=act_promedios.get) if act_promedios else "—"
        peor  = min(act_promedios, key=act_promedios.get) if act_promedios else "—"
        n_inc = inc_por_colab.get(colab["nombre"], 0)
        # Cada incidencia del mes resta puntos porcentuales al promedio global final.
        prom_global = max(0, round(prom_global_bruto - n_inc * PENALIZACION_POR_INCIDENCIA_PCT, 1))

        vals = [colab["nombre"], ", ".join(colab.get("puestos") or []), dias, f"{prom_global}%",
                f"{mejor} ({act_promedios.get(mejor,0):.0f}%)",
                f"{peor} ({act_promedios.get(peor,0):.0f}%)", n_inc]
        for col, v in enumerate(vals, 1):
            cell = ws.cell(fila, col, v)
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = Border(
                left=Side(style="thin", color="cccccc"),
                right=Side(style="thin", color="cccccc"),
                top=Side(style="thin", color="cccccc"),
                bottom=Side(style="thin", color="cccccc")
            )
            if col == 1:
                cell.font = Font(bold=True, name="Calibri")
            if col == 4:
                color = color_pct(prom_global)
                cell.fill = PatternFill("solid", fgColor=color)
                cell.font = Font(bold=True, color="FFFFFF", name="Calibri")
        fila += 1

    fila += 2

    # ── SECCIÓN 2: DETALLE DÍA A DÍA ─────────────────────────────────────
    ws.merge_cells(f"A{fila}:H{fila}")
    cell = ws.cell(fila, 1, f"DETALLE DÍA A DÍA — {nombre_mes.upper()} {anio}")
    aplicar_estilo(cell, **estilo_header("1e3a5f", "93c5fd", bold=True, size=12))
    ws.row_dimensions[fila].height = 22
    fila += 1

    for colab in colaboradores:
        evals_colab = sorted([e for e in evals if e["colaborador"] == colab["nombre"]], key=lambda x: x["dia"])
        if not evals_colab: continue

        # Header colaborador
        ws.merge_cells(f"A{fila}:H{fila}")
        cell = ws.cell(fila, 1, f"{colab['nombre']} — {', '.join(colab.get('puestos') or [])}")
        aplicar_estilo(cell, **estilo_header("0f4c81", "e0f2fe", bold=True, size=11))
        ws.row_dimensions[fila].height = 20
        fila += 1

        # Obtener todas las actividades del colaborador
        actividades = colab.get("actividades", [])
        if not actividades:
            for e in evals_colab:
                actividades = list((e.get("calificaciones") or {}).keys())
                if actividades: break

        # Header tabla: Actividad | Día 1 | Día 2 | ... | Promedio
        dias_eval = sorted(set(e["dia"] for e in evals_colab))
        headers = ["Actividad"] + [f"Día {d}" for d in dias_eval] + ["Promedio %"]
        for col, h in enumerate(headers, 1):
            cell = ws.cell(fila, col, h)
            aplicar_estilo(cell, **estilo_header("1e40af", "dbeafe"))
            ws.column_dimensions[get_column_letter(col)].width = max(
                ws.column_dimensions[get_column_letter(col)].width or 0,
                len(h) + 4
            )
        ws.column_dimensions["A"].width = 35
        ws.row_dimensions[fila].height = 16
        fila += 1

        # Filas por actividad
        for act in actividades:
            row_vals = [act]
            vals_act = []
            for dia in dias_eval:
                eval_dia = next((e for e in evals_colab if e["dia"] == dia), None)
                val = (eval_dia.get("calificaciones") or {}).get(act, "") if eval_dia else ""
                row_vals.append(val)
                if val != "": vals_act.append(val)
            prom_act = round(sum(vals_act)/len(vals_act)/5*100,1) if vals_act else 0
            row_vals.append(f"{prom_act}%" if vals_act else "—")

            for col, v in enumerate(row_vals, 1):
                cell = ws.cell(fila, col, v)
                cell.alignment = Alignment(horizontal="center", vertical="center")
                cell.border = Border(
                    left=Side(style="thin", color="dddddd"),
                    right=Side(style="thin", color="dddddd"),
                    top=Side(style="thin", color="dddddd"),
                    bottom=Side(style="thin", color="dddddd")
                )
                if col == 1:
                    cell.alignment = Alignment(horizontal="left", vertical="center")
                    cell.font = Font(bold=True, size=9, name="Calibri")
                elif col == len(row_vals):  # Promedio
                    color = color_pct(prom_act)
                    cell.fill = PatternFill("solid", fgColor=color)
                    cell.font = Font(bold=True, color="FFFFFF", size=9, name="Calibri")
                elif v != "" and isinstance(v, (int, float)):
                    # Color por calificación
                    colors_cal = {5:"166534",4:"1a7a4a",3:"854d0e",2:"9a3412",1:"991b1b",0:"7f1d1d"}
                    bg = colors_cal.get(int(v), "374151")
                    cell.fill = PatternFill("solid", fgColor=bg)
                    cell.font = Font(bold=True, color="FFFFFF", size=9, name="Calibri")
            fila += 1

        # Fila de promedio diario
        row_prom = ["PROMEDIO DÍA %"]
        for dia in dias_eval:
            eval_dia = next((e for e in evals_colab if e["dia"] == dia), None)
            if eval_dia:
                row_prom.append(f"{eval_dia.get('pct',0):.0f}%")
            else:
                row_prom.append("—")
        prom_mes_colab = round(sum(e.get("pct",0) for e in evals_colab)/len(evals_colab),1)
        row_prom.append(f"{prom_mes_colab}%")
        for col, v in enumerate(row_prom, 1):
            cell = ws.cell(fila, col, v)
            aplicar_estilo(cell, **estilo_header("0f766e", "ccfbf1"))
            if col > 1:
                try:
                    pct_val = float(v.replace("%",""))
                    color = color_pct(pct_val)
                    cell.fill = PatternFill("solid", fgColor=color)
                    cell.font = Font(bold=True, color="FFFFFF", name="Calibri")
                except: pass
        fila += 2

    # Guardar en buffer
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    nombre_archivo = f"Evaluaciones_LUQROSS_{nombre_mes}_{anio}.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={nombre_archivo}"}
    )

@app.get("/api/evaluaciones/{nombre}/{anio}/{mes}")
def get_eval_mes(nombre:str,anio:int,mes:int):
    rows = db_fetch("SELECT * FROM evaluaciones WHERE colaborador=%s AND anio=%s AND mes=%s",
                    (nombre, anio, mes))
    for r in rows:
        if isinstance(r.get("calificaciones"), str):
            r["calificaciones"] = json.loads(r["calificaciones"])
    return rows

# ── Importar Excel masivo (una pestaña por colaborador) ────────────────────
@app.post("/api/importar-excel")
async def importar_excel(file: UploadFile = File(...)):
    import pandas as pd
    try:
        xls = pd.ExcelFile(file.file, engine="openpyxl")
        colaboradores = db_fetch("SELECT * FROM colaboradores")
        for c in colaboradores:
            if isinstance(c.get("actividades"), str):
                c["actividades"] = json.loads(c["actividades"])
        resumen = []
        errores = []

        for sheet_name in xls.sheet_names:
            # Buscar colaborador cuyo nombre coincida con la pestaña (flexible)
            colab_match = None
            sheet_upper = sheet_name.strip().upper()
            for c in colaboradores:
                if c["nombre"].upper() in sheet_upper or sheet_upper in c["nombre"].upper():
                    colab_match = c
                    break
            # Si no hay match exacto buscar por primer nombre/apellido
            if not colab_match:
                for c in colaboradores:
                    partes = c["nombre"].upper().split()
                    if any(p in sheet_upper for p in partes if len(p) > 3):
                        colab_match = c
                        break

            if not colab_match:
                errores.append(f"Pestaña '{sheet_name}': no se encontró colaborador.")
                continue

            try:
                df = pd.read_excel(xls, sheet_name=sheet_name, header=None)

                # Detectar filas clave buscando AÑO, MES, DIA, ACTIVIDAD
                fila_anio = fila_mes = fila_dia = fila_act = None
                for i, row in df.iterrows():
                    val = str(row.iloc[0]).strip().upper()
                    if "AÑO" in val or "ANO" in val:   fila_anio = i
                    elif "MES" in val:                  fila_mes  = i
                    elif "DIA" in val or "DÍA" in val: fila_dia  = i
                    elif "ACTIVIDAD" in val:            fila_act  = i

                if fila_dia is None or fila_act is None:
                    errores.append(f"Pestaña '{sheet_name}': no se encontró estructura DIA/ACTIVIDAD.")
                    continue

                # Leer año y mes
                anio_row = df.iloc[fila_anio] if fila_anio is not None else None
                mes_row  = df.iloc[fila_mes]  if fila_mes  is not None else None
                dia_row  = df.iloc[fila_dia]

                # Columnas de datos (desde columna 1 en adelante)
                dias_registrados = 0
                for col_idx in range(1, len(dia_row)):
                    try:
                        dia_val = dia_row.iloc[col_idx]
                        if pd.isna(dia_val): continue
                        dia = int(float(str(dia_val)))
                        if dia < 1 or dia > 31: continue

                        anio = int(float(str(anio_row.iloc[col_idx]))) if anio_row is not None and not pd.isna(anio_row.iloc[col_idx]) else datetime.now().year
                        mes  = int(float(str(mes_row.iloc[col_idx])))  if mes_row  is not None and not pd.isna(mes_row.iloc[col_idx])  else datetime.now().month

                        # Leer calificaciones por actividad
                        calificaciones = {}
                        for act_idx in range(fila_act + 1, len(df)):
                            act_nombre = str(df.iloc[act_idx, 0]).strip().upper()
                            if not act_nombre or act_nombre == "NAN" or act_nombre == "TOTAL": continue
                            cal_val = df.iloc[act_idx, col_idx]
                            if pd.isna(cal_val): continue
                            try:
                                cal = int(float(str(cal_val)))
                                if 0 <= cal <= 5:
                                    calificaciones[act_nombre] = cal
                            except: continue

                        if not calificaciones: continue

                        # Calcular promedio y pct
                        vals = list(calificaciones.values())
                        promedio = round(sum(vals)/len(vals), 2)
                        pct = round((promedio/5)*100, 1)

                        # Guardar en BD (upsert)
                        db_exec("""INSERT INTO evaluaciones (colaborador,anio,mes,dia,calificaciones,promedio,pct,guardado)
                                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                                   ON CONFLICT (colaborador,anio,mes,dia)
                                   DO UPDATE SET calificaciones=%s,promedio=%s,pct=%s,guardado=%s""",
                                (colab_match["nombre"], anio, mes, dia,
                                 json.dumps(calificaciones), promedio, pct, datetime.now().strftime("%d/%m/%Y %H:%M"),
                                 json.dumps(calificaciones), promedio, pct, datetime.now().strftime("%d/%m/%Y %H:%M")))
                        dias_registrados += 1
                    except: continue

                resumen.append({
                    "pestaña": sheet_name,
                    "colaborador": colab_match["nombre"],
                    "dias": dias_registrados
                })

            except Exception as e:
                errores.append(f"Pestaña '{sheet_name}': error al leer — {str(e)}")
                continue

        return {
            "status": "success",
            "resumen": resumen,
            "errores": errores,
            "total_colaboradores": len(resumen),
            "message": f"Importación completada: {len(resumen)} colaboradores, {sum(r['dias'] for r in resumen)} días registrados."
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ── API incidencias ────────────────────────────────────────────────────────
@app.post("/api/incidencia")
def guardar_inc(i:Incidencia):
    colabs = [c.strip() for c in i.colaboradores if c and c.strip()]
    if not colabs:
        raise HTTPException(status_code=400, detail="Selecciona al menos un colaborador.")
    tipo_final = i.tipo_personalizado.strip() if i.tipo == "Otro" and i.tipo_personalizado else i.tipo
    r = db_exec("""INSERT INTO incidencias (fecha,colaborador,tipo,responsable,ingresado_por,observaciones,estatus)
                   VALUES (%s,%s,%s,%s,%s,%s,'Abierta') RETURNING id""",
                (datetime.now().strftime("%d/%m/%Y %H:%M"), json.dumps(colabs), tipo_final,
                 i.responsable, i.ingresado_por, i.observaciones))
    return {"ok":True, "id": r["id"] if r else 0}

@app.post("/api/incidencia/{inc_id}/foto")
async def subir_foto_incidencia(inc_id: int, file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename)[1].lower() or ".jpg"
    path = f"static/incidencias/{inc_id}{ext}"
    with open(path, "wb") as b: b.write(await file.read())
    foto_url = f"/static/incidencias/{inc_id}{ext}"
    db_exec("UPDATE incidencias SET foto=%s WHERE id=%s", (foto_url, inc_id))
    return {"ok": True, "foto": foto_url}

@app.patch("/api/incidencia/{inc_id}/resolver")
def resolver_inc(inc_id:int, body:dict={}):
    db_exec("""UPDATE incidencias SET estatus='Resuelta', solucion=%s, resuelto_por=%s, fecha_resolucion=%s
               WHERE id=%s""",
            (body.get("solucion","").strip(), body.get("resuelto_por","").strip(),
             datetime.now().strftime("%d/%m/%Y %H:%M"), inc_id))
    return {"ok":True}

# ── API horas ──────────────────────────────────────────────────────────────
@app.post("/api/horas")
def guardar_horas(h:RegistroHoras):
    minutos_prep=None
    try:
        inicio=h.horas.get("Hora de Conocimiento de Ruta","")
        fin=h.horas.get("Hora de Término de Preparación","")
        if inicio and fin:
            hi=list(map(int,inicio.split(":")))
            hf=list(map(int,fin.split(":")))
            minutos_prep=(hf[0]*60+hf[1])-(hi[0]*60+hi[1])
    except: pass

    minutos_ruta=None
    try:
        sal=h.horas.get("Hora de Salida","")
        reg=h.horas.get("Hora de Llegada (Comida)","")
        if sal and reg:
            hs=list(map(int,sal.split(":")))
            hr=list(map(int,reg.split(":")))
            minutos_ruta=(hr[0]*60+hr[1])-(hs[0]*60+hs[1])
    except: pass

    meta = META_LOCAL if h.tipo_ruta == "Ruta Local" else META_PAQ
    efic_prep = round((meta / minutos_prep) * 100, 1) if minutos_prep and minutos_prep > 0 else None
    nivel_prep = None
    if efic_prep is not None:
        nivel_prep = "Óptimo" if efic_prep >= 100 else ("Aceptable" if efic_prep >= 80 else "Por mejorar")

    METAS_HORA = {
        "Hora de Conocimiento de Ruta":"09:40","Hora de Entrega de Etiquetas":"09:55",
        "Hora de Término de Preparación":"10:40","Hora de Entrega de Papeles":"11:00","Hora de Salida":"12:10",
    }
    cumplimientos = {}
    for act, meta_hora in METAS_HORA.items():
        val = h.horas.get(act, "")
        if val:
            try:
                hv=list(map(int,val.split(":"))); hm=list(map(int,meta_hora.split(":")))
                cumplimientos[act]={"hora_real":val,"meta":meta_hora,"cumple":(hv[0]*60+hv[1])<=(hm[0]*60+hm[1])}
            except: pass

    db_exec("""INSERT INTO horas (colaborador,fecha,tipo_ruta,destino,horas,minutos_prep,minutos_ruta,
               meta_prep,efic_prep,nivel_prep,cumplimientos,guardado)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (colaborador,fecha)
               DO UPDATE SET tipo_ruta=%s,destino=%s,horas=%s,minutos_prep=%s,minutos_ruta=%s,
               meta_prep=%s,efic_prep=%s,nivel_prep=%s,cumplimientos=%s,guardado=%s""",
            (h.colaborador, h.fecha, h.tipo_ruta, h.destino, json.dumps(h.horas),
             minutos_prep, minutos_ruta, meta, efic_prep, nivel_prep,
             json.dumps(cumplimientos), datetime.now().strftime("%d/%m/%Y %H:%M"),
             h.tipo_ruta, h.destino, json.dumps(h.horas), minutos_prep, minutos_ruta,
             meta, efic_prep, nivel_prep, json.dumps(cumplimientos), datetime.now().strftime("%d/%m/%Y %H:%M")))
    return {"ok": True, "minutos_prep": minutos_prep, "efic_prep": efic_prep, "nivel_prep": nivel_prep}

@app.patch("/api/horas/regreso")
def actualizar_regreso(body: dict):
    colaborador  = body.get("colaborador","").strip()
    fecha        = body.get("fecha","").strip()
    hora_regreso = body.get("hora_regreso","").strip()
    rows = db_fetch("SELECT * FROM horas WHERE colaborador=%s AND fecha=%s", (colaborador, fecha))
    if not rows:
        raise HTTPException(400, "No se encontró el registro.")
    row = rows[0]
    horas_dict = row["horas"] if isinstance(row["horas"], dict) else json.loads(row["horas"])
    horas_dict["Hora de Llegada (Comida)"] = hora_regreso
    minutos_ruta = None
    try:
        sal = horas_dict.get("Hora de Salida","")
        if sal:
            hs=list(map(int,sal.split(":"))); hr=list(map(int,hora_regreso.split(":")))
            minutos_ruta=(hr[0]*60+hr[1])-(hs[0]*60+hs[1])
    except: pass
    db_exec("UPDATE horas SET horas=%s, minutos_ruta=%s WHERE colaborador=%s AND fecha=%s",
            (json.dumps(horas_dict), minutos_ruta, colaborador, fecha))
    return {"ok": True}

@app.post("/api/tiempo")
def guardar_tiempo(t:Tiempo):
    meta=META_LOCAL if t.tipo_ruta=="Ruta Local" else META_PAQ
    eficiencia=round((meta/t.minutos)*100,1) if t.minutos>0 else 0
    nivel="Óptimo" if eficiencia>=100 else ("Aceptable" if eficiencia>=80 else "Por mejorar")
    db_exec("""INSERT INTO tiempos (fecha,colaborador,tipo_ruta,modelo_tarea,minutos,meta_minutos,eficiencia,nivel,observaciones)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (datetime.now().strftime("%d/%m/%Y %H:%M"), t.colaborador, t.tipo_ruta,
             t.modelo_tarea, t.minutos, meta, eficiencia, nivel, t.observaciones))
    return {"ok":True,"eficiencia":eficiencia,"nivel":nivel}

# ── Página principal ───────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def pagina():
    colaboradores = db_fetch("SELECT * FROM colaboradores ORDER BY nombre")
    for c in colaboradores:
        if isinstance(c.get("actividades"), str):
            c["actividades"] = json.loads(c["actividades"])
        c["puestos"] = parse_lista_json(c.get("puesto"))
        if c.get("activo") is None:
            c["activo"] = True
    colaboradores_activos = [c for c in colaboradores if c.get("activo")]
    colaboradores_inactivos = [c for c in colaboradores if not c.get("activo")]

    evaluaciones = db_fetch("SELECT * FROM evaluaciones ORDER BY anio,mes,dia")
    for e in evaluaciones:
        if isinstance(e.get("calificaciones"), str):
            e["calificaciones"] = json.loads(e["calificaciones"])

    incidencias = db_fetch("SELECT * FROM incidencias ORDER BY id DESC")
    for inc in incidencias:
        inc["colaboradores"] = parse_colabs(inc.get("colaborador"))
        a, m = fecha_anio_mes(inc.get("fecha"))
        inc["anio"], inc["mes"] = a, m

    tiempos = db_fetch("SELECT * FROM tiempos ORDER BY id DESC")

    horas = db_fetch("SELECT * FROM horas ORDER BY fecha DESC LIMIT 80")
    for h in horas:
        if isinstance(h.get("horas"), str):
            h["horas"] = json.loads(h["horas"])
        if isinstance(h.get("cumplimientos"), str):
            h["cumplimientos"] = json.loads(h["cumplimientos"])

    logo_html=f'<img src="/static/{LOGO_FILE}" alt="LUQROSS" class="w-28 h-auto object-contain block mr-4 select-none">' if os.path.exists(f"static/{LOGO_FILE}") else ""
    # "_activos": para registrar cosas NUEVAS (evaluación, incidencia, horas) — solo colaboradores activos.
    # "_todos": para filtros/análisis histórico — incluye también a los desactivados, para no perder su historial.
    opts_colab_activos="".join([f'<option value="{c["nombre"]}">{c["nombre"]}</option>' for c in colaboradores_activos])
    opts_colab_todos="".join([f'<option value="{c["nombre"]}">{c["nombre"]}</option>' for c in colaboradores])
    opts_colab_chk="".join([f'<label class="flex items-center gap-2 text-[11px] text-gray-300 hover:text-white cursor-pointer"><input type="checkbox" class="inc-colab-chk accent-rose-500" value="{c["nombre"]}"> {c["nombre"]}</label>' for c in colaboradores_activos]) or '<p class="text-[10px] text-gray-600">No hay colaboradores activos.</p>'

    # KPI cards globales
    total_eval=len(set((e["colaborador"],e["anio"],e["mes"]) for e in evaluaciones))
    inc_abiertas=sum(1 for i in incidencias if i["estatus"]=="Abierta")
    pct_optimos=round(sum(1 for t in tiempos if t["nivel"]=="Óptimo")/len(tiempos)*100,1) if tiempos else 0
    prom_global=round(sum(e["pct"] for e in evaluaciones)/len(evaluaciones),1) if evaluaciones else 0

    # Cumplimiento de tiempos de preparación desde Registro de Horas
    horas_con_prep = [h for h in horas if h.get("minutos_prep") is not None]
    total_h = len(horas_con_prep)
    en_meta = sum(1 for h in horas_con_prep if h.get("efic_prep") is not None and h["efic_prep"] >= 100)
    pct_cumplimiento_prep = round((en_meta / total_h) * 100, 1) if total_h > 0 else None

    # ALMACEN = Paquetería, todos los demás = Local
    local_h = [h for h in horas_con_prep if h.get("colaborador","").upper() != "ALMACEN"]
    paq_h   = [h for h in horas_con_prep if h.get("colaborador","").upper() == "ALMACEN"]
    local_ok = sum(1 for h in local_h if h.get("efic_prep") is not None and h["efic_prep"] >= 100)
    paq_ok   = sum(1 for h in paq_h   if h.get("efic_prep") is not None and h["efic_prep"] >= 100)
    pct_local = round((local_ok/len(local_h))*100,1) if local_h else None
    pct_paq   = round((paq_ok/len(paq_h))*100,1)    if paq_h   else None

    # Promedios de tiempo real
    def mins_a_hms(mins):
        if mins is None: return "—"
        h = int(mins)//60; m = int(mins)%60; s = 0
        return f"{h:02d}:{m:02d}:00"

    avg_local_mins = round(sum(h["minutos_prep"] for h in local_h)/len(local_h),1) if local_h else None
    avg_paq_mins   = round(sum(h["minutos_prep"] for h in paq_h)/len(paq_h),1)     if paq_h   else None
    avg_local_str  = mins_a_hms(avg_local_mins)
    avg_paq_str    = mins_a_hms(avg_paq_mins)

    color_prep = ('text-emerald-400' if pct_cumplimiento_prep and pct_cumplimiento_prep>=80
                  else 'text-yellow-400' if pct_cumplimiento_prep and pct_cumplimiento_prep>=60
                  else 'text-rose-400')

    # Color por fila
    def color_pct(p):
        if p is None: return "text-gray-400", "bg-gray-800"
        if p >= 100: return "text-emerald-400", "bg-emerald-500"
        if p >= 80:  return "text-yellow-400",  "bg-yellow-500"
        return "text-rose-400", "bg-rose-500"

    cl_local, bg_local = color_pct(pct_local)
    cl_paq,   bg_paq   = color_pct(pct_paq)

    # Filas incidencias
    col_tipo={"Falta":"text-rose-400","Retardo":"text-orange-400","Permiso":"text-blue-400",
              "Accidente":"text-purple-400","Conducta":"text-pink-400",
              "Material Sucio":"text-amber-400","Material Incompleto":"text-yellow-400",
              "Mal Etiquetado":"text-cyan-400"}
    filas_inc=""
    if not incidencias:
        filas_inc='<tr><td colspan="7" class="p-5 text-center text-xs text-gray-500">No hay incidencias registradas.</td></tr>'
    else:
        for inc in reversed(incidencias):
            nombres_inc=", ".join(inc.get("colaboradores") or [])
            ct=col_tipo.get(inc["tipo"],"text-gray-400")
            est_badge=('bg-emerald-500/10 text-emerald-400 border-emerald-500/20' if inc["estatus"]=="Resuelta"
                       else 'bg-rose-500/10 text-rose-400 border-rose-500/20')
            btn=f'<button onclick="abrirModalResolver({inc["id"]})" class="text-[9px] font-bold text-emerald-400 hover:text-emerald-300 underline transition-colors">✓ Resolver</button>' if inc["estatus"]=="Abierta" else ""
            solucion_html=""
            if inc.get("solucion"):
                solucion_html=f'<div class="mt-1 text-[9px] text-emerald-300/70 italic max-w-[120px] truncate" title="{inc["solucion"]}">💬 {inc["solucion"][:40]}...</div>' if len(inc.get("solucion",""))>40 else f'<div class="mt-1 text-[9px] text-emerald-300/70 italic">💬 {inc["solucion"]}</div>'
            foto_html=""
            if inc.get("foto"):
                foto_html=f'<img src="{inc["foto"]}" onclick="verFotoInc(\'{inc["foto"]}\')" class="w-10 h-10 object-cover rounded-lg border border-gray-700 cursor-pointer hover:scale-110 transition-transform shadow" title="Ver foto">'
            else:
                foto_html='<span class="text-gray-700 text-[10px]">—</span>'
            filas_inc+=f"""<tr class="border-b border-gray-800 hover:bg-gray-900/30 text-xs transition-colors" data-ingresado="{inc.get('ingresado_por','')}">
                <td class="px-3 py-2.5 font-mono text-gray-400 text-[10px]">{inc['fecha']}</td>
                <td class="px-3 py-2.5 font-bold text-white uppercase">{nombres_inc}</td>
                <td class="px-3 py-2.5 font-bold {ct}">{inc['tipo']}</td>
                <td class="px-3 py-2.5 text-gray-500 text-[10px]">{inc['responsable']}</td>
                <td class="px-3 py-2.5 text-gray-500 text-[10px] italic max-w-[150px] truncate">{inc.get('observaciones','')}</td>
                <td class="px-3 py-2.5 text-center">{foto_html}</td>
                <td class="px-3 py-2.5 text-center"><span class="text-[9px] font-bold px-2 py-0.5 rounded-full border {est_badge}">{inc['estatus']}</span><div class="mt-0.5">{btn}</div>{solucion_html}</td>
            </tr>"""

    # Filas tiempos
    filas_tiempos=""
    if not tiempos:
        filas_tiempos='<tr><td colspan="7" class="p-5 text-center text-xs text-gray-500">No hay tiempos registrados.</td></tr>'
    else:
        col_niv={"Óptimo":"text-emerald-400","Aceptable":"text-yellow-400","Por mejorar":"text-rose-400"}
        for t in reversed(tiempos):
            cn=col_niv.get(t["nivel"],"text-gray-400")
            pct=min(t["eficiencia"],100)
            bc="bg-emerald-500" if t["nivel"]=="Óptimo" else ("bg-yellow-500" if t["nivel"]=="Aceptable" else "bg-rose-500")
            filas_tiempos+=f"""<tr class="border-b border-gray-800 hover:bg-gray-900/30 text-xs">
                <td class="px-3 py-2.5 font-mono text-gray-400 text-[10px]">{t['fecha']}</td>
                <td class="px-3 py-2.5 font-bold text-white uppercase">{t['colaborador']}</td>
                <td class="px-3 py-2.5 text-blue-400">{t['tipo_ruta']}</td>
                <td class="px-3 py-2.5 text-gray-300 uppercase">{t['modelo_tarea']}</td>
                <td class="px-3 py-2.5 text-center font-bold text-white">{t['minutos']} <span class="text-gray-600 font-normal">/ {t['meta_minutos']} min</span></td>
                <td class="px-3 py-2.5 min-w-[110px]"><div class="flex items-center gap-2"><div class="flex-1 h-1.5 bg-gray-800 rounded-full"><div class="{bc} h-full rounded-full" style="width:{pct}%"></div></div><span class="font-black text-[11px] {cn}">{t['eficiencia']}%</span></div></td>
                <td class="px-3 py-2.5 font-bold {cn}">{t['nivel']}</td>
            </tr>"""

    # Filas horas
    filas_horas=""
    if not horas:
        filas_horas='<tr><td colspan="7" class="p-5 text-center text-xs text-gray-500">No hay registros de horas.</td></tr>'
    else:
        col_niv={"Óptimo":"text-emerald-400","Aceptable":"text-yellow-400","Por mejorar":"text-rose-400"}
        for h in reversed(horas[-80:]):
            prep   = f"{h.get('minutos_prep','—')} min" if h.get('minutos_prep') else "—"
            efic   = h.get('efic_prep')
            nivel  = h.get('nivel_prep','')
            cn     = col_niv.get(nivel,'text-gray-400')
            efic_str = f"{efic}%" if efic is not None else "—"
            pct_bar  = min(efic, 130) if efic else 0
            bc = "bg-emerald-500" if nivel=="Óptimo" else ("bg-yellow-500" if nivel=="Aceptable" else "bg-rose-500")
            # Semáforo de cumplimiento
            cumps = h.get('cumplimientos', {})
            total_c = len(cumps)
            ok_c    = sum(1 for v in cumps.values() if v.get('cumple'))
            sem = (f'<span class="text-emerald-400 font-black">{ok_c}/{total_c} ✓</span>'
                   if ok_c == total_c else
                   f'<span class="text-rose-400 font-black">{ok_c}/{total_c}</span>')
            tipo_badge = ('bg-blue-500/10 text-blue-400 border-blue-500/20' if h.get('tipo_ruta')=='Ruta Local'
                          else 'bg-purple-500/10 text-purple-400 border-purple-500/20')
            h_safe = json.dumps(h).replace("'", "\\'")
            tiene_regreso = h.get('horas',{}).get('Hora de Llegada (Comida)','')
            btn_regreso = f"""<button onclick="event.stopPropagation();abrirModalRegreso('{h['colaborador']}','{h['fecha']}')"
                class="text-[9px] font-bold text-amber-400 hover:text-amber-300 border border-amber-500/30 hover:border-amber-400 px-2 py-0.5 rounded-lg transition-colors whitespace-nowrap">
                {'✓ ' + tiene_regreso if tiene_regreso else '+ Regreso'}
            </button>"""
            filas_horas+=f"""<tr class="border-b border-gray-800 hover:bg-gray-900/30 text-xs cursor-pointer transition-colors" onclick='verHoras({json.dumps(h)})'>
                <td class="px-3 py-2.5 font-mono text-gray-400 text-[10px]">{h['fecha']}</td>
                <td class="px-3 py-2.5 font-bold text-white uppercase text-[10px]">{h['colaborador']}</td>
                <td class="px-3 py-2.5"><span class="text-[9px] font-bold px-2 py-0.5 rounded-full border {tipo_badge}">{h.get('tipo_ruta','—')}</span></td>
                <td class="px-3 py-2.5 text-gray-400 text-[10px]">{h.get('destino','—')}</td>
                <td class="px-3 py-2.5 font-bold text-center text-white">{prep}</td>
                <td class="px-3 py-2.5 min-w-[100px]">
                  {'<div class="flex items-center gap-1.5"><div class="flex-1 h-1.5 bg-gray-800 rounded-full"><div class="' + bc + ' h-full rounded-full" style="width:' + str(min(pct_bar,100)) + '%"></div></div><span class="font-black text-[10px] ' + cn + '">' + efic_str + '</span></div>' if efic else '<span class="text-gray-600">—</span>'}
                </td>
                <td class="px-3 py-2.5 text-center text-[11px]">{sem}</td>
                <td class="px-3 py-2.5 text-center">{btn_regreso}</td>
            </tr>"""

    # JSON para JS
    colabs_json=json.dumps(colaboradores)
    evals_json =json.dumps(evaluaciones)
    MESES_ES = ["Enero","Febrero","Marzo","Abril","Mayo","Junio","Julio","Agosto","Septiembre","Octubre","Noviembre","Diciembre"]

    html=f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>LUQROSS | Almacen Evaluaciones</title>
        <link rel="icon" type="image/png" href="/static/logo.png">
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
@font-face{{font-family:'MiFuenteCustom';src:url('/static/{FONT_FILE}') format('truetype');}}
.font-custom{{font-family:'MiFuenteCustom',sans-serif;letter-spacing:.05em;}}
body{{background:radial-gradient(circle at top,#1a3a5c 0%,#0d1f35 60%, #071525 100%) fixed;}}
input[type=range]{{accent-color:#eab308;}}
.tab-btn{{padding:.625rem .875rem;font-size:.75rem;text-transform:uppercase;letter-spacing:.05em;transition:all .2s;font-family:'MiFuenteCustom',sans-serif;color:#9ca3af;}}
.tab-btn:hover{{color:#e5e7eb;}}
.tab-btn.active{{border-bottom:2px solid #eab308;color:#eab308;font-weight:700;}}
.field{{width:100%;background:#030712;border:1px solid #1f2937;border-radius:.75rem;padding:.5rem .75rem;font-size:.75rem;color:#fff;outline:none;transition:border-color .2s;}}
.field:focus{{border-color:#eab308;}}
.lbl{{display:block;font-size:.625rem;color:#6b7280;text-transform:uppercase;font-weight:700;margin-bottom:.25rem;}}
.cal-cell{{min-width:38px;text-align:center;font-size:10px;border:1px solid #1f2937;padding:2px;}}
.score-badge-0{{background:#7f1d1d;color:#fca5a5;}}
.score-badge-1{{background:#7c2d12;color:#fdba74;}}
.score-badge-2{{background:#713f12;color:#fde68a;}}
.score-badge-3{{background:#1e3a5f;color:#93c5fd;}}
.score-badge-4{{background:#14532d;color:#86efac;}}
.score-badge-5{{background:#064e3b;color:#6ee7b7;}}
</style>
</head>
<body class="text-gray-100 min-h-screen flex flex-col items-center px-2 py-3 pb-16">

<!-- MODAL tarjetas equipo completo -->
<div id="modal-equipo" class="hidden fixed inset-0 z-50 bg-gray-950/95 backdrop-blur-sm flex items-start justify-center p-4 overflow-y-auto">
  <div class="bg-gray-900 border border-gray-800 rounded-2xl p-6 w-full max-w-5xl my-4 space-y-4">
    <div class="flex justify-between items-center flex-wrap gap-3">
      <h3 class="text-sm font-bold text-yellow-500 font-custom uppercase">Desempeño del Equipo</h3>
      <div class="flex items-center gap-3">
        <select id="equipo-mes-selector" onchange="renderTarjetasEquipo()" class="bg-gray-950 border border-gray-700 text-white text-xs rounded-lg px-3 py-1.5 outline-none focus:border-yellow-500">
          {"".join([f'<option value="{anio}-{mes}">{MESES_ES[mes-1]} {anio}</option>' for anio,mes in sorted(set((e["anio"],e["mes"]) for e in evaluaciones), reverse=True)] if evaluaciones else ['<option>Sin datos</option>'])}
        </select>
        <button onclick="document.getElementById('modal-equipo').classList.add('hidden')" class="text-gray-500 hover:text-white font-bold text-xl">✕</button>
      </div>
    </div>
    <div id="equipo-tarjetas-container" class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4"></div>
  </div>
</div>

<!-- MODAL actualizar hora de regreso -->
<div id="modal-regreso" class="hidden fixed inset-0 z-50 bg-gray-950/90 backdrop-blur-sm flex items-center justify-center p-4">
  <div class="bg-gray-900 border border-gray-800 rounded-2xl p-6 w-full max-w-sm space-y-4">
    <div class="flex justify-between items-center">
      <h3 class="text-xs font-bold text-amber-400 font-custom uppercase">Registrar Hora de Regreso</h3>
      <button onclick="document.getElementById('modal-regreso').classList.add('hidden')" class="text-gray-500 hover:text-white font-bold">✕</button>
    </div>
    <input type="hidden" id="regreso-colaborador">
    <input type="hidden" id="regreso-fecha">
    <div class="bg-gray-950/60 border border-gray-800 rounded-xl p-3 text-xs text-gray-400 space-y-1">
      <p><span class="text-gray-500">Operador:</span> <span id="regreso-nombre-display" class="font-bold text-white uppercase"></span></p>
      <p><span class="text-gray-500">Fecha:</span> <span id="regreso-fecha-display" class="font-mono text-yellow-500"></span></p>
    </div>
    <div>
      <label class="lbl">Hora de Llegada / Regreso</label>
      <input id="regreso-hora" type="time" class="field font-mono text-lg text-center font-bold">
    </div>
    <div class="flex gap-3">
      <button onclick="document.getElementById('modal-regreso').classList.add('hidden')" class="flex-1 bg-gray-800 hover:bg-gray-700 text-gray-300 font-bold py-2.5 rounded-xl text-xs uppercase font-custom transition-colors">Cancelar</button>
      <button onclick="guardarRegreso()" class="flex-1 bg-amber-500 hover:bg-amber-400 text-gray-950 font-black py-2.5 rounded-xl text-xs uppercase font-custom transition-colors">Guardar</button>
    </div>
    <div id="res-regreso" class="hidden p-2 text-center text-xs font-bold rounded-xl"></div>
  </div>
</div>

<!-- MODAL detalle horas -->
<div id="modal-horas" class="hidden fixed inset-0 z-50 bg-gray-950/90 backdrop-blur-sm flex items-center justify-center p-4">
  <div class="bg-gray-900 border border-gray-800 rounded-2xl p-6 w-full max-w-sm space-y-3">
    <div class="flex justify-between"><h3 class="text-xs font-bold text-yellow-500 font-custom uppercase">Registro de Horas</h3><button onclick="document.getElementById('modal-horas').classList.add('hidden')" class="text-gray-500 hover:text-white font-bold">✕</button></div>
    <div id="modal-horas-body"></div>
  </div>
</div>

<!-- MODAL resolución de incidencia -->
<!-- MODAL desglose tiempo preparación -->
<div id="modal-prep" class="hidden fixed inset-0 z-50 bg-gray-950/90 backdrop-blur-sm flex items-center justify-center p-4">
  <div class="bg-gray-900 border border-gray-800 rounded-2xl p-6 w-full max-w-lg space-y-4">
    <div class="flex justify-between items-center">
      <h3 class="text-sm font-bold text-amber-400 font-custom uppercase">Tiempo de Preparación</h3>
      <button onclick="document.getElementById('modal-prep').classList.add('hidden')" class="text-gray-500 hover:text-white font-bold text-xl">✕</button>
    </div>
    <p class="text-[10px] text-gray-500 uppercase font-bold">Registros del período actual (últimos 80)</p>
    <table class="w-full border-collapse text-sm">
      <thead>
        <tr class="bg-gray-800 text-[10px] text-gray-400 uppercase font-bold tracking-wide">
          <th class="px-4 py-3 text-left">Entrega</th>
          <th class="px-4 py-3 text-center">Objetivo</th>
          <th class="px-4 py-3 text-center">Tiempo Media</th>
          <th class="px-4 py-3 text-center">Rutas</th>
          <th class="px-4 py-3 text-center">Puntaje</th>
        </tr>
      </thead>
      <tbody>
        <tr class="border-b border-gray-800">
          <td class="px-4 py-3 font-bold text-white">LOCAL</td>
          <td class="px-4 py-3 text-center text-gray-400 text-xs">&lt; 3h</td>
          <td class="px-4 py-3 text-center font-mono font-bold text-blue-300">{avg_local_str}</td>
          <td class="px-4 py-3 text-center text-[11px] text-gray-400">{local_ok}/{len(local_h)}</td>
          <td class="px-4 py-3 text-center">
            <span class="font-black text-sm px-3 py-1 rounded-lg {bg_local} text-white">{f'{pct_local}%' if pct_local is not None else '—'}</span>
          </td>
        </tr>
        <tr>
          <td class="px-4 py-3 font-bold text-white">PAQUETERÍA</td>
          <td class="px-4 py-3 text-center text-gray-400 text-xs">&lt; 5h</td>
          <td class="px-4 py-3 text-center font-mono font-bold text-purple-300">{avg_paq_str}</td>
          <td class="px-4 py-3 text-center text-[11px] text-gray-400">{paq_ok}/{len(paq_h)}</td>
          <td class="px-4 py-3 text-center">
            <span class="font-black text-sm px-3 py-1 rounded-lg {bg_paq} text-white">{f'{pct_paq}%' if pct_paq is not None else '—'}</span>
          </td>
        </tr>
      </tbody>
    </table>
    <div class="bg-gray-800/50 rounded-xl p-3 text-[10px] text-gray-400 space-y-1">
      <p>• <span class="font-bold text-white">LOCAL</span> — todos los operadores excepto ALMACEN. Meta: menos de 3h.</p>
      <p>• <span class="font-bold text-white">PAQUETERÍA</span> — solo ALMACEN. Meta: menos de 5h.</p>
      <p>• Tiempo medido desde <span class="text-yellow-400">Conocimiento de Ruta</span> hasta <span class="text-yellow-400">Término de Preparación</span>.</p>
    </div>
  </div>
</div>

<div id="modal-resolver" class="hidden fixed inset-0 z-50 bg-gray-950/90 backdrop-blur-sm flex items-center justify-center p-4">
  <div class="bg-gray-900 border border-gray-800 rounded-2xl p-6 w-full max-w-md space-y-4">
    <div class="flex justify-between items-center">
      <h3 class="text-xs font-bold text-emerald-400 font-custom uppercase">Registrar Solución</h3>
      <button onclick="document.getElementById('modal-resolver').classList.add('hidden')" class="text-gray-500 hover:text-white font-bold">✕</button>
    </div>
    <input type="hidden" id="resolver-inc-id">
    <div>
      <label class="lbl">Resuelto por</label>
      <select id="resolver-por" class="field">
        <option value="">-- Selecciona --</option>
        <option>NALLELI GUAJARDO</option>
        <option>SEBASTIAN YAEL</option>
        <option>LESLY</option>
        <option>IRMA</option>
        <option>DIEGO</option>
        <option>VICTOR</option>
      </select>
    </div>
    <div>
      <label class="lbl">Descripción de la Solución</label>
      <textarea id="resolver-solucion" rows="4" class="field resize-none" placeholder="Describe cómo se resolvió la incidencia..."></textarea>
    </div>
    <div class="flex gap-3">
      <button onclick="document.getElementById('modal-resolver').classList.add('hidden')" class="flex-1 bg-gray-800 hover:bg-gray-700 text-gray-300 font-bold py-2.5 rounded-xl text-xs uppercase font-custom transition-colors">Cancelar</button>
      <button onclick="confirmarResolucion()" class="flex-1 bg-emerald-600 hover:bg-emerald-500 text-white font-black py-2.5 rounded-xl text-xs uppercase font-custom transition-colors">Marcar como Resuelta</button>
    </div>
    <div id="res-resolver" class="hidden p-2 text-center text-xs font-bold rounded-xl"></div>
  </div>
</div>

<!-- MODAL foto incidencia -->
<div id="modal-foto-inc" class="hidden fixed inset-0 z-50 bg-gray-950/95 backdrop-blur-sm flex items-center justify-center p-4" onclick="document.getElementById('modal-foto-inc').classList.add('hidden')">
  <div class="relative max-w-2xl w-full">
    <img id="modal-foto-inc-img" src="" class="w-full rounded-2xl border border-gray-700 shadow-2xl max-h-[80vh] object-contain">
    <button class="absolute top-3 right-3 bg-gray-900/80 text-white font-black w-8 h-8 rounded-full flex items-center justify-center hover:bg-rose-500 transition-colors text-sm">✕</button>
  </div>
</div>

<!-- MODAL historial calificaciones colaborador -->
<div id="modal-historial" class="hidden fixed inset-0 z-50 bg-gray-950/90 backdrop-blur-sm flex items-center justify-center p-4 overflow-y-auto">
  <div class="bg-gray-900 border border-gray-800 rounded-2xl p-6 w-full max-w-2xl space-y-4 my-4">
    <div class="flex justify-between items-start">
      <div class="flex items-center gap-4">
        <div>
          <img id="mh-foto" src="" onerror="this.style.display='none';document.getElementById('mh-init').style.display='flex'"
            class="w-20 h-20 rounded-xl object-cover border-2 border-yellow-500/40 shadow">
          <div id="mh-init" class="w-20 h-20 rounded-xl bg-yellow-500/10 border-2 border-yellow-500/30 items-center justify-center text-yellow-500 font-black text-2xl hidden"></div>
        </div>
        <div>
          <h3 id="mh-nombre" class="text-sm font-black text-white font-custom uppercase"></h3>
          <p id="mh-puesto" class="text-[10px] text-gray-500 uppercase font-bold"></p>
          <p class="text-[9px] text-yellow-500/70 mt-0.5">Historial de Evaluaciones</p>
        </div>
      </div>
      <button onclick="document.getElementById('modal-historial').classList.add('hidden')"
        class="text-gray-500 hover:text-white font-bold text-lg">✕</button>
    </div>

    <!-- Resumen global -->
    <div class="grid grid-cols-3 gap-3" id="mh-resumen"></div>

    <!-- Tabla por mes -->
    <div class="border border-gray-800 rounded-xl overflow-hidden">
      <table class="w-full text-left border-collapse">
        <thead>
          <tr class="bg-gray-950 text-[10px] text-gray-400 uppercase font-bold border-b border-gray-800">
            <th class="px-4 py-2.5">Mes</th>
            <th class="px-4 py-2.5 text-center">Días Eval.</th>
            <th class="px-4 py-2.5 text-center">Promedio</th>
            <th class="px-4 py-2.5">Mejor Actividad</th>
            <th class="px-4 py-2.5">Actividad a Mejorar</th>
          </tr>
        </thead>
        <tbody id="mh-tabla"></tbody>
      </table>
    </div>

    <!-- Mini gráfica de tendencia -->
    <div class="bg-gray-950/50 border border-gray-800 rounded-xl p-4 h-[200px]">
      <p class="text-[10px] font-bold text-yellow-500 uppercase mb-2 font-custom">Tendencia de Calificación</p>
      <div class="h-[160px]"><canvas id="mh-chart"></canvas></div>
    </div>
  </div>
</div>

<!-- MODAL tarjeta colaborador -->
<div id="modal-tarjeta" class="hidden fixed inset-0 z-50 bg-gray-950/90 backdrop-blur-sm flex items-center justify-center p-4 overflow-y-auto">
  <div class="bg-gray-900 border border-gray-800 rounded-2xl p-6 w-full max-w-md space-y-3 my-4">
    <div class="flex justify-between"><h3 class="text-xs font-bold text-yellow-500 font-custom uppercase">Tarjeta de Evaluación</h3><button onclick="document.getElementById('modal-tarjeta').classList.add('hidden')" class="text-gray-500 hover:text-white font-bold">✕</button></div>
    <div id="modal-tarjeta-body"></div>
  </div>
</div>

<!-- MODAL agregar/editar colaborador -->
<div id="modal-colab" class="hidden fixed inset-0 z-50 bg-gray-950/90 backdrop-blur-sm flex items-center justify-center p-4 overflow-y-auto">
  <div class="bg-gray-900 border border-gray-800 rounded-2xl p-6 w-full max-w-lg space-y-4 my-4">
    <div class="flex justify-between"><h3 class="text-xs font-bold text-yellow-500 font-custom uppercase">Agregar Colaborador</h3><button onclick="document.getElementById('modal-colab').classList.add('hidden')" class="text-gray-500 hover:text-white font-bold">✕</button></div>
    <div class="space-y-3">
      <div><label class="lbl">Nombre Completo</label><input id="col-nombre" class="field" placeholder="Ej. Juan Pérez"></div>
      <div>
        <label class="lbl">Tipo de Puesto (puedes marcar varios)</label>
        <div class="field" style="display:flex;flex-direction:column;gap:6px;">
          {"".join([f'<label class="flex items-center gap-2 text-[11px] text-gray-300 hover:text-white cursor-pointer"><input type="checkbox" class="col-puesto-chk accent-yellow-500" value="{t}" onchange="{"toggleTipoPuestoPersonalizado()" if t=="Otro" else ""}"> {t}</label>' for t in TIPOS_PUESTO])}
        </div>
      </div>
      <div id="col-puesto-custom-box" class="hidden">
        <label class="lbl">Especifica el puesto</label>
        <input id="col-puesto-custom" class="field" placeholder="Ej. Supervisor">
      </div>
      <div>
        <label class="lbl">Actividades a Evaluar (una por línea)</label>
        <textarea id="col-actividades" rows="8" class="field resize-none" placeholder="ETIQUETADO CORRECTO&#10;PRODUCTO CORRECTO&#10;PREPARACIÓN DE PEDIDO&#10;..."></textarea>
      </div>
      <button onclick="guardarColab()" class="w-full bg-yellow-500 hover:bg-yellow-400 text-gray-950 font-black py-2.5 rounded-xl text-xs uppercase font-custom tracking-wider transition-colors">Agregar Colaborador</button>
      <div id="res-colab" class="hidden p-2 text-center text-xs font-bold rounded-xl"></div>
    </div>
  </div>
</div>

<!-- HEADER -->
<div class="w-full max-w-[1600px] bg-gray-900/40 border border-gray-800 backdrop-blur-md rounded-2xl p-4 mb-4 shadow-2xl flex flex-col md:flex-row items-center justify-between gap-4">
  <div class="flex items-center">{logo_html}
    <div>
      <h1 class="text-3xl font-black tracking-wider text-white font-custom">LUQROSS AUTOMOTRIZ</h1>
      <p class="text-[10px] text-gray-500 tracking-widest font-bold uppercase mt-0.5">Gestión de Personal — Evaluaciones & KPIs</p>
    </div>
  </div>
  <div class="flex border-b border-gray-800 bg-gray-950/20 rounded-t-xl px-2 gap-1 overflow-x-auto self-end">
    <button onclick="switchTab('evaluacion')" id="btn-evaluacion" class="tab-btn">EVALUACIÓN DIARIA</button>
    <button onclick="switchTab('incidencias')" id="btn-incidencias" class="tab-btn">INCIDENCIAS</button>
    <button onclick="switchTab('kpi')" id="btn-kpi" class="tab-btn">KPIs</button>
    <button onclick="switchTab('horas')" id="btn-horas" class="tab-btn">REGISTRO DE HORAS</button>
    <button onclick="switchTab('colaboradores')" id="btn-colaboradores" class="tab-btn">COLABORADORES</button>
  </div>
</div>

<!-- KPI CARDS -->
<div class="w-full max-w-[1600px] grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">

  <!-- Promedio Global con selector de mes -->
  <div class="bg-gray-900/40 border border-gray-800 rounded-xl p-4 text-center shadow space-y-1">
    <p class="text-[9px] text-gray-500 uppercase font-bold tracking-wider">Promedio Global</p>
    <select id="kpi-mes-selector" onchange="actualizarPromedioGlobal()" class="w-full bg-gray-950 border border-gray-700 text-white text-[10px] rounded-lg px-2 py-1 outline-none focus:border-yellow-500 text-center">
      {"".join([f'<option value="{anio}-{mes}">{MESES_ES[mes-1]} {anio}</option>' for anio,mes in sorted(set((e["anio"],e["mes"]) for e in evaluaciones), reverse=True)] if evaluaciones else ['<option>Sin datos</option>'])}
    </select>
    <p id="kpi-prom-valor" class="text-2xl font-black {'text-emerald-400' if prom_global>=80 else 'text-yellow-400' if prom_global>=60 else 'text-rose-400'} font-custom">{prom_global}%</p>
    <p id="kpi-mes-label" class="text-[9px] text-gray-500">{MESES_ES[datetime.now().month-1]} {datetime.now().year}</p>
    <button onclick="descargarExcelEval()" class="w-full mt-1 bg-emerald-700 hover:bg-emerald-600 text-white font-black text-[9px] py-1.5 rounded-lg uppercase font-custom tracking-wide transition-colors">
      📥 Descargar Excel
    </button>
  </div>

  <div class="bg-gray-900/40 border border-gray-800 rounded-xl p-4 text-center shadow">
    <p class="text-[9px] text-rose-400 uppercase font-bold tracking-wider mb-1">Incidencias Abiertas</p>
    <p class="text-2xl font-black text-rose-400 font-custom">{inc_abiertas}</p>
  </div>

  <div class="card p-4 text-center shadow cursor-pointer hover:bg-rose-900/20 transition-colors col-span-2 md:col-span-1" onclick="document.getElementById('modal-prep').classList.remove('hidden')">
    <p class="text-[9px] text-amber-400 uppercase font-bold tracking-wider mb-1">Meta Preparación <span class="text-[8px] text-gray-600">(clic para desglose)</span></p>
    <p class="text-2xl font-black {color_prep} font-custom">{f'{pct_cumplimiento_prep}%' if pct_cumplimiento_prep is not None else '—'}</p>
    <div class="flex justify-center gap-3 mt-1.5">
      <span class="text-[9px] text-gray-500">🚐 Local: <span class="font-bold text-blue-400">{f'{pct_local}%' if pct_local is not None else '—'}</span></span>
      <span class="text-[9px] text-gray-500">📦 Paq: <span class="font-bold text-purple-400">{f'{pct_paq}%' if pct_paq is not None else '—'}</span></span>
    </div>
    <p class="text-[8px] text-gray-600 mt-1">Rutas en tiempo (3h local / 5h paq)</p>
  </div>

  <!-- Rutas Óptimas → abre tarjetas de colaboradores -->
  <div class="bg-gray-900/40 border border-gray-800 rounded-xl p-4 text-center shadow cursor-pointer hover:bg-gray-900/60 transition-colors" onclick="abrirTarjetasColaboradores()">
    <p class="text-[9px] text-emerald-400 uppercase font-bold tracking-wider mb-1">Desempeño del Equipo <span class="text-[8px] text-gray-600">(clic)</span></p>
    <p class="text-2xl font-black text-emerald-400 font-custom">{prom_global}%</p>
    <p class="text-[9px] text-gray-500 mt-1">Ver tarjetas individuales</p>
  </div>
</div>

<div class="w-full max-w-[1600px] space-y-4">

<!-- ══ TAB EVALUACIÓN DIARIA ══════════════════════════════════════════ -->
<div id="tab-evaluacion" class="tab-content hidden space-y-4">

  <!-- Importar Excel -->
  <div class="bg-gray-900/20 border border-gray-800/50 backdrop-blur-sm rounded-2xl p-5 shadow-2xl">
    <div class="flex flex-col md:flex-row md:items-center gap-4">
      <div class="flex-1">
        <h3 class="text-xs font-bold text-emerald-400 font-custom uppercase mb-1">Importar Excel Masivo</h3>
        <p class="text-[10px] text-gray-500">Sube el archivo con todas las pestañas (una por colaborador). El sistema detecta automáticamente el año, mes, días y calificaciones.</p>
      </div>
      <div class="flex flex-col sm:flex-row gap-3 items-start sm:items-center shrink-0">
        <input type="file" id="excel-import-file" accept=".xlsx,.xls" class="text-xs text-gray-400 bg-gray-950 border border-gray-800 rounded-xl p-2.5 w-full sm:w-auto">
        <button onclick="importarExcel()" class="bg-emerald-600 hover:bg-emerald-500 text-white font-black px-6 py-2.5 rounded-xl text-xs uppercase font-custom tracking-wider transition-colors whitespace-nowrap shadow-md">
          Importar
        </button>
      </div>
    </div>
    <div id="res-import" class="hidden mt-4 space-y-2"></div>
  </div>

  <!-- Selector colaborador + mes -->
  <div class="bg-gray-900/20 border border-gray-800/50 backdrop-blur-sm rounded-2xl p-5 shadow-2xl">
    <div class="flex flex-col md:flex-row gap-4 items-center">
      <!-- Foto colaborador -->
      <div id="eval-foto-box" class="shrink-0 hidden">
        <div class="relative">
          <img id="eval-foto-img" src="" onerror="this.style.display='none';document.getElementById('eval-foto-init').style.display='flex'"
            class="w-20 h-20 rounded-2xl object-cover border-2 border-yellow-500/40 shadow-lg">
          <div id="eval-foto-init" class="w-20 h-20 rounded-2xl bg-yellow-500/10 border-2 border-yellow-500/30 items-center justify-center text-yellow-500 font-black text-3xl hidden"></div>
          <div id="eval-foto-badge" class="absolute -bottom-1 -right-1 bg-gray-900 border border-gray-700 rounded-full px-1.5 py-0.5 text-[8px] font-bold text-gray-400 uppercase"></div>
        </div>
      </div>
      <div class="flex-1 flex flex-col md:flex-row gap-3 items-end w-full">
        <div class="flex-1 w-full">
          <label class="lbl">Colaborador</label>
          <select id="eval-colab-sel" onchange="cargarCalendario(); actualizarFotoEval();" class="field">
            <option value="">-- Selecciona colaborador --</option>
            {opts_colab_activos}
          </select>
        </div>
        <div class="w-44">
          <label class="lbl">Mes</label>
          <input type="month" id="eval-mes" onchange="cargarCalendario()" class="field" value="{datetime.now().strftime('%Y-%m')}">
        </div>
        <button onclick="verTarjeta()" class="bg-yellow-500 hover:bg-yellow-400 text-gray-950 font-black px-5 py-2 rounded-xl text-xs uppercase font-custom tracking-wider transition-colors whitespace-nowrap">Ver Tarjeta</button>
      </div>
    </div>
  </div>

  <!-- Calendario evaluación -->
  <div class="bg-gray-900/20 border border-gray-800/50 backdrop-blur-sm rounded-2xl p-5 shadow-2xl space-y-4">
    <div class="flex justify-between items-center border-b border-gray-800 pb-2">
      <h3 class="text-xs font-bold text-gray-400 font-custom uppercase">Matriz de Evaluación Diaria</h3>
      <div class="flex gap-2 flex-wrap">
        <span class="text-[9px] px-2 py-0.5 rounded score-badge-5 font-bold">5 Excelente</span>
        <span class="text-[9px] px-2 py-0.5 rounded score-badge-4 font-bold">4 Satisfactorio</span>
        <span class="text-[9px] px-2 py-0.5 rounded score-badge-3 font-bold">3 En observación</span>
        <span class="text-[9px] px-2 py-0.5 rounded score-badge-2 font-bold">2 Necesita mejora</span>
        <span class="text-[9px] px-2 py-0.5 rounded score-badge-1 font-bold">1 Insuficiente</span>
        <span class="text-[9px] px-2 py-0.5 rounded score-badge-0 font-bold">0 Crítico</span>
      </div>
    </div>
    <div id="cal-container" class="overflow-x-auto">
      <p class="text-xs text-gray-500 text-center py-8">Selecciona un colaborador y mes para ver la matriz.</p>
    </div>
    <div class="flex justify-end gap-3 pt-2 border-t border-gray-800">
      <button onclick="guardarEvaluacion()" id="btn-guardar-eval" class="hidden bg-yellow-500 hover:bg-yellow-400 text-gray-950 font-black px-8 py-2.5 rounded-xl text-xs uppercase font-custom tracking-wider transition-colors shadow-md">Guardar Evaluación del Mes</button>
    </div>
    <div id="res-eval" class="hidden p-2.5 text-center text-xs font-bold rounded-xl"></div>
  </div>
</div>

<!-- ══ TAB INCIDENCIAS ════════════════════════════════════════════════ -->
<div id="tab-incidencias" class="tab-content hidden">
  <div class="grid grid-cols-1 lg:grid-cols-5 gap-4">
    <div class="lg:col-span-2 bg-gray-900/20 border border-gray-800/50 backdrop-blur-sm rounded-2xl p-6 shadow-2xl space-y-3">
      <h3 class="text-xs font-bold text-rose-400 border-b border-gray-800 pb-2 font-custom uppercase">Registrar Incidencia</h3>
      <div>
        <label class="lbl">Ingresado por</label>
        <select id="inc-ingresado-por" class="field">
          <option value="">-- Selecciona --</option>
          <option>NALLELI GUAJARDO</option>
          <option>SEBASTIAN YAEL</option>
          <option>LESLY</option>
          <option>IRMA</option>
          <option>DIEGO</option>
          <option>VICTOR</option>
        </select>
      </div>
      <div>
        <label class="lbl">Colaboradores Involucrados</label>
        <div id="inc-colab-box" class="field" style="max-height:140px;overflow-y:auto;display:flex;flex-direction:column;gap:6px;">{opts_colab_chk}</div>
      </div>
      <div>
        <label class="lbl">Tipo de Incidencia</label>
        <select id="inc-tipo" onchange="toggleTipoPersonalizado()" class="field">
          {"".join([f'<option>{t}</option>' for t in TIPOS_INC])}
        </select>
      </div>
      <div id="inc-tipo-custom-box" class="hidden">
        <label class="lbl">Especifica la incidencia</label>
        <input id="inc-tipo-custom" class="field" placeholder="Describe el tipo de incidencia...">
      </div>
      <div><label class="lbl">Responsable que Reporta</label><input id="inc-resp" class="field" placeholder="Nombre del supervisor"></div>
      <div><label class="lbl">Observaciones</label><textarea id="inc-obs" rows="3" class="field resize-none" placeholder="Describe la incidencia..."></textarea></div>
      <button onclick="guardarInc()" class="w-full bg-rose-500 hover:bg-rose-400 text-white font-black py-2.5 rounded-xl text-xs uppercase font-custom tracking-wider transition-colors">Registrar</button>

      <!-- Foto de evidencia -->
      <div class="border-t border-gray-800 pt-3 space-y-2">
        <p class="text-[10px] text-gray-500 uppercase font-bold">Foto de Evidencia (opcional)</p>
        <p class="text-[9px] text-gray-600">Puedes tomar la foto antes o después de registrar. Se asocia por ID de incidencia.</p>
        <div class="flex gap-2">
          <label class="flex-1 flex items-center justify-center gap-2 bg-gray-800 hover:bg-gray-700 border border-gray-700 hover:border-yellow-500/50 text-gray-300 hover:text-white font-bold py-2.5 rounded-xl text-xs uppercase font-custom cursor-pointer transition-colors">
            📷 Tomar / Subir Foto
            <input type="file" id="inc-foto-input" accept="image/*" capture="environment" class="hidden" onchange="previsualizarFotoInc(this)">
          </label>
        </div>
        <div id="inc-foto-preview" class="hidden">
          <img id="inc-foto-img" class="w-full rounded-xl border border-gray-700 max-h-40 object-cover">
          <button onclick="limpiarFotoInc()" class="mt-1 text-[10px] text-rose-400 hover:text-rose-300 font-bold">✕ Quitar foto</button>
        </div>
      </div>

      <div id="res-inc" class="hidden p-2 text-center text-xs font-bold rounded-xl"></div>
    </div>
    <div class="lg:col-span-3 bg-gray-900/20 border border-gray-800/50 backdrop-blur-sm rounded-2xl p-6 shadow-2xl space-y-3">
      <div class="border-b border-gray-800 pb-3 space-y-2">
        <div class="flex justify-between items-center flex-wrap gap-2">
          <h3 class="text-xs font-bold text-gray-400 font-custom uppercase">Historial de Incidencias</h3>
          <button onclick="limpiarFiltrosInc()" class="text-[9px] text-gray-500 hover:text-yellow-500 font-bold uppercase transition-colors">✕ Limpiar filtros</button>
        </div>
        <div class="grid grid-cols-2 md:grid-cols-4 gap-2">
          <div>
            <label class="lbl">Colaborador</label>
            <select id="filtro-inc-colab" onchange="filtrarInc()" class="bg-gray-950 border border-gray-800 text-white text-xs rounded-lg px-2 py-1.5 outline-none w-full focus:border-yellow-500">
              <option value="">-- Todos --</option>
              {opts_colab_todos}
            </select>
          </div>
          <div>
            <label class="lbl">Tipo</label>
            <select id="filtro-inc-tipo" onchange="filtrarInc()" class="bg-gray-950 border border-gray-800 text-white text-xs rounded-lg px-2 py-1.5 outline-none w-full focus:border-yellow-500">
              <option value="">-- Todos --</option>
              {"".join([f'<option>{t}</option>' for t in TIPOS_INC])}
            </select>
          </div>
          <div>
            <label class="lbl">Ingresado por</label>
            <select id="filtro-inc-ingresado" onchange="filtrarInc()" class="bg-gray-950 border border-gray-800 text-white text-xs rounded-lg px-2 py-1.5 outline-none w-full focus:border-yellow-500">
              <option value="">-- Todos --</option>
              <option>NALLELI GUAJARDO</option>
              <option>SEBASTIAN YAEL</option>
              <option>LESLY</option>
              <option>IRMA</option>
              <option>DIEGO</option>
              <option>VICTOR</option>
            </select>
          </div>
          <div>
            <label class="lbl">Estatus</label>
            <select id="filtro-inc-est" onchange="filtrarInc()" class="bg-gray-950 border border-gray-800 text-white text-xs rounded-lg px-2 py-1.5 outline-none w-full focus:border-yellow-500">
              <option value="">-- Todos --</option>
              <option>Abierta</option>
              <option>Resuelta</option>
            </select>
          </div>
        </div>
        <div>
          <label class="lbl">Buscar texto libre</label>
          <input id="filtro-inc-texto" oninput="filtrarInc()" type="text" placeholder="Buscar en observaciones, responsable, solución..." class="bg-gray-950 border border-gray-800 text-white text-xs rounded-lg px-3 py-1.5 outline-none w-full focus:border-yellow-500 transition-colors">
        </div>
        <div id="filtro-inc-contador" class="text-[9px] text-gray-600 font-bold"></div>
      </div>
      <div class="overflow-y-auto max-h-[440px] border border-gray-900 rounded-xl bg-gray-950/40">
        <table class="w-full text-left border-collapse">
          <thead><tr class="bg-gray-900/60 text-[10px] text-gray-400 border-b border-gray-800 uppercase font-bold tracking-wide sticky top-0 backdrop-blur">
            <th class="px-3 py-2">Fecha</th><th class="px-3 py-2">Colaborador</th><th class="px-3 py-2">Tipo</th>
            <th class="px-3 py-2">Responsable</th><th class="px-3 py-2">Observaciones</th><th class="px-3 py-2 text-center">Foto</th><th class="px-3 py-2 text-center">Estatus</th>
          </tr></thead>
          <tbody id="body-inc">{filas_inc}</tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<!-- ══ TAB KPIs ════════════════════════════════════════════════════════ -->
<div id="tab-kpi" class="tab-content hidden space-y-4">

  <!-- Filtros con calendario interactivo -->
  <div class="bg-gray-900/20 border border-gray-800/50 backdrop-blur-sm rounded-2xl p-5 shadow-2xl">
    <div class="flex flex-col lg:flex-row gap-5 items-start">

      <!-- Calendario de meses -->
      <div class="shrink-0">
        <p class="lbl mb-2">Selecciona Mes</p>
        <div class="bg-gray-950/70 border border-gray-800 rounded-xl p-3 w-64">
          <!-- Navegador de año -->
          <div class="flex items-center justify-between mb-3">
            <button onclick="cambiarAnioKpi(-1)" class="text-gray-400 hover:text-yellow-500 font-black px-2 py-1 rounded-lg hover:bg-gray-800 transition-colors text-sm">‹</button>
            <span id="kpi-anio-display" class="text-sm font-black text-white font-custom">{datetime.now().year}</span>
            <button onclick="cambiarAnioKpi(1)" class="text-gray-400 hover:text-yellow-500 font-black px-2 py-1 rounded-lg hover:bg-gray-800 transition-colors text-sm">›</button>
          </div>
          <!-- Grid de meses -->
          <div class="grid grid-cols-3 gap-1.5" id="kpi-meses-grid">
            <!-- Se genera con JS -->
          </div>
        </div>
      </div>

      <!-- Separador -->
      <div class="hidden lg:block w-px bg-gray-800 self-stretch"></div>

      <!-- Filtro colaborador + resumen -->
      <div class="flex-1 space-y-3 w-full">
        <div>
          <label class="lbl">Colaborador</label>
          <select id="kpi-colab-sel" onchange="renderKpiCharts()" class="field">
            <option value="">-- Todos los colaboradores --</option>
            {opts_colab_todos}
          </select>
        </div>
        <!-- Chips de info del filtro activo -->
        <div class="flex gap-2 flex-wrap">
          <span class="text-[10px] bg-yellow-500/10 border border-yellow-500/20 text-yellow-500 px-3 py-1 rounded-full font-bold">
            📅 Mostrando: <span id="kpi-filtro-label">{datetime.now().strftime('%B %Y')}</span>
          </span>
          <button onclick="limpiarFiltroKpi()" class="text-[10px] bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-400 hover:text-white px-3 py-1 rounded-full font-bold transition-colors">
            Limpiar filtro
          </button>
        </div>
      </div>
    </div>
  </div>

  <!-- Cards dinámicas del colaborador seleccionado -->
  <div id="kpi-cards-colab" class="hidden grid grid-cols-2 md:grid-cols-5 gap-3">
    <div class="bg-gray-900/40 border border-gray-800 rounded-xl p-4 text-center">
      <p class="text-[9px] text-gray-500 uppercase font-bold tracking-wider mb-1">Días Evaluados</p>
      <p id="kc-dias" class="text-2xl font-black text-yellow-500 font-custom">—</p>
    </div>
    <div class="bg-gray-900/40 border border-gray-800 rounded-xl p-4 text-center">
      <p class="text-[9px] text-gray-500 uppercase font-bold tracking-wider mb-1">Promedio del Mes</p>
      <p id="kc-prom" class="text-2xl font-black text-emerald-400 font-custom">—</p>
    </div>
    <div class="bg-gray-900/40 border border-gray-800 rounded-xl p-4 text-center">
      <p class="text-[9px] text-rose-400 uppercase font-bold tracking-wider mb-1">Incidencias</p>
      <p id="kc-inc" class="text-2xl font-black text-rose-400 font-custom">—</p>
    </div>
    <div class="bg-gray-900/40 border border-gray-800 rounded-xl p-4 text-center">
      <p class="text-[9px] text-amber-400 uppercase font-bold tracking-wider mb-1">Meta Preparación</p>
      <p id="kc-prep" class="text-2xl font-black text-amber-400 font-custom">—</p>
      <p id="kc-prep-sub" class="text-[9px] text-gray-600 mt-1">rutas en tiempo</p>
    </div>
    <div class="bg-gray-900/40 border border-gray-800 rounded-xl p-4 text-center">
      <p class="text-[9px] text-blue-400 uppercase font-bold tracking-wider mb-1">Mejor Actividad</p>
      <p id="kc-mejor" class="text-xs font-black text-blue-400 font-custom leading-tight">—</p>
    </div>
  </div>

  <!-- Gráficas -->
  <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
    <div class="bg-gray-900/20 border border-gray-800/50 rounded-2xl p-5 shadow-xl flex flex-col">
      <p class="text-[11px] font-bold text-yellow-500 uppercase mb-3 font-custom">% Promedio por Colaborador</p>
      <div class="h-[300px]"><canvas id="chartEval"></canvas></div>
    </div>
    <div class="bg-gray-900/20 border border-gray-800/50 rounded-2xl p-5 shadow-xl flex flex-col">
      <p class="text-[11px] font-bold text-blue-400 uppercase mb-3 font-custom">Desglose por Categoría LUQROSS</p>
      <div class="h-[300px]"><canvas id="chartActividades"></canvas></div>
    </div>
  </div>
  <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
    <div class="bg-gray-900/20 border border-gray-800/50 rounded-2xl p-5 shadow-xl flex flex-col">
      <p class="text-[11px] font-bold text-rose-400 uppercase mb-3 font-custom">Incidencias por Colaborador</p>
      <div class="h-[260px]"><canvas id="chartInc"></canvas></div>
    </div>
    <div class="bg-gray-900/20 border border-gray-800/50 rounded-2xl p-5 shadow-xl flex flex-col">
      <p class="text-[11px] font-bold text-emerald-400 uppercase mb-3 font-custom">Tendencia Diaria del Mes</p>
      <div class="h-[260px]"><canvas id="chartTendencia"></canvas></div>
    </div>
  </div>
  <!-- Gráfica tiempos de preparación desde Registro de Horas -->
  <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
    <div class="bg-gray-900/20 border border-gray-800/50 rounded-2xl p-5 shadow-xl flex flex-col">
      <p class="text-[11px] font-bold text-amber-400 uppercase mb-1 font-custom">Tiempo Promedio de Preparación (min)</p>
      <p class="text-[9px] text-gray-500 mb-3">Calculado automáticamente desde Registro de Horas · Meta: Ruta Local 180 min · Paquetería 300 min</p>
      <div class="h-[260px]"><canvas id="chartTiemposPrep"></canvas></div>
    </div>
    <div class="bg-gray-900/20 border border-gray-800/50 rounded-2xl p-5 shadow-xl flex flex-col">
      <p class="text-[11px] font-bold text-emerald-400 uppercase mb-1 font-custom">Eficiencia de Preparación %</p>
      <p class="text-[9px] text-gray-500 mb-3">Verde ≥ 100% · Amarillo ≥ 80% · Rojo &lt; 80%</p>
      <div class="h-[260px]"><canvas id="chartEficPrep"></canvas></div>
    </div>
  </div>
</div>

<!-- ══ TAB REGISTRO DE HORAS ══════════════════════════════════════════ -->
<div id="tab-horas" class="tab-content hidden">
  <div class="grid grid-cols-1 lg:grid-cols-5 gap-4">
    <div class="lg:col-span-2 bg-gray-900/20 border border-gray-800/50 backdrop-blur-sm rounded-2xl p-6 shadow-2xl space-y-3">
      <h3 class="text-xs font-bold text-blue-400 border-b border-gray-800 pb-2 font-custom uppercase">Registrar Horas del Día</h3>

      <div><label class="lbl">Operador</label>
        <select id="hr-colab" class="field">
          <option value="">-- Selecciona --</option>
          {opts_colab_activos}
          <option>ALMACEN</option>
        </select>
      </div>

      <div><label class="lbl">Fecha</label>
        <input id="hr-fecha" type="date" class="field" value="{datetime.now().strftime('%Y-%m-%d')}">
      </div>

      <div class="grid grid-cols-2 gap-3">
        <div><label class="lbl">Tipo de Ruta</label>
          <select id="hr-tipo-ruta" class="field">
            <option value="Ruta Local">Ruta Local (meta 3h)</option>
            <option value="Paquetería">Paquetería (meta 5h)</option>
          </select>
        </div>
        <div><label class="lbl">Destino</label>
          <input id="hr-destino" type="text" class="field" placeholder="Ej. CDMX, Toluca...">
        </div>
      </div>

      <div class="bg-gray-950/50 border border-gray-800 rounded-xl p-3 space-y-2">
        <p class="text-[9px] text-yellow-500 font-bold uppercase">Horarios del Día</p>
        <div class="grid grid-cols-1 gap-2">
          <div class="flex items-center justify-between gap-2">
            <div class="flex-1">
              <label class="lbl">Conocimiento de Ruta <span class="text-rose-400">máx 9:40</span></label>
              <input id="hr-0" type="time" class="field font-mono text-sm">
            </div>
          </div>
          <div class="flex items-center justify-between gap-2">
            <div class="flex-1">
              <label class="lbl">Entrega de Etiquetas <span class="text-rose-400">máx 9:55</span></label>
              <input id="hr-1" type="time" class="field font-mono text-sm">
            </div>
          </div>
          <div class="flex items-center justify-between gap-2">
            <div class="flex-1">
              <label class="lbl">Término de Preparación <span class="text-rose-400">máx 10:40</span></label>
              <input id="hr-2" type="time" class="field font-mono text-sm">
            </div>
          </div>
          <div class="flex items-center justify-between gap-2">
            <div class="flex-1">
              <label class="lbl">Entrega de Papeles <span class="text-rose-400">máx 11:00</span></label>
              <input id="hr-3" type="time" class="field font-mono text-sm">
            </div>
          </div>
          <div class="flex items-center justify-between gap-2">
            <div class="flex-1">
              <label class="lbl">Hora de Salida <span class="text-rose-400">máx 12:10</span></label>
              <input id="hr-4" type="time" class="field font-mono text-sm">
            </div>
          </div>
          <div>
            <label class="lbl">Hora de Regreso</label>
            <input id="hr-5" type="time" class="field font-mono text-sm">
          </div>
        </div>
      </div>

      <button onclick="guardarHoras()" class="w-full bg-blue-600 hover:bg-blue-500 text-white font-black py-2.5 rounded-xl text-xs uppercase font-custom transition-colors">Registrar Horas</button>
      <div id="res-hr" class="hidden p-2 text-center text-xs font-bold rounded-xl"></div>
    </div>

    <div class="lg:col-span-3 bg-gray-900/20 border border-gray-800/50 backdrop-blur-sm rounded-2xl p-6 shadow-2xl space-y-3">
      <h3 class="text-xs font-bold text-gray-400 border-b border-gray-800 pb-2 font-custom uppercase">Historial — Clic para ver detalle</h3>
      <div class="overflow-y-auto max-h-[560px] border border-gray-900 rounded-xl bg-gray-950/40">
        <table class="w-full text-left border-collapse">
          <thead><tr class="bg-gray-900/60 text-[10px] text-gray-400 border-b border-gray-800 uppercase font-bold tracking-wide sticky top-0 backdrop-blur">
            <th class="px-3 py-2">Fecha</th>
            <th class="px-3 py-2">Operador</th>
            <th class="px-3 py-2">Tipo</th>
            <th class="px-3 py-2">Destino</th>
            <th class="px-3 py-2 text-center">T. Prep</th>
            <th class="px-3 py-2 text-center">Efic.</th>
            <th class="px-3 py-2 text-center">Cumplimiento</th>
            <th class="px-3 py-2 text-center">Regreso</th>
          </tr></thead>
          <tbody>{filas_horas}</tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<!-- ══ TAB COLABORADORES ══════════════════════════════════════════════ -->
<div id="tab-colaboradores" class="tab-content hidden space-y-4">
  <div class="flex justify-between items-center">
    <h3 class="text-xs font-bold text-gray-400 font-custom uppercase">Equipo Registrado ({len(colaboradores_activos)} colaboradores)</h3>
    <div class="flex items-center gap-2">
      <button onclick="document.getElementById('box-inactivos').classList.toggle('hidden')" class="text-[10px] text-gray-500 hover:text-yellow-500 font-bold uppercase transition-colors">Ver inactivos ({len(colaboradores_inactivos)})</button>
      <button onclick="document.getElementById('modal-colab').classList.remove('hidden')" class="bg-yellow-500 hover:bg-yellow-400 text-gray-950 font-black px-5 py-2 rounded-xl text-xs uppercase font-custom tracking-wider transition-colors">+ Agregar Colaborador</button>
    </div>
  </div>

  <div id="box-inactivos" class="hidden bg-gray-900/20 border border-gray-800 rounded-xl p-4 space-y-2">
    <p class="text-[10px] text-gray-500 uppercase font-bold">Colaboradores desactivados — su historial se conserva; reactívalos si fue un error o vuelven a trabajar contigo.</p>
    {''.join([f'''
    <div class="flex items-center justify-between bg-gray-950/40 rounded-lg px-3 py-2">
      <span class="text-xs text-gray-400 uppercase">{c["nombre"]}</span>
      <button onclick="reactivarColab('{js_str(c["nombre"])}')" class="text-[10px] font-bold text-emerald-400 hover:text-emerald-300 underline">Reactivar</button>
    </div>''' for c in colaboradores_inactivos]) or '<p class="text-[10px] text-gray-600">No hay colaboradores desactivados.</p>'}
  </div>

  <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4" id="grid-colaboradores">
    {''.join([f"""
    <div onclick="abrirHistorial('{js_str(c['nombre'])}')" class="bg-gray-900/30 border border-gray-800 rounded-2xl p-4 space-y-3 shadow cursor-pointer hover:border-yellow-500/40 hover:bg-gray-900/50 transition-all duration-200 group">
      <div class="flex items-center gap-3">
        <div class="relative">
          <img src="/static/fotos/{c['nombre']}.jpg" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'" class="w-20 h-20 rounded-full object-cover border-2 border-yellow-500/40 shadow group-hover:border-yellow-500 transition-colors">
          <div class="w-20 h-20 rounded-full bg-yellow-500/10 border-2 border-yellow-500/30 items-center justify-center text-yellow-500 font-black text-2xl hidden" style="display:none">{c['nombre'][0].upper()}</div>
          <label onclick="event.stopPropagation()" class="absolute -bottom-1 -right-1 bg-gray-800 hover:bg-yellow-500 text-gray-400 hover:text-gray-900 border border-gray-700 rounded-full w-5 h-5 flex items-center justify-center cursor-pointer transition-colors text-[10px]" title="Cambiar foto">
            📷<input type="file" accept=".jpg,.jpeg,.png" onchange="subirFoto('{js_str(c['nombre'])}',this)" class="hidden">
          </label>
        </div>
        <div class="flex-1">
          <p class="text-sm font-black text-white group-hover:text-yellow-400 transition-colors">{c['nombre']}</p>
          <p class="text-[10px] text-gray-500 uppercase">{", ".join(c.get('puestos') or [])}</p>
          <p class="text-[10px] text-yellow-500/70">{len(c.get('actividades',[]))} actividades</p>
        </div>
        <div class="flex flex-col items-end gap-1">
          <button onclick="event.stopPropagation();eliminarColab('{js_str(c['nombre'])}')" class="text-gray-700 hover:text-rose-400 transition-colors font-bold text-sm">✕</button>
          <span class="text-[9px] text-gray-600 group-hover:text-yellow-500/60 transition-colors font-bold">Ver historial →</span>
        </div>
      </div>
      <div class="bg-gray-950/40 rounded-xl p-2 max-h-32 overflow-y-auto space-y-0.5">
        {''.join([f'<p class="text-[10px] text-gray-400 font-medium">• {act}</p>' for act in c.get('actividades',[])])}
      </div>
    </div>""" for c in colaboradores_activos])}
  </div>
</div>

</div><!-- /main -->

<script>
const COLABS = {colabs_json};
const EVALS  = {evals_json};
const INCIDENCIAS_DATA = {json.dumps(incidencias)};
const HORAS_DATA       = {json.dumps(horas)};
const ACTIVIDADES_HORAS = {json.dumps(ACTIVIDADES_HORAS)};
const META_LOCAL = {META_LOCAL};
const META_PAQ   = {META_PAQ};
const PENALIZACION_POR_INCIDENCIA_PCT = {PENALIZACION_POR_INCIDENCIA_PCT};
const COLABS_DATA = COLABS;
const EVALS_DATA  = EVALS;
const MESES_ES_JS = ['Enero','Febrero','Marzo','Abril','Mayo','Junio','Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre'];

// Incidencias de un colaborador en un año/mes dado (para restar del promedio global).
function incidenciasDelMes(nombre, anio, mes) {{
  return INCIDENCIAS_DATA.filter(i => (i.colaboradores||[]).includes(nombre) && i.anio===anio && i.mes===mes).length;
}}

// ── Descargar Excel evaluaciones ──────────────────────────────────────────
async function descargarExcelEval() {{
  const sel = document.getElementById('kpi-mes-selector').value;
  if (!sel || sel === 'Sin datos') {{ alert('Selecciona un mes primero.'); return; }}
  const [anio, mes] = sel.split('-');
  const btn = document.querySelector('[onclick="descargarExcelEval()"]');
  const textoOriginal = btn.innerText;
  btn.innerText = '⏳ Generando...'; btn.disabled = true;
  try {{
    const r = await fetch(`/api/exportar-evaluaciones?anio=${{anio}}&mes=${{mes}}`);
    if (!r.ok) {{ alert('Error al generar el Excel.'); return; }}
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `Evaluaciones_LUQROSS_${{MESES_ES_JS[parseInt(mes)-1]}}_${{anio}}.xlsx`;
    a.click();
    URL.revokeObjectURL(url);
  }} finally {{
    btn.innerText = textoOriginal; btn.disabled = false;
  }}
}}

// ── Promedio Global por mes ───────────────────────────────────────────────
function actualizarPromedioGlobal() {{
  const sel = document.getElementById('kpi-mes-selector').value;
  if (!sel || sel === 'Sin datos') return;
  const [anio, mes] = sel.split('-').map(Number);
  const evsMes = EVALS_DATA.filter(e => e.anio === anio && e.mes === mes);
  const prom = evsMes.length > 0
    ? Math.round(evsMes.reduce((a,b) => a + b.pct, 0) / evsMes.length * 10) / 10
    : 0;
  const el = document.getElementById('kpi-prom-valor');
  el.innerText = prom + '%';
  el.className = 'text-2xl font-black font-custom ' + (prom>=80?'text-emerald-400':prom>=60?'text-yellow-400':'text-rose-400');
  document.getElementById('kpi-mes-label').innerText = MESES_ES_JS[mes-1] + ' ' + anio;
}}

// ── Tarjetas equipo ───────────────────────────────────────────────────────
function abrirTarjetasColaboradores() {{
  document.getElementById('modal-equipo').classList.remove('hidden');
  renderTarjetasEquipo();
}}

function renderTarjetasEquipo() {{
  const sel = document.getElementById('equipo-mes-selector').value;
  if (!sel || sel === 'Sin datos') return;
  const [anio, mes] = sel.split('-').map(Number);
  const container = document.getElementById('equipo-tarjetas-container');
  container.innerHTML = '<p class="text-center text-gray-500 text-xs py-4 col-span-3">Calculando...</p>';

  setTimeout(() => {{
    container.innerHTML = '';
    COLABS_DATA.forEach(colab => {{
      const evsMes = EVALS_DATA.filter(e => e.colaborador === colab.nombre && e.anio === anio && e.mes === mes);
      if (evsMes.length === 0) return;
      const actMap = {{}};
      evsMes.forEach(e => {{
        if (e.calificaciones) Object.entries(e.calificaciones).forEach(([act, val]) => {{
          if (!actMap[act]) actMap[act] = [];
          actMap[act].push(val);
        }});
      }});
      const actProms = Object.entries(actMap).map(([act, vals]) => ({{
        act, pct: Math.round((vals.reduce((a,b)=>a+b,0)/vals.length/5)*100)
      }})).sort((a,b) => b.pct - a.pct);
      const promTotal = actProms.length > 0
        ? Math.round(actProms.reduce((a,b)=>a+b.pct,0)/actProms.length)
        : 0;
      const colorTotal = promTotal>=80?'text-emerald-400':promTotal>=60?'text-yellow-400':'text-rose-400';
      const filasActs = actProms.map(a => {{
        const c = a.pct>=80?'text-emerald-400':a.pct>=60?'text-yellow-400':'text-rose-400';
        return `<tr class="border-b border-gray-800/50">
          <td class="py-1.5 pr-3 text-[10px] text-gray-300 uppercase">${{a.act}}</td>
          <td class="py-1.5 text-right font-black text-[11px] ${{c}}">${{a.pct}}%</td>
        </tr>`;
      }}).join('');
      container.innerHTML += `
      <div class="bg-gray-900/60 border border-gray-700 rounded-2xl p-4 space-y-3">
        <div class="flex items-center gap-3">
          <img src="/static/fotos/${{colab.nombre}}.jpg"
            onerror="this.style.display='none';this.nextElementSibling.style.display='flex'"
            class="w-16 h-16 rounded-full object-cover border-2 border-yellow-500/40 shrink-0">
          <div class="w-16 h-16 rounded-full bg-yellow-500/10 border-2 border-yellow-500/30 items-center justify-center text-yellow-500 font-black text-2xl shrink-0" style="display:none">${{colab.nombre[0]}}</div>
          <div class="flex-1 min-w-0">
            <p class="font-black text-white text-sm uppercase leading-tight">${{colab.nombre}}</p>
            <p class="text-[10px] text-gray-400 font-bold uppercase">${{(colab.puestos||[]).join(', ')}}</p>
            <p class="text-[10px] text-yellow-500">${{MESES_ES_JS[mes-1]}} ${{anio}}</p>
          </div>
          <div class="text-right shrink-0">
            <p class="text-[9px] text-gray-500 uppercase font-bold">TOTAL</p>
            <p class="text-2xl font-black ${{colorTotal}} font-custom">${{promTotal}}%</p>
          </div>
        </div>
        <table class="w-full border-collapse">
          <thead><tr class="border-b border-gray-700">
            <th class="text-left text-[9px] text-gray-500 uppercase font-bold py-1">Actividad</th>
            <th class="text-right text-[9px] text-gray-500 uppercase font-bold py-1">Puntaje</th>
          </tr></thead>
          <tbody>${{filasActs}}</tbody>
        </table>
        <div class="flex gap-2 text-[9px] flex-wrap pt-1 border-t border-gray-800">
          <span class="bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 px-2 py-0.5 rounded-full font-bold">≥80% Excelente</span>
          <span class="bg-blue-500/10 text-blue-400 border border-blue-500/20 px-2 py-0.5 rounded-full font-bold">60-79% En observación</span>
          <span class="bg-rose-500/10 text-rose-400 border border-rose-500/20 px-2 py-0.5 rounded-full font-bold">&lt;60% Crítico</span>
        </div>
      </div>`;
    }});
    if (!container.innerHTML) {{
      container.innerHTML = '<p class="text-center text-gray-500 text-xs py-8 col-span-3">No hay evaluaciones para este mes.</p>';
    }}
  }}, 50);
}}

// ── Importar Excel masivo ─────────────────────────────────────────────────
async function importarExcel() {{
  const fileInput = document.getElementById('excel-import-file');
  const resDiv    = document.getElementById('res-import');
  if (!fileInput.files || !fileInput.files[0]) {{
    resDiv.innerHTML = '<p class="text-xs text-rose-400 font-bold">⚠ Selecciona un archivo Excel primero.</p>';
    resDiv.classList.remove('hidden');
    return;
  }}
  resDiv.innerHTML = '<p class="text-xs text-yellow-500 font-bold animate-pulse">⏳ Procesando archivo, espera...</p>';
  resDiv.classList.remove('hidden');

  const fd = new FormData();
  fd.append('file', fileInput.files[0]);

  try {{
    const r = await fetch('/api/importar-excel', {{ method: 'POST', body: fd }});
    const d = await r.json();

    if (d.status === 'success') {{
      let html = `<div class="bg-emerald-500/10 border border-emerald-500/30 rounded-xl p-4 space-y-3">
        <p class="text-xs font-black text-emerald-400">✓ ${{d.message}}</p>
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-2">`;
      d.resumen.forEach(r => {{
        html += `<div class="flex items-center justify-between bg-gray-950/60 rounded-lg px-3 py-2 text-[11px]">
          <span class="font-bold text-white uppercase">${{r.colaborador}}</span>
          <span class="text-emerald-400 font-black">${{r.dias}} días</span>
        </div>`;
      }});
      html += '</div>';
      if (d.errores && d.errores.length > 0) {{
        html += `<div class="border-t border-gray-800 pt-2 space-y-1">
          <p class="text-[10px] font-bold text-orange-400 uppercase">Advertencias:</p>
          ${{d.errores.map(e => `<p class="text-[10px] text-orange-400">⚠ ${{e}}</p>`).join('')}}
        </div>`;
      }}
      html += `<button onclick="location.reload()" class="w-full bg-emerald-600 hover:bg-emerald-500 text-white font-black py-2 rounded-xl text-xs uppercase font-custom transition-colors">Recargar para ver datos</button>`;
      html += '</div>';
      resDiv.innerHTML = html;
    }} else {{
      resDiv.innerHTML = `<p class="text-xs text-rose-400 font-bold bg-rose-500/10 border border-rose-500/20 rounded-xl p-3">⚠ Error: ${{d.message}}</p>`;
    }}
  }} catch(err) {{
    resDiv.innerHTML = '<p class="text-xs text-rose-400 font-bold bg-rose-500/10 border border-rose-500/20 rounded-xl p-3">⚠ Error de conexión.</p>';
  }}
}}

// ── Historial de calificaciones por colaborador ───────────────────────────
let mhChart = null;
function abrirHistorial(nombre) {{
  const colab = COLABS.find(c => c.nombre === nombre);
  if (!colab) return;

  const foto = document.getElementById('mh-foto');
  const init = document.getElementById('mh-init');
  foto.src = `/static/fotos/${{nombre}}.jpg`;
  foto.style.display = 'block'; init.style.display = 'none';
  init.innerText = nombre[0].toUpperCase();
  document.getElementById('mh-nombre').innerText = nombre;
  document.getElementById('mh-puesto').innerText = (colab.puestos||[]).join(', ');

  const evs = EVALS.filter(e => e.colaborador === nombre);
  const porMes = {{}};
  evs.forEach(e => {{
    const key = `${{e.anio}}-${{String(e.mes).padStart(2,'0')}}`;
    if (!porMes[key]) porMes[key] = {{ anio:e.anio, mes:e.mes, dias:[], pcts:[], cals:{{}} }};
    porMes[key].dias.push(e.dia);
    porMes[key].pcts.push(e.pct);
    Object.entries(e.calificaciones).forEach(([act,val]) => {{
      if (!porMes[key].cals[act]) porMes[key].cals[act] = [];
      porMes[key].cals[act].push(val);
    }});
  }});

  const MES = ['','Enero','Febrero','Marzo','Abril','Mayo','Junio','Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre'];
  const keys = Object.keys(porMes).sort();
  const totalDias = evs.length;
  const promGlobal = totalDias > 0 ? (evs.reduce((a,b)=>a+b.pct,0)/totalDias).toFixed(1) : 0;
  const cg = parseFloat(promGlobal)>=80?'text-emerald-400':parseFloat(promGlobal)>=60?'text-yellow-400':'text-rose-400';

  document.getElementById('mh-resumen').innerHTML = `
    <div class="bg-gray-950/60 border border-gray-800 rounded-xl p-3 text-center">
      <p class="text-[9px] text-gray-500 uppercase font-bold mb-1">Meses Evaluados</p>
      <p class="text-2xl font-black text-yellow-500 font-custom">${{keys.length}}</p>
    </div>
    <div class="bg-gray-950/60 border border-gray-800 rounded-xl p-3 text-center">
      <p class="text-[9px] text-gray-500 uppercase font-bold mb-1">Días Totales</p>
      <p class="text-2xl font-black text-blue-400 font-custom">${{totalDias}}</p>
    </div>
    <div class="bg-gray-950/60 border border-gray-800 rounded-xl p-3 text-center">
      <p class="text-[9px] text-gray-500 uppercase font-bold mb-1">Promedio Global</p>
      <p class="text-2xl font-black ${{cg}} font-custom">${{promGlobal}}%</p>
    </div>`;

  let filas = '';
  if (!keys.length) {{
    filas = '<tr><td colspan="5" class="p-6 text-center text-xs text-gray-500">No hay evaluaciones registradas aún.</td></tr>';
  }} else {{
    keys.forEach(key => {{
      const d = porMes[key];
      const prom = (d.pcts.reduce((a,b)=>a+b,0)/d.pcts.length).toFixed(1);
      const col  = parseFloat(prom)>=80?'text-emerald-400':parseFloat(prom)>=60?'text-yellow-400':'text-rose-400';
      const bg   = parseFloat(prom)>=80?'bg-emerald-500':parseFloat(prom)>=60?'bg-yellow-500':'bg-rose-500';
      let mejorAct='—',mejorPct=0,peorAct='—',peorPct=101;
      Object.entries(d.cals).forEach(([act,vals]) => {{
        const p=Math.round((vals.reduce((a,b)=>a+b,0)/vals.length/5)*100);
        if(p>mejorPct){{mejorPct=p;mejorAct=act;}}
        if(p<peorPct){{peorPct=p;peorAct=act;}}
      }});
      filas+=`<tr class="border-b border-gray-800/60 hover:bg-gray-900/30 transition-colors text-xs">
        <td class="px-4 py-3 font-bold text-white">${{MES[d.mes]}} ${{d.anio}}</td>
        <td class="px-4 py-3 text-center text-gray-400 font-bold">${{d.dias.length}}</td>
        <td class="px-4 py-3">
          <div class="flex items-center gap-2">
            <div class="w-20 h-2 bg-gray-800 rounded-full overflow-hidden"><div class="${{bg}} h-full rounded-full" style="width:${{Math.min(parseFloat(prom),100)}}%"></div></div>
            <span class="font-black ${{col}}">${{prom}}%</span>
          </div>
        </td>
        <td class="px-4 py-3 text-emerald-400 text-[10px] font-medium max-w-[130px] truncate" title="${{mejorAct}} (${{mejorPct}}%)">${{mejorAct}}</td>
        <td class="px-4 py-3 text-rose-400 text-[10px] font-medium max-w-[130px] truncate" title="${{peorAct}} (${{peorPct}}%)">${{peorAct}}</td>
      </tr>`;
    }});
  }}
  document.getElementById('mh-tabla').innerHTML = filas;

  const labels = keys.map(k=>{{ const d=porMes[k]; return MES[d.mes].substring(0,3)+' '+d.anio; }});
  const data   = keys.map(k=>{{ const d=porMes[k]; return (d.pcts.reduce((a,b)=>a+b,0)/d.pcts.length).toFixed(1); }});
  const ptColors = data.map(v=>parseFloat(v)>=80?'#10b981':parseFloat(v)>=60?'#f59e0b':'#f43f5e');
  if(mhChart) mhChart.destroy();
  mhChart = new Chart(document.getElementById('mh-chart'),{{
    type:'line',
    data:{{ labels, datasets:[{{ data, borderColor:'#eab308', backgroundColor:'rgba(234,179,8,0.08)',
      borderWidth:2, pointRadius:5, pointBackgroundColor:ptColors, pointBorderColor:'#fff',
      pointBorderWidth:1.5, fill:true, tension:0.3 }}] }},
    options:{{ responsive:true, maintainAspectRatio:false,
      plugins:{{ legend:{{display:false}}, tooltip:{{callbacks:{{label:ctx=>` ${{ctx.parsed.y}}%`}}}} }},
      scales:{{
        x:{{ ticks:{{color:'#6b7280',font:{{size:9}}}}, grid:{{color:'#1f2937'}} }},
        y:{{ min:0, max:100, ticks:{{color:'#6b7280',font:{{size:9}},callback:v=>v+'%'}}, grid:{{color:'#1f2937'}} }}
      }}
    }}
  }});
  document.getElementById('modal-historial').classList.remove('hidden');
}}

// ── Calendario interactivo KPI ────────────────────────────────────────────
const MESES_ES = ['Enero','Febrero','Marzo','Abril','Mayo','Junio','Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre'];
let kpiAnio = new Date().getFullYear();
let kpiMes  = new Date().getMonth() + 1; // 1-12

function iniciarCalendarioKpi() {{
  document.getElementById('kpi-anio-display').innerText = kpiAnio;
  const grid = document.getElementById('kpi-meses-grid');
  grid.innerHTML = '';
  MESES_ES.forEach((m, i) => {{
    const mesN = i + 1;
    const activo = mesN === kpiMes;
    const btn = document.createElement('button');
    btn.innerText = m.substring(0,3);
    btn.className = `text-[10px] font-bold py-1.5 px-1 rounded-lg transition-all ${{
      activo
        ? 'bg-yellow-500 text-gray-950 shadow-lg shadow-yellow-500/20'
        : 'text-gray-400 hover:bg-gray-800 hover:text-white'
    }}`;
    btn.onclick = () => {{ kpiMes = mesN; iniciarCalendarioKpi(); renderKpiCharts(); }};
    grid.appendChild(btn);
  }});
  document.getElementById('kpi-filtro-label').innerText = `${{MESES_ES[kpiMes-1]}} ${{kpiAnio}}`;
}}

function cambiarAnioKpi(dir) {{
  kpiAnio += dir;
  iniciarCalendarioKpi();
  renderKpiCharts();
}}

function limpiarFiltroKpi() {{
  kpiAnio = new Date().getFullYear();
  kpiMes  = new Date().getMonth() + 1;
  document.getElementById('kpi-colab-sel').value = '';
  iniciarCalendarioKpi();
  renderKpiCharts();
}}

// ── Foto colaborador en evaluación ────────────────────────────────────────
function actualizarFotoEval() {{
  const nombre = document.getElementById('eval-colab-sel').value;
  const box    = document.getElementById('eval-foto-box');
  if (!nombre) {{ box.classList.add('hidden'); return; }}
  const colab = COLABS.find(c => c.nombre === nombre);
  box.classList.remove('hidden');
  box.style.display = 'block';
  const img  = document.getElementById('eval-foto-img');
  const init = document.getElementById('eval-foto-init');
  const badge = document.getElementById('eval-foto-badge');
  img.src = `/static/fotos/${{nombre}}.jpg`;
  img.style.display = 'block';
  init.style.display = 'none';
  init.innerText = nombre[0].toUpperCase();
  badge.innerText = colab ? (colab.puestos||[]).join(', ') : '';
}}
function switchTab(name) {{
  document.querySelectorAll('.tab-content').forEach(el => el.classList.add('hidden'));
  document.getElementById('tab-' + name).classList.remove('hidden');
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('btn-' + name).classList.add('active');
  if (name === 'kpi') {{ iniciarCalendarioKpi(); renderKpiCharts(); }}
}}
window.addEventListener('DOMContentLoaded', () => {{
  switchTab('evaluacion');
  iniciarCalendarioKpi();
}});

// ── Calendario evaluación ─────────────────────────────────────────────────
let evalData = {{}};  // dia -> {{ actividad -> score }}

function cargarCalendario() {{
  const nombre = document.getElementById('eval-colab-sel').value;
  const mes    = document.getElementById('eval-mes').value;
  if (!nombre || !mes) return;

  const [anio, mesN] = mes.split('-').map(Number);
  const diasMes = new Date(anio, mesN, 0).getDate();
  const colab   = COLABS.find(c => c.nombre === nombre);
  if (!colab) return;

  const actividades = colab.actividades || [];
  // Cargar evals existentes del mes
  evalData = {{}};
  EVALS.filter(e => e.colaborador===nombre && e.anio===anio && e.mes===mesN).forEach(e => {{
    evalData[e.dia] = e.calificaciones;
  }});

  // Días de la semana — sin domingos (dow=0)
  const DIAS_SEM = ['D','L','M','M','J','V','S'];
  // Lista de días hábiles (lun-sab)
  const diasHabiles = [];
  for (let d = 1; d <= diasMes; d++) {{
    const dow = new Date(anio, mesN-1, d).getDay();
    if (dow !== 0) diasHabiles.push({{ d, dow }});
  }}

  let headerDias = '<th class="cal-cell bg-gray-900 text-yellow-500 font-bold sticky left-0 z-10 min-w-[180px] text-left px-2">ACTIVIDAD</th>';
  diasHabiles.forEach(({{ d, dow }}) => {{
    const hoy = new Date();
    const esHoy = (d===hoy.getDate()&&mesN===hoy.getMonth()+1&&anio===hoy.getFullYear());
    const esSab = dow === 6;
    headerDias += `<th class="cal-cell ${{esHoy?'bg-yellow-500/20 text-yellow-400':esSab?'bg-blue-900/20 text-blue-400':'bg-gray-900 text-gray-400'}} font-bold">
      <div>${{d}}</div><div class="text-[8px]">${{DIAS_SEM[dow]}}</div>
    </th>`;
  }});

  let filas = '';
  actividades.forEach(act => {{
    let fila = `<td class="cal-cell bg-gray-950/60 text-gray-300 font-bold sticky left-0 z-10 text-left px-2 text-[10px] uppercase whitespace-nowrap">${{act}}</td>`;
    diasHabiles.forEach(({{ d }}) => {{
      const score = evalData[d]?.[act] ?? '';
      const cls   = score !== '' ? `score-badge-${{score}}` : 'bg-gray-900/40 text-gray-700';
      fila += `<td class="cal-cell cursor-pointer hover:ring-1 hover:ring-yellow-500 transition-all" onclick="ciclarScore(this,${{d}},'${{act.replace(/'/g,"\\\\'")}}')">
        <div class="rounded text-[10px] font-black py-0.5 px-1 ${{cls}}">${{score !== '' ? score : '·'}}</div>
      </td>`;
    }});
    filas += `<tr class="hover:bg-gray-900/20 transition-colors">${{fila}}</tr>`;
  }});

  // Fila promedio
  let filaProm = `<td class="cal-cell bg-gray-900 text-yellow-500 font-black sticky left-0 z-10 text-left px-2 text-[10px] uppercase">PROMEDIO %</td>`;
  diasHabiles.forEach(({{ d }}) => {{
    const dayScores = evalData[d] ? Object.values(evalData[d]) : [];
    if (dayScores.length > 0) {{
      const avg = dayScores.reduce((a,b)=>a+b,0)/dayScores.length;
      const pct = Math.round((avg/5)*100);
      const col = pct>=80?'text-emerald-400':pct>=60?'text-yellow-400':'text-rose-400';
      filaProm += `<td class="cal-cell bg-gray-900/60 font-black ${{col}} text-[10px]">${{pct}}%</td>`;
    }} else {{
      filaProm += `<td class="cal-cell bg-gray-900/60 text-gray-700 text-[10px]">—</td>`;
    }}
  }});
  filas += `<tr class="border-t border-gray-700">${{filaProm}}</tr>`;

  document.getElementById('cal-container').innerHTML = `
    <table class="border-collapse text-center" style="table-layout:fixed;">
      <thead><tr>${{headerDias}}</tr></thead>
      <tbody>${{filas}}</tbody>
    </table>`;

  document.getElementById('btn-guardar-eval').classList.remove('hidden');
}}

function ciclarScore(td, dia, actividad) {{
  if (!evalData[dia]) evalData[dia] = {{}};
  const actual = evalData[dia][actividad] ?? -1;
  const nuevo  = (actual + 1) % 6;
  evalData[dia][actividad] = nuevo;
  const div = td.querySelector('div');
  div.className = `rounded text-[10px] font-black py-0.5 px-1 score-badge-${{nuevo}}`;
  div.innerText = nuevo;
  // Actualizar fila promedio
  const nombre = document.getElementById('eval-colab-sel').value;
  const mes    = document.getElementById('eval-mes').value;
  const [anio, mesN] = mes.split('-').map(Number);
  cargarCalendarioSilencioso(anio, mesN, nombre);
}}

function cargarCalendarioSilencioso(anio, mesN, nombre) {{
  // Recalcular solo promedios sin rerenderizar toda la tabla
  const diasMes = new Date(anio, mesN, 0).getDate();
  const colab = COLABS.find(c=>c.nombre===nombre);
  if(!colab) return;
  const nActs = (colab.actividades||[]).length;
  // Buscar la última fila (promedio)
  const table = document.querySelector('#cal-container table tbody');
  if(!table) return;
  const lastRow = table.lastElementChild;
  if(!lastRow) return;
  const cells = lastRow.querySelectorAll('td');
  for(let d=1;d<=diasMes;d++) {{
    const dayScores = evalData[d] ? Object.values(evalData[d]) : [];
    const cell = cells[d];
    if(!cell) continue;
    if(dayScores.length>0) {{
      const avg=dayScores.reduce((a,b)=>a+b,0)/dayScores.length;
      const pct=Math.round((avg/5)*100);
      cell.className=`cal-cell bg-gray-900/60 font-black text-[10px] ${{pct>=80?'text-emerald-400':pct>=60?'text-yellow-400':'text-rose-400'}}`;
      cell.innerText=pct+'%';
    }}
  }}
}}

async function guardarEvaluacion() {{
  const nombre = document.getElementById('eval-colab-sel').value;
  const mes    = document.getElementById('eval-mes').value;
  if (!nombre || !mes) {{ showRes('res-eval','⚠ Selecciona colaborador y mes.','err'); return; }}
  const [anio,mesN] = mes.split('-').map(Number);
  const promesas = [];
  for (const [diaStr, cals] of Object.entries(evalData)) {{
    if (Object.keys(cals).length === 0) continue;
    promesas.push(fetch('/api/evaluacion',{{
      method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{colaborador:nombre,anio,mes:mesN,dia:parseInt(diaStr),calificaciones:cals}})
    }}));
  }}
  await Promise.all(promesas);
  showRes('res-eval',`✓ Evaluación guardada — ${{Object.keys(evalData).length}} días registrados.`,'ok');
  setTimeout(()=>location.reload(),1000);
}}

// ── Tarjeta visual ────────────────────────────────────────────────────────
function verTarjeta() {{
  const nombre = document.getElementById('eval-colab-sel').value;
  const mes    = document.getElementById('eval-mes').value;
  if (!nombre) {{ alert('Selecciona un colaborador.'); return; }}
  const [anio,mesN] = mes.split('-').map(Number);
  const colab = COLABS.find(c=>c.nombre===nombre);
  if (!colab) return;

  // Calcular promedio por actividad de todos los días del mes
  const actScores = {{}};
  EVALS.filter(e=>e.colaborador===nombre&&e.anio===anio&&e.mes===mesN).forEach(e=>{{
    Object.entries(e.calificaciones).forEach(([act,score])=>{{
      if(!actScores[act]) actScores[act]=[];
      actScores[act].push(score);
    }});
  }});
  // Combinar con evalData en memoria
  Object.entries(evalData).forEach(([dia,cals])=>{{
    Object.entries(cals).forEach(([act,score])=>{{
      if(!actScores[act]) actScores[act]=[];
      actScores[act].push(score);
    }});
  }});

  const meses=['Enero','Febrero','Marzo','Abril','Mayo','Junio','Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre'];
  const mesNombre=meses[mesN-1];

  let totalScores=[]; 
  let filasTarjeta='';
  (colab.actividades||[]).forEach(act=>{{
    const scores=actScores[act]||[];
    const avg=scores.length>0?scores.reduce((a,b)=>a+b,0)/scores.length:null;
    const pct=avg!==null?Math.round((avg/5)*100):null;
    totalScores.push(pct??0);
    const color=pct===null?'text-gray-500':pct>=80?'text-emerald-400':pct>=60?'text-yellow-400':'text-rose-400';
    filasTarjeta+=`<tr class="border-b border-gray-800/60">
      <td class="py-1.5 px-2 text-[10px] font-bold text-gray-300 uppercase">${{act}}</td>
      <td class="py-1.5 px-2 text-right font-black text-sm ${{color}}">${{pct!==null?pct+'%':'—'}}</td>
    </tr>`;
  }});

  const promTotal=totalScores.length>0?Math.round(totalScores.reduce((a,b)=>a+b,0)/totalScores.length):0;
  const colorTotal=promTotal>=80?'text-emerald-400':promTotal>=60?'text-yellow-400':'text-rose-400';

  document.getElementById('modal-tarjeta-body').innerHTML=`
    <div class="space-y-4">
      <div class="flex items-center gap-4">
        <img src="/static/fotos/${{nombre}}.jpg" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'" 
          class="w-20 h-20 rounded-xl object-cover border-2 border-yellow-500/40 shadow-lg">
        <div class="w-20 h-20 rounded-xl bg-yellow-500/10 border-2 border-yellow-500/30 items-center justify-center text-yellow-500 font-black text-3xl hidden">${{nombre[0].toUpperCase()}}</div>
        <div>
          <p class="text-lg font-black text-white uppercase font-custom">${{nombre}}</p>
          <p class="text-xs text-gray-400 uppercase font-bold">${{(colab.puestos||[]).join(', ')}}</p>
          <p class="text-xs text-yellow-500/70 mt-1">${{mesNombre}} ${{anio}}</p>
        </div>
        <div class="ml-auto text-right">
          <p class="text-[9px] text-gray-500 uppercase font-bold">Total</p>
          <p class="text-3xl font-black ${{colorTotal}} font-custom">${{promTotal}}%</p>
        </div>
      </div>
      <div class="bg-gray-950/60 border border-gray-800 rounded-xl overflow-hidden">
        <table class="w-full">
          <thead><tr class="bg-gray-900 text-[9px] text-gray-400 uppercase font-bold border-b border-gray-800">
            <th class="px-2 py-2 text-left">Actividad</th><th class="px-2 py-2 text-right">Puntaje</th>
          </tr></thead>
          <tbody>${{filasTarjeta}}</tbody>
        </table>
      </div>
      <div class="flex gap-2 flex-wrap text-[9px]">
        <span class="score-badge-5 px-2 py-0.5 rounded font-bold">≥80% Excelente</span>
        <span class="score-badge-3 px-2 py-0.5 rounded font-bold">60-79% En observación</span>
        <span class="score-badge-0 px-2 py-0.5 rounded font-bold">&lt;60% Crítico</span>
      </div>
    </div>`;
  document.getElementById('modal-tarjeta').classList.remove('hidden');
}}

function toggleTipoPersonalizado() {{
  const tipo = document.getElementById('inc-tipo').value;
  const box  = document.getElementById('inc-tipo-custom-box');
  box.classList.toggle('hidden', tipo !== 'Otro');
  if (tipo !== 'Otro') document.getElementById('inc-tipo-custom').value = '';
}}

function toggleTipoPuestoPersonalizado() {{
  const otroChecked = document.querySelector('.col-puesto-chk[value="Otro"]')?.checked;
  const box = document.getElementById('col-puesto-custom-box');
  box.classList.toggle('hidden', !otroChecked);
  if (!otroChecked) document.getElementById('col-puesto-custom').value = '';
}}

function previsualizarFotoInc(input) {{
  if (!input.files || !input.files[0]) return;
  const url = URL.createObjectURL(input.files[0]);
  document.getElementById('inc-foto-img').src = url;
  document.getElementById('inc-foto-preview').classList.remove('hidden');
}}

function limpiarFotoInc() {{
  document.getElementById('inc-foto-input').value = '';
  document.getElementById('inc-foto-preview').classList.add('hidden');
  document.getElementById('inc-foto-img').src = '';
}}

function verFotoInc(src) {{
  document.getElementById('modal-foto-inc-img').src = src;
  document.getElementById('modal-foto-inc').classList.remove('hidden');
}}

// ── Modal resolver incidencia ─────────────────────────────────────────────
function abrirModalResolver(id) {{
  document.getElementById('resolver-inc-id').value = id;
  document.getElementById('resolver-solucion').value = '';
  document.getElementById('resolver-por').value = '';
  document.getElementById('res-resolver').classList.add('hidden');
  document.getElementById('modal-resolver').classList.remove('hidden');
}}

async function confirmarResolucion() {{
  const id       = parseInt(document.getElementById('resolver-inc-id').value);
  const solucion = document.getElementById('resolver-solucion').value.trim();
  const por      = document.getElementById('resolver-por').value;
  if (!solucion) {{ showRes('res-resolver','⚠ Escribe la solución aplicada.','err'); return; }}
  if (!por)      {{ showRes('res-resolver','⚠ Selecciona quién resuelve.','err'); return; }}
  const r = await fetch(`/api/incidencia/${{id}}/resolver`, {{
    method:'PATCH', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{ solucion, resuelto_por: por }})
  }});
  if (r.ok) {{
    document.getElementById('modal-resolver').classList.add('hidden');
    location.reload();
  }} else showRes('res-resolver','⚠ Error al resolver.','err');
}}

// ── Guardar incidencia ────────────────────────────────────────────────────
async function guardarInc() {{
  const ingresadoPor = document.getElementById('inc-ingresado-por').value;
  const colabs = Array.from(document.querySelectorAll('.inc-colab-chk:checked')).map(el=>el.value);
  const resp  = document.getElementById('inc-resp').value.trim();
  const tipo  = document.getElementById('inc-tipo').value;
  const tipoCustom = document.getElementById('inc-tipo-custom')?.value.trim() || '';
  if (!ingresadoPor) {{ showRes('res-inc','⚠ Selecciona quién ingresa la incidencia.','err'); return; }}
  if (!colabs.length || !resp) {{ showRes('res-inc','⚠ Selecciona al menos un colaborador y completa el responsable.','err'); return; }}
  if (tipo === 'Otro' && !tipoCustom) {{ showRes('res-inc','⚠ Especifica el tipo de incidencia.','err'); return; }}

  const payload = {{
    colaboradores: colabs, tipo, tipo_personalizado: tipoCustom,
    responsable: resp, ingresado_por: ingresadoPor,
    observaciones: document.getElementById('inc-obs').value
  }};
  const r = await fetch('/api/incidencia',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}});
  if (!r.ok) {{ showRes('res-inc','⚠ Error al guardar.','err'); return; }}
  const d = await r.json();

  // Subir foto si hay una seleccionada
  const fotoInput = document.getElementById('inc-foto-input');
  if (fotoInput.files && fotoInput.files[0]) {{
    if (d.id) {{
      const fd = new FormData();
      fd.append('file', fotoInput.files[0]);
      await fetch(`/api/incidencia/${{d.id}}/foto`, {{method:'POST', body:fd}});
    }}
  }}

  showRes('res-inc','✓ Incidencia registrada.','ok');
  setTimeout(()=>location.reload(),800);
}}

async function resolverInc(id) {{
  // Redirige al modal — función de compatibilidad
  abrirModalResolver(id);
}}

// ── Actualizar hora de regreso ────────────────────────────────────────────
function abrirModalRegreso(colaborador, fecha) {{
  document.getElementById('regreso-colaborador').value = colaborador;
  document.getElementById('regreso-fecha').value = fecha;
  document.getElementById('regreso-nombre-display').innerText = colaborador;
  document.getElementById('regreso-fecha-display').innerText = fecha;
  document.getElementById('regreso-hora').value = '';
  document.getElementById('res-regreso').classList.add('hidden');
  document.getElementById('modal-regreso').classList.remove('hidden');
}}

async function guardarRegreso() {{
  const colaborador  = document.getElementById('regreso-colaborador').value;
  const fecha        = document.getElementById('regreso-fecha').value;
  const hora_regreso = document.getElementById('regreso-hora').value;
  if (!hora_regreso) {{ showRes('res-regreso','⚠ Ingresa la hora de regreso.','err'); return; }}
  const r = await fetch('/api/horas/regreso', {{
    method: 'PATCH',
    headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{ colaborador, fecha, hora_regreso }})
  }});
  if (r.ok) {{
    showRes('res-regreso','✓ Hora de regreso guardada.','ok');
    setTimeout(() => location.reload(), 800);
  }} else {{
    showRes('res-regreso','⚠ Error al guardar.','err');
  }}
}}

// ── Guardar horas ────────────────────────────────────────────────────────
async function guardarHoras() {{
  const colab = document.getElementById('hr-colab').value;
  const fecha = document.getElementById('hr-fecha').value;
  const tipo  = document.getElementById('hr-tipo-ruta').value;
  const dest  = document.getElementById('hr-destino').value;
  if (!colab || !fecha) {{ showRes('res-hr','⚠ Selecciona operador y fecha.','err'); return; }}

  const horas = {{}};
  ACTIVIDADES_HORAS.forEach((act,i) => {{
    const v = document.getElementById('hr-'+i).value;
    if (v) horas[act] = v;
  }});

  const r = await fetch('/api/horas', {{
    method: 'POST', headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{ colaborador:colab, fecha, tipo_ruta:tipo, destino:dest, horas }})
  }});
  const d = await r.json();
  if (r.ok) {{
    let msg = '✓ Horas guardadas.';
    if (d.minutos_prep) msg += ` Preparación: ${{d.minutos_prep}} min`;
    if (d.efic_prep)    msg += ` · Eficiencia: ${{d.efic_prep}}% (${{d.nivel_prep}})`;
    showRes('res-hr', msg, 'ok');
    setTimeout(() => location.reload(), 1000);
  }} else showRes('res-hr','⚠ Error al guardar.','err');
}}

function verHoras(h) {{
  const col_niv = {{'Óptimo':'text-emerald-400','Aceptable':'text-yellow-400','Por mejorar':'text-rose-400'}};
  const cn = col_niv[h.nivel_prep] || 'text-gray-400';
  let rows = '';
  ACTIVIDADES_HORAS.forEach(act => {{
    const v   = h.horas[act] || '—';
    const cum = h.cumplimientos?.[act];
    const ok  = cum ? (cum.cumple ? '✓' : '✗') : '';
    const cc  = cum ? (cum.cumple ? 'text-emerald-400' : 'text-rose-400') : 'text-gray-600';
    const meta = cum ? ` <span class="text-gray-600">(máx ${{cum.meta}})</span>` : '';
    rows += `<div class="flex justify-between items-center py-1.5 border-b border-gray-800/60 text-xs">
      <span class="text-gray-400">${{act}}</span>
      <span class="font-mono font-bold text-yellow-500">${{v}}${{meta}} <span class="${{cc}} font-black">${{ok}}</span></span>
    </div>`;
  }});
  const eficHTML = h.efic_prep
    ? `<div class="flex justify-between items-center py-2 text-xs border-t border-gray-700 mt-1">
        <span class="font-bold text-gray-300">Eficiencia Preparación</span>
        <span class="font-black text-lg ${{cn}}">${{h.efic_prep}}% <span class="text-xs">${{h.nivel_prep}}</span></span>
       </div>
       <div class="flex justify-between items-center py-1 text-xs">
        <span class="text-gray-500">Meta (${{h.tipo_ruta}})</span>
        <span class="font-bold text-gray-400">${{h.meta_prep}} min</span>
       </div>
       <div class="flex justify-between items-center py-1 text-xs">
        <span class="text-gray-500">Tiempo real prep.</span>
        <span class="font-bold text-white">${{h.minutos_prep}} min</span>
       </div>`
    : '';
  const rutaHTML = h.minutos_ruta
    ? `<div class="flex justify-between items-center py-1 text-xs">
        <span class="text-gray-500">Duración de ruta</span>
        <span class="font-bold text-blue-400">${{h.minutos_ruta}} min</span>
       </div>` : '';

  document.getElementById('modal-horas-body').innerHTML = `
    <div class="space-y-1">
      <div class="flex justify-between text-[10px] text-gray-500 mb-2">
        <span class="font-bold text-white uppercase">${{h.colaborador}}</span>
        <span>${{h.fecha}} · ${{h.tipo_ruta}}</span>
      </div>
      ${{h.destino ? `<p class="text-[10px] text-blue-400 mb-2">Destino: ${{h.destino}}</p>` : ''}}
      ${{rows}}
      ${{eficHTML}}
      ${{rutaHTML}}
    </div>`;
  document.getElementById('modal-horas').classList.remove('hidden');
}}

// ── Guardar tiempo ────────────────────────────────────────────────────────
function actualizarMeta(){{
  const t=document.getElementById('tp-tipo').value;
  document.getElementById('meta-disp').innerText=t==='Ruta Local'?'3h 00min':'5h 00min';
}}
async function guardarTiempo(){{
  const colab=document.getElementById('tp-colab').value;
  const min=parseInt(document.getElementById('tp-min').value);
  if(!colab||!min||min<1){{showRes('res-tp','⚠ Completa todos los campos.','err');return;}}
  const r=await fetch('/api/tiempo',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{colaborador:colab,tipo_ruta:document.getElementById('tp-tipo').value,
      modelo_tarea:document.getElementById('tp-mod').value||'General',
      minutos:min,observaciones:document.getElementById('tp-obs').value}})}});
  const d=await r.json();
  if(r.ok){{showRes('res-tp',`✓ Eficiencia: ${{d.eficiencia}}% (${{d.nivel}})`,'ok');setTimeout(()=>location.reload(),900);}}
  else showRes('res-tp','⚠ Error.','err');
}}

// ── Colaboradores ────────────────────────────────────────────────────────
async function guardarColab(){{
  const nombre=document.getElementById('col-nombre').value.trim();
  const puestos=Array.from(document.querySelectorAll('.col-puesto-chk:checked')).map(el=>el.value);
  const puestoCustom=document.getElementById('col-puesto-custom')?.value.trim() || '';
  const acts=document.getElementById('col-actividades').value.split('\\n').map(s=>s.trim().toUpperCase()).filter(Boolean);
  if(!nombre||!puestos.length||acts.length===0){{showRes('res-colab','⚠ Completa el nombre, al menos un tipo de puesto y las actividades.','err');return;}}
  if(puestos.includes('Otro') && !puestoCustom){{showRes('res-colab','⚠ Especifica el puesto personalizado.','err');return;}}
  const r=await fetch('/api/colaborador',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{nombre,puestos,puesto_personalizado:puestoCustom,actividades:acts}})}});
  if(r.ok){{showRes('res-colab','✓ Colaborador agregado.','ok');setTimeout(()=>location.reload(),800);}}
  else showRes('res-colab','⚠ '+(await r.json()).detail,'err');
}}
async function eliminarColab(nombre){{
  if(!confirm(`¿Desactivar a ${{nombre}}? Ya no aparecerá para registrar evaluaciones, incidencias u horas nuevas, pero su historial se conserva. Lo puedes reactivar después desde "Ver inactivos".`))return;
  await fetch(`/api/colaborador/${{encodeURIComponent(nombre)}}`,{{method:'DELETE'}});
  location.reload();
}}
async function reactivarColab(nombre){{
  await fetch(`/api/colaborador/${{encodeURIComponent(nombre)}}/activar`,{{method:'PATCH'}});
  location.reload();
}}
async function subirFoto(nombre,input){{
  const fd=new FormData();fd.append('file',input.files[0]);
  await fetch(`/api/colaborador/${{encodeURIComponent(nombre)}}/foto`,{{method:'POST',body:fd}});
  location.reload();
}}

// ── Filtros ───────────────────────────────────────────────────────────────
function filtrarInc(){{
  const fColab    = document.getElementById('filtro-inc-colab')?.value.toUpperCase() || '';
  const fTipo     = document.getElementById('filtro-inc-tipo')?.value || '';
  const fIngresado= document.getElementById('filtro-inc-ingresado')?.value.toUpperCase() || '';
  const fEst      = document.getElementById('filtro-inc-est')?.value || '';
  const fTexto    = document.getElementById('filtro-inc-texto')?.value.toUpperCase() || '';
  const rows      = document.querySelectorAll('#body-inc tr');
  let visibles = 0;

  rows.forEach(tr => {{
    const cells = tr.querySelectorAll('td');
    if (!cells.length) return;
    const fecha     = (cells[0]?.innerText || '').toUpperCase();
    const colab     = (cells[1]?.innerText || '').toUpperCase();
    const tipo      = (cells[2]?.innerText || '');
    const resp      = (cells[3]?.innerText || '').toUpperCase();
    const obs       = (cells[4]?.innerText || '').toUpperCase();
    const estatus   = (cells[6]?.innerText || '');
    // ingresado_por no está en la tabla visible, lo buscamos en data attribute si existe
    const ingresado = tr.getAttribute('data-ingresado') || '';

    const okColab     = !fColab     || colab.includes(fColab);
    const okTipo      = !fTipo      || tipo.includes(fTipo);
    const okIngresado = !fIngresado || ingresado.toUpperCase().includes(fIngresado);
    const okEst       = !fEst       || estatus.includes(fEst);
    const okTexto     = !fTexto     || colab.includes(fTexto) || obs.includes(fTexto) || resp.includes(fTexto) || fecha.includes(fTexto) || tipo.toUpperCase().includes(fTexto);

    const visible = okColab && okTipo && okIngresado && okEst && okTexto;
    tr.style.display = visible ? '' : 'none';
    if (visible) visibles++;
  }});

  const contador = document.getElementById('filtro-inc-contador');
  if (contador) contador.innerText = visibles > 0 ? `${{visibles}} incidencia(s) encontrada(s)` : 'Sin resultados';
}}

function limpiarFiltrosInc() {{
  ['filtro-inc-colab','filtro-inc-tipo','filtro-inc-ingresado','filtro-inc-est'].forEach(id => {{
    const el = document.getElementById(id); if(el) el.value='';
  }});
  const txt = document.getElementById('filtro-inc-texto'); if(txt) txt.value='';
  filtrarInc();
}}
function filtrarTiempos(){{
  const fc=document.getElementById('filtro-tp-colab').value.toUpperCase();
  document.querySelectorAll('#body-tiempos tr').forEach(tr=>{{
    const cells=tr.querySelectorAll('td');
    if(!cells.length)return;
    tr.style.display=(!fc||(cells[1]?.innerText||'').toUpperCase().includes(fc))?'':'none';
  }});
}}

// ── KPI charts ────────────────────────────────────────────────────────────
let cE=null,cI=null,cF=null,cA=null,cT=null;
function renderKpiCharts(){{
  const colabSel = document.getElementById('kpi-colab-sel')?.value || '';
  const anioFilt = kpiAnio;
  const mesFilt  = kpiMes;

  // Filtrar evaluaciones
  let evFilt = EVALS.filter(e=>{{
    if(colabSel && e.colaborador!==colabSel) return false;
    if(anioFilt && e.anio!==anioFilt) return false;
    if(mesFilt  && e.mes!==mesFilt)   return false;
    return true;
  }});

  // ── Cards colaborador individual ─────────────────────────────────────
  const cardsDiv = document.getElementById('kpi-cards-colab');
  if(colabSel && evFilt.length>0){{
    cardsDiv.classList.remove('hidden');
    cardsDiv.style.display='grid';
    const dias=evFilt.length;
    const incCount=incidenciasDelMes(colabSel, anioFilt, mesFilt);
    const promBruto=evFilt.reduce((a,b)=>a+b.pct,0)/dias;
    const prom=Math.max(0, promBruto - incCount*PENALIZACION_POR_INCIDENCIA_PCT).toFixed(1);
    // Mejor actividad
    const actTotals={{}};
    evFilt.forEach(e=>Object.entries(e.calificaciones).forEach(([k,v])=>{{
      if(!actTotals[k]) actTotals[k]=[];
      actTotals[k].push(v);
    }}));
    let mejorAct='—', mejorPct=0;
    Object.entries(actTotals).forEach(([k,vals])=>{{
      const p=Math.round((vals.reduce((a,b)=>a+b,0)/vals.length/5)*100);
      if(p>mejorPct){{mejorPct=p;mejorAct=k;}}
    }});
    document.getElementById('kc-dias').innerText=dias;
    const pc=document.getElementById('kc-prom');
    pc.innerText=prom+'%';
    pc.className=`text-2xl font-black font-custom ${{parseFloat(prom)>=80?'text-emerald-400':parseFloat(prom)>=60?'text-yellow-400':'text-rose-400'}}`;
    document.getElementById('kc-inc').innerText=incCount;
    document.getElementById('kc-mejor').innerText=mejorAct+' ('+mejorPct+'%)';

    // Cumplimiento de tiempos de preparación del colaborador
    const horasColab = HORAS_DATA.filter(h => h.colaborador === colabSel && h.minutos_prep !== null && h.minutos_prep !== undefined);
    const horasEnMeta = horasColab.filter(h => h.efic_prep !== null && h.efic_prep !== undefined && h.efic_prep >= 100);
    const prepEl = document.getElementById('kc-prep');
    const prepSub = document.getElementById('kc-prep-sub');
    if (horasColab.length > 0) {{
      const pctPrep = Math.round((horasEnMeta.length / horasColab.length) * 100);
      prepEl.innerText = pctPrep + '%';
      prepEl.className = `text-2xl font-black font-custom ${{pctPrep>=80?'text-emerald-400':pctPrep>=60?'text-yellow-400':'text-rose-400'}}`;
      prepSub.innerText = `${{horasEnMeta.length}}/${{horasColab.length}} rutas en tiempo`;
    }} else {{
      prepEl.innerText = '—';
      prepSub.innerText = 'sin registros de horas';
    }}
  }} else {{
    cardsDiv.classList.add('hidden');
    cardsDiv.style.display='none';
  }}

  // ── Gráfica 1: % promedio por colaborador ────────────────────────────
  const evalMap={{}};
  evFilt.forEach(e=>{{if(!evalMap[e.colaborador])evalMap[e.colaborador]=[];evalMap[e.colaborador].push(e.pct);}});
  const eL=Object.keys(evalMap).length?Object.keys(evalMap):['Sin datos'];
  const eD=eL.map(l=>{{
    if(!evalMap[l]) return 0;
    const raw=evalMap[l].reduce((a,b)=>a+b,0)/evalMap[l].length;
    const penal=incidenciasDelMes(l, anioFilt, mesFilt)*PENALIZACION_POR_INCIDENCIA_PCT;
    return Math.max(0, raw-penal).toFixed(1);
  }});
  const eColors=eD.map(v=>parseFloat(v)>=80?'#10b981':parseFloat(v)>=60?'#f59e0b':'#f43f5e');

  if(cE)cE.destroy();
  cE=new Chart(document.getElementById('chartEval'),{{
    type:'bar',
    data:{{labels:eL,datasets:[{{data:eD,backgroundColor:eColors,borderRadius:4}}]}},
    options:{{indexAxis:'y',responsive:true,maintainAspectRatio:false,
      plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:ctx=>` ${{ctx.parsed.x}}%`}}}}}},
      scales:{{x:{{min:0,max:100,ticks:{{color:'#6b7280',font:{{size:10}},callback:v=>v+'%'}},grid:{{color:'#1f2937'}}}},
               y:{{ticks:{{color:'#9ca3af',font:{{size:10}}}},grid:{{display:false}}}}}}}}
  }});

  // ── Gráfica 2: Desglose por CATEGORÍAS GENERALES LUQROSS ─────────────
  // Mapa: actividad individual → categoría general
  const MAPA_CATEGORIAS = {{
    // CONTROL DE ETIQUETAS
    'REGISTRAR ETIQUETAS EN SISTEMA':           'CONTROL DE ETIQUETAS',
    'EVITAR QUIEBRES DE STOCK ETIQUETAS':       'CONTROL DE ETIQUETAS',
    'CONTROL DE SALIDAS Y ENTRADAS DE ETIQUETAS':'CONTROL DE ETIQUETAS',
    'REVISION DE CORRECTO ETIQUETADO':          'CONTROL DE ETIQUETAS',
    'ENTREGA DE ETIQUETAS QR':                  'CONTROL DE ETIQUETAS',
    'ENTREGA DE ETIQUETAS':                     'CONTROL DE ETIQUETAS',
    'ETIQUETADO CORRECTO':                      'CONTROL DE ETIQUETAS',
    'CORRECTO ETIQUETADO':                      'CONTROL DE ETIQUETAS',
    'SUPERVISION DE ETIQUETADO':                'CONTROL DE ETIQUETAS',
    // OPERATIVO
    'PRODUCTO CORRECTO':                        'OPERATIVO',
    'PREPARACIÓN DE PEDIDO':                    'OPERATIVO',
    'PREPARACION DE PEDIDO':                    'OPERATIVO',
    'CORRECTO PREPARACION DE PAQUETE':          'OPERATIVO',
    'CUIDADO AL MANEJAR MATERIAL':              'OPERATIVO',
    'SUPERVISION DE PREPARACION DE PEDIDOS':    'OPERATIVO',
    'ENTREGA DE MATERIAL EN ZONA DE PREPARACION':'OPERATIVO',
    'ACOMODO DE MATERIAL':                      'OPERATIVO',
    // REGISTROS
    'REGISTRO DIARIO DE TARIMAS':               'REGISTROS',
    'INCIDENCIAS REGISTRADAS':                  'REGISTROS',
    'REGISTRO DIARIO DE TARIMAS':               'REGISTROS',
    // LIMPIEZA ALMACEN/UNIDADES
    'LIMPIEZA DE ALMACEN':                      'LIMPIEZA ALMACEN/UNIDADES',
    'LIMPIEZA DE UNIDADES':                     'LIMPIEZA ALMACEN/UNIDADES',
    'LIMPIEZA DE PRODUCTOS':                    'LIMPIEZA ALMACEN/UNIDADES',
    'REVISION DE LIMPIEZA DE UNIDADES':         'LIMPIEZA ALMACEN/UNIDADES',
    'REVISION DE LIMPIEZA DE ALMACEN':          'LIMPIEZA ALMACEN/UNIDADES',
    // CONTEOS CICLICOS
    'CONTEOS CICLICOS':                         'CONTEOS CICLICOS',
    'PRIMERAS SALIDAS PEMPS':                   'CONTEOS CICLICOS',
    // ENTREGA DE PAQUETES/PEDIDOS
    'ENTREGA DE PEDIDOS':                       'ENTREGA DE PAQUETES/PEDIDOS',
    'ENTREGA DE PAQUETES COMPLETOS':            'ENTREGA DE PAQUETES/PEDIDOS',
    'REVISION DE MATERIAL COMPLETO':            'ENTREGA DE PAQUETES/PEDIDOS',
    'CORRECTA LIMPIEZA DE PEDIDOS':             'ENTREGA DE PAQUETES/PEDIDOS',
    'CORRECTO ETIQUETADO':                      'ENTREGA DE PAQUETES/PEDIDOS',
    // EVITAR REENVIOS
    'EVITAR REENVIOS':                          'EVITAR REENVIOS',
    'EVITAR REPROCESOS':                        'EVITAR REENVIOS',
    // CALIDAD (todo en general — se calcula como promedio global)
  }};

  const ORDEN_CATS = [
    'CONTROL DE ETIQUETAS',
    'OPERATIVO',
    'REGISTROS',
    'LIMPIEZA ALMACEN/UNIDADES',
    'CONTEOS CICLICOS',
    'ENTREGA DE PAQUETES/PEDIDOS',
    'EVITAR REENVIOS',
    'CALIDAD'
  ];

  // Acumular scores por categoría
  const catMap = {{}};
  ORDEN_CATS.forEach(c => catMap[c] = []);

  evFilt.forEach(e => {{
    Object.entries(e.calificaciones).forEach(([act, val]) => {{
      const cat = MAPA_CATEGORIAS[act.trim().toUpperCase()];
      if (cat && catMap[cat]) catMap[cat].push(val);
      // Toda actividad va a CALIDAD
      catMap['CALIDAD'].push(val);
    }});
  }});

  const catLabels = ORDEN_CATS;
  const catData   = ORDEN_CATS.map(c => {{
    const vals = catMap[c];
    return vals.length > 0 ? Math.round((vals.reduce((a,b)=>a+b,0)/vals.length/5)*100) : 0;
  }});
  const catColors = catData.map((v,i) => {{
    if (i === ORDEN_CATS.length - 1) return '#6366f1'; // CALIDAD en morado
    return v>=80?'#10b981':v>=60?'#f59e0b':'#f43f5e';
  }});

  if(cA)cA.destroy();
  cA=new Chart(document.getElementById('chartActividades'),{{
    type:'bar',
    data:{{
      labels: catLabels,
      datasets:[{{
        data: catData,
        backgroundColor: catColors,
        borderRadius: 4
      }}]
    }},
    options:{{
      indexAxis:'y', responsive:true, maintainAspectRatio:false,
      plugins:{{
        legend:{{display:false}},
        tooltip:{{callbacks:{{label:ctx=>` ${{ctx.parsed.x}}% (${{catMap[ctx.label]?.length||0}} registros)`}}}}
      }},
      scales:{{
        x:{{min:0,max:100,ticks:{{color:'#6b7280',font:{{size:10}},callback:v=>v+'%'}},grid:{{color:'#1f2937'}}}},
        y:{{ticks:{{color:'#9ca3af',font:{{size:10}}}},grid:{{display:false}}}}
      }}
    }}
  }});

  // ── Gráfica 3: Incidencias por colaborador ───────────────────────────
  let incFilt=INCIDENCIAS_DATA;
  if(colabSel) incFilt=incFilt.filter(i=>(i.colaboradores||[]).includes(colabSel));
  const incMap={{}};
  incFilt.forEach(i=>{{(i.colaboradores||[]).forEach(nombre=>{{incMap[nombre]=(incMap[nombre]||0)+1;}});}});
  const iL=Object.keys(incMap).length?Object.keys(incMap):['Sin datos'];
  const iD=iL.map(l=>incMap[l]||0);

  if(cI)cI.destroy();
  cI=new Chart(document.getElementById('chartInc'),{{
    type:'bar',
    data:{{labels:iL,datasets:[{{data:iD,backgroundColor:'#f43f5e',borderRadius:4}}]}},
    options:{{indexAxis:'y',responsive:true,maintainAspectRatio:false,
      plugins:{{legend:{{display:false}}}},
      scales:{{x:{{ticks:{{color:'#6b7280',font:{{size:10}}}},grid:{{color:'#1f2937'}}}},
               y:{{ticks:{{color:'#9ca3af',font:{{size:10}}}},grid:{{display:false}}}}}}}}
  }});

  // ── Gráfica 4: Tendencia diaria del mes ──────────────────────────────
  const diasMap={{}};
  evFilt.forEach(e=>{{diasMap[e.dia]=(diasMap[e.dia]||[]);diasMap[e.dia].push(e.pct);}});
  const dL=Object.keys(diasMap).sort((a,b)=>parseInt(a)-parseInt(b));
  const dD=dL.map(d=>(diasMap[d].reduce((a,b)=>a+b,0)/diasMap[d].length).toFixed(1));

  if(cT)cT.destroy();
  cT=new Chart(document.getElementById('chartTendencia'),{{
    type:'line',
    data:{{labels:dL.map(d=>'Día '+d),datasets:[{{
      data:dD,borderColor:'#eab308',backgroundColor:'rgba(234,179,8,0.1)',
      borderWidth:2,pointRadius:3,pointBackgroundColor:'#eab308',fill:true,tension:0.3
    }}]}},
    options:{{responsive:true,maintainAspectRatio:false,
      plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:ctx=>` ${{ctx.parsed.y}}%`}}}}}},
      scales:{{
        x:{{ticks:{{color:'#6b7280',font:{{size:9}}}},grid:{{color:'#1f2937'}}}},
        y:{{min:0,max:100,ticks:{{color:'#6b7280',font:{{size:9}},callback:v=>v+'%'}},grid:{{color:'#1f2937'}}}}
      }}}}
  }});

  // ── Gráfica 5: Tiempo promedio de preparación desde Horas ────────────
  let horasFilt = HORAS_DATA;
  if (colabSel) horasFilt = horasFilt.filter(h => h.colaborador === colabSel);

  // Agrupar minutos_prep por colaborador
  const prepMap = {{}};
  horasFilt.forEach(h => {{
    if (h.minutos_prep === null || h.minutos_prep === undefined) return;
    if (!prepMap[h.colaborador]) prepMap[h.colaborador] = [];
    prepMap[h.colaborador].push(h.minutos_prep);
  }});

  const pL = Object.keys(prepMap).length ? Object.keys(prepMap) : ['Sin datos'];
  const pD = pL.map(l => prepMap[l] ? Math.round(prepMap[l].reduce((a,b)=>a+b,0)/prepMap[l].length) : 0);
  // Color por meta (usamos META_LOCAL como referencia base)
  const pColors = pD.map(v => v <= META_LOCAL ? '#10b981' : v <= META_PAQ ? '#f59e0b' : '#f43f5e');

  // Líneas de referencia
  let cTP = null;
  if(window._cTP) window._cTP.destroy();
  window._cTP = new Chart(document.getElementById('chartTiemposPrep'), {{
    type: 'bar',
    data: {{
      labels: pL,
      datasets: [
        {{ label:'Tiempo real (min)', data: pD, backgroundColor: pColors, borderRadius: 4 }},
        {{ label:'Meta Ruta Local', data: pL.map(()=>META_LOCAL), type:'line', borderColor:'#eab308', borderWidth:1.5, borderDash:[4,4], pointRadius:0, fill:false }},
        {{ label:'Meta Paquetería',  data: pL.map(()=>META_PAQ),   type:'line', borderColor:'#6366f1', borderWidth:1.5, borderDash:[4,4], pointRadius:0, fill:false }}
      ]
    }},
    options: {{
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: {{ legend: {{ position:'bottom', labels:{{ color:'#9ca3af', font:{{size:9}} }} }},
                 tooltip: {{ callbacks: {{ label: ctx => ` ${{ctx.parsed.x}} min` }} }} }},
      scales: {{
        x: {{ ticks:{{color:'#6b7280',font:{{size:9}}}}, grid:{{color:'#1f2937'}} }},
        y: {{ ticks:{{color:'#9ca3af',font:{{size:10}}}}, grid:{{display:false}} }}
      }}
    }}
  }});

  // ── Gráfica 6: Eficiencia % calculada desde horas ────────────────────
  const eficPrepMap = {{}};
  horasFilt.forEach(h => {{
    if (h.minutos_prep === null || h.minutos_prep === undefined || h.minutos_prep <= 0) return;
    // Determinar meta: si el tiempo es <= 200 min asumimos ruta local, sino paquetería
    const meta = h.minutos_prep <= 200 ? META_LOCAL : META_PAQ;
    const efic = Math.round((meta / h.minutos_prep) * 100);
    if (!eficPrepMap[h.colaborador]) eficPrepMap[h.colaborador] = [];
    eficPrepMap[h.colaborador].push(efic);
  }});

  const fPL = Object.keys(eficPrepMap).length ? Object.keys(eficPrepMap) : ['Sin datos'];
  const fPD = fPL.map(l => eficPrepMap[l] ? Math.round(eficPrepMap[l].reduce((a,b)=>a+b,0)/eficPrepMap[l].length) : 0);
  const fPColors = fPD.map(v => v >= 100 ? '#10b981' : v >= 80 ? '#f59e0b' : '#f43f5e');

  if(window._cEP) window._cEP.destroy();
  window._cEP = new Chart(document.getElementById('chartEficPrep'), {{
    type: 'bar',
    data: {{ labels: fPL, datasets: [{{ data: fPD, backgroundColor: fPColors, borderRadius: 4 }}] }},
    options: {{
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: {{ legend:{{display:false}},
                 tooltip:{{ callbacks:{{ label: ctx => ` ${{ctx.parsed.x}}% ${{ctx.parsed.x>=100?'✓ Óptimo':ctx.parsed.x>=80?'Aceptable':'Por mejorar'}}` }} }} }},
      scales: {{
        x: {{ min:0, max:130, ticks:{{color:'#6b7280',font:{{size:9}},callback:v=>v+'%'}}, grid:{{color:'#1f2937'}},
               plugins:{{ annotation:{{ annotations:{{ line1:{{ type:'line', xMin:100, xMax:100, borderColor:'#10b981', borderWidth:1, borderDash:[4,4] }} }} }} }} }},
        y: {{ ticks:{{color:'#9ca3af',font:{{size:10}}}}, grid:{{display:false}} }}
      }}
    }}
  }});
}}

// ── Helper flash ──────────────────────────────────────────────────────────
function showRes(id,msg,type){{
  const el=document.getElementById(id);
  el.className=`p-2.5 text-center text-xs font-bold rounded-xl ${{type==='ok'?'bg-emerald-500/20 text-emerald-400':'bg-red-500/20 text-red-400'}}`;
  el.innerText=msg;el.classList.remove('hidden');
}}
</script>
</body></html>"""
    return HTMLResponse(content=html)
