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
 // Relative mouse: five buttons (left, right, middle, back, forward), X/Y, wheel and AC Pan.
 // Each wheel sits in its own logical collection with a Resolution Multiplier feature
 // (logical 0..1 -> 1x..8x). A host that sets it receives 1/8-detent units; others keep detents.
 0x05,0x01, 0x09,0x02, 0xa1,0x01, 0x85,0x03,
 0x09,0x01, 0xa1,0x00,
 0x05,0x09, 0x19,0x01, 0x29,0x05, 0x15,0x00, 0x25,0x01,
 0x75,0x01, 0x95,0x05, 0x81,0x02, 0x75,0x03, 0x95,0x01, 0x81,0x03,
 0x05,0x01, 0x09,0x30, 0x09,0x31,
 0x15,0x81, 0x25,0x7f, 0x75,0x08, 0x95,0x02, 0x81,0x06,
 0xa1,0x02,
 0x09,0x48, 0x15,0x00, 0x25,0x01, 0x35,0x01, 0x45,0x08, 0x75,0x02, 0x95,0x01, 0xb1,0x02,
 0x09,0x38, 0x15,0x81, 0x25,0x7f, 0x35,0x00, 0x45,0x00, 0x75,0x08, 0x81,0x06,
 0xc0,
 0xa1,0x02,
 0x09,0x48, 0x15,0x00, 0x25,0x01, 0x35,0x01, 0x45,0x08, 0x75,0x02, 0xb1,0x02,
 0x35,0x00, 0x45,0x00, 0x75,0x04, 0xb1,0x03,
 0x05,0x0c, 0x0a,0x38,0x02, 0x15,0x81, 0x25,0x7f, 0x75,0x08, 0x81,0x06,
 0xc0,
 0xc0,0xc0,
 // Consumer AC Keyboard Layout Select: Apple's Globe key.
 0x05,0x0c, 0x09,0x01, 0xa1,0x01, 0x85,0x04,
 0x15,0x00, 0x25,0x01, 0x0a,0x9d,0x02,
 0x75,0x01, 0x95,0x01, 0x81,0x02,
 0x75,0x07, 0x95,0x01, 0x81,0x03, 0xc0,
 // Consumer keys as a 16-bit usage array: volume, playback, brightness, AC Home/Back and so on.
 0x05,0x0c, 0x09,0x01, 0xa1,0x01, 0x85,0x05,
 0x15,0x00, 0x26,0xff,0x03, 0x19,0x00, 0x2a,0xff,0x03,
 0x75,0x10, 0x95,0x01, 0x81,0x00, 0xc0,
 // System Control: power down, sleep and wake up as values 1..3.
 0x05,0x01, 0x09,0x80, 0xa1,0x01, 0x85,0x06,
 0x19,0x81, 0x29,0x83, 0x15,0x01, 0x25,0x03,
 0x75,0x02, 0x95,0x01, 0x81,0x00, 0x75,0x06, 0x81,0x03, 0xc0
};
static NimBLECharacteristic *touchReport, *keyReport, *wheelReport, *globeReport, *consumerReport, *systemReport, *wheelFeature;
// Set by the host through the Resolution Multiplier feature; cleared for each new connection.
static volatile bool wheelHiRes = false, panHiRes = false;
static constexpr int32_t HI_RES_UNITS = 8;  // Units per detent once the multiplier is enabled.
static int32_t scrollRemainder[2] = {};
// Scroll waiting to be sent, in report units (detents, or 1/8 detents once hi-res is on).
// One report per connection interval keeps a fast swipe from arriving as a single jump or a burst.
static int32_t scrollPending[2] = {};
static uint32_t lastScrollReport = 0;
static volatile uint16_t connInterval = 12;  // 1.25 ms units; 12 = 15 ms.
static constexpr uint16_t PREFERRED_INTERVAL = 12;  // 15 ms, within Apple's accessory guidelines.
static volatile bool connected = false;
static uint16_t lastX = 0, lastY = 0;
static bool pointerDown = false;
static uint8_t pointerButton = 0;
static bool relativeButton = false;
static bool sessionReady = false;
static bool keysDown = false;
static uint32_t lastActivity = 0;
static constexpr uint32_t INPUT_TIMEOUT_MS = 2500;
// Standby keeps USB serial alive so the desktop can wake it; deep sleep would drop the USB CDC link.
static bool sleeping = false;
static constexpr uint32_t AWAKE_CPU_MHZ = 160, SLEEP_CPU_MHZ = 80;  // USB serial needs the 80 MHz PLL.

class Callbacks : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer*) override { connected = true; Serial.println("CONNECTED"); }
  void onDisconnect(NimBLEServer*) override {
    connected = false;
    // The next host sets its own multiplier; until then send whole detents.
    wheelHiRes = panHiRes = false; scrollRemainder[0] = scrollRemainder[1] = 0;
    scrollPending[0] = scrollPending[1] = 0;
    const uint8_t lowRes = 0; wheelFeature->setValue(&lowRes, 1);
    Serial.println("DISCONNECTED");
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
void relativeMouse(uint8_t buttons, int8_t wheel = 0, int8_t pan = 0) {
  const uint8_t data[] = {buttons, 0, 0, static_cast<uint8_t>(wheel), static_cast<uint8_t>(pan)};
  wheelReport->setValue(data, sizeof(data)); wheelReport->notify();
}
void globe(bool down) {
  const uint8_t data[] = {static_cast<uint8_t>(down)};
  globeReport->setValue(data, sizeof(data)); globeReport->notify();
}
void consumer(uint16_t usage) {
  const uint8_t data[] = {static_cast<uint8_t>(usage & 255), static_cast<uint8_t>(usage >> 8)};
  consumerReport->setValue(data, sizeof(data)); consumerReport->notify();
}
void systemControl(uint8_t value) {
  systemReport->setValue(&value, 1); systemReport->notify();
}
// Spend about a third of the backlog per report so a long swipe eases out instead of lagging.
static int8_t scrollStep(int32_t pending) {
  if (!pending) return 0;
  int32_t step = (abs(pending) + 2) / 3;
  if (step > 127) step = 127;
  return pending > 0 ? step : -step;
}
void flushScroll(bool force = false) {
  if (!scrollPending[0] && !scrollPending[1]) return;
  const uint32_t now = millis();
  if (!force && now - lastScrollReport < (connInterval * 5u + 3) / 4) return;
  const int8_t wheel = scrollStep(scrollPending[0]), pan = scrollStep(scrollPending[1]);
  scrollPending[0] -= wheel; scrollPending[1] -= pan;
  // Use the current button state so a pending scroll never re-presses a released button.
  relativeMouse(relativeButton && pointerDown ? pointerButton : 0, wheel, pan);
  lastScrollReport = now;
}
void clearScroll() { scrollPending[0] = scrollPending[1] = 0; }
// Scroll in 1/8-detent units on axis 0 (wheel, positive up) or 1 (pan, positive right).
// Without the host's multiplier the units collect into whole detents.
void scrollUnits(uint8_t axis, int32_t units) {
  int32_t amount = units;
  if (!(axis ? panHiRes : wheelHiRes)) {
    scrollRemainder[axis] += units;
    amount = scrollRemainder[axis] / HI_RES_UNITS;
    scrollRemainder[axis] -= amount * HI_RES_UNITS;
  }
  // Reversing direction drops the old backlog so the page responds at once.
  if ((amount > 0 && scrollPending[axis] < 0) || (amount < 0 && scrollPending[axis] > 0)) scrollPending[axis] = 0;
  scrollPending[axis] += amount;
  flushScroll();  // Sends immediately when the link has been idle for an interval.
}
class WheelFeatureCallbacks : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic *characteristic) override {
    const std::string value = characteristic->getValue();
    const uint8_t bits = value.empty() ? 0 : value[0];
    wheelHiRes = bits & 0x03; panHiRes = bits & 0x0c;
    scrollRemainder[0] = scrollRemainder[1] = 0; clearScroll();
  }
};

void releaseAll() {
  pointerDown = false; keysDown = false; relativeButton = false; pointerButton = 0; clearScroll();
  if (!connected) return;
  touch(false, lastX, lastY); relativeMouse(0);
  const uint8_t empty[6] = {}; keyState(0, empty); globe(false);
}
// Report a power-off like a keyboard switched off, so the target reconnects when it sees us again.
void dropPeers() {
  NimBLEServer *server = NimBLEDevice::getServer();
  for (uint16_t handle : server->getPeerDevices()) server->disconnect(handle, BLE_ERR_RD_CONN_TERM_PWROFF);
}
// Drop the target and stop advertising so the target no longer sees a keyboard and pointer.
void enterSleep() {
  if (sleeping) return;
  releaseAll();
  sleeping = true;
  NimBLEDevice::stopAdvertising();
  dropPeers();
  setCpuFrequencyMhz(SLEEP_CPU_MHZ);
  Serial.println("SLEEPING");
}
// Advertising again lets the bonded target reconnect on its own.
void exitSleep() {
  if (!sleeping) return;
  setCpuFrequencyMhz(AWAKE_CPU_MHZ);
  sleeping = false;
  NimBLEDevice::startAdvertising();
  Serial.println("AWAKE");
}

// The target's own GAP name and Device Information identify which device is connected.
// Reads run one at a time in the NimBLE host task; loop() prints the finished result.
static constexpr size_t PEER_FIELD = 96;
struct PeerInfo { char name[PEER_FIELD], manufacturer[PEER_FIELD], model[PEER_FIELD]; };
static const ble_uuid16_t PEER_UUIDS[] = {{{BLE_UUID_TYPE_16}, 0x2a00}, {{BLE_UUID_TYPE_16}, 0x2a29}, {{BLE_UUID_TYPE_16}, 0x2a24}};  // Name, manufacturer, model
static PeerInfo peerWork, peerInfo;
static size_t peerLength[3];
static uint8_t peerStep;
static uint16_t peerConn = BLE_HS_CONN_HANDLE_NONE, peerHandle;
static volatile bool peerKnown = false, peerChanged = false;
static portMUX_TYPE peerLock = portMUX_INITIALIZER_UNLOCKED;
static char *peerField(PeerInfo &info, uint8_t step) { return step == 0 ? info.name : step == 1 ? info.manufacturer : info.model; }

// Keep the line parseable: no quotes or control bytes, and no UTF-8 sequence cut in half.
static void finishPeerField(char *text, size_t length) {
  size_t start = length;
  while (start && (static_cast<uint8_t>(text[start - 1]) & 0xc0) == 0x80) --start;
  if (start) {
    const uint8_t lead = text[start - 1];
    const size_t need = lead >= 0xf0 ? 4 : lead >= 0xe0 ? 3 : lead >= 0xc0 ? 2 : 1;
    if (length - (start - 1) < need) length = start - 1;
  }
  text[length] = 0;
  for (size_t i = 0; i < length; ++i)
    if (static_cast<uint8_t>(text[i]) < 0x20 || text[i] == '"' || text[i] == 0x7f) text[i] = ' ';
}

static void readNextPeerField();
static int onPeerValue(uint16_t conn, const ble_gatt_error *error, ble_gatt_attr *attr, void *) {
  if (conn != peerConn) return 0;
  if (!error->status && attr) {
    size_t &length = peerLength[peerStep];
    const uint16_t size = OS_MBUF_PKTLEN(attr->om);
    if (attr->offset == length && length < PEER_FIELD - 1) {
      const uint16_t take = min<size_t>(size, PEER_FIELD - 1 - length);
      os_mbuf_copydata(attr->om, 0, take, peerField(peerWork, peerStep) + length);
      length += take;
    }
    return 0;
  }
  ++peerStep; readNextPeerField();
  return 0;
}
static int onPeerHandle(uint16_t conn, const ble_gatt_error *error, ble_gatt_attr *attr, void *) {
  if (conn != peerConn) return 0;
  if (!error->status && attr) { if (!peerHandle) peerHandle = attr->handle; return 0; }
  // Read the full value by handle; a read-by-type response is cut to the ATT MTU.
  if (peerHandle && !ble_gattc_read_long(conn, peerHandle, 0, onPeerValue, nullptr)) return 0;
  ++peerStep; readNextPeerField();
  return 0;
}
static void readNextPeerField() {
  for (; peerStep < 3; ++peerStep) {
    peerHandle = 0;
    if (!ble_gattc_read_by_uuid(peerConn, 1, 0xffff, &PEER_UUIDS[peerStep].u, onPeerHandle, nullptr)) return;
  }
  for (uint8_t step = 0; step < 3; ++step) finishPeerField(peerField(peerWork, step), peerLength[step]);
  portENTER_CRITICAL(&peerLock);
  peerInfo = peerWork; peerKnown = true; peerChanged = true;
  portEXIT_CRITICAL(&peerLock);
}
static void startPeerRead(uint16_t conn) {
  peerConn = conn; peerStep = 0;
  memset(&peerWork, 0, sizeof(peerWork)); memset(peerLength, 0, sizeof(peerLength));
  readNextPeerField();
}
static void forgetPeer() {
  portENTER_CRITICAL(&peerLock);
  peerConn = BLE_HS_CONN_HANDLE_NONE; peerKnown = false; peerChanged = false;
  portEXIT_CRITICAL(&peerLock);
}
static void printPeer() {
  PeerInfo info;
  portENTER_CRITICAL(&peerLock);
  const bool known = peerKnown; info = peerInfo; peerChanged = false;
  portEXIT_CRITICAL(&peerLock);
  if (!known) { Serial.println("PEER unknown"); return; }
  Serial.printf("PEER name=\"%s\" manufacturer=\"%s\" model=\"%s\"\n", info.name, info.manufacturer, info.model);
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
    Serial.printf("%s sleeping=%u advertising=%u bonds=%d touch_subscribers=%u key_subscribers=%u mouse_subscribers=%u wheel_hires=%u pan_hires=%u interval_ms=%u\n", connected ? "CONNECTED" : "DISCONNECTED", (unsigned)sleeping, (unsigned)NimBLEDevice::getAdvertising()->isAdvertising(), NimBLEDevice::getNumBonds(), (unsigned)touchReport->getSubscribedCount(), (unsigned)keyReport->getSubscribedCount(), (unsigned)wheelReport->getSubscribedCount(), (unsigned)wheelHiRes, (unsigned)panHiRes, (unsigned)(connInterval * 5u / 4)); return;
  }
  if (!strcmp(op, "peer") && count == 0) { printPeer(); return; }
  if (!strcmp(op, "sleep") && count == 0) { enterSleep(); Serial.println("OK"); return; }
  if (!strcmp(op, "wake") && count == 0) { exitSleep(); Serial.println("OK"); return; }
  if (!strcmp(op, "ble_recover") && count == 0) {
    exitSleep();
    if (connected) { Serial.println("OK already connected"); return; }
    NimBLEDevice::stopAdvertising();
    if (NimBLEDevice::startAdvertising()) Serial.println("OK advertising");
    else Serial.println("ERR advertising failed");
    return;
  }
  // Without a stored peer IRK the iPad's rotating address cannot be matched to its bond.
  if (!strcmp(op, "bonds") && count == 0) {
    ble_addr_t peers[MYNEWT_VAL(BLE_STORE_MAX_BONDS)];
    int peerCount = 0;
    ble_store_util_bonded_peers(peers, &peerCount, MYNEWT_VAL(BLE_STORE_MAX_BONDS));
    for (int i = 0; i < peerCount; ++i) {
      ble_store_key_sec key = {};
      ble_store_value_sec value = {};
      key.peer_addr = peers[i];
      const int rc = ble_store_read_peer_sec(&key, &value);
      Serial.printf("BLE bond %d addr=%s type=%u rc=%d irk=%u ltk=%u sc=%u authenticated=%u\n", i,
                    NimBLEAddress(key.peer_addr).toString().c_str(), key.peer_addr.type, rc,
                    value.irk_present, value.ltk_present, value.sc, value.authenticated);
    }
    Serial.println("OK"); return;
  }
  // A bond without the peer IRK cannot be repaired in place; drop it so the target pairs again.
  if (!strcmp(op, "unpair") && count == 0) {
    NimBLEServer *server = NimBLEDevice::getServer();
    for (uint16_t handle : server->getPeerDevices()) server->disconnect(handle);
    NimBLEDevice::deleteAllBonds();
    Serial.printf("OK bonds=%d\n", NimBLEDevice::getNumBonds()); return;
  }
  if (!strcmp(op, "help") && count == 0) {
    Serial.println("status | peer | bonds | unpair | ble_recover | sleep | wake | goto X Y | prepare X Y | down X Y [BUTTON] | move X Y | up | scroll DIRECTION STEPS | smooth AXIS DIRECTION UNITS | media USAGE | system USAGE | keys MOD [USAGE ...] | shortcut MOD USAGE | expose | release"); return;
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
  // Buttons 1-2 press on the absolute pointer; 3-7 are relative left, right, middle, back and forward.
  if (!strcmp(op, "down") && (count == 2 || count == 3) && !pointerDown && (count == 2 || (args[2] >= 1 && args[2] <= 7))) {
    const uint8_t selected = count == 3 ? args[2] : 1;
    relativeButton = selected > 2;
    pointerButton = relativeButton ? 1 << (selected - 3) : selected;
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
    if (relativeButton) while (scrollPending[0] || scrollPending[1]) flushScroll(true);
    pointerDown = false; pointerButton = 0;
    if (relativeButton) relativeMouse(0); else touch(false, lastX, lastY);
    relativeButton = false; Serial.println("OK"); return;
  }
  if (!strcmp(op, "scroll") && count == 2 && args[0] <= 1 && args[1] >= 1 && args[1] <= 127) {
    scrollUnits(0, (args[0] ? 1 : -1) * static_cast<int32_t>(args[1]) * HI_RES_UNITS);
    Serial.println("OK"); return;
  }
  if (!strcmp(op, "smooth") && count == 3 && args[0] <= 1 && args[1] <= 1 && args[2] >= 1 && args[2] <= 127 * HI_RES_UNITS) {
    scrollUnits(args[0], (args[1] ? 1 : -1) * static_cast<int32_t>(args[2]));
    Serial.println("OK"); return;
  }
  // Taps only, so nothing can be left held; the delay spans a connection interval.
  if (!strcmp(op, "media") && count == 1 && args[0] >= 1 && args[0] <= 0x3ff) {
    if (!consumerReport->getSubscribedCount()) { Serial.println("ERR media not subscribed"); return; }
    consumer(args[0]); delay(60); consumer(0);
    Serial.println("OK"); return;
  }
  if (!strcmp(op, "system") && count == 1 && args[0] >= 0x81 && args[0] <= 0x83) {
    if (!systemReport->getSubscribedCount()) { Serial.println("ERR system not subscribed"); return; }
    systemControl(args[0] - 0x80); delay(60); systemControl(0);
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
    clearScroll(); pointerDown = false; keysDown = false; touch(false, lastX, lastY); relativeMouse(0); relativeButton = false;
    const uint8_t empty[6] = {}; keyState(0, empty); globe(false); Serial.println("OK"); return;
  }
  Serial.println("ERR invalid command");
}
static void logParams(const ble_gap_conn_desc &desc) {
  connInterval = desc.conn_itvl;
  Serial.printf("BLE params interval_ms=%u.%02u latency=%u timeout_ms=%u\n", desc.conn_itvl * 125 / 100, desc.conn_itvl * 125 % 100,
                desc.conn_latency, desc.supervision_timeout * 10);
}
// Input feels sluggish above 15 ms; ask once per connection, the target decides.
static void requestFastInterval(uint16_t conn) {
  ble_gap_conn_desc desc;
  if (ble_gap_conn_find(conn, &desc) || desc.conn_itvl <= PREFERRED_INTERVAL) return;
  ble_gap_upd_params params = {};
  params.itvl_min = params.itvl_max = PREFERRED_INTERVAL;
  params.latency = 0;
  params.supervision_timeout = 400;  // 4 s.
  Serial.printf("BLE params request interval_ms=15 rc=%d\n", ble_gap_update_params(conn, &params));
}
// Trace each link stage so a failed reconnect shows where it stopped.
static int logGap(ble_gap_event *event, void *) {
  ble_gap_conn_desc desc;
  switch (event->type) {
    case BLE_GAP_EVENT_CONNECT:
      if (!event->connect.status && !ble_gap_conn_find(event->connect.conn_handle, &desc))
        Serial.printf("BLE connect peer=%s type=%u ota=%s type=%u\n", NimBLEAddress(desc.peer_id_addr).toString().c_str(), desc.peer_id_addr.type,
                      NimBLEAddress(desc.peer_ota_addr).toString().c_str(), desc.peer_ota_addr.type);
      else Serial.printf("BLE connect failed status=%d\n", event->connect.status);
      if (!event->connect.status && !ble_gap_conn_find(event->connect.conn_handle, &desc)) logParams(desc);
      break;
    case BLE_GAP_EVENT_CONN_UPDATE:
      if (!ble_gap_conn_find(event->conn_update.conn_handle, &desc)) logParams(desc);
      else Serial.printf("BLE params update status=%d\n", event->conn_update.status);
      break;
    case BLE_GAP_EVENT_DISCONNECT:
      forgetPeer();
      Serial.printf("BLE disconnect reason=0x%x\n", event->disconnect.reason); break;
    case BLE_GAP_EVENT_ENC_CHANGE:
      // Some targets only expose Device Information over an encrypted link.
      if (!event->enc_change.status) {
        startPeerRead(event->enc_change.conn_handle);
        requestFastInterval(event->enc_change.conn_handle);
      }
      if (!ble_gap_conn_find(event->enc_change.conn_handle, &desc))
        Serial.printf("BLE encryption status=%d encrypted=%u bonded=%u\n", event->enc_change.status, desc.sec_state.encrypted, desc.sec_state.bonded);
      else Serial.printf("BLE encryption status=%d\n", event->enc_change.status);
      break;
    case BLE_GAP_EVENT_REPEAT_PAIRING: Serial.println("BLE repeat pairing"); break;
    case BLE_GAP_EVENT_SUBSCRIBE:
      Serial.printf("BLE subscribe handle=%u notify=%u\n", event->subscribe.attr_handle, event->subscribe.cur_notify); break;
    case BLE_GAP_EVENT_ADV_COMPLETE:
      Serial.printf("BLE advertising complete reason=%d\n", event->adv_complete.reason); break;
  }
  return 0;
}
void setup() {
  Serial.begin(115200);
  NimBLEDevice::init("ESP32 Remote Input");
  NimBLEDevice::setCustomGapHandler(logGap);
  NimBLEDevice::setSecurityAuth(true, false, true);
  // iOS reconnects from a rotating private address; both sides exchange identity keys so the bond can follow it.
  NimBLEDevice::setSecurityInitKey(BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID);
  NimBLEDevice::setSecurityRespKey(BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID);
  NimBLEServer *server = NimBLEDevice::createServer();
  server->setCallbacks(new Callbacks());
  // loop() restarts advertising; NimBLE's own restart would ignore standby and invite a reconnect loop.
  server->advertiseOnDisconnect(false);
  auto *hid = new NimBLEHIDDevice(server);
  keyReport = hid->inputReport(2);
  hid->outputReport(2);
  touchReport = hid->inputReport(1); wheelReport = hid->inputReport(3);
  globeReport = hid->inputReport(4);
  consumerReport = hid->inputReport(5); systemReport = hid->inputReport(6);
  wheelFeature = hid->featureReport(3);
  const uint8_t lowRes = 0; wheelFeature->setValue(&lowRes, 1);
  wheelFeature->setCallbacks(new WheelFeatureCallbacks());
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
  static uint32_t lastAdvertisingCheck = 0;
  // A target that connected just before advertising stopped is dropped too.
  static uint32_t lastSleepDrop = 0;
  if (sleeping && connected && millis() - lastSleepDrop >= 500) {
    lastSleepDrop = millis();
    dropPeers();
  }
  if (!sleeping && !connected && !NimBLEDevice::getAdvertising()->isAdvertising() && millis() - lastAdvertisingCheck >= 1000) {
    lastAdvertisingCheck = millis();
    if (NimBLEDevice::startAdvertising()) Serial.println("ADVERTISING");
  }
  if (peerChanged) printPeer();
  if (connected) flushScroll();
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
  delay(sleeping ? 10 : 2);
}
