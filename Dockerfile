FROM python:3.12-slim

WORKDIR /app

COPY requirements-cloud.txt .
RUN pip install --no-cache-dir -r requirements-cloud.txt

COPY . .

COPY start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 5000

ENTRYPOINT ["/start.sh"]
