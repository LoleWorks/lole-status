FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Europe/Chisinau
WORKDIR /app
RUN pip install --no-cache-dir requests==2.32.3
COPY hub.py .
RUN mkdir -p /data && chown nobody /data
USER nobody
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"
CMD ["python", "hub.py", "serve"]
