FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pic ./pic
COPY web/dist ./web/dist
COPY evaluation ./evaluation

EXPOSE 8000
CMD ["uvicorn", "pic.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
