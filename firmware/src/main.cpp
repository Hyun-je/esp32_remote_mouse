#include <Arduino.h>
#include <NimBLEDevice.h>
#include <NimBLEHIDDevice.h>
#include <stdlib.h>
#include <string.h>

// Touch coordinates are normalized to 0..10000; keyboard usages follow HID page 7.
static const uint8_t reportMap[] = {
 // Standard keyboard collection first, including the keyboard LED output.
 0x05,0x01, 0x09,0x06, 0xa1,0x01, 0x85,0x02,
 0x05,0x07, 0x19,0xe0, 0x29,0xe7, 0x15,0x00, 0x25,0x01,
 0x75,0x01, 0x95,0x08, 0x81,0x02,
 0x95,0x01, 0x75,0x08, 0x81,0x01,
 0x95,0x05, 0x75,0x01, 0x05,0x08, 0x19,0x01, 0x29,0x05, 0x91,0x02,
 0x95,0x01, 0x75,0x03, 0x91,0x01,
 0x95,0x06, 0x75,0x08, 0x15,0x00, 0x25,0x65, 0x05,0x07,
 0x19,0x00, 0x29,0x65, 0x81,0x00, 0xc0,
  // Absolute mouse: buttons and 16-bit X/Y in one report, based on
 // esp32controlios BLE Combo's Absolute Pointer report.
 0x05,0x01, 0x09,0x02, 0xa1,0x01, 0x85,0x01,
 0x09,0x01, 0xa1,0x00, 0x05,0x09, 0x19,0x01, 0x29,0x02,
 0x15,0x00, 0x25,0x01, 0x75,0x01, 0x95,0x02, 0x81,0x02,
 0x75,0x06, 0x95,0x01, 0x81,0x03,
 0x05,0x01, 0x09,0x30, 0x09,0x31, 0x15,0x00, 0x26,0xff,0x7f,
 0x75,0x10, 0x95,0x02, 0x81,0x02, 0xc0,0xc0,
 // Separate relative mouse report for wheel and optional button compatibility.
 0x05,0x01, 0x09,0x02, 0xa1,0x01, 0x85,0x03,
 0x09,0x01, 0xa1,0x00,
 0x05,0x09, 0x19,0x01, 0x29,0x03, 0x15,0x00, 0x25,0x01,
 0x75,0x01, 0x95,0x03, 0x81,0x02, 0x75,0x05, 0x95,0x01, 0x81,0x03,
 0x05,0x01, 0x09,0x30, 0x09,0x31, 0x09,0x38,
 0x15,0x81, 0x25,0x7f, 0x75,0x08, 0x95,0x03, 0x81,0x06, 0xc0,0xc0,
 // Consumer AC Keyboard Layout Select: Apple's Globe key.
 0x05,0x0c, 0x09,0x01, 0xa1,0x01, 0x85,0x04,
 0x15,0x00, 0x25,0x01, 0x0a,0x9d,0x02,
 0x75,0x01, 0x95,0x01, 0x81,0x02,
 0x75,0x07, 0x95,0x01, 0x81,0x03, 0xc0
};
static NimBLECharacteristic *touchReport, *keyReport, *wheelReport, *globeReport;
static volatile bool connected = false;
static uint16_t lastX = 0, lastY = 0;
static bool pointerDown = false;
static uint8_t pointerButton = 0;
static bool relativeButton = false;
static bool sessionReady = false;
static bool keysDown = false;
static uint32_t lastActivity = 0;
static constexpr uint32_t INPUT_TIMEOUT_MS = 2500;

class Callbacks : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer*) override { connected = true; Serial.println("CONNECTED"); }
  void onDisconnect(NimBLEServer*) override {
    connected = false;
    Serial.println("DISCONNECTED");
    NimBLEDevice::startAdvertising();
  }
};

void touch(bool down, uint16_t x, uint16_t y) {
  lastX = x; lastY = y;
  uint8_t data[] = {static_cast<uint8_t>(down ? pointerButton : 0), static_cast<uint8_t>((x * 32767UL / 10000) & 255), static_cast<uint8_t>((x * 32767UL / 10000) >> 8), static_cast<uint8_t>((y * 32767UL / 10000) & 255), static_cast<uint8_t>((y * 32767UL / 10000) >> 8)};
  touchReport->setValue(data, sizeof(data)); touchReport->notify();
}
void keyState(uint8_t modifiers, const uint8_t usages[6]) {
  uint8_t data[] = {modifiers, 0, 0, 0, 0, 0, 0, 0};
  memcpy(data + 2, usages, 6);
  keyReport->setValue(data, sizeof(data)); keyReport->notify();
}
void relativeMouse(uint8_t buttons, int8_t wheel = 0) {
  const uint8_t data[] = {buttons, 0, 0, static_cast<uint8_t>(wheel)};
  wheelReport->setValue(data, sizeof(data)); wheelReport->notify();
}
void globe(bool down) {
  const uint8_t data[] = {static_cast<uint8_t>(down)};
  globeReport->setValue(data, sizeof(data)); globeReport->notify();
}

// Parse the entire command; reject negative values, overflow, trailing junk and excess arguments.
void command(String line) {
  line.trim();
  char buffer[81];
  line.toCharArray(buffer, sizeof(buffer));
  char *save = nullptr;
  char *op = strtok_r(buffer, " \t", &save);
  if (!op) return;
  unsigned long args[7] = {};
  size_t count = 0;
  for (char *token = strtok_r(nullptr, " \t", &save); token; token = strtok_r(nullptr, " \t", &save)) {
    if (count == 7 || token[0] < '0' || token[0] > '9') { Serial.println("ERR invalid command"); return; }
    char *end = nullptr;
    unsigned long value = strtoul(token, &end, 10);
    if (*end || value > 10000) { Serial.println("ERR invalid command"); return; }
    args[count++] = value;
  }
  lastActivity = millis();
  if (!strcmp(op, "status") && count == 0) {
    Serial.printf("%s touch_subscribers=%u key_subscribers=%u mouse_subscribers=%u\n", connected ? "CONNECTED" : "DISCONNECTED", (unsigned)touchReport->getSubscribedCount(), (unsigned)keyReport->getSubscribedCount(), (unsigned)wheelReport->getSubscribedCount()); return;
  }
  if (!strcmp(op, "help") && count == 0) {
    Serial.println("status | goto X Y | prepare X Y | down X Y [BUTTON] | move X Y | up | scroll DIRECTION STEPS | keys MOD [USAGE ...] | shortcut MOD USAGE | expose | release"); return;
  }
  if (!connected || !sessionReady) { Serial.println("ERR not connected"); return; }
  // Keep shortcut press and release ordered even if serial output is delayed.
  if (!strcmp(op, "shortcut") && count == 2 && args[0] <= 255 && args[1] >= 4 && args[1] <= 101 && !pointerDown && !keysDown) {
    const uint8_t pressed[6] = {static_cast<uint8_t>(args[1])};
    const uint8_t empty[6] = {};
    keyState(args[0], pressed); delay(120); keyState(0, empty);
    Serial.println("OK"); return;
  }
  if (!strcmp(op, "expose") && count == 0 && !pointerDown && !keysDown) {
    if (!globeReport->getSubscribedCount()) { Serial.println("ERR globe not subscribed"); return; }
    const uint8_t down[6] = {81};
    const uint8_t empty[6] = {};
    globe(true); delay(60); keyState(0, down); delay(100);
    keyState(0, empty); delay(60); globe(false);
    Serial.println("OK"); return;
  }
  // Button 3 can be assigned to App Switcher in iPad AssistiveTouch.
  if (!strcmp(op, "appswitcher") && count == 0 && !pointerDown) {
    relativeMouse(4); delay(100); relativeMouse(0);
    Serial.println("OK"); return;
  }
  if (!strcmp(op, "goto") && count == 2 && !pointerDown) {
    touch(false, args[0], args[1]);
    Serial.println(connected ? "OK" : "ERR disconnected"); return;
  }
  if (!strcmp(op, "prepare") && count == 2 && !pointerDown) {
    const int32_t fromX = lastX, fromY = lastY;
    for (int i = 1; i <= 8 && connected; ++i) {
      touch(false, fromX + (static_cast<int32_t>(args[0]) - fromX) * i / 8,
                   fromY + (static_cast<int32_t>(args[1]) - fromY) * i / 8);
      delay(6);
    }
    Serial.println(connected ? "OK" : "ERR disconnected"); return;
  }
  if (!strcmp(op, "down") && (count == 2 || count == 3) && !pointerDown && (count == 2 || (args[2] >= 1 && args[2] <= 4))) {
    const uint8_t selected = count == 3 ? args[2] : 1;
    relativeButton = selected > 2;
    pointerButton = relativeButton ? selected - 2 : selected;
    pointerDown = true;
    if (relativeButton) relativeMouse(pointerButton); else touch(true, args[0], args[1]);
    // Keep the press visible for at least one iPad BLE connection interval.
    delay(60);
    Serial.println("OK"); return;
  }
  if (!strcmp(op, "move") && count == 2 && pointerDown) {
    touch(!relativeButton, args[0], args[1]); Serial.println("OK"); return;
  }
  if (!strcmp(op, "up") && count == 0) {
    pointerDown = false; pointerButton = 0;
    if (relativeButton) relativeMouse(0); else touch(false, lastX, lastY);
    relativeButton = false; Serial.println("OK"); return;
  }
  if (!strcmp(op, "scroll") && count == 2 && args[0] <= 1 && args[1] >= 1 && args[1] <= 127) {
    const int8_t delta = args[0] ? args[1] : -static_cast<int>(args[1]);
    relativeMouse(relativeButton && pointerDown ? pointerButton : 0, delta);
    Serial.println("OK"); return;
  }
  if ((!strcmp(op, "keys") && count >= 1 && count <= 7) || (!strcmp(op, "key") && count == 2)) {
    if (args[0] > 255) { Serial.println("ERR invalid command"); return; }
    uint8_t usages[6] = {};
    for (size_t i = 1; i < count; ++i) {
      if (args[i] > 101) { Serial.println("ERR invalid command"); return; }
      usages[i - 1] = args[i];
    }
    keysDown = args[0] != 0;
    for (uint8_t usage : usages) keysDown |= usage != 0;
    keyState(args[0], usages); Serial.println("OK"); return;
  }
  if (!strcmp(op, "release") && count == 0) {
    pointerDown = false; keysDown = false; touch(false, lastX, lastY); relativeMouse(0); relativeButton = false;
    const uint8_t empty[6] = {}; keyState(0, empty); globe(false); Serial.println("OK"); return;
  }
  Serial.println("ERR invalid command");
}
void setup() {
  Serial.begin(115200);
  NimBLEDevice::init("ESP32 Remote Input");
  NimBLEDevice::setSecurityAuth(true, false, true);
  NimBLEServer *server = NimBLEDevice::createServer();
  server->setCallbacks(new Callbacks());
  auto *hid = new NimBLEHIDDevice(server);
  keyReport = hid->inputReport(2);
  hid->outputReport(2);
  touchReport = hid->inputReport(1); wheelReport = hid->inputReport(3);
  globeReport = hid->inputReport(4);
  hid->manufacturer()->setValue("Remote Input");
  hid->pnp(0x02, 0xe502, 0xa111, 0x0100);
  hid->hidInfo(0, 1);
  hid->reportMap((uint8_t*)reportMap, sizeof(reportMap));
  hid->startServices();
  hid->setBatteryLevel(100);
  auto *advertising = NimBLEDevice::getAdvertising();
  advertising->setAppearance(0x03c1);
  advertising->addServiceUUID(hid->hidService()->getUUID());
  advertising->start();
  Serial.println("READY: type help");
}
void loop() {
  static String line;
  static bool overflow = false;
  // A reconnect must never replay a previously pressed button or key.
  if (!connected) {
    sessionReady = false;
    pointerDown = false;
    relativeButton = false;
    keysDown = false;
  } else if (!sessionReady && touchReport->getSubscribedCount() && keyReport->getSubscribedCount()) {
    pointerDown = false;
    touch(false, lastX, lastY);
    const uint8_t empty[6] = {}; keyState(0, empty); globe(false);
    sessionReady = true;
    lastActivity = millis();
  }
  if (sessionReady && (pointerDown || keysDown) && millis() - lastActivity >= INPUT_TIMEOUT_MS) {
    pointerDown = false; keysDown = false;
    touch(false, lastX, lastY); relativeMouse(0); relativeButton = false;
    const uint8_t empty[6] = {}; keyState(0, empty);
    Serial.println("INPUT_TIMEOUT");
  }
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (overflow) Serial.println("ERR line too long");
      else if (line.length()) command(line);
      line = ""; overflow = false;
    } else if (!overflow && line.length() < 80) line += c;
    else overflow = true;
  }
  delay(2);
}
