#!/usr/bin/env python3
"""抓取中国银行当前外汇牌价。

默认输出公司使用的 24 种币种，不连接飞书，也不受月初日期限制。

运行方式：
  1. 进入项目目录：
       cd /Users/chenhaojian/Desktop/boc-fx-monthly
  2. 默认获取 24 种项目币种，并在终端输出表格：
       ./fetch_boc_rates.py
  3. 输出 JSON：
       ./fetch_boc_rates.py --format json
  4. 输出 CSV 文件：
       ./fetch_boc_rates.py --format csv --output boc-rates.csv
  5. 获取官网当前可解析的全部币种：
       ./fetch_boc_rates.py --all --format json

也可以使用 Python 直接运行：
  python3 fetch_boc_rates.py

返回字段说明（以澳大利亚元 AUD 为例；数值会随官网当前牌价变化）：

  字段                  含义                    澳大利亚元示例
  code                  币种代码                AUD
  name                  币种名称                澳大利亚元
  exchange_rate         汇率（中行折算价 ÷ 100） 4.6959
  meaning               每 1 个外币对应的人民币  1 AUD = 4.6959 CNY
  time                  中国银行官网发布时间      2026-10-07 09:51:46
"""

import argparse
import csv
import io
import json
import re
import ssl
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path


BOC_URL = "https://www.boc.cn/sourcedb/whpj/"
UA = "Mozilla/5.0"
CA = "/etc/ssl/cert.pem"
CTX = ssl.create_default_context(cafile=CA) if Path(CA).exists() else ssl.create_default_context()

# 中行页面中文名 -> ISO 代码。默认只输出项目实际使用的 24 种币种。
NAME2CODE = {
    "澳大利亚元": "AUD", "巴西雷亚尔": "BRL", "加拿大元": "CAD", "瑞士法郎": "CHF",
    "丹麦克朗": "DKK", "欧元": "EUR", "英镑": "GBP", "港币": "HKD", "日元": "JPY",
    "韩国元": "KRW", "哈萨克斯坦坚戈": "KZT", "澳门元": "MOP", "林吉特": "MYR",
    "挪威克朗": "NOK", "新西兰元": "NZD", "菲律宾比索": "PHP", "巴基斯坦卢比": "PKR",
    "卢布": "RUB", "瑞典克朗": "SEK", "新加坡元": "SGD", "泰国铢": "THB",
    "新台币": "TWD", "美元": "USD", "南非兰特": "ZAR",
}
NAME_OVERRIDE = {"KRW": "韩元"}


class FetchError(RuntimeError):
    """牌价抓取或校验失败。"""


def http_get(url, retries=4):
    last = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(request, timeout=60, context=CTX) as response:
                return response.read().decode("utf-8", "ignore")
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt + 1 < retries:
                time.sleep(3 * (attempt + 1))
    raise FetchError("抓取失败 %s: %s" % (url, last))


def parse_boc(html):
    """解析中行牌价页的八列表格。"""
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        cells = [re.sub(r"<[^>]+>", "", value).strip()
                 for value in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)]
        if len(cells) == 8 and cells[0] and cells[0] != "&nbsp;":
            rows.append(cells)
    return rows


def fetch_rows():
    first = http_get(BOC_URL + "index.html")
    page_match = re.search(r"createPage\((\d+)", first)
    page_count = int(page_match.group(1)) if page_match else 1
    rows = parse_boc(first)
    for page in range(1, page_count):
        rows.extend(parse_boc(http_get("%sindex_%d.html" % (BOC_URL, page))))
        time.sleep(0.5)
    if not rows:
        raise FetchError("没有解析到任何牌价，可能是中行页面结构发生变化")
    return rows


def number(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise FetchError("牌价格式无法解析: %r" % value) from exc


def split_publish_stamp(date_value, time_value):
    """兼容中行页面把日期列写成「日期 时间」的情况。"""
    date_value = (date_value or "").strip()
    time_value = (time_value or "").strip()
    parts = date_value.split(None, 1)
    publish_date = parts[0] if parts else ""
    publish_time = time_value or (parts[1] if len(parts) > 1 else "")
    return publish_date, publish_time


def make_rates(rows, include_all=False):
    """把官网行转换成稳定的 JSON/CSV 字段。"""
    latest = {}
    all_latest = {}
    for row in rows:
        name, forex_buy, cash_buy, forex_sell, cash_sell, mid, date, publish_time = row
        code = NAME2CODE.get(name)
        all_latest[name] = (code, (name, forex_buy, cash_buy, forex_sell, cash_sell, mid, date, publish_time))
        if code:
            latest[code] = (name, forex_buy, cash_buy, forex_sell, cash_sell, mid, date, publish_time)

    if include_all:
        entries = sorted(all_latest.values(), key=lambda item: (item[0] or "ZZZ", item[1][0]))
    else:
        wanted = sorted(NAME2CODE.values())
        missing = sorted(set(wanted) - set(latest))
        if missing:
            raise FetchError("官网缺少项目所需币种: %s" % ", ".join(missing))
        entries = [(code, latest[code]) for code in wanted]

    rates = []
    for code, values in entries:
        name, forex_buy, cash_buy, forex_sell, cash_sell, mid, date, publish_time = values
        publish_date, publish_time = split_publish_stamp(date, publish_time)
        mid_value = number(mid)
        if not include_all and (mid_value is None or mid_value <= 0):
            raise FetchError("%s 折算价无效: %r" % (code, mid))
        unit_rate = mid_value / 100 if mid_value is not None else None
        unit_rate_text = display_value(unit_rate)
        currency_code = code or name
        rates.append({
            "code": code or "",
            "name": NAME_OVERRIDE.get(code, name),
            "exchange_rate": unit_rate,
            "meaning": "1 %s = %s CNY" % (currency_code, unit_rate_text),
            "time": "%s %s" % (publish_date.replace("/", "-"), publish_time),
        })
    return rates


def display_value(value):
    return "" if value is None else ("%.6f" % value).rstrip("0").rstrip(".")


def render_table(rates):
    headers = ["代码", "币种", "汇率", "含义", "时间"]
    data = []
    for rate in rates:
        data.append([
            rate["code"], rate["name"], display_value(rate["exchange_rate"]), rate["meaning"], rate["time"],
        ])
    widths = [max(len(str(row[i])) for row in [headers] + data) for i in range(len(headers))]
    lines = ["  ".join(str(value).ljust(widths[i]) for i, value in enumerate(headers))]
    lines.append("  ".join("-" * width for width in widths))
    lines.extend("  ".join(str(value).ljust(widths[i]) for i, value in enumerate(row)) for row in data)
    return "\n".join(lines)


def render_csv(rates):
    output = io.StringIO()
    fields = ["code", "name", "exchange_rate", "meaning", "time"]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows({**rate, "exchange_rate": display_value(rate["exchange_rate"])} for rate in rates)
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description="抓取中国银行当前外汇牌价")
    parser.add_argument("--all", action="store_true", help="输出官网当前解析到的全部币种；默认只输出项目的 24 种币种")
    parser.add_argument("--format", choices=("table", "json", "csv"), default="table", help="输出格式，默认 table")
    parser.add_argument("--output", help="把结果保存到文件；不指定时直接输出到终端")
    args = parser.parse_args()

    try:
        rates = make_rates(fetch_rows(), include_all=args.all)
        if args.format == "json":
            text = json.dumps({
                "source": "中国银行",
                "source_url": BOC_URL,
                "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "count": len(rates),
                "rates": rates,
            }, ensure_ascii=False, indent=2) + "\n"
        elif args.format == "csv":
            text = render_csv(rates)
        else:
            text = render_table(rates) + "\n"
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
            print("已保存 %d 个币种到 %s" % (len(rates), args.output))
        else:
            print(text, end="")
        return 0
    except (FetchError, OSError) as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
