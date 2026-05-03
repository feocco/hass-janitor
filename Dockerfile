FROM python:3.13-alpine

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY hass_janitor /app/hass_janitor

ENV SERVICE_HOST=0.0.0.0
ENV SERVICE_PORT=8092
ENV AUDIT_PATH=/app/logs/ha-update-audit.md

EXPOSE 8092

CMD ["python", "-m", "hass_janitor.service"]
