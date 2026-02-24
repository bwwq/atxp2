FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir aiohttp>=3.9.0

COPY api_server.py .

RUN mkdir -p /app/data

EXPOSE 8741

ENTRYPOINT ["python", "api_server.py"]
CMD ["-a", "/app/data/accounts.json", "-p", "8741"]
