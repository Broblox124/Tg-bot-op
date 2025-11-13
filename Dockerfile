# Dockerfile — stable runtime with aria2 + Python 3.10
FROM python:3.10-slim

ENV DEBIAN_FRONTEND=noninteractive
# show python output immediately in logs
ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Install system deps including aria2
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      aria2 ca-certificates curl build-essential git libffi-dev libssl-dev pkg-config && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy requirements first for caching
COPY requirements.txt /app/requirements.txt

# Create and use venv at /opt/venv
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Upgrade pip + install requirements
RUN python -m pip install --upgrade pip setuptools wheel
RUN if [ -f /app/requirements.txt ]; then python -m pip install --no-cache-dir -r /app/requirements.txt; fi

# Copy application files
COPY . /app

# Create .venv symlink so start.sh (which expects .venv) works unchanged
RUN ln -s /opt/venv /app/.venv || true

# Ensure scripts are executable
RUN chmod +x /app/start.sh /app/env_builder.sh || true

# (Optional) Create a non-root user for better security (uncomment if you want)
# RUN useradd -m -s /bin/bash appuser && chown -R appuser:appuser /app
# USER appuser

# Default command: run our env builder which will create config.env then exec start.sh
CMD ["bash", "/app/env_builder.sh"]
