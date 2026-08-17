FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m copilot download-runtime

COPY . .
ENV COPILOT_CLI_EXTRACT_DIR=/tmp/copilot-cli
EXPOSE 8088
CMD ["python", "main.py"]
