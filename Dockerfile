FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p data knowledge config/tenants
EXPOSE 8000
# Railway injects $PORT dynamically — use it, fallback to 8000 locally
CMD ["sh", "-c", "uvicorn agent.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
