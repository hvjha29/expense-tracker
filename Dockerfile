FROM python:3.12-slim

# Set environment variables to prevent Python from writing .pyc files
# and to ensure stdout/stderr are unbuffered so logs appear immediately.
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first to leverage Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Expose the default FastMCP port
EXPOSE 8000

# Run the MCP server
CMD ["python", "main.py"]
