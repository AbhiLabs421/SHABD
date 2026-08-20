# One image, used by every service — each container just runs a different main.py.
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# SERVICE env selects which service this container runs (see docker-compose).
ENV SERVICE=gateway
CMD ["sh", "-c", "python services/${SERVICE}_service/main.py 2>/dev/null || python services/${SERVICE}/main.py"]
