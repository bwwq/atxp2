# -*- coding: utf-8 -*-
"""Playwright 注册核心逻辑

处理 chat.atxp.ai → accounts.atxp.ai 的 Privy OTP 注册流程：
1. 访问 chat.atxp.ai → 自动重定向到 accounts.atxp.ai
2. 输入邮箱 → Submit
3. 等待 OTP 邮件（6 位数字验证码，来自 privy.io）
4. 输入 OTP → 自动完成注册/登录
5. 确保到达 chat.atxp.ai 并获取 refreshToken
"""

import asyncio
import logging
import random

from playwright.async_api import BrowserContext, TimeoutError as PlaywrightTimeout

import config
from duckmail import DuckMailClient

logger = logging.getLogger(__name__)

CHAT_URL_PREFIX = "https://chat.atxp.ai"


def _is_on_chat(url: str) -> bool:
    return url.startswith(CHAT_URL_PREFIX)


def _is_on_auth(url: str) -> bool:
    return url.startswith("https://accounts.atxp.ai") or url.startswith("https://auth.atxp.ai")


async def _get_cookie(context: BrowserContext, name: str) -> str:
    """获取指定 cookie 值"""
    for c in await context.cookies():
        if c["name"] == name and c["value"]:
            return c["value"]
    return ""


async def _try_skip(page, email: str = "") -> bool:
    """快速检测并点击 Skip 弹窗"""
    for selector in ['text="Skip for now"', 'text="Skip"']:
        try:
            el = page.locator(selector).first
            if await el.is_visible(timeout=300):
                await el.click()
                logger.info("[%s] 点击 Skip", email)
                return True
        except Exception:
            pass
    return False


async def _extract_result(context: BrowserContext, page, email: str) -> dict:
    """从 context cookies 提取结果"""
    cookies_list = await context.cookies()
    cookie_dict = {c["name"]: c["value"] for c in cookies_list}
    cookie_str = "; ".join(f'{c["name"]}={c["value"]}' for c in cookies_list)
    refresh_token = cookie_dict.get("refreshToken", "")
    return {
        "success": bool(refresh_token),
        "error": "" if refresh_token else "无 refreshToken",
        "cookies": cookie_dict,
        "cookie_str": cookie_str,
        "email": email,
        "final_url": page.url,
        "refresh_token": refresh_token,
    }


async def _wait_privy_auth(context: BrowserContext, page, email: str, timeout: int = 10) -> bool:
    """等待 privy 认证完成（privy-token cookie 出现）"""
    for _ in range(timeout):
        await asyncio.sleep(1)
        await _try_skip(page, email)
        if await _get_cookie(context, "privy-token"):
            logger.info("[%s] privy 认证成功", email)
            return True
        if await _get_cookie(context, "refreshToken"):
            return True
    return False


async def _force_chat_redirect(context: BrowserContext, page, email: str) -> bool:
    """强制导航到 chat.atxp.ai 获取 refreshToken

    privy 认证成功后，context 已持有 privy 凭据，
    导航到 chat.atxp.ai 会自动触发 OIDC 回调流程设置 refreshToken。
    """
    targets = [
        (CHAT_URL_PREFIX + "/", "domcontentloaded"),
        (CHAT_URL_PREFIX + "/c/new", "domcontentloaded"),
        (CHAT_URL_PREFIX + "/", "networkidle"),
    ]

    for url, wait in targets:
        if await _get_cookie(context, "refreshToken"):
            return True
        try:
            logger.info("[%s] 导航 %s (%s)", email, url, wait)
            await page.goto(url, wait_until=wait, timeout=20000)
            # 等待 OIDC callback 完成
            for _ in range(8):
                await asyncio.sleep(1)
                if await _get_cookie(context, "refreshToken"):
                    logger.info("[%s] 导航成功，获取到 refreshToken", email)
                    return True
                await _try_skip(page, email)
        except PlaywrightTimeout:
            continue

    return bool(await _get_cookie(context, "refreshToken"))


async def register_one(context: BrowserContext, mail: DuckMailClient) -> dict:
    """执行单个账号的完整注册流程

    Returns:
        {"success": bool, "error": str, "cookies": dict, "cookie_str": str,
         "email": str, "final_url": str, "refresh_token": str}
    """
    page = await context.new_page()
    email = mail.email
    result_base = {"success": False, "error": "", "cookies": {}, "cookie_str": "",
                   "email": email, "final_url": "", "refresh_token": ""}

    try:
        # === 步骤 1: 导航到认证页 ===
        logger.info("[%s] 访问 %s", email, config.TARGET_URL)
        await page.goto(config.TARGET_URL, wait_until="domcontentloaded", timeout=config.NAVIGATION_TIMEOUT)

        if _is_on_chat(page.url):
            try:
                await page.wait_for_url(lambda url: _is_on_auth(url) or "login" in url, timeout=15000)
            except PlaywrightTimeout:
                pass

        logger.info("[%s] 页面: %s", email, page.url[:80])

        # === 步骤 2: 输入邮箱 ===
        email_input = page.locator("#email-input")
        await email_input.wait_for(state="visible", timeout=30000)
        await email_input.fill(email)
        await asyncio.sleep(random.uniform(0.2, 0.4))

        submit_btn = page.locator('button:has-text("Submit")')
        await submit_btn.click()
        logger.info("[%s] 已提交邮箱", email)

        # 等待 OTP 输入界面（带重试）
        otp_first = page.locator('input[name="code-0"]')
        try:
            await otp_first.wait_for(state="visible", timeout=15000)
        except PlaywrightTimeout:
            logger.warning("[%s] OTP 未出现，重试", email)
            try:
                # 有时需要点击 "Log in with a different method" 或重新提交
                email_input = page.locator("#email-input")
                if await email_input.is_visible(timeout=2000):
                    await email_input.fill(email)
                    await asyncio.sleep(0.3)
                    await submit_btn.click()
                await otp_first.wait_for(state="visible", timeout=15000)
            except PlaywrightTimeout:
                return {**result_base, "error": "OTP 输入框超时", "final_url": page.url}

        logger.info("[%s] OTP 输入界面已出现", email)

        # === 步骤 3: 获取并输入 OTP ===
        otp_code = await mail.wait_verification_code(pattern=r"(\d{6})")
        if not otp_code:
            return {**result_base, "error": "OTP 验证码超时", "final_url": page.url}

        logger.info("[%s] 输入 OTP: %s", email, otp_code)
        for idx, digit in enumerate(otp_code):
            await page.locator(f'input[name="code-{idx}"]').fill(digit)
            await asyncio.sleep(random.uniform(0.03, 0.08))

        # === 步骤 4: 等待认证完成 ===
        logger.info("[%s] OTP 已提交，等待认证...", email)

        # 第一阶段：等待 SPA 自动完成（最多 12 秒）
        for tick in range(12):
            await asyncio.sleep(1)
            if await _get_cookie(context, "refreshToken"):
                logger.info("[%s] 获取到 refreshToken (%d秒)", email, tick + 1)
                return await _extract_result(context, page, email)
            if _is_on_chat(page.url) and tick >= 3:
                await asyncio.sleep(2)
                if await _get_cookie(context, "refreshToken"):
                    return await _extract_result(context, page, email)
            await _try_skip(page, email)

        if await _get_cookie(context, "refreshToken"):
            return await _extract_result(context, page, email)

        # 第二阶段：privy 已认证 → reload 当前页触发 SPA 回调
        has_privy = bool(await _get_cookie(context, "privy-token"))
        if has_privy:
            logger.info("[%s] privy 已认证，reload 触发回调", email)
            try:
                await page.reload(wait_until="domcontentloaded", timeout=15000)
                for _ in range(8):
                    await asyncio.sleep(1)
                    if await _get_cookie(context, "refreshToken"):
                        logger.info("[%s] reload 后获取到 refreshToken", email)
                        return await _extract_result(context, page, email)
                    await _try_skip(page, email)
            except PlaywrightTimeout:
                pass

            # 第三阶段：新页面导航
            if not await _get_cookie(context, "refreshToken"):
                logger.info("[%s] 尝试新页面导航", email)
                page2 = await context.new_page()
                try:
                    success = await _force_chat_redirect(context, page2, email)
                    if success:
                        return await _extract_result(context, page2, email)
                finally:
                    await page2.close()

        return await _extract_result(context, page, email)

    except Exception as e:
        error_msg = str(e).split("\n")[0][:120]
        logger.error("[%s] 注册失败: %s", email, error_msg)
        return {**result_base, "error": error_msg}

    finally:
        await page.close()
