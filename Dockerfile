FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps . && groupadd --gid 10001 social && useradd --uid 10001 --gid 10001 --create-home social && mkdir -p /app/data && chown -R social:social /app
USER social
EXPOSE 8000
CMD ["uvicorn", "social.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
