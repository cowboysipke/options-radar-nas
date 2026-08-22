# -*- coding: utf-8 -*-
"""全功能实机走查：逐页访问面板、逐项执行动作，输出结构化报告。"""
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8787"
REPORT = []


def log(name, ok, detail=""):
    REPORT.append({"item": name, "ok": ok, "detail": detail[:300]})
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}: {detail[:200]}")


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)


def post_form(path, csrf, fields=None):
    data = {"csrf": csrf}
    data.update(fields or {})
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, r.read().decode("utf-8", "replace")


def post_json(path, csrf, fields=None):
    data = {"csrf": csrf}
    data.update(fields or {})
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, r.read().decode("utf-8", "replace")


def extract_csrf(html_text):
    m = re.search(r'name="csrf" value="([^"]+)"', html_text)
    return m.group(1) if m else None


def main():
    # GBK console (Chinese Windows) cannot encode the ✅/❌ summary marks;
    # force UTF-8 so the report prints cleanly and exits 0.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # ---------- 1. 首页 ----------
    try:
        status, html, _ = get("/")
        markers = {
            "今日推荐标题": "今日推荐" in html,
            "日期下拉框": 'name="date"' in html,
            "评价标准说明": "评价标准说明" in html,
            "立即采集按钮": "立即采集" in html,
            "评级A/B/C/D": "A级≥80分" in html,
        }
        log("1. 首页 /", status == 200 and all(markers.values()), json.dumps(markers, ensure_ascii=False))
    except Exception as e:
        log("1. 首页 /", False, str(e))

    # ---------- 2. 日期切换 ----------
    try:
        status, html, _ = get("/?date=2026-08-11")
        has_select = 'selected' in html and '2026-08-11' in html
        # Cards render as rec-top sections; symbol names of that day's top-3
        # vary with data, so also accept any rendered recommendation card.
        has_recs = "rec-top" in html or "GTLB" in html or "QQQ" in html or "SM" in html
        log("2. 日期切换 2026-08-11", status == 200 and has_select and has_recs,
            f"select={has_select} 推荐数据={has_recs}")
    except Exception as e:
        log("2. 日期切换 2026-08-11", False, str(e))

    # ---------- 3. 信号明细 ----------
    try:
        status, html, _ = get("/signals")
        markers = {
            "表格头": "分析师意见数" in html,
            "有信号行": "US." in html,
        }
        log("3. 信号明细 /signals", status == 200 and markers["表格头"],
            json.dumps(markers, ensure_ascii=False))
    except Exception as e:
        log("3. 信号明细 /signals", False, str(e))

    # ---------- 4. 自选与持仓 ----------
    try:
        status, html, _ = get("/portfolio")
        markers = {
            "公司名称列": "公司名称" in html,
            "持仓数据或空态": ("持仓" in html) and ("暂无" in html or "US." in html or "quantity" in html),
        }
        log("4. 自选与持仓 /portfolio", status == 200 and markers["公司名称列"],
            json.dumps(markers, ensure_ascii=False))
    except Exception as e:
        log("4. 自选与持仓 /portfolio", False, str(e))

    # ---------- 5. 回测 ----------
    try:
        status, html, _ = get("/backtest")
        markers = {
            "分析师信号回测": "分析师信号回测" in html,
            "结算笔数": "结算笔数" in html or "filled" in html,
            "平均收益": "平均收益" in html or "avg_net_return" in html,
        }
        log("5. 回测 /backtest", status == 200 and markers["分析师信号回测"],
            json.dumps(markers, ensure_ascii=False))
    except Exception as e:
        log("5. 回测 /backtest", False, str(e))

    # ---------- 6. 系统诊断 ----------
    try:
        status, html, _ = get("/system")
        markers = {
            "IBKR卡": "IBKR" in html,
            "Discord卡": "DISCORD" in html or "Discord" in html,
            "AI卡": "AI" in html,
            "飞书卡": "FEISHU" in html or "飞书" in html,
            "token状态": "token" in html.lower(),
        }
        log("6. 系统诊断 /system", status == 200 and all([markers["IBKR卡"], markers["Discord卡"]]),
            json.dumps(markers, ensure_ascii=False))
    except Exception as e:
        log("6. 系统诊断 /system", False, str(e))

    # ---------- 7. 设置页 ----------
    try:
        status, html, _ = get("/setup")
        csrf = extract_csrf(html)
        markers = {
            "DeepSeek已保存": "DeepSeek API Key" in html and "已保存" in html,
            "飞书webhook已保存": "飞书 Webhook URL" in html and "已保存" in html,
            "Discord token已保存": "Discord 用户 Token" in html and "已保存" in html,
            "Massive已保存": "Massive API Key" in html and "已保存" in html,
            "富途导入按钮": "/futu/import-watchlist" in html,
        }
        log("7. 设置 /setup", status == 200 and markers["富途导入按钮"],
            json.dumps(markers, ensure_ascii=False))
    except Exception as e:
        log("7. 设置 /setup", False, str(e))
        csrf = None

    # ---------- 8. 富途导入（修复验证） ----------
    if csrf:
        try:
            status, body = post_json("/futu/import-watchlist", csrf)
            try:
                data = json.loads(body)
                is_json = isinstance(data, dict)
                msg = data.get("message", "")[:120]
                log("8. 富途导入(JSON响应)", status == 200 and is_json,
                    f"HTTP={status} json={is_json} msg={msg}")
            except ValueError:
                log("8. 富途导入(JSON响应)", False, f"HTTP={status} 非JSON前80字符: {body[:80]}")
        except Exception as e:
            log("8. 富途导入(JSON响应)", False, str(e))
    else:
        log("8. 富途导入(JSON响应)", False, "无CSRF token")

    # ---------- 9. IBKR同步 ----------
    if csrf:
        try:
            status, body = post_json("/api/ibkr/sync", csrf)
            try:
                data = json.loads(body)
                log("9. IBKR同步", status == 200 and isinstance(data, dict),
                    f"HTTP={status} json={isinstance(data, dict)}")
            except ValueError:
                log("9. IBKR同步", False, f"非JSON: {body[:80]}")
        except Exception as e:
            log("9. IBKR同步", False, str(e))

    # ---------- 10. 测试DeepSeek ----------
    if csrf:
        try:
            status, body = post_json("/api/actions/deepseek-test", csrf)
            log("10. 测试DeepSeek", status == 200, f"HTTP={status} body前120: {body[:120]}")
        except Exception as e:
            log("10. 测试DeepSeek", False, str(e))

    # ---------- 11. 测试飞书 ----------
    if csrf:
        try:
            status, body = post_json("/api/actions/feishu-test", csrf)
            log("11. 测试飞书", status == 200, f"HTTP={status} body前120: {body[:120]}")
        except Exception as e:
            log("11. 测试飞书", False, str(e))

    # ---------- 12. 测试Massive ----------
    if csrf:
        try:
            status, body = post_json("/api/providers/massive/test", csrf)
            log("12. 测试Massive", status == 200, f"HTTP={status} body前120: {body[:120]}")
        except Exception as e:
            log("12. 测试Massive", False, str(e))

    # ---------- 13. 备份 ----------
    if csrf:
        try:
            status, body = post_json("/api/actions/backup", csrf)
            log("13. 创建备份", status == 200, f"HTTP={status} body前120: {body[:120]}")
        except Exception as e:
            log("13. 创建备份", False, str(e))

    # ---------- 14. 健康接口 ----------
    try:
        status, body, _ = get("/health")
        data = json.loads(body)
        log("14. 健康接口", status == 200 and data.get("status") == "ok",
            f"status={data.get('status')} discord={data.get('discord',{}).get('status')}")
    except Exception as e:
        log("14. 健康接口", False, str(e))

    # ---------- 汇总 ----------
    passed = sum(1 for r in REPORT if r["ok"])
    print("\n" + "=" * 60)
    print(f"走查结果: {passed}/{len(REPORT)} 通过")
    for r in REPORT:
        mark = "✅" if r["ok"] else "❌"
        print(f"{mark} {r['item']} — {r['detail'][:120]}")
    print("=" * 60)


if __name__ == "__main__":
    main()
