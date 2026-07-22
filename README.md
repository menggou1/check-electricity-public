# 河南科技大学宿舍电量查询 & 记录系统

自动查询宿舍剩余电量，记录到 CSV 文件，并绘制耗电柱状图。

## 功能

- **自动查询**：通过 Playwright 浏览器自动化登录电费系统，获取当前剩余电量
- **CSV 记录**：每次查询结果追加到 `electricity_record.csv`，支持同一天覆盖更新
- **充值检测**：自动识别充值行为（消耗电量为负时标记为充值），在 CSV 中记录 `是否充值` 标志
- **柱状图绘制**：基于记录数据绘制耗电柱状图，跳过充值当天，从充值后第一天开始展示

## 文件说明

| 文件 | 说明 | 是否提交 Git |
|------|------|:---:|
| `check_electricity.py` | 主程序 | ✅ |
| `config.example.ini` | 配置模板（需复制为 `config.ini` 并填入实际值） | ✅ |
| `config.ini` | **敏感配置**（学号、密码、宿舍信息） | ❌ 已 `.gitignore` |
| `electricity_record.csv` | 电量记录数据文件 | ✅ |
| `electricity_chart.png` | 输出的耗电柱状图 | ❌ 已 `.gitignore` |

### CSV 格式

```
日期,剩余电量,消耗电量,是否充值
2026-06-15,100.00,,
2026-06-16,97.32,2.68,0
...
2026-07-18,40.63,-24.25,1
2026-07-19,37.63,3.00,0
```

- `日期`：格式 `YYYY-MM-DD`
- `剩余电量`：当前剩余电力度数（精确到两位小数）
- `消耗电量`：相对上一条记录的消耗度数 = 上条剩余 - 本条剩余（精确到两位小数）；首条为空
- `是否充值`：`1`=充值，`0`=未充值。消耗电量为负时自动标记

### 图表特性

- 从最近一次充值之后的下一天开始绘制（充值当天作为"第零条"基线）
- 如果当天充值，则不绘图（提示 `[SKIP]`）
- 数据超过 30 条时自动取最近 30 条
- 柱顶标注精确到两位小数
- X 轴显示日期 + 距上一条记录的天数间隔

## 首次使用 / 配置

> ⚠️ **敏感信息已抽离**：学号、密码、宿舍楼栋等隐私信息保存在 `config.ini` 中，该文件已被 `.gitignore` 排除，绝不会误提交到 Git 仓库。

1. 复制配置模板：
   ```bash
   cp config.example.ini config.ini
   ```
2. 编辑 `config.ini`，填入实际值：

   ```ini
   [auth]
   username = 你的学号
   password = 你的密码

   [building]
   group = 乾园宿舍
   building = 1栋
   room = 1101
   ```

> 详细配置项说明见 `config.example.ini`。

## 运行

```bash
python check_electricity.py
```

### 运行流程

1. 浏览器自动化登录校园 CAS 认证系统
2. 导航至电费查询页面
3. 提取当前剩余电量
4. 追加/覆盖到 CSV 记录
5. 生成耗电柱状图

## 依赖

```bash
pip install pandas matplotlib playwright
playwright install chromium
```

## 注意事项

- 首次运行需要安装 Playwright 浏览器：`playwright install chromium`
- 脚本使用无头浏览器模式（`headless=True`），不会弹出浏览器窗口
- 查询失败时会自动截图保存到 `debug_screenshot.png` / `error_screenshot.png`
- CSV 使用 `utf-8-sig` 编码保存，兼容 Excel 直接打开

## 敏感内容说明

为保护个人隐私：

- **学号、密码、宿舍**等敏感信息已从 `check_electricity.py` 中移除，统一保存在 `config.ini`
- `config.ini` 已加入 `.gitignore`，不会被提交到 Git 仓库
- 提交到仓库的是 `config.example.ini`（示例模板），不包含任何真实凭据
- 在其他机器上使用时，只需复制 `config.example.ini` 为 `config.ini` 并填入自己的信息即可