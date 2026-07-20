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
// D0-D7: IO2,3,8,9,10,11,12,19
// DC:IO36  CS:IO35  WR:IO37  RD:IO38  RST:IO33
Arduino_SWPAR8  tftBus(36, 35, 37, 38, 2, 3, 8, 9, 10, 11, 12, 19);
Arduino_ILI9488 gfx(&tftBus, 42, 1, false);   // RST=42, rotation=1(横屏)

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

// 所有表情共用同一套尺寸、逐行绘制和屏幕校色逻辑。
void drawExpression(const uint8_t* image) {
  static uint16_t lineBuf[IMG_W];
  for (int y = 0; y < IMG_H; y++) {
    for (int x = 0; x < IMG_W; x++) {
      int byteIdx = y * (IMG_W / 8) + (x / 8);
      int bitIdx  = 7 - (x % 8);
      uint8_t byteVal = pgm_read_byte(image + byteIdx);
      lineBuf[x] = (byteVal & (1 << bitIdx)) ? TFT_WHITE : 0x0000;
    }
    gfx.draw16bitRGBBitmap(0, y, lineBuf, IMG_W, 1);
  }
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
void rotateSteps(int steps) {
  if (steps == 0) return;

  int count = 0;
  bool prev = digitalRead(PIN_COUNT);

  rightForward(ROTATE_SPEED);
  leftBackward(ROTATE_SPEED);

  while (count < steps) {
    bool now = digitalRead(PIN_COUNT);
    if (!prev && now) count++;
    prev = now;
    delay(5);
  }
  // 多转一小段越过最后一条线，避免卡在分界线
  delay(80);
  motorsStop();
  delay(300);
}

// ============================================================
// 旋转后对线校准
// ============================================================
void alignToLine() {
  int mid = digitalRead(PIN_MID);
  int ml  = digitalRead(PIN_ML);
  int mr  = digitalRead(PIN_MR);
  int fl  = digitalRead(PIN_FL);
  int fr  = digitalRead(PIN_FR);

  if (mid == HIGH) return;

  if (fl == HIGH || ml == HIGH) {
    while (digitalRead(PIN_MID) != HIGH) {
      rightForward(ROTATE_SPEED / 2);
      leftBackward(ROTATE_SPEED / 2);
      delay(5);
    }
  } else if (fr == HIGH || mr == HIGH) {
    while (digitalRead(PIN_MID) != HIGH) {
      leftForward(ROTATE_SPEED / 2);
      rightBackward(ROTATE_SPEED / 2);
      delay(5);
    }
  } else {
    unsigned long start = millis();
    rightForward(ROTATE_SPEED / 2);
    leftBackward(ROTATE_SPEED / 2);
    while (millis() - start < 2000) {
      if (digitalRead(PIN_MID) == HIGH ||
          digitalRead(PIN_ML) == HIGH ||
          digitalRead(PIN_MR) == HIGH) break;
      delay(5);
    }
  }

  motorsStop();
  delay(100);
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
void trackToEnd() {
  while (true) {
    int fl  = digitalRead(PIN_FL);
    int ml  = digitalRead(PIN_ML);
    int mid = digitalRead(PIN_MID);
    int mr  = digitalRead(PIN_MR);
    int fr  = digitalRead(PIN_FR);

    int blackCount = fl + ml + mid + mr + fr;
    if (blackCount >= 3) {
      motorsStop();
      Serial.println("到达终点");
      return;
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
      return;
    }

    delay(10);
  }
}

// ============================================================
// 掉头 180°
// ============================================================
void turn180() {
  rightForward(ROTATE_SPEED);
  leftBackward(ROTATE_SPEED);
  delay(TURN180_TIME);
  motorsStop();
  delay(200);
}

// ============================================================
// 循迹返回起点
// ============================================================
void trackBackToStart() {
  while (true) {
    if (ON_BLACK(PIN_COUNT)) {
      trackGo();
      delay(CROSS_PASS_MS);
      motorsStop();
      delay(100);

      // 确保 IO1 离开黑线，否则下次旋转无法计数
      if (ON_BLACK(PIN_COUNT)) {
        int creep = 0;
        while (ON_BLACK(PIN_COUNT) && creep < 50) {
          rightForward(ROTATE_SPEED / 3);
          leftBackward(ROTATE_SPEED / 3);
          delay(10); creep++;
        }
        motorsStop();
        Serial.printf("顺时针微调 %dms 离开黑线\n", creep * 10);
      }

      delay(200);
      Serial.println("回到起点");
      return;
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
      delay(CROSS_PASS_MS);
      motorsStop();
      delay(100);

      if (ON_BLACK(PIN_COUNT)) {
        int creep = 0;
        while (ON_BLACK(PIN_COUNT) && creep < 50) {
          rightForward(ROTATE_SPEED / 3);
          leftBackward(ROTATE_SPEED / 3);
          delay(10); creep++;
        }
        motorsStop();
      }

      delay(200);
      Serial.println("回到起点");
      return;
    }

    delay(10);
  }
}

// ============================================================
// 执行完整任务
// ============================================================
void doTask(int target, ExpressionId arrivalExpression) {
  busy = true;
  showExpression(EXPRESSION_FOCUS);

  int steps = (target - currentDir + 4) % 4;
  Serial.printf("方向 %d→%d，转%d步\n", currentDir, target, steps);
  rotateSteps(steps);
  alignToLine();

  Serial.println("循迹前进...");
  trackToEnd();

  showExpression(arrivalExpression);
  Serial.println("原地停留 4 秒...");
  delay(EXPRESSION_FEEDBACK_MS);
  showExpression(EXPRESSION_FOCUS);

  Serial.println("掉头...");
  turn180();
  alignToLine();

  Serial.println("返回起点...");
  trackBackToStart();

  currentDir = ((target - 1 + 2) % 4) + 1;

  // 确保 MQTT 连接活跃再发送
  mqtt.loop();
  if (!mqtt.connected()) {
    mqttConnect();
  }

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
  if (mqtt.publish(MQTT_TOPIC_PUB, msg)) {
    Serial.printf("已发送：%s → %s\n", MQTT_TOPIC_PUB, msg);
  } else {
    Serial.println("MQTT 发送失败");
  }

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
void mqttConnect() {
  while (!mqtt.connected()) {
    Serial.print("连接 MQTT...");
    String id = "ESP32_" + String(random(0xffff), HEX);
    if (mqtt.connect(id.c_str())) {
      Serial.println("成功");
      mqtt.subscribe(MQTT_TOPIC_SUB);
    } else {
      Serial.printf("失败 rc=%d\n", mqtt.state());
      delay(5000);
    }
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
  mqttConnect();

  Serial.printf("就绪，当前方向：%d\n", currentDir);
}

// ============================================================
// 主循环
// ============================================================
void loop() {
  if (!mqtt.connected()) { mqttConnect(); }
  mqtt.loop();
  updateExpressionTimeout();

  if (pendingExpression != EXPRESSION_NONE) {
    ExpressionId expression = pendingExpression;
    pendingExpression = EXPRESSION_NONE;
    unsigned long duration = expression == EXPRESSION_STABLE
      ? EXPRESSION_FEEDBACK_MS
      : 0;
    showExpression(expression, duration);
  }

  if (pendingTarget != -1) {
    int target = pendingTarget;
    ExpressionId arrivalExpression = pendingArrivalExpression;
    pendingTarget = -1;
    pendingArrivalExpression = EXPRESSION_REMINDER;
    doTask(target, arrivalExpression);
  }

  delay(10);
}
