# Build from the project root so the console reuses the agent's trust_fabric package:
#   az acr build -r <acr> -t trust-console:<tag> -f console/Dockerfile .
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY console/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# trust_fabric resolves its dataset from /app/data, which console/data provides (same generated files).
COPY src/payments-trust-fabric/trust_fabric ./trust_fabric
COPY console/ ./

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
