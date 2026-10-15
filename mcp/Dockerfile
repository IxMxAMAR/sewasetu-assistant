FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# One image, two servers. Which one this container is comes from the command in
# docker-compose.yml, because the difference between them is configuration --
# a role name, a port, an audience -- and not code.
ENV PYTHONPATH=/app
EXPOSE 8101 8102
CMD ["python", "-m", "sewasetu_mcp", "citizen"]
