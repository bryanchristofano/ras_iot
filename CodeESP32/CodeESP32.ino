#include <WiFi.h>
#include <WiFiManager.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <vector>

// --- DEKLARASI PIN ---
const int pinRelay1 = 26;
const int pinRelay2 = 27;
const int pinRelay3 = 25;
const int pinRelay4 = 14;

const int pinSuhu = 4;
const int pinPH = 32;
const int pinTDS = 33;
const int pinDO = 34;
const int pinFloat = 5;
const int pinTombolReset = 0;

// --- KONFIGURASI SENSOR DO ---
#define VREF_ESP32 3300.0    // Tegangan referensi ESP32 (mV)
#define ADC_RES_ESP32 4095.0 // Resolusi ADC ESP32 (12-bit)
#define DO_CAL1_V 1600.0     // Voltase kalibrasi saturasi (mV) 
#define DO_CAL1_T 25.0       // Suhu saat kalibrasi (°C)

// Tabel tingkat kelarutan Oksigen (ug/L atau ppb) pada suhu 0 - 40 derajat Celcius
const uint16_t DO_Table[41] = {
    14460, 14220, 13820, 13440, 13090, 12740, 12420, 12110, 11810, 11530,
    11260, 11010, 10770, 10530, 10300, 10080, 9860, 9660, 9460, 9270,
    9080, 8900, 8730, 8570, 8410, 8250, 8110, 7960, 7820, 7690,
    7560, 7430, 7300, 7180, 7070, 6950, 6840, 6730, 6630, 6530, 6410
};

// --- OBJEK GLOBAL ---
WiFiClient espClient;
PubSubClient mqttClient(espClient);
Preferences pref;
OneWire oneWire(pinSuhu);
DallasTemperature sensorSuhu(&oneWire);

// --- VARIABEL SISTEM & JARINGAN ---
unsigned long lastMsg = 0;
const char* mqtt_server = "broker.hivemq.com";
float kalibrasi_pH_netral = 2.5;        
String deviceID; 

// --- VARIABEL STATUS SLOT (DYNAMIC PROVISIONING) ---
bool en_r1, en_r2, en_r3, en_r4;
bool en_suhu, en_ph, en_tds, en_do, en_float;

// --- VARIABEL NAMA KUSTOM RELAY ---
String nama_r1, nama_r2, nama_r3, nama_r4;

// ==========================================
// FUNGSI PENERIMA PERINTAH DARI DASHBOARD
// ==========================================
void mqttCallback(char* topic, byte* payload, unsigned int length) {
  String messageTemp;
  for (int i = 0; i < length; i++) {
    messageTemp += (char)payload[i];
  }
  
  String topicStr = String(topic);
  
  if (topicStr == "ras/" + deviceID + "/kontrol/input1" && en_r1) {
    if (messageTemp == "ON") digitalWrite(pinRelay1, LOW);
    else if (messageTemp == "OFF") digitalWrite(pinRelay1, HIGH);
    Serial.println("Relay 1 (" + nama_r1 + ") dieksekusi");
  } 
  else if (topicStr == "ras/" + deviceID + "/kontrol/input2" && en_r2) {
    if (messageTemp == "ON") digitalWrite(pinRelay2, LOW);
    else if (messageTemp == "OFF") digitalWrite(pinRelay2, HIGH);
    Serial.println("Relay 2 (" + nama_r2 + ") dieksekusi");
  } 
  else if (topicStr == "ras/" + deviceID + "/kontrol/input3" && en_r3) {
    if (messageTemp == "ON") digitalWrite(pinRelay3, LOW);
    else if (messageTemp == "OFF") digitalWrite(pinRelay3, HIGH);
    Serial.println("Relay 3 (" + nama_r3 + ") dieksekusi");
  } 
  else if (topicStr == "ras/" + deviceID + "/kontrol/input4" && en_r4) {
    if (messageTemp == "ON") digitalWrite(pinRelay4, LOW);
    else if (messageTemp == "OFF") digitalWrite(pinRelay4, HIGH);
    Serial.println("Relay 4 (" + nama_r4 + ") dieksekusi");
  }
}

// ==========================================
// FUNGSI KONEKSI ULANG MQTT
// ==========================================
void reconnectMQTT() {
  while (!mqttClient.connected()) {
    Serial.print("Menghubungkan ke MQTT Broker...");
    String clientId = "RAS-" + deviceID;
    
    if (mqttClient.connect(clientId.c_str())) {
      Serial.println("Terhubung!");
      if (en_r1) mqttClient.subscribe(("ras/" + deviceID + "/kontrol/input1").c_str());
      if (en_r2) mqttClient.subscribe(("ras/" + deviceID + "/kontrol/input2").c_str());
      if (en_r3) mqttClient.subscribe(("ras/" + deviceID + "/kontrol/input3").c_str());
      if (en_r4) mqttClient.subscribe(("ras/" + deviceID + "/kontrol/input4").c_str());
    } else {
      Serial.print("Gagal, rc="); Serial.print(mqttClient.state());
      Serial.println(" Coba lagi dalam 5 detik...");
      delay(5000);
    }
  }
}

// ==========================================
// SETUP: DIJALANKAN 1 KALI SAAT NYALA
// ==========================================
void setup() {
  Serial.begin(115200);

  // --- MENGAMBIL IDENTITAS CHIP (MAC ADDRESS) ---
  uint64_t chipid = ESP.getEfuseMac();
  deviceID = String((uint32_t)(chipid >> 32), HEX);
  deviceID += String((uint32_t)chipid, HEX);
  deviceID.toUpperCase();
  Serial.println("\n==================================");
  Serial.println("ID PERANGKAT (CHIP ID): " + deviceID);
  Serial.println("==================================\n");

  // 1. Inisialisasi Pin
  pinMode(pinRelay1, OUTPUT); digitalWrite(pinRelay1, HIGH);
  pinMode(pinRelay2, OUTPUT); digitalWrite(pinRelay2, HIGH);
  pinMode(pinRelay3, OUTPUT); digitalWrite(pinRelay3, HIGH);
  pinMode(pinRelay4, OUTPUT); digitalWrite(pinRelay4, HIGH);
  pinMode(pinFloat, INPUT_PULLUP);
  pinMode(pinTombolReset, INPUT_PULLUP); 
  
  sensorSuhu.begin();

  // 2. Buka Memori & Ambil Pengaturan Terakhir
  pref.begin("iot_ras", false);
  
  en_r1 = pref.getBool("en_r1", true);
  en_r2 = pref.getBool("en_r2", true);
  en_r3 = pref.getBool("en_r3", true);
  en_r4 = pref.getBool("en_r4", true);
  en_suhu = pref.getBool("en_suhu", true);
  en_ph = pref.getBool("en_ph", true);
  en_tds = pref.getBool("en_tds", true);
  en_do = pref.getBool("en_do", true);
  en_float = pref.getBool("en_float", true);

  // Ambil data nama kustom, jika kosong berikan nilai default
  nama_r1 = pref.getString("nama_r1", "Relay 1");
  nama_r2 = pref.getString("nama_r2", "Relay 2");
  nama_r3 = pref.getString("nama_r3", "Relay 3");
  nama_r4 = pref.getString("nama_r4", "Relay 4");

  // 3. Persiapan Form WiFiManager
  WiFiManager wm;
  
  // Membatasi menu bawaan WiFiManager agar HANYA menampilkan menu konfigurasi Wi-Fi
  std::vector<const char *> menu = {"wifi"};
  wm.setMenu(menu);

  // Menampilkan Device ID di Captive Portal
  String teksInfo = "<div style='background:#fff3cd; color:#856404; padding:10px; border-radius:8px; text-align:center; border: 1px solid #ffeeba; margin-bottom: 15px; margin-top: 15px;'><small>Silakan salin <b>Device ID</b> ini untuk didaftarkan pada Web Dashboard:</small><br><strong style='font-size:22px; font-family:monospace; display:block; margin-top:5px; color:#b58105; letter-spacing: 2px;'>" + deviceID + "</strong></div>";
  WiFiManagerParameter custom_info_id(teksInfo.c_str());
  wm.addParameter(&custom_info_id);

  // BAGIAN 1: PENGATURAN AKTIF/NONAKTIF ELEMEN
  WiFiManagerParameter custom_title_sensor("<div style='font-weight:bold; color:#007bb5; font-size:16px; margin-top:20px; margin-bottom:10px; border-bottom:1px solid #ccc; padding-bottom:5px;'>Choose Component Capabilities</div>");
  wm.addParameter(&custom_title_sensor);

  const char* cb_style = "type=\"checkbox\"";
  WiFiManagerParameter cb_r1("r1", "Enable Relay 1", en_r1 ? "1" : "0", 2, cb_style, WFM_LABEL_AFTER);
  WiFiManagerParameter cb_r2("r2", "Enable Relay 2", en_r2 ? "1" : "0", 2, cb_style, WFM_LABEL_AFTER);
  WiFiManagerParameter cb_r3("r3", "Enable Relay 3", en_r3 ? "1" : "0", 2, cb_style, WFM_LABEL_AFTER);
  WiFiManagerParameter cb_r4("r4", "Enable Relay 4", en_r4 ? "1" : "0", 2, cb_style, WFM_LABEL_AFTER);
  WiFiManagerParameter cb_suhu("suhu", "Temperature Sensor", en_suhu ? "1" : "0", 2, cb_style, WFM_LABEL_AFTER);
  WiFiManagerParameter cb_ph("ph", "pH Sensor", en_ph ? "1" : "0", 2, cb_style, WFM_LABEL_AFTER);
  WiFiManagerParameter cb_tds("tds", "TDS Sensor", en_tds ? "1" : "0", 2, cb_style, WFM_LABEL_AFTER);
  WiFiManagerParameter cb_do("do", "Disolved Oxygen Sensor", en_do ? "1" : "0", 2, cb_style, WFM_LABEL_AFTER);
  WiFiManagerParameter cb_float("flt", "Float Switch Sensor", en_float ? "1" : "0", 2, cb_style, WFM_LABEL_AFTER);

  wm.addParameter(&cb_r1);
  wm.addParameter(&cb_r2);
  wm.addParameter(&cb_r3);
  wm.addParameter(&cb_r4);
  wm.addParameter(&cb_suhu);
  wm.addParameter(&cb_ph);
  wm.addParameter(&cb_tds);
  wm.addParameter(&cb_do);
  wm.addParameter(&cb_float);

  // BAGIAN 2: PENAMAAN KUSTOM RELAY
  WiFiManagerParameter custom_title_nama_relay("<div style='font-weight:bold; color:#007bb5; font-size:16px; margin-top:25px; margin-bottom:10px; border-bottom:1px solid #ccc; padding-bottom:5px;'>Custom Relay Naming</div>");
  wm.addParameter(&custom_title_nama_relay);

  WiFiManagerParameter txt_nama_r1("n_r1", "Relay 1 Name (e.g. Aerator)", nama_r1.c_str(), 20);
  WiFiManagerParameter txt_nama_r2("n_r2", "Relay 2 Name (e.g. Pump)", nama_r2.c_str(), 20);
  WiFiManagerParameter txt_nama_r3("n_r3", "Relay 3 Name (e.g. Feeder)", nama_r3.c_str(), 20);
  WiFiManagerParameter txt_nama_r4("n_r4", "Relay 4 Name (e.g. Heater)", nama_r4.c_str(), 20);

  wm.addParameter(&txt_nama_r1);
  wm.addParameter(&txt_nama_r2);
  wm.addParameter(&txt_nama_r3);
  wm.addParameter(&txt_nama_r4);

  // 4. Desain Visual Captive Portal
  String desainCustom = 
    "<style>"
    "body { background-color: #e8f4f8; font-family: 'Segoe UI', Tahoma, sans-serif; }"
    ".wrap { max-width: 400px; margin: auto; padding: 20px; background: #ffffff; border-radius: 15px; box-shadow: 0px 8px 15px rgba(0,0,0,0.1); }"
    "button { background-color: #007bb5 !important; border-radius: 8px !important; font-weight: bold; text-transform: uppercase; margin-top: 15px; transition: 0.3s; }"
    "input[type='checkbox'] { transform: scale(1.4); margin-right: 10px; margin-top: 12px; }"
    "input[type='text'] { width: 100%; padding: 8px 12px; margin-top: 4px; margin-bottom: 12px; border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box; font-size:14px; }"
    "label { font-weight: 500; color: #444; }"
    "</style>"
    "<script>"
    "window.addEventListener('DOMContentLoaded', function() {"
    "  var wrap = document.querySelector('.wrap');"
    "  if(!wrap) return;"
    "  "
    "  var btns = document.querySelectorAll('button');"
    "  for(var i=0; i<btns.length; i++) {"
    "    if(btns[i].innerText.toUpperCase().includes('CONFIGURE WIFI')) {"
    "      btns[i].innerText = 'CONFIGURE';"
    "    }"
    "  }"
    "  "
    "  if(document.getElementById('r1')) {"
    "    var htmlBaru = '<h1 style=\"color:#007bb5; font-size:24px; margin-bottom:5px; text-align:center;\">Set Up Panel RAS IoT</h1>' + "
    "                   '<div style=\"font-weight:bold; color:#007bb5; font-size:16px; margin-top:15px; margin-bottom:10px; border-bottom:1px solid #ccc; padding-bottom:5px;\">Available Networks</div>';"
    "    var hdr = wrap.querySelector('h1');"
    "    if(hdr) hdr.outerHTML = htmlBaru;"
    "    else wrap.insertAdjacentHTML('afterbegin', htmlBaru);"
    "  } else {"
    "    var mainHdr = wrap.querySelector('h1');"
    "    if(mainHdr && mainHdr.innerText.includes('WiFiManager')) {"
    "      mainHdr.innerText = 'RAS IoT Panel';"
    "    }"
    "  }"
    "});"
    "</script>";

  wm.setCustomHeadElement(desainCustom.c_str());

  Serial.println("Menghubungkan ke WiFi...");
  if (!wm.autoConnect("RAS_IoT_AP")) {
    Serial.println("Gagal koneksi WiFi dan timeout. Restarting...");
    ESP.restart();
  }

  // 5. Menyimpan Data Konfigurasi Baru ke Preferences
  pref.putBool("en_r1", (strncmp(cb_r1.getValue(), "1", 1) == 0));
  pref.putBool("en_r2", (strncmp(cb_r2.getValue(), "1", 1) == 0));
  pref.putBool("en_r3", (strncmp(cb_r3.getValue(), "1", 1) == 0));
  pref.putBool("en_r4", (strncmp(cb_r4.getValue(), "1", 1) == 0));
  pref.putBool("en_suhu", (strncmp(cb_suhu.getValue(), "1", 1) == 0));
  pref.putBool("en_ph", (strncmp(cb_ph.getValue(), "1", 1) == 0));
  pref.putBool("en_tds", (strncmp(cb_tds.getValue(), "1", 1) == 0));
  pref.putBool("en_do", (strncmp(cb_do.getValue(), "1", 1) == 0));
  pref.putBool("en_float", (strncmp(cb_float.getValue(), "1", 1) == 0));

  pref.putString("nama_r1", txt_nama_r1.getValue());
  pref.putString("nama_r2", txt_nama_r2.getValue());
  pref.putString("nama_r3", txt_nama_r3.getValue());
  pref.putString("nama_r4", txt_nama_r4.getValue());

  // Sinkronisasi ulang variabel lokal
  en_r1 = pref.getBool("en_r1");
  en_r2 = pref.getBool("en_r2");
  en_r3 = pref.getBool("en_r3");
  en_r4 = pref.getBool("en_r4");
  en_suhu = pref.getBool("en_suhu");
  en_ph = pref.getBool("en_ph");
  en_tds = pref.getBool("en_tds");
  en_do = pref.getBool("en_do");
  en_float = pref.getBool("en_float");

  nama_r1 = pref.getString("nama_r1");
  nama_r2 = pref.getString("nama_r2");
  nama_r3 = pref.getString("nama_r3");
  nama_r4 = pref.getString("nama_r4");

  // 6. Mulai MQTT
  mqttClient.setServer(mqtt_server, 1883);
  mqttClient.setCallback(mqttCallback);
  Serial.println("Sistem Siap!");
}

// ==========================================
// LOOP
// ==========================================
void loop() {
  if (digitalRead(pinTombolReset) == LOW) {
    Serial.println("\nPERINGATAN: Tombol Reset ditekan!");
    Serial.println("Tahan 3 detik untuk konfirmasi Reset WiFi & MQTT...");
    delay(3000); 
    
    if (digitalRead(pinTombolReset) == LOW) {
      Serial.println("Mereset memori WiFi dan MQTT...");
      WiFiManager wm;
      wm.resetSettings(); 
      pref.clear();       
      Serial.println("Memori bersih! Merestart sistem...");
      delay(1000);
      ESP.restart(); 
    } else {
      Serial.println("Reset dibatalkan (tombol dilepas terlalu cepat).\n");
    }
  }

  if (!mqttClient.connected()) {
    reconnectMQTT();
  }
  mqttClient.loop();

  unsigned long now = millis();
  if (now - lastMsg > 5000) {
    lastMsg = now;
    
    StaticJsonDocument<512> doc;
    float suhuKompensasi = 25.0; 

    if (en_suhu) {
      sensorSuhu.requestTemperatures();
      float suhu = sensorSuhu.getTempCByIndex(0);
      if (suhu != -127.00) suhuKompensasi = suhu; 
      doc["suhu"] = (suhu == -127.00) ? 0 : suhu; 
    }

   
    if (en_ph) {
      long totalRawPH = 0; 
      
      for(int i = 0; i < 20; i++) {
        totalRawPH += analogRead(pinPH);
        delay(5); 
      }
      
      int rataRataRawPH = totalRawPH / 20;
      float voltPH = rataRataRawPH * (3.3 / 4095.0);
      float pH_aktual = 7.0 + ((kalibrasi_pH_netral - voltPH) / 0.18); 
      
      doc["ph"] = pH_aktual; 
    }

    if (en_tds) {
      int rawTDS = analogRead(pinTDS);
      float voltTDS = rawTDS * (3.3 / 4095.0);
      float koefisienKompensasi = 1.0 + 0.02 * (suhuKompensasi - 25.0);
      float voltKompensasi = voltTDS / koefisienKompensasi;
      
      float tds_ppm = (133.42 * pow(voltKompensasi, 3) - 255.86 * pow(voltKompensasi, 2) + 857.39 * voltKompensasi) * 0.5;
      doc["tds_ppm"] = tds_ppm;
    }
    
    if (en_do) {
      int rawDO = analogRead(pinDO);
      float voltDO = rawDO * (VREF_ESP32 / ADC_RES_ESP32);
      int temp_index = (int)suhuKompensasi;
      if (temp_index < 0) temp_index = 0;
      if (temp_index > 40) temp_index = 40;
      float V_saturation = DO_CAL1_V + 35.0 * (float)DO_CAL1_T - 35.0 * (float)temp_index;
      float do_ppb = (voltDO * DO_Table[temp_index]) / V_saturation;
      float do_mgL = do_ppb / 1000.0;
      doc["do_mgL"] = do_mgL; 
    }

    if (en_float) {
      int statusPelampung = digitalRead(pinFloat); 
      doc["air_penuh"] = (statusPelampung == LOW) ? true : false;
    }

    doc["r1_en"] = en_r1;
    doc["r2_en"] = en_r2;
    doc["r3_en"] = en_r3;
    doc["r4_en"] = en_r4;

    // Mengirimkan Nama Kustom Relay ke JSON MQTT
    doc["r1_nama"] = nama_r1;
    doc["r2_nama"] = nama_r2;
    doc["r3_nama"] = nama_r3;
    doc["r4_nama"] = nama_r4;

    char jsonBuffer[512];
    serializeJson(doc, jsonBuffer);

    String topicData = "ras/" + deviceID + "/sensor/data";
    mqttClient.publish(topicData.c_str(), jsonBuffer);
    
    Serial.print("Data Terkirim [" + topicData + "]: ");
    Serial.println(jsonBuffer);
  }
}