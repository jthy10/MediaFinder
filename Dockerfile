FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mediafinder ./mediafinder
ENV DB_PATH=/data/mediafinder.db
VOLUME /data
EXPOSE 8080
CMD ["uvicorn", "mediafinder.app:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8080"]
