FROM python:3.12-alpine
RUN apk add --no-cache docker-cli
COPY admin.py /app/admin.py
WORKDIR /app
CMD ["python", "admin.py"]
