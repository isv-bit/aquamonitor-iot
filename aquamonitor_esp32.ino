// ============================================================
//  AquaMonitor - Tanque de agua inteligente
//  ESP32 + HC-SR04 (nivel) + DS18B20 (temperatura) + Rele (bomba)
//  WiFi + MQTT (telemetria y telecontrol) + InfluxDB
// ============================================================

#include <WiFi.h>               // Conexion WiFi
#include <PubSubClient.h>       // MQTT
#include <ArduinoJson.h>        // Formato JSON
#include <InfluxDbClient.h>     // InfluxDB
#include <InfluxDbCloud.h>      // Certificado de seguridad de InfluxDB Cloud
#include <OneWire.h>            // Bus de un solo cable (DS18B20)
#include <DallasTemperature.h>  // Lectura del DS18B20

// ============================================================
//  1. WiFi (red simulada de Cirkit Designer)
// ============================================================
#define WIFI_SSID     "CirkitWifi"
#define WIFI_PASSWORD ""

// ============================================================
//  2. InfluxDB   DATOS PRIVADOS (no subir a GitHub con estos valores)
//  Mientras digan PEGA_AQUI, el programa se salta InfluxDB.
// ============================================================
#define INFLUXDB_URL    "https://us-east-1-1.aws.cloud2.influxdata.com"
#define INFLUXDB_TOKEN  "PEGA_AQUI_TU_TOKEN"   // Por seguridad, el token real NO se publica
#define INFLUXDB_ORG    "TU_ORG_ID"
#define INFLUXDB_BUCKET "tanque_agua"
#define TZ_INFO         "COT5"   // Zona horaria de Colombia (UTC-5)

// ============================================================
//  3. MQTT
// ============================================================
#define MQTT_SERVER      "broker.hivemq.com"
#define MQTT_PORT        1883
#define MQTT_CLIENT_ID   "aquamonitor_isabella_esp32"   // Nombre unico en el broker
#define TOPIC_TELEMETRIA "eafit/isabella/tanque/telemetria"
#define TOPIC_CONTROL    "eafit/isabella/tanque/control"

// ============================================================
//  4. PINES (iguales a las conexiones del circuito)
// ============================================================
#define TRIG_PIN  5    // HC-SR04: TRIG
#define ECHO_PIN  18   // HC-SR04: ECHO
#define TEMP_PIN  4    // DS18B20: DQ (datos)
#define RELE_PIN  26   // Rele de la bomba: IN
#define LED_PIN   2    // LED indicador de la bomba

// ============================================================
//  5. TANQUE Y RELE
// ============================================================
const float ALTURA_TANQUE_CM = 100.0;   // Distancia del sensor al fondo del tanque

#define RELE_ACTIVO    HIGH   // Rele configurado en "Active high"
#define RELE_INACTIVO  LOW

// ============================================================
//  6. INTERVALOS FIJOS
// ============================================================
const unsigned long INTERVALO_LECTURA = 5000;   // Leer sensores y publicar MQTT cada 5 s
const unsigned long INTERVALO_INFLUX  = 10000;  // Guardar en InfluxDB cada 10 s

unsigned long ultimaLectura     = 0;
unsigned long ultimoInflux      = 0;
unsigned long ultimoIntentoMQTT = 0;

// ============================================================
//  7. OBJETOS Y VARIABLES GLOBALES
// ============================================================
WiFiClient   espClient;
PubSubClient mqttClient(espClient);

InfluxDBClient influxClient(INFLUXDB_URL, INFLUXDB_ORG, INFLUXDB_BUCKET,
                            INFLUXDB_TOKEN, InfluxDbCloud2CACert);
Point puntoTanque("tanque");    // "measurement" (tabla) en InfluxDB

OneWire oneWire(TEMP_PIN);
DallasTemperature sensorTemp(&oneWire);

bool  bombaEncendida    = false;
float ultimaDistancia   = -1;
float ultimoNivel       = -1;
float ultimaTemperatura = DEVICE_DISCONNECTED_C;
bool  influxConfigurado = false;

// ============================================================
//  FUNCIONES DE SENSORES
// ============================================================

// Mide la distancia (cm) entre el sensor y el agua
float medirDistanciaCm() {
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);        // Pulso de 10 us: "dispara el sonido"
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);

  unsigned long duracion = pulseIn(ECHO_PIN, HIGH, 30000);  // Espera max. 30 ms
  if (duracion == 0) return -1;        // -1 = sin respuesta (error)
  return duracion * 0.0343 / 2.0;      // Velocidad del sonido, ida y vuelta
}

// Convierte la distancia en nivel de agua (%)
float calcularNivel(float distancia) {
  float nivel = (ALTURA_TANQUE_CM - distancia) / ALTURA_TANQUE_CM * 100.0;
  return constrain(nivel, 0, 100);
}

// Lee ambos sensores y guarda los valores en las variables globales
void leerSensores() {
  ultimaDistancia = medirDistanciaCm();
  ultimoNivel = (ultimaDistancia < 0) ? -1 : calcularNivel(ultimaDistancia);

  sensorTemp.requestTemperatures();
  ultimaTemperatura = sensorTemp.getTempCByIndex(0);

  Serial.print("Distancia: ");   Serial.print(ultimaDistancia, 1);   Serial.print(" cm | ");
  Serial.print("Nivel: ");       Serial.print(ultimoNivel, 1);       Serial.print(" % | ");
  Serial.print("Temperatura: "); Serial.print(ultimaTemperatura, 1); Serial.print(" C | ");
  Serial.print("Bomba: ");       Serial.println(bombaEncendida ? "ON" : "OFF");
}

// Comprueba que las dos lecturas sean validas
bool lecturasValidas() {
  return (ultimoNivel >= 0) && (ultimaTemperatura != DEVICE_DISCONNECTED_C);
}

// ============================================================
//  FUNCIONES MQTT
// ============================================================

// Publica nivel, temperatura y estado de la bomba en JSON
void publicarTelemetria() {
  if (!mqttClient.connected() || !lecturasValidas()) return;

  StaticJsonDocument<200> doc;
  doc["nivel"]       = round(ultimoNivel * 10) / 10.0;        // 1 decimal
  doc["temperatura"] = round(ultimaTemperatura * 10) / 10.0;  // 1 decimal
  doc["bomba"]       = bombaEncendida ? "ON" : "OFF";

  char buffer[128];
  serializeJson(doc, buffer);
  mqttClient.publish(TOPIC_TELEMETRIA, buffer);

  Serial.print("MQTT publicado -> ");
  Serial.println(buffer);
}

// Enciende o apaga la bomba (rele + LED) y avisa por MQTT
void controlarBomba(bool encender) {
  bombaEncendida = encender;
  digitalWrite(RELE_PIN, encender ? RELE_ACTIVO : RELE_INACTIVO);
  digitalWrite(LED_PIN,  encender ? HIGH : LOW);
  Serial.println(encender ? ">> Bomba ENCENDIDA" : ">> Bomba APAGADA");
  publicarTelemetria();   // Informa el nuevo estado de inmediato
}

// Interpreta un comando de texto ("ON" u "OFF")
void procesarComando(String comando) {
  comando.trim();
  comando.toUpperCase();
  if (comando == "ON")       controlarBomba(true);
  else if (comando == "OFF") controlarBomba(false);
  else Serial.println("Comando no reconocido: " + comando);
}

// Se ejecuta automaticamente cuando llega un mensaje al topico de control
void callbackMQTT(char* topic, byte* payload, unsigned int length) {
  String mensaje = "";
  for (unsigned int i = 0; i < length; i++) mensaje += (char)payload[i];

  Serial.print("Comando MQTT recibido en [");
  Serial.print(topic);
  Serial.print("]: ");
  Serial.println(mensaje);

  procesarComando(mensaje);
}

// Intenta conectarse al broker (un intento cada 5 s, sin bloquear)
void reconectarMQTT() {
  if (millis() - ultimoIntentoMQTT < 5000) return;
  ultimoIntentoMQTT = millis();

  Serial.print("Conectando a MQTT... ");
  if (mqttClient.connect(MQTT_CLIENT_ID)) {
    Serial.println("conectado");
    mqttClient.subscribe(TOPIC_CONTROL);
    Serial.println("Suscrito a: " TOPIC_CONTROL);
  } else {
    Serial.print("fallo, codigo = ");
    Serial.println(mqttClient.state());
  }
}

// ============================================================
//  FUNCION InfluxDB
// ============================================================
void guardarEnInflux() {
  puntoTanque.clearFields();
  puntoTanque.addField("nivel", ultimoNivel);
  puntoTanque.addField("temperatura", ultimaTemperatura);

  Serial.print("InfluxDB -> ");
  Serial.println(puntoTanque.toLineProtocol());

  if (influxClient.writePoint(puntoTanque)) {
    Serial.println("InfluxDB: guardado OK");
  } else {
    Serial.print("!! Error InfluxDB: ");
    Serial.println(influxClient.getLastErrorMessage());
  }
}

// ============================================================
//  SETUP: se ejecuta una vez al iniciar
// ============================================================
void setup() {
  Serial.begin(115200);
  Serial.println("\n=== AquaMonitor ===");

  // Pines
  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);
  pinMode(RELE_PIN, OUTPUT);
  pinMode(LED_PIN,  OUTPUT);
  digitalWrite(RELE_PIN, RELE_INACTIVO);   // Bomba apagada al arrancar
  digitalWrite(LED_PIN, LOW);

  sensorTemp.begin();

  // WiFi
  Serial.print("Conectando a WiFi");
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) {
    delay(250);
    Serial.print(".");
  }
  Serial.print("\nWiFi conectado. IP: ");
  Serial.println(WiFi.localIP());

  // InfluxDB (solo si ya pegaste tus datos)
  influxConfigurado = (String(INFLUXDB_TOKEN).indexOf("PEGA_AQUI") < 0);
  if (influxConfigurado) {
    timeSync(TZ_INFO, "pool.ntp.org", "time.nis.gov");   // Sincroniza la hora
    puntoTanque.addTag("dispositivo", "esp32_tanque");
    if (influxClient.validateConnection()) {
      Serial.print("InfluxDB conectado: ");
      Serial.println(influxClient.getServerUrl());
    } else {
      Serial.print("!! InfluxDB fallo: ");
      Serial.println(influxClient.getLastErrorMessage());
    }
  } else {
    Serial.println("InfluxDB aun sin configurar: se omite por ahora.");
  }

  // MQTT
  mqttClient.setServer(MQTT_SERVER, MQTT_PORT);
  mqttClient.setCallback(callbackMQTT);

  Serial.println("Listo. Tambien puedes escribir ON u OFF en el monitor serie.");
}

// ============================================================
//  LOOP: se repite sin parar
// ============================================================
void loop() {
  // 1. Mantener la conexion MQTT y atender mensajes entrantes
  if (!mqttClient.connected()) reconectarMQTT();
  mqttClient.loop();

  // 2. Comandos desde el monitor serie (opcional, para pruebas)
  if (Serial.available()) {
    procesarComando(Serial.readStringUntil('\n'));
  }

  unsigned long ahora = millis();

  // 3. Leer sensores y publicar por MQTT cada 5 s
  if (ahora - ultimaLectura >= INTERVALO_LECTURA) {
    ultimaLectura = ahora;
    leerSensores();
    if (lecturasValidas()) publicarTelemetria();
    else Serial.println("!! Lectura invalida: revisa los sensores");
  }

  // 4. Guardar en InfluxDB cada 10 s
  if (influxConfigurado && (ahora - ultimoInflux >= INTERVALO_INFLUX)) {
    ultimoInflux = ahora;
    if (lecturasValidas()) guardarEnInflux();
  }
}
