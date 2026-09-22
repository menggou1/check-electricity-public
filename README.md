# 河南科技大学宿舍电量查询与记录系统

使用 Playwright 查询宿舍剩余电量，将快照保存为 CSV，并生成区间净耗电柱状图。

## 功能

- 通过 Playwright 登录电费系统并查询当前剩余电量
- 每个日期只保留最后一次成功查询的快照
- 清理重复或无效记录，并重新计算派生列
- 根据相邻有效快照生成区间净耗电柱状图

## 文件说明

| 文件 | 说明 |
|------|------|
| `check_electricity.py` | 查询、记录和绘图程序 |
| `config.example.ini` | 配置模板（需复制为`config.ini`并填入实际值） |
| `config.ini` | 本地配置文件，包含认证和宿舍信息 |
| `electricity_record.csv` | 有效电量记录，由程序生成 |
| `electricity_record.invalid.csv` | 被隔离的无效历史记录，由程序按需生成 |
| `electricity_chart.png` | 耗电柱状图，由程序生成 |

CSV 和 PNG 文件均为运行时数据或输出文件，不纳入版本控制。

### CSV 格式

```
日期,更新时间,剩余电量,消耗电量,间隔天数
2026-06-15,2026-06-15 20:30:12,100.00,,
2026-06-16,2026-06-16 20:28:03,97.32,2.68,1
...
2026-06-19,2026-06-19 20:25:11,90.63,6.69,3
```

- `日期`：格式 `YYYY-MM-DD`
- `更新时间`：该日最后一次成功查询的时间（Asia/Shanghai）
- `剩余电量`：当前剩余电力度数（精确到两位小数）
- `消耗电量`：相对上一有效记录的区间净耗电 = 上条剩余 - 本条剩余（精确到两位小数）；首条为空。负值表示余额增加，程序不判断具体原因。
- `间隔天数`：本条日期与上一有效记录日期的自然日间隔；首条为空。

`日期` 是唯一键。当天第二次及之后的查询会覆盖当天旧快照，不会新增行；每次写入后，程序都会对全部日期重新计算 `消耗电量` 和 `间隔天数`。

旧版含 `是否充值` 列的 CSV 可以直接读取，程序会移除该列并迁移到当前结构。日期为空、日期无法解析或剩余电量无效的历史行会保存到 `electricity_record.invalid.csv`，供人工核对。

### 图表特性

- 第一条记录只作为基线；从第二条起，每根柱表示“上一条记录至当前记录”的完整区间
- 正值表示该区间余额减少，负值表示余额增加
- 数据超过 `plot_records` 个区间时自动取最近的 `plot_records` 个区间，默认值为 30
- 柱顶标注精确到两位小数
- X 轴显示区间终点日期 + 距上一条记录的天数间隔

## 配置

1. 复制配置模板：
   ```bash
   cp config.example.ini config.ini
   ```
2. 编辑 `config.ini`，填写认证信息、园区、楼栋和房间号：

   ```ini
   [auth]
   username = 你的学号
   password = 你的密码

   [building]
   group = 乾园宿舍
   building = 1栋
   room = 1101
   ```

配置项说明见 `config.example.ini`。

## 运行

```bash
python check_electricity.py
```

### 运行流程

1. 浏览器自动化登录校园 CAS 认证系统
2. 导航至电费查询页面
3. 提取当前剩余电量
4. 清洗历史记录，并覆盖/写入当天唯一 CSV 快照
5. 生成耗电柱状图

记录处理期间会创建临时锁文件，以避免并发读写。写入 CSV 时先生成临时文件，再原子替换正式文件。

## 依赖

```bash
pip install pandas matplotlib playwright
playwright install chromium
```

## 注意事项

- 首次运行需要安装 Playwright 浏览器：`playwright install chromium`
- 脚本使用无头浏览器模式（`headless=True`），不会弹出浏览器窗口
- 查询失败时会保存 `debug_screenshot.png` 或 `error_screenshot.png`
- CSV 使用 `utf-8-sig` 编码，兼容 Excel 直接打开
