# -*- coding: utf-8 -*-
"""结果记录与统计模块"""

import csv
import json
import os
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import config

logger = logging.getLogger(__name__)


@dataclass
class RegisterResult:
    """单次注册结果"""
    index: int
    email: str
    status: str  # "成功" 或 "失败"
    error: str = ""
    duration: float = 0.0
    cookies: dict = field(default_factory=dict)
    cookie_str: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


class ResultRecorder:
    """注册结果记录器（CSV + JSON 双格式）"""

    CSV_HEADER = ["序号", "邮箱", "状态", "失败原因", "耗时(秒)", "时间戳"]

    def __init__(self):
        os.makedirs(config.RESULTS_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._csv_path = os.path.join(config.RESULTS_DIR, f"register_{ts}.csv")
        self._json_path = os.path.join(config.RESULTS_DIR, f"accounts_{ts}.json")
        self._results: list[RegisterResult] = []
        self._accounts: list[dict] = []

        with open(self._csv_path, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(self.CSV_HEADER)

        logger.info("CSV 文件: %s", self._csv_path)
        logger.info("JSON 文件: %s", self._json_path)

    def add(self, result: RegisterResult) -> None:
        """添加一条注册结果"""
        self._results.append(result)

        # 追加 CSV
        with open(self._csv_path, "a", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow([
                result.index, result.email, result.status,
                result.error, f"{result.duration:.1f}", result.timestamp,
            ])

        # 成功的账号写入 JSON
        if result.status == "成功":
            # 提取 2api 所需的关键 cookies
            key_cookie_names = {"connect.sid", "privy-token", "privy-session", "cf_clearance", "__cf_bm", "refreshToken"}
            key_cookies = {k: v for k, v in result.cookies.items() if k in key_cookie_names}

            account = {
                "email": result.email,
                "refresh_token": result.cookies.get("refreshToken", ""),
                "cookies": result.cookie_str,
                "cookie_dict": result.cookies,
                "key_cookies": key_cookies,
                "created_at": result.timestamp,
            }
            self._accounts.append(account)
            Path(self._json_path).write_text(
                json.dumps(self._accounts, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    def summary(self) -> str:
        """汇总统计"""
        total = len(self._results)
        if total == 0:
            return "无注册记录"

        success = sum(1 for r in self._results if r.status == "成功")
        failed = total - success
        durations = [r.duration for r in self._results if r.status == "成功"]
        avg_dur = sum(durations) / len(durations) if durations else 0

        error_counts: dict[str, int] = {}
        for r in self._results:
            if r.status == "失败" and r.error:
                error_counts[r.error] = error_counts.get(r.error, 0) + 1

        lines = [
            "=" * 50,
            "注册结果汇总",
            "=" * 50,
            f"总计: {total}",
            f"成功: {success} ({success / total * 100:.1f}%)",
            f"失败: {failed} ({failed / total * 100:.1f}%)",
            f"平均耗时: {avg_dur:.1f}秒",
            f"CSV: {self._csv_path}",
            f"JSON: {self._json_path}",
        ]
        if error_counts:
            lines.append("")
            lines.append("失败原因:")
            for err, cnt in sorted(error_counts.items(), key=lambda x: -x[1]):
                lines.append(f"  {err}: {cnt}次")
        lines.append("=" * 50)
        return "\n".join(lines)

    @property
    def success_count(self) -> int:
        return sum(1 for r in self._results if r.status == "成功")

    @property
    def total_count(self) -> int:
        return len(self._results)
