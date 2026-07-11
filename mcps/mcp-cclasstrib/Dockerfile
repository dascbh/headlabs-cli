FROM --platform=linux/arm64 python:3.12-slim
ENV MCP_ID=mcp-cclasstrib PORT=8000 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY . /app
EXPOSE 8000
CMD ["python", "server.py"]
