// ============================================================
// 接线参考（当前固件不使用 TFT_eSPI）
// ============================================================
//
// robot_esp32/1.3/1.3.ino 使用 Arduino_GFX_Library，并在 ino 文件中
// 直接创建 Arduino_SWPAR8 和 Arduino_ILI9488。无需替换任何 Arduino
// 库目录中的 User_Setup.h。
//
// 下面的定义只用于集中记录现场接线，必须与 1.3.ino 保持一致：

#define TFT_DC   36
#define TFT_CS   35
#define TFT_WR   37
#define TFT_RD   38
#define TFT_RST  42

#define TFT_D0    2
#define TFT_D1    3
#define TFT_D2    8
#define TFT_D3    9
#define TFT_D4   10
#define TFT_D5   11
#define TFT_D6   12
#define TFT_D7   18

#define TFT_WIDTH  480
#define TFT_HEIGHT 320

// 特别注意：旧说明曾把 RST 写成 IO33，与实际固件的 IO42 不一致。
// 当前版本统一使用 IO42。若现场硬件仍接在 IO33，请先改线到 IO42，
// 不要同时修改两份配置。
