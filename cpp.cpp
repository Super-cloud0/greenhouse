#include <WiFi.h>
#include <PubSubClient.h> 
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <DHT.h>

// ======================= 🔑 НАСТРОЙКИ (ЗАМЕНИ НА СВОИ!) =======================
#define WIFI_SSID "ТВОЙ_WIFI"
#define WIFI_PASSWORD "ТВОЙ_ПАРОЛЬ"

// Настройки Adafruit IO
#define MQTT_SERVER "io.adafruit.com"
#define MQTT_PORT 1883
#define MQTT_USERNAME "ТВОЙ_AIO_USERNAME" 
#define MQTT_PASSWORD "ТВОЙ_AIO_KEY"      
#define CLIENT_ID "ESP32_SmartGarden_FINAL"
#define DATA_TOPIC "ТВОЙ_AIO_USERNAME/feeds/smartgarden_data"     
#define CONTROL_TOPIC "ТВОЙ_AIO_USERNAME/feeds/smartgarden_control" 

// ======================= ПИНЫ И КОНСТАНТЫ =======================
#define DHTPIN 4
#define DHTTYPE DHT11
#define SOIL_PIN 34
#define RELAY_PIN 25
#define WATER_PIN 33 
#define OLED_ADDR 0x3C


const int DRY_VAL = 4095; 
const int WET_VAL = 1200; 
const int WATER_SENSOR_THRESHOLD = 1000; 

const bool INVERT_WATER_LOGIC = false; 

// ======================= ОБЪЕКТЫ =======================
WiFiClient espClient;
PubSubClient client(espClient);
DHT dht(DHTPIN, DHTTYPE);
Adafruit_SSD1306 display(128, 64, &Wire, -1);

// ======================= ПЕРЕМЕННЫЕ И ТАЙМЕРЫ =======================
int autoWaterThreshold = 30; 
unsigned long lastLoopTime = 0;
const int LOOP_DELAY = 2000; 
const int PUMP_SAFETY_DELAY = 500; 

float temp = 0;
float hum = 0;
int soilPct = 0;
int waterLevelRaw = 0;
bool waterOk = false;
bool pumpOn = false;

// ======================= ФУНКЦИИ MQTT =======================

void setup_wifi() {
 
  delay(10);
  Serial.print("Connecting to WiFi");
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\nWiFi Connected");
}

void callback(char* topic, byte* payload, unsigned int length) {
  String message;
  for (int i = 0; i < length; i++) {
    message += (char)payload[i];
  }
  
  if (String(topic) == CONTROL_TOPIC) {
    int newThreshold = message.toInt();
    if (newThreshold > 0 && newThreshold < 100) {
      autoWaterThreshold = newThreshold;
      Serial.print("NEW Threshold: "); Serial.println(autoWaterThreshold);
    }
  }
}

void reconnect() {
  if (!client.connected()) {
    Serial.print("Connecting to MQTT...");
    if (client.connect(CLIENT_ID, MQTT_USERNAME, MQTT_PASSWORD)) {
      Serial.println("connected");
      client.subscribe(CONTROL_TOPIC);
    } else {
      Serial.print("failed, rc=");
      Serial.print(client.state());
    }
  }
}

// ======================= SETUP =======================
void setup() {
  Serial.begin(115200);
  dht.begin();
  pinMode(RELAY_PIN, OUTPUT);
  pinMode(WATER_PIN, INPUT);
  digitalWrite(RELAY_PIN, LOW); 
  

  if(!display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR)) {}
  display.clearDisplay();
  display.setTextColor(WHITE);
  display.setTextSize(1);
  display.setCursor(0,0); display.println("System Boot..."); display.display();
  setup_wifi();
  
  client.setServer(MQTT_SERVER, MQTT_PORT);
  client.setCallback(callback);
}

// ======================= LOOP =======================
void loop() {
  if (!client.connected()) {
    reconnect();
  }
  client.loop();

  if (millis() - lastLoopTime >= LOOP_DELAY) {
    lastLoopTime = millis();
    temp = dht.readTemperature();
    hum = dht.readHumidity();
    
    int rawSoil = analogRead(SOIL_PIN);
    soilPct = map(rawSoil, DRY_VAL, WET_VAL, 0, 100);
    soilPct = constrain(soilPct, 0, 100);

    waterLevelRaw = analogRead(WATER_PIN);

    bool waterCondition = (waterLevelRaw > WATER_SENSOR_THRESHOLD);
    if (INVERT_WATER_LOGIC) {
      waterOk = !waterCondition;
    } else {
      waterOk = waterCondition;
    }

    if (!waterOk) {
       digitalWrite(RELAY_PIN, LOW);
       pumpOn = false;
       Serial.println("PUMP STOP: NO WATER!");
    } 
    else if (soilPct < autoWaterThreshold) { 
       digitalWrite(RELAY_PIN, HIGH);
       pumpOn = true;
       Serial.println("PUMP ON: Watering...");

       delay(PUMP_SAFETY_DELAY); 
    }

    else {
        digitalWrite(RELAY_PIN, LOW);
        pumpOn = false;
    }


    display.clearDisplay();
    display.setCursor(0,0); display.print("Soil: "); display.print(soilPct); display.print("% / Set:"); display.println(autoWaterThreshold);
    display.setCursor(0,15); display.print("Temp: "); display.print(temp, 1); display.println(" C");
    display.setCursor(0,30); display.print("Water Raw: "); display.print(waterLevelRaw);
    display.setCursor(0,45); display.print("Pump: "); display.println(pumpOn ? "ON" : "OFF");
    display.display();


    String payload = String(soilPct) + "," + String(temp, 1) + "," + String(hum, 0) + "," + String(waterOk ? 1 : 0);
    
    if (client.connected()) {
      client.publish(DATA_TOPIC, payload.c_str());
    }
  }
}
