FROM python:3.12-slim

WORKDIR /app

COPY requirements-cloud.txt .
RUN pip install --no-cache-dir -r requirements-cloud.txt

COPY . .

EXPOSE 5000

CMD ["python", "-m", "cloud.server"]
