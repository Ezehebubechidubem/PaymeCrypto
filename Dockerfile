# Start from official Python 3 image
FROM python:3.13-slim

# Set working directory
WORKDIR /app

# Copy all files
COPY . /app

# Install system dependencies for building Python packages
RUN apt-get update && \
    apt-get install -y gcc build-essential && \
    rm -rf /var/lib/apt/lists/*

# Upgrade pip
RUN pip install --upgrade pip

# Install Python dependencies
RUN pip install -r requirements.txt

# Expose port 10000 for Render
EXPOSE 10000

# Start your app
CMD ["gunicorn", "-b", "0.0.0.0:10000", "app:app"]