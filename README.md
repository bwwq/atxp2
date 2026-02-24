# ATXP 2API

将 chat.atxp.ai 账号转换为 OpenAI 兼容 API，支持流式/非流式响应。

## 特性

- OpenAI 兼容接口（`/v1/chat/completions`、`/v1/models`）
- 多账号轮询，自动 token 刷新与轮换
- SSE 流式响应
- 可选 API Key 认证
- Docker 一键部署

## 快速开始

### Docker 部署（推荐）

1. 准备账号文件：

```bash
mkdir data
cp data/accounts.example.json data/accounts.json
# 编辑 data/accounts.json，填入真实账号
```

2. 启动服务：

```bash
docker compose up -d
```

带 API Key 认证：

```bash
API_KEY=sk-your-key docker compose up -d
```

自定义端口：

```bash
PORT=9000 docker compose up -d
```

### 本地运行

```bash
pip install aiohttp
python api_server.py -a data/accounts.json
```

完整参数：

```
python api_server.py -p 8080 -a accounts.json --api-key sk-mykey
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `-p, --port` | 8741 | 监听端口 |
| `-a, --accounts` | results/accounts.json | 账号文件路径 |
| `--host` | 0.0.0.0 | 监听地址 |
| `--api-key` | 无 | API Key（留空则无需认证） |

## API 接口

### POST /v1/chat/completions

兼容 OpenAI Chat Completions API。

```bash
curl http://localhost:8741/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-your-key" \
  -d '{
    "model": "claude-sonnet-4-6",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": true
  }'
```

### GET /v1/models

返回可用模型列表。

```bash
curl http://localhost:8741/v1/models
```

### GET /status

查看账号池状态（无需认证）。

```bash
curl http://localhost:8741/status
```

## 支持的模型

ATXP 端点支持 Anthropic 模型，请求时会自动添加 `anthropic/` 前缀：

- `claude-opus-4-6`
- `claude-sonnet-4-6`
- 其他 Anthropic 模型

## 账号文件格式

```json
[
  {
    "email": "user@example.com",
    "refresh_token": "eyJ..."
  }
]
```

可通过 `register.py` 批量注册获取账号：

```bash
pip install -r requirements.txt
playwright install chromium
python register.py -n 10 -c 5
```

## 在第三方客户端中使用

将 API Base URL 设置为 `http://your-server:8741/v1`，填入对应 API Key 即可接入支持 OpenAI 格式的客户端（ChatGPT-Next-Web、LobeChat 等）。
