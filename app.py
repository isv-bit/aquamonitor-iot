# ============================================================
#  AquaMonitor - App de analisis del tanque de agua
#  Fuente de datos: CSV exportado desde Grafana (InfluxDB)
#  Variables: nivel del agua (%) y temperatura del agua (C)
# ============================================================

import re
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

COLOR_ESTADO = {
    "Critico": "#d62728",
    "Bajo": "#f2a900",
    "Normal": "#2ca02c",
    "Desbordamiento": "#9467bd",
    "Fria (revisar)": "#1f77b4",
    "Tibia": "#d62728",
}


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
        raise ErrorCSV(f"El archivo **{nombre}** esta vacio.")
    except Exception as e:
        if "delimiter" in str(e):
            raise ErrorCSV(f"El archivo **{nombre}** esta vacio o no tiene formato CSV.")
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
        raise ErrorCSV(f"En **{nombre}** ninguna fecha de la columna de tiempo es valida.")
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


def tiempo_en_estados(df, columna_estado):
    resumen = df.groupby(columna_estado)["minutos"].sum().reset_index()
    resumen.columns = ["Estado", "Minutos"]
    total = resumen["Minutos"].sum()
    resumen["% del tiempo"] = (resumen["Minutos"] / total * 100).round(1) if total else 0
    resumen["Minutos"] = resumen["Minutos"].round(1)
    return resumen.sort_values("Minutos", ascending=False)


def episodios(df, condicion, nombre):
    """Agrupa registros consecutivos que cumplen una condicion."""
    grupo = (condicion != condicion.shift()).cumsum()
    filas = []
    for _, bloque in df[condicion].groupby(grupo[condicion]):
        inicio, fin = bloque["tiempo"].iloc[0], bloque["tiempo"].iloc[-1]
        filas.append({
            "Evento": nombre,
            "Inicio": inicio.strftime("%Y-%m-%d %H:%M:%S"),
            "Fin": fin.strftime("%Y-%m-%d %H:%M:%S"),
            "Duracion (min)": round(bloque["minutos"].sum(), 1),
            "Nivel min (%)": round(bloque["nivel"].min(), 1),
            "Nivel max (%)": round(bloque["nivel"].max(), 1),
            "Temp max (C)": round(bloque["temperatura"].max(), 1),
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
        texto = "debil"
    else:
        return "Practicamente no hay relacion lineal entre el nivel y la temperatura."
    sentido = "negativa" if r < 0 else "positiva"
    explicacion = (
        "cuando el tanque tiene menos agua, la temperatura tiende a ser mayor "
        "(un volumen pequeno se calienta mas rapido)"
        if r < 0 else
        "cuando el nivel sube, la temperatura tambien tiende a subir"
    )
    return f"Correlacion {sentido} {texto}: {explicacion}."


# ------------------------------------------------------------
# 4. FUNCIONES DE GRAFICAS
# ------------------------------------------------------------
def grafica_variable(df, columna, titulo, unidad, bandas, rango_y, estado_col):
    fig = go.Figure()
    for (y0, y1, color, etiqueta) in bandas:
        fig.add_hrect(y0=y0, y1=y1, fillcolor=color, opacity=0.12, line_width=0,
                      annotation_text=etiqueta, annotation_position="top left",
                      annotation_font_size=11)
    fig.add_trace(go.Scatter(x=df["tiempo"], y=df[columna], mode="lines",
                             name=titulo, line=dict(color="#0094ce", width=2)))
    fuera = df[df[estado_col] != "Normal"]
    if not fuera.empty:
        fig.add_trace(go.Scatter(
            x=fuera["tiempo"], y=fuera[columna], mode="markers", name="Fuera de rango",
            marker=dict(color=[COLOR_ESTADO.get(e, "#d62728") for e in fuera[estado_col]],
                        size=6),
            text=fuera[estado_col], hovertemplate="%{x}<br>%{y:.1f} " + unidad + "<br>%{text}"))
    fig.update_layout(title=titulo, xaxis_title="Fecha y hora (Colombia)",
                      yaxis_title=f"{titulo} ({unidad})", yaxis_range=rango_y,
                      height=380, margin=dict(l=10, r=10, t=50, b=10),
                      legend=dict(orientation="h", y=-0.25))
    return fig


def mostrar_grafica(fig):
    try:
        st.plotly_chart(fig, width="stretch")
    except Exception:
        st.plotly_chart(fig, use_container_width=True)


def mostrar_tabla(df):
    try:
        st.dataframe(df, hide_index=True, width="stretch")
    except Exception:
        st.dataframe(df, hide_index=True, use_container_width=True)


# ------------------------------------------------------------
# 5. INTERFAZ
# ------------------------------------------------------------
def main():
    st.set_page_config(page_title="AquaMonitor - Analisis del tanque",
                       page_icon="💧", layout="wide")

    st.title("💧 AquaMonitor: análisis del tanque de agua")
    st.markdown(
        "Sistema IoT de **telemetría y telecontrol** de un tanque elevado de agua. "
        "Un ESP32 mide el **nivel del agua** (sensor ultrasónico HC-SR04) y la "
        "**temperatura del agua** (sensor DS18B20), publica los datos por MQTT, los "
        "guarda en InfluxDB y permite encender la bomba de llenado a distancia. "
        "Esta app analiza el histórico exportado desde **Grafana** en formato CSV."
    )

    # ---------- Barra lateral ----------
    with st.sidebar:
        st.header("Caso real")
        st.markdown(
            "**Tanque elevado de agua potable** para una vivienda o edificio.\n\n"
            f"- Altura útil simulada: {ALTURA_TANQUE_CM} cm\n"
            "- Actuador: relé de la bomba de llenado\n"
            "- Comandos remotos: `ON` / `OFF`"
        )
        st.subheader("Umbrales del sistema")
        st.markdown(
            f"**Nivel del agua (%)**\n"
            f"- ⛔ Crítico: < {NIVEL_CRITICO} %\n"
            f"- ⚠️ Bajo: {NIVEL_CRITICO}–{NIVEL_BAJO} % (encender bomba)\n"
            f"- ✅ Normal: {NIVEL_BAJO}–{NIVEL_ALTO} %\n"
            f"- ⚠️ Desbordamiento: > {NIVEL_ALTO} % (apagar bomba)\n\n"
            f"**Temperatura del agua (°C)**\n"
            f"- ❓ Inusual: < {TEMP_BAJA} °C (revisar sensor)\n"
            f"- ✅ Normal: {TEMP_BAJA}–{TEMP_ALTA} °C\n"
            f"- 🌡️ Tibia: > {TEMP_ALTA} °C (favorece bacterias)"
        )
        st.caption("Umbrales definidos como criterios de diseño del proyecto.")

    # ---------- Carga del CSV ----------
    st.header("1. Cargar datos")
    archivos = st.file_uploader(
        "Sube el CSV exportado desde Grafana (panel 'Registros del tanque'). "
        "También puedes subir los dos CSV de los paneles de nivel y temperatura.",
        type=["csv"], accept_multiple_files=True)

    try:
        if archivos:
            tablas = [leer_un_csv(a, a.name) for a in archivos]
            fuente = ", ".join(a.name for a in archivos)
        elif CSV_REPOSITORIO.exists():
            tablas = [leer_un_csv(CSV_REPOSITORIO, CSV_REPOSITORIO.name)]
            fuente = f"{CSV_REPOSITORIO.name} (CSV exportado de Grafana incluido en el repositorio)"
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

    st.success(f"✅ Columnas validadas (tiempo, nivel, temperatura). Fuente: {fuente}")
    if quitadas:
        st.warning(f"Se descartaron {quitadas} filas vacías, duplicadas o con valores imposibles.")

    # ---------- Filtro de periodo ----------
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

    # ---------- Estado actual ----------
    st.header("2. Estado más reciente del tanque")
    ultimo = datos.iloc[-1]
    c1, c2, c3 = st.columns(3)
    c1.metric("Nivel del agua", f"{ultimo['nivel']:.1f} %", ultimo["estado_nivel"],
              delta_color="off")
    c2.metric("Temperatura del agua", f"{ultimo['temperatura']:.1f} °C", ultimo["estado_temp"],
              delta_color="off")
    c3.metric("Última medición", ultimo["tiempo"].strftime("%d/%m/%Y %H:%M:%S"))

    alertas = []
    if ultimo["estado_nivel"] == "Critico":
        alertas.append(("error", f"⛔ Nivel CRÍTICO ({ultimo['nivel']:.1f} %): encender la bomba de inmediato."))
    elif ultimo["estado_nivel"] == "Bajo":
        alertas.append(("warning", f"⚠️ Nivel bajo ({ultimo['nivel']:.1f} %): se recomienda encender la bomba."))
    elif ultimo["estado_nivel"] == "Desbordamiento":
        alertas.append(("warning", f"⚠️ Riesgo de desbordamiento ({ultimo['nivel']:.1f} %): apagar la bomba."))
    if ultimo["estado_temp"] == "Tibia":
        alertas.append(("warning", f"🌡️ Agua tibia ({ultimo['temperatura']:.1f} °C): revisar exposición al sol y renovación del agua."))
    elif ultimo["estado_temp"] == "Fria (revisar)":
        alertas.append(("warning", f"❓ Temperatura inusual ({ultimo['temperatura']:.1f} °C): revisar el sensor."))
    if not alertas:
        st.success("✅ Condiciones dentro de los rangos normales.")
    for tipo, texto in alertas:
        getattr(st, tipo)(texto)

    # ---------- Estadisticas ----------
    st.header("3. Estadísticas descriptivas")
    s1, s2 = st.columns(2)
    for col, variable, nombre, unidad in [(s1, "nivel", "Nivel del agua", "%"),
                                          (s2, "temperatura", "Temperatura del agua", "°C")]:
        with col:
            st.subheader(f"{nombre} ({unidad})")
            serie = datos[variable]
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Promedio", f"{serie.mean():.1f} {unidad}")
            m2.metric("Máximo", f"{serie.max():.1f} {unidad}")
            m3.metric("Mínimo", f"{serie.min():.1f} {unidad}")
            m4.metric("Registros", f"{serie.count()}")
            st.caption(f"Desviación estándar: {serie.std():.2f} {unidad}")

    duracion_total = (datos["tiempo"].iloc[-1] - datos["tiempo"].iloc[0]).total_seconds() / 60
    st.caption(f"Periodo analizado: {datos['tiempo'].iloc[0]:%d/%m/%Y %H:%M} a "
               f"{datos['tiempo'].iloc[-1]:%d/%m/%Y %H:%M} ({duracion_total:.0f} minutos).")

    # ---------- Graficas historicas ----------
    st.header("4. Gráficas históricas")
    fig_nivel = grafica_variable(
        datos, "nivel", "Nivel del agua", "%",
        bandas=[(0, NIVEL_CRITICO, "red", "Crítico"),
                (NIVEL_CRITICO, NIVEL_BAJO, "orange", "Bajo"),
                (NIVEL_BAJO, NIVEL_ALTO, "green", "Normal"),
                (NIVEL_ALTO, 100, "purple", "Desbordamiento")],
        rango_y=[0, 100], estado_col="estado_nivel")
    mostrar_grafica(fig_nivel)

    t_max = max(40, datos["temperatura"].max() + 2)
    fig_temp = grafica_variable(
        datos, "temperatura", "Temperatura del agua", "°C",
        bandas=[(0, TEMP_BAJA, "blue", "Inusual"),
                (TEMP_BAJA, TEMP_ALTA, "green", "Normal"),
                (TEMP_ALTA, t_max, "red", "Tibia")],
        rango_y=[0, t_max], estado_col="estado_temp")
    mostrar_grafica(fig_temp)

    # ---------- Valores fuera de umbral ----------
    st.header("5. Valores fuera de los umbrales")
    fuera = datos[(datos["estado_nivel"] != "Normal") | (datos["estado_temp"] != "Normal")]
    f1, f2, f3 = st.columns(3)
    f1.metric("Registros fuera de rango", f"{len(fuera)} de {len(datos)}",
              f"{len(fuera) / len(datos) * 100:.1f} %", delta_color="off")
    f2.metric("Nivel fuera de rango", int((datos["estado_nivel"] != "Normal").sum()))
    f3.metric("Temperatura fuera de rango", int((datos["estado_temp"] != "Normal").sum()))

    if fuera.empty:
        st.success("Ningún registro superó los umbrales en este periodo.")
    else:
        tabla_fuera = fuera[["tiempo", "nivel", "estado_nivel", "temperatura", "estado_temp"]].copy()
        tabla_fuera.columns = ["Fecha y hora", "Nivel (%)", "Estado nivel",
                               "Temperatura (°C)", "Estado temperatura"]
        tabla_fuera["Nivel (%)"] = tabla_fuera["Nivel (%)"].round(1)
        tabla_fuera["Temperatura (°C)"] = tabla_fuera["Temperatura (°C)"].round(1)
        mostrar_tabla(tabla_fuera)
        st.download_button("⬇️ Descargar registros fuera de rango (CSV)",
                           tabla_fuera.to_csv(index=False).encode("utf-8"),
                           file_name="registros_fuera_de_rango.csv", mime="text/csv")

    # ---------- Analisis adicional ----------
    st.header("6. Análisis para el operador")
    tab1, tab2, tab3 = st.tabs(["⏱️ Tiempo en cada estado", "🚨 Eventos de nivel",
                                "🔗 Relación nivel–temperatura y tendencias"])

    with tab1:
        st.markdown("¿Cuánto tiempo pasó el tanque en cada condición durante el periodo?")
        e1, e2 = st.columns(2)
        for col, estado_col, titulo in [(e1, "estado_nivel", "Nivel del agua"),
                                        (e2, "estado_temp", "Temperatura del agua")]:
            with col:
                resumen = tiempo_en_estados(datos, estado_col)
                fig = go.Figure(go.Bar(
                    x=resumen["% del tiempo"], y=resumen["Estado"], orientation="h",
                    marker_color=[COLOR_ESTADO.get(e, "#888") for e in resumen["Estado"]],
                    text=[f"{p} %" for p in resumen["% del tiempo"]], textposition="auto"))
                fig.update_layout(title=titulo, xaxis_title="% del tiempo", height=280,
                                  margin=dict(l=10, r=10, t=50, b=10))
                mostrar_grafica(fig)
                mostrar_tabla(resumen)

    with tab2:
        st.markdown("Periodos continuos en que el nivel salió del rango normal. "
                    "Sirven para revisar si la bomba se encendió o apagó a tiempo.")
        eventos = (episodios(datos, datos["nivel"] < NIVEL_BAJO, f"Nivel bajo (< {NIVEL_BAJO} %)") +
                   episodios(datos, datos["nivel"] > NIVEL_ALTO, f"Riesgo de desbordamiento (> {NIVEL_ALTO} %)"))
        if eventos:
            tabla_eventos = pd.DataFrame(eventos).sort_values("Inicio")
            mostrar_tabla(tabla_eventos)
            criticos = int((datos["nivel"] < NIVEL_CRITICO).sum())
            st.caption(f"Registros en nivel crítico (< {NIVEL_CRITICO} %): {criticos}.")
        else:
            st.success("No hubo eventos de nivel bajo ni de desbordamiento en este periodo.")

    with tab3:
        if datos["nivel"].std() > 0 and datos["temperatura"].std() > 0:
            r = datos["nivel"].corr(datos["temperatura"])
            st.metric("Coeficiente de correlación de Pearson (r)", f"{r:.2f}")
            st.info(interpretar_correlacion(r))
            fig = go.Figure(go.Scatter(
                x=datos["nivel"], y=datos["temperatura"], mode="markers",
                marker=dict(color=[COLOR_ESTADO.get(e, "#888") for e in datos["estado_nivel"]],
                            size=6, opacity=0.7),
                text=datos["estado_nivel"], name="Registros",
                hovertemplate="Nivel %{x:.1f} %<br>Temp %{y:.1f} °C<br>%{text}"))
            if len(datos) >= 3:
                m, b = np.polyfit(datos["nivel"], datos["temperatura"], 1)
                xs = np.linspace(datos["nivel"].min(), datos["nivel"].max(), 50)
                fig.add_trace(go.Scatter(x=xs, y=m * xs + b, mode="lines",
                                         name="Tendencia lineal", line=dict(color="#333", dash="dash")))
            fig.update_layout(title="Nivel vs. temperatura (color = estado del nivel)",
                              xaxis_title="Nivel del agua (%)",
                              yaxis_title="Temperatura del agua (°C)", height=400,
                              margin=dict(l=10, r=10, t=50, b=10))
            mostrar_grafica(fig)
        else:
            st.warning("Una de las variables no cambió en el periodo; no se puede calcular la correlación.")

        st.subheader("Tendencia en el periodo")
        p_nivel = pendiente_por_hora(datos, "nivel")
        p_temp = pendiente_por_hora(datos, "temperatura")
        t1, t2 = st.columns(2)
        if p_nivel is not None:
            t1.metric("Tendencia del nivel", f"{p_nivel:+.1f} % por hora",
                      "Llenándose" if p_nivel > 0 else "Vaciándose", delta_color="off")
        if p_temp is not None:
            t2.metric("Tendencia de la temperatura", f"{p_temp:+.2f} °C por hora",
                      "Calentándose" if p_temp > 0 else "Enfriándose", delta_color="off")
        st.caption("Pendiente de una regresión lineal sobre el periodo seleccionado.")

    # ---------- Datos ----------
    with st.expander("Ver todos los registros del periodo"):
        tabla = datos[["tiempo", "nivel", "temperatura", "estado_nivel", "estado_temp"]].copy()
        tabla.columns = ["Fecha y hora", "Nivel (%)", "Temperatura (°C)",
                         "Estado nivel", "Estado temperatura"]
        mostrar_tabla(tabla)

    st.markdown("---")
    st.caption("AquaMonitor · Proyecto final de Computación Física e IoT · Universidad EAFIT · "
               "Datos: ESP32 simulado en Cirkit Designer → MQTT → InfluxDB → Grafana (CSV).")


if __name__ == "__main__":
    main()
