# Use a lightweight Python 3.11 image
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Copy all your project files into the container
COPY . /app

# Upgrade pip and install Cython first
RUN pip install --upgrade pip
RUN pip install Cython

# Install your Python dependencies
RUN pip install -r requirements.txt

# Expose port 10000 (Render uses this for HTTP services)
EXPOSE 10000

# Run your app with gunicorn
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:10000"]