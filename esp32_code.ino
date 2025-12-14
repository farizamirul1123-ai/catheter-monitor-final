#include <WiFi.h>
#include <HTTPClient.h> 
#include <ArduinoJson.h> // *** WAJIB: Pastikan anda sudah install library ini ***

// ===== WiFi Credentials (GANTI INI) =====
#define WIFI_SSID "@Faizall Ghazali"
#define WIFI_PASSWORD "nisa100316"

// ===== Flask API Server URL (GANTI IP INI DENGAN IP ANDA) =====
// Pastikan ini sepadan dengan IP yang dipaparkan oleh Flask Server
const char* FLASK_API_URL = "http://192.168.100.18:5000/api/v1/log_data"; 

// ===== Pins =====
#define RXD2 16 // Terima data dari Arduino
#define TXD2 17 // Hantar ke Arduino
#define LED 2

// ===== Data Variables =====
float weight_kg = 0;
String status_message = "";
String alertLevel = "NONE";
String buzzerStatus = "OFF"; // Status buzzer awal
bool pyuria_detected = false; 
float pyuria_confidence = 0.0;

// ===== Timing =====
unsigned long lastUploadTime = 0;
const long uploadInterval = 3000; // Hantar data setiap 3 saat

// ===== Flags (Untuk Logik Amaran) =====
bool level500_sent = false;
bool level1000_sent = false;
bool level1500_sent = false;
bool isDataReady = false; 

// ===== Get Status Message (Dikekalkan) =====
String getStatusMessage(float weight) {
    if (weight <= 0.5) {
        return "Catherer masih berada dalam paras 500 ml";
    } 
    else if (weight > 0.5 && weight <= 1.0) {
        return "Catherer bersedia untuk ditukar dalam 1 jam";
    }
    else {
        return "Catherer perlu ditukar segera supaya elak dari infection";
    }
}

// ===== Upload to Flask API (Menggunakan ArduinoJson) =====
void uploadToFlaskAPI() {
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("⚠️ WiFi Disconnected, skipping upload.");
        return;
    }

    if (!isDataReady) {
        Serial.println("⚠️ Data is not ready, skipping upload.");
        return;
    }
    
    // 1. Bina JSON Payload menggunakan ArduinoJson
    DynamicJsonDocument doc(256);
    doc["weight_kg"] = weight_kg;
    doc["status_message"] = status_message;
    doc["alert_level"] = alertLevel;
    doc["buzzer_status"] = buzzerStatus;
    // Nilai AI Dummy
    doc["pyuria_detected"] = pyuria_detected;
    doc["pyuria_confidence"] = pyuria_confidence; 

    String jsonPayload;
    serializeJson(doc, jsonPayload);

    // 2. Hantar Permintaan HTTP
    HTTPClient http;
    http.begin(FLASK_API_URL);
    http.addHeader("Content-Type", "application/json");

    Serial.print("\n🔥 POST data to API: ");
    Serial.println(jsonPayload);

    int httpResponseCode = http.POST(jsonPayload);

    // 3. Semak Jawapan
    if (httpResponseCode > 0) {
        Serial.printf("✅ HTTP Code: %d\n", httpResponseCode);
        // Jika anda mahu membaca response body dari Flask API, boleh gunakan:
        // String response = http.getString();
        // Serial.print("API Response: ");
        // Serial.println(response);
    } else {
        Serial.printf("❌ HTTP Failed. Error Code: %d\n", httpResponseCode);
        Serial.println(http.errorToString(httpResponseCode));
    }
    http.end();
}


void setup() {
    Serial.begin(115200);
    Serial2.begin(9600, SERIAL_8N1, RXD2, TXD2);
    
    pinMode(LED, OUTPUT);
    digitalWrite(LED, LOW);
    
    Serial.println("\n════════════════════════════════════════");
    Serial.println("     ESP32 WEIGHT UNIT (Flask API)");
    Serial.println("════════════════════════════════════════\n");
    
    // ===== WiFi Connect (Dikekalkan) =====
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    Serial.print("WiFi");
    
    while (WiFi.status() != WL_CONNECTED) {
        Serial.print(".");
        delay(400);
    }
    
    Serial.println("\n✓ WiFi OK");
    Serial.print("IP: ");
    Serial.println(WiFi.localIP());
    
    Serial.println("\n📡 Waiting Arduino data...\n");
}

void loop() {
    // ===== Receive from Arduino =====
    if (Serial2.available()) {
        String dataString = Serial2.readStringUntil('\n');
        dataString.trim();
        
        if (dataString.startsWith("DATA,")) {
            digitalWrite(LED, LOW);
            delay(40);
            digitalWrite(LED, HIGH);
            
            // Logik Parsing Data (Dibezakan dengan koma)
            dataString.remove(0, 5); // Buang "DATA,"

            int idx1 = dataString.indexOf(',');
            
            if (idx1 > 0) {
                // Format: weight,buzzer
                weight_kg = dataString.substring(0, idx1).toFloat();
                buzzerStatus = dataString.substring(idx1 + 1);
            } else {
                // Format: weight (Buzzer status missing, assume OFF)
                weight_kg = dataString.toFloat();
                buzzerStatus = "OFF"; 
            }
            
            // Logik Status & Alert (Dikekalkan)
            status_message = getStatusMessage(weight_kg);
            alertLevel = "NONE";
            
            if (weight_kg >= 0.5 && weight_kg < 1.0) {
                alertLevel = "LEVEL_500_REACHED";
            } else if (weight_kg >= 1.0 && weight_kg < 1.5) {
                alertLevel = "LEVEL_1000_REACHED";
            } else if (weight_kg >= 1.5) {
                alertLevel = "LEVEL_1500_DANGER";
            }
            
            // AI DUMMY LOGIC (Dipengaruhi oleh berat untuk ujian)
            pyuria_detected = (weight_kg >= 1.0);
            pyuria_confidence = (weight_kg >= 1.0) ? 0.85 : 0.0;

            Serial.println("\n────────────────────────────");
            Serial.printf("📥 Weight: %.3f kg\n", weight_kg);
            Serial.printf("🚨 Alert: %s\n", alertLevel.c_str());
            Serial.printf("🔬 Pyuria: %s (%.2f)\n", pyuria_detected ? "TRUE" : "FALSE", pyuria_confidence);
            Serial.println("────────────────────────────");
            
            isDataReady = true;

        } else if (dataString.startsWith("READY")) {
            Serial.println("✓ Arduino ready");
        } 
    }
    
    // ===== Upload =====
    if (isDataReady && (millis() - lastUploadTime >= uploadInterval)) {
        lastUploadTime = millis();
        uploadToFlaskAPI();
    }
    
    delay(50);
}