# -*- coding: utf-8 -*-
"""DuckMail 临时邮箱 API 客户端

基于 https://api.duckmail.sbs，Hydra 风格 REST API。
认证方式：API key 作为 Bearer token → 创建账号后切换为账号 token。
"""

import asyncio
import random
import re
import string
import time
import logging

import aiohttp

import config

logger = logging.getLogger(__name__)


class DuckMailClient:
    """DuckMail API 异步客户端（每个注册任务一个实例）"""

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session
        self._base = config.DUCKMAIL_API_BASE.rstrip("/")
        self.email: str | None = None
        self.password: str | None = None
        self.token: str | None = None
        self.account_id: str | None = None
        # API key 作为初始 Bearer token
        self._api_key = config.DUCKMAIL_API_KEY

    def _auth_headers(self, use_account_token: bool = True) -> dict:
        """构造认证头：优先使用账号 token，否则用 API key"""
        if use_account_token and self.token:
            return {"Authorization": f"Bearer {self.token}"}
        if self._api_key:
            return {"Authorization": f"Bearer {self._api_key}"}
        return {}

    async def _request(self, method: str, path: str, headers: dict | None = None, **kwargs) -> dict | list:
        """发送 API 请求"""
        url = f"{self._base}{path}"
        merged_headers = {**self._auth_headers(), **(headers or {})}
        async with self._session.request(method, url, headers=merged_headers, **kwargs) as resp:
            if resp.status == 204:
                return {}
            try:
                data = await resp.json(content_type=None)
            except Exception:
                text = await resp.text()
                if resp.status >= 400:
                    raise RuntimeError(f"DuckMail API 错误 [{resp.status}]: {text[:200]}")
                return {}
            if resp.status >= 400:
                msg = data.get("message", data.get("error", str(data))) if isinstance(data, dict) else str(data)
                raise RuntimeError(f"DuckMail API 错误 [{resp.status}]: {msg}")
            return data

    async def get_domains(self) -> list[str]:
        """获取可用域名列表"""
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        result = await self._request("GET", "/domains", headers=headers)
        domains = []
        if isinstance(result, dict):
            for item in result.get("hydra:member", []):
                if isinstance(item, dict) and "domain" in item:
                    domains.append(item["domain"])
                elif isinstance(item, str):
                    domains.append(item)
        return domains

    async def create_temp_email(self, username: str | None = None) -> str | None:
        """创建临时邮箱（完整流程：选域名→创建账号→获取 token）

        Returns:
            邮箱地址，失败返回 None
        """
        try:
            # 选择域名
            domain = config.DUCKMAIL_DOMAIN
            try:
                domains = await self.get_domains()
                if domains:
                    domain = random.choice(domains)
            except Exception:
                pass

            # 生成用户名和密码
            if not username:
                username = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
            self.email = f"{username}@{domain}"
            self.password = "".join(random.choices(string.ascii_letters + string.digits, k=12))

            # 创建账号（用 API key 认证）
            payload = {"address": self.email, "password": self.password}
            headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
            result = await self._request("POST", "/accounts", headers=headers, json=payload)
            self.account_id = result.get("id", "")

            # 获取账号 token（替换认证身份）
            token_result = await self._request("POST", "/token", headers=headers, json=payload)
            self.token = token_result.get("token", "")
            if not self.token:
                raise RuntimeError(f"获取 token 失败: {token_result}")

            logger.info("邮箱就绪: %s", self.email)
            return self.email

        except Exception as e:
            logger.error("邮箱申请异常: %s", e)
            return None

    async def get_emails(self) -> list[dict]:
        """获取收件箱邮件列表（含详情）"""
        if not self.token:
            return []
        try:
            headers = {"Authorization": f"Bearer {self.token}"}
            result = await self._request("GET", "/messages", headers=headers, params={"page": 1})

            messages = []
            if isinstance(result, dict):
                raw_list = result.get("hydra:member", [])
            elif isinstance(result, list):
                raw_list = result
            else:
                return []

            for msg in raw_list:
                msg_id = msg.get("id", "")
                if not msg_id:
                    continue
                detail = await self._get_message_detail(msg_id)
                if not detail:
                    continue
                html = detail.get("html", [])
                messages.append({
                    "id": detail.get("id", msg_id),
                    "subject": detail.get("subject", ""),
                    "text_content": detail.get("text", "") or "",
                    "html_content": "".join(html) if isinstance(html, list) else str(html),
                })
            return messages

        except Exception as e:
            logger.error("获取邮件异常: %s", e)
            return []

    async def _get_message_detail(self, msg_id: str) -> dict | None:
        """获取单封邮件详情"""
        try:
            headers = {"Authorization": f"Bearer {self.token}"}
            return await self._request("GET", f"/messages/{msg_id}", headers=headers)
        except Exception:
            return None

    async def wait_verification_code(self, pattern: str = r"(\d{6})", timeout: int = 0) -> str | None:
        """轮询收件箱，提取验证码

        Args:
            pattern: 验证码正则表达式
            timeout: 超时秒数，0 使用配置值

        Returns:
            验证码字符串，超时返回 None
        """
        timeout = timeout or config.EMAIL_POLL_TIMEOUT
        interval = config.EMAIL_POLL_INTERVAL
        logger.info("等待验证码: %s", self.email)

        start = time.time()
        seen: set[str] = set()

        while time.time() - start < timeout:
            for msg in await self.get_emails():
                mid = msg.get("id", "")
                if mid in seen:
                    continue
                seen.add(mid)

                content = msg.get("text_content", "") + " " + msg.get("html_content", "")
                # 去除 HTML 标签
                content = re.sub(r"<[^<]+?>", " ", content)
                match = re.search(pattern, content, re.IGNORECASE)
                if match:
                    code = match.group(1) if match.groups() else match.group(0)
                    logger.info("验证码: %s", code)
                    return code
            await asyncio.sleep(interval)

        logger.error("验证码等待超时")
        return None

    async def delete_account(self) -> None:
        """删除临时邮箱账号"""
        if not self.token or not self.account_id:
            return
        try:
            headers = {"Authorization": f"Bearer {self.token}"}
            await self._request("DELETE", f"/accounts/{self.account_id}", headers=headers)
            logger.info("已删除临时邮箱: %s", self.email)
        except RuntimeError as e:
            logger.warning("删除临时邮箱失败: %s", e)
