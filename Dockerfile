FROM python:3.12-slim

WORKDIR /app

COPY requirements-cloud.txt .
RUN pip install --no-cache-dir -r requirements-cloud.txt

COPY . .

EXPOSE 5000

CMD ["/bin/sh", "-c", "gunicorn --worker-class eventlet -w 1 --bind 0.0.0.0:$PORT cloud.server:app"]
