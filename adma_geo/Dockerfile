FROM python:3.10-slim-bookworm

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y \
    libpq-dev \
    postgresql-client \
    build-essential \
    libmagic1 \
    gdal-bin \
    libgdal-dev \
    libgeos-dev \
    libproj-dev \
    proj-data \
    proj-bin \
    libspatialindex-dev \
    && rm -rf /var/lib/apt/lists/*

# Set work directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip uninstall -y opencv-python && pip install --no-cache-dir opencv-python-headless

# ssh client: Ask ADMA runs jobs on HCC Swan through each user's own login
RUN apt-get update && (apt-get install -y --no-install-recommends openssh-client || (sed -i '/debian-security/d' /etc/apt/sources.list && apt-get update && apt-get install -y --no-install-recommends openssh-client)) && rm -rf /var/lib/apt/lists/*

# Copy project
COPY . .

# Create necessary directories
RUN mkdir -p /app/media/uploads /app/staticfiles

# Collect static files (initial collection during build)
RUN python manage.py collectstatic --noinput

# Expose port
EXPOSE 8000

# Default command (overridden by docker-compose)
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
