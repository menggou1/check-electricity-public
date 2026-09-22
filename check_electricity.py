"""
河南科技大学宿舍剩余电量查询 + CSV记录 + 柱状图绘制

功能：
  1. 登录并查询当前剩余电量
  2. 每天仅保留一条最新快照，并重算 CSV 派生数据
  3. 绘制最近 N 个记录区间的净耗电柱状图

敏感配置（学号/密码/宿舍信息）已抽离到 config.ini 中，
请复制 config.example.ini 为 config.ini 并根据实际修改。
"""

import configparser
import re
import sys
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

# ── 第三方库 ──
import pandas as pd
import matplotlib
matplotlib.use("Agg")  # 非交互后端，适用于无 GUI 服务器
import matplotlib.pyplot as plt
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

# CSV 的唯一正式格式。消耗电量和间隔天数均由“日期、更新时间、剩余电量”重算，
# 不能作为手工维护的原始数据。
RECORD_COLUMNS = ["日期", "更新时间", "剩余电量", "消耗电量", "间隔天数"]
RAW_COLUMNS = ["日期", "更新时间", "剩余电量"]
INVALID_RECORD_PATH = os.path.join(_BASE, "electricity_record.invalid.csv")

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
                    """({selector, text}) => {
                    const area = document.querySelector(selector);
                    if (area) {
                        for (let opt of area.options) {
                            if (opt.text.includes(text)) {
                                area.value = opt.value;
                                area.dispatchEvent(new Event('change', {bubbles: true}));
                                break;
                            }
                        }
                    }
                    }""",
                    {"selector": "#area", "text": BUILDING_GROUP},
                )
                page.wait_for_timeout(3000)

                page.evaluate(
                    """({selector, text}) => {
                    const build = document.querySelector(selector);
                    if (build) {
                        for (let opt of build.options) {
                            if (opt.text.includes(text)) {
                                build.value = opt.value;
                                build.dispatchEvent(new Event('change', {bubbles: true}));
                                break;
                            }
                        }
                    }
                    }""",
                    {"selector": "#build", "text": BUILDING},
                )
                page.wait_for_timeout(3000)

                page.evaluate(
                    """({selector, text}) => {
                    const room = document.querySelector(selector);
                    if (room) {
                        for (let opt of room.options) {
                            if (opt.text.includes(text)) {
                                room.value = opt.value;
                                room.dispatchEvent(new Event('change', {bubbles: true}));
                                break;
                            }
                        }
                    }
                    }""",
                    {"selector": "#room", "text": ROOM},
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
def empty_records():
    """返回符合正式结构的空记录表。"""
    return pd.DataFrame(columns=RECORD_COLUMNS)


@contextmanager
def record_file_lock(csv_path, timeout_seconds=15):
    """在读取、清洗和写回期间持有跨进程文件锁，防止并发运行互相覆盖。"""
    lock_path = f"{csv_path}.lock"
    lock_file = open(lock_path, "a+b")
    if os.path.getsize(lock_path) == 0:
        lock_file.write(b"0")
        lock_file.flush()

    deadline = time.monotonic() + timeout_seconds
    locked = False
    try:
        while not locked:
            try:
                lock_file.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("记录文件正被另一个查询任务使用，请稍后重试")
                time.sleep(0.2)
        yield
    finally:
        if locked:
            try:
                lock_file.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        lock_file.close()


def _parse_datetimes(values):
    """逐项解析日期，兼容旧 CSV 中混合的日期和时间格式。"""
    return values.map(
        lambda value: pd.to_datetime(value, errors="coerce")
        if str(value).strip()
        else pd.NaT
    )


def _write_invalid_rows(raw, original, invalid_mask):
    """隔离无法可靠入库的行，绝不为它们猜测日期。"""
    if not invalid_mask.any():
        return

    # 隔离文件保留原始文本，便于人工找回日期或读数；原因则依据解析后的数据生成。
    invalid = original.loc[invalid_mask].copy()
    invalid["原因"] = ""
    invalid.loc[raw.loc[invalid_mask, "日期"].isna(), "原因"] += "日期无效或为空;"
    invalid.loc[raw.loc[invalid_mask, "剩余电量"].isna(), "原因"] += "剩余电量无效或为空;"
    invalid["隔离时间"] = datetime.now(ZoneInfo("Asia/Shanghai")).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    write_header = not os.path.exists(INVALID_RECORD_PATH)
    invalid.to_csv(
        INVALID_RECORD_PATH,
        mode="a",
        index=False,
        encoding="utf-8-sig",
        header=write_header,
        na_rep="",
    )
    print(f"  [WARN] 已隔离 {len(invalid)} 条日期或剩余电量无效的记录: {INVALID_RECORD_PATH}")


def recompute_derived_columns(df):
    """按日期顺序重算所有派生列，确保每一段都基于前一有效快照。"""
    if df.empty:
        return empty_records()

    result = df[RAW_COLUMNS].copy().sort_values("日期").reset_index(drop=True)
    result["日期"] = pd.to_datetime(result["日期"], errors="raise").dt.normalize()
    result["更新时间"] = pd.to_datetime(result["更新时间"], errors="raise")
    result["剩余电量"] = result["剩余电量"].round(2)
    result["消耗电量"] = (result["剩余电量"].shift(1) - result["剩余电量"]).round(2)
    result["间隔天数"] = result["日期"].diff().dt.days.astype("Int64")
    return result[RECORD_COLUMNS]


def load_records(csv_path):
    """读取、校验、清洗已有记录；无法识别的表结构会中止本次写入。"""
    if not os.path.exists(csv_path):
        return empty_records()
    try:
        source = pd.read_csv(csv_path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    except pd.errors.EmptyDataError as exc:
        raise ValueError("CSV 是空文件，已停止写入以防覆盖历史数据") from exc

    source.columns = [str(column).strip() for column in source.columns]
    columns = list(source.columns)
    legacy_columns = ["日期", "剩余电量", "消耗电量"]
    legacy_recharge_columns = ["日期", "剩余电量", "消耗电量", "是否充值"]
    if columns == RECORD_COLUMNS:
        raw = source[RAW_COLUMNS].copy()
    elif columns == legacy_columns or columns == legacy_recharge_columns:
        # 迁移旧格式：历史“是否充值”字段被有意丢弃，消耗电量会统一重算。
        raw = source[["日期", "剩余电量"]].copy()
        raw["更新时间"] = raw["日期"]
        raw = raw[RAW_COLUMNS]
        print("  [迁移] 已读取旧 CSV 格式，将移除充值字段并重算派生数据")
    else:
        raise ValueError(
            f"CSV 列名不受支持: {columns!r}。请修复文件后再运行，原文件未被修改。"
        )

    raw_text = raw.copy()
    raw["日期"] = _parse_datetimes(raw["日期"]).dt.normalize()
    raw["更新时间"] = _parse_datetimes(raw["更新时间"])
    raw["更新时间"] = raw["更新时间"].where(raw["更新时间"].notna(), raw["日期"])
    raw["剩余电量"] = pd.to_numeric(raw["剩余电量"], errors="coerce")

    invalid_mask = raw["日期"].isna() | raw["剩余电量"].isna()
    _write_invalid_rows(raw, raw_text, invalid_mask)
    valid = raw.loc[~invalid_mask].copy()
    if valid.empty:
        return empty_records()

    valid["_source_order"] = range(len(valid))
    valid = valid.sort_values(["日期", "更新时间", "_source_order"])
    duplicate_count = valid.duplicated("日期", keep="last").sum()
    if duplicate_count:
        print(f"  [清洗] 发现 {duplicate_count} 条重复日期记录，保留当天最后一次查询")
        valid = valid.drop_duplicates("日期", keep="last")
    return recompute_derived_columns(valid)


def write_records_atomically(csv_path, df):
    """先写临时文件，再原子替换正式 CSV，避免中断时产生半个文件。"""
    df_out = df.copy()
    df_out["日期"] = df_out["日期"].dt.strftime("%Y-%m-%d")
    df_out["更新时间"] = df_out["更新时间"].dt.strftime("%Y-%m-%d %H:%M:%S")
    directory = os.path.dirname(os.path.abspath(csv_path))
    fd, temp_path = tempfile.mkstemp(prefix=".electricity_record_", suffix=".tmp", dir=directory)
    os.close(fd)
    try:
        df_out.to_csv(temp_path, index=False, encoding="utf-8-sig", float_format="%.2f", na_rep="")
        os.replace(temp_path, csv_path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def upsert_daily_record(csv_path, df, record_day, queried_at, remaining):
    """以日期为唯一键写入当天最新快照，并重算所有派生数据。"""
    record_day = pd.Timestamp(record_day).normalize()
    queried_at = pd.Timestamp(queried_at)
    if queried_at.tzinfo is not None:
        # CSV 统一保存中国本地钟表时间，不写入时区偏移，避免与旧记录混合成 object 列。
        queried_at = queried_at.tz_localize(None)
    existing_count = (df["日期"] == record_day).sum()
    raw = df.loc[df["日期"] != record_day, RAW_COLUMNS].copy()
    new_row = pd.DataFrame(
        [{"日期": record_day, "更新时间": queried_at, "剩余电量": float(remaining)}]
    )
    result = recompute_derived_columns(pd.concat([raw, new_row], ignore_index=True))
    write_records_atomically(csv_path, result)

    current = result.loc[result["日期"] == record_day].iloc[0]
    action = "已覆盖" if existing_count else "已写入"
    print(f"\n  ✅ {action}记录: {record_day:%Y-%m-%d}, 剩余 {current['剩余电量']:.2f} 度", end="")
    if pd.notna(current["消耗电量"]):
        print(f", 相对上次净耗电 {current['消耗电量']:.2f} 度（间隔 {current['间隔天数']} 天）")
    else:
        print("（首条记录，无比较数据）")
    return result


# ================== 柱状图绘制 ===================
def plot_consumption(df, chart_path):
    """
    绘制各相邻快照之间的净耗电柱状图。

    逻辑：
      - 首条记录只有基线，没有柱；其余每条记录对应一个完整区间
      - 正数表示余额减少，负数表示余额增加；本程序不对余额增加原因作判断
      - 数据超过 PLOT_RECORDS 时，绘制最近的完整区间
    """
    valid = df.dropna(subset=["消耗电量"]).copy()
    if len(valid) < 1:
        print("  [SKIP] 不足 2 条有效记录，跳过绘图")
        return

    # 如果数据过多，取最近 PLOT_RECORDS 条
    if len(valid) > PLOT_RECORDS:
        valid = valid.tail(PLOT_RECORDS).reset_index(drop=True)
        print(f"  [绘图] 数据较多，取最近 {PLOT_RECORDS} 条")

    plot_data = valid.reset_index(drop=True)
    n = len(plot_data)
    print(f"  [绘图] 使用 {n} 条记录绘制柱状图")

    # ── 构建图表 ──
    fig, ax = plt.subplots(figsize=(max(14, n * 0.7), 6))

    # 柱状图：每根柱代表从上一条记录到当前记录的完整区间。
    x = range(n)
    values = plot_data["消耗电量"].values
    colors = ["#4A90D9" if value >= 0 else "#D97706" for value in values]

    ax.bar(
        x,
        values,
        width=0.6,
        color=colors,
        edgecolor="#2C5F8A",
        linewidth=0.8,
        alpha=0.85,
    )

    # 在柱的外侧标注数值，正负值分别放在柱顶和柱底。
    offset = max(float(max(abs(values))) * 0.02, 0.05)
    for i, v in enumerate(values):
        if pd.notna(v):
            ax.text(
                i,
                v + offset if v >= 0 else v - offset,
                f"{v:.2f}",
                ha="center",
                va="bottom" if v >= 0 else "top",
                fontsize=7,
                fontweight="bold",
            )

    # ── X 轴标签：区间终点日期 + 与前一次记录的间隔 ──
    labels = []
    for i in range(n):
        curr_date = plot_data.iloc[i]["日期"]
        days_diff = int(plot_data.iloc[i]["间隔天数"])
        labels.append(f"{curr_date:%Y-%m-%d}\n距上次 {days_diff} 天")

    # ── X 轴标签旋转防重叠 ──
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=7, rotation=30, ha="right")

    # ── 标题和轴标签 ──
    first_date = plot_data.iloc[0]["日期"]
    last_date = plot_data.iloc[-1]["日期"]
    date_range_str = f"{first_date:%Y-%m-%d} ~ {last_date:%Y-%m-%d}"
    ax.set_title(
        f"{BUILDING_GROUP} {BUILDING} {ROOM} 区间净耗电图（{date_range_str}）",
        fontsize=13,
        fontweight="bold",
        pad=12,
    )
    ax.set_ylabel("区间净耗电（度；负值表示余额增加）", fontsize=10)
    ax.set_xlabel("记录日期", fontsize=10)

    ax.axhline(0, color="#555555", linewidth=0.8)

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
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    today = now.date()
    print(f"  查询日期: {today:%Y-%m-%d}（Asia/Shanghai）")
    print("=" * 55)

    # 1. 查询电量
    remaining = query_electricity()
    if remaining is None:
        print("\n❌ 查询电量失败，程序退出")
        sys.exit(1)

    # 2. 加载、清洗、去重并写入当天唯一快照。锁覆盖整个读改写过程。
    print("\n" + "-" * 45)
    print("  记录处理:")
    try:
        with record_file_lock(CSV_PATH):
            df = load_records(CSV_PATH)
            print(f"  清洗后记录数: {len(df)}")
            queried_at = datetime.now(ZoneInfo("Asia/Shanghai"))
            df = upsert_daily_record(CSV_PATH, df, today, queried_at, remaining)
    except (OSError, TimeoutError, ValueError) as exc:
        print(f"\n❌ 记录处理失败，未写入 CSV：{exc}")
        sys.exit(1)

    # 4. 绘图
    print("\n" + "-" * 45)
    print("  绘图:")
    plot_consumption(df, CHART_PATH)

    print("\n" + "=" * 55)
    print("  全部完成！")
    print("=" * 55)


if __name__ == "__main__":
    main()
