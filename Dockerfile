FROM python:3.12

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8888

CMD ["python", "server.py"]

# Build immagine
#docker build -t torneo-app .

# Avvio container (porta 8888)
#docker run --rm -p 8888:8888 torneo-app

# (opzionale) verifiche
#docker images
#docker ps


