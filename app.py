# ============================================================
#  AquaMonitor - App de analisis del tanque de agua
#  Fuente de datos: CSV exportado desde Grafana (InfluxDB)
#  Variables: nivel del agua (%) y temperatura del agua (C)
# ============================================================

import unicodedata
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# ------------------------------------------------------------
# 1. CONFIGURACION DEL CASO (umbrales definidos en el proyecto)
# ------------------------------------------------------------
ZONA_HORARIA = "America/Bogota"
ALTURA_TANQUE_CM = 100

NIVEL_CRITICO = 10    # % por debajo: nivel critico
NIVEL_BAJO = 20       # % por debajo: nivel bajo (encender bomba)
NIVEL_ALTO = 95       # % por encima: riesgo de desbordamiento (apagar bomba)
TEMP_BAJA = 10        # C por debajo: valor inusual (revisar sensor)
TEMP_ALTA = 25        # C por encima: agua tibia (favorece bacterias)

# El CSV real exportado de Grafana puede estar en la carpeta data/ o junto a app.py
CARPETA_APP = Path(__file__).parent
CSV_REPOSITORIO = next(
    (r for r in (CARPETA_APP / "data" / "registros_tanque.csv",
                 CARPETA_APP / "registros_tanque.csv") if r.exists()),
    CARPETA_APP / "data" / "registros_tanque.csv",
)

# ---------- Paleta visual ----------
AZUL = "#0B6BCB"
AZUL_CLARO = "#E8F2FC"
TINTA = "#0F2537"
GRIS = "#5B7083"
BORDE = "#E3EBF3"

ESTADOS = {
    # estado: (color, fondo suave, icono, texto para el operador)
    "Normal": ("#1E9E6A", "#E6F6EF", "✅", "Dentro del rango normal"),
    "Bajo": ("#C98A00", "#FFF6DD", "⚠️", "Nivel bajo: encender la bomba"),
    "Critico": ("#D64545", "#FDECEC", "⛔", "Nivel crítico: encender la bomba ya"),
    "Desbordamiento": ("#7B61FF", "#F0EDFF", "⚠️", "Riesgo de desbordamiento: apagar la bomba"),
    "Tibia": ("#E8590C", "#FFF0E6", "🌡️", "Agua tibia: revisar sol y renovación"),
    "Fria (revisar)": ("#2F80ED", "#EAF2FE", "❓", "Valor inusual: revisar el sensor"),
}
NOMBRE_ESTADO = {"Critico": "Crítico", "Fria (revisar)": "Fría (revisar)"}
ORDEN_NIVEL = ["Critico", "Bajo", "Normal", "Desbordamiento"]
ORDEN_TEMP = ["Fria (revisar)", "Normal", "Tibia"]


class ErrorCSV(Exception):
    """Error con un mensaje claro para mostrar al usuario."""


# ------------------------------------------------------------
# 2. FUNCIONES DE LECTURA Y LIMPIEZA DEL CSV
# ------------------------------------------------------------
def normalizar(texto):
    """Minusculas y sin tildes, para comparar nombres de columnas."""
    texto = unicodedata.normalize("NFKD", str(texto))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return texto.strip().lower()


def buscar_columna(df, claves, exactas=()):
    """Devuelve la primera columna cuyo nombre coincide con alguna clave."""
    for col in df.columns:
        if normalizar(col) in exactas:
            return col
    for col in df.columns:
        nombre = normalizar(col)
        if any(clave in nombre for clave in claves):
            return col
    return None


def a_numero(serie):
    """Convierte textos como '63.1%', '25 °C' o '23,5' en numeros."""
    if pd.api.types.is_numeric_dtype(serie):
        return serie.astype(float)
    texto = serie.astype(str).str.replace(",", ".", regex=False)
    numero = texto.str.extract(r"(-?\d+(?:\.\d+)?)", expand=False)
    return pd.to_numeric(numero, errors="coerce")


def a_fecha(serie):
    """Convierte la columna de tiempo a fecha y hora de Colombia."""
    if pd.api.types.is_numeric_dtype(serie):
        # Grafana sin formato exporta milisegundos desde 1970 (UTC)
        fechas = pd.to_datetime(serie, unit="ms", utc=True, errors="coerce")
        return fechas.dt.tz_convert(ZONA_HORARIA).dt.tz_localize(None)

    texto = serie.astype(str).str.strip()
    tiene_zona = texto.str.contains(r"(?:Z|[+-]\d{2}:?\d{2})$", regex=True).mean() > 0.5
    if tiene_zona:
        fechas = pd.to_datetime(texto, utc=True, errors="coerce", format="mixed")
        return fechas.dt.tz_convert(ZONA_HORARIA).dt.tz_localize(None)
    # Grafana con formato exporta la hora local del navegador (Colombia)
    return pd.to_datetime(texto, errors="coerce", format="mixed")


def leer_un_csv(origen, nombre):
    """Lee un CSV de Grafana y devuelve tiempo, nivel y/o temperatura."""
    try:
        df = pd.read_csv(origen, sep=None, engine="python", encoding="utf-8-sig")
    except pd.errors.EmptyDataError:
        raise ErrorCSV(f"El archivo **{nombre}** está vacío.")
    except Exception as e:
        if "delimiter" in str(e):
            raise ErrorCSV(f"El archivo **{nombre}** está vacío o no tiene formato CSV.")
        raise ErrorCSV(f"No se pudo leer **{nombre}** como CSV ({e}).")

    if df.empty:
        raise ErrorCSV(f"El archivo **{nombre}** no tiene filas de datos.")

    col_tiempo = buscar_columna(df, ["time", "fecha", "timestamp", "date"],
                                exactas=("time", "_time", "fecha", "timestamp"))
    col_nivel = buscar_columna(df, ["nivel", "level"])
    col_temp = buscar_columna(df, ["temp"])

    if col_tiempo is None:
        raise ErrorCSV(
            f"**{nombre}** no tiene columna de tiempo. Columnas encontradas: "
            f"{', '.join(map(str, df.columns))}. Debe existir una columna 'time'."
        )
    if col_nivel is None and col_temp is None:
        raise ErrorCSV(
            f"**{nombre}** no tiene columnas 'nivel' ni 'temperatura'. "
            f"Columnas encontradas: {', '.join(map(str, df.columns))}."
        )

    salida = pd.DataFrame({"tiempo": a_fecha(df[col_tiempo])})
    if col_nivel is not None:
        salida["nivel"] = a_numero(df[col_nivel])
    if col_temp is not None:
        salida["temperatura"] = a_numero(df[col_temp])

    salida = salida.dropna(subset=["tiempo"]).sort_values("tiempo")
    if salida.empty:
        raise ErrorCSV(f"En **{nombre}** ninguna fecha de la columna de tiempo es válida.")
    return salida


def combinar(tablas):
    """Une uno o dos CSV (por ejemplo, uno por panel) en una sola tabla."""
    completa = next((t for t in tablas if {"nivel", "temperatura"} <= set(t.columns)), None)
    if completa is not None:
        return completa

    con_nivel = [t for t in tablas if "nivel" in t.columns]
    con_temp = [t for t in tablas if "temperatura" in t.columns]
    if not con_nivel or not con_temp:
        falta = "nivel" if not con_nivel else "temperatura"
        raise ErrorCSV(
            f"Falta la variable **{falta}**. Sube el CSV de la tabla con ambas "
            "variables, o los dos CSV (uno por panel)."
        )
    nivel = con_nivel[0][["tiempo", "nivel"]].sort_values("tiempo")
    temp = con_temp[0][["tiempo", "temperatura"]].sort_values("tiempo")
    return pd.merge_asof(nivel, temp, on="tiempo", direction="nearest",
                         tolerance=pd.Timedelta("30s"))


def limpiar(df):
    """Quita filas vacias y valores imposibles; informa cuantas se quitaron."""
    inicial = len(df)
    df = df.dropna(subset=["nivel", "temperatura"])
    df = df[df["nivel"].between(0, 100) & df["temperatura"].between(-55, 125)]
    df = df.drop_duplicates(subset="tiempo").sort_values("tiempo").reset_index(drop=True)
    return df, inicial - len(df)


# ------------------------------------------------------------
# 3. FUNCIONES DE ANALISIS
# ------------------------------------------------------------
def estado_nivel(v):
    if v < NIVEL_CRITICO:
        return "Critico"
    if v < NIVEL_BAJO:
        return "Bajo"
    if v > NIVEL_ALTO:
        return "Desbordamiento"
    return "Normal"


def estado_temp(v):
    if v < TEMP_BAJA:
        return "Fria (revisar)"
    if v > TEMP_ALTA:
        return "Tibia"
    return "Normal"


def duracion_por_registro(df):
    """Minutos que representa cada registro (hasta el siguiente)."""
    dif = df["tiempo"].diff().shift(-1).dt.total_seconds() / 60
    mediana = dif.median() if dif.notna().any() else 0
    tope = mediana * 3 if mediana and mediana > 0 else 1
    return dif.fillna(mediana).clip(upper=tope)


def tiempo_en_estados(df, columna_estado, orden):
    total = df["minutos"].sum()
    filas = []
    for estado in orden:
        minutos = df.loc[df[columna_estado] == estado, "minutos"].sum()
        filas.append({"Estado": estado, "Minutos": round(minutos, 1),
                      "% del tiempo": round(minutos / total * 100, 1) if total else 0})
    return pd.DataFrame(filas)


def episodios(df, condicion, nombre):
    """Agrupa registros consecutivos que cumplen una condicion."""
    grupo = (condicion != condicion.shift()).cumsum()
    filas = []
    for _, bloque in df[condicion].groupby(grupo[condicion]):
        filas.append({
            "Evento": nombre,
            "Inicio": bloque["tiempo"].iloc[0],
            "Fin": bloque["tiempo"].iloc[-1],
            "Duracion (min)": round(float(bloque["minutos"].sum()), 1),
            "Nivel min (%)": round(float(bloque["nivel"].min()), 1),
            "Nivel max (%)": round(float(bloque["nivel"].max()), 1),
            "Temp max (C)": round(float(bloque["temperatura"].max()), 1),
        })
    return filas


def pendiente_por_hora(df, columna):
    """Tendencia lineal: cuanto sube o baja la variable por hora."""
    if len(df) < 3:
        return None
    horas = (df["tiempo"] - df["tiempo"].iloc[0]).dt.total_seconds() / 3600
    if horas.std() == 0:
        return None
    return np.polyfit(horas, df[columna], 1)[0]


def interpretar_correlacion(r):
    fuerza = abs(r)
    if fuerza >= 0.7:
        texto = "fuerte"
    elif fuerza >= 0.4:
        texto = "moderada"
    elif fuerza >= 0.2:
        texto = "débil"
    else:
        return "Prácticamente no hay relación lineal entre el nivel y la temperatura."
    sentido = "negativa" if r < 0 else "positiva"
    explicacion = (
        "cuando el tanque tiene menos agua, la temperatura tiende a ser mayor "
        "(un volumen pequeño se calienta más rápido)"
        if r < 0 else
        "cuando el nivel sube, la temperatura también tiende a subir"
    )
    return f"Correlación {sentido} {texto}: {explicacion}."


# ------------------------------------------------------------
# 4. ESTILO Y COMPONENTES VISUALES
# ------------------------------------------------------------
CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
html, body, [class*="css"], .stMarkdown, .stText {{ font-family: 'Inter', sans-serif; }}
.block-container {{ padding-top: 2rem; padding-bottom: 3rem; max-width: 1250px; }}
h1, h2, h3 {{ color: {TINTA}; letter-spacing: -0.01em; }}
.hero {{ background: linear-gradient(120deg, #0B6BCB 0%, #0A8FD6 55%, #19B5C9 100%);
  border-radius: 22px; padding: 30px 34px; color: #fff; margin-bottom: 8px; }}
.hero h1 {{ color: #fff; font-size: 2.3rem; font-weight: 800; margin: 0 0 6px 0; }}
.hero p {{ color: rgba(255,255,255,.92); font-size: 1.02rem; margin: 0; max-width: 820px; line-height: 1.55; }}
.chips {{ margin-top: 16px; }}
.chip {{ display: inline-block; background: rgba(255,255,255,.18); border: 1px solid rgba(255,255,255,.35);
  color: #fff; padding: 4px 12px; border-radius: 999px; font-size: .8rem; font-weight: 600; margin: 0 6px 6px 0; }}
.sec {{ font-size: 1.35rem; font-weight: 800; color: {TINTA}; margin: 34px 0 2px 0; }}
.sec-sub {{ color: {GRIS}; font-size: .95rem; margin-bottom: 14px; }}
.card {{ background: #fff; border: 1px solid {BORDE}; border-radius: 18px; padding: 18px 20px;
  box-shadow: 0 1px 3px rgba(15,37,55,.05); height: 100%; color: {TINTA}; }}
.k-label {{ color: {GRIS}; font-size: .82rem; font-weight: 600; text-transform: uppercase; letter-spacing: .04em; }}
.k-value {{ font-size: 2.5rem; font-weight: 800; color: {TINTA}; line-height: 1.1; margin: 6px 0 8px 0; }}
.k-unit {{ font-size: 1.1rem; font-weight: 600; color: {GRIS}; margin-left: 4px; }}
.k-range {{ color: {GRIS}; font-size: .82rem; margin-top: 10px; }}
.pill {{ display: inline-block; padding: 4px 12px; border-radius: 999px; font-size: .82rem; font-weight: 700; }}
.alert {{ border-radius: 14px; padding: 12px 14px; margin-bottom: 10px; font-size: .92rem; font-weight: 600; }}
.mini {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-top: 10px; }}
.mini div {{ background: {AZUL_CLARO}; border-radius: 12px; padding: 10px 12px; }}
.mini b {{ display: block; font-size: 1.25rem; color: {TINTA}; }}
.mini span {{ font-size: .75rem; color: {GRIS}; font-weight: 600; text-transform: uppercase; }}
.insight {{ background: #fff; border: 1px solid {BORDE}; border-left: 6px solid {AZUL};
  border-radius: 14px; padding: 16px 20px; color: {TINTA}; }}
.insight li {{ margin: 6px 0; line-height: 1.5; }}
.tank {{ position: relative; height: 230px; width: 130px; margin: 6px auto 0 auto;
  border: 3px solid #9DB4C8; border-top: none; border-radius: 0 0 26px 26px; background: #F4F8FB; overflow: hidden; }}
.tank-fill {{ position: absolute; bottom: 0; left: 0; right: 0; border-radius: 0 0 22px 22px; opacity: .9; }}
.tank-line {{ position: absolute; left: 0; right: 0; border-top: 2px dashed rgba(15,37,55,.35); }}
.tank-tag {{ position: absolute; right: 4px; font-size: .65rem; color: {TINTA}; font-weight: 700; transform: translateY(-110%); }}
.tank-val {{ position: absolute; top: 42%; width: 100%; text-align: center; font-size: 1.6rem; font-weight: 800;
  color: {TINTA}; text-shadow: 0 1px 0 rgba(255,255,255,.8); }}
.legend-row {{ display: flex; align-items: center; gap: 8px; margin: 5px 0; font-size: .88rem; }}
.dot {{ width: 12px; height: 12px; border-radius: 50%; display: inline-block; flex: none; }}
.howto {{ background: {AZUL_CLARO}; color: {TINTA}; border-radius: 12px; padding: 10px 14px; font-size: .86rem; margin: 4px 0 10px 0; }}
.scale {{ position: relative; height: 12px; border-radius: 999px; margin: 18px 0 6px 0;
  background: linear-gradient(90deg, #D64545 0%, #F2C94C 35%, #E3EBF3 50%, #F2C94C 65%, #1E9E6A 100%); }}
.scale-mark {{ position: absolute; top: -6px; width: 6px; height: 24px; background: {TINTA}; border-radius: 3px; }}
.scale-labels {{ display: flex; justify-content: space-between; color: {GRIS}; font-size: .75rem; }}
.event {{ border: 1px solid {BORDE}; border-radius: 14px; padding: 12px 16px; margin-bottom: 10px; background: #fff; color: {TINTA}; }}
.footer {{ color: {GRIS}; font-size: .82rem; text-align: center; margin-top: 40px; }}
[data-baseweb="tab"] {{ font-weight: 600; }}
</style>
"""


def html(codigo):
    """Muestra HTML sin lineas vacias (para que Markdown no lo altere)."""
    limpio = "\n".join(linea.strip() for linea in codigo.strip().splitlines() if linea.strip())
    st.markdown(limpio, unsafe_allow_html=True)


def seccion(titulo, subtitulo=""):
    html(f'<div class="sec">{titulo}</div>' + (f'<div class="sec-sub">{subtitulo}</div>' if subtitulo else ""))


def pill(estado):
    color, fondo, icono, _ = ESTADOS[estado]
    return (f'<span class="pill" style="color:{color};background:{fondo};">'
            f'{icono} {NOMBRE_ESTADO.get(estado, estado)}</span>')


def caja():
    try:
        return st.container(border=True)
    except TypeError:
        return st.container()


def estilo_grafica(fig, alto=380, titulo=""):
    fig.update_layout(
        title=dict(text=titulo, font=dict(size=16, color=TINTA)),
        template="plotly_white", height=alto,
        font=dict(family="Inter, sans-serif", color=TINTA, size=12),
        paper_bgcolor="white", plot_bgcolor="white",
        margin=dict(l=10, r=10, t=50 if titulo else 20, b=10),
        legend=dict(orientation="h", y=1.08, x=1, xanchor="right"),
        hoverlabel=dict(bgcolor="white", font_size=12),
    )
    fig.update_xaxes(gridcolor="#EEF3F8", linecolor=BORDE)
    fig.update_yaxes(gridcolor="#EEF3F8", linecolor=BORDE)
    return fig


def mostrar_grafica(fig):
    config = {"displaylogo": False}
    try:
        st.plotly_chart(fig, width="stretch", config=config)
    except Exception:
        st.plotly_chart(fig, use_container_width=True, config=config)


def mostrar_tabla(df):
    try:
        st.dataframe(df, hide_index=True, width="stretch")
    except Exception:
        st.dataframe(df, hide_index=True, use_container_width=True)


def grafica_historica(df, columna, nombre, unidad, bandas, rango_y, estado_col, color_linea):
    fig = go.Figure()
    for (y0, y1, estado) in bandas:
        color = ESTADOS[estado][0]
        fig.add_hrect(y0=y0, y1=y1, fillcolor=color, opacity=0.08, line_width=0,
                      annotation_text=NOMBRE_ESTADO.get(estado, estado),
                      annotation_position="top left",
                      annotation_font=dict(size=11, color=color))
    fig.add_trace(go.Scatter(x=df["tiempo"], y=df[columna], mode="lines", name=nombre,
                             line=dict(color=color_linea, width=2.5, shape="hv"),
                             hovertemplate="%{y:.1f} " + unidad + "<extra></extra>"))
    fuera = df[df[estado_col] != "Normal"]
    if not fuera.empty:
        fig.add_trace(go.Scatter(
            x=fuera["tiempo"], y=fuera[columna], mode="markers", name="Fuera de rango",
            marker=dict(color=[ESTADOS[e][0] for e in fuera[estado_col]], size=6,
                        line=dict(color="white", width=1)),
            customdata=[NOMBRE_ESTADO.get(e, e) for e in fuera[estado_col]],
            hovertemplate="%{customdata}<extra></extra>"))
    fig.update_layout(hovermode="x unified", yaxis_range=rango_y,
                      yaxis_title=f"{nombre} ({unidad})", xaxis_title="Hora (Colombia)")
    return estilo_grafica(fig)


def barra_estados(resumen, titulo):
    fig = go.Figure()
    for _, fila in resumen.iterrows():
        if fila["% del tiempo"] <= 0:
            continue
        estado = fila["Estado"]
        fig.add_trace(go.Bar(
            x=[fila["% del tiempo"]], y=[""], orientation="h",
            name=NOMBRE_ESTADO.get(estado, estado), marker_color=ESTADOS[estado][0],
            text=f'{fila["% del tiempo"]:.0f}%' if fila["% del tiempo"] >= 5 else "",
            textposition="inside", insidetextanchor="middle",
            textfont=dict(color="white", size=13),
            hovertemplate=f"{NOMBRE_ESTADO.get(estado, estado)}: "
                          f"{fila['% del tiempo']:.1f}% ({fila['Minutos']:.1f} min)<extra></extra>"))
    fig.update_layout(barmode="stack", xaxis=dict(range=[0, 100], ticksuffix="%"),
                      bargap=0.35)
    return estilo_grafica(fig, alto=170, titulo=titulo)


# ------------------------------------------------------------
# 5. INTERFAZ
# ------------------------------------------------------------
def main():
    st.set_page_config(page_title="AquaMonitor · Análisis del tanque",
                       page_icon="💧", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)

    # ---------- Encabezado ----------
    html("""
    <div class="hero">
      <h1>💧 AquaMonitor</h1>
      <p>Análisis del <b>tanque elevado de agua</b>: nivel del agua y temperatura medidos por un ESP32,
      enviados por MQTT, guardados en InfluxDB y exportados desde Grafana. Esta página te dice
      <b>cómo está el tanque, cuándo salió de lo normal y qué debe hacer el operador</b>.</p>
      <div class="chips">
        <span class="chip">📏 HC-SR04 · Nivel (%)</span>
        <span class="chip">🌡️ DS18B20 · Temperatura (°C)</span>
        <span class="chip">🔌 Relé · Bomba de llenado</span>
        <span class="chip">ESP32 → MQTT → InfluxDB → Grafana → CSV</span>
      </div>
    </div>
    """)

    # ---------- Barra lateral ----------
    with st.sidebar:
        st.markdown("### 💧 Caso real")
        st.markdown(
            "**Tanque elevado de agua potable** de una vivienda o edificio. "
            f"Altura útil simulada: **{ALTURA_TANQUE_CM} cm**. "
            "El operador puede encender o apagar la **bomba de llenado** a distancia (`ON` / `OFF`)."
        )
        st.markdown("### 🎨 Cómo leer los colores")
        filas = "".join(
            f'<div class="legend-row"><span class="dot" style="background:{ESTADOS[e][0]}"></span>'
            f'<b>{NOMBRE_ESTADO.get(e, e)}</b>&nbsp;<span style="color:{GRIS}">{rango}</span></div>'
            for e, rango in [
                ("Critico", f"nivel < {NIVEL_CRITICO} %"),
                ("Bajo", f"nivel {NIVEL_CRITICO}–{NIVEL_BAJO} %"),
                ("Normal", f"nivel {NIVEL_BAJO}–{NIVEL_ALTO} % · temp {TEMP_BAJA}–{TEMP_ALTA} °C"),
                ("Desbordamiento", f"nivel > {NIVEL_ALTO} %"),
                ("Tibia", f"temp > {TEMP_ALTA} °C"),
                ("Fria (revisar)", f"temp < {TEMP_BAJA} °C"),
            ])
        html(filas)
        st.caption("Umbrales definidos como criterios de diseño del proyecto.")

    # ---------- Carga del CSV ----------
    seccion("📂 Datos", "Sube el CSV exportado desde Grafana. Si no subes ninguno, se usa el "
                       "CSV real del proyecto incluido en el repositorio.")
    archivos = st.file_uploader(
        "CSV del panel 'Registros del tanque' (o los dos CSV de los paneles de nivel y temperatura)",
        type=["csv"], accept_multiple_files=True)

    try:
        if archivos:
            tablas = [leer_un_csv(a, a.name) for a in archivos]
            fuente = ", ".join(a.name for a in archivos)
        elif CSV_REPOSITORIO.exists():
            tablas = [leer_un_csv(CSV_REPOSITORIO, CSV_REPOSITORIO.name)]
            fuente = f"{CSV_REPOSITORIO.name} (exportado de Grafana, incluido en el repositorio)"
        else:
            st.info("Sube el archivo CSV exportado desde Grafana para comenzar el análisis.")
            st.stop()

        datos = combinar(tablas)
        datos, quitadas = limpiar(datos)
    except ErrorCSV as e:
        st.error(str(e))
        st.info("Exporta el CSV desde Grafana: menú del panel → Inspect → Data → Download CSV.")
        st.stop()

    if len(datos) < 2:
        st.error("El CSV tiene menos de 2 registros válidos con nivel y temperatura. "
                 "Exporta un intervalo de tiempo más amplio desde Grafana.")
        st.stop()

    st.success(f"Columnas validadas: tiempo, nivel y temperatura · Fuente: {fuente}")
    if quitadas:
        st.warning(f"Se descartaron {quitadas} filas vacías, duplicadas o con valores imposibles.")

    inicio_total = datos["tiempo"].min().to_pydatetime()
    fin_total = datos["tiempo"].max().to_pydatetime()
    if inicio_total < fin_total:
        rango = st.slider("Periodo a analizar", min_value=inicio_total, max_value=fin_total,
                          value=(inicio_total, fin_total), format="DD/MM HH:mm")
        datos = datos[datos["tiempo"].between(rango[0], rango[1])].reset_index(drop=True)
    if len(datos) < 2:
        st.warning("El periodo elegido tiene menos de 2 registros. Amplía el rango.")
        st.stop()

    datos["estado_nivel"] = datos["nivel"].apply(estado_nivel)
    datos["estado_temp"] = datos["temperatura"].apply(estado_temp)
    datos["minutos"] = duracion_por_registro(datos)

    ultimo = datos.iloc[-1]
    e_nivel, e_temp = ultimo["estado_nivel"], ultimo["estado_temp"]
    duracion_total = (datos["tiempo"].iloc[-1] - datos["tiempo"].iloc[0]).total_seconds() / 60

    # ---------- Estado actual ----------
    seccion("🚰 Estado del tanque",
            f"Última medición: {ultimo['tiempo']:%d/%m/%Y a las %H:%M:%S}")
    c_tanque, c_nivel, c_temp, c_alertas = st.columns([1, 1.2, 1.2, 1.6])

    with c_tanque:
        color_fill = ESTADOS[e_nivel][0]
        html(f"""
        <div class="card" style="text-align:center;">
          <div class="k-label">Tanque</div>
          <div class="tank">
            <div class="tank-fill" style="height:{ultimo['nivel']:.1f}%;background:{color_fill};"></div>
            <div class="tank-line" style="bottom:{NIVEL_ALTO}%;"></div>
            <div class="tank-tag" style="bottom:{NIVEL_ALTO}%;">{NIVEL_ALTO}%</div>
            <div class="tank-line" style="bottom:{NIVEL_BAJO}%;"></div>
            <div class="tank-tag" style="bottom:{NIVEL_BAJO}%;">{NIVEL_BAJO}%</div>
            <div class="tank-val">{ultimo['nivel']:.0f}%</div>
          </div>
        </div>
        """)

    with c_nivel:
        html(f"""
        <div class="card">
          <div class="k-label">📏 Nivel del agua</div>
          <div class="k-value">{ultimo['nivel']:.1f}<span class="k-unit">%</span></div>
          {pill(e_nivel)}
          <div class="k-range">Rango normal: {NIVEL_BAJO}–{NIVEL_ALTO} %</div>
        </div>
        """)

    with c_temp:
        html(f"""
        <div class="card">
          <div class="k-label">🌡️ Temperatura del agua</div>
          <div class="k-value">{ultimo['temperatura']:.1f}<span class="k-unit">°C</span></div>
          {pill(e_temp)}
          <div class="k-range">Rango normal: {TEMP_BAJA}–{TEMP_ALTA} °C</div>
        </div>
        """)

    with c_alertas:
        avisos = [e for e in (e_nivel, e_temp) if e != "Normal"]
        if avisos:
            cuerpo = "".join(
                f'<div class="alert" style="background:{ESTADOS[e][1]};color:{ESTADOS[e][0]};">'
                f'{ESTADOS[e][2]} {ESTADOS[e][3]}</div>' for e in avisos)
        else:
            c, f, i, t = ESTADOS["Normal"]
            cuerpo = (f'<div class="alert" style="background:{f};color:{c};">'
                      f'{i} Todo en orden: nivel y temperatura dentro de los rangos normales.</div>')
        html(f"""
        <div class="card">
          <div class="k-label">🔔 Qué debe hacer el operador</div>
          <div style="margin-top:12px;">{cuerpo}</div>
        </div>
        """)

    # ---------- Resumen en palabras ----------
    t_nivel = tiempo_en_estados(datos, "estado_nivel", ORDEN_NIVEL).set_index("Estado")
    t_temp = tiempo_en_estados(datos, "estado_temp", ORDEN_TEMP).set_index("Estado")
    eventos = (episodios(datos, datos["nivel"] < NIVEL_BAJO, f"Nivel bajo (< {NIVEL_BAJO} %)") +
               episodios(datos, datos["nivel"] > NIVEL_ALTO, f"Riesgo de desbordamiento (> {NIVEL_ALTO} %)"))
    n_bajo = sum(1 for e in eventos if e["Evento"].startswith("Nivel bajo"))
    n_alto = len(eventos) - n_bajo
    r = (datos["nivel"].corr(datos["temperatura"])
         if datos["nivel"].std() > 0 and datos["temperatura"].std() > 0 else None)

    frases = [
        f"Se analizaron <b>{len(datos)} mediciones</b> durante <b>{duracion_total:.0f} minutos</b> "
        f"({datos['tiempo'].iloc[0]:%d/%m %H:%M} a {datos['tiempo'].iloc[-1]:%d/%m %H:%M}).",
        f"El nivel estuvo en rango normal el <b>{t_nivel.loc['Normal', '% del tiempo']:.0f} %</b> del tiempo; "
        f"llegó a un mínimo de <b>{datos['nivel'].min():.1f} %</b> y un máximo de <b>{datos['nivel'].max():.1f} %</b>.",
        f"Hubo <b>{n_bajo} evento(s) de nivel bajo</b> y <b>{n_alto} de riesgo de desbordamiento</b>.",
        f"El agua estuvo tibia (> {TEMP_ALTA} °C) durante <b>{t_temp.loc['Tibia', 'Minutos']:.0f} minutos</b> "
        f"({t_temp.loc['Tibia', '% del tiempo']:.0f} % del periodo).",
    ]
    if r is not None:
        frases.append(f"{interpretar_correlacion(r)} (r = {r:.2f})")
    seccion("📝 Resumen del periodo", "Lo más importante, en palabras sencillas.")
    html('<div class="insight"><ul style="margin:0;padding-left:18px;">' +
         "".join(f"<li>{f}</li>" for f in frases) + "</ul></div>")

    # ---------- Estadisticas ----------
    seccion("📊 Estadísticas descriptivas", "Promedio, máximo, mínimo y cantidad de registros del periodo.")
    s1, s2 = st.columns(2)
    for col, variable, nombre, unidad, icono in [(s1, "nivel", "Nivel del agua", "%", "📏"),
                                                 (s2, "temperatura", "Temperatura del agua", "°C", "🌡️")]:
        serie = datos[variable]
        with col:
            html(f"""
            <div class="card">
              <div class="k-label">{icono} {nombre} ({unidad})</div>
              <div class="mini">
                <div><span>Promedio</span><b>{serie.mean():.1f} {unidad}</b></div>
                <div><span>Máximo</span><b>{serie.max():.1f} {unidad}</b></div>
                <div><span>Mínimo</span><b>{serie.min():.1f} {unidad}</b></div>
                <div><span>Registros</span><b>{serie.count()}</b></div>
              </div>
              <div class="k-range">Desviación estándar: {serie.std():.2f} {unidad}</div>
            </div>
            """)

    # ---------- Graficas historicas ----------
    seccion("📈 Histórico", "Cómo cambiaron las variables en el tiempo.")
    html('<div class="howto">💡 <b>Cómo leer:</b> la franja verde es el rango normal. Los puntos de color '
         'marcan las mediciones fuera de rango (amarillo = bajo, rojo = crítico o tibia, '
         'morado = desbordamiento). Pasa el mouse sobre la línea para ver el valor exacto.</div>')
    with caja():
        mostrar_grafica(grafica_historica(
            datos, "nivel", "Nivel del agua", "%",
            bandas=[(0, NIVEL_CRITICO, "Critico"), (NIVEL_CRITICO, NIVEL_BAJO, "Bajo"),
                    (NIVEL_BAJO, NIVEL_ALTO, "Normal"), (NIVEL_ALTO, 100, "Desbordamiento")],
            rango_y=[0, 100], estado_col="estado_nivel", color_linea=AZUL))
    t_max = max(40, datos["temperatura"].max() + 2)
    with caja():
        mostrar_grafica(grafica_historica(
            datos, "temperatura", "Temperatura del agua", "°C",
            bandas=[(0, TEMP_BAJA, "Fria (revisar)"), (TEMP_BAJA, TEMP_ALTA, "Normal"),
                    (TEMP_ALTA, t_max, "Tibia")],
            rango_y=[0, t_max], estado_col="estado_temp", color_linea="#E8590C"))

    # ---------- Valores fuera de umbral ----------
    seccion("🚨 Mediciones fuera de los umbrales",
            "Registros en los que el nivel o la temperatura salieron del rango normal.")
    fuera = datos[(datos["estado_nivel"] != "Normal") | (datos["estado_temp"] != "Normal")]
    n_fuera_n = int((datos["estado_nivel"] != "Normal").sum())
    n_fuera_t = int((datos["estado_temp"] != "Normal").sum())
    f1, f2, f3 = st.columns(3)
    for col, etiqueta, valor, extra in [
        (f1, "Total fuera de rango", f"{len(fuera)}", f"de {len(datos)} registros ({len(fuera) / len(datos) * 100:.0f} %)"),
        (f2, "📏 Nivel fuera de rango", f"{n_fuera_n}", "registros"),
        (f3, "🌡️ Temperatura fuera de rango", f"{n_fuera_t}", "registros"),
    ]:
        with col:
            html(f'<div class="card"><div class="k-label">{etiqueta}</div>'
                 f'<div class="k-value">{valor}</div><div class="k-range">{extra}</div></div>')

    if fuera.empty:
        st.success("Ningún registro superó los umbrales en este periodo.")
    else:
        tabla_fuera = pd.DataFrame({
            "Fecha y hora": fuera["tiempo"].dt.strftime("%d/%m/%Y %H:%M:%S"),
            "Nivel (%)": fuera["nivel"].round(1),
            "Estado del nivel": [f"{ESTADOS[e][2]} {NOMBRE_ESTADO.get(e, e)}" for e in fuera["estado_nivel"]],
            "Temperatura (°C)": fuera["temperatura"].round(1),
            "Estado de la temperatura": [f"{ESTADOS[e][2]} {NOMBRE_ESTADO.get(e, e)}" for e in fuera["estado_temp"]],
        })
        with st.expander(f"Ver los {len(fuera)} registros fuera de rango"):
            mostrar_tabla(tabla_fuera)
            st.download_button("⬇️ Descargar estos registros (CSV)",
                               tabla_fuera.to_csv(index=False).encode("utf-8"),
                               file_name="registros_fuera_de_rango.csv", mime="text/csv")

    # ---------- Analisis adicional ----------
    seccion("🔎 Análisis para el operador", "Información para tomar decisiones sobre la bomba y el agua.")
    tab1, tab2, tab3 = st.tabs(["⏱️ Tiempo en cada estado", "📋 Eventos de nivel",
                                "🔗 Nivel vs. temperatura"])

    with tab1:
        html('<div class="howto">💡 Cada barra es el 100 % del periodo, dividido según la condición '
             'del tanque. Lo ideal es que la mayor parte sea verde.</div>')
        with caja():
            mostrar_grafica(barra_estados(t_nivel.reset_index(), "Nivel del agua"))
            mostrar_grafica(barra_estados(t_temp.reset_index(), "Temperatura del agua"))

    with tab2:
        html('<div class="howto">💡 Periodos continuos en que el nivel salió del rango normal. '
             'Sirven para revisar si la bomba se encendió o se apagó a tiempo.</div>')
        if eventos:
            for ev in sorted(eventos, key=lambda e: e["Inicio"]):
                estado = "Bajo" if ev["Evento"].startswith("Nivel bajo") else "Desbordamiento"
                if estado == "Bajo" and ev["Nivel min (%)"] < NIVEL_CRITICO:
                    estado = "Critico"
                color, fondo, icono, consejo = ESTADOS[estado]
                html(f"""
                <div class="event" style="border-left:6px solid {color};">
                  <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;">
                    <b>{icono} {ev['Evento']}</b>
                    <span style="color:{GRIS};">{ev['Inicio']:%H:%M:%S} → {ev['Fin']:%H:%M:%S} · {ev['Duracion (min)']:.1f} min</span>
                  </div>
                  <div style="margin-top:6px;color:{GRIS};font-size:.9rem;">
                    Nivel entre <b style="color:{TINTA}">{ev['Nivel min (%)']:.1f} %</b> y
                    <b style="color:{TINTA}">{ev['Nivel max (%)']:.1f} %</b> ·
                    Temperatura máxima <b style="color:{TINTA}">{ev['Temp max (C)']:.1f} °C</b> ·
                    Acción: {consejo.split(': ')[-1]}
                  </div>
                </div>
                """)
        else:
            st.success("No hubo eventos de nivel bajo ni de desbordamiento en este periodo.")

    with tab3:
        if r is not None:
            posicion = (r + 1) / 2 * 100
            cr, cg = st.columns([1, 2])
            with cr:
                html(f"""
                <div class="card">
                  <div class="k-label">Correlación de Pearson (r)</div>
                  <div class="k-value">{r:.2f}</div>
                  <div class="scale"><div class="scale-mark" style="left:calc({posicion:.1f}% - 3px);"></div></div>
                  <div class="scale-labels"><span>−1 inversa</span><span>0 sin relación</span><span>+1 directa</span></div>
                  <p style="margin-top:14px;font-size:.92rem;line-height:1.5;">{interpretar_correlacion(r)}</p>
                </div>
                """)
                p_nivel = pendiente_por_hora(datos, "nivel")
                p_temp = pendiente_por_hora(datos, "temperatura")
                if p_nivel is not None and p_temp is not None:
                    html(f"""
                    <div class="card" style="margin-top:12px;">
                      <div class="k-label">Tendencia del periodo</div>
                      <div style="margin-top:8px;font-size:.95rem;line-height:1.7;">
                        📏 Nivel: <b>{p_nivel:+.1f} %/hora</b> ({'llenándose' if p_nivel > 0 else 'vaciándose'})<br>
                        🌡️ Temperatura: <b>{p_temp:+.2f} °C/hora</b> ({'calentándose' if p_temp > 0 else 'enfriándose'})
                      </div>
                    </div>
                    """)
            with cg:
                fig = go.Figure()
                for estado in ORDEN_NIVEL:
                    sub = datos[datos["estado_nivel"] == estado]
                    if sub.empty:
                        continue
                    fig.add_trace(go.Scatter(
                        x=sub["nivel"], y=sub["temperatura"], mode="markers",
                        name=NOMBRE_ESTADO.get(estado, estado),
                        marker=dict(color=ESTADOS[estado][0], size=8, opacity=0.75,
                                    line=dict(color="white", width=1)),
                        hovertemplate="Nivel %{x:.1f} %<br>Temperatura %{y:.1f} °C<extra></extra>"))
                if len(datos) >= 3:
                    m, b = np.polyfit(datos["nivel"], datos["temperatura"], 1)
                    xs = np.linspace(datos["nivel"].min(), datos["nivel"].max(), 50)
                    fig.add_trace(go.Scatter(x=xs, y=m * xs + b, mode="lines", name="Tendencia",
                                             line=dict(color=TINTA, dash="dash", width=2),
                                             hoverinfo="skip"))
                fig.update_layout(xaxis_title="Nivel del agua (%)",
                                  yaxis_title="Temperatura del agua (°C)")
                with caja():
                    mostrar_grafica(estilo_grafica(fig, alto=420,
                                                   titulo="Cada punto es una medición (color = estado del nivel)"))
        else:
            st.warning("Una de las variables no cambió en el periodo; no se puede calcular la correlación.")

    # ---------- Datos ----------
    with st.expander("📄 Ver todos los registros del periodo"):
        mostrar_tabla(pd.DataFrame({
            "Fecha y hora": datos["tiempo"].dt.strftime("%d/%m/%Y %H:%M:%S"),
            "Nivel (%)": datos["nivel"].round(1),
            "Temperatura (°C)": datos["temperatura"].round(1),
            "Estado del nivel": [NOMBRE_ESTADO.get(e, e) for e in datos["estado_nivel"]],
            "Estado de la temperatura": [NOMBRE_ESTADO.get(e, e) for e in datos["estado_temp"]],
        }))

    html('<div class="footer">AquaMonitor · Proyecto final de Computación Física e IoT · Universidad EAFIT<br>'
         'ESP32 simulado en Cirkit Designer → MQTT → InfluxDB → Grafana → CSV → Streamlit</div>')


if __name__ == "__main__":
    main()
