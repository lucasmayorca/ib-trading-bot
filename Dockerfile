FROM python:3.12-slim

WORKDIR /app

COPY requirements-cloud.txt .
RUN pip install --no-cache-dir -r requirements-cloud.txt

COPY . .

EXPOSE 5000

CMD gunicorn --worker-class eventlet -w 1 --bind 0.0.0.0:${PORT:-5000} cloud.server:app
