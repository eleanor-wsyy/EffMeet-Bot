// ============================================================
// 会议干预机器人 v1.3 — TFT 显示图片版
// 舵机替换为 3.5" TFT LCD (ILI9488) 显示黑白图片
// 库: GFX Library for Arduino (by Moononournation)
// ============================================================

#include <WiFi.h>
#include <PubSubClient.h>
#include <Arduino_GFX_Library.h>

// ============================================================
// 可调参数
// ============================================================
#define TRACK_SPEED     230
#define ROTATE_SPEED    150
#define TURN180_TIME    1100
#define CROSS_PASS_MS   150     // 过十字后继续前进时长（毫秒）
#define EXPRESSION_FEEDBACK_MS 4000
#define ROTATE_TIMEOUT_MS       15000
#define ALIGN_TIMEOUT_MS         5000
#define TRACK_TIMEOUT_MS        60000
#define MQTT_RECONNECT_MS        3000
#define WIFI_RECONNECT_MS        5000

// ============================================================
// WiFi
// ============================================================
#define WIFI_SSID       "xinle"
#define WIFI_PASSWORD   "ljqljqljq"

// ============================================================
// MQTT
// ============================================================
#define MQTT_SERVER      "broker.emqx.io"
#define MQTT_PORT        1883
#define MQTT_TOPIC_SUB   "esp32s3/control"
#define MQTT_TOPIC_PUB   "esp32s3/status"
#define MQTT_MSG_DONE    "done"

// ============================================================
// 引脚定义
// ============================================================

// --- 5 路循迹（保持原接线）---
#define PIN_FL    4
#define PIN_ML    5
#define PIN_MID   6
#define PIN_MR    7
#define PIN_FR    15

// --- 计数循迹 ---
#define PIN_COUNT 1

// --- L298N 电机驱动（保持原接线）---
#define IN1   21
#define IN2   47
#define IN3   14
#define IN4   13
#define ENA   40
#define ENB   41

// ============================================================
// 传感器逻辑
// ============================================================
#define ON_BLACK(pin) (digitalRead(pin) == HIGH)

// ============================================================
// 图片数组
// ============================================================
#include "image_array.h"     // 专注表情，480x320 1-bit 位图
#include "reminder_image.h"  // 提醒表情，480x320 1-bit 位图
#include "curious_image.h"   // 好奇表情，480x320 1-bit 位图
#include "stable_image.h"    // 稳定表情，480x320 1-bit 位图

// ============================================================
// TFT 初始化（软件8位并口，引脚与传感器不冲突）
// ============================================================
// D0-D7: IO2,3,8,9,10,11,12,18
// DC:IO36  CS:IO35  WR:IO37  RD:IO38  RST:IO42
#define TFT_RST 42
Arduino_SWPAR8  tftBus(36, 35, 37, 38, 2, 3, 8, 9, 10, 11, 12, 18);
Arduino_ILI9488 gfx(&tftBus, TFT_RST, 1, false);   // rotation=1（横屏）

// 校色白色 (标准 0xFFFF 偏绿)
#define TFT_WHITE 0xFFBE

// ============================================================
// 全局状态
// ============================================================
WiFiClient   espClient;
PubSubClient mqtt(espClient);

enum ExpressionId : int8_t {
  EXPRESSION_NONE = -1,
  EXPRESSION_FOCUS,
  EXPRESSION_REMINDER,
  EXPRESSION_CURIOUS,
  EXPRESSION_STABLE
};

int          currentDir               = 1;
bool         busy                     = false;
int          pendingTarget            = -1;
ExpressionId pendingArrivalExpression = EXPRESSION_REMINDER;
ExpressionId pendingExpression        = EXPRESSION_NONE;
unsigned long expressionRestoreAt     = 0;
unsigned long lastMqttReconnectAt     = 0;
unsigned long lastWifiReconnectAt     = 0;
uint32_t      renderedFrameCount      = 0;
char          pendingStatus[128]       = {0};

bool mqttConnect();
void serviceNetwork();
void publishStatus(const char* message);

void cooperativeDelay(unsigned long durationMs) {
  unsigned long startedAt = millis();
  while (millis() - startedAt < durationMs) {
    serviceNetwork();
    delay(5);
  }
}

// 所有表情共用同一套尺寸、逐行绘制和屏幕校色逻辑。
void drawExpression(const uint8_t* image) {
  static uint16_t lineBuf[IMG_W];
  unsigned long startedAt = millis();

  // 一次设置整屏地址窗口，再按行连续写像素。旧实现逐像素重复发送
  // CASET/PASET/RAMWR，既慢又容易在中途留下半屏；这里将命令量降到一帧一次。
  gfx.startWrite();
  gfx.writeAddrWindow(0, 0, IMG_W, IMG_H);
  for (int y = 0; y < IMG_H; y++) {
    for (int x = 0; x < IMG_W; x++) {
      int byteIdx = y * (IMG_W / 8) + (x / 8);
      int bitIdx  = 7 - (x % 8);
      uint8_t byteVal = pgm_read_byte(image + byteIdx);
      lineBuf[x] = (byteVal & (1 << bitIdx)) ? TFT_WHITE : 0x0000;
    }
    gfx.writePixels(lineBuf, IMG_W);
  }
  gfx.endWrite();

  renderedFrameCount++;
  Serial.printf(
    "[TFT] 完整帧 #%lu，耗时 %lums\n",
    (unsigned long)renderedFrameCount,
    millis() - startedAt
  );
}

const uint8_t* expressionImage(ExpressionId expression) {
  switch (expression) {
    case EXPRESSION_REMINDER: return reminderImage;
    case EXPRESSION_CURIOUS:  return curiousImage;
    case EXPRESSION_STABLE:   return stableImage;
    case EXPRESSION_FOCUS:
    default:                  return focusImage;
  }
}

const char* expressionName(ExpressionId expression) {
  switch (expression) {
    case EXPRESSION_REMINDER: return "reminder";
    case EXPRESSION_CURIOUS:  return "curious";
    case EXPRESSION_STABLE:   return "stable";
    case EXPRESSION_FOCUS:    return "focus";
    default:                  return "unknown";
  }
}

ExpressionId parseExpressionName(const char* name) {
  if (strcmp(name, "focus") == 0) return EXPRESSION_FOCUS;
  if (strcmp(name, "reminder") == 0) return EXPRESSION_REMINDER;
  if (strcmp(name, "curious") == 0) return EXPRESSION_CURIOUS;
  if (strcmp(name, "stable") == 0) return EXPRESSION_STABLE;
  return EXPRESSION_NONE;
}

void showExpression(ExpressionId expression, unsigned long durationMs = 0) {
  drawExpression(expressionImage(expression));
  expressionRestoreAt = durationMs > 0 ? millis() + durationMs : 0;
  Serial.printf("[表情] %s\n", expressionName(expression));
}

void recoverDisplay(ExpressionId expression) {
  // 电机启停产生的电源波动或干扰可能让 LCD 控制器状态异常。
  // 在到达和返程结束、电机已经停止时重新硬复位并绘制完整帧。
  gfx.begin();
  gfx.fillScreen(0x0000);
  showExpression(expression);
  Serial.printf("[TFT] 已复位并恢复 %s 表情\n", expressionName(expression));
}

void updateExpressionTimeout() {
  if (expressionRestoreAt == 0) return;
  if ((long)(millis() - expressionRestoreAt) >= 0) {
    showExpression(EXPRESSION_FOCUS);
  }
}

// ============================================================
// 电机驱动
// ============================================================
void rightForward(int spd) {
  digitalWrite(IN1, LOW); digitalWrite(IN2, HIGH);
  ledcWrite(0, spd);
}
void rightBackward(int spd) {
  digitalWrite(IN1, HIGH); digitalWrite(IN2, LOW);
  ledcWrite(0, spd);
}
void rightStop() {
  digitalWrite(IN1, LOW); digitalWrite(IN2, LOW);
  ledcWrite(0, 0);
}

void leftForward(int spd) {
  digitalWrite(IN3, LOW); digitalWrite(IN4, HIGH);
  ledcWrite(1, spd);
}
void leftBackward(int spd) {
  digitalWrite(IN3, HIGH); digitalWrite(IN4, LOW);
  ledcWrite(1, spd);
}
void leftStop() {
  digitalWrite(IN3, LOW); digitalWrite(IN4, LOW);
  ledcWrite(1, 0);
}

void motorsStop() { leftStop(); rightStop(); }

// ============================================================
// 原地差速顺时针旋转
// ============================================================
bool rotateSteps(int steps) {
  if (steps == 0) return true;

  int count = 0;
  bool prev = digitalRead(PIN_COUNT);
  unsigned long startedAt = millis();

  rightForward(ROTATE_SPEED);
  leftBackward(ROTATE_SPEED);

  while (count < steps) {
    if (millis() - startedAt >= ROTATE_TIMEOUT_MS) {
      motorsStop();
      Serial.println("[故障] 旋转计数超时，已停止电机");
      return false;
    }
    bool now = digitalRead(PIN_COUNT);
    if (!prev && now) count++;
    prev = now;
    cooperativeDelay(5);
  }
  // 多转一小段越过最后一条线，避免卡在分界线
  cooperativeDelay(80);
  motorsStop();
  cooperativeDelay(300);
  return true;
}

// ============================================================
// 旋转后对线校准
// ============================================================
bool alignToLine() {
  int mid = digitalRead(PIN_MID);
  int ml  = digitalRead(PIN_ML);
  int mr  = digitalRead(PIN_MR);
  int fl  = digitalRead(PIN_FL);
  int fr  = digitalRead(PIN_FR);

  if (mid == HIGH) return true;

  unsigned long startedAt = millis();

  if (fl == HIGH || ml == HIGH) {
    while (digitalRead(PIN_MID) != HIGH) {
      if (millis() - startedAt >= ALIGN_TIMEOUT_MS) {
        motorsStop();
        Serial.println("[故障] 左侧对线超时，已停止电机");
        return false;
      }
      rightForward(ROTATE_SPEED / 2);
      leftBackward(ROTATE_SPEED / 2);
      cooperativeDelay(5);
    }
  } else if (fr == HIGH || mr == HIGH) {
    while (digitalRead(PIN_MID) != HIGH) {
      if (millis() - startedAt >= ALIGN_TIMEOUT_MS) {
        motorsStop();
        Serial.println("[故障] 右侧对线超时，已停止电机");
        return false;
      }
      leftForward(ROTATE_SPEED / 2);
      rightBackward(ROTATE_SPEED / 2);
      cooperativeDelay(5);
    }
  } else {
    rightForward(ROTATE_SPEED / 2);
    leftBackward(ROTATE_SPEED / 2);
    while (millis() - startedAt < ALIGN_TIMEOUT_MS) {
      if (digitalRead(PIN_MID) == HIGH ||
          digitalRead(PIN_ML) == HIGH ||
          digitalRead(PIN_MR) == HIGH) break;
      cooperativeDelay(5);
    }
  }

  motorsStop();
  cooperativeDelay(100);
  bool foundLine = digitalRead(PIN_MID) == HIGH
    || digitalRead(PIN_ML) == HIGH
    || digitalRead(PIN_MR) == HIGH;
  if (!foundLine) {
    Serial.println("[故障] 对线未找到轨迹，已停止电机");
  }
  return foundLine;
}

// ============================================================
// 循迹分级转向
// ============================================================
void trackGo() {
  leftForward(TRACK_SPEED);
  rightForward(TRACK_SPEED);
}
void trackLeft1() {                     // 微左转：双轮反向原地微调
  leftBackward(TRACK_SPEED * 0.6);
  rightForward(TRACK_SPEED * 0.6);
}
void trackLeft2() {                     // 急左转：双轮反向原地急调
  leftBackward(255);
  rightForward(255);
}
void trackRight1() {                    // 微右转：双轮反向原地微调
  leftForward(TRACK_SPEED * 0.6);
  rightBackward(TRACK_SPEED * 0.6);
}
void trackRight2() {                    // 急右转：双轮反向原地急调
  leftForward(255);
  rightBackward(255);
}

// ============================================================
// 循迹至终点（五路全黑）
// ============================================================
bool trackToEnd() {
  unsigned long startedAt = millis();
  while (true) {
    if (millis() - startedAt >= TRACK_TIMEOUT_MS) {
      motorsStop();
      Serial.println("[故障] 前往终点超时，已停止电机");
      return false;
    }
    int fl  = digitalRead(PIN_FL);
    int ml  = digitalRead(PIN_ML);
    int mid = digitalRead(PIN_MID);
    int mr  = digitalRead(PIN_MR);
    int fr  = digitalRead(PIN_FR);

    int blackCount = fl + ml + mid + mr + fr;
    if (blackCount >= 3) {
      motorsStop();
      Serial.println("到达终点");
      return true;
    }

    if (mid == HIGH) trackGo();
    else if (fl == HIGH) trackLeft2();
    else if (fr == HIGH) trackRight2();
    else if (ml == HIGH) trackLeft1();
    else if (mr == HIGH) trackRight1();

    // 转向过程中可能已经到达终点，二次检测
    fl  = digitalRead(PIN_FL);
    ml  = digitalRead(PIN_ML);
    mid = digitalRead(PIN_MID);
    mr  = digitalRead(PIN_MR);
    fr  = digitalRead(PIN_FR);
    blackCount = fl + ml + mid + mr + fr;
    if (blackCount >= 3) {
      motorsStop();
      Serial.println("到达终点");
      return true;
    }

    cooperativeDelay(10);
  }
}

// ============================================================
// 掉头 180°
// ============================================================
void turn180() {
  rightForward(ROTATE_SPEED);
  leftBackward(ROTATE_SPEED);
  cooperativeDelay(TURN180_TIME);
  motorsStop();
  cooperativeDelay(200);
}

// ============================================================
// 循迹返回起点
// ============================================================
bool trackBackToStart() {
  unsigned long startedAt = millis();
  while (true) {
    if (millis() - startedAt >= TRACK_TIMEOUT_MS) {
      motorsStop();
      Serial.println("[故障] 返回起点超时，已停止电机");
      return false;
    }
    if (ON_BLACK(PIN_COUNT)) {
      trackGo();
      cooperativeDelay(CROSS_PASS_MS);
      motorsStop();
      cooperativeDelay(100);

      // 确保 IO1 离开黑线，否则下次旋转无法计数
      if (ON_BLACK(PIN_COUNT)) {
        int creep = 0;
        while (ON_BLACK(PIN_COUNT) && creep < 50) {
          rightForward(ROTATE_SPEED / 3);
          leftBackward(ROTATE_SPEED / 3);
          cooperativeDelay(10); creep++;
        }
        motorsStop();
        Serial.printf("顺时针微调 %dms 离开黑线\n", creep * 10);
      }

      cooperativeDelay(200);
      Serial.println("回到起点");
      return true;
    }

    int fl  = digitalRead(PIN_FL);
    int ml  = digitalRead(PIN_ML);
    int mid = digitalRead(PIN_MID);
    int mr  = digitalRead(PIN_MR);
    int fr  = digitalRead(PIN_FR);

    if (mid == HIGH) trackGo();
    else if (fl == HIGH) trackLeft2();
    else if (fr == HIGH) trackRight2();
    else if (ml == HIGH) trackLeft1();
    else if (mr == HIGH) trackRight1();

    // 转向过程中可能已到十字，二次检测 IO1
    if (ON_BLACK(PIN_COUNT)) {
      trackGo();
      cooperativeDelay(CROSS_PASS_MS);
      motorsStop();
      cooperativeDelay(100);

      if (ON_BLACK(PIN_COUNT)) {
        int creep = 0;
        while (ON_BLACK(PIN_COUNT) && creep < 50) {
          rightForward(ROTATE_SPEED / 3);
          leftBackward(ROTATE_SPEED / 3);
          cooperativeDelay(10); creep++;
        }
        motorsStop();
      }

      cooperativeDelay(200);
      Serial.println("回到起点");
      return true;
    }

    cooperativeDelay(10);
  }
}

// ============================================================
// 执行完整任务
// ============================================================
void abortTask(int target, const char* phase) {
  motorsStop();
  recoverDisplay(EXPRESSION_FOCUS);

  char msg[96];
  snprintf(msg, sizeof(msg), "error|target=%d|phase=%s", target, phase);
  publishStatus(msg);

  busy = false;
  Serial.printf("[任务终止] 目标=%d，阶段=%s\n", target, phase);
}

void doTask(int target, ExpressionId arrivalExpression) {
  busy = true;
  showExpression(EXPRESSION_FOCUS);

  int steps = (target - currentDir + 4) % 4;
  Serial.printf("方向 %d→%d，转%d步\n", currentDir, target, steps);
  if (!rotateSteps(steps)) {
    abortTask(target, "rotate");
    return;
  }
  if (!alignToLine()) {
    abortTask(target, "align_outbound");
    return;
  }

  Serial.println("循迹前进...");
  if (!trackToEnd()) {
    abortTask(target, "outbound");
    return;
  }

  recoverDisplay(arrivalExpression);
  Serial.println("原地停留 4 秒...");
  cooperativeDelay(EXPRESSION_FEEDBACK_MS);
  showExpression(EXPRESSION_FOCUS);

  Serial.println("掉头...");
  turn180();
  if (!alignToLine()) {
    abortTask(target, "align_return");
    return;
  }

  Serial.println("返回起点...");
  if (!trackBackToStart()) {
    abortTask(target, "return");
    return;
  }

  currentDir = ((target - 1 + 2) % 4) + 1;
  recoverDisplay(EXPRESSION_FOCUS);

  char msg[96];
  snprintf(
    msg,
    sizeof(msg),
    "%s|dir=%d|target=%d|expression=%s",
    MQTT_MSG_DONE,
    currentDir,
    target,
    expressionName(arrivalExpression)
  );
  publishStatus(msg);

  busy = false;
  Serial.printf("任务完成，当前方向：%d\n", currentDir);
}

// ============================================================
// MQTT 回调
// ============================================================
void mqttCallback(char* topic, byte* payload, unsigned int len) {
  if (len == 0) return;

  char buf[48] = {0};
  unsigned int copyLen = len < sizeof(buf) - 1 ? len : sizeof(buf) - 1;
  memcpy(buf, payload, copyLen);
  buf[copyLen] = '\0';

  if (strncmp(buf, "expr:", 5) == 0) {
    ExpressionId expression = parseExpressionName(buf + 5);
    if (expression == EXPRESSION_NONE) {
      Serial.printf("[MQTT] 未知表情指令：%s\n", buf);
      return;
    }
    if (busy) {
      Serial.printf("[MQTT] 任务执行中，忽略表情指令：%s\n", buf);
      return;
    }
    pendingExpression = expression;
    Serial.printf("[MQTT] 收到表情：%s\n", expressionName(expression));
    return;
  }

  int target = -1;
  ExpressionId arrivalExpression = EXPRESSION_REMINDER;

  if (strlen(buf) == 1 && buf[0] >= '1' && buf[0] <= '4') {
    target = buf[0] - '0';
  } else if (strncmp(buf, "move:", 5) == 0) {
    char* targetText = buf + 5;
    char* expressionText = strchr(targetText, ':');
    if (expressionText != nullptr) {
      *expressionText = '\0';
      expressionText++;
      arrivalExpression = parseExpressionName(expressionText);
      if (
        arrivalExpression != EXPRESSION_REMINDER
        && arrivalExpression != EXPRESSION_CURIOUS
      ) {
        Serial.printf("[MQTT] 移动指令表情无效：%s\n", expressionText);
        return;
      }
    }
    target = atoi(targetText);
  }

  if (target < 1 || target > 4) {
    Serial.printf("[MQTT] 未知控制指令：%s\n", buf);
    return;
  }

  if (busy) {
    Serial.printf("[MQTT] 忙，忽略目标：%d\n", target);
    return;
  }

  Serial.printf(
    "[MQTT] 收到移动：目标=%d，到达表情=%s\n",
    target,
    expressionName(arrivalExpression)
  );
  pendingTarget = target;
  pendingArrivalExpression = arrivalExpression;
}

// ============================================================
// MQTT 连接
// ============================================================
void flushPendingStatus() {
  if (pendingStatus[0] == '\0' || !mqtt.connected()) return;
  if (mqtt.publish(MQTT_TOPIC_PUB, pendingStatus)) {
    Serial.printf("[MQTT] 补发状态：%s\n", pendingStatus);
    pendingStatus[0] = '\0';
  }
}

void publishStatus(const char* message) {
  if (mqtt.connected() && mqtt.publish(MQTT_TOPIC_PUB, message)) {
    Serial.printf("[MQTT] 已发送：%s → %s\n", MQTT_TOPIC_PUB, message);
    return;
  }

  strncpy(pendingStatus, message, sizeof(pendingStatus) - 1);
  pendingStatus[sizeof(pendingStatus) - 1] = '\0';
  Serial.printf("[MQTT] 暂存待补发状态：%s\n", pendingStatus);
}

void publishExpressionAck(ExpressionId expression) {
  if (!mqtt.connected()) return;
  char msg[80];
  snprintf(
    msg,
    sizeof(msg),
    "ack|type=expression|expression=%s|frame=%lu",
    expressionName(expression),
    (unsigned long)renderedFrameCount
  );
  mqtt.publish(MQTT_TOPIC_PUB, msg);
}

bool mqttConnect() {
  if (mqtt.connected()) return true;
  if (WiFi.status() != WL_CONNECTED) return false;

  Serial.print("连接 MQTT...");
  String id = "EffMeet_ESP32_" + String((uint32_t)ESP.getEfuseMac(), HEX);
  bool connected = mqtt.connect(
    id.c_str(),
    MQTT_TOPIC_PUB,
    0,
    true,
    "offline"
  );
  if (connected) {
    Serial.println("成功");
    mqtt.subscribe(MQTT_TOPIC_SUB);
    mqtt.publish(MQTT_TOPIC_PUB, "online", true);
    flushPendingStatus();
    return true;
  }

  Serial.printf("失败 rc=%d\n", mqtt.state());
  return false;
}

void serviceNetwork() {
  unsigned long now = millis();

  if (WiFi.status() != WL_CONNECTED) {
    // 行驶时不做可能阻塞的重连，保证电机控制循环不中断。
    if (!busy && now - lastWifiReconnectAt >= WIFI_RECONNECT_MS) {
      lastWifiReconnectAt = now;
      Serial.println("[WiFi] 连接中断，正在重连...");
      WiFi.reconnect();
    }
    return;
  }

  if (mqtt.connected()) {
    mqtt.loop();
    flushPendingStatus();
    return;
  }

  // 已断线时等任务结束再建立 TCP 连接；行驶期间只做实时电机控制。
  if (!busy && now - lastMqttReconnectAt >= MQTT_RECONNECT_MS) {
    lastMqttReconnectAt = now;
    mqttConnect();
  }
}

// ============================================================
// 初始化
// ============================================================
void setup() {
  Serial.begin(115200);
  delay(800);
  Serial.println("\n===== v1.3 TFT 显示版 =====");

  // 循迹传感器
  pinMode(PIN_FL,    INPUT);
  pinMode(PIN_ML,    INPUT);
  pinMode(PIN_MID,   INPUT);
  pinMode(PIN_MR,    INPUT);
  pinMode(PIN_FR,    INPUT);
  pinMode(PIN_COUNT, INPUT);

  // 电机
  pinMode(IN1, OUTPUT);
  pinMode(IN2, OUTPUT);
  pinMode(IN3, OUTPUT);
  pinMode(IN4, OUTPUT);
  ledcAttachChannel(ENA, 5000, 8, 0);
  ledcAttachChannel(ENB, 5000, 8, 1);
  motorsStop();

  // TFT 初始化，显示图片（从上电起一直显示）
  gfx.begin();
  gfx.fillScreen(0x0000);

  showExpression(EXPRESSION_FOCUS);
  Serial.println("TFT 初始化完成，图片已显示");

  // WiFi
  Serial.printf("连接 WiFi：%s\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  int timeout = 0;
  while (WiFi.status() != WL_CONNECTED && timeout < 30) {
    delay(1000);
    Serial.print(".");
    timeout++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\nWiFi OK, IP: %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("\nWiFi 连接超时，继续运行（MQTT 将不可用）");
  }

  // MQTT
  mqtt.setServer(MQTT_SERVER, MQTT_PORT);
  mqtt.setCallback(mqttCallback);
  mqtt.setKeepAlive(20);
  mqtt.setSocketTimeout(3);
  mqtt.setBufferSize(256);
  if (!mqttConnect()) {
    Serial.println("[MQTT] 首次连接未完成，将在主循环自动重试");
  }

  Serial.printf("就绪，当前方向：%d\n", currentDir);
}

// ============================================================
// 主循环
// ============================================================
void loop() {
  serviceNetwork();
  updateExpressionTimeout();

  if (pendingExpression != EXPRESSION_NONE) {
    ExpressionId expression = pendingExpression;
    pendingExpression = EXPRESSION_NONE;
    unsigned long duration = expression == EXPRESSION_STABLE
      ? EXPRESSION_FEEDBACK_MS
      : 0;
    showExpression(expression, duration);
    publishExpressionAck(expression);
  }

  if (pendingTarget != -1) {
    int target = pendingTarget;
    ExpressionId arrivalExpression = pendingArrivalExpression;
    pendingTarget = -1;
    pendingArrivalExpression = EXPRESSION_REMINDER;
    doTask(target, arrivalExpression);
  }

  cooperativeDelay(10);
}
