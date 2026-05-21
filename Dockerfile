FROM python:3.13-slim
WORKDIR /app
COPY cleanup-notify.py status-server.py dedup-scan.py trickplay-scan.py cleanup-scan.py logo.png ./
EXPOSE 5380
CMD ["python", "status-server.py"]
