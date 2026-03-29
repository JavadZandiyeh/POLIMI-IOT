#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>

#define PIR_PIN          4        // Motion sensor
#define LDR_PIN          34       // Light sensor (ADC)
#define ADC_RESOLUTION   9        // 9-bit → at most 3 decimal digits for luminosity
#define uS_TO_S_FACTOR   1000000ULL
#define PERSON_CODE      "11044962"
#define LUX_THRESHOLD    10        // Min luminosity change to trigger send

// ---------------------------------------------------------------------------
// RTC-persistent state (survives deep sleep)
// ---------------------------------------------------------------------------
RTC_DATA_ATTR bool  lastMotion = false;
RTC_DATA_ATTR int   lastLux    = -1;

// ---------------------------------------------------------------------------
// Variables
// ---------------------------------------------------------------------------

// Sensors
unsigned long tSensorAvailabilityStart = 0;
unsigned long tSensorAvailabilityEnd = 0;
unsigned long tSensorAvailabilityTotal = 0;
unsigned long tSensorAvailabilityIdle = 0;
unsigned long tSensorReadStart = 0;
unsigned long tSensorReadEnd = 0;
unsigned long tSensorReadDuration = 0;

// Sender
unsigned long tSenderAvailabilityStart = 0;
unsigned long tSenderAvailabilityEnd = 0;
unsigned long tSenderAvailabilityTotal = 0;
unsigned long tSenderAvailabilityIdle = 0;
unsigned long tSenderSpikeStart = 0;
unsigned long tSenderSpikeEnd = 0;
unsigned long tSenderSpikeDuration = 0;

// Deep Sleep
unsigned long tBootStart = 0;
unsigned long tBootEnd = 0;
unsigned long tBootDuration = 0;
unsigned long tWiFiAvailabilityStart = 0;
unsigned long tWiFiAvailabilityEnd = 0;
unsigned long tWiFiAvailabilityTotal = 0;
unsigned long tWiFiOnStart = 0;
unsigned long tWiFiOnEnd = 0;
unsigned long tWiFiOnDuration = 0;
unsigned long tWiFiOffDuration = 0;


// ---------------------------------------------------------------------------
// Sensor Log
// ---------------------------------------------------------------------------

static void runLog(const String &msg) {
  Serial.println(String("[run log] ") + msg);
}

// ---------------------------------------------------------------------------
// Pins
// ---------------------------------------------------------------------------

void initPins() {
  pinMode(PIR_PIN, INPUT);
  pinMode(LDR_PIN, INPUT);
  analogReadResolution(ADC_RESOLUTION);
}

// ---------------------------------------------------------------------------
// ESP-NOW
// ---------------------------------------------------------------------------

static uint8_t broadcastAddress[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
static volatile bool sendDone = false;
static volatile esp_now_send_status_t lastSendStatus = ESP_NOW_SEND_FAIL;

void onDataSent(const wifi_tx_info_t *info, esp_now_send_status_t status) {
  (void)info;
  sendDone = true;
  lastSendStatus = status;
}

bool initEspNow() {
  WiFi.mode(WIFI_STA);

  tWiFiAvailabilityStart = micros();

  // Improvement (Lower TX power (dBm)): Set maximum transmission power to 13 dBm instead of 19.5 dBm
  // Reduces peak power significantly, while slightly reducing range (acceptable in simulation)
  if (esp_wifi_set_max_tx_power(52) != ESP_OK) { // power is in 0.25 dBm steps (e.g. 52 → 13 dBm)
    runLog("error: esp_wifi_set_max_tx_power");
    return false;
  }

  if (esp_now_init() != ESP_OK) {
    runLog("error: esp_now_init");
    return false;
  }

  esp_now_register_send_cb(onDataSent);

  esp_now_peer_info_t peerInfo = {};
  memcpy(peerInfo.peer_addr, broadcastAddress, sizeof(broadcastAddress));
  peerInfo.channel = 0;
  peerInfo.encrypt = false;

  if (esp_now_add_peer(&peerInfo) != ESP_OK) {
    runLog("error: esp_now_add_peer");
    return false;
  }

  return true;
}

// ---------------------------------------------------------------------------
// Build payload
// ---------------------------------------------------------------------------

bool readMotion() {
  return digitalRead(PIR_PIN) == HIGH;
}

int readLuminosity() {
  return analogRead(LDR_PIN);
}

String buildPayloadFrom(bool motion, int lux) {
  char luxStr[8];
  snprintf(luxStr, sizeof(luxStr), "%03d", lux);

  // Improvement (Compact message format): MOTION_DETECTED-LUMINOSITY -> 1, MOTION_NOT_DETECTED-LUMINOSITY -> 0
  // Shorter packet -> lower transmission time -> less energy
  if (motion) {
    return String("1:") + luxStr;
  }
  return String("0:") + luxStr;
}

// ---------------------------------------------------------------------------
// Send Payload
// ---------------------------------------------------------------------------

typedef struct {
  char text[64];
} MessagePacket;

bool sendPayload(const String &payload) {
  MessagePacket packet = {};
  payload.toCharArray(packet.text, sizeof(packet.text));

  sendDone = false;

  tSenderSpikeStart = micros();

  esp_err_t err = esp_now_send(broadcastAddress, (uint8_t *)&packet, sizeof(packet));

  if (err != ESP_OK) {
    runLog("error: esp_now_send");
    return false;
  }

  while (!sendDone) {
    delayMicroseconds(1);
  }

  tSenderSpikeEnd = micros();

  return sendDone && (lastSendStatus == ESP_NOW_SEND_SUCCESS);
}

// ---------------------------------------------------------------------------
// Deep sleep
// ---------------------------------------------------------------------------

float deepSleepSecondsFromPersonCode(const String &personCode) {
  const int ab = personCode.substring(personCode.length() - 2).toInt();
  return (ab % 50 + 5) / 10.0f;
}

const float DEEP_SLEEP_SECONDS = deepSleepSecondsFromPersonCode(PERSON_CODE);

void goToDeepSleep() {
  esp_sleep_enable_timer_wakeup((uint64_t)(DEEP_SLEEP_SECONDS * uS_TO_S_FACTOR));
  Serial.flush();
  esp_deep_sleep_start();
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

void setup() {
  tBootStart = micros();
  Serial.begin(115200);
  tBootEnd = micros();

  initPins();

  tSensorAvailabilityStart = micros();

  const bool espNowReady = initEspNow();
  if (!espNowReady) {
    runLog("warn: communication_init failed, deep_sleep");
    goToDeepSleep();
  }
  tSenderAvailabilityStart = micros();

  tSensorReadStart = micros();
  const bool motion = readMotion();
  // Improvement (Avoid reading luminosity if motion is not detected): Only read luminosity if motion is detected
  const int  lux    = motion ? readLuminosity() : lastLux;
  tSensorReadEnd = micros();

  const bool motionChanged = (motion != lastMotion);
  const bool luxChanged    = motion && ((lastLux < 0) || (abs(lux - lastLux) > LUX_THRESHOLD));

  // Improvement (Avoid sending if no significant change): Only send if motion or luminosity has changed significantly
  bool sent = false;
  if (motionChanged || luxChanged) {
    const String payload = buildPayloadFrom(motion, lux);
    runLog("payload: " + payload);

    tWiFiOnStart = micros();
    sent = sendPayload(payload);
    tWiFiOnEnd = micros();
    runLog("send_status: " + String(sent ? "success" : "fail"));

    lastMotion = motion;
    lastLux    = lux;
  } else {
    runLog("skip: no significant change");
    tWiFiOnStart = tWiFiOnEnd = micros();
  }

  tSensorAvailabilityEnd = tSenderAvailabilityEnd = tWiFiAvailabilityEnd = micros();

  tSensorAvailabilityTotal = tSensorAvailabilityEnd - tSensorAvailabilityStart;
  tSensorReadDuration = tSensorReadEnd - tSensorReadStart;
  tSensorAvailabilityIdle = tSensorAvailabilityTotal - tSensorReadDuration;

  tSenderAvailabilityTotal = tSenderAvailabilityEnd - tSenderAvailabilityStart;
  tSenderSpikeDuration = tSenderSpikeEnd - tSenderSpikeStart;
  tSenderAvailabilityIdle = tSenderAvailabilityTotal - tSenderSpikeDuration;

  tBootDuration = tBootEnd - tBootStart;
  tWiFiAvailabilityTotal = tWiFiAvailabilityEnd - tWiFiAvailabilityStart;
  tWiFiOnDuration = tWiFiOnEnd - tWiFiOnStart;
  tWiFiOffDuration = tWiFiAvailabilityTotal - tWiFiOnDuration;

  runLog("sensor_read_us: " + String(tSensorReadDuration));
  runLog("sensor_idle_us: " + String(tSensorAvailabilityIdle));

  runLog("sender_spike_us: " + String(tSenderSpikeDuration));
  runLog("sender_idle_us: " + String(tSenderAvailabilityIdle));

  runLog("wifi_on_us: " + String(tWiFiOnDuration));
  runLog("wifi_off_us: " + String(tWiFiOffDuration));
  runLog("boot_us: " + String(tBootDuration));
  runLog("deep_sleep_s: " + String(DEEP_SLEEP_SECONDS));

  goToDeepSleep();
}

// ---------------------------------------------------------------------------
// Loop
// ---------------------------------------------------------------------------

void loop() {}
