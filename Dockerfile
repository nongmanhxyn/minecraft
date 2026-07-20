FROM python:3.11-slim

# Cài Node.js 20
RUN apt-get update && apt-get install -y curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean

WORKDIR /app

# Cài dependencies Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Cài dependencies Node.js
COPY package.json .
RUN npm install

# Copy toàn bộ code
COPY . .

# Port 8000 sẽ được Railway tự động map qua biến PORT
EXPOSE 8000

CMD ["python", "main.py"]
