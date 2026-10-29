#!/usr/bin/env python
"""佳明 Personal API 独立自检脚本（不依赖 AstrBot）。

用途：在把插件装进 AstrBot 之前，先确认 Key、区域、网络是否正常。
只做只读 GET，不会修改任何佳明数据。

实测要点（2026-10-29 验证）：
- token 端点返回的是 text/plain 的 JWT 字符串，不是 JSON；
- 请求 token 端点时**不要**带 `Accept: application/json`，否则返回 HTTP 406。

用法（PowerShell）：
    $env:GARMIN_API_KEY="你的Key"
    $env:GARMIN_REGION="cn"
    python selftest.py

    # 或直接传参数（Key 会留在命令历史里）
    python selftest.py --api-key 你的Key --region cn --date 2026-10-28
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

try:
    import httpx
except ImportError:
    print("缺少 httpx，请先执行：python -m pip install httpx")
    sys.exit(2)

BASE_URLS = {
    "cn": "https://personal.connectapi.garmin.cn",
    "global": "https://personal.connectapi.garmin.com",
}
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"


def main() -> int:
    parser = argparse.ArgumentParser(description="Garmin Personal API self-test")
    parser.add_argument("--api-key", default=os.getenv("GARMIN_API_KEY", ""))
    parser.add_argument(
        "--region",
        default=os.getenv("GARMIN_REGION", "cn"),
        choices=list(BASE_URLS),
    )
    parser.add_argument("--date", default="", help="YYYY-MM-DD，默认今天")
    args = parser.parse_args()

    if not args.api_key:
        print("未提供 Key：请设置环境变量 GARMIN_API_KEY 或使用 --api-key")
        return 2

    key = args.api_key.strip()
    day_date = date.fromisoformat(args.date) if args.date else date.today()
    day = day_date.isoformat()
    start_day = (day_date - timedelta(days=30)).isoformat()
    base = BASE_URLS[args.region]

    print(f"区域: {args.region}")
    print(f"基地址: {base}")
    print(f"Key 长度: {len(key)}（不打印内容）")
    print(f"日期: {day}")

    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        # 1) 换 token —— 注意不能带 Accept: application/json
        try:
            r = client.post(
                base + TOKEN_PATH,
                headers={"Authorization": f"ApiKey {key}"},
            )
        except httpx.HTTPError as exc:
            print(f"\n[失败] 网络错误: {exc}")
            print("  检查本机能否访问该域名（浏览器打开 https://connect.garmin.cn 试试）")
            return 1

        print(f"\n[1] 换 token: HTTP {r.status_code}")
        if r.status_code != 200:
            print(f"  响应: {r.text[:300]}")
            if r.status_code == 401:
                print("  → Key 无效/过期，请到 设置 → Personal API Key 重新生成")
                print("    （大陆站与国际站的 Key 不能混用）")
            elif r.status_code == 406:
                print("  → 令牌端点不接受 Accept: application/json，请勿添加该请求头")
            elif r.status_code == 526:
                print("  → 大陆端点暂不可用，稍后再试")
            return 1

        body = r.text.strip()
        token = None
        if body.startswith("{"):
            try:
                data = r.json()
                token = (
                    data.get("access_token")
                    or data.get("accessToken")
                    or (data.get("data") or {}).get("access_token")
                )
            except ValueError:
                token = None
        if not token and body.count(".") == 2:
            token = body
        if not token:
            print(f"  无法解析 token 响应: {body[:200]}")
            return 1
        print(f"  ✓ 拿到 token（{len(token)} 字符）")

        headers = {"Authorization": f"Bearer {token}"}
        checks = [
            ("账号资料", "/userprofile-service/userprofile/settings"),
            (
                "活动列表(最近3条)",
                "/activitylist-service/activities/search/activities?start=0&limit=3",
            ),
            (
                "身体成分(近30天)",
                f"/weight-service/weight/range/{start_day}/{day}?includeAll=true",
            ),
            ("睡眠(当天)", f"/sleep-service/sleep/dailySleep?date={day}"),
            ("心率(当天)", f"/wellness-service/wellness/dailyHeartRate?date={day}"),
            ("HRV(当天)", f"/hrv-service/hrv/{day}"),
            ("压力(当天)", f"/usersummary-service/stats/stress/daily/{day}/{day}"),
            (
                "BodyBattery(当天)",
                "/wellness-service/wellness/bodyBattery/reports/daily"
                f"?startDate={day}&endDate={day}",
            ),
            ("血氧(当天)", f"/wellness-service/wellness/daily/spo2acclimation/{day}"),
            (
                "每日汇总(当天)",
                f"/usersummary-service/v2/dailysummaries?startDate={day}&endDate={day}",
            ),
            ("健身年龄(当天)", f"/fitnessage-service/fitnessage/v2/{day}"),
        ]

        ok = 0
        for label, path in checks:
            try:
                resp = client.get(base + path, headers=headers)
                status = "✓" if resp.status_code == 200 else "✗"
                print(
                    f"[{status}] {label}: HTTP {resp.status_code}, "
                    f"{len(resp.content)} bytes"
                )
                if resp.status_code == 200:
                    ok += 1
                else:
                    print(f"     {resp.text[:200]}")
            except httpx.HTTPError as exc:
                print(f"[✗] {label}: 网络错误 {exc}")

        print(f"\n结果: {ok}/{len(checks)} 项接口可用")
        print("提示：身体成分返回 0 条是正常的——255 手表本身不测体重，")
        print("      需要在 Garmin Connect 里手动记录体重或配佳明 Index 体重秤。")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
