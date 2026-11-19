# 💧 AquaMonitor: telemetría y telecontrol de un tanque de agua

Proyecto final de **Computación Física e Internet de las Cosas (IoT)**, Universidad EAFIT.
Autora: Isabella Saldarriaga Valencia.

Sistema IoT que monitorea a distancia el **nivel** y la **temperatura** del agua de un tanque
elevado y permite **encender o apagar la bomba de llenado** de forma remota.

## Enlaces

| Componente | Enlace |
|---|---|
| Circuito simulado (Cirkit Designer) | https://app.cirkitdesigner.com/project/27b7380c-8263-4970-88a0-f784f79c2203 |
| App de análisis (Streamlit Cloud) | _(pegar aquí el enlace público)_ |
| Dashboard de Grafana | Ver capturas del dashboard en este repositorio |

## Arquitectura

```
ESP32 + HC-SR04 + DS18B20 + Relé (Cirkit Designer)
   │  JSON cada 5 s                       ▲ "ON" / "OFF"
   ▼                                      │
Broker MQTT público (broker.hivemq.com:1883)
   │                                      ▲
   ▼                                      │
Node-RED (panel de telemetría y botones de control)

ESP32 ──(cada 10 s, HTTPS)──► InfluxDB Cloud (bucket tanque_agua)
                                   │
                                   ▼
                              Grafana Cloud ──► CSV ──► App Streamlit Cloud
```

## Variables, sensores y umbrales

| Variable | Sensor | Unidad | Normal | Alertas |
|---|---|---|---|---|
| Nivel del agua | HC-SR04 (ultrasónico) | % | 20–95 % | < 10 % crítico, 10–20 % bajo, > 95 % desbordamiento |
| Temperatura del agua | DS18B20 | °C | 10–25 °C | > 25 °C agua tibia, < 10 °C inusual |

Actuador: módulo relé (bomba de llenado) y LED indicador.

## MQTT

| Uso | Tópico | Formato |
|---|---|---|
| Telemetría (publica el ESP32) | `eafit/isabella/tanque/telemetria` | `{"nivel":70.0,"temperatura":21.5,"bomba":"OFF"}` |
| Control (escucha el ESP32) | `eafit/isabella/tanque/control` | `ON` / `OFF` |

## InfluxDB

- Bucket: `tanque_agua`
- Measurement: `tanque`
- Tag: `dispositivo=esp32_tanque`
- Fields: `nivel`, `temperatura`
- Timestamp asignado al recibir cada punto.

## Estructura del repositorio

| Archivo | Contenido |
|---|---|
| `app.py` | App de Streamlit |
| `requirements.txt` | Librerías de la app |
| `registros_tanque.csv` | CSV exportado desde Grafana (datos del proyecto) |
| `aquamonitor_esp32.ino` | Código del ESP32 (sin credenciales) |
| `flujo_nodered_aquamonitor.json` | Flujo de Node-RED |
| Capturas `.png` | Dashboard de Grafana |

## Seguridad

El token de InfluxDB **no está publicado**. En `aquamonitor_esp32.ino` aparece como
`PEGA_AQUI_TU_TOKEN`; para ejecutar el código hay que reemplazarlo por un token propio con
permisos de lectura y escritura sobre el bucket.

## Ejecutar la app localmente

```bash
pip install -r requirements.txt
streamlit run app.py
```
