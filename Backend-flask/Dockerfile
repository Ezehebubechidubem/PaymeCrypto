# Use Python slim image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies (needed for compiling some packages)
RUN apt-get update && \
    apt-get install -y build-essential gcc && \
    rm -rf /var/lib/apt/lists/*

# Copy project files
COPY . /app

# Upgrade pip
RUN pip install --upgrade pip

# Install Python dependencies
RUN pip install -r requirements.txt

# Expose port 10000 for Render
EXPOSE 10000

# Command to run the app
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:10000", "app:app"]