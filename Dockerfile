# Use Python 3.11 slim image as base
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY juliana_bot_2.py .
COPY system_prompt_for_script_2.txt .

# Expose port 5000
EXPOSE 5000

# Set Python path and run the application with gunicorn
ENV PYTHONPATH=/app
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--chdir", "/app", "juliana_bot_2:app"]
