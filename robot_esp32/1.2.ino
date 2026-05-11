// ============================================================
// 会议干预机器人 v1.3
// 4人逆时针排列：1(上) → 2(左) → 3(下) → 4(右)
// 旋转方向：逆时针（左轮前进 + 右轮后退）
// 回起点后通过 MQTT 发送 done|dir=X
// ============================================================

#include <WiFi.h>
#include <PubSubClient.h>
#include <ESP32Servo.h>

// ============================================================
// 可调参数
// ============================================================
#define TRACK_SPEED     230
#define ROTATE_SPEED    150
#define SERVO_ANGLE     90      // 舵机摆动角度
#define SERVO_STEP_MS   15      // 舵机每步延时（越小越慢）
#define TURN180_TIME    900     // 掉头180°定时（毫秒）

// ============================================================
// WiFi
// ============================================================
#define WIFI_SSID       "Redmi K70"
#define WIFI_PASSWORD   "20060325xck"

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
#define PIN_FL    4
#define PIN_ML    5
#define PIN_MID   6
#define PIN_MR    7
#define PIN_FR    15
#define PIN_COUNT 1
#define PIN_SERVO 19

#define IN1   21
#define IN2   47
#define IN3   14
#define IN4   13
#define ENA   40
#define ENB   41

// ============================================================
// 传感器逻辑：黑线 = HIGH
// ============================================================
#define ON_BLACK(pin) (digitalRead(pin) == HIGH)

// ============================================================
// 全局状态
// ============================================================
WiFiClient   espClient;
PubSubClient mqtt(espClient);
Servo        servo;

int  currentDir = 1;
bool busy       = false;
int  pendingCmd = -1;

// ============================================================
// 电机驱动
// ============================================================
void rightForward(int spd) {
  digitalWrite(IN1, HIGH); digitalWrite(IN2, LOW);
  ledcWrite(0, spd);
}
void rightBackward(int spd) {
  digitalWrite(IN1, LOW); digitalWrite(IN2, HIGH);
  ledcWrite(0, spd);
}
void rightStop() {
  digitalWrite(IN1, LOW); digitalWrite(IN2, LOW);
  ledcWrite(0, 0);
}

void leftForward(int spd) {
  digitalWrite(IN3, HIGH); digitalWrite(IN4, LOW);
  ledcWrite(1, spd);
}
void leftBackward(int spd) {
  digitalWrite(IN3, LOW); digitalWrite(IN4, HIGH);
  ledcWrite(1, spd);
}
void leftStop() {
  digitalWrite(IN3, LOW); digitalWrite(IN4, LOW);
  ledcWrite(1, 0);
}

void motorsStop() { leftStop(); rightStop(); }

// ============================================================
// 原地差速逆时针旋转 steps × 90°
// 逆时针：左轮前进 + 右轮后退
// 方向对应：1(上/0°) → 2(左/90°CCW) → 3(下/180°) → 4(右/270°CCW)
// ============================================================
void rotateSteps(int steps) {
  if (steps == 0) return;

  int count = 0;
  bool prev = digitalRead(PIN_COUNT);

  leftForward(ROTATE_SPEED);   // 逆时针：左轮向前
  rightBackward(ROTATE_SPEED); // 逆时针：右轮向后

  while (count < steps) {
    bool now = digitalRead(PIN_COUNT);
    if (!prev && now) count++;
    prev = now;
    delay(5);
  }
  motorsStop();
  delay(300);
}

// ============================================================
// 旋转后用 5 路传感器微调对线
// 确保中间传感器对准黑线再开始循迹
// ============================================================
void alignToLine() {
  int mid = digitalRead(PIN_MID);
  int ml  = digitalRead(PIN_ML);
  int mr  = digitalRead(PIN_MR);
  int fl  = digitalRead(PIN_FL);
  int fr  = digitalRead(PIN_FR);

  // 中间已对准，无需校准
  if (mid == HIGH) return;

  // 左侧传感器看到线 → 微左转
  if (fl == HIGH || ml == HIGH) {
    while (digitalRead(PIN_MID) != HIGH) {
      rightForward(ROTATE_SPEED / 2);
      leftBackward(ROTATE_SPEED / 2);
      delay(5);
    }
  }
  // 右侧传感器看到线 → 微右转
  else if (fr == HIGH || mr == HIGH) {
    while (digitalRead(PIN_MID) != HIGH) {
      leftForward(ROTATE_SPEED / 2);
      rightBackward(ROTATE_SPEED / 2);
      delay(5);
    }
  }
  // 全白 → 慢速左转扫描找线
  else {
    unsigned long start = millis();
    rightForward(ROTATE_SPEED / 2);
    leftBackward(ROTATE_SPEED / 2);
    while (millis() - start < 2000) {
      if (digitalRead(PIN_MID) == HIGH ||
          digitalRead(PIN_ML) == HIGH ||
          digitalRead(PIN_MR) == HIGH) {
        break;
      }
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
void trackLeft1() {
  leftStop();
  rightForward(TRACK_SPEED);
}
void trackLeft2() {
  leftBackward(TRACK_SPEED);
  rightForward(TRACK_SPEED);
}
void trackRight1() {
  leftForward(TRACK_SPEED);
  rightStop();
}
void trackRight2() {
  leftForward(TRACK_SPEED);
  rightBackward(TRACK_SPEED);
}

// ============================================================
// 循迹前进至终点（终点 = 五路全黑）
// ============================================================
void trackToEnd() {
  while (true) {
    int fl  = digitalRead(PIN_FL);
    int ml  = digitalRead(PIN_ML);
    int mid = digitalRead(PIN_MID);
    int mr  = digitalRead(PIN_MR);
    int fr  = digitalRead(PIN_FR);

    if (fl == HIGH && ml == HIGH && mid == HIGH && mr == HIGH && fr == HIGH) {
      motorsStop();
      Serial.println("到达终点");
      return;
    }

    if (mid == HIGH) {
      trackGo();
    } else if (fl == HIGH) {
      trackLeft2();
    } else if (fr == HIGH) {
      trackRight2();
    } else if (ml == HIGH) {
      trackLeft1();
    } else if (mr == HIGH) {
      trackRight1();
    }

    delay(10);
  }
}

// ============================================================
// 舵机缓慢来回摆动（逐度步进，可调 SERVO_STEP_MS 控制快慢）
// ============================================================
void servoWave(int angle) {
  int mid = 90;
  int step = 1;

  // 中位 → 左端
  for (int pos = mid; pos >= mid - angle; pos -= step) {
    servo.write(pos);
    delay(SERVO_STEP_MS);
  }
  // 左端 → 右端
  for (int pos = mid - angle; pos <= mid + angle; pos += step) {
    servo.write(pos);
    delay(SERVO_STEP_MS);
  }
  // 右端 → 中位
  for (int pos = mid + angle; pos >= mid; pos -= step) {
    servo.write(pos);
    delay(SERVO_STEP_MS);
  }
  servo.write(mid);
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
// 循迹返回起点（IO1 检测中心十字）
// ============================================================
void trackBackToStart() {
  while (true) {
    if (ON_BLACK(PIN_COUNT)) {
      trackGo();
      delay(300);
      motorsStop();
      delay(200);
      Serial.println("回到起点");
      return;
    }

    int fl  = digitalRead(PIN_FL);
    int ml  = digitalRead(PIN_ML);
    int mid = digitalRead(PIN_MID);
    int mr  = digitalRead(PIN_MR);
    int fr  = digitalRead(PIN_FR);

    if (mid == HIGH) {
      trackGo();
    } else if (fl == HIGH) {
      trackLeft2();
    } else if (fr == HIGH) {
      trackRight2();
    } else if (ml == HIGH) {
      trackLeft1();
    } else if (mr == HIGH) {
      trackRight1();
    }

    delay(10);
  }
}

// ============================================================
// 执行完整任务
// ============================================================
void doTask(int target) {
  busy = true;

  int steps = (target - currentDir + 4) % 4;
  Serial.printf("方向 %d→%d，转%d步\n", currentDir, target, steps);
  rotateSteps(steps);
  alignToLine();

  Serial.println("循迹前进...");
  trackToEnd();

  Serial.println("舵机摆动...");
  servoWave(SERVO_ANGLE);

  Serial.println("掉头...");
  turn180();
  alignToLine();

  Serial.println("返回起点...");
  trackBackToStart();

  currentDir = ((target - 1 + 2) % 4) + 1;

  // 向电脑端发送完成消息
  char msg[32];
  snprintf(msg, sizeof(msg), "%s|dir=%d", MQTT_MSG_DONE, currentDir);
  mqtt.publish(MQTT_TOPIC_PUB, msg);
  Serial.printf("已发送：%s → %s\n", MQTT_TOPIC_PUB, msg);

  busy = false;
  Serial.printf("任务完成，当前方向：%d\n", currentDir);
}

// ============================================================
// MQTT 回调
// ============================================================
void mqttCallback(char* topic, byte* payload, unsigned int len) {
  if (len == 0) return;
  char buf[4] = {0};
  memcpy(buf, payload, len < 3 ? len : 3);
  int cmd = atoi(buf);

  if (cmd < 1 || cmd > 4) { return; }
  if (busy) {
    Serial.printf("[MQTT] 忙，忽略：%d\n", cmd);
    return;
  }

  Serial.printf("[MQTT] 收到：%d\n", cmd);
  pendingCmd = cmd;
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
  Serial.println("\n===== v1.3 逆时针4人干预 + MQTT 回复 =====");

  pinMode(PIN_FL,    INPUT);
  pinMode(PIN_ML,    INPUT);
  pinMode(PIN_MID,   INPUT);
  pinMode(PIN_MR,    INPUT);
  pinMode(PIN_FR,    INPUT);
  pinMode(PIN_COUNT, INPUT);
  pinMode(IN1, OUTPUT);
  pinMode(IN2, OUTPUT);
  pinMode(IN3, OUTPUT);
  pinMode(IN4, OUTPUT);

  ledcAttachChannel(ENA, 5000, 8, 0);
  ledcAttachChannel(ENB, 5000, 8, 1);

  servo.attach(PIN_SERVO);
  servo.write(90);

  motorsStop();

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print("."); }
  Serial.printf("\nWiFi OK, IP: %s\n", WiFi.localIP().toString().c_str());

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

  if (pendingCmd != -1) {
    int cmd = pendingCmd;
    pendingCmd = -1;
    doTask(cmd);
  }

  delay(10);
}
