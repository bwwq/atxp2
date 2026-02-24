# -*- coding: utf-8 -*-
"""高并发注册脚本 - 主入口

chat.atxp.ai（LibreChat）批量注册：
  DuckMail 临时邮箱 + Playwright 自动化 + Privy OTP 验证

用法:
    python register.py                # 默认配置
    python register.py -n 10          # 注册 10 个
    python register.py -c 5 -n 20     # 5 并发注册 20 个
    python register.py --no-headless  # 显示浏览器
"""

import argparse
import asyncio
import logging
import os
import sys
import time

import aiohttp
from playwright.async_api import async_playwright

import config
from duckmail import DuckMailClient
from registrar import register_one
from results import RegisterResult, ResultRecorder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(stream=open(sys.stdout.fileno(), mode='w', encoding='utf-8', closefd=False))],
)
logger = logging.getLogger("register")

MAX_RETRY = 2  # 失败后最多重试次数


async def _try_register(
    browser,
    http_session: aiohttp.ClientSession,
    index: int,
    attempt: int,
) -> tuple[bool, RegisterResult]:
    """单次注册尝试，返回 (成功, 结果)"""
    start = time.time()
    tag = f"[{index}]" if attempt == 0 else f"[{index} 重试{attempt}]"

    mail = DuckMailClient(http_session)
    email = await mail.create_temp_email()
    if not email:
        return False, RegisterResult(
            index=index, email="(创建失败)", status="失败",
            error="临时邮箱创建失败", duration=time.time() - start,
        )

    context = await browser.new_context(
        viewport={"width": 1280, "height": 720},
        locale="en-US",
    )
    context.set_default_timeout(config.PAGE_TIMEOUT)

    try:
        result = await register_one(context, mail)
        duration = time.time() - start

        if result["success"]:
            logger.info("%s 注册成功: %s (%.1f秒)", tag, email, duration)
            return True, RegisterResult(
                index=index, email=email, status="成功",
                duration=duration, cookies=result.get("cookies", {}),
                cookie_str=result.get("cookie_str", ""),
            )
        else:
            logger.warning("%s 注册失败: %s - %s", tag, email, result["error"])
            return False, RegisterResult(
                index=index, email=email, status="失败",
                error=result["error"], duration=duration,
            )
    except Exception as e:
        logger.exception("%s 未预期错误", tag)
        return False, RegisterResult(
            index=index, email=email, status="失败",
            error=f"未预期错误: {e}", duration=time.time() - start,
        )
    finally:
        await context.close()


async def _process_one(
    sem: asyncio.Semaphore,
    browser,
    http_session: aiohttp.ClientSession,
    index: int,
    recorder: ResultRecorder,
) -> None:
    """单个注册任务（带重试）"""
    async with sem:
        logger.info("[%d/%d] 开始", index, config.TOTAL_ACCOUNTS)

        for attempt in range(1 + MAX_RETRY):
            success, reg_result = await _try_register(browser, http_session, index, attempt)
            if success:
                recorder.add(reg_result)
                return
            # 最后一次失败才记录
            if attempt == MAX_RETRY:
                recorder.add(reg_result)
            else:
                logger.info("[%d] 准备重试 (%d/%d)...", index, attempt + 1, MAX_RETRY)
                await asyncio.sleep(2)


async def main(args: argparse.Namespace) -> None:
    if args.count:
        config.TOTAL_ACCOUNTS = args.count
    if args.concurrency:
        config.CONCURRENCY = args.concurrency
    if args.no_headless:
        config.HEADLESS = False

    os.makedirs(config.RESULTS_DIR, exist_ok=True)

    logger.info("=" * 50)
    logger.info("高并发注册脚本启动")
    logger.info("目标: %s", config.TARGET_URL)
    logger.info("总数: %d, 并发: %d, 无头: %s, 重试: %d", config.TOTAL_ACCOUNTS, config.CONCURRENCY, config.HEADLESS, MAX_RETRY)
    logger.info("=" * 50)

    recorder = ResultRecorder()
    sem = asyncio.Semaphore(config.CONCURRENCY)

    async with aiohttp.ClientSession() as http_session:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=config.HEADLESS,
                args=config.BROWSER_ARGS,
                slow_mo=config.SLOW_MO,
            )
            try:
                tasks = [
                    _process_one(sem, browser, http_session, i + 1, recorder)
                    for i in range(config.TOTAL_ACCOUNTS)
                ]
                await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                await browser.close()

    print()
    print(recorder.summary())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="chat.atxp.ai 高并发注册脚本")
    parser.add_argument("-n", "--count", type=int, help=f"注册数量（默认 {config.TOTAL_ACCOUNTS}）")
    parser.add_argument("-c", "--concurrency", type=int, help=f"并发数（默认 {config.CONCURRENCY}）")
    parser.add_argument("--no-headless", action="store_true", help="显示浏览器窗口")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
