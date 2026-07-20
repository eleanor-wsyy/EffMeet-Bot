// ============================================================
// TFT_eSPI User_Setup.h — 3.5" TFT Shield MCUFriend 并口
// 适用于: ILI9341 / ILI9486 / ILI9488 等 8-bit 并口屏
// 用法: 替换 Arduino/libraries/TFT_eSPI/User_Setup.h
// ============================================================

// --- 驱动选择（依次只开一个，不行的换另一个）---
#define ILI9488_DRIVER       // 先试这个，3.5"屏常见
// #define ILI9486_DRIVER    // 如果不行换这个
// #define ILI9341_DRIVER    // 或这个

// --- ESP32-S3 引脚定义 ---
#define TFT_CS   19          // A3 → IO19
#define TFT_DC   2           // A2 → IO2
#define TFT_RST  33          // A4 → IO33
#define TFT_WR   3           // A1 → IO3
// TFT_RD 接 3.3V，不配置

// --- 8 位数据口 ---
#define TFT_D0   8           // D8 孔
#define TFT_D1   9           // D9 孔
#define TFT_D2   10          // D2 孔
#define TFT_D3   11          // D3 孔
#define TFT_D4   12          // D4 孔
#define TFT_D5   16          // D5 孔
#define TFT_D6   17          // D6 孔
#define TFT_D7   18          // D7 孔

// --- 屏幕尺寸 ---
#define TFT_WIDTH  480
#define TFT_HEIGHT 320

// --- 字体 ---
#define SMOOTH_FONT

// --- SPI 频率 ---
#define SPI_FREQUENCY  40000000
#define SPI_READ_FREQUENCY  20000000
