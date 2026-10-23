FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv
RUN pip install --no-cache-dir tzdata
COPY simulator.py .
CMD ["python", "simulator.py"]
