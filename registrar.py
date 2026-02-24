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
    """判断 URL 是否在 chat.atxp.ai 域名下（排除 URL 参数中的误匹配）"""
    return url.startswith(CHAT_URL_PREFIX)


def _is_on_auth(url: str) -> bool:
    """判断 URL 是否在认证页面（accounts.atxp.ai 或 auth.atxp.ai）"""
    return url.startswith("https://accounts.atxp.ai") or url.startswith("https://auth.atxp.ai")


async def register_one(context: BrowserContext, mail: DuckMailClient) -> dict:
    """执行单个账号的完整注册流程（邮箱提交 → OTP 验证）

    Returns:
        {"success": bool, "error": str, "cookies": dict, "cookie_str": str,
         "email": str, "final_url": str, "refresh_token": str}
    """
    page = await context.new_page()
    email = mail.email
    result_base = {"success": False, "error": "", "cookies": {}, "cookie_str": "",
                   "email": email, "final_url": "", "refresh_token": ""}

    try:
        # === 步骤 1: 导航到登录页 ===
        logger.info("[%s] 访问 %s", email, config.TARGET_URL)
        await page.goto(config.TARGET_URL, wait_until="domcontentloaded", timeout=config.NAVIGATION_TIMEOUT)
        await asyncio.sleep(3)

        # 等待 OpenID 重定向到认证页
        if _is_on_chat(page.url):
            try:
                await page.wait_for_url(
                    lambda url: _is_on_auth(url),
                    timeout=config.NAVIGATION_TIMEOUT,
                )
            except PlaywrightTimeout:
                pass

        # 等待 SPA 完全渲染
        logger.info("[%s] 认证页: %s", email, page.url[:80])
        await asyncio.sleep(5)

        # === 步骤 2: 等待 SPA 渲染并输入邮箱 ===
        email_input = page.locator("#email-input")
        await email_input.wait_for(state="visible", timeout=60000)
        await email_input.fill(email)
        await asyncio.sleep(random.uniform(0.3, 0.8))

        submit_btn = page.locator('button:has-text("Submit")')
        await submit_btn.click()
        logger.info("[%s] 已提交邮箱", email)

        # 等待 OTP 输入界面
        otp_first = page.locator('input[name="code-0"]')
        await otp_first.wait_for(state="visible", timeout=config.PAGE_TIMEOUT)
        logger.info("[%s] OTP 输入界面已出现", email)

        # === 步骤 3: 等待 OTP 验证码 ===
        otp_code = await mail.wait_verification_code(pattern=r"(\d{6})")
        if not otp_code:
            return {**result_base, "error": "OTP 验证码等待超时", "final_url": page.url}

        # === 步骤 4: 输入 OTP ===
        logger.info("[%s] 输入 OTP: %s", email, otp_code)
        for idx, digit in enumerate(otp_code):
            code_input = page.locator(f'input[name="code-{idx}"]')
            await code_input.fill(digit)
            await asyncio.sleep(random.uniform(0.05, 0.15))

        # OTP 验证后可能出现 Stripe 自动充值弹窗，需要跳过
        await asyncio.sleep(3)
        for attempt in range(8):
            try:
                # 检查多种可能的 "Skip" 元素（button/a/span）
                skip = page.locator('text="Skip for now"').first
                if await skip.is_visible():
                    logger.info("[%s] 点击 Skip for now", email)
                    await skip.scroll_into_view_if_needed()
                    await skip.click()
                    await asyncio.sleep(2)
                    continue
            except Exception:
                pass

            try:
                skip2 = page.locator('text="Skip"').first
                if await skip2.is_visible():
                    logger.info("[%s] 点击 Skip", email)
                    await skip2.click()
                    await asyncio.sleep(2)
                    continue
            except Exception:
                pass

            # 检查页面是否已经离开充值页面
            page_text = await page.evaluate("() => document.body?.innerText?.substring(0, 200) || ''")
            if "Get $5" not in page_text and "auto-topup" not in page_text:
                break
            await asyncio.sleep(2)

        # === 步骤 5: 等待跳转到 chat.atxp.ai ===
        logger.info("[%s] 等待跳转...", email)

        # 轮询检查 URL（最多 60 秒）
        for attempt in range(12):
            await asyncio.sleep(5)
            if _is_on_chat(page.url):
                logger.info("[%s] 已跳转到 chat.atxp.ai (%d秒)", email, (attempt + 1) * 5)
                break
        else:
            # 60 秒仍未跳转，尝试手动导航
            logger.warning("[%s] 跳转超时，尝试手动导航", email)
            try:
                await page.goto(CHAT_URL_PREFIX + "/", wait_until="networkidle", timeout=30000)
                await asyncio.sleep(5)
            except PlaywrightTimeout:
                pass

            if not _is_on_chat(page.url):
                # 再试一次 - 有时第一次手动导航会重定向到 auth
                try:
                    await page.goto(CHAT_URL_PREFIX + "/c/new", wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(5)
                except PlaywrightTimeout:
                    pass

        # 等待页面稳定（特别是 OAuth callback 需要时间处理）
        await asyncio.sleep(3)

        # 如果在 callback URL 上，等待它完成处理
        if "/oauth/" in page.url or "/callback" in page.url:
            logger.info("[%s] 在 OAuth 回调页面，等待处理完成...", email)
            for _ in range(10):
                await asyncio.sleep(3)
                if "/oauth/" not in page.url and "/callback" not in page.url:
                    break
                # 检查 refreshToken 是否已设置
                cookies_check = await context.cookies()
                if any(c["name"] == "refreshToken" for c in cookies_check):
                    logger.info("[%s] refreshToken 已设置", email)
                    break
            await asyncio.sleep(2)

        # 提取 cookies（从所有域名）
        cookies_list = await context.cookies()
        cookie_dict = {c["name"]: c["value"] for c in cookies_list}
        cookie_str = "; ".join(f'{c["name"]}={c["value"]}' for c in cookies_list)

        final_url = page.url
        refresh_token = cookie_dict.get("refreshToken", "")
        on_chat = _is_on_chat(final_url)

        # 成功条件：在聊天页且有 refreshToken
        success = on_chat and bool(refresh_token)

        if not success and "privy-token" in cookie_dict and not on_chat:
            # privy 认证成功但未跳转 — 尝试通过新页面获取 refreshToken
            logger.info("[%s] privy 认证成功但未跳转，尝试新页面", email)
            page2 = await context.new_page()
            try:
                await page2.goto(CHAT_URL_PREFIX + "/", wait_until="networkidle", timeout=30000)
                await asyncio.sleep(5)
                if _is_on_chat(page2.url):
                    # 重新提取 cookies
                    cookies_list = await context.cookies()
                    cookie_dict = {c["name"]: c["value"] for c in cookies_list}
                    cookie_str = "; ".join(f'{c["name"]}={c["value"]}' for c in cookies_list)
                    refresh_token = cookie_dict.get("refreshToken", "")
                    final_url = page2.url
                    success = bool(refresh_token)
                    logger.info("[%s] 新页面跳转%s", email, "成功" if success else "失败")
            except Exception:
                pass
            finally:
                await page2.close()

        return {
            "success": success,
            "error": "" if success else f"最终 URL: {final_url[:80]}, refreshToken: {'有' if refresh_token else '无'}",
            "cookies": cookie_dict,
            "cookie_str": cookie_str,
            "email": email,
            "final_url": final_url,
            "refresh_token": refresh_token,
        }

    except Exception as e:
        error_msg = str(e)
        logger.error("[%s] 注册失败: %s", email, error_msg)
        try:
            await page.screenshot(path=f"results/error_{email.split('@')[0]}.png")
        except Exception:
            pass
        return {**result_base, "error": error_msg}

    finally:
        await page.close()
