"""
河南科技大学宿舍剩余电量查询 + CSV记录 + 柱状图绘制

功能：
  1. 登录并查询当前剩余电量
  2. 将结果追加写入 CSV（日期 | 剩余电量 | 消耗电量）
  3. 绘制最近 N 条的耗电柱状图

敏感配置（学号/密码/宿舍信息）已抽离到 config.ini 中，
请复制 config.example.ini 为 config.ini 并根据实际修改。
"""

import configparser
import re
import sys
import os
from datetime import datetime, date
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ── 第三方库 ──
import pandas as pd
import matplotlib
matplotlib.use("Agg")  # 非交互后端，适用于无 GUI 服务器
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.ticker import MaxNLocator
from matplotlib.font_manager import FontProperties
import warnings
warnings.filterwarnings("ignore", message="Glyph.*missing from font")

# ── 中文字体配置 ──
def _get_chinese_font():
    """查找系统可用的中文字体，返回 FontProperties 或 None"""
    # Windows 常见中文字体
    candidates = [
        "Microsoft YaHei",       # 微软雅黑
        "SimHei",                # 黑体
        "SimSun",                # 宋体
        "KaiTi",                 # 楷体
        "FangSong",              # 仿宋
        "DengXian",              # 等线
        "Source Han Sans SC",    # 思源黑体
        "Noto Sans CJK SC",      # Noto
    ]
    for name in candidates:
        try:
            fp = FontProperties(family=name)
            # 验证字体是否存在
            from matplotlib.font_manager import findfont, FontManager
            findfont(fp, fallback_to_default=False)
            return name
        except Exception:
            continue
    return None

CHINESE_FONT = _get_chinese_font()
if CHINESE_FONT:
    plt.rcParams["font.family"] = CHINESE_FONT
else:
    pass
plt.rcParams["axes.unicode_minus"] = False

# ── 控制台编码 ──
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

# ===================== 加载配置 =====================
_BASE = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_BASE, "config.ini")

if not os.path.exists(_CONFIG_PATH):
    print(f"[ERROR] 找不到配置文件: {_CONFIG_PATH}")
    print("请复制 config.example.ini 为 config.ini 并填入实际值。")
    sys.exit(1)

_cfg = configparser.ConfigParser()
_cfg.read(_CONFIG_PATH, encoding="utf-8")

# ── 认证信息 ──
USERNAME = _cfg.get("auth", "username")
PASSWORD = _cfg.get("auth", "password")

# ── 宿舍信息 ──
BUILDING_GROUP = _cfg.get("building", "group")
BUILDING = _cfg.get("building", "building")
ROOM = _cfg.get("building", "room")

# ── 系统参数 ──
TARGET_URL = _cfg.get("system", "target_url",
                      fallback="https://cwpay.haust.edu.cn/xysf/modules/life/ElecRoomInfo.aspx")
PLOT_RECORDS = _cfg.getint("system", "plot_records", fallback=30)

# CSV 文件路径（与脚本同目录）
CSV_PATH = os.path.join(_BASE, "electricity_record.csv")

# 柱状图输出路径
CHART_PATH = os.path.join(_BASE, "electricity_chart.png")
# ====================================================


# ================== 截图辅助 ===================
def safe_screenshot(page, path):
    try:
        page.screenshot(path=path, full_page=True, timeout=10000)
        print(f"  [截图] 已保存: {path}")
    except Exception:
        print("  [截图] 截图失败")


# ================== 电量提取 ===================
def extract_electricity(page):
    """从页面中提取剩余电量，返回 (数值: float) 或 None"""
    body_text = page.locator("body").text_content() or ""

    # 方法1：正则匹配 "剩余电量 XX.XX 度"
    patterns = [
        r"剩余电量\s*(\d+\.?\d*)\s*度",
        r"剩余[^0-9]*?(\d+\.?\d*)\s*度",
        r"余额[^0-9]*?(\d+\.?\d*)\s*(?:度|kWh)",
        r"电量[^0-9]*?(\d+\.?\d*)\s*(?:度|kWh)",
    ]
    for pattern in patterns:
        match = re.search(pattern, body_text)
        if match:
            return float(match.group(1))

    # 方法2：查找包含"剩余电量"的元素
    for selector in [
        "span:has-text('剩余电量')",
        "td:has-text('剩余电量')",
        "div:has-text('剩余电量')",
        "p:has-text('剩余电量')",
    ]:
        elems = page.locator(selector).all()
        for elem in elems:
            text = (elem.text_content() or "").strip()
            m = re.search(r"(\d+\.?\d*)\s*度", text)
            if m:
                return float(m.group(1))

    return None


# ================== 查询电量 ===================
def query_electricity():
    """返回当前剩余电量 (float)，失败返回 None"""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--ignore-certificate-errors", "--disable-web-security"],
        )
        context = browser.new_context(
            ignore_https_errors=True,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.set_default_timeout(30000)

        try:
            # ==== 第1步：访问目标页面 ====
            print("[1/4] 正在访问目标页面...")
            page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
            print(f"  当前页面: {page.url}")

            # ==== 第2步：处理登录 ====
            if "login" in page.url.lower() or "cas" in page.url.lower():
                print("[2/4] 检测到登录页面...")

                # 点击 "校内师生登录"
                campus_btn = page.locator("text=校内师生登录").first
                if campus_btn.is_visible():
                    print("  点击 [校内师生登录]...")
                    try:
                        with page.expect_navigation(
                            timeout=20000, wait_until="domcontentloaded"
                        ):
                            campus_btn.click()
                    except Exception:
                        pass
                    page.wait_for_timeout(3000)
                    print(f"  跳转到: {page.url}")

                # CAS 页面：选择校内登录 tab
                if "cas" in page.url.lower():
                    campus_tab = page.locator("text=校内登录").first
                    if campus_tab.count() > 0 and campus_tab.is_visible():
                        campus_tab.click()
                        page.wait_for_timeout(1000)

                page.wait_for_timeout(2000)

                # 填写用户名
                username_filled = False
                for sel in [
                    "input[placeholder*='学工号']",
                    "input[placeholder*='账号']",
                    "input[placeholder*='学号']",
                    "#txt_yhm",
                ]:
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        loc.fill(USERNAME)
                        username_filled = True
                        break
                if not username_filled:
                    for inp in page.locator("input[type='text']").all():
                        if inp.is_visible():
                            inp.fill(USERNAME)
                            username_filled = True
                            break
                print(f"  已输入用户名: {USERNAME}")

                # 填写密码
                for sel in [
                    "input[placeholder*='密码']",
                    "#txt_pwd",
                    "input[type='password']",
                ]:
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        loc.fill(PASSWORD)
                        break
                print("  已输入密码")

                # 点击登录
                for sel in [
                    "button:has-text('登录')",
                    "#btn_dl",
                    "input[value*='登']",
                ]:
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        try:
                            with page.expect_navigation(
                                timeout=30000, wait_until="domcontentloaded"
                            ):
                                loc.click()
                        except Exception:
                            pass
                        break
                page.wait_for_timeout(3000)
                print(f"  登录后: {page.url}")
            else:
                print("[2/4] 已登录")

            # ==== 第3步：导航到电费页面 ====
            print("[3/4] 导航到电费页面...")
            if "elecroominfo" not in page.url.lower():
                page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(3000)
            print(f"  当前页面: {page.url}")

            # ==== 第4步：读取剩余电量 ====
            print("[4/4] 读取剩余电量...")
            page.wait_for_timeout(2000)

            result = extract_electricity(page)

            if result is None:
                # 需要选择房间
                print("  页面未显示电量，尝试通过 JS 选择房间...")

                page.evaluate(
                    """() => {
                    const area = document.querySelector('#area');
                    if (area) {
                        for (let opt of area.options) {
                            if (opt.text.includes('乾园')) {
                                area.value = opt.value;
                                area.dispatchEvent(new Event('change', {bubbles: true}));
                                break;
                            }
                        }
                    }
                }"""
                )
                page.wait_for_timeout(3000)

                page.evaluate(
                    """() => {
                    const build = document.querySelector('#build');
                    if (build) {
                        for (let opt of build.options) {
                            if (opt.text.includes('6')) {
                                build.value = opt.value;
                                build.dispatchEvent(new Event('change', {bubbles: true}));
                                break;
                            }
                        }
                    }
                }"""
                )
                page.wait_for_timeout(3000)

                page.evaluate(
                    """() => {
                    const room = document.querySelector('#room');
                    if (room) {
                        for (let opt of room.options) {
                            if (opt.text.includes('6309')) {
                                room.value = opt.value;
                                room.dispatchEvent(new Event('change', {bubbles: true}));
                                break;
                            }
                        }
                    }
                }"""
                )
                page.wait_for_timeout(3000)

                # 点击查询按钮
                for sel in [
                    "input[value*='查询']",
                    "button:has-text('查询')",
                    "input[type='button']",
                ]:
                    btn = page.locator(sel).first
                    if btn.count() > 0:
                        try:
                            btn.click(force=True)
                        except Exception:
                            page.evaluate(
                                "document.querySelector('"
                                + sel.split(",")[0]
                                + "')?.click()"
                            )
                        page.wait_for_timeout(3000)
                        break

                result = extract_electricity(page)

            if result is not None:
                print(f"\n  ✅ 房间: {BUILDING_GROUP} {BUILDING} {ROOM}")
                print(f"     剩余电量: {result} 度")
            else:
                print("\n  ❌ 未能提取剩余电量")
                body = page.locator("body").text_content() or ""
                print(
                    "  页面内容:",
                    re.sub(r"\s+", " ", body).strip()[:1000],
                )
                safe_screenshot(page, os.path.join(os.path.dirname(__file__), "debug_screenshot.png"))

            return result

        except Exception as e:
            print(f"\n[ERROR] {e}")
            import traceback

            traceback.print_exc()
            safe_screenshot(page, os.path.join(os.path.dirname(__file__), "error_screenshot.png"))
            return None

        finally:
            print("\n浏览器将在 3 秒后关闭...")
            page.wait_for_timeout(3000)
            browser.close()


# ================== CSV 读写 ===================
def load_records(csv_path):
    """读取已有 CSV 记录，返回 DataFrame（空表则返回空 DataFrame）"""
    if not os.path.exists(csv_path):
        return pd.DataFrame(columns=["日期", "剩余电量", "消耗电量", "是否充值"])
    try:
        df = pd.read_csv(csv_path, encoding="utf-8-sig")
        # 统一列名
        expected_cols = ["日期", "剩余电量", "消耗电量", "是否充值"]
        if len(df.columns) == 3:
            # 旧格式：无 是否充值 列
            df.columns = ["日期", "剩余电量", "消耗电量"]
            df["是否充值"] = 0
        else:
            df.columns = expected_cols[: len(df.columns)]
            if "是否充值" not in df.columns:
                df["是否充值"] = 0
        # 日期列转 datetime
        df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
        df["剩余电量"] = pd.to_numeric(df["剩余电量"], errors="coerce")
        df["消耗电量"] = pd.to_numeric(df["消耗电量"], errors="coerce")
        df["是否充值"] = pd.to_numeric(df["是否充值"], errors="coerce").fillna(0).astype(int)
        return df
    except Exception as e:
        print(f"  [WARN] 读取 CSV 出错: {e}，将重新创建")
        return pd.DataFrame(columns=["日期", "剩余电量", "消耗电量", "是否充值"])


def append_record(csv_path, df, today_str, remaining):
    """追加或覆盖当天记录到 DataFrame，并写回 CSV"""
    # 检查当天是否已有记录
    today_dt = pd.to_datetime(today_str)
    mask = df["日期"] == today_dt
    if mask.any():
        # 已有当天记录 → 覆盖
        idx = df.index[mask][0]
        old_consumption = df.loc[idx, "消耗电量"]
        old_recharge = df.loc[idx, "是否充值"]

        # 重新计算消耗电量：用前一条剩余电量 - 本次剩余电量
        if idx > 0:
            prev_remaining = df.iloc[idx - 1]["剩余电量"]
            if pd.notna(prev_remaining):
                consumption = round(prev_remaining - remaining, 2)
            else:
                consumption = None
        else:
            consumption = None

        df.loc[idx, "剩余电量"] = round(remaining, 2)
        df.loc[idx, "消耗电量"] = consumption

        # 充值检测逻辑
        recharge = old_recharge  # 默认保持原值
        if consumption is not None and pd.notna(consumption):
            if consumption < 0:
                # 消耗为负 → 发生了充值
                recharge = 1
            else:
                # 消耗为正
                # 如果旧值标记为充值(1)且旧消耗为负（说明之前是充值状态）
                # 现在消耗变正 → 只是正常消耗，不改变充值变量
                if not (old_recharge == 1 and pd.notna(old_consumption) and old_consumption < 0):
                    recharge = 0
        df.loc[idx, "是否充值"] = recharge

        print(f"\n  ✅ 已覆盖记录: {today_str}, 剩余 {remaining} 度", end="")
        if consumption is not None:
            print(f", 消耗 {consumption} 度", end="")
        else:
            print(" (首条记录，无消耗数据)", end="")
        if recharge:
            print(" [充值]")
        else:
            print()
    else:
        # 无当天记录 → 新增
        if len(df) >= 1:
            prev_remaining = df.iloc[-1]["剩余电量"]
            if pd.notna(prev_remaining):
                consumption = round(prev_remaining - remaining, 2)
            else:
                consumption = None
        else:
            consumption = None

        # 充值检测：消耗为负就是充值
        recharge = 1 if (consumption is not None and consumption < 0) else 0

        new_row = pd.DataFrame(
            [
                {
                    "日期": today_str,
                    "剩余电量": round(remaining, 2),
                    "消耗电量": consumption,
                    "是否充值": recharge,
                }
            ]
        )
        df = pd.concat([df, new_row], ignore_index=True)
        print(f"\n  ✅ 已写入记录: {today_str}, 剩余 {remaining} 度", end="")
        if consumption is not None:
            print(f", 消耗 {consumption} 度", end="")
        else:
            print(" (首条记录，无消耗数据)", end="")
        if recharge:
            print(" [充值]")
        else:
            print()

    # 写回 CSV（日期以短格式写入）
    df_out = df.copy()
    # 若日期列是 datetime 则转为短字符串 YYYY-MM-DD
    if pd.api.types.is_datetime64_any_dtype(df_out["日期"]):
        df_out["日期"] = df_out["日期"].dt.strftime("%Y-%m-%d")
    # 消耗电量写入时若为 NaN 则为空，但保留列
    df_out.to_csv(csv_path, index=False, encoding="utf-8-sig", float_format="%.2f", na_rep="")
    # 确保内存中 df 的日期是 datetime 类型
    df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
    return df


# ================== 柱状图绘制 ===================
def plot_consumption(df, chart_path):
    """
    绘制耗电柱状图，跳过充值当天（第零条）。
    逻辑：
      - 丢弃消耗电量为空的行（首条无消耗）
      - 从最近一次充值之后的下一条开始绘图
      - 如果最近一次充值在当天 → 无法绘图
      - 如果充值之后数据很多 → 仍然只绘制最近 PLOT_RECORDS 条
    """
    # 丢弃消耗电量为空的行
    valid = df.dropna(subset=["消耗电量"]).copy()
    if len(valid) < 1:
        print("  [SKIP] 不足 2 条有效记录，跳过绘图")
        return

    # 找到最近一次充值的位置（是否充值 == 1）
    recharge_mask = valid["是否充值"] == 1
    if recharge_mask.any():
        last_recharge_idx = valid[recharge_mask].index[-1]  # valid 中的原始索引
        last_recharge_pos = valid.index.get_loc(last_recharge_idx)  # 在 valid 中的位置

        # 如果最近一次充值就在 valid 的最后一条（包含今天）
        if last_recharge_pos == len(valid) - 1:
            print("  [SKIP] 最近一次充值为当天记录，无法绘图")
            return

        # 从充值之后的下一条开始
        plot_data = valid.iloc[last_recharge_pos + 1:].reset_index(drop=True)
        print(f"  [充值] 最近充值位置: {valid.iloc[last_recharge_pos]['日期'].strftime('%Y-%m-%d')}，从其后开始绘图")
    else:
        # 没有充值记录，从第1条开始（第0条作为基线）
        if len(valid) < 2:
            print("  [SKIP] 无充值记录且不足 2 条有效数据，跳过绘图")
            return
        plot_data = valid.iloc[1:].reset_index(drop=True)
        print("  [充值] 无充值记录")

    # 如果数据过多，取最近 PLOT_RECORDS 条
    if len(plot_data) > PLOT_RECORDS:
        plot_data = plot_data.tail(PLOT_RECORDS).reset_index(drop=True)
        print(f"  [绘图] 数据较多，取最近 {PLOT_RECORDS} 条")

    n = len(plot_data)
    if n < 1:
        print("  [SKIP] 无足够数据绘图")
        return
    print(f"  [绘图] 使用 {n} 条记录绘制柱状图")

    # ── 构建图表 ──
    fig, ax = plt.subplots(figsize=(max(14, n * 0.7), 6))

    # 柱状图：X 轴是记录索引，Y 轴是消耗电量
    x = range(n)
    values = plot_data["消耗电量"].values

    bars = ax.bar(
        x,
        values,
        width=0.6,
        color="#4A90D9",
        edgecolor="#2C5F8A",
        linewidth=0.8,
        alpha=0.85,
    )

    # 在每个柱上方标注数值
    for i, v in enumerate(values):
        if pd.notna(v):
            ax.text(
                i,
                v + (max(values) * 0.02 if max(values) > 0 else 0.5),
                f"{v:.2f}",
                ha="center",
                va="bottom",
                fontsize=7,
                fontweight="bold",
            )

    # ── X 轴标签：当前日期 + 间隔天数 ──
    def fmt(d):
        if pd.isna(d):
            return "?"
        if isinstance(d, pd.Timestamp):
            return d.strftime("%Y-%m-%d")
        return str(d)

    labels = []
    for i in range(n):
        curr_date = plot_data.iloc[i]["日期"]
        curr_str = fmt(curr_date)

        # 在原始 df 中的位置
        same_day_mask = df["日期"] == curr_date
        if same_day_mask.any():
            pos_in_df = same_day_mask.idxmax() + same_day_mask.sum() - 1
        else:
            pos_in_df = df["日期"].searchsorted(curr_date, side="left")

        if pos_in_df > 0:
            prev_date = df.iloc[pos_in_df - 1]["日期"]
            if isinstance(curr_date, pd.Timestamp) and isinstance(prev_date, pd.Timestamp):
                days_diff = (curr_date - prev_date).days
                label = f"{curr_str}\n{days_diff}天"
            else:
                label = curr_str
        else:
            label = curr_str
        labels.append(label)

    # ── X 轴标签旋转防重叠 ──
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=7, rotation=30, ha="right")

    # ── 标题和轴标签 ──
    first_date = plot_data.iloc[0]["日期"]
    last_date = plot_data.iloc[-1]["日期"]
    fs = fmt(first_date) if not pd.isna(first_date) else "?"
    ls = fmt(last_date) if not pd.isna(last_date) else "?"
    date_range_str = f"{fs} ~ {ls}"
    ax.set_title(
        f"{BUILDING_GROUP} {BUILDING} {ROOM} 耗电图（{date_range_str}）",
        fontsize=13,
        fontweight="bold",
        pad=12,
    )
    ax.set_ylabel("消耗电量（度）", fontsize=10)
    ax.set_xlabel("日期间隔", fontsize=10)

    # Y 轴从 0 开始
    ax.set_ylim(bottom=0)

    # 网格线
    ax.yaxis.grid(True, linestyle="--", alpha=0.3)
    ax.set_axisbelow(True)

    # ── 保存 ──
    plt.tight_layout()
    fig.savefig(chart_path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"  ✅ 柱状图已保存: {chart_path}")


# ================== 主流程 ===================
def main():
    print("=" * 55)
    print("  河南科技大学宿舍电量查询 & 记录系统")
    print(f"  {BUILDING_GROUP} {BUILDING} {ROOM}")
    today_str = date.today().strftime("%Y-%m-%d")
    print(f"  查询日期: {today_str}")
    print("=" * 55)

    # 1. 查询电量
    remaining = query_electricity()
    if remaining is None:
        print("\n❌ 查询电量失败，程序退出")
        sys.exit(1)

    # 2. 加载已有记录
    print("\n" + "-" * 45)
    print("  记录处理:")
    df = load_records(CSV_PATH)
    print(f"  已有记录数: {len(df)}")

    # 3. 追加新记录
    df = append_record(CSV_PATH, df, today_str, remaining)

    # 4. 绘图
    print("\n" + "-" * 45)
    print("  绘图:")
    plot_consumption(df, CHART_PATH)

    print("\n" + "=" * 55)
    print("  全部完成！")
    print("=" * 55)


if __name__ == "__main__":
    main()